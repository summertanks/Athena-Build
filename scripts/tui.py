#  Copyright (c) 2023. Harkirat S Virk <harkiratsvirk@gmail.com>
#
#  This program comes with ABSOLUTELY NO WARRANTY; for details see COPYING.
#  This is free software, and you are welcome to redistribute it under certain
#  conditions; see COPYING for details.

"""
tui — curses-based terminal UI for Athena-Build.

Usage::

    tui = Tui("Athena Build v0.1")
    tui_instance = tui          # make available to Prompt / Spinner / ProgressBar
    signal.signal(signal.SIGINT, tui.sig_shutdown)
    tui.run()
    tui.wait()

Only one Tui instance may exist at a time.  Built-in commands: clear, demo,
history, info, help, quit.  Register additional commands with
``tui.register_command(name, fn, tooltip)``.
"""

import curses
import curses.panel
import datetime
import logging
import os
import platform
import queue
import signal
import socket
import sys
import threading
import time
from math import floor
from queue import LifoQueue
from types import FrameType
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict

import psutil


# ---------------------------------------------------------------------------
# Shared TypedDict for tab entries
# ---------------------------------------------------------------------------

class _TabEntry(TypedDict):
    win:      curses.window
    panel:    curses.panel.panel
    buffer:   List[Tuple[str, int]]   # (text_with_newline, curses_attr)
    cursor:   int                     # how many buffer lines are "visible"
    selected: bool


# ---------------------------------------------------------------------------
# Tui
# ---------------------------------------------------------------------------

