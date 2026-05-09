"""Headless CLI backend — Path B of UX-05.

Mirrors the Tui surface that ``Console``, ``Spinner``, ``ProgressBar``,
and ``Prompt`` (all in ``tui.py``) reach for via the module-level
``tui_instance`` singleton.  When ``build-system.sh --headless`` is
passed, ``build.py:main()`` constructs ``Cli()`` instead of ``Tui()``;
``Cli.__init__`` registers itself as ``tui.tui_instance`` so every
existing facade resolves to it without a single line changing in the
command-handler files.

Design contract (intentionally minimal):

- ``print(msg, attribute=None)``: writes one line to stdout.  Color
  attribute is accepted-but-ignored.
- ``INFO/WARNING/ERROR(msg)``: writes a tagged line to stderr — keeps
  diagnostic noise separable from operator-facing output via shell
  redirection (``./build-system.sh --headless 2>warnings.log``).
- ``add_widget``/``del_widget``: no-ops.  ``Spinner.done()`` prints
  ``"<msg> … done"`` directly via ``_tui.print``, so it works.  A
  ``ProgressBar.close(persist=True)`` likewise prints its final state.
  Bars closed with ``persist=False`` go silent in CLI mode — accepted
  v1 limitation; the operator's progress signal is the per-stage log
  lines and the autorun summary, not animated bars.
- ``prompt(message, masked, keymode)``: blocks on stdin.  ``masked``
  uses ``getpass.getpass`` so password input doesn't echo.  ``keymode``
  (PROMPT_PAUSE) reads a line and discards it.
- ``register_command(name, fn, tooltip)``: stores in a dict.
- ``wait()``: REPL loop — read-line / dispatch / loop until EOF (Ctrl+D),
  ``quit``, ``exit``, or a handler calls ``self.exit(code)``.
- ``mark`` / ``trim_to``: no-ops.  Can't unprint stdout that's already
  out the door.

What's NOT in this v1 (deferred until a real use case appears):

- ``--yes`` flag to auto-answer YESNO prompts.
- ``ATHENA_SUDO_PASSWORD`` env-var pickup for password prompts.
- ProgressBar throttling (line every N steps / M seconds).
- ANSI color codes when stdout is a TTY.
- Headless ``-c <cmd>`` flag for one-shot command execution.

All of those are easy add-ons once the bones are in place.
"""
import getpass
import logging
import sys
from typing import Any, Callable, Dict, Optional, Tuple

import tui

logger = logging.getLogger('athena')


