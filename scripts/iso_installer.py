"""Master the installer ISO from buildroot/installer/.

The installer chroot becomes the initrd as a monolithic cpio.gz (no
separate squashfs layer in v1).  Kernel comes from the linux-image-amd64
.deb in repo/ — same kernel that ships on the live ISO.  The apt pool
(repo/) is bundled onto the ISO so the installer reads packages from
/cdrom/pool at install time (matches the locked decision: no Debian
repo fallback ever).  grub-mkrescue produces the hybrid BIOS+EFI
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
import string
import subprocess
from typing import Optional, TYPE_CHECKING

import tui

if TYPE_CHECKING:
    import buildcontainer   # forward-reference target for type hints

# apt-repo metadata generators — lifted from this module to scripts/apt_repo.py
# in CONF-01 Stage A (2026-05-22).  See docs/plans/conf-01-repo-layout-migration.md.
# Pure code motion; call sites below kept the same semantics.
from apt_repo import (
    generate_apt_repo,
    sign_release_files,
    export_pubkey_to_staging,
)

logger = logging.getLogger('athena.iso')


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
    container: 'buildcontainer.BuildContainer',
    suite: str = 'thor',
    codename: str = 'thor',
    version: str = '0.1',
    snapshot: str = '',
    base_include_pkgs: Optional[list] = None,
    deb_whitelist: 'Optional[set[str]]' = None,
    signing_homedir: Optional[str] = None,
    signing_pubkey_path: Optional[str] = None,
    pkg_groups: Optional['dict[str, set]'] = None,
    group_meta: Optional['dict[str, dict[str, str]]'] = None,
    expected_kernel_pkg: Optional[str] = None,
    dir_repo_main_udeb: Optional[str] = None,   # CONF-01 Stage D
    exclude_names: Optional[set] = None,        # fork-superseded upstream bins
    dir_repo_extras: Optional['list[str]'] = None,  # non-main component dirs
    audit_identity_scan: bool = True,            # [Audit] IdentityScan
) -> bool:
    """Build the installer ISO end to end.

    Args:
        dir_chroot_installer: Path to the unpacked installer chroot
                              (the buildroot/installer/ produced by
                              cmd_build_chroot_installer).
        dir_repo:             Path to repo/main/ (in the CONF-01 unified
                              layout this is repo/dists/<codename>/main/
                              binary-<arch>/) — the dir holding regular
                              .debs.  Used for kernel lookup + as the
                              first source dir for pool staging.
        dir_repo_main_udeb:   Path to repo/dists/<codename>/main/
                              debian-installer/binary-<arch>/ — udebs
                              live here post-CONF-01.  Optional for
                              backwards-compat; when omitted, udebs are
                              not staged to the ISO pool (legacy code
                              expected them colocated with .debs in
                              repo/main/, which no longer holds).
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
    logger.info(
        f"build_installer_iso: → {iso_basename} "
        f"(suite={suite}, codename={codename}, version={version}, "
        f"snapshot={snapshot or '-'})"
    )
    _staging = os.path.join(dir_image, 'staging-installer')

    if not _prepare_staging(_staging, password):
        return False

    _kernel_src = _find_kernel(dir_repo, dir_chroot_installer, password,
                               expected_kernel_pkg=expected_kernel_pkg)
    if not _kernel_src:
        return False
    if not _stage_kernel(_kernel_src, _staging):
        return False

    if not _build_initrd(dir_chroot_installer, _staging, password):
        return False

    if not _stage_grub_cfg(_staging, installer_dir):
        return False

    if not _stage_disk_info(_staging, installer_dir, codename, version,
                            snapshot):
        return False

    # FORK-01 Step 5b (2026-05-17): the synthetic athena-tasksel-data
    # .deb generation that USED to live here was retired — the fork
    # at fork/source/athena-tasksel/ now produces both athena-tasksel
    # AND athena-tasksel-data via a multi-binary debian/control.  The
    # fork ships /usr/share/tasksel/descs/athena-tasks.desc with the
    # full chosen task set (standard curated, ssh-server, laptop,
    # desktop, gnome-desktop, development-tools) statically — no
    # per-build .desc generation, no pkg.list-group-derived synthetic.
    # athena-tasksel-data flows through normal source-build → pool
    # → debootstrap base_include via cache.
    if not _stage_base_include(_staging, base_include_pkgs):
        return False

    # CONF-01 Stage D: pool sources are now nested under dists/<codename>/.
    # Pass both .deb (binary-<arch>/) and .udeb (debian-installer/
    # binary-<arch>/) source dirs so the staged pool ends up flat as
    # the apt-cdrom logic on the target expects.
    _pool_sources = [dir_repo]
    if dir_repo_main_udeb is not None:
        _pool_sources.append(dir_repo_main_udeb)
    # Non-main component dirs (contrib/non-free/non-free-firmware) hold
    # tunneled binaries that must ship on the cdrom too — without them
    # finish-install.d/08hw-detect's apt-install of microcode/firmware
    # fails with "Unable to locate package" (the cdrom pool is the only
    # source available on an offline / no-mirror install).  _select_pool_
    # _files uses non-recursive listdir so these MUST be passed in.
    # Missing/empty dirs are tolerated (listdir errors are swallowed).
    if dir_repo_extras:
        _pool_sources.extend(d for d in dir_repo_extras if d)
    if not _stage_pool(_pool_sources, _staging, password, deb_whitelist,
                       exclude_names):
        return False

    # Per-group package manifests still useful for operator inspection
    # at .disk/groups/<group>.list (apt-pulls a group post-install
    # without needing to know the package list).  Task definitions
    # themselves are owned by the fork now — pkg.list groups serve as
    # build/pool categories, not tasksel sources.
    if pkg_groups:
        if not _stage_group_manifests(_staging, pkg_groups):
            return False

    # CONF-10 S3: identity-residue scan over the staged ISO root, after
    # all the text-staging steps have copied + substituted but before
    # binary-heavy steps (apt-repo generation, signing, mkrescue) — so
    # a residue hit aborts cheaply.  Reuses the audit_identity walker
    # + the same allowlist file as S1; pool/*.deb and other binaries
    # are filtered out by the scanner's _SKIP_GLOBS + NUL-byte probe.
    # Gated by [Audit] IdentityScan in build.conf (default true).
    if audit_identity_scan:
        if not _audit_staged_iso(_staging, dir_image):
            return False
    else:
        logger.warning(
            "_audit_staged_iso: SKIPPED via [Audit] IdentityScan = false "
            "— Debian residue can ship without flagging"
        )

    if not generate_apt_repo(_staging, suite, codename, version, password):
        return False

    # Sign Release with the project key and ship the matching pubkey at
    # .disk/archive-key.gpg.  Without these the target's apt rejects our
    # unsigned Release with "does not have a Release file" and the whole
    # apt-cdrom-setup chain falls apart (caught 2026-05-13 — see Phase C
    # diagnosis in docs/known-issues.md).  Both params None ⇒ skip
    # signing (useful for tests).
    if signing_homedir is not None:
        if not sign_release_files(
                _staging, suite, signing_homedir, password):
            return False
    if signing_pubkey_path is not None:
        if not export_pubkey_to_staging(
                _staging, signing_pubkey_path, password):
            return False

    _iso_path = os.path.join(dir_image, iso_basename)
    if not _run_grub_mkrescue(_staging, _iso_path, container, password):
        return False

    _report_iso(_iso_path)
    return True


