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

from . import facade, widgets
from .dispatcher import Dispatcher
from .events import (
    ClearTab, ConsoleTrim, LogEvent, PrintEvent, Shutdown, StatusEvent,
    WidgetAdd, WidgetRemove, WidgetTick,
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
            raise RuntimeError('Only one Tui_v2 instance allowed')
        Tui._instance = self
        self._banner = banner[:50]
        self._cmd_registry: Dict[str, Tuple[Callable[..., Any], str]] = {}
        self._exit_code = 0

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

        # Bind the singleton facades to this dispatcher.
        facade.set_dispatcher(self.dispatcher)
        widgets.set_dispatcher(self.dispatcher)

        # Register as the legacy tui_instance so Console / Prompt /
        # ProgressBar fall-back paths in tui.py find us.
        import tui as _legacy_tui
        _legacy_tui.tui_instance = self

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
                          name='tui_v2-status').start()
        # Shell loop — blocking request_prompt + dispatch.
        threading.Thread(target=self._shell, daemon=True,
                          name='tui_v2-shell').start()
        # Input pump — blocking getkey -> KeyEvent.
        start_input_pump(self._stdscr, self.dispatcher)

        # Dispatcher loop runs on this thread (so curses redraws happen
        # here — same thread as initscr, required by curses).
        self._dispatcher_thread = threading.Thread(
            target=self._run_dispatcher, daemon=False, name='tui_v2-dispatch')
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
        self.exit(signum)

    # ─── Legacy-compatibility proxy methods ──────────────────────────────
    # The existing `scripts/tui.py:Console` facade resolves
    # `tui_instance.X(...)` directly.  build.py imports `console`,
    # `Prompt`, `ProgressBar`, `Spinner` from the legacy module — they
    # all dispatch through `tui_instance`.  Expose the same surface
    # here so v2 is a drop-in replacement without touching call sites.

    def print(self, message: str, attribute: Optional[int] = None) -> None:
        """Legacy Console.print → PrintEvent."""
        self.dispatcher.post(PrintEvent(message, attribute or 0))

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

    # ─── Internal: dispatcher thread wrapper ─────────────────────────────
    def _run_dispatcher(self) -> None:
        try:
            self._exit_code = self.dispatcher.run()
        finally:
            self._renderer.shutdown()

    # ─── Status monitor (psutil sampling) ────────────────────────────────
    def _status_pump(self) -> None:
        while not self.dispatcher.state.quit:
            try:
                cpu  = psutil.cpu_percent(interval=2)
                mem  = psutil.virtual_memory().percent
                disk = psutil.disk_usage('/').percent
                self.dispatcher.post(StatusEvent(cpu, mem, disk))
            except Exception:
                time.sleep(2)

    # ─── Shell loop ──────────────────────────────────────────────────────
    def _shell(self) -> None:
        while not self.dispatcher.state.quit:
            try:
                line = self.dispatcher.request_prompt('$ ', mode='line').strip()
            except Exception:
                return
            if not line:
                continue
            self.dispatcher.post(PrintEvent(f'${line}',
                                             COLOR_HIGHLIGHT))
            self.dispatcher.state.cmd.push_history(line)
            parts = line.split()
            entry = self._cmd_registry.get(parts[0])
            if entry is None:
                self.dispatcher.post(PrintEvent(
                    f'  Unknown command: "{parts[0]}"  — type "help"'))
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


# ── Public re-exports for parity with legacy tui module ──────────────────
from .facade import (
    PROMPT_YESNO, PROMPT_INPUT, PROMPT_OPTIONS, PROMPT_PASSWORD, PROMPT_PAUSE,
    Console, Prompt, console,
)
from .widgets import ProgressBar, Spinner
