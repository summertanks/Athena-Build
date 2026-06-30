#!/usr/bin/env python3
"""Remote build AGENT — runs ON a remote host, drives one source build and
exposes a localhost HTTP API the orchestrator (BS1) reaches over an SSH tunnel
and POLLS for status + logs.

This supersedes the SSH-tail control plane (remote_build.py driven directly
over `ssh`): the agent runs DETACHED, so a build survives orchestrator
disconnects (BS1 reconnects the tunnel and resumes polling from its last log
offset), and a PROGRESS-based watchdog — not a wall-clock timer — kills a
wedged build.  A multi-hour build that is actively producing output / burning
CPU is healthy; one with NO progress for `--hang-secs` is hung.

stdlib only + the `docker` CLI (and the sibling remote_build.py for the shared
docker build/run logic).  No third-party deps, no Athena imports.

API — every request must carry  `X-Athena-Token: <per-remote token>`:
    GET  /status            -> JSON build status (phase, exit_code, outputs, …)
    GET  /logs?from=<N>     -> raw build-log bytes from offset N
                               (+ X-Log-Next / X-Log-Size headers)
    POST /abort             -> kill the container, mark aborted
    POST /shutdown          -> stop the agent process

The bound port (127.0.0.1:0 → OS-assigned) and the agent PID are written to
<bundle>/agent.port and <bundle>/agent.pid so the orchestrator can discover
them after launching the agent detached.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional, Tuple

import remote_build   # sibling — shared docker build/run logic

# Build phases (the API's `phase` field).
PHASE_INIT = 'init'
PHASE_BUILD_IMAGE = 'building-image'
PHASE_RUN = 'running'
PHASE_DONE = 'done'
PHASE_FAILED = 'failed'
PHASE_TIMEOUT = 'timeout'      # watchdog killed a hung build
PHASE_ABORTED = 'aborted'      # operator/orchestrator abort

_TERMINAL = {PHASE_DONE, PHASE_FAILED, PHASE_TIMEOUT, PHASE_ABORTED}

DEFAULT_HANG_SECS = 1800       # 30 min of NO progress ⇒ hung
WATCHDOG_TICK = 15
CPU_PROGRESS_PCT = 0.5         # container CPU above this counts as progress


def container_name_for(bundle: str) -> str:
    """A docker-safe, deterministic container name from the bundle basename —
    the watchdog kills it by name and the orchestrator reaps it by name."""
    _base = os.path.basename(os.path.abspath(bundle)) or 'build'
    _safe = re.sub(r'[^A-Za-z0-9_.-]', '-', _base)
    return f'athena-build-{_safe}'


class BuildState:
    """Thread-safe build state the HTTP handlers snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = PHASE_INIT
        self.exit_code: Optional[int] = None
        self.outputs: list = []
        self.watchdog = 'ok'           # 'ok' | 'hang-killed'
        self.abort_requested = False
        self._last_progress = time.time()

    def set(self, **kw) -> None:
        with self._lock:
            for _k, _v in kw.items():
                setattr(self, _k, _v)

    def get_phase(self) -> str:
        with self._lock:
            return self.phase

    def mark_progress(self) -> None:
        with self._lock:
            self._last_progress = time.time()

    def progress_age(self) -> float:
        with self._lock:
            return time.time() - self._last_progress

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'phase': self.phase,
                'exit_code': self.exit_code,
                'outputs': list(self.outputs),
                'watchdog': self.watchdog,
                'abort_requested': self.abort_requested,
                'last_progress_age': round(time.time() - self._last_progress, 1),
            }


def read_log(path: str, frm: int) -> 'Tuple[bytes, int, int]':
    """Return (bytes-from-offset, next-offset, total-size) for the build log —
    the offset model lets the orchestrator poll incrementally and resume losslessly
    after a reconnect."""
    try:
        _size = os.path.getsize(path)
    except OSError:
        return (b'', frm, frm)
    if frm < 0:
        frm = 0
    if frm >= _size:
        return (b'', _size, _size)
    try:
        with open(path, 'rb') as _fh:
            _fh.seek(frm)
            _data = _fh.read()
    except OSError:
        return (b'', frm, _size)
    # next-offset = frm + bytes actually read, NOT the stale _size: the file
    # may have grown between getsize() and read(), so _data can exceed
    # _size-frm; frm+len(_data) is lossless and overlap-free.
    return (_data, frm + len(_data), _size)


