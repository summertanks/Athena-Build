"""Generate fork/ as a local Debian mirror at cache-build time.

Walks fork/source/<pkg>/ for valid Debian source trees, runs dpkg-source -b
against each to produce .dsc + .tar.xz in fork/source/repo/, then emits
fork/{Packages, Packages-udeb, Sources, Release} (plus .gz variants) so the
Cache can ingest fork as a file:// Mirror without any fork-specific code
beyond mirror enumeration.

See docs/plans/fork-source.md Step 2 for the design.  The supersede
behaviour (fork pkgs drop upstream entries with the same Package: name)
lives in scripts/cache.py, not here — this module is purely metadata
generation.

Public API:
    generate_fork_mirror(buildconfig) -> bool
    register_fork_mirror(mirrors, buildconfig) -> List[Mirror]
"""
import glob
import gzip
import hashlib
import logging
import os
import shutil
import subprocess
import time
from typing import Dict, List, Tuple

from debian.changelog import Changelog
from debian.deb822 import Deb822

import tui
import utils

logger = logging.getLogger('athena')

# Mandatory Packages fields per the Debian spec — populated with safe
# placeholders for unbuilt binaries.  Cache never consults Size/SHA256
# for fork pkgs (they're never tunneled — always source-built) so the
# placeholders are inert.
_PLACEHOLDER_MD5    = '0' * 32
_PLACEHOLDER_SHA256 = '0' * 64

# Index file basenames generated in dir_fork.  Order matters for Release
# file row order (matches what apt-ftparchive emits for visual parity).
_INDEX_FILES = ('Packages', 'Packages-udeb', 'Sources')

_DPKG_SOURCE_TIMEOUT = 120  # seconds; native pkg tarball is fast (<1s typical)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_fork_mirror(buildconfig: 'utils.BuildConfig') -> bool:
    """Walk fork/source/, generate source pkgs + index files for fork/.

    Returns True if at least one fork pkg was discovered and its metadata
    written; False if fork/source/ is empty (no Mirror should be
    registered — skip-if-empty per FORK-01 plan Q6).

    Side effects (on True return):
        - fork/source/repo/*.dsc + *.tar.* generated via dpkg-source -b
        - fork/{Packages, Packages-udeb, Sources} written
        - fork/{Packages.gz, Packages-udeb.gz, Sources.gz} written
        - fork/Release written with hashes of all of the above

    On False return: no files written, no Mirror should be registered.
    """
    dir_fork             = buildconfig.dir_fork
    dir_fork_source      = buildconfig.dir_fork_source
    dir_fork_source_repo = buildconfig.dir_fork_source_repo
    codename             = buildconfig.build_codename

    pkg_dirs = _discover_fork_source_trees(dir_fork_source)
    if not pkg_dirs:
        logger.info(f"fork_mirror: no source trees in {dir_fork_source} — skipping mirror generation")
        return False

    # Defensive — BuildConfig.__init__ creates these, but the helper may
    # also be invoked from tests that don't construct a full BuildConfig.
    os.makedirs(dir_fork, exist_ok=True)
    os.makedirs(dir_fork_source_repo, exist_ok=True)

    tui.console.print(
        f"fork_mirror: discovered {len(pkg_dirs)} source tree(s) in fork/source/"
    )

    # Change-detection + cache invalidation: for each fork pkg whose
    # tree content differs from its persisted tree-hash, wipe every
    # downstream artifact (fork tarball, source/ copy, repo/ debs,
    # build logs) so the rebuild starts from clean state.  Eliminates
    # the "fork edited but stale .deb in repo/ keeps shipping" hazard
    # that bit us with athena-tasksel + packages/list 2026-05-18.
    for _pkg_dir in pkg_dirs:
        _check_and_invalidate_fork_pkg(_pkg_dir, buildconfig)

    # CONF-11: audit each fork pkg's own build system for paths it
    # references but doesn't have.  Surfaces the 2026-05-18 athena-tasksel
    # / `packages/list` class of bug at cache-build time rather than deep
    # inside a failed dpkg-buildpackage with an opaque `install: cannot
    # stat` error.  Warnings only — operator triages.
    for _pkg_dir in pkg_dirs:
        _findings = audit_fork_tree(_pkg_dir)
        if not _findings:
            continue
        _pkg_name = os.path.basename(_pkg_dir)
        tui.console.print(
            f"fork_mirror: {_pkg_name}: {len(_findings)} dangling "
            f"build-system reference(s) — see log", tui.COLOR_WARNING)
        for _f in _findings:
            logger.warning(
                f"audit_fork_tree {_pkg_name}: "
                f"{_f['file']}:{_f['line_no']} → '{_f['source']}' "
                f"({_f['reason']})")

    src_pkg_files = _generate_source_packages(pkg_dirs, dir_fork_source_repo)
    if not src_pkg_files:
        tui.console.print(
            "fork_mirror: dpkg-source -b produced no usable output — skipping mirror"
        )
        logger.warning("fork_mirror: src_pkg_files empty after generation pass")
        return False

    deb_stanzas, udeb_stanzas = _build_packages_stanzas(pkg_dirs, buildconfig.arch)
    src_stanzas = _build_sources_stanzas(src_pkg_files)

    _write_index(os.path.join(dir_fork, 'Packages'),      deb_stanzas)
    _write_index(os.path.join(dir_fork, 'Packages-udeb'), udeb_stanzas)
    _write_index(os.path.join(dir_fork, 'Sources'),       src_stanzas)

    for _name in _INDEX_FILES:
        _gzip_file(os.path.join(dir_fork, _name))

    _write_release(dir_fork, codename, list(_INDEX_FILES))

    tui.console.print(
        f"fork_mirror: emitted Release + {len(deb_stanzas)} deb(s) + "
        f"{len(udeb_stanzas)} udeb(s) + {len(src_stanzas)} source(s)"
    )

    # Persist tree hashes AFTER successful regen so the next cache build
    # can detect "no change since last run" and skip the invalidation
    # wipe.  Done here (not inside _generate_source_packages) so the
    # hash reflects the FULLY-PROCESSED state: source pkg + index files
    # all written, mirror is self-consistent.
    for _pkg_dir in pkg_dirs:
        if os.path.basename(_pkg_dir) in src_pkg_files:
            _persist_tree_hash(_pkg_dir, buildconfig)

    return True


