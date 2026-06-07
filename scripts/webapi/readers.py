"""API-01 — disk readers behind the read-only endpoints.

Pure functions over the on-disk artifacts (build records, buildflags).
No fastapi imports, no BuildSession — these run identically under the
API server, tests, or any future consumer.  All paths are passed in
explicitly by the caller (the --api launcher derives them from
BuildConfig; tests pass tempdirs).

Security: every package-name path parameter is validated against
``_PKG_NAME_RE`` (Debian source-name grammar) BEFORE touching the
filesystem — names are never joined into paths unvalidated, so
traversal via ``../`` is structurally impossible.
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import utils

logger = logging.getLogger('athena.webapi')

# Debian source package name: lowercase alnum start, then alnum + . + -
# (Policy §5.6.1).  Also caps length — nothing legitimate is >128.
_PKG_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9+.\-]{0,127}$')


def valid_package_name(name: str) -> bool:
    return bool(_PKG_NAME_RE.match(name or ''))


def read_flags(flags_path: str) -> 'Dict[str, Any]':
    """The buildflags.json payload (pipeline stage gates).  Missing or
    malformed file → empty flags rather than an error: a fresh workdir
    has no flags yet and that's a valid state to report."""
    try:
        with open(flags_path) as _fh:
            _doc = json.load(_fh)
    except (OSError, ValueError):
        return {'flags': {}}
    if not isinstance(_doc, dict):
        return {'flags': {}}
    return {'flags': _doc.get('flags', {})}


def list_builds(buildlog_dir: str, phase: 'Optional[str]' = None,
                limit: int = 100, offset: int = 0) -> 'Dict[str, Any]':
    """Paginated index over ``*.build.json``.  Per-item summary only —
    the full (verified) record comes from :func:`get_build`.

    Reads records unverified here (plain json) for speed: an index over
    985 files must not pay 985 HMAC checks per page view.  The
    ``verified`` answer is only claimed on the single-record endpoint.
    """
    _suffix = utils.BUILD_RECORD_SUFFIX
    _items: 'List[Dict[str, Any]]' = []
    try:
        _entries = sorted(os.listdir(buildlog_dir))
    except OSError:
        _entries = []
    for _entry in _entries:
        if not _entry.endswith(_suffix):
            continue
        _path = os.path.join(buildlog_dir, _entry)
        try:
            with open(_path) as _fh:
                _rec = json.load(_fh)
        except (OSError, ValueError):
            continue
        if phase and _rec.get('phase') != phase:
            continue
        _items.append({
            'package': _rec.get('package', _entry[:-len(_suffix)]),
            'phase': _rec.get('phase'),
            'status': _rec.get('status'),
            'built_version': _rec.get('built_version'),
            'intended_version': _rec.get('intended_version'),
            'output_count': _rec.get('output_count'),
            'elapsed_seconds': _rec.get('elapsed_seconds'),
            'mtime': os.path.getmtime(_path),
        })
    _total = len(_items)
    _page = _items[offset:offset + max(0, limit)]
    return {'total': _total, 'offset': offset, 'limit': limit,
            'items': _page}


def get_build(buildlog_dir: str, package: str) -> 'Dict[str, Any]':
    """One package's record with integrity status:

    ``found``     — a ``<pkg>.build.json`` exists on disk
    ``verified``  — its HMAC signature checked out (via the same
                    ``utils.read_build_record`` every other consumer uses)
    ``record``    — the verified record, or None

    found=True + verified=False = tampered/corrupt — surfaced, never
    silently treated as absent (the API is an audit surface).
    """
    if not valid_package_name(package):
        return {'found': False, 'verified': False, 'record': None,
                'error': 'invalid package name'}
    _path = os.path.join(buildlog_dir, f"{package}{utils.BUILD_RECORD_SUFFIX}")
    _exists = os.path.isfile(_path)
    _rec = utils.read_build_record(buildlog_dir, package) if _exists else None
    return {
        'found': _exists,
        'verified': _rec is not None,
        'record': _rec,
    }


