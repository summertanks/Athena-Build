"""Package cache + dependency resolution — the `cache` / `dep` handlers.

cmd_build_cache fetches and parses the upstream APT indices into the
in-memory package universe; cmd_parse_dependency is the resolution engine
(distribution-mode closure over pkg/live/installer/pool lists, build-mode
subset), with _cache_parse_build_mode / _canonical_select_count /
_refuse_in_build_mode as helpers.  cmd_cache_purge / cmd_cache_select /
cmd_cache_info round out the cache surface.  The `cache` noun-dispatcher
stays in build.py.  Extracted verbatim from BuildSession; see
commands/base.py for how the mixin shares session state.
"""
import logging
import os
from typing import Optional

import buildcontainer
import dependencytree
import persistence
import selection_lock
import tui
import utils
from cache import Cache
from tui import console, Prompt, PROMPT_YESNO, Spinner
from utils import BuildConfig

from commands.base import SessionState

logger = logging.getLogger('athena.build')


class CacheCommandsMixin(SessionState):
    def cmd_build_cache(self, *args):
        """Fetch and parse the upstream APT package indices into an in-memory cache.

        Downloads the binary and source Packages files for the configured base
        distribution and architecture, then indexes them for fast lookup during
        dependency resolution.  Must be run before parse_dependency.

        Idempotency: if cache_ready is already set and an in-memory
        Cache is loaded, the call no-ops with a hint to use `clean
        cache` (wipe + re-fetch) or `cache build force` (re-run over
        existing files).  Prevents accidental multi-GB re-fetches when
        the operator just re-ran the command out of habit.
        """
        if self.flags.cache_ready and self.cache is not None and 'force' not in args:
            console.print(
                "cache build: already complete — pass `force` to rebuild "
                "over existing files, or run `clean cache` to wipe first",
                tui.COLOR_INFO,
            )
            return
        # cache build depends on the snapshot pin — on a fresh system (no
        # base/current in config/snapshot.state) prompt the operator to select
        # both before proceeding (UPD-01).
        if not self._ensure_snapshot_pins():
            return
        console.print("Building Cache...", tui.COLOR_INFO)
        self.flags.cache_ready = False  # reset in case we're re-running

        if self.config.snapshot_enabled:
            try:
                _ts = utils.resolve_snapshot_timestamp(self.config)
            except (RuntimeError, ValueError) as e:
                console.print(f"ERROR: snapshot timestamp resolution failed — {e}",
                              tui.COLOR_ERROR)
                logger.error(f"resolve_snapshot_timestamp: {e}")
                return
            _human = utils.format_snapshot_timestamp(_ts) if _ts else '<unresolved>'
            _suffix = ' (latest)' if self.config.snapshot_timestamp_config == 'latest' else ''
            console.print(f"Snapshot timestamp: {_human}{_suffix}", tui.COLOR_HIGHLIGHT)

        else:
            console.print("Snapshot pinning: disabled (live mirrors)", tui.COLOR_INFO)

        # Top-level Spinner that covers the whole Cache() construction.
        # Cache() internally raises its own ProgressBars during the
        # record-index passes (per-mirror Packages/Sources/d-i Packages)
        # — both render together in the TUI's widget stack.  The
        # spinner signals "the overall cache build is alive" during the
        # quiet phases between those bars (mirror fetch, cross-validation,
        # dep-graph internals).
        _spin = Spinner("Building Cache (mirrors + indices + dep graph)")
        try:
            try:
                self.cache = Cache(self.config)
            except Exception as e:
                console.print(f"ERROR: build cache - {e}")
                logger.error(f"Cache() raised: {e}")
                return
        finally:
            _spin.done()

        if not self.cache.is_valid:
            console.print(f"ERROR: build cache - {self.cache.error_str}")
            logger.error(f"Cache invalid: {self.cache.error_str}")
            return

        self.flags.cache_ready = True


    # --------------------------------------Command: cache purge-------------------------------------

    def cmd_cache_purge(self, *args):
        """Delete every regular file in the cache directory.

        The cache holds re-downloadable mirror metadata (Packages, Sources,
        InRelease — both compressed and decompressed) and the resolved
        snapshot.timestamp marker.  Purging is safe — the next `cache build`
        re-fetches everything from configured mirrors; the cost is a multi-GB
        re-download.  Useful when mirror metadata has gone stale or a
        previous run left orphan artefacts (e.g. the reverted UX-04
        cache.pkl.gz / buildflags.json files).

        Subdirectories (if any) are left intact — only top-level files go.
        Resets cache_ready and dep_check_ready since the in-memory Cache
        and DependencyTree (if any) now point at deleted files.

        `force` arg (or called with skip_prompt by `clean all` orchestrator)
        bypasses the YESNO confirmation.
        """
        _force = 'force' in args
        try:
            _entries = [e for e in os.scandir(self.config.dir_cache)
                        if e.is_file(follow_symlinks=False)]
        except OSError as e:
            console.print(f"ERROR: cannot read cache directory: {e}")
            logger.error(f"cache purge scandir({self.config.dir_cache}): {e}")
            return

        if not _entries:
            console.print(f"Cache directory already empty: {self.config.dir_cache}")
            return

        _total_bytes = sum(e.stat().st_size for e in _entries)
        _mb = _total_bytes / (1024 * 1024)
        if not _force:
            _resp = Prompt(PROMPT_YESNO,
                f"Delete {len(_entries)} file(s) ({_mb:.1f} MB) from "
                f"{self.config.dir_cache}?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                console.print("Cache purge cancelled.")
                return

        _deleted = 0
        _failed  = 0
        for _entry in _entries:
            try:
                os.unlink(_entry.path)
                _deleted += 1
            except OSError as e:
                logger.error(f"cache purge: cannot delete {_entry.path}: {e}")
                _failed += 1

        # In-memory cache (and anything built off it) now points at deleted
        # files; drop the references and reset prerequisite flags so the
        # downstream guards trip cleanly until cache build runs again.
        self.cache = None
        self.dep_tree = None
        self.flags.cache_ready = False
        self.flags.dep_check_ready = False

        if _failed:
            console.print(
                f"Cache purge: {_deleted} deleted, {_failed} failed "
                "(see log)", tui.COLOR_WARNING)
        else:
            console.print(
                f"Cache purge: {_deleted} file(s) deleted "
                f"({_mb:.1f} MB freed)", tui.COLOR_INFO)

    # --------------------------------------Command: parse_dependency-------------------------------------

    def _refuse_in_build_mode(self, what: str) -> bool:
        """Chroot/ISO commands are N/A in build mode (a single-package
        builder doesn't assemble the target system).  Returns True iff
        the command should refuse + operator-visible reason printed.

        Tolerant of a missing `config` attr (defensive against
        test doubles that don't construct a full BuildConfig).
        """
        _mode = getattr(getattr(self, 'config', None), 'build_mode',
                        'distribution')
        if _mode == 'build':
            console.print(
                f"{what}: N/A in build mode — `[Build] Mode = build` "
                "skips chroot/ISO assembly.  Run on a dist-mode host, "
                "or change config/build.conf.",
                tui.COLOR_WARNING)
            return True
        return False

    def _canonical_select_count(self, tree) -> int:
        """Count canonical-name keys in selected_pkgs (excludes Provides
        aliases).  Used by build-mode summary so the operator sees the
        true binary count, not the aliased duplicate count."""
        if tree is None:
            return 0
        return sum(
            1 for _k in tree.selected_pkgs
            if _k == tree.selected_pkgs[_k]['Package']
        )

    def _cache_parse_build_mode(self) -> bool:
        """MIRROR-02: build-mode dep parse.  Populates
        `dep_tree.selected_pkgs` with the binaries named in
        `config/build_pkg.list` (latest version per name), then
        `dep_tree.selected_srcs` via `parse_sources` so the source
        builder knows each Build-Depends.

        No runtime dep closure walk — build mode is single-package
        scope; runtime installability is the MIRROR's invariant, not
        the build host's (enforced at `mirror publish` time in
        chunk 11).  No udeb_dep_tree (installer is N/A in build mode).

        Returns True iff at least one package resolved.  Logs WARNINGs
        for misses; does NOT abort on partial resolution (operator
        sees the WARNINGs + the per-pass count summary).
        """
        assert self.dep_tree is not None
        assert self.cache is not None
        _names = utils.parse_build_pkg_list(self.config.build_pkg_list_path)
        if not _names:
            console.print(
                f"WARNING: [Build] Mode = build but build_pkg.list at "
                f"{self.config.build_pkg_list_path} is empty or missing.",
                tui.COLOR_WARNING)
            return False
        console.print(
            f"build mode: {len(_names)} package(s) from build_pkg.list",
            tui.COLOR_INFO)
        _resolved = 0
        for _name in _names:
            # package_hashtable is Dict[name, Dict[Version, List[Package]]];
            # pick the highest version then the first Package at that
            # version (multi-mirror duplicates are byte-identical at this
            # point — the cache builder already deduped).
            _versions = self.cache.package_hashtable.get(_name)
            if not _versions:
                console.print(
                    f"  WARNING: '{_name}' not in cache — skipping",
                    tui.COLOR_WARNING)
                logger.warning(
                    f"build mode: '{_name}' not in package_hashtable")
                continue
            _latest_ver = max(_versions.keys())
            _latest_pkgs = _versions[_latest_ver]
            if not _latest_pkgs:
                console.print(
                    f"  WARNING: '{_name}' present but no Package at "
                    f"{_latest_ver} — skipping",
                    tui.COLOR_WARNING)
                continue
            _pkg = _latest_pkgs[0]
            # Use canonical name as the key (matches what parse_dependency
            # does at line ~491).  No Provides aliasing — build mode is
            # narrow-scope and no other code reads through aliases for
            # this tree.
            self.dep_tree.selected_pkgs[_pkg['Package']] = _pkg
            _resolved += 1
        if _resolved == 0:
            return False
        # parse_sources walks selected_pkgs → selected_srcs and pulls
        # Build-Depends; same call the dist-mode path makes at line 1186.
        if not self.dep_tree.parse_sources():
            console.print(
                "build mode: parse_sources reported errors "
                "(see log) — continuing with partial source set",
                tui.COLOR_WARNING)
        # udeb_dep_tree stays None — installer is N/A in build mode.
        return True

    def cmd_parse_dependency(self, *args):
        """Resolve the full closure of packages needed to build the target system.

        Runs SIX dependency-resolution passes — five against the deb world
        (Cache.package_hashtable) and a final one against the udeb world
        (Cache.udeb_hashtable, accessed via Cache.udeb_view()):

          Pass I   — 'required' packages (essential base; every package they pull
                      in is also marked required so it survives any later pruning)
          Pass II  — 'important' packages (strongly recommended by Debian policy;
                      avoids excessive manual intervention on a bare system)
          Pass III — manually listed packages from pkg.list (user-selected base)
          Pass IV  — packages from live.list — what the live system needs over
                      and above pkg.list.  Anything new lands in
                      live_exclusive_pkg_names.
          Pass V   — installer.list deb arm — entries that exist in the deb
                      cache (e.g. efibootmgr, grub-pc-bin) get pulled into the
                      deb tree and credited to installer_exclusive_pkg_names.
          Pass VI  — installer.list udeb arm + Cache.udeb_required +
                      Cache.udeb_important resolved against udeb_hashtable
                      via a parallel udeb_dep_tree.  Produces the udeb closure
                      that becomes the installer ramdisk content.
          Pass VII — pool.list — packages that ship in the apt pool on
                      the installer ISO but are NEVER installed in any
                      chroot.  Resolved with `check_conflicts=False`
                      so mutually-conflicting metas (e.g. `grub-pc` +
                      `grub-efi-amd64`, picked at install time by
                      grub-installer based on firmware mode) coexist
                      in selected_pkgs.  validate_selection skips
                      Breaks/Conflicts involving any pool extra; apt
                      enforces them at install time on the target.

        installer.list is mixed-universe — each entry is dispatched per its
        membership in the deb / udeb hashtables (deb match → Pass V; udeb
        match → Pass VI; both → both).

        After resolution, validates the selection for Breaks/Conflicts, then
        maps every selected binary package back to its source package so that
        source sync and source build know what to fetch and build.  Both
        trees' selected_pkgs are mapped to sources via parse_sources;
        downstream consumers iterate over the union (Phase 4 work).

        Patch files are discovered at this stage so that buildcontainer.build()
        can mount them at container start time without a second disk scan.
        """
        if not self.flags.cache_ready:
            console.print("Cache not ready, Run 'cache build' first")
            return

        # Idempotency: if dep_check_ready is set and in-memory trees
        # exist, no-op with a hint.  The full deb+udeb resolve
        # over a real bookworm cache takes minutes; protect against
        # accidental re-runs.
        if (self.flags.dep_check_ready
                and self.dep_tree is not None
                and 'force' not in args):
            console.print(
                "cache parse: already complete — pass `force` to re-resolve, "
                "or change config/{pkg,live,installer,pool}.list and re-run "
                "after a cache rebuild",
                tui.COLOR_INFO,
            )
            return

        _spiner = Spinner("Parsing Dependencies")
        self.flags.dep_check_ready = False  # reset before the long parse

        # ── SELECT-LOCK: load the signed selection lockfile up front ────────
        # `_lock`/`_lstatus` drive the post-resolve closure guard; `_pins` feed
        # the resolver so a genuine multi-provider prompt resolves the SAME way
        # it did at baseline (no re-prompt, no false closure delta).  A present-
        # but-tampered lockfile HARD-STOPS before the expensive resolve — never
        # silently rebuilt (would erase deprecation history).  Distribution mode
        # only; build mode (build_pkg.list) has its own selection model.
        _lock: 'Optional[dict]' = None
        _lstatus = selection_lock.STATUS_MISSING
        _pins: dict = {}
        if self.config.build_mode != 'build':
            _lock, _lstatus = selection_lock.read_selection_state(self.config)
            if _lstatus in (selection_lock.STATUS_BADSIG,
                            selection_lock.STATUS_MALFORMED):
                _spiner.done()
                console.print(
                    f"cache parse: selection.state is {_lstatus} — refusing to "
                    "resolve against an untrusted selection authority.  "
                    "Restore the file/key, or `cache purge-state` to "
                    "re-baseline.", tui.COLOR_ERROR)
                self.flags.dep_check_ready = False
                return
            if _lstatus == selection_lock.STATUS_OK and _lock:
                _pins = _lock.get('pins', {}) or {}

        console.print("Preparing Parsing Tree...", tui.COLOR_INFO)
        self.dep_tree = dependencytree.DependencyTree(self.cache, select_recommended=False,
                    arch=self.config.arch, build_profiles=self.config.build_profiles,
                    pins=_pins)

        # ── MIRROR-02 build-mode branch ─────────────────────────────────
        # In build mode the build host targets just the packages named in
        # `config/build_pkg.list` — no runtime dep closure walk, no live/installer/
        # pool extras, no chroot/ISO. selected_pkgs is populated directly from
        # the build_pkg.list lookups (single-version pick per name); parse_sources
        # still runs so the source builder knows each package's Build-Depends.
        # Skip-everything-else: Passes I-VII don't run, udeb_dep_tree stays None.
        if self.config.build_mode == 'build':
            if self._cache_parse_build_mode():
                self.flags.dep_check_ready = True
                _spiner.done()
                console.print(
                    f"cache parse (build): {len(self.dep_tree.selected_srcs)} "
                    f"source(s), {self._canonical_select_count(self.dep_tree)} "
                    f"binary(ies)",
                    tui.COLOR_HIGHLIGHT)
            else:
                _spiner.done()
                console.print(
                    "cache parse (build): no usable packages in build_pkg.list "
                    "(see warnings above); dep_check NOT set.",
                    tui.COLOR_ERROR)
            return

        # --- Pass I: required ---------------------------------------------------
        required_packages = self.cache.required
        console.print("Pass I: Checking dependency for required packages", tui.COLOR_INFO)
        self.dep_tree.resolve_packages(required_packages)

        __num_required = self.dep_tree.selected_count
        console.print(f"Dependencies Selected for 'required' : {__num_required}")

        # Mark every package pulled in by 'required' as required too.
        # Virtual packages (aliases) are skipped — the canonical name carries the priority.
        for _pkg in self.dep_tree.selected_pkgs:
            if _pkg != self.dep_tree.selected_pkgs[_pkg]['Package']:
                continue
            self.dep_tree.selected_pkgs[_pkg].priority = 'required'

        # --- Pass II: important --------------------------------------------------
        # 'Important' packages are not strictly needed for a minimal system but
        # omitting them causes enough breakage that manual intervention is required
        # for almost every subsequent step.  If the list is ever stabilised we
        # could replace it with a curated hand-picked set.
        important_packages = self.cache.important

        # Option to manually add additional packages we think are important, e.g. dialog
        # important_packages.extend(['dialog'])

        console.print("Pass II: Checking dependency for important packages", tui.COLOR_INFO)
        self.dep_tree.resolve_packages(important_packages)

        __num_now = self.dep_tree.selected_count
        console.print(f"Dependencies Selected for 'important' : {__num_now - __num_required}")
        __num_required = __num_now

        # Mark everything not already 'required' as 'important'.
        for _pkg in self.dep_tree.selected_pkgs:
            if _pkg != self.dep_tree.selected_pkgs[_pkg]['Package']:
                continue
            if self.dep_tree.selected_pkgs[_pkg].priority != 'required':
                self.dep_tree.selected_pkgs[_pkg].priority = 'important'

        # Sanity check — no package should carry any other priority string at this point.
        for _pkg in self.dep_tree.selected_pkgs:
            _priority = self.dep_tree.selected_pkgs[_pkg].priority
            if _priority != 'required' and _priority != 'important':
                console.print(f"Package {_pkg} with unexpected priority :{_priority}")

        # --- Pass III: manual list (per-group) ------------------------------
        # pkg.list may be flat (legacy — implicit `[base]`) or INI-style
        # with named `[group]` sections.  Either way we iterate groups
        # in declaration order, resolving each one's seeds and
        # crediting the newly-pulled-in canonical names to that group's
        # entry in `pkg_group_pkg_names`.  Non-`[base]` groups end up
        # in `pkg_group_extras_pkg_names` — same exclusion semantics
        # as pool extras (subtracted from `_base_include`, filtered
        # from live install batches) but conflicts ARE enforced
        # because group-level packages are not mutually exclusive
        # within a single install run.
        console.print("Pass III: Checking dependency for manually selected packages", tui.COLOR_INFO)

        console.print(f"Parsing {self.config.pkglist_path}...")
        try:
            _pkg_groups = utils.parse_pkg_list_groups(self.config.pkglist_path)
            _pkg_group_meta = utils.parse_pkg_list_group_meta(self.config.pkglist_path)
        except (OSError, ValueError) as e:
            console.print(f"ERROR: cannot read package list {self.config.pkglist_path}: {e}")
            logger.error(f"parse_pkg_list_groups({self.config.pkglist_path}): {e}")
            _pkg_groups = {}
            _pkg_group_meta = {}
        self.dep_tree.pkg_group_meta = _pkg_group_meta

        _total_manual_added = 0
        for _group, _seeds in _pkg_groups.items():
            _pre_group_keys = set(self.dep_tree.selected_pkgs.keys())
            # Filter out names already in selected_pkgs (required /
            # important / earlier groups) — resolve_packages no-ops on
            # them anyway but the count stays accurate.
            _new_seeds = [_p for _p in _seeds if _p not in _pre_group_keys]
            if _new_seeds:
                self.dep_tree.resolve_packages(_new_seeds)
            # Per-group package names = delta in selected_pkgs.keys(),
            # collapsed to canonical names.  selected_pkgs is keyed by
            # BOTH real Package: names AND every virtual Provides: name
            # — for downstream code that only reasons about what dpkg
            # actually installs (tasksel/task_avail, base_include,
            # install batches), the virtual aliases are noise that
            # masquerade as separate packages.
            #
            # Why this matters specifically for tasksel: the .desc's
            # `Key:` list ends up downstream of these names; tasksel's
            # `task_avail()` calls `apt-cache dumpavail` to verify each
            # Key is installable, and dumpavail only emits real
            # `Package:` stanzas (Provides aren't separate stanzas).
            # A single virtual name in Key → task hidden from menu.
            # Caught 2026-05-15 — athena-development-tools had 10/49
            # Key entries that were virtuals (cpp, c++-compiler,
            # git-core, …); tasksel silently filtered the whole task.
            _delta_keys = (
                set(self.dep_tree.selected_pkgs.keys()) - _pre_group_keys
            )
            self.dep_tree.pkg_group_pkg_names[_group] = {
                self.dep_tree.selected_pkgs[_n]['Package']
                for _n in _delta_keys
                if _n in self.dep_tree.selected_pkgs
            }
            _delta = len(self.dep_tree.pkg_group_pkg_names[_group])
            _total_manual_added += _delta
            console.print(
                f"  [{_group}] {len(_seeds)} seed(s) → {_delta} canonical "
                "package(s) (delta from prior groups + required/important)"
            )

        # Non-base groups: their packages get filtered from
        # _base_include + live install batches but stay in the pool.
        self.dep_tree.pkg_group_extras_pkg_names = set().union(*[
            _names for _group, _names in self.dep_tree.pkg_group_pkg_names.items()
            if _group != 'base'
        ])

        console.print(
            f"Manual: {len(_pkg_groups)} group(s), "
            f"{_total_manual_added} total canonical added"
        )

        __num_total = self.dep_tree.selected_count
        console.print(f"Dependencies for manually added packages : {__num_total - __num_required}")

        # --- Pass IV: live.list ------------------------------------------------
        # Snapshot pkg.list closure here — anything pulled in beyond this point
        # by live.list / installer.list goes into the corresponding exclusive
        # set.  Required + important + pkg.list are ALL in pkg_closure (as
        # intended — exclusivity is computed against everything pkg.list
        # transitively needs, not just the literal pkg.list lines).
        _pkg_closure = set(self.dep_tree.selected_pkgs.keys())

        console.print("Pass IV: Checking dependency for live-only packages", tui.COLOR_INFO)
        _live_list = self._read_pkg_list(self.config.livelist_path,
                                         already_selected=_pkg_closure)
        if _live_list:
            self.dep_tree.resolve_packages(_live_list)
        self.dep_tree.live_exclusive_pkg_names = (
            set(self.dep_tree.selected_pkgs.keys()) - _pkg_closure
        )
        console.print(
            f"Live-exclusive packages : {len(self.dep_tree.live_exclusive_pkg_names)}"
        )

        # --- Pass V: installer.list (mixed deb + udeb per-entry dispatch) ----
        # installer.list contains BOTH udeb names (for the installer
        # ramdisk) AND deb names like efibootmgr/grub-pc-bin
        # (for the target system to apt-pull at install time).  Each entry
        # is looked up in both hashtables:
        #   - udeb match → goes into the udeb seed set (Pass VI below)
        #   - deb match  → goes into the deb dep tree (this pass) and ends
        #                  up in installer_exclusive_pkg_names
        #   - both match → both happen
        console.print("Pass V: Dispatching installer.list (deb arm)", tui.COLOR_INFO)
        _installer_raw = self._read_pkg_list(
            self.config.installerlist_path, already_selected=set())
        _udeb_table = self.cache.udeb_hashtable
        _deb_table  = self.cache.package_hashtable
        _installer_deb_names: list = []
        _installer_udeb_names: list = []
        _installer_unknown: list = []
        for _name in _installer_raw:
            _in_deb  = _name in _deb_table
            _in_udeb = _name in _udeb_table
            if _in_deb:
                _installer_deb_names.append(_name)
            if _in_udeb:
                _installer_udeb_names.append(_name)
            if not (_in_deb or _in_udeb):
                _installer_unknown.append(_name)
        if _installer_unknown:
            console.print(
                f"WARNING: installer.list has {len(_installer_unknown)} "
                f"name(s) not in deb or udeb cache: "
                f"{', '.join(_installer_unknown[:5])}"
                f"{'…' if len(_installer_unknown) > 5 else ''}"
            )
            logger.warning(
                f"installer.list unknown names: {_installer_unknown}"
            )
        # Filter deb arm to entries not already in the closure (Pass IV may
        # have pulled some in transitively).
        _installer_deb_new = [
            n for n in _installer_deb_names
            if n not in self.dep_tree.selected_pkgs
        ]
        if _installer_deb_new:
            self.dep_tree.resolve_packages(_installer_deb_new)
        self.dep_tree.installer_exclusive_pkg_names = (
            set(self.dep_tree.selected_pkgs.keys())
            - _pkg_closure
            - self.dep_tree.live_exclusive_pkg_names
        )
        console.print(
            f"Installer-exclusive deb packages : {len(self.dep_tree.installer_exclusive_pkg_names)}"
        )

        __num_total = self.dep_tree.selected_count
        console.print(f"Total Selected (deb) Packages : {__num_total}", tui.COLOR_HIGHLIGHT)
        _spiner.done()

        # --- Pass VI: udeb world (parallel dep tree) -------------------------
        # Build a parallel DependencyTree against Cache.udeb_view() (which
        # presents udeb_hashtable as package_hashtable).  Seeds = the
        # udeb-priority required + important sets + every udeb-bound name
        # in installer.list.  Resolves transitively through the udeb dep
        # graph into udeb_dep_tree.selected_pkgs.
        console.print("Pass VI: Resolving udeb (installer ramdisk) tree", tui.COLOR_INFO)
        _udeb_spiner = Spinner("Resolving udeb dependencies")
        _udeb_view = self.cache.udeb_view()
        self.udeb_dep_tree = dependencytree.DependencyTree(
            _udeb_view, select_recommended=False,
            arch=self.config.arch,
            build_profiles=self.config.build_profiles,
            # Udeb world commonly has multi-Package-name
            # "providers" that are really just kernel-ABI variants of the
            # same module (ext4-modules-6.1.0-{NN}-amd64-di etc.).  Auto-
            # pick the highest version across names instead of prompting.
            auto_pick_highest_when_ambiguous=True,
            pins=_pins,
        )
        _udeb_seeds_required = list(self.cache.udeb_required)
        _udeb_seeds_important = list(self.cache.udeb_important)
        if _udeb_seeds_required:
            self.udeb_dep_tree.resolve_packages(_udeb_seeds_required)
        if _udeb_seeds_important:
            self.udeb_dep_tree.resolve_packages(_udeb_seeds_important)
        _udeb_seeds_from_list = [
            n for n in _installer_udeb_names
            if n not in self.udeb_dep_tree.selected_pkgs
        ]
        if _udeb_seeds_from_list:
            self.udeb_dep_tree.resolve_packages(_udeb_seeds_from_list)
        console.print(
            f"Udeb closure: {self.udeb_dep_tree.selected_count} udeb(s) "
            f"(seeds: {len(_udeb_seeds_required)} required + "
            f"{len(_udeb_seeds_important)} important + "
            f"{len(_installer_udeb_names)} from installer.list)",
            tui.COLOR_HIGHLIGHT,
        )
        _udeb_spiner.done()

        # --- Pass VII: pool.list (deb-only, conflicts not enforced) ----------
        # Pool extras: shipped in the apt pool on the installer ISO but
        # never installed in any chroot.  Goes through the resolver
        # normally (Depends pulled in transitively) BUT with
        # `check_conflicts=False` so mutually-conflicting bootloader
        # metas (grub-pc + grub-efi-amd64) coexist in selected_pkgs.
        # `validate_selection` skips Breaks/Conflicts where either side
        # is in `pool_extras_pkg_names` — see dependencytree.py for the
        # membership-based bypass and pool.list for the contract.
        console.print("Pass VII: Resolving pool.list (deb arm, conflicts disabled)", tui.COLOR_INFO)
        _pool_raw = self._read_pkg_list(
            self.config.poollist_path, already_selected=set())
        _pool_deb_names = []
        _pool_unknown   = []
        for _name in _pool_raw:
            if _name in _deb_table:
                _pool_deb_names.append(_name)
            else:
                _pool_unknown.append(_name)
        if _pool_unknown:
            console.print(
                f"WARNING: pool.list has {len(_pool_unknown)} name(s) not in deb cache: "
                f"{', '.join(_pool_unknown[:5])}{'…' if len(_pool_unknown) > 5 else ''}"
            )
            logger.warning(f"pool.list unknown names: {_pool_unknown}")
        _pre_pool_closure = set(self.dep_tree.selected_pkgs.keys())
        if _pool_deb_names:
            self.dep_tree.resolve_packages(
                _pool_deb_names, check_conflicts=False)
        self.dep_tree.pool_extras_pkg_names = (
            set(self.dep_tree.selected_pkgs.keys()) - _pre_pool_closure
        )
        console.print(
            "Pool extras (shipped in pool, not installed) : "
            f"{len(self.dep_tree.pool_extras_pkg_names)}"
        )

        # When [Build] IncludeRecommends is on (default)
        if self.config.include_recommends:
            _added = self.dep_tree.pull_recommends_extras()
            if _added:
                console.print(
                    f"EXTRAS: pulled {_added} recommended package(s) into the repo ", tui.COLOR_INFO)
            else:
                console.print("EXTRAS: 0 recommends added — if unexpected check logs ", tui.COLOR_INFO)
        else:
            console.print("EXTRAS: disabled — check IncludeRecommends", tui.COLOR_INFO)

        # --- Validation ---------------------------------------------------------
        console.print("Checking Breaks and Conflicts...")
        if not self.dep_tree.validate_selection():
            _resp = Prompt(
                PROMPT_YESNO,
                "There are one or more dependency validation failures, Proceed?",
                informational=True,   # UX-05f: safe to auto-yes under --yes
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                self.flags.dep_check_ready = False
                return

        # Write the resolved binary package list to disk for post-mortem inspection.
        try:
            with open(os.path.join(self.config.dir_log, 'selected_packages.list'), 'w') as f:
                for pkg in self.dep_tree.selected_pkgs:
                    # Skip virtual-package aliases — log only canonical names.
                    if pkg != self.dep_tree.selected_pkgs[pkg]['Package']:
                        continue
                    f.write(str(self.dep_tree.selected_pkgs[pkg]) + '\n\n')
        except OSError as e:
            console.print("ERROR: cannot write selected_packages.list")
            logger.error(f"selected_packages.list write: {e}")
            return

        # --- Source mapping -----------------------------------------------------
        # Map each selected binary package to its upstream source.  This populates
        # self.dep_tree.selected_srcs which all subsequent stages consume.
        console.print("Parsing Source Packages...", tui.COLOR_INFO)

        if not self.dep_tree.parse_sources():
            _resp = Prompt(
                PROMPT_YESNO,
                "There are one or more source parse failures, Proceed?",
                informational=True,   # UX-05f: safe to auto-yes under --yes
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        # parse_sources for the udeb tree too.  Sources are universal
        # (same source produces both .deb and .udeb), so the shared
        # source_hashtable already has the records we need.
        # udeb_dep_tree.selected_srcs is populated independently;
        # downstream consumers (source sync / source build) will
        # iterate over the UNION of both trees' selected_srcs.
        if self.udeb_dep_tree is not None:
            console.print("Parsing Udeb Source Packages...", tui.COLOR_INFO)
            if not self.udeb_dep_tree.parse_sources():
                _resp = Prompt(
                    PROMPT_YESNO,
                    "There are one or more udeb source parse failures, Proceed?",
                    informational=True,   # UX-05f
                ).get_response()
                if _resp.lower() not in ('y', 'yes'):
                    return
            console.print(
                f"Udeb sources: {len(self.udeb_dep_tree.selected_srcs)} ",
                tui.COLOR_HIGHLIGHT,
            )

        # EXTRAS: identify which sources are extras-only so source_build
        # default skips them and `source_build recommended` builds only them.
        _extras_only = self.dep_tree.derive_extras_src_names()
        if self.dep_tree.extras_pkg_names:
            console.print(f"EXTRAS: {_extras_only} source(s) are extras-only ", tui.COLOR_INFO)

        # Derive live/installer-exclusive *source* names so source-build
        # / chroot-build subset filters can route them.
        _live_only, _installer_only = self.dep_tree.derive_subset_exclusive_src_names()
        if _live_only or _installer_only:
            console.print(
                f"SUBSETS: {_live_only} live-exclusive src, "
                f"{_installer_only} installer-exclusive src",
                tui.COLOR_INFO,
            )

        # Apply per-package skip_test flag from config (suppresses 'nocheck' build opt).
        for _pkg in self.config.skip_build_test:
            if _pkg in self.dep_tree.selected_srcs:
                self.dep_tree.selected_srcs[_pkg].skip_test = True

        # --- Patch discovery ----------------------------------------------------
        self._refresh_patches()

        # Write source lists to disk for auditing.
        try:
            with open(os.path.join(self.config.dir_log, 'selected_sources.list'), 'w') as fa:
                with open(os.path.join(self.config.dir_log, 'source_file.list'), 'w') as fb:
                    for _pkg in self.dep_tree.selected_srcs:
                        fa.write(str(self.dep_tree.selected_srcs[_pkg]) + '\n\n')
                        for _file in self.dep_tree.selected_srcs[_pkg].files:
                            fb.write(f"{_file}: {self.dep_tree.selected_srcs[_pkg].files[_file]}\n")
        except OSError as e:
            console.print("ERROR: cannot write source lists")
            logger.error(f"source lists write: {e}")
            return

        console.print(f"Selected {len(self.dep_tree.selected_srcs)} source packages", tui.COLOR_HIGHLIGHT)

        # ── SELECT-LOCK: two-stage closure guard ────────────────────────────
        # Stage (a) = the closure we just resolved; stage (b) = the signed
        # lockfile loaded up front.  Asymmetric: a closure SHRINK (seed edit,
        # snapshot dropping a dep, or IncludeRecommends off) BLOCKS — the
        # removed packages are mirror-deprecation candidates the operator must
        # acknowledge via `cache select` / `cache restore` / `cache purge-state`.
        # Additions are low-impact (absorbed + warned).  First run bootstraps.
        if self.config.build_mode != 'build':
            # `accept-removals` (set by `cache select`) turns the shrink BLOCK
            # into an accepted re-baseline: the lockfile is rewritten to the new
            # (smaller) closure and the dropped packages become mirror-
            # deprecation candidates on the next publish.  A raw `cache parse`
            # never accepts a shrink.
            _accept = 'accept-removals' in args
            _fresh = selection_lock.build_closure(
                self.dep_tree, self.udeb_dep_tree, self.config)
            _action, _added, _removed = selection_lock.classify(
                _lstatus, _lock, _fresh)
            # ── LEDGER-01: lifecycle layer inputs ───────────────────────
            # Source→version union across BOTH trees (udeb-only sources
            # like anna/debootstrap included) + the snapshot pin.  The
            # touch itself runs on the non-blocked branches below.
            _lc_sel = {n: str(s.version)
                       for n, s in self.dep_tree.selected_srcs.items()}
            if self.udeb_dep_tree is not None:
                for _n, _s in self.udeb_dep_tree.selected_srcs.items():
                    _lc_sel.setdefault(_n, str(_s.version))
            _lc_pin = getattr(self.config, 'snapshot_timestamp_config', '')
            if _lc_pin == 'latest':
                try:
                    _lc_pin = utils.resolve_snapshot_timestamp(
                        self.config) or 'latest'
                except Exception:
                    _lc_pin = 'latest'
            _lc_log = os.path.join(self.config.dir_log, 'build')

            def _lc_touch():
                _st = utils.lifecycle_touch_selected(_lc_log, _lc_sel, _lc_pin)
                _changed = sum(_v for _k, _v in _st.items()
                               if _k != 'unchanged')
                if _changed:
                    console.print(
                        f"lifecycle: {_st['created']} created, "
                        f"{_st['stamped']} stamped, {_st['rolled']} version-"
                        f"roll(s), {_st['reselected']} re-selected "
                        f"({_st['unchanged']} unchanged)", tui.COLOR_INFO)
            if _action == selection_lock.ACTION_BLOCK and not _accept:
                _rb = sorted(_removed['bins'])
                _rs = sorted(_removed['srcs'])
                console.print(
                    f"cache parse: BLOCKED — the selection closure SHRANK vs "
                    f"the signed selection.state: {len(_rb)} binary(ies) and "
                    f"{len(_rs)} source(s) would be dropped.", tui.COLOR_ERROR)
                for _n in _rb[:20]:
                    console.print(f"    - bin {_n}", tui.COLOR_ERROR)
                if len(_rb) > 20:
                    console.print(f"    … (+{len(_rb) - 20} more bins)",
                                  tui.COLOR_ERROR)
                for _n in _rs[:20]:
                    console.print(f"    - src {_n}", tui.COLOR_ERROR)
                if len(_rs) > 20:
                    console.print(f"    … (+{len(_rs) - 20} more srcs)",
                                  tui.COLOR_ERROR)
                console.print(
                    "  These are mirror-deprecation candidates.  Choose one:\n"
                    "    1. `cache select accept` — accept the removal (updates "
                    "the lockfile + marks them deprecated on publish)\n"
                    "    2. `cache restore` — regenerate the list files from "
                    "the lockfile (undo the edit)\n"
                    "    3. `cache purge-state` — re-baseline the selection "
                    "authority (heavy mirror impact)", tui.COLOR_WARNING)
                self.flags.dep_check_ready = False
                _spiner.done()
                return
            _state = selection_lock.assemble_state(
                self.dep_tree, self.udeb_dep_tree, self.config, closure=_fresh)
            if _action == selection_lock.ACTION_BLOCK and _accept:
                selection_lock.write_selection_state(self.config, _state)
                # LEDGER-01 intent-at-accept: record the deprecation on the
                # dropped sources' build records NOW (before any publish),
                # then touch the still-selected set.
                _dep_n = utils.lifecycle_mark_deprecated(
                    _lc_log, _removed['srcs'], _lc_pin)
                _lc_touch()
                console.print(
                    f"cache select accept: re-baselined selection.state — "
                    f"{len(_removed['bins'])} binary(ies) / "
                    f"{len(_removed['srcs'])} source(s) DROPPED "
                    f"({_dep_n} record(s) marked deprecated); they become "
                    "mirror-deprecation candidates on the next `mirror "
                    "publish`.", tui.COLOR_HIGHLIGHT)
                self.flags.dep_check_ready = True
                _spiner.done()
                persistence.save_session(self, self.config.dir_cache)
                return
            if _action == selection_lock.ACTION_BOOTSTRAP:
                selection_lock.write_selection_state(self.config, _state)
                console.print(
                    f"selection.state: created — {len(_fresh['bins'])} "
                    f"binary(ies) / {len(_fresh['srcs'])} source(s) locked",
                    tui.COLOR_HIGHLIGHT)
            else:  # ACTION_REFRESH (unchanged or additions-only)
                if _added['bins'] or _added['srcs']:
                    console.print(
                        f"selection.state: absorbed {len(_added['bins'])} new "
                        f"binary(ies) / {len(_added['srcs'])} new source(s) "
                        "(additions are low-impact)", tui.COLOR_WARNING)
                selection_lock.write_selection_state(self.config, _state)
            # LEDGER-01: stamp the lifecycle layer on every selected source
            # (bootstrap + refresh paths; the accept path touched above).
            _lc_touch()

        self.flags.dep_check_ready = True
        # UX-04: persist Cache + DT to dir_cache/session.pkl.gz so
        # `resume` (next process) can skip cache build + cache parse.
        # Best-effort: a save failure is logged but the build continues.
        persistence.save_session(self, self.config.dir_cache)

    def cmd_cache_select(self, *args):
        """COMP-06 — interactive package-set selector (`cache select`).

        Opens a `select` tab where the operator toggles packages in
        `config/pkg.list` and adds new ones from the cache, then saves.
        Requires the cache (for metadata) — gate on cache_ready.
        Interactive-only: needs the curses tab + key-interceptor API,
        absent on the headless Cli backend.

        `cache select accept` — the sanctioned way to ACCEPT a selection
        SHRINK after an edit: re-resolves and rewrites selection.state to the
        new (smaller) closure, so the dropped packages become mirror-
        deprecation candidates on the next publish.  This is the only path
        that accepts a shrink (a raw `cache parse` BLOCKs on one)."""
        if args and args[0] == 'accept':
            if not self.flags.cache_ready or self.cache is None:
                console.print("Run 'cache build' first (accept re-resolves the "
                              "selection)")
                return
            # Re-resolve + accept the shrink (impact is printed by the guard).
            return self.cmd_parse_dependency('force', 'accept-removals')
        if not self.flags.cache_ready or self.cache is None:
            console.print("Run 'cache build' first (selector needs package metadata)")
            return
        if not hasattr(tui.tui_instance, 'set_tab_key_handler'):
            console.print("cache select: interactive selector needs the curses TUI "
                          "(not available in --headless mode); edit "
                          f"{self.config.pkglist_path} by hand")
            return
        from select_packages import SelectPackages
        # Back up the editable lists up front so a cancelled apply (or discard)
        # is a true rollback — "cancel = no changes".
        _backup = self._backup_select_lists()
        _sel = SelectPackages(self.config, self.cache, tui.tui_instance)
        _sel.activate()
        # Block the shell thread while the operator edits on the dispatcher
        # thread; resume with their exit intent.  The selector wrote the lists
        # already (on save / save&apply); we run the parse + accept prompt here
        # in normal console context.
        _intent = _sel.wait_for_done()
        try:
            if _intent == 'apply':
                self._select_apply_transaction(_backup)
            elif _intent == 'discard':
                self._restore_select_lists(_backup)
                console.print("cache select: discarded — list files reverted, "
                              "no changes.", tui.COLOR_INFO)
            else:   # 'quit' — no pending in-memory edits
                console.print("cache select: closed (no changes).",
                              tui.COLOR_INFO)
        finally:
            self._cleanup_select_backup(_backup)

    # ─── cache select save&apply transaction ─────────────────────────────
    def _backup_select_lists(self) -> 'dict':
        """Copy pkg.list + pool.list to temp files for transactional rollback.
        Returns {label: (orig_path, backup_path)} for files that exist."""
        import shutil
        import tempfile
        _out: dict = {}
        for _label, _path in (('pkg', self.config.pkglist_path),
                              ('pool', self.config.poollist_path)):
            if os.path.exists(_path):
                _fd, _tmp = tempfile.mkstemp(prefix='.select-bak.')
                os.close(_fd)
                try:
                    shutil.copy2(_path, _tmp)
                    _out[_label] = (_path, _tmp)
                except OSError as _e:
                    logger.warning(f"cache select backup {_path}: {_e}")
        return _out

    def _restore_select_lists(self, backup: 'dict') -> None:
        import shutil
        for _label, (_path, _tmp) in backup.items():
            try:
                shutil.copy2(_tmp, _path)
            except OSError as _e:
                logger.error(f"cache select restore {_path}: {_e}")

    def _cleanup_select_backup(self, backup: 'dict') -> None:
        for _label, (_path, _tmp) in backup.items():
            try:
                os.remove(_tmp)
            except OSError:
                pass

    def _select_apply_transaction(self, backup: 'dict') -> None:
        """Re-parse after a `cache select` save&apply, preview the closure
        impact, and require a typed `accept` for a closure SHRINK (large
        impact = mirror deprecations).  Cancel reverts the list files."""
        console.print("cache select: re-resolving the selection…",
                      tui.COLOR_INFO)
        self.cmd_parse_dependency('force')
        if self.flags.dep_check_ready:
            # REFRESH — additions-only or no closure change; already applied.
            console.print("cache select: applied — selection.state updated "
                          "(no closure shrink).", tui.COLOR_HIGHLIGHT)
            return
        # BLOCKED — the guard already listed the dropped packages as
        # mirror-deprecation candidates above.  Require an explicit typed
        # accept (large impact), else roll the edit back.
        _resp = Prompt(
            tui.PROMPT_INPUT,
            "Type 'accept' to apply this removal (the dropped packages are "
            "deprecated on the mirror at next publish), or anything else to "
            "cancel",
        ).get_response()
        if _resp.strip().lower() == 'accept':
            self.cmd_parse_dependency('force', 'accept-removals')
            console.print("cache select: ACCEPTED — selection.state "
                          "re-baselined.", tui.COLOR_HIGHLIGHT)
        else:
            self._restore_select_lists(backup)
            self.flags.dep_check_ready = False
            console.print("cache select: CANCELLED — list files reverted, "
                          "no changes.", tui.COLOR_WARNING)

    def cmd_cache_restore(self, *args):
        """`cache restore` — regenerate config/{pkg,live,installer,pool}.list
        from the signed selection.state (the selection AUTHORITY).

        The escape hatch when a list edit caused the parse to BLOCK: it
        reverts the files to exactly what the lockfile pins, so a re-parse
        shows zero closure delta.  Requires a verified lockfile — there is
        nothing to restore from if it's missing/tampered."""
        _force = 'force' in args
        _lock, _status = selection_lock.read_selection_state(self.config)
        if _status != selection_lock.STATUS_OK or _lock is None:
            console.print(
                f"cache restore: selection.state is {_status} — nothing "
                "trustworthy to restore from.  Run `cache parse` to bootstrap "
                "it, or fix/restore the file.", tui.COLOR_ERROR)
            return
        if not _force:
            _resp = Prompt(
                PROMPT_YESNO,
                "Overwrite config/{pkg,live,installer,pool}.list from "
                "selection.state?  Operator edits to those files are LOST.",
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                console.print("cache restore: aborted — no files changed.")
                return
        _written = selection_lock.restore_list_files(self.config, _lock)
        console.print(
            f"cache restore: regenerated {len(_written)} list file(s) from "
            "selection.state.  Run `cache parse` to confirm zero delta.",
            tui.COLOR_HIGHLIGHT)
        # The on-disk lists changed under any resolved tree — force a re-parse.
        self.flags.dep_check_ready = False

    def cmd_cache_purge_state(self, *args):
        """`cache purge-state` — delete selection.state to re-baseline the
        selection authority.  DISTINCT from `cache purge` (which clears the
        re-downloadable apt cache).

        HEAVY: the next `cache parse` bootstraps a brand-new lockfile from
        whatever the lists currently resolve to, so any package the lockfile
        was tracking as owned-but-dropped is FORGOTTEN as a deprecation
        candidate — the mirror keeps owning it with no signal to release.
        Use only to intentionally re-baseline."""
        _force = 'force' in args
        _path = selection_lock.selection_state_path(self.config)
        if not os.path.exists(_path):
            console.print(f"cache purge-state: no lockfile at {_path} "
                          "(already re-baselined).")
            return
        if not _force:
            _resp = Prompt(
                PROMPT_YESNO,
                "Delete selection.state?  This re-baselines the selection "
                "authority; packages it tracked as dropped will NOT be marked "
                "deprecated on the mirror.  Proceed?",
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                console.print("cache purge-state: aborted — lockfile kept.")
                return
        try:
            os.remove(_path)
            console.print(
                "cache purge-state: selection.state deleted — next `cache "
                "parse` re-bootstraps it.", tui.COLOR_HIGHLIGHT)
        except OSError as _e:
            console.print(f"cache purge-state: cannot delete {_path}: {_e}",
                          tui.COLOR_ERROR)

    def cmd_cache_info(self, *args):
        """`cache info <pkg>` — concise package facts + relations.

        Looks the name up in the cache and prints identity (version,
        arch, section, priority, installed size, source), the one-line
        description, and the relation fields (Depends, Pre-Depends,
        Recommends, Suggests, Provides, Conflicts, Breaks, Replaces).
        Alternatives render as `a | b`.  If the dep tree is built, also
        notes whether the package is in the selected closure and lists
        a few reverse-dependencies."""
        if not self.flags.cache_ready or self.cache is None:
            console.print("Run 'cache build' first")
            return
        if not args:
            console.print("Usage: cache info <pkg_name>")
            return
        name = args[0]
        pkgs = self.cache.get_packages(name)
        if not pkgs:
            console.print(f'cache info: "{name}" not found in cache')
            return

        from print_commands import _fmt_dep, _fmt_dep_group
        pkg = pkgs[0]   # best candidate (highest version)

        def _size(p):
            try:
                kb = int(p.get('Installed-Size', '0') or 0)
            except (ValueError, TypeError):
                return '?'
            if kb < 1024:
                return f'{kb} KB'
            return f'{kb / 1024.0:.1f} MB'

        console.print(f'  {pkg.get("Package", name)}  '
                      f'{pkg.get("Version", "?")}  ({pkg.get("Architecture", "?")})',
                      tui.COLOR_HIGHLIGHT)
        console.print(f'    Section      : {pkg.get("Section", "-")}')
        console.print(f'    Priority     : {pkg.get("Priority", "-")}')
        console.print(f'    Installed    : {_size(pkg)}')
        _src = pkg.get('Source', '') or pkg.get('Package', name)
        console.print(f'    Source       : {_src}')
        if len(pkgs) > 1:
            _vers = ', '.join(str(p.get('Version', '?')) for p in pkgs[:6])
            console.print(f'    Versions     : {len(pkgs)} available ({_vers})')
        _desc = (pkg.get('Description', '') or '').split('\n')[0].strip()
        if _desc:
            console.print(f'    Description  : {_desc}')

        # Relations.  `.depends`/`.pre_depends`/... are single-tuple
        # lists; `.alt_depends`/`.conflicts`/... are groups (lists of
        # tuples) rendered with ` | `.
        def _line(label, singles, groups=()):
            parts = [_fmt_dep(t) for t in singles]
            parts += [_fmt_dep_group(g) for g in groups]
            if parts:
                console.print(f'    {label:<13}: {", ".join(parts)}')

        _line('Depends',     getattr(pkg, 'depends', []),      getattr(pkg, 'alt_depends', []))
        _line('Pre-Depends', getattr(pkg, 'pre_depends', []),  getattr(pkg, 'alt_pre_depends', []))
        _line('Recommends',  getattr(pkg, 'recommends', []))
        _line('Suggests',    getattr(pkg, 'suggests', []))
        _line('Provides',    [], getattr(pkg, 'provides', []))
        _line('Conflicts',   [], getattr(pkg, 'conflicts', []))
        _line('Breaks',      [], getattr(pkg, 'breaks', []))
        _line('Replaces',    [], getattr(pkg, 'replaces', []))

        # Dep-tree context (only if parsed).
        if self.dep_tree is not None:
            selected = name in self.dep_tree.selected_pkgs
            console.print(f'    In closure   : {"yes" if selected else "no"}')
            rdeps = getattr(pkg, 'depended_by', []) or []
            if rdeps:
                _shown = ', '.join(rdeps[:8])
                _more = f'  (+{len(rdeps) - 8} more)' if len(rdeps) > 8 else ''
                console.print(f'    Required by  : {_shown}{_more}')
