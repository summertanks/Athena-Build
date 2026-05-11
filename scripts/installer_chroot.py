"""COMP-01b phase 5: build the installer chroot from the udeb closure.

Unlike the live/installed chroot (apt + dpkg with mounting + maintainer
scripts run at build time), the installer chroot is just a directory tree
of UNPACKED udebs.  Maintainer scripts (postinst) are deferred to first
boot — rootskel's init scripts + main-menu drive them via udpkg.

The build engine does NOT inspect configuration content under installer/;
it only copies files per the table in installer/README.md.  Operator
rebrand/preseed changes live entirely in installer/, never here.

Reference: docs/plans/comp-01-installer.md; project memory
project_installer_from_source.md.
"""

import logging
import os
import subprocess
from typing import List

import tui
import utils

logger = logging.getLogger('athena')


# Engine mapping: (source path under installer_dir, target path under chroot)
# This is the ONLY place the engine knows about installer/ contents — adding
# a new mapping is a one-line append here + a row in installer/README.md.
# `installer/branding/debconf-overrides.dat` is intentionally absent: it's
# applied via a first-boot hook (Phase 6), not by chroot-build cp.
_OVERLAY_MAP = [
    ('preseed/preseed.cfg',    'preseed.cfg'),
    ('cdebconf/cdebconf.conf', 'etc/cdebconf.conf'),
]


def build_installer_chroot(
    udeb_tree,
    dir_repo: str,
    dir_chroot_installer: str,
    installer_dir: str,
    password: str,
) -> bool:
    """Build the installer chroot end to end.

    Args:
        udeb_tree:           DependencyTree against Cache.udeb_view() — the
                             udeb closure to unpack.
        dir_repo:            Path to repo/ containing built .udeb files.
        dir_chroot_installer: Target chroot dir (wiped + recreated).
        installer_dir:       Path to the installer/ data-layer tree.
        password:            Cached sudo password (caller validates with `sudo -v`).

    Returns True on success, False on any unrecoverable error.  All errors
    are logged AND printed to the console; caller does not need to
    re-surface them.
    """
    if not _wipe_and_create(dir_chroot_installer, password):
        return False

    if not _bootstrap_dpkg(dir_chroot_installer, password):
        return False

    _udeb_files = _resolve_udeb_files(udeb_tree, dir_repo)
    if not _udeb_files:
        tui.console.print(
            "ERROR: no udeb files resolved from repo — was 'source build "
            "installer' run, and are the .udeb outputs in repo/?"
        )
        logger.error("build_installer_chroot: no udeb files resolved")
        return False
    tui.console.print(
        f"Resolved {len(_udeb_files)} udeb file(s) for unpack"
    )

    if not _dpkg_unpack(dir_chroot_installer, _udeb_files, password):
        return False

    if not _apply_installer_overlay(
            dir_chroot_installer, installer_dir, password):
        return False

    _report_stats(dir_chroot_installer)
    return True


# ---------------------------------------------------------------------------
# Helpers — each step of build_installer_chroot, factored for testability
# ---------------------------------------------------------------------------


def _sudo(cmd_args: List[str], password: str) -> subprocess.CompletedProcess:
    """Run `sudo -S <cmd_args>` with the cached password.

    Captured stdout/stderr returned to caller.  Caller checks returncode.
    Single source of truth for the sudo invocation pattern so tests can
    monkeypatch this one function.
    """
    return subprocess.run(
        ['sudo', '-S'] + cmd_args,
        input=password + '\n',
        capture_output=True, text=True,
    )


def _wipe_and_create(dir_chroot_installer: str, password: str) -> bool:
    """rm -rf the installer chroot dir then recreate it empty.

    `rm -rf` runs under sudo because a prior partial run may have left
    root-owned files inside.  But the RE-CREATE step uses plain
    os.makedirs so the top-level chroot dir is user-owned — same
    pattern the live chroot already uses.  If we sudo-mkdir'd here, the
    recreated dir would be root-owned, and BuildConfig's next startup
    would fail its `os.access(dir, os.W_OK)` check (caught in
    production 2026-05-11).

    Operator can wipe a stale root-owned chroot manually with
    `sudo rm -rf <dir>`; BuildConfig recreates it user-owned on the
    next launch.  `rm -rf` is a no-op on a missing path so the first
    call here on a fresh install just succeeds without any wipe work.
    """
    tui.console.print(f"Wiping {dir_chroot_installer}...")
    _r = _sudo(['rm', '-rf', dir_chroot_installer], password)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: failed to wipe {dir_chroot_installer}: "
            f"{_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_wipe_and_create rm -rf {dir_chroot_installer}: "
            f"rc={_r.returncode}, stderr={_r.stderr.strip()}"
        )
        return False
    try:
        os.makedirs(dir_chroot_installer, exist_ok=True)
    except OSError as e:
        tui.console.print(
            f"ERROR: failed to create {dir_chroot_installer}: {e}"
        )
        logger.error(
            f"_wipe_and_create os.makedirs {dir_chroot_installer}: {e}"
        )
        return False
    return True


