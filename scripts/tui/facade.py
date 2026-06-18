"""Console + Prompt — thin facades that producer threads call.

Both resolve through the module-level `tui_instance` singleton (set
by Tui.__init__ for the curses backend, or Cli.__init__ for the
headless backend).  Same dispatch surface in both modes:

    tui_instance.print(msg, attr)
    tui_instance.INFO/WARNING/ERROR(msg)
    tui_instance.console_mark() / console_trim_to(mark)
    tui_instance.add_widget(w) / del_widget(wid)
    tui_instance.prompt(message, masked, keymode) -> str

The Console / Prompt classes here exist for API parity with the
legacy `scripts/tui.py` — ~290 console.print sites + ~10 Prompt
constructors across the pipeline.
"""
from __future__ import annotations

from typing import Any, List, Optional


# ── Prompt-type constants (mirror legacy) ────────────────────────────────
PROMPT_YESNO    = 1001
PROMPT_INPUT    = 1002
PROMPT_OPTIONS  = 1003
PROMPT_PASSWORD = 1004
PROMPT_PAUSE    = 1006


def _instance() -> Any:
    """Resolve the active backend at call time (Tui or Cli).  Lazy so
    the module-level `console = Console()` below is safe to create
    before any backend exists — calls only fail if no backend has
    been constructed by the time the call runs."""
    import tui as _pkg
    return _pkg.tui_instance


# ── Console ──────────────────────────────────────────────────────────────
class Console:
    """Operator-facing output facade — delegates to tui_instance.

    Method set mirrors the legacy Console exactly: `print`, `INFO`,
    `WARNING`, `ERROR` plus lowercase aliases `info` / `warning` /
    `error` and the bookmark helpers `mark` / `trim_to` (long-form
    `console_mark` / `console_trim_to` also retained)."""

    def print(self, message: str, attribute: Optional[int] = None) -> None:
        b = _instance()
        if b is not None:
            b.print(message, attribute)

    # ── Severity (legacy uppercase) ──────────────────────────────────────
    def INFO(self, message: str) -> None:
        b = _instance()
        if b is not None:
            b.INFO(message)

    def WARNING(self, message: str) -> None:
        b = _instance()
        if b is not None:
            b.WARNING(message)

    def ERROR(self, message: str) -> None:
        b = _instance()
        if b is not None:
            b.ERROR(message)

    # ── Severity (lowercase aliases — match legacy Console surface) ──────
    def info(self, message: str) -> None:
        self.INFO(message)

    def warning(self, message: str) -> None:
        self.WARNING(message)

    def error(self, message: str) -> None:
        self.ERROR(message)

    # ── Console-buffer bookmarks ────────────────────────────────────────
    def console_mark(self) -> int:
        b = _instance()
        return b.console_mark() if b is not None else 0

    def console_trim_to(self, mark: int) -> None:
        b = _instance()
        if b is not None:
            b.console_trim_to(mark)

    # Legacy short forms
    def mark(self) -> int:
        return self.console_mark()

    def trim_to(self, mark: int) -> None:
        self.console_trim_to(mark)

# Module-level Console instance — matches legacy `tui.console`
console = Console()


# ── Prompt ───────────────────────────────────────────────────────────────
class Prompt:
    """Blocking user-input request.  Delegates to tui_instance.prompt.

    Construct with a type + message; call .get_response() to block
    until the user submits.  Validation loop for YESNO/OPTIONS
    re-prompts on invalid input."""

    _VALID = (PROMPT_YESNO, PROMPT_INPUT, PROMPT_OPTIONS,
              PROMPT_PASSWORD, PROMPT_PAUSE)
    _YESNO = {'y', 'Y', 'n', 'N', 'yes', 'no', 'Yes', 'No'}

    def __init__(self, prompt_type: int, message: str,
                 options: Optional[List[str]] = None,
                 *, informational: bool = False) -> None:
        """UX-05f: `informational=True` marks a YESNO prompt as
        skippable under `--yes`.  Use it for "Proceed with X?"
        confirmations where defaulting to yes is safe under unattended
        operation.  Do NOT use it for hard-required prompts (sudo
        password — those are PROMPT_PASSWORD anyway; conflict-resolution
        OPTIONS; security audit gates per [Security] AuditBuildDeps).
        Default False — backward-compat with existing call sites.
        """
        if prompt_type not in self._VALID:
            raise ValueError(f'Invalid prompt type: {prompt_type}')
        if prompt_type == PROMPT_OPTIONS and (not options or len(options) < 2):
            raise ValueError('PROMPT_OPTIONS requires >= 2 options')
        self._type    = prompt_type
        self._options = list(options) if options else []
        self._masked  = (prompt_type == PROMPT_PASSWORD)
        self._keymode = (prompt_type == PROMPT_PAUSE)
        self._informational = informational
        if prompt_type == PROMPT_YESNO:
            self._message = f'{message} [y/n]: '
        elif prompt_type == PROMPT_OPTIONS:
            self._message = f'{message} [{"/".join(self._options)}]: '
        else:
            self._message = message if message.endswith(' ') else message + ' '

    def get_response(self) -> str:
        b = _instance()
        if b is None:
            return ''
        # + 05f: auto-answer informational YESNO prompts under --yes.
        # Hard prompts (sudo password, OPTIONS, INPUT, PAUSE) and
        # non-informational YESNO (security gates etc.) always wait for
        # operator input.  Honour the backend's auto_yes flag only when
        # the prompt is BOTH a YESNO AND marked informational at the
        # construction site.
        if (self._type == PROMPT_YESNO
                and self._informational
                and getattr(b, 'auto_yes', False)):
            console.print(f'  {self._message}y  [auto-answered: --yes]')
            return 'y'
        while True:
            answer = b.prompt(
                message=self._message,
                masked=self._masked,
                keymode=self._keymode,
            )
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
