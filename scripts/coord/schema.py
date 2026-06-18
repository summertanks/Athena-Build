"""Schema — the on-the-wire data shapes.

Three records cross the host boundary:

  Claim       — one .deb's published-state line (per-builder JSONL).
                Signed by the writing builder's Ed25519 claim key.
                Future-tolerant: unknown top-level keys preserved
                through verify-and-re-emit roundtrips so an older
                builder doesn't strip fields a newer builder relies
                on (verify pins claim_sig over the original canonical
                bytes, so we don't roundtrip; the older builder just
                ignores fields it doesn't know).

  SnapshotState — the canonical {base, current, published, external}
                  pin tuple, referenced by coord-head.

  CoordHead   — the per-publish manifest pinning {InRelease-sha256,
                builder-id → last-seq, snapshot, coord-head time}.
                GPG-clearsigned with the tier-1 (repo signing) key.
                Builders refuse to accept a coord-head older than
                the InRelease they're consuming — defeats sidecar-
                rollback by a compromised repo host.

Canonical-JSON serialization: sort_keys + ensure_ascii + no whitespace.
Two semantically-identical records produce byte-identical canonical
bytes across Python versions / locales / dict insertion order.  Same
discipline as utils._canonical_record_bytes — kept consistent so the
mental model is one rule across the codebase.
"""

import json
from typing import Any, Dict, Iterable, Optional

# Pinned at v1; bump on any breaking field change.  Readers tolerate
# unknown future keys (preserve in dict; ignore semantics they don't
# know).  Removing a key is a v2.
# v1 → v2 (SELECT-LOCK): adds claim_state 'deprecated' + the optional
# `deprecates_seq` field.  Back-compat: claim_from_jsonl never compares `v`,
# so v2 PUBLISHED/PENDING/RETRACTED lines parse on a v1 reader unchanged; only
# a 'deprecated' line is rejected by a v1 reader (unknown state → None), which
# is a SAFE degrade — the v1 peer keeps treating the file as owned/published
# (stale, not corrupt) until it upgrades.
# v2 → v3 (LEDGER-01): adds claim_state 'obsolete' + the optional
# `obsoletes_seq` field — a version-superseded marker the owner stamps on
# their own older-version claims at publish (Step 6c).  Same degrade: a v2
# peer rejects only the 'obsolete' lines and keeps treating the old file as
# published — stale, not corrupt, until it upgrades.
# v3 → v4 (RECLAIM-01): adds the optional `reclaims_seq` field on a normal
# 'published' claim — the sanctioned exception to the INVARIANT that a
# published filename's bytes are frozen forever.  A reclaim is a NEW live
# claim for the SAME filename with a NEW sha256 (same-version rebuild:
# Position-X OOB normalization, disaster-recovery rebuilds — builds are not
# bit-reproducible); the claim it back-references is folded everywhere a
# retraction/deprecation/obsolescence target would be.  Emitted ONLY by the
# explicit `mirror reclaim` operator command, never by
# generate_pending_claims.  Degrade caveat: a v3 reader ACCEPTS the reclaim
# line (state 'published' is known; unknown keys preserved) but its folds
# don't know `reclaims_seq`, so it sees two live claims for one filename
# with different shas → its hash-conflict scan halts publish.  Peers must
# upgrade BEFORE the federation's first reclaim.
CLAIM_RECORD_SCHEMA_VERSION = 4
COORD_HEAD_SCHEMA_VERSION = 3
# v1 → v2 (MIRROR-01 Phase 2): adds `neighbours: list[str]` — the
# federation membership list (every mirror's coord-head carries the
# canonical URL set, signed by tier-1 GPG).  v1 readers tolerate a
# missing field as an empty list; the federation-gate at publish time
# treats empty-from-v1 as "no peers known" and uses the local config
# as the source of truth on first contact.
# v2 → v3 (MIRROR-01 Phase 7): `neighbours` becomes a list of
# per-peer records: ``[{url, public_url, public_proto}, ...]``.  Lets a
# heterogeneous federation (peer A serves https, peer B serves http;
# peers under different DNS shapes) round-trip the apt-readable URL
# per peer instead of every joiner inheriting the operator's --proto.
# Read-compat: v2 ``list[str]`` is auto-promoted to records with empty
# ``public_url`` / ``public_proto`` (which `cmd_mirror_add` falls back
# on by inheriting the operator's --proto).  Per-peer ssh keys are NOT
# in the schema today — federations assume a single operator-supplied
# ssh key for all peers.
SNAPSHOT_STATE_SCHEMA_VERSION = 1