# ---------------------------------------------------------------------------
# Helpers — one per mastering step
# ---------------------------------------------------------------------------


def _sudo(cmd_args: 'list[str]', password: str) -> subprocess.CompletedProcess:
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
    logger.info(f"prepare staging: wipe + recreate {staging}")
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
                 password: str,
                 expected_kernel_pkg: Optional[str] = None) -> Optional[str]:
    """Locate a usable vmlinuz.

    Strategy:
      1. Look for vmlinuz under dir_chroot_installer/boot/ — if a
         kernel-image-*-di udeb unpacked one, use it.  Self-contained.
      2. Fall back to extracting from repo/linux-image-*-amd64*.deb.

    Picker (Strategy 2): when expected_kernel_pkg is provided (the
    binary name the cache predicts, e.g. `linux-image-6.1.0-47-amd64`),
    PREFER .debs whose filename starts with that binary name.  This
    avoids picking a stale higher-ABI .deb left in repo/ from a
    pre-rollback snapshot — the canonical bug shape that bit us
    2026-05-19 (ISO ramdisk had ABI 47 modules but vmlinuz was
    ABI 48 because the picker grabbed the highest sorted glob match).

    Falls back to highest-ABI sort when no expected_kernel_pkg or
    no matching candidate exists.

    Returns absolute path to vmlinuz on success, None if neither
    strategy yields one.
    """
    logger.info(
        f"find kernel: chroot={dir_chroot_installer}/boot, repo={dir_repo}, "
        f"expected={expected_kernel_pkg or '-'}"
    )
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

    # Snapshot-aware pick: if the caller told us which binary name the
    # cache expects (e.g. `linux-image-6.1.0-47-amd64`), filter to that
    # first.  Without this filter, the fallback (highest sorted) picks
    # whichever stale ABI happens to be lexicographically newest on
    # disk — wrong after a snapshot rollback that left higher-ABI
    # binaries from the pre-rollback state.
    _preferred = []
    if expected_kernel_pkg:
        _prefix = expected_kernel_pkg + '_'
        _preferred = [_d for _d in _linux_debs
                      if os.path.basename(_d).startswith(_prefix)]
        if _preferred:
            tui.console.print(
                "Kernel picker: matching cache prediction "
                f"`{expected_kernel_pkg}` "
                f"({len(_preferred)} candidate(s) of {len(_linux_debs)})"
            )
        else:
            tui.console.print(
                f"Kernel picker: cache predicts `{expected_kernel_pkg}` but "
                f"no matching .deb in repo/ — falling back to highest-ABI",
                tui.COLOR_INFO,
            )

    # Pick the highest ABI version.  Sort key extracts the ABI tuple from
    # the package name so '6.1.0-47' > '6.1.0-9' lexicographically wrong
    # would otherwise be a hazard — but with consistent numeric padding
    # in Debian's ABI naming, sort-on-name is fine.  Use the last entry.
    _deb = (_preferred or _linux_debs)[-1]
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
    logger.info(f"stage kernel: {os.path.basename(kernel_src)} → boot/vmlinuz")
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
    logger.info(f"build initrd: cpio | gzip {dir_chroot_installer} → boot/initrd.gz")
    _initrd = os.path.join(staging, 'boot', 'initrd.gz')
    # `find . -print0 | cpio --null -o -H newc | gzip > initrd.gz`
    # Run as a single shell pipeline under sudo so cpio can read the
    # root-owned files.  cd into the chroot so paths inside the cpio are
    # relative to /.
    _shell_cmd = (
        f"cd {dir_chroot_installer} && "
        f"find . -print0 | cpio --null -o -H newc --quiet | "
        f"gzip -9 > {_initrd}"
    )
    # Spinner — cpio|gzip on a 200-300 MB installer chroot takes
    # 30-60s with no per-file output (--quiet).  Without a spinner
    # the TUI looks frozen mid-iso-build.
    _spin = tui.Spinner(f"Packing initrd (cpio | gzip) → {_initrd}")
    try:
        _r = _sudo(['bash', '-c', _shell_cmd], password)
    finally:
        _spin.done()
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
    """Copy installer/boot/grub.cfg → staging/boot/grub/grub.cfg, and
    any optional sibling boot assets (grub-background.png, etc.) into
    the same staged dir so grub.cfg can reference them by their basename.

    If the operator hasn't provided a grub.cfg (file absent), this is an
    error — without it grub-mkrescue produces an unusable ISO with no
    boot entries.  v1 ships a default; the operator can edit but not
    delete it.

    Optional assets (not fatal if absent):
      - grub-background.png — referenced by grub.cfg's `background_image`
        line for the COMP-01f Phase 2 boot splash.  When absent, GRUB's
        `if loadfont … ; then … background_image …; fi` guard simply
        skips the splash; boot still works in text mode.
    """
    logger.info(f"stage grub.cfg + boot assets from {installer_dir}/boot/")
    _src = os.path.join(installer_dir, 'boot', 'grub.cfg')
    if not os.path.exists(_src):
        tui.console.print(
            "ERROR: installer/boot/grub.cfg is missing.  Without a "
            "boot config the ISO has no menu entries."
        )
        logger.error(f"_stage_grub_cfg: {_src} absent")
        return False
    _grub_dir = os.path.join(staging, 'boot', 'grub')
    _dst = os.path.join(_grub_dir, 'grub.cfg')
    try:
        shutil.copy2(_src, _dst)
    except OSError as e:
        tui.console.print(f"ERROR: copy grub.cfg: {e}")
        logger.error(f"_stage_grub_cfg: {e}")
        return False
    tui.console.print(f"Boot menu: {_src} → boot/grub/grub.cfg")

    # Optional boot assets — copy each if present; skip silently if not.
    # Filenames here MUST match what grub.cfg references by basename.
    for _asset in ('grub-background.png',):
        _asrc = os.path.join(installer_dir, 'boot', _asset)
        if not os.path.exists(_asrc):
            continue
        try:
            shutil.copy2(_asrc, os.path.join(_grub_dir, _asset))
            tui.console.print(f"Boot asset: {_asrc} → boot/grub/{_asset}")
        except OSError as e:
            # Non-fatal — background splash is cosmetic, not load-bearing.
            logger.warning(f"_stage_grub_cfg: copy {_asset}: {e}")
    return True


