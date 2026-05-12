# (C) Athena Linux Project
"""
build.py — top-level orchestrator for the Athena Linux build system.

Responsibilities:
  - Parse build configuration and APT cache
  - Resolve the full dependency tree for the target package list
  - Download upstream source archives
  - Manage the Docker build container image
  - Build each selected source package inside a clean container
  - Optionally tunnel (download prebuilt) binary packages from the base Debian repo
  - Expose all of the above as interactive TUI commands

Typical operator workflow:
    cache build → dep parse → source download → container init → source build

Each step sets a flag in _progress_flags so later commands can verify prerequisites
without re-running earlier work.
"""

import faulthandler
faulthandler.enable(open('/tmp/athena_crash.log', 'w'))

import tui
import datetime
import os
import glob
import shutil
import subprocess
import time
import sys
from typing import Optional

import apt_pkg

# Local imports
import utils
from utils import BuildConfig
from cache import Cache

import buildcontainer
import dependencytree
import buildsystem
import installer_chroot
import iso_installer
import signal


import logging
from tui import Tui, console, Prompt, PROMPT_YESNO, PROMPT_PASSWORD, Spinner, ProgressBar, Exit
from tui import setup_file_logging

logger = logging.getLogger('athena')

asciiart_logo = '╔══╦╗╔╗─────────╔╗╔╗\n' \
                '║╔╗║╚╣╚╦═╦═╦╦═╗─║║╠╬═╦╦╦╦╦╗\n' \
                '║╠╣║╔╣║║╩╣║║║╬╚╗║╚╣║║║║║╠║╣\n' \
                '╚╝╚╩═╩╩╩═╩╩═╩══╝╚═╩╩╩═╩═╩╩╝'

# TODO: make all apt_pkg.parse functions arch specific


class BuildFlags:
    """Tracks which pipeline stages have completed successfully.

    Each flag is set to True at the end of its corresponding command handler
    and checked as a prerequisite by later stages.  This prevents commands
    from running on stale or missing state without repeating the earlier work.
    """

    def __init__(self):
        self.cache_ready: bool = False             # build_cache completed
        self.dep_check_ready: bool = False         # parse_dependency completed
        self.download_ready: bool = False          # source_download completed
        self.build_container_ready: bool = False   # build_container initialised
        self.source_build_ready: bool = False      # source_build completed
        self.signing_key_verified: bool = False    # signing key sign+verify roundtrip
        self.chroot_ready: bool = False            # build_chroot completed (live)
        self.chroot_verified: bool = False         # build_chroot + verify_chroot all checks passed
        # COMP-01b phase 5: installer chroot built from udeb closure.
        # Independent of chroot_ready/_verified — the two chroots have
        # different lifecycles (live = squashfs payload; installer = initrd).
        self.chroot_installer_ready: bool = False  # build_chroot installer completed
        # Set on a successful `iso build live` / `iso build installer`.  Lets
        # `autorun live` / `autorun installer` gate the ISO-build step the
        # same way they gate every other stage — without these the autorun
        # step driver has no flag to check, so a silent ISO-build failure
        # would not be caught.
        self.iso_live_ready: bool = False
        self.iso_installer_ready: bool = False

    def __str__(self) -> str:
        """Return a compact one-line status string for display in the TUI."""
        fields = ['cache_ready', 'dep_check_ready', 'download_ready',
                  'build_container_ready', 'source_build_ready',
                  'signing_key_verified',
                  'chroot_ready', 'chroot_verified', 'chroot_installer_ready',
                  'iso_live_ready', 'iso_installer_ready']
        return '  '.join(f"[{'✓' if getattr(self, f) else '·'}] {f.replace('_ready', '')}" for f in fields)

