"""tui_v2 — clean-slate TUI dispatcher.

Architecture: single event queue, single dispatcher thread, pure renderer,
adaptive idle.  Public surface mirrors `scripts/tui.py` so existing call
sites (Console, Prompt, ProgressBar, Spinner, COLOR_*, PROMPT_*) work
unchanged when the operator opts in via `ATHENA_TUI_V2=1`.

Module layout:
    events.py          — Event dataclasses (the messages on the bus)
    wrap.py            — Pure char-based line wrap helpers
    state.py           — State / TabState / CmdLine (single-owner data)
    dispatcher.py      — Event loop + handlers; sole state mutator
    render.py          — Pure renderer: (state, stdscr) -> curses calls
    input_pump.py      — Blocking curses getkey thread
    widgets.py         — ProgressBar, Spinner
    facade.py          — Console + Prompt thin shims
    logging_bridge.py  — logging.Handler -> LogEvent
    tui.py             — Top-level Tui class wires it together
"""
