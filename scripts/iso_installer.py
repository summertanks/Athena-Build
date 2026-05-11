"""COMP-01b phase 7: master the installer ISO from buildroot/installer/.

The installer chroot becomes the initrd as a monolithic cpio.gz (no
separate squashfs layer in v1).  Kernel comes from the linux-image-amd64
.deb in repo/ — same kernel that ships on the live ISO.  The apt pool
(repo/) is bundled onto the ISO so the installer reads packages from
/cdrom/pool at install time (matches the locked COMP-01b decision: no
Debian repo fallback ever).  grub-mkrescue produces the hybrid BIOS+EFI
bootable image.

Reference: docs/plans/comp-01-installer.md; project memory
project_installer_from_source.md.

Engine surface mirrors installer_chroot.py: a single top-level
build_installer_iso() that orchestrates a sequence of small helpers,
each of which handles one ISO mastering step and returns bool.  All
data-layer files live under installer/; this module reads from them
but never inspects content.
"""

import glob
import logging
import os
import re
import shutil
import subprocess
from typing import Optional

import tui

logger = logging.getLogger('athena')


# Real kernel packages carry a numeric ABI in the package name:
#   linux-image-6.1.0-47-amd64_6.1.170-3_amd64.deb
# Metapackages do NOT — they're empty + just Depends: on the real one:
#   linux-image-amd64_6.1.170-3_amd64.deb         (meta — vanilla flavor)
#   linux-image-rt-amd64_6.1.170-3_amd64.deb      (meta — rt preempt flavor)
#   linux-image-cloud-amd64_6.1.170-3_amd64.deb   (meta — cloud flavor)
# We only want files matching the numeric-ABI pattern and the plain
# amd64 flavor (no -rt-, -cloud-, -trunk-, -dbg- suffix).
_KERNEL_PKG_RE = re.compile(
    r'^linux-image-(\d+\.\d+\.\d+-\d+)-amd64_'
)


def build_installer_iso(
    dir_chroot_installer: str,
    dir_repo: str,
    dir_image: str,
    installer_dir: str,
    password: str,
    iso_basename: str,
) -> bool:
    """Build the installer ISO end to end.

    Args:
        dir_chroot_installer: Path to the unpacked installer chroot
                              (the buildroot/installer/ produced by
                              cmd_build_chroot_installer).
        dir_repo:             Path to repo/ containing built .debs + .udebs
                              — bundled onto the ISO so the installer can
                              apt-pull from /cdrom/pool at install time.
        dir_image:            Output directory for the ISO.
        installer_dir:        Path to the installer/ data-layer tree
                              (grub.cfg + future boot assets live here).
        password:             Cached sudo password — needed for cpio of
                              root-owned chroot content and for copying
                              the pool.
        iso_basename:         Filename for the produced ISO (caller chooses
                              based on config.build_version etc.).

    Returns True on success, False on any unrecoverable error.  All
    errors are logged AND printed to the console.
    """
    _staging = os.path.join(dir_image, 'staging-installer')

    if not _prepare_staging(_staging, password):
        return False

    _kernel_src = _find_kernel(dir_repo, dir_chroot_installer, password)
    if not _kernel_src:
        return False
    if not _stage_kernel(_kernel_src, _staging):
        return False

    if not _build_initrd(dir_chroot_installer, _staging, password):
        return False

    if not _stage_grub_cfg(_staging, installer_dir):
        return False

    if not _stage_disk_info(_staging, installer_dir):
        return False

    if not _stage_pool(dir_repo, _staging, password):
        return False

    _iso_path = os.path.join(dir_image, iso_basename)
    if not _run_grub_mkrescue(_staging, _iso_path):
        return False

    _report_iso(_iso_path)
    return True


# ---------------------------------------------------------------------------
# Helpers — one per mastering step
# ---------------------------------------------------------------------------


def _sudo(cmd_args, password: str) -> subprocess.CompletedProcess:
    """Run `sudo -S <cmd>` with the cached password.  Captured output."""
    return subprocess.run(
        ['sudo', '-S'] + cmd_args,
        input=password + '\n',
        capture_output=True, text=True,
    )


