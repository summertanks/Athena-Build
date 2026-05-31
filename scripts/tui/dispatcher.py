"""Dispatcher — the event loop and sole state mutator.

Architecture:
    1. One Queue[Event] receives messages from all producers.
    2. One thread (.run()) pops events sequentially and updates State.
    3. After each event, if state.dirty, the Renderer is invoked.
    4. The loop blocks on events.get(timeout=...) — timeout comes from
       state.next_animation_deadline(), so when nothing's animating the
       loop sleeps until a producer posts or curses pushes a key.

No locks (Queue handles its own).  No Conditions (Futures handle
prompt request/response).  No `_dirty` flag plumbing through every
mutating method (State owns it; dispatcher checks once per event).
"""
from __future__ import annotations

import queue
import time
from concurrent.futures import Future
from typing import Callable, Optional, Protocol

from . import wrap
from .events import (
    ClearTab, ConsoleMark, ConsoleTrim, KeyEvent, LogEvent, PrintEvent,
    PromptRequest, SetTabBuffer, Shutdown, StatusEvent, TabActivate, TabAdd,
    TabRemove, WidgetAdd, WidgetRemove,
)
from .state import State


# ── Renderer protocol — keeps dispatcher.py free of curses imports ─────
class Renderer(Protocol):
    def render(self, state: State) -> None: ...
    def width(self) -> int: ...
    def content_rows(self) -> int: ...


