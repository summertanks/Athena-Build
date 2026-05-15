"""Build the installer chroot from the udeb closure.

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
    ('preseed/preseed.cfg',          'preseed.cfg'),
    ('cdebconf/cdebconf.conf',       'etc/cdebconf.conf'),
    # Debug hook — tails d-i's per-step syslog to /dev/ttyS0 for QEMU
    # serial capture.  Skipped automatically if the source file is
    # absent (operator removes it for a non-debug ISO).
    ('debug/syslog-to-serial.sh',
     'lib/debian-installer-startup.d/S99-syslog-to-serial'),
]


def build_installer_chroot(
    udeb_tree,
    dir_repo: str,
    dir_chroot_installer: str,
    installer_dir: str,
    password: str,
    codename: str = 'sid',
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

    if not _create_runtime_dirs(dir_chroot_installer, password):
        return False

    # Stock d-i image-build actions that aren't done by any udeb's
    # postinst (deferred-postinst model: udebs are unpacked here but
    # their maintainer scripts only run at first boot under
    # rootskel + main-menu).  Each action is what stock d-i's
    # installer/build/Makefile does after the dpkg --unpack pass.
    if not _write_athena_stub_template(dir_chroot_installer, password):
        return False

    if not _run_depmod(dir_chroot_installer, password):
        return False

    if not _write_release_files(dir_chroot_installer, codename, password):
        return False

    if not _register_self_in_dpkg_status(
            dir_chroot_installer, codename, password):
        return False

    if not _apply_installer_overlay(
            dir_chroot_installer, installer_dir, password):
        return False

    if not _install_debootstrap_codename_script(
            dir_chroot_installer, codename, password):
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
        # is the single source of truth shared with the deb pipeline —
        # tolerates udeb extension.
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
        # `cp -p` preserves mode + ownership + timestamps.  Needed for
        # data-layer files where the executable bit matters (e.g. the
        # debug syslog-to-serial.sh script lands under
        # /lib/debian-installer-startup.d/ where rootskel run-parts
        # expects executables).
        _r = _sudo(['cp', '-p', _src, _dst], password)
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


# COMP-02 phase B TODO: replace this Python helper with an
# `athena-installer-stubs-udeb` source package that ships the directories
# in its `debian/dirs` so dpkg --unpack creates them.  See
# docs/plans/comp-02-robust-build.md.
#
# FHS dirs that random d-i scripts assume exist but no udeb in our
# closure ships.  Caught 2026-05-11 — check-missing-firmware silently
# warned "can't create /tmp/dmesg.txt: nonexistent directory" and
# bootstrap-base's run-debootstrap would have done the same later.
# rootskel.udeb ships /dev /proc /run /sys but NOT /tmp /var/tmp /root,
# and we don't have a separate postinst layer that creates them
# (standard d-i builds bake these in at ramdisk-pack time).  Mode
# 1777 for /tmp + /var/tmp (sticky, world-writable — standard FHS),
# 0700 for /root.
_RUNTIME_DIRS = [
    ('tmp',     '1777'),
    ('var/tmp', '1777'),
    ('root',    '0700'),
]


def _create_runtime_dirs(dir_chroot_installer: str, password: str) -> bool:
    """Create FHS dirs no udeb ships but every d-i script assumes."""
    for _rel, _mode in _RUNTIME_DIRS:
        _path = os.path.join(dir_chroot_installer, _rel)
        _r = _sudo(['mkdir', '-p', _path], password)
        if _r.returncode != 0:
            tui.console.print(
                f"ERROR: mkdir {_path}: {_r.stderr.strip()[:200]}"
            )
            logger.error(
                f"_create_runtime_dirs mkdir {_path}: rc={_r.returncode}, "
                f"stderr={_r.stderr.strip()}"
            )
            return False
        _r = _sudo(['chmod', _mode, _path], password)
        if _r.returncode != 0:
            tui.console.print(
                f"ERROR: chmod {_mode} {_path}: {_r.stderr.strip()[:200]}"
            )
            logger.error(
                f"_create_runtime_dirs chmod {_path}: rc={_r.returncode}, "
                f"stderr={_r.stderr.strip()}"
            )
            return False
    tui.console.print(
        f"Runtime dirs: {len(_RUNTIME_DIRS)} dir(s) created (/tmp, /var/tmp, /root)"
    )
    return True


# COMP-02 phase B TODO: replace with `athena-debootstrap-codenames-udeb`
# that ships `scripts/<codename>` directly via `debian/install`.  The
# codename is read from the udeb's own debian/changelog at build time;
# no Python helper or chroot post-unpack mutation.  See
# docs/plans/comp-02-robust-build.md.
_DEBOOTSTRAP_SCRIPTS_DIR = 'usr/share/debootstrap/scripts'

# Codenames already shipped by debootstrap-udeb — skipping the copy
# avoids stomping an upstream script with a stale snapshot.  Any name
# NOT in this set is assumed to be a derivative codename (Athena's
# default) and gets a copy of `sid` as its script.
_DEBOOTSTRAP_KNOWN_SUITES = frozenset({
    'sid', 'unstable', 'testing', 'stable',
    'trixie', 'bookworm', 'bullseye', 'buster',
})


def _install_debootstrap_codename_script(
    dir_chroot_installer: str, codename: str, password: str,
) -> bool:
    """Make `/usr/share/debootstrap/scripts/<codename>` exist in the chroot.

    Debootstrap is suite-keyed: bootstrap-base passes our codename (e.g.
    "thor") as the suite argument, debootstrap looks for a script of
    that exact name under /usr/share/debootstrap/scripts/, and silently
    bails when it's missing.  bootstrap-base then exits 10 and main-menu
    falls back to step-selection — exactly the "succeeded but requested
    to be left unconfigured" loop caught 2026-05-11 on the first VMware
    install attempt.

    Fix: copy the existing `sid` script to `<codename>`.  `sid` is an
    11-line wrapper that delegates everything to `debian-common`, so a
    copy gives our derivative codename the same behaviour upstream uses
    for unstable — correct for a sid-tracking derivative like Athena.
    No-op when the codename is already an upstream-shipped suite name
    (sid / bookworm / trixie / etc.) — those scripts already exist and
    overwriting them would be wrong.
    """
    if codename in _DEBOOTSTRAP_KNOWN_SUITES:
        tui.console.print(
            f"Debootstrap script: '{codename}' is upstream — no copy needed"
        )
        return True
    _scripts = os.path.join(dir_chroot_installer, _DEBOOTSTRAP_SCRIPTS_DIR)
    _src = os.path.join(_scripts, 'sid')
    _dst = os.path.join(_scripts, codename)
    if not os.path.isfile(_src):
        tui.console.print(
            f"ERROR: {_src} missing — debootstrap-udeb didn't unpack its "
            "scripts dir.  Was it included in the udeb closure?"
        )
        logger.error(f"_install_debootstrap_codename_script: {_src} absent")
        return False
    _r = _sudo(['cp', '-p', _src, _dst], password)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: cp {_src} → {_dst}: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_install_debootstrap_codename_script cp: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    tui.console.print(
        f"Debootstrap script: sid → {codename} (so debootstrap recognises "
        "our suite)"
    )
    return True


# ---------------------------------------------------------------------------
# Stock d-i image-build conformance helpers
#
# Each helper here mirrors one action stock d-i's installer/build/Makefile
# performs against $(TREE) AFTER the dpkg --unpack pass and BEFORE the
# initrd is packed.  Our priority hierarchy (locked 2026-05-12):
#
#   1. Build-pipeline action in installer_chroot.py (this file)  ← preferred
#   2. Stock kernel-cmdline knob via installer/boot/grub.cfg
#   3. Custom Athena udeb (minimise — every custom udeb is one more
#      thing to maintain across kernel/d-i refreshes)
#   4. Quilt patch on stock source (last resort — patches rot fast)
#
# Each helper here is option 1 for an action that stock does in its
# Makefile and we previously did via overlay file or skipped entirely.
# ---------------------------------------------------------------------------


# Stub template for debconf questions that an upstream udeb would
# normally define but we don't ship.  Currently just mirror/protocol —
# bootstrap-base.postinst's UNGUARDED `db_get mirror/protocol` returns
# 10 → set -e exits → bootstrap-base silent-loops on
# "succeeded but requested to be left unconfigured".  In stock d-i the
# template comes from choose-mirror-udeb; we don't ship it because we
# only support file:///cdrom installs.
#
# Lives in /var/lib/dpkg/info/ so rootskel's S20templates run-part
# picks it up via debconf-loadtemplate at boot — same path the real
# udebs use.  Caught 2026-05-11 — diagnosed by patching the postinst
# with `set -x` to /dev/ttyS0 since the failure happened before any
# base-installer logger call.
_ATHENA_STUB_TEMPLATES = """\
Template: mirror/protocol
Type: string
Default: file
Description: Mirror protocol (Athena stub)
 file:///cdrom is Athena's only repo source.  This template exists
 solely so bootstrap-base.postinst's UNGUARDED `db_get mirror/protocol`
 (line 77 of d-i's bootstrap-base postinst, where the `|| true` is on
 the next line — too late to suppress the error) doesn't return 10 and
 trip `set -e`, killing the postinst before debootstrap is invoked.
 .
 In stock d-i this template is provided by choose-mirror-udeb, which
 we don't ship — we only support file:///cdrom installs.
