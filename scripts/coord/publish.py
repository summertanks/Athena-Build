"""Publish transaction — sign + push the claim layer.

Two entry points:

  local_publish    sign pending claims into the local jsonl + bump
                   the local coord-head (offline / single-host).

  remote_publish   acquire remote flock → fetch remote coord state →
                   sanity-check (hash conflict, PUBLISH_HALT) →
                   sign+append pending claims → push jsonl → re-sign
                   coord-head with updated last_seqs → push coord-head
                   → release flock.

`remote_publish` push the .deb pool per-file as part of the same
transaction: each pending claim's binary lands
on the remote pool before its claim line is appended, so a partial
publish leaves a consistent view.  When `pool_remote_spec` is None
(legacy callers), pool push is skipped and the operator is expected
to land binaries out-of-band — `cmd_mirror_publish` always wires
both, so the legacy path is dead in tree.

The 11-step state machine called out in the plan lives here in
remote_publish().  Each step is a single function call; if any
returns failure, we log + release lock + report.  Crash recovery
is forward-only: re-running publish picks up where we left off
because claim writes are idempotent (same seq + same canonical
bytes = same line).
"""

import logging
import os
from typing import Callable, Dict, List, Optional, Tuple

import arch_filter as _arch_filter

from . import head as _head
from . import identity as _identity
from . import reconcile as _reconcile
from . import schema as _schema
from . import store as _store
from . import transport as _transport

logger = logging.getLogger('athena')


# ───────────────────────── pending-claim discovery ─────────────────────────


def generate_pending_claims(
    *,
    builder_id: str,
    buildlog_dir: str,
    claims_dir: str,
    public_key_path: str,
    snapshot_pin: str,
    read_build_record: Callable[[str, str], 'Optional[dict]'],
    build_arch: 'Optional[str]' = None,
) -> List[dict]:
    """Walk build.json records; for each phase=done / phase=tunneled
    output whose filename isn't already in this builder's live jsonl,
    create an UNSIGNED pending claim dict.

    When ``build_arch`` is given, an output whose binary TARGETS a
    foreign architecture (a cross-toolchain by-product like
    ``binutils-aarch64-linux-gnu``; see ``arch_filter``) is skipped so
    the federation never takes ownership of it.  ``None`` (legacy/test
    callers) disables the filter.

    Returns the list in stable order (sorted by package + filename)
    so a re-run before any append produces the same ordering.

    The caller signs each claim, appends to the jsonl, and decides
    when to flip to claim_state='published'.
    """
    _existing = _store.read_builder_claims(
        claims_dir, builder_id, public_key_path)
    _known: set = {
        _c.get('filename') for _c in _existing
        if isinstance(_c.get('filename'), str)
        and _c.get('claim_state') not in _schema.INACTIVE_CLAIM_STATES
    }
    _pending: List[dict] = []
    try:
        _entries = sorted(os.listdir(buildlog_dir))
    except OSError:
        return _pending
    for _entry in _entries:
        if not _entry.endswith('.build.json'):
            continue
        _pkg = _entry[:-len('.build.json')]
        _rec = read_build_record(buildlog_dir, _pkg)
        if _rec is None:
            continue
        _phase = _rec.get('phase')
        if _phase not in ('done', 'tunneled'):
            continue
        # A package we PULLED from a peer (mirror pull stamps `pulled_from`
        # on its build record) belongs to that peer — never re-claim it.
        # It would be ownership-blocked anyway, but skipping avoids the noise
        # and the reverse-sync footgun where a publisher that pulled a peer's
        # packages tries to take ownership of them on its next publish.
        # This INCLUDES adopted tunnels: `mirror pull` now stamps
        # `pulled_from` on tunneled adoptions too, so a peer that pulled a
        # no-owner republish does NOT re-push/re-claim byte-identical copies
        # — only the original republisher ships it.  A SELF-tunnel
        # (`cmd_tunnel_package`, the first shipper) carries no `pulled_from`
        # and stays claimable.
        if _rec.get('pulled_from'):
            continue
        # a deprecated/retracted source must NOT regenerate
        # claims — its build record stays phase=done (the receipt is
        # kept), but the deprecation excluded its old claims from
        # `_known`, so without this guard the next publish would
        # resurrect them as fresh no-owner re-claims and silently UNDO
        # the deprecation.  Re-selection flips the record back to
        # 'selected' (cache parse), restoring the re-claim path.
        if _rec.get('selection') in ('deprecated', 'retracted'):
            continue
        _outputs = _rec.get('outputs') or []
        _hashes = _rec.get('output_hashes') or {}
        # per-output upstream provenance for tunneled
        # passthrough.  Build record's `republished_from` field is
        # the {filename: {url, upstream_sha256}} dict written by
        # cmd_tunnel_package (chunk 9).  We pass each file's entry
        # into the claim so the federation can treat tunneled
        # packages as no-owner (project_owners returns builder=None).
        _republished = _rec.get('republished_from') or {}
        # Apt component for this source's binaries.  Pinned on the
        # build record by new_build_record (from src._mirror.component
        # at entry time).  Default 'main' for back-compat with pre-
        # component records (they pre-date non-main publish, so 'main'
        # is correct anyway).
        _comp = str(_rec.get('component') or 'main')
        for _fn in _outputs:
            if _fn in _known:
                continue
            # A cross-toolchain by-product that TARGETS a foreign arch
            # (binutils-aarch64-linux-gnu, …) is not part of the
            # distribution — never claim it.  Build receipts keep the
            # output; the filter lives only at the claim layer.
            if build_arch and _arch_filter.is_foreign_target_binary(
                    _fn.split('_', 1)[0], build_arch):
                continue
            _sha = _hashes.get(_fn)
            if not isinstance(_sha, str) or not _sha:
                # Pre-coord legacy or skipped backfill — skip.  The
                # operator runs `repo repair backfill-hashes` first.
                continue
            _rfrom = _republished.get(_fn) if isinstance(_republished, dict) else None
            if not (isinstance(_rfrom, dict) and _rfrom):
                _rfrom = None
            _pending.append(_schema.new_claim(
                builder=builder_id,
                seq=0,  # caller assigns before signing
                package=_pkg,
                intended_version=str(_rec.get('intended_version', '')),
                built_version=str(_rec.get('built_version', '')),
                filename=_fn,
                sha256=_sha,
                size=0,  # filled in below by stat'ing pool path
                snapshot=snapshot_pin,
                built_at=str(_rec.get('finished') or _rec.get('started') or ''),
                claim_state=_schema.CLAIM_STATE_PENDING,
                republished_from=_rfrom,
                component=_comp,
            ))
    _pending.sort(key=lambda _c: (_c['package'], _c['filename']))
    return _pending


def emit_deprecation_claims(
    *,
    builder_id: str,
    by_builder: Dict[str, List[dict]],
    closure_srcs: 'set',
    snapshot_pin: str,
    built_at: str,
    start_seq: int,
) -> List[dict]:
    """Build unsigned `deprecated` claims for every file we
    currently OWN+publish whose SOURCE is no longer selected.

    The authority is the selection lockfile (`closure_srcs` = the set of
    selected source names) — NOT build.json, which lingers after a drop.
    Keyed on the SOURCE (the claim's `package`), NOT the binary: a source build
    publishes ALL its binaries (-dev/-doc/-udeb/-source), most of which are
    legitimately absent from the install closure — so the deprecation unit is
    the source.  While a source stays selected every binary it built is kept;
    when the source leaves the selection, all its binaries deprecate together.
    Deprecating releases ownership to the commons (project_owners then reports
    builder=None) while the .deb stays in the pool.

    Idempotent: a file already deprecated is not re-emitted (project_owners
    reports it builder=None, so it fails the `owner == us` test).  Caller
    signs + appends each returned claim, assigning seqs from `start_seq`.
    """
    _owners = _store.project_owners(by_builder)
    _out: List[dict] = []
    _seq = start_seq
    for _fn in sorted(_owners):
        _owner = _owners[_fn]
        if _owner.get('builder') != builder_id:
            continue   # not ours / already released (deprecated / tunneled)
        if _owner.get('claim_state') != _schema.CLAIM_STATE_PUBLISHED:
            continue
        _claim = _owner.get('claim') or {}
        if str(_claim.get('package') or '') in closure_srcs:
            continue   # source still selected — keep owning every binary it builds
        _seq += 1
        _out.append(_schema.new_deprecation(
            builder=builder_id,
            seq=_seq,
            package=str(_claim.get('package') or ''),
            intended_version=str(_claim.get('intended_version') or ''),
            built_version=str(_claim.get('built_version') or ''),
            filename=_fn,
            sha256=str(_claim.get('sha256') or ''),
            size=int(_claim.get('size') or 0),
            snapshot=snapshot_pin,
            built_at=built_at,
            deprecates_seq=int(_claim.get('seq') or 0),
            component=str(_claim.get('component') or 'main'),
        ))
    return _out


