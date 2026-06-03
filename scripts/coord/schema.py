"""COORD-01 schema — the on-the-wire data shapes.

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
from typing import Any, Dict, Optional

# Pinned at v1; bump on any breaking field change.  Readers tolerate
# unknown future keys (preserve in dict; ignore semantics they don't
# know).  Removing a key is a v2.
CLAIM_RECORD_SCHEMA_VERSION = 1
COORD_HEAD_SCHEMA_VERSION = 1
SNAPSHOT_STATE_SCHEMA_VERSION = 1

# Lifecycle states a claim can be in.  `pending` is the post-rsync,
# pre-reindex state — the .deb is on the remote's pool but apt won't
# see it until the next reindex+sign.  `published` is the steady
# state.  `retracted` is a signed tombstone — only the owner can
# write it; references the seq of the claim being retracted.
CLAIM_STATE_PENDING = 'pending'
CLAIM_STATE_PUBLISHED = 'published'
CLAIM_STATE_RETRACTED = 'retracted'
CLAIM_STATES = frozenset({
    CLAIM_STATE_PENDING, CLAIM_STATE_PUBLISHED, CLAIM_STATE_RETRACTED,
})

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
) -> dict:
    """Build an unsigned claim record.  Caller passes the result through
    identity.sign_claim before storing.  The `sig` field is added there.

    `republished_from`: pass {'url': str, 'upstream_sha256': str} for
    tunneled packages; None / omitted for self-built packages.  Affects
    `coord sync pull` policy (the "don't pull your own" rule does NOT
    apply to republished entries — the upstream is the authority).
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
    revoked_builders: 'Optional[Dict[str, str]]' = None,
) -> dict:
    """The signed canonical state snapshot.  GPG-clearsigned by the
    tier-1 (InRelease) signing key, stored at repo-coord/coord-head.asc.

    - inrelease_sha256: sha256 of the dists/<suite>/InRelease this head
      pins.  Builders refuse a coord-head whose InRelease sha doesn't
      match the InRelease they fetched (defeats rollback).
    - snapshot: the SnapshotState dict at publish time.
    - last_seqs: {builder_id: highest_seq_published}.  Stale JSONL is
      detected by reading a max(seq) lower than this.
    - head_time: ISO8601 UTC.  Compared against InRelease `Date:` —
      a head older than InRelease is refused.
    - revoked_builders: optional {builder_id: revoked_at_iso} for
      tier-2 key revocation propagation.
    """
    _head: Dict[str, Any] = {
        'v':                COORD_HEAD_SCHEMA_VERSION,
        'inrelease_sha256': inrelease_sha256,
        'snapshot':         snapshot,
        'last_seqs':        dict(last_seqs),
        'head_time':        head_time,
    }
    if revoked_builders:
        _head['revoked_builders'] = dict(revoked_builders)
    return _head
