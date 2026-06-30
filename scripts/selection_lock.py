"""The signed package-selection lockfile (`config/selection.state`).

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
                "IncludeBuildClosure": false},
      "seeds": {"pkg": {"<group>": [...]},
                "live": [...], "installer": [...], "pool": [...]},
      "seeds_raw": {"pkg": "<verbatim file text>",   # SELECT-01: byte-faithful
                    "live": "...", "installer": "...", "pool": "..."},

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
STATUS_IOERROR = 'ioerror'   # present but a transient read error (not corruption)


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
      * ``(None, 'ioerror')``  — present but a transient READ error (EIO/
                                 EACCES/NFS) — NOT corruption; retry, don't
                                 re-baseline

    Callers treat 'badsig', 'malformed' and 'ioerror' as HARD errors (do not
    rebuild / fail closed), 'missing' as the bootstrap trigger.
    """
    _path = selection_state_path(config)
    try:
        with open(_path, 'rb') as _fh:
            _raw = _fh.read()
    except FileNotFoundError:
        return None, STATUS_MISSING
    except OSError as _e:
        # A transient READ error (EIO/EACCES/ENFILE/NFS hiccup) — the file
        # exists but couldn't be read.  Distinct from MALFORMED (a genuine
        # parse/sig failure below): both fail closed, but IOERROR tells the
        # operator to retry, not to wipe + re-baseline a healthy lockfile.
        utils.logger.warning(f"selection.state read failed ({_path}): {_e}")
        return None, STATUS_IOERROR

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


# ───────────────────────── full-state assembly + policy ──────────────────────


def _read_flat_seeds(path: str) -> list:
    """Raw seed names from a flat list file (one per line, # comments and
    blanks ignored).  Order-preserving — used to round-trip a file on
    `cache restore`."""
    try:
        _raw = utils.readfile(path).split('\n')
    except OSError:
        return []
    _out = []
    for _line in _raw:
        _name = _line.strip()
        if _name and not _name.startswith('#'):
            _out.append(_name)
    return _out


def seeds_from_config(config: 'Any') -> dict:
    """The `seeds` block (the 4 lists as parsed) for the selection state, read
    from the config's list paths.  Shared by assemble_state and the federation
    seeder so both produce the identical shape."""
    _pkg = getattr(config, 'pkglist_path', '')
    return {
        'pkg': utils.parse_pkg_list_groups(_pkg) if _pkg else {},
        # pkg-group descriptions — preserved so `cache restore` can faithfully
        # regenerate the `## Description:` lines tasksel needs.
        'pkg_meta': utils.parse_pkg_list_group_meta(_pkg) if _pkg else {},
        'live': _read_flat_seeds(getattr(config, 'livelist_path', '')),
        'installer': _read_flat_seeds(
            getattr(config, 'installerlist_path', '')),
        'pool': _read_flat_seeds(getattr(config, 'poollist_path', '')),
    }


def seeds_raw_from_config(config: 'Any') -> 'Dict[str, str]':
    """Verbatim text of the four seed list files (SELECT-01).

    Captured so `cache restore` can rewrite each file BYTE-FOR-BYTE, keeping
    operator comments (pool.list ships ~156 doc lines) and — critically —
    pkg-group ORDER.  Group order is load-bearing: it steers OR-dependency
    resolution, so re-rendering from the parsed `seeds` (whose group dict is
    alphabetised by `json.dumps(sort_keys=True)`) can drift the closure
    (SELECT-02).  The parsed `seeds` block stays the closure authority; this
    is purely restore fidelity.  A missing file maps to '' so `restore_list_files`
    falls back to the name-render (also covers lockfiles written before this key).
    """
    def _read(path: str) -> str:
        try:
            return utils.readfile(path) if path else ''
        except OSError:
            return ''
    return {
        'pkg': _read(getattr(config, 'pkglist_path', '')),
        'live': _read(getattr(config, 'livelist_path', '')),
        'installer': _read(getattr(config, 'installerlist_path', '')),
        'pool': _read(getattr(config, 'poollist_path', '')),
    }


