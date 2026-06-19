"""Canonical builder config propagated through the coord tree.

The distribution-mode owner writes `<coord>/config/canonical.json` on publish
and records its sha256 in the SIGNED coord-head (`config_sha256`).  Peers fetch
it on `mirror builders register` / `mirror pull`, verify the file's sha256
against the head's `config_sha256`, and adopt it — so every builder in the
federation shares one package selection.  Nothing unverified is ever applied
(the head's signature vouches for the hash; the hash vouches for the file).

v2 (current) carries the FULL selection authority so a peer builds the
identical distribution, not just identical pkg/pool inputs:
  - `lists`: the 4 list files (pkg/live/installer/pool) as RAW TEXT — byte
    faithful, so a peer re-parses identical bytes → identical closure (we do
    NOT regenerate from parsed seeds, which would reorder groups and drift the
    order-sensitive OR-resolution).
  - `pins` / `closure` / `flags` / `arch` / `snapshot`: the selection.state
    payload the peer seeds locally (re-signed with its own HMAC key — the
    federation trust is the GPG head sha, the HMAC is only local tamper
    detection).
  - top-level `pkg.list` / `pool.list`: duplicated for an OLD peer that only
    knows v1 (it applies pkg+pool and ignores the rest — graceful).

v1 (`{v:1, 'pkg.list', 'pool.list'}`) is still read for back-compat.
"""
import hashlib
import json
import os
from typing import Optional, Tuple

CONFIG_MANIFEST_VERSION = 2


def manifest_path(coord_dir: str) -> str:
    return os.path.join(coord_dir, 'config', 'canonical.json')


def _read_text(path: str) -> str:
    """Best-effort read; '' when the path is empty/absent/unreadable."""
    if not path or not os.path.isfile(path):
        return ''
    try:
        with open(path) as _fh:
            return _fh.read()
    except OSError:
        return ''


def write_canonical_config(coord_dir: str, pkglist_path: str,
                           poollist_path: str,
                           livelist_path: str = '',
                           installerlist_path: str = '',
                           selection_state: 'Optional[dict]' = None) -> str:
    """Write `<coord_dir>/config/canonical.json` (v2) from the local list files
    and the owner's verified selection.state.  Returns its sha256 (hex) for the
    coord-head, or '' if the local pkg.list can't be read.

    `selection_state` is the owner's `read_selection_state` result (or None for
    a lists-only manifest); only its pins/closure/flags/arch/snapshot travel.
    List TEXTS are raw strings (byte-faithful); only the wrapping dict is
    sorted — `pins`/`closure` are name→value maps where order is irrelevant."""
    try:
        with open(pkglist_path) as _fh:
            _pkg = _fh.read()
    except OSError:
        return ''
    _pool = _read_text(poollist_path)
    _live = _read_text(livelist_path)
    _installer = _read_text(installerlist_path)
    _sel = selection_state or {}
    _doc = {
        'v': CONFIG_MANIFEST_VERSION,
        'lists': {'pkg': _pkg, 'live': _live,
                  'installer': _installer, 'pool': _pool},
        'pins': _sel.get('pins', {}) or {},
        'closure': _sel.get('closure', {}) or {},
        'flags': _sel.get('flags', {}) or {},
        'arch': _sel.get('arch', '') or '',
        'snapshot': _sel.get('snapshot', '') or '',
        # back-compat for an old (v1-only) peer:
        'pkg.list': _pkg,
        'pool.list': _pool,
    }
    _payload = json.dumps(
        _doc, sort_keys=True, ensure_ascii=True, indent=2).encode('utf-8')
    _path = manifest_path(coord_dir)
    try:
        os.makedirs(os.path.dirname(_path), mode=0o755, exist_ok=True)
        with open(_path, 'wb') as _fh:
            _fh.write(_payload)
    except OSError:
        return ''
    return hashlib.sha256(_payload).hexdigest()


def apply_canonical_config(fetched_coord_dir: str, expected_sha256: str,
                           pkglist_path: str, poollist_path: str,
                           livelist_path: str = '',
                           installerlist_path: str = '',
                           ) -> 'Tuple[bool, str, Optional[dict]]':
    """Verify the fetched canonical.json against `expected_sha256` (carried by
    the signed coord-head) and apply it.  Refuses on absence / missing head
    hash / sha mismatch — never applies unverified config.

    Returns (ok, detail, selection_payload).  For a v2 manifest the payload is
    `{pins, closure, flags, arch, snapshot}` for the caller to seed
    selection.state; for v1 (or on failure) it is None."""
    _path = manifest_path(fetched_coord_dir)
    try:
        with open(_path, 'rb') as _fh:
            _bytes = _fh.read()
    except OSError:
        return False, 'no canonical config on the mirror', None
    if not expected_sha256:
        return False, ('coord-head carries no config_sha256 — refusing '
                       'unverified config'), None
    _got = hashlib.sha256(_bytes).hexdigest()
    if _got != expected_sha256:
        return False, (f'canonical config sha mismatch (head '
                       f'{expected_sha256[:12]} != file {_got[:12]})'), None
    try:
        _doc = json.loads(_bytes.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as _e:
        return False, f'canonical config unparseable: {_e}', None

    def _write(_p: str, _text: str) -> None:
        if _p:
            with open(_p, 'w') as _fh:
                _fh.write(_text if isinstance(_text, str) else '')

    _v = _doc.get('v')
    _lists = _doc.get('lists')
    if isinstance(_v, int) and _v >= 2 and isinstance(_lists, dict):
        _pkg = _lists.get('pkg')
        if not isinstance(_pkg, str):
            return False, 'canonical config missing pkg.list', None
        try:
            _write(pkglist_path, _pkg)
            _write(poollist_path, _lists.get('pool', '') or '')
            _write(livelist_path, _lists.get('live', '') or '')
            _write(installerlist_path, _lists.get('installer', '') or '')
        except OSError as _e:
            return False, f'write failed: {_e}', None
        _payload = {'pins': _doc.get('pins', {}) or {},
                    'closure': _doc.get('closure', {}) or {},
                    'flags': _doc.get('flags', {}) or {},
                    'arch': _doc.get('arch', '') or '',
                    'snapshot': _doc.get('snapshot', '') or ''}
        return True, (f'applied canonical config — 4 lists '
                      f'(pkg {len(_pkg.splitlines())} lines) + selection'), _payload

    # v1 back-compat: pkg.list + pool.list only.
    _pkg = _doc.get('pkg.list')
    _pool = _doc.get('pool.list', '')
    if not isinstance(_pkg, str):
        return False, 'canonical config missing pkg.list', None
    try:
        _write(pkglist_path, _pkg)
        _write(poollist_path, _pool if isinstance(_pool, str) else '')
    except OSError as _e:
        return False, f'write failed: {_e}', None
    return True, (f'applied canonical pkg.list ({len(_pkg.splitlines())} '
                  'lines) + pool.list (v1)'), None
