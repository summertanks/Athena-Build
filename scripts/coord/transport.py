"""Transport — SSH/rsync fetch + push for the coord tree.

The shared sidecar lives at the publish host's `repo-coord/` tree
(siblings of `repo/` — never served over HTTP).  Builders interact
with it via two operations:

  pull (P2)  fetch remote `repo-coord/` → local `coord/fetched/`,
             verify signatures + freshness, report deltas.
  push (P3)  rsync local `<builder-id>.jsonl` → remote claims/
             rsync local coord-head + .sig → remote root
             (always under remote flock acquired by coord.publish)

Remote spec format mirrors rsync's native syntax — either:
  /local/path                    local filesystem
  user@host:/remote/path         SSH target
  rsync://host/path              rsync daemon (not used in v1)

We pass through to rsync verbatim; OpenSSH's auth config (key paths,
agent forwarding, etc.) is the operator's responsibility.

Output:  rsync invocations log their stderr tail on failure; exit
codes propagate.  No fancy progress bars at this layer (publish path
in cmd_repo_publish has its own ProgressBar that wraps this).
"""

import hashlib
import logging
import os
import shlex
import subprocess
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger('athena')


# Standard rsync flags for the coord tree.  -aH preserves modes +
# hardlinks (not strictly needed for jsonl/json files, but harmless
# and consistent with the existing publish path).  --delete is OPT-IN
# — pulls accumulate by default so a transient remote-file removal
# can't silently scrub our local fetched/ copy.
# --mkpath: rsync 3.2.3+ creates the destination's missing parent
# dirs.  Needed by push_jsonl and push_single_deb on first publish —
# `<remote-coord>/keyring/builders/`, `<remote-coord>/claims/`, and the
# `<pool>/dists/<codename>/<comp>/binary-<arch>/` subtree don't exist on
# a freshly-prepared mirror endpoint (probe_remote_writable creates the
# coord root and pool root but not their subdirs).  No-op for the three
# other callsites in this module (pull_remote_coord, pull_single_file,
# push_coord_head) — their destination parents are pre-created by
# os.makedirs or by probe_remote_writable.
_RSYNC_BASE: List[str] = ['rsync', '-aH', '--mkpath', '--info=stats1']


def _ssh_arg(ssh_key: 'Optional[str]') -> 'Optional[List[str]]':
    """Build the `-e <ssh-cmd>` rsync argument when ssh_key is given.
    Returns None when rsync should run in default mode (no -e).
    """
    if not ssh_key:
        return None
    return ['-e', f"ssh -i {ssh_key} -o StrictHostKeyChecking=accept-new"]


def pull_remote_coord(
    *, local_dest: str, remote_spec: str,
    ssh_key: 'Optional[str]' = None,
) -> Tuple[bool, str]:
    """Fetch the remote coord tree to `local_dest`.

    `remote_spec` is rsync-native; pass either `/path` or
    `user@host:/path`.  Trailing slash on `remote_spec` is enforced
    (rsync's "copy contents of dir" mode) so the layout under
    `local_dest` mirrors the remote.

    Returns (ok, detail).  detail is the rsync stderr tail on failure.
    """
    os.makedirs(local_dest, mode=0o755, exist_ok=True)
    _src = remote_spec.rstrip('/') + '/'
    _argv = list(_RSYNC_BASE)
    _ssh = _ssh_arg(ssh_key)
    if _ssh is not None:
        _argv += _ssh
    _argv += [_src, local_dest.rstrip('/') + '/']
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        _tail = (_r.stderr or _r.stdout or '').strip().splitlines()[-5:]
        _detail = ' | '.join(_tail)
        logger.error(
            f"coord.transport.pull: rc={_r.returncode}: {_detail}")
        return False, _detail
    return True, ''


