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
    PromptRequest, Shutdown, StatusEvent, TabActivate, TabAdd, WidgetAdd,
    WidgetRemove, WidgetTick,
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

    def __init__(self, renderer: Renderer, state: Optional[State] = None) -> None:
        self.state = state if state is not None else State()
        self._renderer = renderer
        self._events: 'queue.Queue[object]' = queue.Queue()
        self._pending_prompt: Optional[PromptRequest] = None

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
        """Min of animation deadline (if any) and IDLE_TIMEOUT."""
        d = self.state.next_animation_deadline(time.monotonic())
        if d is None:
            return self.IDLE_TIMEOUT
        return min(d, self.IDLE_TIMEOUT)

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
        elif isinstance(e, WidgetTick):       self.state.dirty = True
        elif isinstance(e, PromptRequest):    self._on_prompt(e)
        elif isinstance(e, TabActivate):      self.state.activate(e.name)
        elif isinstance(e, TabAdd):           self.state.add_tab(e.name)
        elif isinstance(e, ClearTab):         self._on_clear(e)
        elif isinstance(e, ConsoleMark):      self._on_console_mark(e)
        elif isinstance(e, ConsoleTrim):      self._on_console_trim(e)
        elif isinstance(e, Shutdown):         self._on_shutdown(e)
        # Unknown event types silently ignored — producers may post events
        # the dispatcher doesn't yet handle without crashing the loop.

    # ─── Individual handlers ─────────────────────────────────────────────
    def _on_key(self, key: str) -> None:
        """Single keystroke dispatch.

        Order matters:
          1. KEY_RESIZE → mark dirty so renderer recomputes geometry.
          2. PromptRequest active → routes the key to whoever's waiting.
          3. Tab switch / scroll / history / cursor / completion.
          4. Fall-through: edit the cmdline.
        """
        if key == 'KEY_RESIZE':
            self.state.dirty = True
            return

        # Active prompt diverts EVERY key to the prompt's mode handler.
        if self._pending_prompt is not None:
            self._key_for_prompt(key)
            return

        # ── No-prompt path: editor + navigation ──────────────────────────
        st = self.state

        # Page Up/Down: scroll active tab by content_rows (display rows).
        if key == 'KEY_PPAGE':
            st.active_tab().scroll_by(
                self._renderer.content_rows(),
                self._renderer.width(),
                self._renderer.content_rows(),
            )
            st.dirty = True
            return
        if key == 'KEY_NPAGE':
            st.active_tab().scroll_by(
                -self._renderer.content_rows(),
                self._renderer.width(),
                self._renderer.content_rows(),
            )
            st.dirty = True
            return

        # Up/Down: history walk.
        if key == 'KEY_UP':
            if st.cmd.history_prev():
                st.dirty = True
            return
        if key == 'KEY_DOWN':
            if st.cmd.history_next():
                st.dirty = True
            return

        # Left/Right: edit cursor.
        if key == 'KEY_LEFT':
            if st.cmd.move_left():
                st.dirty = True
            return
        if key == 'KEY_RIGHT':
            if st.cmd.move_right():
                st.dirty = True
            return

        # Backspace.
        if key in ('KEY_BACKSPACE', '\x7f', '\x08'):
            if st.cmd.backspace():
                st.dirty = True
            return

        # F-keys: switch to nth tab.
        if key.startswith('KEY_F(') and key.endswith(')'):
            try:
                n = int(key[len('KEY_F('):-1])
            except ValueError:
                return
            st.activate_by_index(n - 1)   # F1 -> index 0
            return

        # Bare ESC: swallow (Alt+Fn on terminals that split sequences).
        if key == '\x1b':
            return

        # Tab: command-name completion.
        if key == '\t':
            self._complete_command()
            return

        # Enter: submit line if there's a shell waiting (handled via the
        # PromptRequest path above when shell is the one waiting).  In
        # the no-prompt fallback (no shell registered), Enter just
        # clears the cmdline.
        if key == '\n':
            st.cmd.reset()
            st.dirty = True
            return

        # Ignore other unrecognized multi-char sequences.
        if len(key) > 1:
            return

        # Printable: insert at cursor.
        st.cmd.insert(key)
        st.dirty = True

    def _key_for_prompt(self, key: str) -> None:
        """Route a keystroke to whatever Prompt is currently active."""
        assert self._pending_prompt is not None
        pp = self._pending_prompt
        st = self.state

        if pp.mode == 'key':
            # PROMPT_PAUSE — any key satisfies it.  Don't insert
            # into the cmdline.
            pp.future.set_result(key)
            self._end_prompt()
            return

        # 'line' or 'masked' — edit cmdline; Enter submits.
        if key == 'KEY_RESIZE':
            st.dirty = True
            return
        if key == '\n':
            answer = st.cmd.text
            st.cmd.reset()
            self._end_prompt()
            pp.future.set_result(answer)
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
        if len(key) > 1 or key == '\x1b':
            return
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
        self._pending_prompt = None
        self.state.cmd_prompt = '$ '
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
        tab.append([(e.text, e.severity)], self._renderer.width())
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
