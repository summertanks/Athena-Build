"""Console + Prompt — thin facades that producer threads call.

These exist for API parity with the legacy `scripts/tui.py`:
  - `Console.print(msg, attr)` etc. — used by ~290 sites across the
    pipeline.
  - `Prompt(type, message, options).get_response()` — used by ~10 sites.

Both delegate to the singleton dispatcher (wired by Tui at startup).
Neither holds state; they're just typed wrappers around `post()` and
`request_prompt()`.
"""
from __future__ import annotations

from typing import Any, List, Optional

from .events import ClearTab, ConsoleTrim, PrintEvent
from .state import SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING


# ── Prompt-type constants (mirror legacy) ────────────────────────────────
PROMPT_YESNO    = 1001
PROMPT_INPUT    = 1002
PROMPT_OPTIONS  = 1003
PROMPT_PASSWORD = 1004
PROMPT_PAUSE    = 1006


# ── Dispatcher singleton (wired by Tui) ──────────────────────────────────
_dispatcher: Optional[Any] = None


def set_dispatcher(d: Any) -> None:
    global _dispatcher
    _dispatcher = d


def _post(event: Any) -> None:
    if _dispatcher is not None:
        _dispatcher.post(event)


# ── Console ──────────────────────────────────────────────────────────────
class Console:
    """Operator-facing output facade.

    The dispatcher owns the actual buffer; this class just packages
    each call into the right Event and posts it.  Color attributes
    are already-resolved curses attrs (the caller passes one of the
    COLOR_* sentinels imported from render.py; we forward them
    verbatim and let the renderer translate)."""

    def print(self, message: str, attribute: Optional[int] = None) -> None:
        # Resolve COLOR_* index → curses attr at the producer side
        # so the dispatcher buffer stores usable attrs (no sentinel
        # collision with SEVERITY_*).  Done via the renderer helper
        # if a dispatcher is bound; otherwise pass through as 0.
        attr = 0
        if attribute and _dispatcher is not None:
            try:
                attr = _dispatcher._renderer.attr_for_color(attribute)
            except Exception:
                attr = 0
        _post(PrintEvent(message, attr))

    def INFO(self, message: str) -> None:
        from .events import LogEvent
        _post(LogEvent(SEVERITY_INFO, f'[INFO ] {message}'))

    def WARNING(self, message: str) -> None:
        from .events import LogEvent
        _post(LogEvent(SEVERITY_WARNING, f'[WARN ] {message}'))

    def ERROR(self, message: str) -> None:
        from .events import LogEvent
        _post(LogEvent(SEVERITY_ERROR, f'[ERROR] {message}'))

    def console_mark(self) -> int:
        """Return current console buffer length (blocking round-trip)."""
        if _dispatcher is None:
            return 0
        return _dispatcher.console_mark()

    def console_trim_to(self, mark: int) -> None:
        _post(ConsoleTrim(mark))

    def clear(self, name: str = 'console') -> None:
        _post(ClearTab(name))


# Module-level Console instance — matches legacy `tui.console`
console = Console()


# ── Prompt ───────────────────────────────────────────────────────────────
class Prompt:
    """Blocking user-input request.

    Construct with a type + message; call .get_response() to block
    until the user submits.  Validation loop for YESNO/OPTIONS
    re-prompts on invalid input."""

    _VALID = (PROMPT_YESNO, PROMPT_INPUT, PROMPT_OPTIONS,
              PROMPT_PASSWORD, PROMPT_PAUSE)
    _YESNO = {'y', 'Y', 'n', 'N', 'yes', 'no', 'Yes', 'No'}

    def __init__(self, prompt_type: int, message: str,
                 options: Optional[List[str]] = None) -> None:
        if prompt_type not in self._VALID:
            raise ValueError(f'Invalid prompt type: {prompt_type}')
        if prompt_type == PROMPT_OPTIONS and (not options or len(options) < 2):
            raise ValueError('PROMPT_OPTIONS requires >= 2 options')
        self._type    = prompt_type
        self._options = list(options) if options else []
        if prompt_type == PROMPT_YESNO:
            self._message = f'{message} [y/n]: '
        elif prompt_type == PROMPT_OPTIONS:
            self._message = f'{message} [{"/".join(self._options)}]: '
        else:
            self._message = message if message.endswith(' ') else message + ' '

    def get_response(self) -> str:
        if _dispatcher is None:
            return ''
        mode = ('key'    if self._type == PROMPT_PAUSE    else
                'masked' if self._type == PROMPT_PASSWORD else
                'line')
        while True:
            answer = _dispatcher.request_prompt(self._message, mode)
            if self._type == PROMPT_OPTIONS and answer not in self._options:
                console.print(f'  Please enter one of: {", ".join(self._options)}')
                continue
            if self._type == PROMPT_YESNO and answer not in self._YESNO:
                console.print('  Please answer y or n')
                continue
            break
        if self._type == PROMPT_PAUSE:
            return ''
        echo = ('*' * len(answer)) if self._type == PROMPT_PASSWORD else answer
        console.print(f'  {self._message}{echo}')
        return answer