def emit_obsolescence_claims(
    *,
    builder_id: str,
    by_builder: Dict[str, List[dict]],
    snapshot_pin: str,
    built_at: str,
    start_seq: int,
) -> List[dict]:
    """Build unsigned `obsolete` claims for every OLD-version
    file we own+publish that a NEWER version of the same binary (same
    name + arch) supersedes.

    Grouping is by (binary name, arch) parsed from the filename via
    rsplit('_', 2) — same-version different-arch files never obsolete
    each other.  Within a group with >1 distinct version, every claim
    but the newest (apt_pkg.version_compare, the same convention as
    filter_pending_by_ownership) gets a new_obsolescence referencing it.

    Only the owner marks; idempotent — once obsoleted, the obsolete
    claim is the filename winner and fails the PUBLISHED test below.
    Caller signs + appends, assigning seqs from `start_seq`.
    """
    import apt_pkg
    try:
        apt_pkg.init_system()
    except Exception:
        pass
    _owners = _store.project_owners(by_builder)
    # (name, arch) → list of (built_version, filename, claim)
    _groups: 'Dict[Tuple[str, str], List[Tuple[str, str, dict]]]' = {}
    for _fn in sorted(_owners):
        _owner = _owners[_fn]
        if _owner.get('builder') != builder_id:
            continue
        if _owner.get('claim_state') != _schema.CLAIM_STATE_PUBLISHED:
            continue
        _base = _fn.rsplit('.', 1)[0]
        _parts = _base.rsplit('_', 2)
        if len(_parts) != 3:
            continue
        _name, _ver, _arch = _parts
        _claim = _owner.get('claim') or {}
        _bv = str(_claim.get('built_version') or _ver)
        _groups.setdefault((_name, _arch), []).append((_bv, _fn, _claim))
    _out: List[dict] = []
    _seq = start_seq

    def _ver_cmp(_a: 'Tuple[str, str, dict]',
                 _b: 'Tuple[str, str, dict]') -> int:
        return int(apt_pkg.version_compare(_a[0], _b[0]))

    import functools
    for (_name, _arch), _entries in sorted(_groups.items()):
        if len(_entries) < 2:
            continue
        _entries.sort(key=functools.cmp_to_key(_ver_cmp))
        # every entry but the newest (last) gets an obsolescence
        for _bv, _fn, _claim in _entries[:-1]:
            _seq += 1
            _out.append(_schema.new_obsolescence(
                builder=builder_id,
                seq=_seq,
                package=str(_claim.get('package') or ''),
                intended_version=str(_claim.get('intended_version') or ''),
                built_version=_bv,
                filename=_fn,
                sha256=str(_claim.get('sha256') or ''),
                size=int(_claim.get('size') or 0),
                snapshot=snapshot_pin,
                built_at=built_at,
                obsoletes_seq=int(_claim.get('seq') or 0),
                component=str(_claim.get('component') or 'main'),
            ))
    return _out


def validate_reclaim_intents(
    *,
    builder_id: str,
    by_builder: Dict[str, List[dict]],
    intents: List[dict],
    snapshot_pin: str,
) -> Tuple[List[dict], List[str]]:
    """Re-validate operator-resolved reclaim intents
    against the freshly fetched remote view and construct the pending
    reclaim claims (seq=0; caller assigns + stamps published at
    append, same as any pending claim).

    Each intent is a ``mirror.local_ahead_candidates`` row: filename,
    package, component, old_seq, remote_sha, local_sha, size,
    intended_version, built_version.

    Returns ``(claims, skip_reasons)``.  An intent is SKIPPED (loud,
    never fatal — the rest of the publish proceeds) when, on the
    just-fetched view, the back-referenced claim:
      - doesn't exist at old_seq / names a different filename
      - isn't a live published assertion (marker state, or already
        superseded by any *_seq back-ref — e.g. a concurrent reclaim)
      - carries a different sha than the operator confirmed at listing
        time (remote drift)
      - already matches the local sha (nothing to reclaim)
    """
    _ok: List[dict] = []
    _skipped: List[str] = []
    _ours = by_builder.get(builder_id) or []
    _dead: set = set()
    for _c in _ours:
        for _f in ('retracts_seq', 'deprecates_seq', 'obsoletes_seq',
                   'reclaims_seq'):
            _t = _c.get(_f)
            if isinstance(_t, int):
                _dead.add(_t)
    _by_seq = {int(_c.get('seq', 0)): _c for _c in _ours}
    for _i in intents:
        _fn = str(_i.get('filename') or '')
        _old_seq = int(_i.get('old_seq', 0))
        _old = _by_seq.get(_old_seq)
        if _old is None:
            _skipped.append(
                f"{_fn}: back-referenced claim seq={_old_seq} not in "
                "our remote jsonl")
            continue
        if str(_old.get('filename') or '') != _fn:
            _skipped.append(
                f"{_fn}: claim seq={_old_seq} names "
                f"{_old.get('filename')!r} — stale intent")
            continue
        if _old.get('claim_state') != _schema.CLAIM_STATE_PUBLISHED:
            _skipped.append(
                f"{_fn}: claim seq={_old_seq} is "
                f"{_old.get('claim_state')!r} — only a live published "
                "claim can be reclaimed")
            continue
        if _old_seq in _dead:
            _skipped.append(
                f"{_fn}: claim seq={_old_seq} already superseded on "
                "remote")
            continue
        _remote_sha = str(_old.get('sha256') or '')
        if _remote_sha != str(_i.get('remote_sha') or ''):
            _skipped.append(
                f"{_fn}: remote sha drifted since listing "
                f"(claim {_remote_sha[:12]} vs confirmed "
                f"{str(_i.get('remote_sha') or '')[:12]}) — re-run "
                "`mirror reclaim`")
            continue
        _local_sha = str(_i.get('local_sha') or '')
        if not _local_sha or _local_sha == _remote_sha:
            _skipped.append(f"{_fn}: already in sync — nothing to "
                            "reclaim")
            continue
        _ok.append(_schema.new_reclaim(
            builder=builder_id,
            seq=0,  # caller assigns before signing
            package=str(_i.get('package') or _old.get('package') or ''),
            intended_version=str(
                _i.get('intended_version')
                or _old.get('intended_version') or ''),
            built_version=str(
                _i.get('built_version')
                or _old.get('built_version') or ''),
            filename=_fn,
            sha256=_local_sha,
            size=int(_i.get('size', 0)),
            snapshot=snapshot_pin,
            built_at=_utc_now(),
            reclaims_seq=_old_seq,
            component=str(
                _i.get('component') or _old.get('component') or 'main'),
        ))
    return _ok, _skipped


def filter_pending_by_ownership(
    builder_id: str,
    candidates: List[dict],
    existing_owners: Dict[str, dict],
) -> Tuple[List[dict], List[dict]]:
    """Apply the publish-time ownership decision
    matrix to each candidate claim.

    Returns ``(kept_pending, blocked)`` where ``kept_pending`` is the
    list of candidates we're allowed to publish (caller signs + appends
    + uploads), and ``blocked`` is a list of finding dicts
    ``{filename, owner, our_version, owner_version, reason}`` for the
    candidates we cannot publish under current ownership rules.

    Decision matrix (per filename):
      - No existing owner record         → KEEP (we become owner)
      - Existing owner, different sha256  → BLOCK (`frozen_bytes_blocked`,
                                            STA-47) unless a reclaim
      - Owner is us                      → KEEP (re-claim is no-op)
      - Tunneled (no owner)              → KEEP (we take ownership)
      - Owner is other, our_ver > theirs → KEEP (ownership transfers)
      - Owner is other, our_ver <= theirs → BLOCK (`ownership_blocked`)

    Version compare via apt_pkg.version_compare so epoch/NMU suffixes
    sort correctly (same convention as `dep_drift` and elsewhere).
    """
    import apt_pkg
    _kept: List[dict] = []
    _blocked: List[dict] = []
    for _c in candidates:
        _fn = _c.get('filename')
        if not isinstance(_fn, str):
            continue
        _owner = existing_owners.get(_fn)
        if _owner is None:
            _kept.append(_c)
            continue
        # an existing owner record means the remote pool already
        # holds bytes under this filename (frozen).  If our rebuilt bytes
        # differ and this isn't a sanctioned reclaim, we must NOT publish:
        # Step 5b's push_single_deb uses --ignore-existing for non-reclaim
        # files, so the remote keeps the OLD bytes while our new claim
        # would pin a sha the pool doesn't serve → claim_apt_sha_mismatch
        # CRITICAL + delete-on-mismatch in every peer's pull.  (Reclaims
        # are injected AFTER this filter and carry reclaims_seq; the guard
        # is belt-and-suspenders for them.)  Frozen-filename invariant:
        # same filename = same bytes; a deliberate version-less change
        # goes through `mirror reclaim`, a content change bumps the version.
        _our_sha = str(_c.get('sha256') or '')
        _their_sha = str(_owner.get('sha256') or '')
        _is_reclaim = isinstance(_c.get('reclaims_seq'), int)
        if (not _is_reclaim and _our_sha and _their_sha
                and _our_sha != _their_sha):
            _blocked.append({
                'filename':       _fn,
                'owner':          _owner.get('builder'),
                'our_version':    str(_c.get('built_version') or ''),
                'owner_version':  str(_owner.get('version') or ''),
                'reason':         (
                    f'bytes differ from the frozen pool copy '
                    f'(sha {_our_sha[:12]} != {_their_sha[:12]}) — use '
                    f'`mirror reclaim` for a deliberate version-less '
                    f'change, or bump the version for a content change'),
            })
            continue
        _owner_builder = _owner.get('builder')
        if _owner_builder is None:
            # Tunneled — anyone can take ownership by republishing.
            _kept.append(_c)
            continue
        if _owner_builder == builder_id:
            # Already ours — keep (re-claim is harmless; the
            # _known dedup in generate_pending_claims usually skips
            # this, but cover the case for callers that don't dedup
            # by filename).
            _kept.append(_c)
            continue
        # Owner is another builder.  Version-compare.
        _ours = str(_c.get('built_version') or '')
        _theirs = str(_owner.get('version') or '')
        try:
            _cmp = apt_pkg.version_compare(_ours, _theirs)
        except (SystemError, TypeError):
            _cmp = 0
        if _cmp > 0:
            _kept.append(_c)
            continue
        _blocked.append({
            'filename':       _fn,
            'owner':          _owner_builder,
            'our_version':    _ours,
            'owner_version':  _theirs,
            'reason':         ('owner is another builder and our '
                               'version is not strictly higher'),
        })
    return _kept, _blocked


