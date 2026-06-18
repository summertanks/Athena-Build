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

import logging
import os
import subprocess
from typing import Callable, List, Optional, Tuple

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
    return _proc.returncode, ' | '.join(_tail[-5:])


def push_single_deb(
    *, local_path: str, remote_spec: str,
    ssh_key: 'Optional[str]' = None,
    overwrite: bool = False,
    on_bytes: 'Optional[Callable[[int], None]]' = None,
) -> Tuple[bool, str]:
    """Rsync one local `.deb` (or `.udeb`) → `remote_spec` (a remote FILE
    path, not a directory).

    Uses `--ignore-existing` so a re-publish never re-uploads an
    unchanged file by name — .deb filenames are immutable per (pkg,
    version, arch), so name-match is the right key.  Single-file
    semantics so MIRROR-01 Phase 3b can tick a ProgressBar one notch
    per .deb and report failures precisely.

    overwrite=True: drop --ignore-existing so the
    transfer REPLACES the remote bytes — used exclusively for reclaim
    claims, the sanctioned exception to filename immutability (the
    remote file exists by definition and skipping it would publish a
    claim whose bytes never shipped).
    """
    if not os.path.isfile(local_path):
        return False, f"local file missing: {local_path}"
    _argv = list(_RSYNC_BASE)
    if not overwrite:
        _argv += ['--ignore-existing']
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


def pull_single_file(
    *, remote_spec: str, local_path: str,
    ssh_key: 'Optional[str]' = None,
) -> Tuple[bool, str]:
    """Rsync one remote file → `local_path`.  `remote_spec` MUST point to
    the source FILE (with filename), not a directory.

    Used by MIRROR-01 Phase 3 `mirror pull` for per-`.deb` downloads:
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
    inside dists/<codename>/ since CONF-01 Stage D — are owned solely
    by ``push_single_deb`` (additive, ``--ignore-existing``, or
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
    # invariant bypassed; STA-47 masked).  `--exclude` removes
    # pool artifacts from the transfer set entirely AND (since they're
    # excluded, not just protected) keeps `--delete` from reaping them —
    # so this pass never overwrites NOR deletes a pool file.  Pool bytes
    # are pushed only by push_single_deb; pruning stays an explicit
    # operator action (UPD-01 publish-before-prune).  `--delete` still
    # reaps stale index files + removed-component dirs.
    _argv = list(_RSYNC_BASE) + [
        '--delete',
        '--exclude=*.deb',
        '--exclude=*.udeb',
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
        _hold = (f"echo {builder_id} > {lock_path}.holder; "
                 f"echo COORD_LOCK_ACQUIRED; cat; rm -f {lock_path}.holder")
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
