"""tui — event-dispatcher TUI for Athena-Build.

Public surface (everything callers should import from `tui`):

Backend:
    Tui                — top-level class; one per process.
    tui_instance       — singleton, set by Tui.__init__ / Cli.__init__.
    Exit               — graceful shutdown helper used by build.py.

Console / output:
    Console, console   — facade + module-level singleton.

Prompts:
    Prompt             — blocking input request widget.
    PROMPT_YESNO, PROMPT_INPUT, PROMPT_OPTIONS, PROMPT_PASSWORD, PROMPT_PAUSE

Widgets:
    ProgressBar, Spinner

Colors (curses color-pair indices, passed to Console.print(msg, attr)):
    COLOR_NORMAL, COLOR_REVERSE, COLOR_WARNING, COLOR_ERROR,
    COLOR_HIGHLIGHT, COLOR_FOOTER, COLOR_INFO

Severity (logger handler routing):
    SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_ERROR
    DISPLAY (custom logging level for operator-facing console output).

Logging:
    setup_logging(dispatcher)               — bind 'athena' handlers.
    setup_file_logging(dir, name='build')   — attach a FileHandler.
    LOGGER_NAME = 'athena'

Internals (don't import from outside this package):
    .events, .state, .wrap, .dispatcher,
    .render, .input_pump, .widgets, .facade, .logging_bridge, .tui

Architecture:
    Single event queue.  One dispatcher thread is the sole state
    mutator.  Pure renderer.  Adaptive idle (events.get(timeout=...)
    blocks until an animation deadline or the next producer post).
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
from typing import Optional

# Re-exports — public API
from .facade import (  # noqa: F401
    Console, Prompt, console,
    PROMPT_INPUT, PROMPT_OPTIONS, PROMPT_PASSWORD, PROMPT_PAUSE, PROMPT_YESNO,
)
from .logging_bridge import (  # noqa: F401
    DISPLAY, LOGGER_NAME, setup_logging,
)
from .render import (  # noqa: F401
    COLOR_ERROR, COLOR_FOOTER, COLOR_HIGHLIGHT, COLOR_INFO, COLOR_NORMAL,
    COLOR_REVERSE, COLOR_WARNING,
)
from .state import (  # noqa: F401
    SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING,
)
from .tui import Tui  # noqa: F401
from .widgets import ProgressBar, Spinner  # noqa: F401


# ── Module-level singleton ──────────────────────────────────────────────
# Set by Tui.__init__ (curses backend) or Cli.__init__ (headless mirror).
# Console / Prompt / ProgressBar / Spinner facades resolve through this
# at call time, so the singleton can be swapped without re-importing.
tui_instance: Optional[object] = None


# ── setup_file_logging — attach a FileHandler to the athena logger ──────
def setup_file_logging(log_dir: str, name: str = 'build') -> str:
    """Attach a FileHandler to the 'athena' logger.

    Filename is timestamped (`{name}-YYYY-MM-DDTHH-MM-SS.log`) so each
    run lands in its own file rather than overwriting prior runs.  The
    handler captures DEBUG and above so subprocess transcripts routed
    through `logger.debug` end up in the file alongside INFO/WARNING/
    ERROR records.

    Returns the absolute path of the newly-opened log file.
    """
    os.makedirs(log_dir, exist_ok=True)
    ts   = datetime.datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
    path = os.path.abspath(os.path.join(log_dir, f'{name}-{ts}.log'))

    fh = logging.FileHandler(path, mode='w', encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)-7s] %(message)s', '%Y-%m-%d %H:%M:%S'
    ))
    logging.getLogger(LOGGER_NAME).addHandler(fh)
    return path


# ── Module-level command registration helper ────────────────────────────
def register_command(name: str, fn, tooltip: str = '') -> None:
    """Convenience: forward to `tui_instance.register_command`.

    Lets call sites use `tui.register_command(...)` without first
    grabbing the singleton.  No-op when no backend is bound."""
    if tui_instance is not None:
        tui_instance.register_command(name, fn, tooltip)   # type: ignore[attr-defined]


# ── Exit — graceful shutdown wrapper used by build.py ───────────────────
def Exit(err_code: int = 0) -> None:
    """Shut down the active TUI/Cli cleanly, then sys.exit(err_code).

    Resolves the singleton tui_instance, calls .exit() + .wait() if
    present, prints a brief termination notice, and exits the process.
    Safe to call when no backend was ever constructed (still exits).
    """
    global tui_instance
    if tui_instance is not None:
        try:
            tui_instance.exit(err_code)        # type: ignore[attr-defined]
            tui_instance.wait()                # type: ignore[attr-defined]
        except Exception:
            pass
        tui_instance = None
    print('Shutdown TUI\n')
    sys.exit(err_code)
