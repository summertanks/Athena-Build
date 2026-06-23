"""Pre-ship audit — the `audit` command surface.

The repo-audit gate (dep closure + conflicts + stale files + content
integrity + NMU residue) and the source/repo preflight gates that
`chroot build` runs before it touches the chroot.  Extracted verbatim
from build.py's BuildSession; see commands/base.py for how the mixin
shares session state.
"""
import logging
import os
import re

import buildcontainer
import repo_audit
import tui
import utils
from tui import console, Prompt, PROMPT_YESNO, ProgressBar

from commands.base import SessionState

logger = logging.getLogger('athena.build')


def _dedupe_bidirectional_conflicts(conflicts):
    """Collapse `A Conflicts B` + `B Conflicts A` into a single entry.

    Many Debian conflicts are declared symmetrically (both sides of a
    pair-alternative carry `Conflicts:` against each other).  Reporting
    both halves doubles the apparent count and clutters the operator's
    triage.  We keep the entry whose consumer name sorts first.

    Operates on the list shape produced by audit_repo_closure:
      [(consumer_pkg, field, other_pkg, relation_str), ...]
    """
    _seen = set()
    _out = []
    for _entry in conflicts:
        _consumer, _field, _other, _rel = _entry
        # Strip any <virtual …> wrapper for symmetry comparison —
        # if the other side resolves via virtual, the reverse-direction
        # entry (real-pkg → real-pkg) is the canonical one to keep.
        _other_canon = _other
        if _other_canon.startswith('<virtual ') and _other_canon.endswith('>'):
            _other_canon = _other_canon[len('<virtual '):-1]
        _key = frozenset({_consumer, _other_canon})
        # Tiebreak: prefer the entry where consumer sorts first by name,
        # so subsequent runs of the same audit produce stable output.
        if _key in _seen:
            continue
        _seen.add(_key)
        _out.append(_entry)
    return _out


