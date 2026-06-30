"""Drive a remote source build over SSH: stage a bundle, ship it, run
scripts/remote_agent.py DETACHED on the remote, and POLL its localhost HTTP API
over an SSH tunnel until the build is terminal — then recover the .debs
(`run_remote_agent`, the REMOTE-API control plane).

This module only moves bytes + drives SSH/scp/the poll API — it contains NO
Athena build logic.  The build recipe (image build-args + container cmd_str) is
computed by BuildContainer.compose_recipe() and handed in as `recipe`, so a
remote build is byte-identical to a local one.  The build itself runs where
Docker is local (the remote host), so the container's bind mounts just work —
no daemon-API copy-in/out.
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

# REMOTE-API poll cadence + how long with NO successful /status before we give
# up on a (presumed-dead) agent and re-queue the build elsewhere.
AGENT_POLL_INTERVAL = 2.0
AGENT_DEAD_GRACE_SEC = 300.0

# The stable on-remote path for the local build mirror — a literal `~` the
# remote python resolves (remote_localmirror.py and remote_build.py both
# expanduser it), so BS1 needn't know the remote user's $HOME.  The build
# container bind-mounts this at /localmirror.
REMOTE_LOCALMIRROR_DIR = '~/athena-localmirror'
LM_PROGRESS_MARKER = '__ATHENA_LM_PROGRESS__'
LM_RESULT_MARKER = '__ATHENA_LM_RESULT__'


def parse_ssh_host(remote: str) -> str:
    """`ssh://user@host[:port]` or `user@host` → `user@host` for ssh/scp."""
    _r = remote.strip()
    if _r.startswith('ssh://'):
        _r = _r[len('ssh://'):]
    return _r.rstrip('/')


def _ssh_base(host: str, ssh_key: 'str | None' = None) -> 'list[str]':
    """`ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new [-i <key>]
    <host>` — the argv prefix for a remote command.  Centralises `-i` insertion
    so every ssh call site honours the per-remote key copied into config/ (vs
    the operator's ambient ~/.ssh).  MAT-11(4): `accept-new` matches every
    other ssh site (mirror probes, the agent tunnel) so a first contact with a
    fresh remote records its host key instead of failing under BatchMode."""
    _argv = ['ssh', '-o', 'BatchMode=yes',
             '-o', 'StrictHostKeyChecking=accept-new']
    if ssh_key:
        _argv += ['-i', ssh_key]
    _argv.append(host)
    return _argv


def _scp_base(ssh_key: 'str | None' = None) -> 'list[str]':
    """`scp -q -o StrictHostKeyChecking=accept-new [-i <key>]` — the argv prefix
    for a bundle/output transfer (MAT-11(4): accept-new for parity with the ssh
    sites + a fresh remote's first transfer)."""
    _argv = ['scp', '-q', '-o', 'StrictHostKeyChecking=accept-new']
    if ssh_key:
        _argv += ['-i', ssh_key]
    return _argv


def stage_bundle(bundle: str, *, dockerfile: str, source_files: 'list[str]',
                 patch_dir: str, recipe: dict, build_cpus, build_memory,
                 remote_build_py: str, remote_agent_py: 'str | None' = None,
                 localmirror_dir: 'str | None' = None) -> None:
    """Populate <bundle> with everything the remote needs: Dockerfile,
    source/<pkg files>, patch/<*.patch>, remote_build.py, the optional
    remote_agent.py (REMOTE-API transport), and build.json (the params blob —
    image tag/args + cmd_str + caps).

    `localmirror_dir` (set only when the recipe emits the file:///localmirror
    source) tells remote_build.py to bind-mount that on-remote mirror dir."""
    os.makedirs(os.path.join(bundle, 'source'), exist_ok=True)
    os.makedirs(os.path.join(bundle, 'patch'), exist_ok=True)
    shutil.copy(dockerfile, os.path.join(bundle, 'Dockerfile'))
    shutil.copy(remote_build_py, os.path.join(bundle, 'remote_build.py'))
    if remote_agent_py:
        shutil.copy(remote_agent_py, os.path.join(bundle, 'remote_agent.py'))
    for _f in source_files:
        shutil.copy(_f, os.path.join(bundle, 'source', os.path.basename(_f)))
    if patch_dir and os.path.isdir(patch_dir):
        for _p in sorted(os.listdir(patch_dir)):
            if _p.endswith('.patch'):
                shutil.copy(os.path.join(patch_dir, _p),
                            os.path.join(bundle, 'patch', _p))
    _params: dict = {
        'package':    recipe.get('filename_prefix'),
        'image_tag':  recipe['image_tag'],
        'build_args': recipe['build_args'],
        'cmd_str':    recipe['cmd_str'],
    }
    if build_cpus:
        _params['build_cpus'] = build_cpus
    if build_memory:
        _params['build_memory'] = build_memory
    if localmirror_dir:
        _params['localmirror_dir'] = localmirror_dir
    with open(os.path.join(bundle, 'build.json'), 'w') as _fh:
        json.dump(_params, _fh, indent=2)


def _has_image(image_tag: str, ssh_host: 'str | None' = None,
               ssh_key: 'str | None' = None) -> bool:
    """True if `image_tag` exists — on the remote (ssh_host set) or locally."""
    if ssh_host:
        _cmd = _ssh_base(ssh_host, ssh_key) + [
            f'docker image inspect {image_tag}']
    else:
        _cmd = ['docker', 'image', 'inspect', image_tag]
    return subprocess.run(_cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def ensure_remote_image(host: str, image_tag: str, *,
                        ssh_key: 'str | None' = None, log=print) -> str:
    """Make sure `image_tag` is available on the remote, the FAST way.

    The remote rebuilding the toolchain image from the internet is the slow
    path (~tens of minutes on a thin link).  If THIS host already has the image
    (e.g. cached from a prior local build), stream it over the LAN instead
    (`docker save | ssh docker load`) — far faster than re-downloading.

    Returns:
      'present'      — the remote already had it (nothing to do)
      'transferred'  — copied from this host over the LAN
      'build'        — neither has it; remote_build.py will build it remotely
    """
    if _has_image(image_tag, ssh_host=host, ssh_key=ssh_key):
        return 'present'
    if not _has_image(image_tag):
        return 'build'                       # remote_build.py builds it remotely
    log(f"image {image_tag} not on remote — streaming it over the LAN "
        "(docker save | ssh docker load) …")
    _save = subprocess.Popen(['docker', 'save', image_tag],
                             stdout=subprocess.PIPE)
    _load = subprocess.Popen(_ssh_base(host, ssh_key) + ['docker load'],
                             stdin=_save.stdout)
    if _save.stdout is not None:
        _save.stdout.close()               # let _load receive SIGPIPE on exit
    _load.wait()
    _save.wait()
    # Require BOTH ends: a failed `docker save` (image pruned mid-flight, disk
    # error) can close the pipe early and still let `docker load` exit 0 on a
    # truncated stream — reporting 'transferred' for an image not actually on
    # the remote, so the first remotebuild fails "image not found".
    if _load.returncode == 0 and _save.returncode == 0:
        return 'transferred'
    log("LAN image transfer failed — the remote will build the image instead")
    return 'build'


def build_remote_image(host: str, dockerfile_path: str, image_tag: str,
                       build_args: dict, *, ssh_key: 'str | None' = None,
                       log=print) -> bool:
    """Build `image_tag` ON the remote from `dockerfile_path`, streamed over ssh
    via stdin (empty build context — matches remote_build.build_image).  Returns
    True on success.  `container remote init` uses this to eagerly build the
    image when NEITHER this host nor the remote has it cached, rather than
    deferring the multi-ten-minute build to the first `source remotebuild`."""
    _flags = [f'--build-arg {shlex.quote(f"{_k}={_v}")}'
              for _k, _v in (build_args or {}).items()]
    # --progress=plain → line-based output; BuildKit's default TTY progress uses
    # carriage-returns + cursor control that corrupt a curses TUI when streamed
    # back over ssh.  Capture stdout/stderr (Popen, NOT inherited) and route each
    # line through `log` so it never writes raw to the terminal under the TUI.
    _cmd = (f"docker build --progress=plain -t {shlex.quote(image_tag)} "
            f"{' '.join(_flags)} -")
    log(f"building image {image_tag} on {host} (this can take a while) …")
    try:
        with open(dockerfile_path, 'rb') as _fh:
            _proc = subprocess.Popen(
                _ssh_base(host, ssh_key) + [_cmd], stdin=_fh,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            assert _proc.stdout is not None
            for _line in _proc.stdout:
                log(_line.rstrip('\n'))
            _proc.wait()
    except OSError as _e:
        log(f"build_remote_image: {_e}")
        return False
    return _proc.returncode == 0


def stage_remote_localmirror(
        host: str, plan_dict: dict, remote_localmirror_py: str, *,
        remote_mirror_dir: str = REMOTE_LOCALMIRROR_DIR,
        ssh_key: 'str | None' = None, on_progress=None,
        log=print) -> 'dict | None':
    """Populate the local build mirror ON the remote from a BS1-computed plan.

    Ships ``plan.json`` + ``remote_localmirror.py`` to a throwaway staging dir on
    the remote, runs the runner (which downloads the closure into the STABLE
    ``remote_mirror_dir`` — resumable, so re-running just continues), streams its
    PROGRESS markers to ``on_progress(payload)`` for a live two-bar display, and
    returns the parsed RESULT dict (or None on a transport/spawn failure).  The
    mirror dir persists; only the staging dir is cleaned up.

    The plan carries only UPSTREAM (snapshot, http) members — local_mirror.plan
    excludes our fork / file:// packages — so the remote fetches everything
    itself; nothing is pushed from BS1.
    """
    import json as _json
    import tempfile as _tempfile
    _ssh = _ssh_base(host, ssh_key)
    _stage = f"/tmp/athena-lm-stage-{os.getpid()}-{abs(hash(host)) % 100000}"
    _result: 'dict | None' = None
    _local_plan = None
    try:
        if subprocess.run(_ssh + [f'mkdir -p {_stage}']).returncode != 0:
            log(f"remote-localmirror: cannot create {_stage} on {host}")
            return None
        _fd, _local_plan = _tempfile.mkstemp(suffix='.json', prefix='lmplan-')
        with os.fdopen(_fd, 'w') as _fh:
            _json.dump(plan_dict, _fh)
        if subprocess.run(_scp_base(ssh_key) + [
                _local_plan, remote_localmirror_py,
                f'{host}:{_stage}/']).returncode != 0:
            log("remote-localmirror: scp of plan/runner failed")
            return None
        _planfn = os.path.basename(_local_plan)
        _runnerfn = os.path.basename(remote_localmirror_py)
        _cmd = (f'cd {_stage} && python3 {_runnerfn} --plan {_planfn} '
                f'--dir {remote_mirror_dir}')
        _proc = subprocess.Popen(
            _ssh + [_cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        assert _proc.stdout is not None
        for _line in _proc.stdout:
            _line = _line.rstrip('\n')
            if _line.startswith(LM_PROGRESS_MARKER):
                if on_progress is not None:
                    try:
                        on_progress(_json.loads(_line[len(LM_PROGRESS_MARKER):]))
                    except (ValueError, TypeError):
                        pass
            elif _line.startswith(LM_RESULT_MARKER):
                try:
                    _result = _json.loads(_line[len(LM_RESULT_MARKER):])
                except (ValueError, TypeError):
                    _result = None
            else:
                log(_line)
        _proc.wait()
        return _result
    finally:
        subprocess.run(_ssh + [f'rm -rf {_stage}'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if _local_plan and os.path.exists(_local_plan):
            try:
                os.remove(_local_plan)
            except OSError:
                pass


def _free_local_port() -> int:
    """An OS-assigned free localhost port for the SSH tunnel's local end."""
    _s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _s.bind(('127.0.0.1', 0))
        return _s.getsockname()[1]
    finally:
        _s.close()


def _agent_request(local_port: int, path: str, token: str,
                   method: str = 'GET', timeout: float = 10.0):
    """One token-authed HTTP request to the agent through the tunnel.  Returns
    (status, body-bytes, headers-dict).  Module-level so tests can patch it."""
    _req = urllib.request.Request(
        f'http://127.0.0.1:{local_port}{path}', method=method)
    _req.add_header('X-Athena-Token', token)
    with urllib.request.urlopen(_req, timeout=timeout) as _r:
        return _r.status, _r.read(), dict(_r.headers)


class _AgentHandle:
    """A register_proc handle whose ``.terminate()`` aborts the remote build +
    tears the tunnel down — the fan-out scheduler's SIGINT path only calls
    ``.terminate()`` on whatever it registered, so this duck-types a Popen."""

    def __init__(self, abort_cb) -> None:
        self._abort = abort_cb
        self._done = False

    def terminate(self) -> None:
        if not self._done:
            self._done = True
            try:
                self._abort()
            except Exception:      # noqa: BLE001 — abort is strictly best-effort
                pass

    def kill(self) -> None:
        self.terminate()

    def poll(self):
        return None

    def wait(self, *a, **k) -> int:
        return 0


def _remote_container_name(remote_dir: str) -> str:
    """The container name the agent will use (athena-build-<bundle basename>),
    so the orchestrator can reap an orphan by name.  Mirrors
    remote_agent.container_name_for over the SAME basename."""
    import re
    _base = os.path.basename(remote_dir.rstrip('/')) or 'build'
    return 'athena-build-' + re.sub(r'[^A-Za-z0-9_.-]', '-', _base)


def run_remote_agent(host: str, local_bundle: str, remote_dir: str,
                     local_out: str, *, token: str,
                     ssh_key: 'str | None' = None,
                     remote_agent_py: 'str | None' = None,
                     hang_secs: int = 1800,
                     register_proc=None, log=print) -> 'tuple[int, list[str]]':
    """REMOTE-API transport: ship the bundle + agent, start the agent DETACHED
    on the remote, open an SSH tunnel to its localhost API, and POLL /status +
    /logs until the build is terminal — then recover the .debs.  The build
    survives orchestrator disconnects (the tunnel is reopened and polling
    resumes from the last log offset), and a PROGRESS watchdog on the remote
    (not a wall-clock timer) kills a wedged build.

    Exit codes: 10 (mkdir/start) / 11 (scp-up) / 12 (watchdog timeout,
    agent-dead, or partial recovery) are TRANSPORT failures the scheduler
    re-queues; a real build exit otherwise.  130 = aborted (SIGINT).
    """
    _ssh = _ssh_base(host, ssh_key)
    _tunnel: 'subprocess.Popen | None' = None
    _state: dict = {'local_port': None, 'aborted': False}

    def _abort() -> None:
        _state['aborted'] = True
        _lp = _state['local_port']
        if _lp is not None:
            try:
                _agent_request(_lp, '/abort', token, method='POST', timeout=5)
            except Exception:      # noqa: BLE001
                pass

    if register_proc is not None:
        register_proc(_AgentHandle(_abort))

    def _open_tunnel(remote_port: int) -> 'tuple[subprocess.Popen, int]':
        _lp = _free_local_port()
        # StrictHostKeyChecking=accept-new matches the _ssh_base contract so
        # every ssh site behaves the same (TOFU on first connect, refuse on a
        # changed key) — _open_tunnel previously omitted it.
        _argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                 '-o', 'StrictHostKeyChecking=accept-new']
        if ssh_key:
            _argv += ['-i', ssh_key]
        _argv += ['-N', '-L', f'127.0.0.1:{_lp}:127.0.0.1:{remote_port}', host]
        return subprocess.Popen(_argv), _lp

    try:
        # 1. remote scratch dir
        if subprocess.run(_ssh + [f'mkdir -p {remote_dir}']).returncode != 0:
            log(f"remote: cannot create {remote_dir} on {host}")
            return (10, [])
        # 2. drop the token into the bundle (0600) + ship everything
        _tokfile = os.path.join(local_bundle, 'agent.token')
        with open(_tokfile, 'w') as _tf:
            _tf.write(token)
        os.chmod(_tokfile, 0o600)
        if remote_agent_py:
            shutil.copy(remote_agent_py,
                        os.path.join(local_bundle, 'remote_agent.py'))
        _items = [os.path.join(local_bundle, _e)
                  for _e in sorted(os.listdir(local_bundle))]
        if subprocess.run(_scp_base(ssh_key) + ['-r', *_items,
                          f'{host}:{remote_dir}/']).returncode != 0:
            log("remote: scp of bundle failed")
            return (11, [])
        # 3. start the agent DETACHED (survives this ssh session closing).
        # `cd … || exit 1; nohup … &` — NOT `cd && … &`: with `&&` the shell
        # backgrounds the WHOLE `cd && nohup` compound as a subshell that runs
        # the (long-lived) agent in its foreground, so the subshell keeps the
        # SSH channel's stdout open and the start ssh never returns.  Using `;`
        # runs cd in the session and backgrounds ONLY the fd-redirected agent,
        # releasing the channel so the start returns immediately.
        _start = (
            f'cd {remote_dir} || exit 1; nohup python3 remote_agent.py '
            f'--bundle . --token-file agent.token --port 0 '
            f'--hang-secs {int(hang_secs)} < /dev/null > agent.out 2>&1 & '
            f'echo AGENT_STARTED')
        if subprocess.run(_ssh + [_start]).returncode != 0:
            log("remote: agent failed to start")
            return (10, [])
        # 4. learn the agent's chosen localhost port
        _rport = None
        for _ in range(120):
            _r = subprocess.run(
                _ssh + [f'cat {remote_dir}/agent.port 2>/dev/null'],
                capture_output=True, text=True)
            _v = (_r.stdout or '').strip()
            if _v.isdigit():
                _rport = int(_v)
                break
            if _state['aborted']:
                return (130, [])
            time.sleep(0.5)
        if _rport is None:
            log("remote: agent did not publish its port")
            return (10, [])
        # 5. tunnel + 6. poll
        _tunnel, _state['local_port'] = _open_tunnel(_rport)
        _log_off = 0
        _status: dict = {}
        _last_ok = time.time()
        while True:
            if _state['aborted']:
                break
            if _tunnel.poll() is not None:          # tunnel collapsed — reopen
                _tunnel, _state['local_port'] = _open_tunnel(_rport)
            _lp = _state['local_port']
            try:
                _, _body, _ = _agent_request(_lp, '/status', token, timeout=10)
                _status = json.loads(_body)
                _last_ok = time.time()
            except (urllib.error.URLError, OSError, ValueError):
                if time.time() - _last_ok > AGENT_DEAD_GRACE_SEC:
                    log(f"remote: agent on {host} unreachable for "
                        f"{int(AGENT_DEAD_GRACE_SEC)}s — re-queueing")
                    return (12, [])
                time.sleep(AGENT_POLL_INTERVAL)
                continue
            try:
                _, _ldata, _lhdr = _agent_request(
                    _lp, f'/logs?from={_log_off}', token, timeout=10)
                if _ldata:
                    for _line in _ldata.decode('utf-8', 'replace').splitlines():
                        log(_line)
                    _log_off = int(_lhdr.get('X-Log-Next', _log_off))
            except (urllib.error.URLError, OSError, ValueError):
                pass
            if _status.get('phase') in ('done', 'failed', 'timeout', 'aborted'):
                break
            time.sleep(AGENT_POLL_INTERVAL)
        # 7. terminal handling
        _phase = _status.get('phase')
        _outputs = list(_status.get('outputs') or [])
        if _state['aborted'] or _phase == 'aborted':
            return (130, [])
        if _phase == 'timeout':
            log(f"remote: watchdog killed a hung build on {host} "
                "(no progress) — re-queueing")
            return (12, [])
        if _outputs:
            os.makedirs(local_out, exist_ok=True)
            subprocess.run(_scp_base(ssh_key) + [
                f'{host}:{remote_dir}/out/*.deb', f'{local_out}/'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(_scp_base(ssh_key) + [
                f'{host}:{remote_dir}/out/*.udeb', f'{local_out}/'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _recovered = (set(os.listdir(local_out))
                          if os.path.isdir(local_out) else set())
            _missing = [_o for _o in _outputs
                        if os.path.basename(_o) not in _recovered]
            if _missing:
                log(f"remote: scp recovered {len(_outputs) - len(_missing)}/"
                    f"{len(_outputs)} artifact(s) from {host}; missing "
                    f"{sorted(os.path.basename(_m) for _m in _missing)} — "
                    "transport failure, re-queueing")
                return (12, [])
        _exit = _status.get('exit_code')
        return (int(_exit) if _exit is not None else 1, _outputs)
    finally:
        _lp = _state['local_port']
        if _lp is not None:
            try:
                _agent_request(_lp, '/shutdown', token, method='POST', timeout=5)
            except Exception:      # noqa: BLE001
                pass
        if _tunnel is not None:
            try:
                _tunnel.terminate()
                _tunnel.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    _tunnel.kill()
                except OSError:
                    pass
        # reap an orphaned container by name + wipe the scratch dir
        subprocess.run(
            _ssh + [f'docker rm -f {_remote_container_name(remote_dir)} '
                    f'2>/dev/null; rm -rf {remote_dir}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