# Lifecycle states a claim can be in.  `pending` is the post-rsync,
# pre-reindex state — the .deb is on the remote's pool but apt won't
# see it until the next reindex+sign.  `published` is the steady
# state.  `retracted` is a signed tombstone — only the owner can
# write it; references the seq of the claim being retracted.
# `deprecated` (SELECT-LOCK) — the owner published this file but the
# package is no longer in their selection (dropped via `cache select`).
# UNLIKE retracted (a tombstone that strips the file's metadata), a
# deprecated claim KEEPS the filename/sha/size so the .deb stays
# resolvable in the pool — it just RELEASES ownership: project_owners
# reports builder=None (like a tunneled claim), so any other builder may
# take the file over by republishing.  References `deprecates_seq`.
# `obsolete` (LEDGER-01) — this exact file (an OLD version) has been
# superseded by a newer version of the same binary published by the SAME
# owner.  Natural aging, not abandonment: ownership is RETAINED
# (project_owners keeps the builder, unlike deprecated), the file stays
# in the append-only pool as a LABELED prune candidate (UPD-01
# publish-before-prune), and presence/index audits skip it because the
# old file may legitimately drop out of the local repo and the apt
# index.  References `obsoletes_seq`.
CLAIM_STATE_PENDING = 'pending'
CLAIM_STATE_PUBLISHED = 'published'
CLAIM_STATE_RETRACTED = 'retracted'
CLAIM_STATE_DEPRECATED = 'deprecated'
CLAIM_STATE_OBSOLETE = 'obsolete'
CLAIM_STATES = frozenset({
    CLAIM_STATE_PENDING, CLAIM_STATE_PUBLISHED, CLAIM_STATE_RETRACTED,
    CLAIM_STATE_DEPRECATED, CLAIM_STATE_OBSOLETE,
})
# States that are NOT an active, asserted ownership of a present-in-our-index
# file.  Audit/reconcile/index sites skip these: a retracted claim's file is
# gone, and a deprecated claim's file was dropped from our local repo (its pool
# copy may linger for takeover, but we no longer ASSERT it).  project_owners is
# the deliberate exception — it surfaces a deprecated claim as no-owner.
# NOTE: 'obsolete' is deliberately NOT here — an obsolete claim still asserts
# ownership (and stays in publish `_known` sets: a superseded filename never
# re-claims, versions only move forward).
INACTIVE_CLAIM_STATES = frozenset({
    CLAIM_STATE_RETRACTED, CLAIM_STATE_DEPRECATED,
})
# States whose FILE may legitimately be absent from the local repo and the
# apt index (retracted: withdrawn; deprecated: dropped from selection;
# obsolete: old version pruned).  Presence/index audits skip these so a
# pruned old file never fires a false CRITICAL; ownership semantics stay
# governed by INACTIVE_CLAIM_STATES above.
PRESENCE_SKIP_CLAIM_STATES = frozenset({
    CLAIM_STATE_RETRACTED, CLAIM_STATE_DEPRECATED, CLAIM_STATE_OBSOLETE,
})

# The back-ref fields a superseding record carries to name the seq it
# replaces.  A retraction/deprecation/obsolescence marker, AND a reclaim
# (a LIVE published claim, not a marker — RECLAIM-01), all point at the
# prior claim's seq.  Collected unconditionally: a `*_seq` field only ever
# appears on the record that owns it, so no state-gating is needed.
SUPERSESSION_BACKREF_FIELDS = (
    'retracts_seq', 'deprecates_seq', 'obsoletes_seq', 'reclaims_seq',
)


