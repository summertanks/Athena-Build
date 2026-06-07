"""API-01 chunk 4 — job queue + Api console backend.

The command dispatcher is deliberately thin: an HTTP POST enqueues the
raw command string; the **main thread** (running ``ApiBackend.wait()``,
exactly where the REPL loop would sit) pops jobs one at a time and feeds
them through the same ``Cli._dispatch_one`` the headless CLI uses.  One
queue + one executor thread = the single-writer invariant holds by
construction; uvicorn serves HTTP from a daemon thread and only ever
touches the queue.

``ApiBackend`` subclasses ``Cli`` so the entire console-facade surface
(widgets, marks, register_command, 291 print callsites) is inherited;
only three behaviours change:

  * output lands in the current job's buffer (and the logger) instead
    of stdout;
  * ``prompt()`` NEVER blocks — interactive input cannot transit HTTP,
    so a command that needs a prompt fails fast with a structured
    ``prompt_required`` error (sudo flows use ATHENA_SUDO_PASSWORD on
    the server process per UX-05b; ``--yes`` informational prompts are
    inherited from Cli untouched);
  * ``wait()`` runs the job loop instead of a stdin REPL.
"""

import logging
import queue
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from cli import Cli

logger = logging.getLogger('athena.webapi')


class PromptRequired(Exception):
    """Raised inside a job when a command asks for interactive input."""


class Job:
    __slots__ = ('id', 'cmd', 'state', 'output', 'error',
                 'submitted', 'started', 'finished')

    def __init__(self, cmd: str):
        self.id = uuid.uuid4().hex[:12]
        self.cmd = cmd
        self.state = 'queued'          # queued → running → done | error
        self.output: 'List[str]' = []
        self.error: 'Optional[str]' = None
        self.submitted = time.time()
        self.started: 'Optional[float]' = None
        self.finished: 'Optional[float]' = None

    def as_dict(self) -> 'Dict[str, Any]':
        return {
            'id': self.id, 'cmd': self.cmd, 'state': self.state,
            'output': list(self.output), 'error': self.error,
            'submitted': self.submitted, 'started': self.started,
            'finished': self.finished,
            'elapsed': (round(self.finished - self.started, 3)
                        if self.started and self.finished else None),
        }


class ApiBackend(Cli):
    """Console-facade backend for ``--api`` mode.  See module docstring."""

    def __init__(self) -> None:
        super().__init__()             # registers self as tui.tui_instance
        self._queue: 'queue.Queue[Optional[Job]]' = queue.Queue()
        self._jobs: 'Dict[str, Job]' = {}
        self._jobs_lock = threading.Lock()
        self._current: 'Optional[Job]' = None
        self._shutdown = threading.Event()

    # ── job api (called from uvicorn's thread via the routes) ──────────

    def known_command(self, line: str) -> bool:
        _first = (line.split() or [''])[0]
        return _first in self._cmds

    def submit(self, cmd: str) -> Job:
        _job = Job(cmd)
        with self._jobs_lock:
            self._jobs[_job.id] = _job
        self._queue.put(_job)
        return _job

    def get_job(self, job_id: str) -> 'Optional[Job]':
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> 'List[Dict[str, Any]]':
        with self._jobs_lock:
            _items = sorted(self._jobs.values(), key=lambda j: j.submitted)
        return [{'id': _j.id, 'cmd': _j.cmd, 'state': _j.state,
                 'submitted': _j.submitted} for _j in _items]

    def shutdown(self) -> None:
        self._shutdown.set()
        self._queue.put(None)          # unblock the worker

    # ── facade overrides ────────────────────────────────────────────────

    def print(self, message: str, attribute: 'Optional[int]' = None) -> None:
        # Capture only — deliberately NO logger call here: Cli binds a
        # logging handler that routes log records back into the backend
        # (so they show on the console surface), and print→logger→
        # handler→INFO→print would feed back forever.  Log lines still
        # reach the job buffer via that handler → INFO → print.
        _job = self._current
        if _job is not None:
            _job.output.append(str(message))

    # Cli's severity trio writes to stderr via builtin print() — invisible
    # to an HTTP client.  Route them through self.print so diagnostics
    # (notably _dispatch_one's "command X raised …" line) land in the
    # job's output buffer.
    def INFO(self, message: str) -> None:
        self.print(f'[INFO ] {message}')

    def WARNING(self, message: str) -> None:
        self.print(f'[WARN ] {message}')

    def ERROR(self, message: str) -> None:
        self.print(f'[ERROR] {message}')

    def prompt(self, message: str, masked: bool = False,
               keymode: bool = False) -> str:
        if keymode:
            return ''                  # pause prompts are no-ops headlessly
        raise PromptRequired(message)

    # ── executor loop (runs on the MAIN thread, replaces the REPL) ──────

    def wait(self) -> None:
        # Drain-then-stop: jobs queued BEFORE shutdown() still execute
        # (the None sentinel sits behind them in FIFO order); the event
        # check only covers shutdown-without-sentinel (SIGINT path).
        while True:
            try:
                _job = self._queue.get(timeout=1.0)
            except queue.Empty:
                if self._shutdown.is_set() or self._exit_code is not None:
                    break
                continue
            if _job is None:
                break
            _job.state = 'running'
            _job.started = time.time()
            self._current = _job
            try:
                _ok = self._dispatch_one(_job.cmd)
                _job.state = 'done' if _ok else 'error'
                if not _ok and _job.error is None:
                    _job.error = (_job.output[-1] if _job.output
                                  else 'command failed')
            except PromptRequired as _p:
                # raised through _dispatch_one only when a handler lets
                # it propagate; normally _dispatch_one catches it and
                # we land in the not-_ok branch with the ERROR line.
                _job.state = 'error'
                _job.error = f'prompt_required: {_p}'
            finally:
                _job.finished = time.time()
                self._current = None
        if self._exit_code is None:
            self._exit_code = 0
