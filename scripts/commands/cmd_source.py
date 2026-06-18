"""Source build, fork management and the bump update-plan — the `source`
command surface.

Builds source packages through the container (cmd_source_build /
_build_one_source), classifies per-source state (_source_state /
_patchhash_status), audits and repairs the source tree, manages fork
packages, and owns the bump UPDATE PLAN: _do_update_build /
_needs_bump_build / _workload_* / _update_build_pending /
_preflight_stamp_invariant decide which sources need a +asg rebuild.

The pure version math lives in bump.py; this module is the orchestration
that drives it (which sources, against which snapshot/ledger).  Extracted
verbatim from build.py's BuildSession; see commands/base.py for how the
mixin shares session state.
"""
import logging
import os
import re
import subprocess

import apt_pkg
import buildcontainer
import repo_audit
import tui
import utils
from tui import console, Prompt, PROMPT_YESNO, ProgressBar

from commands.base import SessionState

logger = logging.getLogger('athena.build')


class SourceCommandsMixin(SessionState):
    # Subset selectors recognised by `source build` — pkg / live /
    # installer / recommended / all are mutually exclusive; named pkgs
    # are a sixth (also exclusive) mode.  'pkg' is the default when no
    # subset and no names are given (Phase 4 — used to be 'live' pre-pivot).
    # 'pkg' = pkg.list closure only; 'live' = live extras only; 'installer'
    # = udeb closure + installer.list deb-arm extras; 'recommended' = extras
    # pulled by depth-1 Recommends; 'all' = union of every selected source
    # in dep_tree + udeb_dep_tree (no exclusions — equivalent to running
    # pkg + live + installer + recommended back-to-back, deduped).
    # 'build' is recognised by the arg parser; gated
    # to [Build] Mode = build at dispatch time (cmd_source_build
    # rejects it under dist mode with a hint pointing at 'all').
    _SOURCE_SUBSETS = ('pkg', 'live', 'installer', 'recommended', 'all',
                       'indl')

    @staticmethod
    def _parse_source_build_args(args):
        """Pure-function argument parser for cmd_source_build.

        Recognises:
          - 'force' as a case-insensitive flag-word at any position
          - 'pkg' / 'live' / 'installer' / 'recommended' / 'all' as
            case-insensitive subset selectors at any position; mutually
            exclusive with each other AND with named packages
          - one optional `[profile,...]` bracket-token (override for both
            DEB_BUILD_PROFILES and DEB_BUILD_OPTIONS); multiple bracket
            tokens is a parse error
          - everything else as a package name

        Default: bare `source build` (no subset, no names) resolves to
        subset='pkg'.

        Returns ``(err, force, subset, names, profile_override)``.
        On success ``err`` is None; on parse error ``err`` is a printable
        string the caller should surface.  ``subset`` is one of
        'pkg' / 'live' / 'installer' / 'recommended' / 'all' when a
        subset selector was given (or no args at all); '' when named
        packages were given.
        ``profile_override`` is None when no bracket-token was given; an
        empty list when the operator wrote `[]` (most-permissive build);
        a populated list otherwise.
        """
        _bracket_token = None
        _other_args = []
        for _a in args:
            _s = _a.strip()
            if _s.startswith('[') and _s.endswith(']'):
                if _bracket_token is not None:
                    return (
                        f"Usage: only one [profiles] override per invocation "
                        f"(saw {_bracket_token!r} and {_s[1:-1]!r})",
                        False, '', [], None,
                    )
                _bracket_token = _s[1:-1]
            else:
                _other_args.append(_a)
        _flags = {a.strip().lower() for a in _other_args}
        _force = 'force' in _flags
        _subset_words = sorted(_flags & set(SourceCommandsMixin._SOURCE_SUBSETS))
        if len(_subset_words) > 1:
            return (
                f"Usage: pick at most one of "
                f"{'/'.join(SourceCommandsMixin._SOURCE_SUBSETS)} "
                f"(saw {', '.join(_subset_words)})",
                False, '', [], None,
            )
        _subset = _subset_words[0] if _subset_words else ''
        _reserved = {'force'} | set(SourceCommandsMixin._SOURCE_SUBSETS)
        _names = [a for a in _other_args
                  if a.strip().lower() not in _reserved]
        if _subset and _names:
            return (
                f"Usage: 'source build {_subset}' is mutually exclusive with "
                f"named packages.  Use one or the other.",
                False, '', [], None,
            )
        # Default: bare `source build` resolves to pkg (pkg-layer
        # only); operator runs explicit 'source build live' for live
        # extras and 'source build installer' for the installer udeb
        # closure.  autorun chains pkg + live for the live ISO workflow.
        if not _subset and not _names:
            _subset = 'pkg'
        _profile_override = None
        if _bracket_token is not None:
            _profile_override = [
                p.strip() for p in _bracket_token.split(',') if p.strip()
            ]
        return (None, _force, _subset, _names, _profile_override)

    # ─────────────────────────────────────────────────────────────────
    # Source state classifier — shared by cmd_source_audit (reports)
    # and cmd_source_repair (acts).  Pure read-only: walks disk only,
    # never mutates .result / .patchhash / source files.
    # ─────────────────────────────────────────────────────────────────

    def _patchhash_status(self, pkg: str, src) -> str:
        """Pure-read patch-baseline status for one source.  Returns:

          'matches'  — stored patch_set_hash equals current on-disk
                       patch set.  Strongest signal: binaries produced
                       under this baseline are provably current.

          'differs'  — stored hash disagrees.  Patches on disk have
                       changed since the build wrote its record.

          'absent'   — no signed record, or the record's
                       patch_set_hash field is empty (legitimate when
                       a source declares no patches AND was built
                       before any patches landed).  Caller decides.
        """
        _buildlog = os.path.join(self.config.dir_log, 'build')
        _record = utils.read_build_record(_buildlog, pkg)
        if _record is None:
            return 'absent'
        _stored = str(_record.get('patch_set_hash') or '')
        if not _stored:
            return 'absent'
        _patch_dir = os.path.join(
            self.config.dir_patch_source, pkg,
            utils.version_no_epoch(src.version),
        )
        _current = utils.patch_set_hash(_patch_dir, src.patch_list or [])
        return 'matches' if _stored == _current else 'differs'

    _SOURCE_STATES = (
        'ok',             # binaries present + valid, record=done/PASS,
                          # patches unchanged
        'no_pkgs',        # source declares no main-tier binaries
        'tunneled',       # record=tunneled (third-party pull, not built)
        'fail',           # record=failed — explicit prior-build failure
                          # marker.  Repair LEAVES ALONE (operator
                          # signal: "this is known broken; don't
                          # second-guess").
        'needs_sync',     # source files missing or .verified absent
        'needs_build',    # binaries missing or invalid (legitimate rebuild)
        'interrupted',    # record has a non-terminal phase — build
                          # process was killed mid-flight.  Repair
                          # CLEARS the record so next build re-runs and
                          # produces a proper terminal phase.
        'stale_pass',     # record=PASS but state has drifted (binaries
                          # gone OR patches changed) — repair would CLEAR
    )

    def _source_state(self, pkg: str, src) -> str:
        """Classify one source's current state.  Returns one of
        `_SOURCE_STATES`.  Pure-read.

        Two axes of evidence feed the classification:

          A. signed build.json record — classified by
             utils.classify_build_record into 'missing' / 'interrupted'
             / 'ok' / 'fail' / 'tunneled'.
          B. patch_set_hash drift — does the record's stored hash
             match the current on-disk patch set?

        Plus one filesystem check: do predicted binaries exist + open
        as valid ar archives?

        Resolution table (binaries valid case):

          | record      | patch hash | state       |
          |-------------|------------|-------------|
          | ok          | matches    | ok          |
          | ok          | differs    | stale_pass  ← patches drifted
          | ok          | absent     | ok           (no patches declared)
          | tunneled    | any        | tunneled
          | missing     | any        | needs_build ← no record = rebuild
          | fail        | any        | fail         (operator marker; no-op)
          | interrupted | any        | interrupted  (rebuild required)

        Missing record always routes to 'needs_build' — without a
        record we have no proof the on-disk binaries match the current
        patches, so a rebuild is the only safe action.

        Resolution order (short-circuits):
          1. no predicted binaries → 'no_pkgs'
          2. record=fail → 'fail'  (operator marker, leave alone)
          3. any source file missing on disk OR .verified absent → 'needs_sync'
          4. record=interrupted → 'interrupted'
          5. predicted-binaries disk check — `find_matching_artifact` +
             `is_ar_file` (the SAME accept-pristine-or-+asg gate
             check_build uses, so audit and build agree).  Any binary
             missing or not-ar →
                'stale_pass'   if record=ok or record=tunneled
                'needs_build'  otherwise (missing record)
          6. binaries valid → classify via the table above.

        Step 5 is critical for record=tunneled: a tunneled record claims
        binaries are present, but if `_do_tunnel` was interrupted or a
        post-tunnel run wiped the pool, the binaries can be gone or in
        a wrong (upstream-suffixed, non-normalised) shape.  Without the
        disk check audit would silently green-light the package while
        `source build` (using the same `find_matching_artifact` gate)
        re-attempts the tunnel.  Routing to 'stale_pass' surfaces it
        and `source repair` clears the record for re-tunnel.
        """
        _expected = self._predicted_files_for_source(pkg)
        if not _expected:
            return 'no_pkgs'
        _buildlog = os.path.join(self.config.dir_log, 'build')

        # the signed build.json record is the sole source of
        # truth.  classify_build_record returns:
        #   'missing'     — file absent / tampered / corrupt
        #   'interrupted' — phase != done|failed|tunneled (process
        #                   killed mid-build).  Routed to its own state
        #                   below so audit distinguishes from "never
        #                   tried".
        #   'ok' / 'fail' / 'tunneled' — terminal phases.
        _record = utils.read_build_record(_buildlog, pkg)
        _record_state = utils.classify_build_record(_record)
        # 'fail' is an explicit operator marker — leave the on-disk
        # state alone (repair won't touch it; the operator must
        # `source build force <pkg>` to retry).  All other terminal
        # states fall through to the disk-verification path so audit
        # and `source build`'s check_build gate stay in lock-step.
        if _record_state == 'fail':
            return 'fail'
        _interrupted = (_record_state == 'interrupted')
        _record_ok = (_record_state == 'ok')
        _record_tunneled = (_record_state == 'tunneled')

        # (3) source files — every entry in src.files must exist on
        # disk with a .verified sidecar (download_source's gate).
        _sync_ok = True
        for _fname in src.files:
            _path = os.path.join(self.config.dir_source, _fname)
            if not os.path.isfile(_path):
                _sync_ok = False
                break
            if not os.path.isfile(_path + '.verified'):
                _sync_ok = False
                break
        if not _sync_ok:
            return 'needs_sync'

        # a non-terminal phase in the record means the build
        # process was killed mid-flight — segregate, normalize, or
        # something else didn't finish.  On-disk binaries may be
        # pre-strip, partial, or missing.  Surface as its own state
        # so the audit distinguishes "we tried and didn't finish" from
        # "never tried".  Repair handles by clearing the record;
        # subsequent build re-runs and writes a proper terminal phase.
        if _interrupted:
            return 'interrupted'

        # (5) predicted binaries.  `is_ar_file` is a staticmethod so we
        # don't need a live container instance — read-only callers
        # (source audit / source repair) work without `container init`.
        # MUST match check_build's accept criteria (pristine or +asg
        # via find_matching_artifact) so audit and the build's
        # skip-rebuild gate never disagree.  Component comes from the
        # source's origin mirror — without this, a non-free-firmware
        # package (e.g. amd64-microcode) would be looked up under main/
        # binary-arch/ and audit would report 'stale_pass' even though
        # the file sits at non-free-firmware/binary-arch/.  Mirrors
        # check_build's component derivation at buildcontainer.py:1537.
        _comp = getattr(getattr(src, '_mirror', None), 'component', '') or 'main'
        _all_present = True
        for _f in _expected:
            _dst_dir = self.config.deb_dest_for_filename(_f, _comp)
            _match = utils.find_matching_artifact(_dst_dir, _f)
            if _match is None:
                _all_present = False
                break
            if not buildcontainer.BuildContainer.is_ar_file(_match):
                _all_present = False
                break

        if not _all_present:
            # record=ok or record=tunneled with missing binaries: the
            # record claims success but disk disagrees.  Same state in
            # both cases — `source repair` clears the record; next
            # build re-tunnels (record was tunneled) or re-builds
            # (record was ok).
            if _record_ok or _record_tunneled:
                return 'stale_pass'
            return 'needs_build'

        # (6) binaries valid; classify via record × patch_set_hash.
        if _record_tunneled:
            # Tunneled packages don't carry our patch set, so
            # patch_set_hash drift doesn't apply.  Binaries present in
            # normalised form → the record is honest.
            return 'tunneled'
        _patchhash = self._patchhash_status(pkg, src)
        if _record_ok:
            # PASS row.  Matches OR absent (no patches declared) → ok.
            # Differs → stale_pass.
            return 'stale_pass' if _patchhash == 'differs' else 'ok'

        # No record (fail / interrupted handled above).  Without a
        # record we have no proof binaries reflect current patches —
        # rebuild is the only safe action.
        return 'needs_build'


    _AUDIT_HARD_STATES = (
        'needs_build', 'stale_pass', 'interrupted', 'needs_sync')

    def _detect_build_audit_divergence(
            self, packages) -> 'list[tuple[str, str, str]]':
        """Sanity-check that source build and source audit agree about
        which sources are done.  For each source in `packages` (the
        in-scope set the build pass operated on):

          - Query `_source_state` (audit's classifier).
          - Query the build record's classifier.
          - If audit says a hard state (needs_build / stale_pass /
            interrupted / needs_sync) AND the build record does NOT
            say 'fail' (which would be a consistent declared failure),
            emit a (pkg, audit_state, record_state) finding.

        Skip-src packages (`cache.skip_src`) are excluded — they're
        the operator's declared "leave alone" set; audit can flag them
        without it being a divergence.

        The empty list means audit and build are in lock-step.  Any
        non-empty list means an on-disk shape the skip-gate accepts is
        different from what the audit classifier accepts (which is
        exactly the firefox-esr bug class: build's `check_build` saw a
        missing/wrong-shaped file and returned False, while audit's
        `_source_state` early-returned `tunneled` based on the record
        alone).  Surface to `cmd_source_build` as a gate that refuses
        to set `source_build_ready` until investigated.

        Pure read-only — no mutation, safe to invoke from any
        post-build path.
        """
        _buildlog = os.path.join(self.config.dir_log, 'build')
        _findings: 'list[tuple[str, str, str]]' = []
        for _p in packages:
            if (self.cache is not None
                    and _p.package in self.cache.skip_src):
                continue
            try:
                _audit_state = self._source_state(_p.package, _p)
            except Exception as _e:
                logger.warning(
                    f"divergence gate: _source_state({_p.package}) "
                    f"raised {type(_e).__name__}: {_e}")
                continue
            if _audit_state not in self._AUDIT_HARD_STATES:
                continue
            _record = utils.read_build_record(_buildlog, _p.package)
            _record_state = utils.classify_build_record(_record)
            if _record_state == 'fail':
                continue
            _findings.append((_p.package, _audit_state, _record_state))
        return _findings

    def cmd_source_repair(self, *args):
        """Align build records with current source state.  MUTATOR.

        Usage: source repair [verbose]

        Walks every source via `_source_state` and acts on each state:

          stale_pass   — record=done but state drifted (binaries gone
                         OR patches changed)  → drop the record so
                         next `source build` rebuilds.
          interrupted  — non-terminal phase (build killed mid-flight)
                         → drop the record.
          others       — leave alone.  needs_build / needs_sync /
                         tunneled / ok / fail / no_pkgs are not
                         repair's concern; `source audit` reports them.

        `source repair` does NOT trigger a rebuild itself — it only
        adjusts the records so the NEXT `source build` does the right
        thing.  Read-only WRT the container; needs cache + dep tree.
        """
        if not (self.flags.cache_ready and self.flags.dep_check_ready):
            console.print(
                "source repair needs `cache build` + `cache parse` to "
                "have run first.",
                tui.COLOR_ERROR,
            )
            return

        _verbose = 'verbose' in args

        assert self.dep_tree is not None
        _srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _name, _src in self.udeb_dep_tree.selected_srcs.items():
                if _name not in _srcs:
                    _srcs[_name] = _src

        _cleared:  'list[str]' = []
        _other:    'dict[str, int]' = {}

        _bar = ProgressBar(
            label='Source repair', maxvalue=max(1, len(_srcs)),
            show_rate=False,
        )
        try:
            for _name, _src in sorted(_srcs.items()):
                _bar.step(1)
                _state = self._source_state(_name, _src)
                if _state in ('stale_pass', 'interrupted'):
                    # stale_pass: record=done but state has drifted
                    #   (binaries gone OR patches changed).
                    # interrupted: build process was killed mid-flight
                    #   — on-disk artifacts can't be trusted.
                    # Same action: drop the signed record so the next
                    # source build rebuilds.
                    # `source repair` may run before `container
                    # init`, so self.container is None — derive the buildlog
                    # dir from config like the read-only siblings do.
                    _f = os.path.join(
                        self.config.dir_log, 'build',
                        _name + utils.BUILD_RECORD_SUFFIX)
                    try:
                        os.remove(_f)
                        _cleared.append(_name)
                        logger.info(
                            f"source repair: cleared {_state} {_name}")
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        logger.warning(
                            f"source repair: cannot remove {_f}: {e}")
                else:
                    _other[_state] = _other.get(_state, 0) + 1
        finally:
            _bar.close()

        console.print("Source repair:")
        console.print(
            f"  {len(_cleared):5d}  stale/interrupted records cleared "
            "(binaries gone, patches changed, or build was killed mid-flight)"
        )
        for _state in self._SOURCE_STATES:
            if _state == 'stale_pass':
                continue
            _n = _other.get(_state, 0)
            if _n:
                console.print(f"  {_n:5d}  {_state}  (no action)")

        if _verbose and _cleared:
            console.print("")
            console.print(f"Cleared ({len(_cleared)}):")
            for _n in _cleared:
                console.print(f"  {_n}")

    @staticmethod
    def _is_fork_source(src) -> bool:
        """True if `src` came from our LOCAL fork mirror (cache stamps it with
        `_mirror.id == 'fork'`; see scripts/fork_mirror.py).  Forks are not part
        of any upstream snapshot's Sources index, so the snapshot-to-snapshot
        change-detection must skip them — they only change when WE edit a fork
        (a rebase + `package reload`), never when the snapshot advances.  Without
        this they'd hit the "absent from upstream Sources" branch and be flagged
        as changed on every update build, then rebuild to an identical filename
        and get dropped as a dup by `_segregate_built_artifacts`."""
        return getattr(getattr(src, '_mirror', None), 'id', '') == 'fork'

    def _workload_current_to_target(self, target_ts: str):
        """Sources whose upstream SOURCE version at `target_ts` is NEWER than
        the current pin's selected source version — what you'd rebuild
        advancing current → target (UPD-01 `snapshot workload` driving
        `source build all` + `mirror publish`).

        Detection is by FULL source version (epoch-stripped, NMU-INTACT), NOT
        the pristine base: the Sources index IS the source-change ledger, so a
        security/point-release upload (e.g. 3.0.15-1 → 3.0.15-1+deb12u2) — a
        REAL source change — is caught, while a binNMU (+bN, binary-only, never
        present in Sources) is correctly ignored.  (We still STRIP +debNuN and
        publish as +asg<R>u<N>; that's the version we ship, not the change
        signal.)  Metadata-only (fetches the target Sources index; no build).
        Returns (sorted_names, None) on success or (None, error) on fetch fail."""
        _target = repo_audit.fetch_source_versions_at(self.config, target_ts)
        if _target is None:
            return None, (f"could not fetch the target snapshot ({target_ts}) "
                          f"Sources index — check network / the timestamp")
        assert self.dep_tree is not None
        _srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _n, _s in self.udeb_dep_tree.selected_srcs.items():
                _srcs.setdefault(_n, _s)
        _out: 'list[str]' = []
        for _name, _src in _srcs.items():
            if self._is_fork_source(_src):
                continue   # forks are LOCAL, not driven by the upstream snapshot
            _tgt = _target.get(_name)
            if _tgt is None:
                continue
            _cur_v = utils.version_no_epoch(str(_src.version))
            _tgt_v = utils.version_no_epoch(_tgt)
            if apt_pkg.version_compare(_tgt_v, _cur_v) > 0:
                _out.append(_name)
        return sorted(_out), None

    def _workload_since_snapshot(self, from_ts: str):
        """Sources whose CURRENT-pin source version is NEWER than at `from_ts`
        — i.e. what changed between the mirror floor and current.  This is
        the update rebuild set (floor = min(mirror.current across enabled
        mirrors), destination = local snapshot.current).  Full source-
        version compare (catches +debNuN; ignores binNMU).  A source absent
        from `from_ts` (new since) also counts.  Returns (sorted_names, None)
        or (None, error)."""
        assert self.dep_tree is not None
        _srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _n, _s in self.udeb_dep_tree.selected_srcs.items():
                _srcs.setdefault(_n, _s)
        # an empty floor means a mirror is configured but has never
        # published (see _update_build_pending) — there is nothing to diff
        # against, so the whole non-fork selected set IS the workload.  Skip
        # the snapshot fetch, which would dead-end on an empty timestamp with
        # a misleading network error.
        if not from_ts:
            return sorted(
                _name for _name, _src in _srcs.items()
                if not self._is_fork_source(_src)), None
        _from = repo_audit.fetch_source_versions_at(self.config, from_ts)
        if _from is None:
            return None, (f"could not fetch the published snapshot ({from_ts}) "
                          f"Sources index — check network / the timestamp")
        _out: 'list[str]' = []
        for _name, _src in _srcs.items():
            if self._is_fork_source(_src):
                continue   # forks are LOCAL, not driven by the upstream snapshot
            _fv = _from.get(_name)
            _cur_v = utils.version_no_epoch(str(_src.version))
            if _fv is None or apt_pkg.version_compare(
                    _cur_v, utils.version_no_epoch(_fv)) > 0:
                _out.append(_name)
        return sorted(_out), None

    def _update_build_pending(self) -> bool:
        """True when `source build` should run in UPDATE mode: at least one
        publish target is behind the local current snapshot.

        The floor is `min(mirror.<each>.state.current)` across configured
        mirrors — any laggard means an unpublished delta and UPDATE mode
        is the right path.  No mirrors configured → no publish target →
        no UPDATE mode.

        Requires dep_check_ready AND a non-empty local published.manifest
        (the +asg uN ledger; without it we have no version-stamping
        authority and a clean build is needed first).
        """
        if not self.flags.dep_check_ready:
            return False
        if not repo_audit.published_ledger(self.config):
            return False
        _cur = self._snapshot_current()
        if not _cur:
            return False
        import mirror as _mirror_mod
        if not _mirror_mod.list_mirrors(self.config):
            # No mirrors configured → no publish target → no UPDATE mode.
            return False
        _floor = self._mirror_floor()
        if not _floor:
            # A mirror is configured but never published — that delta IS
            # the workload.
            return True
        return apt_pkg.version_compare(_cur, _floor) > 0

    def _needs_bump_build(self, name: str, src, ledger: dict,
                          release: int) -> bool:
        """True when a workload source is a same-base re-spin (security/NMU)
        whose THIS-generation +asg<R>u<N> artifact is not yet on disk — the one
        case the un-forced skip-gate wrongly skips, because the rebuilt
        pristine filename collides with the prior build's.  PER-FILE: a target
        if ANY predicted main binary's exact uN is missing.

        NOT a target when the source isn't a delta (a clean new-base upload
        ships pristine and the un-forced gate builds it normally), nor when
        every main binary already carries this generation's exact uN
        (idempotent: a re-run skips).

        N is per-file (`utils.asg_next_n` against the local signed manifest), so
        the EXACT expected uN is checked with os.path.isfile — NOT
        find_matching_artifact, which would accept a stale older +asg and wrongly
        skip a needed re-bump.  The same-base test mirrors buildcontainer's
        `_src_is_delta` so this decision and the post-build stamp agree.
        """
        if utils.strip_nmu_suffix(str(src.version)) == str(src.version):
            return False                      # clean new base — not a re-spin
        # Component from origin mirror so non-main packages (contrib /
        # non-free / non-free-firmware) look up the correct dir.
        # Without this, every non-main re-spin would report "bump
        # missing" on every run (file IS there, just under the right
        # component) and force a needless rebuild loop.  Mirrors
        # check_build / _source_state.
        _comp = getattr(getattr(src, '_mirror', None), 'component', '') or 'main'
        for _f in self._predicted_files_for_source(name):
            if not (_f.endswith(('.deb', '.udeb'))):
                continue
            if utils.classify_repo_subdir(_f) != 'main':
                continue
            _parts = os.path.splitext(_f)[0].split('_')
            if len(_parts) != 3:
                continue
            _bin, _ver, _arch = _parts
            _n = utils.asg_next_n(
                ledger.get(_bin, []), utils.pristine_base(_ver), release)
            _expected = utils.asg_filename(_f, release, _n)
            _dst = self.config.deb_dest_for_filename(_f, _comp)
            if not os.path.isfile(os.path.join(_dst, _expected)):
                return True                   # this file's current-gen bump missing
        return False

    def _do_update_build(self):
        """Rebuild the published→current source delta (+asg<R>u<N>-stamped,
        per-file N from the published manifest) AND build any OTHER selected
        source that needs building — forks etc. that aren't part of the upstream
        snapshot delta — so update-mode `source build` builds everything the
        audit lists, not just the delta.

        NO blanket force: the un-forced skip gate skips packages already built
        for this generation; same-base security/NMU re-spins (whose rebuilt
        pristine filename collides with the prior build, so the filename gate
        can't see the change) are rebuilt as bump-targets; genuinely-missing
        binaries (needs_build, e.g. a fresh fork) build normally.  Resumable +
        idempotent."""
        # defensive reset.  cmd_source_build already resets on entry
        # before routing here, but the convention pins False-on-entry for
        # every function that sets the flag True — so a future direct
        # caller can't leak a stale True if this raises mid-run.
        self.flags.source_build_ready = False
        _floor = self._mirror_floor()
        _current = self._snapshot_current()
        console.print(
            f"source build: UPDATE mode — floor {_floor or '(none)'} → current "
            f"{_current}; rebuilding the changed source delta (+asg-stamped, "
            f"per-file N) plus any other source needing a build.",
            tui.COLOR_HIGHLIGHT)
        _workload, _err = self._workload_since_snapshot(_floor)
        if _err:
            console.print(f"source build (update): {_err}", tui.COLOR_ERROR)
            return
        # N authority = the PUBLISHED manifest only.  N is a published-generation
        # counter (advances on `mirror publish`, not per build): the next-to-mint
        # is one past the highest PUBLISHED uN; local rebuilds of the still-
        # pending update keep the same N.  (A build-ledger union was tried +
        # reverted — recording locally-built uN made asg_next_n overshoot by 1,
        # turning every same-base delta into a perpetual bump-target.)
        _ledger = repo_audit.published_ledger(self.config)
        if self.container is not None:
            self.container.asg_ledger = _ledger
        try:
            _release = None
            try:
                _release = int(
                    str(self.config.build_version).strip('"').strip("'"))
            except (TypeError, ValueError):
                pass
            _srcs = dict(self.dep_tree.selected_srcs)
            if self.udeb_dep_tree is not None:
                for _n, _s in self.udeb_dep_tree.selected_srcs.items():
                    _srcs.setdefault(_n, _s)
            _wset = set(_workload)

            # (1) the snapshot delta: respins rebuilt+stamped, current ones
            # skipped.  Mirrors the loop's exact skip rule (skip iff check_build
            # AND not a bump-target) so the report matches what will build.
            _delta_to_build = []
            for _n in _workload:
                _s = _srcs.get(_n)
                if _s is None:
                    _delta_to_build.append(_n)
                    continue
                _is_bump = (
                    _release is not None and self.container is not None
                    and self._needs_bump_build(_n, _s, _ledger, _release))
                _expected = self._predicted_files_for_source(_n)
                if _is_bump or not (
                        self.container is not None
                        and self.container.check_build(_s, _expected)):
                    _delta_to_build.append(_n)

            # (2) any OTHER selected source that needs building (forks, or any
            # source with missing/invalid binaries) — NOT in the upstream delta.
            # These build pristine (not deltas → not +asg-stamped).
            _extra = []
            if self.container is not None:
                _extra = [
                    _n for _n, _s in sorted(_srcs.items())
                    if _n not in _wset
                    and self._source_state(_n, _s) in ('needs_build', 'stale_pass')
                ]

            console.print(
                f"  {len(_workload)} changed source(s): "
                f"{len(_workload) - len(_delta_to_build)} already up-to-date, "
                f"{len(_delta_to_build)} to (re)build"
                + (f": {', '.join(_delta_to_build)}" if _delta_to_build else ""))
            if _extra:
                console.print(
                    f"  + {len(_extra)} other source(s) needing a build "
                    f"(not in the delta — e.g. forks): {', '.join(_extra)}")

            if not _delta_to_build and not _extra:
                console.print("source build: everything already up-to-date for "
                              "this generation — nothing to build.")
                # Treat "nothing to build" as success — the workload is in
                # the published state, repo/ has the binaries, downstream
                # stages can proceed.  cmd_source_build's entry guard at
                # ~L7381 reset the flag to False; if we don't re-arm it
                # here, autorun aborts at the next step thinking the
                # build didn't complete.
                self.flags.source_build_ready = True
                return
            # Pass the full delta workload (the loop skips the up-to-date and
            # rebuilds bump-targets) plus the extra needs_build sources.  Ledger
            # loaded → delta respins get +asg-stamped; forks build pristine.
            # _in_update_build=True enables _bump_active inside cmd_source_build
            # — the bump-target predicate (same-base re-spin → exact
            # `+asg<R>u<N>` missing) only makes sense in this workflow.
            self._in_update_build = True
            try:
                self.cmd_source_build(*(list(_workload) + _extra))
            finally:
                self._in_update_build = False
        finally:
            if self.container is not None:
                self.container.asg_ledger = None

    def _preflight_stamp_invariant(self, names) -> 'list[tuple[str, str]]':
        """Guard A (preemptive, zero builds): for each source's predicted
        pristine files, verify a hypothetical +asg<R>u1 stamp round-trips via
        match_pristine_base.  Returns (filename, reason) offenders; empty = OK.
        Catches malformed/epoch version shapes and matcher/stamper drift BEFORE
        any build, so a doomed run aborts up front instead of looping."""
        try:
            _release = int(str(self.config.build_version).strip('"').strip("'"))
        except (TypeError, ValueError):
            return [('<config>', "[Build] VERSION is not an integer: "
                     f"{self.config.build_version!r}")]
        _offenders: 'list[tuple[str, str]]' = []
        for _name in names:
            for _f in self._predicted_files_for_source(_name):
                if not (_f.endswith(('.deb', '.udeb'))):
                    continue
                _stamped = utils.asg_filename(_f, _release, 1)
                if _stamped == _f or not utils.match_pristine_base(_f, _stamped):
                    _offenders.append((_f, "asg stamp does not round-trip"))
        return _offenders

    def cmd_source_audit(self, *args):
        """READ-ONLY: report the build-state of every selected source.

        Usage: source audit [verbose] [summary]

        Audits dep_tree + udeb_dep_tree (merged).  Classifies each
        source into one of `_SOURCE_STATES` via `_source_state` —
        the same classifier `source repair` consults — and reports
        counts per state, optionally broken down by subset
        (base / live / installer / pool / recommended).

        States surfaced:
          ok            — source files synced + binaries present + valid,
                          record=done/PASS, patches unchanged.  Nothing to do.
          needs_sync    — source files missing or .verified sidecar
                          absent → run `source sync <pkg>` (or `source
                          sync` for bulk).
          needs_build   — binaries missing or invalid → run `source build`.
          stale_pass    — WARN: record=PASS but state has drifted
                          (binaries gone OR patches changed since last
                          successful build) → run `source repair` to
                          clear the lie; next `source build` will rebuild.
          interrupted   — WARN: record has a non-terminal phase (build
                          was killed mid-flight) → run `source repair`
                          to clear the record; next `source build` will
                          rerun.
          fail          — record=failed.  Operator marker; no-op.
          tunneled      — record=tunneled (third-party pull).  Not built.
          no_pkgs       — source declares no main-tier binary in either
                          tree (rare; side-artifact-only sources).

        `summary` arg prints only the rebuild-queue count + subset
        breakdown — the operator's "how much work remains" view.

        `verbose` arg adds two things to the default report:
          - the existing per-name subset annotation under "Failures:"
            (one line per actionable source)
          - an "Informational (verbose)" block listing the names in
            the terminal-state buckets (`tunneled`, `fail`,
            `no_pkgs`) which the default report only summarises by
            count.  The `ok` bucket is deliberately omitted from
            verbose listing — it's almost always the full corpus.

        MIRROR-02: in `[Build] Mode = build`, `dep_tree.selected_srcs`
        IS the indl subset (chunk 2 sets it directly from build_pkg.list,
        no closure walk), so this audit naturally scopes to just
        those sources — no extra filter needed.  In dist mode it
        walks the full corpus as before.

        Read-only by design: never writes records, never invokes
        BuildContainer.build, never calls _refresh_patches.  Mutating
        the build state is `source repair`'s job (which uses the same
        classifier).

        Prereqs: cache_ready + dep_check_ready.  Walks dep_tree to know
        which sources are selected.  The "Next-run build" section needs
        a live container (to call check_build); when none is initialized
        that section is omitted but the rest of the audit still runs.
        """
        if not (self.flags.cache_ready and self.flags.dep_check_ready):
            console.print(
                "source audit needs `cache build` + `cache parse` to have "
                "run first.",
                tui.COLOR_ERROR,
            )
            return

        _verbose = 'verbose' in args
        _summary = 'summary' in args

        # Merge deb + udeb dep trees.  Source objects shared via
        # source_hashtable, so the dict-update naturally dedupes.
        assert self.dep_tree is not None
        _srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _name, _src in self.udeb_dep_tree.selected_srcs.items():
                if _name not in _srcs:
                    _srcs[_name] = _src

        # Per-state buckets — names only, in name order.
        from collections import defaultdict as _dd
        _by_state: 'dict[str, list[str]]' = _dd(list)

        _bar = ProgressBar(
            label='Source audit', maxvalue=max(1, len(_srcs)),
            show_rate=False,
        )
        try:
            for _name, _src in sorted(_srcs.items()):
                _bar.step(1)
                _state = self._source_state(_name, _src)
                _by_state[_state].append(_name)
        finally:
            _bar.close()

        # Subset classifier — which `source build <mode>` addresses
        # each source.  Priority: pkg > installer > live > recommended.
        _live_set    = self.dep_tree.live_exclusive_src_names
        _inst_set    = self.dep_tree.installer_exclusive_src_names
        _pool_set    = self.dep_tree.pool_extras_src_names
        _extras_set  = self.dep_tree.extras_src_names
        _udeb_names: set = set()
        if self.udeb_dep_tree is not None:
            _udeb_names = set(self.udeb_dep_tree.selected_srcs.keys())

        def _subset_for(name: str) -> str:
            if (name not in _live_set and name not in _inst_set
                    and name not in _pool_set
                    and name not in _extras_set
                    and name not in _udeb_names):
                return 'pkg'
            if name in _inst_set or name in _udeb_names:
                return 'installer'
            if name in _live_set:
                return 'live'
            if name in _pool_set:
                return 'pool'
            if name in _extras_set:
                return 'recommended'
            return 'unclassified'

        # ── summary mode ────────────────────────────────────────────
        # Just the rebuild-queue count + subset breakdown.  Operator
        # wants "how much work remains?" — nothing more.
        _rebuild_candidates = (
            _by_state.get('needs_build', [])
            + _by_state.get('stale_pass', [])
        )
        _by_subset: 'dict[str, list[str]]' = _dd(list)
        for _n in _rebuild_candidates:
            _by_subset[_subset_for(_n)].append(_n)

        if _summary:
            console.print(
                f"Source audit (summary): {len(_rebuild_candidates)} "
                f"source(s) need build / repair."
            )
            if _rebuild_candidates:
                _cmd_map = {
                    'pkg':          'source build',
                    'installer':    'source build installer',
                    'live':         'source build live',
                    'pool':         'source build (pool)',
                    'recommended':  'source build recommended',
                    'unclassified': '(none — investigate)',
                }
                for _subset in ('pkg', 'installer', 'live', 'pool',
                                'recommended', 'unclassified'):
                    _names = _by_subset.get(_subset, [])
                    if not _names:
                        continue
                    console.print(
                        f"  {len(_names):5d}  {_subset:<13s}  "
                        f"→  {_cmd_map[_subset]}"
                    )
            return

        # ── full report ─────────────────────────────────────────────
        _total = len(_srcs)
        console.print("Source audit (read-only):")
        console.print(f"  {len(_by_state.get('ok',[])):5d}  ok "
                      "(synced, built, record=PASS, patches fresh)")
        console.print(f"  {len(_by_state.get('needs_build',[])):5d}  "
                      "needs_build (binaries missing/invalid or no record)")
        if _by_state.get('stale_pass'):
            console.print(
                f"  {len(_by_state['stale_pass']):5d}  "
                "stale_pass (WARN: record=PASS but state drifted; "
                "run `source repair`)",
                tui.COLOR_WARNING,
            )
        if _by_state.get('interrupted'):
            console.print(
                f"  {len(_by_state['interrupted']):5d}  "
                "interrupted (WARN: build was killed mid-flight; "
                "run `source repair`)",
                tui.COLOR_WARNING,
            )
        if _by_state.get('needs_sync'):
            console.print(
                f"  {len(_by_state['needs_sync']):5d}  "
                "needs_sync (source files missing or .verified absent; "
                "run `source sync`)",
                tui.COLOR_WARNING,
            )
        if _by_state.get('tunneled'):
            console.print(
                f"  {len(_by_state['tunneled']):5d}  tunneled "
                "(record=TUNNELED, third-party pull)"
            )
        if _by_state.get('fail'):
            console.print(
                f"  {len(_by_state['fail']):5d}  fail "
                "(record=FAIL, explicit prior-build failure)"
            )
        if _by_state.get('no_pkgs'):
            console.print(
                f"  {len(_by_state['no_pkgs']):5d}  no_pkgs "
                "(source declares no main-tier binary)"
            )
        console.print(f"  {_total:5d}  total")

        if _rebuild_candidates:
            console.print("")
            console.print("Rebuild queue by subset:")
            _cmd_map = {
                'pkg':          'source build',
                'installer':    'source build installer',
                'live':         'source build live',
                'pool':         'source build (pool)',
                'recommended':  'source build recommended',
                'unclassified': '(none — investigate)',
            }
            for _subset in ('pkg', 'installer', 'live', 'pool',
                            'recommended', 'unclassified'):
                _names = _by_subset.get(_subset, [])
                if not _names:
                    continue
                console.print(
                    f"  {len(_names):5d}  {_subset:<13s}  "
                    f"→  {_cmd_map[_subset]}"
                )

        # Concise failure list — shown by default whenever any
        # actionable state is non-empty.  One line per state, names
        # wrapped to fit terminal width.  `verbose` adds per-name
        # subset annotation.
        _actionable = ('needs_build', 'stale_pass', 'interrupted',
                       'needs_sync')
        _any_actionable = any(_by_state.get(_s) for _s in _actionable)
        if _any_actionable:
            console.print("")
            console.print("Failures:")
            for _state in _actionable:
                _names = sorted(_by_state.get(_state, []))
                if not _names:
                    continue
                if _verbose:
                    console.print(f"  [{_state}] ({len(_names)}):")
                    for _n in _names:
                        console.print(f"    {_n}  ({_subset_for(_n)})")
                else:
                    self._print_wrapped_names(
                        f"  {_state} ({len(_names)})", _names,
                    )

        # Verbose-only: drill into the informational (non-actionable)
        # buckets — `tunneled`, `fail`, `no_pkgs`.  These are TERMINAL
        # states the operator can't re-run their way out of (tunneled
        # = explicit third-party pull; fail = prior-build marker;
        # no_pkgs = side-artifact-only source).  Default audit hides
        # the names — too noisy on a corpus this size — but the
        # operator asked, so verbose lists each.
        if _verbose:
            _informational = ('tunneled', 'fail', 'no_pkgs')
            _any_info = any(_by_state.get(_s) for _s in _informational)
            if _any_info:
                console.print("")
                console.print("Informational (verbose):")
                for _state in _informational:
                    _names = sorted(_by_state.get(_state, []))
                    if not _names:
                        continue
                    console.print(f"  [{_state}] ({len(_names)}):")
                    for _n in _names:
                        console.print(f"    {_n}  ({_subset_for(_n)})")

        # Next-run rebuild queue (matches `source build all`).  The static
        # classifier above (needs_build / stale_pass) misses the UPD-01
        # bump-target predicate that fires for NMU-suffixed sources whose
        # current-generation +asg<R>u<N> file isn't yet on disk; those
        # rebuild even though check_build returns True for the pristine.
        # We compute the same workload _do_update_build does so the count
        # reported here matches what `source build all` actually runs.
        if self.container is not None:
            self._print_next_run_build_queue(
                _srcs, _rebuild_candidates, _verbose)

        # Obsolete patch detection.  patch/source/<pkg>/<ver>/ holds version-
        # pinned patches; when the cached source version moves past <ver>
        # (and no <new_ver>/ dir exists), the patches are NEVER applied —
        # silently — and the operator's intent is lost.  Surface so they
        # can move/refresh the patches before the next build.
        self._print_obsolete_patch_warning(_srcs)

    def _print_next_run_build_queue(
        self, _srcs: dict, _rebuild_candidates: 'list[str]', _verbose: bool,
    ) -> None:
        """Compute the rebuild list the next `source build all` will run
        and print it.  Mirrors `_do_update_build`'s decision logic so the
        count here matches the actual build."""
        console.print("")
        console.print("Next-run build (`source build all`):")
        if not self._update_build_pending():
            console.print(
                "  Mode: NORMAL (no snapshot delta pending)",
                tui.COLOR_INFO)
            console.print(
                f"  {len(_rebuild_candidates):5d}  total to (re)build "
                f"(matches `Rebuild queue by subset` above)")
            return
        _floor = self._mirror_floor()
        _current = self._snapshot_current()
        console.print(
            f"  Mode: UPDATE — mirror floor {_floor or '(none)'} → "
            f"current {_current}",
            tui.COLOR_INFO)
        _workload, _err = self._workload_since_snapshot(_floor)
        if _err:
            console.print(
                f"  WARN: cannot compute snapshot delta — {_err}",
                tui.COLOR_WARNING)
            return
        _ledger = repo_audit.published_ledger(self.config)
        try:
            _release = int(
                str(self.config.build_version).strip('"').strip("'"))
        except (TypeError, ValueError):
            _release = None
        _wset = set(_workload or [])
        _delta_to_build: 'list[str]' = []
        _bump_targets: 'list[str]' = []
        for _n in _workload or []:
            _s = _srcs.get(_n)
            if _s is None:
                _delta_to_build.append(_n)
                continue
            _is_bump = (
                _release is not None
                and self._needs_bump_build(
                    _n, _s, _ledger, _release))
            if _is_bump:
                _bump_targets.append(_n)
                _delta_to_build.append(_n)
                continue
            _expected = self._predicted_files_for_source(_n)
            assert self.container is not None
            if not self.container.check_build(_s, _expected):
                _delta_to_build.append(_n)
        _extras = sorted([
            _n for _n, _s in _srcs.items()
            if _n not in _wset
            and self._source_state(_n, _s) in ('needs_build', 'stale_pass')
        ])
        _all_next = sorted(set(_delta_to_build) | set(_extras))
        console.print(f"  {len(_all_next):5d}  total to (re)build")
        if _bump_targets:
            console.print(
                f"  {len(_bump_targets):5d}  bump-target "
                "(UPD-01 +asg<R>u<N> stamp pending for current generation)")
        _delta_non_bump = sorted(set(_delta_to_build) - set(_bump_targets))
        if _delta_non_bump:
            console.print(
                f"  {len(_delta_non_bump):5d}  delta-rebuild "
                "(in workload, binaries invalid/missing)")
        if _extras:
            console.print(
                f"  {len(_extras):5d}  extra (needs_build/stale_pass, not in workload)")
        if _verbose and _all_next:
            self._print_wrapped_names("  pkgs", _all_next)

    def _print_obsolete_patch_warning(self, _srcs: dict) -> None:
        """Scan patch/source/<pkg>/ for version subdirs whose <ver> is
        OLDER than the source's current cache version.  Patches in such
        dirs are silently NOT applied (the build-time discovery in
        _refresh_patches keys on version_no_epoch(current_src.version),
        so the entire old dir is invisible).  WARN so the operator can
        move/refresh them before they ship an unpatched build.
        """
        _obsolete: 'list[tuple[str, list[str], str]]' = []
        for _n, _src in sorted(_srcs.items()):
            _current_ver = utils.version_no_epoch(str(_src.version))
            _pkg_dir = os.path.join(self.config.dir_patch_source, _n)
            try:
                _subdirs = [
                    _d for _d in os.listdir(_pkg_dir)
                    if os.path.isdir(os.path.join(_pkg_dir, _d))]
            except OSError:
                continue
            _stale = []
            for _ver in _subdirs:
                if _ver == _current_ver:
                    continue
                try:
                    _patches = [
                        _p for _p in os.listdir(
                            os.path.join(_pkg_dir, _ver))
                        if _p.endswith('.patch')]
                except OSError:
                    continue
                if not _patches:
                    continue
                # Only WARN when current is strictly newer (patches are
                # for a past version).  Future-version dirs (rare) get
                # surfaced as INFO in _verbose only — they're not stale.
                if apt_pkg.version_compare(_current_ver, _ver) > 0:
                    _stale.append(_ver)
            if _stale:
                _obsolete.append((_n, sorted(_stale), _current_ver))
        if not _obsolete:
            return
        console.print("")
        console.print(
            f"WARN: obsolete patches ({len(_obsolete)} pkg(s) — source "
            "advanced past the patch dir; patches silently NOT applied):",
            tui.COLOR_WARNING)
        for _n, _stale, _cur in _obsolete:
            console.print(
                f"  {_n}: {', '.join(_stale)}  (current: {_cur})",
                tui.COLOR_WARNING)

    @staticmethod
    def _print_wrapped_names(label: str, names: 'list[str]',
                              wrap: int = 72) -> None:
        """Print `label: name, name, name, …` with names wrapped to
        `wrap` columns past the label's indent.  Keeps the failure
        summary compact (one logical line per state, wrapped to
        terminal width rather than ballooning to N lines for N names).
        """
        _indent = '    '
        _line = f"{label}: "
        _first = True
        for _n in names:
            _piece = _n if _first else ', ' + _n
            if len(_line) + len(_piece) > wrap and not _first:
                console.print(_line)
                _line = _indent + _n
            else:
                _line += _piece
            _first = False
        if _line.strip():
            console.print(_line)

    def cmd_source_fork(self, *args):
        """Manage fork packages — create a new fork from upstream source,
        reload an existing fork after edits, or toggle enabled/disabled.

        Usage:
          source fork <pkg>              — create fork (if absent) OR
                                            reload (if present)
          source fork <pkg> enabled      — enable (remove .disabled marker)
          source fork <pkg> disabled     — disable (add .disabled marker)

        Creation (no fork tree exists yet):
          - Looks up the source in cache.source_hashtable (requires
            `cache build`).
          - Downloads .dsc + tarballs into source/ if not already there.
          - Extracts via `dpkg-source -x` into fork/source/<pkg>/.
            The extracted tree carries the upstream debian/ — operator
            edits in place.
          - Invalidates cache_ready + dep_check_ready so the next
            `cache build` + `cache parse` pick the fork up.

        Reload (fork tree already exists):
          - Delegates to the former `repo reload` logic — light-touch
            rebuild when tree-hash changed but dep-affecting fields
            didn't; refuses (with actionable diagnostic) when dep
            fields changed.

        Enable / disable:
          - Toggles a `.disabled` marker file at fork/source/<pkg>/.
            fork_mirror skips disabled trees during cache ingest.
          - Both operations invalidate cache + dep_tree so the change
            takes effect on the next `cache build` + `cache parse`.

        Replaces the standalone `repo reload <pkg>...` command (P4
        2026-05-23).
        """
        if not args:
            console.print(
                "Usage: source fork <pkg> [enabled|disabled]",
                tui.COLOR_INFO,
            )
            return
        _pkg = args[0]
        _action = args[1] if len(args) > 1 else None

        if _action not in (None, 'enabled', 'disabled'):
            console.print(
                f"source fork: unknown action {_action!r} — "
                "expected 'enabled' or 'disabled'",
                tui.COLOR_ERROR,
            )
            return

        _pkg_dir = os.path.join(self.config.dir_fork_source, _pkg)

        if _action == 'enabled':
            return self._fork_set_enabled(_pkg, _pkg_dir, enable=True)
        if _action == 'disabled':
            return self._fork_set_enabled(_pkg, _pkg_dir, enable=False)

        # No action arg — create or reload based on presence.
        if os.path.isdir(_pkg_dir):
            console.print(
                f"source fork {_pkg}: fork tree present — reloading "
                f"after edit",
                tui.COLOR_INFO,
            )
            return self.cmd_reload_fork(_pkg)
        return self._fork_create(_pkg, _pkg_dir)

    def _fork_set_enabled(self, pkg: str, pkg_dir: str, *,
                           enable: bool) -> None:
        """Toggle the .disabled marker on a fork tree.  Both directions
        invalidate cache + dep state so the change is picked up.
        """
        if not os.path.isdir(pkg_dir):
            console.print(
                f"source fork {pkg} {'enabled' if enable else 'disabled'}: "
                f"no fork tree at {pkg_dir} — create it first with "
                f"`source fork {pkg}`",
                tui.COLOR_ERROR,
            )
            return
        _marker = os.path.join(pkg_dir, '.disabled')
        if enable:
            try:
                os.remove(_marker)
                console.print(
                    f"source fork {pkg}: enabled "
                    f"(.disabled marker removed)"
                )
            except FileNotFoundError:
                console.print(
                    f"source fork {pkg}: already enabled (no .disabled "
                    f"marker present) — no change"
                )
                return
            except OSError as e:
                console.print(
                    f"source fork {pkg} enable: cannot remove {_marker}: "
                    f"{e}",
                    tui.COLOR_ERROR,
                )
                return
        else:
            if os.path.exists(_marker):
                console.print(
                    f"source fork {pkg}: already disabled (.disabled "
                    f"marker present) — no change"
                )
                return
            try:
                with open(_marker, 'w') as fh:
                    fh.write('Disabled via `source fork '
                             f'{pkg} disabled` — remove this file or '
                             'run `source fork {pkg} enabled` to '
                             're-include in cache builds.\n')
                console.print(
                    f"source fork {pkg}: disabled "
                    f"(.disabled marker written)"
                )
            except OSError as e:
                console.print(
                    f"source fork {pkg} disable: cannot write {_marker}: "
                    f"{e}",
                    tui.COLOR_ERROR,
                )
                return
        # Invalidate so next cache build picks up the change.
        self._invalidate_for_fork_change()

    def _fork_create(self, pkg: str, pkg_dir: str) -> None:
        """Create a new fork tree by downloading + extracting upstream
        source.  Requires cache_ready (need source_hashtable to look
        up the source's files + mirror).
        """
        if not self.flags.cache_ready:
            console.print(
                f"source fork {pkg}: requires `cache build` first "
                f"(need cache.source_hashtable to look up the source)",
                tui.COLOR_ERROR,
            )
            return
        assert self.cache is not None
        _sources = self.cache.source_hashtable.get(pkg, [])
        if not _sources:
            console.print(
                f"source fork {pkg}: source not in cache — check spelling, "
                f"or `cache parse` may need to run first to populate "
                f"selected_srcs",
                tui.COLOR_ERROR,
            )
            return
        # Highest version wins (cache may have multiple — security
        # update + main + updates).  Source.version is a debian Version
        # object that orders correctly via max().
        _src = max(_sources, key=lambda s: s.version)

        # Download files if not already present.  Uses the same
        # synthetic-tree wrapper as `source sync <pkg>`.
        class _SingleSrcTree:
            def __init__(_self, _name, _s):
                _self.selected_srcs = {_name: _s}
                _self.download_size = sum(
                    int(_f.get('size', 0)) for _f in _s.files.values()
                )
        console.print(
            f"source fork {pkg}: fetching {len(_src.files)} source "
            f"file(s) (version {_src.version})…"
        )
        utils.download_source(
            _SingleSrcTree(pkg, _src),  # type: ignore[arg-type]
            self.config.dir_source,
        )

        # Find the .dsc — dpkg-source -x needs it.
        _dsc = None
        for _fname in _src.files:
            if _fname.endswith('.dsc'):
                _dsc = os.path.join(self.config.dir_source, _fname)
                break
        if _dsc is None or not os.path.isfile(_dsc):
            console.print(
                f"source fork {pkg}: no .dsc among downloaded files",
                tui.COLOR_ERROR,
            )
            return

        # Extract.  dpkg-source -x refuses if the target dir exists.
        # We already gated on `os.path.isdir(pkg_dir)` False in caller.
        console.print(
            f"source fork {pkg}: dpkg-source -x {os.path.basename(_dsc)} "
            f"{pkg_dir}"
        )
        try:
            _r = subprocess.run(
                ['dpkg-source', '-x', _dsc, pkg_dir],
                capture_output=True, text=True, timeout=300,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            console.print(
                f"source fork {pkg}: dpkg-source failed: {e}",
                tui.COLOR_ERROR,
            )
            return
        if _r.returncode != 0:
            console.print(
                f"source fork {pkg}: dpkg-source -x exit "
                f"{_r.returncode}: {_r.stderr.strip()[:300]}",
                tui.COLOR_ERROR,
            )
            return
        console.print(
            f"source fork {pkg}: fork tree created at {pkg_dir} "
            f"(enabled by default)",
            tui.COLOR_HIGHLIGHT,
        )
        console.print(
            f"  Next steps:\n"
            f"    1. Edit fork/source/{pkg}/ as needed\n"
            f"    2. `cache build force` + `cache parse force` to "
            f"pick up the fork\n"
            f"    3. `source build {pkg}` to build",
            tui.COLOR_INFO,
        )
        self._invalidate_for_fork_change()

    def _invalidate_for_fork_change(self) -> None:
        """Reset cache_ready + dep_check_ready so the next pipeline run
        re-ingests the fork tree change."""
        if self.flags.cache_ready or self.flags.dep_check_ready:
            self.flags.cache_ready = False
            self.flags.dep_check_ready = False
            console.print(
                "  cache + dep state invalidated — run `cache build` "
                "+ `cache parse` to propagate the fork change",
                tui.COLOR_INFO,
            )

    def _build_one_source(
        self, _src_pkg,
        _force: bool,
        _bump_active: bool,
        _bump_release,
        _profile_override,
    ) -> 'tuple[str, int]':
        """COMP-03 Phase 4: one source's work unit.  Extracted from the
        old serial loop in cmd_source_build so the parallel
        ThreadPoolExecutor path can submit the same operation N at a
        time without duplicating the per-source check_build / skip_src
        / tunnel / bump-target / build / verify logic.

        Returns ('built'|'tunneled'|'failed'|'skipped', 0).  The int
        slot is reserved for future extensibility (e.g. wall-clock).

        Thread-safety contract: this method touches only its arguments,
        self.config (read-only on the build path), self.cache (read-
        only here), self.container (registry + check_build + build —
        all thread-safe under Phase 1's per-worker scratch dir + Phase
        2's _live_lock-guarded registry), and the logger.  Counters
        and progress_bar.step are NOT touched here — caller tallies on
        the main thread after the worker returns.
        """
        # cmd_source_build's prelude (callers below) gates on these being
        # non-None; the asserts narrow the Optional types for mypy and
        # document the contract for human readers.
        assert self.cache is not None
        assert self.container is not None

        # Packages on the skip_src list are excluded unconditionally —
        # typically packages that are known to be unbuildable in the
        # current environment.
        if _src_pkg.package in self.cache.skip_src:
            logger.warning(f"Package {_src_pkg.package} in skip_list")
            return ('skipped', 0)

        # Predicted artefacts (union across both dep_trees) — used by
        # both check_build (skip-rebuild gate) and _do_tunnel.  These
        # are PRISTINE strip-NMU names: tunneled .debs are normalised
        # (strip + asg-stamp) on download so on-disk they look identical
        # to from-source-built .debs.  The check_build gate is the same
        # for both paths; only the WAY we acquire the file differs.
        _expected_files = self._predicted_files_for_source(_src_pkg.package)

        # Tunneled packages: download Debian's prebuilt binary and run
        # it through the same strip + asg-stamp normalisation a source
        # build would.  check_build uses the SAME pristine prediction
        # for both paths — once a tunneled .deb has been normalised it
        # is, to every other subsystem (repo audit, source audit,
        # check_build, find_matching_artifact), a legitimately-built
        # binary.  The only place the difference surfaces is the
        # federation sidecar, where the claim's `republished_from`
        # field marks the package as "no owner" (tunneled provenance).
        if _src_pkg.package in self.config.tunnel_packages:
            if self.container.check_build(_src_pkg, _expected_files):
                logger.warning(
                    f"Package {_src_pkg.package} already tunneled [SKIPPED]")
                return ('skipped', 0)
            _build_result = self._do_tunnel(_src_pkg)
            if _build_result:
                logger.warning(f"Tunnel {_src_pkg.package} [TUNNELED]")
                return ('tunneled', 0)
            logger.error(f"Tunnel {_src_pkg.package} [FAIL]")
            # the live-build summary reports counts only — name the
            # failing package on the console too, not just the log tab.
            console.print(f"  Tunnel {_src_pkg.package} [FAIL]", tui.COLOR_ERROR)
            return ('failed', 0)

        # Bump-target: same-base re-spin whose current-generation +asg uN
        # is missing.  Forces rebuild even un-forced so the stamp can
        # mint the new uN.  Idempotent: once uN exists it's no longer
        # a target.
        _bump_target = (
            _bump_active and self._needs_bump_build(
                _src_pkg.package, _src_pkg,
                self.container.asg_ledger or {}, _bump_release))

        # Skip packages with a valid existing build result unless force
        # is set — but never skip a bump-target.
        if (not _force and not _bump_target
                and self.container.check_build(_src_pkg, _expected_files)):
            logger.info(
                f"Package {_src_pkg.package} already built [SKIPPED]")
            return ('skipped', 0)

        _build_result = self.container.build(
            _src_pkg,
            profiles_override=_profile_override,
            options_override=_profile_override,
        )

        if _build_result and self.container.check_build(
                _src_pkg, _expected_files):
            logger.info(f"Building Package {_src_pkg.package} [PASS]")
            # Loop guard: a bump-target must come out stamped (its uN
            # now present).  If still missing, the predicate and the
            # post-build stamper disagreed — warn so it's visible.
            if _bump_target and self._needs_bump_build(
                    _src_pkg.package, _src_pkg,
                    self.container.asg_ledger or {}, _bump_release):
                logger.warning(
                    f"asg-bump: {_src_pkg.package} built but its +asg uN "
                    f"artifact is still absent — bump predicate/stamper "
                    f"mismatch; shipped unstamped this generation")
            return ('built', 0)
        if _build_result:
            # Guard B (UPD-01): the build reported PASS but check_build
            # still can't find/match the predicted artifacts — non-
            # convergence (CONF-13 cross-run rebuild-loop shape).  Fail
            # loudly NOW.
            logger.error(
                f"Building Package {_src_pkg.package} [FAIL] — built but "
                f"check_build still false (predicted artifact missing or "
                f"unmatched): expected {_expected_files}")
            # surface the failing package on the console, not just log.
            console.print(
                f"  Building {_src_pkg.package} [FAIL] — built but artifacts "
                "missing/unmatched", tui.COLOR_ERROR)
            return ('failed', 0)
        logger.error(f"Building Package {_src_pkg.package} [FAIL]")
        # surface the failing package on the console, not just log.
        console.print(f"  Building {_src_pkg.package} [FAIL]", tui.COLOR_ERROR)
        return ('failed', 0)

    def cmd_source_build(self, *args):
        """Build source packages inside the Docker build container.

        Usage: source build [force] [pkg | live | installer | recommended | all | <pkg> ...] [[profile,...]]

        Subset selectors use layered semantics matching the
        parallel-universe architecture:

          force         — rebuild packages even if a valid result already exists
          pkg           — build the pkg.list closure ONLY (no live, installer,
                          or extras).  This is the default when no subset and
                          no names are given.
          live          — build live-exclusive sources only (sources pulled
                          in solely by live.list).  Mixed sources are NOT
                          here — they're under 'pkg'.
          installer     — build the udeb closure's source set + installer-
                          exclusive deb sources (efibootmgr, grub-pc-bin
                          equivalents).  Many sources overlap with pkg/live
                          (cdebconf produces both .deb and .udeb outputs)
                          and are deduped via shared source_hashtable.
          recommended   — build ONLY the Recommends-only extras sources
                          (depth-1 Recommends pulled into the repo by
                          parse_dependency, but excluded from chroot install).
          all           — build EVERY selected source — the union of pkg +
                          live + installer + recommended in one pass,
                          deduped.  Equivalent to running the four subset
                          modes back-to-back; convenient when you don't
                          care about the staging and just want a complete
                          repo (apt-pool included).  Same per-source skip-
                          if-built gate applies, so re-running is cheap.
          <pkg>...      — limit the build to the named source packages
          [profile,...] — bracket-delimited token (e.g. `[nocheck]`) overrides
                          BOTH DEB_BUILD_PROFILES and DEB_BUILD_OPTIONS for
                          this invocation only.  Use `[]` (empty) for the
                          most permissive build (no profiles/options — docs
                          and tests included).  Implies `force` because the
                          .result cache wouldn't reflect the override.
          (no arg)      — equivalent to `source build pkg`.

        pkg / live / installer / recommended / all are mutually exclusive
        with each other and with named packages.

        For a complete live ISO: source build → source build live.
        For a complete installer ISO: source build → source build installer.
        For a complete repo in one command: source build all.
        autorun chains pkg + live for the live workflow.

        Each package is built in a fresh container instance with its declared
        build-dependencies installed at runtime.  Result files (.result) and build
        logs are written to the configured log/build directory.

        Packages listed under 'Tunneled' in build.conf are downloaded from the
        base Debian repo instead of being built locally, even when running
        source_build (the force flag has no effect on tunneled packages).

        Prompts before returning if any builds fail, allowing the operator to
        decide whether to continue with the partial package set.
        """
        # Eager mode-validation for the 'indl' subset — reject outside
        # build mode BEFORE any flag-prereq checks so the operator gets
        # a clear "wrong mode" message instead of a stale "Run 'source
        # sync' first" hint.
        if ('indl' in (_a.strip().lower() for _a in args)
                and self.config.build_mode != 'build'):
            console.print(
                "source build indl: only valid under "
                "`[Build] Mode = build`.  Did you mean `source "
                "build all`?",
                tui.COLOR_ERROR)
            return
        if not self.flags.download_ready:
            console.print("Run 'source sync' first")
            return

        if not self.flags.build_container_ready:
            console.print("Run 'container init' first")
            return

        # reset on entry so an interrupted partial build can't
        # leak a stale True from a previous successful run.  Re-set to
        # True on the success-tail (~L7115).
        self.flags.source_build_ready = False

        # Parse args via the static helper for testability.
        _err, _force, _subset, _names, _profile_override = \
            self._parse_source_build_args(args)
        if _err:
            console.print(_err)
            return

        # Load the asg ledger from the LOCAL signed manifest BEFORE any
        # build path branches.  Previously the ledger was only loaded in
        # UPDATE mode (via _do_update_build), so a NORMAL-mode rebuild of
        # a source whose binaries had previously been published at
        # `+asg<R>u<N>` would silently ship pristine — losing the
        # lineage and breaking dep pins in sibling-source metas that
        # captured the previous version.  Loading unconditionally lets
        # `_normalize_built_artifacts` see the lineage and bump
        # monotonically against the manifest.  No mirrors needed — the
        # manifest is local.
        if self.container is not None:
            self.container.asg_ledger = repo_audit.published_ledger(
                self.config)

        # A subset/all/bare invocation AUTO-DETECTS update mode — when a
        # published base exists and current is ahead of it, rebuild the
        # published→current SOURCE delta (stamped `+asg<R>u<N>`) instead
        # of a plain subset build.  Explicit package names or a [profile]
        # override opt out (manual builds).  The operator runs the same
        # `source build [all|live|installer]`; we tell them which we ran.
        if (not _names and _profile_override is None
                and self._update_build_pending()):
            return self._do_update_build()

        # Profile override implies force, because the .result cache from a
        # prior build under different profiles would otherwise short-circuit
        # our rebuild.  Surface this to the operator before silently flipping.
        if _profile_override is not None and not _force:
            console.print(
                f"Profile override [{','.join(_profile_override) or 'empty'}] "
                f"implies force (cache key wouldn't reflect override)",
                tui.COLOR_INFO,
            )
            _force = True

        # In build mode, the bare 'pkg' default gets rewritten to
        # 'indl' so the operator-visible label matches their mode.
        # Functionally identical (pkg's excludes are empty in build
        # mode), this just removes ambiguity from the printed progress.
        if (self.config.build_mode == 'build'
                and _subset == 'pkg'
                and not _names):
            _subset = 'indl'
        # Mode-validation for 'indl' subset already ran at the top of
        # cmd_source_build (eager-fail before flag gates).
        if _force:
            console.print("Force mode: skipping build cache checks")
        if _subset == 'pkg':
            console.print("Pkg mode: building pkg.list closure only "
                          "(no live, installer, or extras)")
        elif _subset == 'live':
            console.print("Live mode: building live-exclusive sources only")
        elif _subset == 'installer':
            console.print("Installer mode: building udeb closure + "
                          "installer-exclusive deb sources")
        elif _subset == 'recommended':
            console.print("Recommended mode: building extras-only sources")
        elif _subset == 'all':
            console.print("All mode: building every selected source "
                          "(pkg + live + installer + recommended union)")
        elif _subset == 'indl':
            console.print(
                "Indl subset: building every source in "
                "config/build_pkg.list")
        if _profile_override is not None:
            console.print(
                f"Profile override active: DEB_BUILD_PROFILES + "
                f"DEB_BUILD_OPTIONS = '{' '.join(_profile_override)}' "
                f"(was: profiles='{' '.join(sorted(self.config.build_profiles))}', "
                f"options='{' '.join(sorted(self.config.build_options))}')",
                tui.COLOR_INFO,
            )

        # Pick the package set per the mode resolved above.
        # Each subset is a tightly-scoped slice of the unified source
        # corpus; chroot build live needs source build + source build
        # live; chroot build installer needs source build + source build
        # installer.  Sources frequently overlap between deb and udeb worlds
        # (e.g. cdebconf produces both .deb and .udeb outputs from one
        # dpkg-buildpackage run) — looking up via dep_tree first then udeb
        # tree returns the same Source instance either way (shared via
        # source_hashtable), so building once produces both kinds of
        # artefacts at the same time.
        if _names:
            packages = []
            for name in _names:
                src = (self.dep_tree.selected_srcs.get(name)
                       or (self.udeb_dep_tree.selected_srcs.get(name)
                           if self.udeb_dep_tree is not None else None))
                if src is None:
                    console.print(f"Unknown package: {name}")
                    return
                packages.append(src)
        elif _subset == 'recommended':
            # `recommended` mode: sources whose every binary is in
            # extras_pkg_names (Recommends-only).  Unchanged from Phase 1.
            packages = [
                self.dep_tree.selected_srcs[n]
                for n in sorted(self.dep_tree.extras_src_names)
                if n in self.dep_tree.selected_srcs
            ]
        elif _subset == 'live':
            # 'live' mode: live-exclusive sources only (sources whose every
            # binary is in live_exclusive_pkg_names — i.e. sources that
            # exist in the closure SOLELY because of live.list).  Mixed
            # sources are NOT here — they get built under 'pkg'.
            packages = [
                self.dep_tree.selected_srcs[n]
                for n in sorted(self.dep_tree.live_exclusive_src_names)
                if n in self.dep_tree.selected_srcs
            ]
        elif _subset == 'installer':
            # 'installer' mode: union of (a) the udeb closure's source set
            # (cdebconf, partman-base, hw-detect, etc. — produce udebs that
            # land in the installer ramdisk) and (b) installer-exclusive
            # deb sources (the deb-arm of installer.list — efibootmgr,
            # grub-pc-bin — needed in repo/ for grub-installer to apt-pull
            # onto the target at install time).
            _src_names_set = set(self.dep_tree.installer_exclusive_src_names)
            if self.udeb_dep_tree is not None:
                _src_names_set |= set(self.udeb_dep_tree.selected_srcs.keys())
            packages = []
            for _name in sorted(_src_names_set):
                _s = (self.dep_tree.selected_srcs.get(_name)
                      or (self.udeb_dep_tree.selected_srcs.get(_name)
                          if self.udeb_dep_tree is not None else None))
                if _s:
                    packages.append(_s)
        elif _subset == 'all':
            # 'all' mode: every selected source across both trees, no
            # exclusions — pkg + live + installer + recommended in one
            # pass.  Per-source check_build still gates skip-if-built so
            # re-running is cheap; the saving over running the four
            # subset modes back-to-back is operator convenience, not
            # work avoidance.  Shared source_hashtable means looking up
            # an overlapping name in dep_tree first dedupes naturally.
            _src_names_set = set(self.dep_tree.selected_srcs.keys())
            if self.udeb_dep_tree is not None:
                _src_names_set |= set(self.udeb_dep_tree.selected_srcs.keys())
            packages = []
            for _name in sorted(_src_names_set):
                _s = (self.dep_tree.selected_srcs.get(_name)
                      or (self.udeb_dep_tree.selected_srcs.get(_name)
                          if self.udeb_dep_tree is not None else None))
                if _s:
                    packages.append(_s)
        elif _subset == 'indl':
            # 'indl' subset: every source in selected_srcs.  In build
            # mode, selected_srcs IS the build_pkg.list contents, so this is
            # "build all of build_pkg.list".  udeb_dep_tree is None in build
            # mode, so no udeb branch.  Same shape as 'all' but with the
            # operator-visible "indl" label.
            packages = [
                _s for _name, _s in sorted(
                    self.dep_tree.selected_srcs.items())
            ]
        else:
            # subset == 'pkg' (the new bare-`source build` default).
            # Build the pkg.list closure ONLY: selected_srcs minus
            # everything credited to live/installer/extras.  Result is a
            # source set whose binaries are exactly what pkg.list pulls in
            # (with required+important folded in) — the "user choices"
            # layer.
            _exclude = (self.dep_tree.live_exclusive_src_names |
                        self.dep_tree.installer_exclusive_src_names |
                        self.dep_tree.extras_src_names)
            packages = [
                _s for _name, _s in self.dep_tree.selected_srcs.items()
                if _name not in _exclude
            ]

        if not packages:
            console.print("No source packages to build")
            return

        # Tunneled and locally-built successes are tracked separately so the
        # autorun summary can report them as distinct categories.
        _built = _tunneled = _failed = _skipped = 0
        _total = len(packages)
        # Per-package label (just the pkg name, fixed-width); the
        # (A/B) count is conveyed by {value}/{total} on the bar, so
        # don't duplicate it in the label.  show_rate=False — per-
        # package source-build time varies enormously (firefox: 90min,
        # libfoo: 2s), so an avg pkg/s rate is misleading noise.
        progress_bar = ProgressBar(
            label='Source Build',
            label_width=24,
            maxvalue=_total,
            show_rate=False,
        )

        # bump-aware build: when a ledger is loaded (only
        # `_do_update_build` does so), un-forced builds additionally rebuild a
        # same-base security/NMU re-spin whose THIS-generation +asg<R>u<N>
        # artifact is missing — the one case the filename-based skip gate can't
        # see (the rebuilt pristine name collides with the prior build).  N is
        # per-file; the post-build stamp (buildcontainer) applies it.
        # _bump_active forces same-base re-spin rebuilds (the predicate
        # at _needs_bump_build checks if THIS-generation `+asg<R>u<N>`
        # artifacts are on disk for NMU-suffixed sources).  Gated on
        # _in_update_build so it ONLY fires inside _do_update_build's
        # snapshot-delta workflow.  Outside update mode, the ledger may
        # be loaded for post-build stamping (lineage continuation), but
        # bump-target detection must stay off — otherwise every NMU-
        # versioned upstream source (3/4 of the corpus) gets flagged on
        # every cmd_source_build call.
        _bump_active = (self.container is not None
                        and getattr(self.container, 'asg_ledger', None) is not None
                        and self._in_update_build)
        _bump_release = None
        if _bump_active:
            try:
                _bump_release = int(
                    str(self.config.build_version).strip('"').strip("'"))
            except (TypeError, ValueError):
                _bump_active = False   # can't derive N → stamper skips too

        # split the work into tunneled (serial — network-
        # bound, dest-dir-locked) and to-build (parallelisable).  The
        # serial path falls through to MaxParallelBuilds==1 below.
        _tunnel_pkgs = [
            _p for _p in packages
            if _p.package in self.config.tunnel_packages
        ]
        _build_pkgs = [
            _p for _p in packages
            if _p.package not in self.config.tunnel_packages
        ]

        # Tunnel sub-phase: always serial (network I/O + apt repo lock).
        for _src_pkg in _tunnel_pkgs:
            progress_bar.label(_src_pkg.package)
            _result, _ = self._build_one_source(
                _src_pkg, _force, _bump_active, _bump_release,
                _profile_override)
            if _result == 'built':
                _built += 1
            elif _result == 'tunneled':
                _tunneled += 1
            elif _result == 'failed':
                _failed += 1
            else:
                _skipped += 1
            progress_bar.step(1)

        # Build sub-phase: serial when MaxParallelBuilds == 1 (backward-
        # compat verbatim with the pre-Phase-4 loop), else
        # ThreadPoolExecutor with N workers + scoped SIGINT handler that
        # calls self.container.request_shutdown() (Phase 3) — force-
        # removing in-flight containers unblocks workers from
        # container.wait/logs so the executor drains in ~100ms instead
        # of waiting for 90-minute builds to finish.
        _n_parallel = self.config.max_parallel_builds
        if _n_parallel <= 1:
            for _src_pkg in _build_pkgs:
                progress_bar.label(_src_pkg.package)
                _result, _ = self._build_one_source(
                    _src_pkg, _force, _bump_active, _bump_release,
                    _profile_override)
                if _result == 'built':
                    _built += 1
                elif _result == 'tunneled':
                    _tunneled += 1
                elif _result == 'failed':
                    _failed += 1
                else:
                    _skipped += 1
                progress_bar.step(1)
        else:
            progress_bar.label(f'Source Build ({_n_parallel} workers)')
            import concurrent.futures as _cf
            import signal as _signal
            # Scoped SIGINT handler: re-installed for the executor block
            # only.  On Ctrl+C, signal request_shutdown() on the build
            # container (sets shutdown_event + reaps live containers);
            # the next wait() yield will then see futures
            # complete-with-False and the loop breaks via shutdown_event.
            #
            # signal.signal() can ONLY be called from the main thread.
            # In TUI mode cmd_source_build runs on the shell worker
            # thread (tui/tui.py:_shell), so the call raises ValueError.
            # Best-effort install: skip the scoped hook off the main
            # thread; workers can't be SIGINT-aborted there, but
            # executor.shutdown(wait=True, cancel_futures=True) in
            # finally still drains on command exit.
            def _on_sigint(_sig, _frame):
                logger.warning(
                    "SIGINT received during parallel source_build — "
                    "reaping live containers and shutting down workers")
                self.container.request_shutdown()
            _old_sigint = None
            try:
                _old_sigint = _signal.signal(_signal.SIGINT, _on_sigint)
            except ValueError:
                logger.warning(
                    "cmd_source_build: not running on the main thread — "
                    "SIGINT-driven container reap unavailable; in-flight "
                    "workers will run to completion if Ctrl+C fires")
            try:
                _executor = _cf.ThreadPoolExecutor(max_workers=_n_parallel)
                # heavy-package scheduler.  Sources in
                # config.heavy_packages run alone — they only start once
                # every in-flight build has drained, and while they
                # execute no new builds are submitted.  Empty set =
                # behaviour identical to a vanilla "submit all + drain"
                # pool (the scheduler degenerates to fill-to-N + wait).
                _heavy = self.config.heavy_packages
                _pending = list(_build_pkgs)              # FIFO
                _in_flight: 'dict' = {}                   # Future -> src_pkg

                def _heavy_active() -> bool:
                    return any(_p.package in _heavy
                               for _p in _in_flight.values())

                def _can_submit_next() -> bool:
                    if not _pending:
                        return False
                    # A heavy build in flight blocks every new submission
                    # (drain-before-resume contract).
                    if _heavy_active():
                        return False
                    _nxt = _pending[0]
                    # A heavy build must wait until the in-flight pool
                    # has fully drained before starting.
                    if _nxt.package in _heavy and _in_flight:
                        return False
                    return len(_in_flight) < _n_parallel
                try:
                    _break = False
                    while (_pending or _in_flight) and not _break:
                        while _can_submit_next():
                            _nxt = _pending.pop(0)
                            _fut = _executor.submit(
                                self._build_one_source,
                                _nxt, _force, _bump_active, _bump_release,
                                _profile_override)
                            _in_flight[_fut] = _nxt
                        if not _in_flight:
                            # Pending only — but _can_submit_next is False.
                            # The only way that happens with empty in-flight
                            # is shutdown_event mid-loop; break out.
                            break
                        _done, _ = _cf.wait(
                            list(_in_flight.keys()),
                            return_when=_cf.FIRST_COMPLETED)
                        for _fut in _done:
                            _pkg = _in_flight.pop(_fut)
                            try:
                                _result, _ = _fut.result()
                            except Exception as _e:    # noqa: BLE001
                                logger.error(
                                    f"worker for {_pkg.package} raised: {_e}")
                                _result = 'failed'
                            if _result == 'built':
                                _built += 1
                            elif _result == 'tunneled':
                                _tunneled += 1
                            elif _result == 'failed':
                                _failed += 1
                            else:
                                _skipped += 1
                            progress_bar.step(1)
                            if self.container.shutdown_event.is_set():
                                logger.warning(
                                    "shutdown_event set — cancelling "
                                    "remaining queued builds")
                                _break = True
                                break
                finally:
                    # wait=True: workers must drain their finally blocks
                    # (container reap, scratch-dir rmtree); reap already
                    # unblocked their I/O so drain is ~100ms.
                    # cancel_futures=True: queued-but-not-started futures
                    # are dropped so the pool doesn't pull them on shutdown.
                    _executor.shutdown(wait=True, cancel_futures=True)
            finally:
                if _old_sigint is not None:
                    _signal.signal(_signal.SIGINT, _old_sigint)

        progress_bar.close(persist=True)

        # Persist the counts so the autorun summary can read them
        # later.  This is overwritten on every source_build invocation.
        self.last_source_build_counts = {
            'built':    _built,
            'tunneled': _tunneled,
            'failed':   _failed,
            'skipped':  _skipped,
            'total':    _total,
        }

        console.print(
            f"Source build complete: {_built} built, {_tunneled} tunneled, "
            f"{_failed} failed, {_skipped} skipped"
        )
        if _failed > 0:
            logger.error(f"{_failed} source build(s) failed")
            _resp = Prompt(
                PROMPT_YESNO,
                "There are source build failures, Proceed?",
                informational=True,   # UX-05f
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        # Audit-vs-build divergence gate.  After every source build the
        # in-scope set should now classify (per `_source_state`) as ok /
        # tunneled / no_pkgs / fail — the legitimate terminal states.
        # If any classify as needs_build / stale_pass / interrupted /
        # needs_sync while the build record DOESN'T say 'fail', the
        # build's check_build skip-gate and audit's classifier
        # disagree about whether the source is done.  That's the bug
        # class behind the firefox-esr "audit clean, build re-fires"
        # incident; this gate makes any future drift loud.
        _divergences = self._detect_build_audit_divergence(packages)
        if _divergences:
            logger.error(
                f"source build / audit divergence: {len(_divergences)} "
                f"source(s) where build claimed done but audit says "
                f"rebuild needed")
            console.print(
                f"AUDIT DIVERGENCE: {len(_divergences)} source(s) where "
                f"the build pass thought it was done but `source audit` "
                f"disagrees.  The skip-gate (check_build / "
                f"find_matching_artifact) and the audit classifier "
                f"(_source_state) are out of sync, OR an on-disk file "
                f"was wiped between build and the gate.  First 10:",
                tui.COLOR_ERROR)
            for _pkg_n, _audit_said, _record_said in _divergences[:10]:
                console.print(
                    f"  {_pkg_n}: audit={_audit_said}  "
                    f"record={_record_said}",
                    tui.COLOR_ERROR)
            if len(_divergences) > 10:
                console.print(
                    f"  ... and {len(_divergences) - 10} more — run "
                    f"`source audit verbose` for the full list",
                    tui.COLOR_ERROR)
            console.print(
                "  source_build_ready is NOT set; investigate via "
                "`source audit` + `source repair` before chroot/iso build.",
                tui.COLOR_ERROR)
            return

        self.flags.source_build_ready = True