def _run_rsync_streaming(
    argv: 'List[str]', on_bytes: 'Callable[[int], None]',
) -> 'Tuple[int, str]':
    """Run rsync streaming its `--info=progress2` output, calling
    `on_bytes(cumulative)` with the running transferred-byte count parsed
    from each progress update.  stderr is merged into stdout so error text
    can't deadlock on a full pipe; non-numeric lines are kept as the
    failure tail.  Returns (returncode, detail-tail)."""
    try:
        _proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as _e:
        return 1, str(_e)
    _tail: 'List[str]' = []
    _buf = b''
    assert _proc.stdout is not None
    _fd = _proc.stdout.fileno()
    while True:
        try:
            _chunk = os.read(_fd, 4096)
        except OSError:
            break
        if not _chunk:
            break
        # progress2 redraws the same line with \r; the final stats line
        # ends with \n.  Normalise both to \n, parse complete lines, keep
        # the incomplete tail for the next read.
        _buf += _chunk.replace(b'\r', b'\n')
        *_lines, _buf = _buf.split(b'\n')
        for _ln in _lines:
            _s = _ln.strip()
            if not _s:
                continue
            _first = _s.split()[0].replace(b',', b'')
            if _first.isdigit():
                try:
                    on_bytes(int(_first))
                except Exception:
                    pass
            else:
                _tail.append(_s.decode('utf-8', 'replace'))
    _proc.wait()
    # Flush the residual tail: rsync's final error line may not be newline-
    # terminated, so it sits in _buf and was dropped from the failure tail.
    _resid = _buf.strip()
    if _resid:
        _first = _resid.split()[0].replace(b',', b'')
        if not _first.isdigit():
            _tail.append(_resid.decode('utf-8', 'replace'))
    return _proc.returncode, ' | '.join(_tail[-5:])


def push_single_deb(
    *, local_path: str, remote_spec: str,
    ssh_key: 'Optional[str]' = None,
    overwrite: bool = False,
    on_bytes: 'Optional[Callable[[int], None]]' = None,
) -> Tuple[bool, str]:
    """Rsync one local `.deb` (or `.udeb`) → `remote_spec` (a remote FILE
    path, not a directory).

    Uses `--size-only` so a re-publish skips an unchanged file by SIZE
    (the cheap stat — .deb filenames are immutable per (pkg, version,
    arch), so a same-name same-size remote file IS the same bytes), while
    a TRUNCATED / partial remote file (a prior push that died mid-flight
    leaves a wrong-size file) is re-sent rather than silently trusted by
    name.  `--ignore-existing` would skip such a file forever, stranding a
    claim whose pinned sha the remote pool can't serve.  Same-size byte
    corruption is the residual (rare external corruption — caught by a
    peer's pull-side sha verify and `mirror audit`).  Single-file
    semantics so Phase 3b can tick a ProgressBar one notch per .deb and
    report failures precisely.

    overwrite=True: drop --size-only so the
    transfer REPLACES the remote bytes — used exclusively for reclaim
    claims, the sanctioned exception to filename immutability (the
    remote file exists by definition and skipping it would publish a
    claim whose bytes never shipped).
    """
    if not os.path.isfile(local_path):
        return False, f"local file missing: {local_path}"
    _argv = list(_RSYNC_BASE)
    if not overwrite:
        # --size-only (NOT --ignore-existing): skip a remote file only when its
        # size matches, so a correct immutable .deb still skips transfer but a
        # wrong-size (truncated/partial) remote file is healed by re-sending.
        _argv += ['--size-only']
    _ssh = _ssh_arg(ssh_key)
    if _ssh is not None:
        _argv += _ssh
    # on_bytes wants live byte progress (large-file pushes — ISOs); stream
    # --info=progress2.  Without it, keep the cheap blocking run (per-file
    # ticks at the .deb push layer don't need byte granularity).
    if on_bytes is not None:
        _argv += ['--info=progress2']
        _argv += [local_path, remote_spec]
        _rc, _detail = _run_rsync_streaming(_argv, on_bytes)
        if _rc != 0:
            logger.error(
                f"coord.transport.push_single_deb: rc={_rc}: {_detail}")
            return False, _detail
        return True, ''
    _argv += [local_path, remote_spec]
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        _tail = (_r.stderr or _r.stdout or '').strip().splitlines()[-5:]
        _detail = ' | '.join(_tail)
        logger.error(
            f"coord.transport.push_single_deb: rc={_r.returncode}: {_detail}")
        return False, _detail
    return True, ''