def register_fork_mirror(mirrors: List['utils.Mirror'],
                         buildconfig: 'utils.BuildConfig') -> List['utils.Mirror']:
    """Return mirrors with the fork Mirror PREPENDED at index 0.

    Cache parses mirrors in declaration order; fork-first means the
    cache's `_fork_pkg_names` set is populated BEFORE upstream walks
    begin, so upstream same-name records can be dropped on the spot
    (see cache.py supersede logic).

    The fork Mirror uses flat-layout signals (component=''), which
    Mirror's @property accessors recognise (see utils.Mirror.is_flat).
    """
    _fork = utils.Mirror(
        id        = 'fork',
        baseurl   = 'file://' + buildconfig.working_dir,
        baseid    = 'fork',
        release   = './',     # apt convention for flat layout; suite=='./'
        suffix    = '',
        component = '',       # flat-layout signal — Mirror.is_flat checks this
        arch      = buildconfig.arch,
    )
    return [_fork] + list(mirrors)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_fork_source_trees(dir_fork_source: str) -> List[str]:
    """Return absolute paths to each fork/source/<pkg>/ that has a valid
    debian/control file.

    Skip rules:
      - `repo/` is the helper output dir; not a fork.
      - non-directory entries (stray files).
      - missing `debian/control` (not a Debian source tree).
      - presence of a `.disabled` marker file at the fork-pkg root.
        Operator uses `source fork <pkg> disabled` to hide a fork from
        cache ingest without deleting it; `source fork <pkg> enabled`
        removes the marker.  Disabled forks still take disk space and
        keep their git history (when ignored under VCS); they just
        don't show up in apt's view of the build universe.
    """
    _found: List[str] = []
    try:
        _entries = sorted(os.listdir(dir_fork_source))
    except OSError as e:
        logger.warning(f"fork_mirror: cannot list {dir_fork_source}: {e}")
        return []

    for _name in _entries:
        if _name == 'repo':
            continue
        _pkg_dir = os.path.join(dir_fork_source, _name)
        if not os.path.isdir(_pkg_dir):
            continue
        if os.path.exists(os.path.join(_pkg_dir, '.disabled')):
            logger.info(
                f"fork_mirror: {_name} is disabled (.disabled marker) — "
                f"skipping"
            )
            continue
        _control = os.path.join(_pkg_dir, 'debian', 'control')
        if not os.path.isfile(_control):
            logger.warning(
                f"fork_mirror: {_pkg_dir} missing debian/control — skipping"
            )
            continue
        _found.append(_pkg_dir)
    return _found


# ---------------------------------------------------------------------------
# Change detection + cache invalidation
# ---------------------------------------------------------------------------

# debian/control fields that influence apt resolution / dep tree.  When
# any of these change, the cache + dep tree are stale and need a full
# rebuild — `repo reload` refuses the light path in that case.
# Everything else (Description, Maintainer, Homepage, Vcs-*, Standards-
# Version, file content under data/ tasks/, Makefile changes, etc.) is
# package-local and a light reload is safe.
_DEP_AFFECTING_FIELDS = frozenset((
    'Source', 'Package', 'Architecture',
    'Depends', 'Pre-Depends', 'Recommends', 'Suggests', 'Enhances',
    'Provides', 'Conflicts', 'Replaces', 'Breaks',
))


