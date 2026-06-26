#!/usr/bin/env python3
"""End-to-end test for the REMOTE-API remote-build agent (MAT-07).

This is a MANUAL e2e — NOT part of tests/test_module.py — because it needs a
real Docker daemon, and optionally a real remote host + SSH.  Two modes:

  python3 tests/e2e_remote_agent.py
      LOCAL DOCKER (no SSH): run scripts/remote_agent.py against this host's
      Docker.  Scenario 1 builds a real image, runs a container that produces
      a .deb, and checks phase=done + the artifact is recovered + the named
      container is reaped.  Scenario 2 starts a hung container and checks the
      PROGRESS watchdog kills it (phase=timeout, real `docker kill`, no
      leftover container).

  python3 tests/e2e_remote_agent.py --remote user@host [--ssh-key PATH]
      FULL TRANSPORT: drive remote_orchestrate.run_remote_agent against a REAL
      remote — ship the bundle, start the agent DETACHED, open the SSH tunnel,
      poll the API, recover the .deb, reap the container.  Point it at each
      builder (BS2, …) to validate a real host; run it against two hosts to
      validate the fan-out transport end-to-end.

Exits 0 on success, non-zero (with a message) on the first failure.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
_AGENT_PY = os.path.join(_ROOT, 'scripts', 'remote_agent.py')

# A trivial-but-real build: FROM a small base, copy a file into /repo as the
# "built .deb" so the whole build→recover→reap path runs without a real source.
_DOCKERFILE = 'FROM debian:bookworm-slim\n'
_OK_CMD = ('set -e; echo building testpkg; sleep 1; '
           'cp /etc/hostname /repo/testpkg_1.0_amd64.deb; echo done')
_HANG_CMD = 'echo starting; sleep 600'
_OUT_DEB = 'testpkg_1.0_amd64.deb'


def _make_bundle(d, cmd_str, *, tag='athena-e2e:test'):
    with open(os.path.join(d, 'Dockerfile'), 'w') as _f:
        _f.write(_DOCKERFILE)
    with open(os.path.join(d, 'build.json'), 'w') as _f:
        json.dump({'package': 'testpkg', 'image_tag': tag,
                   'build_args': {}, 'cmd_str': cmd_str}, _f)


def _req(port, path, method='GET', token='tok'):
    _r = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method=method)
    _r.add_header('X-Athena-Token', token)
    with urllib.request.urlopen(_r, timeout=8) as _resp:
        return _resp.status, _resp.read(), dict(_resp.headers)


def _wait_terminal_local(port, tries=1200):
    _st = {}
    for _ in range(tries):
        _st = json.loads(_req(port, '/status')[1])
        if _st['phase'] in ('done', 'failed', 'timeout', 'aborted'):
            return _st
        time.sleep(0.5)
    return _st


def _running_agent_containers(filter_running=True):
    _args = ['docker', 'ps'] + ([] if filter_running else ['-a'])
    _args += ['--filter', 'name=athena-build', '--format', '{{.Names}}']
    return subprocess.run(_args, capture_output=True, text=True).stdout.strip()


def _local_docker_e2e():
    print('=== LOCAL DOCKER e2e ===')
    # Scenario 1 — real build → done, artifact recovered, container reaped.
    with tempfile.TemporaryDirectory() as _d:
        with open(os.path.join(_d, 'token'), 'w') as _f:
            _f.write('tok')
        _make_bundle(_d, _OK_CMD)
        _p = subprocess.Popen(
            [sys.executable, _AGENT_PY, '--bundle', _d, '--token-file',
             os.path.join(_d, 'token'), '--port', '0', '--hang-secs', '60'])
        _portf = os.path.join(_d, 'agent.port')
        _port = None
        for _ in range(200):
            if os.path.exists(_portf) and open(_portf).read().strip():
                _port = int(open(_portf).read().strip())
                break
            time.sleep(0.1)
        assert _port, 'agent did not publish its port'
        try:
            _st = _wait_terminal_local(_port)
            print(f"  scenario 1: phase={_st['phase']} outputs={_st['outputs']}")
            assert _st['phase'] == 'done', _st
            assert _st['outputs'] == [_OUT_DEB], _st
            assert os.listdir(os.path.join(_d, 'out')) == [_OUT_DEB]
            assert _running_agent_containers(filter_running=False) == '', \
                'a container leaked'
        finally:
            try:
                _req(_port, '/shutdown', 'POST')
            except Exception:
                pass
            _p.wait(timeout=10)
    print('  scenario 1 OK — real build, recovered, reaped')

    # Scenario 2 — hung container killed by the progress watchdog.
    with tempfile.TemporaryDirectory() as _d:
        with open(os.path.join(_d, 'token'), 'w') as _f:
            _f.write('tok')
        _make_bundle(_d, _HANG_CMD)
        _p = subprocess.Popen(
            [sys.executable, _AGENT_PY, '--bundle', _d, '--token-file',
             os.path.join(_d, 'token'), '--port', '0', '--hang-secs', '5'])
        _portf = os.path.join(_d, 'agent.port')
        _port = None
        for _ in range(200):
            if os.path.exists(_portf) and open(_portf).read().strip():
                _port = int(open(_portf).read().strip())
                break
            time.sleep(0.1)
        assert _port, 'agent did not publish its port'
        try:
            _st = _wait_terminal_local(_port)
            print(f"  scenario 2: phase={_st['phase']} watchdog={_st['watchdog']}")
            assert _st['phase'] == 'timeout' and _st['watchdog'] == 'hang-killed', _st
            time.sleep(1)
            assert _running_agent_containers() == '', 'hung container survived'
        finally:
            try:
                _req(_port, '/shutdown', 'POST')
            except Exception:
                pass
            _p.wait(timeout=10)
    print('  scenario 2 OK — hung container killed by watchdog, reaped')


def _remote_transport_e2e(remote, ssh_key):
    print(f'=== FULL TRANSPORT e2e against {remote} ===')
    import shutil
    import remote_orchestrate as _ro
    with tempfile.TemporaryDirectory() as _bundle, \
            tempfile.TemporaryDirectory() as _out:
        _make_bundle(_bundle, _OK_CMD)
        # The agent runs from the shipped bundle dir and imports remote_build,
        # so it must travel in the bundle (production stage_bundle does this).
        shutil.copy(os.path.join(_ROOT, 'scripts', 'remote_build.py'),
                    os.path.join(_bundle, 'remote_build.py'))
        _logs = []
        _exit, _outputs = _ro.run_remote_agent(
            remote, _bundle,
            f'/tmp/athena-e2e-{os.getpid()}', _out,
            token='e2e-token', ssh_key=ssh_key,
            remote_agent_py=_AGENT_PY, hang_secs=120, log=_logs.append)
        print(f"  exit={_exit} outputs={_outputs}")
        assert _exit == 0, f"remote build failed (exit {_exit}); log tail:\n" \
            + '\n'.join(_logs[-15:])
        assert _outputs == [_OUT_DEB], _outputs
        assert os.path.isfile(os.path.join(_out, _OUT_DEB)), 'artifact not recovered'
    print('  OK — real ship → detached agent → tunnel → poll → recover → reap')


def main(argv=None):
    _p = argparse.ArgumentParser(description='REMOTE-API agent e2e')
    _p.add_argument('--remote', default=None,
                    help='user@host of a real remote (full-transport mode)')
    _p.add_argument('--ssh-key', default=None)
    _a = _p.parse_args(argv)
    try:
        if _a.remote:
            _remote_transport_e2e(_a.remote, _a.ssh_key)
        else:
            _local_docker_e2e()
    except AssertionError as _e:
        print(f"E2E FAILED: {_e}", file=sys.stderr)
        return 1
    print('\nALL E2E PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
