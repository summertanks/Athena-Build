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
import string
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
    suite: str = 'athena',
    codename: str = 'athena',
    version: str = '0.1',
    base_include_pkgs: Optional[list] = None,
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

    if not _stage_disk_info(_staging, installer_dir, codename, version):
        return False

    if not _stage_base_include(_staging, base_include_pkgs):
        return False

    if not _stage_pool(dir_repo, _staging, password):
        return False

    if not _generate_apt_repo(_staging, suite, codename, version, password):
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


def _stage_disk_info(
    staging: str, installer_dir: str, codename: str, version: str,
) -> bool:
    """Copy installer/disk/* → staging/.disk/* (excluding *.md READMEs)
    with `${codename}` and `${version}` placeholder substitution.

    These files are d-i's "is this an installer disc?" marker convention:
    cdrom-detect parses the quoted codename out of /cdrom/.disk/info and
    uses it to locate /cdrom/dists/<codename>/Release; base-installer
    looks for /cdrom/.disk/base_installable; /cdrom/.disk/base_components
    tells base-installer which debootstrap components are in /cdrom/pool/.

    The codename in .disk/info MUST match the suite under dists/ for
    cdrom-detect to find the Release file.  Both come from the same
    source (`build.conf [Build] CODENAME`): _generate_apt_repo names
    dists/<codename>/ and this helper substitutes ${codename} in
    .disk/info.  Caught 2026-05-11 — earlier static .disk/info said
    "athena" but the build's actual codename was "thor", so
    cdrom-detect reported "Error reading Release file".

    If installer/disk/ is absent, that's a hard error — cdrom-detect
    would silently reject the disc; better to fail loud at iso-build
    than have the operator boot and see "No installation media".
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
    _vars = {'codename': codename, 'version': version}
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


def _generate_apt_repo(
    staging: str, suite: str, codename: str, version: str, password: str,
) -> bool:
    """Generate apt-repo metadata under staging/dists/<suite>/.

    Layout produced (Debian apt-repo convention):
      dists/<suite>/Release                                        (top-level)
      dists/<suite>/main/binary-amd64/{Release,Packages,Packages.gz,Packages.xz}
      dists/<suite>/main/debian-installer/binary-amd64/{Release,Packages,Packages.gz,Packages.xz}
      dists/<suite>/main/source/{Release,Sources,Sources.gz,Sources.xz}

    Pool layout stays FLAT — apt reads Filename: from each Packages
    record, which points into pool/ relative to the apt root.  No need
    to restructure into pool/<comp>/<initial>/<src>/.

    UNSIGNED for v1.  The target's apt sources.list will need
    `[trusted=yes]` to bypass signature verification.  Signing the
    Release file lands with CONF-02 phase 2 (the signing key from
    CONF-02 phase 1 is already in place).

    Tools used:
      dpkg-scanpackages  — scans pool/ for .debs / .udebs, emits Packages
      dpkg-scansources   — scans pool/ for .dsc, emits Sources
      apt-ftparchive release — generates the top-level Release with
                               SHA256 hashes of every Packages/Sources

    All three are standard Debian utilities in dpkg-dev + apt-utils.
    """
    _COMPONENT = 'main'
    _ARCH = 'amd64'

    _suite_base   = os.path.join(staging, 'dists', suite)
    _comp_base    = os.path.join(_suite_base, _COMPONENT)
    _binary_dir   = os.path.join(_comp_base, f'binary-{_ARCH}')
    _udeb_dir     = os.path.join(_comp_base, 'debian-installer', f'binary-{_ARCH}')
    _source_dir   = os.path.join(_comp_base, 'source')

    tui.console.print(f"Generating apt-repo metadata in dists/{suite}/...")

    # Step 1: directory hierarchy.  Made user-owned (no sudo) so the
    # subsequent sudo-driven dpkg-scanpackages output can write into them
    # — the parent staging tree is user-owned too.  Files inside will
    # become root-owned via the sudo invocation, which is fine.
    for _d in (_binary_dir, _udeb_dir, _source_dir):
        try:
            os.makedirs(_d, exist_ok=True)
        except OSError as e:
            tui.console.print(f"ERROR: mkdir {_d}: {e}")
            logger.error(f"_generate_apt_repo mkdir {_d}: {e}")
            return False

    # Step 2: regular .deb Packages index.  -m tolerates multiple
    # versions of the same package without warning (we have multiple
    # kernel ABIs etc. in repo/).  Run from staging/ so Filename:
    # entries are relative paths like `pool/foo.deb`.
    if not _scan_packages_to(
            staging, 'pool', os.path.join(_binary_dir, 'Packages'),
            password, udeb=False):
        return False

    # Step 3: udeb Packages index (Section: debian-installer records).
    # dpkg-scanpackages -t udeb filters to .udeb files only.
    if not _scan_packages_to(
            staging, 'pool', os.path.join(_udeb_dir, 'Packages'),
            password, udeb=True):
        return False

    # Step 4: source Sources index.  dpkg-scansources walks pool/ for
    # .dsc files (which our source-build pipeline lands alongside .debs).
    if not _scan_sources_to(
            staging, 'pool', os.path.join(_source_dir, 'Sources'),
            password):
        return False

    # Step 5: per-component Release files.  These pin Suite/Codename/
    # Component/Architecture on each binary-arch / source dir so apt can
    # verify they match what the top-level Release advertises.
    for _dir, _arch_label in (
        (_binary_dir, _ARCH),
        (_udeb_dir,   _ARCH),
        (_source_dir, 'source'),
    ):
        if not _write_subdir_release(
                _dir, suite, codename, _COMPONENT, _arch_label, password):
            return False

    # Step 6: top-level dists/<suite>/Release.  apt-ftparchive release
    # walks the sub-tree, hashes every Packages/Sources/Packages.gz/...,
    # and emits the SHA256: block apt verifies.  -o flags carry the
    # distro identity fields; without them apt-ftparchive emits empty
    # Suite/Codename and apt refuses the repo.
    if not _generate_top_release(
            staging, suite, codename, version,
            os.path.join(_suite_base, 'Release'), password):
        return False

    tui.console.print(
        f"apt-repo: dists/{suite}/ ready (unsigned — target sources.list "
        f"needs [trusted=yes])",
        tui.COLOR_HIGHLIGHT,
    )
    return True


def _scan_packages_to(
    staging: str, pool_subdir: str, output_path: str,
    password: str, udeb: bool,
) -> bool:
    """sudo dpkg-scanpackages -m [-t udeb] <pool_subdir> > <output> + compress.

    Run with cwd=staging so Packages records carry relative Filename
    entries (matching the layout apt walks via /cdrom/pool/...).
    """
    _flag = '-t udeb' if udeb else ''
    _label = 'udebs' if udeb else 'debs'
    tui.console.print(f"Scanning {pool_subdir}/ for {_label}...")
    _shell = (
        f'cd {staging} && '
        f'dpkg-scanpackages -m {_flag} {pool_subdir} 2>/dev/null '
        f'> {output_path}'
    )
    _r = _sudo(['bash', '-c', _shell], password)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: dpkg-scanpackages ({_label}) failed: "
            f"{_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_scan_packages_to {_label}: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        tui.console.print(
            f"ERROR: Packages output {output_path} is missing or empty"
        )
        logger.error(f"_scan_packages_to {_label}: empty output at {output_path}")
        return False
    _n = _count_records(output_path)
    tui.console.print(f"  → {_n} {_label} indexed")
    return _compress_index(output_path, password)


def _scan_sources_to(
    staging: str, pool_subdir: str, output_path: str, password: str,
) -> bool:
    """sudo dpkg-scansources <pool_subdir> > <output> + compress.

    Same cwd trick as _scan_packages_to.  dpkg-scansources tolerates
    pool/ subdirs with no .dsc — emits an empty Sources, still useful.
    """
    tui.console.print(f"Scanning {pool_subdir}/ for sources...")
    _shell = (
        f'cd {staging} && '
        f'dpkg-scansources {pool_subdir} 2>/dev/null '
        f'> {output_path}'
    )
    _r = _sudo(['bash', '-c', _shell], password)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: dpkg-scansources failed: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_scan_sources_to: rc={_r.returncode}, stderr={_r.stderr.strip()}"
        )
        return False
    _n = _count_records(output_path) if os.path.exists(output_path) else 0
    tui.console.print(f"  → {_n} sources indexed")
    return _compress_index(output_path, password)


def _compress_index(path: str, password: str) -> bool:
    """gzip -9k + xz -9k to produce Packages.gz / Packages.xz (or
    Sources.gz / Sources.xz).  -k keeps the original uncompressed file
    — apt accepts any of the three forms but the uncompressed one is
    what's hashed in the per-subdir Release."""
    for _tool in (['gzip', '-9', '-k', '-f', path],
                  ['xz',   '-9', '-k', '-f', path]):
        _r = _sudo(_tool, password)
        if _r.returncode != 0:
            tui.console.print(
                f"ERROR: {_tool[0]} compress {path}: "
                f"{_r.stderr.strip()[:200]}"
            )
            logger.error(
                f"_compress_index {_tool[0]} {path}: rc={_r.returncode}, "
                f"stderr={_r.stderr.strip()}"
            )
            return False
    return True


