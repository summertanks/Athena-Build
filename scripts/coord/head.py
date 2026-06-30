"""Coord-head manifest — GPG-clearsigned canonical state.

The coord-head is the single point of authority for "what is the
current state of the coord tree", signed by the tier-1 (InRelease)
GPG key.

On-disk layout, sibling of repo-coord/claims/:

  coord-head.json       canonical JSON body (the dict from
                        schema.new_coord_head)
  coord-head.json.sig   ASCII-armored detached GPG signature

Mirrors the existing `repo_audit._write_signed_manifest` / `_read_
signed_manifest` pattern (detached `.sig` next to the data file)
verbatim so the operator's mental model is one shape across the
codebase.  Differences:

  - data file is JSON (not Packages text) — that's just the bytes
  - signing key is still tier-1 (signing.signing_home(config))
  - verify is the same gpg --verify pattern

Freshness:
  read_coord_head(...) returns the parsed dict + the head_time field.
  is_fresh(head, inrelease_sha256, inrelease_date_iso) is a separate
  policy check the caller composes (called by `coord sync pull` in
  P2 and the publish state machine in P3).
"""

import datetime as _dt
import json as _json
import logging as _logging
import os as _os
import subprocess as _subprocess
from typing import Optional, Tuple

from . import policy as _policy

logger = _logging.getLogger('athena')


COORD_HEAD_BASENAME = 'coord-head.json'
COORD_HEAD_SIG_SUFFIX = '.sig'


def coord_head_path(coord_dir: str) -> str:
    return _os.path.join(coord_dir, COORD_HEAD_BASENAME)


def coord_head_sig_path(coord_dir: str) -> str:
    return coord_head_path(coord_dir) + COORD_HEAD_SIG_SUFFIX


def write_coord_head(
    coord_dir: str, head: dict, signing_homedir: str,
) -> bool:
    """Serialize `head` to canonical JSON, write to coord-head.json,
    GPG-detached-sign with tier-1 key, write to coord-head.json.sig.

    Returns True iff BOTH the JSON AND the signature land on disk.
    On signing failure, the unsigned JSON is removed (no false
    authority — mirrors discipline from repo_audit).
    """
    _path = coord_head_path(coord_dir)
    _sig = coord_head_sig_path(coord_dir)
    # Stage to temp files and only atomically swap BOTH in after signing
    # succeeds — so a write/sign failure leaves the PRIOR signed manifest+sig
    # intact instead of destroying it and leaving no authority.
    _path_tmp = _path + '.tmp'
    _sig_tmp = _sig + '.tmp'

    def _cleanup_tmp() -> None:
        for _t in (_path_tmp, _sig_tmp):
            try:
                _os.unlink(_t)
            except OSError:
                pass

    try:
        _os.makedirs(coord_dir, mode=0o755, exist_ok=True)
        _payload = _json.dumps(
            head, sort_keys=True, ensure_ascii=True, indent=2,
        ).encode('utf-8')
        with open(_path_tmp, 'wb') as _fh:
            _fh.write(_payload)
    except OSError as _e:
        logger.error(f"coord.head write: {_e}")
        _cleanup_tmp()
        return False
    if not signing_homedir or not _os.path.isdir(signing_homedir):
        logger.error(
            f"coord.head: tier-1 signing homedir missing or unreadable: "
            f"{signing_homedir!r} — refusing to leave unsigned manifest")
        _cleanup_tmp()
        return False
    _r = _subprocess.run(
        ['gpg', '--homedir', signing_homedir, '--batch', '--yes',
         '--detach-sign', '--armor', '-o', _sig_tmp, _path_tmp],
        capture_output=True, text=True,
    )
    if _r.returncode != 0:
        logger.error(
            f"coord.head: gpg --detach-sign failed (rc={_r.returncode}): "
            f"{_r.stderr.strip()[:200]} — leaving prior manifest intact")
        _cleanup_tmp()
        return False
    # The detached signature is over _path_tmp's bytes; os.replace preserves
    # them, so the .sig still verifies against the final coord-head.json.
    try:
        _os.replace(_path_tmp, _path)
        _os.replace(_sig_tmp, _sig)
    except OSError as _e:
        logger.error(f"coord.head: atomic replace failed: {_e}")
        _cleanup_tmp()
        return False
    return True