def _bootstrap_dpkg(dir_chroot_installer: str, password: str) -> bool:
    """Create the minimal /var/lib/dpkg structure that `dpkg --root` needs.

    Without this, the first `dpkg --unpack` fails because dpkg refuses to
    run when its admin dir is absent.  Mirrors chroot.py's
    _init_dpkg_database for the deb world.
    """
    _dpkg_dirs = [
        'var/lib/dpkg',
        'var/lib/dpkg/info',
        'var/lib/dpkg/updates',
        'var/lib/dpkg/triggers',
    ]
    for _d in _dpkg_dirs:
        _path = os.path.join(dir_chroot_installer, _d)
        _r = _sudo(['mkdir', '-p', _path], password)
        if _r.returncode != 0:
            tui.console.print(
                f"ERROR: mkdir {_path} failed: {_r.stderr.strip()[:200]}"
            )
            logger.error(
                f"_bootstrap_dpkg mkdir {_path}: rc={_r.returncode}, "
                f"stderr={_r.stderr.strip()}"
            )
            return False
    for _f in ['var/lib/dpkg/status', 'var/lib/dpkg/available']:
        _path = os.path.join(dir_chroot_installer, _f)
        _r = _sudo(['touch', _path], password)
        if _r.returncode != 0:
            tui.console.print(
                f"ERROR: touch {_path} failed: {_r.stderr.strip()[:200]}"
            )
            logger.error(
                f"_bootstrap_dpkg touch {_path}: rc={_r.returncode}, "
                f"stderr={_r.stderr.strip()}"
            )
            return False
    return True


def _resolve_udeb_files(udeb_tree, dir_repo: str) -> List[str]:
    """Map udeb_tree.selected_pkgs canonical names → absolute .udeb paths in
    repo/.

    Uses the Filename field from the udeb's Packages-index record and
    strips any binNMU suffix (+bN) via utils.strip_build_version — same
    pattern as chroot.py's _get_deb_files for the deb world.  This
    matches what dpkg-buildpackage actually emits: a source rebuild of
    `foo` whose index version is `1.0-2+b7` produces a file named
    `foo_1.0-2_amd64.udeb` on disk (no `+b7`).  Constructing the
    filename from the version field would miss the rename.

    Returns the list of resolved paths.  Missing-on-disk udebs are
    logged + skipped (warning); they don't fail the whole resolve —
    caller will notice if the list is too short.
    """
    _files: List[str] = []
    for _name in udeb_tree.selected_pkgs:
        _pkg = udeb_tree.selected_pkgs[_name]
        # Skip virtual-package alias entries — same canonical pkg under
        # multiple keys would be unpacked multiple times.
        if _name != _pkg['Package']:
            continue
        _filename = os.path.basename(_pkg.get('Filename') or '')
        if not _filename:
            logger.warning(
                f"_resolve_udeb_files: {_name} has no Filename field — skipping"
            )
            continue
        # Strip +bN binNMU suffix so the recorded filename matches what
        # dpkg-buildpackage actually produced.  utils.strip_build_version
        # is the single source of truth shared with the deb pipeline
        # (STA-15) — tolerates udeb extension.
        try:
            _filename = utils.strip_build_version(_filename)
        except ValueError:
            logger.warning(
                f"_resolve_udeb_files: malformed Filename {_filename!r} for "
                f"{_name} — using original (binNMU strip skipped)"
            )
        _filepath = os.path.join(dir_repo, _filename)
        if os.path.exists(_filepath):
            _files.append(_filepath)
            continue
        logger.warning(
            f"_resolve_udeb_files: missing {_name} — expected {_filepath}"
        )
    return _files