def _count_records(path: str) -> int:
    """Count Packages/Sources records.  Each record is a Package: (or
    a Source: stanza in Sources files) at column 0; records are
    separated by a blank line.  Sources records use 'Package: <name>'
    too (the field is named Package even in Sources)."""
    try:
        with open(path, 'r', errors='replace') as fh:
            _content = fh.read()
    except OSError:
        return 0
    return _content.count('\nPackage: ') + (
        1 if _content.startswith('Package: ') else 0
    )


def _write_subdir_release(
    target_dir: str, suite: str, codename: str, component: str,
    arch_label: str, password: str,
) -> bool:
    """Write a minimal per-subdir Release file pinning Suite/Codename/
    Component/Architecture.  apt cross-checks these against the top-level
    Release; mismatch → apt refuses the repo with a useful error.
    """
    _content = (
        f"Origin: Athena\n"
        f"Label: Athena\n"
        f"Archive: {suite}\n"
        f"Suite: {suite}\n"
        f"Codename: {codename}\n"
        f"Component: {component}\n"
        f"Architecture: {arch_label}\n"
        f"Description: Athena installer media — {component}/{arch_label}\n"
    )
    _path = os.path.join(target_dir, 'Release')
    # Write via sudo tee so the file lands root-owned next to the other
    # root-owned index files in this subdir.
    _shell = f"cat > {_path}"
    _r = subprocess.run(
        ['sudo', '-S', 'bash', '-c', _shell],
        input=password + '\n' + _content,
        capture_output=True, text=True,
    )
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: write {_path}: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_write_subdir_release {_path}: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    return True


