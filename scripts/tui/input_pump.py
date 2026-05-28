"""Curses input pump — one thread, one job.

Blocks on `stdscr.getkey()` and posts each keystroke as a KeyEvent.
Daemon thread; exits when the process exits.

This is the only place outside Renderer.shutdown() that touches the
curses input mode (`nodelay(False)` so getkey blocks instead of busy-
polling).
"""
from __future__ import annotations

import curses
import threading
from typing import Any

from .events import KeyEvent, Shutdown


def start_input_pump(stdscr, dispatcher: Any) -> threading.Thread:
    """Spin up the input-pump daemon.  Returns the Thread (kept by Tui
    only for diagnostics; not joined since it's daemon)."""
    stdscr.keypad(True)
    stdscr.nodelay(False)   # BLOCKING.  Zero CPU when idle.

    def _pump() -> None:
        while True:
            try:
                key = stdscr.getkey()
            except curses.error:
                continue
            try:
                dispatcher.post(KeyEvent(key))
            except Exception:
                # Dispatcher gone — process is shutting down.
                return
            if dispatcher.state.quit:
                return

    t = threading.Thread(target=_pump, daemon=True, name='tui-input')
    t.start()
    return t
