"""COORD-01 reconciliation — 3-way audit (local, repo, cross).

Three audit modes (per the COORD-01 plan):

  local   pool ↔ this builder's own jsonl
          - every owned .deb present in pool with claim hash matching
            file hash
          - every pool .deb whose owner is us has a live (non-retracted)
            local claim
          - schema_version consistency

  cross   build.json ↔ all-builder jsonls
          - every local build.json with phase=done has a claim from us
          - hashes in build.json output_hashes agree with claim sha256

  repo    remote pool + remote keyring + coord-head ↔ all-builder jsonls
          - LANDS IN P3 — needs transport.py (fetch remote tree)
          - this module exposes the primitives; the actual command
            lives in coord.transport once that exists.

This module is policy-aware via `coord.policy` but does NOT make the
publishing decision — `coord.publish` reads PUBLISH_HALT and acts.
Reconcile only reports; the report's CRITICAL count surfaces to the
operator via cmd_coord_audit (P1 wired) or as a publish-gate
precondition (P3).

Findings vocabulary:

  CRITICAL — security or correctness issue.  Hash conflict,
             signature failure, owner-missing-from-keyring.
  WARN     — operational drift.  Orphan past threshold, missing-
             pool-file for a claim, mtime drift.
  INFO     — informational.  Healthy state summaries, fresh claims.
"""

import dataclasses as _dc
import logging as _logging
import os as _os
from typing import Callable, Dict, List, Optional, Tuple

from . import schema as _schema
from . import store as _store
from . import policy as _policy

logger = _logging.getLogger('athena')


# ───────────────────────── data shapes ─────────────────────────


@_dc.dataclass(frozen=True)
class Finding:
    severity: str           # 'CRITICAL' | 'WARN' | 'INFO'
    kind: str               # short slug: 'hash_conflict', 'orphan', ...
    message: str            # operator-readable
    package: Optional[str] = None
    filename: Optional[str] = None
    builder: Optional[str] = None


@_dc.dataclass(frozen=True)
class Report:
    mode: str               # 'local' | 'cross' | 'repo'
    findings: List[Finding]
    critical: int
    warn: int
    info: int

    @classmethod
    def make(cls, mode: str, findings: List[Finding]) -> 'Report':
        _c = sum(1 for _f in findings if _f.severity == 'CRITICAL')
        _w = sum(1 for _f in findings if _f.severity == 'WARN')
        _i = sum(1 for _f in findings if _f.severity == 'INFO')
        return cls(mode=mode, findings=findings,
                   critical=_c, warn=_w, info=_i)


# ───────────────────────── pool inventory ─────────────────────────


def scan_pool_files(repo_root: str) -> Dict[str, str]:
    """Walk `repo_root` for .deb/.udeb files; return {filename: path}.

    Filename is the basename; path is absolute.  Duplicates surface as
    last-write-wins — the audit reports duplicate basenames in
    different subdirs separately.

    Cheap O(N) walk; no hashing.  Audit consumers call utils.get_sha256
    (cached) on the files they actually need to verify.
    """
    _out: Dict[str, str] = {}
    if not _os.path.isdir(repo_root):
        return _out
    for _root, _dirs, _files in _os.walk(repo_root):
        for _f in _files:
            if _f.endswith(('.deb', '.udeb')):
                _out[_f] = _os.path.join(_root, _f)
    return _out


# ───────────────────────── local audit ─────────────────────────