def assemble_state(
    dep_tree: 'Any', udeb_dep_tree: 'Any', config: 'Any',
    closure: 'Optional[dict]' = None,
) -> dict:
    """Build the full signed-state document from the resolved trees + config.
    `closure` may be passed to avoid recomputation; otherwise built here.

    Pins are the UNION of both trees' `_pinned_chosen` (every genuinely
    ambiguous resolution this run made).  Flags lock IncludeRecommends (the
    closure guard enforces it — flipping it off shrinks the closure ⇒ blocks).
    """
    if closure is None:
        closure = build_closure(dep_tree, udeb_dep_tree, config)
    _pins: 'Dict[str, str]' = {}
    _pins.update(getattr(dep_tree, '_pinned_chosen', {}) or {})
    if udeb_dep_tree is not None:
        _pins.update(getattr(udeb_dep_tree, '_pinned_chosen', {}) or {})
    return {
        'schema_version': SELECTION_STATE_SCHEMA_VERSION,
        'arch': getattr(config, 'arch', ''),
        'snapshot': getattr(config, 'snapshot_timestamp_config', ''),
        'flags': {
            'IncludeRecommends': bool(getattr(config, 'include_recommends', False)),
            'IncludeBuildClosure': bool(
                getattr(config, 'include_build_closure', False)),
        },
        'seeds': seeds_from_config(config),
        'seeds_raw': seeds_raw_from_config(config),
        'closure': closure,
        'pins': _pins,
    }


def federation_state(config: 'Any', payload: dict) -> dict:
    """Build a selection.state from a federation-applied canonical config:
    `seeds` parsed from the just-written local lists (byte-faithful, since the
    peer wrote them verbatim); `pins`/`closure`/`flags`/`arch`/`snapshot`
    adopted from the OWNER's payload so the peer's next `cache parse` resolves
    with the same provider pins and validates against the owner's closure.

    The caller signs it with the PEER's own HMAC key (write_selection_state);
    the federation trust came from the GPG-signed coord-head sha that gated the
    apply — the HMAC is only local tamper-detection."""
    return {
        'schema_version': SELECTION_STATE_SCHEMA_VERSION,
        'arch': payload.get('arch') or getattr(config, 'arch', ''),
        'snapshot': (payload.get('snapshot')
                     or getattr(config, 'snapshot_timestamp_config', '')),
        'flags': payload.get('flags') or {
            'IncludeRecommends': bool(
                getattr(config, 'include_recommends', False)),
            'IncludeBuildClosure': bool(
                getattr(config, 'include_build_closure', False)),
        },
        'seeds': seeds_from_config(config),
        # The peer wrote the four list files verbatim from the owner's
        # canonical config, so their raw bytes are byte-faithful here too.
        'seeds_raw': seeds_raw_from_config(config),
        'closure': payload.get('closure') or {},
        'pins': payload.get('pins') or {},
    }


# Parse-guard outcomes (Chunk 4 consumes these; the IO/console lives in
# cmd_parse_dependency).
ACTION_BOOTSTRAP = 'bootstrap'   # no lockfile yet → write fresh, proceed
ACTION_REFRESH = 'refresh'       # closure unchanged or additions-only → write, proceed
ACTION_BLOCK = 'block'           # closure SHRANK → deprecation candidates, refuse
ACTION_HARDSTOP = 'hardstop'     # lockfile present but badsig/malformed → refuse


