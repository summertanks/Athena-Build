"""Renderer — pure (state, stdscr) → curses calls.

Single entry point: `Renderer.render(state)`.  Nothing here mutates
State; nothing here reads any thread-shared mutable data other than
the snapshot of `state` it was handed.

Curses lives entirely inside this module.  Tests can drive the
dispatcher with a non-curses renderer (Protocol-based) and never
need `initscr()`.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

import curses
import curses.panel

from . import wrap
from .state import SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING, State


# ── Color pair indices (mirror legacy Tui.COLOR_*) ───────────────────────
COLOR_NORMAL    = 1
COLOR_REVERSE   = 2
COLOR_WARNING   = 3
COLOR_ERROR     = 4
COLOR_HIGHLIGHT = 5
COLOR_FOOTER    = 6
COLOR_INFO      = 7

# ── Layout constants ────────────────────────────────────────────────────
FOOTER_HEIGHT   = 2     # row 0 = prompt, row 1 = status
MIN_COLS        = 80
MIN_LINES       = 24


def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Write `text` at (y, x) inside `win`, clipping to fit.  Never raises.

    Fills the FULL row width, including the last column.  The catch:
    `addstr`-ing a glyph into the bottom-right cell of a window forces
    the cursor past the corner — curses raises `curses.error` and (on
    some terminals) leaves a pending line-wrap that swallows the next
    write.  So the last column is written with `insch`, which places a
    glyph without advancing the cursor past the right margin — no error,
    no pending wrap, and the rightmost column actually renders.

    Off-window coords silently no-op.  The wrap layer
    (``Renderer.width()``) reserves the same full budget (``max_x``)."""
    try:
        max_y, max_x = win.getmaxyx()
        if y < 0 or y >= max_y or x < 0 or x >= max_x:
            return
        budget = max_x - x
        s = text[:budget]
        if not s:
            return
        fills_last_col = (len(s) == budget)
        # addstr everything up to the penultimate column; the last column
        # (if our text reaches it) goes in via insch below.
        head = s[:-1] if fills_last_col else s
        if head:
            if attr:
                win.addstr(y, x, head, attr)
            else:
                win.addstr(y, x, head)
        if fills_last_col:
            last_x = x + budget - 1     # == max_x - 1, the right margin
            # insstr, not insch: insch takes a single-byte chtype, so a
            # non-ASCII final character raises OverflowError ("byte
            # doesn't fit in chtype") — froze the renderer on 2026-07-18
            # when identity-scan findings ended in localized debconf
            # text.  insstr does the same insert-without-advance for
            # multi-byte strings.
            if attr:
                win.insstr(y, last_x, s[-1], attr)
            else:
                win.insstr(y, last_x, s[-1])
    except (curses.error, OverflowError):
        pass


