"""COORD-01 publish transaction — sign + push the claim layer.

Two entry points:

  local_publish    sign pending claims into the local jsonl + bump
                   the local coord-head (offline / single-host).

  remote_publish   acquire remote flock → fetch remote coord state →
                   sanity-check (hash conflict, PUBLISH_HALT) →
                   sign+append pending claims → push jsonl → re-sign
                   coord-head with updated last_seqs → push coord-head
                   → release flock.

`remote_publish` does NOT push the .deb pool itself.  The operator
runs `repo publish ssh` (or `repo publish local`) — both already
exist — to land binaries.  This split lets coord/publish stay
focused on the sidecar tracking and the existing publish paths stay
backwards-compatible for non-coord operators.

The 11-step state machine called out in the plan lives here in
remote_publish().  Each step is a single function call; if any
returns failure, we log + release lock + report.  Crash recovery
is forward-only: re-running publish picks up where we left off
because claim writes are idempotent (same seq + same canonical
bytes = same line).
"""

import logging
import os
from typing import Callable, List, Optional, Tuple

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
) -> List[dict]:
    """Walk build.json records; for each phase=done / phase=tunneled
    output whose filename isn't already in this builder's live jsonl,
    create an UNSIGNED pending claim dict.

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
        and _c.get('claim_state') != _schema.CLAIM_STATE_RETRACTED
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
        _outputs = _rec.get('outputs') or []
        _hashes = _rec.get('output_hashes') or {}
        for _fn in _outputs:
            if _fn in _known:
                continue
            _sha = _hashes.get(_fn)
            if not isinstance(_sha, str) or not _sha:
                # Pre-coord legacy or skipped backfill — skip.  The
                # operator runs `repo repair backfill-hashes` first.
                continue
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
                republished_from=None,
            ))
    _pending.sort(key=lambda _c: (_c['package'], _c['filename']))
    return _pending


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


# ───────────────────────── pool-bootstrap path ─────────────────────────


def bootstrap_claims_from_pool(
    *,
    builder_id: str,
    config,
    private_key_path: str,
    public_key_path: str,
    snapshot_pin: str,
    get_sha256: Callable[[str], str],
) -> Tuple[int, int]:
    """One-shot recovery: walk repo/ for .deb/.udeb files, create a
    signed published claim for each one not already in our local
    jsonl.  Returns (created, skipped).

    Use this when build.json records lack outputs (e.g. legacy corpus
    pre-OBS-01 outputs tracking, or after a one-time .result→.build.json
    migration that didn't backfill outputs).  The claim's `package`
    field is derived from the .deb filename's prefix (`foo_1.0_amd64
    .deb` → `foo`).  `built_version` is parsed from the filename
    between the first '_' and the next '_'.

    Pre-flight: PUBLISH_HALT check, same as local_publish.
    """
    _halt = _reconcile.publish_halt_reason(config.dir_coord)
    if _halt is not None:
        logger.error(
            f"coord.publish.bootstrap: PUBLISH_HALT set ({_halt}); "
            f"refusing to publish")
        return (0, 0)
    _pool = _reconcile.scan_pool_files(config.dir_repo)
    _existing = _store.read_builder_claims(
        config.dir_coord_claims, builder_id, public_key_path)
    _known: set = {
        _c.get('filename') for _c in _existing
        if isinstance(_c.get('filename'), str)
        and _c.get('claim_state') != _schema.CLAIM_STATE_RETRACTED
    }
    _seq = _store.max_seq(config.dir_coord_claims, builder_id)
    _now = _utc_now()
    _created = 0
    _skipped = 0
    for _fn in sorted(_pool.keys()):
        if _fn in _known:
            continue
        _path = _pool[_fn]
        # Parse filename: <name>_<version>_<arch>.deb
        _parts = _fn.rsplit('.', 1)[0].split('_')
        if len(_parts) < 2:
            _skipped += 1
            continue
        _pkg = _parts[0]
        _ver = _parts[1] if len(_parts) >= 2 else ''
        _sha = get_sha256(_path)
        if not _sha:
            _skipped += 1
            continue
        try:
            _size = os.path.getsize(_path)
        except OSError:
            _size = 0
        _seq += 1
        _claim = _schema.new_claim(
            builder=builder_id, seq=_seq, package=_pkg,
            intended_version=_ver, built_version=_ver,
            filename=_fn, sha256=_sha, size=_size,
            snapshot=snapshot_pin, built_at=_now,
            claim_state=_schema.CLAIM_STATE_PUBLISHED,
        )
        try:
            _store.append_claim(
                config.dir_coord_claims, builder_id, _claim,
                private_key_path,
            )
            _created += 1
        except (OSError, ValueError) as _e:
            logger.warning(
                f"coord.publish.bootstrap: append failed for {_fn}: {_e}")
            _skipped += 1
    return (_created, _skipped)


# ───────────────────────── local publish ─────────────────────────


def local_publish(
    *,
    builder_id: str,
    config,
    private_key_path: str,
    public_key_path: str,
    snapshot_pin: str,
    read_build_record: Callable,
    get_sha256: Callable,
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
    )
    fill_sizes_from_pool(_pending, _pool)
    if not _pending:
        return (0, 0)
    _created = 0
    _skipped = 0
    _seq = _store.max_seq(config.dir_coord_claims, builder_id)
    for _claim in _pending:
        _seq += 1
        _claim['seq'] = _seq
        # local_publish flips straight to published — no remote handoff
        _claim['claim_state'] = _schema.CLAIM_STATE_PUBLISHED
        try:
            _store.append_claim(
                config.dir_coord_claims, builder_id, _claim,
                private_key_path,
            )
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
    get_sha256: Callable,
    local_mirror_urls: 'Optional[list]' = None,
    ssh_host: 'Optional[str]' = None,
    flock_path: str = '/var/lock/repo-coord.lock',
    flock_timeout: int = 60,
    ssh_key: 'Optional[str]' = None,
    pool_remote_spec: 'Optional[str]' = None,
    on_progress: 'Optional[Callable]' = None,
) -> Tuple[bool, str]:
    """11-step publish transaction (see module docstring).  Returns
    (ok, detail).  On failure detail explains the step that aborted.

    `inrelease_local_path` MUST be a local copy of the same InRelease
    that's published at the remote — typically rsync'd in the same
    operator step as `repo publish ssh`.  Used to compute the
    sha256 that the new coord-head pins.

    `local_mirror_urls` (MIRROR-01 Phase 3): the canonical federation
    URL set the local builder has configured.  When `None`, federation
    gate is BYPASSED (legacy path).  When provided:
      - if remote already has a coord-head → compare neighbours; BLOCK on diff
      - if remote has no coord-head (first publish) → BOOTSTRAP:
        upload our pubkey to <root>/keyring/builders/<id>.pub and
        initialise the new coord-head with neighbours = local_mirror_urls

    `pool_remote_spec` (MIRROR-01 Phase 3b): when set, the rsync target
    for the apt POOL root (sibling of the coord root on the mirror
    host).  For each pending claim whose .deb is on the local pool,
    push that .deb per-file BEFORE writing its claim.  Files that fail
    to push are dropped from the claim set — partial publishes converge
    on retry.  When `None`, the .deb-pool sync is skipped (legacy path:
    operator runs `repo publish ssh full` for the .debs out-of-band).

    `on_progress` callback (MIRROR-01 Phase 3b): invoked once per .deb
    push attempt as `on_progress(current, total, filename, ok)`.  Caller
    drives a tui.ProgressBar from this.  Optional; no-op when omitted.

    `ssh_host` is the host portion of the remote spec; defaulted from
    `remote_coord_spec` if not given (parses the `user@host:/path`
    form).  Required for flock acquisition.
    """
    # Step 0 — pre-flight: PUBLISH_HALT check
    _halt = _reconcile.publish_halt_reason(config.dir_coord)
    if _halt is not None:
        return False, f"PUBLISH_HALT set: {_halt}"

    if ssh_host is None and remote_coord_spec:
        if ':' in remote_coord_spec and '@' in remote_coord_spec.split(':', 1)[0]:
            ssh_host = remote_coord_spec.split(':', 1)[0]
        # MIRROR-01 Phase 3b: local-fs mirrors (file://, /abs/path) have
        # no ssh_host — flock-over-ssh is N/A.  Skip the lock step and
        # proceed; local-fs mirrors are dev/single-host workflows where
        # cross-host concurrency isn't a concern.

    # Step 1 — acquire remote flock (skipped when local-fs)
    _lock_proc = None
    if ssh_host:
        _lock_proc = _transport.remote_flock_acquire(
            ssh_host=ssh_host, lock_path=flock_path,
            timeout_sec=flock_timeout, ssh_key=ssh_key,
        )
        if _lock_proc is None:
            return False, f"failed to spawn flock SSH for {ssh_host}"

    try:
        # Step 2 — fetch remote coord tree (under lock)
        _fetched = config.dir_coord_fetched
        _ok, _detail = _transport.pull_remote_coord(
            local_dest=_fetched, remote_spec=remote_coord_spec,
            ssh_key=ssh_key,
        )
        if not _ok:
            return False, f"pull failed: {_detail}"

        # Step 3 — verify coord-head, build keyring + claim view
        import signing
        _signing_home = signing.signing_home(config)
        _head_dict = _head.read_coord_head(_fetched, _signing_home)
        _keyring_dir = os.path.join(_fetched, 'keyring', 'builders')
        _keyring = _identity.load_keyring(_keyring_dir)
        _revoked = (_head_dict or {}).get('revoked_builders') or {}
        _by_builder = _store.read_all_claims(
            os.path.join(_fetched, 'claims'), _keyring, _revoked)

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

        # Step 4 — hash conflict detection
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
            and _c.get('claim_state') != _schema.CLAIM_STATE_RETRACTED
        }
        _pending = generate_pending_claims(
            builder_id=builder_id,
            buildlog_dir=_buildlog,
            claims_dir=config.dir_coord_claims,
            public_key_path=public_key_path,
            snapshot_pin=snapshot_pin,
            read_build_record=read_build_record,
        )
        _pending = [_p for _p in _pending if _p['filename'] not in _remote_known]
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

        # Step 6 — sign + append every pending claim to the LOCAL jsonl
        # (state=published; the .deb is now on the remote pool either
        # because step 5b just pushed it or the operator ran the legacy
        # `repo publish ssh full` before this command)
        _seq = _store.max_seq(config.dir_coord_claims, builder_id)
        _appended = 0
        for _claim in _pending:
            _seq += 1
            _claim['seq'] = _seq
            _claim['claim_state'] = _schema.CLAIM_STATE_PUBLISHED
            try:
                _store.append_claim(
                    config.dir_coord_claims, builder_id, _claim,
                    private_key_path,
                )
                _appended += 1
            except (OSError, ValueError) as _e:
                logger.warning(
                    f"coord.publish: local append failed for "
                    f"{_claim.get('package')}: {_e}")

        # Step 7 — push the updated jsonl to the remote
        _local_jsonl = _store.claims_path(
            config.dir_coord_claims, builder_id)
        if os.path.isfile(_local_jsonl):
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
        # MIRROR-01 Phase 2/3: neighbours sourcing.
        #   - bootstrap (first publish, no prior head) → from local config
        #   - subsequent publishes → preserve from the fetched head
        # Publish itself never adds or removes peers in steady state;
        # `mirror add` / `mirror remove` / `mirror reconcile-neighbours`
        # is the operator-driven membership-change path.
        if _is_bootstrap and local_mirror_urls is not None:
            _neighbours = local_mirror_urls
        else:
            _neighbours = (_head_dict or {}).get('neighbours') or []
        _new_head = _schema.new_coord_head(
            inrelease_sha256=_ir_sha,
            snapshot=_ss,
            last_seqs=_last_seqs,
            head_time=_utc_now(),
            neighbours=_neighbours,
            revoked_builders=(_head_dict or {}).get('revoked_builders'),
        )
        _ok = _head.write_coord_head(
            config.dir_coord, _new_head, _signing_home)
        if not _ok:
            return False, "coord-head write/sign failed"

        # Step 9 — push the new coord-head to the remote
        _ok, _detail = _transport.push_coord_head(
            local_coord_dir=config.dir_coord,
            remote_dir_spec=remote_coord_spec.rstrip('/') + '/',
            ssh_key=ssh_key,
        )
        if not _ok:
            return False, f"push coord-head failed: {_detail}"

        _push_summary = (
            f"; pushed {_pushed_count} .deb(s)"
            + (f" ({_push_fail_count} failed)" if _push_fail_count else "")
            if pool_remote_spec is not None else "")
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
        if _c.get('claim_state') != _schema.CLAIM_STATE_RETRACTED
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