def read_coord_head(
    coord_dir: str, signing_homedir: str,
) -> Optional[dict]:
    """Read coord-head.json and verify against coord-head.json.sig
    using `signing_homedir` (gpg --verify).

    Returns the parsed dict on success.  Returns None for:
      - missing JSON or sig
      - signature verify failure
      - JSON decode error
      - signing homedir missing / unreadable

    Logs an ERROR on any of those so the operator sees the cause
    (replay/rollback would show up as a signed-but-old head, NOT a
    read failure — freshness is checked separately).
    """
    _path = coord_head_path(coord_dir)
    _sig = coord_head_sig_path(coord_dir)
    if not _os.path.isfile(_path):
        return None
    if not _os.path.isfile(_sig):
        logger.error(f"coord.head: {_path} present but {_sig} absent")
        return None
    if not signing_homedir or not _os.path.isdir(signing_homedir):
        logger.error(
            "coord.head: tier-1 homedir missing — refusing unverified read")
        return None
    _r = _subprocess.run(
        ['gpg', '--homedir', signing_homedir, '--batch', '--verify',
         _sig, _path],
        capture_output=True, text=True,
    )
    if _r.returncode != 0:
        logger.error(
            f"coord.head: SIGNATURE VERIFY FAILED for {_path} "
            f"(rc={_r.returncode}): {_r.stderr.strip()[:200]}")
        return None
    try:
        with open(_path, 'rb') as _fh:
            _data = _json.loads(_fh.read())
    except (OSError, _json.JSONDecodeError) as _e:
        logger.error(f"coord.head: parse {_path}: {_e}")
        return None
    if not isinstance(_data, dict):
        logger.error(f"coord.head: {_path} is not a JSON object")
        return None
    return _data


def is_fresh(
    head: dict, inrelease_sha256: str, inrelease_date_iso: str,
) -> Tuple[bool, str]:
    """Policy check: is this coord-head fresh enough to use?

    Returns (ok, reason).  `ok=False` cases:
      - head's inrelease_sha256 doesn't match the InRelease we fetched
        (rollback signal — the head claims to pin a DIFFERENT InRelease)
      - head_time is older than the InRelease Date (the apt-side
        metadata is fresher than our coord state — the apt host may
        have rolled coord back to hide a recent revocation)
      - head_time is more than COORD_HEAD_MAX_AGE_SECONDS old

    Time parsing tolerates the trailing-Z UTC format and locale-
    independent ISO-8601.
    """
    _head_in = head.get('inrelease_sha256')
    if _head_in != inrelease_sha256:
        return False, (
            f"coord-head pins InRelease sha256={_head_in!r}, "
            f"we fetched {inrelease_sha256!r}")
    _head_time_s = head.get('head_time')
    if not isinstance(_head_time_s, str) or not _head_time_s:
        return False, "coord-head missing head_time"
    try:
        _head_time = _dt.datetime.fromisoformat(
            _head_time_s.replace('Z', '+00:00'))
    except ValueError:
        return False, f"coord-head head_time unparseable: {_head_time_s!r}"
    if _head_time.tzinfo is None:
        _head_time = _head_time.replace(tzinfo=_dt.timezone.utc)
    _ir_dt: 'Optional[_dt.datetime]' = None
    if inrelease_date_iso:
        try:
            _ir_dt = _dt.datetime.fromisoformat(
                inrelease_date_iso.replace('Z', '+00:00'))
            if _ir_dt.tzinfo is None:
                _ir_dt = _ir_dt.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            _ir_dt = None
    if _ir_dt is not None and _head_time < _ir_dt:
        return False, (
            f"coord-head head_time {_head_time_s} predates InRelease "
            f"Date {inrelease_date_iso}")
    _now = _dt.datetime.now(_dt.timezone.utc)
    _age = (_now - _head_time).total_seconds()
    if _age > _policy.COORD_HEAD_MAX_AGE_SECONDS:
        return False, (
            f"coord-head is {int(_age)}s old; limit is "
            f"{_policy.COORD_HEAD_MAX_AGE_SECONDS}s")
    return True, ''
