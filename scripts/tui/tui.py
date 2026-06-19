"""Tui — top-level wiring.

Public surface mirrors legacy `scripts/tui.py:Tui` so call sites in
build.py don't change:
    tui = Tui(banner)
    tui.register_command(name, fn, tooltip)
    tui.run()
    tui.wait()
    tui.exit(code)

Internally: instantiates Renderer (which owns curses), Dispatcher
(which owns state), wires the input pump + shell + resource-monitor
threads, and registers itself as the singleton `tui_instance` on the
legacy `tui` module so the existing Console / Spinner / ProgressBar
facade resolution paths keep working.
"""
from __future__ import annotations

import curses
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil

from .dispatcher import Dispatcher
from .events import (
    ClearTab, ConsoleTrim, LogEvent, PrintEvent, Shutdown, StatusEvent,
    WidgetAdd, WidgetRemove,
)
from .logging_bridge import LOGGER_NAME, setup_logging
from .render import (
    COLOR_ERROR, COLOR_FOOTER, COLOR_HIGHLIGHT, COLOR_INFO, COLOR_NORMAL,
    COLOR_REVERSE, COLOR_WARNING, MIN_COLS, MIN_LINES, Renderer,
)
from .state import (
    DEFAULT_TABS, SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING, State,
)


class Tui:
    """Top-level TUI backend.  One per process."""

    # Mirror legacy class-level constants so callers that read
    # `Tui.COLOR_INFO` / `Tui.SEVERITY_*` keep working.
    COLOR_NORMAL    = COLOR_NORMAL
    COLOR_REVERSE   = COLOR_REVERSE
    COLOR_WARNING   = COLOR_WARNING
    COLOR_ERROR     = COLOR_ERROR
    COLOR_HIGHLIGHT = COLOR_HIGHLIGHT
    COLOR_FOOTER    = COLOR_FOOTER
    COLOR_INFO      = COLOR_INFO
    SEVERITY_ERROR   = SEVERITY_ERROR
    SEVERITY_WARNING = SEVERITY_WARNING
    SEVERITY_INFO    = SEVERITY_INFO
    MIN_COLS  = MIN_COLS
    MIN_LINES = MIN_LINES

    _instance: 'Optional[Tui]' = None

    def __init__(self, banner: str) -> None:
        if Tui._instance is not None:
            raise RuntimeError('Only one Tui instance allowed')
        Tui._instance = self
        self._banner = banner[:50]
        self._cmd_registry: Dict[str, Tuple[Callable[..., Any], str]] = {}
        self._exit_code = 0
        # --yes auto-answer flag.  Set by build.py:main() from
        # argv.  Only consulted by `Prompt(..., informational=True)`
        # — hard prompts (sudo password, OPTIONS) always wait for
        # operator input regardless.
        self.auto_yes: bool = False

        # Curses setup (must happen on main thread before any input pump).
        self._stdscr = curses.initscr()
        try:
            curses.noecho()
            curses.cbreak()
            curses.curs_set(0)
        except curses.error:
            pass

        # Wire renderer + dispatcher + facades.
        self._renderer = Renderer(self._stdscr)
        self.dispatcher = Dispatcher(self._renderer)
        self.dispatcher.state.banner = self._banner
        self.dispatcher.set_command_names_source(lambda: list(self._cmd_registry))
        # Status-tab content provider: a callable returning (text, COLOR_*)
        # rows.  Wired by build.py:main() to print_commands.status_lines.
        # Re-rendered after every command so the 'status' tab is current.
        self._status_provider: 'Optional[Callable[[], list]]' = None

        # Optional command gate: name -> bool.  When set (build.py wires it to
        # onboarding.command_allowed), _shell refuses non-allowlisted commands
        # until the box is configured, pointing the operator at `configure`.
        self.command_gate: 'Optional[Callable[[str], bool]]' = None

        # Register as the singleton tui_instance — exposed at the
        # package level via tui/__init__.py.  Console / Prompt /
        # ProgressBar / Spinner facades resolve through this at call
        # time so no per-facade wiring is needed.
        import tui as _pkg
        _pkg.tui_instance = self

        setup_logging(self.dispatcher)

        # Built-in commands.
        self.register_command('help',    self._cmd_help,    'List registered commands')
        self.register_command('clear',   self._cmd_clear,   'Clear tab buffer: clear <name|all>')
        self.register_command('history', self._cmd_history, 'Show command history')
        self.register_command('quit',    self.exit,         'Exit the application')

    # ─── Public API ──────────────────────────────────────────────────────
    def register_command(self, name: str, fn: Callable[..., Any],
                          tooltip: str = '') -> None:
        name = name.strip()
        if name and name not in self._cmd_registry:
            self._cmd_registry[name] = (fn, tooltip)

    def run(self) -> None:
        """Launch dispatcher + input pump + shell + status monitor threads."""
        from .input_pump import start_input_pump

        # Status monitor (psutil sampling).
        threading.Thread(target=self._status_pump, daemon=True,
                          name='tui-status').start()
        # Shell loop — blocking request_prompt + dispatch.
        threading.Thread(target=self._shell, daemon=True,
                          name='tui-shell').start()
        # Input pump — blocking getkey -> KeyEvent.
        start_input_pump(self._stdscr, self.dispatcher)

        # Dispatcher loop (and thus all curses redraws) runs on a dedicated
        # non-daemon `tui-dispatch` thread; run() returns to the caller and
        # wait() joins it.
        self._dispatcher_thread = threading.Thread(
            target=self._run_dispatcher, daemon=False, name='tui-dispatch')
        self._dispatcher_thread.start()

    def wait(self) -> None:
        if hasattr(self, '_dispatcher_thread'):
            self._dispatcher_thread.join()
        if self._exit_code != 0:
            print(f'Exited with error code: {self._exit_code}\r\n')

    def exit(self, error_code: int = 0) -> None:
        try:
            code = int(error_code)
        except (TypeError, ValueError):
            code = 0
        self._exit_code = code
        self.dispatcher.post(Shutdown(code))

    def sig_shutdown(self, signum: int, frame: Any) -> None:
        # 128 + SIGINT(2) = 130, the POSIX convention — same code the Cli
        # backend reports, so a Ctrl-C'd run exits 130 in either mode.
        self.exit(130)

    # ─── Legacy-compatibility proxy methods ──────────────────────────────
    # The existing `scripts/tui.py:Console` facade resolves
    # `tui_instance.X(...)` directly.  build.py imports `console`,
    # `Prompt`, `ProgressBar`, `Spinner` from the legacy module — they
    # all dispatch through `tui_instance`.  Expose the same surface
    # here so v2 is a drop-in replacement without touching call sites.

    def print(self, message: str, attribute: Optional[int] = None) -> None:
        """Legacy Console.print → PrintEvent.

        `attribute` from callers is a COLOR_* index (1..7).  Resolve
        it to a curses attr HERE (producer side) so PrintEvents
        arrive at the dispatcher pre-resolved — buffer entries store
        usable attrs and there's no SEVERITY_*/COLOR_* sentinel
        collision in the renderer."""
        attr = self._renderer.attr_for_color(attribute)
        self.dispatcher.post(PrintEvent(message, attr))

    def INFO(self, message: str) -> None:
        self.dispatcher.post(LogEvent(SEVERITY_INFO, f'[INFO ] {message}'))

    def WARNING(self, message: str) -> None:
        self.dispatcher.post(LogEvent(SEVERITY_WARNING, f'[WARN ] {message}'))

    def ERROR(self, message: str) -> None:
        self.dispatcher.post(LogEvent(SEVERITY_ERROR, f'[ERROR] {message}'))

    def console_mark(self) -> int:
        return self.dispatcher.console_mark()

    def console_trim_to(self, mark: int) -> None:
        self.dispatcher.post(ConsoleTrim(mark))

    def add_widget(self, widget: object) -> int:
        """Add a widget (legacy ProgressBar/Spinner construction path).

        Posts a WidgetAdd event so the renderer picks it up.  Returns
        id(widget) immediately so the legacy widget's __init__ has
        something to store in `self._widget_id` — that id is what
        WidgetRemove uses at close()."""
        self.dispatcher.post(WidgetAdd(widget))
        return id(widget)

    def del_widget(self, wid: int) -> None:
        self.dispatcher.post(WidgetRemove(wid))

    def prompt(self, message: str, masked: bool = False,
                keymode: bool = False) -> str:
        """Legacy Prompt.get_response calls this directly on
        tui_instance.  Translates the masked/keymode flags into v2's
        single `mode` string and round-trips through the dispatcher."""
        mode = ('key' if keymode else ('masked' if masked else 'line'))
        return self.dispatcher.request_prompt(message, mode)

    # ─── Interactive-tab API (COMP-06 package selector) ──────────────────
    def add_tab(self, name: str) -> None:
        """Create a tab at runtime (idempotent)."""
        from .events import TabAdd
        self.dispatcher.post(TabAdd(name))

    def remove_tab(self, name: str) -> None:
        """Remove a tab; if it was active, the dispatcher activates the
        first remaining tab."""
        from .events import TabRemove
        self.dispatcher.post(TabRemove(name))

    def activate_tab(self, name: str) -> None:
        """Switch the active tab by name."""
        from .events import TabActivate
        self.dispatcher.post(TabActivate(name))

    def set_tab_buffer(self, name: str, rows: list) -> None:
        """Replace a tab's buffer with `rows` (list of (text, attr)).
        Used by interactive controllers that render their own view."""
        from .events import SetTabBuffer
        self.dispatcher.post(SetTabBuffer(name, rows))

    def set_tab_key_handler(self, name: str, fn) -> None:
        """Register an interceptor that owns keystrokes while `name`
        is active.  fn(key) -> bool: True = consumed, False = fall
        through (so F-keys still switch tabs)."""
        self.dispatcher.set_key_interceptor(name, fn)

    def clear_tab_key_handler(self) -> None:
        self.dispatcher.clear_key_interceptor()

    def viewport_rows(self) -> int:
        """Tab content-row count — for sizing an interactive view."""
        return self.dispatcher.viewport_rows()

    def attr_reverse(self) -> int:
        """curses.A_REVERSE — exposed so controllers can highlight the
        cursor row without importing curses directly."""
        import curses
        return curses.A_REVERSE

    def attr_color(self, color_index: int) -> int:
        """Resolve a COLOR_* index to a curses attr (for controllers)."""
        return self._renderer.attr_for_color(color_index)

    # ─── Internal: dispatcher thread wrapper ─────────────────────────────
    def _run_dispatcher(self) -> None:
        try:
            self._exit_code = self.dispatcher.run()
        finally:
            self._renderer.shutdown()

    # ─── Status monitor (psutil sampling) ────────────────────────────────
    def _status_pump(self) -> None:
        _net0 = None
        try:
            _net0 = psutil.net_io_counters()
        except Exception:
            _net0 = None
        while not self.dispatcher.state.quit:
            try:
                # cpu_percent(interval=2) blocks ~2s and is our sample
                # window; bracket the network counters around it so up/down
                # are bytes/s over that same window.
                _t0 = time.monotonic()
                cpu  = psutil.cpu_percent(interval=2)
                mem  = psutil.virtual_memory().percent
                disk = psutil.disk_usage('/').percent
                up = down = 0.0
                try:
                    _net1 = psutil.net_io_counters()
                    _dt = max(0.5, time.monotonic() - _t0)
                    if _net0 is not None:
                        up   = max(0.0, (_net1.bytes_sent - _net0.bytes_sent) / _dt)
                        down = max(0.0, (_net1.bytes_recv - _net0.bytes_recv) / _dt)
                    _net0 = _net1
                except Exception:
                    pass
                self.dispatcher.post(StatusEvent(cpu, mem, disk, up, down))
            except Exception:
                time.sleep(2)

    # ─── Shell loop ──────────────────────────────────────────────────────
    def _shell(self) -> None:
        while not self.dispatcher.state.quit:
            try:
                line = self.dispatcher.request_prompt('> ', mode='line').strip()
            except Exception:
                # request_prompt raises if its Future was cancelled.  On
                # shutdown that's terminal — exit the loop.  Otherwise a
                # concurrent prompt displaced ours; re-request rather than
                # killing the shell (which would silently ignore every
                # subsequent command).
                if self.dispatcher.state.quit:
                    return
                continue
            if not line:
                continue
            self.dispatcher.post(PrintEvent(f'> {line}',
                                             COLOR_HIGHLIGHT))
            self.dispatcher.state.cmd.push_history(line)
            parts = line.split()
            entry = self._cmd_registry.get(parts[0])
            if entry is None:
                self.dispatcher.post(PrintEvent(
                    f'  Unknown command: "{parts[0]}"  — type "help"'))
                continue
            if self.command_gate is not None and not self.command_gate(parts[0]):
                self.dispatcher.post(PrintEvent(
                    f'  "{parts[0]}" is unavailable until this build system '
                    'is configured — run `configure` first.'))
                continue
            fn, _tip = entry
            try:
                fn(*parts[1:])
            except SystemExit:
                raise
            except Exception as e:
                self.dispatcher.post(PrintEvent(f'  Error: {e}'))
                import logging
                logging.getLogger(LOGGER_NAME).error(
                    f'{type(e).__name__}: {e}')
            # Refresh the status tab after every command — build-environment
            # actions (cache/parse/build/iso) settle into flag + state
            # changes the operator can review by flipping to F3.
            self._refresh_status()

    def set_status_provider(self, provider: 'Callable[[], list]') -> None:
        """Register the status-tab content source (build.py:main wires
        print_commands.status_lines bound to the session) and render once."""
        self._status_provider = provider
        self._refresh_status()

    def _refresh_status(self) -> None:
        """Regenerate the 'status' tab: clear it, then post the provider's
        (text, COLOR_*) rows.  No-op when no provider is wired or the
        provider raises (status is a convenience view, never load-bearing)."""
        if self._status_provider is None:
            return
        try:
            _rows = self._status_provider()
        except Exception:
            return
        self.dispatcher.post(ClearTab('status'))
        for _text, _color in _rows:
            _attr = self._renderer.attr_for_color(_color)
            self.dispatcher.post(PrintEvent(_text, _attr, tab='status'))

    # ─── Built-in commands ──────────────────────────────────────────────
    def _cmd_help(self) -> None:
        self.dispatcher.post(PrintEvent('  Available commands:'))
        for name, (_fn, tip) in self._cmd_registry.items():
            self.dispatcher.post(PrintEvent(f'    {name:<14}{tip}'))

    def _cmd_clear(self, name: str = 'console') -> None:
        self.dispatcher.post(ClearTab(name))

    def _cmd_history(self) -> None:
        for entry in self.dispatcher.state.cmd.history[-50:]:
            self.dispatcher.post(PrintEvent(f'  {entry}'))
