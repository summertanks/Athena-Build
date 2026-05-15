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
    deb_whitelist=None,
    signing_homedir: Optional[str] = None,
    signing_pubkey_path: Optional[str] = None,
    pkg_groups: Optional['dict[str, set]'] = None,
    group_meta: Optional['dict[str, dict[str, str]]'] = None,
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

    # GROUPS-01: when non-base groups exist we ship a synthetic
    # `athena-tasksel-data` package that drops `athena.desc` into
    # `/usr/share/tasksel/descs/` on /target.  Tasksel's discovery is
    # just `glob /usr/share/tasksel/descs/*.desc` (see
    # /usr/bin/tasksel:53-65) — so the .desc lands where tasksel reads
    # it via standard dpkg machinery; no pre-pkgsel hook gymnastics.
    # Inject the package name into base_include BEFORE staging so
    # debootstrap installs it on every /target.  The .deb is built
    # later (after _stage_pool) and dropped into pool/.
    _has_non_base_groups = bool(
        pkg_groups and any(_g != 'base' for _g in pkg_groups)
    )
    _effective_base_include = base_include_pkgs
    if _has_non_base_groups:
        _effective_base_include = list(base_include_pkgs or []) + [
            'athena-tasksel-data',
        ]

    if not _stage_base_include(_staging, _effective_base_include):
        return False

    if not _stage_pool(dir_repo, _staging, password, deb_whitelist):
        return False

    # GROUPS-01: per-group package manifests so the installer (and
    # post-install operator scripts) can apt-install a chosen group
    # from /cdrom/pool.  Ships plain-text lists at .disk/groups/<group>.list.
    if pkg_groups:
        if not _stage_group_manifests(_staging, pkg_groups):
            return False
        # Generate `.disk/athena-tasks.desc` for operator inspection
        # (xorriso-readable, no boot required).  This is the same
        # content that goes into the athena-tasksel-data .deb below.
        if not _stage_tasksel_desc(_staging, pkg_groups, group_meta or {}):
            return False
        # Build the synthetic .deb and drop into pool so debootstrap
        # picks it up via base_include + apt's cdrom: source.
        if _has_non_base_groups:
            if not _build_tasksel_data_deb(
                    _staging, pkg_groups, group_meta or {}, version):
                return False

    if not _generate_apt_repo(_staging, suite, codename, version, password):
        return False

    # Sign Release with the project key and ship the matching pubkey at
    # .disk/archive-key.gpg.  Without these the target's apt rejects our
    # unsigned Release with "does not have a Release file" and the whole
    # apt-cdrom-setup chain falls apart (caught 2026-05-13 — see Phase C
    # diagnosis in docs/known-issues.md).  Both params None ⇒ skip
    # signing (useful for tests).
    if signing_homedir is not None:
        if not _sign_release_files(
                _staging, suite, signing_homedir, password):
            return False
    if signing_pubkey_path is not None:
        if not _export_pubkey_to_staging(
                _staging, signing_pubkey_path, password):
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