def _stage_disk_info(
    staging: str, installer_dir: str, codename: str, version: str,
    snapshot: str = '',
) -> bool:
    """Copy installer/disk/* → staging/.disk/* (excluding *.md READMEs)
    with `${codename}`, `${version}` and `${snapshot}` placeholder
    substitution, and (when `snapshot` is set) write a `.disk/snapshot`
    marker so the media records which upstream snapshot it was built from
    (UPD-01 — distinguishes a base-snapshot disc from a stepped one).

    These files are d-i's "is this an installer disc?" marker convention:
    cdrom-detect parses the quoted codename out of /cdrom/.disk/info and
    uses it to locate /cdrom/dists/<codename>/Release; base-installer
    looks for /cdrom/.disk/base_installable; /cdrom/.disk/base_components
    tells base-installer which debootstrap components are in /cdrom/pool/.

    The codename in .disk/info MUST match the suite under dists/ for
    cdrom-detect to find the Release file.  Both come from the same
    source (`build.conf [Build] CODENAME`): apt_repo.generate_apt_repo
    names dists/<codename>/ and this helper substitutes ${codename} in
    .disk/info.  Caught 2026-05-11 — earlier static .disk/info said
    "athena" but the build's actual codename was "thor", so
    cdrom-detect reported "Error reading Release file".

    If installer/disk/ is absent, that's a hard error — cdrom-detect
    would silently reject the disc; better to fail loud at iso-build
    than have the operator boot and see "No installation media".
    """
    logger.info(
        f"stage .disk markers: codename={codename}, version={version}, "
        f"snapshot={snapshot or '-'}"
    )
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
    _vars = {'codename': codename, 'version': version, 'snapshot': snapshot}
    # Record the snapshot on the media as its own marker (doesn't touch the
    # cdrom-detect-parsed .disk/info format).
    if snapshot:
        try:
            with open(os.path.join(_dst_dir, 'snapshot'), 'w',
                      encoding='utf-8') as fh:
                fh.write(snapshot + '\n')
        except OSError as e:
            tui.console.print(f"ERROR: write .disk/snapshot: {e}")
            logger.error(f"_stage_disk_info snapshot marker: {e}")
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
            # Read source, substitute ${codename} / ${version} placeholders,
            # write to dest.  string.Template.safe_substitute leaves
            # unknown $variables untouched — operator-friendly.  Binary
            # files would break here, but .disk/ entries are all text.
            with open(_src, 'r', encoding='utf-8', errors='replace') as fh:
                _content = fh.read()
            _content = string.Template(_content).safe_substitute(_vars)
            with open(_dst, 'w', encoding='utf-8') as fh:
                fh.write(_content)
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
    tui.console.print(
        f"Disk markers: {_shipped} file(s) → .disk/ "
        f"(codename={codename}, version={version})"
    )
    return True