def superseded_seqs(claims: 'Iterable[dict]') -> 'set[int]':
    """The set of claim seqs that have been SUPERSEDED — named by a
    back-ref (`SUPERSESSION_BACKREF_FIELDS`) on any claim in `claims`.

    Per-builder fold: pass ONE builder's claim list (seqs are per-builder).
    A claim whose own `seq` is in this set is no longer the live assertion
    for its filename — even when its `claim_state` is still 'published'
    (the reclaim/obsolescence case: the new record lives, the old published
    claim is folded).  This is the single canonical fold; every consumer
    that walks claims as "live" MUST apply it, or a reclaim's old/new pair
    reads as two live claims for one filename (spurious hash conflicts,
    pull sha-mismatch, double-counted ownership).
    """
    _out: 'set[int]' = set()
    for _c in claims:
        for _k in SUPERSESSION_BACKREF_FIELDS:
            _s = _c.get(_k)
            if isinstance(_s, int):
                _out.add(_s)
    return _out


def is_superseded_claim(claim: dict, dead_seqs: 'set[int]') -> bool:
    """True if `claim` is NOT the live assertion for its filename: either
    its state is a presence-skip marker (retracted/deprecated/obsolete) OR
    its `seq` is in `dead_seqs` (a `superseded_seqs(...)` result for the
    same builder).  The inverse — "treat as live" — is ``not
    is_superseded_claim(...)``."""
    if claim.get('claim_state') in PRESENCE_SKIP_CLAIM_STATES:
        return True
    _seq = claim.get('seq')
    return isinstance(_seq, int) and _seq in dead_seqs

# Required keys on every well-formed claim line.  `republished_from`
# is OPTIONAL — present only for tunneled (passthrough) packages,
# where the owner field reflects republisher identity (the builder
# that downloaded + re-signed), not original-build provenance.
_CLAIM_REQUIRED = frozenset({
    'v', 'builder', 'seq', 'package', 'intended_version',
    'built_version', 'filename', 'sha256', 'size', 'snapshot',
    'built_at', 'claim_state', 'sig',
})


def new_claim(
    *,
    builder: str,
    seq: int,
    package: str,
    intended_version: str,
    built_version: str,
    filename: str,
    sha256: str,
    size: int,
    snapshot: str,
    built_at: str,
    claim_state: str = CLAIM_STATE_PENDING,
    republished_from: 'Optional[Dict[str, Any]]' = None,
    component: str = 'main',
) -> dict:
    """Build an unsigned claim record.  Caller passes the result through
    identity.sign_claim before storing.  The `sig` field is added there.

    `republished_from`: pass {'url': str, 'upstream_sha256': str} for
    tunneled packages; None / omitted for self-built packages.  Affects
    `coord sync pull` policy (the "don't pull your own" rule does NOT
    apply to republished entries — the upstream is the authority).

    `component`: the apt component (main / contrib / non-free /
    non-free-firmware) of the source's origin mirror.  Determines the
    on-disk dir under repo/dists/<codename>/<comp>/binary-<arch>/ — a
    `mirror pull` consumer reads this to route the file correctly.
    Defaults to 'main' for back-compat with pre-component claim files
    (which only ever lived in main).
    """
    if claim_state not in CLAIM_STATES:
        raise ValueError(f"bad claim_state: {claim_state!r}")
    _rec: Dict[str, Any] = {
        'v':                 CLAIM_RECORD_SCHEMA_VERSION,
        'builder':           builder,
        'seq':                seq,
        'package':           package,
        'intended_version':  intended_version,
        'built_version':     built_version,
        'filename':          filename,
        'sha256':            sha256,
        'size':              size,
        'snapshot':          snapshot,
        'built_at':          built_at,
        'claim_state':       claim_state,
        'component':         component,
    }
    if republished_from is not None:
        _rec['republished_from'] = republished_from
    return _rec


