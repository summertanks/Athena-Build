"""Virtual build — static prediction of the build pipeline.

`virtual build` simulates cache parse -> source build without running
containers, and `virtual validate` reconciles the predicted per-source
binary set against the recorded build outputs (the deterministic
build_config_divergence check).  Extracted verbatim from build.py's
BuildSession; see commands/base.py for how the mixin shares session state.
"""
import logging
import os

import tui
import utils
from buildlog import BuildLog
from tui import console

from commands.base import SessionState

logger = logging.getLogger('athena.build')


class VirtualCommandsMixin(SessionState):
    def cmd_virtual(self, action: str = '', *args):
        """Virtual build pipeline — static simulation of cache parse →
        source build → repo + publish audit, without running any
        builds.  Sub-actions:

          build [all|indl|<src>...]   run virtual pipeline on scope
          run                          alias for `virtual build`
          validate [<src>...]          compare synthesizer vs the
                                       on-disk build.json output_hashes
                                       (self-test: surfaces synth bugs
                                       before they show up in audit)
        """
        if action in ('build', 'run', ''):
            return self.cmd_virtual_build(*args)
        if action == 'validate':
            return self.cmd_virtual_validate(*args)
        _table = {
            'build [scope]':     "virtual pipeline; scope = all|indl|<src>...",
            'run':               "alias for `virtual build`",
            'validate [<src>...]': "compare synthesizer vs real build.json output_hashes",
        }
        return self._group_help('virtual', _table, action)

    def cmd_virtual_validate(self, *args):
        """Self-test: run the synthesizer against every source we have
        a successful build.json for, compare predicted filenames
        against the real ``output_hashes`` keys, report drift.

        Use this after a successful real build to confirm the
        synthesizer's predictions match reality.  Catches:
          - asg-stamp math wrong (version drift)
          - upstream-version lookup wrong (metapackage case)
          - Build-Profile or arch filtering wrong (extra/missing files)

        Scope: explicit source names, else every source in
        dep_tree.selected_srcs ∪ udeb's.
        """
        if self.cache is None or self.dep_tree is None:
            console.print(
                "virtual validate: cache + dep_tree must be populated; "
                "run `cache build` + `cache parse`.", tui.COLOR_ERROR)
            return False
        import virtual_build as _vb
        _selected_srcs: dict = dict(
            getattr(self.dep_tree, 'selected_srcs', {}) or {})
        if self.udeb_dep_tree is not None:
            for _n, _s in (getattr(self.udeb_dep_tree, 'selected_srcs', {})
                           or {}).items():
                _selected_srcs.setdefault(_n, _s)
        _scope = list(args) if args else sorted(_selected_srcs.keys())
        if not _scope:
            console.print(
                "virtual validate: scope is empty.", tui.COLOR_WARNING)
            return True
        try:
            _release = int(str(self.config.build_version)
                           .strip('"').strip("'"))
        except (TypeError, ValueError):
            _release = 1
        _universe = _vb.from_cache(self.cache)
        # Canonical-source map: binary_name -> upstream `Source:` (the REAL
        # producer), built from the deb + udeb indices.  Lets validate
        # attribute on-disk emissions to their true source rather than to
        # whichever source declares them in `Binary:` — fixes the
        # linux / linux-signed-amd64 installer-udeb attribution split.
        import apt_pkg as _ap
        # Build from the from_cache-FILTERED _universe (standalone Package==name
        # producer), NOT the raw package/udeb hashtables.  The raw walk took
        # _rec[0] and the apt-highest version — for a Provides-aliased name like
        # `telnet` that grabs the epoch-bearing alias inetutils-telnet (2:...)
        # over the standalone telnet (0.17...) and misattributes the binary to
        # the alias's Source.  Synth (synthesize_source_binaries) derives canon
        # from this SAME filtered universe, so validate must too or its canon
        # diverges from synth's.  _universe values are {ver: record} (single
        # record per the from_cache filter), so no _rec[0] indexing.
        _canon_map: 'dict[str, str]' = {}
        for _bn, _vers in _universe.items():
            _best_v = _best_r = None
            for _v, _rec in (_vers.items()
                             if hasattr(_vers, 'items') else []):
                if (_best_v is None
                        or _ap.version_compare(str(_v), str(_best_v)) > 0):
                    _best_v, _best_r = _v, _rec
            if _best_r is not None:
                _canon_map[_bn] = (
                    (_best_r.get('Source') or _bn).split(' ', 1)[0])
        _tunnel_srcs = frozenset(
            getattr(self.config, 'tunnel_packages', set()) or set())

        def _lookup(_name: str):
            _src = _selected_srcs.get(_name)
            if _src is not None:
                return _src
            _candidates = (getattr(self.cache, 'source_hashtable', {})
                           .get(_name, []))
            return _candidates[0] if _candidates else None

        _buildlog = os.path.join(self.config.dir_log, 'build')
        _profiles_set = sorted(getattr(
            self.config, 'build_profiles', set()) or set())
        console.print(
            f"virtual validate: scope={len(_scope)} source(s)  "
            f"arch={self.config.arch}  release={_release}",
            tui.COLOR_HIGHLIGHT)
        console.print(
            f"  build_profiles: {','.join(_profiles_set) or '(none)'}",
            tui.COLOR_INFO)
        _stats, _findings = _vb.validate_against_build_records(
            source_names=_scope, source_lookup=_lookup,
            package_universe=_universe,
            release=_release, arch=self.config.arch,
            buildlog_dir=_buildlog,
            active_profiles=frozenset(
                getattr(self.config, 'build_profiles', frozenset())),
            repo_dir=self.config.dir_repo,
            fork_dsc_dir=getattr(self.config, 'dir_fork_source_repo', None),
            canonical_src_map=_canon_map,
            tunnel_sources=_tunnel_srcs,
        )
        console.print(
            f"  checked={_stats['sources_checked']}  "
            f"matched={_stats['sources_matched']}  "
            f"build_config_divergence={_stats.get('buildcfg_sources', 0)}  "
            f"drifted={_stats['sources_drifted']}",
            tui.COLOR_INFO)
        for _sev, _kind, _msg in _findings[:50]:
            _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                      else tui.COLOR_INFO if _sev == 'INFO'
                      else tui.COLOR_WARNING)
            console.print(f"  {_sev:8s}  {_kind}: {_msg}", _color)
            # Per-source breakdown for the build-config divergence: list each
            # source and the declared-but-not-built binaries under it.
            if _kind == 'virtual_validate_build_config_divergence':
                _detail = _stats.get('buildcfg_detail', {}) or {}
                for _i, _s in enumerate(sorted(_detail)):
                    if _i:
                        console.print("")
                    console.print(f"            {_s}", _color)
                    for _b in _detail[_s]:
                        console.print(f"              not built: {_b}", _color)
        if len(_findings) > 50:
            console.print(
                f"  …and {len(_findings) - 50} more findings",
                tui.COLOR_WARNING)
        _crit = any(_f[0] == 'CRITICAL' for _f in _findings)
        if _crit:
            console.print(
                "virtual validate: SYNTHESIZER DRIFT — see version_drift "
                "findings.", tui.COLOR_ERROR)
            return False
        if _stats['sources_drifted']:
            console.print(
                "virtual validate: completed with WARNING drift "
                "(filename set diffs, not version drift).",
                tui.COLOR_WARNING)
        else:
            console.print(
                "virtual validate: PASS — synthesizer predictions match "
                "real build artifacts.", tui.COLOR_INFO)
        return True

    def _write_virtual_buildlog(self, name, src, records, arch, release):
        """Companion: write `log/build/<pkg>.vbuildlog` — the virtual
        build's PREDICTED artifact set for one source, formatted to sit
        alongside (and diff against) the real `<pkg>.buildlog` an actual
        `source build` produces.

        Sections: EXPECTED (declared Package-List), PREDICTED ARTIFACTS
        (synthesized filenames = name_version_arch.ext), and FILTERED
        (declared binaries the synthesizer dropped via arch/profile/
        canonical-source gates).  Best-effort — never raises into the
        virtual-build flow.
        """
        try:
            _blog = BuildLog(
                os.path.join(self.config.dir_log, 'build'),
                name, kind='virtual', suffix='.vbuildlog')
            _declared = sorted(getattr(src, 'binary', []) or [])
            _predicted = sorted(
                os.path.basename(_r.get('Filename', '') or '')
                for _r in records if _r.get('Filename'))
            _pred_names = {_f.split('_', 1)[0] for _f in _predicted}
            _blog.header(
                intended_version=str(getattr(src, 'version', '')),
                arch=arch, release=release,
                profiles=' '.join(sorted(
                    getattr(self.config, 'build_profiles', frozenset())))
                or '(none)')
            _blog.section(
                f"EXPECTED (Package-List declared: {len(_declared)})")
            if _declared:
                for _b in _declared:
                    _blog.bullet(_b)
            else:
                _blog.empty('(no Binary: list)')
            _blog.section(f"PREDICTED ARTIFACTS ({len(_predicted)})")
            if _predicted:
                for _f in _predicted:
                    _blog.file(_f)
            else:
                _blog.empty()
            _filtered = sorted(set(_declared) - _pred_names)
            _blog.section(
                f"FILTERED (declared but not predicted: {len(_filtered)})")
            _blog.bullet(', '.join(_filtered) if _filtered else '(none)')
            _blog.footer(
                predicted=len(_predicted), declared=len(_declared))
            _blog.write()
        except Exception as _e:
            logger.warning(f"virtual buildlog {name}: {_e}")

    def cmd_virtual_build(self, *args):
        """Run the virtual build pipeline for `scope` and report findings.

        Scope:
          (no arg) / `all`           every source in dep_tree.selected_srcs
                                     (∪ udeb_dep_tree's)
          `indl`                     names from config/build_pkg.list
                                     (mirrors `source build indl`)
          <src1> <src2> ...          explicit source names

        Phases (mirrors the real pipeline):
          1. cache parse  — REAL (operator-interactive).  We just read
             the already-populated cache + dep_tree.
          2. source sync  — virtual (trust cache.source_hashtable)
          3. source build — virtual (per-binary transpose
             + upstream-inherit + sibling pin rewrite)
          4. source audit — covered implicitly: every selected source
             must have a synthesizable binary set
          5. repo audit   — REAL audit_dep_closure + audit_conflict_cohort
             against synthetic RepoState
          6. publish dry-run — REAL detect_hash_conflicts +
             project_owners + ownership rule check; remote state used
             when available from the last `mirror pull`

        Findings printed per-phase with the same severity convention as
        `mirror audit`.  Returns True when no CRITICAL was emitted.

        Substvar caveat: virtual build inherits upstream binary
        `Depends:` verbatim.  Fork patches that change linked sonames
        are a BLIND SPOT — see docs/virtual-build.md.
        """
        if self.cache is None:
            console.print(
                "virtual build: cache not parsed yet — run `cache build` "
                "+ `cache parse` first.", tui.COLOR_ERROR)
            return False
        if self.dep_tree is None:
            console.print(
                "virtual build: dep_tree not populated — `cache parse` "
                "must complete first.", tui.COLOR_ERROR)
            return False
        import virtual_build as _vb
        import mirror as _mirror
        import coord.identity as _id
        import coord.store as _store

        # ---- Scope resolution -----------------------------------------
        _selected_srcs: dict = dict(
            getattr(self.dep_tree, 'selected_srcs', {}) or {})
        if self.udeb_dep_tree is not None:
            for _n, _s in (getattr(self.udeb_dep_tree, 'selected_srcs', {})
                           or {}).items():
                _selected_srcs.setdefault(_n, _s)
        if not args or args[0] == 'all':
            _scope_names = sorted(_selected_srcs.keys())
            _scope_label = 'all'
        elif args[0] == 'indl':
            _scope_names = utils.parse_build_pkg_list(
                getattr(self.config, 'build_pkg_list_path', '') or '')
            _scope_label = f'indl ({len(_scope_names)} pkg)'
        else:
            _scope_names = list(args)
            _scope_label = f'{len(_scope_names)} explicit src(s)'
        if not _scope_names:
            console.print(
                "virtual build: scope is empty — nothing to simulate.",
                tui.COLOR_WARNING)
            return True

        # ---- Release + asg ledger (real disk state) -------------------
        try:
            _release = int(str(self.config.build_version)
                           .strip('"').strip("'"))
        except (TypeError, ValueError):
            _release = 1
        _universe = _vb.from_cache(self.cache)
        _arch = self.config.arch

        # ---- Header ---------------------------------------------------
        console.print(
            f"virtual build: {_scope_label}  arch={_arch}  "
            f"release={_release}", tui.COLOR_HIGHLIGHT)
        console.print(
            "  pre-build prediction; cache parse decides scope",
            tui.COLOR_INFO)

        # ---- Phase: synthesize binary records -------------------------
        _records: 'list[dict]' = []
        _missing_srcs: 'list[str]' = []
        for _name in _scope_names:
            _src = _selected_srcs.get(_name)
            if _src is None:
                # Try cache.source_hashtable for names not in selection
                # (operator-explicit scope can reach beyond dep_tree).
                _candidates = (getattr(self.cache, 'source_hashtable', {})
                               .get(_name, []))
                _src = _candidates[0] if _candidates else None
            if _src is None:
                _missing_srcs.append(_name)
                continue
            _was_patched = bool(getattr(_src, 'patch_list', None))
            _src_records = _vb.synthesize_source_binaries(
                source=_src, package_universe=_universe,
                release=_release,
                arch=_arch, was_patched=_was_patched,
                peer_sources=set(_scope_names),
                active_profiles=frozenset(
                    getattr(self.config, 'build_profiles', frozenset())),
                fork_dsc_dir=getattr(
                    self.config, 'dir_fork_source_repo', None),
            )
            _records.extend(_src_records)
            # companion: persist the PREDICTED artifact set as
            # log/build/<pkg>.vbuildlog — the reference to diff against the
            # real <pkg>.buildlog after an actual source build.
            self._write_virtual_buildlog(
                _name, _src, _src_records, _arch, _release)
        if _missing_srcs:
            console.print(
                f"  WARNING  {len(_missing_srcs)} source(s) not in cache: "
                f"{', '.join(_missing_srcs[:5])}"
                + (f" +{len(_missing_srcs) - 5} more"
                   if len(_missing_srcs) > 5 else ''),
                tui.COLOR_WARNING)
        if not _records:
            console.print(
                "  CRITICAL  synthesized 0 binary records — nothing to "
                "audit (every source missing or empty).",
                tui.COLOR_ERROR)
            return False
        console.print(
            f"  ok        synthesized {len(_records)} virtual binary "
            f"record(s) across {len(_scope_names) - len(_missing_srcs)} "
            "source(s)", tui.COLOR_INFO)

        # ---- Phase: repo audit ----------------------------------------
        # self.dep_tree is guaranteed non-None here (the method returns early
        # above when it is None), so no guard is needed for it.
        _install_corpus: 'frozenset[str]' = frozenset(
            getattr(self.dep_tree, 'selected_pkgs', {}).keys())
        if self.udeb_dep_tree is not None:
            _install_corpus |= frozenset(
                getattr(self.udeb_dep_tree, 'selected_pkgs', {}).keys())
        _state, _audit_findings = _vb.virtual_repo_audit(
            _records, install_corpus=_install_corpus or None,
        )
        _audit_crit = [_t for _t in _audit_findings if _t[0] == 'CRITICAL']
        console.print(
            "\nvirtual repo audit:", tui.COLOR_HIGHLIGHT)
        if not _audit_findings:
            console.print(
                "  ok        synthetic closure clean", tui.COLOR_INFO)
        for _sev, _kind, _msg in _audit_findings[:25]:
            _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                      else (tui.COLOR_WARNING if _sev == 'WARNING'
                            else tui.COLOR_INFO))
            console.print(f"  {_sev:8s}  {_kind}: {_msg}", _color)
        if len(_audit_findings) > 25:
            console.print(
                f"  …and {len(_audit_findings) - 25} more findings",
                tui.COLOR_WARNING)

        # ---- Phase: publish dry-run -----------------------------------
        # Cross-mirror state — best-effort: read last-fetched sidecars
        # under cache/mirror/<name>/fetched/claims/.  Operator should
        # run `mirror pull` first for accuracy; we warn loudly when
        # nothing's there.
        _remote_by_builder: 'dict[str, list[dict]]' = {}
        _signing_home = ''
        try:
            import signing as _signing
            _signing_home = _signing.signing_home(self.config)
        except Exception:
            pass
        _mirror_names = _mirror.list_mirrors(self.config)
        for _mn in _mirror_names:
            _fetched = os.path.join(
                self.config.dir_cache, 'mirror', _mn, 'fetched')
            _claims_dir = os.path.join(_fetched, 'claims')
            if not os.path.isdir(_claims_dir):
                continue
            try:
                import coord.head as _head_mod
                _head = _head_mod.read_coord_head(_fetched, _signing_home)
                if _head is None:
                    continue
                _keyring = _id.load_keyring(
                    os.path.join(_fetched, 'keyring', 'builders'))
                # FED-03 D: trust only pubkeys bound in the tier-1-signed head.
                _keyring, _dropped, _has_bind = _id.verified_keyring_from_head(
                    _keyring, _head)
                _bmsg = _id.binding_drop_summary(_dropped, _has_bind)
                if _bmsg:
                    logger.warning(f"virtual build: mirror {_mn}: {_bmsg}")
                _revoked = _head.get('revoked_builders') or {}
                _bb = _store.read_all_claims(_claims_dir, _keyring, _revoked)
                for _bid, _cl in _bb.items():
                    _remote_by_builder.setdefault(_bid, []).extend(_cl)
            except Exception as _e:
                logger.warning(
                    f"virtual build: cannot read mirror {_mn} state: {_e}")
        # Builder id — needed for ownership decision.  If absent (rare),
        # default to a synthetic id so the merge still runs but every
        # peer claim looks foreign (max ownership-pessimism).
        try:
            _our_bid = self._coord_builder_id() or 'athena-virtual'
        except (AttributeError, OSError):
            _our_bid = 'athena-virtual'
        _snapshot = self._snapshot_current() or 'T'
        # Filenames this builder ADOPTED from a peer — the real publish
        # never re-claims them (generate_pending_claims skips pulled_from
        # / deprecated / retracted records), so the dry-run must skip them
        # too or it false-conflicts a placeholder SHA against the owner's
        # real SHA on every adopted file.  Source of truth = the same
        # build.json records the real publish walks.
        _adopted_fns: 'set[str]' = set()
        _buildlog = os.path.join(self.config.dir_log, 'build')
        try:
            for _entry in os.listdir(_buildlog):
                if not _entry.endswith('.build.json'):
                    continue
                _rec = utils.read_build_record(
                    _buildlog, _entry[:-len('.build.json')])
                if _rec is None:
                    continue
                if (_rec.get('pulled_from')
                        or _rec.get('selection') in ('deprecated', 'retracted')):
                    for _ofn in (_rec.get('outputs') or []):
                        _adopted_fns.add(str(_ofn))
        except OSError:
            pass
        console.print("\nvirtual publish dry-run:", tui.COLOR_HIGHLIGHT)
        if _adopted_fns:
            console.print(
                f"  {len(_adopted_fns)} adopted (pulled_from) file(s) "
                "excluded — a real publish would not re-claim them",
                tui.COLOR_INFO)
        if not _remote_by_builder:
            console.print(
                "  WARNING  no cached remote state — run `mirror pull` "
                "first for ownership / cross-builder checks.  Continuing "
                "with intra-our-claims hash-conflict scan only.",
                tui.COLOR_WARNING)
        _merged, _pub_findings = _vb.virtual_publish_dry_run(
            _records, our_builder_id=_our_bid, snapshot=_snapshot,
            remote_by_builder=(_remote_by_builder or None),
            local_adopted_fns=_adopted_fns,
        )
        _pub_crit = [_t for _t in _pub_findings if _t[0] == 'CRITICAL']
        if not _pub_findings:
            console.print(
                "  ok        no cross-builder conflicts; no ownership "
                "blocks", tui.COLOR_INFO)
        for _sev, _kind, _msg in _pub_findings[:25]:
            _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                      else (tui.COLOR_WARNING if _sev == 'WARNING'
                            else tui.COLOR_INFO))
            console.print(f"  {_sev:8s}  {_kind}: {_msg}", _color)
        if len(_pub_findings) > 25:
            console.print(
                f"  …and {len(_pub_findings) - 25} more findings",
                tui.COLOR_WARNING)

        # ---- Summary --------------------------------------------------
        _total_crit = len(_audit_crit) + len(_pub_crit)
        console.print("")
        if _total_crit == 0:
            console.print(
                "virtual build: PASS — pipeline projection clean.",
                tui.COLOR_INFO)
            return True
        console.print(
            f"virtual build: BLOCKED — {_total_crit} CRITICAL finding(s).",
            tui.COLOR_ERROR)
        return False
