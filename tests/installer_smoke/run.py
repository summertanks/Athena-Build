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
from pathlib import Path

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


def build_qemu_cmd(
    iso_path: str,
    serial_log: str,
    disk_img: str,
    *,
    mode: str = 'bios',
    mem_mb: int = _DEFAULT_MEM_MB,
    kernel_append: 'str | None' = None,
) -> 'list[str]':
    """Build the qemu-system-x86_64 argv.

    mode='bios' uses SeaBIOS (the QEMU default).  mode='efi' uses
    OVMF if the package is installed at /usr/share/OVMF/OVMF_CODE.fd
    — apt-install ovmf to get it.

    Serial is captured to `serial_log` (no graphical console).  This
    matches what /lib/debian-installer-startup.d/S99-syslog-to-serial
    in our installer ramdisk expects: tails /var/log/syslog to
    /dev/ttyS0, which we route to the file.

    Returns argv as list[str] — caller subprocess.Popen's it.
    """
    _qemu = _need_tool('qemu-system-x86_64')
    _cmd = [
        _qemu,
        '-m', str(mem_mb),
        '-nographic',                          # serial only; no SDL/GTK window
        '-cdrom', iso_path,
        '-drive', f'file={disk_img},format=qcow2,if=virtio',
        '-boot', 'd',                          # boot from CD-ROM first
        '-serial', f'file:{serial_log}',
        '-monitor', 'none',                    # no QEMU monitor — keeps stdin clean
    ]
    if shutil.which('kvm-ok'):
        # Optional accelerator — speeds full installs by ~10×.  Fails
        # silently if /dev/kvm not accessible; QEMU falls back to TCG.
        _cmd[1:1] = ['-enable-kvm']
    if mode == 'efi':
        _ovmf_code = '/usr/share/OVMF/OVMF_CODE.fd'
        if not os.path.isfile(_ovmf_code):
            _err(
                f'mode=efi requires {_ovmf_code} '
                f'(apt-get install ovmf)'
            )
        _cmd[1:1] = ['-drive', f'if=pflash,format=raw,readonly=on,file={_ovmf_code}']
    if kernel_append:
        # When the harness drives an unattended install, we override
        # the boot kernel cmdline to point at our preseed and force
        # console=ttyS0 so output lands in serial_log.
        _cmd.extend(['-append', kernel_append])
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
    mode: str = 'bios',
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

    _kernel_append: 'str | None' = None
    if full:
        # Operator NOTE: this requires preseed.cfg to be tuned for
        # fully-unattended partman + base-install.  See preseed.cfg
        # header for the required answers.  Until that's done, --full
        # will time out at the first un-answered prompt.
        _kernel_append = (
            'auto=true priority=critical console=ttyS0,115200n8 '
            'preseed/file=/cdrom/preseed.cfg'   # uses the in-ISO preseed
            # OR fetch ours via http://10.0.2.2:8080/preseed.cfg —
            # see _serve_preseed_http below (not yet wired).
        )

    _cmd = build_qemu_cmd(
        iso_path, _serial_log, _disk_img,
        mode=mode, mem_mb=mem_mb, kernel_append=_kernel_append,
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
        # Stock d-i emits this near the end of finish-install.d
        # — match in --full mode to short-circuit the timeout.
        'Installation complete' if full else None
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


def main() -> 'None':
    _ap = argparse.ArgumentParser(
        prog='installer-smoke',
        description='Boot installer ISO under QEMU + scan serial log for regressions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__ or '').rstrip(),
    )
    _ap.add_argument('--iso', required=True, help='Path to installer ISO')
    _ap.add_argument('--output-dir', default='/tmp/installer-smoke',
                     help='Where to write serial log + disk image (default: /tmp/installer-smoke)')
    _ap.add_argument('--mode', choices=('bios', 'efi'), default='bios',
                     help='Firmware mode (default: bios)')
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
