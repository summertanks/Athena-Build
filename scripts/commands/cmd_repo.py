"""Local repo lifecycle — the `repo` / `index` / `package` command surface.

Generates apt-repo metadata under repo/dists/ (cmd_index_repo /
cmd_index_repo_minimal), and runs the repo-state fixups: repair, strip,
backfill-hashes, stale-file cleanup (cmd_repo* / cmd_strip_repo /
cmd_package_cleanup / _scan_stale_files / _superseded_binary_names) plus
the fork-reload path (cmd_reload_fork).  Extracted verbatim from build.py's
BuildSession; see commands/base.py for how the mixin shares session state.
"""
import logging
import os
import re
import shutil
import subprocess

import apt_pkg
import repo_audit
import tui
import utils
from cache import Cache
from tui import console, Prompt, PROMPT_YESNO, PROMPT_PASSWORD, Spinner, ProgressBar

from commands.base import SessionState

logger = logging.getLogger('athena.build')


class RepoCommandsMixin(SessionState):
    def cmd_index_repo(self, *args):
        """Generate apt-repo metadata IN-PLACE under repo/dists/.

        CONF-01 Stage B (2026-05-22).  Produces:
          repo/dists/<codename>/Release, InRelease, Release.gpg
          repo/dists/<codename>/main/binary-amd64/Packages*
          repo/dists/<codename>/main/debian-installer/binary-amd64/Packages*
          repo/dists/<codename>/main/source/Sources*
          repo/dists/<codename>/doc/binary-amd64/Packages*
          repo/dists/<codename>/tests/binary-amd64/Packages*
          repo/dists/<codename>-debug/Release, InRelease, Release.gpg
          repo/dists/<codename>-debug/main/binary-amd64/Packages*

        Layout matches docs/plans/conf-01-repo-layout-migration.md.

        Args: none (reads config.build_codename for the suite name).
        """
        del args   # no positional args today
        import signing
        import apt_repo

        _codename = self.config.build_codename.strip('"').strip("'")
        _suites_spec: 'dict[str, list[str]]' = {
            _codename: ['main', 'doc', 'tests',
                        'contrib', 'non-free', 'non-free-firmware'],
            f'{_codename}-debug': ['main'],
        }
        _codename_for_suite = {
            _suite: _suite for _suite in _suites_spec
        }
        _description_for_suite = {
            _codename: 'Asgard Linux',
            f'{_codename}-debug': 'Asgard Linux — debug symbols',
        }

        _password = Prompt(
            PROMPT_PASSWORD, "Enter sudo password",
        ).get_response()
        _r = subprocess.run(
            ['sudo', '-S', '-v'],
            input=_password + '\n',
            capture_output=True, text=True,
        )
        if _r.returncode != 0:
            console.print("ERROR: incorrect sudo password")
            logger.error("cmd_index_repo: sudo -v failed")
            _password = '*' * len(_password)
            return False

        try:
            _ok = apt_repo.generate_repo_indexes(
                repo_root=self.config.dir_repo,
                suites_spec=_suites_spec,
                codename_for_suite=_codename_for_suite,
                version=self.config.build_version,
                arch=self.config.arch,
                password=_password,
                signing_homedir=signing.signing_home(self.config),
                signing_pubkey_path=signing.signing_pubkey_path(self.config),
                description_for_suite=_description_for_suite,
            )
            if not _ok:
                console.print(
                    "ERROR: apt-repo index generation failed — "
                    "check log for details"
                )
                logger.error("cmd_index_repo: generate_repo_indexes returned False")
                return False
            console.print(
                f"apt-repo indexed: {self.config.dir_repo}/dists/"
                f"{{{_codename},{_codename}-debug}}/",
                tui.COLOR_HIGHLIGHT,
            )
            self._print_repo_index_summary(_codename, _suites_spec)
        finally:
            _password = '*' * len(_password)  # noqa: F841
        # mirror publish's auto-index path gates on this bool; without it
        # a fully-successful index returned None and was misread as failure.
        return True

    def _print_repo_index_summary(
        self, codename: str,
        suites_spec: 'dict[str, list[str]]',
    ) -> None:
        """Post-index summary: per-suite + per-component file counts +
        total on-disk size of dists/ + suite-level Release presence.

        Quick at-a-glance confirmation that the index landed
        completely; surfaces empty components and missing signatures
        without re-running the full audit.
        """
        _root = self.config.dir_repo
        console.print("\nrepo index summary\n")
        _total_files = 0
        _total_bytes = 0
        for _suite, _components in suites_spec.items():
            _suite_dir = os.path.join(_root, 'dists', _suite)
            if not os.path.isdir(_suite_dir):
                console.print(f"  {_suite:25s} : not generated (skipped)")
                continue
            _has_release   = os.path.isfile(os.path.join(_suite_dir, 'Release'))
            _has_rel_gpg   = os.path.isfile(os.path.join(_suite_dir, 'Release.gpg'))
            _has_inrelease = os.path.isfile(os.path.join(_suite_dir, 'InRelease'))
            _sig_marker = (
                'signed' if (_has_rel_gpg and _has_inrelease)
                else 'unsigned' if _has_release
                else 'missing'
            )
            console.print(
                f"\n  suite: {_suite}  (Release: {_sig_marker})"
            )
            for _comp in _components:
                # Walk dists/<suite>/<comp>/ for binary-*/ + source/.
                _comp_dir = os.path.join(_suite_dir, _comp)
                if not os.path.isdir(_comp_dir):
                    console.print(
                        f"    {_comp:8s} : empty (not in this suite)"
                    )
                    continue
                _subdirs = []
                for _walk_root, _dirs, _files in os.walk(_comp_dir):
                    _has_packages = any(
                        _f == 'Packages' for _f in _files
                    )
                    _has_sources = any(
                        _f == 'Sources' for _f in _files
                    )
                    if _has_packages or _has_sources:
                        _rel = os.path.relpath(_walk_root, _suite_dir)
                        _n_payload = sum(
                            1 for _f in _files
                            if _f.endswith(('.deb', '.udeb', '.dsc'))
                        )
                        _bytes_here = sum(
                            os.path.getsize(os.path.join(_walk_root, _f))
                            for _f in _files
                            if os.path.isfile(os.path.join(_walk_root, _f))
                        )
                        _kind = ('Sources' if _has_sources else 'Packages')
                        _subdirs.append((_rel, _kind, _n_payload, _bytes_here))
                        _total_files += _n_payload
                        _total_bytes += _bytes_here
                if not _subdirs:
                    console.print(
                        f"    {_comp:8s} : empty component"
                    )
                    continue
                for _rel, _kind, _n, _b in _subdirs:
                    console.print(
                        f"    {_rel:50s}  {_kind:8s} "
                        f"{_n:5d} files  {_b // (2 ** 20):5d} MB"
                    )
        console.print(
            f"\n  Total payload: {_total_files} file(s), "
            f"{_total_bytes // (2 ** 20)} MB across dists/"
        )

    def cmd_index_repo_minimal(self, *args):
        """UPD-02: STAGE the minimal (runtime) subset into publish/ in the SAME
        nested layout as the full repo — so full and minimal are structurally
        identical and a single destination-side `dpkg-scanpackages` pass
        handles both with no clobber.

        Minimal = the main-component binary .debs a booted system apt-installs,
        MINUS debug/source (apt_repo.deb_excluded_from_minimal); no udebs,
        source, or debug suite.  NO index/sign here — `mirror publish`
        rebuilds the index ON THE REMOTE (dpkg-scanpackages run over the
        landed pool).

          publish/dists/<codename>/main/binary-<arch>/<subset>.deb

        Returns True on success.  Standalone command; staging for a
        minimal publish workflow.
        """
        del args
        import apt_repo
        _src_dir = self.config.dir_repo_main   # dists/<cn>/main/binary-<arch>/
        if not os.path.isdir(_src_dir):
            console.print(
                f"repo index minimal: no built debs at {_src_dir} — run "
                f"`source build` first")
            return False
        _rel = os.path.relpath(_src_dir, self.config.dir_repo)
        _dst_dir = os.path.join(self.config.dir_publish, _rel)
        _dists = os.path.join(self.config.dir_publish, 'dists')
        if os.path.isdir(_dists):
            shutil.rmtree(_dists)
        os.makedirs(_dst_dir, exist_ok=True)

        _copied = _skipped = 0
        for _f in sorted(os.listdir(_src_dir)):
            if not (_f.endswith(('.deb', '.udeb'))):
                continue
            if apt_repo.deb_excluded_from_minimal(_f):
                _skipped += 1
                continue
            shutil.copy2(os.path.join(_src_dir, _f),
                         os.path.join(_dst_dir, _f))
            _copied += 1
        if _copied == 0:
            console.print(
                f"repo index minimal: no runtime debs in {_src_dir} "
                f"({_skipped} debug/source excluded)")
            return False
        console.print(
            f"repo index minimal: staged {_copied} runtime deb(s) → "
            f"publish/{_rel} ({_skipped} debug/source excluded; "
            f"index built on the remote at publish time)")
        return True

    def cmd_repo(self, action: str = '', *args):
        """Dispatcher for `repo <action>` — LOCAL .deb pool lifecycle.

        Remote-endpoint state (publish, audit-remote, federation
        membership) lives under `mirror`.  Source-producing operations
        (sync, build, tunnel) live under `source`.  MIRROR-01 Phase 8
        rationalised the surface:

          - `tunnel` moved to `source tunnel` (endpoint is a built .deb)
          - `index` is no longer operator-visible — `chroot build` and
            `mirror publish` auto-index when InRelease is missing; the
            handlers stay callable internally (and via
            `repo repair refresh`).  This avoids an operator decision
            point with no real user-facing output.

        `repo` now exposes: audit + repair.
        """
        _table = {
            'audit':          'pre-ship gate: dep + conflict + stale-files + '
                              'content integrity + NMU residue.  Pass `quick` '
                              'to skip the slow (~30s) integrity scan.  Pass '
                              'a target name to drill in: `repo audit lsb-base`.',
            'repair':         'umbrella for repo-state fixups: strip, cleanup, '
                              'backfill-hashes',
        }
        if action == 'tunnel':
            console.print(
                "`repo tunnel` moved to `source tunnel` in MIRROR-01 "
                "Phase 8 (tunnel's endpoint is a built .deb — same "
                "shape as source build).  Use `source tunnel [pkg…]`.",
                tui.COLOR_WARNING)
            return False
        if action == 'index':
            console.print(
                "`repo index` is no longer operator-visible (MIRROR-01 "
                "Phase 8) — `chroot build` and `mirror publish` "
                "auto-index when needed.  Use `repo repair` if you "
                "suspect a stale index.",
                tui.COLOR_WARNING)
            return False
        if action == 'audit':
            return self.cmd_audit(*args)
        if action == 'repair':
            return self.cmd_repo_repair(*args)
        return self._group_help('repo', _table, action)

    def cmd_repo_repair(self, action: str = '', *args):
        """Umbrella for repo-state fixups.  Each sub-action mutates
        repo/ to bring it into a clean state:

          strip   — one-time NMU/binNMU/backport suffix backfill.  For
                    every .deb/.udeb in repo/ whose Version or dep
                    constraints carry an NMU layer, rewrite to pristine.
                    Future fresh builds get stripped automatically
                    post-`dpkg-buildpackage` so this is the corpus-fixup
                    path for .debs that arrived via another route
                    (manual ingest, pre-strip-policy builds).
          cleanup — delete obsolete .debs/.udebs (orphan source / version
                    drift).  Dry-run by default; pass `force` to delete.
        """
        _table = {
            'strip':           'NMU-suffix backfill across repo/',
            'cleanup':         'delete obsolete .debs/.udebs (dry-run by default; '
                               'pass `force` to actually delete)',
            'backfill-hashes': 'COORD-01: walk build.json records, hash any '
                               'emitted .deb/.udeb missing output_hashes, bump '
                               'schema_version v1→v2.  Idempotent.',
        }
        if action == 'strip':
            return self.cmd_strip_repo(*args)
        if action == 'cleanup':
            return self.cmd_package_cleanup(*args)
        if action == 'backfill-hashes':
            return self.cmd_repo_backfill_hashes(*args)
        return self._group_help('repo repair', _table, action)

    def cmd_repo_backfill_hashes(self, *args):
        """COORD-01 one-shot: walk cache/log/build/*.build.json and add
        output_hashes (SHA-256 over each emitted .deb/.udeb basename
        located under repo/) for records still on schema v1.

        Usage: repo repair backfill-hashes

        Idempotent — already-v2 records with hashes for every output
        are skipped.  Files referenced by an output but absent from
        repo/ are reported in stats; the record's schema is still
        bumped so a future read doesn't keep retrying.
        """
        del args
        _stats = utils.backfill_output_hashes(
            self.container.buildlog_path
            if self.container is not None
            else os.path.join(self.config.dir_log, 'build'),
            self.config.dir_repo,
        )
        console.print(
            f"backfill-hashes: scanned={_stats['scanned']} "
            f"upgraded={_stats['upgraded']} skipped={_stats['skipped']} "
            f"missing_files={_stats['missing_files']}")
        return True

    def cmd_strip_repo(self, *args):
        """One-time backfill: strip NMU suffix from every .deb/.udeb
        in repo/.

        Future fresh builds get stripped automatically by BuildContainer
        post-build; this command exists for the existing corpus and
        for any .deb that arrived in repo/ via a path that bypasses
        BuildContainer (manual copy, ingestion, etc.).

        Usage: package strip [force]
          force — skip the PROMPT_YESNO confirmation
        """
        _force = 'force' in args
        # Post-segregation: .debs live in dists/<codename>/<comp>/
        # binary-<arch>/ (CONF-01 Stage D unified layout); udebs in
        # main/debian-installer/binary-<arch>/; dbgsyms in
        # dists/<codename>-debug/main/binary-<arch>/.  Walk all of
        # them so strip catches every tier.
        _repo = self.config.dir_repo
        # Tunneled packages must NOT be stripped — they're pristine Debian
        # binary passthrough and rewriting their control/filename would
        # invalidate any embedded signature (shim-signed et al. carry
        # Microsoft Secure Boot signatures).  Compute the set of binary
        # NAMES whose source is tunneled, then skip them in the loop below.
        _tunnel_bins: 'set[str]' = set()
        _tun = set(self.config.tunnel_packages)
        if _tun and self.flags.cache_ready and self.cache is not None:
            for _bin_name, _pkg in self.cache.package_hashtable.items():
                _src_field = (_pkg.get('Source') or _bin_name).strip()
                _src_name = _src_field.split(' ', 1)[0]
                if _src_name in _tun:
                    _tunnel_bins.add(_bin_name)
        elif _tun:
            # Cache not loaded: fall back to source-name match (only catches
            # binaries whose name equals their source, e.g. intel-microcode).
            # Log so the operator knows broader-set skips weren't possible.
            logger.warning(
                "strip_repo: cache not ready — tunnel-skip falls back to "
                "source-name match only (run `cache parse` for full coverage)"
            )
            _tunnel_bins = set(_tun)
        _files: 'list[str]' = []
        for _deb_dir in self.config.all_deb_dirs():
            try:
                for _f in os.listdir(_deb_dir):
                    if _f.endswith(('.deb', '.udeb')):
                        _files.append(os.path.join(_deb_dir, _f))
            except OSError:
                continue
        _files.sort()
        if not _files:
            console.print(
                f"{_repo} (dists/*/<comp>/binary-*/) has no "
                f".deb/.udeb files — nothing to do"
            )
            return
        console.print(
            f"Found {len(_files)} package(s) under {_repo}/<subdir>.  "
            f"Strip walks each, rewriting only those with NMU residue."
        )
        if not _force:
            _resp = Prompt(
                PROMPT_YESNO,
                f"Strip NMU suffix from {len(_files)} .deb(s)?  "
                f"Repacks each affected file.",
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                console.print("Aborted")
                return

        _rewritten = _unchanged = _failed = _tunneled_skipped = 0
        _total_strips = 0
        _bar = ProgressBar(
            label='Strip NMU', maxvalue=len(_files), show_rate=False,
        )
        for _path in _files:
            _bar.step(1)
            _f = os.path.basename(_path)
            # Skip pristine tunneled passthrough — rewriting would corrupt
            # any embedded signature (Microsoft Secure Boot on shim-signed,
            # for one).  The suffix is intentional, not residue.
            _bin_name = _f.split('_', 1)[0]
            if _bin_name in _tunnel_bins:
                _tunneled_skipped += 1
                continue
            try:
                _r = utils.strip_nmu_from_deb(_path)
                if _r['status'] == 'rewritten':
                    _rewritten += 1
                    _total_strips += _r['strips_count']
                    if _r['new_path'] != _path:
                        logger.info(
                            f"strip_nmu: {_f} → "
                            f"{os.path.basename(_r['new_path'])}"
                        )
                elif _r['status'] == 'unchanged':
                    _unchanged += 1
                else:
                    # status is 'malformed' or 'skipped'; surface the
                    # filename + reason so the operator can decide
                    # whether to rebuild or accept (otherwise "1 failed"
                    # is opaque and the operator has to grep the repo
                    # to find the culprit).
                    _failed += 1
                    logger.warning(
                        f"strip_nmu: {_f} skipped (status={_r['status']})"
                    )
            except Exception as e:
                logger.error(f"strip_nmu: {_f} failed: {e}")
                _failed += 1
                console.print(f"FAIL: {_f} — {e}")
        _bar.close()

        # Filenames + Versions just shifted; the cached Packages
        # snapshot in dir_temp is now stale.
        repo_audit.invalidate_cache(self.config.dir_repo)

        _tun_tail = (f", {_tunneled_skipped} tunneled (preserved)"
                     if _tunneled_skipped else "")
        console.print(
            f"Strip complete: {_rewritten} rewritten, "
            f"{_unchanged} unchanged, {_failed} failed{_tun_tail}.  "
            f"{_total_strips} suffix(es) stripped in total.  "
            f"Run `repo audit_nmu` to confirm zero residue."
        )

    def _superseded_binary_names(self) -> 'set[str]':
        """Binary names a SELECTED FORK supersedes via Conflicts/Replaces
        (e.g. athena-setup-udeb Conflicts apt-setup-udeb), EXCLUDING names that
        are themselves selected (so genuine pool mutual-exclusions like
        grub-pc/grub-efi-amd64 are kept — both are selected pool extras).

        d-i's anna/udpkg IGNORE Conflicts, so a rename fork's superseded
        upstream binary (apt-setup-udeb) would otherwise ship + run alongside
        the fork — the security.debian.org install bug.  Used to drop such
        binaries from the cleanup AND to exclude them from the installer pool.

        ONLY fork packages are scanned.  A normal upstream package's
        Conflicts/Replaces is ordinary Debian metadata, NOT a supersession —
        e.g. `usrmerge Conflicts: cryptsetup` (usr-merge transition),
        `busybox Replaces: busybox-static` (package split),
        `binutils-x86-64-linux-gnu Replaces: binutils-dev`.  Treating those as
        supersessions wrongly marked 82 production-sibling binaries (from 49
        selected sources) as removable orphans — fixed 2026-06-08 by gating on
        the fork set.  Same-name forks are covered via the source check.
        """
        _cache = getattr(self, 'cache', None)
        _fork_names: 'set[str]' = (
            set(getattr(_cache, '_fork_pkg_names', set()) or set())
            | set(getattr(_cache, '_fork_src_names', set()) or set())
            | set(getattr(_cache, '_fork_udeb_names', set()) or set())
        ) if _cache is not None else set()
        _selected_names: 'set[str]' = set()
        _superseded: 'set[str]' = set()
        for _tree in (self.dep_tree, self.udeb_dep_tree):
            if _tree is None:
                continue
            for _name, _pkgobj in (
                    getattr(_tree, 'selected_pkgs', None) or {}).items():
                _selected_names.add(_name)
                # Only a FORK's Conflicts/Replaces supersede an upstream
                # binary; everything else is normal transitional metadata.
                _src = (_pkgobj.get('Source') or _name).split(' ', 1)[0]
                if not (_name in _fork_names or _src in _fork_names):
                    continue
                for _field in ('Conflicts', 'Replaces'):
                    _val = _pkgobj.get(_field) or ''
                    for _dep in _val.split(','):
                        _nm = (_dep.strip().split('(', 1)[0]
                               .split(':', 1)[0].strip())
                        if _nm:
                            _superseded.add(_nm)
        return _superseded - _selected_names

    def _scan_stale_files(self) -> 'tuple[list, list, list, int]':
        """Walk the build-output components (main, main-udeb, doc, dbgsym,
        tests — `utils._STALE_SCAN_SUBDIRS`; STA-38 added `main-udeb`) for
        .deb/.udeb files that shouldn't be there given the current
        selected_srcs + src_pkg_files.  Pristine tunneled binaries in the
        non-main components are intentionally out of scope (the classifier
        can't predict their filenames — see _STALE_SCAN_SUBDIRS).

        Returns (orphan, drift, malformed, total):
          orphan    — list of (sub, filename, source_name, size) where
                      the file's Source field doesn't name any selected
                      source.  Most common cause: source dropped from
                      the dep tree (e.g. upstream `tasksel` replaced by
                      `athena-tasksel` fork → leaves 222 task-*
                      binaries orphaned).
          drift     — list of (sub, filename, source_name, size): a
                      SUPERSEDED artifact that should be pruned.  Two
                      cases, both = "a newer version of this exact
                      (name, pristine-base, arch) exists on disk":
                      (a) the source is selected and the current build
                      predicts the higher version (base-files_12.4 left
                      over after the fork bumped to +deb12u14+athena1);
                      (b) ANY lower version in a pristine-base group,
                      selected or not — single-snapshot local repo keeps
                      only the highest, so a superseded production sibling
                      (e2fsprogs comerr-dev +asg1u1 vs +asg1u2) is drift
                      too, not a permanent pool resident.
          malformed — list of 'sub/filename' where dpkg control couldn't
                      be parsed (truncated/corrupt .deb).
          total     — total .deb/.udeb files scanned across all subdirs.

        Shared by cmd_package_cleanup (DELETE on `force`) and cmd_audit
        (warn-only).  Requires dep_check_ready — caller verifies.
        """
        # Build the three reference sets:
        #   _expected_files     — exact predicted filenames across both
        #                         trees.  A file matching one is KEEP.
        #   _selected_pkg_names — binary pkg names appearing in any
        #                         src_pkg_files entry.  File whose name
        #                         is here but filename ISN'T in
        #                         _expected_files = version drift.
        #   _selected_srcs      — source names selected across both
        #                         trees.  File whose Source isn't in
        #                         this set = orphan-source.
        #
        # Filename-keyed (not Version-field-keyed) to avoid the dpkg
        # epoch convention trap (bsdutils source 2.38.1-5 → binary
        # Version 1:2.38.1-5 but Filename bsdutils_2.38.1-5_amd64.deb,
        # epoch stripped — comparing Version fields raw false-positives
        # every epoch-bumped binary).
        _expected_files: 'set[str]' = set()
        _expected_keys: 'set[tuple[str, str, str]]' = set()
        _selected_pkg_names: 'set[str]' = set()
        _selected_srcs: 'set[str]' = set()
        # Upstream binaries a selected fork supersedes (Conflicts/Replaces) —
        # removable even when their source lingers as "selected".
        _superseded = self._superseded_binary_names()

        def _file_key(_fn):
            # (name, pristine-base, arch) for a name_ver_arch.(u)deb file —
            # base via utils.pristine_base so a +asg<R>u<N> stamp groups with
            # its pristine prediction.  None if not in name_ver_arch shape.
            if not (_fn.endswith(('.deb', '.udeb'))):
                return None
            _stem = _fn.rsplit('.', 1)[0]
            _parts = _stem.split('_')
            if len(_parts) != 3:
                return None
            return (_parts[0], utils.pristine_base(_parts[1]), _parts[2])

        for _tree in (self.dep_tree, self.udeb_dep_tree):
            if _tree is None:
                continue
            _selected_srcs.update(_tree.selected_srcs.keys())
            for _files in _tree.src_pkg_files.values():
                _expected_files.update(_files)
                for _fn in _files:
                    _selected_pkg_names.add(_fn.split('_', 1)[0])
                    _k = _file_key(_fn)
                    if _k is not None:
                        _expected_keys.add(_k)

        _orphan: 'list[tuple[str, str, str, int]]' = []
        _drift:  'list[tuple[str, str, str, int]]' = []
        _malformed: 'list[str]' = []
        _total = 0

        # CONF-01 Stage E (2026-05-22): walk the apt indexes instead
        # of per-file DebFile opens.  repo_audit.iter_packages_all_versions
        # uses dpkg-scanpackages' cached --multiversion output and
        # parses it via apt_pkg.TagFile — same Source/Package/Size
        # fields we need, but a single subprocess + fast in-process
        # parse instead of N×(fork+exec+tar-extract).  On a 5k-pkg
        # repo this is an order of magnitude faster.
        #
        # UPD-01: group every on-disk artifact by (subdir, name, base, arch).
        # Within a group matching an EXPECTED base, keep only the HIGHEST
        # version (single-snapshot local) — a lower version (e.g. the pristine
        # predecessor of a freshly +asg<R>u<N>-stamped delta) is drift.  This
        # is the local prune that returns repo/ to one-version-per-package
        # after a refresh; the superseded version is already on the (additive)
        # remote, so deleting it locally is safe (publish-before-prune).
        from collections import defaultdict
        _by_key: 'dict[tuple, list]' = defaultdict(list)
        # STA-38: walk the build-output components via _STALE_SCAN_SUBDIRS
        # (the same canon all_deb_dirs uses), NOT the narrower _REPO_SUBDIRS
        # that was missing `main-udeb` — so a superseded +asg<R>u<N> udeb in
        # main/debian-installer/ (our built e2fsprogs-udeb / keyring-udeb)
        # survived both cleanup and the chroot stale gate.
        for _sub in utils._STALE_SCAN_SUBDIRS:
            # _index_seen: filenames dpkg-scanpackages emitted for this
            # subdir — used below to recover the malformed bucket (on-disk
            # files the scanner couldn't parse are absent from the index).
            _index_seen: 'set[str]' = set()
            # refresh=True: re-scan repo/ fresh so the keep/delete decision
            # reflects the CURRENT on-disk artifacts (not a stale cached
            # Packages snapshot) — deleting on stale data is dangerous, and a
            # stale snapshot is exactly what hid the orphaned upstream apt-setup
            # udebs after the rename fork.
            for _filename, _ctrl in repo_audit.iter_packages_all_versions(
                    self.config, subdir=_sub, refresh=True):
                _total += 1
                _index_seen.add(_filename)
                _pkg = (_ctrl.get('Package') or '').strip()
                _src_field = (_ctrl.get('Source') or '').strip()
                # Source field is "name" or "name (version)" — drop the
                # version qualifier; fall back to Package name when the
                # control omits Source (single-binary sources).
                _src_name = (_src_field.split(' ', 1)[0].strip()
                             if _src_field else _pkg)
                try:
                    _size = int(_ctrl.get('Size') or 0)
                except (TypeError, ValueError):
                    _size = 0
                _ver = (_ctrl.get('Version') or '').strip()
                _by_key[(_sub, _file_key(_filename))].append(
                    (_filename, _ver, _src_name, _size))

            # STA-38: recover the malformed bucket.  dpkg-scanpackages
            # silently omits a .deb/.udeb it can't parse (truncated /
            # corrupt control), so any binary on disk in this subdir that
            # the index didn't emit is unscannable.  Diff on-disk files
            # against _index_seen (filename-keyed; .verified sidecars and
            # non-binary files ignored).
            try:
                _dir = self.config.deb_dir_for(_sub)
            except ValueError:
                _dir = None
            if _dir and os.path.isdir(_dir):
                for _ondisk in os.listdir(_dir):
                    if (_ondisk.endswith(('.deb', '.udeb'))
                            and _ondisk not in _index_seen):
                        _malformed.append(f"{_sub}/{_ondisk}")

        for (_sub, _key), _entries in _by_key.items():
            # Highest version in this (subdir, name, base, arch) group.
            _hi_fn, _hi_ver = None, None
            for _fn, _ver, _src_name, _size in _entries:
                if _hi_ver is None or apt_pkg.version_compare(
                        utils.version_no_epoch(_ver),
                        utils.version_no_epoch(_hi_ver)) > 0:
                    _hi_fn, _hi_ver = _fn, _ver
            _is_expected = _key is not None and _key in _expected_keys
            for _fn, _ver, _src_name, _size in _entries:
                # Single-snapshot local repo (UPD-01): within any
                # pristine-base group ONLY the highest version is current —
                # every lower version is superseded drift, SELECTED OR NOT.
                # This is what prunes superseded PRODUCTION SIBLINGS (e.g.
                # e2fsprogs comerr-dev / ss-dev / fuse2fs +asg1u1 left
                # behind by the +asg1u2 rebuild) that the per-selection
                # branch below would otherwise KEEP forever — unbounded
                # pool accumulation that also violates one-version-per-
                # package locally and rides onto the installer ISO's
                # /cdrom/pool.  Safe to delete: the superseded version is
                # already on the additive remote (publish-before-prune).
                # (_key is None ⇒ filename not name_ver_arch-parseable —
                # those skip the version compare and fall through unchanged.)
                if _key is not None and _fn != _hi_fn:
                    _drift.append((_sub, _fn, _src_name, _size))   # superseded
                    continue
                # _fn is the current (highest) version of its group.
                if _is_expected:
                    continue                        # current expected — KEEP
                _file_pkg = _fn.split('_', 1)[0]
                if _file_pkg in _superseded:
                    # Upstream binary a selected fork Conflicts/Replaces
                    # (e.g. apt-setup-udeb vs athena-setup-udeb).  Must go —
                    # anna ignores Conflicts so it'd ship + run alongside the
                    # fork.  Removable even though its source may be selected.
                    _orphan.append((_sub, _fn, _src_name, _size))
                elif _src_name not in _selected_srcs:
                    _orphan.append((_sub, _fn, _src_name, _size))
                elif _file_pkg in _selected_pkg_names:
                    _drift.append((_sub, _fn, _src_name, _size))
                # else: pkg name not predicted but source IS selected —
                # CURRENT production sibling (lib*-i386, lib*-l10n, etc.)
                # that ships in /cdrom/pool but isn't an install target.
                # KEEP (only its superseded lower versions were pruned above).

        return _orphan, _drift, _malformed, _total

    def _scan_orphaned_sidecars(self) -> 'list[tuple[str, str]]':
        """Find `.verified` sha-cache sidecars whose `.deb`/`.udeb` is gone.

        A `.verified` (utils.get_sha256's cache) lives next to a binary
        and is meaningless once that binary is removed.  Several removal
        paths drop the binary but NOT its sidecar — source-build output
        replacement (`+asg1u1` → `+asg1u2`), pre-STA-38 cleanups, lifecycle
        pruning — so orphans accumulate (15 found 2026-06-13 after the
        e2fsprogs/keyring rebuilds + the reportbug deprecation).

        Unconditionally safe to delete: pure filesystem garbage, regenerated
        on demand if the binary ever returns.  Needs NO dep tree (unlike
        _scan_stale_files) — this is filesystem consistency, not a stale-vs-
        selected judgement, so it is deliberately NOT part of the chroot
        pre-flight gate (orphan sidecars are harmless and must not block a
        build).  Read-only.

        Returns [(subdir-label, sidecar-filename), ...] over the
        build-output components (_STALE_SCAN_SUBDIRS).
        """
        _orphans: 'list[tuple[str, str]]' = []
        for _sub in utils._STALE_SCAN_SUBDIRS:
            try:
                _dir = self.config.deb_dir_for(_sub)
            except ValueError:
                continue
            if not os.path.isdir(_dir):
                continue
            for _f in os.listdir(_dir):
                if not _f.endswith('.verified'):
                    continue
                _base = _f[:-len('.verified')]
                if not os.path.exists(os.path.join(_dir, _base)):
                    _orphans.append((_sub, _f))
        return _orphans

    def _live_published_claim_filenames(self) -> 'set[str]':
        """STA-25: filenames currently covered by a LIVE published claim in
        our local coord ledger (`config/coord/claims/<builder>.jsonl`).

        These are bytes a published claim on a mirror still names as live —
        pruning them locally before the mirror is told (deprecate / obsolete
        via `mirror publish`, or `mirror reclaim`) violates UPD-01's
        publish-before-prune discipline: the mirror keeps serving a sha we no
        longer hold, so a later reclaim/audit can't reproduce it.

        Reads our own append-only jsonl WITHOUT signature verification (our
        local record, not an attack surface) and applies the standard
        supersession fold via `iter_live_claims_by_filename`, keeping only
        genuinely-published claims (deprecate/obsolete/retract markers are
        already release/prune signals, so excluded).  Returns the empty set
        when there's no coord ledger (non-federated operator → cleanup
        unaffected)."""
        import coord.store as _store
        import coord.schema as _schema
        _dir = getattr(self.config, 'dir_coord_claims', None)
        if not _dir or not os.path.isdir(_dir):
            return set()
        _by_builder: 'dict[str, list]' = {}
        try:
            _entries = sorted(os.listdir(_dir))
        except OSError:
            return set()
        for _e in _entries:
            if not _e.endswith('.jsonl'):
                continue
            _claims = _store.read_builder_claims(_dir, _e[:-len('.jsonl')])
            if _claims:
                _by_builder[_e[:-len('.jsonl')]] = _claims
        return {
            _fn for _fn, _c in _store.iter_live_claims_by_filename(_by_builder)
            if _c.get('claim_state') == _schema.CLAIM_STATE_PUBLISHED
        }

    def cmd_package_cleanup(self, *args):
        """Identify and delete obsolete .debs/.udebs in repo/.

        Usage: package cleanup [verbose]            — dry-run report
               package cleanup force [verbose]      — actually delete

        Obsolete categories (BOTH selected_srcs and selected_pkgs from
        both deb + udeb trees are factored — base / live / installer /
        pool / extras-from-recommends all included automatically):

          orphan-source : file's Source field names a source that is
                          NOT in any selected_srcs.  Most common case:
                          previously-built source got dropped from the
                          dep tree (e.g. upstream `tasksel` replaced by
                          `athena-tasksel` fork — leaves 222 task-*
                          binaries as orphans).

          version-drift : file's source IS selected but at a DIFFERENT
                          stripped version than what selected_srcs has.
                          Typical case: snapshot rolled forward between
                          builds, leaving stale .debs at the older
                          version.

        Files that are KEPT:
          - any .deb whose source is in selected_srcs AND whose Version
            matches the source's selected version (post-strip)
          - including sibling binaries the build emits but doesn't
            install (libc6-i386, libc-l10n, etc.) — these ship in
            /cdrom/pool and may be apt-installed on target later

        Safety:
          - Default is dry-run.  Reports per-source groupings + size
            totals.  No file touched.
          - `force` triggers actual deletion AFTER a final YESNO prompt.
          - dep_check_ready is required (selected_srcs must be populated).
        """
        if not self.flags.dep_check_ready:
            console.print(
                "Run `cache parse` first — cleanup needs selected_srcs "
                "to know what's NOT obsolete"
            )
            return

        _force = 'force' in args
        _verbose = 'verbose' in args

        # _scan_stale_files re-scans repo/ fresh (dpkg-scanpackages per subdir) —
        # several seconds on a big repo with no other output; spin it.
        _spin = Spinner("Scanning repo/ for obsolete artifacts")
        try:
            _orphan, _drift, _malformed, _total_files = self._scan_stale_files()
            _sidecar_orphans = self._scan_orphaned_sidecars()
        finally:
            _spin.done()

        # STA-25: which obsolete targets are still named by a LIVE published
        # claim?  Deleting their bytes before the mirror is told (publish-
        # before-prune) strands a sha the mirror keeps serving.
        _live_fns = self._live_published_claim_filenames()
        _claimed = sorted(
            {_f for _sub, _f, *_ in _orphan if _f in _live_fns}
            | {_f for _sub, _f, *_ in _drift if _f in _live_fns}
        )

        # ------ Report ------
        _n_obsolete = len(_orphan) + len(_drift)
        _bytes_obsolete = (sum(s for *_, s in _orphan)
                           + sum(s for *_, s in _drift))
        console.print(
            f"\nScanned {_total_files} .deb/.udeb file(s) under "
            f"{self.config.dir_repo}/{{main,doc,dbgsym,tests}}"
        )
        console.print(
            f"  orphan-source   : {len(_orphan)} file(s) "
            f"(source not in selected_srcs)"
        )
        console.print(
            f"  version-drift   : {len(_drift)} file(s) "
            f"(source selected but version mismatch)"
        )
        if _malformed:
            console.print(
                f"  malformed       : {len(_malformed)} file(s) "
                f"(skipped — can't read control)"
            )
        if _sidecar_orphans:
            console.print(
                f"  orphan sidecars : {len(_sidecar_orphans)} `.verified` "
                f"file(s) whose .deb/.udeb is gone (always safe to drop)"
            )
        if _n_obsolete == 0 and not _sidecar_orphans:
            console.print("repo/ is clean — no obsolete files found")
            return
        if _n_obsolete:
            console.print(
                f"  TOTAL OBSOLETE  : {_n_obsolete} file(s), "
                f"{_bytes_obsolete / 1024 / 1024:.1f} MB"
            )

        # Group orphan by source so the operator sees the shape (e.g.
        # 222 task-* from a single removed source is one line, not 222).
        if _orphan:
            from collections import defaultdict
            _by_src: 'dict[str, list[tuple]]' = defaultdict(list)
            for _sub, _f, _src, _sz in _orphan:
                _by_src[_src].append((_sub, _f, _sz))
            console.print("\nOrphan source removals (grouped by source):")
            _src_sorted = sorted(
                _by_src.items(), key=lambda kv: -sum(s for *_, s in kv[1]),
            )
            _show = _src_sorted if _verbose else _src_sorted[:30]
            for _src, _files in _show:
                _src_total = sum(s for *_, s in _files)
                console.print(
                    f"  {_src:35s} → {len(_files):4d} file(s), "
                    f"{_src_total / 1024 / 1024:.1f} MB"
                )
                if _verbose:
                    for _sub, _f, _sz in _files[:10]:
                        console.print(f"      {_sub}/{_f}")
                    if len(_files) > 10:
                        console.print(f"      … (+{len(_files) - 10} more)")
            if len(_src_sorted) > 30 and not _verbose:
                console.print(
                    f"  … (+{len(_src_sorted) - 30} more source(s); "
                    f"pass `verbose` for full list)"
                )

        if _drift:
            console.print(
                "\nVersion-drift residue (binary name selected, this "
                "specific filename not in predicted output):"
            )
            _show = _drift if _verbose else _drift[:30]
            for _sub, _f, _src, _sz in _show:
                console.print(
                    f"  {_sub}/{_f}  (source: {_src})"
                )
            if len(_drift) > 30 and not _verbose:
                console.print(
                    f"  … (+{len(_drift) - 30} more; "
                    f"pass `verbose` for full list)"
                )

        if _claimed:
            console.print(
                f"\n  ⚠ PUBLISH-BEFORE-PRUNE: {len(_claimed)} of these are "
                "still named by a LIVE published claim on a mirror:",
                tui.COLOR_WARNING)
            for _f in _claimed[:15]:
                console.print(f"      {_f}", tui.COLOR_WARNING)
            if len(_claimed) > 15:
                console.print(
                    f"      … (+{len(_claimed) - 15} more)", tui.COLOR_WARNING)
            console.print(
                "    Deprecate/obsolete them on the mirror first "
                "(`mirror publish`), or `mirror reclaim` to refresh the bytes "
                "— deleting now strands a sha the mirror still serves.",
                tui.COLOR_WARNING)

        if not _force:
            console.print(
                "\nDRY-RUN — no files were deleted.  "
                "Pass `repo repair cleanup force` to actually delete.",
                tui.COLOR_INFO,
            )
            return

        # STA-25: a dedicated publish-before-prune gate — when any target is
        # still live on a mirror, require an explicit acknowledgement BEFORE
        # the generic delete prompt (the operator should normally
        # `mirror publish` the supersession first).
        if _claimed:
            _resp = Prompt(
                PROMPT_YESNO,
                f"{len(_claimed)} file(s) are still named by a LIVE published "
                "claim on a mirror (publish-before-prune).  Delete locally "
                "anyway?",
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                console.print(
                    "Aborted — `mirror publish` the supersession first, "
                    "then re-run cleanup")
                return

        # Force mode: final confirmation prompt.  _n_to_delete counts the
        # obsolete .deb/.udeb plus the orphaned `.verified` sidecars.
        _n_to_delete = _n_obsolete + len(_sidecar_orphans)
        _resp = Prompt(
            PROMPT_YESNO,
            f"DELETE {_n_obsolete} obsolete file(s) "
            f"({_bytes_obsolete / 1024 / 1024:.1f} MB)"
            + (f" + {len(_sidecar_orphans)} orphan sidecar(s)"
               if _sidecar_orphans else "")
            + "?  This is IRREVERSIBLE.",
        ).get_response()
        if _resp.lower() not in ('y', 'yes'):
            console.print("Aborted — no files deleted")
            return

        # ------ Delete ------
        _deleted = 0
        _delete_failed = 0
        _bar = ProgressBar(
            label='Cleanup', maxvalue=_n_to_delete, show_rate=False,
        )
        # STA-38: resolve the on-disk dir from the SCANNED label `_sub`
        # via deb_dir_for — the scan found the file in exactly that dir.
        # (The previous filename-derived routing defaulted component to
        # 'main', which mis-routed a non-free-firmware .deb to main/ →
        # delete failure.)  Also drop the orphaned `.verified` sidecar
        # alongside each removed binary.
        def _remove_artifact(_sub, _f):
            nonlocal _deleted, _delete_failed
            _p = os.path.join(self.config.deb_dir_for(_sub), _f)
            try:
                os.remove(_p)
                _deleted += 1
            except OSError as e:
                _delete_failed += 1
                logger.error(f"cleanup: cannot remove {_p}: {e}")
                return
            try:
                os.remove(_p + '.verified')
            except OSError:
                pass  # sidecar may legitimately not exist

        for _sub, _f, *_ in _orphan:
            _bar.step(1)
            _remove_artifact(_sub, _f)
        for _sub, _f, *_ in _drift:
            _bar.step(1)
            _remove_artifact(_sub, _f)
        # Orphaned `.verified` sidecars — remove directly (the binary they
        # cached is already gone; nothing to thread through _remove_artifact).
        for _sub, _f in _sidecar_orphans:
            _bar.step(1)
            _sp = os.path.join(self.config.deb_dir_for(_sub), _f)
            try:
                os.remove(_sp)
                _deleted += 1
            except OSError as e:
                _delete_failed += 1
                logger.error(f"cleanup: cannot remove sidecar {_sp}: {e}")
        _bar.close()

        # repo state changed — audit's Packages snapshot is stale.
        repo_audit.invalidate_cache(self.config.dir_repo)

        console.print(
            f"\nCleanup complete: {_deleted} deleted, "
            f"{_delete_failed} failed.  "
            f"Run `repo audit` to confirm constraints still resolve."
        )

    def cmd_reload_fork(self, *pkgs):
        """Light-touch rebuild of a fork pkg after a content edit.

        Usage: package reload <pkg>...

        For each named fork pkg the command:

          1. Compares the current tree-hash + dep-hash against the
             persisted sidecars from the previous successful build.
          2. Branches on what changed:
             - tree-hash matches: NO-OP (no content change since last build)
             - dep-hash differs: GATE — print the gating fields, refuse the
               light path.  Operator must do a full cycle:
                   cache build force → cache parse force →
                   source sync force → source build <pkg>
             - tree-hash differs but dep-hash matches: LIGHT PATH:
                 a. Wipe the pkg's derived artifacts (fork tarball,
                    source/ copy, repo/ debs, build log sidecars).
                 b. Regenerate the fork tarball via generate_fork_mirror.
                 c. Copy the fresh tarball into source/ so BuildContainer
                    can `cp /source/<pkg>_* .` it.
                 d. Invoke `source build force <pkg>` to rebuild.
                 e. Persist updated hashes (done by generate_fork_mirror).

        Prereqs: cache build + cache parse + container init must have
        run earlier in the session.  The reload only avoids RE-RUNNING
        them; it doesn't bypass them entirely.

        Why this exists: editing a fork file (e.g. fix a typo in
        debian/rules) used to require `cache build force` →
        `cache parse force` → `source sync force` →
        `source build <pkg>`, with each force flag manually remembered
        because the *_ready flags don't auto-invalidate.  This command
        does the right thing in one step for the common case (content
        change, no dep impact) and refuses loudly for the uncommon one
        (dep field changed, must rebuild cache).

        Tunneled packages aren't fork packages; this command skips
        names not present under fork/source/.
        """
        if not pkgs:
            console.print(
                "Usage: package reload <pkg>...  "
                "(name(s) of fork/source/<pkg>/ to reload)",
                tui.COLOR_INFO,
            )
            return

        # Prereqs: we're not the right tool for first-run-of-session.
        if not (self.flags.cache_ready and self.flags.dep_check_ready
                and self.flags.build_container_ready):
            console.print(
                "package reload requires cache build + cache parse + container "
                "init to have run earlier in this session.  For first-run, "
                "use `autorun installer` or the per-step sequence.",
                tui.COLOR_ERROR,
            )
            return

        import fork_mirror
        import glob

        for _pkg in pkgs:
            _pkg_dir = os.path.join(self.config.dir_fork_source, _pkg)
            if not os.path.isdir(_pkg_dir):
                console.print(
                    f"package reload: {_pkg} is not a fork "
                    f"(no {self.config.dir_fork_source}/{_pkg}/) — skipping",
                    tui.COLOR_INFO,
                )
                continue
            if not os.path.isfile(os.path.join(_pkg_dir, 'debian', 'control')):
                console.print(
                    f"package reload: {_pkg} missing debian/control — skipping",
                    tui.COLOR_INFO,
                )
                continue

            # Compute current hashes
            _current_tree = utils.compute_tree_hash(_pkg_dir)
            _current_dep  = fork_mirror._compute_dep_hash(_pkg_dir)
            _stored_tree, _stored_dep = fork_mirror.load_pkg_hashes(
                _pkg, self.config.dir_fork_source_repo,
            )

            # Decision: no-op
            if _current_tree == _stored_tree and _stored_tree:
                console.print(
                    f"{_pkg}: unchanged since last build — nothing to do",
                    tui.COLOR_INFO,
                )
                continue

            # Decision: gate (dep-affecting change)
            if _stored_dep and _current_dep != _stored_dep:
                console.print(
                    f"{_pkg}: dep-affecting field(s) changed in debian/control "
                    "or debian/changelog (Depends / Provides / Version / etc). "
                    "Light reload would diverge from cache + dep tree.",
                    tui.COLOR_ERROR,
                )
                console.print(
                    "  Full restart required:\n"
                    "    cache build force\n"
                    "    cache parse force\n"
                    "    source sync force\n"
                    f"    source build {_pkg}",
                    tui.COLOR_INFO,
                )
                continue

            # Decision: light path
            console.print(
                f"{_pkg}: package-local change detected — light reload",
                tui.COLOR_INFO,
            )

            # Step (a) + (b) + (e): generate_fork_mirror handles wipe,
            # regen, and hash persist for changed forks.  Runs over ALL
            # fork pkgs but only changed ones do actual work (mtime gate
            # in _generate_source_packages skips unchanged ones).
            if not fork_mirror.generate_fork_mirror(self.config):
                console.print(
                    f"{_pkg}: fork mirror regeneration failed; see log",
                    tui.COLOR_ERROR,
                )
                continue

            # Step (c): copy the fresh tarball to source/ so BuildContainer
            # finds it.  download_source would also do this via file://
            # but we don't want to re-run the whole download phase.
            _copied = 0
            for _src_path in glob.glob(
                    os.path.join(self.config.dir_fork_source_repo, f'{_pkg}_*')):
                if _src_path.endswith(('.tree-hash', '.dep-hash')):
                    continue
                _basename = os.path.basename(_src_path)
                _dest = os.path.join(self.config.dir_source, _basename)
                try:
                    shutil.copyfile(_src_path, _dest)
                    # Stale .verified sidecar would be confused by the
                    # new mtime; remove so first SHA query recomputes.
                    _verified = _dest + '.verified'
                    if os.path.exists(_verified):
                        os.remove(_verified)
                    _copied += 1
                except OSError as e:
                    console.print(
                        f"package reload: copy {_basename} → source/ failed: {e}",
                        tui.COLOR_ERROR,
                    )
            if _copied == 0:
                console.print(
                    f"{_pkg}: regen produced no files to copy — skipping rebuild",
                    tui.COLOR_ERROR,
                )
                continue
            console.print(
                f"{_pkg}: copied {_copied} file(s) from fork mirror → source/",
                tui.COLOR_INFO,
            )

            # Step (d): rebuild via the standard source build path.  force
            # so check_build doesn't short-circuit on the wiped .result.
            self.cmd_source_build('force', _pkg)