def _stage_base_include(staging: str, pkgs: Optional[list]) -> bool:
    """Write staging/.disk/base_include — one package name per line.

    base-installer reads /cdrom/.disk/base_include during bootstrap-base
    and appends those names to debootstrap's --include list.  Without
    this file, debootstrap installs only Priority: required + important,
    so the target ends up with a minimal stub instead of the same package
    set the ISO ships.

    Caught 2026-05-11 — first ISO boot reached bootstrap-base and
    reported "succeeded but requested to be left unconfigured" because
    base-installer had no list of what we actually want on the target.
    Generating this from dep_tree.selected_pkgs at iso-build time keeps
    target install set == ISO pool closure, no manual list to drift.
    """
    logger.info(
        f"stage base_include: {len(pkgs) if pkgs else 0} package(s) → "
        f".disk/base_include"
    )
    if not pkgs:
        tui.console.print("base_include: skipped (no package list provided)")
        return True
    _path = os.path.join(staging, '.disk', 'base_include')
    try:
        os.makedirs(os.path.dirname(_path), exist_ok=True)
        with open(_path, 'w', encoding='utf-8') as fh:
            for _name in pkgs:
                fh.write(_name + '\n')
    except OSError as e:
        tui.console.print(f"ERROR: write {_path}: {e}")
        logger.error(f"_stage_base_include {_path}: {e}")
        return False
    tui.console.print(
        f"base_include: {len(pkgs)} package(s) → .disk/base_include"
    )
    return True


