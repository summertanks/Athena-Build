"""COORD-01 transport — SSH/rsync fetch + push for the coord tree.

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
from typing import List, Optional, Tuple

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


def push_single_deb(
    *, local_path: str, remote_spec: str,
    ssh_key: 'Optional[str]' = None,
) -> Tuple[bool, str]:
    """Rsync one local `.deb` (or `.udeb`) → `remote_spec` (a remote FILE
    path, not a directory).

    Uses `--ignore-existing` so a re-publish never re-uploads an
    unchanged file by name — .deb filenames are immutable per (pkg,
    version, arch), so name-match is the right key.  Single-file
    semantics so MIRROR-01 Phase 3b can tick a ProgressBar one notch
    per .deb and report failures precisely.
    """
    if not os.path.isfile(local_path):
        return False, f"local file missing: {local_path}"
    _argv = list(_RSYNC_BASE)
    _argv += ['--ignore-existing']
    _ssh = _ssh_arg(ssh_key)
    if _ssh is not None:
        _argv += _ssh
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
    """
    if not os.path.isdir(local_dist_dir):
        return False, (
            f"local dist dir {local_dist_dir} does not exist — "
            "run `repo index` (or let `mirror publish` auto-index) "
            "before pushing")
    _src = local_dist_dir.rstrip('/') + '/'
    _dst = remote_dir_spec.rstrip('/') + '/'
    # Append-only pool guard (2026-06-11): the pool .debs live INSIDE
    # dists/<codename>/ (CONF-01 Stage D — this docstring's "under
    # pool/..." predates that), so a bare --delete mirrors any local
    # prune onto the remote: 17 obsolete/deprecated files vanished from
    # the append-only pool on publish.  Protect pool artifacts from
    # receiver-side deletion; --delete still reaps stale index files
    # and removed-component dirs.  Remote pruning, when it comes, is an
    # explicit operator action (UPD-01 publish-before-prune), never a
    # dist-tree push side effect.
    _argv = list(_RSYNC_BASE) + [
        '--delete',
        '--filter=P *.deb',
        '--filter=P *.udeb',
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


def remote_flock_acquire(
    *, ssh_host: str, lock_path: str, timeout_sec: int = 60,
    ssh_key: 'Optional[str]' = None,
) -> 'Optional[subprocess.Popen]':
    """Open an SSH session that holds `lock_path` via flock(1) for the
    duration of the session.  Returns the Popen handle — caller must
    .terminate() / .wait() to release the lock.

    Uses `flock -n -w <timeout>` so the wait is bounded; rc=0 means
    "got the lock", rc!=0 means timeout / file missing.  The inner
    shell `cat` blocks forever; closing stdin from our side via
    .terminate() releases flock as the shell exits.

    Lands in P3 with the publish state machine — this is just the
    primitive, exercised in tests.
    """
    _ssh_cmd = ['ssh']
    if ssh_key:
        _ssh_cmd += ['-i', ssh_key]
    _ssh_cmd += ['-o', 'StrictHostKeyChecking=accept-new', ssh_host]
    # flock -w blocks up to <secs> waiting; -n returns immediately if
    # busy.  Combine: try non-blocking; if that fails, fall back to
    # bounded wait.  We use the bounded-wait form: `flock -w <s>`.
    _inner = (
        f"flock -w {int(timeout_sec)} {lock_path} -c 'cat' "
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