def _compute_dep_hash(pkg_dir: str) -> str:
    """SHA256 over debian/changelog version + debian/control's dep-
    affecting fields, in stable canonical form.

    Used by `repo reload` to distinguish "this edit only affects
    package content" (light rebuild safe) from "this edit changes apt
    resolution" (need to re-run cache build + dep parse).

    Field set: see _DEP_AFFECTING_FIELDS.  Normalisation: whitespace
    collapsed via `' '.join(value.split())` so a line-fold reflow
    doesn't shift the hash.  Stanza ordering: deterministic by
    Package/Source name.

    Missing files or parse errors yield a stable "empty" digest so a
    truly-broken fork doesn't crash callers — the comparison still
    detects "something changed" downstream.
    """
    _h = hashlib.sha256()

    # Changelog version goes first.  A version bump alone always
    # gates (sibling cross-refs like `(= ${binary:Version})` would
    # otherwise resolve to wrong values).
    try:
        with open(os.path.join(pkg_dir, 'debian', 'changelog')) as fh:
            _ver = str(Changelog(fh.read()).version)
    except (OSError, Exception) as e:
        logger.warning(f"fork_mirror: changelog unreadable for {pkg_dir}: {e}")
        _ver = ''
    _h.update(b'version|')
    _h.update(_ver.encode('utf-8'))
    _h.update(b'\0')

    # Parse debian/control via Deb822 — handles line-folded values
    # + multi-stanza forks correctly.
    try:
        with open(os.path.join(pkg_dir, 'debian', 'control')) as fh:
            for _stanza in Deb822.iter_paragraphs(fh):
                _name = _stanza.get('Package', _stanza.get('Source', '<unknown>'))
                _h.update(b'stanza|')
                _h.update(_name.encode('utf-8'))
                _h.update(b'\0')
                for _field in sorted(_DEP_AFFECTING_FIELDS):
                    if _field in _stanza:
                        _val = ' '.join(_stanza[_field].split())
                        _h.update(_field.encode('utf-8'))
                        _h.update(b'=')
                        _h.update(_val.encode('utf-8'))
                        _h.update(b'\0')
    except OSError as e:
        logger.warning(f"fork_mirror: control unreadable for {pkg_dir}: {e}")
    return _h.hexdigest()


def load_pkg_hashes(pkg_name: str, dir_fork_source_repo: str) -> tuple:
    """Return (stored_tree_hash, stored_dep_hash) for a pkg.  Empty
    strings for missing/unreadable sidecars."""
    _result = []
    for _ext in ('.tree-hash', '.dep-hash'):
        _path = os.path.join(dir_fork_source_repo, pkg_name + _ext)
        _value = ''
        try:
            with open(_path) as fh:
                _value = fh.read().strip()
        except OSError:
            pass
        _result.append(_value)
    return tuple(_result)


def _binary_names_from_control(pkg_dir: str) -> List[str]:
    """Parse debian/control's Package: stanzas, return all binary names.

    Used by the invalidation wipe to cover multi-binary forks: for
    athena-tasksel the source is `athena-tasksel` but the produced
    binaries are `athena-tasksel` + `athena-tasksel-data`.  A glob by
    source name alone misses the data binary because of the dash-vs-
    underscore split (`athena-tasksel-data_*` doesn't match
    `athena-tasksel_*`).
    """
    _names: List[str] = []
    _ctrl = os.path.join(pkg_dir, 'debian', 'control')
    try:
        with open(_ctrl) as fh:
            for _line in fh:
                if _line.startswith('Package:'):
                    _names.append(_line.split(':', 1)[1].strip())
    except OSError as e:
        logger.warning(f"fork_mirror: cannot parse {_ctrl}: {e}")
    return _names