def _container_cpu(container_name: 'Optional[str]') -> 'Optional[float]':
    """Container CPU% via `docker stats --no-stream`, or None if unavailable
    (no name, container gone, docker error)."""
    if not container_name:
        return None
    try:
        _r = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}",
             container_name],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    _m = re.search(r'([\d.]+)%', _r.stdout or '')
    try:
        return float(_m.group(1)) if _m else None
    except ValueError:
        return None


def watchdog_loop(
    state: BuildState, log_path: str, container_name: 'Optional[str]',
    hang_secs: int, stop: threading.Event, kill_build: 'Callable[[], None]',
    tick: 'Optional[int]' = None,
) -> None:
    """Kill the build when it makes NO progress for `hang_secs`.  Progress =
    the build log grew OR (for the docker path) the container is burning CPU.
    Time-based total runtime is irrelevant — only the absence of progress."""
    _tick = tick if tick is not None else max(1, min(WATCHDOG_TICK, hang_secs))
    _last_size = -1
    while not stop.is_set():
        stop.wait(_tick)
        if stop.is_set():
            return
        if state.get_phase() not in (PHASE_RUN, PHASE_BUILD_IMAGE):
            continue
        try:
            _size = os.path.getsize(log_path)
        except OSError:
            _size = _last_size
        _progress = _size > _last_size
        _last_size = max(_last_size, _size)
        if not _progress:
            _cpu = _container_cpu(container_name)
            if _cpu is not None and _cpu > CPU_PROGRESS_PCT:
                _progress = True
        if _progress:
            state.mark_progress()
            continue
        if state.progress_age() > hang_secs:
            state.set(watchdog='hang-killed')
            kill_build()
            return


# The running build subprocess (the --build-cmd path), so abort/watchdog can
# kill it.  The docker path is killed by `docker kill <name>` instead.
_build_proc: 'list[Optional[subprocess.Popen]]' = [None]
_proc_lock = threading.Lock()


def make_killer(container_name: 'Optional[str]') -> 'Callable[[], None]':
    """A callable that stops the build: `docker kill` the named container
    (the docker-run client dying does NOT stop the container), and/or kill the
    --build-cmd subprocess."""
    def _kill() -> None:
        if container_name:
            try:
                subprocess.run(["docker", "kill", container_name],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
        with _proc_lock:
            _p = _build_proc[0]
        if _p is not None and _p.poll() is None:
            try:
                _p.kill()
            except OSError:
                pass
    return _kill


def run_build(
    bundle: str, state: BuildState, container_name: str,
    build_cmd: 'Optional[str]', log_path: str, stop: threading.Event,
) -> None:
    """Run the build (docker path, or the --build-cmd override for tests),
    streaming combined output to `log_path`, then settle the final phase."""
    _out_dir = os.path.join(bundle, 'out')
    os.makedirs(_out_dir, exist_ok=True)
    _rc = 1
    try:
        with open(log_path, 'wb') as _log:
            if build_cmd is not None:
                state.set(phase=PHASE_RUN)
                _proc = subprocess.Popen(
                    ['bash', '-c', build_cmd], cwd=bundle,
                    stdout=_log, stderr=subprocess.STDOUT)
                with _proc_lock:
                    _build_proc[0] = _proc
                _rc = _proc.wait()
            else:
                with open(os.path.join(bundle, 'build.json')) as _fh:
                    _params = json.load(_fh)
                state.set(phase=PHASE_BUILD_IMAGE)
                remote_build.build_image(
                    bundle, _params['image_tag'],
                    _params.get('build_args', {}), out=_log)
                state.set(phase=PHASE_RUN)
                _rc = remote_build.run_container(
                    bundle, _params['image_tag'], _params['cmd_str'],
                    _params.get('build_cpus'), _params.get('build_memory'),
                    _params.get('localmirror_dir'),
                    container_name=container_name, out=_log)
    except (OSError, ValueError, KeyError, SystemExit) as _e:
        try:
            with open(log_path, 'ab') as _log:
                _log.write(f"[agent] build error: {_e}\n".encode())
        except OSError:
            pass
        state.set(phase=PHASE_FAILED, exit_code=2)
        stop.set()
        return

    _outputs = sorted(
        os.path.basename(_f)
        for _pat in ('*.deb', '*.udeb')
        for _f in glob.glob(os.path.join(_out_dir, _pat)))

    _snap = state.snapshot()
    if _snap['abort_requested']:
        state.set(phase=PHASE_ABORTED, exit_code=_rc, outputs=_outputs)
    elif _snap['watchdog'] == 'hang-killed':
        state.set(phase=PHASE_TIMEOUT, exit_code=_rc, outputs=_outputs)
    elif _rc == 0 and _outputs:
        state.set(phase=PHASE_DONE, exit_code=0, outputs=_outputs)
    elif _rc == 0:
        state.set(phase=PHASE_FAILED, exit_code=3, outputs=_outputs)  # built nothing
    else:
        state.set(phase=PHASE_FAILED, exit_code=_rc, outputs=_outputs)
    stop.set()


class _AgentServer(ThreadingHTTPServer):
    # injected after construction
    state: BuildState
    token: str
    log_path: str
    abort_cb: 'Callable[[], None]'
    shutdown_cb: 'Callable[[], None]'
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:        # silence default stderr spam
        pass

    def _srv(self) -> _AgentServer:
        _s = self.server
        assert isinstance(_s, _AgentServer)
        return _s

    def _auth_ok(self) -> bool:
        return self.headers.get('X-Athena-Token', '') == self._srv().token

    def _json(self, code: int, obj: dict) -> None:
        _b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(_b)))
        self.end_headers()
        self.wfile.write(_b)

    def _deny(self) -> None:
        self.send_response(401)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self) -> None:
        if not self._auth_ok():
            return self._deny()
        _path, _, _query = self.path.partition('?')
        if _path == '/status':
            return self._json(200, self._srv().state.snapshot())
        if _path == '/logs':
            _from = 0
            for _kv in _query.split('&'):
                if _kv.startswith('from='):
                    try:
                        _from = int(_kv[5:])
                    except ValueError:
                        _from = 0
            _data, _next, _size = read_log(self._srv().log_path, _from)
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('X-Log-Next', str(_next))
            self.send_header('X-Log-Size', str(_size))
            self.send_header('Content-Length', str(len(_data)))
            self.end_headers()
            self.wfile.write(_data)
            return
        self._json(404, {'error': 'not found'})

    def do_POST(self) -> None:
        if not self._auth_ok():
            return self._deny()
        if self.path == '/abort':
            self._srv().abort_cb()
            return self._json(200, {'aborted': True})
        if self.path == '/shutdown':
            self._json(200, {'shutdown': True})
            self._srv().shutdown_cb()
            return
        self._json(404, {'error': 'not found'})