def fill_sizes_from_pool(claims: List[dict], pool_index: dict) -> None:
    """In-place: populate `size` for each claim by stat'ing the pool
    file.  Missing files get size=0; reconcile.audit_local catches
    those as orphan warnings."""
    for _c in claims:
        _fn = _c.get('filename')
        if not isinstance(_fn, str):
            continue
        _path = pool_index.get(_fn)
        if _path is None:
            continue
        try:
            _c['size'] = os.path.getsize(_path)
        except OSError:
            _c['size'] = 0


# ───────────────────────── local publish ─────────────────────────


def local_publish(
    *,
    builder_id: str,
    config,
    private_key_path: str,
    public_key_path: str,
    snapshot_pin: str,
    read_build_record: Callable,
) -> Tuple[int, int]:
    """Single-host publish: sign + append every pending claim to the
    local jsonl, mark them published immediately (no remote round
    trip).  Returns (created, skipped).

    Used for offline development / first-builder bootstrap.  Does NOT
    update or write coord-head — that's a remote_publish concern.

    Pre-flight: if PUBLISH_HALT is set, refuse (operator must
    `coord conflict resolve` first).
    """
    _halt = _reconcile.publish_halt_reason(config.dir_coord)
    if _halt is not None:
        logger.error(
            f"coord.publish.local_publish: PUBLISH_HALT set ({_halt}); "
            f"refusing to publish")
        return (0, 0)
    _buildlog = os.path.join(config.dir_log, 'build')
    _pool = _reconcile.scan_pool_files(config.dir_repo)
    _pending = generate_pending_claims(
        builder_id=builder_id,
        buildlog_dir=_buildlog,
        claims_dir=config.dir_coord_claims,
        public_key_path=public_key_path,
        snapshot_pin=snapshot_pin,
        read_build_record=read_build_record,
        build_arch=getattr(config, 'arch', None),
    )
    fill_sizes_from_pool(_pending, _pool)
    if not _pending:
        return (0, 0)
    _created = 0
    _skipped = 0
    _seq = _store.max_seq(config.dir_coord_claims, builder_id)
    for _claim in _pending:
        # commit the seq counter only on a successful append —
        # advancing before the try left a permanent sidecar_seq_gap on a
        # transient failure.
        _candidate_seq = _seq + 1
        _claim['seq'] = _candidate_seq
        # local_publish flips straight to published — no remote handoff
        _claim['claim_state'] = _schema.CLAIM_STATE_PUBLISHED
        try:
            _store.append_claim(
                config.dir_coord_claims, builder_id, _claim,
                private_key_path,
            )
            _seq = _candidate_seq
            _created += 1
        except (OSError, ValueError) as _e:
            logger.warning(
                f"coord.publish.local_publish: append failed for "
                f"{_claim.get('package')}/{_claim.get('filename')}: {_e}")
            _skipped += 1
    return (_created, _skipped)


# ───────────────────────── remote publish ─────────────────────────


def _read_inrelease_sha256_and_date(inrelease_path: str) -> Tuple[str, str]:
    """Return (sha256_hex, 'Date: ...' value) of a local InRelease
    file copy.  Empty strings on missing/unreadable.  Caller pulls
    this BEFORE running publish so the new coord-head pins the
    current apt-side metadata.
    """
    import hashlib
    _sha = ''
    _date = ''
    try:
        with open(inrelease_path, 'rb') as _fh:
            _bytes = _fh.read()
        _sha = hashlib.sha256(_bytes).hexdigest()
        for _line in _bytes.decode('utf-8', errors='replace').splitlines():
            if _line.startswith('Date:'):
                _date = _line.split(':', 1)[1].strip()
                break
    except OSError:
        pass
    return _sha, _date


def unsanctioned_local_ahead(ahead_rows: 'List[dict]',
                             reclaim_intents: 'List[dict]') -> 'List[dict]':
    """Publish-hazard gate.  Of the local-ahead candidates (our own
    already-published files whose on-disk bytes diverged at the SAME version),
    return those NOT covered by an explicit reclaim intent.

    A non-empty result is the hazard: `generate_pending_claims` emits 0 claims
    for an already-claimed filename (frozen-bytes invariant), but the dist-tree
    rsync pushes the whole repo — so a same-version rebuild's new bytes + index
    would land on the remote while the signed claim stays at the old sha,
    leaving claim_apt_sha_mismatch.  Caller aborts and points the operator at
    `mirror reclaim` (sanctioned version-less overwrite) or a version bump."""
    _reclaimed = {_i.get('filename') for _i in reclaim_intents}
    return [_a for _a in ahead_rows if _a.get('filename') not in _reclaimed]


def snapshot_divergence_note(local_pin: str, remote_current: str) -> 'Optional[str]':
    """Operator note when a publish's local snapshot pin diverges from the
    mirror's current pin.  Returns an ADVANCE note (local ahead → other
    builders' next pull moves forward), a behind-WARNING (mirror ahead →
    pull recommended; owned packages may still publish above the base), or
    None when the pins match or either is unknown."""
    if not (local_pin and remote_current) or local_pin == remote_current:
        return None
    if local_pin > remote_current:
        return (f"snapshot: this publish ADVANCES the mirror pin "
                f"{remote_current} -> {local_pin}; every other builder's "
                "next `mirror pull` will move its pin forward to it")
    return (f"WARNING: mirror snapshot {remote_current} is AHEAD of your pin "
            f"{local_pin} and you have not pulled. A peer may have published "
            "newer packages; run `mirror pull` to sync. You may still "
            "publish the filenames you OWN (no two builders own one "
            "filename) as long as they are not below the mirror base -- but "
            "verify your asg uN lineage is current first to keep bump "
            "tracking monotonic.")