def classify(
    read_status: str, lock: 'Optional[dict]', fresh_closure: dict,
) -> 'Tuple[str, Dict[str, set], Dict[str, set]]':
    """Pure policy: given the lockfile read status, the loaded lockfile (or
    None), and the freshly-resolved closure, decide the parse-guard action.

    Returns ``(action, added, removed)`` where added/removed are
    ``{'bins': set, 'srcs': set}``.  Asymmetric: ANY removal ⇒ BLOCK
    (deprecation candidates); additions alone ⇒ REFRESH (low-impact).
    """
    def _fresh() -> 'Dict[str, set]':
        # A NEW dict with NEW sets each call — dict(_empty) was a shallow copy
        # that shared the inner set() objects across every return, so a caller
        # mutating an 'added'/'removed' set would corrupt later results.
        return {'bins': set(), 'srcs': set()}
    if read_status == STATUS_MISSING:
        return ACTION_BOOTSTRAP, _fresh(), _fresh()
    if read_status in (STATUS_BADSIG, STATUS_MALFORMED, STATUS_IOERROR):
        return ACTION_HARDSTOP, _fresh(), _fresh()
    _added, _removed = diff_closure((lock or {}).get('closure', {}), fresh_closure)
    if _removed['bins'] or _removed['srcs']:
        return ACTION_BLOCK, _added, _removed
    return ACTION_REFRESH, _added, _removed


# ───────────────────── list-file regeneration (cache restore) ────────────────


def render_pkg_list(seeds: dict) -> str:
    """Render the grouped pkg.list text from a lockfile's seeds — INI groups
    with their `## Description:` metadata preserved, so a re-parse reproduces
    the same groups + tasksel descriptions.  Round-trips through
    ``utils.parse_pkg_list_groups`` / ``parse_pkg_list_group_meta``."""
    _pkg = (seeds or {}).get('pkg', {}) or {}
    _meta = (seeds or {}).get('pkg_meta', {}) or {}
    _lines: list = []
    for _group, _names in _pkg.items():
        _lines.append(f'[{_group}]')
        _desc = (_meta.get(_group, {}) or {}).get('description')
        if _desc:
            _lines.append(f'## Description: {_desc}')
        _lines.extend(_names)
        _lines.append('')
    return ('\n'.join(_lines).rstrip('\n') + '\n') if _lines else ''


def render_flat_list(names: list) -> str:
    """Render a flat list file (pool/live/installer) — one seed per line."""
    return ('\n'.join(names) + '\n') if names else ''


def restore_list_files(config: 'Any', lock: dict) -> 'Dict[str, str]':
    """Regenerate config/{pkg,live,installer,pool}.list from a lockfile's
    seeds (the authoritative selection).  Returns {label: path} for the files
    written.  IO — caller confirms with the operator first."""
    _seeds = (lock or {}).get('seeds', {}) or {}
    _raw = (lock or {}).get('seeds_raw', {}) or {}
    _written: 'Dict[str, str]' = {}
    _targets = [
        ('pkg', getattr(config, 'pkglist_path', ''), render_pkg_list(_seeds)),
        ('live', getattr(config, 'livelist_path', ''),
         render_flat_list(_seeds.get('live', []))),
        ('installer', getattr(config, 'installerlist_path', ''),
         render_flat_list(_seeds.get('installer', []))),
        ('pool', getattr(config, 'poollist_path', ''),
         render_flat_list(_seeds.get('pool', []))),
    ]
    for _label, _path, _rendered in _targets:
        if not _path:
            continue
        # SELECT-01: prefer the verbatim bytes (operator comments + pkg-group
        # order preserved); fall back to the name-render for lockfiles written
        # before seeds_raw existed (or a file that was absent at capture time).
        _verbatim = _raw.get(_label)
        _text = _verbatim if isinstance(_verbatim, str) and _verbatim else _rendered
        utils._atomic_write_bytes(_path, _text.encode('utf-8'))
        _written[_label] = _path
    return _written


# closure_matches_lock outcomes.
COHERENCE_MATCH = 'match'      # in-memory closure == signed lockfile
COHERENCE_DELTA = 'delta'      # lockfile OK but the resolved closure differs
COHERENCE_NOLOCK = 'nolock'    # no lockfile yet (force-through / pre-bootstrap)
COHERENCE_BADLOCK = 'badlock'  # lockfile present but untrusted (badsig/malformed)


