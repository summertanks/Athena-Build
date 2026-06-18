"""COORD-01 local storage — per-builder JSONL append + fold-over reader.

The on-disk layout under <repo_coord>/claims/:

  claims/<builder-id>.jsonl    append-only, single-writer per file

Each builder writes ONLY its own `.jsonl`.  Cross-host concurrency is
trivial because there's no shared write path.  Within a host we use
fcntl advisory locking when appending so a concurrent rebuild from
the same builder doesn't interleave bytes mid-line.

Reads fold over EVERY *.jsonl in the directory.  The "logical sidecar"
that consumers see is this merge.  Per-line signature verification is
applied at read time (identity.verify_claim_against_keyring); failed
lines are discarded with a logged warning, not raised.

Append discipline: write a complete JSONL line in one write(), fsync,
then release.  POSIX guarantees a single write() of length ≤ PIPE_BUF
is atomic; our lines are typically a few hundred bytes — well within
limits — and we use file locking belt-and-braces.

Atomicity vs rsync: rsync transferring a file that's actively being
appended to can transfer a half-line.  We write a complete line first,
fsync, release the lock, THEN trigger the rsync — never rsync
mid-append.  See coord.transport for the SSH push primitive.
"""

import fcntl
import logging
import os
from typing import Dict, Iterable, List, Optional, Tuple

from . import schema as _schema
from . import identity as _identity

logger = logging.getLogger('athena')


def claims_path(claims_dir: str, builder_id: str) -> str:
    return os.path.join(claims_dir, builder_id + '.jsonl')


def max_seq(claims_dir: str, builder_id: str) -> int:
    """Return the highest `seq` value across all claims in this builder's
    jsonl.  Returns 0 if the file is missing / empty / unreadable so
    the next seq is 1 (caller adds 1).

    Skips lines that fail JSON parse — the writer is supposed to be
    line-atomic so a torn line is a hard signal of trouble, but we
    don't want max_seq() to error out; the audit will catch it.
    """
    _path = claims_path(claims_dir, builder_id)
    _max = 0
    try:
        with open(_path, 'rb') as _fh:
            for _line in _fh:
                _claim = _schema.claim_from_jsonl(_line)
                if _claim is None:
                    continue
                if _claim.get('builder') != builder_id:
                    continue
                _seq = _claim.get('seq')
                if isinstance(_seq, int) and _seq > _max:
                    _max = _seq
    except FileNotFoundError:
        return 0
    except OSError as _e:
        logger.warning(f"coord.store.max_seq({_path}): {_e}")
        return 0
    return _max