def _generate_top_release(
    staging: str, suite: str, codename: str, version: str,
    output_path: str, password: str,
) -> bool:
    """apt-ftparchive release dists/<suite>/ > dists/<suite>/Release.

    The -o flags pin distro identity — apt-ftparchive emits empty
    Suite/Codename/etc. otherwise and apt then refuses the repo with
    "Repository ... does not have a Release file."

    NOT using `bash -c` for this call: some -o values can contain
    spaces (e.g. Description="Athena installer disc"), and shell
    word-splitting would chop them into separate tokens — apt-ftparchive
    then chokes with "Invalid operation installer".  Caught in
    production 2026-05-11.  Pass argv directly to subprocess.run with
    cwd= and stdout=file_handle for redirection.
    """
    _opts = [
        '-o', 'APT::FTPArchive::Release::Origin=Athena',
        '-o', 'APT::FTPArchive::Release::Label=Athena',
        '-o', f'APT::FTPArchive::Release::Suite={suite}',
        '-o', f'APT::FTPArchive::Release::Codename={codename}',
        '-o', f'APT::FTPArchive::Release::Version={version}',
        '-o', 'APT::FTPArchive::Release::Architectures=amd64',
        '-o', 'APT::FTPArchive::Release::Components=main',
        '-o', 'APT::FTPArchive::Release::Description=Athena installer disc',
    ]
    _argv = (
        ['sudo', '-S', 'apt-ftparchive'] + _opts +
        ['release', f'dists/{suite}']
    )
    try:
        with open(output_path, 'wb') as fh:
            _r = subprocess.run(
                _argv,
                input=(password + '\n').encode('utf-8'),
                stdout=fh,
                stderr=subprocess.PIPE,
                cwd=staging,
            )
    except OSError as e:
        tui.console.print(f"ERROR: open {output_path} for write: {e}")
        logger.error(f"_generate_top_release open: {e}")
        return False
    _stderr = (_r.stderr or b'').decode('utf-8', errors='replace').strip()
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: apt-ftparchive release failed (rc={_r.returncode}): "
            f"{_stderr[:200]}"
        )
        logger.error(
            f"_generate_top_release: rc={_r.returncode}, stderr={_stderr}"
        )
        return False
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        tui.console.print(
            f"ERROR: top-level Release at {output_path} is missing or empty"
        )
        logger.error(f"_generate_top_release: empty output at {output_path}")
        return False
    return True


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