def _dpkg_unpack(
    dir_chroot_installer: str, udeb_files: List[str], password: str
) -> bool:
    """Run `sudo dpkg --root=<chroot> --force-depends --no-triggers --unpack`
    against every udeb file.

    `--force-depends`: udebs declare deps on other udebs (cdebconf-udeb,
        libc6-udeb, libreadline8-udeb) that aren't installed on the host —
        but ARE in the install set itself.
    `--force-overwrite`: d-i udebs SHIP OVERLAPPING FILES BY DESIGN.
        Classic case: busybox-udeb ships /sbin/depmod (a busybox applet
        stub), kmod-udeb ships the real /sbin/depmod.  Same for
        modprobe, insmod, and many other utility paths.  d-i's own
        udpkg ignores file conflicts entirely; under dpkg we need this
        flag to mirror that permissiveness.  Caught in production
        2026-05-11 — dpkg rejected the kmod-udeb unpack until this flag
        landed.
    `--no-triggers`: trigger machinery is irrelevant for udebs and would
        try to run host hooks against the chroot.
    `--unpack` (not -i): skip configure.  Postinsts use db_input which
        requires cdebconf running — that happens at first boot under
        rootskel + main-menu, not at chroot-build time.

    Logs stdout/stderr in full to the file logger; returns False if
    dpkg exits non-zero AND we can't attribute the failure to
    --force-depends/--force-overwrite warnings.
    """
    tui.console.print(
        f"Unpacking {len(udeb_files)} udeb(s) into {dir_chroot_installer}..."
    )
    _cmd = [
        'dpkg',
        '--root=' + dir_chroot_installer,
        '--force-depends',
        '--force-overwrite',
        '--no-triggers',
        '--unpack',
    ] + udeb_files
    _r = _sudo(_cmd, password)
    # dpkg may exit 1 even on successful --force-depends unpack because of
    # the dependency warnings.  We log fully and inspect stderr for
    # genuine errors (vs warnings).  Most operators will accept the
    # warnings; outright failure (e.g. file conflict, bad archive) is
    # what we gate on.
    logger.info(
        f"_dpkg_unpack stdout:\n{_r.stdout}\n_dpkg_unpack stderr:\n{_r.stderr}"
    )
    if _r.returncode == 0:
        return True
    # Heuristic: if stderr contains the word "error" (dpkg prefixes hard
    # errors with "dpkg: error:" while warnings start with "dpkg-deb: warning"
    # or are dependency-related), treat as failure.
    _stderr_low = _r.stderr.lower()
    if 'dpkg: error' in _stderr_low or 'failed to ' in _stderr_low:
        tui.console.print(
            f"ERROR: dpkg --unpack failed (rc={_r.returncode}): "
            f"{_r.stderr.strip().splitlines()[-1][:200] if _r.stderr.strip() else 'no stderr'}"
        )
        logger.error(
            f"_dpkg_unpack failed: rc={_r.returncode}, "
            f"stderr_tail={_r.stderr.strip().splitlines()[-5:]}"
        )
        return False
    # Non-zero exit with no hard error → assume dependency warnings only.
    tui.console.print(
        f"dpkg --unpack: rc={_r.returncode}, treating as --force-depends warnings only"
    )
    logger.warning(
        f"_dpkg_unpack returned {_r.returncode} but no hard error in "
        f"stderr — proceeding"
    )
    return True


def _apply_installer_overlay(
    dir_chroot_installer: str, installer_dir: str, password: str
) -> bool:
    """Copy data-layer files from installer/ to their target paths in the
    chroot, per _OVERLAY_MAP.

    Missing source files are silently skipped — that's the documented
    contract in installer/README.md.  Operator who wants to override a
    file drops it in the right place under installer/; operator who's
    fine with the udeb defaults leaves the source absent.
    """
    for _src_rel, _dst_rel in _OVERLAY_MAP:
        _src = os.path.join(installer_dir, _src_rel)
        if not os.path.exists(_src):
            logger.info(
                f"_apply_installer_overlay: {_src_rel} absent — skipping"
            )
            continue
        _dst = os.path.join(dir_chroot_installer, _dst_rel)
        _dst_parent = os.path.dirname(_dst)
        if _dst_parent:
            _r = _sudo(['mkdir', '-p', _dst_parent], password)
            if _r.returncode != 0:
                tui.console.print(
                    f"ERROR: mkdir {_dst_parent} for overlay failed: "
                    f"{_r.stderr.strip()[:200]}"
                )
                logger.error(
                    f"_apply_installer_overlay mkdir {_dst_parent}: "
                    f"rc={_r.returncode}, stderr={_r.stderr.strip()}"
                )
                return False
        _r = _sudo(['cp', _src, _dst], password)
        if _r.returncode != 0:
            tui.console.print(
                f"ERROR: cp {_src} → {_dst} failed: "
                f"{_r.stderr.strip()[:200]}"
            )
            logger.error(
                f"_apply_installer_overlay cp {_src} {_dst}: "
                f"rc={_r.returncode}, stderr={_r.stderr.strip()}"
            )
            return False
        tui.console.print(f"Overlay: {_src_rel} → {_dst_rel}")
    return True


def _report_stats(dir_chroot_installer: str) -> None:
    """Print a short summary of what's in the chroot post-unpack.

    Best-effort — failures here don't fail the build, since the chroot
    is already on disk and the operator can inspect it directly.
    """
    try:
        _r = subprocess.run(
            ['du', '-sh', dir_chroot_installer],
            capture_output=True, text=True, timeout=30,
        )
        if _r.returncode == 0:
            tui.console.print(f"Installer chroot size: {_r.stdout.strip()}")
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Count packages dpkg believes are installed.
    _status = os.path.join(dir_chroot_installer, 'var/lib/dpkg/status')
    if os.path.exists(_status):
        try:
            with open(_status, 'r', errors='replace') as fh:
                _content = fh.read()
            _n = _content.count('\nPackage: ') + (
                1 if _content.startswith('Package: ') else 0
            )
            tui.console.print(
                f"Installer chroot: {_n} package(s) in dpkg status"
            )
        except OSError:
            pass
