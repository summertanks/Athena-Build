"""Session config (`set` / `get`) and the autorun pipelines (`auto`).

cmd_set / cmd_get manipulate session-local config params via the
_SETTABLE / _GETTABLE registries (late-bound on BuildSession in build.py);
cmd_auto_run* chain the pipeline stages (live / installer / disk / build)
through _run_autorun_steps.  Extracted verbatim from build.py's
BuildSession; see commands/base.py for how the mixin shares session state.
"""
import datetime
import logging
import time
from typing import Callable, Optional

import tui
from tui import console

from commands.base import SessionState

logger = logging.getLogger('athena.build')


class ConfigRunCommandsMixin(SessionState):
    # ─────────────────────────────────────────────────────────────────
    # set / get — session-local config parameter manipulation
    # ─────────────────────────────────────────────────────────────────

    def _set_mode(self, value: str) -> None:
        """`set mode <distribution|build>` — switch build mode in
        the running session.  Clears dep_check_ready so the next
        pipeline step re-resolves under the new mode; does NOT
        persist to build.conf (operator commits the change explicitly
        if they want it durable)."""
        _valid = ('distribution', 'build')
        if value not in _valid:
            console.print(
                f"  invalid mode: {value!r}  (try: {' | '.join(_valid)})",
                tui.COLOR_ERROR)
            return
        if self.config.build_mode == value:
            console.print(f"  mode already = {value}", tui.COLOR_INFO)
            # Operator may have re-typed this to confirm state.
            # Surface whether the dep tree is parsed under this mode —
            # `mode already = build` reads ambiguously when
            # dep_check_ready is False (operator could think they're
            # ready to source-build when they aren't).
            if not self.flags.dep_check_ready:
                console.print(
                    "  (dep tree not yet parsed — run `cache parse` "
                    "before the next pipeline step)",
                    tui.COLOR_WARNING)
            return
        _prev = self.config.build_mode
        self.config.build_mode = value
        # The dep tree's selected set depends on the mode (build mode
        # short-circuits Passes III–VII and reads build_pkg.list
        # instead), so any cached parse state is now invalid.  Clear
        # dep_check_ready unconditionally — no-op when already False —
        # and ALWAYS print the warning so the operator can't proceed
        # to source build / chroot / iso under a half-stale tree.
        self.flags.dep_check_ready = False
        console.print(
            f"  mode  {_prev}  →  {value}  (session-local, "
            "build.conf unchanged)", tui.COLOR_HIGHLIGHT)
        console.print(
            "  WARNING: mode change requires `cache parse` rerun",
            tui.COLOR_WARNING)
        # Refresh the persistent TUI footer tag so the operator can't
        # forget what mode they're in.  No-op on the CLI backend.
        _inst = getattr(tui, 'tui_instance', None)
        if _inst is not None and hasattr(_inst, 'dispatcher'):
            try:
                _banner = (
                    "Athena Build System v0.1"
                    + (' [build]' if value == 'build' else ''))[:50]
                _inst.dispatcher.state.banner = _banner
            except AttributeError:
                pass

    def _set_include_recommends(self, value: str) -> None:
        """`set include-recommends <true|false>`"""
        _v = value.lower()
        if _v in ('true', '1', 'yes', 'on'):
            _new = True
        elif _v in ('false', '0', 'no', 'off'):
            _new = False
        else:
            console.print(
                f"  invalid bool: {value!r}  (try: true | false)",
                tui.COLOR_ERROR)
            return
        if getattr(self.config, 'include_recommends', False) == _new:
            console.print(f"  include-recommends already = {_new}",
                          tui.COLOR_INFO)
            return
        self.config.include_recommends = _new
        console.print(
            f"  include-recommends  →  {_new}  (session-local)",
            tui.COLOR_HIGHLIGHT)
        if self.flags.dep_check_ready:
            self.flags.dep_check_ready = False
            console.print(
                "  dep_check_ready cleared — run `cache parse`",
                tui.COLOR_INFO)

    _SETTABLE: 'dict[str, Callable]' = {}    # populated below
    _GETTABLE: 'dict[str, Callable]' = {}

    def cmd_set(self, *args) -> None:
        """set <param> <value> — change a session-local config param.

        Bare `set` lists the settable params.  Changes are NOT written
        to build.conf; restart resets to the file's values.
        """
        if not args:
            console.print("Settable params (session-local):")
            for _p in sorted(self._SETTABLE):
                console.print(f"  set {_p} <value>")
            return
        if len(args) < 2:
            console.print(
                f"  usage: set {args[0]} <value>", tui.COLOR_ERROR)
            return
        _param, _value = args[0], args[1]
        _handler = self._SETTABLE.get(_param)
        if _handler is None:
            console.print(
                f"  unknown param: {_param!r}", tui.COLOR_ERROR)
            console.print(
                f"  available: {', '.join(sorted(self._SETTABLE))}")
            return
        _handler(self, _value)

    def cmd_get(self, *args) -> None:
        """get [param] — show a session-local config param.

        Bare `get` lists every gettable param + current value.
        """
        if not args:
            console.print("Current config (session-local view):")
            _w = max(len(_p) for _p in self._GETTABLE) if self._GETTABLE else 0
            for _p in sorted(self._GETTABLE):
                _v = self._GETTABLE[_p](self)
                console.print(f"  {_p:<{_w}}  =  {_v}")
            return
        _param = args[0]
        _getter = self._GETTABLE.get(_param)
        if _getter is None:
            console.print(
                f"  unknown param: {_param!r}", tui.COLOR_ERROR)
            console.print(
                f"  available: {', '.join(sorted(self._GETTABLE))}")
            return
        _value = _getter(self)
        console.print(f"  {_param}  =  {_value}")

    def cmd_auto_run(self, action: str = '', *args):
        """Group dispatcher: bare `autorun` → autorun live (preserves
        existing UX); explicit `autorun live` or `autorun installer`
        run their respective pipelines.

        Both pipelines share the early stages (cache → cache parse →
        source sync → container init → source build pkg) and diverge
        at the subset-specific source build + chroot build, then converge
        on `iso build *` to produce the bootable image.
        """
        _table = {
            'live':       'cache→parse→download→container→source build (+live)→chroot build live→iso build live',
            'installer':  'cache→parse→download→container→source build (+installer)→chroot build installer→iso build installer',
            'disk':       'cache→parse→download→container→source build (+live)→chroot build live→iso build disk (qcow2)',
            'build': 'cache→parse→download→container→source build (indl) — STOPS at source_build_ready (no chroot/ISO)',
        }
        # MIRROR-02: in [Build] Mode = build, bare `autorun`
        # routes to the build pipeline (the live/installer/disk
        # variants would refuse at their chroot/ISO steps anyway).
        # Defensive against missing .config (test doubles).
        _mode = getattr(getattr(self, 'config', None), 'build_mode',
                        'distribution')
        if action == '' and _mode == 'build':
            return self.cmd_auto_run_build(*args)
        if action in ('', 'live'):
            return self.cmd_auto_run_live(*args)
        if action == 'installer':
            return self.cmd_auto_run_installer(*args)
        if action == 'disk':
            return self.cmd_auto_run_disk(*args)
        if action == 'build':
            return self.cmd_auto_run_build(*args)
        return self._group_help('autorun', _table, action)

    def cmd_auto_run_live(self):
        """Run the full pipeline through to a bootable live ISO.

        Bare `source build` builds pkg.list closure only.  For a
        complete live ISO, we need pkg + live extras;
        chain both before chroot build.  Each step uses the
        source_build_ready flag, which cmd_source_build resets at entry —
        so bailing on either subset's failure works the same way.
        """
        _steps = [
            (self.cmd_build_cache,       'cache_ready',           'cache build'),
            (self.cmd_parse_dependency,  'dep_check_ready',       'cache parse'),
            (self.cmd_source_sync,       'download_ready',        'source sync'),
            (self.cmd_init_container,    'build_container_ready', 'container init'),
            (self.cmd_source_build,                                  # bare = pkg
                                          'source_build_ready',    'source build'),
            (lambda: self.cmd_source_build('live'),                  # live extras
                                          'source_build_ready',    'source build live'),
            # chroot build also runs chroot verify; chroot_verified is True
            # only when both build AND all 8 verify checks passed.
            (self.cmd_build_chroot_live, 'chroot_verified',       'chroot build'),
            (self.cmd_build_iso_live,    'iso_live_ready',        'iso build live'),
        ]
        self._run_autorun_steps('autorun live', _steps)

    def cmd_auto_run_installer(self):
        """Run the full pipeline through to a bootable installer ISO.

        Parallel to cmd_auto_run_live but diverges at the subset-specific
        source build (installer subset = udeb closure + installer-exclusive
        deb sources) and chroot build (unpack udebs into buildroot/installer/
        via dpkg --unpack), then converges on iso build installer.
        """
        _steps = [
            (self.cmd_build_cache,       'cache_ready',                'cache build'),
            (self.cmd_parse_dependency,  'dep_check_ready',            'cache parse'),
            (self.cmd_source_sync,       'download_ready',             'source sync'),
            (self.cmd_init_container,    'build_container_ready',      'container init'),
            (self.cmd_source_build,                                       # bare = pkg
                                          'source_build_ready',         'source build'),
            (lambda: self.cmd_source_build('installer'),                  # udeb closure
                                          'source_build_ready',         'source build installer'),
            (self.cmd_build_chroot_installer,
                                          'chroot_installer_ready',     'chroot build installer'),
            (self.cmd_build_iso_installer,
                                          'iso_installer_ready',        'iso build installer'),
        ]
        self._run_autorun_steps('autorun installer', _steps)

    def cmd_auto_run_disk(self):
        """Run the full pipeline through to a pre-installed bootable qcow2
        disk image (COMP-09).

        Shares every early stage with cmd_auto_run_live and reuses the same
        verified LIVE chroot — the disk image is mastered from buildroot/live,
        not a distinct chroot.  Only the terminal step differs: iso build disk
        instead of iso build live.  Gates the final step on iso_disk_ready.
        """
        _steps = [
            (self.cmd_build_cache,       'cache_ready',           'cache build'),
            (self.cmd_parse_dependency,  'dep_check_ready',       'cache parse'),
            (self.cmd_source_sync,       'download_ready',        'source sync'),
            (self.cmd_init_container,    'build_container_ready', 'container init'),
            (self.cmd_source_build,                                  # bare = pkg
                                          'source_build_ready',    'source build'),
            (lambda: self.cmd_source_build('live'),                  # live extras
                                          'source_build_ready',    'source build live'),
            (self.cmd_build_chroot_live, 'chroot_verified',       'chroot build'),
            (self.cmd_build_iso_disk,    'iso_disk_ready',        'iso build disk'),
        ]
        self._run_autorun_steps('autorun disk', _steps)

    def cmd_auto_run_build(self):
        """MIRROR-02: run the build-mode pipeline through to a complete
        source build of every package in `config/build_pkg.list`.

        Stops at source_build_ready — no chroot or ISO assembly
        (those are refused in build mode anyway per chunk 3).  The
        intended endpoint is `mirror publish`, which the operator
        runs explicitly after autorun completes successfully.

        Refuses cleanly when the host isn't in build mode (hint
        points at the live/installer/disk variants).
        """
        if self.config.build_mode != 'build':
            console.print(
                "autorun build: requires `[Build] Mode = build`. "
                " Use `autorun live`/`installer`/`disk` for dist mode.",
                tui.COLOR_ERROR)
            return
        _steps = [
            (self.cmd_build_cache,       'cache_ready',           'cache build'),
            (self.cmd_parse_dependency,  'dep_check_ready',       'cache parse'),
            (self.cmd_source_sync,       'download_ready',        'source sync'),
            (self.cmd_init_container,    'build_container_ready', 'container init'),
            (lambda: self.cmd_source_build('build'),
                                          'source_build_ready',    'source build indl'),
        ]
        self._run_autorun_steps('autorun build', _steps)

    def _run_autorun_steps(self, label: str, _steps: list) -> None:
        """Common driver shared by cmd_auto_run_{live,installer}.

        Walks _steps sequentially, calls each function, gates on its
        success flag.  On the first failure logs + breaks.  Emits the
        autorun summary (via print_commands.summary) on every exit path,
        carrying the stage label that aborted (if any) + total wall time.
        """
        import print_commands
        # MIRROR-02: surface the build mode at the top of the autorun
        # run so the operator can never mistake a 5-step indl chain
        # for a broken 8-step live chain.
        _mode = getattr(self.config, 'build_mode', 'distribution')
        console.print(
            f"{label}: starting (MODE = {_mode})", tui.COLOR_HIGHLIGHT)
        _t0    = time.monotonic()
        _t0_dt = datetime.datetime.now()
        _aborted_at: Optional[str] = None

        for _fn, _flag, _name in _steps:
            _fn()
            if not getattr(self.flags, _flag):
                console.print(f"{label}: '{_name}' did not complete — aborting")
                logger.error(f"{label} aborted at '{_name}' (flag {_flag} not set)")
                _aborted_at = _name
                break

        if _aborted_at is None:
            console.print(f"{label}: all stages complete")

        _t1_dt   = datetime.datetime.now()
        _elapsed = int(time.monotonic() - _t0)
        print_commands.summary(self, timing=print_commands.AutorunTiming(
            started=_t0_dt,
            finished=_t1_dt,
            elapsed=_elapsed,
            aborted_at=_aborted_at,
        ))
