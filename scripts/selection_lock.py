"""SELECT-LOCK — the signed package-selection lockfile (`config/selection.state`).

The build resolves a package SELECTION from the seed lists (pkg.list, pool.list,
live.list, installer.list) into a closure of binary + source names.  Historically
that closure lived only in RAM, so a stray list edit silently changed what the
whole distribution shipped, and `mirror publish` had no authority telling it a
package we own had been dropped.

This module is the AUTHORITY for the resolved selection: a single HMAC-signed
JSON document that `cache parse` writes on first run and guards against on every
subsequent run.  The signing reuses the build-record HMAC key
(`log/build/.metrics.hmac.key`) — one local tamper-detection key; the threat
model is exactly the build records' (detect accidental/local corruption, not
defend against a remote adversary, which is the federation Ed25519 layer's job).

Chunk 1 scope: pure read/write/verify of the document.  Closure extraction and
the parse-time guard live in `build_closure`/`diff_closure` (Chunk 2) and
`cmd_parse_dependency` (Chunk 4).

State document shape (extensible — readers MUST preserve unknown keys):

    {
      "schema_version": 1,
      "arch": "amd64",
      "snapshot": "20260602T173733Z",        # informational baseline
      "flags": {"IncludeRecommends": true,
                "IncludeBuildDep": false},    # IncludeBuildDep reserved
      "seeds": {"pkg": {"<group>": [...]},
                "live": [...], "installer": [...], "pool": [...]},
      "closure": {"bins": {"<name>": ["<tier>", ...]},
                  "srcs": {"<name>": ["<origin>", ...]}},
      "pins": {"<ambiguous_seed>": "<chosen_pkg>"},
      "sig": "<hmac-sha256 hex>"
    }
"""

import json
import os
from typing import Optional, Tuple

import utils

SELECTION_STATE_SCHEMA_VERSION = 1

# Read-status discriminator.  Unlike build records (which collapse a verify
# failure to "missing" and rebuild), the selection authority MUST distinguish
# tamper from absence: silently rebuilding a tampered/sig-failed lockfile would
# erase the deprecation history the mirror relies on.
STATUS_OK = 'ok'
STATUS_MISSING = 'missing'
STATUS_BADSIG = 'badsig'
STATUS_MALFORMED = 'malformed'


def selection_state_path(config: 'utils.BuildConfig') -> str:
    """Path to the lockfile — `config/selection.state`, alongside
    `snapshot.state` (see :func:`utils.snapshot_state_path`)."""
    return os.path.join(config.dir_config, 'selection.state')


def _lock_hmac_key(config: 'utils.BuildConfig') -> bytes:
    """The signing key — the SAME local HMAC key that signs build records.
    One key, one threat model (local tamper detection)."""
    return utils._load_or_create_hmac_key(os.path.join(config.dir_log, 'build'))


def read_selection_state(
    config: 'utils.BuildConfig',
) -> 'Tuple[Optional[dict], str]':
    """Load + verify the lockfile.

    Returns ``(state_dict, status)``:
      * ``(dict, 'ok')``       — present and HMAC-verified
      * ``(None, 'missing')``  — file absent (first run / after purge-state)
      * ``(None, 'badsig')``   — present but HMAC mismatch (tamper / key change)
      * ``(None, 'malformed')``— present but not valid JSON / not a dict /
                                 missing schema_version

    Callers treat 'badsig' and 'malformed' as HARD errors (do not rebuild),
    'missing' as the bootstrap trigger.
    """
    _path = selection_state_path(config)
    try:
        with open(_path, 'rb') as _fh:
            _raw = _fh.read()
    except FileNotFoundError:
        return None, STATUS_MISSING
    except OSError as _e:
        utils.logger.warning(f"selection.state read failed ({_path}): {_e}")
        return None, STATUS_MALFORMED

    try:
        _state = json.loads(_raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None, STATUS_MALFORMED
    if not isinstance(_state, dict):
        return None, STATUS_MALFORMED
    if 'schema_version' not in _state:
        return None, STATUS_MALFORMED

    if not utils._verify_record(_state, _lock_hmac_key(config)):
        utils.logger.error(
            f"selection.state SIGNATURE VERIFY FAILED ({_path}) — "
            "refusing to use it (tamper or HMAC-key change)")
        return None, STATUS_BADSIG
    return _state, STATUS_OK


def write_selection_state(config: 'utils.BuildConfig', state: dict) -> None:
    """Sign `state` (HMAC-SHA256 over its canonical form) and atomically write
    it to `config/selection.state`.  `schema_version` is stamped if absent.

    Pure w.r.t. the caller's dict — signs a copy.  Disk form is pretty-printed
    (sorted keys, indent 2) for human diffing; the signature is over the
    canonical compact form so whitespace never affects verification.
    """
    _doc = dict(state)
    _doc.setdefault('schema_version', SELECTION_STATE_SCHEMA_VERSION)
    _signed = utils._sign_record(_doc, _lock_hmac_key(config))
    _data = (json.dumps(_signed, sort_keys=True, indent=2) + '\n').encode('utf-8')
    utils._atomic_write_bytes(selection_state_path(config), _data)
