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
"""
import hashlib
import json
import os
import tempfile
from typing import Optional, Tuple


def _write_lists_atomic(items: 'list[Tuple[str, str]]') -> None:
    """Write several files all-or-nothing.  Each (path, text) is staged to a
    temp file in the SAME directory and fsync'd; only once EVERY file has
    staged successfully are they os.replace()'d into place (atomic rename
    within a dir).  A failure mid-staging (e.g. ENOSPC) raises BEFORE the first
    replace, so every target file is left untouched — no partial application of
    the canonical config's four lists.  Empty paths are skipped.  Raises
    OSError on failure, after cleaning up any staged temps."""
    _staged: 'list[Tuple[str, str]]' = []        # (tmp_path, final_path)
    try:
        for _p, _text in items:
            if not _p:
                continue
            _dir = os.path.dirname(_p) or '.'
            _fd, _tmp = tempfile.mkstemp(dir=_dir, prefix='.canon-',
                                         suffix='.tmp')
            try:
                with os.fdopen(_fd, 'w', encoding='utf-8') as _fh:
                    _fh.write(_text if isinstance(_text, str) else '')
                    _fh.flush()
                    os.fsync(_fh.fileno())
            except BaseException:
                try:
                    os.unlink(_tmp)
                finally:
                    raise
            _staged.append((_tmp, _p))
        for _tmp, _p in _staged:
            os.replace(_tmp, _p)
        _staged = []
    finally:
        for _tmp, _ in _staged:
            try:
                os.unlink(_tmp)
            except OSError:
                pass

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

    Returns (ok, detail, selection_payload).  The payload is
    `{pins, closure, flags, arch, snapshot}` for the caller to seed
    selection.state; on failure it is None."""
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

    _v = _doc.get('v')
    _lists = _doc.get('lists')
    if isinstance(_v, int) and _v >= 2 and isinstance(_lists, dict):
        _pkg = _lists.get('pkg')
        if not isinstance(_pkg, str):
            return False, 'canonical config missing pkg.list', None
        try:
            # All-or-nothing: a mid-write failure (e.g. ENOSPC during pull)
            # must not leave pkg.list rewritten with federation content while
            # pool/live/installer keep stale content AND the caller is told
            # "NOT applied" (so no re-resolve is forced) — a silent split brain.
            _write_lists_atomic([
                (pkglist_path, _pkg),
                (poollist_path, _lists.get('pool', '') or ''),
                (livelist_path, _lists.get('live', '') or ''),
                (installerlist_path, _lists.get('installer', '') or ''),
            ])
        except OSError as _e:
            return False, f'write failed: {_e}', None
        _payload = {'pins': _doc.get('pins', {}) or {},
                    'closure': _doc.get('closure', {}) or {},
                    'flags': _doc.get('flags', {}) or {},
                    'arch': _doc.get('arch', '') or '',
                    'snapshot': _doc.get('snapshot', '') or ''}
        return True, (f'applied canonical config — 4 lists '
                      f'(pkg {len(_pkg.splitlines())} lines) + selection'), _payload

    return False, (f'canonical config has unsupported schema '
                   f'(v={_v!r}) — republish from a current builder'), None


# ───────────────────────── ClosureLedger ─────────────────────────
#
# Same trust model as the canonical config: the owner writes
# `<coord>/config/closure_ledger.json` and pins its sha256 in the SIGNED
# coord-head (`closure_ledger_sha256`).  A peer fetches it on `mirror pull`,
# verifies the file's sha against the head pin, and drives its download set
# off it — assembling the full closure across a mixed-snapshot mirror without
# keying on per-claim snapshots.  Nothing unverified is ever consumed.


def closure_ledger_path(coord_dir: str) -> str:
    return os.path.join(coord_dir, 'config', 'closure_ledger.json')


def write_closure_ledger(coord_dir: str, ledger_doc: dict) -> str:
    """Write `<coord_dir>/config/closure_ledger.json` from a
    `schema.new_closure_ledger` doc.  Returns its sha256 (hex) for the
    coord-head pin, or '' on any write failure (publish never aborts over
    the ledger — a missing pin just makes peers fall back to live claims)."""
    try:
        _payload = json.dumps(
            ledger_doc, sort_keys=True, ensure_ascii=True,
            indent=2).encode('utf-8')
    except (TypeError, ValueError):
        return ''
    _path = closure_ledger_path(coord_dir)
    try:
        os.makedirs(os.path.dirname(_path), mode=0o755, exist_ok=True)
        with open(_path, 'wb') as _fh:
            _fh.write(_payload)
    except OSError:
        return ''
    return hashlib.sha256(_payload).hexdigest()


def read_verified_closure_ledger(
    fetched_coord_dir: str, expected_sha256: str,
) -> 'Tuple[bool, str, Optional[dict]]':
    """Verify the fetched closure_ledger.json against `expected_sha256`
    (carried by the signed coord-head) and return its parsed doc.  Refuses on
    absence / missing head pin / sha mismatch / unparseable — never returns a
    doc the head doesn't vouch for.

    Returns (ok, detail, ledger_doc).  ledger_doc is None on any failure."""
    _path = closure_ledger_path(fetched_coord_dir)
    try:
        with open(_path, 'rb') as _fh:
            _bytes = _fh.read()
    except OSError:
        return False, 'no closure ledger on the mirror', None
    if not expected_sha256:
        return False, ('coord-head carries no closure_ledger_sha256 — '
                       'refusing unverified ledger'), None
    _got = hashlib.sha256(_bytes).hexdigest()
    if _got != expected_sha256:
        return False, (f'closure ledger sha mismatch (head '
                       f'{expected_sha256[:12]} != file {_got[:12]})'), None
    try:
        _doc = json.loads(_bytes.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as _e:
        return False, f'closure ledger unparseable: {_e}', None
    if not isinstance(_doc, dict) or not isinstance(_doc.get('entries'), dict):
        return False, 'closure ledger malformed (no entries map)', None
    return True, (f'verified closure ledger '
                  f'({len(_doc["entries"])} entries)'), _doc