def audit_local(
    *, repo_root: str, claims_dir: str, builder_id: str,
    public_key_path: str, get_sha256: Callable,
) -> Report:
    """Pool ↔ this-builder claims audit.

    Inputs:
      repo_root        — directory containing the pool to scan
      claims_dir       — directory containing per-builder jsonls
                         (typically <dir_coord>/claims/)
      builder_id       — our own builder identifier
      public_key_path  — our own public key (verifies our claims)
      get_sha256       — callable f(path) -> hex digest (typically
                         utils.get_sha256; injected so tests can
                         stub it without monkey-patching utils)

    Checks:
      C-1  every published claim of ours points at a .deb in our
           local pool (orphan if not)
      C-2  the .deb's actual sha256 matches the claim's sha256
      C-3  every pool .deb that we own (per filename match in our
           jsonl) has a live claim
      C-4  every claim in our jsonl verifies cryptographically
    """
    _findings: List[Finding] = []
    _pool = scan_pool_files(repo_root)
    _my_claims = _store.read_builder_claims(
        claims_dir, builder_id, public_key_path)
    # Project to live state (collapse retractions, prefer published
    # over pending).
    _by_builder = {builder_id: _my_claims} if _my_claims else {}
    _live = _store.project_live_claims(_by_builder)

    # C-1, C-2 — for each of our LIVE claims, check pool presence + hash
    _claimed_filenames: set = set()
    for _key, _claim in _live.items():
        _fn = _claim.get('filename')
        if not isinstance(_fn, str) or not _fn:
            continue
        _claimed_filenames.add(_fn)
        _pkg = _claim.get('package')
        _claim_sha = _claim.get('sha256')
        _path = _pool.get(_fn)
        if _path is None:
            _findings.append(Finding(
                severity='WARN', kind='orphan',
                message=(
                    f"claim for {_fn} exists but file is absent from "
                    f"local pool {repo_root} — rebuild or retract"),
                package=_pkg, filename=_fn, builder=builder_id,
            ))
            continue
        _disk_sha = get_sha256(_path)
        if not _disk_sha:
            _findings.append(Finding(
                severity='WARN', kind='hash_unreadable',
                message=f"could not compute sha256 for {_path}",
                package=_pkg, filename=_fn, builder=builder_id,
            ))
            continue
        if _disk_sha != _claim_sha:
            _findings.append(Finding(
                severity='CRITICAL', kind='hash_mismatch',
                message=(
                    f"claim sha256 {_claim_sha} does not match disk "
                    f"sha256 {_disk_sha} for {_fn} at {_path} — "
                    f"file was rewritten without claim update"),
                package=_pkg, filename=_fn, builder=builder_id,
            ))

    # C-3 — for each pool .deb, do we have a claim for it?
    # (Only flags files in OUR pool whose claim would naturally be
    # ours; we can't distinguish "owned by another builder, no
    # claim yet rsync'd locally" from "rogue" at the LOCAL layer —
    # that's audit_repo's job.  For local audit we just check we
    # haven't accidentally lost track of a file we built.)
    for _fn in _pool:
        if _fn in _claimed_filenames:
            continue
        # Heuristic: if a build.json record (consumed by audit_cross)
        # names this file as one of our outputs, it's "ours but
        # unclaimed".  At this layer we don't open build.json; we
        # just report unclaimed pool files as INFO and let the
        # cross-audit upgrade to WARN if it's something we built.
        _findings.append(Finding(
            severity='INFO', kind='unclaimed_pool_file',
            message=(
                f"pool file {_fn} has no claim from us (may be owned by "
                f"another builder or pre-coord legacy)"),
            filename=_fn,
        ))

    # Always emit a summary INFO so silent success still surfaces
    # the operator's "what was checked" signal.
    _findings.append(Finding(
        severity='INFO', kind='summary',
        message=(
            f"audit_local: pool={len(_pool)} files, "
            f"live_claims={len(_live)}, "
            f"claimed_pool_files={sum(1 for _f in _pool if _f in _claimed_filenames)}"),
    ))
    return Report.make('local', _findings)


# ───────────────────────── cross-audit ─────────────────────────