def _stage_tasksel_desc(staging: str,
                        pkg_groups: 'dict[str, set]',
                        group_meta: 'dict[str, dict[str, str]]') -> bool:
    """Write a tasksel `.desc` file at `staging/.disk/athena-tasks.desc`
    listing every non-`[base]` group as a tasksel task.

    The installer's pre-pkgsel.d hook (shipped via the installer
    overlay) copies this file to `/target/usr/share/tasksel/descs/`
    before pkgsel runs `in-target tasksel`, so tasksel finds it when
    it scans for tasks during the "Software selection" step of d-i.

    `.desc` format (RFC-822 stanzas per task):

        Task: athena-<group>
        Section: athena
        Description: <one-line title>
         <optional extended body paragraph>
        Key:
         <seed package name>
         ...

    `Description:` comes from `group_meta[group].get('description')` if
    the operator declared `## Description: ...` in pkg.list under the
    `[group]` header; otherwise falls back to a default.

    `Key:` lists the SEEDS for the task (the canonical-name set we
    resolved at build time minus deps that have other roots).  We use
    the full canonical set here — tasksel re-resolves transitive deps
    via apt at install time, so duplicates with other tasks are
    harmless.

    `[base]` is intentionally omitted — base packages are installed at
    debootstrap time via base-installer's `base_include` mechanism,
    well before pkgsel runs.
    """
    if not pkg_groups:
        return True
    _non_base = [
        _g for _g in pkg_groups.keys() if _g != 'base'
    ]
    if not _non_base:
        # Only `[base]` defined — nothing for tasksel to offer.
        tui.console.print(
            "tasksel: only [base] group defined — skipping .desc generation"
        )
        return True
    _dir = os.path.join(staging, '.disk')
    _path = os.path.join(_dir, 'athena-tasks.desc')
    try:
        os.makedirs(_dir, exist_ok=True)
        _stanzas = []
        for _g in _non_base:
            _desc = (group_meta.get(_g, {}).get('description')
                     or f"Athena {_g} group")
            _keys = sorted(pkg_groups.get(_g, set()))
            _stanza = [
                f"Task: athena-{_g}",
                "Section: athena",
                f"Description: {_desc}",
                " Operator-selected install-time group from Athena's pkg.list.",
                "Key:",
            ]
            for _k in _keys:
                _stanza.append(f" {_k}")
            _stanzas.append('\n'.join(_stanza))
        with open(_path, 'w', encoding='utf-8') as fh:
            fh.write('\n\n'.join(_stanzas) + '\n')
    except OSError as e:
        tui.console.print(f"ERROR: write tasksel desc: {e}")
        logger.error(f"_stage_tasksel_desc: {e}")
        return False
    tui.console.print(
        f"tasksel: {len(_non_base)} task(s) → .disk/athena-tasks.desc "
        f"({', '.join(sorted(_non_base))})"
    )
    return True