# Artifact kinds the API may serve, mapped to their on-disk filename
# pattern.  An explicit allowlist — anything else 404s — so the route
# can never be talked into serving an arbitrary file from the log dir.
_ARTIFACT_SUFFIX = {
    'buildlog': '.buildlog',     # OBS-04 narrative (small, KBs)
    'vbuildlog': '.vbuildlog',   # virtual prediction (small)
    'log': '',                   # raw container stdout (can be 200+ MB)
}

_FULL_READ_CAP = 4 * 1024 * 1024   # 4 MB — generous for narratives,
                                   # a hard stop for container logs


def _tail_lines(path: str, n: int,
                block: int = 65536) -> 'tuple[List[str], int]':
    """Last ``n`` lines of ``path`` without reading the whole file —
    backwards block reads from EOF (linux's container log is 215 MB;
    naive read() is exactly the trap this exists to avoid).  Returns
    (lines, total_size_bytes)."""
    with open(path, 'rb') as _fh:
        _fh.seek(0, os.SEEK_END)
        _size = _fh.tell()
        _data = b''
        _pos = _size
        while _pos > 0 and _data.count(b'\n') <= n:
            _step = min(block, _pos)
            _pos -= _step
            _fh.seek(_pos)
            _data = _fh.read(_step) + _data
    _lines = _data.splitlines()[-n:]
    return [_l.decode('utf-8', errors='replace') for _l in _lines], _size


def read_artifact(buildlog_dir: str, package: str, kind: str,
                  tail: int = 0) -> 'Dict[str, Any]':
    """Serve one build sidecar.

    ``kind`` must be in the allowlist (`buildlog`/`vbuildlog`/`log`).
    ``tail`` > 0 returns only the last N lines (mandatory in practice
    for ``log``); 0 returns the full text but is refused for files
    over ``_FULL_READ_CAP`` to keep a 200 MB container log from being
    slurped through the JSON layer by accident.
    """
    if not valid_package_name(package):
        return {'found': False, 'error': 'invalid package name'}
    _suffix = _ARTIFACT_SUFFIX.get(kind)
    if _suffix is None:
        return {'found': False, 'error': f'unknown artifact kind {kind!r}'}
    _path = os.path.join(buildlog_dir, f"{package}{_suffix}")
    if not os.path.isfile(_path):
        return {'found': False}
    _size = os.path.getsize(_path)
    if tail and tail > 0:
        _lines, _size = _tail_lines(_path, tail)
        return {'found': True, 'size': _size, 'tail': tail,
                'truncated': _size > sum(len(_l) + 1 for _l in _lines),
                'lines': _lines}
    if _size > _FULL_READ_CAP:
        return {'found': True, 'size': _size, 'truncated': True,
                'error': (f'file is {_size} bytes — pass ?tail=N '
                          f'(full reads capped at {_FULL_READ_CAP})')}
    with open(_path, errors='replace') as _fh:
        return {'found': True, 'size': _size, 'truncated': False,
                'text': _fh.read()}


# Any config key whose name matches this is value-redacted before the
# config leaves the host.  Deliberately broad — a false-positive
# redaction costs nothing; a missed secret is unrecoverable.
_SECRET_KEY_RE = re.compile(r'(?i)\b\w*(key|secret|token|passw\w*)\w*\s*=')


def read_config_redacted(conf_path: str) -> 'Dict[str, Any]':
    """build.conf as text with secret-bearing values replaced by
    ``[REDACTED]``.  Whole-line redaction keyed on the option NAME, not
    the value — we never try to recognise secrets by shape."""
    try:
        with open(conf_path, errors='replace') as _fh:
            _lines = _fh.read().splitlines()
    except OSError:
        return {'found': False}
    _out = []
    for _line in _lines:
        if _SECRET_KEY_RE.match(_line.strip()):
            _name = _line.split('=', 1)[0].rstrip()
            _out.append(f"{_name} = [REDACTED]")
        else:
            _out.append(_line)
    return {'found': True, 'text': '\n'.join(_out)}