class AuditCommandsMixin(SessionState):
    def _preflight_audit_source(self) -> bool:
        """Source-side audit gate for `chroot build live/installer`.

        N/A in build mode (chroot/ISO are refused there — see
        _refuse_in_build_mode in chunk 3).  In dist mode walks
        `_source_state` over the merged deb+udeb dep tree.
        Aborts (with operator y/n prompt) when any of these states
        are non-empty for selected sources:

          needs_build  — binaries missing or invalid (legitimate rebuild)
          needs_sync   — source files missing or .verified absent
          stale_pass   — record=PASS but state drifted (patches changed
                          since the build wrote the record)
          interrupted  — build process was killed mid-flight; on-disk
                          artifacts can't be trusted

        All four states would either miss .debs entirely or include
        stale ones if the chroot build proceeded.

        Returns True to proceed, False to abort.  Cheap (~5s) — each
        per-source classify is a few stat() calls.

        If dep_tree isn't built (operator hit chroot build before
        cache parse), gate is skipped with a hint — the source-build
        flag guard above already catches that miss-ordering.
        """
        if not (self.dep_tree and self.dep_tree.selected_srcs):
            return True

        assert self.dep_tree is not None
        _srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _n, _s in self.udeb_dep_tree.selected_srcs.items():
                if _n not in _srcs:
                    _srcs[_n] = _s

        _hard = ('needs_build', 'needs_sync', 'stale_pass', 'interrupted')
        _by_state: 'dict[str, list[str]]' = {
            _s: [] for _s in _hard
        }
        for _name, _src in _srcs.items():
            _state = self._source_state(_name, _src)
            if _state in _by_state:
                _by_state[_state].append(_name)

        _hard_count = sum(len(_by_state[_s]) for _s in _hard)

        if _hard_count == 0:
            console.print(
                f"Source audit OK: {len(_srcs)} sources synced, "
                f"built, fresh."
            )
            return True

        console.print("Source audit found build-state issues:")
        for _state in _hard:
            _names = _by_state[_state]
            if not _names:
                continue
            console.print(f"  {len(_names):5d}  {_state}")
        for _state in _hard:
            _names = sorted(_by_state[_state])
            if not _names:
                continue
            self._print_wrapped_names(f"    {_state}", _names)
        console.print(
            "  Fix: `source build` (rebuilds), `source repair` "
            "(clears stale records), `source sync` (re-fetches)."
        )

        _resp = Prompt(
            PROMPT_YESNO,
            "Proceed with chroot build despite source audit "
            "findings?  (rebuild recommended)",
        ).get_response()
        return _resp.lower() in ('y', 'yes')

    def _preflight_audit_repo(self) -> bool:
        """Repo audit gate for `chroot build live/installer`.

        Runs the full three-check audit (dep gate over the whole repo +
        conflict cohorts for live and installer).  Cheap (~3s) when the
        persisted Packages snapshot is fresher than repo/'s max mtime.

        Returns True to proceed, False to abort.

        Gate triggers on:
          - any unresolved hard Depends/Pre-Depends in the whole repo
          - any conflict within the live cohort
          - any conflict within the installer cohort
          - any build.json↔disk output-hash mismatch
          - any stale artifact in repo/ (version-drift / orphan-source /
            malformed) — version-drift files are silently consumable by
            the chroot installer (superseded e2fsprogs poisoned the disk
            image, 2026-06-11), so obsolete files BLOCK until
            `repo repair cleanup`

        When dep_tree / udeb_dep_tree aren't populated, the relevant
        cohort check is skipped with a hint to run `cache parse`.  Dep
        check still runs unconditionally.

        I/O error → don't gate (fall back to install-time discovery).
        """
        _state = repo_audit.scan_repo_state(self.config)
        if _state is None:
            console.print(
                "Repo audit: scan failed (see log) — skipping gate, "
                "proceeding with chroot build"
            )
            return True
        if not _state.packages:
            return True

        _corpus = self._resolve_install_corpus()
        _unresolved, _ = repo_audit.audit_dep_closure(
            _state, consumer_set=_corpus,
        )
        # Classify the actionable subclass of the unresolved set: bare
        # pristine `=` pins whose repo target is +asg-stamped (the
        # cross-source strip hazard).  A subset of _unresolved, so it is
        # NOT added to the risk count — surfaced separately with a remedy.
        _asg_pins = repo_audit.detect_dangling_asg_equals_pins(
            _state, consumer_set=_corpus,
        )
        _live = self._resolve_live_cohort()
        _installer = self._resolve_installer_cohort()
        _live_conflicts = (
            _dedupe_bidirectional_conflicts(
                repo_audit.audit_conflict_cohort(_state, _live)
            ) if _live is not None else []
        )
        _inst_conflicts = (
            _dedupe_bidirectional_conflicts(
                repo_audit.audit_conflict_cohort(_state, _installer)
            ) if _installer is not None else []
        )

        # RECORD↔DISK integrity: every terminal build.json output_hashes
        # entry must match the on-disk artifact.  Catches stale hashes from a
        # post-record re-pack/re-stamp (mtime-cached, so a clean repo is near
        # free).  'absent' outputs are NORMAL (repo is a pruned subset), so
        # only 'mismatched' counts as a risk.
        _hash_audit = utils.verify_output_hashes(
            os.path.join(self.config.dir_log, 'build'), self.config.dir_repo,
        )
        _hash_drift = _hash_audit['mismatched']

        # STALE-FILE gate: a superseded .deb lingering in
        # repo/ is SILENTLY consumed — find_matching_artifact accepts any
        # +asg-stamped variant of the predicted pristine name, so a
        # version-drift artifact from a pre-fix build can poison the
        # chroot.  Caught live: e2fsprogs +asg1u1 (broken Pre-Depends)
        # was installed while the fixed +asg1u2 sat beside it; the disk
        # image reboot-looped and nothing flagged it.  Orphan-source and
        # malformed files gate too — all are one `repo repair cleanup`
        # away from gone.
        _stale_orphan: list = []
        _stale_drift: list = []
        _stale_foreign: list = []
        _stale_malformed: list = []
        if self.flags.dep_check_ready:
            (_stale_orphan, _stale_drift, _stale_foreign, _stale_malformed,
             _) = self._scan_stale_files()
        else:
            console.print(
                "Repo audit: stale-file gate SKIPPED — run `cache parse` "
                "to enable (needs selected_srcs)")

        # Foreign cross-toolchains are NON-gating here: they're inert
        # build by-products (not install targets, don't poison the chroot
        # the way version-drift does), and a binutils/gcc rebuild produces
        # fresh ones every time — gating local builds on them would be a
        # workflow trap.  The HARD gate lives at publish (`mirror audit`
        # → foreign_target_claim_published).  Surfaced here as a prune
        # nudge only.
        def _foreign_note() -> None:
            if not _stale_foreign:
                return
            _fshow = min(5, len(_stale_foreign))
            console.print(
                f"\n{len(_stale_foreign)} foreign cross-toolchain "
                f"by-product(s) in repo/ (binary targets a non-"
                f"{self.config.arch} arch — never ships; `repo repair "
                f"cleanup` or `mirror withdraw-foreign` prunes them):",
                tui.COLOR_WARNING)
            for _sub, _fn, _src, _sz in _stale_foreign[:_fshow]:
                console.print(f"  {_sub}/{_fn}  (source: {_src})")

        _n_stale = (len(_stale_orphan) + len(_stale_drift)
                    + len(_stale_malformed))
        _bad = (
            len(_unresolved) + len(_live_conflicts) + len(_inst_conflicts)
            + len(_hash_drift) + _n_stale
        )
        if _bad == 0:
            console.print(
                f"Repo audit OK: {len(_state.packages)} pkgs, "
                f"hard-dep closure clean, no install-cohort conflicts, "
                f"build.json↔disk hashes match ({_hash_audit['scanned']} "
                f"outputs checked), no stale artifacts."
            )
            _foreign_note()
            return True
        console.print(
            f"Repo audit found install-time risks:\n"
            f"  UNRESOLVED Depends/Pre-Depends (whole repo): "
            f"{len(_unresolved)}\n"
            f"  CONFLICTS in LIVE cohort:                    "
            f"{len(_live_conflicts)}\n"
            f"  CONFLICTS in INSTALLER ramdisk cohort:       "
            f"{len(_inst_conflicts)}\n"
            f"  build.json↔disk HASH MISMATCHES:             "
            f"{len(_hash_drift)}\n"
            f"  STALE artifacts (version-drift):             "
            f"{len(_stale_drift)}\n"
            f"  STALE artifacts (orphan-source/malformed):   "
            f"{len(_stale_orphan) + len(_stale_malformed)}"
        )
        _foreign_note()
        if _stale_drift:
            _sshow = min(5, len(_stale_drift))
            console.print(
                f"\nFirst {_sshow} VERSION-DRIFT artifacts (older build of "
                f"a selected source — the chroot installer can pick these "
                f"up SILENTLY instead of the current build; "
                f"`repo repair cleanup` removes them):")
            for _sub, _fn, _src, _sz in _stale_drift[:_sshow]:
                console.print(f"  {_sub}/{_fn}  (source: {_src})")
        if _stale_orphan or _stale_malformed:
            console.print(
                f"\n{len(_stale_orphan)} orphan-source / "
                f"{len(_stale_malformed)} malformed artifact(s) — "
                f"`repo repair cleanup` to review/remove.")
        if _hash_drift:
            _hshow = min(10, len(_hash_drift))
            console.print(
                f"\nFirst {_hshow} HASH MISMATCHES (record ≠ on-disk — "
                f"re-pack drift; refresh the record or rebuild):")
            for _pkg, _o in _hash_drift[:_hshow]:
                console.print(f"  {_pkg}  {_o}")
        _show = min(10, len(_unresolved))
        if _show:
            console.print(f"\nFirst {_show} UNRESOLVED:")
            for _pkg, _field, _rel, _why in _unresolved[:_show]:
                console.print(f"  {_pkg}  {_field}: {_rel}")
        if _asg_pins:
            _ashow = min(10, len(_asg_pins))
            console.print(
                f"\n{len(_asg_pins)} of these are dangling `=` pins on "
                f"+asg-stamped targets (cross-source strip hazard); "
                f"first {_ashow}:")
            for _c, _f, _t, _pin, _av, _rem in _asg_pins[:_ashow]:
                console.print(
                    f"  {_c}  {_f}: {_t} (= {_pin})  → repo has {_av};  {_rem}")
        _show = min(10, len(_live_conflicts))
        if _show:
            console.print(f"\nFirst {_show} LIVE conflicts:")
            for _pkg, _field, _other, _rel in _live_conflicts[:_show]:
                console.print(f"  {_pkg}  {_field}: {_rel}  → {_other}")
        _show = min(10, len(_inst_conflicts))
        if _show:
            console.print(f"\nFirst {_show} INSTALLER conflicts:")
            for _pkg, _field, _other, _rel in _inst_conflicts[:_show]:
                console.print(f"  {_pkg}  {_field}: {_rel}  → {_other}")
        console.print(
            "\nRun `repo audit verbose` for the full lists."
        )
        _resp = Prompt(
            PROMPT_YESNO,
            "Proceed with chroot build anyway?  (n recommended; "
            "fix the issues first)",
        ).get_response()
        return _resp.lower() in ('y', 'yes')

    def _audit_row(self, label: str, result: str, ok: bool = True) -> None:
        """One aligned `repo audit` row: a left-justified section label and
        its result, coloured green-ish (ok) or amber (attention).  Keeps the
        audit's output consistent with the rest of the CLI (label + result,
        no `=== … ===` banners)."""
        console.print(
            f"  {label:<38}{result}",
            tui.COLOR_INFO if ok else tui.COLOR_WARNING)

    def cmd_audit(self, *args):
        """Single repo audit covering every install-correctness +
        content-integrity + policy-residue gate in repo/.  P3 absorbed
        the former `source verify` and `repo audit_nmu` commands —
        this is now the one-stop pre-ship gate.

        Five sections, in this order:

          DEP GATE (hard) — every pkg in repo/main MUST have its hard
            Depends/Pre-Depends satisfiable in repo/main.  Per-cohort
            (deb / udeb) when dep_tree is built; whole-repo fallback
            otherwise.  Resolution honours Provides per Debian Policy
            §7.5.

          LIVE CONFLICTS (hard) — within the live chroot install set
            (dep_tree.selected_pkgs − pool_extras − installer_exclusive),
            no two pkgs may co-install with mutual Conflicts/Breaks.
            Self-conflict-via-Provides is filtered.

          INSTALLER CONFLICTS (hard) — same shape, cohort is the d-i
            ramdisk udebs (udeb_dep_tree.selected_pkgs).

          STALE FILES (soft) — orphan-source / version-drift residue
            under repo/.  No deletion (that's `repo repair cleanup`).

          CONTENT INTEGRITY (hard) — per-cohort: every .deb/.udeb's
            internal Package/Version/Architecture must match the
            filename, AND its Depends must resolve against the
            corresponding repo state.  Was the standalone `source
            verify`; absorbed here per the P3 dedup.  Slow (~30s);
            skipped with `quick` flag.

          NMU RESIDUE (hard) — every .deb in repo/ must be at the
            pristine source version with stripped dep constraints
            (no +bN / +debNuN / ~bpoN+N).  Was the standalone
            `repo audit_nmu`; absorbed here.

        Conflicts outside a cohort (e.g. grub-pc in pool conflicting
        with grub-efi-amd64 in live) are NOT flagged — apt arbitrates.

        Per-target drill-in: pass a target package name as the first
        non-flag argument.  Shows full state for that target (cache +
        dep_tree + repo/main + Provides + consumers).  Drill-in skips
        the section walks.

        Gap classification: when the dep gate finds any unresolved
        target, each is classified as build_failed / missed_by_parse /
        transitional / other (formerly `audit_gap`).

        Usage:
          repo audit                — full overview (all sections)
          repo audit quick          — skip CONTENT INTEGRITY (~30s)
          repo audit <target>       — drill into one target
          repo audit verbose        — full lists, all categories
          repo audit strict         — also list Recommends
          repo audit refresh        — force re-scan of repo state
        """
        _verbose = 'verbose' in args
        _strict = 'strict' in args
        _refresh = 'refresh' in args
        _quick = 'quick' in args
        # Anything other than known flags is treated as a drill-in target.
        _flags = {'verbose', 'strict', 'refresh', 'quick'}
        _drill_target = next(
            (a for a in args if a not in _flags), None,
        )
        _state = repo_audit.scan_repo_state(
            self.config, subdir='main', refresh=_refresh,
        )
        if _state is None:
            return
        if not _state.packages:
            console.print(
                "repo/main has no .deb/.udeb files — nothing to audit"
            )
            return

        _deb_cohort = self._resolve_deb_cohort()
        _udeb_cohort = self._resolve_udeb_cohort()
        if _deb_cohort is None and _udeb_cohort is None:
            console.print(
                "Note: dep_tree not built — falling back to repo/main-"
                "wide dep gate.  Run `cache parse` first to scope by cohort."
            )
            _unresolved, _weak = repo_audit.audit_dep_closure(
                _state, consumer_set=None,
            )
        else:
            # Run each cohort independently.  Resolution scope is the
            # whole repo in both passes (Option B): udebs with deb deps
            # — at-spi2-core-udeb, libgtk-4-1-udeb, ppp-udeb, grub-
            # installer (~9 upstream metadata cases) — resolve via
            # debs.  Matches d-i runtime, where the deb gets debootstrapped
            # onto /target rather than into the installer ramdisk.
            _unresolved = []
            _weak = []
            _per_cohort = []
            for _label, _consumer_set in (('deb', _deb_cohort),
                                          ('udeb', _udeb_cohort)):
                if _consumer_set is None:
                    continue
                _u, _w = repo_audit.audit_dep_closure(
                    _state, consumer_set=_consumer_set,
                )
                _per_cohort.append((_label, _consumer_set, _u, _w))
                _unresolved.extend(_u)
                _weak.extend(_w)

        # Drill-in mode short-circuits the overview/cohort sections —
        # pass the merged unresolved list (gap classification is cohort-
        # agnostic).
        if _drill_target:
            self._audit_gap_drill_in(_state, _unresolved, _drill_target)
            return

        _live = self._resolve_live_cohort()
        _installer = self._resolve_installer_cohort()

        console.print(f"\nrepo audit  arch={self.config.arch}\n")
        _row = self._audit_row

        if _deb_cohort is None and _udeb_cohort is None:
            # Whole-repo fallback path.
            _row(f"dep gate (whole repo, {len(_state.packages)} pkgs)",
                 f"unresolved: {len(_unresolved)}"
                 + (f"  weak: {len(_weak)}" if _strict else ''),
                 ok=not _unresolved)
            self._report_unresolved(_unresolved, _weak, _state,
                                    verbose=_verbose, strict=_strict)
        else:
            for _label, _consumer_set, _u, _w in _per_cohort:
                _row(f"dep gate ({_label}, {len(_consumer_set)} consumers)",
                     f"unresolved: {len(_u)}"
                     + (f"  weak: {len(_w)}" if _strict else ''),
                     ok=not _u)
                self._report_unresolved(_u, _w, _state,
                                        verbose=_verbose, strict=_strict)

        if _live is None:
            _row("live conflicts", "skipped — run `cache parse`", ok=False)
        else:
            _live_conflicts = _dedupe_bidirectional_conflicts(
                repo_audit.audit_conflict_cohort(_state, _live)
            )
            _row(f"live conflicts ({len(_live)} pkgs)",
                 f"conflicts: {len(_live_conflicts)}",
                 ok=not _live_conflicts)
            self._report_conflicts(_live_conflicts, verbose=_verbose)

        if _installer is None:
            _row("installer conflicts", "skipped — run `cache parse`",
                 ok=False)
        else:
            _inst_conflicts = _dedupe_bidirectional_conflicts(
                repo_audit.audit_conflict_cohort(_state, _installer)
            )
            _row(f"installer conflicts ({len(_installer)} udebs)",
                 f"conflicts: {len(_inst_conflicts)}",
                 ok=not _inst_conflicts)
            self._report_conflicts(_inst_conflicts, verbose=_verbose)

        # Soft-warn section.  Doesn't gate the audit — these aren't broken
        # constraints, they're "shouldn't be in the pool" residue.  Mirrors
        # the categorisation `repo repair cleanup` uses, without the deletion
        # half.  Surfaces the silent-drift scenarios that DID bite us —
        # apt picks the highest version per name and the lower one becomes
        # a phantom, so dep-resolution looks fine right up until install
        # time (when dpkg refuses two .debs of the same name in the pool)
        # or chroot build (where the older one might be picked, depending
        # on order).
        if self.flags.dep_check_ready:
            self._report_stale_files_warning(verbose=_verbose)
        else:
            _row("stale files", "skipped — run `cache parse` first", ok=False)

        # ── CONTENT INTEGRITY (was `source verify`) ─────────────────
        # Per-cohort deep walk: open each predicted .deb/.udeb with
        # DebFile, verify internal Package/Version/Arch matches the
        # filename + every hard Depends resolves against the cohort's
        # RepoState.  Slow (~30-40s on ~900 sources) — skip via `quick`.
        if _quick:
            _row("content integrity", "skipped (`quick` flag)", ok=True)
        elif not self.flags.build_container_ready:
            _row("content integrity",
                 "skipped — run `container local init`", ok=False)
        else:
            self._report_content_integrity(_state, verbose=_verbose,
                                            refresh=_refresh)

        # ── NMU RESIDUE (was `repo audit_nmu`) ──────────────────────
        # Sweeps `state.packages` for any .deb whose Version or dep
        # constraint versions still carry +bN / +debNuN / ~bpoN+N
        # suffixes.  Fresh builds get stripped post-`dpkg-buildpackage`
        # by BuildContainer; anything reported here slipped past the
        # normaliser (manually staged ingest, etc.).
        self._report_nmu_residue(_state, verbose=_verbose)

    def _report_content_integrity(self, deb_state, *, verbose: bool,
                                    refresh: bool = False) -> None:
        """Per-cohort content-integrity scan — absorbed from the former
        `cmd_source_verify`.  Resolution
        scope is the cohort's RepoState (deb→main, udeb→main-udeb)
        rather than the cache — see `verify_pkg_artifact`'s docstring
        for why (NMU-vs-pristine version skew).
        """
        _udeb_state = None
        if (self.udeb_dep_tree is not None
                and self.udeb_dep_tree.selected_srcs):
            _udeb_state = repo_audit.scan_repo_state(
                self.config, 'main-udeb', refresh=refresh,
            )

        # Caller has gated on build_container_ready before entry, so
        # self.container is non-None.  The two trees are typed Optional
        # on BuildSession; we only append cohorts where the tree exists
        # and has work, so the loop's `_tree` is non-None too.
        assert self.container is not None
        for _label, _tree, _state, _ext in (
            ('deb',  self.dep_tree,      deb_state,   '.deb'),
            ('udeb', self.udeb_dep_tree, _udeb_state, '.udeb'),
        ):
            if _tree is None or not _tree.selected_srcs:
                continue
            if _state is None:
                self._audit_row(f"content integrity ({_label})",
                                "skipped — repo state scan failed", ok=False)
                continue
            _ok = 0
            _failed: 'list[tuple]' = []   # (src, binary, diag)
            _skipped_tunneled = 0
            _skipped_missing = 0
            _no_pkgs = 0
            _bar = ProgressBar(
                label=f'Integrity {_label}',
                maxvalue=max(1, len(_tree.selected_srcs)),
                show_rate=False,
            )
            try:
                for _name, _src in sorted(_tree.selected_srcs.items()):
                    _bar.step(1)
                    _expected = [
                        _f for _f in (_tree.src_pkg_files.get(_name) or [])
                        if _f.endswith(_ext)
                    ]
                    if not _expected:
                        _no_pkgs += 1
                        continue
                    # tunneled sources are pristine upstream
                    # passthrough copies — skip deep-verify (different
                    # version semantics than our built binaries).
                    _record = utils.read_build_record(
                        self.container.buildlog_path, _name)
                    if (_record is not None
                            and utils.classify_build_record(_record) == 'tunneled'):
                        _skipped_tunneled += 1
                        continue
                    _any_missing = False
                    _failing = None
                    # Component from the source's origin mirror — without
                    # this, non-main packages (contrib / non-free /
                    # non-free-firmware) would be looked up under main/
                    # and silently miscounted as missing.  Mirrors
                    # check_build (buildcontainer.py:1537) and
                    # _source_state.
                    _src_comp = getattr(
                        getattr(_src, '_mirror', None), 'component', '') or 'main'
                    for _f in _expected:
                        # The predicted filename `_f` is the PRISTINE name; the
                        # on-disk artifact may carry a +asg<R>u<N> stamp.
                        # find_matching_artifact accepts either, and we verify
                        # the ACTUAL file under its ACTUAL name so the internal
                        # control-version-vs-filename check matches.
                        _path = utils.find_matching_artifact(
                            self.config.deb_dest_for_filename(_f, _src_comp), _f)
                        if _path is None:
                            _any_missing = True
                            break
                        _actual = os.path.basename(_path)
                        _ok_v, _reason = self.container.verify_pkg_artifact(
                            _path, _actual, repo_state=_state,
                        )
                        if not _ok_v:
                            _failing = (_actual, _reason)
                            break
                    if _any_missing:
                        _skipped_missing += 1
                        continue
                    if _failing is None:
                        _ok += 1
                    else:
                        _failed.append((_name,) + _failing)
                        logger.info(
                            f"repo audit integrity {_label} {_name}: "
                            f"{_failing[0]}: {_failing[1]}"
                        )
            finally:
                _bar.close()

            _parts = [f"{_ok} pass", f"{len(_failed)} fail"]
            if _skipped_tunneled:
                _parts.append(f"{_skipped_tunneled} tunneled")
            if _skipped_missing:
                _parts.append(f"{_skipped_missing} missing")
            if _no_pkgs:
                _parts.append(f"{_no_pkgs} none-declared")
            self._audit_row(f"content integrity ({_label})",
                            ", ".join(_parts), ok=not _failed)
            if _failed:
                from collections import Counter, defaultdict
                _types: 'Counter[str]' = Counter()
                _by_type: 'dict[str, list[str]]' = defaultdict(list)
                for _src_name, _f, _diag in _failed:
                    _prefix = (_diag.split(':', 1)[0]
                               if ':' in _diag else _diag)
                    _types[_prefix] += 1
                    _by_type[_prefix].append(_src_name)
                console.print("")
                console.print(f"  Failure types ({_label}):")
                for _k, _v in _types.most_common():
                    console.print(f"    {_v:5d}  {_k}")

                # Concise list of failing source names, grouped by
                # failure type.  Wrapped to terminal-friendly width.
                console.print("")
                console.print(f"  Failing sources ({_label}):")
                for _prefix, _names in _by_type.items():
                    self._print_wrapped_names(
                        f"    {_prefix} ({len(_names)})",
                        sorted(set(_names)),
                    )

            if verbose and _failed:
                console.print("")
                console.print(
                    f"  Per-binary detail ({_label}, {len(_failed)}):"
                )
                for _src_name, _f, _diag in _failed:
                    console.print(f"    {_src_name}: {_f}: {_diag}")

    def _report_nmu_residue(self, state, *, verbose: bool) -> None:
        """NMU-suffix residue check — absorbed from the former
        `cmd_audit_nmu`.  Catches any .deb
        in repo/ that bypassed BuildContainer's post-build stripper.

        Tunneled packages are EXCLUDED — they're pristine Debian binary
        passthrough and MUST keep their upstream ~debNuN / +debNuN suffix
        (shim-signed et al. carry Microsoft Secure Boot signatures that
        any rewrite would invalidate)."""
        _tun = set(self.config.tunnel_packages)
        _findings = repo_audit.audit_nmu_residue(state, tunnel_sources=_tun)
        # Count how many binaries we skipped because their source is tunneled
        # — surface it so the operator knows what audit chose not to scan.
        _skipped = 0
        if _tun:
            for _pkg, _entry in state.packages.items():
                _src_field = (_entry.get('Source') or _pkg).strip()
                _src_name = _src_field.split(' ', 1)[0]
                if _src_name in _tun:
                    _skipped += 1
        _scanned = len(state.packages) - _skipped
        _tail = (f" ({_skipped} tunneled binary/binaries skipped — "
                 f"pristine passthrough)" if _skipped else "")
        if not _findings:
            self._audit_row(
                f"nmu residue ({_scanned} pkgs)",
                f"clean — no +bN / +debNuN / ~bpoN+N residue{_tail}")
            return
        from collections import Counter
        _by_field = Counter(f[1] for f in _findings)
        _pkgs_with_residue = sorted({f[0] for f in _findings})
        self._audit_row(
            f"nmu residue ({_scanned} pkgs)",
            f"{len(_findings)} finding(s) across "
            f"{len(_pkgs_with_residue)} pkg(s){_tail}",
            ok=False)
        for _field, _count in _by_field.most_common():
            console.print(f"    {_count:5d}  {_field}")
        # Concise: list pkg NAMES wrapped to terminal width.  Verbose:
        # full per-finding detail (pkg + field + raw value + why).
        console.print("")
        self._print_wrapped_names(
            f"  Affected pkgs ({len(_pkgs_with_residue)})",
            _pkgs_with_residue,
        )
        if verbose:
            console.print("")
            console.print(f"  Per-finding detail ({len(_findings)}):")
            for _pkg, _field, _val, _why in _findings:
                console.print(
                    f"    {_pkg:30s}  {_field:14s}  {_val}  — {_why}"
                )
        console.print(
            "  Fix: `repo repair strip` re-applies the strip to every "
            ".deb in repo/ (tunneled packages auto-skipped — pristine "
            "passthrough must keep its upstream suffix).",
            tui.COLOR_INFO,
        )

    def _report_stale_files_warning(self, *, verbose: bool) -> None:
        """Soft-warning STALE FILES section for package audit.

        Lists counts (and a short preview) of orphan-source and
        version-drift residue under repo/.  Doesn't delete — the
        operator runs `repo repair cleanup` when they want to act.
        """
        (_orphan, _drift, _foreign, _malformed,
         _total) = self._scan_stale_files()
        # Orphaned `.verified` sidecars (binary gone, sha-cache left behind).
        # Non-gating — harmless cruft, NOT a stale-artifact gate risk — but
        # surfaced so the operator knows `repo repair cleanup` has work.
        _sidecars = self._scan_orphaned_sidecars()
        if _sidecars:
            self._audit_row(
                "orphan sidecars",
                f"{len(_sidecars)} `.verified` with no .deb/.udeb — "
                f"`repo repair cleanup` sweeps them", ok=True)
        _n_stale = len(_orphan) + len(_drift) + len(_foreign)
        if _n_stale == 0 and not _malformed:
            self._audit_row(f"stale files ({_total} files)",
                            "clean — no orphan-source or drift residue")
            return
        _bytes = (sum(s for *_, s in _orphan)
                  + sum(s for *_, s in _drift)
                  + sum(s for *_, s in _foreign))
        _mal = f", {len(_malformed)} malformed" if _malformed else ""
        _frn = f", {len(_foreign)} foreign-cross" if _foreign else ""
        self._audit_row(
            f"stale files ({_total} files)",
            f"{_n_stale} stale ({_bytes / 1024 / 1024:.1f} MB) — "
            f"{len(_orphan)} orphan-source, {len(_drift)} version-drift"
            f"{_frn}{_mal}",
            ok=(_n_stale == 0))
        if _n_stale:
            # Short preview — one line per source for orphans (collapses
            # the task-* family case), individual lines for drift.  Full
            # detail lives in `repo repair cleanup` (dry-run).
            _show = 5 if not verbose else max(len(_orphan), len(_drift))
            if _orphan:
                from collections import defaultdict
                _by_src: 'dict[str, int]' = defaultdict(int)
                for _, _, _src, _ in _orphan:
                    _by_src[_src] += 1
                _src_top = sorted(_by_src.items(), key=lambda kv: -kv[1])
                _slice = _src_top if verbose else _src_top[:_show]
                console.print(f"  First {len(_slice)} orphan source(s):")
                for _src, _cnt in _slice:
                    console.print(f"    {_src:30s} → {_cnt} file(s)")
                if len(_src_top) > _show and not verbose:
                    console.print(
                        f"    … (+{len(_src_top) - _show} more; "
                        f"pass `verbose` for full list)"
                    )
            if _drift:
                _drift_slice = _drift if verbose else _drift[:_show]
                console.print(f"  First {len(_drift_slice)} drift file(s):")
                for _sub, _f, _src, _ in _drift_slice:
                    console.print(f"    {_sub}/{_f} (source: {_src})")
                if len(_drift) > _show and not verbose:
                    console.print(
                        f"    … (+{len(_drift) - _show} more; "
                        f"pass `verbose` for full list)"
                    )
            if _foreign:
                _frn_slice = _foreign if verbose else _foreign[:_show]
                console.print(
                    f"  First {len(_frn_slice)} foreign cross-toolchain "
                    f"file(s):")
                for _sub, _f, _src, _ in _frn_slice:
                    console.print(f"    {_sub}/{_f} (source: {_src})")
                if len(_foreign) > _show and not verbose:
                    console.print(
                        f"    … (+{len(_foreign) - _show} more; "
                        f"pass `verbose` for full list)"
                    )
            console.print(
                "  Run `repo repair cleanup` to review/remove (dry-run by "
                "default).",
                tui.COLOR_INFO,
            )

    def _report_unresolved(self, unresolved, weak, state, *,
                            verbose: bool, strict: bool):
        """Detailed report for the dep gate.  Includes gap classification
        when dep_tree + cache are available (formerly `audit_gap`)."""
        _show = len(unresolved) if verbose else min(30, len(unresolved))
        if _show:
            console.print(f"  first {_show}:")
            for _pkg, _field, _rel, _why in unresolved[:_show]:
                console.print(f"    {_pkg}  {_field}: {_rel}  — {_why}")
        if strict:
            _show = len(weak) if verbose else min(30, len(weak))
            if _show:
                console.print(f"  weak Recommends ({_show}):")
                for _pkg, _field, _rel in weak[:_show]:
                    console.print(f"    {_pkg}  {_field}: {_rel}")

        if not unresolved:
            return

        # Classify each missing target via gap analysis (formerly the
        # standalone `audit_gap` command).  Requires dep_tree + cache;
        # falls back to the simple grouped-by-target tally otherwise.
        if (self.dep_tree and self.dep_tree.selected_pkgs
                and self.cache and getattr(self.cache, 'package_hashtable', None)):
            self._report_gap_classification(unresolved, state, verbose=verbose)
        elif not verbose:
            from collections import Counter
            _missing: 'Counter[str]' = Counter()
            for _pkg, _field, _rel, _why in unresolved:
                _first = _rel.split(' ', 1)[0].split(':', 1)[0]
                if _first:
                    _missing[_first] += 1
            console.print(
                f"  Unresolved grouped by missing target "
                f"({len(_missing)} distinct):"
            )
            for _target, _count in _missing.most_common(20):
                console.print(f"    {_count:5d}  → {_target}")

    def _report_conflicts(self, conflicts, *, verbose: bool):
        """Detailed report for a conflict-cohort result."""
        _show = len(conflicts) if verbose else min(30, len(conflicts))
        if _show:
            console.print(f"  first {_show}:")
            for _pkg, _field, _other, _rel in conflicts[:_show]:
                console.print(
                    f"    {_pkg}  {_field}: {_rel}  → {_other}"
                )

    def _report_gap_classification(self, unresolved, state, *,
                                     verbose: bool) -> None:
        """Classify each missing target into one of four buckets:
          build_failed     — in dep_tree, not in repo/main (source
                             build dropped it or skipped)
          missed_by_parse  — known to upstream cache (real or virtual)
                             but NOT in dep_tree (parse didn't reach it)
          transitional     — not in upstream cache (renamed/removed)
          other            — in both dep_tree AND repo, but constraint
                             didn't satisfy (version skew)

        cache.package_hashtable folds real pkgs and Provides under one
        namespace (see cache.py:520-525), so a single membership check
        covers both.  Repo virtual coverage uses state.provides_index.
        """
        assert self.dep_tree is not None and self.cache is not None
        _in_dep_tree = set(self.dep_tree.selected_pkgs.keys())
        _in_repo = set(state.packages.keys())
        _in_repo_virtual = set(state.provides_index.keys())
        _in_repo_either = _in_repo | _in_repo_virtual
        _in_upstream = set(self.cache.package_hashtable.keys())

        _consumers_by_target: 'dict[str, list]' = {}
        for _consumer, _field, _rel_str, _why in unresolved:
            _first = _rel_str.split(' ', 1)[0].split(':', 1)[0]
            if _first:
                _consumers_by_target.setdefault(_first, []).append(_consumer)

        _build_failed: 'list[str]' = []
        _missed_by_parse: 'list[str]' = []
        _transitional: 'list[str]' = []
        _other: 'list[str]' = []

        for _target in _consumers_by_target.keys():
            _in_dt = _target in _in_dep_tree
            _in_r = _target in _in_repo_either
            _in_up = _target in _in_upstream
            if _in_r and _in_dt:
                _other.append(_target)
            elif _in_dt and not _in_r:
                _build_failed.append(_target)
            elif _in_up and not _in_dt:
                _missed_by_parse.append(_target)
            elif not _in_up:
                _transitional.append(_target)
            else:
                _other.append(_target)

        def _ref_count(lst):
            return sum(len(_consumers_by_target[_t]) for _t in lst)

        _buckets = [
            ('build_failed',    'not in repo',         _build_failed),
            ('missed_by_parse', 'not in dep_tree',     _missed_by_parse),
            ('transitional',    'not in upstream',     _transitional),
            ('other',           'version-skew',        _other),
        ]
        _nonzero = [(_n, _t, _l) for _n, _t, _l in _buckets if _l]
        console.print(
            f"\nGap: {len(_consumers_by_target)} target(s), "
            f"{len(unresolved)} ref(s)"
        )
        for _name, _tag, _lst in _nonzero:
            console.print(
                f"  {_name:<16} {len(_lst):4d}  "
                f"({_ref_count(_lst)} refs, {_tag})"
            )

        def _show_category(name: str, targets: list, n: int = 30):
            if not targets:
                return
            _ranked = sorted(
                targets,
                key=lambda t: (-len(_consumers_by_target[t]), t),
            )
            _limit = len(_ranked) if verbose else min(n, len(_ranked))
            console.print(f"\n  {name}:")
            for _t in _ranked[:_limit]:
                _consumers = _consumers_by_target[_t]
                _sample = ', '.join(sorted(set(_consumers))[:3])
                if len(set(_consumers)) > 3:
                    _sample += f', … (+{len(set(_consumers)) - 3})'
                console.print(
                    f"    {len(_consumers):4d}× {_t:42s} ← {_sample}"
                )

        for _name, _tag, _lst in _nonzero:
            _show_category(_name, _lst)

    def _audit_gap_drill_in(self, state, unresolved, target: str) -> None:
        """Per-target diagnostic for `repo audit_gap <name>`.

        Surfaces enough state to root-cause why `<name>` is unresolved:
          - in upstream cache?  (with versions if so)
          - in our dep_tree.selected_pkgs?
          - in our repo state? (with version)
          - virtually provided in repo? (with provider + version)
          - which consumers reference it (with exact constraint)
        """
        console.print(f"\ngap drill-in  target={target}\n")
        if self.cache is None or self.dep_tree is None:
            console.print(
                "  cache or dep_tree not built — run `build_cache` and "
                "`cache parse` first for full drill-in"
            )
            return

        # Upstream cache state
        _up_versions = sorted(
            self.cache.package_hashtable.get(target, {}).keys(),
            key=lambda v: str(v),
        )
        if _up_versions:
            _samples = ', '.join(str(v) for v in _up_versions[:5])
            _suffix = f' (+{len(_up_versions) - 5} more)' if len(_up_versions) > 5 else ''
            console.print(
                f"  upstream cache : YES — versions: {_samples}{_suffix}"
            )
        else:
            console.print("  upstream cache : NO — name not in cache")

        # dep_tree state
        _dt_pkg = self.dep_tree.selected_pkgs.get(target)
        if _dt_pkg is not None:
            _dt_canon = _dt_pkg.get('Package', target)
            console.print(
                f"  dep_tree       : YES — canonical name "
                f"{_dt_canon!r}, Version "
                f"{_dt_pkg.get('Version', '<none>')!r}"
            )
        else:
            console.print("  dep_tree       : NO")

        # Repo state — real
        _repo_entry = state.packages.get(target)
        if _repo_entry is not None:
            console.print(
                f"  repo (real)    : YES at version "
                f"{_repo_entry.get('Version', '<none>')!r}"
            )
        else:
            console.print("  repo (real)    : NO")

        # Repo state — virtual via Provides
        _providers = state.provides_index.get(target, [])
        if _providers:
            console.print("  repo (virtual) : YES — provided by:")
            for _p, _ver in _providers[:5]:
                _provider_repo_ver = (
                    state.packages.get(_p, {}).get('Version', '<missing>')
                )
                console.print(
                    f"    {_p:35s} Provides {target}"
                    f"{(' (= ' + _ver + ')') if _ver else ' (unversioned)'}"
                    f"  [provider at {_provider_repo_ver}]"
                )
            if len(_providers) > 5:
                console.print(f"    … +{len(_providers) - 5} more")
        else:
            console.print("  repo (virtual) : no providers")

        # Consumer constraints
        _consumers = [
            (c, f, r) for (c, f, r, _) in unresolved
            if r.split(' ', 1)[0].split(':', 1)[0] == target
        ]
        if _consumers:
            console.print(
                f"  consumers      : {len(_consumers)} unresolved ref(s):"
            )
            _show = min(15, len(_consumers))
            for _c, _f, _r in _consumers[:_show]:
                console.print(f"    {_c:30s} {_f}: {_r}")
            if len(_consumers) > _show:
                console.print(f"    … +{len(_consumers) - _show} more")
        else:
            console.print(
                f"  consumers      : (none) — `{target}` is not in any "
                f"unresolved dep; either drill-in is for the wrong name "
                f"or audit hasn't surfaced it"
            )