def _wipe_fork_pkg_outputs(pkg_name: str, binary_names: List[str],
                           buildconfig: 'utils.BuildConfig') -> None:
    """Delete every artifact derived from a fork pkg whose tree changed.

    Targets:
      - fork/source/repo/<src>_* (dsc + tar.xz from dpkg-source -b)
      - fork/source/repo/<src>.tree-hash (stale; rewritten post-regen)
      - source/<src>_* (downloaded copy + .verified sidecar)
      - repo/<bin>_* for each binary in debian/control (.deb / .udeb)
      - log/build/<src>{,.result,.patchhash} (build log + sidecars)
    """
    _targets: List[str] = []

    # Source-named artifacts (.dsc, .tar.xz, .verified, .tree-hash)
    for _dir in (buildconfig.dir_fork_source_repo, buildconfig.dir_source):
        _targets.extend(glob.glob(os.path.join(_dir, f'{pkg_name}_*')))
    _targets.append(
        os.path.join(buildconfig.dir_fork_source_repo, f'{pkg_name}.tree-hash')
    )
    _targets.append(
        os.path.join(buildconfig.dir_fork_source_repo, f'{pkg_name}.dep-hash')
    )

    # Per-binary artifacts across all repo/ subdirs (covers multi-binary
    # forks that emit a mix of main/dev/doc/dbgsym/tests).  CONF-01
    # Stage D: artifacts now live nested under
    # dists/<codename>/<comp>/binary-<arch>/ (.debs) or main/
    # debian-installer/binary-<arch>/ (.udebs).  Walk config.
    # all_deb_dirs() to cover all of them at once.
    for _deb_dir in buildconfig.all_deb_dirs():
        for _bin in binary_names:
            _targets.extend(glob.glob(
                os.path.join(_deb_dir, f'{_bin}_*'),
            ))

    # Build-log sidecars (source-named)
    _build_log_dir = os.path.join(buildconfig.dir_log, 'build')
    for _suffix in ('', '.result', '.patchhash'):
        _targets.append(os.path.join(_build_log_dir, pkg_name + _suffix))

    _wiped = 0
    for _t in _targets:
        if not (os.path.isfile(_t) or os.path.islink(_t)):
            continue
        try:
            os.remove(_t)
            _wiped += 1
            logger.info(f"fork_mirror: wiped stale {_t}")
        except OSError as e:
            logger.warning(f"fork_mirror: couldn't wipe {_t}: {e}")
    if _wiped:
        tui.console.print(
            f"fork_mirror: {pkg_name} — wiped {_wiped} stale artifact(s)"
        )


def _check_and_invalidate_fork_pkg(pkg_dir: str,
                                   buildconfig: 'utils.BuildConfig') -> bool:
    """Compare current tree hash against persisted .tree-hash; on
    mismatch (including first-run-no-hash), wipe downstream artifacts
    so they regenerate cleanly.

    Returns True if invalidation happened, False if hash matched
    (no-op, cached artifacts still valid).
    """
    _pkg_name = os.path.basename(pkg_dir)
    _hash_file = os.path.join(
        buildconfig.dir_fork_source_repo, f'{_pkg_name}.tree-hash'
    )
    _current = utils.compute_tree_hash(pkg_dir)
    _stored = ''
    try:
        with open(_hash_file) as fh:
            _stored = fh.read().strip()
    except OSError:
        pass

    # First-run case (no stored hash): if no existing artifacts either,
    # this is benign — the regen flow will produce them from scratch.
    # If artifacts DO exist without a stored hash, they're orphans from
    # a pre-invalidation-mechanism era (or a partial wipe); treat as
    # mismatch and wipe so subsequent runs are deterministic.
    if _current == _stored and _stored:
        return False

    logger.info(
        f"fork_mirror: tree-hash mismatch for {_pkg_name} "
        f"(stored={_stored or '<none>'}, current={_current[:12]}…); invalidating"
    )
    _binary_names = _binary_names_from_control(pkg_dir)
    _wipe_fork_pkg_outputs(_pkg_name, _binary_names, buildconfig)
    return True


def _persist_tree_hash(pkg_dir: str,
                       buildconfig: 'utils.BuildConfig') -> None:
    """Write current tree-hash AND dep-hash AFTER successful regen.
    Both sidecars are kept in lockstep so `repo reload`'s
    light-vs-gate decision has consistent baselines.  Idempotent."""
    _pkg_name = os.path.basename(pkg_dir)
    _tree_file = os.path.join(
        buildconfig.dir_fork_source_repo, f'{_pkg_name}.tree-hash'
    )
    _dep_file = os.path.join(
        buildconfig.dir_fork_source_repo, f'{_pkg_name}.dep-hash'
    )
    _tree = utils.compute_tree_hash(pkg_dir)
    _dep  = _compute_dep_hash(pkg_dir)
    for _path, _value in ((_tree_file, _tree), (_dep_file, _dep)):
        try:
            with open(_path, 'w') as fh:
                fh.write(_value + '\n')
        except OSError as e:
            logger.warning(
                f"fork_mirror: couldn't persist {os.path.basename(_path)} "
                f"for {_pkg_name}: {e}"
            )


# ---------------------------------------------------------------------------
# Source package generation (dpkg-source -b)
# ---------------------------------------------------------------------------

