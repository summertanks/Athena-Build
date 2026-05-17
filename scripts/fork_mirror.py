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

def generate_fork_mirror(buildconfig) -> bool:
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

    src_pkg_files = _generate_source_packages(pkg_dirs, dir_fork_source_repo)
    if not src_pkg_files:
        tui.console.print(
            "fork_mirror: dpkg-source -b produced no usable output — skipping mirror"
        )
        logger.warning("fork_mirror: src_pkg_files empty after generation pass")
        return False

    deb_stanzas, udeb_stanzas = _build_packages_stanzas(pkg_dirs)
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
    return True


def register_fork_mirror(mirrors: List['utils.Mirror'],
                         buildconfig) -> List['utils.Mirror']:
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
    debian/control file.  Skips 'repo' (helper output dir) and silently
    ignores entries that aren't directories or lack debian/control.
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
        _control = os.path.join(_pkg_dir, 'debian', 'control')
        if not os.path.isfile(_control):
            logger.warning(
                f"fork_mirror: {_pkg_dir} missing debian/control — skipping"
            )
            continue
        _found.append(_pkg_dir)
    return _found


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

        _dsc = os.path.join(dst_dir, f'{_pkg_name}_{_ver}.dsc')
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


def _list_files_for_pkg(dst_dir: str, pkg_name: str, ver: str) -> List[str]:
    """Return list of files dpkg-source produced (filtered by `<pkg>_<ver>` prefix)."""
    _prefix = f'{pkg_name}_{ver}'
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

def _build_packages_stanzas(pkg_dirs: List[str]) -> Tuple[List[str], List[str]]:
    """Parse each pkg's debian/control; emit Packages-format stanzas
    routed by Package-Type field.

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
        _maintainer = _src_stanza.get('Maintainer', 'Athena Linux <athena@local>')

        for _bin in _stanzas[1:]:
            _text = _format_packages_stanza(_bin, _src_name, _ver, _maintainer)
            if _bin.get('Package-Type', '') == 'udeb':
                _udeb_stanzas.append(_text)
            else:
                _deb_stanzas.append(_text)

    return _deb_stanzas, _udeb_stanzas


def _format_packages_stanza(binary: Deb822, source_name: str,
                            ver: str, maintainer: str) -> str:
    """Format ONE binary stanza into Packages-format text.

    Filename uses the bare basename matching what dpkg-buildpackage will
    deposit in repo/ post-build: `<pkg>_<ver>_<arch>.<deb|udeb>`.
    Size/MD5sum/SHA256 are placeholders (cache never tunnels fork pkgs
    so these are inert, but the fields MUST be present per the user
    feedback 2026-05-16).
    """
    _pkg_name = binary['Package']
    _arch     = binary.get('Architecture', 'all')
    _ext      = 'udeb' if binary.get('Package-Type', '') == 'udeb' else 'deb'
    _filename = f'{_pkg_name}_{ver}_{_arch}.{_ext}'

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

    # Relation fields — preserve only when present and non-empty
    for _rel in ('Depends', 'Pre-Depends', 'Recommends', 'Suggests',
                 'Conflicts', 'Replaces', 'Provides', 'Breaks', 'Enhances'):
        _val = (binary.get(_rel, '') or '').strip()
        if _val:
            _fields.append(f'{_rel}: {_val}')

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
        f'Maintainer: {dsc.get("Maintainer", "Athena Linux <athena@local>")}'
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