class Cli:
    """Headless rendering backend.  See module docstring for the contract.

    Mimics the Tui class's public surface used by Console / Spinner /
    ProgressBar / Prompt.  Curses is never imported.
    """

    # Severity constants — matched to Tui's so the logging handlers
    # (_LogTabHandler, _ConsoleTabHandler in tui.py) work unchanged when
    # they call self._tui.INFO/WARNING/ERROR/print on a Cli instance.
    SEVERITY_ERROR   = 1
    SEVERITY_WARNING = 2
    SEVERITY_INFO    = 3

    # Color constants present so callers passing ``tui.COLOR_*`` to print()
    # don't break on attribute lookup.  Values are arbitrary in CLI mode —
    # the print() implementation ignores the attribute.
    COLOR_NORMAL    = 0
    COLOR_REVERSE   = 0
    COLOR_WARNING   = 0
    COLOR_ERROR     = 0
    COLOR_HIGHLIGHT = 0
    COLOR_FOOTER    = 0
    COLOR_INFO      = 0

    _PROMPT_IDLE = 'athena-build> '

    def __init__(self) -> None:
        # Command registry — name → (fn, tooltip)
        self._cmds: Dict[str, Tuple[Callable[..., Any], str]] = {}
        # Widget ledger — kept only so add_widget() returns a stable id; CLI
        # doesn't draw widgets, so the dict's contents are otherwise unused.
        self._widget_ids: Dict[int, object] = {}
        self._next_widget_id = 0
        # exit_code is None while alive; an int sets the wait() loop to break.
        self._exit_code: Optional[int] = None

        # Register self as the module singleton — same pattern Tui uses on
        # construction.  Console, Spinner, ProgressBar, Prompt all resolve
        # via this module-level reference at call time.
        tui.tui_instance = self

        # Bind the 'athena' logger to route through self.INFO/WARNING/ERROR
        # for log-tab traffic and self.print for DISPLAY-level records.
        # Same handlers used by Tui — they only call methods that Cli also
        # implements.  setup_file_logging() is called from build.py:main()
        # AFTER Cli construction, identical to the Tui ordering.
        tui.setup_logging(tui=self)

    # ─── Console facade contract ───────────────────────────────────────────

    def print(self, message: str, attribute: Optional[int] = None) -> None:
        """Write *message* to stdout, one line.  Color attribute ignored."""
        # Multi-line messages (e.g. the ASCII banner) print as-is — print()
        # handles embedded newlines naturally.
        print(message, flush=True)

    def INFO(self, message: str) -> None:
        """Diagnostic INFO → stderr with severity tag."""
        print(f'[INFO ] {message}', file=sys.stderr, flush=True)

    def WARNING(self, message: str) -> None:
        """Diagnostic WARNING → stderr with severity tag."""
        print(f'[WARN ] {message}', file=sys.stderr, flush=True)

    def ERROR(self, message: str) -> None:
        """Diagnostic ERROR → stderr with severity tag."""
        print(f'[ERROR] {message}', file=sys.stderr, flush=True)

    # ─── Widget contract — no-ops in CLI mode ──────────────────────────────

    def add_widget(self, widget: object) -> int:
        """Register a live widget.  CLI doesn't draw; just hands back an id
        the widget can use later in del_widget()."""
        wid = self._next_widget_id
        self._next_widget_id += 1
        self._widget_ids[wid] = widget
        return wid

    def del_widget(self, wid: int) -> None:
        """Deregister a widget.  No drawing to clean up."""
        self._widget_ids.pop(wid, None)

    # ─── Prompt contract — blocks on stdin ─────────────────────────────────

    def prompt(self, message: str, masked: bool = False,
               keymode: bool = False) -> str:
        """Block until the operator provides input; return the input string.

        ``masked``: read via ``getpass.getpass`` so the input doesn't echo
        (PROMPT_PASSWORD).  ``keymode``: PROMPT_PAUSE — read any line and
        discard.  Default: read a line from stdin via ``input()``.
        """
        if keymode:
            try:
                input(message)
            except EOFError:
                # Ctrl+D at a pause prompt — treat as "continue".
                print()
            return ''
        if masked:
            try:
                return getpass.getpass(message)
            except EOFError:
                print()
                return ''
        try:
            return input(message)
        except EOFError:
            print()
            return ''

    # ─── Command registration contract ─────────────────────────────────────

    def register_command(self, name: str, fn: Callable[..., Any],
                         tooltip: str = '') -> None:
        """Register *fn* under shell command *name*.  Duplicates ignored."""
        name = name.strip()
        if not name:
            self.ERROR('Cannot register a command with an empty name')
            return
        if name in self._cmds:
            self.INFO(f"Duplicate command '{name}' — ignored")
            return
        self._cmds[name] = (fn, tooltip)

    # ─── Lifecycle ─────────────────────────────────────────────────────────

    def run(self) -> None:
        """No-op for CLI mode.  Kept for Tui-surface symmetry: build.py:main()
        calls ``tui_inst.run()`` after construction; for the curses Tui that
        starts the event-loop daemon thread, for Cli there's nothing to do
        (the REPL loop runs on the main thread inside ``wait()``)."""
        return

    def wait(self) -> None:
        """REPL loop.  Reads one command per line from stdin, dispatches to
        the registered handler.  Exits on EOF (Ctrl+D), ``quit`` / ``exit``
        commands, or any handler calling ``self.exit(code)``.

        A handler raising an exception is caught — the REPL keeps running
        so the operator can inspect state, fix things, and retry.  Same
        forgiving model as Tui's shell().
        """
        while self._exit_code is None:
            try:
                line = input(self._PROMPT_IDLE)
            except EOFError:
                # Ctrl+D — clean exit.
                print()
                break
            except KeyboardInterrupt:
                # Ctrl+C at the prompt — abort current line, keep REPL alive.
                print()
                continue

            line = line.strip()
            if not line:
                continue

            parts = line.split()
            cmd, args = parts[0], parts[1:]

            if cmd in ('quit', 'exit'):
                break
            if cmd == 'help':
                self._print_help()
                continue

            entry = self._cmds.get(cmd)
            if entry is None:
                print(f'  Unknown command: "{cmd}"  — type "help" for a list')
                continue

            fn = entry[0]
            logger.info(f'Executing: {line}')
            try:
                fn(*args)
            except Exception as exc:
                self.ERROR(f"command '{cmd}' raised "
                           f"{type(exc).__name__}: {exc}")
                # Don't kill the REPL on a single command failure — match
                # Tui.shell()'s forgiving behaviour.

        if self._exit_code is None:
            self._exit_code = 0

    def exit(self, error_code: int = 0) -> None:
        """Signal the wait() loop to break and return *error_code*."""
        self._exit_code = error_code

    def sig_shutdown(self, signum: int, frame: Any) -> None:
        """SIGINT handler.  Mirror Tui.sig_shutdown's signature so build.py
        can wire the same handler in either mode."""
        # 128 + SIGINT (2) = 130 — POSIX convention for interrupted programs.
        self._exit_code = 130

    # ─── mark / trim_to — no-ops in CLI mode ───────────────────────────────

    def mark(self) -> int:
        """Console.mark calls this to remember a 'rewind point' on the
        console tab.  Stdout has no rewind, so return a sentinel and let
        trim_to() ignore it."""
        return 0

    def trim_to(self, mark: int) -> None:
        """Console.trim_to would erase tab buffer back to *mark* in the TUI.
        Can't unprint stdout — silently no-op."""
        return

    # ─── help screen ───────────────────────────────────────────────────────

    def _print_help(self) -> None:
        """Print the registered command list — matches Tui.help()'s format."""
        print('  Available commands:')
        # Built-ins first.
        print(f'    {"help":<14}List the registered commands')
        print(f'    {"quit":<14}Exit the headless REPL')
        for name, (_fn, tip) in self._cmds.items():
            print(f'    {name:<14}{tip}')