def closure_matches_lock(
    dep_tree: 'Any', udeb_dep_tree: 'Any', config: 'Any',
) -> 'Tuple[str, Dict[str, set], Dict[str, set]]':
    """Defensive check for consumers that act on the in-memory selection
    (ISO staging, audits): does the resolved closure match the signed
    selection.state?  Returns ``(status, added, removed)`` where status is one
    of COHERENCE_MATCH / _DELTA / _NOLOCK / _BADLOCK.

    Lets a stale RAM tree never silently disagree with the authority a
    publish/ISO would ship against — a genuine DELTA is a hard error for the
    caller, while NOLOCK (authority not written yet) is typically a soft warn."""
    _empty: 'Dict[str, set]' = {'bins': set(), 'srcs': set()}
    _lock, _status = read_selection_state(config)
    if _status in (STATUS_BADSIG, STATUS_MALFORMED, STATUS_IOERROR):
        return COHERENCE_BADLOCK, dict(_empty), dict(_empty)
    _fresh = build_closure(dep_tree, udeb_dep_tree, config)
    if _status == STATUS_MISSING or _lock is None:
        return COHERENCE_NOLOCK, {'bins': set(_fresh['bins']),
                                  'srcs': set(_fresh['srcs'])}, dict(_empty)
    _added, _removed = diff_closure(_lock.get('closure', {}), _fresh)
    if _added['bins'] or _added['srcs'] or _removed['bins'] or _removed['srcs']:
        return COHERENCE_DELTA, _added, _removed
    return COHERENCE_MATCH, _added, _removed


def audit_selection_coherence(
    closure_srcs: 'set', by_builder: 'Dict[str, list]', builder_id: str,
    buildlog_dir: str = '',
    read_build_record: 'Optional[Any]' = None,
) -> 'list':
    """Coherence check: the lockfile selection ⟷ our published claims.

    For a file we still OWN+PUBLISH whose SOURCE is no longer selected:

      * — when the source's build record says
        ``selection='deprecated'`` (the operator accepted the removal at
        `cache select accept`, intent recorded locally), this is the
        EXPECTED window before the next publish propagates it →
        **WARNING** ``selection_deprecation_pending``.
      * otherwise the signed selection and the mirror disagree with NO
        recorded intent (a drop that bypassed accept, or a publish that
        failed to emit) → **CRITICAL** ``selection_claim_not_in_closure``.

    Pass ``buildlog_dir`` + ``read_build_record`` to enable the pending
    refinement; without them every mismatch stays CRITICAL (back-compat).

    Keyed on the SOURCE (the claim's `package`), NOT the binary name: a source
    build publishes ALL its binaries (-dev/-doc/-udeb/-source variants), most
    of which are legitimately NOT in the install closure (`closure.bins`).  The
    deprecation unit is the source — while a source stays selected every binary
    it builds is a valid published artifact.  Pure; `mirror audit` wires it in.
    """
    from coord import store as _store
    _owners = _store.project_owners(by_builder)
    _findings: list = []
    _pending_cache: 'Dict[str, bool]' = {}

    def _intent_recorded(_src: str) -> bool:
        if read_build_record is None or not buildlog_dir:
            return False
        if _src not in _pending_cache:
            _rec = read_build_record(buildlog_dir, _src)
            _pending_cache[_src] = bool(
                _rec and _rec.get('selection') == 'deprecated')
        return _pending_cache[_src]

    for _fn in sorted(_owners):
        _owner = _owners[_fn]
        if _owner.get('builder') != builder_id:
            continue
        if _owner.get('claim_state') != 'published':
            continue
        _src = str((_owner.get('claim') or {}).get('package') or '')
        if _src and _src not in closure_srcs:
            if _intent_recorded(_src):
                _findings.append((
                    'WARNING', 'selection_deprecation_pending',
                    f"{_fn}: source {_src!r} deprecated"))
            else:
                _findings.append((
                    'CRITICAL', 'selection_claim_not_in_closure',
                    f"{_fn}: owned+published but its source {_src!r} is no "
                    "longer selected and not deprecated — `mirror publish` "
                    "to release it, or `cache select` to re-add it"))
    return _findings
