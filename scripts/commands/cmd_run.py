"""Session config (`set` / `get`) and the autorun pipelines (`auto`).

cmd_set / cmd_get manipulate session-local config params via the
_SETTABLE / _GETTABLE registries (late-bound on BuildSession in build.py);
cmd_auto_run* chain the pipeline stages (live / installer / disk / build)
through _run_autorun_steps.  Extracted verbatim from build.py's
BuildSession; see commands/base.py for how the mixin shares session state.
"""
import datetime
import logging
import os
import time
from typing import Callable, Optional

import tui
import utils
from tui import console

from commands.base import SessionState

logger = logging.getLogger('athena.build')


class ConfigRunCommandsMixin(SessionState):
    # ─────────────────────────────────────────────────────────────────
    # set / get — session-local config parameter manipulation
    # ─────────────────────────────────────────────────────────────────

    def _build_mode_block_reason(self) -> Optional[str]:
        """Why this box may NOT enter build mode, or None if it may.

        Two rules, mirroring onboarding + the publish-time gate
        (cmd_mirror first-publish refusal): a FIRST/origin system stays in
        distribution (build mode needs a published baseline it can't itself
        be), and any peer must be federation-registered first — a
        registration marker in the signed config/mirror.conf, written once
        registration succeeds."""
        if getattr(self.config, 'system_role', '') == 'first':
            return ("a FIRST/origin system stays in distribution mode "
                    "(build mode needs a published baseline from an origin)")
        import mirror as _mirror
        if not _mirror.get_registration(self.config):
            return ("register to a mirror first (run setup or "
                    "`mirror builders register`) — build mode publishes a "
                    "subset that only an already-bootstrapped mirror accepts")
        return None

    def _set_mode(self, value: str) -> None:
        """`set mode <distribution|build>` — switch build mode in
        the running session.  Clears dep_check_ready so the next
        pipeline step re-resolves under the new mode, and PERSISTS the
        choice to the untracked config/local.conf so it survives a
        restart (mode is a per-machine decision, not a repo-tracked one;
        build.conf is never touched)."""
        _valid = ('distribution', 'build')
        if value not in _valid:
            console.print(
                f"  invalid mode: {value!r}  (try: {' | '.join(_valid)})",
                tui.COLOR_ERROR)
            return
        if value == 'build':
            _block = self._build_mode_block_reason()
            if _block is not None:
                console.print(
                    f"  cannot switch to build mode: {_block}",
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
        # Persist to the machine-local sidecar (config/local.conf) so the
        # mode survives a restart.  A write failure must not abort the
        # in-memory switch — warn and carry on (the session is already in
        # the new mode; only durability is lost).
        try:
            utils.write_local_conf(self.config, mode=value)
            _persist = "persisted to config/local.conf"
        except OSError as _e:
            _persist = f"WARNING: could not persist to local.conf ({_e})"
        console.print(
            f"  mode  {_prev}  →  {value}  ({_persist})", tui.COLOR_HIGHLIGHT)
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

    # ── machine-local setters: persist to config/local.conf ──────────
    # These mutate the per-machine keys that the config split moved OUT of the
    # tracked config (build.conf/distro.conf) and INTO the untracked
    # local.conf.  Each validates, updates the live config, and write_local_conf
    # read-merge-writes the one field (other fields preserved).  build.conf is
    # never touched.

    def _set_name(self, value: str) -> None:
        """`set name <builder-id>` — this machine's builder identity."""
        _v = value.strip()
        if not (1 <= len(_v) <= 64
                and all(_c.isalnum() or _c in '-_' for _c in _v)):
            console.print(
                f"  invalid name: {value!r} (ascii letters/digits/-/_, 1-64)",
                tui.COLOR_ERROR)
            return
        self.config.system_name = _v
        utils.write_local_conf(self.config, name=_v)
        console.print(f"  name  →  {_v}  (saved to local.conf)",
                      tui.COLOR_HIGHLIGHT)

    def _set_jobs(self, value: str) -> None:
        """`set jobs <N>` — MaxParallelBuilds (1-8)."""
        try:
            _n = int(value)
        except ValueError:
            console.print(f"  jobs must be an integer 1-8, got {value!r}",
                          tui.COLOR_ERROR)
            return
        if not (1 <= _n <= 8):
            console.print(
                f"  jobs must be 1-8 (docker-py pool limit), got {_n}",
                tui.COLOR_ERROR)
            return
        self.config.max_parallel_builds = _n
        utils.write_local_conf(self.config, max_parallel_builds=_n)
        console.print(
            f"  jobs (MaxParallelBuilds)  →  {_n}  (saved to local.conf)",
            tui.COLOR_HIGHLIGHT)

    def _set_cpus(self, value: str) -> None:
        """`set cpus <F>` — per-container CPU quota (0 = no cap)."""
        try:
            _f = float(value)
        except ValueError:
            console.print(f"  cpus must be a number >= 0, got {value!r}",
                          tui.COLOR_ERROR)
            return
        if _f < 0:
            console.print(f"  cpus must be >= 0, got {_f}", tui.COLOR_ERROR)
            return
        self.config.build_cpus = _f
        utils.write_local_conf(self.config, build_cpus=_f)
        console.print(f"  cpus (BuildCpus)  →  {_f}  (saved to local.conf)",
                      tui.COLOR_HIGHLIGHT)

    def _set_memory(self, value: str) -> None:
        """`set memory <8g>` — per-container RAM cap ('' / 'none' = no cap)."""
        _v = value.strip()
        if _v.lower() in ('none', '0', 'off', ''):
            _v = ''
        elif not (_v[:-1].isdigit() and _v[-1] in 'bkmgBKMG') and not _v.isdigit():
            console.print(
                f"  memory must be a docker size like 8g/512m (or none), "
                f"got {value!r}", tui.COLOR_ERROR)
            return
        self.config.build_memory = _v
        utils.write_local_conf(self.config, build_memory=_v)
        console.print(
            f"  memory (BuildMemory)  →  {_v or '(no cap)'}  "
            "(saved to local.conf)", tui.COLOR_HIGHLIGHT)

    def _set_docker_server(self, value: str) -> None:
        """`set docker-server <url>` — local Docker daemon endpoint
        ('' / 'local' = default socket).  The exposure guard (refuses an
        unsafely-reachable bare tcp:// daemon) fires at `container local
        init`."""
        _v = value.strip()
        if _v.lower() in ('local', 'none', ''):
            _v = ''
        self.config.docker_server = _v
        utils.write_local_conf(self.config, docker_server=_v)
        console.print(
            f"  docker-server  →  {_v or '(local socket)'}  "
            "(saved to local.conf)", tui.COLOR_HIGHLIGHT)

    def _set_signing_uid(self, value: str) -> None:
        """`set signing-uid <'Name <email>'>` — repo signing identity."""
        _v = value.strip()
        if not ('<' in _v and _v.endswith('>') and '@' in _v):
            console.print(
                f"  signing-uid must be 'Name <email>', got {value!r}",
                tui.COLOR_ERROR)
            return
        self.config.signing_key_uid = _v
        utils.write_local_conf(self.config, signing_key_uid=_v)
        console.print(f"  signing-uid  →  {_v}  (saved to local.conf)",
                      tui.COLOR_HIGHLIGHT)

    _SETTABLE: 'dict[str, Callable]' = {}    # populated below
    _GETTABLE: 'dict[str, Callable]' = {}

    def cmd_configure(self, *args) -> None:
        """configure — first-run / re-run setup wizard.

        Establishes the system role (first/origin vs federation peer), the
        federation registration handshake for a peer, build mode, and the
        snapshot pin; records it in the untracked config/local.conf.  Until it
        succeeds, the command gate refuses the build pipeline.  Re-runnable any
        time to add a mirror or change mode."""
        import onboarding
        onboarding.run_onboarding(self)

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

    def cmd_version(self, *args) -> None:
        """version [--verbose] — show the Athena-Build TOOLCHAIN version.

        Distinct from the DISTRIBUTION version (`get build_version` / the Asgard
        release stamped into the ISO).  `--verbose` adds python / commit / build
        date and, for context, the distribution this toolchain is building.
        """
        import _version
        _verbose = any(_a in ('--verbose', '-v', 'verbose') for _a in args)
        console.print(_version.version_line(verbose=_verbose))
        if _verbose:
            _bv = getattr(self.config, 'build_version', None)
            if _bv:
                _distro = getattr(self.config, 'build_distribution', 'Asgard')
                _code = getattr(self.config, 'build_codename', '')
                console.print(f"  builds   {_distro} {_bv} ({_code})")

    def cmd_config(self, action: str = '', *args) -> None:
        """config check — validate build.conf identity + probe mirror
        reachability.  Read-only: no downloads, no mutation.  Surfaces config
        problems and dead mirrors up front instead of failing deep inside
        `cache build`.
        """
        if action and action != 'check':
            console.print(
                f"unknown config action {action!r}; try `config check`",
                tui.COLOR_ERROR)
            return
        _cfg = self.config
        # 1. validity
        if getattr(_cfg, 'is_valid', True):
            console.print("config: valid", tui.COLOR_HIGHLIGHT)
        else:
            console.print(
                f"config: INVALID — {getattr(_cfg, 'error_str', '')}",
                tui.COLOR_ERROR)
        # 2. identity
        console.print("identity:")
        console.print(
            f"  distribution    {_cfg.build_distribution} ({_cfg.build_base_id})")
        console.print(f"  codename        {_cfg.build_codename}")
        console.print(f"  version         {_cfg.build_version}")
        console.print(f"  arch            {_cfg.arch}")
        console.print(f"  target release  {_cfg.release} {_cfg.baseversion}")
        _tracks = ("tracks target" if _cfg.container_release == _cfg.release
                   else "pinned behind/ahead of target")
        console.print(
            f"  container rel   {_cfg.container_release}  ({_tracks})")
        # Snapshot pin + mirror reachability only run once the box is
        # CONFIGURED.  On a fresh community pull we stop here, after the
        # distro.conf identity above — there is no snapshot pin (and we must
        # NOT derive one from the repo by resolving 'latest', which would fix +
        # persist a timestamp), and no mirrors to probe yet.  `configure` sets
        # the pin (a federation mirror's `current`, or an explicit prompt) and
        # runs this check itself once setup completes.
        if not getattr(_cfg, 'setup_complete', False):
            return
        # 3. snapshot
        _ts = None
        if _cfg.snapshot_enabled:
            try:
                _ts = utils.resolve_snapshot_timestamp(_cfg)
                _human = (utils.format_snapshot_timestamp(_ts)
                          if _ts else '<unresolved>')
                console.print(f"  snapshot        {_human}")
            except Exception as _e:        # noqa: BLE001
                console.print(
                    f"  snapshot        resolution FAILED — {_e}",
                    tui.COLOR_ERROR)
        else:
            console.print("  snapshot        disabled (live mirrors)")
        # 4. mirror reachability
        console.print(
            f"mirror reachability ({len(_cfg.mirrors)} configured):")
        _reach = utils.check_mirror_reachability(
            _cfg.mirrors, _ts, _cfg.snapshot_baseurl)
        for _lbl, _ok, _det in _reach:
            console.print(
                f"  [{'OK  ' if _ok else 'DOWN'}] {_lbl}",
                tui.COLOR_HIGHLIGHT if _ok else tui.COLOR_ERROR)
            if not _ok:
                console.print(f"         {_det}", tui.COLOR_ERROR)
        _bad = [_l for _l, _ok, _d in _reach if not _ok]
        if _bad:
            console.print(
                f"{len(_bad)} mirror URL(s) unreachable — cache build will "
                "refuse until fixed.", tui.COLOR_ERROR)
        elif _reach:
            console.print("all mirrors reachable.", tui.COLOR_HIGHLIGHT)

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
            'disk':       'cache→parse→download→container→source build→chroot build disk→iso build disk (qcow2)',
            'build': 'cache→parse→download→container→source build (indl) — STOPS at source_build_ready (no chroot/ISO)',
        }
        # in [Build] Mode = build, bare `autorun`
        # routes to the build pipeline (the live/installer/disk
        # variants would refuse at their chroot/ISO steps anyway).
        # Defensive against missing .config (test doubles).
        _mode = getattr(getattr(self, 'config', None), 'build_mode',
                        'distribution')
        # The pipelines take no extra tokens — reject stray args with a
        # usage line instead of forwarding them to a zero-arg handler
        # (which would raise TypeError).
        if args:
            console.print(
                "Usage: autorun [live|installer|disk|build]  "
                f"(unexpected: {' '.join(args)})")
            return self._group_help('autorun', _table, action)
        if action == '' and _mode == 'build':
            return self.cmd_auto_run_build()
        if action in ('', 'live'):
            return self.cmd_auto_run_live()
        if action == 'installer':
            return self.cmd_auto_run_installer()
        if action == 'disk':
            return self.cmd_auto_run_disk()
        if action == 'build':
            return self.cmd_auto_run_build()
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
            (self.cmd_init_container,    'build_container_ready', 'container local init'),
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
            (self.cmd_init_container,    'build_container_ready',      'container local init'),
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
        disk image.

        The disk image is mastered from its OWN minimal chroot
        (buildroot/disk, the [Disk] Groups closure) — decoupled from the
        live/GNOME chroot.  Gates the final step on iso_disk_ready.
        """
        _steps = [
            (self.cmd_build_cache,       'cache_ready',           'cache build'),
            (self.cmd_parse_dependency,  'dep_check_ready',       'cache parse'),
            (self.cmd_source_sync,       'download_ready',        'source sync'),
            (self.cmd_init_container,    'build_container_ready', 'container local init'),
            (self.cmd_source_build,                                  # bare = pkg
                                          'source_build_ready',    'source build'),
            (self.cmd_build_chroot_disk, 'chroot_disk_ready',     'chroot build disk'),
            (self.cmd_build_iso_disk,    'iso_disk_ready',        'iso build disk'),
        ]
        self._run_autorun_steps('autorun disk', _steps)

    def cmd_auto_run_build(self):
        """Run the build-mode pipeline through to a complete
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
            (self.cmd_init_container,    'build_container_ready', 'container local init'),
            # call cmd_source_build BARE.  'build' is not a valid
            # _SOURCE_SUBSETS token, so it was classified as a package NAME
            # → "Unknown package: build" → source_build_ready never set →
            # autorun aborted at the last step.  Bare → the 'pkg' subset,
            # relabelled 'indl' in build mode (the intended whole-set build).
            (self.cmd_source_build,      'source_build_ready',    'source build indl'),
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
        # surface the build mode at the top of the autorun
        # run so the operator can never mistake a 5-step indl chain
        # for a broken 8-step live chain.
        _mode = getattr(self.config, 'build_mode', 'distribution')
        console.print(
            f"{label}: starting (MODE = {_mode})", tui.COLOR_HIGHLIGHT)
        _t0    = time.monotonic()
        _t0_dt = datetime.datetime.now(datetime.timezone.utc)
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

        _t1_dt   = datetime.datetime.now(datetime.timezone.utc)
        _elapsed = int(time.monotonic() - _t0)
        print_commands.summary(self, timing=print_commands.AutorunTiming(
            started=_t0_dt,
            finished=_t1_dt,
            elapsed=_elapsed,
            aborted_at=_aborted_at,
        ))

    # ─────────────────────────────────────────────────────────────────
    # build — OBS-02 cross-run build-history ledger
    # ─────────────────────────────────────────────────────────────────

    def cmd_build(self, action: str = '', *args):
        """`build <history>` — query the cross-run build ledger."""
        if action == 'history':
            return self.cmd_build_history(*args)
        self._group_help('build', {
            'history': 'per-package run pass/fail ledger: build history [pkg]',
        }, action)

    def cmd_build_history(self, *args) -> None:
        """`build history [pkg]` — OBS-02.  No pkg: every package's run count
        + rolling pass rate, flakiest first.  With pkg: that package's most
        recent runs.  Reads the append-only log/build-history.jsonl ledger."""
        _buildlog = os.path.join(self.config.dir_log, 'build')
        _rows = utils.read_build_history(_buildlog)
        if not _rows:
            console.print(
                "build history: no runs recorded yet "
                "(log/build-history.jsonl is empty — run a `source build`)",
                tui.COLOR_WARNING)
            return

        _pkg = args[0] if args else ''
        if _pkg:
            _runs = [_r for _r in _rows if _r.get('package') == _pkg]
            if not _runs:
                console.print(f"build history: no runs recorded for '{_pkg}'",
                              tui.COLOR_WARNING)
                return
            _pass = sum(1 for _r in _runs if _r.get('status') == 'PASS')
            _rate = round(100 * _pass / len(_runs))
            console.print(
                f"\nbuild history: {_pkg} — {len(_runs)} run(s), "
                f"{_pass} pass / {len(_runs) - _pass} fail ({_rate}%)\n",
                tui.COLOR_HIGHLIGHT)
            console.print(f"  {'ts':<22}{'status':<8}{'version':<20}"
                          f"{'elapsed':<11}{'peakRSS':<10}cpu%")
            for _r in _runs[-20:][::-1]:
                _el = _r.get('elapsed_seconds')
                _rss = _r.get('peak_rss_mb')       # OBS-03
                _cpu = _r.get('peak_cpu_pct')
                _color = (tui.COLOR_ERROR if _r.get('status') == 'FAIL'
                          else tui.COLOR_NORMAL)
                console.print(
                    f"  {str(_r.get('ts', '-')):<22}"
                    f"{str(_r.get('status', '-')):<8}"
                    f"{str(_r.get('version') or '-'):<20}"
                    f"{('-' if _el is None else f'{_el}s'):<11}"
                    f"{('-' if _rss is None else f'{_rss}MB'):<10}"
                    f"{('-' if _cpu is None else _cpu)}", _color)
            return

        # No package → per-package aggregate, flakiest first.
        _agg: 'dict[str, dict]' = {}
        for _r in _rows:
            _p = _r.get('package')
            if not _p:
                continue
            _a = _agg.setdefault(_p, {'runs': 0, 'pass': 0, 'last': None})
            _a['runs'] += 1
            if _r.get('status') == 'PASS':
                _a['pass'] += 1
            _a['last'] = _r          # ledger is chronological
        _total = len(_rows)
        _tpass = sum(1 for _r in _rows if _r.get('status') == 'PASS')
        console.print(
            f"\nbuild history — {_total} run(s) across {len(_agg)} package(s)\n",
            tui.COLOR_HIGHLIGHT)
        console.print(f"  {'package':<28}{'runs':>5}{'pass':>6}{'fail':>6}"
                      f"{'rate':>6}  last")
        for _p, _a in sorted(
                _agg.items(),
                key=lambda _kv: (_kv[1]['runs'] - _kv[1]['pass'], _kv[1]['runs']),
                reverse=True):
            _fail = _a['runs'] - _a['pass']
            _rate = round(100 * _a['pass'] / _a['runs']) if _a['runs'] else 0
            _last = _a['last'] or {}
            _color = tui.COLOR_ERROR if _fail else tui.COLOR_NORMAL
            console.print(
                f"  {_p[:27]:<28}{_a['runs']:>5}{_a['pass']:>6}{_fail:>6}"
                f"{_rate:>5}%  {str(_last.get('status', '-'))} "
                f"{str(_last.get('ts', '-'))[:10]}", _color)
        _orate = round(100 * _tpass / _total) if _total else 0
        console.print(
            f"\n  totals: {_total} run(s), {_tpass} pass, "
            f"{_total - _tpass} fail ({_orate}% overall)\n", tui.COLOR_INFO)