def _prepare_staging(staging: str, password: str) -> bool:
    """Wipe + recreate the staging tree (boot/, boot/grub/, pool/) so
    repeated `iso build installer` runs start clean.

    sudo rm — the staging tree from a previous run may contain root-owned
    artefacts copied from the chroot or pool.  Re-creation uses plain
    mkdir so the top-level stays user-owned (same pattern as
    installer_chroot._wipe_and_create — see comment there).
    """
    tui.console.print(f"Wiping staging tree {staging}...")
    _r = _sudo(['rm', '-rf', staging], password)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: failed to wipe {staging}: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_prepare_staging rm -rf {staging}: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    try:
        os.makedirs(os.path.join(staging, 'boot', 'grub'), exist_ok=True)
    except OSError as e:
        tui.console.print(f"ERROR: mkdir {staging}/boot/grub: {e}")
        logger.error(f"_prepare_staging mkdir: {e}")
        return False
    return True


def _find_kernel(dir_repo: str, dir_chroot_installer: str,
                 password: str) -> Optional[str]:
    """Locate a usable vmlinuz.

    Strategy:
      1. Look for vmlinuz under dir_chroot_installer/boot/ — if a
         kernel-image-*-di udeb unpacked one, use it.  Self-contained.
      2. Fall back to extracting from repo/linux-image-*-amd64*.deb.
         dpkg-deb -x extracts the .deb into a temp dir; we pull vmlinuz
         out of that.  Same kernel that ships on the live ISO.

    Returns absolute path to vmlinuz on success, None if neither
    strategy yields one.
    """
    # Strategy 1: chroot's boot dir
    _candidates = sorted(glob.glob(
        os.path.join(dir_chroot_installer, 'boot', 'vmlinuz-*')))
    if _candidates:
        _k = _candidates[-1]
        tui.console.print(
            f"Kernel found in installer chroot: {os.path.basename(_k)}"
        )
        return _k

    # Strategy 2: extract from a linux-image deb in repo/.  Filter the
    # glob to packages with a numeric ABI in the name (real kernels)
    # and only the plain amd64 flavor.  This excludes:
    #   - meta packages (linux-image-amd64, linux-image-rt-amd64,
    #     linux-image-cloud-amd64) — empty .debs, no vmlinuz
    #   - non-vanilla flavors (-rt-, -cloud-, etc.) — work but we want
    #     the plain kernel for a generic installer
    #   - debug packages (-dbg-) — symbols, no vmlinuz
    _all_linux_debs = sorted(glob.glob(
        os.path.join(dir_repo, 'linux-image-*-amd64*.deb')))
    _linux_debs = [
        _d for _d in _all_linux_debs
        if _KERNEL_PKG_RE.match(os.path.basename(_d))
        and 'dbg' not in os.path.basename(_d).lower()
    ]
    if not _linux_debs:
        tui.console.print(
            "ERROR: no kernel found.  Looked in:\n"
            f"  {dir_chroot_installer}/boot/vmlinuz-*\n"
            f"  {dir_repo}/linux-image-<ABI>-amd64*.deb\n"
            "Is the linux-image package built and in repo/?"
        )
        if _all_linux_debs:
            tui.console.print(
                f"  ({len(_all_linux_debs)} non-matching linux-image-* "
                "candidates exist but are meta/flavor packages with no vmlinuz)"
            )
        logger.error(
            f"_find_kernel: no numeric-ABI kernel package found; "
            f"non-matching candidates: "
            f"{[os.path.basename(d) for d in _all_linux_debs]}"
        )
        return None

    # Pick the highest ABI version.  Sort key extracts the ABI tuple from
    # the package name so '6.1.0-47' > '6.1.0-9' lexicographically wrong
    # would otherwise be a hazard — but with consistent numeric padding
    # in Debian's ABI naming, sort-on-name is fine.  Use the last entry.
    _deb = _linux_debs[-1]
    tui.console.print(f"Extracting kernel from {os.path.basename(_deb)}...")

    # Extract under a /tmp work dir.  dpkg-deb -x is non-destructive and
    # doesn't need root.
    _extract_dir = os.path.join('/tmp', 'athena-installer-kernel-extract')
    if os.path.exists(_extract_dir):
        # Leftover from a prior run.
        _r = _sudo(['rm', '-rf', _extract_dir], password)
        if _r.returncode != 0:
            logger.warning(
                f"_find_kernel: failed to clear {_extract_dir}: {_r.stderr.strip()}"
            )
    try:
        os.makedirs(_extract_dir, exist_ok=True)
    except OSError as e:
        tui.console.print(f"ERROR: mkdir {_extract_dir}: {e}")
        logger.error(f"_find_kernel mkdir {_extract_dir}: {e}")
        return None

    _r = subprocess.run(
        ['dpkg-deb', '-x', _deb, _extract_dir],
        capture_output=True, text=True,
    )
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: dpkg-deb -x failed: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_find_kernel dpkg-deb -x {_deb}: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return None
    _extracted = sorted(glob.glob(
        os.path.join(_extract_dir, 'boot', 'vmlinuz-*')))
    if not _extracted:
        tui.console.print(
            f"ERROR: {os.path.basename(_deb)} extracted but no vmlinuz under "
            f"{_extract_dir}/boot/"
        )
        logger.error(
            f"_find_kernel: no vmlinuz-* under {_extract_dir}/boot after "
            f"dpkg-deb -x {_deb}"
        )
        return None
    _k = _extracted[-1]
    tui.console.print(f"Kernel extracted: {os.path.basename(_k)}")
    return _k


