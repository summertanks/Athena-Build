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

Shipped since the first cut:

- ``--yes`` auto-answers *informational* YESNO prompts (``self.auto_yes``);
  hard prompts (sudo password, conflict OPTIONS) still wait for input.
- ANSI color on stdout when attached to a TTY and ``NO_COLOR`` is unset.
- ``-c <cmd>`` one-shot dispatch (``self.one_shot_cmds``): run each queued
  command in order, then exit with a non-zero code if any failed.

Still deferred until a real use case appears:

- ``ATHENA_SUDO_PASSWORD`` env-var pickup for password prompts.
- ProgressBar throttling (line every N steps / M seconds).
"""
import getpass
import logging
import sys
from typing import Any, Callable, Dict, Optional, Tuple

import tui

logger = logging.getLogger('athena')


def _is_progress_bar(widget: object) -> bool:
    """True iff `widget` is a ProgressBar (used by Cli.add_widget /
    del_widget to print start/finish markers in CLI mode).  Imported
    here so cli.py doesn't carry a top-level `from tui import
    ProgressBar` (would tighten coupling between the two modules
    needlessly when the class only matters at this branch)."""
    from tui import ProgressBar
    return isinstance(widget, ProgressBar)


def _widget_label(widget: object) -> str:
    """Pull the human-readable label off a widget.  ProgressBar stores
    it in `_label` (mutated by .label()); fall back to the class name
    if the attribute is absent.  ProgressBar pins label_width via
    ljust-padding so the TUI bar doesn't shift between updates — strip
    that trailing whitespace before printing the linear CLI marker
    where horizontal stability isn't a concern."""
    return getattr(widget, '_label', type(widget).__name__).rstrip()


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

    # UX-05d: ANSI color codes for stdout when running attached to a TTY
    # and the operator hasn't set NO_COLOR.  Values are arbitrary
    # integers used as keys in the ANSI map below — the curses backend
    # has its own COLOR_* with different numeric values; what matters is
    # that the Console facade passes whatever each backend defines
    # opaquely back into print().
    COLOR_NORMAL    = 0
    COLOR_REVERSE   = 1
    COLOR_WARNING   = 2
    COLOR_ERROR     = 3
    COLOR_HIGHLIGHT = 4
    COLOR_FOOTER    = 5
    COLOR_INFO      = 6

    # Map our COLOR_* keys to ANSI escape sequences.  Choices roughly
    # mirror curses semantics: errors red, warnings yellow, highlights
    # green, info cyan; reverse uses ANSI inverse video.  Lines that
    # don't pass a color (or pass COLOR_NORMAL) bypass the wrapping.
    _ANSI = {
        COLOR_ERROR:     '\x1b[31m',     # red
        COLOR_WARNING:   '\x1b[33m',     # yellow
        COLOR_INFO:      '\x1b[36m',     # cyan
        COLOR_HIGHLIGHT: '\x1b[32m',     # green
        COLOR_REVERSE:   '\x1b[7m',      # inverse video
        COLOR_FOOTER:    '\x1b[2m',      # dim
    }
    _ANSI_RESET = '\x1b[0m'

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
        # UX-05a: --yes auto-answer.  Set by build.py:main() from argv.
        # Consulted only by `Prompt(..., informational=True)` —  hard
        # prompts (sudo password, conflict-resolution OPTIONS) wait
        # for operator input regardless of the flag.
        self.auto_yes: bool = False
        # UX-05d: ANSI colour for stdout when running attached to a TTY
        # and the operator hasn't set NO_COLOR (https://no-color.org/).
        # Redirected output (`> log.txt`) gets plain text.
        import os
        self._use_color: bool = (
            sys.stdout.isatty() and 'NO_COLOR' not in os.environ)
        # UX-05e: one-shot command queue.  build.py:main() populates from
        # `-c <cmd>` argv tokens; wait() dispatches each in order and
        # exits without entering the REPL when the queue is non-empty.
        self.one_shot_cmds: list = []

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
        """Write *message* to stdout, one line.

        UX-05d: if a non-None attribute maps to a known ANSI sequence
        AND stdout is a TTY AND NO_COLOR isn't set, wrap the message in
        the colour escape + reset.  Redirected output (`> log.txt`,
        piped to a file) gets plain text — the isatty() check prevents
        ANSI from polluting log files."""
        if (self._use_color
                and attribute is not None
                and attribute in self._ANSI):
            message = f'{self._ANSI[attribute]}{message}{self._ANSI_RESET}'
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

    # ─── Widget contract ───────────────────────────────────────────────────

    def add_widget(self, widget: object) -> int:
        """Register a live widget.

        For ProgressBar, print a one-line `[start]` marker so the operator
        sees that a long-running staged operation has begun.  Spinner is
        silent here — it already prints its own `… done` line when it
        finishes (see Spinner.done() in tui.py).  Throttled intermediate
        progress is deferred (see UX-05 Path B "out of scope" notes).
        """
        wid = self._next_widget_id
        self._next_widget_id += 1
        self._widget_ids[wid] = widget
        if _is_progress_bar(widget):
            self.print(f'{_widget_label(widget)} [start]')
        return wid

    def del_widget(self, wid: int) -> None:
        """Deregister a widget.

        For ProgressBar, print a one-line `[done: value/max]` marker.
        Combined with the start line from add_widget, the operator gets
        bracketed visibility on every staged loop.  Spinner is silent —
        its own done() handler prints `… done` (and would race with us
        if we printed too).  Note: ProgressBar.close(persist=True) ALSO
        prints the bar's str() before calling del_widget; in CLI that
        renders an unhelpful unicode-block bar but doesn't conflict
        semantically with our [done] marker — both fire, in that order.
        """
        widget = self._widget_ids.pop(wid, None)
        if widget is not None and _is_progress_bar(widget):
            self.print(f'{_widget_label(widget)} [done: '
                       f'{getattr(widget, "value", "?")}/'
                       f'{getattr(widget, "_max", "?")}]')

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
        """REPL loop OR one-shot dispatcher.

        UX-05e: when ``self.one_shot_cmds`` is non-empty, run each queued
        command in order and exit — no REPL.  Useful for CI / scripted
        installs (`./build-system.sh -c "cache build" -c "dep parse"`).
        Exit code: 0 if all succeeded, 1 if any handler raised.

        Otherwise: REPL.  Reads one command per line from stdin,
        dispatches to the registered handler.  Exits on EOF (Ctrl+D),
        ``quit`` / ``exit`` commands, or any handler calling
        ``self.exit(code)``.  A handler raising an exception is caught —
        the REPL keeps running so the operator can inspect state, fix
        things, and retry.  Same forgiving model as Tui's shell().
        """
        if self.one_shot_cmds:
            self._run_one_shot()
            return

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

            if not self._dispatch_one(line):
                # Special tokens: quit/exit return False to break the loop.
                if line.strip() in ('quit', 'exit'):
                    break

        if self._exit_code is None:
            self._exit_code = 0

    def _run_one_shot(self) -> None:
        """UX-05e: dispatch every queued command in order, then exit.
        Exit code reflects the worst outcome (0 if all OK, 1 if any
        handler raised, 130 if interrupted mid-queue)."""
        _failed = 0
        for _cmd in self.one_shot_cmds:
            print(f'{self._PROMPT_IDLE}{_cmd}')
            try:
                _ok = self._dispatch_one(_cmd)
                if not _ok:
                    # quit/exit ends the queue cleanly; an unknown command
                    # or a handler that raised counts as a failure so the
                    # process exit code is non-zero (CI / scripted installs
                    # rely on this).
                    if _cmd.strip() in ('quit', 'exit'):
                        break
                    _failed += 1
            except KeyboardInterrupt:
                # Ctrl+C aborts the remaining queue.
                self._exit_code = 130
                self.ERROR('one-shot queue interrupted by SIGINT')
                return
        if self._exit_code is None:
            self._exit_code = 1 if _failed else 0

    def _dispatch_one(self, line: str) -> bool:
        """Parse + dispatch one input line.  Returns False for empty
        input, unknown commands, quit/exit tokens, or handler failures
        — True only when a handler ran to completion (no raise).
        Shared by both the REPL (wait) and the one-shot driver
        (_run_one_shot)."""
        line = line.strip()
        if not line:
            return False

        parts = line.split()
        cmd, args = parts[0], parts[1:]

        if cmd in ('quit', 'exit'):
            return False
        if cmd == 'help':
            self._print_help()
            return True

        entry = self._cmds.get(cmd)
        if entry is None:
            print(f'  Unknown command: "{cmd}"  — type "help" for a list')
            return False

        fn = entry[0]
        logger.info(f'Executing: {line}')
        try:
            fn(*args)
            return True
        except Exception as exc:
            self.ERROR(f"command '{cmd}' raised "
                       f"{type(exc).__name__}: {exc}")
            return False

    def exit(self, error_code: int = 0) -> None:
        """Signal the wait() loop to break and return *error_code*."""
        self._exit_code = error_code

    def sig_shutdown(self, signum: int, frame: Any) -> None:
        """SIGINT handler.  Mirror Tui.sig_shutdown's signature so build.py
        can wire the same handler in either mode."""
        # 128 + SIGINT (2) = 130 — POSIX convention for interrupted programs.
        self._exit_code = 130

    # ─── console_mark / console_trim_to — no-ops in CLI mode ───────────────
    # NOTE the underscore-prefix names: the Console facade in tui.py calls
    # self._resolve().console_mark() and .console_trim_to() — NOT plain
    # mark/trim_to.  Naming them wrong was a real bug in the first cut and
    # crashed parse_dependency at the multi-provider auto-pick path.

    def console_mark(self) -> int:
        """Console.mark calls this to remember a 'rewind point' on the
        console tab (multi-provider Prompt clears its scratch lines via
        this).  Stdout has no rewind, so return a sentinel and let
        console_trim_to() ignore it."""
        return 0

    def console_trim_to(self, mark: int) -> None:
        """Console.trim_to would erase tab buffer back to *mark* in the
        TUI.  Can't unprint stdout — silently no-op."""
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
