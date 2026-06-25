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

import os
import queue
import time
import traceback as _traceback
from concurrent.futures import Future
from concurrent.futures import TimeoutError as _FutureTimeout
from typing import Any, Callable, List, Optional, Protocol

from . import wrap
from .events import (
    ClearTab, ConsoleMark, ConsoleTrim, KeyEvent, LogEvent, PrintEvent,
    PromptRequest, SetTabBuffer, Shutdown, StatusEvent, TabActivate, TabAdd,
    TabRemove, WidgetAdd, WidgetRemove,
)
from .state import State


# ── Render-failure capture ───────────────────────────────────────────────
# The dispatcher's _safe_render historically swallowed exceptions
# silently — the comment said "Log via stderr in the bridge layer"
# but nothing actually logged.  A render that started raising stayed
# silent: buffers filled, but nothing painted; operator saw no error,
# no echo, no body output for the rest of the session.  Surfaced
# 2026-05-31 after the log-tab removal (commits 1097e32 → bd14f2b);
# couldn't pin the mechanism without ground truth.
#
# Strategy: ONE-SHOT capture on the FIRST failure.  Subsequent fails
# just bump the counter — no log spam if the failure repeats every
# event for the rest of the session.  File path is overrideable for
# tests via RENDER_FAILURE_PATH.

RENDER_FAILURE_PATH: Optional[str] = None        # test hook
_render_failure_count: int = 0


def _default_failure_paths() -> List[str]:
    """Where to drop the post-mortem if no test override.  cwd-relative
    `log/` first (matches the project's dir_log convention so build.py
    operators find it next to their build logs); /tmp fallback if log/
    isn't writable (test runs from /tmp etc)."""
    return [
        os.path.join(os.getcwd(), 'log', 'render-failure.log'),
        os.path.join('/tmp', 'athena-render-failure.log'),
    ]


def _capture_render_failure(state: State, exc: BaseException) -> None:
    """Write a render-failure post-mortem the first time render() raises.

    Subsequent calls increment the counter only — no I/O on repeat
    failures (a broken render typically fires on every event for the
    rest of the session, would spam the disk otherwise)."""
    global _render_failure_count
    _render_failure_count += 1
    if _render_failure_count > 1:
        return
    paths = (
        [RENDER_FAILURE_PATH] if RENDER_FAILURE_PATH else _default_failure_paths()
    )
    lines: List[str] = [
        '=== TUI render failure (one-shot capture) ===',
        f'timestamp:        {time.strftime("%Y-%m-%dT%H:%M:%S")}',
        f'exception:        {type(exc).__name__}: {exc}',
    ]
    try:
        lines.append(f'active_tab:       {state.active_tab_name()}')
    except Exception:
        lines.append('active_tab:       (active_tab_name raised)')
    try:
        lines.append(f'tabs:             {list(state.tabs.keys())}')
        for _name, _t in state.tabs.items():
            lines.append(
                f'  {_name:<10s} buffer={len(_t.buffer):>5d} '
                f'scroll_offset={_t.scroll_offset}'
            )
    except Exception:
        lines.append('tabs:             (state.tabs introspection raised)')
    lines.append(f'widgets:          {len(getattr(state, "widgets", []))}')
    lines.append('')
    lines.append('traceback:')
    lines.append(_traceback.format_exc())
    body = '\n'.join(lines) + '\n'
    for _path in paths:
        try:
            _parent = os.path.dirname(_path)
            if _parent:
                os.makedirs(_parent, exist_ok=True)
            with open(_path, 'w', encoding='utf-8') as _fh:
                _fh.write(body)
            return
        except OSError:
            continue


def render_failure_count() -> int:
    """Public accessor — number of render() failures since process start
    (or last reset).  Useful for tests + an in-TUI `print state` row
    that surfaces "your TUI is silently broken" instead of leaving the
    operator guessing."""
    return _render_failure_count


def _reset_render_failure_state() -> None:
    """Test-only reset: clear the one-shot guard so the next failure
    re-captures.  Production code never calls this."""
    global _render_failure_count
    _render_failure_count = 0


