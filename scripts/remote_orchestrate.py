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
import shutil
import subprocess

import remote_build   # for the shared RESULT_MARKER (single source)

RESULT_MARKER = remote_build.RESULT_MARKER


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
                 remote_build_py: str) -> None:
    """Populate <bundle> with the everything remote_build.py needs:
    Dockerfile, source/<pkg files>, patch/<*.patch>, remote_build.py, and
    build.json (the params blob — image tag/args + cmd_str + caps)."""
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
    if _load.returncode == 0:
        return 'transferred'
    log("LAN image transfer failed — the remote will build the image instead")
    return 'build'


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
    dir is always cleaned up.  Returns (exit_code, [recovered basenames]).

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
            # globs expand on the remote shell; only run when there's output
            subprocess.run(_scp_base(ssh_key) + [
                f'{host}:{remote_dir}/out/*.deb', f'{local_out}/'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(_scp_base(ssh_key) + [
                f'{host}:{remote_dir}/out/*.udeb', f'{local_out}/'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return (_exit, _outputs)
    finally:
        subprocess.run(_ssh + [f'rm -rf {remote_dir}'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