def audit_cross(
    *, buildlog_dir: str, claims_dir: str, builder_id: str,
    public_key_path: str, read_build_record: Callable,
) -> Report:
    """build.json ↔ all-builder jsonls audit.

    Inputs:
      buildlog_dir       — typically <dir_log>/build/
      claims_dir         — <dir_coord>/claims/
      builder_id         — our own id
      public_key_path    — our pubkey (verifies our claims)
      read_build_record  — callable f(buildlog_dir, package) -> dict|None
                           (typically utils.read_build_record)

    Checks:
      X-1  every build.json with phase=done has at least one claim
           from us covering each of its outputs
      X-2  hashes in build.json output_hashes agree with the matching
           claim's sha256 (when both are populated)
    """
    _findings: List[Finding] = []
    _my_claims = _store.read_builder_claims(
        claims_dir, builder_id, public_key_path)
    # Index claims by (package, filename)
    _claim_by_fn: Dict[str, dict] = {}
    for _c in _my_claims:
        if _c.get('claim_state') in _schema.INACTIVE_CLAIM_STATES:
            continue
        _fn = _c.get('filename')
        if isinstance(_fn, str):
            _claim_by_fn[_fn] = _c

    # Walk build.json records
    _record_count = 0
    _done_count = 0
    _output_count = 0
    try:
        _entries = sorted(_os.listdir(buildlog_dir))
    except OSError as _e:
        logger.warning(f"audit_cross: cannot list {buildlog_dir}: {_e}")
        return Report.make('cross', _findings)
    _suffix = '.build.json'
    for _entry in _entries:
        if not _entry.endswith(_suffix):
            continue
        _pkg = _entry[:-len(_suffix)]
        _rec = read_build_record(buildlog_dir, _pkg)
        if _rec is None:
            continue
        _record_count += 1
        _phase = _rec.get('phase')
        if _phase != 'done':
            # tunneled records have their own claim path; failed
            # records have no outputs to track.
            continue
        _done_count += 1
        _outputs = _rec.get('outputs') or []
        _hashes = _rec.get('output_hashes') or {}
        _output_count += len(_outputs)
        for _out_fn in _outputs:
            _claim = _claim_by_fn.get(_out_fn)
            if _claim is None:
                _findings.append(Finding(
                    severity='WARN', kind='unclaimed_build',
                    message=(
                        f"build.json for {_pkg} lists output {_out_fn} but "
                        f"no live claim from us covers it — publish may "
                        f"have failed or been skipped"),
                    package=_pkg, filename=_out_fn, builder=builder_id,
                ))
                continue
            _build_sha = _hashes.get(_out_fn)
            _claim_sha = _claim.get('sha256')
            if (isinstance(_build_sha, str) and _build_sha
                    and isinstance(_claim_sha, str)
                    and _build_sha != _claim_sha):
                _findings.append(Finding(
                    severity='CRITICAL', kind='hash_drift',
                    message=(
                        f"build.json sha256 {_build_sha} != claim sha256 "
                        f"{_claim_sha} for {_out_fn} (pkg {_pkg}) — build "
                        f"and claim disagree on what we shipped"),
                    package=_pkg, filename=_out_fn, builder=builder_id,
                ))

    # Summary INFO so silent success surfaces the scan totals.
    _findings.append(Finding(
        severity='INFO', kind='summary',
        message=(
            f"audit_cross: build.json records={_record_count} "
            f"(phase=done {_done_count}), outputs in done={_output_count}, "
            f"claims_by_filename={len(_claim_by_fn)}"),
    ))
    return Report.make('cross', _findings)


# ───────────────────────── hash-conflict detection ─────────────────────────