class BuildSession:
    """Owns the full pipeline state and the cmd_* command handlers the TUI
    registers.  Replaces the prior module-level globals (build_config,
    build_cache, dependency_tree, build_container, _tui, _progress_flags)
    so handlers can be exercised without standing up the curses TUI.
    """

    def __init__(self, config: BuildConfig, tui_inst: 'Tui') -> None:
        self.config: BuildConfig = config
        self.tui: 'Tui' = tui_inst
        self.cache: 'Optional[Cache]' = None
        self.dep_tree: 'Optional[dependencytree.DependencyTree]' = None
        # COMP-01b phase 3: parallel dep tree resolved against the udeb
        # world (Cache.udeb_hashtable via Cache.udeb_view()).  Populated
        # by cmd_parse_dependency after the deb passes complete.  Stays
        # None until then; consumers MUST gate on dep_check_ready before
        # touching it.
        self.udeb_dep_tree: 'Optional[dependencytree.DependencyTree]' = None
        self.container: 'Optional[buildcontainer.BuildContainer]' = None
        self.flags: BuildFlags = BuildFlags()
        self.last_source_build_counts: 'Optional[dict]' = None

    @staticmethod
    def _read_pkg_list(path: str, already_selected: set) -> list:
        """Read a pkg-list file (one package per line, # comments, blanks
        ignored) and return entries NOT already in ``already_selected``.

        Used by Pass IV (live.list) and Pass V (installer.list) to feed
        only the new requests into resolve_packages — entries that are
        already in the closure are no-ops and skipping them keeps the
        resolve_packages invocation tight.

        Missing or unreadable file → empty list + a warning logged; the
        caller treats that as "no exclusive packages".  installer.list
        starts empty by design (COMP-01a populates it).
        """
        try:
            _raw = utils.readfile(path).split('\n')
        except OSError as e:
            console.print(f"WARNING: cannot read pkg list {path} — treating as empty")
            logger.warning(f"_read_pkg_list({path}): {e}")
            return []
        _out = []
        for _line in _raw:
            _name = _line.strip()
            if not _name or _name.startswith('#'):
                continue
            if _name in already_selected:
                continue
            _out.append(_name)
        return _out

    def cmd_build_cache(self):
        """Fetch and parse the upstream APT package indices into an in-memory cache.

        Downloads the binary and source Packages files for the configured base
        distribution and architecture, then indexes them for fast lookup during
        dependency resolution.  Must be run before parse_dependency.
        """
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

        try:
            self.cache = Cache(self.config)
        except Exception as e:
            console.print(f"ERROR: build cache - {e}")
            logger.error(f"Cache() raised: {e}")
            return

        if not self.cache.is_valid:
            console.print(f"ERROR: build cache - {self.cache.error_str}")
            logger.error(f"Cache invalid: {self.cache.error_str}")
            return

        self.flags.cache_ready = True


    # --------------------------------------Command: cache purge-------------------------------------

    def cmd_cache_purge(self):
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
        """
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

    def cmd_parse_dependency(self):
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

        installer.list is mixed-universe — each entry is dispatched per its
        membership in the deb / udeb hashtables (deb match → Pass V; udeb
        match → Pass VI; both → both).

        After resolution, validates the selection for Breaks/Conflicts, then
        maps every selected binary package back to its source package so that
        source download and source build know what to fetch and build.  Both
        trees' selected_pkgs are mapped to sources via parse_sources;
        downstream consumers iterate over the union (Phase 4 work).

        Patch files are discovered at this stage so that buildcontainer.build()
        can mount them at container start time without a second disk scan.
        """
        if not self.flags.cache_ready:
            console.print("Cache not ready, Run 'cache build' first")
            return

        _spiner = Spinner("Parsing Dependencies")
        self.flags.dep_check_ready = False  # reset before the long parse

        console.print("Preparing Parsing Tree...", tui.COLOR_INFO)
        self.dep_tree = dependencytree.DependencyTree(self.cache, select_recommended=False,
                    arch=self.config.arch, build_profiles=self.config.build_profiles)

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

        # --- Pass III: manual list ----------------------------------------------
        console.print("Pass III: Checking dependency for manually selected packages", tui.COLOR_INFO)
        selected_packages = list(self.dep_tree.selected_pkgs.keys())
        manual_list = []

        console.print(f"Parsing {self.config.pkglist_path}...")
        try:
            manual_packages_list = utils.readfile(self.config.pkglist_path).split('\n')
        except OSError as e:
            console.print(f"ERROR: cannot read package list {self.config.pkglist_path}")
            logger.error(f"readfile({self.config.pkglist_path}): {e}")
            manual_packages_list = []

        # Strip comments and blank lines; only add packages not already selected.
        for pkg in manual_packages_list:
            if pkg and not pkg.startswith('#') and not pkg.isspace():
                pkg = pkg.strip()
                if pkg not in selected_packages:
                    manual_list.append(pkg)

        console.print(f"Added {len(manual_list)} unique manually selected packages")
        self.dep_tree.resolve_packages(manual_list)

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
        # Phase 3 (COMP-01b): installer.list contains BOTH udeb names (for
        # the installer ramdisk) AND deb names like efibootmgr/grub-pc-bin
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
            # COMP-01b phase 3: udeb world commonly has multi-Package-name
            # "providers" that are really just kernel-ABI variants of the
            # same module (ext4-modules-6.1.0-{NN}-amd64-di etc.).  Auto-
            # pick the highest version across names instead of prompting.
            auto_pick_highest_when_ambiguous=True,
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

        # When [Build] IncludeRecommendsInRepo is on (default)
        if self.config.include_recommends_in_repo:
            _added = self.dep_tree.pull_recommends_extras()
            if _added:
                console.print(
                    f"EXTRAS: pulled {_added} recommended package(s) into the repo ", tui.COLOR_INFO)
            else:
                console.print("EXTRAS: 0 recommends added — if unexpected check logs ", tui.COLOR_INFO)
        else:
            console.print("EXTRAS: disabled — check IncludeRecommendsInRepo", tui.COLOR_INFO)

        # --- Validation ---------------------------------------------------------
        console.print("Checking Breaks and Conflicts...")
        if not self.dep_tree.validate_selection():
            _resp = Prompt(PROMPT_YESNO, "There are one or more dependency validation failures, Proceed?").get_response()
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
            _resp = Prompt(PROMPT_YESNO, "There are one or more source parse failures, Proceed?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        # COMP-01b phase 3: parse_sources for the udeb tree too.  Sources
        # are universal (same source produces both .deb and .udeb), so the
        # shared source_hashtable already has the records we need.
        # udeb_dep_tree.selected_srcs is populated independently; downstream
        # consumers (source download / source build) will iterate over the
        # UNION of both trees' selected_srcs in Phase 4.
        if self.udeb_dep_tree is not None:
            console.print("Parsing Udeb Source Packages...", tui.COLOR_INFO)
            if not self.udeb_dep_tree.parse_sources():
                _resp = Prompt(PROMPT_YESNO,
                               "There are one or more udeb source parse failures, Proceed?"
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

        # COMP-01c phase 1: derive live/installer-exclusive *source* names
        # so future source-build / chroot-build subset filters can route
        # them.  Phase 1 just records the sets; behaviour change lands later.
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
        self.flags.dep_check_ready = True


    # --------------------------------------Command: patch_refresh-------------------------------------

    def _refresh_patches(self) -> int:
        # Scan the patch tree for files matching <package>/<version>/*.patch and
        # populate each Source's patch_list.  Sorting by the first five characters
        # preserves the numeric prefix ordering (e.g. 9001-, 9002-) used to control
        # application order.  Resets patch_list per source so removed patch files
        # are reflected on re-runs (operator-driven `patch_refresh` after
        # out-of-band changes to the patch tree).
        #
        # Phase 4 (COMP-01b): walks BOTH the deb tree AND the udeb tree.
        # Without the udeb pass, sources that live only in the udeb closure
        # (e.g. fuse3 → libfuse3-3-udeb pulled by a d-i udeb) never get
        # their patches discovered — `source build fuse3` then fails because
        # patch_list is empty even though patch/source/fuse3/<ver>/*.patch
        # exists on disk.  Caught in production 2026-05-10.  Both trees
        # share Source instances via source_hashtable, so the union dict
        # naturally dedupes and each Source's patch_list is set exactly once.
        _unified_srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _name, _src in self.udeb_dep_tree.selected_srcs.items():
                if _name not in _unified_srcs:
                    _unified_srcs[_name] = _src

        for _pkg in _unified_srcs:
            _src = _unified_srcs[_pkg]
            _ver = str(_src.version)
            _patch_path = os.path.join(self.config.dir_patch_source, _pkg, _ver)
            _src.patch_list = []
            try:
                if os.path.exists(_patch_path):
                    _patch_files = [f for f in os.listdir(_patch_path) if f.endswith('.patch')]
                    _src.patch_list = sorted(_patch_files, key=lambda x: x[:5])
                    logger.info(f"[patch] {_pkg} {_ver}: {_patch_files}")

                    # soft DEP-3 header check on each discovered patch. Missing fields → log-tab warning only;
                    # the patch is still applied at build time.  Keeps the convention enforceable
                    # without blocking ad-hoc one-off operator patches.
                    for _pf in _patch_files:
                        _missing = utils.check_dep3_header(
                            os.path.join(_patch_path, _pf))
                        if _missing:
                            logger.warning(f"DEP-3: {_pkg}/{_ver}/{_pf} missing header(s): {', '.join(_missing)}")

            except OSError as e:
                console.print(f"WARNING: cannot list patches for '{_pkg}'")
                logger.warning(f"patch discovery {_patch_path}: {e}")

        _patched = sum(1 for _s in _unified_srcs.values() if _s.patch_list)
        console.print(f"Found patches for {_patched} source package(s)", tui.COLOR_INFO)
        return _patched

    def cmd_patch_refresh(self):
        """Re-scan the patch tree and refresh each Source's patch_list.

        Use after editing patch/source/<pkg>/<ver>/ out-of-band so the next
        source_build picks up the new patch set without re-running the full
        parse_dependency stage.  Requires parse_dependency to have run at
        least once.
        """
        if not self.flags.dep_check_ready:
            console.print("Dependency tree not ready, run 'dep parse' first")
            return
        self._refresh_patches()


    # --------------------------------------Command: print-------------------------------------

    def cmd_print(self, category: str = '', *extras):
        """Display summary information about the current build state.

        Thin dispatcher into print_commands.dispatch — the per-category
        handlers and the help screen live there.  Empty or unknown
        category prints the help screen.  Parametrized views (`print pkg
        <name>`, `print src <name>`, `print deps <name>`) consume `*extras`.
        See `print help` for the full category list.
        """
        import print_commands
        print_commands.dispatch(self, category, *extras)


    # ----------------------------------Command: generate_signing_key--------------------------

    def cmd_generate_signing_key(self, *args):
        """Generate the project's signing keypair (one-time setup).

        Usage: generate_signing_key [force]

          force — overwrite an existing key for the same UID. use with caution,
          it invalidates previously signed repos. Prompts for confirmation in either case; 

        - UID comes from `[Repo] SigningKeyUid` in build.conf.  
        - Private is under `<dir_gnupg>/signing/`; 
        - Public key is exported to `<dir_gnupg>/signing/athena-archive-keyring.gpg` f
        """
        import signing
        _force = 'force' in (a.strip().lower() for a in args)

        _existing = signing.get_key_info(self.config)
        if _existing and not _force:
            console.print(
                f"Signing key already exists for "
                f"'{self.config.signing_key_uid}':",
                tui.COLOR_WARNING,
            )
            console.print(f"  Fingerprint : {_existing['fingerprint']}")
            console.print(f"  UID         : {_existing['uid']}")
            console.print("Add `force` to overwrite — Use with Caution ")
            return

        # Confirm before any irreversible action — overwrite warning
        if _existing:
            _msg = (f"Overwrite existing signing key for '{self.config.signing_key_uid}'?")
        else:
            _msg = (f"Generate new signing key for '{self.config.signing_key_uid}'?")

        _resp = Prompt(PROMPT_YESNO, _msg).get_response()
        if _resp.lower() not in ('y', 'yes'):
            console.print("Aborted.")
            return

        console.print(f"Generating signing key for '{self.config.signing_key_uid}'...",tui.COLOR_INFO,)
        
        if not signing.generate_key(self.config):
            console.print("ERROR: key generation failed — see log for the gpg stderr", tui.COLOR_ERROR,)
            return

        _info = signing.get_key_info(self.config)
        if _info is None:
            console.print("ERROR: key generation completed but the key is not "
                "queryable — likely a gpg homedir permission issue", tui.COLOR_ERROR,)
            return
        
        console.print("Signing key generated:", tui.COLOR_HIGHLIGHT)
        console.print(f"  Fingerprint : {_info['fingerprint']}")
        console.print(f"  UID         : {_info['uid']}")
        console.print(f"  Public key  : {signing.signing_pubkey_path(self.config)}")
        console.print("Run `verify_signing_key` to confirm a sign+verify roundtrip")


    # ------------------------------------Command: verify_signing_key----------------------


    def cmd_verify_signing_key(self):
        """Sanity-check the signing key by performing a real gpg
        sign+verify roundtrip against a small test payload.

        Reports the key's fingerprint, uid, creation, expiration —
        plus an OK/FAIL line for the roundtrip itself.  
        
        Catches: missing key, expired key, missing pubkey, gpg-agent issues,
        keys that grew a passphrase out-of-band.
        """
        import signing
        _info = signing.get_key_info(self.config)
        if _info is None:
            console.print(
                f"No signing key for '{self.config.signing_key_uid}' — "
                f"run `generate_signing_key` first",
                tui.COLOR_WARNING,
            )
            return
        console.print("Signing key:", tui.COLOR_INFO)
        console.print(f"  Fingerprint : {_info['fingerprint']}")
        console.print(f"  UID         : {_info['uid']}")
        console.print(f"  Created     : {_info['created']}  (gpg epoch seconds)")
        console.print(f"  Expires     : {_info['expires'] or '(never — manual rotation)'}")

        _ok, _msg = signing.verify_key(self.config)
        if _ok:
            console.print(f"  Verification: OK — {_msg}", tui.COLOR_HIGHLIGHT)
        else:
            console.print(f"  Verification: FAIL — {_msg}", tui.COLOR_ERROR)


    # -----------------------------------Command: source_download--------------------

    def cmd_source_download(self):
        """Download upstream source archives for all selected source packages.

        - Fetches .dsc, .orig.tar.*, and .debian.tar.* files from the configured
        base mirror into dir_source.

        - Skips files that are already present and have correct checksums.

        - Does a size verification

        Phase 4 (COMP-01b): downloads from BOTH the deb tree AND the udeb
        tree.  Without the udeb pass, sources that exist only in the udeb
        closure (base-installer, debian-installer-utils, debootstrap,
        depthcharge-tools-installer, …) never land in dir_source, and a
        later `source build installer` fails with "cp: cannot stat
        /source/<pkg>*: No such file or directory" inside the build
        container.  Sources shared between trees (cdebconf, etc.) are
        skipped in the second pass via the existing on-disk sha check;
        size accounting double-counts them slightly — cosmetic only.
        """
        if not self.flags.dep_check_ready:
            console.print("Run 'dep parse' first")
            return

        self.flags.download_ready = False  # reset before starting

        _deb_size  = self.dep_tree.download_size
        _udeb_size = (self.udeb_dep_tree.download_size
                      if self.udeb_dep_tree is not None else 0)
        _src_download_size = _deb_size + _udeb_size
        console.print(
            f"Total download is about {_src_download_size // (2**20)} MB "
            f"(deb: {_deb_size // (2**20)} MB, udeb: {_udeb_size // (2**20)} MB)"
        )

        _total, _used, _free = shutil.disk_usage(self.config.dir_source)
        console.print(f"Disk space — Total: {_total // (2**30)} GiB, "
                      f"Used: {_used // (2**30)} GiB, Free: {_free // (2**30)} GiB")

        console.print("Starting downloads (deb tree)...")
        _downloaded_size = utils.download_source(self.dep_tree, self.config.dir_source)

        if self.udeb_dep_tree is not None and self.udeb_dep_tree.selected_srcs:
            console.print("Starting downloads (udeb tree)...")
            _downloaded_size += utils.download_source(
                self.udeb_dep_tree, self.config.dir_source)

        # A size mismatch usually means a network interruption or a package whose
        # expected size in the index differs from what the mirror actually served.
        if _src_download_size != _downloaded_size:
            _resp = Prompt(PROMPT_YESNO, "Download size mismatch, continue?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        self.flags.download_ready = True


    # -----------------------------Command: self.container------------------------
    def cmd_init_container(self):
        """Initialise the Docker build container image.

        Builds the image from config/Dockerfile if it does not exist or if the
        Dockerfile has changed since the last build (detected via SHA-256 label).
        Optionally connects to an external Docker daemon if DOCKER_SERVER is set
        in build.conf; falls back to the local daemon on connection failure.
        """
        self.flags.build_container_ready = False
        spin = Spinner("Initialising build container")
        try:
            self.container = buildcontainer.BuildContainer(self.config, docker_server=self.config.docker_server or None)
            self.flags.build_container_ready = True
            spin.done()
            console.print("  Build container ready")
        except RuntimeError as e:
            spin.done()
            console.print(f"  ERROR: build container initialisation failed — {e}")
            logger.error(f"BuildContainer() raised: {e}")


    # --------------------------Internal helper: tunnel download------------------

    def _do_tunnel(self, src_pkg) -> bool:
        """Download prebuilt binary .deb files for src_pkg from the base Debian repo.

        Used when building a package from source is being to stubborn

        The result file written at the end uses the 'TUNNELED' tag (rather than
        'PASS') so that check_build() can distinguish tunneled packages from
        locally built ones if needed.

        Args:
            src_pkg: Source package object with .pkgs (list of .deb filenames),
                     .directory (pool path), and .package (source name).

        Returns:
            True if every binary package was downloaded successfully, False otherwise.
        """
        if not src_pkg.pkgs:
            logger.error(f"tunnel {src_pkg.package}: no binary packages known (run parse_dependency first)")
            return False

        # Construct the pool base URL from the source's origin mirror.  This
        # matters for sources in bookworm-security, whose pool lives at a
        # different baseid than main.
        if src_pkg._mirror is None:
            logger.error(f"tunnel {src_pkg.package}: source has no _mirror — cache ingest bug")
            return False
        _base = src_pkg._mirror.url
        _success = True

        for _filename in src_pkg.pkgs:
            _dest = os.path.join(self.container.repo_path, _filename)

            # Skip files already on disk — no integrity check here; the repo
            # directory is trusted to contain only valid packages.
            if os.path.isfile(_dest):
                logger.info(f"tunnel {src_pkg.package}: {_filename} already present, skipping download")
                continue

            _url = f"{_base}/{src_pkg.directory}/{_filename}"
            logger.info(f"tunnel {src_pkg.package}: downloading {_url}")
            _bytes, _detail = utils.download_file(_url, _dest)
            if _bytes < 0:
                logger.error(f"tunnel {src_pkg.package}: failed to download {_filename}: {_detail or 'unknown'}")
                _success = False

        # Write a result file so check_build() can skip re-tunneling on the next run.
        _result_file = os.path.join(self.container.buildlog_path, src_pkg.package + '.result')
        try:
            with open(_result_file, 'w') as fh:
                fh.write('TUNNELED\n' if _success else 'FAIL\n')
        except OSError as e:
            logger.error(f"tunnel {src_pkg.package}: cannot write result file: {e}")

        return _success


    # --------------------------Command: tunnel_package--------------------------

    def cmd_tunnel_package(self, *args):
        """Download prebuilt binary .debs from the base Debian repo for named packages.

        Usage: tunnel_package [pkg ...]

        If no package names are given, uses the 'Tunneled' list from build.conf.
        Packages must already be present in the dependency tree (run parse_dependency
        first).  Skips packages whose result file already says TUNNELED or PASS.
        """
        if not self.flags.dep_check_ready:
            console.print("Run 'dep parse' first")
            return

        # Fall back to the config list if no names were given on the command line.
        _names = list(args) if args else self.config.tunnel_packages
        if not _names:
            console.print("No packages specified and Tunneled list in build.conf is empty")
            return

        # Validate all names up front before starting any downloads.
        packages = []
        for name in _names:
            src = self.dep_tree.selected_srcs.get(name)
            if src is None:
                console.print(f"Unknown package: {name}")
                return
            packages.append(src)

        _success = _failed = 0
        progress_bar = ProgressBar(label='Tunnel', itr_label='pkgs', maxvalue=len(packages))
        
        for _src_pkg in packages:
            _result = self._do_tunnel(_src_pkg)
            if _result:
                logger.warning(f"Tunnel {_src_pkg.package} [TUNNELED]")
                _success += 1
            else:
                logger.error(f"Tunnel {_src_pkg.package} [FAIL]")
                _failed += 1
            progress_bar.step(1)
        progress_bar.close()

        console.print(f"Tunnel complete: {_success} tunneled, {_failed} failed")


    # ------------------------------------Command: build_bootable------------

    def _ensure_signing_key_verified(self) -> bool:
        """CONF-02 phase 3: gate-keep the signing key before chroot work.

        Runs `signing.verify_key` (a real sign+verify roundtrip).  On
        success, sets `flags.signing_key_verified` and prints the key's
        fingerprint + uid + creation/expiration

        On failure (no key, expired, agent issues, …), prompts the
        operator with PROMPT_YESNO to generate a key now.  If they
        accept, runs the same flow as `cmd_generate_signing_key`
        then re-verifies. If they decline, returns False so the caller 
        (cmd_build_chroot_live) bails before any heavy chroot setup happens.

        Returns True iff the key is verified at exit; the flag mirrors
        the return value.
        """
        import signing
        _ok, _msg = signing.verify_key(self.config)
        if _ok:
            _info = signing.get_key_info(self.config)
            console.print("Signing key verified:", tui.COLOR_HIGHLIGHT)
            if _info is not None:
                console.print(f"  Fingerprint : {_info['fingerprint']}")
                console.print(f"  UID         : {_info['uid']}")
                console.print(f"  Created     : {_info['created']}  (gpg epoch seconds)")
                console.print("  Expires     : {_info['expires'] or '(never — manual rotation)'}")
            self.flags.signing_key_verified = True
            return True

        # Verify failed — usually because no key has been generated yet,
        # but could also be expired / agent issues / passphrase added
        # out-of-band.  Surface the actual reason before the prompt so
        # the operator can decide whether `generate` is the right fix.
        console.print(f"Signing key check FAILED — {_msg}", tui.COLOR_WARNING)
        
        console.print( "valid key is required before the chroot setup can proceed.")
        
        _resp = Prompt(PROMPT_YESNO, "Generate a new signing key for "
                       "'{self.config.signing_key_uid}' now?").get_response()
        
        if _resp.lower() not in ('y', 'yes'):
            console.print("Aborted — signing key is mandatory", tui.COLOR_ERROR)
            self.flags.signing_key_verified = False
            return False

        console.print( f"Generating signing key for '{self.config.signing_key_uid}'...", tui.COLOR_INFO)

        if not signing.generate_key(self.config):
            console.print( "ERROR: key generation failed — see log for the gpg stderr", tui.COLOR_ERROR)
            self.flags.signing_key_verified = False
            return False

        # Re-verify the freshly-generated key.  Belt-and-braces — also
        # gives the operator the same "verified, fingerprint=…" output
        # they'd get on a returning run.
        _ok, _msg = signing.verify_key(self.config)
        if not _ok:
            console.print(f"ERROR: newly-generated key failed verify — {_msg}",tui.COLOR_ERROR)
            self.flags.signing_key_verified = False
            return False
        
        _info = signing.get_key_info(self.config)
        console.print("Signing key generated and verified:", tui.COLOR_HIGHLIGHT)
        if _info is not None:
            console.print(f"  Fingerprint : {_info['fingerprint']}")
            console.print(f"  UID         : {_info['uid']}")
            console.print(f"  Public key  : {signing.signing_pubkey_path(self.config)}")
        
        self.flags.signing_key_verified = True
        return True

    def cmd_build_chroot_live(self, *args):
        """Assemble the resolved package set into a bootable live chroot.

        Usage: chroot build live [with_debug]   (or bare `chroot build [with_debug]`)

          with_debug — write /etc/systemd/journald.conf.d/50-console.conf so all
                       journal entries forward to /dev/console (ttyS0 in serial
                       boots).  Off by default — production images should not leak
                       logs onto the console.

        Takes the .deb files produced by source build from dir_repo and installs
        them into a chroot tree at dir_chroot using dpkg.  The resulting chroot
        can be packaged into a live ISO via `iso build live`.

        Prerequisites: source build must have completed (source_build_ready flag)
        AND the signing key must verify (signing_key_verified flag, gated up
        front via _ensure_signing_key_verified — see CONF-02 phase 3 for why).
        The sudo password is collected interactively at the start of this command.
        """
        if not self.flags.source_build_ready:
            console.print("Run 'source build' first")
            return

        # Verify the project signing key before any sudo / mount / dpkg work
        if not self._ensure_signing_key_verified():
            return

        _debug = 'with_debug' in args
        if _debug:
            console.print("Debug mode: journald will forward to ttyS0 in built chroot")

        console.print("Initialising build system...")
        try:
            build_system = buildsystem.BuildSystem(self.dep_tree, self.config)
        except RuntimeError as e:
            console.print(f"ERROR: build system initialisation failed — {e}")
            logger.error(f"BuildSystem() raised: {e}")
            return

        # Bracket the BuildSystem's lifetime so the cached sudo password is
        # scrubbed on every exit path — success, build failure, 
        try:
            console.print("Building chroot environment...")
            _result = build_system.build_chroot(debug=_debug)
            if not _result:
                console.print("ERROR: chroot build failed — check logs for details")
                logger.error("build_chroot() returned False")
                return

            self.flags.chroot_ready = True

            # Run verification immediately — chroot_verified gates build_iso
            _passed, _failed = self._verify_chroot(build_system.password, self.config.dir_chroot)
            self.flags.chroot_verified = (_failed == 0)
            if _failed > 0:
                logger.error(f"chroot verification: {_failed} of {_passed + _failed} checks failed")
        finally:
            build_system.scrub_password()


    def cmd_build_chroot_installer(self, *args):
        """Build the d-i installer chroot from the udeb closure.

        Usage: chroot build installer

        Wipes + (re)creates dir_chroot_installer, then `dpkg --unpack`s
        every udeb in udeb_dep_tree.selected_pkgs into it.  Postinsts
        are NOT run at chroot-build time — they run at first boot under
        rootskel + main-menu (this matches how d-i itself works; see
        project memory project_installer_from_source).

        After unpack, applies the data-layer overlays from installer/
        per the engine mapping in installer_chroot._OVERLAY_MAP.  All
        configuration (preseed, cdebconf overrides, branding) lives in
        installer/ and can be edited without touching this engine code.

        Prerequisites:
          - dep_check_ready (so udeb_dep_tree is populated)
          - source_build_ready (so the .udeb files exist in repo/)
        Collects sudo password — dpkg --root + the wipe/bootstrap need
        root to set file ownerships correctly inside the chroot.

        On success sets self.flags.chroot_installer_ready.
        """
        if not self.flags.dep_check_ready:
            console.print("Run 'dep parse' first")
            return
        if not self.flags.source_build_ready:
            console.print(
                "Run 'source build installer' first (need .udeb files in repo/)"
            )
            return
        if self.udeb_dep_tree is None:
            console.print(
                "Udeb dep tree not built — re-run 'dep parse' (it populates "
                "udeb_dep_tree alongside the deb tree)"
            )
            return
        if not self.udeb_dep_tree.selected_pkgs:
            console.print(
                "Udeb closure is empty — check installer.list contains udeb "
                "names and cache has the d-i Packages index"
            )
            return

        self.flags.chroot_installer_ready = False  # reset before work

        # Sudo password — same pattern as cmd_build_chroot_live's BuildSystem.
        # Collect once + validate via `sudo -v`; scrub on every exit path.
        _password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()
        _r = subprocess.run(
            ['sudo', '-S', '-v'],
            input=_password + '\n',
            capture_output=True, text=True,
        )
        if _r.returncode != 0:
            console.print("ERROR: incorrect sudo password")
            logger.error("chroot build installer: sudo -v failed")
            _password = '*' * len(_password)
            return

        try:
            console.print("Building installer chroot from udeb closure...")
            _codename = self.config.build_codename.strip('"').strip("'")
            _ok = installer_chroot.build_installer_chroot(
                udeb_tree=self.udeb_dep_tree,
                dir_repo=self.config.dir_repo,
                dir_chroot_installer=self.config.dir_chroot_installer,
                installer_dir=os.path.join(self.config.working_dir, 'installer'),
                password=_password,
                codename=_codename,
            )
            if not _ok:
                console.print(
                    "ERROR: installer chroot build failed — check log for details"
                )
                logger.error("build_installer_chroot returned False")
                return

            self.flags.chroot_installer_ready = True
            console.print(
                f"Installer chroot ready at {self.config.dir_chroot_installer}",
                tui.COLOR_HIGHLIGHT,
            )
        finally:
            # Single-use credential; overwrite the in-memory copy.
            _password = '*' * len(_password)  # noqa: F841


    # -------------------------------Command: build_iso---------------------
    
    def cmd_build_iso_live(self, *args):
        """Build a bootable hybrid BIOS/EFI live ISO from the assembled chroot.

        Usage: iso build live [force]

          force — skip the chroot_verified flag check.  After a manual
                  edit of the chroot tree (e.g. dropping in extra config
                  files between `chroot build` and `iso build live`) the
                  in-memory chroot_verified flag is stale even though the
                  on-disk chroot may still be valid.  With force, we
                  re-run verify_chroot against the on-disk chroot using
                  the password just collected for ISO assembly, and
                  proceed only if all 8 checks still pass.

        Packages the chroot produced by chroot build into a squashfs live
        image, writes a GRUB configuration, and runs grub-mkrescue to
        produce a bootable ISO at dir_image/athena-VERSION-amd64.iso.

        Requires on the host: squashfs-tools, grub-pc-bin, grub-efi-amd64-bin,
        xorriso.  These are checked by build-system.sh at startup.

        Prerequisites: chroot must be built AND verified (chroot_verified
        flag), unless `force` is given in which case verify is re-run.
        """
        _force = 'force' in args
        if not _force and not self.flags.chroot_verified:
            if self.flags.chroot_ready:
                console.print("Chroot built but verification failed — re-run 'chroot verify' after fixing")
            else:
                console.print("Run 'chroot build' first")
            return

        self.flags.iso_live_ready = False  # reset before work; set True only on success
        console.print("Initialising build system for ISO...")
        try:
            build_system = buildsystem.BuildSystem.for_iso(self.config)
        except RuntimeError as e:
            console.print(f"ERROR: build system initialisation failed — {e}")
            logger.error(f"BuildSystem.for_iso() raised: {e}")
            return

        # Same try/finally pattern as cmd_build_chroot_live — scrub the cached
        # sudo password on every exit path so it does not outlive the ISO
        # build command.
        try:
            if _force:
                console.print("Force mode: re-verifying chroot before ISO...")
                _passed, _failed = self._verify_chroot(
                    build_system.password, self.config.dir_chroot)
                if _failed > 0:
                    console.print(
                        f"ERROR: chroot verification failed "
                        f"({_failed} of {_passed + _failed} checks) — "
                        f"refusing to build ISO"
                    )
                    logger.error(
                        f"build_iso force: verify failed "
                        f"{_failed}/{_passed + _failed}"
                    )
                    return
                # Refresh the flag so subsequent (non-force) calls work
                # without re-verifying.
                self.flags.chroot_verified = True

            console.print("Building ISO...")
            _result = build_system.build_iso()
            if not _result:
                console.print("ERROR: ISO build failed — check logs for details")
                logger.error("build_iso() returned False")
                return
            self.flags.iso_live_ready = True
        finally:
            build_system.scrub_password()


    def cmd_build_iso_installer(self, *args):
        """Build the installer ISO from buildroot/installer/ + repo/.

        Usage: iso build installer

        Mastering steps (delegated to iso_installer.build_installer_iso):
          1. Wipe + create dir_image/staging-installer/
          2. Find kernel — first try installer chroot's /boot/vmlinuz-*,
             fall back to extracting from repo/linux-image-*-amd64*.deb
          3. Build monolithic cpio.gz initrd from buildroot/installer/
          4. Copy installer/boot/grub.cfg → staging/boot/grub/grub.cfg
          5. Copy repo/ → staging/pool/ (for /cdrom/pool runtime read)
          6. grub-mkrescue produces hybrid BIOS+EFI ISO

        All configurable bits (boot menu, kernel cmdline) live in
        installer/boot/grub.cfg — operator edits there without touching
        engine code.

        Prerequisites:
          - chroot_installer_ready (so buildroot/installer/ exists)

        Collects sudo password — initrd cpio reads root-owned chroot
        content, pool copy preserves ownership.
        """
        if not self.flags.chroot_installer_ready:
            console.print(
                "Run 'chroot build installer' first (need "
                "buildroot/installer/ populated with the udeb closure)"
            )
            return

        self.flags.iso_installer_ready = False  # reset before work; set True only on success

        # Sudo password — same pattern as cmd_build_chroot_installer.
        _password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()
        _r = subprocess.run(
            ['sudo', '-S', '-v'],
            input=_password + '\n',
            capture_output=True, text=True,
        )
        if _r.returncode != 0:
            console.print("ERROR: incorrect sudo password")
            logger.error("iso build installer: sudo -v failed")
            _password = '*' * len(_password)
            return

        try:
            _version  = self.config.build_version.strip('"').strip("'")
            _codename = self.config.build_codename.strip('"').strip("'")
            # Suite == codename for our single-suite distro.  If we ever
            # ship multiple suites (e.g. athena-stable / athena-testing),
            # the suite would come from a separate config field.
            _suite    = _codename
            _iso_basename = f"athena-installer-{_version}-amd64.iso"
            console.print(
                f"Building installer ISO {_iso_basename}..."
            )
            # Canonical names only (virtuals skipped); Recommends-only
            # extras dropped so target install set tracks pkg.list closure,
            # not the full ISO pool.  See _stage_base_include for context.
            _base_include = sorted({
                _name for _name in self.dep_tree.selected_pkgs
                if _name == self.dep_tree.selected_pkgs[_name]['Package']
                and _name not in self.dep_tree.extras_pkg_names
            })
            _ok = iso_installer.build_installer_iso(
                dir_chroot_installer=self.config.dir_chroot_installer,
                dir_repo=self.config.dir_repo,
                dir_image=self.config.dir_image,
                installer_dir=os.path.join(self.config.working_dir, 'installer'),
                password=_password,
                iso_basename=_iso_basename,
                suite=_suite,
                codename=_codename,
                version=_version,
                base_include_pkgs=_base_include,
            )
            if not _ok:
                console.print(
                    "ERROR: installer ISO build failed — check log for details"
                )
                logger.error("build_installer_iso returned False")
                return
            self.flags.iso_installer_ready = True
        finally:
            _password = '*' * len(_password)  # noqa: F841


    # ---------------------------------------------------------------------------
    # Command: source_build
    # ---------------------------------------------------------------------------

    # Subset selectors recognised by `source build` — pkg / live /
    # installer / recommended are mutually exclusive; named pkgs are a
    # fifth (also exclusive) mode.  'pkg' is the default when no subset
    # and no names are given (Phase 4 — used to be 'live' pre-pivot).
    # 'pkg' = pkg.list closure only; 'live' = live extras only; 'installer'
    # = udeb closure + installer.list deb-arm extras; 'recommended' = extras
    # pulled by depth-1 Recommends.
    _SOURCE_SUBSETS = ('pkg', 'live', 'installer', 'recommended')

    @staticmethod
    def _parse_source_build_args(args):
        """Pure-function argument parser for cmd_source_build.

        Recognises:
          - 'force' as a case-insensitive flag-word at any position
          - 'live' / 'installer' / 'recommended' as case-insensitive
            subset selectors at any position; mutually exclusive with
            each other AND with named packages
          - one optional `[profile,...]` bracket-token (override for both
            DEB_BUILD_PROFILES and DEB_BUILD_OPTIONS); multiple bracket
            tokens is a parse error
          - everything else as a package name

        Default: bare `source build` (no subset, no names) resolves to
        subset='live'.

        Returns ``(err, force, subset, names, profile_override)``.
        On success ``err`` is None; on parse error ``err`` is a printable
        string the caller should surface.  ``subset`` is one of
        'live' / 'installer' / 'recommended' when a subset selector was
        given (or no args at all); '' when named packages were given.
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
        _subset_words = sorted(_flags & set(BuildSession._SOURCE_SUBSETS))
        if len(_subset_words) > 1:
            return (
                f"Usage: pick at most one of "
                f"{'/'.join(BuildSession._SOURCE_SUBSETS)} "
                f"(saw {', '.join(_subset_words)})",
                False, '', [], None,
            )
        _subset = _subset_words[0] if _subset_words else ''
        _reserved = {'force'} | set(BuildSession._SOURCE_SUBSETS)
        _names = [a for a in _other_args
                  if a.strip().lower() not in _reserved]
        if _subset and _names:
            return (
                f"Usage: 'source build {_subset}' is mutually exclusive with "
                f"named packages.  Use one or the other.",
                False, '', [], None,
            )
        # Default: bare `source build` resolves to pkg (Phase 4 of COMP-01b
        # — used to be 'live' pre-pivot when bare meant "build everything for
        # the live ISO").  Now bare = pkg-layer only; operator runs explicit
        # 'source build live' for live extras and 'source build installer'
        # for the installer udeb closure.  autorun chains pkg + live for the
        # live ISO workflow.
        if not _subset and not _names:
            _subset = 'pkg'
        _profile_override = None
        if _bracket_token is not None:
            _profile_override = [
                p.strip() for p in _bracket_token.split(',') if p.strip()
            ]
        return (None, _force, _subset, _names, _profile_override)

    def cmd_source_build(self, *args):
        """Build source packages inside the Docker build container.

        Usage: source build [force] [pkg | live | installer | recommended | <pkg> ...] [[profile,...]]

        Phase 4 of COMP-01b rewired the subset selectors to layered
        semantics matching the parallel-universe architecture:

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
          recommended   — build ONLY the EXTRAS-01 sources (depth-1 Recommends
                          pulled into the repo by parse_dependency, but
                          excluded from chroot install).
          <pkg>...      — limit the build to the named source packages
          [profile,...] — bracket-delimited token (e.g. `[nocheck]`) overrides
                          BOTH DEB_BUILD_PROFILES and DEB_BUILD_OPTIONS for
                          this invocation only.  Use `[]` (empty) for the
                          most permissive build (no profiles/options — docs
                          and tests included).  Implies `force` because the
                          .result cache wouldn't reflect the override.
          (no arg)      — equivalent to `source build pkg`.

        pkg / live / installer / recommended are mutually exclusive with each
        other and with named packages.

        For a complete live ISO: source build → source build live.
        For a complete installer ISO: source build → source build installer.
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
        if not self.flags.download_ready:
            console.print("Run 'source download' first")
            return

        if not self.flags.build_container_ready:
            console.print("Run 'container init' first")
            return

        # Parse args via the static helper for testability.
        _err, _force, _subset, _names, _profile_override = \
            self._parse_source_build_args(args)
        if _err:
            console.print(_err)
            return

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
            console.print("Recommended mode: building EXTRAS-01 extras-only sources")
        if _profile_override is not None:
            console.print(
                f"Profile override active: DEB_BUILD_PROFILES + "
                f"DEB_BUILD_OPTIONS = '{' '.join(_profile_override)}' "
                f"(was: profiles='{' '.join(sorted(self.config.build_profiles))}', "
                f"options='{' '.join(sorted(self.config.build_options))}')",
                tui.COLOR_INFO,
            )

        # Pick the package set per the mode resolved above.
        # COMP-01b phase 4: subset semantics rewired to the parallel-universe
        # spec.  Each subset is now a tightly-scoped slice of the unified
        # source corpus; chroot build live needs source build + source build
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
        # autorun summary (UX-03) can report them as distinct categories.
        _built = _tunneled = _failed = _skipped = 0
        _total = len(packages)
        progress_bar = ProgressBar(label='Source Build', itr_label='pkgs', maxvalue=_total)

        for _index, _src_pkg in enumerate(packages, start=1):
            progress_bar.label(f'({_index}/{_total}) {_src_pkg.package[:20]}')

            # Packages on the skip_src list are excluded unconditionally — typically
            # packages that are known to be unbuildable in the current environment.
            if _src_pkg.package in self.cache.skip_src:
                logger.warning(f"Package {_src_pkg.package} in skip_list")
                _skipped = _skipped + 1
                progress_bar.step(1)
                continue

            # Tunneled packages are always downloaded rather than built locally.
            # check_build() accepts 'TUNNELED' as a valid result so we can skip
            # packages that were already tunneled in a previous run.
            if _src_pkg.package in self.config.tunnel_packages:
                if self.container.check_build(_src_pkg):
                    logger.warning(f"Package {_src_pkg.package} already tunneled [SKIPPED]")
                    _skipped += 1
                    progress_bar.step(1)
                    continue
                _build_result = self._do_tunnel(_src_pkg)
                if _build_result:
                    logger.warning(f"Tunnel {_src_pkg.package} [TUNNELED]")
                    _tunneled += 1
                else:
                    logger.error(f"Tunnel {_src_pkg.package} [FAIL]")
                    _failed += 1
                progress_bar.step(1)
                continue

            # Skip packages with a valid existing build result unless force is set.
            if not _force and self.container.check_build(_src_pkg):
                logger.info(f"Package {_src_pkg.package} already built [SKIPPED]")
                _skipped = _skipped + 1
                progress_bar.step(1)
                continue

            _build_result = self.container.build(
                _src_pkg,
                profiles_override=_profile_override,
                options_override=_profile_override,
            )

            if _build_result:
                logger.info(f"Building Package {_src_pkg.package} [PASS]")
                _built = _built + 1
            else:
                logger.error(f"Building Package {_src_pkg.package} [FAIL]")
                _failed = _failed + 1

            progress_bar.step(1)

        progress_bar.close(persist=True)

        # Persist the counts so the autorun summary (UX-03) can read them
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
            _resp = Prompt(PROMPT_YESNO, "There are source build failures, Proceed?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        self.flags.source_build_ready = True


    # ---------------------------------------------------------------------------
    # Command: verify_chroot
    # ---------------------------------------------------------------------------

    def _verify_chroot(self, password: str, chroot: str) -> tuple:
        """Run the 8-check chroot verification suite. Returns (passed, failed).

        Caller is responsible for prerequisite checks, password validation, and
        setting any progress flags. Prints per-check PASS/FAIL lines and a summary.
        """
        # Checks performed:
        #   1. dpkg --audit          — no packages in a broken state
        #   2. dpkg --get-selections — all packages fully installed (none half-configured)
        #   3. Kernel present        — at least one vmlinuz-* in /boot/
        #   4. Initramfs present     — at least one initrd.img-* in /boot/
        #   5. bash --version        — shell is executable inside the chroot
        #   6. systemctl --version   — systemd is present and executable
        #   7. live-boot installed   — required for live ISO boot
        #   8. /etc/os-release       — OS identity file written by generate_system_configs
        console.print(f"Verifying chroot at {chroot}...")

        _passed = 0
        _failed = 0

        def _check(label: str, ok: bool, detail: str = ''):
            nonlocal _passed, _failed
            _status = '[PASS]' if ok else '[FAIL]'
            _color  = tui.COLOR_HIGHLIGHT if ok else tui.COLOR_ERROR
            _suffix = f' — {detail}' if detail else ''
            console.print(f'  {label:<45} {_status}{_suffix}', _color)
            if ok:
                _passed += 1
            else:
                _failed += 1

        def _chroot_run(*cmd):
            return subprocess.run(
                ['sudo', '-S', 'chroot', chroot] + list(cmd),
                input=password + '\n', capture_output=True, text=True
            )

        # ── Check 1: dpkg --audit ────────────────────────────────────────────────
        _r = _chroot_run('dpkg', '--audit')
        _audit_out = _r.stdout.strip()
        _check('dpkg audit — no broken packages',
               _r.returncode == 0 and not _audit_out,
               'clean' if not _audit_out else _audit_out.splitlines()[0][:60])

        # ── Check 2: all packages fully configured ───────────────────────────────
        _r = _chroot_run('dpkg', '--get-selections')
        _lines      = _r.stdout.splitlines()
        _total      = len(_lines)
        _incomplete = [l.split()[0] for l in _lines if l and not l.endswith('\tinstall')]
        _check('All packages fully installed',
               not _incomplete,
               f'{_total} packages installed' if not _incomplete
               else f'{len(_incomplete)} incomplete: {", ".join(_incomplete[:4])}')

        # ── Check 3: kernel ──────────────────────────────────────────────────────
        _kernels = sorted(glob.glob(os.path.join(chroot, 'boot', 'vmlinuz-*')))
        _check('Kernel present in /boot/',
               bool(_kernels),
               os.path.basename(_kernels[-1]) if _kernels else 'no vmlinuz-* found')

        # ── Check 4: initramfs ───────────────────────────────────────────────────
        _initrds = sorted(glob.glob(os.path.join(chroot, 'boot', 'initrd.img-*')))
        _check('Initramfs present in /boot/',
               bool(_initrds),
               os.path.basename(_initrds[-1]) if _initrds else 'no initrd.img-* found')

        # ── Check 5: bash ────────────────────────────────────────────────────────
        _r = _chroot_run('bash', '--version')
        _ver = _r.stdout.splitlines()[0] if _r.stdout else ''
        _check('Bash executable inside chroot',
               _r.returncode == 0,
               _ver[:60] if _ver else _r.stderr.strip()[:60])

        # ── Check 6: systemd ─────────────────────────────────────────────────────
        _r = _chroot_run('systemctl', '--version')
        _ver = _r.stdout.splitlines()[0] if _r.stdout else ''
        _check('systemd present and executable',
               _r.returncode == 0,
               _ver[:60] if _ver else _r.stderr.strip()[:60])

        # ── Check 7: live-boot ───────────────────────────────────────────────────
        _r = _chroot_run('dpkg', '-l', 'live-boot')
        _live_ok = _r.returncode == 0 and any(l.startswith('ii') for l in _r.stdout.splitlines())
        _check('live-boot installed',
               _live_ok,
               'installed' if _live_ok else 'not installed or unconfigured')

        # ── Check 8: /etc/os-release ─────────────────────────────────────────────
        _os_release = os.path.join(chroot, 'etc', 'os-release')
        _os_ok = os.path.exists(_os_release)
        _os_detail = ''
        if _os_ok:
            try:
                with open(_os_release) as _osf:
                    _os_lines = _osf.read().splitlines()
                _os_detail = next(
                    (l.split('=', 1)[1].strip('"') for l in _os_lines
                     if l.startswith('PRETTY_NAME=')), 'present')
            except OSError:
                _os_detail = 'present'
        _check('/etc/os-release written',
               _os_ok,
               _os_detail if _os_ok else 'missing — run build_bootable again')

        # ── CONF-02 phase 3: signing keyring present? (informational) ──────────
        # Not a check — non-gating because the chroot is still a valid live
        # ISO without our keyring; the keyring matters for trusting future
        # apt sources pointing at the Athena repo.  Surfaced here so the
        # operator sees presence/absence at verify time without having to
        # poke at the chroot tree manually.
        _keyring = os.path.join(
            chroot, 'usr/share/keyrings/athena-archive-keyring.gpg')
        if os.path.exists(_keyring):
            console.print(
                '  Athena signing keyring                        present',
                tui.COLOR_INFO,
            )
        else:
            console.print(
                '  Athena signing keyring                        absent  '
                '(run `generate_signing_key` then re-run `build_chroot`)'
            )

        # ── Summary ──────────────────────────────────────────────────────────────
        _total_checks = _passed + _failed
        console.print('')
        if _failed == 0:
            console.print(
                f'Verification complete: {_passed}/{_total_checks} passed'
                f' — chroot is ready for ISO build',
                tui.COLOR_HIGHLIGHT
            )
        else:
            console.print(
                f'Verification complete: {_passed}/{_total_checks} passed,'
                f' {_failed} failed — build_iso blocked until verify passes',
                tui.COLOR_ERROR
            )

        return _passed, _failed

    def cmd_verify_chroot(self):
        """Re-run the chroot verification suite against an existing chroot.

        Useful after a manual edit of the chroot to re-establish the
        chroot_verified flag without rebuilding from scratch.

        Prerequisites: chroot must already be built (chroot_ready flag).
        """
        if not self.flags.chroot_ready:
            console.print("Run 'chroot build' first")
            return

        _password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()
        try:
            _proc = subprocess.run(['sudo', '-S', '-v'],
                                   input=_password + '\n', capture_output=True, text=True)
            if _proc.returncode != 0:
                console.print("ERROR: incorrect sudo password")
                logger.error("verify_chroot: sudo -v failed")
                return

            _passed, _failed = self._verify_chroot(_password, self.config.dir_chroot)
            self.flags.chroot_verified = (_failed == 0)
        finally:
            # Drop the local reference as soon as we are done — same caveat
            # as BuildSystem.scrub_password (Python strings are immutable).
            _password = ''

    # --------------------------------------Group dispatchers-------------------------------------
    # Each top-level command is a noun (cache, dep, patch, source, package,
    # container, chroot, iso, key); the second token is the verb.  Mirrors
    # the existing `print <category>` pattern.  The actual implementations
    # remain on cmd_<old_name> methods unchanged — these are thin forwarders.

    def _group_help(self, group: str, table: dict, unknown: str = '') -> None:
        if unknown:
            console.print(f"Unknown {group} action: '{unknown}'")
        console.print(f"{group}: {' | '.join(table.keys())}")
        for _action, _desc in table.items():
            console.print(f"  {group} {_action}\t{_desc}")

    def cmd_cache(self, action: str = '', *args):
        _table = {
            'build': 'build apt cache from configured mirrors',
            'purge': 'delete every file in the cache directory (re-fetched on next build)',
        }
        if action == 'build':
            return self.cmd_build_cache(*args)
        if action == 'purge':
            return self.cmd_cache_purge(*args)
        return self._group_help('cache', _table, action)

    def cmd_dep(self, action: str = '', *args):
        _table = {'parse': 'resolve full dep closure for selected packages'}
        if action == 'parse':
            return self.cmd_parse_dependency(*args)
        return self._group_help('dep', _table, action)

    def cmd_patch(self, action: str = '', *args):
        _table = {'refresh': 're-scan patch/source/ tree (after out-of-band edits)'}
        if action == 'refresh':
            return self.cmd_patch_refresh(*args)
        return self._group_help('patch', _table, action)

    def cmd_source(self, action: str = '', *args):
        _table = {
            'download': 'fetch source tarballs for selected sources',
            'build':    'build sources: source build [force] [live | installer | recommended | <pkg>…] [[profile,…]]',
        }
        if action == 'download':
            return self.cmd_source_download(*args)
        if action == 'build':
            return self.cmd_source_build(*args)
        return self._group_help('source', _table, action)

    def cmd_package(self, action: str = '', *args):
        _table = {'tunnel': 'pull prebuilt .debs from Debian repo (package tunnel [pkg…])'}
        if action == 'tunnel':
            return self.cmd_tunnel_package(*args)
        return self._group_help('package', _table, action)

    def cmd_container(self, action: str = '', *args):
        _table = {'init': 'build the Docker build sandbox image'}
        if action == 'init':
            return self.cmd_init_container(*args)
        return self._group_help('container', _table, action)

    def cmd_chroot(self, action: str = '', *args):
        _table = {
            'build [live]':    'install built .debs into buildroot/live (default sub-action)',
            'build installer': 'unpack udeb closure into buildroot-installer/ (no postinst configure)',
            'verify':          '8-check chroot health verifier',
        }
        if action == 'build':
            # Default to live; explicit `live`/`installer` consumes the
            # next token as the sub-action.  Anything else is treated as
            # args to the live build (preserves `chroot build with_debug`).
            if args and args[0] in ('live', 'installer'):
                _sub = args[0]
                _rest = args[1:]
                if _sub == 'installer':
                    return self.cmd_build_chroot_installer(*_rest)
                return self.cmd_build_chroot_live(*_rest)
            return self.cmd_build_chroot_live(*args)
        if action == 'verify':
            return self.cmd_verify_chroot(*args)
        return self._group_help('chroot', _table, action)

    def cmd_iso(self, action: str = '', *args):
        _table = {
            'build live':      'wrap live chroot into bootable hybrid BIOS/EFI ISO',
            'build installer': 'wrap installer chroot + kernel + pool into hybrid BIOS+EFI ISO',
        }
        if action == 'build':
            if not args:
                console.print("Usage: iso build <live | installer>")
                return self._group_help('iso', _table)
            _sub = args[0]
            _rest = args[1:]
            if _sub == 'live':
                return self.cmd_build_iso_live(*_rest)
            if _sub == 'installer':
                return self.cmd_build_iso_installer(*_rest)
            return self._group_help('iso', _table, f'build {_sub}')
        return self._group_help('iso', _table, action)

    def cmd_key(self, action: str = '', *args):
        _table = {
            'generate': 'generate the project signing keypair (`force` to overwrite)',
            'verify':   'sign+verify roundtrip against the current signing key',
        }
        if action == 'generate':
            return self.cmd_generate_signing_key(*args)
        if action == 'verify':
            return self.cmd_verify_signing_key(*args)
        return self._group_help('key', _table, action)

    def cmd_auto_run(self, action: str = '', *args):
        """Group dispatcher: bare `autorun` → autorun live (preserves
        existing UX); explicit `autorun live` or `autorun installer`
        run their respective pipelines.

        Both pipelines share the early stages (cache → dep parse →
        source download → container init → source build pkg) and diverge
        at the subset-specific source build + chroot build, then converge
        on `iso build *` to produce the bootable image.
        """
        _table = {
            'live':      'cache→parse→download→container→source build (+live)→chroot build live→iso build live',
            'installer': 'cache→parse→download→container→source build (+installer)→chroot build installer→iso build installer',
        }
        if action in ('', 'live'):
            return self.cmd_auto_run_live(*args)
        if action == 'installer':
            return self.cmd_auto_run_installer(*args)
        return self._group_help('autorun', _table, action)

    def cmd_auto_run_live(self):
        """Run the full pipeline through to a bootable live ISO.

        bare `source build` now builds pkg.list closure only (Phase 4 of
        COMP-01b).  For a complete live ISO, we need pkg + live extras;
        chain both before chroot build.  Each step uses the
        source_build_ready flag, which cmd_source_build resets at entry —
        so bailing on either subset's failure works the same way.
        """
        _steps = [
            (self.cmd_build_cache,       'cache_ready',           'cache build'),
            (self.cmd_parse_dependency,  'dep_check_ready',       'dep parse'),
            (self.cmd_source_download,   'download_ready',        'source download'),
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
            (self.cmd_parse_dependency,  'dep_check_ready',            'dep parse'),
            (self.cmd_source_download,   'download_ready',             'source download'),
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

    def _run_autorun_steps(self, label: str, _steps: list) -> None:
        """Common driver shared by cmd_auto_run_{live,installer}.

        Walks _steps sequentially, calls each function, gates on its
        success flag.  On the first failure logs + breaks.  Emits the
        UX-03 summary (via print_commands.summary) on every exit path,
        carrying the stage label that aborted (if any) + total wall time.
        """
        import print_commands
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
    # ---------------------------------------------------------------------------
    # Entry point
    # ---------------------------------------------------------------------------


def main(banner: str) -> None:
    """Initialise apt_pkg, BuildConfig, the rendering backend (TUI or CLI),
    and a BuildSession; register every cmd_X handler; block until the user
    exits.

    `--headless` flag (anywhere in argv) selects the CLI backend instead of
    the curses TUI.  Both backends register themselves as `tui.tui_instance`
    so every existing facade (Console, Spinner, ProgressBar, Prompt) works
    unchanged.  See scripts/cli.py for the CLI backend's contract.
    """
    # UX-05 Path B: detect --headless before BuildConfig sees argv.  Strip
    # it after detection — BuildConfig uses argparse and would error on
    # unknown flags.
    _headless = '--headless' in sys.argv
    if _headless:
        sys.argv.remove('--headless')

    try:
        print("Initialising apt_pkg...")
        apt_pkg.init_system()
    except Exception as e:
        print(f"ERROR: Failed to initialise apt_pkg - {e}, Exiting...")
        sys.exit(1)

    try:
        print("Parsing config...")
        config = BuildConfig()
    except Exception as e:
        print(f"ERROR: load configuration - {e}, Exiting...")
        sys.exit(1)

    if not config.is_valid:
        print(f"ERROR: load configuration - {config.error_str}, Exiting...")
        sys.exit(1)

    if _headless:
        print("Initialising headless CLI backend...")
        try:
            from cli import Cli
            tui_inst = Cli()
            # Cli.__init__ registers itself as tui.tui_instance and binds
            # logging.  No event-loop thread to spin up — wait() runs the
            # REPL on the main thread.
            signal.signal(signal.SIGINT, tui_inst.sig_shutdown)
        except Exception as e:
            print(f"FATAL: CLI initialisation failed: {e}")
            sys.exit(1)
    else:
        print("Initialising TUI...")
        try:
            tui_inst = Tui(banner)
            tui_inst.run()
            # Tui.__init__ already registers itself as the module singleton.
            signal.signal(signal.SIGINT, tui_inst.sig_shutdown)
        except Exception as e:
            print(f"FATAL: TUI initialisation failed: {e}")
            Exit(1)

    # Attach a timestamped FileHandler to the 'athena' logger after the
    # Tui is constructed.  Tui.__init__ calls setup_logging() to (re)bind
    # the tab handlers; setup_logging is careful to leave non-tab
    # handlers alone, but attaching the file handler *after* the Tui is
    # the safer ordering — and it lets the operator see the path on the
    # console tab via tui.console.print rather than only host stdout.
    try:
        _log_path = setup_file_logging(config.dir_log)
        console.print(f"Logging to {_log_path}", tui.COLOR_INFO)
    except OSError as e:
        console.print(f"WARN: could not open run log ({e}); "
                      f"continuing without file logging", tui.COLOR_WARNING)

    session = BuildSession(config, tui_inst)

    tui.register_command('cache',     session.cmd_cache,     '\tCache:      cache build')
    tui.register_command('dep',       session.cmd_dep,       '\tDeps:       dep parse')
    tui.register_command('patch',     session.cmd_patch,     '\tPatches:    patch refresh')
    tui.register_command('source',    session.cmd_source,    '\tSources:    source download | source build [live|installer|recommended]')
    tui.register_command('package',   session.cmd_package,   '\tPackages:   package tunnel')
    tui.register_command('container', session.cmd_container, '\tContainer:  container init')
    tui.register_command('chroot',    session.cmd_chroot,    '\tChroot:     chroot build [live|installer] | chroot verify')
    tui.register_command('iso',       session.cmd_iso,       '\tISO:        iso build live | iso build installer')
    tui.register_command('key',       session.cmd_key,       '\tSigning:    key generate | key verify')
    tui.register_command('autorun',   session.cmd_auto_run,  '\tAutorun:    autorun [live] | autorun installer')
    tui.register_command('print',     session.cmd_print,     '\tPrint build state — try: print help')

    console.print(asciiart_logo, tui.COLOR_ERROR)
    console.print("Starting Source Build System for Athena Linux...", tui.COLOR_HIGHLIGHT)
    console.print(f"\tArch\t\t\t{config.arch}")
    console.print(f"\tParent Distribution\t{config.release} {config.baseversion}")
    console.print(f"\tBuild Distribution\t{config.build_codename} {config.build_version}")

    tui_inst.wait()
    Exit(0)


if __name__ == '__main__':
    build_banner = "Athena Build Environment v0.1"
    print(asciiart_logo)
    main(build_banner)
