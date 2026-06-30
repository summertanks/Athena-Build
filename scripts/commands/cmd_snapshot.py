"""Snapshot pin management — the `snapshot` command cluster.

Manages the upstream `current` snapshot pin (config/snapshot.state) and
the update-workload views.  Extracted verbatim from build.py's
BuildSession; see commands/base.py for how the mixin shares session state.
"""
import logging
import re

import tui
import utils
from tui import console, Prompt, PROMPT_YESNO, PROMPT_INPUT

from commands.base import SessionState

logger = logging.getLogger('athena.build')


class SnapshotCommandsMixin(SessionState):
    # ─────────────────────────────────────────────────────────────────────
    # Snapshot pin management
    # ─────────────────────────────────────────────────────────────────────

    def cmd_snapshot(self, action: str = '', *args):
        """Manage the upstream `current` snapshot pin and inspect the update
        workload.  The pin persists in config/snapshot.state (durable —
        survives `clean cache`) and takes precedence over [Snapshot] Timestamp.

        Usage:
          snapshot                          — status overview (current/latest)
          snapshot list                     — current + the snapshots between
                                              current and latest
          snapshot workload [<ts>|latest]   — sources changed current → target
          snapshot select                   — interactive picker: choose the next
                                              current from the in-between snapshots
          snapshot select <ts|latest>       — set current explicitly (forward-only)
          snapshot select force             — prompt for ANY ts (backtrack OK)
          snapshot advance <ts|latest>      — alias for `select <ts|latest>`
          snapshot history                  — last 20 selected current pins

        Forward-only by default.  `select force` is the documented
        backtrack path.  Archive-floor and per-target published pins live
        in per-mirror state (see `mirror summary`), not here.
        """
        _table = {
            'list':         'current + the snapshots between current and latest',
            'workload':     'sources changed current → target: snapshot workload [<ts>|latest]',
            'select':       'pick the next current interactively (no args), or '
                            'explicitly: snapshot select <ts|latest>',
            'select force': 'backtrack / arbitrary set: prompt for any timestamp '
                            '(bypasses forward-only; cautioned)',
            'advance':      'alias for `select <ts|latest>`',
            'history':      'last 20 selected current pins (newest first)',
            'status':       'status overview (also the bare `snapshot`)',
        }
        if action in ('', 'status'):
            return self._cmd_snapshot_overview()
        if action == 'list':
            return self._cmd_snapshot_list()
        if action == 'workload':
            return self._cmd_snapshot_workload(*args)
        if action == 'select':
            return self._cmd_snapshot_select(*args)
        if action == 'advance':
            return self._cmd_snapshot_select(*args)
        if action == 'history':
            return self._cmd_snapshot_history(*args)
        return self._group_help('snapshot', _table, action)

    def _snapshot_current(self):
        """Effective current pin (state.current > [Snapshot] Timestamp)."""
        try:
            return utils.resolve_snapshot_timestamp(self.config)
        except Exception as e:
            logger.warning(f"_snapshot_current: {e}")
            return None

    def _mirror_floor(self) -> str:
        """Return min(mirror.<each>.state.current) across every configured
        mirror — the workload floor for (`_do_update_build`).

        When NO mirrors are configured, returns the local current pin
        (workload = "since current" = empty; no UPDATE mode work).
        When a mirror is configured but never published, its current is
        empty — treated as unconditionally behind (workload = full).
        """
        import mirror as _mirror_mod
        _names = _mirror_mod.list_mirrors(self.config)
        if not _names:
            return self._snapshot_current() or ''
        _floor = None
        for _n in _names:
            _st = _mirror_mod.read_mirror_state(self.config, _n)
            if _st is None:
                continue
            _mc = _st.get('current') or ''
            if not _mc:
                # Never published → behind everything → empty floor
                return ''
            if _floor is None or _mc < _floor:
                _floor = _mc
        return _floor or self._snapshot_current() or ''

    def _snapshot_latest(self):
        """Latest upstream timestamp, or None on query failure."""
        try:
            return utils._query_snapshot_latest(
                self.config.snapshot_timestamp_api,
                self.config.snapshot_archive_keys)
        except Exception as e:
            logger.warning(f"_snapshot_latest: {e}")
            return None

    def _prompt_for_timestamp(self, label: str, default: str,
                              allow_latest: bool) -> str:
        """Prompt for a snapshot timestamp (or `latest` when allowed), applying
        `default` on empty input and resolving `latest`.  Returns the resolved
        YYYYMMDDTHHMMSSZ string, or '' if the operator gives up (3 tries)."""
        for _ in range(3):
            _ans = Prompt(
                PROMPT_INPUT,
                f"Select {label} [default {default}]:").get_response().strip()
            if not _ans:
                _ans = default
            if _ans == 'latest':
                if not allow_latest:
                    console.print("  'latest' not allowed here — enter a "
                                  "concrete timestamp", tui.COLOR_WARNING)
                    continue
                _resolved = self._snapshot_latest()
                if not _resolved:
                    console.print("  could not resolve 'latest' — enter a "
                                  "concrete timestamp", tui.COLOR_WARNING)
                    continue
                return _resolved
            if re.match(r'^\d{8}T\d{6}Z$', _ans):
                return _ans
            console.print(f"  '{_ans}' is not a YYYYMMDDTHHMMSSZ timestamp",
                          tui.COLOR_WARNING)
        return ''

    def _ensure_snapshot_pins(self) -> bool:
        """Before cache build: ensure a durable CURRENT pin exists in
        config/snapshot.state.  On a fresh system (the state file is gitignored,
        so a new checkout has none) PROMPT the operator to select one — cache
        build depends on the snapshot.  Returns True to proceed, False to abort.

        Only `current` is local state.  Archive-floor `base` and per-target
        `published` pins live in per-mirror state (see `mirror summary`)."""
        if not self.config.snapshot_enabled:
            return True
        if utils.read_snapshot_state(self.config).get('current'):
            return True

        console.print(
            "No snapshot pin defined (config/snapshot.state is empty).  cache "
            "build needs a CURRENT pin (the build snapshot) — select now.",
            tui.COLOR_WARNING)
        _cfg_ts = str(self.config.snapshot_timestamp_config).strip()
        _default_cur = (_cfg_ts if re.match(r'^\d{8}T\d{6}Z$', _cfg_ts)
                        else 'latest')
        _current = self._prompt_for_timestamp(
            "current snapshot (YYYYMMDDTHHMMSSZ or 'latest')",
            _default_cur, allow_latest=True)
        if not _current:
            console.print("cache build aborted — no current snapshot selected.",
                          tui.COLOR_ERROR)
            return False
        utils.write_snapshot_state(self.config, current=_current)
        utils.append_snapshot_history(self.config, _current)
        console.print(
            f"snapshot pin set — current={_current} (config/snapshot.state)",
            tui.COLOR_HIGHLIGHT)
        return True

    def _cmd_snapshot_overview(self):
        """READ-ONLY status: current + latest + the current source.

        Archive-floor and per-target published pins live in per-mirror
        state — see `mirror summary`.  Only the local build pin
        (`current`) lives in config/snapshot.state.
        """
        if not self.config.snapshot_enabled:
            console.print(
                "  snapshot pinning is DISABLED ([Snapshot] Enabled=false)")
            return
        _cur = self._snapshot_current()
        _src = ('config/snapshot.state'
                if utils.read_snapshot_state(self.config).get('current')
                else f'[Snapshot] Timestamp={self.config.snapshot_timestamp_config}')
        console.print("Snapshot status:")
        console.print(f"  current  (build pin) : {_cur or '(unresolved)'}")
        console.print(f"  current source       : {_src}")
        _latest = self._snapshot_latest()
        console.print(f"  latest upstream      : {_latest or '(query failed)'}")
        # Update-pending hint: ANY laggard mirror means there's work to
        # publish.  Uses the unified `_update_build_pending` gate which
        # reads per-mirror state when mirrors are configured (Phase 4).
        if self._update_build_pending():
            console.print(
                f"  → at least one publish target is behind {_cur} — "
                f"`refresh` to build + publish the delta")
        if _cur and _latest and _latest > _cur:
            console.print("  → latest is AHEAD of current ", tui.COLOR_INFO)
            console.print("  — `snapshot workload latest` to see the delta\n"
                          "  - `snapshot select latest` to roll forward")

    def _cmd_snapshot_list(self):
        """READ-ONLY: current + every snapshot BETWEEN current and latest
        (numbered) — the ones you can advance current to."""
        if not self.config.snapshot_enabled:
            console.print("  snapshot pinning is DISABLED")
            return
        _cur = self._snapshot_current()
        _latest = self._snapshot_latest()
        console.print("Snapshot timeline:")
        console.print(f"  [ current ] {_cur or '(unresolved)'}"
                      + (f"  ({utils.format_snapshot_timestamp(_cur)})"
                         if _cur else ''))
        if not (_cur and _latest):
            console.print(f"  [ latest ] {_latest or '(query failed)'}")
            return
        _cands = utils.list_snapshots_between(self.config, _cur, _latest)
        if not _cands:
            console.print(f"  [ latest ] {_latest}  (current is already latest — "
                          f"nothing to advance to)")
            return
        console.print(f"  available to advance to — {len(_cands)} snapshot(s) "
                      f"between current and latest:")
        for _i, _ts in enumerate(_cands, 1):
            _mark = '  ← latest' if _ts == _latest else ''
            console.print(
                f"    {_i:3d}  {_ts}  "
                f"({utils.format_snapshot_timestamp(_ts)}){_mark}")
        console.print(
            "  → `snapshot select` opens an interactive picker to set one of "
            "these as the new current")

    def _cmd_snapshot_select(self, *args):
        """Set the `current` snapshot pin.

        Forms:
          snapshot select               — interactive picker (forward-only
                                          candidates between current and latest)
          snapshot select <ts|latest>   — explicit; forward-only
          snapshot select force         — prompt for ANY timestamp (including
                                          older than current); cautioned +
                                          confirmed; intended for backtrack /
                                          operator-driven recovery only.

        Forward-only is the default rule because's `+asg uN` ledger and
        the per-mirror federation state would otherwise drift.  `force` is the
        escape hatch — no list, no candidate filter, just a free-form prompt.
        """
        _target = args[0] if args else ''
        if not _target:
            return self._snapshot_select_interactive()
        if _target == 'force':
            return self._snapshot_select_force()
        if _target == 'latest':
            _resolved = self._snapshot_latest()
            if not _resolved:
                console.print("snapshot select: could not resolve `latest`",
                              tui.COLOR_ERROR)
                return
            _target = _resolved
        self._set_snapshot_pin(_target)

    def _snapshot_select_force(self):
        """Backtrack / arbitrary set: prompt for a timestamp, accept it
        regardless of direction.  Bypasses the forward-only rule but keeps
        the y/n caution + history append + cache-flag invalidation."""
        _cur = self._snapshot_current() or ''
        console.print(
            "snapshot select force: bypasses forward-only.  Backtracking "
            "current invalidates the +asg uN ledger expectations for the "
            "delta between current and the new pin — only use when you "
            "know what you're doing.", tui.COLOR_WARNING)
        _ans = Prompt(
            PROMPT_INPUT,
            f"New current (YYYYMMDDTHHMMSSZ; default {_cur or '(unset)'}):"
        ).get_response().strip()
        if not _ans:
            console.print("  cancelled — pin unchanged")
            return
        if not re.match(r'^\d{8}T\d{6}Z$', _ans):
            console.print(
                f"snapshot select force: '{_ans}' is not a YYYYMMDDTHHMMSSZ "
                f"timestamp", tui.COLOR_ERROR)
            return
        if Prompt(PROMPT_YESNO,
                  f"Set current pin to {_ans} (override forward-only)?"
                  ).get_response().lower() not in ('y', 'yes'):
            console.print("  aborted — pin unchanged")
            return
        if _cur:
            _direction = ('BACKTRACK' if _ans < _cur
                          else 'forward' if _ans > _cur else 'unchanged')
            console.print(
                f"  {_direction}: {_cur} → {_ans}", tui.COLOR_WARNING)
        utils.write_snapshot_state(self.config, current=_ans)
        utils.append_snapshot_history(self.config, _ans)
        # Sync the build.conf [Snapshot] Timestamp mirror NOW (like the
        # non-force `snapshot select` path) so the visible config doesn't lie
        # about the active pin until the next startup's lazy reconcile.
        _synced = utils.reconcile_snapshot_pin(self.config)
        console.print(
            f"snapshot select force: current pin set to {_ans} "
            f"(config/snapshot.state; appended to config/snapshot.history"
            + ("; build.conf [Snapshot] Timestamp synced" if _synced else "")
            + ")")
        self.flags.cache_ready = False
        self.flags.dep_check_ready = False
        console.print(
            "  cache invalidated — run `cache build` + `cache parse` to "
            "resolve the dep tree at the new pin")

    def _has_unpublished_local_builds(self) -> bool:
        """True iff we hold LOCALLY-BUILT (not pulled) artifacts that aren't yet
        published to a mirror — i.e. advancing the pin would skip publishing real
        local work.  Uses `generate_pending_claims`, which already SKIPS
        `pulled_from` records, so a peer-pulled delta (already on the mirror)
        does NOT count.  Best-effort: False (no warning) on any
        non-federated / IO error — the warning is advisory, never a gate."""
        import os
        try:
            import coord.publish as _publish
            _bid = self._coord_builder_id()
            _keys = self._coord_self_keys()
            if not _bid or _keys is None:
                return False
            _pending = _publish.generate_pending_claims(
                builder_id=_bid,
                buildlog_dir=os.path.join(self.config.dir_log, 'build'),
                claims_dir=self.config.dir_coord_claims,
                public_key_path=_keys[2],
                snapshot_pin=self._snapshot_current() or '',
                read_build_record=utils.read_build_record,
                build_arch=self.config.arch,
            )
            return len(_pending) > 0
        except Exception as _e:    # noqa: BLE001 — advisory; never block select
            logger.warning(
                f"snapshot select: unpublished-builds check skipped: {_e}")
            return False

    def _set_snapshot_pin(self, target: str) -> bool:
        """Validate (forward-only) + caution + write the `current` pin.
        Appends to config/snapshot.history on success.  Returns True iff set."""
        if not re.match(r'^\d{8}T\d{6}Z$', target):
            console.print(f"snapshot select: '{target}' is not a "
                          f"YYYYMMDDTHHMMSSZ timestamp", tui.COLOR_ERROR)
            return False
        _old = self._snapshot_current()
        if _old and target < _old:
            console.print(
                f"snapshot select: REFUSED — current can only move forward "
                f"({target} < {_old}).  To backtrack, edit "
                f"config/snapshot.state directly (documented manual override).",
                tui.COLOR_ERROR)
            return False
        console.print(f"snapshot select: {_old or '(unset)'} → {target}",
                      tui.COLOR_WARNING)
        console.print(
            "  PRODUCTION IMPACT: next publish will be on selected snapshot")
        # Warn ONLY when we have locally-BUILT, unpublished work at the old pin
        # — advancing would skip publishing it.  A peer-pulled delta (already on
        # the mirror) is NOT our work to publish and must not trigger this; the
        # prior pin>floor guard fired on any difference (incl. pulled state), so
        # a recipient saw a false alarm.
        if _old and self._has_unpublished_local_builds():
            console.print(
                f"  WARNING: you have locally-built, UNPUBLISHED packages at "
                f"{_old}. May run `mirror publish` first.",
                tui.COLOR_WARNING)
        if Prompt(PROMPT_YESNO,
                  f"Set current pin to {target}?").get_response().lower() \
                not in ('y', 'yes'):
            console.print("  aborted — pin unchanged")
            return False
        utils.write_snapshot_state(self.config, current=target)
        utils.append_snapshot_history(self.config, target)
        # Sync build.conf [Snapshot] Timestamp NOW, as part of a successful
        # `snapshot select` — NOT lazily on the next startup's
        # reconcile_snapshot_pin.  Otherwise the visible config lies about the
        # active pin between this command and the next process start, where it
        # would surface as a surprise "updated build.conf to match" warning on
        # an unrelated run.  snapshot.state stays the authoritative pin; this
        # just keeps its build.conf mirror honest immediately.
        _synced = utils.reconcile_snapshot_pin(self.config)
        console.print(
            f"snapshot select: current pin set to {target} "
            "(config/snapshot.state; appended to config/snapshot.history"
            + ("; build.conf [Snapshot] Timestamp synced" if _synced else "")
            + ")")
        # The resolved pin changed → the in-memory cache is now stale; force
        # the operator to re-resolve at the new pin before building.
        self.flags.cache_ready = False
        self.flags.dep_check_ready = False
        console.print(
            "  cache invalidated — run `cache build` + `cache parse`")
        return True

    def _snapshot_select_interactive(self):
        """Modal picker (like `cache select`): list the snapshots between
        current and latest and set the chosen one as the new current.  Only a
        snapshot ABOVE current and up to latest can be picked."""
        if not self.config.snapshot_enabled:
            console.print("snapshot select: snapshot pinning is disabled")
            return
        _cur = self._snapshot_current()
        if not _cur:
            console.print("snapshot select: current pin unresolved — set one "
                          "with `snapshot select <ts>`", tui.COLOR_ERROR)
            return
        _latest = self._snapshot_latest()
        if not _latest:
            console.print("snapshot select: could not query available snapshots",
                          tui.COLOR_ERROR)
            return
        _cands = utils.list_snapshots_between(self.config, _cur, _latest)
        if not _cands:
            console.print(f"snapshot select: current {_cur} is already at/after "
                          f"latest {_latest} — nothing to advance to.")
            return
        console.print(
            f"Advance current ({_cur}) → one of {len(_cands)} snapshot(s) up to "
            f"latest ({_latest}):")
        for _i, _ts in enumerate(_cands, 1):
            _mark = '  ← latest' if _ts == _latest else ''
            console.print(
                f"  {_i:3d}  {_ts}  "
                f"({utils.format_snapshot_timestamp(_ts)}){_mark}")
        _ans = Prompt(
            PROMPT_INPUT,
            f"Pick a number (1-{len(_cands)}) to set as the new current, or "
            f"Enter to cancel:").get_response().strip()
        if not _ans:
            console.print("  cancelled")
            return
        try:
            _idx = int(_ans)
        except ValueError:
            console.print(f"  '{_ans}' is not a number", tui.COLOR_ERROR)
            return
        if not (1 <= _idx <= len(_cands)):
            console.print(f"  {_idx} out of range (1-{len(_cands)})",
                          tui.COLOR_ERROR)
            return
        self._set_snapshot_pin(_cands[_idx - 1])

    def _cmd_snapshot_history(self, *args):
        """READ-ONLY: print the last 20 selected current pins (newest first)
        from config/snapshot.history.  Operator-facing only — never
        load-bearing.
        """
        del args
        _h = utils.read_snapshot_history(self.config)
        if not _h:
            console.print(
                "snapshot history: empty (no `snapshot select` has run yet).")
            return
        console.print(
            f"Snapshot history (last {len(_h)} selected; newest first):")
        _current = self._snapshot_current()
        for _i, _ts in enumerate(_h, 1):
            _mark = '  ← current' if _ts == _current else ''
            console.print(
                f"  {_i:3d}  {_ts}  "
                f"({utils.format_snapshot_timestamp(_ts)}){_mark}")

    def _cmd_snapshot_workload(self, *args):
        """READ-ONLY: sources that change from the CURRENT pin to the TARGET
        (latest, or an explicit <ts>) — the rebuild set if you advance
        current → target via `snapshot select` + `source build all`."""
        if not self.flags.dep_check_ready:
            console.print(
                "snapshot workload: run `cache build` + `cache parse` first",
                tui.COLOR_ERROR)
            return
        _cur = self._snapshot_current()
        _target = args[0] if args else 'latest'
        if _target == 'latest':
            _target = self._snapshot_latest()
            if not _target:
                console.print("snapshot workload: could not resolve `latest`",
                              tui.COLOR_ERROR)
                return
        if not re.match(r'^\d{8}T\d{6}Z$', _target):
            console.print(
                f"snapshot workload: '{_target}' is not a YYYYMMDDTHHMMSSZ "
                f"timestamp or `latest`", tui.COLOR_ERROR)
            return
        console.print(f"snapshot workload: current {_cur} → target {_target}")
        if _cur and _target == _cur:
            console.print("  current == target — nothing would change.")
            return
        console.print(f"  fetching target Sources index ({_target})…")
        _names, _err = self._workload_current_to_target(_target)
        if _err:
            console.print(f"snapshot workload: {_err}", tui.COLOR_ERROR)
            return
        console.print(f"  {len(_names)} source(s) change current → target:")
        for _n in _names:
            console.print(f"    {_n}")
        _off = self._preflight_stamp_invariant(_names)
        if _off:
            console.print(
                f"  Guard A: {len(_off)} preflight issue(s) would BLOCK a "
                f"refresh:", tui.COLOR_ERROR)
            for _f, _why in _off[:20]:
                console.print(f"    {_f}: {_why}")
