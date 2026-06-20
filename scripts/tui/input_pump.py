"""Curses input pump — one thread, one job.

Blocks on `stdscr.getkey()` and posts each keystroke as a KeyEvent.
Daemon thread; the caller (Tui.wait) JOINS it before the interpreter
finalizes.

This is the only place outside Renderer.shutdown() that touches the
curses input mode (`timeout(200)` so getkey wakes every 200ms to re-check
`quit` instead of blocking forever).

WHY the timeout (not `nodelay(False)`): a daemon thread parked inside
ncurses' blocking `getkey()` C call when the interpreter finalizes causes a
use-after-free SIGSEGV at process exit — the SAME class as the psutil
status pump.  With `nodelay(False)` the pump only re-checked `quit` AFTER a
key arrived, so a ONE-SHOT command (no keypress) left it blocked in
ncurses forever → it was still mid-C-call at exit → crash (intermittent,
WSL-timing-sensitive; interactive exits hid it because a keypress woke the
pump).  `timeout(200)` makes the pump exit within ~0.2s of `quit`, so
`Tui.wait` can join it out before finalization.
"""
from __future__ import annotations

import curses
import threading
from typing import Any

from .events import KeyEvent, Shutdown


def start_input_pump(stdscr, dispatcher: Any) -> threading.Thread:
    """Spin up the input-pump daemon.  Returns the Thread; the caller keeps
    it and JOINs it on shutdown (interruptible via the curses timeout)."""
    stdscr.keypad(True)
    stdscr.timeout(200)   # ms; getkey returns (curses.error) every 200ms so
    #                       the pump re-checks `quit` and exits promptly.

    def _pump() -> None:
        while not dispatcher.state.quit:
            try:
                key = stdscr.getkey()
            except curses.error:
                continue   # timeout tick (no key) — loop re-checks `quit`
            try:
                dispatcher.post(KeyEvent(key))
            except Exception:
                # Dispatcher gone — process is shutting down.
                return

    t = threading.Thread(target=_pump, daemon=True, name='tui-input')
    t.start()
    return t