"""


def _sudo_write(path: str, content: str, password: str) -> bool:
    """sudo-write content to path so the destination is root-owned
    (matches what dpkg --unpack would produce).

    Critical: tee READS STDIN.  Earlier version of this helper passed
    `password\\ncontent` as stdin to `sudo -S tee`, expecting sudo to
    consume the password line.  But when sudo's credential cache is hot
    (which happens after the FIRST auth in build_installer_chroot —
    every subsequent sudo call sees a fresh timestamp), `sudo -S` does
    NOT consume the password line — it passes stdin straight through to
    tee, which then writes `password\\ncontent` to the destination.
    This leaked the operator's sudo password into /var/lib/dpkg/status
    (broke main-menu parse — "Iek! Don't find end of field"), and into
    /etc/lsb-release / /etc/default-release / athena-stubs.templates
    (would have shipped on the installer ISO).  Caught 2026-05-13.

    Fix: refresh sudo's timestamp via `sudo -S -v` first (sudo -v DOES
    consume the password line, whether cache was hot or cold — that's
    `-v`'s entire purpose).  Then run the actual tee under plain
    `sudo` (no -S) — tee receives clean stdin = content only.
    """
    _refresh = subprocess.run(
        ['sudo', '-S', '-v'],
        input=password + '\n',
        capture_output=True, text=True,
    )
    if _refresh.returncode != 0:
        tui.console.print(
            f"ERROR: sudo refresh for write {path}: "
            f"{_refresh.stderr.strip()[:200]}"
        )
        logger.error(
            f"_sudo_write sudo -v: rc={_refresh.returncode}, "
            f"stderr={_refresh.stderr.strip()}"
        )
        return False
    _r = subprocess.run(
        ['sudo', 'tee', path],
        input=content,
        capture_output=True, text=True,
    )
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: write {path}: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_sudo_write {path}: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    return True


def _write_athena_stub_template(
    dir_chroot_installer: str, password: str
) -> bool:
    """Write /var/lib/dpkg/info/athena-stubs.templates.

    Replaces the prior installer/templates/athena-stubs.templates
    overlay (deleted 2026-05-12) so all stub-template content lives in
    one place — version-controlled Python rather than a side-car file
    that operators might assume they can edit for rebrand purposes.
    """
    _dst = os.path.join(
        dir_chroot_installer, 'var/lib/dpkg/info/athena-stubs.templates'
    )
    if not _sudo_write(_dst, _ATHENA_STUB_TEMPLATES, password):
        return False
    tui.console.print(
        "Stub templates: athena-stubs.templates → "
        "/var/lib/dpkg/info/ (mirror/protocol)"
    )
    return True


def _run_depmod(dir_chroot_installer: str, password: str) -> bool:
    """Run `depmod -a -b <chroot> <kver>` for each kernel under
    <chroot>/lib/modules/.

    Stock d-i's installer/build/Makefile runs depmod after the unpack
    pass so that the initrd's modules.dep / modules.alias / etc. files
    are up to date — without this, hw-detect's modprobe of (say)
    vmw_pvscsi succeeds on the file but kmod can't compute the right
    soft-dep chain, and certain modules silently fail to load.
    Caught 2026-05-12 as a cosmetic-but-suspicious "depmod: WARNING"
    line in the install log; promoted to a real step because the same
    log later showed modules that depmod's index would have surfaced.

    Multiple kernel versions present → depmod each in turn (rare but
    possible when an old + new kernel-image udeb both land in the
    closure during a kernel refresh).  Missing /lib/modules entirely
    is NOT an error: a non-cdrom flavour (rescue.cfg, hd-media without
    kernel) legitimately has no kernel udebs and just skips this step.
    """
    _modules_dir = os.path.join(dir_chroot_installer, 'lib/modules')
    if not os.path.isdir(_modules_dir):
        tui.console.print(
            "depmod: no /lib/modules in chroot — skipping (non-kernel flavour)"
        )
        logger.info(
            f"_run_depmod: {_modules_dir} absent — no kernel udeb in closure"
        )
        return True
    try:
        _kvers = sorted(
            _e for _e in os.listdir(_modules_dir)
            if os.path.isdir(os.path.join(_modules_dir, _e))
        )
    except OSError as e:
        tui.console.print(f"ERROR: listdir {_modules_dir}: {e}")
        logger.error(f"_run_depmod listdir {_modules_dir}: {e}")
        return False
    if not _kvers:
        tui.console.print(
            "depmod: /lib/modules empty — skipping (no kernel modules)"
        )
        logger.info(f"_run_depmod: {_modules_dir} present but empty")
        return True
    for _kver in _kvers:
        _r = _sudo(
            ['depmod', '-a', '-b', dir_chroot_installer, _kver], password
        )
        # depmod may warn about modules.builtin.modinfo missing (the
        # kernel-image-*-di udeb in linux-signed-amd64 doesn't ship
        # that file).  That's cosmetic — modules still load.  We only
        # fail on hard errors.
        logger.info(
            f"_run_depmod {_kver}: rc={_r.returncode}, "
            f"stderr_tail={_r.stderr.strip().splitlines()[-3:] if _r.stderr.strip() else []}"
        )
        if _r.returncode != 0:
            tui.console.print(
                f"ERROR: depmod -a -b {dir_chroot_installer} {_kver}: "
                f"{_r.stderr.strip()[:200]}"
            )
            logger.error(
                f"_run_depmod {_kver}: rc={_r.returncode}, "
                f"stderr={_r.stderr.strip()}"
            )
            return False
    tui.console.print(
        f"depmod: indexed {len(_kvers)} kernel(s): {', '.join(_kvers)}"
    )
    return True


def _write_release_files(
    dir_chroot_installer: str, codename: str, password: str
) -> bool:
    """Write /etc/default-release and /etc/lsb-release.

    `/etc/default-release` carries the bare codename (e.g. `thor`) — read
    by various d-i scripts that need to know which suite the installer
    targets (apt-setup's release picker, archive-key trust checks).
    Stock d-i echoes $(DEBIAN_RELEASE) to this file from the Makefile.
    Caught 2026-05-12 as a cosmetic install-log warning
    "cat: can't open '/etc/default-release'" — promoted to a real step
    once the audit identified it as a stock action we'd skipped.

    `/etc/lsb-release` carries minimal distrib info so d-i scripts that
    grep it for branding render the Athena name rather than falling
    through to "Debian GNU/Linux".  We use the codename as both
    RELEASE and CODENAME — Athena tracks sid so there's no separate
    point-release identifier to record.
    """
    _default_release = os.path.join(
        dir_chroot_installer, 'etc/default-release'
    )
    if not _sudo_write(_default_release, codename + '\n', password):
        return False
    _lsb = (
        'DISTRIB_ID=Athena\n'
        'DISTRIB_DESCRIPTION="Athena Linux installer"\n'
        f'DISTRIB_RELEASE={codename}\n'
        f'DISTRIB_CODENAME={codename}\n'
    )
    _lsb_path = os.path.join(dir_chroot_installer, 'etc/lsb-release')
    if not _sudo_write(_lsb_path, _lsb, password):
        return False
    tui.console.print(
        f"Release files: /etc/default-release ({codename}) + "
        "/etc/lsb-release written"
    )
    return True


def _register_self_in_dpkg_status(
    dir_chroot_installer: str, codename: str, password: str
) -> bool:
    """Append a `Package: debian-installer` stanza to /var/lib/dpkg/status.

    Stock d-i's image-build Makefile (lines 568-573) writes a 4-field
    dummy `debian-installer` entry so `dpkg-query -W debian-installer`
    returns a result for scripts that probe it.  We keep the package
    name `debian-installer` (not athena-installer) so stock d-i scripts
    that string-compare on it continue to work.

    Format matches stock VERBATIM — 4 fields + trailing blank line.
    Caught 2026-05-13: an earlier 9-field stanza with multi-line
    Description and Maintainer-with-angle-brackets tripped the lean
    libdebian-installer RFC-822 parser ("Iek! Don't find end of field")
    and segfaulted main-menu at startup.  The parser tolerates the
    standard 4 stock fields without issue.

    Separator handling: normalise existing trailing newlines so we always
    get exactly one blank line between the previous stanza and ours.
    dpkg writes status with a trailing blank line by convention, but
    nothing in our flow guarantees that, so we strip+re-add explicitly.
    """
    _stanza = (
        'Package: debian-installer\n'
        'Status: install ok installed\n'
        f'Version: {codename}\n'
        'Description: athena installation image\n'
        '\n'  # trailing blank line — stanza terminator per stock
    )
    _status = os.path.join(dir_chroot_installer, 'var/lib/dpkg/status')
    _r = _sudo(['cat', _status], password)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: read {_status}: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_register_self_in_dpkg_status cat {_status}: "
            f"rc={_r.returncode}, stderr={_r.stderr.strip()}"
        )
        return False
    _existing = _r.stdout
    if 'Package: debian-installer\n' in _existing:
        tui.console.print(
            "dpkg status: debian-installer stanza already present — skipping"
        )
        return True
    # Strip any trailing newlines + add exactly one blank-line separator.
    _normalised = _existing.rstrip('\n') + '\n\n' if _existing else ''
    if not _sudo_write(_status, _normalised + _stanza, password):
        return False
    tui.console.print(
        "dpkg status: added dummy debian-installer stanza"
    )
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