# ── Renderer protocol — keeps dispatcher.py free of curses imports ─────
# The Renderer is the SOLE curses owner: it both draws AND reads input, so
# the UI loop (which calls these) is the only thread touching ncurses.
class Renderer(Protocol):
    def render(self, state: State) -> None: ...
    def width(self) -> int: ...
    def content_rows(self) -> int: ...
    def begin_input(self) -> None: ...
    def read_key(self, timeout_ms: int) -> 'Optional[str]': ...
    def drain_keys(self) -> 'List[str]': ...


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
    # The UI loop now reads input itself (no input-pump thread).  Cap the
    # per-tick getkey wait so keystrokes + worker output render within this
    # bound even when no animation deadline is sooner — 50ms is below the
    # perceptual threshold and costs ~20 idle wakeups/s (a getkey + empty
    # queue drain), negligible.
    INPUT_POLL_MS: int = 50
    # Resource-meter (psutil) sampling cadence, done inline on the UI thread
    # (no daemon status thread → no psutil-at-finalization SIGSEGV).
    STATUS_INTERVAL: float = 2.0

    def __init__(self, renderer: Renderer, state: Optional[State] = None) -> None:
        self.state = state if state is not None else State()
        self._renderer = renderer
        self._events: 'queue.Queue[object]' = queue.Queue()
        self._pending_prompt: Optional[PromptRequest] = None
        # Inline status-sampler state (was the tui-status daemon thread).
        self._last_status: float = 0.0
        self._net0: Any = None
        self._net_t0: Optional[float] = None
        # ── Per-tab key interceptor (package selector) ───────────
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
        cancels the future during shutdown.

        The caller is the MAIN-thread command REPL (and in-command prompts).
        We poll the Future with a short timeout and re-check `state.quit` so a
        shutdown that races in after we post can't deadlock the main thread:
        the UI loop may have already stopped draining the queue, so nobody
        would ever resolve this Future."""
        fut: Future = Future()
        self.post(PromptRequest(message, mode, fut))
        while True:
            try:
                return fut.result(timeout=0.2)
            except _FutureTimeout:
                if self.state.quit:
                    raise RuntimeError(
                        'dispatcher stopped during prompt') from None

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
        while True:
            try:
                return fut.result(timeout=0.2)
            except _FutureTimeout:
                if self.state.quit:
                    return 0    # shutdown raced in — don't hang the caller

    # ─── Loop ────────────────────────────────────────────────────────────
    def run(self) -> int:
        """The single UI thread: read input + drain worker events + sample
        status + render, until Shutdown.  SOLE curses owner (input AND draw
        on this one thread → no ncurses race), so the command REPL can run on
        the main thread.  Returns the exit code from the Shutdown event."""
        self.state.dirty = True
        self._safe_render()
        try:
            self._renderer.begin_input()
        except Exception:                                  # noqa: BLE001
            pass
        self._prime_status()
        while not self.state.quit:
            self._tick()
        # Shutdown: cancel the active prompt AND any still-queued prompt /
        # console_mark Future so a main-thread caller racing in can't block
        # forever (nobody would resolve it once the loop stops).
        self._cancel_pending_futures()
        return self.state.exit_code

    def _tick(self) -> None:
        """One loop iteration — input, events, status, render.  Factored out
        of run() so it can be unit-tested without an infinite loop."""
        # Bound the input wait by the next animation deadline, capped so
        # keystrokes + output stay responsive.
        _ms = max(1, min(int(self._compute_timeout() * 1000),
                         self.INPUT_POLL_MS))
        _key = self._read_key(_ms)
        if _key is not None:
            self._on_key(_key)
            for _k in self._drain_keys():       # absorb fast typing / paste
                self._on_key(_k)
        # Drain ALL pending worker events (batches output → fewer renders).
        while True:
            try:
                _e = self._events.get_nowait()
            except queue.Empty:
                break
            self._handle(_e)
        # Resource meters on cadence (inline — no daemon thread).
        _now = time.monotonic()
        if _now - self._last_status >= self.STATUS_INTERVAL:
            self._last_status = _now
            self._sample_status()
        # Keep live widgets animating.
        if self.state.widgets:
            self.state.dirty = True
        self._safe_render()

    def _cancel_pending_futures(self) -> None:
        if self._pending_prompt is not None:
            self._pending_prompt.future.cancel()
            self._pending_prompt = None
        while True:
            try:
                _e = self._events.get_nowait()
            except queue.Empty:
                break
            if isinstance(_e, (PromptRequest,)):
                _e.future.cancel()
            elif isinstance(_e, ConsoleMark):
                _e.future.cancel()

    # ─── Input (delegated to the Renderer, the sole curses owner) ─────────
    def _read_key(self, timeout_ms: int) -> 'Optional[str]':
        _fn = getattr(self._renderer, 'read_key', None)
        return _fn(timeout_ms) if _fn is not None else None

    def _drain_keys(self) -> 'List[str]':
        _fn = getattr(self._renderer, 'drain_keys', None)
        return _fn() if _fn is not None else []

    # ─── Resource meters (psutil) — sampled inline on the UI thread ───────
    def _prime_status(self) -> None:
        try:
            import psutil
            psutil.cpu_percent(interval=None)             # prime (first → 0.0)
            self._net0 = psutil.net_io_counters()
            self._net_t0 = time.monotonic()
        except Exception:                                  # noqa: BLE001
            self._net0 = None
            self._net_t0 = None

    def _sample_status(self) -> None:
        try:
            import psutil
        except Exception:                                  # noqa: BLE001
            return
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            disk = psutil.disk_usage('/').percent
            up = down = 0.0
            try:
                _net1 = psutil.net_io_counters()
                if self._net0 is not None and self._net_t0 is not None:
                    _dt = max(0.5, time.monotonic() - self._net_t0)
                    up = max(0.0, (_net1.bytes_sent
                                   - self._net0.bytes_sent) / _dt)
                    down = max(0.0, (_net1.bytes_recv
                                     - self._net0.bytes_recv) / _dt)
                self._net0 = _net1
                self._net_t0 = time.monotonic()
            except Exception:                              # noqa: BLE001
                pass
            self._on_status(StatusEvent(cpu, mem, disk, up, down))
        except Exception:                                  # noqa: BLE001
            pass

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
        except Exception as exc:
            # Renderer failure must not kill the loop.  First failure
            # is captured to disk (see _capture_render_failure) so
            # post-mortems aren't blind; further failures only bump
            # the counter, no log spam.
            _capture_render_failure(self.state, exc)
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
    def _page_rows(self) -> int:
        """Rows of scrollable content actually visible on the active tab —
        the PgUp/PgDn page size AND the scroll clamp's viewport height.

        The console tab overlays live widgets (spinners, progress bars) on
        its bottom rows, so its real viewport is shorter by len(widgets).
        Using the full height there makes the scroll clamp stop short and
        leaves the oldest len(widgets) lines unreachable during a build."""
        cr = self._renderer.content_rows()
        if self.state.active_tab_name() == 'console':
            cr = max(1, cr - len(self.state.widgets))
        return cr

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

        # Too-small overlay says "Press Q to exit" — honor it.  The command
        # line is inert while the overlay is up, so this is the only way out.
        if self.state.too_small and key in ('q', 'Q'):
            self.post(Shutdown(0))
            return

        # ── Per-tab key interceptor (selector) ───────────────────
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
            cr = self._page_rows()
            st.active_tab().scroll_by(cr, self._renderer.width(), cr)
            st.dirty = True
            return
        if key == 'KEY_NPAGE':
            cr = self._page_rows()
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
        # fall back to 'log' (a real tab), not the long-gone 'build'.
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

    @staticmethod
    def _fmt_rate(_bps: float) -> str:
        """Bytes/sec → compact human rate (e.g. '2.4M', '12K', '0')."""
        _v = float(_bps)
        for _u in ('', 'K', 'M'):
            if _v < 1024:
                return f'{_v:.0f}{_u}' if _u == '' else f'{_v:.1f}{_u}'
            _v /= 1024
        return f'{_v:.1f}G'

    def _on_status(self, e: StatusEvent) -> None:
        self.state.status_text = (
            f'CPU:{e.cpu:.0f}%  MEM:{e.mem:.0f}%  DISK:{e.disk:.0f}%  '
            f'NET:↑{self._fmt_rate(e.up)} ↓{self._fmt_rate(e.down)}'
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