def _build_tasksel_data_deb(
    staging: str,
    pkg_groups: 'dict[str, set]',
    group_meta: 'dict[str, dict[str, str]]',
    version: str,
) -> bool:
    """Build the synthetic `athena-tasksel-data_<ver>_all.deb` and
    drop it into `staging/pool/`.

    The .deb ships a single file — `usr/share/tasksel/descs/athena.desc`
    — generated from the operator's pkg.list groups.  tasksel's
    discovery is just `glob /usr/share/tasksel/descs/*.desc` (verbatim
    from upstream `/usr/bin/tasksel` lines 53-65); dpkg-installing the
    file is enough to make the tasks appear in the Software-selection
    menu.  Avoids the entire pre-pkgsel.d hook contraption — and the
    apt-cdrom mount-on-demand fight that made the hook unreliable.

    Why a synthetic .deb (rather than baking the .desc into the initrd,
    or copying it from /cdrom): the file is part of the system's
    install footprint, so dpkg should track it.  `athena-tasksel-data`
    appears in `dpkg -l` on /target like any other package; the
    operator can `dpkg -P athena-tasksel-data` to remove it.  Static
    initrd-baked / hook-copied files don't have that property.

    The .deb is in the pool only — we don't ship it in `repo/` (the
    source-build artefact area).  Generation happens at every
    `iso build installer` run so the .desc content stays in sync with
    the operator's current pkg.list groups.

    Version semantics: we use the ISO version (e.g. `0.1`) so all
    iso-build artefacts share the same version string.  No upstream-
    style versioning — this is purely an iso-build internal package
    that doesn't track an external source.
    """
    _non_base = [_g for _g in pkg_groups.keys() if _g != 'base']
    if not _non_base:
        # No-op — base-only installs don't need tasksel customisation.
        return True

    # Read the .desc we just wrote in _stage_tasksel_desc — single
    # source of truth for the file content (avoids the two helpers
    # generating slightly-different output).
    _desc_src = os.path.join(staging, '.disk', 'athena-tasks.desc')
    if not os.path.isfile(_desc_src):
        tui.console.print(
            f"ERROR: athena-tasks.desc not found at {_desc_src} — "
            "_stage_tasksel_desc must run first"
        )
        logger.error(f"_build_tasksel_data_deb: missing {_desc_src}")
        return False

    import tempfile
    _pool_dir = os.path.join(staging, 'pool')
    os.makedirs(_pool_dir, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix='athena-tasksel-data-') as _tmp:
            _pkg_root = os.path.join(_tmp, 'pkg')
            _debian_dir = os.path.join(_pkg_root, 'DEBIAN')
            _descs_dir = os.path.join(
                _pkg_root, 'usr', 'share', 'tasksel', 'descs',
            )
            os.makedirs(_debian_dir)
            os.makedirs(_descs_dir)

            # Payload — copy the staged .desc into the .deb's tasksel
            # discovery path.  tasksel will glob it from
            # /usr/share/tasksel/descs/athena.desc on /target.
            shutil.copy2(_desc_src, os.path.join(_descs_dir, 'athena.desc'))

            # Compute installed size in KB (Debian convention) — required
            # by policy 5.6.20.  Just the .desc file plus filesystem overhead.
            _payload_size_kb = max(
                1, (os.path.getsize(_desc_src) + 1023) // 1024,
            )

            _control = (
                f"Package: athena-tasksel-data\n"
                f"Version: {version}\n"
                f"Section: misc\n"
                f"Priority: optional\n"
                f"Architecture: all\n"
                f"Maintainer: Athena Build <athena@local>\n"
                f"Installed-Size: {_payload_size_kb}\n"
                # Depend on tasksel so dpkg doesn't install us into a
                # /target that's missing the consumer.  pkg.list [base]
                # already pins tasksel for an unrelated reason
                # (debconf wiring); this Depends is a belt+braces
                # declaration for any future operator who edits [base].
                f"Depends: tasksel\n"
                f"Description: Athena task descriptions for tasksel\n"
                f" Drops /usr/share/tasksel/descs/athena.desc, the operator-defined\n"
                f" task list generated at ISO-build time from pkg.list groups.\n"
                f" tasksel discovers it via its standard glob over the descs\n"
                f" directory at install/upgrade time.\n"
            )
            with open(os.path.join(_debian_dir, 'control'), 'w') as fh:
                fh.write(_control)

            _deb_name = f"athena-tasksel-data_{version}_all.deb"
            _deb_path = os.path.join(_pool_dir, _deb_name)
            _r = subprocess.run(
                ['dpkg-deb', '--build', '--root-owner-group',
                 _pkg_root, _deb_path],
                capture_output=True, text=True,
            )
            if _r.returncode != 0:
                tui.console.print(
                    f"ERROR: dpkg-deb --build failed: {_r.stderr.strip()}"
                )
                logger.error(
                    f"_build_tasksel_data_deb dpkg-deb rc={_r.returncode}: "
                    f"{_r.stderr.strip()}"
                )
                return False
    except OSError as e:
        tui.console.print(f"ERROR: athena-tasksel-data build: {e}")
        logger.error(f"_build_tasksel_data_deb: {e}")
        return False

    tui.console.print(
        f"tasksel: athena-tasksel-data_{version}_all.deb → pool/ "
        f"({_payload_size_kb} KB payload, {len(_non_base)} task(s))"
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


# Backwards-compat shim — older tests/callers may still reach for the
# name-only parser.  Wraps the new tuple-returning helper.
def _parse_deb_package_name(filename: str) -> str:
    return _parse_deb_filename(filename)[0]


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
    dir_repo: str, deb_whitelist,
) -> tuple:
    """Decide which files in repo/ ship on the installer ISO.

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

    Returns `(kept_list, skipped_count)`.

    Design note: this filter intentionally does NOT cross-walk
    versions against any external metadata (cache versions, base_include,
    etc.).  Our source-build pipeline produces filenames whose version
    form differs from Debian's apt indices (epoch stripped, no binNMU
    suffix) — pinning by cache version was fragile.  Pinning by
    "latest in repo/ per name" is robust to source-build version
    drift and still achieves the goal: drop older builds of the same
    package.
    """
    try:
        _entries = sorted(os.listdir(dir_repo))
    except OSError:
        return [], 0
    if deb_whitelist is None:
        _kept = [
            n for n in _entries
            if os.path.isfile(os.path.join(dir_repo, n))
        ]
        return _kept, 0
    _udebs: list = []
    _by_name: dict = {}   # canonical_name → (version_str, filename)
    _skipped = 0
    for _name in _entries:
        _full = os.path.join(dir_repo, _name)
        if not os.path.isfile(_full):
            continue
        if _name.endswith('.udeb'):
            _udebs.append(_name)
            continue
        if not _name.endswith('.deb'):
            _skipped += 1
            continue
        _pkg, _ver = _parse_deb_filename(_name)
        if not _pkg or _pkg.endswith('-dbgsym'):
            _skipped += 1
            continue
        if _pkg not in deb_whitelist:
            _skipped += 1
            continue
        _existing = _by_name.get(_pkg)
        if _existing is None:
            _by_name[_pkg] = (_ver, _name)
        elif _debian_version_cmp(_existing[0], _ver) < 0:
            # Newer version wins; the older one becomes dead weight.
            _skipped += 1
            _by_name[_pkg] = (_ver, _name)
        else:
            _skipped += 1
    _kept = _udebs + [_fn for _ver, _fn in _by_name.values()]
    return _kept, _skipped