class Renderer:
    """Owns the curses windows + panels.  Stateless wrt the dispatcher
    — its only mutable state is the curses objects themselves, which
    are tied to terminal geometry."""

    def __init__(self, stdscr) -> None:
        self._stdscr = stdscr
        self._tab_wins: dict = {}     # tab name -> curses.window
        self._tab_panels: dict = {}   # tab name -> curses.panel.panel
        self._footer: Optional[Any] = None
        self._w = 0
        self._h = 0
        self._tab_h = 0   # tab content rows (lines - FOOTER_HEIGHT)
        self._setup_colors()
        self._build_windows()

    # ─── Public API (called by Dispatcher) ───────────────────────────────
    def render(self, state: State) -> None:
        """Render the full state to curses."""
        # Resize detection — always cheap.
        curses.update_lines_cols()
        if curses.LINES != self._h or curses.COLS != self._w:
            self._resize(curses.LINES, curses.COLS, state)

        if state.too_small or self._w < MIN_COLS or self._h < MIN_LINES:
            self._render_too_small()
            return

        # Sync tabs (in case dispatcher added one)
        for name in state.tabs:
            if name not in self._tab_wins:
                self._create_tab_window(name)

        self._stdscr.erase()
        self._stdscr.noutrefresh()

        self._render_tab(state)
        self._render_footer(state)

        for name, tab in state.tabs.items():
            pnl = self._tab_panels.get(name)
            if pnl is None:
                continue
            if tab.selected:
                pnl.show()
                pnl.top()
            else:
                pnl.hide()

        curses.panel.update_panels()
        try:
            curses.doupdate()
        except curses.error:
            pass

    def width(self) -> int:
        """Current writable tab content width — used by the dispatcher for
        wrap-row calculations.

        Full window width: `_safe_addstr` renders every column including
        the last (the corner cell goes in via `insch`), so the wrap budget
        must be the same full width or the dispatcher's row counts and the
        renderer's actual layout drift apart — which is exactly what made
        scrolling stop one screenful short."""
        _raw = self._w if self._w > 0 else MIN_COLS
        return max(1, _raw)

    def content_rows(self) -> int:
        """Current tab content rows — used by dispatcher for scroll."""
        return self._tab_h if self._tab_h > 0 else (MIN_LINES - FOOTER_HEIGHT)

    @staticmethod
    def attr_for_severity(severity: int) -> int:
        """Map a SEVERITY_* constant to a curses attr.  Called by
        Dispatcher._on_log to resolve severity at append time so the
        buffer stores ready-to-paint attrs."""
        if severity == SEVERITY_ERROR:
            return curses.color_pair(COLOR_ERROR) | curses.A_BOLD
        if severity == SEVERITY_WARNING:
            return curses.color_pair(COLOR_WARNING) | curses.A_BOLD
        if severity == SEVERITY_INFO:
            return curses.color_pair(COLOR_INFO)
        return 0

    @staticmethod
    def attr_for_color(color_index: Optional[int]) -> int:
        """Resolve a COLOR_* index (1..7) into a curses attr.

        Called by the producer side (v2 Tui.print proxy, Console
        facade) so PrintEvents arrive at the dispatcher pre-resolved
        — no SEVERITY_*/COLOR_* sentinel collision in the renderer."""
        if color_index is None or color_index == 0:
            return 0
        return curses.color_pair(color_index)

    # ─── Setup / resize ──────────────────────────────────────────────────
    def _setup_colors(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        fg = curses.COLOR_WHITE
        bg = -1
        curses.init_pair(COLOR_NORMAL,    fg,                  bg)
        curses.init_pair(COLOR_REVERSE,   bg,                  fg)
        curses.init_pair(COLOR_WARNING,   curses.COLOR_YELLOW, bg)
        curses.init_pair(COLOR_ERROR,     curses.COLOR_RED,    bg)
        curses.init_pair(COLOR_HIGHLIGHT, curses.COLOR_GREEN,  bg)
        curses.init_pair(COLOR_FOOTER,    curses.COLOR_WHITE,  curses.COLOR_BLUE)
        curses.init_pair(COLOR_INFO,      curses.COLOR_CYAN,   bg)

    def _build_windows(self) -> None:
        curses.update_lines_cols()
        self._h, self._w = curses.LINES, curses.COLS
        self._tab_h = max(1, self._h - FOOTER_HEIGHT)
        self._footer = curses.newwin(FOOTER_HEIGHT, self._w, self._h - FOOTER_HEIGHT, 0)

    def _create_tab_window(self, name: str) -> None:
        if name in self._tab_wins:
            return
        win = curses.newwin(self._tab_h, self._w, 0, 0)
        pnl = curses.panel.new_panel(win)
        win.scrollok(False)
        win.bkgd(' ', curses.color_pair(COLOR_NORMAL))
        self._tab_wins[name] = win
        self._tab_panels[name] = pnl

    def _resize(self, new_h: int, new_w: int, state: State) -> None:
        if new_h < MIN_LINES or new_w < MIN_COLS:
            self._h, self._w = new_h, new_w
            self._tab_h = max(1, new_h - FOOTER_HEIGHT)
            state.too_small = True
            return
        state.too_small = False
        try:
            curses.resize_term(new_h, new_w)
        except curses.error:
            pass
        self._h, self._w = new_h, new_w
        self._tab_h = max(1, new_h - FOOTER_HEIGHT)
        # Clamp scroll offsets to the new total display rows.  Count at the
        # SAME width the renderer wraps at (self.width()), else the clamp
        # undercounts rows and the bottom of the buffer becomes unreachable.
        _ww = self.width()
        for t in state.tabs.values():
            total = wrap.total_display_rows(t.buffer, _ww)
            t.scroll_offset = min(t.scroll_offset, max(0, total - self._tab_h))
        # Resize footer + tab windows in place.
        if self._footer is not None:
            try:
                self._footer.resize(FOOTER_HEIGHT, self._w)
                self._footer.mvwin(self._h - FOOTER_HEIGHT, 0)
            except curses.error:
                pass
        for _name, win in self._tab_wins.items():
            try:
                win.resize(self._tab_h, self._w)
                win.mvwin(0, 0)
            except curses.error:
                pass

    # ─── Tab content render (with wrap) ──────────────────────────────────
    def _render_tab(self, state: State) -> None:
        name = state.active_tab_name()
        tab = state.tabs[name]
        win = self._tab_wins.get(name)
        if win is None:
            self._create_tab_window(name)
            win = self._tab_wins[name]

        try:
            max_y, max_x = win.getmaxyx()
        except curses.error:
            return

        # Widgets render on console tab only, overlaid on the bottom rows
        is_console   = (name == 'console')
        widget_strs  = [str(w) for w in state.widgets] if is_console else []
        widget_rows  = len(widget_strs)
        content_rows = max(1, max_y - widget_rows)

        win.erase()

        # Walk buffer back-to-front, wrap each, collect display rows
        # until we have content_rows + scroll_offset of them.
        _so   = max(0, tab.scroll_offset)
        need  = content_rows + _so
        display: List[Tuple[str, int]] = []
        i = len(tab.buffer) - 1
        while i >= 0 and len(display) < need:
            text, attr = tab.buffer[i]
            # Wrap budget = full window width; `_safe_addstr` paints every
            # column (last cell via insch).  MUST match `Renderer.width()`
            # so the dispatcher's wrapped-row counts agree with what's drawn.
            wrapped = wrap.wrap_line(text.rstrip('\n'), max(1, max_x))
            for chunk in reversed(wrapped):
                display.append((chunk, attr))
            i -= 1
        display.reverse()

        # Clamp scroll, slice the visible window
        max_off = max(0, len(display) - content_rows)
        _so = min(_so, max_off)
        end = len(display) - _so
        start = max(0, end - content_rows)

        row = 0
        for r in range(start, end):
            text, attr = display[r]
            _safe_addstr(win, row, 0, text, self._attr_for(attr))
            row += 1

        # Anchor widgets to the bottom band: when the content didn't fill the
        # pane, advance row so the widgets sit at the bottom rather than
        # floating directly under the last content line.  max() keeps row as-is
        # when content already overflows into the widget band.
        row = max(row, max_y - len(widget_strs))
        for wstr in widget_strs:
            if row >= max_y:
                break
            _safe_addstr(win, row, 0, wstr, curses.A_BOLD)
            row += 1

    def _attr_for(self, raw: int) -> int:
        """Pass-through.

        Buffer entries store already-resolved curses attrs (the
        dispatcher / producer resolves color_pair(...) before posting,
        same pattern as the legacy Tui).  Kept as a hook in case a
        future renderer needs to transform attrs at paint time."""
        return raw


    # ─── Footer (cmdline + status bar) ───────────────────────────────────
    def _render_footer(self, state: State) -> None:
        assert self._footer is not None
        try:
            max_y, max_x = self._footer.getmaxyx()
        except curses.error:
            return
        if max_x < 20 or max_y < FOOTER_HEIGHT:
            return
        self._footer.erase()

        # ── Row 0: command prompt + edit cursor caret ─────────────────────
        prompt_str = state.cmd_prompt[:max_x - 2]
        cmd_area_w = max_x - len(prompt_str) - 1
        cmd_x      = len(prompt_str)
        _safe_addstr(self._footer, 0, 0, prompt_str)
        if cmd_area_w >= 1:
            text = state.cmd.text
            display = ('*' * len(text)) if state.cmd.masked else text
            cur     = state.cmd.cursor
            pan     = cur - (cmd_area_w - 1) if cur > cmd_area_w - 1 else 0
            visible = display[pan:pan + cmd_area_w]
            local_cur = cur - pan
            _safe_addstr(self._footer, 0, cmd_x, visible)
            if local_cur < len(visible):
                _safe_addstr(self._footer, 0, cmd_x + local_cur,
                             visible[local_cur], curses.A_REVERSE)
            else:
                _safe_addstr(self._footer, 0, cmd_x + local_cur, ' ',
                             curses.A_REVERSE)

        # ── Row 1: status bar (fixed bright color pair) ──────────────────
        bar  = curses.color_pair(COLOR_FOOTER)
        bold = bar | curses.A_BOLD
        _safe_addstr(self._footer, 1, 0, ' ' * max_x, bar)

        hint    = '  F1-F3: tab  PgUp/Dn: scroll'
        right_x = max_x
        if len(hint) + 4 < max_x:
            right_x -= len(hint)
            _safe_addstr(self._footer, 1, right_x, hint, bar)
        if state.status_text and len(state.status_text) + 4 < right_x:
            right_x -= (len(state.status_text) + 2)
            _safe_addstr(self._footer, 1, right_x, state.status_text, bar)

        banner_text = f' {state.banner} '
        if len(banner_text) >= right_x:
            banner_text = banner_text[:right_x - 1]
        _safe_addstr(self._footer, 1, 0, banner_text, bold)

        x = len(banner_text) + 1
        for _i, (name, tab) in enumerate(state.tabs.items(), start=1):
            _lbl = f'F{_i}: {name.capitalize()}'
            tag = f'[{_lbl}]' if tab.selected else f' {_lbl} '
            if x + len(tag) + 1 >= right_x:
                break
            attr = bold if tab.selected else bar
            _safe_addstr(self._footer, 1, x, tag, attr)
            x += len(tag) + 1

        try:
            self._footer.noutrefresh()
        except curses.error:
            pass

    # ─── Too-small overlay ───────────────────────────────────────────────
    def _render_too_small(self) -> None:
        try:
            h, w = curses.LINES, curses.COLS
            self._stdscr.erase()
            lines = [
                '',
                'Terminal too small',
                '',
                f'Minimum : {MIN_COLS} × {MIN_LINES}',
                f'Current : {w} × {h}',
                '',
                'Resize the terminal to continue',
                'Press  Q  to exit',
                '',
            ]
            start_y = max(0, (h - len(lines)) // 2)
            for i, msg in enumerate(lines):
                y = start_y + i
                if y >= h or not msg:
                    continue
                x = max(0, (w - len(msg)) // 2)
                _safe_addstr(self._stdscr, y, x, msg[:max(1, w - 1)],
                             curses.A_BOLD if i in (1, 7) else 0)
            self._stdscr.noutrefresh()
            curses.doupdate()
        except curses.error:
            pass

    # ─── Input (the UI thread is the SOLE curses owner: input + render) ──
    def begin_input(self) -> None:
        """Put the screen into keypad mode.  Called once by the UI loop,
        which now reads input AND renders on the same (sole curses-owning)
        thread — there is no separate input-pump thread to race the renderer."""
        try:
            self._stdscr.keypad(True)
        except curses.error:
            pass

    def read_key(self, timeout_ms: int) -> 'Optional[str]':
        """Wait up to `timeout_ms` for ONE key; return its getkey() string,
        or None on timeout / transient curses error.  Runs on the UI thread."""
        try:
            self._stdscr.timeout(max(0, int(timeout_ms)))
            return self._stdscr.getkey()
        except curses.error:
            return None

    def drain_keys(self) -> 'list[str]':
        """Non-blocking: collect any further buffered keys after read_key()
        returned one, so fast typing / paste isn't throttled to one key per
        loop tick."""
        _keys: 'list[str]' = []
        try:
            self._stdscr.timeout(0)
            while True:
                try:
                    _keys.append(self._stdscr.getkey())
                except curses.error:
                    break
        except curses.error:
            pass
        return _keys

    # ─── Teardown ────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        """Restore terminal state.  Called on dispatcher exit."""
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
            curses.endwin()
        except curses.error:
            pass


# ── Module-level color helpers — re-exported from Renderer.attr_for_* ─────
# Some callers may import these directly (e.g. logging_bridge or tests
# that don't have a Renderer instance handy).  Both forms resolve to the
# same staticmethods so behaviour is identical.
attr_for_severity = Renderer.attr_for_severity
attr_for_color    = Renderer.attr_for_color
