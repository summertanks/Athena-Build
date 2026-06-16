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
from typing import List, Optional, TYPE_CHECKING

import tui
import utils

if TYPE_CHECKING:
    import dependencytree   # forward-reference target for type hints

logger = logging.getLogger('athena.chroot')


# Engine mapping: (source path under installer_dir, target path under chroot)
# This is the ONLY place the engine knows about installer/ contents — adding
# a new mapping is a one-line append here + a row in installer/README.md.
# Note: Phase-6 branding overrides are no longer an engine-time overlay; they
# ship inside fork/source/athena-installer-data/data/ and apply via a first-
# boot hook (S40-athena-branding).  See docs/branding-methodology.md.
_OVERLAY_MAP = [
    ('preseed/preseed.cfg',          'preseed.cfg'),
    ('cdebconf/cdebconf.conf',       'etc/cdebconf.conf'),
    # Debug hook — tails d-i's per-step syslog to /dev/ttyS0 for QEMU
    # serial capture.  Skipped automatically if the source file is
    # absent (operator removes it for a non-debug ISO).
    ('debug/syslog-to-serial.sh',
     'lib/debian-installer-startup.d/S99-syslog-to-serial'),
    # When the operator skips the network mirror, write a default Athena apt
    # source so the installed system isn't left with only the (about-to-be-
    # disabled) cdrom source.  Numbered 05 — runs before 08hw-detect so the
    # network fallback is in sources.list before any post-pkgsel apt-install
    # tries to find packages by name.  No-ops when a mirror was selected.
    # cp -p preserves its +x bit.
    ('finish-install/05athena-default-source',
     'usr/lib/finish-install.d/05athena-default-source'),
    # COMP-01: disable the install-disc apt source on the target post-install
    # (so apt doesn't block on "insert the disc").  Conditional on a network
    # source existing — see the script.  Numbered 11 (NOT 06) so it runs AFTER
    # 08hw-detect — that hook apt-installs queued microcode + vmware tools at
    # finish-install time, and on offline / no-mirror installs the cdrom is
    # the only source carrying those packages.  Caught 2026-05-28: at 06 it
    # disabled cdrom BEFORE 08hw-detect, so apt-install couldn't find
    # intel-microcode / open-vm-tools-desktop / shim-signed.  cp -p preserves
    # its +x bit.
    ('finish-install/11athena-disable-cdrom',
     'usr/lib/finish-install.d/11athena-disable-cdrom'),
]


def build_installer_chroot(
    udeb_tree: 'dependencytree.DependencyTree',
    dir_udebs: str,
    dir_chroot_installer: str,
    installer_dir: str,
    password: str,
    codename: str = 'thor',
    pool_pkg_names: 'Optional[set[str]]' = None,
) -> bool:
    """Build the installer chroot end to end.

    Args:
        udeb_tree:           DependencyTree against Cache.udeb_view() — the
                             udeb closure to unpack.
        dir_udebs:           Path to the dir holding .udeb files.  CONF-01
                             Stage D: this is repo/dists/<codename>/main/
                             debian-installer/binary-<arch>/ in the unified
                             layout.  Previously was repo/ (top-level) and
                             we appended `main`; now resolved by caller for
                             clarity.
        dir_chroot_installer: Target chroot dir (wiped + recreated).
        installer_dir:       Path to the installer/ data-layer tree.
        password:            Cached sudo password (caller validates with `sudo -v`).

    Returns True on success, False on any unrecoverable error.  All errors
    are logged AND printed to the console; caller does not need to
    re-surface them.
    """
    logger.info(
        f"build_installer_chroot: → {dir_chroot_installer} (codename={codename})"
    )
    if not _wipe_and_create(dir_chroot_installer, password):
        return False

    if not _bootstrap_dpkg(dir_chroot_installer, password):
        return False

    _udeb_files = _resolve_udeb_files(udeb_tree, dir_udebs)
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

    if not _assert_apt_setup_generators(dir_chroot_installer):
        return False

    if not _audit_and_strip_chroot_hooks(
            dir_chroot_installer, pool_pkg_names,
            installer_dir, password):
        return False

    # Stock d-i image-build actions that aren't done by any udeb's
    # postinst (deferred-postinst model: udebs are unpacked here but
    # their maintainer scripts only run at first boot under
    # rootskel + main-menu).
    #
    # FORK-01 Steps 3+4 (2026-05-16/17) — moved into the
    # athena-installer-data udeb (dpkg-unpacked above with the rest
    # of the closure):
    #   - Step 3: mirror/protocol stub template
    #   - Step 4: /etc/lsb-release, /etc/default-release (codename
    #             substituted at build time), runtime dirs (/tmp,
    #             /var/tmp, /root), debootstrap codename symlink
    # _register_self_in_dpkg_status stays here — it writes a fake
    # `Package: debian-installer` stanza; doing this from a udeb's
    # postinst would change the lifecycle timing.
    if not _run_depmod(dir_chroot_installer, password):
        return False

    if not _register_self_in_dpkg_status(
            dir_chroot_installer, codename, password):
        return False

    if not _apply_installer_overlay(
            dir_chroot_installer, installer_dir, password):
        return False

    _report_stats(dir_chroot_installer)
    return True