def repo_summary(repo_dir: str) -> 'Dict[str, Any]':
    """Per-directory .deb/.udeb counts + bytes under repo/dists —
    the at-a-glance pool shape."""
    _dirs: 'Dict[str, Dict[str, int]]' = {}
    _total_files = 0
    _total_bytes = 0
    for _root, _, _files in os.walk(os.path.join(repo_dir, 'dists')):
        _n = 0
        _b = 0
        for _f in _files:
            if _f.endswith(('.deb', '.udeb')):
                _n += 1
                try:
                    _b += os.path.getsize(os.path.join(_root, _f))
                except OSError:
                    pass
        if _n:
            _rel = os.path.relpath(_root, repo_dir)
            _dirs[_rel] = {'files': _n, 'bytes': _b}
            _total_files += _n
            _total_bytes += _b
    return {'dirs': _dirs, 'total_files': _total_files,
            'total_bytes': _total_bytes}


def mirror_state(config_dir: str, coord_dir: str) -> 'Dict[str, Any]':
    """Registered mirrors' state files + the local coord head.  All of
    this is already public-by-design federation metadata (heads are
    published to every peer); private identity material under
    coord/identity is NOT served."""
    _mirrors: 'Dict[str, Any]' = {}
    try:
        for _entry in sorted(os.listdir(config_dir)):
            if _entry.startswith('mirror.') and _entry.endswith('.state'):
                try:
                    with open(os.path.join(config_dir, _entry)) as _fh:
                        _mirrors[_entry[len('mirror.'):-len('.state')]] = \
                            json.load(_fh)
                except (OSError, ValueError):
                    _mirrors[_entry] = {'error': 'unreadable'}
    except OSError:
        pass
    _head: 'Any' = None
    try:
        with open(os.path.join(coord_dir, 'coord-head.json')) as _fh:
            _head = json.load(_fh)
    except (OSError, ValueError):
        pass
    return {'mirrors': _mirrors, 'coord_head': _head}


_DELTA_MISS_RE = re.compile(r'declared-not-emitted: (.+)')
_DELTA_EXTRA_RE = re.compile(r'emitted-not-declared: (.+)')
_FILTERED_RE = re.compile(r'FILTERED[^\n]*\n  (.+)')
_INFLIGHT_PHASES = ('entry', 'container_exited', 'segregated')


def _split_names(raw: str) -> 'List[str]':
    raw = (raw or '').strip()
    if not raw or raw == '(none)':
        return []
    return [_x.strip() for _x in raw.split(',') if _x.strip()]


