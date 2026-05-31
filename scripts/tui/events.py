"""Events — the messages that flow over the dispatcher bus.

Every input to the TUI enters as an Event.  Producers (curses input pump,
shell thread, logger handler, psutil monitor, Console.print, Prompt
constructors, widget `step()` calls) all do exactly one thing: build an
Event and call `dispatcher.post(event)`.  The dispatcher pops events from
a single queue and mutates state — no shared mutable state, no locks.

All events are frozen dataclasses so they can't be partially mutated en
route to the dispatcher.  Match-case in dispatcher._handle keys off the
runtime type, which is why every event is its own class rather than one
enum-tagged record.
"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass(frozen=True)
class KeyEvent:
    """A single keystroke from the curses input pump.

    `key` is whatever `stdscr.getkey()` returned — single char for
    printables, `'KEY_F(3)'` / `'KEY_LEFT'` etc. for special keys, and
    `'KEY_RESIZE'` for terminal resize."""
    key: str


@dataclass(frozen=True)
class PrintEvent:
    """Append a line of operator-facing output to a tab buffer.

    Caller-facing wrapper is `Console.print(...)` (facade.py).  `attr`
    is a curses attribute (color pair, A_BOLD, etc.) — already-resolved
    int so the dispatcher doesn't need to know about curses constants."""
    text: str
    attr: int = 0
    tab: str = 'console'


@dataclass(frozen=True)
class LogEvent:
    """Diagnostic log record (INFO / WARNING / ERROR severity).

    Routed by `logging_bridge._LogTabHandler` from
    `logging.getLogger('athena[.stage]')`.  `severity` is one of
    State.SEVERITY_* constants — pre-mapped from logging levelno so
    the dispatcher doesn't import `logging`."""
    severity: int
    text: str
    tab: str = 'build'


@dataclass(frozen=True)
class StatusEvent:
    """Resource-monitor update (psutil sample).

    Posted ~every 2s by the status_pump thread.  Dispatcher updates
    State.status; renderer reads it for the footer status bar."""
    cpu: float
    mem: float
    disk: float


@dataclass(frozen=True)
class WidgetAdd:
    """Register a live widget (ProgressBar, Spinner, …)."""
    widget: Any         # Widget protocol — duck-typed to keep this module light


@dataclass(frozen=True)
class WidgetRemove:
    """Deregister a live widget by its id (id(widget))."""
    widget_id: int


@dataclass(frozen=True)
class PromptRequest:
    """A blocking input request from a non-dispatcher thread.

    Caller (shell thread or a cmd_* handler) calls
    `dispatcher.request_prompt(message, mode)` which:
      1. Builds this event with a fresh Future,
      2. Posts it,
      3. Blocks on `future.result()` until the dispatcher fulfills it.

    `mode` is one of: 'line' (newline-terminated input), 'key' (single
    keypress, e.g. PAUSE), 'masked' (line input echoed as '*').  The
    dispatcher resolves the future when the user submits."""
    message: str
    mode: str
    future: 'Future[str]'


@dataclass(frozen=True)
class TabActivate:
    """Switch the active tab by name (F1..Fn / explicit selection)."""
    name: str


@dataclass(frozen=True)
class TabAdd:
    """Create a new tab at runtime.  Idempotent: re-adding an existing
    name is a no-op (matches the legacy add_tab contract)."""
    name: str


@dataclass(frozen=True)
class ClearTab:
    """Wipe a tab's buffer.  `name='all'` wipes every tab."""
    name: str = 'console'


@dataclass(frozen=True)
class ConsoleMark:
    """Capture the console tab's current length into a Future.

    Caller threads do `mark = dispatcher.console_mark()` which packages
    this event with a Future; dispatcher reads `len(state.tabs['console'].
    buffer)` and resolves the future.  The caller later passes the mark
    to ConsoleTrim to undo any prints made between."""
    future: 'Future[int]'


@dataclass(frozen=True)
class ConsoleTrim:
    """Truncate the console buffer back to a previously recorded mark."""
    mark: int


@dataclass(frozen=True)
class TabRemove:
    """Remove a tab by name (COMP-06 selector teardown).  If the
    removed tab was active, the dispatcher activates the first
    remaining tab.  Default tabs can be removed too — caller's
    responsibility not to orphan the UI."""
    name: str


@dataclass(frozen=True)
class SetTabBuffer:
    """Replace a tab's buffer wholesale with `rows` (list of
    (text, attr) tuples).  Used by interactive controllers (the
    package selector) that render their own view rather than
    appending log lines.  scroll_offset is reset to 0 so the
    controller-supplied rows render top-aligned."""
    name: str
    rows: list


@dataclass(frozen=True)
class Shutdown:
    """Stop the dispatcher loop with `code` as the exit status.  After
    Shutdown is handled the loop exits; producers posting further events
    silently drop them (the input pump and resource monitor are daemon
    threads and die with the process)."""
    code: int = 0


# ── List of all event types (handy for tests / discovery) ────────────────
ALL_EVENT_TYPES: List[type] = [
    KeyEvent, PrintEvent, LogEvent, StatusEvent,
    WidgetAdd, WidgetRemove,
    PromptRequest, TabActivate, TabAdd, ClearTab,
    ConsoleMark, ConsoleTrim, TabRemove, SetTabBuffer, Shutdown,
]
