"""Drive a remote source build over SSH: stage a bundle, ship it, run
scripts/remote_build.py on the remote host, recover the produced .debs.

This module only moves bytes + drives SSH/scp — it contains NO Athena build
logic.  The build recipe (image build-args + container cmd_str) is computed by
BuildContainer.compose_recipe() and handed in as `recipe`, so a remote build is
byte-identical to a local one.  The build itself runs where Docker is local (the
remote host), so the container's bind mounts just work — no daemon-API
copy-in/out.
"""

import json
import os
import shlex
import shutil
import subprocess

import remote_build   # for the shared RESULT_MARKER (single source)

RESULT_MARKER = remote_build.RESULT_MARKER

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
    """`ssh -o BatchMode=yes [-i <key>] <host>` — the argv prefix for a remote
    command.  Centralises `-i` insertion so every ssh call site honours the
    per-remote key copied into config/ (vs the operator's ambient ~/.ssh)."""
    _argv = ['ssh', '-o', 'BatchMode=yes']
    if ssh_key:
        _argv += ['-i', ssh_key]
    _argv.append(host)
    return _argv


def _scp_base(ssh_key: 'str | None' = None) -> 'list[str]':
    """`scp -q [-i <key>]` — the argv prefix for a bundle/output transfer."""
    _argv = ['scp', '-q']
    if ssh_key:
        _argv += ['-i', ssh_key]
    return _argv


def stage_bundle(bundle: str, *, dockerfile: str, source_files: 'list[str]',
                 patch_dir: str, recipe: dict, build_cpus, build_memory,
                 remote_build_py: str,
                 localmirror_dir: 'str | None' = None) -> None:
    """Populate <bundle> with the everything remote_build.py needs:
    Dockerfile, source/<pkg files>, patch/<*.patch>, remote_build.py, and
    build.json (the params blob — image tag/args + cmd_str + caps).

    `localmirror_dir` (set only when the recipe emits the file:///localmirror
    source) tells remote_build.py to bind-mount that on-remote mirror dir."""
    os.makedirs(os.path.join(bundle, 'source'), exist_ok=True)
    os.makedirs(os.path.join(bundle, 'patch'), exist_ok=True)
    shutil.copy(dockerfile, os.path.join(bundle, 'Dockerfile'))
    shutil.copy(remote_build_py, os.path.join(bundle, 'remote_build.py'))
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


def _parse_marker_line(line: 'str | None') -> 'tuple[int, list[str]]':
    """Parse one `__ATHENA_REMOTE_RESULT__ {json}` line into
    (exit_code, [output basenames]).  None / malformed → (1, [])."""
    if not line:
        return (1, [])
    try:
        _d = json.loads(line[len(RESULT_MARKER):])
        return (int(_d.get('exit_code', 1)), list(_d.get('outputs', [])))
    except (ValueError, TypeError):
        return (1, [])


def run_remote(host: str, local_bundle: str, remote_dir: str,
               local_out: str, *, ssh_key: 'str | None' = None,
               register_proc=None, log=print) -> 'tuple[int, list[str]]':
    """scp the bundle up, run remote_build.py on the remote with its output
    written to a LOG FILE ON THE REMOTE (so the build runs at full speed,
    decoupled from the network), tail that file back through `log` for live
    progress, then scp the produced .debs back to `local_out`.  The remote temp
    dir is always cleaned up.  Returns (exit_code, [recovered basenames]) — the
    basename list is reconciled against the remote's reported outputs, so a
    short recovery surfaces as a transport failure rather than a complete build.

    Exit codes: the remote_build.py exit on a real build; 10 (remote mkdir) /
    11 (bundle scp-up) / 12 (partial scp-DOWN recovery) are TRANSPORT failures
    the fan-out scheduler re-queues on another remote.

    `register_proc`, if given, is called with the live `ssh` Popen running the
    build so a caller (the fan-out scheduler) can `terminate()` it on Ctrl+C.
    """
    _ssh = _ssh_base(host, ssh_key)
    try:
        if subprocess.run(_ssh + [f'mkdir -p {remote_dir}']).returncode != 0:
            log(f"remote: cannot create {remote_dir} on {host}")
            return (10, [])
        _items = [os.path.join(local_bundle, _e)
                  for _e in sorted(os.listdir(local_bundle))]
        if subprocess.run(_scp_base(ssh_key) + ['-r', *_items,
                           f'{host}:{remote_dir}/']).returncode != 0:
            log("remote: scp of bundle failed")
            return (11, [])
        # Decoupled logging: the build writes to remote_dir/build.log on the
        # remote's local disk (full speed — never blocked by the network), and a
        # SEPARATE `tail -F` streams it back through `log` for live progress.  A
        # slow network / log sink can't backpressure the build (it writes to the
        # file independently).  `--pid` stops the tail when remote_build.py
        # exits; a final grep guarantees the result marker reaches us even if
        # tail missed the last line.  The marker is scanned incrementally
        # (O(1) memory, not the whole log buffered).
        # `cd` must apply to BOTH the backgrounded build AND the foreground
        # tail/grep — wrap in a brace group so the bare `&` doesn't background
        # the `cd` itself (which would leave tail/grep in the wrong directory).
        _remote_cmd = (
            f'cd {remote_dir} && {{ '
            f'python3 remote_build.py . > build.log 2>&1 & _p=$!; '
            f'tail -n +1 --pid=$_p -F build.log 2>/dev/null; '
            f'wait $_p; '
            f'grep -a {RESULT_MARKER} build.log | tail -1; }}'
        )
        _proc = subprocess.Popen(
            _ssh + [_remote_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if register_proc is not None:
            register_proc(_proc)
        _marker: 'str | None' = None
        assert _proc.stdout is not None
        for _line in _proc.stdout:
            log(_line.rstrip('\n'))
            if _line.startswith(RESULT_MARKER):
                _marker = _line          # keep only the last marker (O(1) mem)
        _proc.wait()
        _exit, _outputs = _parse_marker_line(_marker)
        if _outputs:
            os.makedirs(local_out, exist_ok=True)
            # globs expand on the remote shell; only run when there's output.
            # stderr→DEVNULL because an empty glob (a source producing only
            # .debs, or only .udebs) is a benign no-match — the reconcile below
            # is the real integrity check, not the per-scp return code.
            subprocess.run(_scp_base(ssh_key) + [
                f'{host}:{remote_dir}/out/*.deb', f'{local_out}/'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(_scp_base(ssh_key) + [
                f'{host}:{remote_dir}/out/*.udeb', f'{local_out}/'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Reconcile recovery against the marker: a partial scp (connection
            # dropped mid-transfer) must NOT be recorded as a complete build.
            # EVERY artifact the remote reported must have landed locally; if any
            # is missing, signal a transport failure (exit 12) so the scheduler
            # re-queues this package on another remote — the remote_dir is wiped
            # in `finally`, so the re-run rebuilds cleanly.  Returning the marker
            # list with a short recovery would yield a `done` record claiming
            # more .debs than are on disk.
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
        return (_exit, _outputs)
    finally:
        subprocess.run(_ssh + [f'rm -rf {remote_dir}'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