def _generate_source_packages(pkg_dirs: List[str],
                              dst_dir: str) -> Dict[str, List[str]]:
    """Run dpkg-source -b for each pkg dir; output lands in dst_dir.

    Returns: {pkg_name: [dsc_path, tar_path, ...]} for each successfully
    built source pkg.  Failures are logged + skipped (don't fail the
    whole helper).

    Stale detection: if `<pkg>_<ver>.dsc` already exists and is newer
    than the pkg's debian/changelog, skip rebuild (avoids re-running
    dpkg-source on every cache build).  Operator can force-rebuild by
    `rm -rf fork/source/repo/`.
    """
    _result: Dict[str, List[str]] = {}

    for _pkg_dir in pkg_dirs:
        _pkg_name = os.path.basename(_pkg_dir)
        try:
            _ver = _read_pkg_version(_pkg_dir)
        except (OSError, ValueError) as e:
            logger.warning(f"fork_mirror: {_pkg_name} changelog unreadable: {e}")
            continue

        _dsc = os.path.join(dst_dir, f'{_pkg_name}_{_filename_version(_ver)}.dsc')
        _changelog = os.path.join(_pkg_dir, 'debian', 'changelog')

        if (os.path.isfile(_dsc) and
                os.path.getmtime(_dsc) > os.path.getmtime(_changelog)):
            logger.info(f"fork_mirror: {_pkg_name} src pkg up to date")
            _result[_pkg_name] = _list_files_for_pkg(dst_dir, _pkg_name, _ver)
            continue

        # dpkg-source -b emits to CWD; cd into dst_dir so output lands there
        try:
            _r = subprocess.run(
                ['dpkg-source', '-b', _pkg_dir],
                cwd=dst_dir,
                capture_output=True, text=True, timeout=_DPKG_SOURCE_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            tui.console.print(f"ERROR: dpkg-source -b {_pkg_name}: {e}")
            logger.error(f"fork_mirror: dpkg-source -b {_pkg_dir}: {e}")
            continue

        if _r.returncode != 0:
            tui.console.print(
                f"ERROR: dpkg-source -b {_pkg_name}: "
                f"{_r.stderr.strip()[:200] or 'no stderr'}"
            )
            logger.error(
                f"fork_mirror: dpkg-source -b {_pkg_dir}: rc={_r.returncode}, "
                f"stderr={_r.stderr.strip()}"
            )
            continue

        _files = _list_files_for_pkg(dst_dir, _pkg_name, _ver)
        if _files:
            _result[_pkg_name] = _files
            tui.console.print(
                f"fork_mirror: built source pkg {_pkg_name}_{_ver}"
            )
        else:
            logger.warning(
                f"fork_mirror: dpkg-source -b {_pkg_dir} returned 0 but no "
                f"output files matched {_pkg_name}_{_ver}*"
            )

    return _result


def _read_pkg_version(pkg_dir: str) -> str:
    """Top-entry version from debian/changelog."""
    with open(os.path.join(pkg_dir, 'debian', 'changelog'), 'r') as fh:
        _ch = Changelog(fh)
    return str(_ch.version)


def _filename_version(ver: str) -> str:
    """Version as it appears in dpkg-produced FILENAMES — i.e. with the
    epoch (`N:` prefix) stripped.  dpkg never puts the epoch in a
    `.dsc`/`.deb`/`.udeb` filename, so any code that constructs or
    matches those filenames from a changelog version (which DOES carry
    the epoch) must strip it first.  The `Version:` index field keeps
    the epoch — only filenames drop it.  (First exercised by the
    athena-apt-setup fork, whose upstream carries epoch `1:`.)"""
    return ver.split(':', 1)[1] if ':' in ver else ver


def _list_files_for_pkg(dst_dir: str, pkg_name: str, ver: str) -> List[str]:
    """Return list of files dpkg-source produced (filtered by `<pkg>_<ver>` prefix)."""
    _prefix = f'{pkg_name}_{_filename_version(ver)}'
    _found: List[str] = []
    try:
        for _name in sorted(os.listdir(dst_dir)):
            if not _name.startswith(_prefix):
                continue
            if _name.endswith('.dsc') or '.tar.' in _name:
                _found.append(os.path.join(dst_dir, _name))
    except OSError:
        pass
    return _found


# ---------------------------------------------------------------------------
# Packages / Packages-udeb generation (placeholder hashes)
# ---------------------------------------------------------------------------

def _build_packages_stanzas(pkg_dirs: List[str], build_arch: str
                            ) -> Tuple[List[str], List[str]]:
    """Parse each pkg's debian/control; emit Packages-format stanzas
    routed by Package-Type field.

    `build_arch` is the target arch the build container produces
    binaries for; it's substituted into the synthesized Filename and
    Architecture fields for any binary whose `Architecture:` is not
    literally `all`.  See _format_packages_stanza for the rationale.

    Returns: (deb_stanzas, udeb_stanzas).
    """
    _deb_stanzas: List[str] = []
    _udeb_stanzas: List[str] = []

    for _pkg_dir in pkg_dirs:
        _pkg_name = os.path.basename(_pkg_dir)
        try:
            _ver = _read_pkg_version(_pkg_dir)
        except (OSError, ValueError):
            continue

        _control = os.path.join(_pkg_dir, 'debian', 'control')
        try:
            with open(_control, 'r') as fh:
                _stanzas = list(Deb822.iter_paragraphs(fh))
        except (OSError, ValueError) as e:
            logger.warning(f"fork_mirror: cannot parse {_control}: {e}")
            continue

        if not _stanzas:
            continue

        # First stanza is Source: ...; subsequent are per-binary
        _src_stanza = _stanzas[0]
        _src_name   = _src_stanza.get('Source', _pkg_name)
        _maintainer = _src_stanza.get('Maintainer', 'Athena Build <athena@local>')

        for _bin in _stanzas[1:]:
            _text = _format_packages_stanza(_bin, _src_name, _ver,
                                            _maintainer, build_arch)
            if _bin.get('Package-Type', '') == 'udeb':
                _udeb_stanzas.append(_text)
            else:
                _deb_stanzas.append(_text)

    return _deb_stanzas, _udeb_stanzas


def _format_packages_stanza(binary: Deb822, source_name: str,
                            ver: str, maintainer: str,
                            build_arch: str) -> str:
    """Format ONE binary stanza into Packages-format text.

    Filename uses the bare basename matching what dpkg-buildpackage will
    deposit in repo/ post-build: `<pkg>_<ver>_<arch>.<deb|udeb>`.
    Size/MD5sum/SHA256 are placeholders (cache never tunnels fork pkgs
    so these are inert, but the fields MUST be present per the user
    feedback 2026-05-16).

    Architecture mapping: control's `Architecture:` field can be `all`
    (arch-independent → .deb is named `_all.deb`), `any` (built per
    arch → .deb is named `_<target-arch>.deb`), `linux-any`, or a
    specific arch / arch list.  Only `all` keeps the literal string
    in the filename; any other value resolves to `build_arch` (the
    target the build container produces for — `amd64` in our
    pipeline).  Without this remap, arch=any fork packages (e.g. our
    base-files same-name fork) emit `_any.deb` filenames that no
    actual build step produces, breaking the audit's prediction (it
    looks for the literal filename and never finds it, looping the
    rebuild).  Caught 2026-05-21 when migrating base-files to Path X.
    """
    _pkg_name = binary['Package']
    _arch     = binary.get('Architecture', 'all')
    if _arch != 'all':
        _arch = build_arch
    _ext      = 'udeb' if binary.get('Package-Type', '') == 'udeb' else 'deb'
    _filename = f'{_pkg_name}_{_filename_version(ver)}_{_arch}.{_ext}'

    _fields: List[str] = []
    _fields.append(f'Package: {_pkg_name}')
    if source_name != _pkg_name:
        _fields.append(f'Source: {source_name}')
    _fields.append(f'Version: {ver}')
    _fields.append(f'Architecture: {_arch}')
    _fields.append(f'Maintainer: {maintainer}')
    if 'Section' in binary:
        _fields.append(f'Section: {binary["Section"]}')
    if 'Priority' in binary:
        _fields.append(f'Priority: {binary["Priority"]}')
    if 'Package-Type' in binary:
        _fields.append(f'Package-Type: {binary["Package-Type"]}')

    # Relation fields — preserve only when present and non-empty.
    # Strip debhelper substvars like ${misc:Depends}, ${shlibs:Depends}
    # that haven't been substituted yet (cache reads debian/control
    # BEFORE dh_gencontrol runs).  Leaving them in produces "unresolved
    # dependency '${misc:Depends}'" warnings at dep-parse time.  The
    # actual built .deb has these substituted by dh, so this strip
    # affects only the synthetic Packages record.
    import re as _re
    _SUBSTVAR_RE = _re.compile(r'\$\{[^}]+\}')
    for _rel in ('Depends', 'Pre-Depends', 'Recommends', 'Suggests',
                 'Conflicts', 'Replaces', 'Provides', 'Breaks', 'Enhances'):
        _val = (binary.get(_rel, '') or '').strip()
        if not _val:
            continue
        # Drop substvar tokens; clean up the resulting comma noise
        _val = _SUBSTVAR_RE.sub('', _val)
        _parts = [_p.strip() for _p in _val.split(',') if _p.strip()]
        if _parts:
            _fields.append(f'{_rel}: {", ".join(_parts)}')

    _fields.append(f'Filename: {_filename}')
    _fields.append('Size: 0')
    _fields.append(f'MD5sum: {_PLACEHOLDER_MD5}')
    _fields.append(f'SHA256: {_PLACEHOLDER_SHA256}')

    _desc = (binary.get('Description', '') or f'{_pkg_name} package').strip()
    _fields.append(f'Description: {_desc}')

    return '\n'.join(_fields)


# ---------------------------------------------------------------------------
# Sources generation (real hashes)
# ---------------------------------------------------------------------------

def _build_sources_stanzas(src_pkg_files: Dict[str, List[str]]) -> List[str]:
    """Parse each .dsc; emit Sources-format stanzas with REAL hashes
    (these files actually exist on disk at this point)."""
    _stanzas: List[str] = []
    for _pkg_name, _files in src_pkg_files.items():
        _dsc = next((f for f in _files if f.endswith('.dsc')), None)
        if not _dsc:
            continue
        try:
            with open(_dsc, 'r') as fh:
                _meta = Deb822(fh)
        except (OSError, ValueError) as e:
            logger.warning(f"fork_mirror: cannot parse {_dsc}: {e}")
            continue
        _stanzas.append(_format_sources_stanza(_meta, _files))
    return _stanzas


def _format_sources_stanza(dsc: Deb822, files: List[str]) -> str:
    """Format ONE Sources stanza from .dsc metadata + computed hashes."""
    _fields: List[str] = []
    _fields.append(f'Package: {dsc["Source"]}')
    if 'Binary' in dsc:
        _fields.append(f'Binary: {dsc["Binary"]}')
    _fields.append(f'Version: {dsc["Version"]}')
    _fields.append(
        f'Maintainer: {dsc.get("Maintainer", "Athena Build <athena@local>")}'
    )
    _fields.append(f'Architecture: {dsc.get("Architecture", "all")}')
    if 'Standards-Version' in dsc:
        _fields.append(f'Standards-Version: {dsc["Standards-Version"]}')
    if 'Build-Depends' in dsc:
        _fields.append(f'Build-Depends: {dsc["Build-Depends"]}')
    if 'Format' in dsc:
        _fields.append(f'Format: {dsc["Format"]}')
    _fields.append('Directory: source/repo')

    _md5_lines: List[str] = []
    _sha256_lines: List[str] = []
    for _path in files:
        _size = os.path.getsize(_path)
        _md5    = _file_hash(_path, 'md5')
        _sha256 = _file_hash(_path, 'sha256')
        _basename = os.path.basename(_path)
        _md5_lines.append(f' {_md5} {_size} {_basename}')
        _sha256_lines.append(f' {_sha256} {_size} {_basename}')
    _fields.append('Files:\n' + '\n'.join(_md5_lines))
    _fields.append('Checksums-Sha256:\n' + '\n'.join(_sha256_lines))

    return '\n'.join(_fields)


# ---------------------------------------------------------------------------
# Release generation + file I/O helpers
# ---------------------------------------------------------------------------

def _write_index(path: str, stanzas: List[str]) -> None:
    """Write a Packages/Sources-format file — stanzas separated by blank
    line per Debian spec (RFC 822-ish)."""
    with open(path, 'w') as fh:
        for _s in stanzas:
            fh.write(_s)
            fh.write('\n\n')


def _gzip_file(src: str) -> None:
    """Produce <src>.gz alongside src via the gzip module (compresslevel 6
    — balance of size vs CPU; matches what apt-ftparchive defaults to)."""
    with open(src, 'rb') as f_in:
        with gzip.open(src + '.gz', 'wb', compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)


def _file_hash(path: str, algo: str) -> str:
    _h = hashlib.new(algo)
    with open(path, 'rb') as fh:
        while True:
            _chunk = fh.read(65536)
            if not _chunk:
                break
            _h.update(_chunk)
    return _h.hexdigest()


def _write_release(dir_fork: str, codename: str,
                   index_names: List[str]) -> None:
    """Write fork/Release covering uncompressed + .gz variants of each
    index file.  Format mirrors what apt-ftparchive release produces:
    one MD5Sum block + one SHA256 block, each listing every covered file
    with `<hash> <size> <name>` per row."""
    _date = time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())

    _rows: List[Tuple[str, int, str, str]] = []
    for _name in index_names:
        for _variant in (_name, _name + '.gz'):
            _full = os.path.join(dir_fork, _variant)
            if os.path.isfile(_full):
                _rows.append((
                    _variant,
                    os.path.getsize(_full),
                    _file_hash(_full, 'md5'),
                    _file_hash(_full, 'sha256'),
                ))

    _lines: List[str] = [
        'Origin: Athena',
        'Label: Athena Fork',
        f'Suite: {codename}',
        f'Codename: {codename}',
        f'Date: {_date}',
        'Architectures: amd64 all',
        'Components: ',  # empty for flat layout
        'Description: Athena fork (local source-built packages)',
        'MD5Sum:',
    ]
    for _name, _size, _md5, _ in _rows:
        _lines.append(f' {_md5} {_size:>16} {_name}')
    _lines.append('SHA256:')
    for _name, _size, _, _sha256 in _rows:
        _lines.append(f' {_sha256} {_size:>16} {_name}')

    with open(os.path.join(dir_fork, 'Release'), 'w') as fh:
        fh.write('\n'.join(_lines))
        fh.write('\n')


