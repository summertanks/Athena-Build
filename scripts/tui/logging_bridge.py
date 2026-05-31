"""logging.Handler -> dispatcher events.

Mirror of `_LogTabHandler` / `_ConsoleTabHandler` from legacy tui.py
but emits Events instead of calling methods directly.  Routes by
logger name (`athena.<stage>` -> `<stage>` tab) per ARCH-14 P6.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .events import LogEvent, PrintEvent
from .state import SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING


LOGGER_NAME = 'athena'

# Custom level for "operator-facing console output" — between INFO
# (20) and WARNING (30).  Matches legacy DISPLAY constant.
DISPLAY = 25
logging.addLevelName(DISPLAY, 'DISPLAY')


def _tab_for_logger(name: str) -> str:
    """athena.<stage> -> '<stage>'; bare athena / unknown -> 'build'.

    The 'build' fallback covers cross-stage helper modules (utils,
    signing, apt_repo, repo_audit, persistence, cli) that don't bind
    to a specific pipeline stage.  Their records — repo Release gen,
    GPG signing, file downloads, session save/restore — live with the
    orchestrator's tab since build.py drives the dispatch."""
    if name.startswith(LOGGER_NAME + '.'):
        return name[len(LOGGER_NAME) + 1:].split('.', 1)[0]
    return 'build'


class _LogTabHandler(logging.Handler):
    """Routes >= INFO records to a tab.

    Backend is duck-typed:
      - has `.post`  → Dispatcher: post LogEvent.
      - else          → Cli-like: call ERROR/WARNING/INFO directly.

    Lets the same setup_logging serve both the curses TUI (via the
    Dispatcher) and the headless Cli backend without per-backend
    branching at the call site."""

    def __init__(self, backend: Any, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._backend = backend

    def emit(self, record: logging.LogRecord) -> None:
        try:
            b = self._backend
            if b is None:
                return
            msg = self.format(record)
            if hasattr(b, 'post'):
                if record.levelno >= logging.ERROR:
                    sev = SEVERITY_ERROR
                elif record.levelno >= logging.WARNING:
                    sev = SEVERITY_WARNING
                else:
                    sev = SEVERITY_INFO
                b.post(LogEvent(sev, msg, tab=_tab_for_logger(record.name)))
            else:
                # Cli-like backend — direct method dispatch.
                if record.levelno >= logging.ERROR:
                    b.ERROR(msg)
                elif record.levelno >= logging.WARNING:
                    b.WARNING(msg)
                else:
                    b.INFO(msg)
        except Exception:
            self.handleError(record)


class _ConsoleTabHandler(logging.Handler):
    """Routes DISPLAY-level records to the console tab.

    Same dual-backend dispatch as _LogTabHandler — Dispatcher gets
    a PrintEvent posted; Cli-like backends get .print called directly."""

    def __init__(self, backend: Any) -> None:
        super().__init__(level=DISPLAY)
        self._backend = backend

    def emit(self, record: logging.LogRecord) -> None:
        try:
            b = self._backend
            if b is None:
                return
            attr = getattr(record, 'attribute', 0) or 0
            if hasattr(b, 'post'):
                b.post(PrintEvent(record.getMessage(), attr))
            else:
                b.print(record.getMessage(), attr)
        except Exception:
            self.handleError(record)


def setup_logging(backend: Any = None, tui: Any = None,
                   dispatcher: Any = None) -> logging.Logger:
    """Bind the 'athena' logger to route through *backend*.

    `backend` is either a Dispatcher (has `.post` → events posted) or
    a Cli-like instance (has `.INFO/.WARNING/.ERROR/.print` → direct
    method calls).  The two are duck-typed so callers don't branch.

    `tui=` and `dispatcher=` kwargs are accepted as aliases for
    backwards-compat with call sites that predate the event-loop
    cutover (legacy Tui.__init__ called `setup_logging(tui=self)`).

    Idempotent: removes any previously-attached _LogTabHandler /
    _ConsoleTabHandler before attaching fresh ones.  Leaves
    FileHandler / NullHandler / captureWarnings handlers alone."""
    target = backend if backend is not None else (
        dispatcher if dispatcher is not None else tui
    )
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        if isinstance(h, (_LogTabHandler, _ConsoleTabHandler)):
            logger.removeHandler(h)
    log_h = _LogTabHandler(target, level=logging.INFO)
    log_h.addFilter(lambda r: r.levelno != DISPLAY)
    logger.addHandler(log_h)
    con_h = _ConsoleTabHandler(target)
    con_h.addFilter(lambda r: r.levelno == DISPLAY)
    logger.addHandler(con_h)
    return logger