def _stage_kernel(kernel_src: str, staging: str) -> bool:
    """Copy vmlinuz into staging/boot/ with the conventional name."""
    _dst = os.path.join(staging, 'boot', 'vmlinuz')
    try:
        shutil.copy2(kernel_src, _dst)
    except OSError as e:
        tui.console.print(f"ERROR: copy kernel: {e}")
        logger.error(f"_stage_kernel: {e}")
        return False
    return True


def _build_initrd(dir_chroot_installer: str, staging: str,
                  password: str) -> bool:
    """Pack the installer chroot as a cpio.gz initrd.

    Reads the chroot under sudo (its contents are root-owned post-unpack),
    pipes through cpio -o -H newc, gzips, lands in staging/boot/initrd.gz.

    `cpio -o -H newc` produces the format Linux's initramfs loader
    expects.  `--quiet` suppresses the per-file chatter — full transcript
    available in the file log via stderr capture if needed.
    """
    _initrd = os.path.join(staging, 'boot', 'initrd.gz')
    tui.console.print(
        f"Building monolithic initrd from {dir_chroot_installer}..."
    )
    # `find . -print0 | cpio --null -o -H newc | gzip > initrd.gz`
    # Run as a single shell pipeline under sudo so cpio can read the
    # root-owned files.  cd into the chroot so paths inside the cpio are
    # relative to /.
    _shell_cmd = (
        f"cd {dir_chroot_installer} && "
        f"find . -print0 | cpio --null -o -H newc --quiet | "
        f"gzip -9 > {_initrd}"
    )
    _r = _sudo(['bash', '-c', _shell_cmd], password)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: cpio|gzip pipeline failed (rc={_r.returncode}): "
            f"{_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_build_initrd cpio|gzip: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    # cpio writes the file as root; chown back to the running user so
    # later operations (grub-mkrescue) can read without sudo.
    _r = _sudo(['chown', f'{os.getuid()}:{os.getgid()}', _initrd], password)
    if _r.returncode != 0:
        logger.warning(
            f"_build_initrd chown {_initrd}: {_r.stderr.strip()}"
        )
    try:
        _size_mb = os.path.getsize(_initrd) // (2 ** 20)
        tui.console.print(f"Initrd built: {_size_mb} MB")
    except OSError:
        pass
    return True


def _stage_grub_cfg(staging: str, installer_dir: str) -> bool:
    """Copy installer/boot/grub.cfg → staging/boot/grub/grub.cfg.

    If the operator hasn't provided a grub.cfg (file absent), this is an
    error — without it grub-mkrescue produces an unusable ISO with no
    boot entries.  v1 ships a default; the operator can edit but not
    delete it.
    """
    _src = os.path.join(installer_dir, 'boot', 'grub.cfg')
    if not os.path.exists(_src):
        tui.console.print(
            "ERROR: installer/boot/grub.cfg is missing.  Without a "
            "boot config the ISO has no menu entries."
        )
        logger.error(f"_stage_grub_cfg: {_src} absent")
        return False
    _dst = os.path.join(staging, 'boot', 'grub', 'grub.cfg')
    try:
        shutil.copy2(_src, _dst)
    except OSError as e:
        tui.console.print(f"ERROR: copy grub.cfg: {e}")
        logger.error(f"_stage_grub_cfg: {e}")
        return False
    tui.console.print(f"Boot menu: {_src} → boot/grub/grub.cfg")
    return True


