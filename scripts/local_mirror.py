"""Local build mirror — a snapshot-pinned apt repo of the build closure.

When ``CreateLocalMirror`` is on, we download every Debian binary needed to
BUILD all shipped packages (the build closure: the Build-Depends of every
selected source, resolved transitively) into a flat ``localmirror/`` repo,
pinned to the selected snapshot.  Build containers add it as their FIRST apt
source (``deb [trusted=yes] file:///localmirror ./``) so build-deps come from
local disk instead of being re-downloaded from snapshot.debian.org on every
container instance — a large win on bandwidth-limited links where small
packages are network-bound, not CPU-bound.

The mirror is keyed to the snapshot via a ``.snapshot`` marker file: when the
snapshot advances the mirror is stale and gets rebuilt (after a size /
free-space prompt).  apt sees identical versions in both the local mirror and
the snapshot mirror, so the local one must out-prioritise the snapshot-origin
pin (1001) — its Release carries ``Origin: AthenaLocalMirror`` and the
container pins ``release o=AthenaLocalMirror`` to 1002.

This module is the self-contained engine: plan / disk_check / download / index
/ is_valid_for / status / purge.  The cache→closure step reuses
``build_closure.compute_build_closure``; per-file downloads reuse
``utils.download_file`` (its own per-file bar) under a cumulative bar; the
index is a plain ``dpkg-scanpackages`` flat repo.
"""
import gzip
import logging
import lzma
import os
import shutil
import subprocess
import time
from email.utils import formatdate
from typing import Any, Dict, List, Optional, Set, Tuple, cast
from urllib.parse import urljoin

import build_closure
import utils
from tui import ProgressBar

logger = logging.getLogger('athena.build')

MARKER = '.snapshot'                 # records the snapshot ts the mirror holds
LOCAL_ORIGIN = 'AthenaLocalMirror'   # Release Origin → the container pins this
_HEADROOM = 1.05                     # 5% free-space headroom over the estimate


# ── Cache → build-closure universe ───────────────────────────────────────────
def _universe_from_cache(cache: Any) -> Tuple[dict, dict, dict]:
    """From cache.package_hashtable build the three structures
    compute_build_closure needs plus a name→Package map for downloads:

      bin_index      {name: {'Depends':.., 'Pre-Depends':..}} (highest ver)
      provides_index {virtual_name: [provider_name, ...]}
      best           {name: Package}  (the highest-version Package object)
    """
    bin_index: dict = {}
    provides: dict = {}
    best: dict = {}
    for _name, _vbucket in cache.package_hashtable.items():
        if not _vbucket:
            continue
        _ver = max(_vbucket.keys())          # Version objects order correctly
        _pkgs = _vbucket.get(_ver) or []
        if not _pkgs:
            continue
        _pkg = _pkgs[0]
        best[_name] = _pkg
        bin_index[_name] = {
            'Depends': _pkg.get('Depends', '') or '',
            'Pre-Depends': _pkg.get('Pre-Depends', '') or '',
        }
        for _grp in build_closure._rels(_pkg.get('Provides', '') or ''):
            for _rel in _grp:
                provides.setdefault(_rel['name'], []).append(_name)
    return bin_index, provides, best


def _stanza_for(pkg: Any, basename: str) -> str:
    """The package's apt Packages stanza with ``Filename:`` rewritten to
    ``./<basename>`` (flat repo).  Uses ``pkg.dump()`` — python-debian's own
    serializer — so every control field the dep solver needs (Depends, Provides,
    Conflicts, …) is carried verbatim; no hand-rolled emitter.  Lets the REMOTE
    localmirror be indexed natively from cache metadata (no dpkg-scanpackages)."""
    _txt = pkg.dump() or ''
    _out = []
    _seen_filename = False
    for _ln in _txt.splitlines():
        if _ln.startswith('Filename:'):
            _ln = f'Filename: ./{basename}'
            _seen_filename = True
        _out.append(_ln)
    if not _seen_filename:
        _out.append(f'Filename: ./{basename}')
    return '\n'.join(_out).rstrip('\n') + '\n'