# ---------------------------------------------------------------------------
# Helpers — each step of build_installer_chroot, factored for testability
# ---------------------------------------------------------------------------


# ARCH-19: canonical wrapper in utils.sudo; module-local `_sudo` kept as an
# alias so call sites + test monkeypatching stay unchanged.
_sudo = utils.sudo


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
    logger.info(f"wipe + recreate {dir_chroot_installer}")
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
    logger.info(f"bootstrap_dpkg: init var/lib/dpkg under {dir_chroot_installer}")
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


def _resolve_udeb_files(udeb_tree: 'dependencytree.DependencyTree',
                        dir_udebs: str) -> List[str]:
    """Map udeb_tree.selected_pkgs canonical names → absolute .udeb
    paths.

    CONF-01 Stage D: `dir_udebs` is now the dir holding .udeb files
    directly (in the unified apt-repo layout that's
    repo/dists/<codename>/main/debian-installer/binary-<arch>/).
    Pre-CONF-01 the param was the top-level repo/ and we joined 'main';
    Stage D pushes the resolved path to the caller for clarity.

    Maps the cache's Packages-index Filename via strip_build_version
    (drops Debian buildd's +bN bin-NMU suffix).  Post-build NMU strip
    further normalises the on-disk .udeb to its pristine source
    version.

    Missing-on-disk udebs are logged + skipped (warning); caller
    notices if the list is too short.
    """
    logger.info(
        f"resolve_udeb_files: {len(udeb_tree.selected_pkgs)} candidate(s) "
        f"from {dir_udebs}"
    )
    _files: List[str] = []
    _main = dir_udebs
    for _name in udeb_tree.selected_pkgs:
        _pkg = udeb_tree.selected_pkgs[_name]
        if _name != _pkg['Package']:
            continue
        _filename = os.path.basename(_pkg.get('Filename') or '')
        if not _filename:
            logger.warning(
                f"_resolve_udeb_files: {_name} has no Filename field — skipping"
            )
            continue
        _filename = utils.normalize_repo_filename(_filename)
        # The index Filename is the PRISTINE name, but the on-disk .udeb may
        # carry a +asg<R>u<N> stamp (UPD-01 update layer).  find_matching_artifact
        # accepts the pristine name OR its stamped variant — the same
        # reconciliation check_build / _source_state use.  Without it, every
        # stamped udeb (e.g. busybox-udeb after a security delta) resolves as
        # "missing" and is dropped from the initrd → kernel panic "no init found"
        # (no /bin/sh, since busybox is gone).
        _match = utils.find_matching_artifact(_main, _filename)
        if _match:
            _files.append(_match)
            continue
        logger.warning(
            f"_resolve_udeb_files: missing {_name} — expected "
            f"{os.path.join(_main, _filename)} (or a +asg-stamped variant)"
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
    logger.info(
        f"dpkg --unpack: {len(udeb_files)} udeb(s) → {dir_chroot_installer}"
    )
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
    # Per-line emit (one logger call per dpkg line) so each becomes its
    # own buffer entry in the TUI — emitting the whole stdout as a single
    # multi-line record makes the log tab wrap-slice it across line
    # boundaries (caught 2026-05-31).  "Selecting/Preparing/Unpacking"
    # stay at INFO so the chroot tab paints them as progress; everything
    # else (file lists, debconf chatter) at DEBUG to keep the tab quiet.
    _PROGRESS_PREFIXES = (
        'Selecting ', 'Preparing to unpack ', 'Unpacking ',
        'Setting up ', 'Processing triggers for ',
    )
    for _line in _r.stdout.splitlines():
        if _line.startswith(_PROGRESS_PREFIXES):
            logger.info(_line)
        else:
            logger.debug(_line)
    for _line in _r.stderr.splitlines():
        logger.debug(f"stderr: {_line}")
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


def _assert_apt_setup_generators(dir_chroot_installer: str) -> bool:
    """Fail the build if either apt-source generator is missing.

    apt-setup runs its generators (/usr/lib/apt-setup/generators/) in
    numeric order; the installer needs BOTH source generators present:

      40cdrom (athena-cdrom-setup) — writes the `deb cdrom:` source.
        Runs before 50mirror, so the install stays self-contained when
        the operator declines the network mirror.  Missing → no-mirror
        path has no apt source → "Configure apt" fails, tasksel empty,
        grub-efi won't install.

      50mirror (athena-mirror-setup) — drives the network-mirror step
        (choose-mirror) and writes the mirror source.  Missing → no
        mirror prompt at all and no mirror in the target's sources.list.

    Both can vanish silently from the udeb closure via a bad Provides
    (2026-05-27: athena-cdrom-setup wrongly Provides apt-mirror-setup, so
    seeding it satisfied athena-setup-udeb's `Depends: athena-mirror-setup`
    and the real 50mirror was never pulled).  A with-mirror OR no-mirror
    smoke test alone hides one or the other, so assert both here.
    """
    _gendir = os.path.join(
        dir_chroot_installer, 'usr/lib/apt-setup/generators')
    _required = {
        '40cdrom': "athena-cdrom-setup (cdrom apt-source; no-mirror path)",
        '50mirror': "athena-mirror-setup (network-mirror step)",
    }
    _ok = True
    for _gen, _owner in _required.items():
        if not os.path.exists(os.path.join(_gendir, _gen)):
            tui.console.print(
                f"ERROR: apt-setup generator '{_gen}' is missing from the "
                f"installer chroot — expected from {_owner}. Check the udeb "
                f"closure (config/installer.list seeds + fork Provides)."
            )
            logger.error(
                f"_assert_apt_setup_generators: {_gen} absent — {_owner} "
                f"not in the udeb closure"
            )
            _ok = False
    return _ok


def _audit_and_strip_chroot_hooks(
    dir_chroot_installer: str,
    pool_pkg_names: 'Optional[set[str]]',
    installer_dir: str,
    password: str,
) -> bool:
    """CONF-10 S2 — durable replacement for the hardcoded strip list.

    Per the "Athena ships as Athena" principle
    (memory/project_filter_debian_specific_installer_hooks.md), every
    apt-install in pre-pkgsel.d / finish-install.d must either resolve
    against our pool or be excluded entirely.

    Pre-CONF-10: hardcoded `_targets = (20install-hwpackages,
    50save-logs)` — rotted whenever upstream added/renamed a hook.

    Post-CONF-10: walks the unpacked installer chroot's hook trees,
    parses every `apt-install X` line, and cross-references the named
    pkgs against the build's pool.  Decisions:

      • all pkgs in pool                          → no action
      • unpooled, hook listed in
        installer/strip-hooks-allowlist           → sudo rm -f
      • unpooled, hook NOT allowlisted, soft
        (`apt-install X || true`)                 → WARN + leave in place
      • unpooled, hook NOT allowlisted, hard      → FAIL build

    `pool_pkg_names=None` (or empty) — audit skipped (build.py call
    sites without a dep tree, early tests).  Returns True so the
    legacy fallback behavior is preserved.

    Returns True on success / WARN-only outcomes, False on FAIL
    findings or sudo rm failures."""
    if not pool_pkg_names:
        logger.info(
            "_audit_and_strip_chroot_hooks: no pool_pkg_names provided "
            "— skipping (legacy compat)"
        )
        return True
    import identity_scan
    _allow_path = os.path.join(installer_dir, '..', 'installer',
                               'strip-hooks-allowlist')
    # The above join is awkward when installer_dir already IS the
    # installer dir.  Normalise: the canonical home of the allowlist
    # is `<working>/installer/strip-hooks-allowlist`; reach it from
    # the installer_dir passed to build_installer_chroot which is
    # `<working>/installer`.
    if os.path.basename(os.path.normpath(installer_dir)) == 'installer':
        _allow_path = os.path.join(installer_dir, 'strip-hooks-allowlist')
    _allow_path = os.path.normpath(_allow_path)

    logger.info(
        f"audit chroot hooks vs pool ({len(pool_pkg_names)} pkg names); "
        f"allowlist={_allow_path}"
    )
    _findings = identity_scan.audit_chroot_hooks(
        dir_chroot_installer, pool_pkg_names, _allow_path,
    )
    if not _findings:
        logger.info("audit chroot hooks: all apt-install targets in pool")
        return True

    _hard_fails = [f for f in _findings if f['action'] == 'fail']
    _warns      = [f for f in _findings if f['action'] == 'warn']
    _strips     = [f for f in _findings if f['action'] == 'strip']

    # WARN-level findings — best-effort `|| true` apt-install of
    # something not in our pool.  Doesn't break the build but worth
    # surfacing so the operator can choose to allowlist-strip.
    for _f in _warns:
        logger.warning(
            f"hook {_f['path']}:{_f['line_no']} apt-install "
            f"(soft) targets unpooled pkg(s): {_f['missing_pkgs']}"
        )

    # STRIP — operator opted into removing the hook via allowlist.
    _stripped_paths: 'set[str]' = set()
    for _f in _strips:
        _path = str(_f['path'])
        if _path in _stripped_paths:
            continue                       # multiple apt-install lines
        _abs = os.path.join(dir_chroot_installer, _path)
        _r = _sudo(['rm', '-f', _abs], password)
        if _r.returncode != 0:
            logger.error(
                f"strip hook {_path}: rm -f failed rc={_r.returncode}, "
                f"stderr={_r.stderr.strip()}"
            )
            tui.console.print(
                f"ERROR: cannot strip {_path} from installer chroot"
            )
            return False
        _stripped_paths.add(_path)
        logger.info(f"stripped {_path} ({_f.get('reason', '?')})")

    # FAIL — unpooled hard apt-install, not allowlisted.  Build aborts
    # with an actionable diagnostic per hit.
    if _hard_fails:
        for _f in _hard_fails:
            logger.error(
                f"hook {_f['path']}:{_f['line_no']} apt-install targets "
                f"pkg(s) not in pool: {_f['missing_pkgs']} — either add "
                f"to pool.list, change the hook, or allow-strip in "
                f"{_allow_path}"
            )
        tui.console.print(
            f"ERROR: {len(_hard_fails)} installer hook(s) reference "
            f"unpooled packages — see log", tui.COLOR_ERROR,
        )
        return False

    tui.console.print(
        f"chroot hooks: stripped {len(_stripped_paths)}, "
        f"warned {len(_warns)} soft-failure(s)"
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
    logger.info(f"apply installer overlay from {installer_dir}/")
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