def _stage_disk_info(staging: str, installer_dir: str) -> bool:
    """Copy installer/disk/* → staging/.disk/* (excluding *.md READMEs).

    These files are d-i's "is this an installer disc?" marker convention:
    cdrom-detect rejects a disc without /cdrom/.disk/info; base-installer
    looks for /cdrom/.disk/base_installable before debootstrapping;
    /cdrom/.disk/base_components tells base-installer which debootstrap
    components are in /cdrom/pool/.

    Walks installer/disk/ at iso-build time (the engine doesn't bake any
    contents in code).  Skips *.md so the README doesn't end up on the
    ISO.  If installer/disk/ is absent, that's a hard error — cdrom-
    detect would silently reject the disc; better to fail loud at
    iso-build than have the operator boot and see "No installation
    media".
    """
    _src_dir = os.path.join(installer_dir, 'disk')
    if not os.path.isdir(_src_dir):
        tui.console.print(
            "ERROR: installer/disk/ is missing.  Without .disk/info on "
            "the ISO, cdrom-detect will reject the disc at boot."
        )
        logger.error(f"_stage_disk_info: {_src_dir} absent")
        return False
    _dst_dir = os.path.join(staging, '.disk')
    try:
        os.makedirs(_dst_dir, exist_ok=True)
    except OSError as e:
        tui.console.print(f"ERROR: mkdir {_dst_dir}: {e}")
        logger.error(f"_stage_disk_info mkdir: {e}")
        return False
    _shipped = 0
    for _entry in sorted(os.listdir(_src_dir)):
        if _entry.endswith('.md'):
            continue
        _src = os.path.join(_src_dir, _entry)
        _dst = os.path.join(_dst_dir, _entry)
        if not os.path.isfile(_src):
            continue
        try:
            shutil.copy2(_src, _dst)
        except OSError as e:
            tui.console.print(f"ERROR: copy {_src} → {_dst}: {e}")
            logger.error(f"_stage_disk_info copy {_src}: {e}")
            return False
        _shipped += 1
    if _shipped == 0:
        tui.console.print(
            "ERROR: installer/disk/ is empty.  At minimum installer/disk/info "
            "must exist so cdrom-detect accepts the disc."
        )
        logger.error(f"_stage_disk_info: {_src_dir} has no non-README files")
        return False
    tui.console.print(f"Disk markers: {_shipped} file(s) → .disk/")
    return True


def _stage_pool(dir_repo: str, staging: str, password: str) -> bool:
    """Copy repo/ → staging/pool/.

    The installer reads from /cdrom/pool at runtime (matches the locked
    COMP-01b decision: file:///cdrom apt source, no network repo
    fallback).  Uses cp -a to preserve modes/timestamps + sudo so any
    root-owned files in repo/ get faithful copies.

    This is the largest step by far — multi-GB pool can take several
    minutes.
    """
    _dst = os.path.join(staging, 'pool')
    _bytes = _bytes_in_dir(dir_repo)
    _mb = _bytes // (2 ** 20)
    tui.console.print(
        f"Copying apt pool ({_mb} MB) — may take a few minutes..."
    )
    _r = _sudo(['cp', '-a', dir_repo + '/.', _dst], password)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: pool copy failed: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_stage_pool cp -a {dir_repo} {_dst}: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    return True


def _bytes_in_dir(d: str) -> int:
    """Best-effort recursive size sum — for the operator-facing log line."""
    try:
        _r = subprocess.run(
            ['du', '-sb', d], capture_output=True, text=True, timeout=30,
        )
        if _r.returncode == 0:
            return int(_r.stdout.split()[0])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return 0


def _run_grub_mkrescue(staging: str, iso_path: str) -> bool:
    """Produce the hybrid BIOS+EFI bootable ISO from the staging tree.

    Same machinery as the live ISO build (iso.py:build_iso); we get
    El-Torito BIOS boot + EFI System Partition boot from a single
    invocation.  Requires grub-pc-bin + grub-efi-amd64-bin + xorriso on
    the host — already gated by build-system.sh's startup checks.
    """
    tui.console.print("Running grub-mkrescue...")
    _r = subprocess.run(
        ['grub-mkrescue', '-o', iso_path, staging],
        capture_output=True, text=True,
    )
    for _line in _r.stdout.splitlines():
        logger.debug(_line)
    for _line in _r.stderr.splitlines():
        logger.debug(_line)
    if _r.returncode != 0:
        tui.console.print(
            "ERROR: grub-mkrescue failed — see unified run log"
        )
        logger.error(
            f"_run_grub_mkrescue: rc={_r.returncode}, "
            f"stderr_tail={_r.stderr.strip().splitlines()[-3:]}"
        )
        return False
    return True


def _report_iso(iso_path: str) -> None:
    """Print final size + path."""
    try:
        _mb = os.path.getsize(iso_path) // (2 ** 20)
        tui.console.print(
            f"Installer ISO built: {iso_path} ({_mb} MB)",
            tui.COLOR_HIGHLIGHT,
        )
    except OSError as e:
        logger.warning(f"_report_iso: cannot stat {iso_path}: {e}")