# ── CONF-11 fork-tree internal completeness audit ─────────────────────────

# `install` short-flags that take a separate value as the next token.  All
# others are no-arg flags.  Used by `_scan_install_sources` to walk past
# the flag block to find the SOURCE operand(s).
_INSTALL_FLAGS_WITH_ARG = frozenset({'-m', '-o', '-g', '-S'})


def _scan_install_sources(line: str) -> List[str]:
    """Return concrete source paths referenced by an `install` command on
    `line`.  Skips variable-expanded paths (start with `$` or `$$`),
    absolute paths (start with `/`), and the destination operand.

    Handles `install <flags> SOURCE [SOURCE...] DEST`.  Returns [] for
    `install -d <dirs>` (directory-only form, no source).
    """
    _tokens = line.split()
    if 'install' not in _tokens:
        return []
    _i = _tokens.index('install') + 1
    while _i < len(_tokens):
        _t = _tokens[_i]
        if _t in ('-d', '-D'):
            return []                    # no SOURCE operands
        if _t == '-t':
            return []                    # target-dir form; too ambiguous
        if _t in _INSTALL_FLAGS_WITH_ARG:
            _i += 2                      # flag + value
            continue
        if _t.startswith('-'):
            _i += 1                      # no-arg flag
            continue
        break
    _rest = _tokens[_i:]
    # Strip a trailing line-continuation '\' if present.
    if _rest and _rest[-1] == '\\':
        _rest = _rest[:-1]
    if len(_rest) < 2:
        return []                        # need at least SOURCE + DEST
    _sources = _rest[:-1]                # last is DEST
    return [
        _s for _s in _sources
        if _s and not _s.startswith(('$', '/', '-', '<', '>', '|'))
    ]