def remote_publish(
    *,
    builder_id: str,
    config,
    private_key_path: str,
    public_key_path: str,
    snapshot_pin: str,
    remote_coord_spec: str,
    inrelease_local_path: str,
    read_build_record: Callable,
    local_mirror_urls: 'Optional[list]' = None,
    ssh_host: 'Optional[str]' = None,
    flock_path: str = '/var/lock/repo-coord.lock',
    flock_timeout: int = 60,
    ssh_key: 'Optional[str]' = None,
    pool_remote_spec: 'Optional[str]' = None,
    on_progress: 'Optional[Callable]' = None,
    on_status: 'Optional[Callable[[str], None]]' = None,
    install_corpus: 'Optional[frozenset[str]]' = None,
    on_published: 'Optional[Callable[[set], None]]' = None,
    reclaim_intents: 'Optional[List[dict]]' = None,
    local_ahead_fn: 'Optional[Callable]' = None,
) -> Tuple[bool, str]:
    """11-step publish transaction (see module docstring).  Returns
    (ok, detail).  On failure detail explains the step that aborted.

    `reclaim_intents` — operator-resolved
    `mirror.local_ahead_candidates` rows from `mirror reclaim`; each is
    re-validated against the just-fetched remote view
    (validate_reclaim_intents) and, when still sound, rides the normal
    pending path: file pushed in 5b (overwriting the remote bytes —
    the sanctioned invariant exception), claim signed published in 6.

    `inrelease_local_path` MUST be a local copy of the same InRelease
    that's published at the remote — typically the InRelease produced
    by `repo index` and pushed as part of this same `mirror publish`
    transaction.  Used to compute the sha256 that the new coord-head
    pins.

    `local_mirror_urls` — accepts either the v2 `list[str]` of canonical
    federation URLs OR the v3 `list[dict]` per-peer records
    (`{url, public_url, public_proto}` from
    `mirror.all_mirror_neighbour_records`).  When `None`, federation
    gate is BYPASSED (legacy path).  When provided:
      - if remote already has a coord-head → compare neighbour URLs
        (`canonicalize_neighbours` projection); BLOCK on diff
      - if remote has no coord-head (first publish) → BOOTSTRAP:
        upload our pubkey to <root>/keyring/builders/<id>.pub and
        initialise the new coord-head with the local records (v3 form
        is preserved verbatim so heterogeneous-proto peers round-trip;
        v2 strings auto-promote to records with empty meta)

    `pool_remote_spec`: when set, the rsync target
    for the apt POOL root (sibling of the coord root on the mirror
    host).  For each pending claim whose .deb is on the local pool,
    push that .deb per-file BEFORE writing its claim.  Files that fail
    to push are dropped from the claim set — partial publishes converge
    on retry.  `cmd_mirror_publish` always wires this; the `None` path
    survives only for unit-test fakes that exercise the sidecar layer
    in isolation.

    `on_progress` callback: invoked once per .deb
    push attempt as `on_progress(current, total, filename, ok)`.  Caller
    drives a tui.ProgressBar from this.  Optional; no-op when omitted.

    `ssh_host` is the host portion of the remote spec; defaulted from
    `remote_coord_spec` if not given (parses the `user@host:/path`
    form).  Required for flock acquisition.
    """
    def _status(msg: str) -> None:
        """Surface a step transition to the operator via on_status (when
        wired by cmd_mirror_publish).  No-op when omitted (tests).
        Also tee'd to the logger so the log tab captures the same trail."""
        if on_status is not None:
            on_status(msg)
        logger.info(f"coord.publish: {msg}")

    # Step 0 — pre-flight: PUBLISH_HALT check
    _halt = _reconcile.publish_halt_reason(config.dir_coord)
    if _halt is not None:
        return False, f"PUBLISH_HALT set: {_halt}"

    if ssh_host is None and remote_coord_spec:
        if ':' in remote_coord_spec and '@' in remote_coord_spec.split(':', 1)[0]:
            ssh_host = remote_coord_spec.split(':', 1)[0]
        # local-fs mirrors (file://, /abs/path) have
        # no ssh_host — flock-over-ssh is N/A.  Skip the lock step and
        # proceed; local-fs mirrors are dev/single-host workflows where
        # cross-host concurrency isn't a concern.

    # Step 1 — acquire remote flock (skipped when local-fs)
    _lock_proc = None
    if ssh_host:
        _status(f"acquiring remote flock on {ssh_host} ({flock_path})")
        _lock_proc = _transport.remote_flock_acquire(
            ssh_host=ssh_host, lock_path=flock_path,
            timeout_sec=flock_timeout, ssh_key=ssh_key,
            builder_id=builder_id,
        )
        if _lock_proc is None:
            _holder = _transport.remote_flock_holder(
                ssh_host=ssh_host, lock_path=flock_path, ssh_key=ssh_key)
            _by = (f" — builder '{_holder}' is publishing" if _holder
                   else " — held by a peer, or SSH failed")
            return False, (
                f"could not acquire remote flock on {ssh_host} "
                f"({flock_path}){_by}; retry shortly")

    try:
        # Step 2 — fetch remote coord tree (under lock)
        _status("fetching remote coord tree (rsync)")
        _fetched = config.dir_coord_fetched
        _ok, _detail = _transport.pull_remote_coord(
            local_dest=_fetched, remote_spec=remote_coord_spec,
            ssh_key=ssh_key,
        )
        if not _ok:
            return False, f"pull failed: {_detail}"

        # Step 3 — verify coord-head, build keyring + claim view
        _status("verifying coord-head signature + loading peer keyring")
        import signing
        _signing_home = signing.signing_home(config)
        _head_dict = _head.read_coord_head(_fetched, _signing_home)
        # read_coord_head returns None for BOTH "no head on the
        # remote" (legit first-publish bootstrap) AND "head present but GPG
        # verify FAILED / sig missing / homedir broken" (tamper or local
        # signing breakage).  Treating the latter as bootstrap would skip
        # the federation gate AND rebuild the head from our local view,
        # silently dropping the remote's revoked_builders + every peer's
        # last_seqs — exactly the authority rollback the signed head exists
        # to defeat.  Distinguish by the file's presence: if the head file
        # is on disk but didn't verify, REFUSE.
        if _head_dict is None and os.path.isfile(
                _head.coord_head_path(_fetched)):
            return False, (
                "coord-head present on the remote but FAILED to verify "
                "(bad signature, missing .sig, or unreadable tier-1 "
                "signing homedir) — refusing to publish (would overwrite "
                "the federation's signed authority with a fresh local "
                "view).  Check the tier-1 key / homedir, or investigate "
                "tampering, then retry.")
        _keyring_dir = os.path.join(_fetched, 'keyring', 'builders')
        _keyring = _identity.load_keyring(_keyring_dir)
        _revoked = (_head_dict or {}).get('revoked_builders') or {}
        _by_builder = _store.read_all_claims(
            os.path.join(_fetched, 'claims'), _keyring, _revoked)
        _status(
            f"loaded {len(_keyring)} peer pubkey(s), "
            f"{sum(len(v) for v in _by_builder.values())} prior claim(s) "
            f"across {len(_by_builder)} builder(s)")

        # Snapshot divergence vs the mirror (informational; the hard floor is
        # the snapshot.current < mirror.base refusal in the command layer).
        _rsnap = (_head_dict or {}).get('snapshot') or {}
        _div = snapshot_divergence_note(
            snapshot_pin, str(_rsnap.get('current') or ''))
        if _div:
            _status(_div)

        # Step 3a — STA-30(a): stale-local-jsonl guard.  Step 6 assigns new
        # seqs from max_seq(our LOCAL jsonl) and Step 7 wholesale-replaces
        # the remote jsonl with it.  If our working dir was wiped/restored
        # (same BUILDER_ID, empty/short local file) max_seq is BEHIND the
        # remote → we'd re-seq from a low number and the push would ERASE
        # the remote's claim history (and its retraction/obsolescence/
        # reclaim lineage).  The signed coord-head documents this exact
        # defense ("stale JSONL is detected by reading a max(seq) lower
        # than last_seqs") but nothing enforced it.  Refuse when our local
        # max is below what the remote already knows about us — checked
        # BEFORE any byte/claim is pushed.
        _our_remote_claims = _by_builder.get(builder_id, [])
        _remote_self_max = max(
            [int(_c.get('seq', 0)) for _c in _our_remote_claims
             if isinstance(_c.get('seq'), int)] or [0])
        _head_last_seq = int(
            ((_head_dict or {}).get('last_seqs') or {}).get(builder_id, 0) or 0)
        _remote_known_max = max(_remote_self_max, _head_last_seq)
        _local_max = _store.max_seq(config.dir_coord_claims, builder_id)
        if _local_max < _remote_known_max:
            return False, (
                f"stale local claims ledger: local max seq {_local_max} is "
                f"behind the remote's record of us ({_remote_known_max}) — "
                f"publishing would re-seq from {_local_max + 1} and "
                f"OVERWRITE the remote's append-only history for "
                f"{builder_id!r}.  Restore the local "
                f"{builder_id}.jsonl from the remote (it was just fetched "
                f"to {os.path.join(_fetched, 'claims')}) before publishing.")

        # Step 3b — MIRROR-01 Phase 3: federation gate.
        # If remote head exists, its neighbours must match local config's
        # mirror URL set.  Diff → BLOCK, surface findings, release flock.
        _is_bootstrap = (_head_dict is None)
        if local_mirror_urls is not None and not _is_bootstrap:
            _fed_findings = _reconcile.check_federation_consistency(
                local_mirror_urls, _head_dict)
            _fed_crit = [
                _f for _f in _fed_findings if _f.severity == 'CRITICAL']
            if _fed_crit:
                _msgs = '; '.join(_f.message for _f in _fed_crit)
                return False, (
                    f"federation gate BLOCK: {_msgs}.  Run "
                    "`mirror reconcile-neighbours` to align peers, "
                    "then retry publish.")

        # Step 3c — MIRROR-01 Phase 3: first-publish bootstrap.
        # When the remote has no coord-head yet (a freshly-prepared mirror
        # endpoint), upload our builder pubkey to <root>/keyring/builders/
        # so subsequent verify steps on this and every other peer can
        # validate our claims.  Subsequent steps then write the new
        # coord-head with neighbours = local_mirror_urls.
        if _is_bootstrap and local_mirror_urls is not None:
            _status(
                f"first-publish bootstrap: uploading our pubkey → "
                f"keyring/builders/{builder_id}.pub")
            _remote_pub = (
                remote_coord_spec.rstrip('/')
                + f'/keyring/builders/{builder_id}.pub')
            _ok_pub, _detail_pub = _transport.push_jsonl(
                local_path=public_key_path,
                remote_spec=_remote_pub,
                ssh_key=ssh_key,
            )
            if not _ok_pub:
                return False, (
                    f"first-publish: pubkey upload failed: {_detail_pub}")

        # Step 4 — hash conflict detection.
        # TOCTOU guarantee: the flock (Step 1) is held continuously through
        # the fetch (Step 2), this conflict scan, the closure gate below, and
        # the push (Step 5b+).  `_by_builder` is the freshly-fetched-under-
        # lock claim view, so a publish that LANDED on the mirror between an
        # operator's earlier `mirror audit` and now is seen here and re-
        # validated — and any concurrent publish is blocked on this lock.
        _status("hash-conflict scan across all builders")
        _conf = _reconcile.detect_hash_conflicts(_by_builder)
        _crit = [_f for _f in _conf if _f.severity == 'CRITICAL']
        if _crit:
            _reconcile.write_publish_halt(
                config.dir_coord,
                f"hash conflict during publish: {_crit[0].message}")
            return False, (
                f"hash conflict detected; PUBLISH_HALT written; "
                f"first conflict: {_crit[0].message}")

        # Step 5 — generate pending claims from this builder's
        # build.json that aren't already in the remote view
        _buildlog = os.path.join(config.dir_log, 'build')
        _pool = _reconcile.scan_pool_files(config.dir_repo)
        _remote_self_claims = _by_builder.get(builder_id, [])
        _remote_known = {
            _c.get('filename') for _c in _remote_self_claims
            if isinstance(_c.get('filename'), str)
            and _c.get('claim_state') not in _schema.INACTIVE_CLAIM_STATES
        }
        _status("generating pending claims from build records")
        _pending = generate_pending_claims(
            builder_id=builder_id,
            buildlog_dir=_buildlog,
            claims_dir=config.dir_coord_claims,
            public_key_path=public_key_path,
            snapshot_pin=snapshot_pin,
            read_build_record=read_build_record,
            build_arch=getattr(config, 'arch', None),
        )
        _pending_total = len(_pending)
        _pending = [_p for _p in _pending if _p['filename'] not in _remote_known]
        # Ownership decision matrix.  Build the cross-builder owners
        # projection and filter our candidates.  `ownership_blocked`
        # findings surface in the publish detail so the operator sees
        # exactly which packages were refused and why; the rest of the
        # publish proceeds (partial-success is the right shape per
        # the design).
        _owners = _store.project_owners(_by_builder)
        _pending, _ownership_blocked = filter_pending_by_ownership(
            builder_id, _pending, _owners)
        if _ownership_blocked:
            for _b in _ownership_blocked:
                logger.warning(
                    f"publish blocked: {_b['filename']} "
                    f"(owner {_b['owner']!r} at {_b['owner_version']!r}, "
                    f"ours {_b['our_version']!r}) — {_b['reason']}")
        # inject operator-resolved reclaim intents AFTER
        # the _remote_known filter (their filenames are by definition
        # already on the remote) and AFTER the ownership filter (the
        # same-filename owner is us).  Each intent is re-validated
        # against the JUST-FETCHED remote view: the back-referenced
        # claim must still exist, be ours, be the filename's live
        # post-fold assertion, and carry the remote_sha the operator
        # confirmed — any drift between listing and execution skips
        # that intent loudly rather than superseding the wrong claim.
        _reclaims_ok, _reclaims_skipped = validate_reclaim_intents(
            builder_id=builder_id,
            by_builder=_by_builder,
            intents=reclaim_intents or [],
            snapshot_pin=snapshot_pin,
        )
        for _why in _reclaims_skipped:
            logger.warning(f"reclaim intent skipped: {_why}")
            _status(f"reclaim intent SKIPPED: {_why}")
        _pending.extend(_reclaims_ok)
        # publish-hazard gate — refuse to silently overwrite frozen
        # published bytes.  The dist-tree rsync below pushes the whole repo, so
        # an already-published file rebuilt at the SAME version (new bytes, no
        # new claim) would land on the remote while its signed claim stayed at
        # the old sha → claim_apt_sha_mismatch.  Abort unless the divergence is
        # a sanctioned reclaim (those filenames are exempt).
        if local_ahead_fn is not None:
            _bad = unsanctioned_local_ahead(
                local_ahead_fn(_by_builder, builder_id,
                               config.dir_repo, _buildlog),
                reclaim_intents or [])
            if _bad:
                _names = ', '.join(sorted(_b['filename'] for _b in _bad)[:5])
                _more = '' if len(_bad) <= 5 else f" (+{len(_bad) - 5} more)"
                return False, (
                    f"refusing to publish: {len(_bad)} already-published "
                    f"file(s) changed on disk at the same version "
                    f"({_names}{_more}); pushing would overwrite frozen remote "
                    f"bytes and strand the signed claim. Run `mirror reclaim "
                    f"<source|file>` for a deliberate version-less change, or "
                    f"bump the version and rebuild.")
        _status(
            f"pending claims: {len(_pending)} to publish "
            f"({_pending_total - len(_pending) + len(_reclaims_ok) - len(_ownership_blocked)} "
            f"already on remote, {len(_ownership_blocked)} ownership-blocked"
            + (f", {len(_reclaims_ok)} reclaim(s)" if _reclaims_ok else '')
            + ")")
        # post-publish installability gate.
        # Walk audit_dep_closure over the local repo state (which, after a
        # `mirror pull`, is the MERGED mirror+local pool) constrained to the
        # install corpus.  Any unresolved hard Depends → BLOCK the publish.
        #
        # Responsibility split across modes (both run this gate):
        #   - distribution publisher: install_corpus is the FULL resolved
        #     closure (dep_tree.selected_pkgs ∪ udeb), so this is a COMPLETE
        #     repo-closure check — the authoritative gate.
        #   - build-mode peer: install_corpus is the SUBSET it built
        #     (build_pkg.list), so this is a local sanity check that OUR
        #     packages resolve against the merged pool.  A peer can't compute
        #     the full closure (it never parses the full pkg.list — that's
        #     the point of build mode), and the owner's corpus would be stale
        #     vs the peer's advanced snapshot; complete federation closure is
        #     therefore the distribution publisher's `mirror audit` on
        #     sync-back, not the peer's publish.
        #
        # Tolerant of test doubles without a full BuildConfig: if
        # scan_repo_state raises (missing attrs), the gate is skipped with a
        # logged warning — production callers always have a real config.
        if _pending and install_corpus is None:
            logger.warning(
                "closure gate: install_corpus not provided; skipping "
                "installability check (caller is likely a test harness "
                "— production cmd_mirror_publish always wires the "
                "dep_tree's selected_pkgs union)")
            _breaks = []
        elif _pending:
            try:
                import mirror as _mirror_mod
                import repo_audit as _repo_audit
                _local_state = _repo_audit.scan_repo_state(config)
                # consumer_set = the INSTALL CORPUS (binary names from
                # dep_tree.selected_pkgs ∪ udeb_dep_tree.selected_pkgs)
                # — exactly what `repo_audit`'s own dep gate uses.
                # `audit_dep_closure`'s docstring is explicit:
                #
                #     "Anything OUTSIDE this set is a side artifact of
                #     dpkg-buildpackage (libfoo-dev / libfoo-doc /
                #     libfoo-tests / libfoo-dbgsym from a libfoo source
                #     we built but never selected for install).  Their
                #     hard deps are NOT install-correctness concerns."
                #
                # Deriving the set from pending claims' filenames (the
                # previous attempt) over-counted: every -dev / -doc /
                # -dbgsym artifact emitted by a build got walked, and
                # their build-tool Depends (debhelper, python3-notify2,
                # python3-psutil, ...) fired as false-positive breaks.
                # `repo audit` correctly excludes those because it
                # constrains to the install corpus.  Caught 2026-06-05
                # the second time around — first try (source-names)
                # under-walked the meta surface; this version walks
                # exactly what apt at install time will resolve.
                assert install_corpus is not None  # narrowed by outer guard
                _breaks = _mirror_mod.find_publish_closure_breaks(
                    _local_state, _by_builder, install_corpus)
            except (AttributeError, OSError) as _e:
                logger.warning(
                    f"closure gate: skipped ({_e!r}); proceeding "
                    "without installability check (likely test harness)")
                _breaks = []
        else:
            _breaks = []
        # act on _breaks AFTER the if/elif/else chain.  This block
        # was previously nested inside the `else:` above (the no-pending
        # branch, where _breaks is unconditionally []), so the refusal was
        # unreachable — the `elif _pending:` branch computed the breaks and
        # then silently discarded them, and a publish that would leave the
        # mirror non-installable proceeded.  The installability invariant
        # is not negotiable (docstring above); enforce it here.
        if _breaks:
            _detail_lines = [
                f"{_pkg}: {_field} {_rel} ({_why})"
                for _pkg, _field, _rel, _why in _breaks[:5]
            ]
            _summary = "; ".join(_detail_lines)
            if len(_breaks) > 5:
                _summary += f" (+ {len(_breaks) - 5} more)"
            return False, (
                f"mirror_closure_break: {_summary} — refusing "
                "to publish (would leave the mirror in a "
                "non-installable state)")
        fill_sizes_from_pool(_pending, _pool)

        # Step 5b — MIRROR-01 Phase 3b: per-file .deb push.  For each
        # pending claim, rsync the .deb from local pool → remote pool
        # (sibling tree of the coord root).  A push failure drops the
        # claim from the publish set (partial = converge on retry; we
        # never claim a file we didn't successfully ship).
        # `--ignore-existing` (in push_single_deb) makes the re-publish
        # cheap: unchanged files skip transfer entirely.
        _pushed_count = 0
        _push_fail_count = 0
        if pool_remote_spec is not None and _pending:
            _status(
                f"pushing {len(_pending)} .deb(s) to remote pool "
                f"(progress bar follows)")
        if pool_remote_spec is not None:
            _total_to_push = len(_pending)
            _kept_pending: list = []
            for _i, _claim in enumerate(_pending, start=1):
                _fn = _claim.get('filename')
                _local_path = _pool.get(_fn) if isinstance(_fn, str) else None
                if _local_path is None:
                    # File listed in build.json's outputs but absent from
                    # the local pool — almost certainly an audit gap; we
                    # can't ship what we don't have.
                    if on_progress is not None:
                        on_progress(_i, _total_to_push, _fn or '?', False)
                    _push_fail_count += 1
                    continue
                _rel = os.path.relpath(_local_path, config.dir_repo)
                _remote_file = (
                    pool_remote_spec.rstrip('/') + '/' + _rel)
                _ok_push, _detail_push = _transport.push_single_deb(
                    local_path=_local_path, remote_spec=_remote_file,
                    ssh_key=ssh_key,
                    # a reclaim's remote file exists with the
                    # OLD bytes — --ignore-existing would silently skip
                    # the transfer and publish a claim whose bytes never
                    # shipped.  Overwrite for reclaim claims only.
                    overwrite=isinstance(_claim.get('reclaims_seq'), int),
                )
                if on_progress is not None:
                    on_progress(_i, _total_to_push, _fn, _ok_push)
                if _ok_push:
                    _kept_pending.append(_claim)
                    _pushed_count += 1
                else:
                    logger.error(
                        f"coord.publish: push {_fn} failed: {_detail_push}")
                    _push_fail_count += 1
            _pending = _kept_pending
            if _total_to_push:
                _status(
                    f"pool push: {_pushed_count} sent, "
                    f"{_push_fail_count} failed")

            # Step 5b.2 — POOL COMPLETENESS guard.  Step 5b pushed only the
            # new-claim delta; the dist tree pushed next (5c) indexes EVERY .deb
            # in the local repo.  A prior per-file push that failed DROPS its
            # claim (above) but leaves the .deb in the local index — so the
            # published index promises files that 404 (observed: 329 missing on
            # the mirror after push gaps over a slow link).  List the remote
            # pool once, diff against the local repo, and push anything missing
            # (we own it — it's in our index but never landed).
            # --ignore-existing keeps a complete pool a cheap no-op.
            if pool_remote_spec is not None:
                _remote_debs = _transport.list_remote_debs(
                    pool_remote_spec, ssh_key)
                if _remote_debs is None:
                    _status("warning: could not list remote pool — "
                            "skipping completeness check")
                else:
                    _local_debs: dict = {}
                    for _wr, _, _wfs in os.walk(config.dir_repo):
                        for _wf in _wfs:
                            if _wf.endswith(('.deb', '.udeb')):
                                _wp = os.path.join(_wr, _wf)
                                _local_debs[os.path.relpath(
                                    _wp, config.dir_repo)] = _wp
                    _missing = sorted(set(_local_debs) - _remote_debs)
                    if _missing:
                        _status(f"pool completeness: {len(_missing)} .deb(s) "
                                "indexed but absent on the mirror — pushing")
                        _comp_fail = 0
                        for _ci, _crel in enumerate(_missing, start=1):
                            _ok_c, _det_c = _transport.push_single_deb(
                                local_path=_local_debs[_crel],
                                remote_spec=(pool_remote_spec.rstrip('/')
                                             + '/' + _crel),
                                ssh_key=ssh_key, overwrite=False)
                            if on_progress is not None:
                                on_progress(_ci, len(_missing), _crel, _ok_c)
                            if not _ok_c:
                                _comp_fail += 1
                                logger.error(
                                    f"coord.publish: completeness push "
                                    f"{_crel} failed: {_det_c}")
                        _status(
                            f"pool completeness: {len(_missing) - _comp_fail} "
                            f"sent, {_comp_fail} failed")
                        if _comp_fail:
                            return False, (
                                f"pool completeness: {_comp_fail} .deb(s) "
                                "still missing on the mirror after push — "
                                "refusing to publish an index that 404s")

            # Step 5c — push the apt-trust path (dists/<codename>/ tree:
            # InRelease, Release, Release.gpg, per-component Packages +
            # variants).  Without this the remote pool is unusable by
            # apt clients even though every .deb is present.  Order
            # matters: pool first (already done above), then dist tree
            # — never the other way around, or apt could fetch an
            # InRelease pointing at a .deb not yet on remote.
            try:
                _codename = str(config.build_codename).strip('"').strip("'")
            except AttributeError:
                _codename = ''
            if _codename and pool_remote_spec is not None:
                _local_dist = os.path.join(
                    config.dir_repo, 'dists', _codename)
                _remote_dist = (
                    pool_remote_spec.rstrip('/')
                    + f'/dists/{_codename}')
                _status(f"pushing dist tree dists/{_codename}/")
                _ok_dt, _dt_detail = _transport.push_dist_tree(
                    local_dist_dir=_local_dist,
                    remote_dir_spec=_remote_dist,
                    ssh_key=ssh_key,
                )
                if not _ok_dt:
                    return False, f"push dist tree failed: {_dt_detail}"

        # Step 6 — sign + append every pending claim to the LOCAL jsonl
        # (state=published; the .deb is now on the remote pool because
        # step 5b just pushed it under the same transaction)
        _seq = _store.max_seq(config.dir_coord_claims, builder_id)
        _appended = 0
        for _claim in _pending:
            # commit the seq counter ONLY on a successful append.
            # The old code did `_seq += 1` before the try, so a transient
            # append failure CONSUMED a seq that never landed → a permanent
            # hole that `audit_sidecar_seq_integrity` flags as a CRITICAL
            # `sidecar_seq_gap` forever.  Assign a candidate; advance only
            # when it actually lands so seqs stay contiguous (the dropped
            # claim's number is reused by the next one).
            _candidate_seq = _seq + 1
            _claim['seq'] = _candidate_seq
            _claim['claim_state'] = _schema.CLAIM_STATE_PUBLISHED
            try:
                _store.append_claim(
                    config.dir_coord_claims, builder_id, _claim,
                    private_key_path,
                )
                _seq = _candidate_seq
                _appended += 1
            except (OSError, ValueError) as _e:
                logger.warning(
                    f"coord.publish: local append failed for "
                    f"{_claim.get('package')}: {_e}")

        _status(
            f"signed + appended {_appended} claim(s) to local jsonl "
            f"(seq → {_seq})")

        # Step 6b — SELECT-LOCK: deprecate files we own+published but no longer
        # select.  Authority = the signed selection.state closure (NOT
        # build.json, which lingers after a drop).  Releases ownership to the
        # commons; the .deb stays in the pool for takeover.  Best-effort — a
        # missing/unreadable lockfile just skips emission (no spurious release).
        try:
            import selection_lock as _sl
            _lock, _lstatus = _sl.read_selection_state(config)
            if _lstatus == _sl.STATUS_OK and _lock is not None:
                _closure_srcs = set((_lock.get('closure') or {}).get('srcs', {}))
                _dep_claims = emit_deprecation_claims(
                    builder_id=builder_id,
                    by_builder=_by_builder,
                    closure_srcs=_closure_srcs,
                    snapshot_pin=snapshot_pin,
                    built_at=_utc_now(),
                    start_seq=_seq,
                )
                _dep_appended = 0
                for _dc in _dep_claims:
                    # re-assign the seq sequentially and commit
                    # only on success — the emit pre-assigns contiguous
                    # seqs, but `_seq = max(...)` before the append left a
                    # gap when one append failed.
                    _candidate_seq = _seq + 1
                    _dc['seq'] = _candidate_seq
                    try:
                        _store.append_claim(
                            config.dir_coord_claims, builder_id, _dc,
                            private_key_path,
                        )
                        _seq = _candidate_seq
                        _dep_appended += 1
                    except (OSError, ValueError) as _e:
                        logger.warning(
                            "coord.publish: deprecation append failed for "
                            f"{_dc.get('filename')}: {_e}")
                if _dep_appended:
                    _status(
                        f"deprecated {_dep_appended} owned file(s) no longer "
                        "in the selection (ownership released)")
        except Exception as _e:   # never let deprecation break a publish
            logger.warning(f"coord.publish: deprecation emission skipped: {_e}")

        # Step 6c — LEDGER-01: mark OUR superseded old-version files
        # obsolete.  The grouping must see the claims just appended in
        # Step 6 (the NEW versions), so merge _pending into our slice of
        # the fetched view.  Ownership retained; the old .deb stays in the
        # append-only pool as a labeled prune candidate.  Best-effort.
        try:
            _view = dict(_by_builder)
            _view[builder_id] = list(_view.get(builder_id, [])) + list(_pending)
            _obs_claims = emit_obsolescence_claims(
                builder_id=builder_id,
                by_builder=_view,
                snapshot_pin=snapshot_pin,
                built_at=_utc_now(),
                start_seq=_seq,
            )
            _obs_appended = 0
            for _oc in _obs_claims:
                # re-assign sequentially, commit only on success.
                _candidate_seq = _seq + 1
                _oc['seq'] = _candidate_seq
                try:
                    _store.append_claim(
                        config.dir_coord_claims, builder_id, _oc,
                        private_key_path,
                    )
                    _seq = _candidate_seq
                    _obs_appended += 1
                except (OSError, ValueError) as _e:
                    logger.warning(
                        "coord.publish: obsolescence append failed for "
                        f"{_oc.get('filename')}: {_e}")
            if _obs_appended:
                _status(
                    f"obsoleted {_obs_appended} superseded old-version "
                    "file(s) (ownership retained; prune candidates)")
        except Exception as _e:   # never let obsolescence break a publish
            logger.warning(f"coord.publish: obsolescence emission skipped: {_e}")

        # Step 7 — push the updated jsonl to the remote
        _local_jsonl = _store.claims_path(
            config.dir_coord_claims, builder_id)
        if os.path.isfile(_local_jsonl):
            _status("pushing claim jsonl to remote")
            _remote_jsonl = (
                remote_coord_spec.rstrip('/') + f'/claims/{builder_id}.jsonl')
            _ok, _detail = _transport.push_jsonl(
                local_path=_local_jsonl,
                remote_spec=_remote_jsonl,
                ssh_key=ssh_key,
            )
            if not _ok:
                return False, f"push jsonl failed: {_detail}"

        # Step 8 — re-sign coord-head with our updated last_seq
        _ir_sha, _ir_date = _read_inrelease_sha256_and_date(
            inrelease_local_path)
        if not _ir_sha:
            return False, (
                f"InRelease at {inrelease_local_path} missing/unreadable "
                "— cannot pin coord-head")
        _last_seqs = dict((_head_dict or {}).get('last_seqs') or {})
        _last_seqs[builder_id] = _seq
        # Snapshot pin tuple — read from local state if available;
        # falls back to the snapshot_pin arg in degenerate cases (e.g.
        # snapshot.state missing, fresh init).
        try:
            import utils as _utils
            _state = _utils.read_snapshot_state(config)
        except Exception:
            _state = {}
        _ss = _schema.new_snapshot_state(
            base=str(_state.get('base') or snapshot_pin),
            current=str(_state.get('current') or snapshot_pin),
            published=str(_state.get('published') or snapshot_pin),
            external=bool(_state.get('external', True)),
        )
        # neighbours sourcing.
        #   - bootstrap (first publish, no prior head) → from local config
        #   - subsequent publishes → preserve from the fetched head
        # Publish itself never adds or removes peers in steady state;
        # `mirror add` / `mirror remove` / `mirror reconcile-neighbours`
        # is the operator-driven membership-change path.
        if _is_bootstrap and local_mirror_urls is not None:
            _neighbours = local_mirror_urls
        else:
            _neighbours = (_head_dict or {}).get('neighbours') or []
        # Canonical config (pkg.list + pool.list) propagation: the
        # distribution-mode OWNER publishes its lists into the coord tree and
        # pins their sha256 in the signed head.  A build-mode peer never owns
        # the canonical config — it preserves the fetched head's pin.
        import coord.config_manifest as _cfgman
        _config_sha = (_head_dict or {}).get('config_sha256')
        _wrote_config = False
        # Closure ledger (latest-version-per-binary across base..current) —
        # owner writes + pins it; a build-mode peer preserves the fetched
        # head's pin.  Lets peers pull the full closure off a mixed-snapshot
        # mirror without keying on per-claim snapshots.
        _ledger_sha = (_head_dict or {}).get('closure_ledger_sha256')
        _wrote_ledger = False
        if getattr(config, 'build_mode', 'distribution') != 'build':
            # Carry the FULL selection authority (4 lists + the owner's verified
            # selection.state pins/closure) so a peer builds the identical
            # distribution.  A bad/missing lock → None → lists-only manifest
            # (never abort publish over it).
            import selection_lock as _sl
            try:
                _lock, _lstatus = _sl.read_selection_state(config)
                _sel = _lock if _lstatus == _sl.STATUS_OK else None
            except Exception:                         # noqa: BLE001
                _sel = None     # robust — never abort publish over the lock
            _sha = _cfgman.write_canonical_config(
                config.dir_coord,
                getattr(config, 'pkglist_path', ''),
                getattr(config, 'poollist_path', ''),
                getattr(config, 'livelist_path', ''),
                getattr(config, 'installerlist_path', ''),
                selection_state=_sel)
            if _sha:
                _config_sha = _sha
                _wrote_config = True
            # Assemble the published ledger = the FULL published binary set
            # (latest version per pkg|arch, foreign-arch excluded), NOT just the
            # install closure — a peer adopts the complete set across
            # base..current, stays conformant, and `virtual build` sees no
            # phantom conflicts.  Must run AFTER the Step 5c repo auto-index.
            import repo_audit as _repo_audit
            try:
                _entries = _repo_audit.published_ledger_entries(config)
            except Exception:                              # noqa: BLE001
                _entries = {}     # robust — never abort publish over the ledger
            if _entries:
                _status(f"published ledger: {len(_entries)} binary(ies)")
                _ledger_doc = _schema.new_closure_ledger(
                    codename=str(getattr(config, 'build_codename', '')),
                    snapshot=str(_ss.get('current') or ''),
                    entries=_entries,
                    generated_at=_utc_now())
                _lsha = _cfgman.write_closure_ledger(
                    config.dir_coord, _ledger_doc)
                if _lsha:
                    _ledger_sha = _lsha
                    _wrote_ledger = True
        _new_head = _schema.new_coord_head(
            inrelease_sha256=_ir_sha,
            snapshot=_ss,
            last_seqs=_last_seqs,
            head_time=_utc_now(),
            neighbours=_neighbours,
            revoked_builders=(_head_dict or {}).get('revoked_builders'),
            config_sha256=_config_sha,
            closure_ledger_sha256=_ledger_sha,
        )
        _status(f"signing coord-head (last_seqs[{builder_id}]={_seq})")
        _ok = _head.write_coord_head(
            config.dir_coord, _new_head, _signing_home)
        if not _ok:
            return False, "coord-head write/sign failed"

        # Push the canonical config BEFORE the head (owner only) so a peer
        # never sees a head whose config_sha256 points at a not-yet-pushed
        # file (it would just refuse + retry, but ordering keeps it clean).
        if _wrote_config:
            _status("pushing canonical config (pkg.list/pool.list)")
            _ok_cfg, _detail_cfg = _transport.push_jsonl(
                local_path=_cfgman.manifest_path(config.dir_coord),
                remote_spec=(remote_coord_spec.rstrip('/')
                             + '/config/canonical.json'),
                ssh_key=ssh_key)
            if not _ok_cfg:
                return False, f"push canonical config failed: {_detail_cfg}"

        # Push the closure ledger BEFORE the head (owner only), same reason
        # as the canonical config — the head's closure_ledger_sha256 must not
        # point at a not-yet-pushed file.
        if _wrote_ledger:
            _status("pushing closure ledger")
            _ok_lg, _detail_lg = _transport.push_jsonl(
                local_path=_cfgman.closure_ledger_path(config.dir_coord),
                remote_spec=(remote_coord_spec.rstrip('/')
                             + '/config/closure_ledger.json'),
                ssh_key=ssh_key)
            if not _ok_lg:
                return False, f"push closure ledger failed: {_detail_lg}"

        # Step 9 — push the new coord-head to the remote
        _status("pushing coord-head to remote")
        _ok, _detail = _transport.push_coord_head(
            local_coord_dir=config.dir_coord,
            remote_dir_spec=remote_coord_spec.rstrip('/') + '/',
            ssh_key=ssh_key,
        )
        if not _ok:
            return False, f"push coord-head failed: {_detail}"
        if ssh_host:
            _status("releasing remote flock")

        _push_summary = (
            f"; pushed {_pushed_count} .deb(s)"
            + (f" ({_push_fail_count} failed)" if _push_fail_count else "")
            if pool_remote_spec is not None else "")
        # every failure path returned earlier — pool, jsonl AND
        # coord-head pushes all succeeded.  Only NOW report the published
        # package set so the caller can stamp published_at on records.
        if on_published is not None:
            try:
                on_published({str(_c.get('package') or '')
                              for _c in _pending if _c.get('package')})
            except Exception as _e:
                logger.warning(f"coord.publish: on_published callback: {_e}")
        return True, (
            f"published {_appended} claim(s){_push_summary}; "
            f"coord-head re-signed @ seq={_seq}")
    finally:
        if _lock_proc is not None:
            _transport.remote_flock_release(_lock_proc)