def detect_hash_conflicts(
    by_builder: 'Dict[str, List[dict]]',
) -> List[Finding]:
    """Scan the merged claim view for binary FILENAMES claimed 2+ times
    with DIFFERENT sha256s.  A `.deb` filename encodes pkg+ver+arch
    uniquely, so two claims for the same filename must agree on the
    file's SHA-256 — they're either the same binary or two builders
    that produced a bit-reproducible result.

    Hash conflict = HASH_CONFLICT_POLICY=BLOCK → CRITICAL.

    Same filename + same SHA-256 (across builders) is NOT a conflict —
    it just means independent builds agreed bit-for-bit on the output.
    That's the happy outcome; report it as INFO so the audit at least
    surfaces the duplication.

    NOTE: grouping by `claim.filename` (not by `(claim.package,
    claim.built_version)`).  `claim.package` holds the SOURCE name —
    e.g. `firmware-nonfree` source produces 6 distinct binary
    filenames (firmware-amd-graphics, firmware-atheros, …) at the
    same source version `20230210-5`.  Grouping by source pkg + ver
    false-positives EVERY multi-binary source as a conflict.  Caught
    2026-06-05.
    """
    _findings: List[Finding] = []
    # Index: filename → list of (builder, sha256, package_source_name)
    _by_fn: 'Dict[str, List[Tuple[str, str, str]]]' = {}
    for _bid, _claims in by_builder.items():
        # the canonical supersession fold.  Folds marker states
        # (retracted/deprecated/obsolete) AND seqs named by a back-ref —
        # including a reclaim's reclaims_seq (a LIVE published claim, not a
        # marker): without that fold the same-filename old/new pair a
        # reclaim deliberately creates reads as a hash conflict and
        # CRITICAL-halts every subsequent publish.
        _dead = _schema.superseded_seqs(_claims)
        for _c in _claims:
            if _schema.is_superseded_claim(_c, _dead):
                continue
            _fn = str(_c.get('filename') or '')
            if not _fn:
                continue
            _by_fn.setdefault(_fn, []).append((
                str(_c.get('builder')),
                str(_c.get('sha256')),
                str(_c.get('package') or ''),
            ))
    for _fn, _entries in _by_fn.items():
        if len(_entries) < 2:
            continue
        _shas = {_s for _b, _s, _ in _entries}
        # Pull a representative source pkg name for the Finding's
        # `package` field — entries for one filename always share the
        # same source.
        _pkg_field = _entries[0][2]
        if len(_shas) > 1:
            _detail = ', '.join(f"{_b}={_s[:12]}" for _b, _s, _ in _entries)
            _findings.append(Finding(
                severity='CRITICAL', kind='hash_conflict',
                message=(
                    f"hash conflict on {_fn}: {_detail} — "
                    f"policy={_policy.HASH_CONFLICT_POLICY} → "
                    f"publish halt"),
                package=_pkg_field,
            ))
        else:
            _builders = ', '.join(_b for _b, _, _ in _entries)
            _findings.append(Finding(
                severity='INFO', kind='reproducible_duplicate',
                message=(
                    f"{_fn} shipped by multiple builders "
                    f"({_builders}) with identical sha256 — reproducible"),
                package=_pkg_field,
            ))
    return _findings


def audit_repo(
    *, fetched_dir: str, by_builder: 'Dict[str, List[dict]]',
    keyring: 'Dict[str, str]', head: 'Optional[dict]',
) -> Report:
    """Repo-side audit — every signature OK, every owner registered,
    no hash conflict, coord-head freshness already verified by caller.

    Inputs:
      fetched_dir   — coord/fetched/ (where pull rsync'd remote tree)
      by_builder    — {bid: [claims]} already-verified via store.read_all_claims
      keyring       — {bid: pubkey_path} from identity.load_keyring
      head          — parsed coord-head dict (already-verified) or None

    Checks:
      R-1  every claim's `builder` is in the keyring (already enforced
           by store.read_all_claims; surface as report metadata)
      R-2  hash conflicts (delegated to detect_hash_conflicts)
      R-3  every live claim's filename is unique across builders (no
           two builders claim the same pool slot); same filename with
           same hash is INFO, different hash already caught by R-2
      R-4  coord-head presence (CRITICAL if None)
      R-5  every claim's seq ≤ coord-head.last_seqs[builder] (otherwise
           the claim is "ahead of head" — coord-head needs re-signing
           by the publisher; surface as WARN)
    """
    _findings: List[Finding] = []
    if head is None:
        _findings.append(Finding(
            severity='CRITICAL', kind='no_coord_head',
            message=(
                f"no verified coord-head at {fetched_dir} — remote "
                "tree is untrusted"),
        ))
        return Report.make('repo', _findings)
    _findings.extend(detect_hash_conflicts(by_builder))

    # R-5: claims ahead of coord-head
    _last_seqs = head.get('last_seqs') or {}
    for _bid, _claims in by_builder.items():
        _head_seq = int(_last_seqs.get(_bid, 0))
        for _c in _claims:
            _seq = int(_c.get('seq', 0))
            if _seq > _head_seq:
                _findings.append(Finding(
                    severity='WARN', kind='claim_ahead_of_head',
                    message=(
                        f"claim seq={_seq} for {_c.get('package')} by "
                        f"{_bid} exceeds coord-head last_seq={_head_seq} "
                        "(re-sign coord-head)"),
                    package=_c.get('package'),
                    filename=_c.get('filename'),
                    builder=_bid,
                ))

    # R-3: cross-builder filename uniqueness (different hash already
    # caught by detect_hash_conflicts; this is a belt-and-braces for
    # cases where versions disagree).
    _fn_owners: Dict[str, set] = {}
    for _bid, _claims in by_builder.items():
        for _c in _claims:
            # PRESENCE_SKIP: an obsolete claim is not an asserted ownership
            # for uniqueness purposes — avoids noise once old files prune.
            if _c.get('claim_state') in _schema.PRESENCE_SKIP_CLAIM_STATES:
                continue
            _fn = _c.get('filename')
            if isinstance(_fn, str) and _fn:
                _fn_owners.setdefault(_fn, set()).add(_bid)
    for _fn, _bids in _fn_owners.items():
        if len(_bids) > 1:
            _findings.append(Finding(
                severity='WARN', kind='multi_owner_filename',
                message=(
                    f"filename {_fn} claimed by multiple builders: "
                    f"{', '.join(sorted(_bids))}"),
                filename=_fn,
            ))

    if not _findings:
        _findings.append(Finding(
            severity='INFO', kind='clean',
            message=(
                f"{sum(len(_v) for _v in by_builder.values())} verified "
                f"claims across {len(by_builder)} builder(s); "
                f"keyring has {len(keyring)} registered"),
        ))
    return Report.make('repo', _findings)