def new_retraction(
    *,
    builder: str,
    seq: int,
    package: str,
    retracts_seq: int,
    filename: str,
    snapshot: str,
    built_at: str,
) -> dict:
    """A signed tombstone — `retracts_seq` is the seq of the prior
    claim being withdrawn.  Only the owner of the original claim can
    write a valid retraction (verifier checks builder match).

    A retraction is itself a claim line in the writer's JSONL; the
    fold-over reader merges it as "this prior claim is withdrawn".
    """
    return {
        'v':            CLAIM_RECORD_SCHEMA_VERSION,
        'builder':      builder,
        'seq':           seq,
        'package':      package,
        'intended_version': '',
        'built_version': '',
        'filename':     filename,
        'sha256':       '',
        'size':         0,
        'snapshot':     snapshot,
        'built_at':     built_at,
        'claim_state':  CLAIM_STATE_RETRACTED,
        'retracts_seq': retracts_seq,
    }


def new_deprecation(
    *,
    builder: str,
    seq: int,
    package: str,
    intended_version: str,
    built_version: str,
    filename: str,
    sha256: str,
    size: int,
    snapshot: str,
    built_at: str,
    deprecates_seq: int,
    component: str = 'main',
) -> dict:
    """A signed ownership-release for a file the builder published but no
    longer selects (SELECT-LOCK).  `deprecates_seq` is the seq of the prior
    published claim.

    Unlike `new_retraction` (a stripped tombstone), a deprecation KEEPS the
    file's identity (filename/sha256/size/version) so the .deb stays
    resolvable in the pool — apt clients can still install it, and another
    builder can take ownership by republishing.  `project_owners` reports
    builder=None for the winning deprecated claim (same no-owner treatment
    as a tunneled claim)."""
    _rec: Dict[str, Any] = {
        'v':                 CLAIM_RECORD_SCHEMA_VERSION,
        'builder':           builder,
        'seq':                seq,
        'package':           package,
        'intended_version':  intended_version,
        'built_version':     built_version,
        'filename':          filename,
        'sha256':            sha256,
        'size':              size,
        'snapshot':          snapshot,
        'built_at':          built_at,
        'claim_state':       CLAIM_STATE_DEPRECATED,
        'deprecates_seq':    deprecates_seq,
        'component':         component,
    }
    return _rec


def new_obsolescence(
    *,
    builder: str,
    seq: int,
    package: str,
    intended_version: str,
    built_version: str,
    filename: str,
    sha256: str,
    size: int,
    snapshot: str,
    built_at: str,
    obsoletes_seq: int,
    component: str = 'main',
) -> dict:
    """A version-superseded marker (LEDGER-01) the owner stamps on their
    OWN older-version claim when a newer version of the same binary is
    published.  `obsoletes_seq` is the seq of the superseded published
    claim (erased in the fold, this record stands in its place).

    Keeps the file's full identity — the old .deb stays in the append-only
    pool as a LABELED prune candidate.  Ownership is RETAINED (unlike a
    deprecation): project_owners keeps the builder, presence/index audits
    skip the file (PRESENCE_SKIP_CLAIM_STATES) because the old version may
    legitimately be pruned locally and drop out of the apt index."""
    _rec: Dict[str, Any] = {
        'v':                 CLAIM_RECORD_SCHEMA_VERSION,
        'builder':           builder,
        'seq':                seq,
        'package':           package,
        'intended_version':  intended_version,
        'built_version':     built_version,
        'filename':          filename,
        'sha256':            sha256,
        'size':              size,
        'snapshot':          snapshot,
        'built_at':          built_at,
        'claim_state':       CLAIM_STATE_OBSOLETE,
        'obsoletes_seq':     obsoletes_seq,
        'component':         component,
    }
    return _rec