def _stage_pool(
    dir_repo: str, staging: str, password: str,
    deb_whitelist=None,
) -> bool:
    """Copy a filtered subset of repo/ → staging/pool/.

    The installer reads from /cdrom/pool at runtime (matches the locked
    decision: file:///cdrom apt source, no network repo fallback).

    When `deb_whitelist` is provided (the normal case from
    cmd_build_iso_installer), only packages the target system will
    actually install plus their Recommends and every .udeb get shipped.
    See `_select_pool_files` for the rules.  Without filtering, repo/
    can be 5+ GB containing dbgsym packages, unused kernel flavors
    (-rt, -cloud), old ABIs, live-exclusive packages — all dead weight
    on an installer ISO.

    Uses rsync --files-from for the copy: handles arbitrary file counts
    without hitting ARG_MAX, preserves modes/timestamps like cp -a,
    runs under sudo for root-owned files in repo/.
    """
    _dst = os.path.join(staging, 'pool')
    _kept, _skipped = _select_pool_files(dir_repo, deb_whitelist)
    if not _kept:
        tui.console.print(
            f"ERROR: pool selection produced 0 files from {dir_repo} — "
            "whitelist likely too aggressive or repo/ is empty"
        )
        logger.error(
            f"_stage_pool: 0 files selected from {dir_repo} "
            f"(whitelist size {len(deb_whitelist) if deb_whitelist else 'None'})"
        )
        return False
    # Estimate the kept-set size for the operator-facing log line.
    _bytes = 0
    for _name in _kept:
        try:
            _bytes += os.path.getsize(os.path.join(dir_repo, _name))
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
        _args.extend(os.path.join(dir_repo, _n) for _n in _chunk)
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

    The Release file is unsigned at the end of this function — signing
    happens in _sign_release_files, invoked from build_installer_iso
    after the apt-repo is laid out.  Splitting the steps keeps each
    helper focused on one job and makes the failure modes distinct
    (apt-ftparchive failure vs gpg failure).

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
        f"apt-repo: dists/{suite}/ ready (unsigned — _sign_release_files "
        "will sign next)",
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