def plan(cache: Any, dep_tree: Any, config: Any,
         include_index: bool = False) -> 'Dict[str, Any]':
    """Resolve the full build closure and map each binary to a download.

    Returns ``{'entries': [{name, basename, url, size, sha256}, ...],
    'total_size': int, 'unsatisfiable': [...]}``.  Reuses the snapshot-rewritten
    ``pkg._mirror.url`` (cache rewrites mirrors once at ingest), so URLs point
    straight at the pinned snapshot.

    When ``include_index`` is set, the result also carries ``packages_index`` —
    the flat-repo Packages text built from the cache stanzas (``pkg.dump()`` with
    ``Filename:`` rewritten) — so a REMOTE host can write a valid apt index
    without dpkg-scanpackages.  Off by default: the LOCAL mirror indexes its
    downloaded files with dpkg-scanpackages and doesn't need this.
    """
    bin_index, provides, best = _universe_from_cache(cache)
    _sbd: dict = {}
    for _sname, _src in dep_tree.selected_srcs.items():
        _sbd[_sname] = ' , '.join(filter(None, [
            _src.get('Build-Depends', '') or '',
            _src.get('Build-Depends-Indep', '') or '',
            _src.get('Build-Depends-Arch', '') or '',
        ]))
    _closure = build_closure.compute_build_closure(
        list(dep_tree.selected_srcs.keys()), _sbd, bin_index, provides)

    _entries: 'List[dict]' = []
    _stanzas: 'List[str]' = []
    _excluded: 'List[str]' = []
    _total = 0
    for _name in sorted(cast('Set[str]', _closure['all'])):
        _pkg = best.get(_name)
        if _pkg is None or getattr(_pkg, '_mirror', None) is None:
            continue
        _fname = _pkg.get('Filename', '') or ''
        if not _fname:
            continue
        _url = urljoin(_pkg._mirror.url + '/', _fname)
        # The build mirror caches the UPSTREAM (snapshot) build closure only.
        # A closure member whose best version is OURS (a fork / locally-built
        # package, served from a file:// mirror) is excluded: it's served by our
        # own repo, and the common case (base-files — Essential) is already in
        # the build container's base image, so it's never downloaded at build.
        if _url.startswith('file:'):
            _excluded.append(os.path.basename(_fname))
            continue
        _basename = os.path.basename(_fname)
        _size = int(_pkg.get('Size', '0') or 0)
        _entries.append({
            'name': _name,
            'basename': _basename,
            'url': _url,
            'size': _size,
            'sha256': _pkg.get('SHA256', '') or '',
        })
        if include_index:
            _stanzas.append(_stanza_for(_pkg, _basename))
        _total += _size
    _result: 'Dict[str, Any]' = {
        'entries': _entries,
        'total_size': _total,
        'unsatisfiable': _closure.get('unsatisfiable', []),
        'local_excluded': _excluded,
    }
    if include_index:
        _result['packages_index'] = '\n'.join(_stanzas)
    return _result


# ── Disk + download ──────────────────────────────────────────────────────────
def disk_check(total_size: int, directory: str) -> 'Tuple[bool, int, int]':
    """(ok, free_bytes, needed_bytes) — needed = estimate + 5% headroom."""
    os.makedirs(directory, exist_ok=True)
    _free = shutil.disk_usage(directory).free
    _needed = int(total_size * _HEADROOM)
    return (_free >= _needed, _free, _needed)


def download(plan_dict: 'Dict[str, Any]', directory: str,
             snapshot_ts: str) -> 'Tuple[int, List[Tuple[str, str]]]':
    """Download every planned binary into *directory* (idempotent — sha256-skip
    already-present files), then stamp the ``.snapshot`` marker IFF every file
    succeeded.

    A cumulative bar (count + total bytes, no rate) wraps the loop; each file's
    own bar (with the real per-file rate) comes from ``utils.download_file``.
    Returns ``(downloaded_bytes, failures)`` where failures is
    ``[(basename, reason), ...]``.
    """
    os.makedirs(directory, exist_ok=True)
    _entries = plan_dict['entries']
    _n = len(_entries)
    _cum: 'Optional[ProgressBar]'
    try:
        _cum = ProgressBar(label=f'(0/{_n})',
                           maxvalue=max(1, plan_dict['total_size']),
                           show_rate=False)
    except Exception as e:           # pragma: no cover - bar is non-essential
        _cum = None
        logger.error(f"local_mirror.download ProgressBar: {type(e).__name__}: {e}")

    _downloaded = 0
    _failures: 'List[Tuple[str, str]]' = []
    for _i, _e in enumerate(_entries, 1):
        if _cum is not None:
            _cum.label(f'({_i}/{_n})')
        _dest = os.path.join(directory, _e['basename'])
        # already present + verified → skip (resumable / re-runnable)
        if _e['sha256'] and utils.get_sha256(_dest) == _e['sha256']:
            if _cum is not None:
                _cum.step(_e['size'])
            _downloaded += _e['size']
            continue
        _sz, _detail = utils.download_file(_e['url'], _dest)
        if _sz < 0:
            _failures.append((_e['basename'], _detail))
            continue
        if _e['sha256'] and utils.get_sha256(_dest) != _e['sha256']:
            _failures.append((_e['basename'], 'sha256 mismatch'))
            try:
                os.remove(_dest)
            except OSError:
                pass
            continue
        if _cum is not None:
            _cum.step(_e['size'] or _sz)
        _downloaded += _sz
    if _cum is not None:
        _cum.close(persist=True)
    # Stamp the snapshot marker ONLY when the download is COMPLETE.  is_valid_for
    # keys "ready" on this marker and _ensure_local_mirror early-returns when
    # valid — so stamping a PARTIAL mirror would mark it valid-for-snapshot and
    # skip it forever, never retrying the missing files (they'd stay
    # snapshot-fallback permanently).  No marker → the next `cache parse` re-runs
    # (resumably, sha-skipping the files already present) and retries them.
    # Matches the remote runner, which returns non-zero when `failed`.
    if not _failures:
        _write_marker(directory, snapshot_ts)
    return _downloaded, _failures