def new_reclaim(
    *,
    builder: str,
    seq: int,
    package: str,
    intended_version: str,
    built_version: str,
    filename: str,
    sha256: str,
    size: int,
    snapshot: str,
    built_at: str,
    reclaims_seq: int,
    component: str = 'main',
) -> dict:
    """The sanctioned exception to the published-filename-
    is-frozen-bytes invariant.  A NEW live claim for the SAME filename
    with a NEW sha256 — a same-version rebuild whose content
    legitimately changed without a version bump (Position-X OOB
    normalization; disaster-recovery rebuilds).  `reclaims_seq` is the
    seq of the superseded published claim, folded everywhere a
    retraction/deprecation/obsolescence target would be — after the
    fold this claim is the filename's single live assertion.

    Unlike the marker shapes (retraction/deprecation/obsolescence),
    the reclaim record itself is the LIVE claim: claim_state starts
    PENDING and the publish pipeline stamps PUBLISHED at append, same
    as a normal new_claim.  Emitted only by the explicit
    `mirror reclaim` operator command — generate_pending_claims never
    re-claims a known filename."""
    _rec: Dict[str, Any] = {
        'v':                 CLAIM_RECORD_SCHEMA_VERSION,
        'builder':           builder,
        'seq':                seq,
        'package':           package,
        'intended_version':  intended_version,
        'built_version':     built_version,
        'filename':          filename,
        'sha256':            sha256,
        'size':              size,
        'snapshot':          snapshot,
        'built_at':          built_at,
        'claim_state':       CLAIM_STATE_PENDING,
        'reclaims_seq':      reclaims_seq,
        'component':         component,
    }
    return _rec


def canonical_bytes(record: dict) -> bytes:
    """Serialize a record (excluding `sig`) to canonical-JSON bytes for
    signing or hashing.  sort_keys + ASCII-safe + no whitespace.

    Mirrors utils._canonical_record_bytes verbatim so the discipline is
    one rule across the codebase.
    """
    _payload = {_k: _v for _k, _v in record.items() if _k != 'sig'}
    return json.dumps(
        _payload, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
    ).encode('utf-8')


def claim_to_jsonl(claim: dict) -> bytes:
    """Serialize a signed claim to a single JSONL line (trailing \\n).
    sort_keys for stability (so two builders independently writing the
    same record bytes-equal the line on disk)."""
    return (json.dumps(claim, sort_keys=True, ensure_ascii=True,
                       separators=(',', ':')) + '\n').encode('utf-8')


def claim_from_jsonl(line: bytes) -> 'Optional[dict]':
    """Parse one JSONL line back to a claim dict.  Returns None on
    JSON parse error or shape mismatch (NOT signature failure —
    that's identity.verify_claim's job, applied separately).
    """
    if not line or line.isspace():
        return None
    try:
        _obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(_obj, dict):
        return None
    if not _CLAIM_REQUIRED.issubset(_obj.keys()):
        return None
    if _obj.get('claim_state') not in CLAIM_STATES:
        return None
    return _obj


# ───────────────────────── SnapshotState ─────────────────────────


def new_snapshot_state(
    base: str, current: str, published: str, external: bool,
) -> dict:
    """The pin-tuple persisted at repo-coord/snapshot.json and
    referenced by coord-head.  Mirrors utils.snapshot.state's shape
    (base/current/published) plus the external flag so external-on
    builders aren't spuriously "ahead" of external-off ones."""
    return {
        'v':         SNAPSHOT_STATE_SCHEMA_VERSION,
        'base':      base,
        'current':   current,
        'published': published,
        'external':  bool(external),
    }


# ───────────────────────── CoordHead ─────────────────────────


