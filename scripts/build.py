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
    build_cache → parse_dependency → source_download → build_container → source_build

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
import cache
import sys
import signing
from typing import Optional

import apt_pkg

# Local imports
import utils
from utils import BuildConfig
from cache import Cache

import buildcontainer
import dependencytree
import buildsystem
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

    UX-04: optionally autosaves to a JSON file on every flag change.  Pass
    ``save_path=...`` to enable.  ``load(path)`` is the inverse — reads back
    a persisted instance and returns one with autosave wired.  On load,
    flags whose underlying state is in-memory-only (cache, dep tree,
    container, signing-key verification) are RESET to False — they can't be
    honestly restored across a process restart, and a misleading True would
    crash downstream stages that dereference ``self.cache=None``.  Flags
    backed by on-disk state (downloads, .debs, chroot tree) survive intact.
    """

    _FIELDS = [
        'cache_ready', 'dep_check_ready', 'download_ready',
        'build_container_ready', 'source_build_ready',
        'signing_key_verified',
        'chroot_ready', 'chroot_verified',
    ]
    # Flags whose underlying state lives only in this Python process
    # (BuildSession.cache / .dep_tree / .container, or the in-session
    # signing-verify result).  Reset on load — restoring True without
    # the in-memory object means the next stage to read self.<X> trips
    # on None.  Once UX-04 commit B (persistence of Cache + DependencyTree
    # via pickle) lands, cache_ready / dep_check_ready will be backable
    # by on-disk state and can move out of this set.
    _IN_MEMORY_ONLY = {
        'cache_ready', 'dep_check_ready', 'build_container_ready',
        'signing_key_verified',
    }
    _FORMAT_VERSION = 1
    _SAVE_FILENAME = 'buildflags.json'

    def __init__(self, save_path: 'Optional[str]' = None):
        # Order matters: _save_path / _autosave must exist BEFORE any
        # field set, because __setattr__ consults them.  Bypass our own
        # setattr for these privates via object.__setattr__.
        object.__setattr__(self, '_save_path', save_path)
        object.__setattr__(self, '_autosave', False)
        for f in self._FIELDS:
            object.__setattr__(self, f, False)
        # All fields exist now — autosave is allowed.  Subsequent
        # `self.cache_ready = True` calls in cmd_* trip __setattr__ →
        # _save().  No-op when save_path is None (tests, stubs).
        object.__setattr__(self, '_autosave', save_path is not None)

    def __setattr__(self, name: str, value) -> None:
        object.__setattr__(self, name, value)
        if (self._autosave
                and not name.startswith('_')
                and name in self._FIELDS):
            self._save()

    def _save(self) -> None:
        """Write current flag state to ``self._save_path`` as JSON.
        Failures log at WARNING — they don't raise; a write failure
        during a long build shouldn't abort the build."""
        if not self._save_path:
            return
        try:
            os.makedirs(os.path.dirname(self._save_path), exist_ok=True)
            payload = {
                '_format_version': self._FORMAT_VERSION,
                '_last_save': datetime.datetime.now().isoformat(timespec='seconds'),
                'flags': {f: getattr(self, f) for f in self._FIELDS},
            }
            import json as _json
            with open(self._save_path, 'w') as fh:
                _json.dump(payload, fh, indent=2)
        except OSError as e:
            logger.warning(f"BuildFlags save failed ({self._save_path}): {e}")

    @classmethod
    def load(cls, save_path: str) -> 'BuildFlags':
        """Load a persisted BuildFlags from disk; returns a fresh
        all-False instance (with autosave wired) if the file is absent
        or unparseable.

        Resets ``_IN_MEMORY_ONLY`` flags to False on load — those reflect
        Python-process state that doesn't survive a restart.
        """
        flags = cls(save_path=save_path)
        if not os.path.exists(save_path):
            return flags
        try:
            import json as _json
            with open(save_path) as fh:
                data = _json.load(fh)
            if data.get('_format_version') != cls._FORMAT_VERSION:
                logger.warning(
                    "BuildFlags: unknown format_version "
                    f"{data.get('_format_version')!r} in {save_path} — "
                    "using defaults"
                )
                return flags
            persisted = data.get('flags', {})
            # Suspend autosave during load — no need to rewrite what we
            # just read.
            object.__setattr__(flags, '_autosave', False)
            for f in cls._FIELDS:
                if f in persisted and isinstance(persisted[f], bool):
                    val = persisted[f]
                    if f in cls._IN_MEMORY_ONLY and val:
                        # Honest reset — see class docstring.
                        val = False
                    object.__setattr__(flags, f, val)
            object.__setattr__(flags, '_autosave', True)
            return flags
        except (OSError, ValueError, KeyError) as e:
            logger.warning(
                f"BuildFlags: could not load {save_path}: "
                f"{type(e).__name__}: {e} — using defaults"
            )
            return flags

    @classmethod
    def default_path(cls, config) -> str:
        """Conventional location for the persisted flags file —
        siblings the existing snapshot.timestamp under dir_cache."""
        return os.path.join(config.dir_cache, cls._SAVE_FILENAME)

    def restored_summary(self) -> str:
        """One-line summary of which flags came back True from disk.
        Empty string when none — caller should not print anything."""
        _set = [f for f in self._FIELDS if getattr(self, f)]
        return ', '.join(_set) if _set else ''

    def __str__(self) -> str:
        """Return a compact one-line status string for display in the TUI."""
        return '  '.join(
            f"[{'✓' if getattr(self, f) else '·'}] {f.replace('_ready', '')}"
            for f in self._FIELDS
        )

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
        self.container: 'Optional[buildcontainer.BuildContainer]' = None
        # UX-04: load persisted flags if a JSON file exists at the
        # default path (dir_cache/buildflags.json).  Autosave is wired —
        # subsequent flag flips in cmd_* will write back to the same
        # path.  In-memory-only flags (cache_ready, dep_check_ready,
        # build_container_ready, signing_key_verified) get reset on
        # load — see BuildFlags class docstring.
        self.flags: BuildFlags = BuildFlags.load(BuildFlags.default_path(config))

        # Counters from the most recent source_build run — populated by
        self.last_source_build_counts: 'Optional[dict]' = None

    def cmd_build_cache(self):
        """Fetch and parse the upstream APT package indices into an in-memory cache.

        Downloads the binary and source Packages files for the configured base
        distribution and architecture, then indexes them for fast lookup during
        dependency resolution.  Must be run before parse_dependency.
        """
        console.print("Building Cache...", tui.COLOR_INFO)
        self.flags.cache_ready = False  # reset in case we're re-running

        # preint current snapshot in readable format
        if self.config.snapshot_enabled:
            try:
                _ts = utils.resolve_snapshot_timestamp(self.config)
            except (RuntimeError, ValueError) as e:
                console.print(f"ERROR: snapshot timestamp resolution failed — {e}",
                              tui.COLOR_ERROR)
                logger.error(f"resolve_snapshot_timestamp: {e}")
                return
            _date_time = utils.format_snapshot_timestamp(_ts) if _ts else '<unresolved>'
            _suffix = ' (latest)' if self.config.snapshot_timestamp_config == 'latest' else ''
            console.print(f"Snapshot timestamp: {_date_time}{_suffix}", tui.COLOR_HIGHLIGHT)
        else:
            console.print("Snapshot pinning: disabled (live mirrors)", tui.COLOR_INFO)

        # build the cache
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

        # UX-04 commit B: persist the populated cache for `resume`.
        # Failure logs WARNING (doesn't abort the build) — we'd just lose
        # the resume shortcut, not any actual data.
        import persistence
        persistence.save_cache(self.cache, self.config)


    # ---------------------------------------------------------------------------
    # Command: parse_dependency
    # ---------------------------------------------------------------------------

    def cmd_parse_dependency(self):
        """Resolve the full closure of packages needed to build the target system.

        Runs three dependency-resolution passes in priority order: This is technically 
        unnecessary to have so many passes, initially created to help with debugging
        but then just remained for sake of clarity.

          Pass I   — 'required' packages (essential base; every package they pull
                      in is also marked required so it survives any later pruning)
          Pass II  — 'important' packages (strongly recommended by Debian policy;
                      avoids excessive manual intervention on a bare system)
          Pass III — manually listed packages from the configured pkglist file
                      (distro-specific selections on top of the Debian base)

        After resolution, 
            - validates the selection for Breaks/Conflicts, 
            - maps every selected binary package back to its source package
            - find patches
            - source_download and source_build to fetch and build.

        """
        if not self.flags.cache_ready:
            console.print("Cache not ready, Run 'build_cache' first")
            return
        
        self.flags.dep_check_ready = False

        _spiner = Spinner("Parsing Dependencies")
        
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
        console.print(f"Total Selected Packages : {__num_total}", tui.COLOR_HIGHLIGHT)
        _spiner.done()

        # --- EXTRAS-01: pull depth-1 Recommends as available-not-installed ----
        # When [Build] IncludeRecommendsInRepo is on (default), recommends of
        # the closure above land in selected_pkgs (so source_download fetches
        # them) but are tracked separately so build_chroot skips them and
        # source_build routes them to `source_build recommended`. 

        if self.config.include_recommends_in_repo:
            _added = self.dep_tree.pull_recommends_extras()
            if _added:
                console.print(f"Pulled {_added} recommended package(s) into the repo ", tui.COLOR_INFO)
            else:
                console.print("Seems no recommends added — check logs if unexpected", tui.COLOR_INFO)
        else:
            console.print("IncludeRecommendsInRepo: disabled", tui.COLOR_INFO)

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

        # --------------------- Source mapping -----------------------------------
        console.print("Parsing Source Packages...", tui.COLOR_INFO)

        # Map each selected binary package to its upstream source. 
        if not self.dep_tree.parse_sources():
            _resp = Prompt(PROMPT_YESNO, "There are one or more source parse failures, Proceed?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        # identify which sources are extras-only so source_build default skips them
        _extras_only = self.dep_tree.derive_extras_src_names()
        if self.dep_tree.extras_pkg_names:
            console.print(f"{_extras_only} source(s) are extras-only ", tui.COLOR_INFO)

        # Apply per-package skip_test flag from config (suppresses 'nocheck' build opt).
        for _pkg in self.config.skip_build_test:
            if _pkg in self.dep_tree.selected_srcs:
                self.dep_tree.selected_srcs[_pkg].skip_test = True

        # -------------------- Patch discovery --------------------------------------
        # Scan the patch tree for files matching <package>/<version>/*.patch.
        # Sorting by the first five characters preserves the numeric prefix ordering
        # (e.g. 9001-, 9002-) used to control application order.
        for _pkg in self.dep_tree.selected_srcs:
            _ver = str(self.dep_tree.selected_srcs[_pkg].version)
            _patch_path = os.path.join(self.config.dir_patch_source, _pkg, _ver)
            try:
                if os.path.exists(_patch_path):
                    _patch_files = [f for f in os.listdir(_patch_path) if f.endswith('.patch')]
                    self.dep_tree.selected_srcs[_pkg].patch_list = sorted(_patch_files, key=lambda x: x[:5])
                    logger.info(f"[patch] {_pkg} {_ver}: {_patch_files}")
                    # soft DEP-3 header check on each discovered patch. 
                   
                    for _pf in _patch_files:
                        _missing = utils.check_dep3_header(os.path.join(_patch_path, _pf))
                        
                        # Missing fields → log-tab warning only; the patch is still applied at build time. 
                        # Keeps the convention enforceable without blocking ad-hoc one-off operator patches.
                        if _missing:
                            logger.warning(f"DEP-3: {_pkg}/{_ver}/{_pf} missing header(s): {', '.join(_missing)}")

            except OSError as e:
                console.print(f"WARNING: cannot list patches for '{_pkg}'")
                logger.warning(f"patch discovery {_patch_path}: {e}")

        _patched = sum(1 for _s in self.dep_tree.selected_srcs.values() if _s.patch_list)
        console.print(f"Found patches for {_patched} source package(s)", tui.COLOR_INFO)

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

        # UX-04 commit B: persist the dep tree for `resume`.  Same pattern
        # as the cache save in cmd_build_cache — failure-log-WARN, don't
        # abort.  cmd_tunnel_package mutates the tree later; if we want
        # those mutations persisted too, repeat this call there.
        import persistence
        persistence.save_dep_tree(self.dep_tree, self.config)


    # ------------------------------ Command: print ---------------------------------

    def cmd_print(self, category: str = '', *extras):
        """Display summary information about the current build state.

        Thin dispatcher into print_commands.dispatch — Parametrized views (`print pkg
        <name>`, `print src <name>`, `print deps <name>`) consume `*extras`.
        See `print help` for the full category list.
        """

        import print_commands
        print_commands.dispatch(self, category, *extras)

    # --------------------------------- Command: generate_signing_key ---------------

    def cmd_generate_signing_key(self, *args):
        
        """Generate the project's signing keypair (one-time setup).

        Usage: generate_signing_key [force]

        force — overwrite an existing key for the same UID. 
        carefull overwriting - would invalidate all previous Release.

        - UID comes from `[Repo] SigningKeyUid` in build.conf.  
        - Private `<dir_gnupg>/signing/`
        - public key is exported to `<dir_gnupg>/signing/athena-archive-keyring.gpg`
        """


        _force = 'force' in (a.strip().lower() for a in args)

        _existing = signing.get_key_info(self.config)
        if _existing and not _force:
            console.print(
                f"Signing key already exists for '{self.config.signing_key_uid}':",
                tui.COLOR_WARNING,
            )
            console.print(f"  Fingerprint : {_existing['fingerprint']}")
            console.print(f"  UID         : {_existing['uid']}")
            console.print("Add `force` to overwrite — Use with CAUTION ")
            return

        # Confirm before any irreversible action — overwrite warning
        # explicitly calls out the cascade.
        if _existing:
            _msg = (f"Overwrite existing signing key for '{self.config.signing_key_uid}'?")
        else:
            _msg = (f"Generate new signing key for '{self.config.signing_key_uid}'?")
        _resp = Prompt(PROMPT_YESNO, _msg).get_response()
        
        if _resp.lower() not in ('y', 'yes'):
            console.print("Aborted.")
            return

        console.print(f"Generating signing key for '{self.config.signing_key_uid}'...", tui.COLOR_INFO)
        if not signing.generate_key(self.config):
            console.print("ERROR: key generation failed — see log for the gpg stderr", tui.COLOR_ERROR)
            return

        _info = signing.get_key_info(self.config)
        if _info is None:
            console.print(
                "ERROR: key generation completed but the key is not "
                "queryable — likely a gpg homedir permission issue",
                tui.COLOR_ERROR,
            )
            return
        console.print("Signing key generated:", tui.COLOR_HIGHLIGHT)
        console.print(f"  Fingerprint : {_info['fingerprint']}")
        console.print(f"  UID         : {_info['uid']}")
        console.print(f"  Public key  : {signing.signing_pubkey_path(self.config)}")
        console.print("Run `verify_signing_key` to confirm")


    # -------------------------------- Command: verify_signing_key --------------
    def cmd_verify_signing_key(self):
        """
        Sanity-check the signing key by performing a gpg sign+verify against a test payload.

        Reports the key's fingerprint, uid, creation, expiration — plus an OK/FAIL .  
        Catches: 
            - missing key, expired key, missing pubkey, gpg-agent issues,
            - keys that grew a passphrase out-of-band.
        """

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


    # ---------------------------- Command: source_download ----------------------
    def cmd_source_download(self):
        """Download upstream source archives for all selected source packages.

        Fetches .dsc, .orig.tar.*, and .debian.tar.* files from the configured
        base mirror into dir_source.  Skips files that are already present and
        have correct checksums. Checks for downloaded size mismatch
        """
        if not self.flags.dep_check_ready:
            console.print("Run 'parse_dependency' first")
            return

        self.flags.download_ready = False  # reset before starting

        _src_download_size = self.dep_tree.download_size
        console.print(f"Total download is about {_src_download_size // (2**20)} MB")

        _total, _used, _free = shutil.disk_usage(self.config.dir_source)
        console.print(f"Disk space — Total: {_total // (2**30)} GiB, "
                      f"Used: {_used // (2**30)} GiB, Free: {_free // (2**30)} GiB")

        console.print("Starting downloads...")
        _downloaded_size = utils.download_source(self.dep_tree, self.config.dir_source)

        # A size mismatch usually means a network interruption or a package whose
        # expected size in the index differs from what the mirror actually served.
        if _src_download_size != _downloaded_size:
            _resp = Prompt(PROMPT_YESNO, "Download size mismatch, continue?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        self.flags.download_ready = True


    # ----------------------------- Command: self.container ----------------------

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


    # ----------------------------- Internal helper: tunnel download -------------------------------

    def _do_tunnel(self, src_pkg) -> bool:
        """Download prebuilt binary .deb files for src_pkg from the base Debian repo.

        Bypass for those stubborn packages that just dont want to build. Files are written directly 
        into the repo directory alongside locally built packages

        The result file written at the end uses the 'TUNNELED' tag 

        Args:
            src_pkg: Source package object with .pkgs (list of .deb filenames),
                     .directory (pool path), and .package (source name).

        Returns:
            True if every binary package was downloaded successfully, False otherwise.
        """

        if not src_pkg.pkgs:
            logger.error(f"tunnel {src_pkg.package}: no binary packages known (run parse_dependency first)")
            return False

        # Construct the pool base URL from the source's origin mirror.  
        # This matters in bookworm-security, whose pool lives at a different baseid than main.
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
                logger.error(
                    f"tunnel {src_pkg.package}: failed to download {_filename}: "
                    f"{_detail or 'unknown'}"
                )
                _success = False

        # Write a result file so check_build() can skip re-tunneling on the next run.
        _result_file = os.path.join(self.container.buildlog_path, src_pkg.package + '.result')
        try:
            with open(_result_file, 'w') as fh:
                fh.write('TUNNELED\n' if _success else 'FAIL\n')
        except OSError as e:
            logger.error(f"tunnel {src_pkg.package}: cannot write result file: {e}")

        return _success


    # ---------------------------------------------------------------------------
    # Command: tunnel_package
    # ---------------------------------------------------------------------------

    def cmd_tunnel_package(self, *args):
        """Download prebuilt binary .debs from the base Debian repo for named packages.

        Usage: tunnel_package [pkg ...]

        If no package names are given, uses the 'Tunneled' list from build.conf.
        Packages must already be present in the dependency tree (run parse_dependency
        first).  Skips packages whose result file already says TUNNELED or PASS.
        """
        if not self.flags.dep_check_ready:
            console.print("Run 'parse_dependency' first")
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


    # ---------------------------------------------------------------------------
    # Command: build_bootable
    # ---------------------------------------------------------------------------

    def _ensure_signing_key_verified(self) -> bool:
        """CONF-02 phase 3: gate-keep the signing key before chroot work.

        Runs `signing.verify_key` (a real sign+verify roundtrip).  On
        success, sets `flags.signing_key_verified` and prints the key's
        fingerprint + uid + creation/expiration so the operator sees
        the identity that's about to be baked into the chroot.

        On failure (no key, expired, agent issues, …), prompts the
        operator with PROMPT_YESNO to generate a key now.  If they
        accept, runs the same flow as `cmd_generate_signing_key`
        (PROMPT_YESNO confirmation, RSA-4096, ~30-60s) then re-verifies.
        If they decline, returns False so the caller (cmd_build_chroot)
        bails before any heavy chroot setup happens.

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
                console.print(
                    "  Expires     : "
                    f"{_info['expires'] or '(never — manual rotation)'}"
                )
            self.flags.signing_key_verified = True
            return True

        # Verify failed — usually because no key has been generated yet,
        # but could also be expired / agent issues / passphrase added
        # out-of-band.  Surface the actual reason before the prompt so
        # the operator can decide whether `generate` is the right fix.
        console.print(
            f"Signing key check FAILED — {_msg}",
            tui.COLOR_WARNING,
        )
        console.print(
            "build_chroot bakes the public keyring into the chroot at "
            "/usr/share/keyrings/athena-archive-keyring.gpg, so a valid "
            "key is required before the chroot setup can proceed."
        )
        _resp = Prompt(
            PROMPT_YESNO,
            "Generate a new signing key for "
            f"'{self.config.signing_key_uid}' now? (~30-60s for RSA-4096)"
        ).get_response()
        if _resp.lower() not in ('y', 'yes'):
            console.print(
                "Aborted — signing_key_verified flag stays unset; "
                "build_chroot will refuse until you run "
                "`generate_signing_key` and re-try.",
                tui.COLOR_ERROR,
            )
            self.flags.signing_key_verified = False
            return False

        console.print(
            f"Generating signing key for '{self.config.signing_key_uid}'...",
            tui.COLOR_INFO,
        )
        if not signing.generate_key(self.config):
            console.print(
                "ERROR: key generation failed — see log for the gpg stderr",
                tui.COLOR_ERROR,
            )
            self.flags.signing_key_verified = False
            return False

        # Re-verify the freshly-generated key.  Belt-and-braces — also
        # gives the operator the same "verified, fingerprint=…" output
        # they'd get on a returning run.
        _ok, _msg = signing.verify_key(self.config)
        if not _ok:
            console.print(
                f"ERROR: newly-generated key failed verify — {_msg}",
                tui.COLOR_ERROR,
            )
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

    def cmd_build_chroot(self, *args):
        """Assemble the resolved package set into a bootable chroot environment.

        Usage: build_chroot [with_debug]

          with_debug — write /etc/systemd/journald.conf.d/50-console.conf so all
                       journal entries forward to /dev/console (ttyS0 in serial
                       boots).  Off by default — production images should not leak
                       logs onto the console.

        Takes the .deb files produced by source_build from dir_repo and installs
        them into a chroot tree at dir_chroot using dpkg.  The resulting chroot
        can be packaged into an ISO or disk image.

        Prerequisites: source_build must have completed (source_build_ready flag)
        AND the signing key must verify (signing_key_verified flag, gated up
        front via _ensure_signing_key_verified — see CONF-02 phase 3 for why).
        The sudo password is collected interactively at the start of this command.
        """
        if not self.flags.source_build_ready:
            console.print("Run 'source_build' first")
            return

        # CONF-02 phase 3: signing-key gate.  Verify (or prompt-then-generate)
        # the project signing key before any sudo / mount / dpkg work — fast
        # to fail, easy to recover from, and the keyring is needed by
        # generate_system_configs anyway so failing fast is honest.
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
        # scrubbed on every exit path — success, build failure, KeyboardInterrupt.
        # Python strings are immutable so this only drops the reference (the GC
        # reclaims it later), but it shrinks the window in which the password is
        # reachable from this process's heap from "for the rest of the TUI
        # session" to "until this command returns".
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


    # ---------------------------------------------------------------------------
    # Command: build_iso
    # ---------------------------------------------------------------------------

    def cmd_build_iso(self, *args):
        """Build a bootable hybrid BIOS/EFI ISO from the assembled chroot.

        Usage: build_iso [force]

          force — skip the chroot_verified flag check.  After a manual
                  edit of the chroot tree (e.g. dropping in extra config
                  files between `build_chroot` and `build_iso`) the
                  in-memory chroot_verified flag is stale even though the
                  on-disk chroot may still be valid.  With force, we
                  re-run verify_chroot against the on-disk chroot using
                  the password just collected for ISO assembly, and
                  proceed only if all 8 checks still pass.

        Packages the chroot produced by build_chroot into a squashfs live
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
                console.print("Chroot built but verification failed — re-run 'verify_chroot' after fixing")
            else:
                console.print("Run 'build_chroot' first")
            return

        console.print("Initialising build system for ISO...")
        try:
            build_system = buildsystem.BuildSystem.for_iso(self.config)
        except RuntimeError as e:
            console.print(f"ERROR: build system initialisation failed — {e}")
            logger.error(f"BuildSystem.for_iso() raised: {e}")
            return

        # Same try/finally pattern as cmd_build_chroot — scrub the cached
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
        finally:
            build_system.scrub_password()


    # ---------------------------------------------------------------------------
    # Command: source_build
    # ---------------------------------------------------------------------------

    @staticmethod
    def _parse_source_build_args(args):
        """Pure-function argument parser for cmd_source_build.

        Recognises:
          - 'force' / 'recommended' as case-insensitive flag-words at any
            position
          - one optional `[profile,...]` bracket-token (override for both
            DEB_BUILD_PROFILES and DEB_BUILD_OPTIONS); multiple bracket
            tokens is a parse error
          - everything else as a package name
          - 'recommended' is mutually exclusive with named packages

        Returns ``(err, force, recommended, names, profile_override)``.
        On success ``err`` is None; on parse error ``err`` is a printable
        string the caller should surface.  ``profile_override`` is None
        when no bracket-token was given; an empty list when the operator
        wrote `[]` (most-permissive build); a populated list otherwise.
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
                        False, False, [], None,
                    )
                _bracket_token = _s[1:-1]
            else:
                _other_args.append(_a)
        _flags = {a.strip().lower() for a in _other_args}
        _force = 'force' in _flags
        _recommended = 'recommended' in _flags
        _names = [a for a in _other_args
                  if a.strip().lower() not in ('force', 'recommended')]
        if _recommended and _names:
            return (
                "Usage: 'source_build recommended' is mutually exclusive with "
                "named packages.  Use one or the other.",
                False, False, [], None,
            )
        _profile_override = None
        if _bracket_token is not None:
            _profile_override = [
                p.strip() for p in _bracket_token.split(',') if p.strip()
            ]
        return (None, _force, _recommended, _names, _profile_override)

    def cmd_source_build(self, *args):
        """Build source packages inside the Docker build container.

        Usage: source_build [force] [recommended | <pkg> ...] [[profile,...]]

          force         — rebuild packages even if a valid result already exists
          recommended   — build ONLY the EXTRAS-01 sources (depth-1 Recommends
                          pulled into the repo by parse_dependency, but
                          excluded from chroot install).  Mutually exclusive
                          with named packages.
          pkg ...       — limit the build to the named source packages
          [profile,...] — bracket-delimited token (e.g. `[nocheck]`) overrides
                          BOTH DEB_BUILD_PROFILES and DEB_BUILD_OPTIONS for
                          this invocation only.  Use `[]` (empty) for the
                          most permissive build (no profiles/options — docs
                          and tests included).  Implies `force` because the
                          .result cache wouldn't reflect the override.
          (no arg)      — build everything in selected_srcs MINUS the
                          extras-only sources (those need explicit
                          `recommended` mode)

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
            console.print("Run 'source_download' first")
            return

        if not self.flags.build_container_ready:
            console.print("Run 'build_container' first")
            return

        # Parse args via the static helper for testability.
        _err, _force, _recommended, _names, _profile_override = \
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
        if _recommended:
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
        if _names:
            packages = []
            for name in _names:
                src = self.dep_tree.selected_srcs.get(name)
                if src is None:
                    console.print(f"Unknown package: {name}")
                    return
                packages.append(src)
        elif _recommended:
            # `recommended` mode: build only sources that exist purely for
            # the recommends pull (extras_src_names is the set whose every
            # binary lands in extras_pkg_names).
            packages = [
                self.dep_tree.selected_srcs[n]
                for n in sorted(self.dep_tree.extras_src_names)
                if n in self.dep_tree.selected_srcs
            ]
        else:
            # Default: build the install closure — selected_srcs MINUS
            # extras-only sources (mixed sources are kept; their recommended
            # binaries fall out as side artefacts of dpkg-buildpackage).
            _extras = self.dep_tree.extras_src_names
            packages = [
                _s for _name, _s in self.dep_tree.selected_srcs.items()
                if _name not in _extras
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
            console.print("Run 'build_chroot' first")
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

    def cmd_auto_run(self):
        """Run the full build pipeline in sequence, bailing at the first
        step that does not set its progress flag.  Each step already resets
        its flag to False at entry and sets it to True only on success, so
        checking the flag after the call is a reliable did-it-complete probe.

        Emits the final summary (UX-03) via print_commands.summary on every
        exit path — success or abort — with stage counts, source-build
        breakdown, predicted ISO path, and total wall time.
        """
        import print_commands
        _steps = [
            (self.cmd_build_cache,       'cache_ready',           'build_cache'),
            (self.cmd_parse_dependency,  'dep_check_ready',       'parse_dependency'),
            (self.cmd_source_download,   'download_ready',        'source_download'),
            (self.cmd_init_container,    'build_container_ready', 'build_container'),
            (self.cmd_source_build,      'source_build_ready',    'source_build'),
            # build_chroot also runs verify_chroot; chroot_verified is True
            # only when both build AND all 8 verify checks passed.
            (self.cmd_build_chroot,      'chroot_verified',       'build_chroot'),
        ]

        _t0   = time.monotonic()
        _t0_dt = datetime.datetime.now()
        _aborted_at: Optional[str] = None

        for _fn, _flag, _name in _steps:
            _fn()
            if not getattr(self.flags, _flag):
                console.print(f"autorun: '{_name}' did not complete — aborting")
                logger.error(f"autorun aborted at '{_name}' (flag {_flag} not set)")
                _aborted_at = _name
                break

        if _aborted_at is None:
            console.print("autorun: all stages complete")

        _t1_dt = datetime.datetime.now()
        _elapsed = int(time.monotonic() - _t0)
        print_commands.summary(self, timing=print_commands.AutorunTiming(
            started=_t0_dt,
            finished=_t1_dt,
            elapsed=_elapsed,
            aborted_at=_aborted_at,
        ))

    # ---------------------------------------------------------------------------
    # Command: resume  (UX-04 commit B)
    # ---------------------------------------------------------------------------

    def cmd_resume(self):
        """Continue a build from a prior session — load persisted Cache +
        DependencyTree from disk to skip the slow build_cache + parse
        rebuilds, then re-validate the on-disk artefacts (downloads,
        container image, source builds) and verify (or re-verify) the
        chroot.

        Behaviour per stage:

          build_cache         loaded from `<dir_cache>/cache.pkl.gz` if
                              that file is present + SHA256 valid + format
                              version matches.  Otherwise falls back to
                              the normal cmd_build_cache rebuild.
          parse_dependency    loaded from `<dir_cache>/deptree.pkl.gz`
                              with the back-ref re-wired to the loaded
                              cache.  Falls back to cmd_parse_dependency
                              on miss.
          source_download     ALWAYS re-runs — on-disk tarballs may have
                              changed since last save; the cmd's per-file
                              hash check is the truth.
          build_container     ALWAYS re-runs — Docker state is opaque to
                              the persistence layer; init is cheap on a
                              cached image.
          source_build        ALWAYS re-runs — the per-package .result
                              files in dir_log are the truth; PASS/TUNNELED
                              packages skip naturally.
          build_chroot        SKIPPED if flags.chroot_ready already True
                              from prior session.  Otherwise runs.
          verify_chroot       runs at the end either way to confirm the
                              on-disk chroot is coherent (or to re-verify
                              after a build_chroot in this resume).

        See UX-04 commit B in the TODO row for the design rationale and
        the integrity model (SHA256 sidecar, NOT GPG-signed in v1).
        """
        import persistence
        console.print("Resuming from prior session...", tui.COLOR_INFO)

        # ── 1. Cache ──────────────────────────────────────────────────────
        self.cache = persistence.load_cache(self.config)
        if self.cache is None:
            console.print(
                "  Cache: no persisted file — running build_cache from scratch",
                tui.COLOR_INFO,
            )
            self.cmd_build_cache()
            if not self.flags.cache_ready:
                console.print(
                    "  Cache: build failed — cannot continue resume",
                    tui.COLOR_ERROR,
                )
                return
        else:
            self.flags.cache_ready = True
            console.print(
                "  Cache: restored from disk "
                f"({len(self.cache.package_hashtable)} package names)",
                tui.COLOR_HIGHLIGHT,
            )

        # ── 2. DependencyTree ─────────────────────────────────────────────
        self.dep_tree = persistence.load_dep_tree(self.config, self.cache)
        if self.dep_tree is None:
            console.print(
                "  Dep tree: no persisted file — running parse_dependency",
                tui.COLOR_INFO,
            )
            self.cmd_parse_dependency()
            if not self.flags.dep_check_ready:
                console.print(
                    "  Dep tree: parse failed — cannot continue resume",
                    tui.COLOR_ERROR,
                )
                return
        else:
            self.flags.dep_check_ready = True
            _canonical = sum(
                1 for k, v in self.dep_tree.selected_pkgs.items()
                if k == v['Package']
            )
            console.print(
                "  Dep tree: restored from disk "
                f"({_canonical} canonical packages)",
                tui.COLOR_HIGHLIGHT,
            )

        # ── 3-5. Re-validate on-disk artefacts ────────────────────────────
        # Each cmd is internally idempotent on warm state; per-file hash
        # checks (source_download), Docker image cache (build_container),
        # and per-package .result files (source_build) are the truth.
        console.print(
            "Re-validating downloads / container / built .debs...",
            tui.COLOR_INFO,
        )
        self.cmd_source_download()
        if not self.flags.download_ready:
            console.print(
                "  source_download failed — resume aborted",
                tui.COLOR_ERROR,
            )
            return
        self.cmd_init_container()
        if not self.flags.build_container_ready:
            console.print(
                "  build_container failed — resume aborted",
                tui.COLOR_ERROR,
            )
            return
        self.cmd_source_build()
        if not self.flags.source_build_ready:
            console.print(
                "  source_build failed — resume aborted",
                tui.COLOR_ERROR,
            )
            return

        # ── 6. Chroot ─────────────────────────────────────────────────────
        # If the prior session already had a verified chroot, re-verify
        # against the on-disk tree.  If chroot_ready was True but verify
        # never passed (or chroot tree is missing), re-run build_chroot.
        if self.flags.chroot_ready:
            console.print(
                "  Chroot: already built in prior session — re-running verify",
                tui.COLOR_INFO,
            )
            self.cmd_verify_chroot()
        else:
            console.print(
                "  Chroot: not built — running build_chroot",
                tui.COLOR_INFO,
            )
            self.cmd_build_chroot()

        if self.flags.chroot_verified:
            console.print(
                "Resume complete — chroot verified, ready for `build_iso`",
                tui.COLOR_HIGHLIGHT,
            )
        else:
            console.print(
                "Resume complete with chroot verification NOT passing — "
                "see verify_chroot output for the failed checks.",
                tui.COLOR_ERROR,
            )

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
    # UX-04 commit B: --resume autoruns cmd_resume after the TUI/CLI is up
    # but before wait().  Operator can also type `resume` interactively
    # at any point.  Strip same as --headless so BuildConfig doesn't see
    # it.
    _resume = '--resume' in sys.argv
    if _resume:
        sys.argv.remove('--resume')

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

    tui.register_command('build_cache',       session.cmd_build_cache,        'Build cache')
    tui.register_command('parse_dependency',  session.cmd_parse_dependency,   'Parse dependency tree for selected packages')
    tui.register_command('source_download',   session.cmd_source_download,    'Download source packages')
    tui.register_command('build_container',   session.cmd_init_container,     'Initialise Docker build container')
    tui.register_command('source_build',      session.cmd_source_build,       'Build sources \u2014 try: source_build [force] [recommended | <pkg> \u2026] [[profile,\u2026]]')
    tui.register_command('tunnel_package',    session.cmd_tunnel_package,     'Download binary .debs from Debian repo (tunnel_package [pkg \u2026])')
    tui.register_command('build_chroot',      session.cmd_build_chroot,       'Build bootable chroot environment')
    tui.register_command('verify_chroot',     session.cmd_verify_chroot,      'Verify chroot health \u2014 8 checks, PASS/FAIL per test')
    tui.register_command('build_iso',         session.cmd_build_iso,          'Build bootable ISO from chroot (build_iso)')
    tui.register_command('autorun',           session.cmd_auto_run,           'Runs all commands in sequence')
    tui.register_command('resume',            session.cmd_resume,             'Resume from disk: load Cache + DependencyTree, re-validate downloads/container/build, verify chroot (UX-04 commit B)')
    tui.register_command('print',             session.cmd_print,              'Print build state — try: print help')
    tui.register_command('generate_signing_key', session.cmd_generate_signing_key, 'Generate the project signing keypair (one-time setup; refuses overwrite without `force`)')
    tui.register_command('verify_signing_key',   session.cmd_verify_signing_key,   'Sign+verify roundtrip against the current signing key')

    console.print(asciiart_logo, tui.COLOR_ERROR)
    console.print("Starting Source Build System for Athena Linux...", tui.COLOR_HIGHLIGHT)
    console.print(f"\tArch\t\t\t{config.arch}")
    console.print(f"\tParent Distribution\t{config.release} {config.baseversion}")
    console.print(f"\tBuild Distribution\t{config.build_codename} {config.build_version}")

    # UX-04: surface restored flags from a prior session if any survived
    # the load (in-memory-only ones get reset; on-disk ones — download,
    # source_build, chroot — survive).  Tells the operator they can pick
    # up where they left off rather than starting from scratch.
    _restored = session.flags.restored_summary()
    if _restored:
        console.print(
            f"Restored from prior session: {_restored}",
            tui.COLOR_INFO,
        )
        console.print(
            "  (in-memory state — cache, dep tree, container — was NOT "
            "restored; re-run those stages or use `autorun`/`resume`)"
        )

    # UX-04 commit B: --resume on the command line auto-fires cmd_resume
    # after registration is done, before handing control to wait() (the
    # TUI event loop or CLI REPL).  Resume runs synchronously on the
    # main thread; widgets / prompts within it work via the same backend
    # as any operator-typed command.
    if _resume:
        console.print(
            "--resume passed at startup; running `resume` automatically",
            tui.COLOR_INFO,
        )
        session.cmd_resume()

    tui_inst.wait()
    Exit(0)


if __name__ == '__main__':
    build_banner = "Athena Build Environment v0.1"
    print(asciiart_logo)
    main(build_banner)
