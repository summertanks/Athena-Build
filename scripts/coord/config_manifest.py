"""Canonical builder config (pkg.list + pool.list) propagated through the
coord tree.

The distribution-mode owner writes `<coord>/config/canonical.json` on publish
and records its sha256 in the SIGNED coord-head (`config_sha256`).  Peers
fetch it on `mirror builders register` / `mirror pull`, verify the file's
sha256 against the head's `config_sha256`, and overwrite their local pkg.list
+ pool.list — so every builder in the federation shares one package
selection.  Nothing unverified is ever applied (the head's signature vouches
for the hash; the hash vouches for the file).
"""
import hashlib
import json
import os
from typing import Tuple

CONFIG_MANIFEST_VERSION = 1


def manifest_path(coord_dir: str) -> str:
    return os.path.join(coord_dir, 'config', 'canonical.json')


def write_canonical_config(coord_dir: str, pkglist_path: str,
                           poollist_path: str) -> str:
    """Write `<coord_dir>/config/canonical.json` from the local pkg.list +
    pool.list.  Returns its sha256 (hex) for the coord-head, or '' if the
    local pkg.list can't be read."""
    try:
        with open(pkglist_path) as _fh:
            _pkg = _fh.read()
    except OSError:
        return ''
    _pool = ''
    if os.path.isfile(poollist_path):
        try:
            with open(poollist_path) as _fh:
                _pool = _fh.read()
        except OSError:
            _pool = ''
    _doc = {'v': CONFIG_MANIFEST_VERSION,
            'pkg.list': _pkg, 'pool.list': _pool}
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
                           pkglist_path: str,
                           poollist_path: str) -> Tuple[bool, str]:
    """Verify the fetched canonical.json against `expected_sha256` (carried by
    the signed coord-head) and overwrite local pkg.list + pool.list.  Refuses
    on absence / missing head hash / sha mismatch — never applies unverified
    config."""
    _path = manifest_path(fetched_coord_dir)
    try:
        with open(_path, 'rb') as _fh:
            _bytes = _fh.read()
    except OSError:
        return False, 'no canonical config on the mirror'
    if not expected_sha256:
        return False, ('coord-head carries no config_sha256 — refusing '
                       'unverified config')
    _got = hashlib.sha256(_bytes).hexdigest()
    if _got != expected_sha256:
        return False, (f'canonical config sha mismatch (head '
                       f'{expected_sha256[:12]} != file {_got[:12]})')
    try:
        _doc = json.loads(_bytes.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as _e:
        return False, f'canonical config unparseable: {_e}'
    _pkg = _doc.get('pkg.list')
    _pool = _doc.get('pool.list', '')
    if not isinstance(_pkg, str):
        return False, 'canonical config missing pkg.list'
    try:
        with open(pkglist_path, 'w') as _fh:
            _fh.write(_pkg)
        with open(poollist_path, 'w') as _fh:
            _fh.write(_pool if isinstance(_pool, str) else '')
    except OSError as _e:
        return False, f'write failed: {_e}'
    return True, (f'applied canonical pkg.list ({len(_pkg.splitlines())} '
                  'lines) + pool.list')
