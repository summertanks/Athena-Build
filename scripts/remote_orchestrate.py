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


def _parse_result(stdout: str) -> 'tuple[int, list[str]]':
    """Pull the last `__ATHENA_REMOTE_RESULT__ {json}` line emitted by
    remote_build.py.  Returns (exit_code, [output basenames])."""
    for _line in reversed(stdout.splitlines()):
        if _line.startswith(RESULT_MARKER):
            try:
                _d = json.loads(_line[len(RESULT_MARKER):])
                return (int(_d.get('exit_code', 1)), list(_d.get('outputs', [])))
            except (ValueError, TypeError):
                return (1, [])
    return (1, [])


def run_remote(host: str, local_bundle: str, remote_dir: str,
               local_out: str, *, log=print) -> 'tuple[int, list[str]]':
    """scp the bundle up, ssh-run remote_build.py (streaming its output through
    `log`), then scp the produced .debs back to `local_out`.  The remote temp
    dir is always cleaned up.  Returns (exit_code, [recovered basenames]).
    """
    _ssh = ['ssh', '-o', 'BatchMode=yes', host]
    try:
        if subprocess.run(_ssh + [f'mkdir -p {remote_dir}']).returncode != 0:
            log(f"remote: cannot create {remote_dir} on {host}")
            return (10, [])
        _items = [os.path.join(local_bundle, _e)
                  for _e in sorted(os.listdir(local_bundle))]
        if subprocess.run(['scp', '-q', '-r', *_items,
                           f'{host}:{remote_dir}/']).returncode != 0:
            log("remote: scp of bundle failed")
            return (11, [])
        # run the build, streaming logs live while capturing for the result line
        _proc = subprocess.Popen(
            _ssh + [f'cd {remote_dir} && python3 remote_build.py .'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        _captured: 'list[str]' = []
        assert _proc.stdout is not None
        for _line in _proc.stdout:
            log(_line.rstrip('\n'))
            _captured.append(_line)
        _proc.wait()
        _exit, _outputs = _parse_result(''.join(_captured))
        if _outputs:
            os.makedirs(local_out, exist_ok=True)
            # globs expand on the remote shell; only run when there's output
            subprocess.run(['scp', '-q', f'{host}:{remote_dir}/out/*.deb',
                            f'{local_out}/'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['scp', '-q', f'{host}:{remote_dir}/out/*.udeb',
                            f'{local_out}/'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return (_exit, _outputs)
    finally:
        subprocess.run(_ssh + [f'rm -rf {remote_dir}'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
