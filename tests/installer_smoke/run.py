#!/usr/bin/env python3
"""COMP-12 Phase F — installer ISO smoke harness.

Boots the latest installer ISO under QEMU, captures the serial log
for a configurable duration, then scans the log for known-bad
patterns (see known_bad_patterns.py).  Exits 0 if no fatal pattern
matched, 1 otherwise.

Designed to be the CI gate the plan doc describes — runnable
locally by the operator OR from a CI job.  Catches upstream-induced
regressions in d-i / cdebconf / main-menu / apt-cdrom-setup at
PR time instead of after release.

Two modes:

  --quick (default)
      Boot the ISO, capture serial log for --timeout seconds
      (default 120), then terminate.  Doesn't drive partman /
      base-install.  Catches "does d-i start cleanly?" — the most
      common regression class (broken initrd, missing udeb, fatal
      cdebconf failure on first dialog).  Fast (~3 min wall time).

  --full
      Boot with the preseed kernel cmdline + the harness's
      preseed.cfg supplied via a tmp HTTP server (preseed/url=).
      Waits for the installed-system boot signal.  Slow (~15-30 min
      wall time depending on package set).  REQUIRES preseed.cfg to
      be tuned for unattended partman + base-install — see the
      module-level NOTE in preseed.cfg.  When the preseed isn't
      complete the install hangs at a prompt and the harness times out.

Invocation (manual smoke):

  python3 tests/installer-smoke/run.py \\
      --iso image/athena-installer-0.1-amd64.iso \\
      --mode bios \\
      --output-dir /tmp/smoke-$(date +%s)

  # or for the boot-only quick gate:
  python3 tests/installer-smoke/run.py --iso <ISO> --quick --timeout 60

Exit codes:
  0  smoke OK — no fatal patterns matched within --timeout
  1  one or more fatal patterns matched (regression)
  2  setup failure (QEMU not installed, ISO missing, etc.)
  124 QEMU hard-timed-out without producing the expected completion
      signal (--full mode only; --quick exits 0 if no patterns)
"""

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
import time

# Import the parser as a sibling module — supports both
# `python3 tests/installer-smoke/run.py` (this dir on sys.path) and
# `python3 -m tests.installer-smoke.run` (package form).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
import known_bad_patterns


# Defaults — operator can override via CLI.
_DEFAULT_TIMEOUT_QUICK = 120        # seconds; should comfortably cover
                                     # boot + d-i first dialog
_DEFAULT_TIMEOUT_FULL  = 1800       # 30 min; covers debootstrap +
                                     # tasksel + finish-install
_DEFAULT_MEM_MB        = 2048
_DEFAULT_DISK_GB       = 8


def _err(msg: str, *, code: int = 2) -> 'None':
    """Print to stderr + exit."""
    print(f'smoke: {msg}', file=sys.stderr)
    sys.exit(code)


def _need_tool(name: str) -> str:
    """Return the absolute path of `name`, or _err if absent."""
    _path = shutil.which(name)
    if not _path:
        _err(f'`{name}` not on PATH — install it (e.g. apt-get install qemu-system-x86)')
    return _path   # type: ignore[return-value]


# OVMF firmware lives at different paths across distros / package versions.
# Modern Debian ships the 4MB build (OVMF_CODE_4M.fd); older ones the legacy
# OVMF_CODE.fd.  CODE is read-only; VARS is the writable NVRAM template that
# MUST be copied per-run (a single shared VARS would be mutated concurrently).
_OVMF_CODE_CANDIDATES = (
    '/usr/share/OVMF/OVMF_CODE_4M.fd',
    '/usr/share/OVMF/OVMF_CODE.fd',
    '/usr/share/edk2/x64/OVMF_CODE.4m.fd',
    '/usr/share/qemu/OVMF_CODE.fd',
)
_OVMF_VARS_CANDIDATES = (
    '/usr/share/OVMF/OVMF_VARS_4M.fd',
    '/usr/share/OVMF/OVMF_VARS.fd',
    '/usr/share/edk2/x64/OVMF_VARS.4m.fd',
)


def _find_ovmf() -> 'tuple[str | None, str | None]':
    """(OVMF_CODE, OVMF_VARS_template) absolute paths, or (None, None)."""
    _code = next((_p for _p in _OVMF_CODE_CANDIDATES if os.path.isfile(_p)), None)
    _vars = next((_p for _p in _OVMF_VARS_CANDIDATES if os.path.isfile(_p)), None)
    return _code, _vars


def _kvm_available() -> bool:
    """True iff /dev/kvm is usable by this user (read+write).  Without it QEMU
    falls back to TCG — correct but ~10× slower (a full install may not reach
    d-i within the --quick timeout)."""
    return os.access('/dev/kvm', os.R_OK | os.W_OK)