def _stage_group_manifests(staging: str, pkg_groups: 'dict[str, set]') -> bool:
    """Write per-group package manifests under `staging/.disk/groups/`.

    One file per group, named `<group>.list`, containing one canonical
    package name per line (alpha-sorted for reproducibility).  The
    installer reads these at install time to drive its `apt-install`
    of operator-selected groups; the same files are useful for
    post-install operator scripts that want to enable a group
    manually (`xargs apt-get install -y < /cdrom/.disk/groups/gnome.list`).

    `[base]` is always emitted (even if it duplicates .disk/base_include)
    so consumers can treat all groups uniformly.

    Returns True on success / no-op (empty `pkg_groups`).
    """
    logger.info(
        f"stage group manifests: {len(pkg_groups) if pkg_groups else 0} "
        f"group(s) → .disk/groups/"
    )
    if not pkg_groups:
        return True
    _dir = os.path.join(staging, '.disk', 'groups')
    try:
        os.makedirs(_dir, exist_ok=True)
        for _group, _names in pkg_groups.items():
            _path = os.path.join(_dir, f'{_group}.list')
            with open(_path, 'w', encoding='utf-8') as fh:
                for _name in sorted(_names):
                    fh.write(_name + '\n')
    except OSError as e:
        tui.console.print(f"ERROR: write group manifest: {e}")
        logger.error(f"_stage_group_manifests: {e}")
        return False
    tui.console.print(
        f"groups: {len(pkg_groups)} manifest(s) → .disk/groups/"
        f" ({', '.join(sorted(pkg_groups.keys()))})"
    )
    return True


def _parse_deb_filename(filename: str) -> tuple:
    """Extract `(name, version)` from a `.deb` / `.udeb` filename.

    Debian binary filename convention: `<name>_<version>_<arch>.{deb,udeb}`
    where neither name nor version contains an underscore (policy
    requirement).  Versions with an epoch are encoded `1%3a2.3-4` in
    the filename (the `%3a` is the URL encoding of `:`).

    For files that don't match (e.g. `Packages.gz` accidentally in
    pool/), returns `('', '')` so the caller can skip them.
    """
    _base = filename
    if _base.endswith('.udeb'):
        _base = _base[:-len('.udeb')]
    elif _base.endswith('.deb'):
        _base = _base[:-len('.deb')]
    else:
        return '', ''
    _parts = _base.split('_')
    if len(_parts) < 3:
        return '', ''
    # filename version → control-file version: decode the epoch.
    _ver = _parts[1].replace('%3a', ':')
    return _parts[0], _ver


def _debian_version_cmp(a: str, b: str) -> int:
    """Compare two Debian version strings.  Returns -1/0/1.

    Uses apt_pkg.version_compare when available (canonical Debian
    semantics: epoch → upstream → debian-revision, with the segment
    grammar from policy 5.6.12).  Falls back to a simple lexicographic
    compare if apt_pkg isn't importable — degraded but never wrong
    for the common case of source-built packages with identical
    everything-except-the-revision (`6.1.170-1` vs `6.1.170-3` etc.).
    """
    try:
        import apt_pkg
        apt_pkg.init_system()
        return apt_pkg.version_compare(a, b)
    except Exception:
        if a == b:
            return 0
        return -1 if a < b else 1