class Dispatcher:
    """Single-threaded event loop + state owner.

    Producers (input pump, shell, logger, psutil, Console, Prompt)
    call `post(event)` from any thread.  `request_prompt` is the
    Future-based blocking-input helper for caller threads.

    The loop runs on its own thread started by `start()`."""

    # Idle poll cap — when no animations and no events arrive, wake up
    # this often so the dispatcher can notice external state (e.g. the
    # quit flag set from outside, or a producer that crashed without
    # posting).  1.0s is generous; tuning lower buys nothing.
    IDLE_TIMEOUT: float = 1.0
    # When widgets are alive but they don't expose `next_frame_at`
    # (e.g. legacy ProgressBar imported from scripts/tui.py), cap the
    # idle wait at this value so the bar still animates at a usable
    # rate.  10 Hz is the visible-perception sweet spot.
    WIDGET_IDLE_TIMEOUT: float = 0.1

    def __init__(self, renderer: Renderer, state: Optional[State] = None) -> None:
        self.state = state if state is not None else State()
        self._renderer = renderer
        self._events: 'queue.Queue[object]' = queue.Queue()
        self._pending_prompt: Optional[PromptRequest] = None
        # ── Per-tab key interceptor (COMP-06 package selector) ───────────
        # When an interactive controller (e.g. SelectPackages) owns a
        # tab, it registers a key handler here.  The interceptor gets
        # first crack at every keystroke — but ONLY while its tab is the
        # active one.  It returns True to swallow the key, False to let
        # it fall through to normal dispatch (so F-keys still switch
        # tabs and the operator can always leave).
        self._interceptor_tab: Optional[str] = None
        self._key_interceptor: Optional[Callable[[str], bool]] = None

    # ─── Producer API (thread-safe; called from any thread) ──────────────
    def post(self, event: object) -> None:
        """Enqueue an event.  Never blocks; safe from any thread."""
        self._events.put(event)

    def request_prompt(self, message: str, mode: str = 'line') -> str:
        """Block CALLER thread until the user submits input.

        mode: 'line' (default), 'key' (single keystroke for PAUSE), or
        'masked' (line input echoed as '*' — PROMPT_PASSWORD).

        Returns the user's input string.  Raises if the dispatcher
        cancels the future during shutdown."""
        fut: Future = Future()
        self.post(PromptRequest(message, mode, fut))
        return fut.result()

    def set_key_interceptor(self, tab_name: str,
                            fn: Callable[[str], bool]) -> None:
        """Register a key handler that owns keystrokes while `tab_name`
        is active.  `fn(key) -> bool`: True = consumed, False = fall
        through.  Called from any thread (the controller sets it from
        the dispatcher thread in practice)."""
        self._interceptor_tab = tab_name
        self._key_interceptor = fn

    def clear_key_interceptor(self) -> None:
        self._interceptor_tab = None
        self._key_interceptor = None

    def viewport_rows(self) -> int:
        """Tab content-row count — used by interactive controllers to
        size their visible window."""
        return self._renderer.content_rows()

    def console_mark(self) -> int:
        """Snapshot the console buffer length.  Round-trips through
        the dispatcher so the read is consistent with concurrent
        PrintEvents in flight."""
        fut: Future = Future()
        self.post(ConsoleMark(fut))
        return fut.result()

    # ─── Loop ────────────────────────────────────────────────────────────
    def run(self) -> int:
        """Block on the event queue, dispatching events until Shutdown.

        Returns the exit code from the Shutdown event (default 0)."""
        # Initial paint so the first frame appears before any event.
        self.state.dirty = True
        self._safe_render()

        while not self.state.quit:
            timeout = self._compute_timeout()
            try:
                event = self._events.get(timeout=timeout)
            except queue.Empty:
                # No event arrived in time — most likely an animation
                # deadline expired.  Mark dirty so the next render
                # picks up the new widget frame.
                if self.state.widgets:
                    self.state.dirty = True
                self._safe_render()
                continue

            self._handle(event)
            self._safe_render()

        # Drain any pending prompt so the caller thread isn't stuck.
        if self._pending_prompt is not None:
            self._pending_prompt.future.cancel()
            self._pending_prompt = None
        return self.state.exit_code

    def _compute_timeout(self) -> float:
        """Pick the next dispatcher wake-up.

        Priority:
          1. A widget's `next_frame_at()` returned a deadline → wake
             at that time (e.g. Spinner's 10 fps animation).
          2. No deadlines but widgets are alive → cap at
             WIDGET_IDLE_TIMEOUT (legacy ProgressBar / Spinner from
             scripts/tui.py don't expose next_frame_at; this keeps
             them animating at ~10 Hz without per-step event posts).
          3. No widgets at all → block up to IDLE_TIMEOUT.
        """
        d = self.state.next_animation_deadline(time.monotonic())
        if d is not None:
            return min(d, self.IDLE_TIMEOUT)
        if self.state.widgets:
            return self.WIDGET_IDLE_TIMEOUT
        return self.IDLE_TIMEOUT

    def _safe_render(self) -> None:
        if not self.state.dirty:
            return
        try:
            self._renderer.render(self.state)
        except Exception:
            # Renderer failure must not kill the loop.  Log via stderr
            # in the bridge layer; here just clear dirty and continue.
            pass
        self.state.dirty = False

    # ─── Event dispatch ──────────────────────────────────────────────────
    def _handle(self, e: object) -> None:
        if isinstance(e, KeyEvent):           self._on_key(e.key)
        elif isinstance(e, PrintEvent):       self._on_print(e)
        elif isinstance(e, LogEvent):         self._on_log(e)
        elif isinstance(e, StatusEvent):      self._on_status(e)
        elif isinstance(e, WidgetAdd):        self.state.add_widget(e.widget)
        elif isinstance(e, WidgetRemove):     self.state.remove_widget(e.widget_id)
        elif isinstance(e, PromptRequest):    self._on_prompt(e)
        elif isinstance(e, TabActivate):      self.state.activate(e.name)
        elif isinstance(e, TabAdd):           self.state.add_tab(e.name)
        elif isinstance(e, ClearTab):         self._on_clear(e)
        elif isinstance(e, ConsoleMark):      self._on_console_mark(e)
        elif isinstance(e, ConsoleTrim):      self._on_console_trim(e)
        elif isinstance(e, TabRemove):        self._on_tab_remove(e)
        elif isinstance(e, SetTabBuffer):     self._on_set_tab_buffer(e)
        elif isinstance(e, Shutdown):         self._on_shutdown(e)
        # Unknown event types silently ignored — producers may post events
        # the dispatcher doesn't yet handle without crashing the loop.

    # ─── Individual handlers ─────────────────────────────────────────────
    def _on_key(self, key: str) -> None:
        """Single keystroke dispatch.

        Navigation keys (F-keys to switch tabs, PgUp/PgDn to scroll, ESC)
        work ALWAYS — including while a command runs — so the operator can
        read output and move around during a long build.

        Command-line editing keys (Up/Down history, Left/Right cursor,
        Backspace, Tab completion, printable insert, Enter) only work while a
        line/masked prompt is ACCEPTING input (`_pending_prompt` set).  Between
        prompts — i.e. while a command is running — the command line is inert:
        keystrokes are ignored, so the busy state (set in `_end_prompt`) is
        unambiguous and stray typing isn't captured.  Only PROMPT_PAUSE
        (mode='key') consumes the next single keystroke whole."""

        if key == 'KEY_RESIZE':
            self.state.dirty = True
            return

        # ── Per-tab key interceptor (COMP-06 selector) ───────────────────
        # Consulted FIRST, but only while its owner tab is active.
        # Returns True to swallow; False falls through to normal
        # dispatch (so F-keys still switch tabs, letting the operator
        # leave the interactive tab).
        if (self._key_interceptor is not None
                and self.state.active_tab_name() == self._interceptor_tab):
            try:
                if self._key_interceptor(key):
                    return
            except Exception:
                # A misbehaving interceptor must not kill the loop.
                pass

        # PROMPT_PAUSE: any keystroke fulfills, NO editor processing.
        if (self._pending_prompt is not None
                and self._pending_prompt.mode == 'key'):
            pp = self._pending_prompt
            self._end_prompt()
            pp.future.set_result(key)
            return

        st = self.state

        # ── Navigation — ALWAYS active, even while a command runs ─────────
        # Scroll the output, switch tabs, leave a selector — so the operator
        # can read output and move around during a long build.
        if key == 'KEY_PPAGE':
            cr = self._renderer.content_rows()
            st.active_tab().scroll_by(cr, self._renderer.width(), cr)
            st.dirty = True
            return
        if key == 'KEY_NPAGE':
            cr = self._renderer.content_rows()
            st.active_tab().scroll_by(-cr, self._renderer.width(), cr)
            st.dirty = True
            return
        if key.startswith('KEY_F(') and key.endswith(')'):
            try:
                n = int(key[len('KEY_F('):-1])
            except ValueError:
                return
            st.activate_by_index(n - 1)
            return
        if key == '\x1b':
            return

        # ── Command-line editing — ONLY while a prompt is accepting input ──
        # Between request_prompt calls (a command is running, _pending_prompt
        # is None) the command line is INERT: keystrokes are ignored so the
        # busy state is unambiguous and stray typing isn't captured.  The
        # busy indicator is set in _end_prompt; navigation above stays live.
        if self._pending_prompt is None:
            return

        if key == 'KEY_UP':
            if st.cmd.history_prev():
                st.dirty = True
            return
        if key == 'KEY_DOWN':
            if st.cmd.history_next():
                st.dirty = True
            return
        if key == 'KEY_LEFT':
            if st.cmd.move_left():
                st.dirty = True
            return
        if key == 'KEY_RIGHT':
            if st.cmd.move_right():
                st.dirty = True
            return
        if key in ('KEY_BACKSPACE', '\x7f', '\x08'):
            if st.cmd.backspace():
                st.dirty = True
            return
        if key == '\t':
            self._complete_command()
            return

        # Enter: submit to the pending line/masked prompt.
        if key == '\n':
            pp = self._pending_prompt
            answer = st.cmd.text
            st.cmd.reset()
            self._end_prompt()
            pp.future.set_result(answer)
            return

        # Ignore unrecognised multi-char sequences.
        if len(key) > 1:
            return

        # Printable: insert at cursor.
        st.cmd.insert(key)
        st.dirty = True

    def _on_prompt(self, e: PromptRequest) -> None:
        """Begin a prompt: stash the request, update cmd_prompt + masked."""
        # If a previous prompt is somehow still active, cancel it so
        # the caller doesn't hang forever.  Shouldn't happen — callers
        # serialize via Future.result() — but defensive.
        if self._pending_prompt is not None:
            self._pending_prompt.future.cancel()
        self._pending_prompt = e
        self.state.cmd_prompt = e.message
        self.state.cmd.masked = (e.mode == 'masked')
        self.state.cmd.reset()
        self.state.dirty = True

    def _end_prompt(self) -> None:
        # A prompt was just answered — no input is accepted again until the
        # next request_prompt (i.e. until the running command finishes and the
        # shell loops back).  Show a BUSY indicator (not the idle '> ' prompt)
        # so the operator can tell a command is running, not waiting for input.
        # _on_key gates line-editing on _pending_prompt, so keys are ignored
        # in this state (scroll / tab-switch stay live).
        self._pending_prompt = None
        self.state.cmd_prompt = '… running (please wait) '
        self.state.cmd.masked = False
        self.state.dirty = True

    def _on_print(self, e: PrintEvent) -> None:
        tab = self.state.tabs.get(e.tab) or self.state.tabs.get('console')
        if tab is None:
            return
        lines = [(line, e.attr) for line in e.text.split('\n')]
        tab.append(lines, self._renderer.width())
        self.state.dirty = True

    def _on_log(self, e: LogEvent) -> None:
        tab = self.state.tabs.get(e.tab) or self.state.tabs.get('log')
        if tab is None:
            return
        # Resolve severity → curses attr via the renderer's helper so
        # the buffer stores a usable attr, not an ambiguous sentinel.
        # Renderer-provided fn: kept off the hot draw path.
        attr = e.severity
        if hasattr(self._renderer, 'attr_for_severity'):
            attr = self._renderer.attr_for_severity(e.severity)
        # Split on embedded newlines so each line becomes its own buffer
        # entry — same pattern as _on_print.  Without this, a single log
        # record containing dpkg/subprocess output (often hundreds of
        # lines) is wrap_line'd as one giant text and slices arbitrarily
        # across line boundaries.
        lines = [(line, attr) for line in e.text.split('\n')]
        tab.append(lines, self._renderer.width())
        self.state.dirty = True

    def _on_status(self, e: StatusEvent) -> None:
        self.state.status_text = (
            f'CPU:{e.cpu:.0f}%  MEM:{e.mem:.0f}%  DISK:{e.disk:.0f}%'
        )
        self.state.dirty = True

    def _on_clear(self, e: ClearTab) -> None:
        if e.name == 'all':
            for t in self.state.tabs.values():
                t.clear()
        elif e.name in self.state.tabs:
            self.state.tabs[e.name].clear()
        self.state.dirty = True

    def _on_console_mark(self, e: ConsoleMark) -> None:
        con = self.state.tabs.get('console')
        e.future.set_result(len(con.buffer) if con else 0)

    def _on_console_trim(self, e: ConsoleTrim) -> None:
        con = self.state.tabs.get('console')
        if con is not None:
            con.trim_to(e.mark, self._renderer.width())
            self.state.dirty = True

    def _on_tab_remove(self, e: TabRemove) -> None:
        if e.name not in self.state.tabs:
            return
        was_active = self.state.tabs[e.name].selected
        del self.state.tabs[e.name]
        if was_active and self.state.tabs:
            first = next(iter(self.state.tabs))
            self.state.activate(first)
        self.state.dirty = True

    def _on_set_tab_buffer(self, e: SetTabBuffer) -> None:
        tab = self.state.tabs.get(e.name)
        if tab is None:
            return
        tab.buffer = list(e.rows)
        tab.scroll_offset = 0
        self.state.dirty = True

    def _on_shutdown(self, e: Shutdown) -> None:
        self.state.quit = True
        self.state.exit_code = e.code

    # ─── Tab completion (minimal — names only) ───────────────────────────
    _command_names: Callable[[], list] = staticmethod(list)

    def set_command_names_source(self, fn: Callable[[], list]) -> None:
        """Inject a callable that returns the registered command names.
        Used by Tab-completion; kept as a function so the facade owns
        the registry without the dispatcher depending on it."""
        self._command_names = fn

    def _complete_command(self) -> None:
        st = self.state
        line = st.cmd.text
        if ' ' in line:
            return
        try:
            names = list(self._command_names())
        except Exception:
            return
        matches = [n for n in names if n.startswith(line)]
        if not matches:
            return
        if len(matches) == 1:
            st.cmd.set_text(matches[0] + ' ')
            st.dirty = True
            return
        # >1: print to console without mutating cmdline.
        self.post(PrintEvent('  ' + '  '.join(matches)))