def extract_boot_images(iso_path: str, dest_dir: str) -> 'tuple[str, str]':
    """Extract /boot/vmlinuz + /boot/initrd.gz from the ISO (via xorriso) so
    the smoke can DIRECT-boot the installer kernel with `console=ttyS0` — the
    only reliable way to capture serial headlessly (the ISO's GRUB uses a
    gfxterm console that produces nothing under `-nographic`) AND the only way
    `-append` (preseed cmdline) actually takes effect.  Returns (vmlinuz,
    initrd)."""
    _xorriso = _need_tool('xorriso')
    _vmlinuz = os.path.join(dest_dir, 'vmlinuz')
    _initrd = os.path.join(dest_dir, 'initrd.gz')
    _r = subprocess.run(
        [_xorriso, '-osirrox', 'on', '-indev', iso_path,
         '-extract', '/boot/vmlinuz', _vmlinuz,
         '-extract', '/boot/initrd.gz', _initrd],
        capture_output=True, text=True)
    if not (os.path.isfile(_vmlinuz) and os.path.isfile(_initrd)):
        _err(f'could not extract /boot/{{vmlinuz,initrd.gz}} from {iso_path}: '
             f'{(_r.stderr or "").strip()[:200]}')
    return _vmlinuz, _initrd


def build_qemu_cmd(
    iso_path: str,
    serial_log: str,
    disk_img: str,
    *,
    mode: str = 'direct',
    mem_mb: int = _DEFAULT_MEM_MB,
    kernel: 'str | None' = None,
    initrd: 'str | None' = None,
    kernel_append: 'str | None' = None,
    ovmf_vars: 'str | None' = None,
) -> 'list[str]':
    """Build the qemu-system-x86_64 argv.

    mode='direct' (default) DIRECT-boots the extracted installer kernel+initrd
      (`-kernel`/`-initrd`/`-append`) — firmware-agnostic, reliable headless
      serial, and the preseed cmdline actually applies.  This is the installer-
      RUNTIME smoke (does d-i start + run cleanly).
    mode='bios' / 'efi' boot the ISO through the firmware/GRUB path (SeaBIOS /
      OVMF) — the firmware-BOOT smoke (does the hybrid ISO boot under this
      firmware).  efi needs OVMF + a per-run writable VARS copy (`ovmf_vars`).

    The CD-ROM is always attached: the installer reads udebs + the pool from
    /cdrom even in direct mode.  Serial is captured to `serial_log`.
    """
    _qemu = _need_tool('qemu-system-x86_64')
    _cmd = [
        _qemu,
        '-m', str(mem_mb),
        '-nographic',                          # serial only; no SDL/GTK window
        '-cdrom', iso_path,
        '-drive', f'file={disk_img},format=qcow2,if=virtio',
        '-serial', f'file:{serial_log}',
        '-monitor', 'none',                    # no QEMU monitor — keeps stdin clean
    ]
    if _kvm_available():
        _cmd[1:1] = ['-enable-kvm']
    if mode == 'direct':
        if not (kernel and initrd):
            _err('mode=direct requires an extracted kernel + initrd')
        _cmd += ['-kernel', kernel, '-initrd', initrd]
        if kernel_append:
            _cmd += ['-append', kernel_append]
    else:
        _cmd += ['-boot', 'd']                 # boot from CD-ROM (→ firmware → GRUB)
        if mode == 'efi':
            _code, _ = _find_ovmf()
            if not _code or not ovmf_vars:
                _err('mode=efi requires OVMF (apt-get install ovmf) + a '
                     'writable VARS copy')
            _cmd[1:1] = [
                '-drive',
                f'if=pflash,format=raw,unit=0,readonly=on,file={_code}',
                '-drive',
                f'if=pflash,format=raw,unit=1,file={ovmf_vars}',
            ]
    return _cmd


def make_blank_disk(path: str, size_gb: int = _DEFAULT_DISK_GB) -> 'None':
    """qemu-img create a sparse qcow2 disk for the install to land on."""
    _qemu_img = _need_tool('qemu-img')
    _r = subprocess.run(
        [_qemu_img, 'create', '-q', '-f', 'qcow2', path, f'{size_gb}G'],
        capture_output=True, text=True,
    )
    if _r.returncode != 0:
        _err(f'qemu-img create failed: {_r.stderr.strip()}')