# ── Index (flat repo) ────────────────────────────────────────────────────────
def index(directory: str) -> bool:
    """Write a flat apt index for *directory*: Packages(+.gz/.xz) via
    dpkg-scanpackages, then a Release carrying ``Origin: AthenaLocalMirror`` so
    the container can pin it.  Returns True on success."""
    _pkgs = os.path.join(directory, 'Packages')
    # Capture scan output in memory and write Packages only on success — a
    # failed scan must NOT leave an empty Packages behind (is_valid_for would
    # then falsely treat the mirror as ready).
    try:
        _data = subprocess.run(
            ['dpkg-scanpackages', '-m', '.'], cwd=directory,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=True).stdout
    except (subprocess.CalledProcessError, OSError) as e:
        logger.error(f"local_mirror.index dpkg-scanpackages: {e}")
        return False
    if not _data:
        logger.error("local_mirror.index: dpkg-scanpackages produced no output")
        return False
    with open(_pkgs, 'wb') as _f:
        _f.write(_data)
    with gzip.open(_pkgs + '.gz', 'wb') as _g:
        _g.write(_data)
    with lzma.open(_pkgs + '.xz', 'wb') as _x:
        _x.write(_data)
    _write_release(directory)
    return True


def _write_release(directory: str) -> None:
    _rows: 'List[Tuple[str, int, str, str]]' = []
    for _name in ('Packages', 'Packages.gz', 'Packages.xz'):
        _p = os.path.join(directory, _name)
        if os.path.isfile(_p):
            _rows.append((_name, os.path.getsize(_p),
                          utils.get_md5(_p), utils.get_sha256(_p)))
    _lines = [
        f'Origin: {LOCAL_ORIGIN}',
        'Label: Athena Local Build Mirror',
        'Suite: local',
        'Codename: local',
        'Architectures: amd64 all',
        'Components:',                      # empty → flat layout
        f'Date: {formatdate(time.time(), usegmt=True)}',
        'Description: Athena local build-closure mirror',
        'MD5Sum:',
    ]
    _lines += [f' {_md5} {_size:>16} {_name}' for _name, _size, _md5, _ in _rows]
    _lines.append('SHA256:')
    _lines += [f' {_sha} {_size:>16} {_name}' for _name, _size, _, _sha in _rows]
    with open(os.path.join(directory, 'Release'), 'w') as _fh:
        _fh.write('\n'.join(_lines) + '\n')


# ── Marker / status ──────────────────────────────────────────────────────────
def _write_marker(directory: str, snapshot_ts: str) -> None:
    with open(os.path.join(directory, MARKER), 'w') as _fh:
        _fh.write((snapshot_ts or '') + '\n')


def mirror_snapshot(directory: str) -> str:
    """The snapshot ts the mirror currently holds, or '' if unmarked."""
    _m = os.path.join(directory, MARKER)
    if os.path.isfile(_m):
        try:
            with open(_m) as _fh:
                return _fh.read().strip()
        except OSError:
            return ''
    return ''


def is_valid_for(directory: 'Any', snapshot_ts: 'Optional[str]') -> bool:
    """True iff the mirror is indexed AND keyed to *snapshot_ts*.  Defensive
    against a non-str dir (e.g. a Mock config in tests, recipe-only containers
    constructed without a real working dir) — returns False rather than raising."""
    if not snapshot_ts or not isinstance(directory, str):
        return False
    _pkgs = os.path.join(directory, 'Packages')
    try:
        if not (os.path.isfile(_pkgs) and os.path.getsize(_pkgs) > 0):
            return False
    except OSError:
        return False
    return mirror_snapshot(directory) == snapshot_ts


def status(directory: str) -> 'Dict[str, Any]':
    if not os.path.isdir(directory):
        return {'snapshot': '', 'n_debs': 0, 'size': 0}
    _debs = [_f for _f in os.listdir(directory) if _f.endswith('.deb')]
    _size = 0
    for _f in _debs:
        try:
            _size += os.path.getsize(os.path.join(directory, _f))
        except OSError:
            pass
    return {'snapshot': mirror_snapshot(directory),
            'n_debs': len(_debs), 'size': _size}


def purge(directory: str) -> None:
    """Remove every file in the mirror dir (the dir itself is kept)."""
    if not os.path.isdir(directory):
        return
    for _f in os.listdir(directory):
        _p = os.path.join(directory, _f)
        try:
            if os.path.isfile(_p) or os.path.islink(_p):
                os.remove(_p)
            elif os.path.isdir(_p):
                shutil.rmtree(_p)
        except OSError as e:
            logger.error(f"local_mirror.purge {_p}: {e}")


def human_size(n: int) -> str:
    _f = float(n)
    for _u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if _f < 1024 or _u == 'TB':
            return f'{_f:.1f}{_u}'
        _f /= 1024
    return f'{_f:.1f}TB'