def _utc_now() -> str:
    """Plain UTC now — bridge to utils._utc_now_iso without a circular
    import (coord/ doesn't import utils to keep the dep direction
    clean; this helper is local)."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def revoke_builder(
    *,
    builder_id_to_revoke: str,
    config,
    remote_coord_spec: str,
    signing_homedir: str,
    ssh_host: 'Optional[str]' = None,
    flock_path: str = '/var/lock/repo-coord.lock',
    flock_timeout: int = 60,
    ssh_key: 'Optional[str]' = None,
    our_builder_id: 'Optional[str]' = None,
    on_status: 'Optional[Callable[[str], None]]' = None,
) -> 'Tuple[bool, str]':
    """Decommission a builder: add it to the coord-head's `revoked_builders`
    under the publish lock, then re-sign + push the head.  The builder's
    claims are dropped federation-wide by `read_all_claims`, so every
    filename it owned becomes no-owner and other builders take it via the
    normal ownership path.  Does NOT need the revoked builder's private key —
    only the tier-1 signing key (to re-sign the head).  Idempotent: a
    builder already revoked is reported and left unchanged.  Preserves every
    other head field (snapshot, last_seqs, neighbours, InRelease pin)."""
    def _status(msg: str) -> None:
        if on_status is not None:
            on_status(msg)

    _halt = _reconcile.publish_halt_reason(config.dir_coord)
    if _halt:
        return False, f"PUBLISH_HALT set: {_halt}"

    _lock_proc = None
    if ssh_host:
        _status(f"acquiring remote flock on {ssh_host} ({flock_path})")
        _lock_proc = _transport.remote_flock_acquire(
            ssh_host=ssh_host, lock_path=flock_path,
            timeout_sec=flock_timeout, ssh_key=ssh_key,
            builder_id=our_builder_id)
        if _lock_proc is None:
            _holder = _transport.remote_flock_holder(
                ssh_host=ssh_host, lock_path=flock_path, ssh_key=ssh_key)
            _by = (f" — builder '{_holder}' is publishing" if _holder
                   else " — held by a peer, or SSH failed")
            return False, f"could not acquire remote flock{_by}; retry shortly"
    try:
        _status("fetching remote coord tree (rsync)")
        _fetched = config.dir_coord_fetched
        _ok, _detail = _transport.pull_remote_coord(
            local_dest=_fetched, remote_spec=remote_coord_spec, ssh_key=ssh_key)
        if not _ok:
            return False, f"pull failed: {_detail}"

        _head_dict = _head.read_coord_head(_fetched, signing_homedir)
        if _head_dict is None:
            if os.path.isfile(_head.coord_head_path(_fetched)):
                return False, (
                    "coord-head present on the remote but FAILED to verify — "
                    "refusing to rewrite it")
            return False, "no coord-head on the mirror — nothing to revoke"

        _revoked = dict((_head_dict.get('revoked_builders') or {}))
        if builder_id_to_revoke in _revoked:
            return True, (
                f"builder '{builder_id_to_revoke}' already revoked "
                f"({_revoked[builder_id_to_revoke]}) — no change")
        _revoked[builder_id_to_revoke] = _utc_now()

        _new_head = _schema.new_coord_head(
            inrelease_sha256=str(_head_dict.get('inrelease_sha256') or ''),
            snapshot=_head_dict.get('snapshot') or {},
            last_seqs=dict(_head_dict.get('last_seqs') or {}),
            head_time=_utc_now(),
            neighbours=_head_dict.get('neighbours') or [],
            revoked_builders=_revoked,
            # Revocation only changes revoked_builders — preserve the prior
            # head's content pins so peers don't lose the canonical config /
            # closure ledger (and fall back unnecessarily) after a revoke.
            config_sha256=_head_dict.get('config_sha256'),
            closure_ledger_sha256=_head_dict.get('closure_ledger_sha256'),
        )
        _status(f"signing coord-head (revoking {builder_id_to_revoke})")
        if not _head.write_coord_head(
                config.dir_coord, _new_head, signing_homedir):
            return False, "coord-head write/sign failed"

        _status("pushing coord-head to remote")
        _ok, _detail = _transport.push_coord_head(
            local_coord_dir=config.dir_coord,
            remote_dir_spec=remote_coord_spec.rstrip('/') + '/',
            ssh_key=ssh_key)
        if not _ok:
            return False, f"push coord-head failed: {_detail}"
        return True, (
            f"revoked builder '{builder_id_to_revoke}'; its packages are now "
            "no-owner — peers can take them on their next publish")
    finally:
        if _lock_proc is not None:
            _transport.remote_flock_release(_lock_proc)


# ───────────────────────── conflict resolution ─────────────────────────


def retract_claim(
    *,
    builder_id: str,
    config,
    private_key_path: str,
    public_key_path: str,
    package: str,
    target_seq: 'Optional[int]' = None,
) -> Tuple[bool, str]:
    """Write a signed retraction line for one of our own claims.

    If `target_seq` is None, retract the highest live (non-retracted)
    seq for `package` in our local jsonl.  Returns (ok, detail).
    """
    _claims = _store.read_builder_claims(
        config.dir_coord_claims, builder_id, public_key_path)
    _live = [
        _c for _c in _claims
        if _c.get('claim_state') not in _schema.INACTIVE_CLAIM_STATES
        and _c.get('package') == package
    ]
    if not _live:
        return False, f"no live claim for {package!r} by {builder_id!r}"
    if target_seq is None:
        _live.sort(key=lambda _c: int(_c.get('seq', 0)))
        _target = _live[-1]
    else:
        _matching = [_c for _c in _live if int(_c.get('seq', 0)) == target_seq]
        if not _matching:
            return False, f"no claim seq={target_seq} for {package!r}"
        _target = _matching[0]
    _next_seq = _store.max_seq(config.dir_coord_claims, builder_id) + 1
    _retraction = _schema.new_retraction(
        builder=builder_id,
        seq=_next_seq,
        package=package,
        retracts_seq=int(_target.get('seq', 0)),
        filename=str(_target.get('filename', '')),
        snapshot=str(_target.get('snapshot', '')),
        built_at=_utc_now(),
    )
    try:
        _store.append_claim(
            config.dir_coord_claims, builder_id, _retraction,
            private_key_path)
    except (OSError, ValueError) as _e:
        return False, f"append retraction failed: {_e}"
    return True, (
        f"retracted {package}@seq={_target.get('seq')} "
        f"with retraction seq={_next_seq}")


def retract_claim_by_filename(
    *,
    builder_id: str,
    config,
    private_key_path: str,
    public_key_path: str,
    filename: str,
) -> Tuple[bool, str]:
    """Write a signed retraction for the live claim on a single
    ``filename`` (not a whole source).

    ``retract_claim`` is source-keyed — it withdraws the highest live
    claim for a *package*, which is wrong for withdrawing ONE foreign
    cross-toolchain binary while the source (binutils, gcc-12) stays
    selected and keeps publishing its native binaries.  This variant
    keys on the exact filename so the rest of the source is untouched.

    Idempotent: a filename with no live claim returns ``(False, …)`` and
    is safely skipped on re-run.  Returns (ok, detail)."""
    # Select the target from the FOLDED live view (project_owners drops
    # already-retracted claims) — a raw-line scan would re-find the prior
    # published line on a second pass and re-retract it forever.
    _claims = _store.read_builder_claims(
        config.dir_coord_claims, builder_id, public_key_path)
    _owned = _store.project_owners({builder_id: _claims}).get(filename)
    if _owned is None or _owned.get('builder') != builder_id:
        return False, f"no live claim for {filename!r} by {builder_id!r}"
    _target = _owned.get('claim') or {}
    _next_seq = _store.max_seq(config.dir_coord_claims, builder_id) + 1
    _retraction = _schema.new_retraction(
        builder=builder_id,
        seq=_next_seq,
        package=str(_target.get('package', '')),
        retracts_seq=int(_owned.get('seq', _target.get('seq', 0))),
        filename=filename,
        snapshot=str(_target.get('snapshot', '')),
        built_at=_utc_now(),
    )
    try:
        _store.append_claim(
            config.dir_coord_claims, builder_id, _retraction,
            private_key_path)
    except (OSError, ValueError) as _e:
        return False, f"append retraction failed: {_e}"
    return True, (
        f"retracted {filename}@seq={_target.get('seq')} "
        f"with retraction seq={_next_seq}")