def check_federation_consistency(
    local_mirror_urls: 'List[str]', head: 'Optional[dict]',
) -> 'List[Finding]':
    """MIRROR-01 Phase 2 — compare local config's mirror URL set against
    coord-head.neighbours.  Returns empty list iff they match (under
    canonical normalisation: trailing-slash + case + sort + dedup).

    A non-empty result = federation drift; the publish path SHOULD treat
    any CRITICAL finding as BLOCK and direct the operator at
    `mirror reconcile-neighbours`.

    Three modes:
      - Both sides canonicalize identically                  → []
      - Local has URLs the coord-head doesn't                → CRITICAL
        (missing_on_remote — peer's neighbours not propagated)
      - Coord-head has URLs the local config doesn't         → CRITICAL
        (extra_on_remote — local removed without propagation, OR a
        peer added something we never registered)

    `head=None` is treated as "no coord-head yet" (first-publish case)
    and returns []: the publish bootstrap initialises neighbours from
    local config.
    """
    if head is None:
        return []
    _expected = set(_schema.canonicalize_neighbours(local_mirror_urls))
    _actual = set(_schema.canonicalize_neighbours(
        head.get('neighbours') or []))
    _findings: 'List[Finding]' = []
    _missing = _expected - _actual
    _extra = _actual - _expected
    if _missing:
        _findings.append(Finding(
            severity='CRITICAL', kind='federation_missing_neighbour',
            message=(
                f"coord-head.neighbours is missing {len(_missing)} mirror(s) "
                f"that the local config has: {sorted(_missing)}.  "
                "Run `mirror reconcile-neighbours` to re-propagate."),
        ))
    if _extra:
        _findings.append(Finding(
            severity='CRITICAL', kind='federation_extra_neighbour',
            message=(
                f"coord-head.neighbours has {len(_extra)} mirror(s) that "
                f"the local config doesn't: {sorted(_extra)}.  Either "
                "`mirror add` to register them, or "
                "`mirror reconcile-neighbours` after `mirror remove`."),
        ))
    return _findings


def write_publish_halt(
    coord_dir: str, reason: str,
) -> None:
    """Drop the PUBLISH_HALT sentinel in `coord_dir` with `reason` as
    its content.  Future publish attempts read this and refuse."""
    _path = _os.path.join(coord_dir, _policy.PUBLISH_HALT_FILENAME)
    try:
        with open(_path, 'w', encoding='utf-8') as _fh:
            _fh.write(reason + '\n')
    except OSError as _e:
        logger.error(f"write_publish_halt: {_path}: {_e}")


def publish_halt_reason(coord_dir: str) -> Optional[str]:
    """Read PUBLISH_HALT sentinel content.  Returns the reason string
    if the sentinel exists; None if it doesn't (publish allowed).
    """
    _path = _os.path.join(coord_dir, _policy.PUBLISH_HALT_FILENAME)
    try:
        with open(_path, 'r', encoding='utf-8') as _fh:
            return _fh.read().strip()
    except FileNotFoundError:
        return None
    except OSError as _e:
        logger.warning(f"publish_halt_reason: {_path}: {_e}")
        return None