def new_coord_head(
    *,
    inrelease_sha256: str,
    snapshot: dict,
    last_seqs: 'Dict[str, int]',
    head_time: str,
    neighbours: 'Optional[list]' = None,
    revoked_builders: 'Optional[Dict[str, str]]' = None,
) -> dict:
    """The signed canonical state snapshot.  GPG-clearsigned by the
    tier-1 (InRelease) signing key, stored at <mirror-root>/coord-head.json.

    - inrelease_sha256: sha256 of the dists/<suite>/InRelease this head
      pins.  Builders refuse a coord-head whose InRelease sha doesn't
      match the InRelease they fetched (defeats rollback).
    - snapshot: the SnapshotState dict at publish time.
    - last_seqs: {builder_id: highest_seq_published}.  Stale JSONL is
      detected by reading a max(seq) lower than this.
    - head_time: ISO8601 UTC.  Compared against InRelease `Date:` —
      a head older than InRelease is refused.
    - neighbours: MIRROR-01 Phase 7.  Canonical federation membership
      records — every mirror's coord-head carries the same
      ``list[{url, public_url, public_proto}]`` set, signed by tier-1
      GPG.  Accepts either a list of URL strings (auto-promoted to
      records with empty meta — v2 input shape) or a list of dicts
      already in record form.  Publish gates on local-config-vs-head
      URL-set diff (`check_federation_consistency`); split-brain
      federations are detected by pair-wise inequality in `mirror
      audit`.  Records are sorted by url + lower-cased for stable
      comparison.
    - revoked_builders: optional {builder_id: revoked_at_iso} for
      tier-2 key revocation propagation.
    """
    _head: Dict[str, Any] = {
        'v':                COORD_HEAD_SCHEMA_VERSION,
        'inrelease_sha256': inrelease_sha256,
        'snapshot':         snapshot,
        'last_seqs':        dict(last_seqs),
        'head_time':        head_time,
        'neighbours':       canonicalize_neighbour_records(neighbours or []),
    }
    if revoked_builders:
        _head['revoked_builders'] = dict(revoked_builders)
    return _head


def canonicalize_neighbour_records(items: 'list') -> 'list':
    """v3 canonical form: a sorted+deduped list of dicts, each carrying
    ``url`` (the ssh:// publish URL) plus ``public_url`` and
    ``public_proto`` (the apt-readable URL the chroot writes into
    sources.list.d, and which protocol scheme it uses).

    Read-tolerant — accepts both v2-shaped input (``list[str]`` of bare
    URLs) and v3 input (``list[dict]``).  Mixed lists work too.  v2
    entries are promoted to records with empty ``public_url`` /
    ``public_proto``, which downstream consumers (`cmd_mirror_add`)
    interpret as "inherit operator's --proto flag".

    Output rules:
      - dedup by canonical url (lowercased, trailing-slash stripped)
      - sort ascending by url for stable JSON diff
      - empty / non-string urls dropped silently
    """
    _out: 'list[Dict[str, str]]' = []
    _seen: 'set[str]' = set()
    for _it in items:
        if isinstance(_it, str):
            _u = _it.strip().rstrip('/').lower()
            _pub_url = ''
            _pub_proto = ''
        elif isinstance(_it, dict):
            _raw = _it.get('url')
            _u = (_raw or '').strip().rstrip('/').lower() if isinstance(_raw, str) else ''
            _pub_url = (_it.get('public_url') or '').strip() if isinstance(_it.get('public_url'), str) else ''
            _pub_proto = (_it.get('public_proto') or '').strip().lower() if isinstance(_it.get('public_proto'), str) else ''
        else:
            continue
        if not _u or _u in _seen:
            continue
        _seen.add(_u)
        _out.append({
            'url':           _u,
            'public_url':    _pub_url,
            'public_proto':  _pub_proto,
        })
    _out.sort(key=lambda _r: _r['url'])
    return _out


def neighbour_urls(items: 'list') -> 'list':
    """Project canonical neighbours to a flat sorted ``list[str]`` of
    URLs.  The federation gate uses this for set comparison against
    local config's mirror URLs."""
    return [_r['url'] for _r in canonicalize_neighbour_records(items)]


def canonicalize_neighbours(urls: 'list') -> 'list':
    """Back-compat alias — v2 callers that expect a ``list[str]``
    canonical URL list keep working unchanged.  Internally just the
    URL projection of `canonicalize_neighbour_records`.

    New code should call `canonicalize_neighbour_records` (when it
    needs the full dict shape) or `neighbour_urls` (when it just needs
    the URL set, e.g. federation-gate set-diff)."""
    return neighbour_urls(urls)