def progress(buildlog_dir: str, total: int = 0,
             window_s: int = 1800) -> 'Dict[str, Any]':
    """The live-rebuild aggregate (direct port of the operator
    monitoring loop used during the thor1 rebuild):

    - phase counts + failed list
    - completion rate over the trailing ``window_s`` + ETA when the
      caller supplies the corpus ``total``
    - in-flight sources with container-log size/age — the liveness
      signal that distinguishes a heavyweight trough from a stall
    - per-source DELTA residue: each ``.buildlog``'s
      declared-not-emitted/emitted-not-declared cross-referenced
      against its ``.vbuildlog`` FILTERED prediction; only the
      UNEXPLAINED remainder is reported (classification beyond that —
      arch-gate bug vs category-C — is the consumer's policy call)
    """
    _now = time.time()
    _phases: 'Dict[str, int]' = {}
    _times: 'List[float]' = []
    _failed: 'List[str]' = []
    _inflight: 'List[Dict[str, Any]]' = []
    try:
        _entries = sorted(os.listdir(buildlog_dir))
    except OSError:
        _entries = []
    for _entry in _entries:
        if not _entry.endswith(utils.BUILD_RECORD_SUFFIX):
            continue
        _path = os.path.join(buildlog_dir, _entry)
        _pkg = _entry[:-len(utils.BUILD_RECORD_SUFFIX)]
        try:
            with open(_path) as _fh:
                _ph = json.load(_fh).get('phase', 'unknown')
            _times.append(os.path.getmtime(_path))
        except (OSError, ValueError):
            _ph = 'unreadable'
        _phases[_ph] = _phases.get(_ph, 0) + 1
        if _ph == 'failed':
            _failed.append(_pkg)
        elif _ph in _INFLIGHT_PHASES:
            _lp = os.path.join(buildlog_dir, _pkg)
            _item: 'Dict[str, Any]' = {'package': _pkg}
            try:
                _item['log_bytes'] = os.path.getsize(_lp)
                _item['log_age_s'] = round(_now - os.path.getmtime(_lp), 1)
            except OSError:
                _item['log_bytes'] = None
                _item['log_age_s'] = None
            _inflight.append(_item)
    _times.sort()
    _recent = [_t for _t in _times if _now - _t < window_s]
    _rate = round(len(_recent) * 3600.0 / window_s, 1) if _recent else 0.0
    _records = sum(_phases.values())
    _doc: 'Dict[str, Any]' = {
        'records': _records,
        'total': total or None,
        'remaining': (total - _records) if total else None,
        'phases': _phases,
        'window_s': window_s,
        'completed_in_window': len(_recent),
        'rate_per_hour': _rate,
        'eta_hours': (round((total - _records) / _rate, 1)
                      if total and _rate > 0 else None),
        'newest_record_age_s': (round(_now - _times[-1], 1)
                                if _times else None),
        'in_flight': _inflight,
        'failed': _failed,
        'deltas': [],
    }
    # DELTA residue: only sources whose buildlog disagrees with the
    # declared set AND whose vbuildlog FILTERED doesn't explain it.
    for _entry in _entries:
        if not _entry.endswith('.buildlog'):
            continue
        _pkg = _entry[:-len('.buildlog')]
        if _pkg in _failed:
            continue   # failure fallout, not a delta finding
        try:
            _txt = open(os.path.join(buildlog_dir, _entry),
                        errors='replace').read()
        except OSError:
            continue
        _m = _DELTA_MISS_RE.search(_txt)
        _e = _DELTA_EXTRA_RE.search(_txt)
        _miss = _split_names(_m.group(1)) if _m else []
        _extra = _split_names(_e.group(1)) if _e else []
        if not _miss and not _extra:
            continue
        _filt: set = set()
        _vb = os.path.join(buildlog_dir, f'{_pkg}.vbuildlog')
        if os.path.isfile(_vb):
            _fm = _FILTERED_RE.search(open(_vb, errors='replace').read())
            if _fm:
                _filt = set(_split_names(_fm.group(1)))
        _unexplained = [_x for _x in _miss if _x not in _filt]
        if _unexplained or _extra:
            _doc['deltas'].append({'package': _pkg,
                                   'missing': _unexplained,
                                   'extra': _extra})
    return _doc


def phase_counts(buildlog_dir: str) -> 'Dict[str, int]':
    """phase → count over every record; the one-line pipeline summary
    the state endpoint carries next to the flags."""
    _counts: 'Dict[str, int]' = {}
    try:
        _entries = os.listdir(buildlog_dir)
    except OSError:
        return _counts
    for _entry in _entries:
        if not _entry.endswith(utils.BUILD_RECORD_SUFFIX):
            continue
        try:
            with open(os.path.join(buildlog_dir, _entry)) as _fh:
                _ph = json.load(_fh).get('phase', 'unknown')
        except (OSError, ValueError):
            _ph = 'unreadable'
        _counts[_ph] = _counts.get(_ph, 0) + 1
    return _counts