def _write_sidecar(path: str, value: str) -> None:
    try:
        with open(path, 'w') as _fh:
            _fh.write(value)
    except OSError:
        pass


def main(argv: 'Optional[list]' = None) -> int:
    _p = argparse.ArgumentParser(
        prog='remote_agent.py',
        description='Drive one source build + serve a localhost poll API.')
    _p.add_argument('--bundle', default='.')
    _p.add_argument('--token-file', default=None,
                    help='file holding the per-remote API token (X-Athena-Token)')
    _p.add_argument('--port', type=int, default=0,
                    help='localhost port (0 = OS-assigned, the default)')
    _p.add_argument('--hang-secs', type=int, default=DEFAULT_HANG_SECS,
                    help='kill the build after this many seconds of NO progress')
    _p.add_argument('--build-cmd', default=None,
                    help='run this shell command as the build instead of the '
                         'docker path (used by the standalone tests)')
    _a = _p.parse_args(argv)

    _bundle = os.path.abspath(_a.bundle)
    _token = ''
    if _a.token_file:
        try:
            with open(_a.token_file) as _fh:
                _token = _fh.read().strip()
        except OSError:
            _token = ''
    _log_path = os.path.join(_bundle, 'build.log')
    _cname = container_name_for(_bundle)
    _state = BuildState()
    _stop = threading.Event()
    # The watchdog only stats a real container (docker path); for --build-cmd
    # it falls back to log-growth alone.
    _killer = make_killer(_cname if _a.build_cmd is None else None)

    _server = _AgentServer(('127.0.0.1', _a.port), _Handler)
    _server.state = _state
    _server.token = _token
    _server.log_path = _log_path

    def _abort() -> None:
        _state.set(abort_requested=True)
        _killer()

    _server.abort_cb = _abort
    _server.shutdown_cb = lambda: threading.Thread(
        target=_server.shutdown, daemon=True).start()

    _port = _server.server_address[1]
    _write_sidecar(os.path.join(_bundle, 'agent.port'), str(_port))
    _write_sidecar(os.path.join(_bundle, 'agent.pid'), str(os.getpid()))

    threading.Thread(
        target=run_build,
        args=(_bundle, _state, _cname, _a.build_cmd, _log_path, _stop),
        daemon=True).start()
    threading.Thread(
        target=watchdog_loop,
        args=(_state, _log_path,
              _cname if _a.build_cmd is None else None,
              _a.hang_secs, _stop, _killer),
        daemon=True).start()

    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