def _scan_cp_sources(line: str) -> List[str]:
    """Return concrete source paths referenced by a `cp` command on `line`.
    Skips variable-expanded paths and absolute paths.  Handles
    `cp [-flags] SOURCE [SOURCE...] DEST`.
    """
    _tokens = line.split()
    if 'cp' not in _tokens:
        return []
    _i = _tokens.index('cp') + 1
    while _i < len(_tokens) and _tokens[_i].startswith('-'):
        if _tokens[_i] in ('-t', '--target-directory'):
            return []                    # target-dir form; too ambiguous
        _i += 1
    _rest = _tokens[_i:]
    if _rest and _rest[-1] == '\\':
        _rest = _rest[:-1]
    if len(_rest) < 2:
        return []
    _sources = _rest[:-1]
    return [
        _s for _s in _sources
        if _s and not _s.startswith(('$', '/', '-', '<', '>', '|'))
    ]


def audit_fork_tree(pkg_dir: str) -> 'List[Dict[str, str]]':
    """Audit a fork/source/<pkg>/ tree for build-system promises that
    point at files NOT in the tree.

    Scans `Makefile` and `debian/rules` for `install <flags> <src> <dst>`
    and `cp [-flags] <src> <dst>` patterns with concrete (non-variable,
    non-absolute) source paths; verifies each source exists in the fork
    tree.  Glob patterns (`etc/*`) require ≥1 match.

    Returns a list of findings, each a dict with:
      file      — relative path of the file that referenced the source
      line_no   — 1-based line number in that file
      source    — the dangling source path (as written)
      reason    — human-readable why-it-failed

    Empty list ⇒ audit clean.  Surfaces the 2026-05-18 athena-tasksel /
    `packages/list` class of bug at cache-build time rather than deep
    inside a failed `dpkg-buildpackage`.

    Design note (CONF-11): a `debian/<binary>.install` audit was
    considered + dropped because dh_install runs AFTER the build target,
    so `.install` legitimately references built binaries and generated
    debian/* files.  Static-time existence check would false-positive on
    every package that builds before installing (e.g. choose-mirror's
    `choose-mirror` C binary).  The original athena-tasksel `packages/list`
    incident was a `Makefile install` reference, not a `.install` entry —
    so the Makefile/rules scan is the load-bearing check anyway.
    """
    _findings: 'List[Dict[str, str]]' = []

    for _scan_rel in ('Makefile', os.path.join('debian', 'rules')):
        _scan_path = os.path.join(pkg_dir, _scan_rel)
        if not os.path.isfile(_scan_path):
            continue
        try:
            with open(_scan_path) as _fh:
                for _line_no, _line in enumerate(_fh, 1):
                    if _line.lstrip().startswith('#'):
                        continue
                    for _src in _scan_install_sources(_line) + _scan_cp_sources(_line):
                        # Skip Makefile-escaped shell vars and interpolated
                        # paths anywhere.
                        if '$' in _src:
                            continue
                        _abs = os.path.join(pkg_dir, _src)
                        if any(_c in _src for _c in '*?['):
                            if not glob.glob(_abs):
                                _findings.append({
                                    'file':    _scan_rel,
                                    'line_no': str(_line_no),
                                    'source':  _src,
                                    'reason':  'no match for glob in fork tree',
                                })
                        elif not os.path.exists(_abs):
                            _findings.append({
                                'file':    _scan_rel,
                                'line_no': str(_line_no),
                                'source':  _src,
                                'reason':  'file not found in fork tree',
                            })
        except OSError as _e:
            logger.warning(f"audit_fork_tree: cannot read {_scan_path}: {_e}")

    return _findings
