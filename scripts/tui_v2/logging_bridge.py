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
    """athena.<stage> -> '<stage>'; bare athena / unknown -> 'log'."""
    if name.startswith(LOGGER_NAME + '.'):
        return name[len(LOGGER_NAME) + 1:].split('.', 1)[0]
    return 'log'


class _LogTabHandler(logging.Handler):
    """Routes >= INFO records to a tab via LogEvent.

    Dispatcher binding is captured at construction so tests can
    inject a stub.  Falls back to module-level _dispatcher if None."""

    def __init__(self, dispatcher: Any, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._dispatcher = dispatcher

    def emit(self, record: logging.LogRecord) -> None:
        try:
            d = self._dispatcher
            if d is None:
                return
            msg = self.format(record)
            if record.levelno >= logging.ERROR:
                sev = SEVERITY_ERROR
            elif record.levelno >= logging.WARNING:
                sev = SEVERITY_WARNING
            else:
                sev = SEVERITY_INFO
            d.post(LogEvent(sev, msg, tab=_tab_for_logger(record.name)))
        except Exception:
            self.handleError(record)


class _ConsoleTabHandler(logging.Handler):
    """Routes DISPLAY-level records to the console tab via PrintEvent."""

    def __init__(self, dispatcher: Any) -> None:
        super().__init__(level=DISPLAY)
        self._dispatcher = dispatcher

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._dispatcher is None:
                return
            attr = getattr(record, 'attribute', 0) or 0
            self._dispatcher.post(PrintEvent(record.getMessage(), attr))
        except Exception:
            self.handleError(record)


def setup_logging(dispatcher: Any) -> logging.Logger:
    """Bind the athena logger to route through the dispatcher.

    Idempotent: removes any previously-attached tab handlers before
    attaching fresh ones.  Leaves FileHandler / NullHandler /
    captureWarnings handlers alone."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        if isinstance(h, (_LogTabHandler, _ConsoleTabHandler)):
            logger.removeHandler(h)
    log_h = _LogTabHandler(dispatcher, level=logging.INFO)
    log_h.addFilter(lambda r: r.levelno != DISPLAY)
    logger.addHandler(log_h)
    con_h = _ConsoleTabHandler(dispatcher)
    con_h.addFilter(lambda r: r.levelno == DISPLAY)
    logger.addHandler(con_h)
    return logger