class Tui:
    """Curses TUI with console + log tabs, command shell, and live widgets.

    Args:
        banner: Short application label shown in the footer (≤ 50 chars).
    """

    # ── Singleton ─────────────────────────────────────────────────────────
    _instance: Optional['Tui'] = None

    # ── Layout ────────────────────────────────────────────────────────────
    BOX_WIDTH     = 1
    FOOTER_HEIGHT = 5     # top-border + 3 content rows + bottom-border
    MIN_COLS      = 80
    MIN_LINES     = 24
    BANNER_MAX    = 50

    # ── Color pair indices ────────────────────────────────────────────────
    COLOR_NORMAL    = 1
    COLOR_REVERSE   = 2
    COLOR_WARNING   = 3
    COLOR_ERROR     = 4
    COLOR_HIGHLIGHT = 5
    COLOR_FOOTER    = 6
    COLOR_INFO      = 7

    # ── Default palette ───────────────────────────────────────────────────
    bg_color        = curses.COLOR_BLACK
    bg_footer       = curses.COLOR_BLUE
    fg_color        = curses.COLOR_WHITE
    warning_color   = curses.COLOR_YELLOW
    error_color     = curses.COLOR_RED
    highlight_color = curses.COLOR_GREEN
    info_color      = curses.COLOR_CYAN

    # ── Severity ──────────────────────────────────────────────────────────
    SEVERITY_ERROR   = 1
    SEVERITY_WARNING = 2
    SEVERITY_INFO    = 3

    # ── Prompt type mirrors (for callers that import Tui.PROMPT_*) ────────
    PROMPT_YESNO    = 1001
    PROMPT_INPUT    = 1002
    PROMPT_OPTIONS  = 1003
    PROMPT_PASSWORD = 1004
    PROMPT_PAUSE    = 1006

    # ── Shell prompt strings ──────────────────────────────────────────────
    _PROMPT_IDLE = '$ '
    _PROMPT_BUSY = '[…] '

    # ── Log map: severity → (tag, attr); rebuilt with colors in _setup() ──
    _log_map: Dict[int, Tuple[str, int]] = {
        SEVERITY_ERROR:   ('[ERROR]', curses.A_BOLD),
        SEVERITY_WARNING: ('[WARN ]', curses.A_BOLD),
        SEVERITY_INFO:    ('[INFO ]', curses.A_NORMAL),
    }

    # =====================================================================
    # Inner helpers
    # =====================================================================

    class _Lockable:
        """Thread-safe wrapper around a single typed value."""

        def __init__(self, var_type: Any) -> None:
            self._lock = threading.Lock()
            self._var  = var_type()

        @property
        def value(self) -> Any:
            with self._lock:
                return self._var

        @value.setter
        def value(self, v: Any) -> None:
            if not isinstance(v, type(self._var)):
                raise TypeError(f'Lockable type mismatch: expected {type(self._var)}, got {type(v)}')
            with self._lock:
                self._var = v

    class _Commands:
        """Command registry and current input-line state."""

        def __init__(self, tui: 'Tui') -> None:
            assert isinstance(tui, Tui)
            self._tui       = tui
            self.current:  str  = ''
            self._history: List[str] = []
            self._reg:     Dict[str, Tuple[Callable[..., Any], str]] = {}
            self._masked:  bool = False
            self._cursor:  int  = 0

        # -- registration --------------------------------------------------

        def register(self, name: str, fn: Callable[..., Any], tip: str = '') -> None:
            name = name.strip()
            if not name:
                self._tui.ERROR('Cannot register a command with an empty name')
                return
            if name in self._reg:
                self._tui.INFO(f'Duplicate command "{name}" — ignored')
                return
            self._reg[name] = (fn, tip)

        def get(self, name: str) -> Optional[Callable[..., Any]]:
            entry = self._reg.get(name.strip())
            return entry[0] if entry else None

        def hints(self) -> List[Tuple[str, str]]:
            return [(n, v[1]) for n, v in self._reg.items()]

        # -- cursor --------------------------------------------------------

        def inc_cursor(self) -> None:
            self._cursor += 1

        def dec_cursor(self) -> None:
            if self._cursor > 0:
                self._cursor -= 1

        def reset_cursor(self) -> None:
            self._cursor = 0

        @property
        def cursor(self) -> int:
            return self._cursor

        # -- mask mode -----------------------------------------------------

        def set_masked(self, state: bool) -> None:
            self._masked = state

        def is_masked(self) -> bool:
            return self._masked

        # -- history -------------------------------------------------------

        def add_history(self, cmd: str) -> None:
            cmd = cmd.strip()
            if cmd:
                self._history.append(cmd)

        @property
        def history(self) -> List[str]:
            return list(self._history)

    # =====================================================================
    # Construction
    # =====================================================================

    def __init__(self, banner: str) -> None:
        if Tui._instance is not None:
            raise RuntimeError('Only one Tui instance may exist at a time')
        Tui._instance = self
        # Register as the module-level singleton so the Console facade /
        # Spinner / ProgressBar / Prompt fall-back path picks us up
        # without callers needing to write `tui.tui_instance = ...`.
        global tui_instance
        tui_instance = self

        self._error_code:     int  = 0
        self._quit:           bool = False
        self._is_setup:       bool = False
        self._resize_pending: bool = False
        self._too_small:      bool = False
        self._dirty:          bool = True

        # Thread locks
        self._prompt_lock  = threading.Lock()
        self._print_lock   = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._log_lock     = threading.Lock()
        self._widget_lock  = threading.Lock()
        self._running_lock = threading.Lock()


        # Dispatch / input queues
        self._dispatch_line: LifoQueue[Any] = queue.LifoQueue()
        self._dispatch_key:  LifoQueue[Any] = queue.LifoQueue()
        self._input_queue:   LifoQueue[Any] = queue.LifoQueue()

        self._banner: str = banner[:self.BANNER_MAX]
        self._psutil  = self._Lockable(str)

        # Layout state
        self._resolution:     Dict[str, int] = {}
        self._tab_coords:     Dict[str, int] = {}
        self._footer_coords:  Dict[str, int] = {}
        self._tabs:   Dict[str, _TabEntry]   = {}
        self._footer: Optional[curses.window] = None
        self._widget: Dict[int, object]       = {}

        # Command / prompt state
        self._cmd        = self._Commands(self)
        self.cmd_prompt: str = self._PROMPT_IDLE

        # Resource monitor daemon
        try:
            threading.Thread(target=self._res_util, daemon=True).start()
        except RuntimeError as e:
            raise RuntimeError(f'Failed to start resource monitor thread: {e}') from e

        # Curses init
        self._stdscr: curses.window
        self._setup()

        try:
            if not self._is_setup:
                raise RuntimeError('failed to initialise screen')
            missing = [n for n in ('console', 'log') if n not in self._tabs]
            if missing or self._footer is None:
                raise RuntimeError(f'mandatory windows missing: {", ".join(missing) or "footer"}')
            self._activate('console')
        except Exception as exc:
            self._shutdown()
            print(f'TUI: fatal — {exc}\r')
            sys.exit(1)

        self.INFO('TUI initialised')

        # Built-in commands
        self._cmd.register('clear',   self.clear,   'Clear a tab buffer: clear <name|all>')
        self._cmd.register('demo',    self.demo,    'Demonstrate built-in widgets')
        self._cmd.register('history', self.history, 'Show command history')
        self._cmd.register('info',    self.info,    'Show system information')
        self._cmd.register('help',    self.help,    'List registered commands')
        self._cmd.register('quit',    self.exit,    'Exit the application')

        try:
            threading.Thread(target=self.shell, daemon=True).start()
        except RuntimeError as e:
            self._shutdown()
            print(f'TUI: failed to start shell thread — {e}\r')
            sys.exit(1)

        # ARCH-07: bind the 'athena' stdlib logger to this Tui so any module
        # using `logging.getLogger('athena')` ends up routed to the same
        # console/log tabs as `tui.console.X`.
        setup_logging(self)

    # =====================================================================
    # Low-level curses helpers
    # =====================================================================

    @staticmethod
    def _safe_addstr(win: curses.window, y: int, x: int,
                     text: str, attr: int = 0) -> None:
        """Write *text* at (y, x) inside *win*, clipping to fit.  Never raises."""
        try:
            max_y, max_x = win.getmaxyx()
            if y < 0 or y >= max_y or x < 0 or x >= max_x:
                return
            available = max_x - x - 1   # -1: writing to the last cell can force a scroll
            if available <= 0:
                return
            if attr:
                win.addstr(y, x, text[:available], attr)
            else:
                win.addstr(y, x, text[:available])
        except curses.error:
            pass

    def _calculateResolution(self) -> None:
        curses.update_lines_cols()
        cols, lines = curses.COLS, curses.LINES
        self._resolution   = {'x': cols, 'y': lines}
        self._tab_coords   = {'h': lines - self.FOOTER_HEIGHT, 'w': cols, 'y': 0, 'x': 0}
        self._footer_coords = {'h': self.FOOTER_HEIGHT, 'w': cols,
                               'y': lines - self.FOOTER_HEIGHT, 'x': 0}

    def _create_tab(self, coords: Dict[str, int]) -> _TabEntry:
        win = curses.newwin(coords['h'], coords['w'], coords['y'], coords['x'])
        pnl = curses.panel.new_panel(win)
        win.scrollok(False)   # layout is fully manual — disable auto-scroll
        win.bkgd(' ', curses.color_pair(self.COLOR_NORMAL))
        return {'win': win, 'panel': pnl, 'buffer': [], 'cursor': 0, 'selected': False}

    def _create_windows(self) -> None:
        """Create (first call) or resize-in-place (subsequent calls) all curses windows."""
        curses.update_lines_cols()

        # ── Below-minimum: flag and return; warning overlay shown by _redraw ──
        if curses.COLS < self.MIN_COLS or curses.LINES < self.MIN_LINES:
            self._too_small = True
            return

        self._too_small = False

        # Nothing changed — avoid redundant resize_term / win.resize calls
        if (curses.LINES == self._resolution.get('y') and
                curses.COLS  == self._resolution.get('x')):
            return

        # Sync the terminal buffer to the new physical size first
        curses.resize_term(curses.LINES, curses.COLS)
        self._calculateResolution()

        fc = self._footer_coords
        tc = self._tab_coords

        if self._footer is None:
            # ── First build: create windows and panels from scratch ───────────
            assert not self._tabs, 'tabs exist without a footer — inconsistent state'
            self._footer = curses.newwin(fc['h'], fc['w'], fc['y'], fc['x'])
            for name in ('console', 'log'):
                self._tabs[name] = self._create_tab(tc)
            self._tabs['console']['selected'] = True
        else:
            # ── Resize in-place: never destroy/recreate ───────────────────────
            # Destroying windows invalidates their panels in undefined GC order;
            # resize/mvwin keeps the C-level window+panel objects intact.

            # Clamp tab cursors to the new height before resizing
            new_h = tc['h']
            for tab in self._tabs.values():
                tab['cursor'] = min(tab['cursor'], max(new_h, len(tab['buffer'])))

            try:
                self._footer.resize(fc['h'], fc['w'])
                self._footer.mvwin(fc['y'], fc['x'])
            except curses.error:
                pass

            for tab in self._tabs.values():
                try:
                    tab['win'].resize(tc['h'], tc['w'])
                    tab['win'].mvwin(tc['y'], tc['x'])
                except curses.error:
                    pass

    # =====================================================================
    # Drawing
    # =====================================================================

    def _refreshfooter(self) -> None:
        max_y, max_x = self._footer.getmaxyx()
        inner_w = max_x - 2 * self.BOX_WIDTH

        if inner_w < 20 or max_y < self.FOOTER_HEIGHT:
            return

        self._footer.erase()
        self._footer.bkgd(curses.color_pair(self.COLOR_FOOTER))
        self._footer.box()

        # ── Row 3: banner (left) ·· resource stats (right) ───────────────
        banner  = self._banner
        res_str = self._psutil.value
        rx      = max_x - self.BOX_WIDTH - len(res_str)

        max_banner = inner_w - len(res_str) - 2
        self._safe_addstr(self._footer, 3, self.BOX_WIDTH,
                          banner[:max_banner], curses.A_BOLD)
        if rx > self.BOX_WIDTH:
            self._safe_addstr(self._footer, 3, rx, res_str, curses.A_BOLD)

        # ── Row 2: tab bar (left) ·· hint (right) ────────────────────────
        hint       = '[Tab] switch'
        prefix     = 'Tabs: '
        hint_x     = max_x - self.BOX_WIDTH - len(hint) - 1
        prefix_end = self.BOX_WIDTH + len(prefix)
        show_hint  = hint_x > prefix_end + 4

        if show_hint:
            self._safe_addstr(self._footer, 2, hint_x, hint)

        self._safe_addstr(self._footer, 2, self.BOX_WIDTH, prefix)
        x            = prefix_end
        tab_area_end = hint_x - 1 if show_hint else max_x - self.BOX_WIDTH

        for tab_name, tab in self._tabs.items():
            label = f' {tab_name} '
            tag   = f'[{label}]' if tab['selected'] else f'|{label}|'
            if x + len(tag) + 1 >= tab_area_end:
                break
            attr = (curses.A_REVERSE | curses.A_BOLD) if tab['selected'] else 0
            self._safe_addstr(self._footer, 2, x, tag, attr)
            x += len(tag) + 1

        # ── Row 1: command input ──────────────────────────────────────────
        prompt_max = max_x - 3   # left border (1) + right border (1) + cursor (1)
        prompt_str = self.cmd_prompt[:prompt_max]
        cmd_area_w = inner_w - len(prompt_str) - 1   # -1 for blink cursor

        self._safe_addstr(self._footer, 1, self.BOX_WIDTH, prompt_str)
        if cmd_area_w < 1:
            return

        raw_cmd     = self._cmd.current
        display_cmd = ('*' * len(raw_cmd)) if self._cmd.is_masked() else raw_cmd
        visible_cmd = display_cmd[self._cmd.cursor: self._cmd.cursor + cmd_area_w]
        cmd_x       = self.BOX_WIDTH + len(prompt_str)

        self._safe_addstr(self._footer, 1, cmd_x, visible_cmd)
        self._safe_addstr(self._footer, 1, cmd_x + len(visible_cmd), '_',
                          curses.A_BLINK | curses.A_BOLD)

    def _refreshtab(self) -> None:
        active = self._activetab
        buffer = active['buffer']
        window = active['win']

        try:
            max_y, _ = window.getmaxyx()
        except curses.error:
            return

        # Widgets only render on the console tab
        is_console = active is self._tabs.get('console')
        with self._widget_lock:
            widget_strs = [str(w) for w in self._widget.values()] if is_console else []

        widget_rows  = len(widget_strs)
        content_rows = max(1, max_y - widget_rows)

        window.erase()

        # Compute visible buffer slice (sliding window ending at cursor)
        cursor    = min(active['cursor'], len(buffer))
        start_idx = max(0, cursor - content_rows)

        row = 0
        for idx in range(start_idx, cursor):
            text, attr = buffer[idx]
            self._safe_addstr(window, row, 0, text.rstrip('\n'), attr)
            row += 1

        # Widgets appear immediately after the last rendered buffer line
        for wstr in widget_strs:
            if row >= max_y:
                break
            self._safe_addstr(window, row, 0, wstr, curses.A_BOLD)
            row += 1

    def _draw_resize_warning(self) -> None:
        """Full-screen overlay shown when the terminal is below minimum size."""
        try:
            # Hide all tab panels before drawing on stdscr; without this the
            # panels sit on top of stdscr and overdraw the warning message.
            for tab in self._tabs.values():
                try:
                    tab['panel'].hide()
                except Exception:
                    pass
            curses.panel.update_panels()   # flush panel visibility to virtual screen

            h, w = curses.LINES, curses.COLS
            self._stdscr.erase()
            self._stdscr.bkgd(' ', curses.color_pair(self.COLOR_REVERSE))

            lines = [
                '',
                'Terminal too small',
                '',
                f'Minimum : {self.MIN_COLS} \u00d7 {self.MIN_LINES}',
                f'Current : {w} \u00d7 {h}',
                '',
                'Resize the terminal to continue',
                'Press  Q  to exit',
                '',
            ]

            start_y = max(0, (h - len(lines)) // 2)
            for i, msg in enumerate(lines):
                y = start_y + i
                if y >= h:
                    break
                if not msg:
                    continue
                x = max(0, (w - len(msg)) // 2)
                try:
                    attr = curses.A_BOLD if i in (1, 7) else 0
                    self._stdscr.addstr(y, x, msg[:max(1, w - 1)], attr)
                except curses.error:
                    pass

            self._stdscr.noutrefresh()
            curses.doupdate()
        except curses.error:
            pass

    def _redraw(self) -> None:
        if not self._is_setup:
            return

        # Detect resize: direct LINES/COLS comparison is more reliable than
        # is_term_resized() which can miss events on some terminal emulators.
        curses.update_lines_cols()
        if (self._resize_pending or
                curses.LINES != self._resolution.get('y', curses.LINES) or
                curses.COLS  != self._resolution.get('x', curses.COLS)):
            self._resize_pending = False
            self._create_windows()

        # Below-minimum: show warning overlay, skip all normal rendering
        if self._too_small:
            self._draw_resize_warning()
            return

        with self._refresh_lock:
            try:
                # Blank stdscr so stale cells don't show in areas not covered by windows
                self._stdscr.erase()
                self._stdscr.noutrefresh()
                self._refreshfooter()
                self._refreshtab()

                # footer is a plain curses.window (not wrapped in a panel),
                # so it has no panel machinery to queue it — hence the explicit _footer.noutrefresh().
                self._footer.noutrefresh()

                for tab in self._tabs.values():
                    if tab['selected']:
                        tab['panel'].show()
                        tab['panel'].top()
                    else:
                        tab['panel'].hide()

                # update_panels() calls wnoutrefresh on each visible panel window
                curses.panel.update_panels()
                curses.doupdate()
            except curses.error:
                pass

    # =====================================================================
    # Tab management
    # =====================================================================

    @property
    def _activetab(self) -> _TabEntry:
        if not self._tabs:
            raise RuntimeError('_activetab called with no tabs initialised')
        active = [v for v in self._tabs.values() if v['selected']]
        if len(active) == 1:
            return active[0]
        # Inconsistent state — recover by resetting to the first tab.  Capture
        # the offending tab names *before* clearing so the diagnostic message
        # actually identifies which tabs were simultaneously selected (or that
        # none were).  Route the message through both the on-disk file logger
        # (for post-mortem after the TUI exits) and the in-memory log tab
        # (for the operator watching live).
        _active_names = [
            name for name, entry in self._tabs.items() if entry['selected']
        ]
        for v in self._tabs.values():
            v['selected'] = False
        first_name, first = next(iter(self._tabs.items()))
        first['selected'] = True
        _msg = (
            f'Tab state inconsistency: {len(active)} tab(s) selected '
            f'({_active_names!r}) — reset to {first_name!r}'
        )
        logging.getLogger('athena').error(_msg)
        self.ERROR(_msg)
        return first

    def _activate(self, name: str) -> None:
        if name not in self._tabs:
            self.ERROR(f'Cannot activate unknown tab "{name}"')
            return
        for v in self._tabs.values():
            v['selected'] = False
        self._tabs[name]['selected'] = True
        self._redraw()

    def _enable_next_tab(self) -> None:
        keys   = list(self._tabs)
        active = [k for k in keys if self._tabs[k]['selected']]
        if not active:
            next_tab = keys[0]
        else:
            idx      = keys.index(active[0])
            next_tab = keys[(idx + 1) % len(keys)]
        self._activate(next_tab)

    def enabletab(self, name: str) -> None:
        """Switch to the named tab (public API)."""
        if name in self._tabs:
            self._activate(name)

    # =====================================================================
    # Setup / teardown
    # =====================================================================

    def _setup(self) -> None:
        if self._is_setup:
            return

        if not hasattr(self, '_stdscr'):
            self._stdscr = curses.initscr()
            curses.update_lines_cols()
            if curses.COLS < self.MIN_COLS or curses.LINES < self.MIN_LINES:
                curses.endwin()
                print(f'TUI: terminal {curses.COLS}×{curses.LINES} below minimum '
                      f'{self.MIN_COLS}×{self.MIN_LINES}\r')
                return

        self._is_setup = True   # set early so _shutdown() can clean up on any failure below

        try:
            curses.noecho()
            curses.cbreak()
            curses.curs_set(0)
        except curses.error as exc:
            raise RuntimeError(f'failed to configure terminal: {exc}') from exc

        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            dark = self._detect_dark_theme()
            self.bg_color = -1
            self.fg_color = curses.COLOR_WHITE if dark else curses.COLOR_BLACK
            curses.init_pair(self.COLOR_NORMAL,    self.fg_color,        self.bg_color)
            curses.init_pair(self.COLOR_REVERSE,   self.bg_color,        self.fg_color)
            curses.init_pair(self.COLOR_WARNING,   self.warning_color,   self.bg_color)
            curses.init_pair(self.COLOR_ERROR,     self.error_color,     self.bg_color)
            curses.init_pair(self.COLOR_HIGHLIGHT, self.highlight_color, self.bg_color)
            curses.init_pair(self.COLOR_FOOTER,    self.fg_color,        self.bg_footer)
            curses.init_pair(self.COLOR_INFO,      self.info_color,      self.bg_color)
            Tui._log_map = {
                self.SEVERITY_ERROR:   ('[ERROR]', curses.color_pair(self.COLOR_ERROR)   | curses.A_BOLD),
                self.SEVERITY_WARNING: ('[WARN ]', curses.color_pair(self.COLOR_WARNING) | curses.A_BOLD),
                self.SEVERITY_INFO:    ('[INFO ]', curses.color_pair(self.COLOR_INFO)),
            }

        self._stdscr.keypad(True)
        self._stdscr.nodelay(True)
        self._create_windows()

    def _shutdown(self) -> None:
        if not self._is_setup:
            return
        self._is_setup = False
        self._footer   = None
        self._tabs.clear()
        self._widget.clear()
        try:
            curses.echo()
            curses.nocbreak()
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            self._stdscr.keypad(False)
            self._stdscr.nodelay(False)
        except curses.error:
            pass
        try:
            self._stdscr.erase()
            self._stdscr.refresh()
        except curses.error:
            pass
        try:
            curses.endwin()
        except curses.error:
            pass
        Tui._instance = None

    def sig_shutdown(self, signum: int, frame: Optional[FrameType]) -> None:
        """Signal handler for SIGINT / SIGTERM."""
        self.exit(signum)

    # =====================================================================
    # Event loop
    # =====================================================================

    def _cmd_input_width(self) -> int:
        """Characters available for command text in the footer input row."""
        inner_w    = self._resolution.get('x', 80) - 2 * self.BOX_WIDTH
        prompt_len = len(self.cmd_prompt[:inner_w - 1])   # inner_w = max_x - 2, so inner_w-1 = max_x-3
        return max(1, inner_w - prompt_len - 1)   # -1 for blink cursor

    def _handle_key(self, c: str) -> None:
        """Dispatch a single key event from the event loop."""
        # Terminal resize
        if c == 'KEY_RESIZE':
            self._resize_pending = True
            self._dirty = True
            return

        # Below minimum — only Q/q exits; all other keys ignored
        if self._too_small:
            if c in ('q', 'Q'):
                self.exit(0)
            return

        active = self._activetab

        # ── Scroll tab content ────────────────────────────────────────────
        if c == 'KEY_UP':
            if active['cursor'] > self._tab_coords.get('h', 1):
                active['cursor'] -= 1
                self._dirty = True
            return

        if c == 'KEY_DOWN':
            active['cursor'] = min(len(active['buffer']), active['cursor'] + 1)
            self._dirty = True
            return

        # ── Scroll command line horizontally ──────────────────────────────
        if c == 'KEY_RIGHT':
            w = self._cmd_input_width()
            if (len(self._cmd.current) > w and
                    self._cmd.cursor < len(self._cmd.current) - w):
                self._cmd.inc_cursor()
                self._dirty = True
            return

        if c == 'KEY_LEFT':
            self._cmd.dec_cursor()
            self._dirty = True
            return

        # ── Backspace ─────────────────────────────────────────────────────
        if c in ('KEY_BACKSPACE', '\x7f', '\x08'):
            if self._cmd.current:
                self._cmd.current = self._cmd.current[:-1]
                if self._cmd.cursor > 0:
                    self._cmd.dec_cursor()
                self._dirty = True
            return

        # ── Tab key: cycle through tabs ───────────────────────────────────
        if c == '\t':
            self._enable_next_tab()   # calls _activate() → _redraw() directly
            return

        # Ignore unrecognised multi-char key sequences (e.g. KEY_F1)
        if len(c) > 1:
            return

        # ── Single-keystroke dispatch (PROMPT_PAUSE etc.) ─────────────────
        if not self._dispatch_key.empty():
            try:
                cond = self._dispatch_key.get_nowait()
            except queue.Empty:
                pass
            else:
                self._input_queue.put(c)
                with cond:
                    cond.notify()
            return

        # ── Normal line input — silently discard if nobody waiting ─────────
        if self._dispatch_line.empty():
            return

        if c == '\n':
            if self._cmd.current.strip():
                self._input_queue.put(self._cmd.current)
                try:
                    cond = self._dispatch_line.get_nowait()
                except queue.Empty:
                    pass
                else:
                    with cond:
                        cond.notify()
            self._cmd.current = ''
            self._cmd.reset_cursor()
            self._dirty = True
        else:
            self._cmd.current += c
            w = self._cmd_input_width()
            if (len(self._cmd.current) > w and
                    self._cmd.cursor < len(self._cmd.current) - w):
                self._cmd.inc_cursor()
            self._dirty = True

    def _run(self, started: threading.Event) -> None:
        self._quit = False
        with self._running_lock:
            started.set()
            while not self._quit:
                curses.napms(10)
                if self._dirty or self._widget:
                    self._redraw()
                    self._dirty = False
                try:
                    c = self._stdscr.getkey()
                except curses.error:
                    continue
                self._handle_key(c)
            self._shutdown()

    def run(self) -> None:
        """Start the event loop in a background daemon thread.

        ncurses owns SIGWINCH internally — it processes the signal and enqueues
        KEY_RESIZE via getch().  Do NOT register a competing SIGWINCH handler
        here; doing so would prevent ncurses from updating its internal state
        and KEY_RESIZE would never be generated.
        """
        started = threading.Event()
        threading.Thread(target=self._run, args=(started,), daemon=True).start()
        started.wait()

    def wait(self) -> None:
        """Block until the TUI exits (the _running_lock is released by _run)."""
        with self._running_lock:
            pass
        if self._error_code != 0:
            print(f'Exited with error code: {self._error_code}\r\n')

    # =====================================================================
    # Resource monitor
    # =====================================================================

    @staticmethod
    def _detect_dark_theme() -> bool:
        """Return True if the terminal appears to use a dark background.

        Reads $COLORFGBG (set by xterm, gnome-terminal, most emulators).
        Format is 'fg;bg' where bg < 8 indicates a dark background color.
        Falls back to True (dark) when the variable is absent or unparseable.
        """
        colorfgbg = os.environ.get('COLORFGBG', '')
        if colorfgbg:
            try:
                bg = int(colorfgbg.split(';')[-1])
                return bg < 8
            except (ValueError, IndexError):
                pass
        return True

    def _res_util(self) -> None:
        """Daemon thread: refreshes _psutil string every ~2 s."""
        while True:
            try:
                cpu  = psutil.cpu_percent(interval=2)
                mem  = psutil.virtual_memory().percent
                disk = psutil.disk_usage('/').percent
                self._psutil.value = f'CPU:{cpu:.0f}%  MEM:{mem:.0f}%  DISK:{disk:.0f}%'
                self._dirty = True
            except Exception as e:
                self.ERROR(f'_res_util: {type(e).__name__}: {e}')
                time.sleep(2)

    # =====================================================================
    # Logging
    # =====================================================================

    def _log(self, severity: int, message: str) -> None:
        if severity not in (self.SEVERITY_ERROR, self.SEVERITY_WARNING, self.SEVERITY_INFO):
            return

        ts = datetime.datetime.now().strftime('%H:%M:%S')
        tag, attr = self._log_map[severity]
        line = f'[{ts}] {tag} {message}'

        with self._log_lock:
            if 'log' not in self._tabs:
                return
            log = self._tabs['log']
            log['buffer'].append((line, attr))
            log['cursor'] = len(log['buffer'])

        self._dirty = True

    def INFO(self,    message: str) -> None: self._log(self.SEVERITY_INFO,    message)
    def WARNING(self, message: str) -> None: self._log(self.SEVERITY_WARNING, message)
    def ERROR(self,   message: str) -> None: self._log(self.SEVERITY_ERROR,   message)

    # =====================================================================
    # Console output
    # =====================================================================

    def print(self, message: str, attribute: Optional[int] = None) -> None:
        """Append *message* to the console tab buffer, splitting on newlines."""
        with self._print_lock:
            if 'console' not in self._tabs:
                return
            attribute = curses.color_pair(attribute if attribute is not None else self.COLOR_NORMAL)
            con = self._tabs['console']
            for line in message.split('\n'):
                con['buffer'].append((line, attribute))
            con['cursor'] = len(con['buffer'])
            self._dirty = True

    def console_mark(self) -> int:
        """Return the current console buffer length as a bookmark."""
        with self._print_lock:
            if 'console' not in self._tabs:
                return 0
            return len(self._tabs['console']['buffer'])

    def console_trim_to(self, mark: int) -> None:
        """Truncate the console buffer back to a previously recorded bookmark."""
        with self._print_lock:
            if 'console' not in self._tabs:
                return
            con = self._tabs['console']
            con['buffer'] = con['buffer'][:mark]
            con['cursor'] = len(con['buffer'])
            self._dirty = True

    # =====================================================================
    # Widget management
    # =====================================================================

    def add_widget(self, widget: object) -> int:
        """Register a live widget; returns its id."""
        wid = id(widget)
        with self._widget_lock:
            self._widget[wid] = widget
        self._dirty = True
        return wid

    def del_widget(self, wid: int) -> None:
        """Deregister a widget by id."""
        with self._widget_lock:
            self._widget.pop(wid, None)
        self._dirty = True

    # =====================================================================
    # Public command registration
    # =====================================================================

    def register_command(self, name: str, fn: Callable[..., Any], tooltip: str = '') -> None:
        """Register *fn* under shell command *name*."""
        self._cmd.register(name, fn, tooltip)

    # =====================================================================
    # Built-in shell commands
    # =====================================================================

    def clear(self, name: str = 'console') -> None:
        """Clear the named tab buffer, or 'all' for every tab."""
        with self._print_lock:
            if name == 'all':
                for tab in self._tabs.values():
                    tab['buffer'] = []
                    tab['cursor'] = 0
            elif name in self._tabs:
                self._tabs[name]['buffer'] = []
                self._tabs[name]['cursor'] = 0
            else:
                self.print(f'Unknown tab: "{name}"')
                return
        self._dirty = True

    def history(self) -> None:
        """Print command history (the current 'history' call is excluded)."""
        cmds = self._cmd.history
        for cmd in cmds[:-1]:
            self.print(f'  {cmd}')

    def info(self) -> None:
        """Print system and runtime information."""
        self.print(f'  Application : {self._banner}')
        self.print(f'  OS          : {platform.system()} {platform.release()}')
        self.print(f'  Hostname    : {socket.gethostname()}')
        self.print(f'  Working dir : {os.getcwd()}')
        self.print(f'  Resources   : {self._psutil.value or "(updating...)"}')
        self.print(f'  Terminal    : {self._resolution.get("y","?")}×{self._resolution.get("x","?")}')

    def help(self) -> None:
        """Print all registered commands and their descriptions."""
        self.print('  Available commands:')
        for name, tip in self._cmd.hints():
            self.print(f'    {name:<14}{tip}')

    def demo(self) -> None:
        """Demonstrate built-in prompts, spinner, progress bar, and log severity."""
        spin = Spinner('Running demo')
        self.print('Demo info message')
        Prompt(PROMPT_YESNO,    'Yes / No prompt').get_response()
        Prompt(PROMPT_INPUT,    'Free-text input').get_response()
        Prompt(PROMPT_OPTIONS,  'Choose one', ['yes', 'no']).get_response()
        Prompt(PROMPT_PASSWORD, 'Masked / password input').get_response()

        bar = ProgressBar('Demo', maxvalue=50)
        while bar.value < 50:
            bar.step()
            curses.napms(20)
        bar.close(persist=True)

        self.print('  Sent log information message')
        self.INFO('Demo info message — everything is fine')
        self.WARNING('Demo warning message — something to watch')
        self.ERROR('Demo error message — something went very wrong')

        Prompt(PROMPT_PAUSE, 'Press any key to finish').get_response()
        spin.done()

    def exit(self, error_code: int = 0) -> None:
        """Gracefully stop the TUI event loop."""
        self._quit       = True
        self._error_code = int(error_code)

    # =====================================================================
    # Prompt (blocking user input)
    # =====================================================================

    def prompt(self, message: str, masked: bool = False,
               keymode: bool = False) -> str:
        """Block until the user provides input; return the input string."""
        with self._prompt_lock:
            old_prompt      = self.cmd_prompt
            self.cmd_prompt = message

            if masked:
                self._cmd.set_masked(True)

            cond = threading.Condition()
            if keymode:
                self._dispatch_key.put(cond)
            else:
                self._dispatch_line.put(cond)

            with cond:
                cond.wait()

            answer = ''
            try:
                answer = self._input_queue.get_nowait()
            except queue.Empty:
                self.ERROR('prompt: input queue empty after condition notify')

            self._cmd.set_masked(False)
            self.cmd_prompt = old_prompt

        return answer

    # =====================================================================
    # Shell (command dispatch thread)
    # =====================================================================

    def shell(self) -> None:
        """Command-dispatch loop.  Runs in its own daemon thread."""
        while True:
            self.cmd_prompt = self._PROMPT_IDLE

            cond = threading.Condition()
            self._dispatch_line.put(cond)
            with cond:
                cond.wait()

            try:
                command = self._input_queue.get_nowait()
            except queue.Empty:
                self.ERROR('shell: notified but input queue is empty')
                continue

            command = command.strip()
            if not command:
                continue

            self.print(f'${command}', self.COLOR_HIGHLIGHT)
            self.cmd_prompt = self._PROMPT_BUSY
            self.INFO(f'Executing: {command}')
            self._cmd.add_history(command)

            parts = command.split()
            fn    = self._cmd.get(parts[0])
            if fn is None:
                self.print(f'  Unknown command: "{parts[0]}"  — type "help" for a list')
                continue

            try:
                fn(*parts[1:])
            except Exception as exc:
                self.print(f'  Error: {exc}')
                self.ERROR(f'{type(exc).__name__}: {exc}')


# ---------------------------------------------------------------------------
# Module-level prompt type constants
# ---------------------------------------------------------------------------

PROMPT_YESNO    = 1001
PROMPT_INPUT    = 1002
PROMPT_OPTIONS  = 1003
PROMPT_PASSWORD = 1004
PROMPT_PAUSE    = 1006

tui_instance: Optional[Tui] = None


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

class Prompt:
    """Blocking user-input widget.

    Args:
        prompt_type: One of PROMPT_YESNO / INPUT / OPTIONS / PASSWORD / PAUSE.
        message:     Prompt text shown to the user.
        options:     Required for PROMPT_OPTIONS; must contain ≥ 2 entries.
    """

    _VALID   = (PROMPT_YESNO, PROMPT_INPUT, PROMPT_OPTIONS, PROMPT_PASSWORD, PROMPT_PAUSE)
    _YESNO   = {'y', 'Y', 'n', 'N', 'yes', 'no', 'Yes', 'No'}

    def __init__(self, prompt_type: int, message: str,
                 options: Optional[List[str]] = None,
                 tui: 'Optional[Tui]' = None) -> None:
        _tui = tui if tui is not None else tui_instance
        if _tui is None:
            raise RuntimeError('No Tui instance — create Tui before using Prompt')
        if prompt_type not in self._VALID:
            raise ValueError(f'Invalid prompt type: {prompt_type}')
        if prompt_type == PROMPT_OPTIONS and (not options or len(options) < 2):
            raise ValueError('PROMPT_OPTIONS requires at least 2 options')

        self._tui     = _tui
        self._type    = prompt_type
        self._options = list(options) if options else []
        self._masked  = (prompt_type == PROMPT_PASSWORD)
        self._keymode = (prompt_type == PROMPT_PAUSE)

        if prompt_type == PROMPT_YESNO:
            self._message = f'{message} [y/n]: '
        elif prompt_type == PROMPT_OPTIONS:
            self._message = f'{message} [{"/".join(self._options)}]: '
        else:
            self._message = message if message.endswith(' ') else message + ' '

    def get_response(self) -> str:
        """Block for user input, re-prompting on validation failure."""
        while True:
            response = self._tui.prompt(
                message=self._message, masked=self._masked, keymode=self._keymode
            )

            if self._type == PROMPT_OPTIONS and response not in self._options:
                self._tui.print(f'  Please enter one of: {", ".join(self._options)}')
                continue

            if self._type == PROMPT_YESNO and response not in self._YESNO:
                self._tui.print('  Please answer y or n')
                continue

            break

        if self._type == PROMPT_PAUSE:
            return ''

        echo = ('*' * len(response)) if self._masked else response
        self._tui.print(f'  {self._message}{echo}')
        return response


# ---------------------------------------------------------------------------
# ProgressBar
# ---------------------------------------------------------------------------

class ProgressBar:
    """Animated progress bar rendered as a live widget in the active tab.

    Default format::

        Label [████████░░░░░░░░░░░░] 42/100  0.50it/s

    Args:
        label:        Left-side label (max 20 chars).
        itr_label:    Rate unit suffix, e.g. ``'pkg/s'`` (max 8 chars).
        bar_width:    Visual fill width, clamped to [10, 60].
        scale_factor: Rate prefix — ``''`` (auto), ``'K'``, ``'M'``, or ``'G'``.
        maxvalue:     Total step count (≥ 1).
        fmt:          Override format string.  Available tokens:
                      ``{label}``, ``{bar}``, ``{value}``, ``{total}``,
                      ``{rate}``, ``{pct}``.
    """

    RUNNING = 1
    PAUSED  = 2
    STOPPED = 3

    _DEFAULT_FMT = '  {label} [{bar}] {value}/{total}  {rate}'
    _SCALE: Dict[str, float] = {'K': 1e3, 'M': 1e6, 'G': 1e9}

    def __init__(self, label: str, itr_label: str = 'it/s', bar_width: int = 40,
                 scale_factor: str = '', maxvalue: int = 100, fmt: str = '',
                 tui: 'Optional[Tui]' = None) -> None:
        _tui = tui if tui is not None else tui_instance
        if _tui is None:
            raise RuntimeError('No Tui instance — create Tui before using ProgressBar')

        self._tui          = _tui
        self._label        = label[:20]
        self._itr_label    = itr_label[:8]
        self._max          = max(1, maxvalue)
        self._value        = 0
        self._state        = self.RUNNING
        self._t0           = time.time_ns()
        self._scale_factor = scale_factor if scale_factor in ('', 'K', 'M', 'G') else ''
        self._bar_width    = max(10, min(60, bar_width))
        self._fmt          = fmt if fmt else self._DEFAULT_FMT
        self._widget_id    = _tui.add_widget(self)

    def __str__(self) -> str:
        pct    = self._value / self._max
        filled = floor(pct * self._bar_width)
        bar    = '█' * filled + '░' * (self._bar_width - filled)

        elapsed = max(1, time.time_ns() - self._t0)
        rate_hz = (self._value / elapsed) * 1e9   # iter/s

        sf = self._scale_factor
        if sf == '':
            if   rate_hz >= 1e9: sf = 'G'
            elif rate_hz >= 1e6: sf = 'M'
            elif rate_hz >= 1e3: sf = 'K'

        rate_disp = rate_hz / self._SCALE.get(sf, 1.0)
        rate_str  = f'{rate_disp:.2f}{sf}{self._itr_label}'
        pct_str   = f'{pct * 100:5.1f}%'

        if   self._max >= 1.15e9: val_sf = 'G'
        elif self._max >= 1.15e6: val_sf = 'M'
        elif self._max >= 1.15e3: val_sf = 'K'
        else:                     val_sf = ''
        val_div   = self._SCALE.get(val_sf, 1.0)
        value_str = f'{self._value / val_div:.2f}{val_sf}' if val_sf else str(self._value)
        total_str = f'{self._max   / val_div:.2f}{val_sf}' if val_sf else str(self._max)

        return self._fmt.format(
            label=self._label, bar=bar,
            value=value_str, total=total_str,
            rate=rate_str, pct=pct_str,
        )

    @property
    def value(self) -> int:
        return self._value

    def step(self, value: int = 1) -> None:
        """Advance the bar by *value* steps."""
        if self._state != self.RUNNING:
            return
        self._value = min(self._value + value, self._max)
        if self._value >= self._max:
            self._state = self.STOPPED

    def pause(self) -> None:
        """Pause the bar — step() calls are ignored until resume()."""
        if self._state == self.RUNNING:
            self._state = self.PAUSED

    def resume(self) -> None:
        """Resume a paused bar."""
        if self._state == self.PAUSED:
            self._state = self.RUNNING

    def set_max(self, value: int) -> None:
        self._max = max(1, value)

    def label(self, message: str) -> None:
        """Update the displayed label."""
        self._label = message.strip()[:20]

    def reset(self) -> None:
        """Reset value and timer."""
        self._value = 0
        self._state = self.RUNNING
        self._t0    = time.time_ns()

    def close(self, persist: bool = False) -> None:
        """Stop the bar; optionally print its final state to the console."""
        self._state = self.STOPPED
        if persist:
            self._tui.print(str(self))
        self._tui.del_widget(self._widget_id)


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------

class Spinner:
    """Animated spinner shown as a live widget.

    Args:
        message: Operation description (max 70 chars).
    """

    _FRAMES = ['⣾', '⣽', '⣻', '⢿', '⡿', '⣟', '⣯', '⣷']

    def __init__(self, message: str, tui: 'Optional[Tui]' = None) -> None:
        _tui = tui if tui is not None else tui_instance
        if _tui is None:
            raise RuntimeError('No Tui instance — create Tui before using Spinner')
        self._tui       = _tui
        self._message   = message[:70]
        self._pos       = 0
        self._running   = True
        self._lock      = threading.Lock()
        self._widget_id = _tui.add_widget(self)
        threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self) -> None:
        while self._running:
            time.sleep(0.1)
            with self._lock:
                self._pos = (self._pos + 1) % len(self._FRAMES)

    def done(self) -> None:
        """Stop the spinner and print a completion line."""
        self._running = False
        self._tui.del_widget(self._widget_id)
        self._tui.print(f'{self._message} … done')

    @property
    def message(self) -> str:
        return self._message

    def __str__(self) -> str:
        with self._lock:
            return f'{self._message}  {self._FRAMES[self._pos]}'


# ---------------------------------------------------------------------------
# Console — thin facade so other modules can write without holding tui_instance
# ---------------------------------------------------------------------------

class Console:
    """Facade over a Tui instance for use by other modules.

    Construct with an explicit ``tui`` argument when testing, or omit
    it to fall back to the module-level ``tui_instance`` singleton at
    every call (the facade resolves lazily so the module-level
    ``console = Console()`` below is safe to create before any Tui
    exists — calls only fail if no Tui has been created by the time
    the call runs).

    Usage::

        from tui import console        # uses singleton
        console.print('Building package...')
        console.error('Something went wrong')

        # or in tests:
        c = Console(tui=stub_tui)
        c.print('hi')                  # routed to stub_tui
    """

    def __init__(self, tui: 'Optional[Tui]' = None) -> None:
        # None means "resolve singleton at call time"; an explicit Tui
        # is captured eagerly so tests are isolated from changes to
        # the global tui_instance during their run.
        self._tui = tui

    def _resolve(self) -> 'Tui':
        t = self._tui if self._tui is not None else tui_instance
        if t is None:
            raise RuntimeError('No Tui instance — create Tui before using Console')
        return t

    def print(self, message: str, attribute: Optional[int] = None) -> None:
        self._resolve().print(message, attribute)

    def mark(self) -> int:
        return self._resolve().console_mark()

    def trim_to(self, mark: int) -> None:
        self._resolve().console_trim_to(mark)

    def error(self, message: str) -> None:
        self._resolve().ERROR(message)

    def info(self, message: str) -> None:
        self._resolve().INFO(message)

    def warning(self, message: str) -> None:
        self._resolve().WARNING(message)


console = Console()


# ---------------------------------------------------------------------------
# Logging adapter (ARCH-07)
# ---------------------------------------------------------------------------
#
# The codebase has historically split output four ways:
#
#   - bare ``print()``                  → host stdout (pre-Tui startup only)
#   - ``tui.console.print(msg)``        → curses console tab (operator-facing)
#   - ``tui.console.{info,warning,error}`` → curses log tab (severity-tagged)
#   - per-command files (chroot-install.log, mksquashfs.log, …)  → diagnostic capture
#
# That fragmentation has two costs: (a) every call site has to pick which
# idiom to use, and (b) tests / future redirection have to monkey-patch
# multiple seams.  The fix is to expose a single stdlib ``logging`` logger
# whose handlers route by level back into the same Tui sinks the Console
# facade already drives — so the call site picks one API (`logging`) and
# the *handlers* decide the destination.
#
# Mapping:
#
#   logger.log(DISPLAY, ...)   → Tui.print              (console tab)
#   logger.info(...)           → Tui.INFO               (log tab)
#   logger.warning(...)        → Tui.WARNING            (log tab)
#   logger.error(...)          → Tui.ERROR              (log tab)
#   logger.critical(...)       → Tui.ERROR              (log tab)
#   logger.debug(...)          → dropped (handler level = INFO)
#
# Existing call sites using the Console facade keep working unchanged;
# both Console and the logger handlers terminate at the same Tui methods,
# so the two APIs are interchangeable.  Per-command log files remain a
# separate concern (raw subprocess output capture, not Python records);
# unifying those is left for a future ticket.

LOGGER_NAME = 'athena'

# Custom level between INFO (20) and WARNING (30).  Records at this level
# carry "operator-facing display text" semantics — they belong on the
# console tab, not the diagnostic log tab.
DISPLAY = 25
logging.addLevelName(DISPLAY, 'DISPLAY')


class _LogTabHandler(logging.Handler):
    """Routes records to the curses log tab via Tui.{INFO,WARNING,ERROR}.

    Severity mapping:
        levelno >= ERROR    → Tui.ERROR
        levelno == WARNING  → Tui.WARNING
        otherwise (INFO)    → Tui.INFO

    DISPLAY-level records are filtered out by setup_logging() so they
    only reach _ConsoleTabHandler.
    """

    def __init__(self, tui: 'Optional[Tui]' = None,
                 level: int = logging.INFO) -> None:
        super().__init__(level)
        self._tui = tui

    def emit(self, record: logging.LogRecord) -> None:
        try:
            t = self._tui if self._tui is not None else tui_instance
            if t is None:
                return  # no Tui yet — drop, same as Console facade did pre-Tui
            msg = self.format(record)
            if record.levelno >= logging.ERROR:
                t.ERROR(msg)
            elif record.levelno >= logging.WARNING:
                t.WARNING(msg)
            else:
                t.INFO(msg)
        except Exception:
            self.handleError(record)


class _ConsoleTabHandler(logging.Handler):
    """Routes DISPLAY-level records to the curses console tab via Tui.print.

    The optional ``attribute`` field on the LogRecord (passed through
    ``logger.log(DISPLAY, msg, extra={'attribute': COLOR_HIGHLIGHT})``)
    is forwarded to Tui.print as the colour-pair argument.
    """

    def __init__(self, tui: 'Optional[Tui]' = None) -> None:
        super().__init__(level=DISPLAY)
        self._tui = tui

    def emit(self, record: logging.LogRecord) -> None:
        try:
            t = self._tui if self._tui is not None else tui_instance
            if t is None:
                return
            attr = getattr(record, 'attribute', None)
            t.print(record.getMessage(), attr)
        except Exception:
            self.handleError(record)


def setup_logging(tui: 'Optional[Tui]' = None) -> logging.Logger:
    """Configure the 'athena' logger to route records into the TUI.

    Called automatically by ``Tui.__init__``; an explicit call is only
    needed when you want the logger configured before a Tui exists, or
    to re-bind an explicit Tui in a test.

    Idempotent: previously-installed *tab* handlers are removed before
    the new ones are attached so tests / re-init paths that re-call
    setup_logging don't accumulate duplicates.  Other handlers (notably
    the FileHandler attached by `setup_file_logging`) are left alone —
    main() may attach the file log before constructing the Tui, and a
    blanket clear here would orphan it.

    Returns the configured logger.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # don't bubble records up to the root logger

    # Selectively drop only the tab handlers we own.  Leaves FileHandler,
    # NullHandler, captureWarnings handlers, etc. intact across re-setup.
    for h in list(logger.handlers):
        if isinstance(h, (_LogTabHandler, _ConsoleTabHandler)):
            logger.removeHandler(h)

    log_h = _LogTabHandler(tui=tui, level=logging.INFO)
    # DISPLAY records belong only on the console tab — keep them out of
    # the log tab even though their numeric level (25) sits above INFO.
    log_h.addFilter(lambda r: r.levelno != DISPLAY)
    logger.addHandler(log_h)

    # And the mirror filter on the console tab: a handler `level=25`
    # is a *floor*, so without this filter WARNING (30) and ERROR (40)
    # would leak into the console tab on top of being routed to the
    # log tab by _LogTabHandler.
    con_h = _ConsoleTabHandler(tui=tui)
    con_h.addFilter(lambda r: r.levelno == DISPLAY)
    logger.addHandler(con_h)
    return logger


def get_logger() -> logging.Logger:
    """Convenience accessor — equivalent to ``logging.getLogger('athena')``."""
    return logging.getLogger(LOGGER_NAME)


def setup_file_logging(log_dir: str, name: str = 'build') -> str:
    """Attach a `FileHandler` to the 'athena' logger.

    Filename is timestamped (`{name}-YYYY-MM-DDTHH-MM-SS.log`) so each
    run lands in its own file rather than overwriting prior runs.
    The handler captures DEBUG and above, so subprocess transcripts
    (chroot install, mksquashfs, …) routed through ``logger.debug``
    end up in the file alongside operator-visible INFO/WARNING/ERROR
    records.

    Args:
        log_dir: Directory to write the log file into; created if absent.
        name:    Filename prefix (default ``'build'``).

    Returns:
        The absolute path of the newly opened log file.
    """
    os.makedirs(log_dir, exist_ok=True)
    ts   = datetime.datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
    path = os.path.abspath(os.path.join(log_dir, f'{name}-{ts}.log'))

    fh = logging.FileHandler(path, mode='w', encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)-7s] %(message)s', '%Y-%m-%d %H:%M:%S'
    ))

    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(fh)
    return path


# Module-level color constants — mirrors Tui class attributes for convenient import
COLOR_NORMAL    = Tui.COLOR_NORMAL
COLOR_REVERSE   = Tui.COLOR_REVERSE
COLOR_WARNING   = Tui.COLOR_WARNING
COLOR_ERROR     = Tui.COLOR_ERROR
COLOR_HIGHLIGHT = Tui.COLOR_HIGHLIGHT
COLOR_INFO      = Tui.COLOR_INFO


def Exit(err_code: int = 0) -> None:
    """Shut down the TUI cleanly then exit the process."""
    global tui_instance
    if tui_instance is not None:
        tui_instance.exit(err_code)
        tui_instance.wait()
        tui_instance = None
    print("Shutdown TUI\n")
    sys.exit(err_code)


def register_command(name: str, fn, tooltip: str = '') -> None:
    """Register a command with the TUI shell."""
    if tui_instance is not None:
        tui_instance.register_command(name, fn, tooltip)


# ---------------------------------------------------------------------------
# Stand-alone test harness
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    tui = Tui('Athena Build Environment v0.1')
    tui_instance = tui

    signal.signal(signal.SIGINT, tui.sig_shutdown)

    tui.run()
    tui.wait()
