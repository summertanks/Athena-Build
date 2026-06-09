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
from typing import Any, Dict, Optional, Tuple

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


# ───────────────────────── closure extraction + diff ─────────────────────────


def _canonical_bin_tiers(dep_tree: 'Any') -> 'Dict[str, list]':
    """Canonical binary names of one deb tree → sorted tier tags.

    Canonical = the key equals the Package object's own ``Package:`` field
    (virtual ``Provides:`` aliases are dropped — exactly the filter
    ``cmd_parse_dependency`` uses when it writes selected_packages.list).
    Tiers are membership in the tree's classification sets; a name in none of
    them is ``base`` (installed everywhere).
    """
    _out: 'Dict[str, list]' = {}
    _extras: set = getattr(dep_tree, 'extras_pkg_names', set())
    _live: set = getattr(dep_tree, 'live_exclusive_pkg_names', set())
    _inst: set = getattr(dep_tree, 'installer_exclusive_pkg_names', set())
    _pool: set = getattr(dep_tree, 'pool_extras_pkg_names', set())
    _groups: dict = getattr(dep_tree, 'pkg_group_pkg_names', {}) or {}
    for _name, _pkg in dep_tree.selected_pkgs.items():
        if _name != _pkg['Package']:          # virtual alias — skip
            continue
        _tiers: set = set()
        if _name in _extras:
            _tiers.add('extras')
        if _name in _live:
            _tiers.add('live')
        if _name in _inst:
            _tiers.add('installer')
        if _name in _pool:
            _tiers.add('pool')
        for _g, _members in _groups.items():
            if _name in _members:
                _tiers.add(f'group:{_g}')
        if not _tiers:
            _tiers.add('base')
        _out[_name] = sorted(_tiers)
    return _out


def build_closure(
    dep_tree: 'Any', udeb_dep_tree: 'Any', config: 'Any',
) -> dict:
    """The resolved selection as a closure document fragment:

        {"bins": {name: [tier, ...]}, "srcs": {name: [origin, ...]}}

    Unions the deb tree and (if present) the udeb tree — the udeb sources
    (anna, cdrom-detect, debootstrap, choose-mirror…) live ONLY in the udeb
    tree, so any single-tree view would falsely orphan the entire installer.
    udeb-tree entries carry a ``udeb`` origin tag so the two tiers stay
    distinguishable.  Canonical names only.
    """
    _bins: 'Dict[str, list]' = _canonical_bin_tiers(dep_tree)
    _srcs: 'Dict[str, list]' = {
        _s: ['deb'] for _s in dep_tree.selected_srcs
    }
    if udeb_dep_tree is not None:
        for _name, _tiers in _canonical_bin_tiers(udeb_dep_tree).items():
            _merged = sorted(set(_bins.get(_name, [])) | set(_tiers) | {'udeb'})
            _bins[_name] = _merged
        for _s in udeb_dep_tree.selected_srcs:
            _merged_s = sorted(set(_srcs.get(_s, [])) | {'udeb'})
            _srcs[_s] = _merged_s
    return {'bins': _bins, 'srcs': _srcs}


def diff_closure(
    old: dict, new: dict,
) -> 'Tuple[Dict[str, set], Dict[str, set]]':
    """Compare two closures by NAME SET only (tier-tag changes are
    non-blocking metadata).  Returns ``(added, removed)`` where each is
    ``{'bins': set, 'srcs': set}``.

    The parse guard blocks on any non-empty ``removed`` (deprecation
    candidates); ``added`` is low-impact (refresh + warn).
    """
    _ob, _nb = set((old or {}).get('bins', {})), set((new or {}).get('bins', {}))
    _os, _ns = set((old or {}).get('srcs', {})), set((new or {}).get('srcs', {}))
    _added = {'bins': _nb - _ob, 'srcs': _ns - _os}
    _removed = {'bins': _ob - _nb, 'srcs': _os - _ns}
    return _added, _removed