def _select_pool_files(
    source_dirs: 'list[str]',
    deb_whitelist: 'Optional[set[str]]',
    exclude_names: 'Optional[set[str]]' = None,
) -> 'tuple[list[tuple[str, str]], int]':
    """Decide which files across `source_dirs` ship on the installer ISO.

    CONF-01 Stage D (2026-05-22): `source_dirs` is a LIST (was single
    str pre-Stage D).  Multiple dirs because .debs and .udebs now
    live in separate dirs under the unified apt-repo layout
    (dists/<codename>/main/binary-<arch>/ vs main/debian-installer/
    binary-<arch>/); the ISO's /cdrom/pool/ is FLAT, so we walk
    multiple sources and merge into one staging tree.

    `deb_whitelist` is one of:
      - set/iterable of canonical package names: keep .deb iff its
        package name is in the set AND it's not a dbgsym variant.
        When repo/ holds multiple versions of the same package (our
        source-build pipeline can leave older binaries behind), only
        the highest version per name survives — measured by Debian
        version-compare semantics.
      - None: legacy blanket-copy — every regular file kept.

    Rules when whitelist is a set:
      - Every `.udeb` is kept (anna may fetch any of them at install
        time — see docs/plans/comp-02-robust-build.md for the
        udeb-pool analysis).
      - `.deb` is kept iff its package name is in the whitelist AND
        does NOT end in `-dbgsym`.
      - When the same package name has multiple .deb files, only the
        highest version is kept; older versions are counted into
        skipped.
      - Anything else (sources, stray files) is skipped.

    Returns `(kept_list, skipped_count)` where kept_list is a list of
    (abs_source_path, filename) tuples — the caller flattens into
    staging by basename.

    Design note: this filter intentionally does NOT cross-walk
    versions against any external metadata (cache versions, base_include,
    etc.).  Our source-build pipeline produces filenames whose version
    form differs from Debian's apt indices (epoch stripped, no binNMU
    suffix) — pinning by cache version was fragile.  Pinning by
    "latest in repo/ per name" is robust to source-build version
    drift and still achieves the goal: drop older builds of the same
    package.
    """
    # Collect (src_dir, filename) across all source_dirs, sorted by
    # filename for determinism.
    _all: 'list[tuple[str, str]]' = []
    for _src_dir in source_dirs:
        try:
            for _name in sorted(os.listdir(_src_dir)):
                if os.path.isfile(os.path.join(_src_dir, _name)):
                    _all.append((_src_dir, _name))
        except OSError:
            continue

    if deb_whitelist is None:
        return _all, 0

    # Binaries a SHIPPED fork supersedes (Conflicts/Replaces) — drop them so an
    # upstream udeb (e.g. apt-setup-udeb superseded by athena-setup-udeb) can't
    # ride along on the ISO and run its generators (the security.debian.org bug).
    # d-i's anna/udpkg ignore Conflicts, so exclusion must be physical.
    _exclude = exclude_names or set()
    _udebs: 'list[tuple[str, str]]' = []
    _by_name: 'dict[str, tuple[str, tuple[str, str]]]' = {}   # name → (ver, (src_dir, fname))
    _skipped = 0
    for _src_dir, _name in _all:
        if _name.endswith('.udeb'):
            _upkg, _ = _parse_deb_filename(_name)
            if _upkg and _upkg in _exclude:
                _skipped += 1
                continue
            _udebs.append((_src_dir, _name))
            continue
        if not _name.endswith('.deb'):
            _skipped += 1
            continue
        _pkg, _ver = _parse_deb_filename(_name)
        if not _pkg or _pkg.endswith('-dbgsym'):
            _skipped += 1
            continue
        if _pkg in _exclude:
            _skipped += 1
            continue
        if _pkg not in deb_whitelist:
            _skipped += 1
            continue
        _existing = _by_name.get(_pkg)
        if _existing is None:
            _by_name[_pkg] = (_ver, (_src_dir, _name))
        elif _debian_version_cmp(_existing[0], _ver) < 0:
            _skipped += 1
            _by_name[_pkg] = (_ver, (_src_dir, _name))
        else:
            _skipped += 1
    _kept = _udebs + [_pair for _ver, _pair in _by_name.values()]
    return _kept, _skipped