def _sign_release_files(
    staging: str, suite: str, signing_homedir: str, password: str,
) -> bool:
    """Sign dists/<suite>/Release with our project key.

    Produces two artifacts apt verifies on the target:
      Release.gpg  — detached signature, classic v1 format
      InRelease    — clearsigned (signature inline), modern preferred format

    apt fetches InRelease first; if absent, falls back to
    Release + Release.gpg.  Shipping both maximises compatibility with
    older apt versions that don't speak InRelease.  Both files MUST be
    signed by a key the target trusts — we ship the matching pubkey at
    .disk/archive-key.gpg so the install-time hook can install it into
    /target/etc/apt/trusted.gpg.d/ before its apt-cdrom add call.

    Caught 2026-05-13: without these, apt-get update on the target said
    "does not have a Release file" — apt-setup's verify step then
    discarded 40cdrom's output and the installed target's sources.list
    never got a working cdrom entry, so any post-install apt-get
    install failed with "Unable to locate package".  See Phase C
    diagnosis in docs/known-issues.md.
    """
    _release = os.path.join(staging, 'dists', suite, 'Release')
    if not os.path.isfile(_release):
        tui.console.print(
            f"ERROR: {_release} missing — apt-repo generation must run first"
        )
        logger.error(f"_sign_release_files: {_release} absent")
        return False
    if not os.path.isdir(signing_homedir):
        tui.console.print(
            f"ERROR: signing homedir {signing_homedir} absent — run "
            "'signing keygen' (or whatever sets up the project key) first"
        )
        logger.error(f"_sign_release_files: {signing_homedir} absent")
        return False
    _release_gpg = _release + '.gpg'
    _inrelease   = os.path.join(os.path.dirname(_release), 'InRelease')
    # gpg refuses to overwrite by default; remove any stale signatures
    # from a previous build run first.
    for _f in (_release_gpg, _inrelease):
        if os.path.exists(_f):
            try:
                os.unlink(_f)
            except OSError as e:
                tui.console.print(f"ERROR: rm {_f}: {e}")
                logger.error(f"_sign_release_files rm {_f}: {e}")
                return False
    # Detached, ASCII-armored signature → Release.gpg.  --batch + --yes
    # so gpg never prompts (we always overwrite the previously-removed file).
    _argv = [
        'gpg', '--homedir', signing_homedir,
        '--batch', '--yes',
        '--output', _release_gpg,
        '--detach-sign', '--armor',
        _release,
    ]
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: gpg --detach-sign Release: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_sign_release_files detach-sign: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    # Clearsigned (signature wraps the original content) → InRelease.
    _argv = [
        'gpg', '--homedir', signing_homedir,
        '--batch', '--yes',
        '--output', _inrelease,
        '--clearsign',
        _release,
    ]
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: gpg --clearsign Release: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_sign_release_files clearsign: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    tui.console.print(
        f"Release signed: dists/{suite}/Release.gpg + InRelease"
    )
    return True


def _export_pubkey_to_staging(
    staging: str, signing_pubkey_path: str, password: str,
) -> bool:
    """Copy the project pubkey to staging/.disk/archive-key.gpg.

    The install-time hook (base-installer quilt patch, see
    patch/source/base-installer/) reads this and copies it to
    /target/etc/apt/trusted.gpg.d/athena-archive-keyring.gpg BEFORE
    running its apt-cdrom add call.  Without the keyring installed,
    apt rejects our signed Release with "NO_PUBKEY" and the chain
    falls apart the same way an unsigned Release would.

    The pubkey was already exported by signing.generate_key at project
    setup time (CONF-02 phase 1); we just copy it onto the disc.  The
    .disk/ directory is created earlier by _stage_disk_info, so we
    just need a write into an existing user-owned dir — no sudo.
    """
    if not os.path.isfile(signing_pubkey_path):
        tui.console.print(
            f"ERROR: pubkey {signing_pubkey_path} missing — run "
            "'signing keygen' (or whatever sets up the project key) first"
        )
        logger.error(
            f"_export_pubkey_to_staging: {signing_pubkey_path} absent"
        )
        return False
    _dst_dir = os.path.join(staging, '.disk')
    if not os.path.isdir(_dst_dir):
        tui.console.print(
            f"ERROR: {_dst_dir} missing — _stage_disk_info must run first"
        )
        logger.error(f"_export_pubkey_to_staging: {_dst_dir} absent")
        return False
    _dst = os.path.join(_dst_dir, 'archive-key.gpg')
    try:
        shutil.copyfile(signing_pubkey_path, _dst)
        os.chmod(_dst, 0o644)
    except OSError as e:
        tui.console.print(f"ERROR: copy pubkey to {_dst}: {e}")
        logger.error(f"_export_pubkey_to_staging copy: {e}")
        return False
    tui.console.print(
        "Pubkey exported: .disk/archive-key.gpg "
        f"({os.path.getsize(_dst)} bytes)"
    )
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