def append_claim(
    claims_dir: str, builder_id: str, claim: dict, private_key_path: str,
) -> int:
    """Sign the claim, append the canonical JSONL line to
    <claims_dir>/<builder-id>.jsonl, fsync, return the seq.

    The caller passes a claim with `seq` already populated — typically
    `max_seq(...) + 1`.  We refuse to write if the claim's builder
    field doesn't match the file we'd write to (caller bug).
    """
    if claim.get('builder') != builder_id:
        raise ValueError(
            f"claim builder {claim.get('builder')!r} != target "
            f"file builder {builder_id!r}")
    os.makedirs(claims_dir, mode=0o755, exist_ok=True)
    _signed = _identity.sign_claim(claim, private_key_path)
    _line = _schema.claim_to_jsonl(_signed)
    _path = claims_path(claims_dir, builder_id)
    # Open append + advisory lock + atomic single write + fsync.
    _fd = os.open(_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(_fd, fcntl.LOCK_EX)
        try:
            os.write(_fd, _line)
            os.fsync(_fd)
        finally:
            fcntl.flock(_fd, fcntl.LOCK_UN)
    finally:
        os.close(_fd)
    _seq = _signed.get('seq')
    return int(_seq) if isinstance(_seq, int) else 0


def read_builder_claims(
    claims_dir: str, builder_id: str,
    public_key_path: 'Optional[str]' = None,
) -> List[dict]:
    """Read every line from <claims_dir>/<builder-id>.jsonl, parse, and
    (if `public_key_path` is given) verify each signature.  Returns
    the list of accepted records in file order — caller folds.

    Lines that fail parse or verify are logged + dropped.  Returns []
    on missing file.
    """
    _out: List[dict] = []
    _path = claims_path(claims_dir, builder_id)
    try:
        _fh = open(_path, 'rb')
    except FileNotFoundError:
        return _out
    except OSError as _e:
        logger.warning(f"coord.store.read_builder_claims({_path}): {_e}")
        return _out
    try:
        for _line in _fh:
            _claim = _schema.claim_from_jsonl(_line)
            if _claim is None:
                continue
            if _claim.get('builder') != builder_id:
                # A jsonl file is single-writer; a foreign builder
                # field is suspicious — log and drop.
                logger.warning(
                    f"coord.store: foreign builder {_claim.get('builder')!r} "
                    f"in {_path}; dropping")
                continue
            if public_key_path is not None:
                if not _identity.verify_claim(_claim, public_key_path):
                    logger.warning(
                        f"coord.store: signature verify failed for "
                        f"{_claim.get('package')}#{_claim.get('seq')} "
                        f"in {_path}; dropping")
                    continue
            _out.append(_claim)
    finally:
        _fh.close()
    return _out


def read_all_claims(
    claims_dir: str, keyring: Dict[str, str],
    revoked: 'Optional[Dict[str, str]]' = None,
) -> Dict[str, List[dict]]:
    """Walk every <builder-id>.jsonl in `claims_dir`, verify each line
    against the matching keyring pubkey, return {builder_id: [claims]}.

    Files whose builder_id isn't in the keyring are entirely SKIPPED
    (whole-file untrusted; no partial inclusion).  Revoked builders
    same treatment.  Caller folds the merged result into the audit
    view.
    """
    _out: Dict[str, List[dict]] = {}
    try:
        _entries = sorted(os.listdir(claims_dir))
    except OSError:
        return _out
    for _e in _entries:
        if not _e.endswith('.jsonl'):
            continue
        _bid = _e[:-len('.jsonl')]
        if _bid not in keyring:
            logger.warning(
                f"coord.store: {_e}: builder not in keyring; dropping file")
            continue
        if revoked and _bid in revoked:
            logger.warning(
                f"coord.store: {_e}: builder revoked; dropping file")
            continue
        _claims = read_builder_claims(claims_dir, _bid, keyring[_bid])
        if _claims:
            _out[_bid] = _claims
    return _out


# ───────────────────────── projection helpers ─────────────────────────


def project_live_claims(
    by_builder: Dict[str, List[dict]],
) -> Dict[Tuple[str, str], dict]:
    """Collapse the per-builder claim lists into a {(package, version):
    claim} map of the CURRENT live state.

    Rules:
      - Within a builder, a retraction line erases the prior claim with
        that retracts_seq.
      - claim_state=published wins over claim_state=pending for the
        same (package, version) tuple from the same builder.
      - Across builders, hash conflict (same (pkg,ver), different
        sha256) → BOTH entries appear in the returned map keyed by
        (pkg, ver, builder).  The reconcile layer flags this as
        CRITICAL.  This function does NOT decide policy.
    """
    # Per-builder fold
    _per: Dict[str, Dict[Tuple[str, str], dict]] = {}
    for _bid, _claims in by_builder.items():
        _state: Dict[Tuple[str, str], dict] = {}
        _retracted_seqs: set = set()
        for _c in _claims:
            if _c.get('claim_state') == _schema.CLAIM_STATE_RETRACTED:
                _retracts = _c.get('retracts_seq')
                if isinstance(_retracts, int):
                    _retracted_seqs.add(_retracts)
                continue
            if _c.get('claim_state') == _schema.CLAIM_STATE_DEPRECATED:
                # deprecation RELEASES ownership — like a
                # retraction it drops the superseded published claim, and the
                # deprecated claim itself is not "live-owned" here (the file
                # stays in the pool, but it's no longer ours).  project_owners
                # surfaces it as no-owner separately.
                _dep = _c.get('deprecates_seq')
                if isinstance(_dep, int):
                    _retracted_seqs.add(_dep)
                continue
            if _c.get('claim_state') == _schema.CLAIM_STATE_OBSOLETE:
                # obsolescence supersedes the old version's
                # published claim; the obsolete claim itself is not part of
                # the live (pkg,ver) map either — project_owners surfaces it
                # per-filename with ownership retained.
                _obs = _c.get('obsoletes_seq')
                if isinstance(_obs, int):
                    _retracted_seqs.add(_obs)
                continue
            # a reclaim is a LIVE claim (no continue) that
            # erases the superseded same-(pkg,ver) claim via its back-ref;
            # the trailing post-filter drops the earlier-processed old
            # claim (reclaims always carry the higher seq).
            _rcl = _c.get('reclaims_seq')
            if isinstance(_rcl, int):
                _retracted_seqs.add(_rcl)
            _seq = _c.get('seq')
            if isinstance(_seq, int) and _seq in _retracted_seqs:
                continue
            _key = (str(_c.get('package')), str(_c.get('built_version')))
            _prev = _state.get(_key)
            if _prev is None:
                _state[_key] = _c
                continue
            # Same (pkg,ver) — prefer published over pending; prefer
            # highest seq within same state.
            _prev_state = _prev.get('claim_state')
            _curr_state = _c.get('claim_state')
            if (_prev_state == _schema.CLAIM_STATE_PENDING
                    and _curr_state == _schema.CLAIM_STATE_PUBLISHED):
                _state[_key] = _c
            elif (_prev_state == _curr_state
                  and int(_c.get('seq', 0)) > int(_prev.get('seq', 0))):
                _state[_key] = _c
        # Filter retractions that landed AFTER the claim in our pass.
        _per[_bid] = {
            _k: _v for _k, _v in _state.items()
            if int(_v.get('seq', 0)) not in _retracted_seqs
        }
    # Cross-builder merge
    _out: Dict[Tuple[str, str], dict] = {}
    for _bid, _state in _per.items():
        for _key, _claim in _state.items():
            _existing = _out.get(_key)
            if _existing is None:
                _out[_key] = _claim
                continue
            # Conflict detection happens at the reconcile layer; we
            # surface both by keying the second one with builder.
            _conflict_key = (_key[0], _key[1] + '!' + str(_claim.get('builder')))
            _out[_conflict_key] = _claim
    return _out


def iter_live_claims_by_filename(
    by_builder: Dict[str, List[dict]],
) -> Iterable[Tuple[str, dict]]:
    """Yield (filename, claim) for every LIVE (non-retracted) claim
    across all builders.  Caller filters by claim_state if needed.

    Multiple builders claiming the SAME filename surface as multiple
    (filename, claim) tuples — reconcile catches that as a hash
    conflict (or, if hashes match, a duplicate).
    """
    for _bid, _claims in by_builder.items():
        _retracted: set = set()
        for _c in _claims:
            if _c.get('claim_state') == _schema.CLAIM_STATE_RETRACTED:
                _r = _c.get('retracts_seq')
                if isinstance(_r, int):
                    _retracted.add(_r)
            elif _c.get('claim_state') == _schema.CLAIM_STATE_DEPRECATED:
                # The published claim a deprecation supersedes is dropped, but
                # the deprecated claim itself is YIELDED so project_owners can
                # report the filename as no-owner (released to the commons).
                _d = _c.get('deprecates_seq')
                if isinstance(_d, int):
                    _retracted.add(_d)
            elif _c.get('claim_state') == _schema.CLAIM_STATE_OBSOLETE:
                # Same shape for obsolescence: the superseded published claim
                # is erased; the obsolete claim is YIELDED so project_owners
                # reports the filename as owned-but-obsolete (prune candidate).
                _o = _c.get('obsoletes_seq')
                if isinstance(_o, int):
                    _retracted.add(_o)
            # a reclaim is itself a LIVE published claim, so
            # its back-ref is collected unconditionally (no state branch);
            # the superseded same-filename claim is erased and the reclaim
            # is yielded as the filename's single live assertion.
            _rc = _c.get('reclaims_seq')
            if isinstance(_rc, int):
                _retracted.add(_rc)
        for _c in _claims:
            if _c.get('claim_state') == _schema.CLAIM_STATE_RETRACTED:
                continue
            if int(_c.get('seq', 0)) in _retracted:
                continue
            _fn = _c.get('filename')
            if isinstance(_fn, str) and _fn:
                yield _fn, _c


def project_owners(
    by_builder: Dict[str, List[dict]],
) -> Dict[str, dict]:
    """MIRROR-02: project the per-builder claim ledger into a
    per-filename ownership view.

    Returns ``{filename: owner_record}`` where ``owner_record`` is::

      {
        'builder':       str | None,   # None ⇔ tunneled (no owner)
        'version':       str,          # built_version on the winning claim
        'sha256':        str,          # SHA-256 of the .deb in the pool
        'claim_state':   str,          # 'published' / 'pending' (never retracted here)
        'seq':           int,          # producer-side seq for tie-break
        'republished_from': dict|None, # truthy ⇔ tunneled
        'claim':         dict,         # the winning claim, verbatim
      }

    Ownership rules:
      - Iterates `iter_live_claims_by_filename` (already drops retractions
        and same-builder pending-vs-published collisions).
      - For each filename, picks the latest claim by (seq, builder)
        across builders.  `seq` ordering reflects builder-local
        chronology of `publish` events; cross-builder ties broken by
        builder-id lexical sort (deterministic, no clock dependency).
      - A claim with `republished_from != None` is TUNNELED — the
        returned record sets ``builder=None``.  Tunneled claims have
        no owner; publishing over them is a normal (auto-ownership
        transfer) path in chunk 8.
      - A non-tunneled claim's ``builder`` field IS the owner.

    Hash conflicts (same filename, different SHA across builders) are
    NOT resolved here — that's `detect_hash_conflicts`' job at
    `scripts/coord/reconcile.py:310`.  This projection just picks
    "the latest" assuming the conflict was already gated.
    """
    _candidates: Dict[str, List[dict]] = {}
    for _fn, _c in iter_live_claims_by_filename(by_builder):
        _candidates.setdefault(_fn, []).append(_c)

    _out: Dict[str, dict] = {}
    for _fn, _claims in _candidates.items():
        # Sort by (seq, builder) descending; first wins.  ``-`` on
        # seq for desc; builder for stable tie-break (asc to match
        # alphabetical convention).
        _sorted = sorted(
            _claims,
            key=lambda _c: (
                -int(_c.get('seq', 0)),
                str(_c.get('builder', '')),
            ),
        )
        _winner = _sorted[0]
        _republished = _winner.get('republished_from')
        _is_tunneled = bool(_republished)
        # a deprecated winner has RELEASED ownership — same
        # no-owner treatment as a tunneled claim, so another builder may take
        # the file over by republishing (filter_pending_by_ownership rule).
        # an OBSOLETE winner is deliberately NOT here — version
        # supersession is natural aging, the owner keeps the (old) file's
        # ownership; it just becomes a labeled prune candidate.
        _is_deprecated = (_winner.get('claim_state')
                          == _schema.CLAIM_STATE_DEPRECATED)
        _no_owner = _is_tunneled or _is_deprecated
        _out[_fn] = {
            'builder':          None if _no_owner
                                else _winner.get('builder'),
            'version':          str(_winner.get('built_version') or ''),
            'sha256':           str(_winner.get('sha256') or ''),
            'claim_state':      str(_winner.get('claim_state') or ''),
            'seq':              int(_winner.get('seq') or 0),
            'republished_from': _republished if _is_tunneled else None,
            'claim':            _winner,
        }
    return _out