def remote_sha256(
    remote_spec: str, ssh_key: 'Optional[str]' = None,
) -> 'Optional[str]':
    """sha256 hex of a remote file via ``ssh <host> sha256sum <path>``.

    ``remote_spec`` is the rsync-style ``user@host:path``.  Returns the digest,
    or None on any failure (unparseable spec, missing file, ssh/timeout error)
    so callers can distinguish "verified mismatch" (a real digest that differs)
    from "could not verify" (None).  Used by the release-asset push to confirm
    the bytes that landed on the mirror match what releases.json advertises —
    catching a same-name file whose bytes drifted without changing size (which
    --size-only skips) — e.g. the qcow2 — or
    a transfer truncated mid-flight over a slow link."""
    if ':' not in remote_spec:
        return None
    _hostpart, _path = remote_spec.split(':', 1)
    _argv = ['ssh']
    if ssh_key:
        _argv += ['-i', ssh_key]
    _argv += ['-o', 'StrictHostKeyChecking=accept-new', '-o', 'BatchMode=yes',
              _hostpart, 'sha256sum', '--', _path]
    try:
        _r = subprocess.run(_argv, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as _e:
        logger.error(f"coord.transport.remote_sha256({remote_spec}): {_e}")
        return None
    if _r.returncode != 0:
        return None
    _parts = (_r.stdout or '').strip().split()
    return _parts[0] if _parts and len(_parts[0]) == 64 else None


def _is_sha256(s: str) -> bool:
    return len(s) == 64 and all(_c in '0123456789abcdef' for _c in s.lower())


def remote_pubkey_state(
    *, spec: str, ssh_key: 'Optional[str]' = None,
) -> 'Tuple[str, str]':
    """TOFU probe of an existing keyring pubkey BEFORE `mirror builders
    register` would overwrite it (FED-03 step B).  Unlike :func:`remote_sha256`
    (whose ``None`` conflates "absent" with "ssh failed"), this DISTINGUISHES:

        ('present', <sha256-hex>)  — a key is already registered for this id
        ('absent',  '')            — no key yet (safe to register)
        ('error',   <reason>)      — could NOT determine (ssh down / bad output);
                                     the caller MUST NOT blind-overwrite.

    ``spec`` is the rsync-style destination of the pubkey — a local path
    (file:// mirror) or ``user@host:path``."""
    if ':' not in spec:                        # local-fs mirror
        if not os.path.isfile(spec):
            return ('absent', '')
        try:
            with open(spec, 'rb') as _fh:
                return ('present', hashlib.sha256(_fh.read()).hexdigest())
        except OSError as _e:
            return ('error', str(_e))
    _host, _, _path = spec.partition(':')
    # One ssh round-trip: print the sha, or a sentinel when the file is absent.
    _probe = (f'if [ -f {shlex.quote(_path)} ]; then '
              f'sha256sum {shlex.quote(_path)} | cut -d" " -f1; '
              f'else echo __ABSENT__; fi')
    _cmd = ['ssh']
    if ssh_key:
        _cmd += ['-i', ssh_key]
    _cmd += ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
             _host, _probe]
    try:
        _r = subprocess.run(_cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as _e:
        return ('error', str(_e))
    if _r.returncode != 0:
        return ('error', (_r.stderr or 'ssh failed').strip() or 'ssh failed')
    _out = (_r.stdout or '').strip()
    if _out == '__ABSENT__':
        return ('absent', '')
    if _is_sha256(_out):
        return ('present', _out.lower())
    return ('error', f'unexpected probe output: {_out!r}')


def list_remote_debs(
    pool_remote_spec: str, ssh_key: 'Optional[str]' = None,
) -> 'Optional[set]':
    """Set of .deb/.udeb paths under the remote pool root, RELATIVE to that
    root (e.g. ``dists/thor/main/binary-amd64/foo.deb``) — matching the local
    relpaths from ``config.dir_repo``.  ``pool_remote_spec`` is the rsync-style
    ``user@host:poolroot``.  Returns None on failure so the pool-completeness
    check can degrade to a warning rather than a false "all present".

    Used by mirror publish to confirm every .deb the pushed Packages index
    references is actually on the mirror — a per-file push that failed dropped
    its CLAIM but left the .deb in the local index, so the published index can
    otherwise promise files that 404."""
    if ':' not in pool_remote_spec:
        return None
    _host, _path = pool_remote_spec.split(':', 1)
    _path = _path.rstrip('/') or '.'
    # MAT-02: source pool artifacts are claimed like binaries — the
    # listing must see them or every source claim audits missing_on_disk
    # (2026-07-13, first post-source publish).  Restricted to source/
    # dirs via the path glob so index files never match.
    _find = (f"cd {shlex.quote(_path)} && "
             "find dists -type f \\( -name '*.deb' -o -name '*.udeb' "
             "-o -path '*/source/*.dsc' -o -path '*/source/*.tar.*' "
             "-o -path '*/source/*.diff.gz' -o -path '*/source/*.asc' \\)")
    _argv = ['ssh']
    if ssh_key:
        _argv += ['-i', ssh_key]
    _argv += ['-o', 'StrictHostKeyChecking=accept-new', '-o', 'BatchMode=yes',
              _host, _find]
    try:
        _r = subprocess.run(_argv, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as _e:
        logger.error(f"coord.transport.list_remote_debs({pool_remote_spec}): {_e}")
        return None
    if _r.returncode != 0:
        logger.error(
            f"coord.transport.list_remote_debs: rc={_r.returncode}: "
            f"{(_r.stderr or '').strip()[:200]}")
        return None
    return {_ln.strip() for _ln in (_r.stdout or '').splitlines() if _ln.strip()}


def pull_single_file(
    *, remote_spec: str, local_path: str,
    ssh_key: 'Optional[str]' = None,
) -> Tuple[bool, str]:
    """Rsync one remote file → `local_path`.  `remote_spec` MUST point to
    the source FILE (with filename), not a directory.

    Used by Phase 3 `mirror pull` for per-`.deb` downloads:
    one rsync invocation per file so progress can be ticked, individual
    file failures can be surfaced precisely, and a hash mismatch on
    one file aborts only that file (not the whole batch).
    """
    _argv = list(_RSYNC_BASE)
    _ssh = _ssh_arg(ssh_key)
    if _ssh is not None:
        _argv += _ssh
    _argv += [remote_spec, local_path]
    os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        _tail = (_r.stderr or _r.stdout or '').strip().splitlines()[-5:]
        _detail = ' | '.join(_tail)
        logger.error(
            f"coord.transport.pull_single_file: rc={_r.returncode}: {_detail}")
        return False, _detail
    return True, ''


def fetch_pool_file(
    *, remote_file_spec: str, dest_path: str,
    ssh_key: 'Optional[str]' = None,
) -> Tuple[bool, str]:
    """COMP-04 chunk C peer-fetch primitive: rsync ONE pool file from a
    peer repo (e.g. the primary's binary-amd64/ dir) to a local path.
    `remote_file_spec` points at the remote FILE.  Caller verifies the
    sha256 against the signed claim afterwards — transport carries no
    trust."""
    _dest_dir = os.path.dirname(dest_path)
    if _dest_dir:
        os.makedirs(_dest_dir, exist_ok=True)
    _argv = list(_RSYNC_BASE)
    _ssh = _ssh_arg(ssh_key)
    if _ssh is not None:
        _argv += _ssh
    _argv += [remote_file_spec, dest_path]
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        return False, (_r.stderr or _r.stdout or 'rsync failed').strip()[:300]
    return True, ''


def push_jsonl(
    *, local_path: str, remote_spec: str, ssh_key: 'Optional[str]' = None,
) -> Tuple[bool, str]:
    """Rsync one local <builder-id>.jsonl to the remote claims/ dir.

    `remote_spec` MUST point to the destination FILE (not the dir),
    e.g. `user@host:/srv/repo-coord/claims/acme-builder.jsonl`.
    This is the safest contract — if the builder mistakenly points at
    a directory, rsync's copy semantics would put the file *inside*
    that dir (and another builder's jsonl could land at a peer's
    expected path).
    """
    if not os.path.isfile(local_path):
        return False, f"local jsonl missing: {local_path}"
    _argv = list(_RSYNC_BASE)
    _ssh = _ssh_arg(ssh_key)
    if _ssh is not None:
        _argv += _ssh
    _argv += [local_path, remote_spec]
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        _tail = (_r.stderr or _r.stdout or '').strip().splitlines()[-5:]
        _detail = ' | '.join(_tail)
        logger.error(
            f"coord.transport.push_jsonl: rc={_r.returncode}: {_detail}")
        return False, _detail
    return True, ''


def push_dist_tree(
    *, local_dist_dir: str, remote_dir_spec: str,
    ssh_key: 'Optional[str]' = None,
) -> Tuple[bool, str]:
    """Rsync the entire ``repo/dists/<codename>/`` subtree (Release,
    InRelease, Release.gpg, per-component Packages + compressed
    variants, Sources, by-hash dirs) to the remote pool root's
    matching path.

    Without this the remote pool has the ``.deb``s under ``pool/...``
    but no apt-trust path — apt clients can't fetch InRelease, can't
    verify the Packages index, and can't resolve any pkg.  Mirror
    audit (gap #1) flags it as ``inrelease_unreachable``.

    `local_dist_dir` is the absolute path to the local
    ``repo/dists/<codename>/`` directory.  `remote_dir_spec` is the
    rsync target for the SAME ``dists/<codename>/`` path on the
    remote — caller composes from ``pool_remote_spec`` +
    ``dists/<codename>``.

    Idempotent (rsync -aH); files already on the remote with matching
    mtimes are skipped.  Uses ``--delete`` so stale per-arch dirs
    from a removed component don't accumulate on the remote.

    This pass syncs INDEX files only (Release/InRelease/Packages/
    Sources/by-hash).  Pool ``.deb``/``.udeb`` artifacts — which live
    inside dists/<codename>/ since Stage D — are owned solely
    by ``push_single_deb`` (additive, ``--size-only``, or
    ``overwrite=True`` for a sanctioned reclaim) and are EXCLUDED here.
    """
    if not os.path.isdir(local_dist_dir):
        return False, (
            f"local dist dir {local_dist_dir} does not exist — "
            "run `repo index` (or let `mirror publish` auto-index) "
            "before pushing")
    _src = local_dist_dir.rstrip('/') + '/'
    _dst = remote_dir_spec.rstrip('/') + '/'
    # EXCLUDE pool artifacts (not merely `--filter=P` protect).
    # The pool .debs live INSIDE dists/<codename>/, so
    # this whole-subtree rsync would otherwise touch them.  A `--filter=P`
    # only stops receiver-side DELETION — rsync -aH still TRANSFERS/
    # overwrites any .deb whose size/mtime differs, so a local-ahead
    # rebuild (same filename, new bytes) silently rewrote the FROZEN
    # remote bytes on every publish, with no reclaim claim (the whole
    # invariant bypassed; masked). `--exclude` removes
    # pool artifacts from the transfer set entirely AND (since they're
    # excluded, not just protected) keeps `--delete` from reaping them —
    # so this pass never overwrites NOR deletes a pool file.  Pool bytes
    # are pushed only by push_single_deb; pruning stays an explicit
    # operator action (publish-before-prune). `--delete` still
    # reaps stale index files + removed-component dirs.
    _argv = list(_RSYNC_BASE) + [
        '--delete',
        '--exclude=*.deb',
        '--exclude=*.udeb',
        # MAT-02: SOURCE pool artifacts live inside dists/<codename>/
        # <comp>/source/ — same append-only discipline as the .debs
        # (pushed per-claim, never transferred/deleted by the dist-tree
        # pass).  Index files (Sources, Sources.gz/.xz) match none of
        # these patterns and still sync + reap normally.
        '--exclude=*.dsc',
        '--exclude=*.tar.*',
        '--exclude=*.diff.gz',
        '--exclude=*.asc',
    ]
    _ssh = _ssh_arg(ssh_key)
    if _ssh is not None:
        _argv += _ssh
    _argv += [_src, _dst]
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        _tail = (_r.stderr or _r.stdout or '').strip().splitlines()[-5:]
        _detail = ' | '.join(_tail)
        logger.error(
            f"coord.transport.push_dist_tree: rc={_r.returncode}: {_detail}")
        return False, _detail
    return True, ''


def push_coord_head(
    *, local_coord_dir: str, remote_dir_spec: str,
    ssh_key: 'Optional[str]' = None,
) -> Tuple[bool, str]:
    """Rsync local coord-head.json + .sig to the remote coord root.

    `remote_dir_spec` points to the remote DIRECTORY (e.g.
    `user@host:/srv/repo-coord/`).  Trailing slash enforced so rsync
    treats it as "copy these files into that dir".
    """
    from . import head as _head
    _json_p = _head.coord_head_path(local_coord_dir)
    _sig_p = _head.coord_head_sig_path(local_coord_dir)
    if not (os.path.isfile(_json_p) and os.path.isfile(_sig_p)):
        return False, (
            f"local coord-head + sig must both exist before push "
            f"(json={_json_p}, sig={_sig_p})")
    _dst = remote_dir_spec.rstrip('/') + '/'
    _argv = list(_RSYNC_BASE)
    _ssh = _ssh_arg(ssh_key)
    if _ssh is not None:
        _argv += _ssh
    _argv += [_json_p, _sig_p, _dst]
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        _tail = (_r.stderr or _r.stdout or '').strip().splitlines()[-5:]
        _detail = ' | '.join(_tail)
        logger.error(
            f"coord.transport.push_coord_head: rc={_r.returncode}: {_detail}")
        return False, _detail
    return True, ''


# ───────────────────────── flock helper (P3) ─────────────────────────

# FED-04: the lock holder touches its `<lock>.holder` sidecar every
# FLOCK_HEARTBEAT_SEC while alive, so a LIVE-but-slow publish (a multi-GB
# push over a poor link) keeps the mtime fresh.  A lock is treated as STALE
# only when it is still held AND its heartbeat is older than
# FLOCK_STALE_AGE_SEC (~4 missed beats) — that gap is what distinguishes a
# dead holder (power-failed builder) from a legitimately slow one.
FLOCK_HEARTBEAT_SEC = 15
FLOCK_STALE_AGE_SEC = 75


def _flock_hold_script(
    lock_path: str, builder_id: str, heartbeat_sec: int = FLOCK_HEARTBEAT_SEC,
) -> str:
    """The shell run INSIDE ``flock -c`` on the mirror for the lifetime of
    the lock.  Records the builder-id and the holder PID in sidecars, starts
    a background heartbeat that touches ``<lock>.holder`` every
    ``heartbeat_sec``, then blocks on ``cat`` (released when we close stdin).
    On clean exit the heartbeat is killed and both sidecars removed.

    builder_id is validated (no shell metacharacters) at `mirror init`, so
    direct interpolation is safe."""
    return (
        f"echo {builder_id} > {lock_path}.holder; "
        f"echo $$ > {lock_path}.pid; "
        f"( while :; do sleep {int(heartbeat_sec)}; "
        f"touch {lock_path}.holder 2>/dev/null || break; done ) & "
        f"_hb=$!; "
        f"echo COORD_LOCK_ACQUIRED; cat; "
        f"kill $_hb 2>/dev/null; rm -f {lock_path}.holder {lock_path}.pid")


def _flock_status_script(lock_path: str) -> str:
    """One-shot remote probe of the publish lock — emits KEY=VALUE lines
    parsed by :func:`_parse_flock_status`.  ``flock -n … -c true`` succeeds
    (HELD=0) only when the lock is FREE, so a held lock reports HELD=1.  AGE
    is the heartbeat sidecar's age in seconds (huge when absent)."""
    return (
        f"if flock -n {lock_path} -c true 2>/dev/null; then echo HELD=0; "
        f"else echo HELD=1; fi; "
        f"echo HOLDER=$(cat {lock_path}.holder 2>/dev/null); "
        f"echo PID=$(cat {lock_path}.pid 2>/dev/null); "
        f"_m=$(stat -c %Y {lock_path}.holder 2>/dev/null || echo 0); "
        f"echo AGE=$(( $(date +%s) - _m ))")


def _flock_break_script(lock_path: str) -> str:
    """Break a stale lock: SIGKILL the recorded holder PID (releasing the
    flock when that shell dies), fall back to ``fuser -k`` where available,
    then clear both sidecars."""
    return (
        f"_p=$(cat {lock_path}.pid 2>/dev/null); "
        f"if [ -n \"$_p\" ]; then kill -9 \"$_p\" 2>/dev/null; fi; "
        f"command -v fuser >/dev/null 2>&1 && fuser -k {lock_path} 2>/dev/null; "
        f"rm -f {lock_path}.holder {lock_path}.pid; echo COORD_LOCK_BROKEN")


def _parse_flock_status(stdout: str) -> 'Dict[str, object]':
    """Parse :func:`_flock_status_script` output into
    ``{held: bool, holder: str|None, pid: str|None, age_sec: int|None}``."""
    _d: 'Dict[str, str]' = {}
    for _line in (stdout or '').splitlines():
        if '=' in _line:
            _k, _, _v = _line.partition('=')
            _d[_k.strip()] = _v.strip()
    try:
        _age: 'Optional[int]' = int(_d['AGE'])
    except (KeyError, ValueError):
        _age = None
    return {
        'held':    _d.get('HELD') == '1',
        'holder':  _d.get('HOLDER') or None,
        'pid':     _d.get('PID') or None,
        'age_sec': _age,
    }


def lock_is_stale(
    status: 'Optional[Dict[str, object]]',
    stale_age_sec: int = FLOCK_STALE_AGE_SEC,
) -> bool:
    """True when a lock status reports HELD and its heartbeat is older than
    ``stale_age_sec`` — the signal that the holder died mid-publish.  A
    live-but-slow holder keeps the heartbeat fresh, so this never fires on a
    legitimate long push."""
    if not status or not status.get('held'):
        return False
    _age = status.get('age_sec')
    return isinstance(_age, int) and _age > stale_age_sec


def remote_flock_lock_status(
    *, ssh_host: str, lock_path: str, ssh_key: 'Optional[str]' = None,
) -> 'Optional[Dict[str, object]]':
    """Probe whether the publish lock is held, by whom, and its heartbeat
    age.  Returns the parsed status dict, or None if the probe SSH failed."""
    _ssh_cmd = ['ssh']
    if ssh_key:
        _ssh_cmd += ['-i', ssh_key]
    _ssh_cmd += ['-o', 'StrictHostKeyChecking=accept-new', ssh_host,
                 _flock_status_script(lock_path)]
    try:
        _r = subprocess.run(_ssh_cmd, capture_output=True, text=True,
                            timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if _r.returncode != 0 and not (_r.stdout or '').strip():
        return None
    return _parse_flock_status(_r.stdout or '')


def remote_flock_break(
    *, ssh_host: str, lock_path: str, ssh_key: 'Optional[str]' = None,
) -> 'Tuple[bool, str]':
    """Forcibly break the publish lock (kill the holder + clear sidecars).
    Caller MUST confirm staleness first — this is the explicit
    `mirror unlock` recovery path, never automatic."""
    _ssh_cmd = ['ssh']
    if ssh_key:
        _ssh_cmd += ['-i', ssh_key]
    _ssh_cmd += ['-o', 'StrictHostKeyChecking=accept-new', ssh_host,
                 _flock_break_script(lock_path)]
    try:
        _r = subprocess.run(_ssh_cmd, capture_output=True, text=True,
                            timeout=30)
    except (OSError, subprocess.SubprocessError) as _e:
        return False, f"break SSH failed: {_e}"
    if 'COORD_LOCK_BROKEN' in (_r.stdout or ''):
        return True, "lock broken (holder killed, sidecars cleared)"
    return False, (_r.stderr or _r.stdout or 'break command produced no output').strip()


def remote_flock_holder(
    *, ssh_host: str, lock_path: str, ssh_key: 'Optional[str]' = None,
) -> 'Optional[str]':
    """Best-effort: read the builder-id currently (or last) holding the
    publish lock, from the `<lock>.holder` sidecar written by
    remote_flock_acquire.  Returns None if unreadable/absent.  May be stale
    if a holder's SSH session died (flock auto-releases but the sidecar
    lingers) — informational only, never a correctness gate."""
    _ssh_cmd = ['ssh']
    if ssh_key:
        _ssh_cmd += ['-i', ssh_key]
    _ssh_cmd += ['-o', 'StrictHostKeyChecking=accept-new', ssh_host,
                 f'cat {lock_path}.holder 2>/dev/null']
    try:
        _r = subprocess.run(_ssh_cmd, capture_output=True, text=True,
                            timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    _id = (_r.stdout or '').strip()
    return _id or None


def remote_flock_acquire(
    *, ssh_host: str, lock_path: str, timeout_sec: int = 60,
    ssh_key: 'Optional[str]' = None, builder_id: 'Optional[str]' = None,
) -> 'Optional[subprocess.Popen]':
    """Open an SSH session that holds `lock_path` via flock(1) for the
    duration of the session.  Returns the Popen handle ONLY once the lock
    is CONFIRMED held — caller must .terminate() / .wait() to release.
    Returns None if the lock can't be acquired (spawn failure, `flock -w`
    timeout because a peer holds it, or the SSH session dies).

    The flock'd shell echoes `COORD_LOCK_ACQUIRED` BEFORE its
    blocking `cat`, and we block reading stdout for that token (bounded by
    `flock -w` + a margin) before returning.  Previously the Popen was
    returned immediately and no caller read it, so publish raced ahead
    while flock was still WAITING, and proceeded UNLOCKED after a timeout —
    the lock was decorative.
    """
    import select as _select
    _ssh_cmd = ['ssh']
    if ssh_key:
        _ssh_cmd += ['-i', ssh_key]
    _ssh_cmd += ['-o', 'StrictHostKeyChecking=accept-new', ssh_host]
    # flock -w blocks up to <secs> waiting; on success it runs `-c`, which
    # announces the lock is held (ACQUIRED) then `cat` holds it open until
    # we close stdin (release).  On timeout flock exits non-zero, the `&&`
    # short-circuits, the shell exits → stdout EOF with no token.
    # When we know our builder-id, record it in a `<lock>.holder` sidecar
    # WHILE the lock is held so a blocked peer can be told who's publishing;
    # remove it on clean release.  builder_id is validated (no shell
    # metacharacters) at `mirror init`, so direct interpolation is safe.
    if builder_id:
        _hold = _flock_hold_script(lock_path, builder_id)
    else:
        _hold = "echo COORD_LOCK_ACQUIRED; cat"
    _inner = (
        f"flock -w {int(timeout_sec)} {lock_path} -c "
        f"'{_hold}' "
        f"&& echo COORD_LOCK_RELEASED")
    _ssh_cmd.append(_inner)
    try:
        _proc = subprocess.Popen(
            _ssh_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as _e:
        logger.error(f"coord.transport.flock: spawn failed: {_e}")
        return None
    # Block until the ACQUIRED token arrives or the wait elapses.  flock's
    # own `-w` bounds the lock wait; add a margin for ssh round-trip.
    _deadline = float(int(timeout_sec)) + 10.0
    _stdout = _proc.stdout
    try:
        assert _stdout is not None  # stdout=PIPE above
        _ready, _, _ = _select.select([_stdout], [], [], _deadline)
        _line = _stdout.readline() if _ready else ''
    except (OSError, ValueError) as _e:
        logger.error(f"coord.transport.flock: read failed: {_e}")
        _line = ''
    if 'COORD_LOCK_ACQUIRED' not in (_line or ''):
        # Timeout / busy / dead session — never hold a phantom lock.
        logger.error(
            f"coord.transport.flock: lock NOT acquired on {ssh_host} "
            f"({lock_path}) within {timeout_sec}s (held by a peer?)")
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        return None
    return _proc


def remote_flock_release(proc: subprocess.Popen) -> None:
    """Release the lock by closing the SSH session.  The shell's
    `cat` receives EOF and exits, flock(1) cleans up."""
    if proc is None:
        return
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except OSError as _e:
        logger.warning(f"coord.transport.flock_release: {_e}")