def _stage_pool(
    source_dirs: 'list[str]', staging: str, password: str,
    deb_whitelist: 'Optional[set[str]]' = None,
    exclude_names: 'Optional[set[str]]' = None,
) -> bool:
    """Copy a filtered subset of source_dirs → staging/pool/ (FLAT).

    CONF-01 Stage D: `source_dirs` is a LIST of source dirs (was a
    single `dir_repo` pre-Stage D).  Walks each, merges into one
    flat staging/pool/ — the apt-cdrom logic on the install target
    expects flat pool/, regardless of how the build host organises
    repo/.

    The installer reads from /cdrom/pool at runtime (matches the locked
    decision: file:///cdrom apt source, no network repo fallback).

    When `deb_whitelist` is provided (the normal case from
    cmd_build_iso_installer), only packages the target system will
    actually install plus their Recommends and every .udeb get shipped.
    See `_select_pool_files` for the rules.  Without filtering, repo/
    can be 5+ GB containing dbgsym packages, unused kernel flavors
    (-rt, -cloud), old ABIs, live-exclusive packages — all dead weight
    on an installer ISO.

    Uses cp -a in batches (not rsync — caught 2026-05-12: rsync
    isn't always on the host): handles arbitrary file counts
    without hitting ARG_MAX, preserves modes/timestamps, runs under
    sudo for root-owned files in repo/.
    """
    logger.info(
        f"stage pool: scanning {len(source_dirs)} source dir(s); "
        f"whitelist={'on' if deb_whitelist else 'off'}, "
        f"exclude={len(exclude_names) if exclude_names else 0}"
    )
    _dst = os.path.join(staging, 'pool')
    _kept, _skipped = _select_pool_files(source_dirs, deb_whitelist,
                                         exclude_names)
    logger.info(
        f"stage pool: selected {len(_kept)} file(s), filtered {_skipped}"
    )
    if not _kept:
        tui.console.print(
            f"ERROR: pool selection produced 0 files from {source_dirs} "
            f"— whitelist likely too aggressive or sources are empty"
        )
        logger.error(
            f"_stage_pool: 0 files selected from {source_dirs} "
            f"(whitelist size {len(deb_whitelist) if deb_whitelist else 'None'})"
        )
        return False
    # Estimate the kept-set size for the operator-facing log line.
    _bytes = 0
    for _src_dir, _name in _kept:
        try:
            _bytes += os.path.getsize(os.path.join(_src_dir, _name))
        except OSError:
            pass
    _mb = _bytes // (2 ** 20)
    if deb_whitelist is not None:
        tui.console.print(
            f"Copying apt pool ({_mb} MB, {len(_kept)} files; "
            f"{_skipped} filtered out) — may take a few minutes..."
        )
    else:
        tui.console.print(
            f"Copying apt pool ({_mb} MB, {len(_kept)} files) — "
            "may take a few minutes..."
        )
    # Ensure the destination exists.  The previous blanket `cp -a
    # repo/. pool` created `pool/` implicitly as part of the directory
    # copy; the new file-by-file form with `-t pool` requires the
    # target to already exist.  Caught 2026-05-12.
    try:
        os.makedirs(_dst, exist_ok=True)
    except OSError as e:
        tui.console.print(f"ERROR: mkdir {_dst}: {e}")
        logger.error(f"_stage_pool mkdir {_dst}: {e}")
        return False
    # Copy in batches via `sudo cp -a -t <dst> -- <files...>`.  Batched
    # so we don't hit ARG_MAX with 1500+ filenames, and chunk-by-chunk
    # so a single failure surfaces with the offending batch rather than
    # an opaque whole-pool error.  cp -a is universally available;
    # rsync is not (caught 2026-05-12: `sudo: rsync: command not found`
    # on the host running the build).
    _BATCH = 200
    for _i in range(0, len(_kept), _BATCH):
        _chunk = _kept[_i:_i + _BATCH]
        _args = ['cp', '-a', '-t', _dst, '--']
        _args.extend(os.path.join(_src_dir, _n) for _src_dir, _n in _chunk)
        _r = _sudo(_args, password)
        if _r.returncode != 0:
            tui.console.print(
                f"ERROR: pool copy failed at batch {_i // _BATCH + 1} "
                f"of {(len(_kept) + _BATCH - 1) // _BATCH}: "
                f"{_r.stderr.strip()[:200]}"
            )
            logger.error(
                f"_stage_pool cp batch {_i}-{_i + len(_chunk)}: "
                f"rc={_r.returncode}, stderr={_r.stderr.strip()}"
            )
            return False
    return True