def run_smoke(
    iso_path: str,
    output_dir: str,
    *,
    mode: str = 'direct',
    timeout_s: int = _DEFAULT_TIMEOUT_QUICK,
    full: bool = False,
    mem_mb: int = _DEFAULT_MEM_MB,
) -> int:
    """Spawn QEMU, wait up to `timeout_s`, scan the captured serial
    log, return an exit code per the module docstring.

    Returns:
      0    no fatal patterns matched
      1    one or more fatal patterns matched
      124  --full timed out without completion signal
    """
    if not os.path.isfile(iso_path):
        _err(f'ISO not found: {iso_path}')
    os.makedirs(output_dir, exist_ok=True)
    _serial_log = os.path.join(output_dir, 'serial.log')
    _disk_img   = os.path.join(output_dir, 'disk.qcow2')
    make_blank_disk(_disk_img)

    # mode='direct' extracts the kernel+initrd and boots them with
    # console=ttyS0 so serial capture works headlessly (and the preseed
    # cmdline applies in --full).  bios/efi go through the firmware/GRUB path;
    # efi needs a per-run writable OVMF VARS copy.
    _kernel: 'str | None' = None
    _initrd: 'str | None' = None
    _ovmf_vars: 'str | None' = None
    _append: 'str | None' = None
    if mode == 'direct':
        _kernel, _initrd = extract_boot_images(iso_path, output_dir)
        _append = 'console=ttyS0,115200n8 ramdisk_size=131072'
        if full:
            # The preseed is baked into the installer initrd at /preseed.cfg
            # (matches the ISO's own "Install Asgard" GRUB entry).
            _append = ('auto=true priority=critical preseed/file=/preseed.cfg '
                       + _append)
        _append += ' ---'
    elif mode == 'efi':
        _, _vars_tpl = _find_ovmf()
        if _vars_tpl:
            _ovmf_vars = os.path.join(output_dir, 'OVMF_VARS.fd')
            shutil.copyfile(_vars_tpl, _ovmf_vars)

    _cmd = build_qemu_cmd(
        iso_path, _serial_log, _disk_img,
        mode=mode, mem_mb=mem_mb,
        kernel=_kernel, initrd=_initrd,
        kernel_append=_append, ovmf_vars=_ovmf_vars,
    )
    print(f'smoke: spawning QEMU\n  argv: {" ".join(_cmd)}\n  serial: {_serial_log}')
    _proc = subprocess.Popen(
        _cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Wait up to timeout_s for either:
    #   (a) QEMU exits (clean install + auto-shutdown, or crash)
    #   (b) timeout fires (no completion signal — assume hung)
    _completion_signal = (
        # Our installer reaches finish-install.d/20final-message at the end;
        # main-menu logs the finish-install step to syslog (→ serial), so this
        # short-circuits the --full timeout once the install is wrapping up.
        'finish-install' if full else None
    )
    _start = time.time()
    _exit_via_signal = False
    try:
        while True:
            try:
                _rc = _proc.wait(timeout=2)
                # QEMU exited on its own — could be install completed,
                # or a panic.  Either way, fall through to log scan.
                print(f'smoke: QEMU exited (rc={_rc}) after '
                      f'{int(time.time() - _start)}s')
                break
            except subprocess.TimeoutExpired:
                pass
            # Poll serial log for the completion signal (--full mode).
            if _completion_signal and os.path.isfile(_serial_log):
                try:
                    with open(_serial_log, 'r', errors='replace') as fh:
                        if _completion_signal in fh.read():
                            print('smoke: completion signal found — '
                                  'terminating QEMU')
                            _exit_via_signal = True
                            break
                except OSError:
                    pass
            if time.time() - _start > timeout_s:
                if full:
                    print(f'smoke: --full timed out after {timeout_s}s '
                          f'with no completion signal — assuming hang')
                    _proc.terminate()
                    try:
                        _proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        _proc.kill()
                    return 124
                # --quick mode: timeout is the END condition, not a
                # failure.  We've captured `timeout_s` seconds of log;
                # scan it for regressions.
                print(f'smoke: --quick timeout reached ({timeout_s}s) — '
                      f'terminating QEMU and scanning captured log')
                break
    finally:
        if _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _proc.kill()

    # Scan
    print(f'smoke: scanning {_serial_log} for known-bad patterns...')
    _findings = known_bad_patterns.scan_log(_serial_log)
    if not _findings:
        print(f'smoke: OK ({_proc.returncode}; '
              f'{os.path.getsize(_serial_log)} bytes of log captured, '
              f'0 patterns matched)')
        return 0

    # Group by severity
    _fatal = [_f for _f in _findings if _f['severity'] == 'fatal']
    _warns = [_f for _f in _findings if _f['severity'] == 'warn']
    print(f'smoke: findings — fatal={len(_fatal)}, warn={len(_warns)}')
    for _f in _findings:
        print(f"  [{_f['severity']}] line {_f['line_no']}: "
              f"{_f['pattern']} — {_f['meaning']}")
        print(f"    > {_f['line'][:160]}")
    return 1 if known_bad_patterns.has_fatal(_findings) else 0


def _self_test() -> int:
    """Validate the pure argv / firmware logic WITHOUT spawning QEMU — a fast
    sanity check runnable anywhere qemu-system-x86_64 is present.  Returns 0 on
    success, 1 on an assertion failure."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as _d:
            _iso = os.path.join(_d, 'x.iso')
            open(_iso, 'w').close()
            _ser = os.path.join(_d, 's.log')
            _disk = os.path.join(_d, 'd.qcow2')
            # direct: -kernel/-initrd/-append + -cdrom, NO -boot d
            _c = build_qemu_cmd(_iso, _ser, _disk, mode='direct',
                                kernel='/k', initrd='/i',
                                kernel_append='console=ttyS0')
            assert '-kernel' in _c and '/k' in _c, _c
            assert '-initrd' in _c and '-append' in _c, _c
            assert '-cdrom' in _c and '-boot' not in _c, _c
            # bios: -cdrom + -boot d, no -kernel
            _c = build_qemu_cmd(_iso, _ser, _disk, mode='bios')
            assert '-cdrom' in _c and _c[_c.index('-boot') + 1] == 'd', _c
            assert '-kernel' not in _c, _c
            # efi: pflash unit0 (code, ro) + unit1 (vars, rw) — if OVMF present
            _code, _vars = _find_ovmf()
            if _code and _vars:
                _vcopy = os.path.join(_d, 'VARS.fd')
                shutil.copyfile(_vars, _vcopy)
                _c = build_qemu_cmd(_iso, _ser, _disk, mode='efi',
                                    ovmf_vars=_vcopy)
                _j = ' '.join(_c)
                assert 'if=pflash' in _j and 'unit=0' in _j and 'unit=1' in _j, _c
                assert 'readonly=on' in _j and _vcopy in _j, _c
                print(f'self-test: efi argv OK (OVMF: {_code})')
            else:
                print('self-test: OVMF not found — skipping efi argv check')
            print('self-test: build_qemu_cmd argv shapes OK')
        return 0
    except AssertionError as _e:
        print(f'self-test FAILED: {_e}', file=sys.stderr)
        return 1


def main() -> 'None':
    _ap = argparse.ArgumentParser(
        prog='installer-smoke',
        description='Boot installer ISO under QEMU + scan serial log for regressions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__ or '').rstrip(),
    )
    _ap.add_argument('--iso', help='Path to installer ISO (required unless '
                                   '--self-test)')
    _ap.add_argument('--self-test', action='store_true',
                     help='Validate the argv/firmware logic without QEMU + exit')
    _ap.add_argument('--output-dir', default='/tmp/installer-smoke',
                     help='Where to write serial log + disk image (default: /tmp/installer-smoke)')
    _ap.add_argument('--mode', choices=('direct', 'bios', 'efi'),
                     default='direct',
                     help='direct = boot the extracted installer kernel+initrd '
                          '(reliable headless serial; installer-runtime smoke). '
                          'bios/efi = boot the ISO via firmware/GRUB (firmware-'
                          'boot smoke; efi needs OVMF).  Default: direct')
    _ap.add_argument('--timeout', type=int, default=None,
                     help=f'Max seconds before forcing QEMU exit '
                          f'(default: {_DEFAULT_TIMEOUT_QUICK} for --quick, '
                          f'{_DEFAULT_TIMEOUT_FULL} for --full)')
    _ap.add_argument('--mem-mb', type=int, default=_DEFAULT_MEM_MB,
                     help=f'Guest RAM in MB (default: {_DEFAULT_MEM_MB})')
    _g = _ap.add_mutually_exclusive_group()
    _g.add_argument('--quick', action='store_true', default=True,
                    help='Boot + capture for --timeout seconds + scan (default)')
    _g.add_argument('--full', action='store_true',
                    help='Drive unattended install via preseed; '
                         'requires preseed.cfg to be tuned (see file header)')
    args = _ap.parse_args()

    if args.self_test:
        sys.exit(_self_test())
    if not args.iso:
        _ap.error('--iso is required (unless --self-test)')

    _timeout = args.timeout
    if _timeout is None:
        _timeout = _DEFAULT_TIMEOUT_FULL if args.full else _DEFAULT_TIMEOUT_QUICK

    _code = run_smoke(
        iso_path=args.iso,
        output_dir=args.output_dir,
        mode=args.mode,
        timeout_s=_timeout,
        full=args.full,
        mem_mb=args.mem_mb,
    )
    sys.exit(_code)


if __name__ == '__main__':
    main()