def _run_grub_mkrescue(staging: str, iso_path: str,
                         container: 'buildcontainer.BuildContainer',
                         password: str) -> bool:
    """Produce the hybrid BIOS+EFI bootable ISO from the staging tree.

    COMP-14 fix path (b) — runs grub-mkrescue inside the build
    container so the ISO embeds bookworm's GRUB toolchain instead of
    the build host's.  Container's apt is pinned to OUR snapshot, so
    `apt-get install grub-{common,pc-bin,efi-amd64-bin}` resolves to
    2.06-13 regardless of what's installed on the host (build hosts
    running Debian 13/trixie would otherwise leak GRUB 2.12 into the
    ISO's bootloader — see ticket history).

    Args:
        staging:   ISO source tree.
        iso_path:  Output ISO file.
        container: BuildContainer instance (host->container bridge).
                   Required (post-COMP-14); see docstring above for why
                   host grub-mkrescue is no longer used.
        password:  Host sudo password — passed through to BuildContainer
                   for symmetry with other ISO helpers (the container
                   itself runs as root with passwordless sudo, so the
                   password isn't actually needed inside).
    """
    logger.info(
        f"grub-mkrescue: staging={staging} → {os.path.basename(iso_path)} "
        f"(via build container)"
    )
    if container is None:
        tui.console.print(
            "ERROR: grub-mkrescue requires the build container "
            "(run `cache build` first to initialise it)"
        )
        logger.error("_run_grub_mkrescue: container is None")
        return False
    _spin = tui.Spinner(
        f"Running grub-mkrescue (bookworm container) → {os.path.basename(iso_path)}"
    )
    try:
        _ok, _stdout, _stderr = container.run_grub_mkrescue(
            staging, iso_path, password,
        )
    finally:
        _spin.done()
    for _line in _stdout.splitlines():
        logger.debug(_line)
    for _line in _stderr.splitlines():
        logger.debug(_line)
    if not _ok:
        tui.console.print(
            "ERROR: grub-mkrescue failed — see unified run log"
        )
        _tail = _stderr.strip().splitlines()[-3:] if _stderr.strip() else []
        logger.error(
            f"_run_grub_mkrescue: failed; stderr_tail={_tail}"
        )
        return False
    return True


def _audit_staged_iso(staging: str, dir_image: str) -> bool:
    """CONF-10 S3 — identity-residue scan over the staged ISO root.

    Walks `staging` for Debian-name tokens that survived staging-time
    substitution.  Reuses identity_scan.audit_identity (same walker
    + skip-globs the S1 fork audit uses; pool/*.deb is binary-filtered).

    Allowlist resolution: walks up the parents of `dir_image` looking
    for `audit/identity-allowlist`.  In production, `dir_image` is
    `<working>/buildroot/image` so the allowlist resolves in 2 hops.

    Returns True when no findings (or no allowlist found — degrades to
    a warning so a test fixture without the file doesn't break iso
    build).  False on any unallowlisted hit."""
    from identity_scan import audit_identity
    _working = dir_image
    _allow_path: 'Optional[str]' = None
    for _ in range(6):
        _working = os.path.dirname(_working)
        if not _working or _working == '/':
            break
        _candidate = os.path.join(_working, 'audit', 'identity-allowlist')
        if os.path.isfile(_candidate):
            _allow_path = _candidate
            break
    if _allow_path is None:
        logger.warning(
            f"_audit_staged_iso: no audit/identity-allowlist found above "
            f"{dir_image} — staged-ISO not gated for this build"
        )
        return True
    logger.info(
        f"audit staged ISO {staging} (allowlist={_allow_path})"
    )
    _findings = audit_identity(staging, _allow_path)
    if not _findings:
        logger.info("audit staged ISO: no identity residue")
        return True
    # Mirror each finding to the console so the operator sees the
    # actionable diagnostic on the active tab regardless of whether
    # they switch to the iso tab to read the logger.error stream.
    tui.console.print(
        f"ERROR: {len(_findings)} identity residue hit(s) in staged ISO:",
        tui.COLOR_ERROR,
    )
    for _f in _findings:
        _msg = (
            f"  {_f['path']}:{_f['line_no']} [{_f['token']}]: {_f['line']}"
        )
        tui.console.print(_msg, tui.COLOR_ERROR)
        logger.error(f"staged-ISO leak {_msg.strip()}")
    tui.console.print(
        f"Either rebrand the source or add an explicit entry to "
        f"{_allow_path}", tui.COLOR_ERROR,
    )
    return False


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
