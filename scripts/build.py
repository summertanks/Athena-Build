# (C) Athena Build Project
"""
build.py — top-level orchestrator for the Athena Build System.

Responsibilities:
  - Parse build configuration and APT cache
  - Resolve the full dependency tree for the target package list
  - Download upstream source archives
  - Manage the Docker build container image
  - Build each selected source package inside a clean container
  - Optionally tunnel (download prebuilt) binary packages from the base Debian repo
  - Expose all of the above as interactive TUI commands

Typical operator workflow:
    cache build → cache parse → source sync → container init → source build

Each step sets a flag in _progress_flags so later commands can verify prerequisites
without re-running earlier work.
"""

import atexit
import faulthandler

# Module-level handle so reload-during-dev cleans up + atexit pairs the
# close on normal exit.  The previous form (faulthandler.enable(open(...)))
# leaked the fd for the process lifetime.
_FAULT_LOG = open('/tmp/athena_crash.log', 'w')
faulthandler.enable(_FAULT_LOG)
atexit.register(_FAULT_LOG.close)

import tui
import datetime
import os
import glob
import re
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
import repo_audit
import signal


import logging
from tui import Tui, console, Prompt, PROMPT_YESNO, PROMPT_INPUT, PROMPT_PASSWORD, Spinner, ProgressBar, Exit
from tui import setup_file_logging

logger = logging.getLogger('athena')

asciiart_logo = (
    '╭─╮╶┬╴╷ ╷╭─╴╭╮╷╭─╮   ╭╮ ╷ ╷╷╷  ╶┬╮   ╭─╮╷ ╷╭─╮╶┬╴╭─╴╭┬╮\n'
    '├─┤ │ ├─┤├╴ │╰┤├─┤   ├┴╮│ │││   ││   ╰─╮╰┬╯╰─╮ │ ├╴ │││\n'
    '╵ ╵ ╵ ╵ ╵╰─╴╵ ╵╵ ╵   ╰─╯╰─╯╵╰─╴╶┴╯   ╰─╯ ╵ ╰─╯ ╵ ╰─╴╵ ╵\n'
    '@Harkirat S Virk\n'
    'https://github.com/summertanks/Athena-Build'
)

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
        # Installer chroot built from udeb closure.  Independent of
        # chroot_ready/_verified — the two chroots have different
        # lifecycles (live = squashfs payload; installer = initrd).
        self.chroot_installer_ready: bool = False  # build_chroot installer completed
        # Set on a successful `iso build live` / `iso build installer`.  Lets
        # `autorun live` / `autorun installer` gate the ISO-build step the
        # same way they gate every other stage — without these the autorun
        # step driver has no flag to check, so a silent ISO-build failure
        # would not be caught.
        self.iso_live_ready: bool = False
        self.iso_installer_ready: bool = False
        # Set on a successful `iso build disk`.  The disk image reuses the
        # verified live chroot (same gate as iso_live_ready) but is a
        # distinct end-state artifact (qcow2, not ISO), so it carries its
        # own flag — lets `autorun disk` gate the final step and `print
        # state` surface the disk target.
        self.iso_disk_ready: bool = False

    def __str__(self) -> str:
        """Return a compact one-line status string for display in the TUI."""
        fields = ['cache_ready', 'dep_check_ready', 'download_ready',
                  'build_container_ready', 'source_build_ready',
                  'signing_key_verified',
                  'chroot_ready', 'chroot_verified', 'chroot_installer_ready',
                  'iso_live_ready', 'iso_installer_ready', 'iso_disk_ready']
        return '  '.join(f"[{'✓' if getattr(self, f) else '·'}] {f.replace('_ready', '')}" for f in fields)


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


class BuildSession:
    """Owns the full pipeline state and the cmd_* command handlers the TUI
    registers.  Replaces the prior module-level globals (build_config,
    build_cache, dependency_tree, build_container, _tui, _progress_flags)
    so handlers can be exercised without standing up the curses TUI.
    """

    def __init__(self, config: BuildConfig, tui_inst) -> None:
        # tui_inst is either a Tui or a Cli — both implement the same
        # duck-typed surface but the static types diverge.  No annotation
        # so mypy doesn't force callers to pick one or the other.
        self.config: BuildConfig = config
        self.tui = tui_inst
        self.cache: 'Optional[Cache]' = None
        self.dep_tree: 'Optional[dependencytree.DependencyTree]' = None
        # Parallel dep tree resolved against the udeb world
        # (Cache.udeb_hashtable via Cache.udeb_view()).  Populated
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
        caller treats that as "no exclusive packages".
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


    # --------------------------------------clean dispatchers---------------------------------
    # Each `clean <X>` wipes the working dir for stage X and resets the
    # corresponding BuildFlags + drops in-memory state pointing at the
    # deleted files.  Sudo for buildroot/* (root-owned chroot content);
    # plain Python for user-owned dirs (cache/, source/, repo/, image/,
    # download/).  `force` arg skips the YESNO prompt — used by `clean
    # all` to consolidate confirmation into a single up-front prompt.

    def _wipe_dir_contents(self, label: str, path: str,
                           sudo: bool, password: str = '',
                           skip_prompt: bool = False) -> bool:
        """Remove every entry inside `path` (the dir itself is preserved,
        so BuildConfig's user-owned-dir invariants stay intact).
        Returns True on success or no-op (empty/missing dir), False on
        any partial failure or operator decline."""
        try:
            _entries = list(os.scandir(path))
        except FileNotFoundError:
            console.print(f"{label}: directory does not exist (already clean): {path}")
            return True
        except OSError as e:
            console.print(f"ERROR: cannot read {label} directory: {e}")
            logger.error(f"clean {label}: scandir({path}): {e}")
            return False

        if not _entries:
            console.print(f"{label}: already empty: {path}")
            return True

        # Best-effort size: top-level files only (recursing under sudo
        # for chroot dirs would be slow).  Operator gets a number to
        # judge "is this what I meant to wipe".
        _total_bytes = 0
        for _e in _entries:
            try:
                if _e.is_file(follow_symlinks=False):
                    _total_bytes += _e.stat().st_size
            except OSError:
                pass
        _mb = _total_bytes / (1024 * 1024)

        if not skip_prompt:
            _resp = Prompt(PROMPT_YESNO,
                f"Wipe {label}: {len(_entries)} entries "
                f"(top-level ~{_mb:.1f} MB) at {path}?"
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                console.print(f"{label}: clean cancelled.")
                return False

        if sudo:
            # `find -mindepth 1 -maxdepth 1 -exec rm -rf {} +` deletes
            # every direct child without recursion through find itself
            # (rm does the recursion).  Avoids globbing in shell.
            _r = subprocess.run(
                ['sudo', 'find', path,
                 '-mindepth', '1', '-maxdepth', '1',
                 '-exec', 'rm', '-rf', '{}', '+'],
                capture_output=True, text=True,
            )
            if _r.returncode != 0:
                console.print(
                    f"ERROR: clean {label} failed: "
                    f"{_r.stderr.strip()[:200]}"
                )
                logger.error(
                    f"clean {label}: rc={_r.returncode}, "
                    f"stderr={_r.stderr.strip()}"
                )
                return False
        else:
            _failed = 0
            for _e in _entries:
                try:
                    if _e.is_dir(follow_symlinks=False):
                        shutil.rmtree(_e.path)
                    else:
                        os.unlink(_e.path)
                except OSError as ex:
                    logger.error(f"clean {label}: cannot delete {_e.path}: {ex}")
                    _failed += 1
            if _failed:
                console.print(
                    f"ERROR: clean {label} partial: {_failed} entries "
                    "failed (see log)", tui.COLOR_WARNING)
                return False

        console.print(
            f"{label}: cleaned ({len(_entries)} entries removed)",
            tui.COLOR_INFO,
        )
        return True

    def _refresh_sudo(self, password: str) -> bool:
        """Validate the sudo password without consuming any other input.
        Returns True iff `sudo -v` succeeds.  Used by clean handlers
        that need root before doing the actual `sudo find ... -exec rm`.
        """
        _r = subprocess.run(['sudo', '-S', '-v'],
            input=password + '\n', capture_output=True, text=True)
        if _r.returncode != 0:
            console.print("ERROR: incorrect sudo password")
            logger.error("clean: sudo -v failed")
            return False
        return True

    def cmd_clean_source(self, *args):
        """Wipe downloaded source tarballs.  Resets download_ready so
        the next `source sync` re-fetches.  Source bytes are
        re-downloadable from upstream; cleaning is safe."""
        if not self._wipe_dir_contents(
                'source', self.config.dir_source,
                sudo=False, skip_prompt='force' in args):
            return
        self.flags.download_ready = False

    def cmd_clean_repo(self, *args):
        """Wipe built .deb / .udeb / .dsc / .source files.  Resets
        source_build_ready so the next `source build` rebuilds.  Note:
        does NOT clean buildroot/ — chroot may still reference files
        that no longer exist; clean buildroot too if you want a fresh
        chroot off the new repo."""
        if not self._wipe_dir_contents(
                'repo', self.config.dir_repo,
                sudo=False, skip_prompt='force' in args):
            return
        self.flags.source_build_ready = False
        self.last_source_build_counts = None

    def cmd_container_purge(self, *args):
        """Stop+remove all containers spawned from `athenalinux:build-*`
        images, then remove the images themselves.  Useful when:
          - Docker daemon has orphaned containers from interrupted
            builds (we use auto_remove=False so logs survive
            container.wait(), but a SIGKILL between wait() and
            remove() can leak)
          - Dockerfile changed and you want to force a fresh image
            rebuild on the next `container init`
          - Disk pressure from accumulated image layers across builds

        Resets build_container_ready and drops self.container.
        `force` arg skips the YESNO prompt — used by `clean all`.

        Honours the same `[Build] DOCKER_SERVER` knob `cmd_init_container`
        does — purges against the configured remote daemon if set,
        else local.
        """
        _force = 'force' in args
        try:
            import docker
        except ImportError as e:
            console.print(f"ERROR: docker module not installed: {e}")
            logger.error(f"container purge: import docker raised: {e}")
            return

        try:
            if self.config.docker_server:
                _client = docker.DockerClient(base_url=self.config.docker_server)
            else:
                _client = docker.from_env()
            _client.ping()
        except (docker.errors.DockerException, OSError) as e:
            console.print(f"ERROR: cannot connect to Docker daemon: {e}")
            logger.error(f"container purge: docker connect raised: {e}")
            return

        # List athenalinux containers (all states — running + stopped).
        # Filter on image tag prefix; `image.tags` is a list because one
        # image can carry multiple tags.
        _our_containers = []
        try:
            for _c in _client.containers.list(all=True):
                _img_tags = []
                try:
                    _img_tags = _c.image.tags or []
                except docker.errors.APIError:
                    pass
                if any(t.startswith('athenalinux:build-') for t in _img_tags):
                    _our_containers.append(_c)
        except docker.errors.APIError as e:
            console.print(f"ERROR: cannot list containers: {e}")
            logger.error(f"container purge: list containers raised: {e}")
            return

        # List athenalinux:build-* images.  Repository filter narrows to
        # the right namespace; per-tag check rules out stray tags.
        _our_images = []
        try:
            for _img in _client.images.list(name='athenalinux'):
                if any(t.startswith('athenalinux:build-')
                       for t in (_img.tags or [])):
                    _our_images.append(_img)
        except docker.errors.APIError as e:
            console.print(f"ERROR: cannot list images: {e}")
            logger.error(f"container purge: list images raised: {e}")
            return

        if not _our_containers and not _our_images:
            console.print(
                "container purge: no athenalinux containers or images present"
            )
            self.container = None
            self.flags.build_container_ready = False
            return

        if not _force:
            _resp = Prompt(PROMPT_YESNO,
                f"Stop+remove {len(_our_containers)} container(s) and "
                f"{len(_our_images)} image(s) tagged "
                "`athenalinux:build-*`?"
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                console.print("container purge: cancelled.")
                return

        # Step 1: kill+remove containers.  force=True kills running
        # containers first; non-force on a running container raises.
        _container_failed = 0
        for _c in _our_containers:
            try:
                _c.remove(force=True)
            except docker.errors.APIError as e:
                logger.warning(
                    f"container purge: remove {_c.short_id} failed: {e}"
                )
                _container_failed += 1

        # Step 2: remove images.  force=True removes even when stopped
        # containers reference them (they were just removed but Docker
        # may still hold image-ref state mid-flight).
        _image_failed = 0
        for _img in _our_images:
            try:
                _client.images.remove(_img.id, force=True)
            except docker.errors.APIError as e:
                logger.warning(
                    f"container purge: remove image {_img.short_id} failed: {e}"
                )
                _image_failed += 1

        self.container = None
        self.flags.build_container_ready = False

        _ok_c = len(_our_containers) - _container_failed
        _ok_i = len(_our_images) - _image_failed
        if _container_failed or _image_failed:
            console.print(
                f"container purge: {_ok_c} container(s) + {_ok_i} image(s) "
                f"removed; {_container_failed} container + {_image_failed} "
                "image failures (see log)",
                tui.COLOR_WARNING,
            )
        else:
            console.print(
                f"container purge: {_ok_c} container(s) + {_ok_i} image(s) "
                "removed",
                tui.COLOR_INFO,
            )

    def cmd_clean_buildroot(self, *args):
        """Wipe both live and installer chroots.  Sudo required —
        chroot contents are root-owned (debootstrap + dpkg --unpack
        run as root; remove-time also needs root)."""
        _force = 'force' in args
        _password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()
        if not self._refresh_sudo(_password):
            return
        _ok_live = self._wipe_dir_contents(
            'buildroot/live', self.config.dir_chroot,
            sudo=True, password=_password, skip_prompt=_force)
        _ok_inst = self._wipe_dir_contents(
            'buildroot/installer', self.config.dir_chroot_installer,
            sudo=True, password=_password, skip_prompt=_force)
        if _ok_live:
            self.flags.chroot_ready = False
            self.flags.chroot_verified = False
        if _ok_inst:
            self.flags.chroot_installer_ready = False

    def cmd_clean_image(self, *args):
        """Wipe built ISOs (and their staging dirs).  Resets iso_*_ready
        so the next `iso build` rebuilds."""
        if not self._wipe_dir_contents(
                'image', self.config.dir_image,
                sudo=False, skip_prompt='force' in args):
            return
        self.flags.iso_live_ready = False
        self.flags.iso_installer_ready = False
        self.flags.iso_disk_ready = False

    def cmd_clean_download(self, *args):
        """Wipe tunneled .deb downloads.  These are re-fetched on
        demand by `repo tunnel` so cleaning is safe."""
        self._wipe_dir_contents(
            'download', self.config.dir_download,
            sudo=False, skip_prompt='force' in args)

    def cmd_clean_all(self, *args):
        """Wipe every working dir + reset every BuildFlag + drop every
        in-memory pipeline reference.  Equivalent to `clean cache` +
        `clean source` + `clean repo` + `clean download` + `clean
        image` + `clean buildroot`, but with a single up-front
        confirmation and a single sudo unlock for the buildroot wipe.
        Preserved: gnupg/ (signing key), log/ (build history),
        patch/ (patch series)."""
        _force = 'force' in args
        if not _force:
            _resp = Prompt(PROMPT_YESNO,
                "clean all: wipes cache/, source/, repo/, download/, "
                "image/, buildroot/{live,installer}, and athenalinux "
                "Docker containers + images.  gnupg/ + log/ + patch/ "
                "preserved.  Continue?"
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                console.print("clean all: cancelled.")
                return

        _password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()
        if not self._refresh_sudo(_password):
            return

        # User-owned dirs: skip per-step prompts (we already confirmed).
        self.cmd_cache_purge('force')
        self._wipe_dir_contents('source',   self.config.dir_source,   sudo=False, skip_prompt=True)
        self._wipe_dir_contents('repo',     self.config.dir_repo,     sudo=False, skip_prompt=True)
        self._wipe_dir_contents('download', self.config.dir_download, sudo=False, skip_prompt=True)
        self._wipe_dir_contents('image',    self.config.dir_image,    sudo=False, skip_prompt=True)
        # Sudo dirs: re-use the unlocked password.
        self._wipe_dir_contents('buildroot/live',
            self.config.dir_chroot, sudo=True, password=_password, skip_prompt=True)
        self._wipe_dir_contents('buildroot/installer',
            self.config.dir_chroot_installer, sudo=True, password=_password, skip_prompt=True)
        # Docker side: kills running athenalinux containers + removes
        # images so next `container init` rebuilds from Dockerfile.
        self.cmd_container_purge('force')

        # Drop in-memory state and reset every flag.  cache_ready and
        # dep_check_ready already cleared by cmd_cache_purge — explicit
        # here so the operator-visible end-state is "everything zero".
        self.cache = None
        self.dep_tree = None
        self.udeb_dep_tree = None
        self.flags = BuildFlags()
        self.last_source_build_counts = None
        # Scrub the password we just collected — same hygiene the rest
        # of the codebase uses for sudo passwords.
        _password = '*' * len(_password)
        console.print("clean all: complete — pipeline state reset", tui.COLOR_INFO)


    # --------------------------------------Command: parse_dependency-------------------------------------

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
        self.flags.dep_check_ready = True


    # --------------------------------------Command: patch_refresh-------------------------------------

    def _refresh_patches(self) -> int:
        # ⚠️  DESTRUCTIVE — DELETES .result FILES when patch-set content
        # has changed since the last build (see ~line 1261 below).
        # The name "refresh" is misleading by today's standards;
        # callers expecting read-only semantics MUST NOT invoke this.
        # Read-only commands (cmd_source_audit, cmd_print*, anything
        # whose name suggests "show / scan / status") are pinned by
        # test_readonly_named_commands_have_no_destructive_calls() to
        # never reach here.
        #
        # Discovered the hard way 2026-05-19: the prior cmd_source_rescan called
        # this on entry to "make the count reflect current patch
        # state" — over-counted by 47 packages because the side
        # effect wiped their PASS state.
        #
        # If you need ONLY the patch_list population (not the .result
        # invalidation), split this function or extract that pass.
        #
        # Scan the patch tree for files matching <package>/<version>/*.patch and
        # populate each Source's patch_list.  Sorting by the first five characters
        # preserves the numeric prefix ordering (e.g. 9001-, 9002-) used to control
        # application order.  Resets patch_list per source so removed patch files
        # are reflected on re-runs (operator-driven `patch_refresh` after
        # out-of-band changes to the patch tree).
        #
        # Walks BOTH the deb tree AND the udeb tree.
        # Without the udeb pass, sources that live only in the udeb closure
        # (e.g. fuse3 → libfuse3-3-udeb pulled by a d-i udeb) never get
        # their patches discovered — `source build fuse3` then fails because
        # patch_list is empty even though patch/source/fuse3/<ver>/*.patch
        # exists on disk.  Caught in production 2026-05-10.  Both trees
        # share Source instances via source_hashtable, so the union dict
        # naturally dedupes and each Source's patch_list is set exactly once.
        # Caller gates on dep_check_ready, which implies dep_tree is set.
        assert self.dep_tree is not None
        _unified_srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _name, _src in self.udeb_dep_tree.selected_srcs.items():
                if _name not in _unified_srcs:
                    _unified_srcs[_name] = _src

        for _pkg in _unified_srcs:
            _src = _unified_srcs[_pkg]
            # version_no_epoch: patch dirs follow Debian's filename
            # convention (epoch stripped).  Without this, `git`
            # (Version `1:2.39.5-…`) and `llvm-toolchain-15`
            # (Version `1:15.0.6-…`) silently never get their patches
            # discovered — the lookup uses `1:…` while the dir is `…`.
            _ver = utils.version_no_epoch(_src.version)
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

        # Invalidate stale .result files when the patch SET for a source
        # has changed since the last successful build.  Without this,
        # autorun's source-build step happily skips packages with
        # `[SKIPPED] already built` even after the operator drops a new
        # patch in patch/source/<pkg>/<ver>/ — and the patch never takes
        # effect.  Caught 2026-05-13 with the base-installer Phase C
        # keyring patch: the patch was on disk, _refresh_patches
        # discovered it, but check_build saw the older .result + .udeb
        # and skipped the rebuild.  Install booted the unpatched
        # base-installer → `gpgv: Can't check signature: No public key`.
        #
        # Two-stage check.  Stage 1 (cheap) is mtime: if no patch is
        # newer than .result, nothing to do.  Stage 2 (precise) is
        # content hash: only invalidate when the patch CONTENT actually
        # changed, not just the mtime.  This avoids spurious rebuilds
        # from header-only edits (DEP-3 commentary, comment tweaks) that
        # bump the mtime but produce an identical diff.
        #
        # Migration: when no .patchhash exists yet (pre-CONF-08 builds),
        # we trust that the existing .result reflects the current patch
        # set (the build that produced it must have applied them), write
        # a baseline hash, and skip invalidation.  Subsequent runs use
        # the recorded baseline normally.
        #
        # Also catches patch REMOVAL: empty patch_list with an existing
        # .patchhash for a non-empty old set → hash differs → invalidate.
        # (Previously documented as not worth the schema; the hash file
        # makes it free.)
        _buildlog = os.path.join(self.config.dir_log, 'build')
        _invalidated = []
        for _pkg, _src in _unified_srcs.items():
            _result_file = os.path.join(_buildlog, _pkg + '.result')
            if not os.path.exists(_result_file):
                continue
            _patch_dir = os.path.join(
                self.config.dir_patch_source, _pkg, utils.version_no_epoch(_src.version),
            )
            _hash_file = os.path.join(_buildlog, _pkg + '.patchhash')
            _stored_hash = None
            try:
                with open(_hash_file, 'r') as fh:
                    _stored_hash = fh.read().strip() or None
            except OSError:
                pass

            # Patch removal: no patches now, but a baseline hash exists
            # for a previous non-empty set → invalidate.  (Hash of empty
            # set differs from any non-empty hash.)
            if not _src.patch_list:
                if _stored_hash is None:
                    continue
                _current_hash = utils.patch_set_hash(_patch_dir, [])
                if _stored_hash == _current_hash:
                    continue
                try:
                    os.remove(_result_file)
                    os.remove(_hash_file)
                    _invalidated.append(_pkg)
                except OSError as e:
                    logger.warning(
                        f"[patch] {_pkg}: cannot remove stale {_result_file}: {e}"
                    )
                continue

            try:
                _result_mtime = os.path.getmtime(_result_file)
            except OSError:
                continue
            _newer = any(
                os.path.getmtime(os.path.join(_patch_dir, _pf)) > _result_mtime
                for _pf in _src.patch_list
                if os.path.exists(os.path.join(_patch_dir, _pf))
            )
            if not _newer:
                continue

            _current_hash = utils.patch_set_hash(_patch_dir, _src.patch_list)

            # Compute a target mtime that is guaranteed > every patch
            # mtime, so the next patch_refresh's mtime gate won't keep
            # re-entering this branch.  os.utime(path, None) uses the
            # kernel clock and can land slightly BEFORE a patch mtime
            # set from time.time() (different clock sources / coarser
            # resolution); set the value explicitly instead.
            _newest_patch_mtime = max(
                (os.path.getmtime(os.path.join(_patch_dir, _pf))
                 for _pf in _src.patch_list
                 if os.path.exists(os.path.join(_patch_dir, _pf))),
                default=0.0,
            )
            _touch_mtime = max(time.time(), _newest_patch_mtime + 1.0)

            if _stored_hash is None:
                # First encounter post-upgrade (or post-clean): the
                # existing .result was written by a build that already
                # applied the current patch set.  Record the baseline,
                # touch .result so we don't re-enter this branch every
                # patch_refresh, and skip invalidation.
                try:
                    with open(_hash_file, 'w') as fh:
                        fh.write(_current_hash + '\n')
                    os.utime(_result_file, (_touch_mtime, _touch_mtime))
                except OSError as e:
                    logger.warning(
                        f"[patch] {_pkg}: cannot write {_hash_file}: {e}"
                    )
                continue

            if _stored_hash == _current_hash:
                # Cosmetic edit (header / comment) — content unchanged.
                # Touch .result to reset the mtime gate; no rebuild.
                try:
                    os.utime(_result_file, (_touch_mtime, _touch_mtime))
                except OSError:
                    pass
                continue

            # Real content change — invalidate + record new baseline.
            try:
                os.remove(_result_file)
                with open(_hash_file, 'w') as fh:
                    fh.write(_current_hash + '\n')
                _invalidated.append(_pkg)
            except OSError as e:
                logger.warning(
                    f"[patch] {_pkg}: cannot remove stale {_result_file}: {e}"
                )
        if _invalidated:
            _names = ', '.join(sorted(_invalidated))
            console.print(
                f"Invalidated {len(_invalidated)} stale .result file(s) — "
                f"these will rebuild next source_build: {_names}",
                tui.COLOR_INFO,
            )
            logger.info(f"[patch] invalidated stale .result: {_names}")
        return _patched

    def cmd_patch_refresh(self):
        """Re-scan the patch tree and refresh each Source's patch_list.

        Use after editing patch/source/<pkg>/<ver>/ out-of-band so the next
        source_build picks up the new patch set without re-running the full
        parse_dependency stage.  Requires parse_dependency to have run at
        least once.
        """
        if not self.flags.dep_check_ready:
            console.print("Dependency tree not ready, run 'cache parse' first")
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

        _resp = Prompt(
            PROMPT_YESNO, _msg,
            informational=True,   # UX-05f: signing-key one-time setup, OK under --yes
        ).get_response()
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
        console.print(f"  Created     : {signing.format_gpg_time(_info['created'])}")
        console.print(f"  Expires     : {signing.format_gpg_time(_info['expires'], '(never — manual rotation)')}")

        _ok, _msg = signing.verify_key(self.config)
        if _ok:
            console.print(f"  Verification: OK — {_msg}", tui.COLOR_HIGHLIGHT)
        else:
            console.print(f"  Verification: FAIL — {_msg}", tui.COLOR_ERROR)


    # -----------------------------------Command: source_download--------------------

    def cmd_source_sync(self, *args):
        """Download upstream source archives.

        Bulk mode (no args) — fetch .dsc, .orig.tar.*, .debian.tar.* for
        every source in dep_tree.selected_srcs + udeb_dep_tree.selected_srcs.
        Skips files whose SHA256 already matches; sets `download_ready`.

        Per-pkg mode (`source sync <pkg> [<pkg>…] [force]`) — fetch
        just the named source(s).  `force` deletes existing files first,
        bypassing the SHA256-skip short-circuit (useful when a file is
        corrupt but its size+sha somehow still match what's expected).
        Doesn't touch `download_ready` — partial pulls aren't the
        full-corpus gate the flag tracks.

        Downloads from BOTH the deb tree AND the udeb tree in bulk
        mode.  Without the udeb pass, sources that exist only in the
        udeb closure (base-installer, debian-installer-utils,
        debootstrap, …) never land in dir_source, and a later
        `source build installer` fails with "cp: cannot stat
        /source/<pkg>*: No such file or directory" inside the build
        container.  Sources shared between trees are skipped in the
        second pass via the on-disk sha check.
        """
        if not self.flags.dep_check_ready:
            console.print("Run 'cache parse' first")
            return

        _force = 'force' in args
        _named = [a for a in args if a != 'force']

        if _named:
            return self._sync_named_sources(_named, _force)

        # Bulk path.
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
            _resp = Prompt(
                PROMPT_YESNO,
                "Download size mismatch, continue?",
                informational=True,   # UX-05f
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        self.flags.download_ready = True

    def _sync_named_sources(self, named: 'list[str]', force: bool) -> None:
        """Per-source download path for `source sync <pkg> [force]`.

        Looks up each name in either dep_tree.selected_srcs or
        udeb_dep_tree.selected_srcs; constructs a synthetic minimal
        tree-shaped wrapper that exposes the two attributes
        utils.download_source reads (`selected_srcs` dict, integer
        `download_size`); runs the download.  Unknown names are
        reported and skipped — partial success is the design choice.
        """
        class _SingleSrcTree:
            def __init__(self, _name, _src):
                self.selected_srcs = {_name: _src}
                self.download_size = sum(
                    int(_f.get('size', 0)) for _f in _src.files.values()
                )

        for _name in named:
            _src = None
            for _tree in (self.dep_tree, self.udeb_dep_tree):
                if _tree is not None and _name in _tree.selected_srcs:
                    _src = _tree.selected_srcs[_name]
                    break
            if _src is None:
                console.print(
                    f"source sync {_name}: not in dep_tree.selected_srcs "
                    f"(run `cache parse` if you expect it to be there)",
                    tui.COLOR_WARNING,
                )
                continue
            if force:
                for _f in _src.files:
                    try:
                        os.unlink(os.path.join(self.config.dir_source, _f))
                    except OSError:
                        pass
            console.print(f"source sync {_name}: fetching "
                          f"{len(_src.files)} file(s)…")
            utils.download_source(
                _SingleSrcTree(_name, _src),  # type: ignore[arg-type]
                self.config.dir_source,
            )


    # -----------------------------Command: self.container------------------------
    def cmd_init_container(self):
        """Initialise the Docker build container image.

        Builds the image from config/Dockerfile if it does not exist or if the
        Dockerfile has changed since the last build (detected via SHA-256 label).
        Optionally connects to an external Docker daemon if DOCKER_SERVER is set
        in build.conf; falls back to the local daemon on connection failure.

        REQUIRES `cache build` to have run first.  BuildContainer is
        constructed with cache=self.cache so source-build's
        `Source.build_depends(cache=…)` can expand virtual provider
        groups (`libcurl4-dev` → libcurl4-{openssl,gnutls,nss}-dev,
        `libjpeg-dev` → libjpeg62-turbo-dev / libjpeg-turbo8-dev, etc.).
        Without the cache, the in-container `apt-get install` for source
        build-deps fails non-interactively on any virtual:
            E: Package 'libcurl4-dev' has no installation candidate
        Refusing to init pre-cache catches the ordering mistake at the
        command boundary instead of leaving a silently-broken container
        that fails subsequent source builds with cryptic apt errors.
        """
        if not self.flags.cache_ready:
            console.print(
                "container init: requires `cache build` first — without "
                "the cache, in-container apt-installs of virtual "
                "Build-Depends (libcurl4-dev, libjpeg-dev etc.) fail "
                "non-interactively on source build"
            )
            return
        self.flags.build_container_ready = False
        spin = Spinner("Initialising build container")
        try:
            self.container = buildcontainer.BuildContainer(
                self.config,
                docker_server=self.config.docker_server or None,
                cache=self.cache,
            )
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
            src_pkg: Source package object — uses .directory (pool path)
                     and .package (source name).  The list of binary
                     filenames to download is resolved via the per-tree
                     src_pkg_files maps (see _predicted_files_for_source).

        Returns:
            True if every binary package was downloaded successfully, False otherwise.
        """
        # Tunneled = pristine Debian binary passthrough.  The filename on
        # disk MUST match the .deb's internal Version (else verify_pkg_artifact
        # flags a filename↔control mismatch), and the URL must match what
        # snapshot.debian.org actually serves — both are the upstream Filename
        # from the cache (carries any ~debNuN / +debNuN suffix), NOT the
        # strip_nmu pristine prediction.  _predicted_files_for_source returns
        # the pristine names (right for BUILT packages whose strip rewrites
        # both control + filename); for tunnel we resolve back to the upstream
        # filename via dep_tree.selected_pkgs[bin].get('Filename').
        _files = self._tunnel_filenames_for_source(src_pkg.package)
        if not _files:
            logger.error(f"tunnel {src_pkg.package}: no binary packages known (run parse_dependency first)")
            return False

        # Construct the pool base URL from the source's origin mirror.  This
        # matters for sources in bookworm-security, whose pool lives at a
        # different baseid than main.
        if src_pkg._mirror is None:
            logger.error(f"tunnel {src_pkg.package}: source has no _mirror — cache ingest bug")
            return False
        _base = src_pkg._mirror.url
        # Route tunneled binaries to their apt component (from the origin
        # mirror): non-free/contrib/non-free-firmware land in the matching
        # repo/dists/<codename>/<comp>/ dir, main stays main.  Empty/flat
        # component → main.
        _comp = src_pkg._mirror.component or 'main'
        _success = True

        # Caller gates this on build_container_ready, so self.container
        # is non-None by the time we get here.
        assert self.container is not None
        for _filename in _files:
            _dst_dir = self.config.deb_dest_for_filename(_filename, _comp)
            _dest = os.path.join(_dst_dir, _filename)

            # Wipe ANY same-binary .deb in the destination that doesn't match
            # the current expected upstream filename.  A prior run with the
            # strip_nmu-pristine bug could have left a wrong-version .deb here
            # (e.g. amd64-microcode_3.x_amd64.deb when the snapshot's actual
            # bookworm-security file is amd64-microcode_3.x~deb12u1_amd64.deb).
            # Without this, both would coexist in repo/ and dpkg-scanpackages
            # would publish both — apt would prefer the unsuffixed one (~deb
            # sorts BEFORE the base), installing the wrong (unstable) binary.
            _bin_name = _filename.split('_', 1)[0]
            try:
                for _existing in os.listdir(_dst_dir):
                    if not (_existing.endswith(('.deb', '.udeb'))):
                        continue
                    if _existing.split('_', 1)[0] != _bin_name:
                        continue
                    if _existing == _filename:
                        continue
                    _stale = os.path.join(_dst_dir, _existing)
                    logger.info(
                        f"tunnel {src_pkg.package}: removing stale "
                        f"non-matching {_existing} (expected {_filename})")
                    try:
                        os.remove(_stale)
                    except OSError as _e:
                        logger.warning(
                            f"tunnel {src_pkg.package}: rm {_stale}: {_e}")
            except OSError:
                pass

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
            console.print("Run 'cache parse' first")
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
        progress_bar = ProgressBar(label='Tunnel', maxvalue=len(packages), show_rate=False)

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
                console.print(f"  Created     : {signing.format_gpg_time(_info['created'])}")
                console.print(f"  Expires     : {signing.format_gpg_time(_info['expires'], '(never — manual rotation)')}")
            self.flags.signing_key_verified = True
            return True

        # Verify failed — usually because no key has been generated yet,
        # but could also be expired / agent issues / passphrase added
        # out-of-band.  Surface the actual reason before the prompt so
        # the operator can decide whether `generate` is the right fix.
        console.print(f"Signing key check FAILED — {_msg}", tui.COLOR_WARNING)

        console.print( "valid key is required before the chroot setup can proceed.")

        _resp = Prompt(
            PROMPT_YESNO,
            "Generate a new signing key for "
            "'{self.config.signing_key_uid}' now?",
            informational=True,   # UX-05f: pre-chroot gate, OK under --yes
        ).get_response()

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


    @staticmethod
    def _canonical_names(tree) -> 'set[str]':
        """Return only canonical-name keys from selected_pkgs.

        DependencyTree.selected_pkgs registers BOTH canonical pkg names
        AND every virtual name a selected pkg provides (parse_dependency
        L494's Provides walk).  Raw .keys() therefore includes virtual
        aliases.

        For cohort/corpus scopes used by the audit, virtual aliases are
        misleading: a name like 'fuse' that's in selected_pkgs only as
        an alias of fuse3 (via `Provides: fuse`) doesn't represent a
        pkg dpkg would install under that name.  Including it as a
        cohort member causes audit_conflict_cohort to false-positive on
        the canonical Debian fork-replaces-upstream idiom:
          fuse3 Provides: fuse + fuse3 Breaks: fuse
                  → 'fuse' is in cohort (as virtual)
                  → audit flags fuse3 → fuse conflict
                  → but at install only fuse3 is installed; no real
                    'fuse' package present → no actual conflict

        Filter to canonical names (where the key == the underlying
        Package's own 'Package' field).  Matches what dpkg actually
        sees at install time.
        """
        return {
            n for n, p in tree.selected_pkgs.items()
            if n == p['Package']
        }

    def _resolve_live_cohort(self) -> Optional[frozenset]:
        """The set of pkgs that get dpkg-installed in the live chroot
        simultaneously — the scope within which Conflicts/Breaks are
        hard violations.

        = dep_tree.selected_pkgs (canonical names only)
          − pool_extras_pkg_names   (pool-only; not auto-installed)
          − installer_exclusive_pkg_names  (installer-support debs not
            in the live closure)

        Returns None when dep_tree isn't populated (operator hasn't
        run `cache parse`).  Caller should fall back to a coarser check
        or print a hint.
        """
        if not self.dep_tree or not self.dep_tree.selected_pkgs:
            return None
        _selected = self._canonical_names(self.dep_tree)
        _selected -= getattr(self.dep_tree, 'pool_extras_pkg_names', set())
        _selected -= getattr(self.dep_tree, 'installer_exclusive_pkg_names',
                              set())
        return frozenset(_selected)

    def _predicted_files_for_source(self, src_name: str) -> 'list[str]':
        """Union of deb-tree + udeb-tree predicted binary filenames for
        this source.  Order: deb entries first, udeb appended.

        Used by every reader that needs "what binaries will this source
        produce in the repo after a successful build?" — check_build,
        cmd_source_audit, cmd_source_repair, cmd_audit's content-
        integrity section, _do_tunnel.  Pulled here because the two
        trees store per-tree maps (src_pkg_files); Source objects no
        longer carry a .pkgs attribute (it leaked across trees — see
        dependencytree.py:src_pkg_files docstring for full history).
        """
        _files: 'list[str]' = []
        if self.dep_tree is not None:
            _files.extend(self.dep_tree.src_pkg_files.get(src_name) or [])
        if self.udeb_dep_tree is not None:
            for _f in (self.udeb_dep_tree.src_pkg_files.get(src_name) or []):
                if _f not in _files:
                    _files.append(_f)
        return _files

    def _tunnel_filenames_for_source(self, src_name: str) -> 'list[str]':
        """Same shape as _predicted_files_for_source but returns the ACTUAL
        upstream binary filenames (Filename: from the cached Packages record)
        instead of strip_nmu pristine names.

        Tunneled packages are pristine Debian passthrough — the on-disk file
        keeps its upstream NMU/security suffix (e.g. ~deb12u1) and the .deb's
        internal control Version matches that filename.  Snapshot.debian.org
        serves the file at that suffixed path; the pristine-stripped URL would
        404 (or worse, coincidentally hit a same-named unstable binary — wrong
        version, silent corruption, caught 2026-05-28 when amd64-microcode
        downloaded the unsuffixed unstable build instead of the bookworm-
        security ~deb12u1 build).

        For each predicted binary name, look up the binary in the dep tree's
        selected_pkgs (cache Package record) and return os.path.basename of
        its Filename.  Falls back to the predicted name if the binary isn't
        resolvable — caller surfaces the resulting fetch failure.
        """
        _predicted = self._predicted_files_for_source(src_name)
        _actual: 'list[str]' = []
        for _f in _predicted:
            _bin_name = _f.split('_', 1)[0]
            _pkg = None
            if self.dep_tree is not None:
                _pkg = self.dep_tree.selected_pkgs.get(_bin_name)
            if _pkg is None and self.udeb_dep_tree is not None:
                _pkg = self.udeb_dep_tree.selected_pkgs.get(_bin_name)
            if _pkg is None:
                logger.warning(
                    f"tunnel: no cached binary {_bin_name!r} for source "
                    f"{src_name!r} — falling back to predicted {_f}")
                _actual.append(_f)
                continue
            _fn = (_pkg.get('Filename') or '').rsplit('/', 1)[-1]
            _actual.append(_fn or _f)
        return _actual

    def _resolve_deb_cohort(self) -> Optional[frozenset]:
        """Consumers audited as the .deb-cohort by package_audit's
        DEP-GATE.  = dep_tree.selected_pkgs canonical names (everything
        we install via debootstrap, tasksel/apt at install time, or
        live-chroot batch).

        Excludes the udeb tree.  Audited separately so each cohort's
        unresolved surface is visible — the old combined audit hid
        per-cohort breakdowns and made it hard to tell which gap came
        from which install path.

        Canonical-name filter: see _canonical_names docstring for why
        virtual-alias keys must be excluded from scope sets.
        """
        if not self.dep_tree or not self.dep_tree.selected_pkgs:
            return None
        return frozenset(self._canonical_names(self.dep_tree))

    def _resolve_udeb_cohort(self) -> Optional[frozenset]:
        """Consumers audited as the .udeb-cohort by package_audit's
        DEP-GATE.  = udeb_dep_tree.selected_pkgs canonical names
        (everything dpkg-unpacked into the d-i installer ramdisk).

        Resolution still spans the whole repo per Option B — udebs
        with deb deps (~9 known upstream metadata cases like
        at-spi2-core-udeb → libsystemd0, libgtk-4-1-udeb → libtiff6)
        get resolved against deb providers, matching d-i's runtime
        behaviour where the deb gets debootstrapped onto /target.
        """
        if (not self.udeb_dep_tree
                or not self.udeb_dep_tree.selected_pkgs):
            return None
        return frozenset(self._canonical_names(self.udeb_dep_tree))

    def _resolve_install_corpus(self) -> Optional[frozenset]:
        """[pkg + installer + live + pool] — your hard-dep gate scope.

        = dep_tree.selected_pkgs ∪ udeb_dep_tree.selected_pkgs
          (canonical names from each tree)

        Every pkg in this union ends up dpkg-installed somewhere — in
        the live chroot, the d-i ramdisk, or the target via tasksel +
        apt at install time.  Their Depends are install-time hard
        constraints.

        Pkgs OUTSIDE this union are side artifacts of dpkg-buildpackage
        (libfoo-dev / -doc / -tests / -dbgsym from sources we built
        but didn't select).  Their Depends never resolve at runtime
        because they never install.

        Returns None when dep_tree isn't populated (operator hasn't
        run `cache parse`); caller falls back to whole-repo audit with
        a hint.
        """
        if not self.dep_tree or not self.dep_tree.selected_pkgs:
            return None
        _all = self._canonical_names(self.dep_tree)
        if self.udeb_dep_tree and self.udeb_dep_tree.selected_pkgs:
            _all |= self._canonical_names(self.udeb_dep_tree)
        return frozenset(_all)

    def _resolve_installer_cohort(self) -> Optional[frozenset]:
        """The set of pkgs that get dpkg-unpacked into the d-i installer
        ramdisk — the scope for installer conflict checks.

        = udeb_dep_tree.selected_pkgs (canonical names)

        Pool / live / pkg debs are NOT in this scope (the ramdisk is
        udeb-only; debs are pulled by the installer onto the target
        system, which is a separate install scenario).
        """
        if (not self.udeb_dep_tree
                or not self.udeb_dep_tree.selected_pkgs):
            return None
        return frozenset(self._canonical_names(self.udeb_dep_tree))

    def _preflight_audit_source(self) -> bool:
        """Source-side audit gate for `chroot build live/installer`.

        Walks `_source_state` over the merged deb+udeb dep tree.
        Aborts (with operator y/n prompt) when any of these states
        are non-empty for selected sources:

          needs_build  — binaries missing or invalid (legitimate rebuild)
          needs_sync   — source files missing or .verified absent
          stale_pass   — .result=PASS but state drifted (patches changed
                          since the build that wrote .result)
          repairable   — binaries valid but .result missing
                          (BUT: see below — repairable is treated as
                          soft, not hard)

        repairable is SOFT (operator should `source repair` but the
        chroot build can still succeed — apt + dpkg don't read
        .result; .result is build-pipeline metadata).  Hard fails
        are needs_build / needs_sync / stale_pass which would either
        miss .debs entirely or include stale ones.

        Returns True to proceed, False to abort.  Cheap (~5s) — each
        per-source classify is a few stat() calls.

        If dep_tree isn't built (operator hit chroot build before
        cache parse), gate is skipped with a hint — the source-build
        flag guard above already catches that miss-ordering.
        """
        if not (self.dep_tree and self.dep_tree.selected_srcs):
            return True
        if self.container is None:
            # _source_state needs container.is_ar_file
            return True

        assert self.dep_tree is not None
        _srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _n, _s in self.udeb_dep_tree.selected_srcs.items():
                if _n not in _srcs:
                    _srcs[_n] = _s

        _hard = ('needs_build', 'needs_sync', 'stale_pass')
        _soft = ('repairable',)
        _by_state: 'dict[str, list[str]]' = {
            _s: [] for _s in _hard + _soft
        }
        for _name, _src in _srcs.items():
            _state = self._source_state(_name, _src)
            if _state in _by_state:
                _by_state[_state].append(_name)

        _hard_count = sum(len(_by_state[_s]) for _s in _hard)
        _soft_count = sum(len(_by_state[_s]) for _s in _soft)

        if _hard_count == 0 and _soft_count == 0:
            console.print(
                f"Source audit OK: {len(_srcs)} sources synced, "
                f"built, fresh."
            )
            return True

        console.print("Source audit found build-state issues:")
        for _state in _hard + _soft:
            _names = _by_state[_state]
            if not _names:
                continue
            _marker = '(hard)' if _state in _hard else '(soft)'
            console.print(
                f"  {len(_names):5d}  {_state}  {_marker}"
            )
        for _state in _hard + _soft:
            _names = sorted(_by_state[_state])
            if not _names:
                continue
            self._print_wrapped_names(f"    {_state}", _names)
        console.print(
            "  Fix: `source build` (rebuilds), `source repair` "
            "(aligns .result), `source sync` (re-fetches)."
        )

        if _hard_count == 0:
            # Only soft findings — proceed silently.  Audit reports
            # the count above; no prompt needed.
            return True
        _resp = Prompt(
            PROMPT_YESNO,
            "Proceed with chroot build despite source audit "
            "findings?  (hard: rebuild recommended)",
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

        _bad = (
            len(_unresolved) + len(_live_conflicts) + len(_inst_conflicts)
        )
        if _bad == 0:
            console.print(
                f"Repo audit OK: {len(_state.packages)} pkgs, "
                f"hard-dep closure clean, no install-cohort conflicts."
            )
            return True
        console.print(
            f"Repo audit found install-time risks:\n"
            f"  UNRESOLVED Depends/Pre-Depends (whole repo): "
            f"{len(_unresolved)}\n"
            f"  CONFLICTS in LIVE cohort:                    "
            f"{len(_live_conflicts)}\n"
            f"  CONFLICTS in INSTALLER ramdisk cohort:       "
            f"{len(_inst_conflicts)}"
        )
        _show = min(10, len(_unresolved))
        if _show:
            console.print(f"\nFirst {_show} UNRESOLVED:")
            for _pkg, _field, _rel, _why in _unresolved[:_show]:
                console.print(f"  {_pkg}  {_field}: {_rel}")
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

        if _deb_cohort is None and _udeb_cohort is None:
            # Whole-repo fallback path.
            console.print(
                f"\n=== DEP GATE (whole repo, "
                f"{len(_state.packages)} pkgs) ==="
            )
            console.print(
                f"  UNRESOLVED Depends/Pre-Depends: {len(_unresolved)}"
                + (f"\n  WEAK (Recommends unresolved):   {len(_weak)}"
                   if _strict else '')
            )
            self._report_unresolved(_unresolved, _weak, _state,
                                    verbose=_verbose, strict=_strict)
        else:
            for _label, _consumer_set, _u, _w in _per_cohort:
                console.print(
                    f"\n=== DEP GATE ({_label} cohort, "
                    f"{len(_consumer_set)} consumers; "
                    f"resolution = whole repo) ==="
                )
                console.print(
                    f"  UNRESOLVED Depends/Pre-Depends: {len(_u)}"
                    + (f"\n  WEAK (Recommends unresolved):   {len(_w)}"
                       if _strict else '')
                )
                self._report_unresolved(_u, _w, _state,
                                        verbose=_verbose, strict=_strict)

        if _live is None:
            console.print(
                "\n=== LIVE CONFLICTS ===\n  skipped — dep_tree not built; "
                "run `cache parse` first"
            )
        else:
            _live_conflicts = _dedupe_bidirectional_conflicts(
                repo_audit.audit_conflict_cohort(_state, _live)
            )
            console.print(
                f"\n=== LIVE CONFLICTS (cohort = "
                f"{len(_live)} pkgs in live chroot) ===\n"
                f"  CONFLICTS: {len(_live_conflicts)}"
            )
            self._report_conflicts(_live_conflicts, verbose=_verbose)

        if _installer is None:
            console.print(
                "\n=== INSTALLER CONFLICTS ===\n  skipped — udeb_dep_tree "
                "not built; run `cache parse` first"
            )
        else:
            _inst_conflicts = _dedupe_bidirectional_conflicts(
                repo_audit.audit_conflict_cohort(_state, _installer)
            )
            console.print(
                f"\n=== INSTALLER CONFLICTS (cohort = "
                f"{len(_installer)} udebs in d-i ramdisk) ===\n"
                f"  CONFLICTS: {len(_inst_conflicts)}"
            )
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
            console.print(
                "\n=== STALE FILES ===\n  skipped — dep_tree not built; "
                "run `cache parse` first"
            )

        # ── CONTENT INTEGRITY (was `source verify`) ─────────────────
        # Per-cohort deep walk: open each predicted .deb/.udeb with
        # DebFile, verify internal Package/Version/Arch matches the
        # filename + every hard Depends resolves against the cohort's
        # RepoState.  Slow (~30-40s on ~900 sources) — skip via `quick`.
        if _quick:
            console.print(
                "\n=== CONTENT INTEGRITY ===\n  skipped (`quick` flag)"
            )
        elif not self.flags.build_container_ready:
            console.print(
                "\n=== CONTENT INTEGRITY ===\n  skipped — container init "
                "not run; `verify_pkg_artifact` lives on BuildContainer"
            )
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
        `cmd_source_verify` (deleted in P3 2026-05-23).  Resolution
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
                console.print(
                    f"\n=== CONTENT INTEGRITY ({_label} cohort) ===\n"
                    f"  skipped — repo state scan failed",
                    tui.COLOR_ERROR,
                )
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
                    _result_file = os.path.join(
                        self.container.buildlog_path, _name + '.result')
                    try:
                        with open(_result_file) as fh:
                            if fh.readline().strip() == 'TUNNELED':
                                _skipped_tunneled += 1
                                continue
                    except OSError:
                        pass
                    _any_missing = False
                    _failing = None
                    for _f in _expected:
                        # The predicted filename `_f` is the PRISTINE name; the
                        # on-disk artifact may carry a +asg<R>u<N> stamp.
                        # find_matching_artifact accepts either, and we verify
                        # the ACTUAL file under its ACTUAL name so the internal
                        # control-version-vs-filename check matches.
                        _path = utils.find_matching_artifact(
                            self.config.deb_dest_for_filename(_f), _f)
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

            console.print(
                f"\n=== CONTENT INTEGRITY ({_label} cohort) ==="
            )
            console.print(
                f"  {_ok:5d}  pass (internal control matches filename + "
                f"deps resolve)"
            )
            console.print(
                f"  {len(_failed):5d}  FAIL — present in repo/ but "
                f"verify rejected"
            )
            if _skipped_tunneled:
                console.print(
                    f"  {_skipped_tunneled:5d}  skipped (TUNNELED)"
                )
            if _skipped_missing:
                console.print(
                    f"  {_skipped_missing:5d}  skipped (binaries missing "
                    f"— `source build` / `source repair`)"
                )
            if _no_pkgs:
                console.print(
                    f"  {_no_pkgs:5d}  no {_ext} binaries declared"
                )
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
        `cmd_audit_nmu` (deleted in P3 2026-05-23).  Catches any .deb
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
        console.print("\n=== NMU RESIDUE ===")
        if not _findings:
            console.print(
                f"  clean ({_scanned} pkgs scanned, "
                f"no +bN / +debNuN / ~bpoN+N residue){_tail}"
            )
            return
        from collections import Counter
        _by_field = Counter(f[1] for f in _findings)
        _pkgs_with_residue = sorted({f[0] for f in _findings})
        console.print(
            f"  {len(_findings):5d} finding(s) across "
            f"{len(_pkgs_with_residue)} pkg(s){_tail}:"
        )
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
        _orphan, _drift, _malformed, _total = self._scan_stale_files()
        _n_stale = len(_orphan) + len(_drift)
        console.print(
            f"\n=== STALE FILES (repo/ scan, {_total} file(s)) ==="
        )
        if _n_stale == 0 and not _malformed:
            console.print("  repo/ is clean — no orphan-source or drift residue")
            return
        _bytes = (sum(s for *_, s in _orphan)
                  + sum(s for *_, s in _drift))
        console.print(
            f"  orphan-source : {len(_orphan)} file(s) "
            f"(source not in selected_srcs)"
        )
        console.print(
            f"  version-drift : {len(_drift)} file(s) "
            f"(source selected but version mismatch)"
        )
        if _malformed:
            console.print(
                f"  malformed     : {len(_malformed)} file(s) "
                f"(can't read control)"
            )
        if _n_stale:
            console.print(
                f"  TOTAL STALE   : {_n_stale} file(s), "
                f"{_bytes / 1024 / 1024:.1f} MB"
            )
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
            console.print(f"  First {_show} UNRESOLVED:")
            for _pkg, _field, _rel, _why in unresolved[:_show]:
                console.print(f"    {_pkg}  {_field}: {_rel}  — {_why}")
        if strict:
            _show = len(weak) if verbose else min(30, len(weak))
            if _show:
                console.print(f"  First {_show} WEAK Recommends:")
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
            console.print(f"  First {_show}:")
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

        console.print(
            f"\nGap classification: {len(_consumers_by_target)} distinct "
            f"missing targets across {len(unresolved)} unresolved refs."
        )
        console.print(
            f"  build_failed    : {len(_build_failed):4d} targets "
            f"({_ref_count(_build_failed)} refs) — in dep_tree, not in repo"
        )
        console.print(
            f"  missed_by_parse : {len(_missed_by_parse):4d} targets "
            f"({_ref_count(_missed_by_parse)} refs) — in upstream, not in dep_tree"
        )
        console.print(
            f"  transitional    : {len(_transitional):4d} targets "
            f"({_ref_count(_transitional)} refs) — not in upstream cache"
        )
        console.print(
            f"  other           : {len(_other):4d} targets "
            f"({_ref_count(_other)} refs) — likely version-skew"
        )

        def _show_category(name: str, targets: list, n: int = 30):
            if not targets:
                return
            _ranked = sorted(
                targets,
                key=lambda t: (-len(_consumers_by_target[t]), t),
            )
            _limit = len(_ranked) if verbose else min(n, len(_ranked))
            console.print(f"\n== {name} (top {_limit} by ref count) ==")
            for _t in _ranked[:_limit]:
                _consumers = _consumers_by_target[_t]
                _sample = ', '.join(sorted(set(_consumers))[:3])
                if len(set(_consumers)) > 3:
                    _sample += f', … (+{len(set(_consumers)) - 3})'
                console.print(
                    f"  {len(_consumers):4d}× {_t:42s} ← {_sample}"
                )

        _show_category('build_failed', _build_failed)
        _show_category('missed_by_parse', _missed_by_parse)
        _show_category('transitional', _transitional)
        _show_category('other (likely version skew)', _other)

        console.print(
            "\nNext steps by category:\n"
            "  build_failed     → check log/build/<name>.result; rebuild\n"
            "  missed_by_parse  → add target to pkg.list / live.list / pool.list\n"
            "  transitional     → update consumer pkg (upstream dropped target)\n"
            "  other            → `repo audit_nmu` first; then drill in"
            " with `repo audit <target>`"
        )

    def _audit_gap_drill_in(self, state, unresolved, target: str) -> None:
        """Per-target diagnostic for `repo audit_gap <name>`.

        Surfaces enough state to root-cause why `<name>` is unresolved:
          - in upstream cache?  (with versions if so)
          - in our dep_tree.selected_pkgs?
          - in our repo state? (with version)
          - virtually provided in repo? (with provider + version)
          - which consumers reference it (with exact constraint)
        """
        console.print(f"\n=== Gap drill-in: {target} ===")
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
            return

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
                return
            console.print(
                f"apt-repo indexed: {self.config.dir_repo}/dists/"
                f"{{{_codename},{_codename}-debug}}/",
                tui.COLOR_HIGHLIGHT,
            )
            self._print_repo_index_summary(_codename, _suites_spec)
        finally:
            _password = '*' * len(_password)  # noqa: F841

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
        console.print("\n=== repo index summary ===")
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
        identical and the one shared remote-scan index (`repo publish`) handles
        both with no clobber.

        Minimal = the main-component binary .debs a booted system apt-installs,
        MINUS debug/source (apt_repo.deb_excluded_from_minimal); no udebs,
        source, or debug suite.  NO index/sign here — `repo publish` rebuilds
        the index ON THE REMOTE (dpkg-scanpackages on the VM).

          publish/dists/<codename>/main/binary-<arch>/<subset>.deb

        Returns True on success.  Called standalone or by `repo publish ssh
        minimal`.
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

    def _print_publish_size_summary(self) -> None:
        """Summarise publish/ (the local staging dir for `repo index minimal`).
        File count + total size — useful for a quick "what am I about to
        ship?" before invoking `repo publish ssh|local minimal`."""
        _root = self.config.dir_publish
        _total = 0
        _n = 0
        for _dp, _dirs, _files in os.walk(_root):
            for _f in _files:
                _p = os.path.join(_dp, _f)
                if not os.path.isfile(_p):
                    continue
                _total += os.path.getsize(_p)
                _n += 1
        console.print(
            f"publish/: {_n} file(s), {_total // 2 ** 20} MB total",
            tui.COLOR_HIGHLIGHT,
        )

    def cmd_audit_external(self, *args):
        """COMP-02: audit the EXTERNALLY-published repo at [Repo]
        AptSourceURL — what apt clients actually fetch.  Read-only, HTTP.

        Checks, in order (stops at the first hard failure):
          1. dists/<codename>/InRelease is reachable.
          2. Its inline signature verifies against our signing key
             (signing.signing_pubkey_path) — same trust chain apt uses.
          3. Every index in the now-trusted SHA256 block is present and
             its size + SHA256 match.  (No pool .deb downloads.)

        `repo audit external ssh` instead RECONCILES the local signed manifest
        against the remote Packages and reports discrepancies (UPD-01).

        Requires AptSourceURL set and a generated signing key.
        """
        if args and args[0] == 'ssh':
            return self._audit_external_vs_manifest()
        del args
        import tempfile
        import signing
        from debian.deb822 import Release

        _base = self.config.apt_source_url.rstrip('/')
        if not _base:
            console.print(
                "repo audit external: [Repo] AptSourceURL is unset — set it "
                "to the published repo URL first")
            return
        _cn = self.config.build_codename.strip('"').strip("'")
        _keyring = signing.signing_pubkey_path(self.config)
        if not os.path.exists(_keyring):
            console.print(
                f"repo audit external: signing pubkey {_keyring} missing — "
                f"run `key generate` first")
            return

        console.print(f"repo audit external: {_base} (suite {_cn})")
        # TemporaryDirectory auto-cleans on exit — keeps this read-only-named
        # command free of explicit delete primitives (per the read-only guard).
        with tempfile.TemporaryDirectory(prefix='athena-audit-ext-') as _tmp:
            _gnupg = os.path.join(_tmp, 'gnupg')
            os.makedirs(_gnupg)
            os.chmod(_gnupg, 0o700)   # gpg refuses a homedir looser than 0700

            # 1. reachable
            _inrel_url = f'{_base}/dists/{_cn}/InRelease'
            _inrel = os.path.join(_tmp, 'InRelease')
            _size, _detail = utils.download_file(_inrel_url, _inrel)
            if _size <= 0:
                console.print(
                    f"  [x] unreachable: {_inrel_url} "
                    f"({_detail or 'no response'})", tui.COLOR_ERROR)
                return
            console.print(f"  [✓] reachable: {_inrel_url}")

            # 2. signature verifies against our key
            _ok, _vdetail = utils.verify_inrelease(_inrel, _keyring, _gnupg)
            if not _ok:
                console.print(
                    f"  [x] signature did NOT verify against "
                    f"{os.path.basename(_keyring)}: {_vdetail}", tui.COLOR_ERROR)
                return
            console.print(f"  [✓] signed: {_vdetail}")

            # 3. every index present + size/SHA256 match the signed Release.
            # Each index downloads to a unique temp name (no in-place reuse,
            # so no os.remove needed here).
            with open(_inrel) as _fh:
                _rel = Release(_fh)
            _entries = _rel.get('SHA256', [])
            if not _entries:
                console.print(
                    "  [x] no SHA256 block in InRelease", tui.COLOR_ERROR)
                return
            _bad = 0
            for _i, _e in enumerate(_entries):
                _name, _exp_sha, _exp_size = (
                    _e['name'], _e['sha256'], int(_e['size']))
                _idx = os.path.join(_tmp, f'idx-{_i}')
                _sz, _ = utils.download_file(f'{_base}/dists/{_cn}/{_name}', _idx)
                if _sz <= 0:
                    console.print(f"  [x] MISSING  {_name}", tui.COLOR_ERROR)
                    _bad += 1
                    continue
                if (utils.get_sha256(_idx) != _exp_sha
                        or os.path.getsize(_idx) != _exp_size):
                    console.print(
                        f"  [x] MISMATCH {_name}", tui.COLOR_ERROR)
                    _bad += 1
                else:
                    console.print(f"  [✓] {_name}")

            if _bad:
                console.print(
                    f"repo audit external: FAIL — {_bad} of {len(_entries)} "
                    f"index file(s) missing or mismatched", tui.COLOR_ERROR)
            else:
                console.print(
                    f"repo audit external: OK — signed + all {len(_entries)} "
                    f"index files consistent", tui.COLOR_HIGHLIGHT)

    def _audit_external_vs_manifest(self):
        """READ-ONLY: reconcile the local signed manifest against the remote
        Packages, reporting (pkg=version) entries present in one but not the
        other (UPD-01 `repo audit external ssh`)."""
        _local = repo_audit.published_ledger(self.config)
        _remote = repo_audit.fetch_remote_ledger(self.config)
        if _remote is None:
            console.print(
                "repo audit external ssh: remote unreachable (check [Repo] "
                "AptSourceURL / network).", tui.COLOR_ERROR)
            return
        _only_local, _only_remote = repo_audit.manifest_vs_remote_discrepancies(
            _local, _remote)
        if not _only_local and not _only_remote:
            console.print(
                "repo audit external ssh: OK — local manifest matches the "
                "remote.", tui.COLOR_HIGHLIGHT)
            return
        console.print(
            f"repo audit external ssh: DISCREPANCY — {len(_only_local)} "
            f"version(s) in the local manifest NOT on the remote; "
            f"{len(_only_remote)} on the remote NOT in the manifest.",
            tui.COLOR_WARNING)
        for _pv in sorted(_only_local)[:30]:
            console.print(f"    manifest-only: {_pv}")
        for _pv in sorted(_only_remote)[:30]:
            console.print(f"    remote-only:   {_pv}")

    def cmd_repo_publish(self, *args):
        """UPD-02 + COMP-02: publish the repo, then rebuild the index at the
        destination.

        Usage:
          repo publish ssh [full|minimal]              (default scope: full)
          repo publish local <path> [full|minimal]     (default scope: full)

          full    — push the whole local pool's .debs.
          minimal — push only the runtime subset (staged nested via
                    cmd_index_repo_minimal; no debug/source/udeb).

        ssh: rsync only the .debs up (ADDITIVE + `--ignore-existing` — .debs
        are immutable per filename, so a re-publish never re-uploads an
        existing one), run `dpkg-scanpackages` ON THE VM to rebuild the
        index from what's ACTUALLY there (sign LOCALLY), upload the
        regenerated metadata, and refresh the local manifest.  The remote
        needs `dpkg-dev` installed; its host fingerprint must be in
        known_hosts (publish is non-interactive).

        local: same flow but the destination is a local filesystem path
        (USB stick, NFS share, web-server docroot, etc.) — rsync runs
        local-to-local (no `-e`), and `dpkg-scanpackages` runs in-process
        instead of via ssh.  Missing target prompts to mkdir -p.

        Because the index is rebuilt from what's at the destination, full
        and minimal never clobber each other — the published `Packages` is
        always the union present at the destination.  full then prunes
        local + records published = current.

        External repo DISABLED (`repo external disable`): full only — index
        locally into the signed manifest (the +asg bump authority), no
        transport.  Both `ssh` and `local` transports respect this gate.
        """
        _kind = args[0] if len(args) > 0 else ''
        if _kind == 'local':
            _local_path = args[1] if len(args) > 1 else ''
            _scope = args[2] if len(args) > 2 else 'full'
            if not _local_path or _scope not in ('full', 'minimal'):
                console.print("Usage: repo publish local <path> [full|minimal]")
                return
        elif _kind == 'ssh':
            _scope = args[1] if len(args) > 1 else 'full'
            _local_path = ''
            if _scope not in ('full', 'minimal'):
                console.print("Usage: repo publish ssh [full|minimal]")
                return
        else:
            console.print("Usage: repo publish ssh [full|minimal]")
            console.print("       repo publish local <path> [full|minimal]")
            return
        _external = self._external_enabled()

        # ── External DISABLED → local-only (full): index into the manifest. ──
        if not _external:
            if _scope != 'full':
                console.print(
                    "repo publish: external disabled — only `full` is supported "
                    "local-only (the manifest mirrors the complete local pool).",
                    tui.COLOR_WARNING)
                return
            if not self._refresh_merge_index():
                console.print("repo publish: local index/merge failed.",
                              tui.COLOR_ERROR)
                return
            console.print("repo publish: external DISABLED — local manifest "
                          "refreshed, nothing rsynced.", tui.COLOR_INFO)
            if self.flags.dep_check_ready:
                self.cmd_package_cleanup('force')
            utils.write_snapshot_state(
                self.config, published=self._snapshot_current())
            return

        # ── External ENABLED. Stage the deb source tree + suites to index. ──
        _deb_dists, _suites_spec = self._publish_stage_source(_scope)
        if _deb_dists is None or _suites_spec is None:
            return

        if _kind == 'ssh':
            self._publish_via_ssh(_scope, _deb_dists, _suites_spec)
        else:
            self._publish_via_local(_scope, _local_path, _deb_dists,
                                    _suites_spec)

    def _publish_stage_source(self, scope: str):
        """Stage the .deb source tree and the suites-spec the reindex will
        walk.  full uses dir_repo/dists (everything); minimal calls
        cmd_index_repo_minimal which lays out the runtime subset under
        dir_publish/dists.  Returns (deb_dists_path, suites_spec) or
        (None, None) on failure."""
        _codename = self.config.build_codename.strip('"').strip("'")
        if scope == 'minimal':
            if not self.cmd_index_repo_minimal():
                return None, None
            _deb_dists = os.path.join(self.config.dir_publish, 'dists')
            _suites_spec = {_codename: ['main']}
        else:
            _deb_dists = os.path.join(self.config.dir_repo, 'dists')
            _suites_spec = {_codename: ['main', 'doc', 'tests',
                                        'contrib', 'non-free',
                                        'non-free-firmware'],
                            f'{_codename}-debug': ['main']}
        if not os.path.isdir(_deb_dists):
            console.print(f"repo publish: {_deb_dists} missing — build first.",
                          tui.COLOR_ERROR)
            return None, None
        return _deb_dists, _suites_spec

    def _publish_finalize(self, scope: str, main_pkgs: str, what: str) -> None:
        """Shared tail of every transport's publish path: refresh the local
        signed manifest from the just-indexed main Packages, surface the
        success line, and (for full) publish-before-prune + record
        published = current.  `what` is the human-readable destination label
        for the success line (`ssh://...` or local path)."""
        _codename = self.config.build_codename.strip('"').strip("'")
        # STA-21: fail loud on signing failure (silent unsigned writes
        # would corrupt +asg uN bump derivation on the next publish).
        if not repo_audit.write_published_manifest(self.config, main_pkgs):
            console.print(
                "repo publish: local manifest sign FAILED — sync succeeded "
                "but the local +asg uN authority is now empty.  Run `key "
                "generate` / `key verify` then re-publish.",
                tui.COLOR_ERROR)
            return
        console.print(
            f"repo publish: synced {scope} + re-indexed at "
            f"{what}", tui.COLOR_HIGHLIGHT)
        if self.config.apt_source_url:
            console.print(
                f"  apt source: {self.config.apt_source_url} {_codename} main",
                tui.COLOR_INFO)
        if scope == 'full':
            console.print("repo publish: pruning local to single-snapshot…")
            if self.flags.dep_check_ready:
                self.cmd_package_cleanup('force')
            utils.write_snapshot_state(
                self.config, published=self._snapshot_current())
            console.print(
                f"repo publish: published is now {self._snapshot_current()}.",
                tui.COLOR_HIGHLIGHT)

    def _publish_via_ssh(self, scope: str, deb_dists: str,
                          suites_spec: dict) -> None:
        """UPD-02 ssh transport — rsync .debs to PublishSshTarget, reindex on
        the remote VM, upload metadata, refresh the local manifest."""
        _codename = self.config.build_codename.strip('"').strip("'")
        _target = self.config.publish_ssh_target
        if not _target:
            console.print(
                "repo publish: [Repo] PublishSshTarget is unset — set it, or "
                "`repo external disable` for local-only.", tui.COLOR_ERROR)
            return
        _dist_id = self.config.build_base_id
        if ':' in _target:
            _userhost, _basepath = _target.split(':', 1)
        else:
            _userhost, _basepath = _target, ''
        _root_path = (_basepath.rstrip('/') + '/' + _dist_id
                      if _basepath else _dist_id)
        _remote_root = f'{_userhost}:{_root_path}'
        _ssh = ['ssh']
        if self.config.publish_ssh_key:
            _ssh += ['-i', self.config.publish_ssh_key]
        _spin = Spinner(f"checking remote {_userhost}:{_root_path}")
        try:
            _rc = subprocess.run(
                _ssh + [_userhost, 'test', '-d', _root_path],
                capture_output=True, text=True).returncode
        finally:
            _spin.done()
        if _rc != 0:
            console.print(
                f"repo publish: remote dir '{_root_path}' not found on "
                f"{_userhost} — `ssh {_userhost} mkdir -p {_root_path}`.",
                tui.COLOR_ERROR)
            return

        # 1. Push only the .debs (immutable → --ignore-existing, deb-only).
        _debfilter = ['--include=*/', '--include=*.deb', '--include=*.udeb',
                      '--exclude=*']
        if self._rsync_streamed(
                f'rsync debs → {_remote_root}', deb_dists,
                f'{_remote_root}/dists/', _ssh,
                ignore_existing=True, filters=_debfilter) != 0:
            console.print("repo publish: .deb upload failed — see log.",
                          tui.COLOR_ERROR)
            return

        # 2. Rebuild the index ON THE REMOTE (scan there, sign locally).
        import signing
        import apt_repo
        _password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()
        if subprocess.run(['sudo', '-S', '-v'], input=_password + '\n',
                          capture_output=True, text=True).returncode != 0:
            console.print("ERROR: incorrect sudo password")
            return
        _staging = os.path.join(self.config.dir_temp, 'remote-reindex')
        if os.path.isdir(_staging):
            shutil.rmtree(_staging)
        os.makedirs(_staging, exist_ok=True)
        try:
            _main_pkgs = apt_repo.remote_reindex_and_sign(
                staging=_staging, ssh_cmd=_ssh, userhost=_userhost,
                remote_root=_root_path, suites_spec=suites_spec,
                codename_for_suite={_s: _s for _s in suites_spec},
                primary_suite=_codename, arch=self.config.arch,
                version=self.config.build_version.strip('"').strip("'"),
                password=_password,
                signing_homedir=signing.signing_home(self.config))
        finally:
            _password = '*' * len(_password)  # noqa: F841
        if _main_pkgs is None:
            console.print("repo publish: remote re-index failed — see log "
                          "(is dpkg-dev installed on the VM?).", tui.COLOR_ERROR)
            return

        # 3. Upload the freshly-generated metadata (staging holds metadata only).
        if self._rsync_streamed(
                f'rsync index → {_remote_root}',
                os.path.join(_staging, 'dists'),
                f'{_remote_root}/dists/', _ssh) != 0:
            console.print("repo publish: metadata upload failed — see log.",
                          tui.COLOR_ERROR)
            return

        self._publish_finalize(scope, _main_pkgs, _remote_root)

    def _publish_via_local(self, scope: str, local_path: str,
                            deb_dists: str, suites_spec: dict) -> None:
        """COMP-02 local transport — rsync .debs to a local destination
        (USB / NFS / web docroot / etc.), reindex in-process with
        dpkg-scanpackages, refresh the local manifest.  Twin of
        _publish_via_ssh but no `-e ssh`, no remote `test -d`, and no
        dpkg-dev-on-the-VM requirement (host's dpkg-dev does the scan)."""
        _codename = self.config.build_codename.strip('"').strip("'")
        _dest = os.path.abspath(os.path.expanduser(local_path))
        if not os.path.isdir(_dest):
            _resp = Prompt(
                PROMPT_YESNO,
                f"Create local publish target {_dest}?").get_response().lower()
            if _resp not in ('y', 'yes'):
                console.print("repo publish: target missing — aborted.",
                              tui.COLOR_ERROR)
                return
            try:
                os.makedirs(_dest, exist_ok=True)
            except OSError as e:
                console.print(
                    f"repo publish: cannot create {_dest}: {e}",
                    tui.COLOR_ERROR)
                return
        if not os.access(_dest, os.W_OK):
            console.print(
                f"repo publish: {_dest} is not writable.",
                tui.COLOR_ERROR)
            return

        # 1. Copy only the .debs (immutable → --ignore-existing, deb-only).
        _debfilter = ['--include=*/', '--include=*.deb', '--include=*.udeb',
                      '--exclude=*']
        if self._rsync_streamed(
                f'rsync debs → {_dest}', deb_dists,
                f'{_dest}/dists/', ssh_cmd=[],
                ignore_existing=True, filters=_debfilter) != 0:
            console.print("repo publish: .deb copy failed — see log.",
                          tui.COLOR_ERROR)
            return

        # 2. Rebuild the index from what's at the destination (in-process).
        import signing
        import apt_repo
        _password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()
        if subprocess.run(['sudo', '-S', '-v'], input=_password + '\n',
                          capture_output=True, text=True).returncode != 0:
            console.print("ERROR: incorrect sudo password")
            return
        _staging = os.path.join(self.config.dir_temp, 'local-reindex')
        if os.path.isdir(_staging):
            shutil.rmtree(_staging)
        os.makedirs(_staging, exist_ok=True)
        try:
            _main_pkgs = apt_repo.local_reindex_and_sign(
                staging=_staging, dest_root=_dest, suites_spec=suites_spec,
                codename_for_suite={_s: _s for _s in suites_spec},
                primary_suite=_codename, arch=self.config.arch,
                version=self.config.build_version.strip('"').strip("'"),
                password=_password,
                signing_homedir=signing.signing_home(self.config))
        finally:
            _password = '*' * len(_password)  # noqa: F841
        if _main_pkgs is None:
            console.print("repo publish: local re-index failed — see log.",
                          tui.COLOR_ERROR)
            return

        # 3. Copy the freshly-generated metadata (staging → dest).
        if self._rsync_streamed(
                f'rsync index → {_dest}',
                os.path.join(_staging, 'dists'),
                f'{_dest}/dists/', ssh_cmd=[]) != 0:
            console.print("repo publish: metadata copy failed — see log.",
                          tui.COLOR_ERROR)
            return

        self._publish_finalize(scope, _main_pkgs, _dest)

    def cmd_repo_summary(self, *args):
        """COMP-02: print a summary of the published repo at the destination.

        Usage:
          repo summary ssh                 (against [Repo] PublishSshTarget)
          repo summary local <path>        (against a local filesystem path)

        Reports:
          - destination + suites discovered there
          - file count + total bytes under <dest>/dists/
          - per-suite InRelease signature (verifies against our pubkey) +
            its `Date:` field
          - snapshot pin state (base / published / current) from
            config/snapshot.state
          - local signed manifest tally (binary versions / source packages
            tracked — = the +asg uN bump authority)

        Read-only — never mutates the destination."""
        _kind = args[0] if args else ''
        if _kind == 'ssh':
            return self._summary_via_ssh()
        if _kind == 'local':
            _path = args[1] if len(args) > 1 else ''
            if not _path:
                console.print("Usage: repo summary local <path>")
                return
            return self._summary_via_local(_path)
        console.print("Usage: repo summary ssh")
        console.print("       repo summary local <path>")

    def _summary_via_ssh(self) -> None:
        """COMP-02 summary via the ssh transport — ssh out to the configured
        PublishSshTarget and inspect dists/ in place (no fetch of pool .debs;
        just stat + cat InRelease)."""
        _target = self.config.publish_ssh_target
        if not _target:
            console.print(
                "repo summary: [Repo] PublishSshTarget is unset.",
                tui.COLOR_ERROR)
            return
        _dist_id = self.config.build_base_id
        if ':' in _target:
            _userhost, _basepath = _target.split(':', 1)
        else:
            _userhost, _basepath = _target, ''
        _root_path = (_basepath.rstrip('/') + '/' + _dist_id
                      if _basepath else _dist_id)
        _ssh = ['ssh']
        if self.config.publish_ssh_key:
            _ssh += ['-i', self.config.publish_ssh_key]

        def _has_inrelease(suite: str) -> bool:
            _r = subprocess.run(
                _ssh + [_userhost, 'test', '-f',
                        f'{_root_path}/dists/{suite}/InRelease'],
                capture_output=True, text=True)
            return _r.returncode == 0

        def _read_inrelease(suite: str):
            _r = subprocess.run(
                _ssh + [_userhost, 'cat',
                        f'{_root_path}/dists/{suite}/InRelease'],
                capture_output=True, text=True)
            return _r.stdout if _r.returncode == 0 else None

        def _walk():
            _r = subprocess.run(
                _ssh + [_userhost,
                        f'find {_root_path}/dists -type f '
                        f'-printf "%s\\n" 2>/dev/null'],
                capture_output=True, text=True)
            _sizes = [int(_x) for _x in _r.stdout.split() if _x.isdigit()]
            return len(_sizes), sum(_sizes)

        self._summarize_destination(
            f'ssh://{_userhost}:{_root_path}',
            _has_inrelease, _read_inrelease, _walk)

    def _summary_via_local(self, local_path: str) -> None:
        """COMP-02 summary via the local transport — stat the destination
        directly."""
        _dest = os.path.abspath(os.path.expanduser(local_path))
        if not os.path.isdir(_dest):
            console.print(
                f"repo summary: {_dest} not found.", tui.COLOR_ERROR)
            return

        def _has_inrelease(suite: str) -> bool:
            return os.path.isfile(
                os.path.join(_dest, 'dists', suite, 'InRelease'))

        def _read_inrelease(suite: str):
            _p = os.path.join(_dest, 'dists', suite, 'InRelease')
            try:
                with open(_p) as _fh:
                    return _fh.read()
            except OSError:
                return None

        def _walk():
            _n, _bytes = 0, 0
            _dists_root = os.path.join(_dest, 'dists')
            if os.path.isdir(_dists_root):
                for _dp, _dirs, _files in os.walk(_dists_root):
                    for _f in _files:
                        _p = os.path.join(_dp, _f)
                        if os.path.isfile(_p):
                            _n += 1
                            _bytes += os.path.getsize(_p)
            return _n, _bytes

        self._summarize_destination(
            _dest, _has_inrelease, _read_inrelease, _walk)

    def _summarize_destination(self, label: str, has_inrelease,
                                read_inrelease, walk_files) -> None:
        """Shared body of `_summary_via_{ssh,local}`.  Transport-specific bits
        are the three callables:
          has_inrelease(suite) → bool       — probe for InRelease at the dest
          read_inrelease(suite) → str|None  — read its content (or None)
          walk_files() → (n, bytes)         — count + sum of files under dists/

        Everything else (signature verify, snapshot pin readout, manifest
        tally) is destination-agnostic."""
        import signing
        import tempfile
        _codename = self.config.build_codename.strip('"').strip("'")
        console.print(f"repo summary @ {label}", tui.COLOR_HIGHLIGHT)

        # 1. file walk under dists/
        _n, _bytes = walk_files()
        console.print(
            f"  Files            : {_n:,} file(s)  "
            f"{_bytes // (2 ** 20):,} MB")

        # 2. per-suite InRelease signature + date
        _keyring = signing.signing_pubkey_path(self.config)
        _have_key = os.path.exists(_keyring)
        for _suite in (_codename, f'{_codename}-debug'):
            if not has_inrelease(_suite):
                continue
            _text = read_inrelease(_suite)
            if _text is None:
                console.print(
                    f"  InRelease ({_suite}) : (could not read)",
                    tui.COLOR_WARNING)
                continue
            _date = ''
            for _line in _text.splitlines():
                if _line.startswith('Date:'):
                    _date = _line.split(':', 1)[1].strip()
                    break
            if not _have_key:
                console.print(
                    f"  InRelease ({_suite}) : (no pubkey at {_keyring} — "
                    f"`key generate` first)", tui.COLOR_WARNING)
            else:
                with tempfile.TemporaryDirectory(
                        prefix='athena-summary-') as _tmp:
                    _gnupg = os.path.join(_tmp, 'gnupg')
                    os.makedirs(_gnupg)
                    os.chmod(_gnupg, 0o700)
                    _inrel = os.path.join(_tmp, 'InRelease')
                    with open(_inrel, 'w') as _fh:
                        _fh.write(_text)
                    _ok, _detail = utils.verify_inrelease(
                        _inrel, _keyring, _gnupg)
                if _ok:
                    console.print(
                        f"  InRelease ({_suite}) : [✓] {_detail}")
                else:
                    console.print(
                        f"  InRelease ({_suite}) : [x] {_detail}",
                        tui.COLOR_ERROR)
            if _date:
                console.print(f"                     dated {_date}")

        # 3. snapshot pin state
        _state = utils.read_snapshot_state(self.config)
        console.print(
            f"  Snapshot pins    : base      {_state.get('base', '(unset)')}")
        console.print(
            f"                     published {_state.get('published', '(unset)')}")
        console.print(
            f"                     current   {_state.get('current', '(unset)')}")

        # 4. local signed manifest tally
        _ledger = repo_audit.published_ledger(self.config)
        _bins = sum(len(_v) for _v in _ledger.values())
        console.print(
            f"  Local manifest   : {_bins:,} binary version(s) across "
            f"{len(_ledger):,} package(s)")

    def _rsync_streamed(self, label, src, dest, ssh_cmd, delete=False,
                        ignore_existing=False, filters=None):
        """rsync -aH (ADDITIVE by default) with --info=progress2, mirroring
        rsync's single overall % into a ProgressBar so the operator sees
        movement during a multi-GB sync.  rsync writes progress to stdout
        using \\r in-place updates, so we read raw and split on \\r/\\n.
        stderr is merged into stdout to avoid a pipe-buffer deadlock;
        non-progress lines are kept for the error log.  Returns the rsync
        return code.

        delete=False (default) → NO `--delete`: the destination ACCUMULATES
        files, never removing a published version absent from the local tree
        (the UPD-01 append-only guarantee).  delete=True is reserved for an
        explicit, deliberate mirror-reset — never the normal publish path.

        ignore_existing=True (UPD-02) → `--ignore-existing`: skip files already
        present at the destination by NAME (not mtime) — for the immutable .deb
        pool pass, so a re-publish never re-uploads unchanged .debs.  `filters`
        are extra rsync `--include`/`--exclude` args (e.g. a deb-only filter).

        ssh_cmd=[] (COMP-02 local publish) → no `-e` arg, so rsync runs in
        local-to-local mode (`dest` is a plain filesystem path)."""
        _bar = ProgressBar(label, itr_label='', maxvalue=100,
                           show_rate=False, label_width=34)
        _argv = ['rsync', '-aH']
        if delete:
            _argv.append('--delete')
        if ignore_existing:
            _argv.append('--ignore-existing')
        _argv += list(filters or [])
        _argv += ['--info=progress2']
        if ssh_cmd:
            _argv += ['-e', ' '.join(ssh_cmd)]
        _argv += [f'{src}/', dest]
        _proc = subprocess.Popen(
            _argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        _tail: 'list[str]' = []
        try:
            _stream = _proc.stdout
            _seg = ''
            while _stream is not None:
                _ch = _stream.read(1)
                if not _ch:
                    break
                if _ch in '\r\n':
                    _line, _seg = _seg.strip(), ''
                    if not _line:
                        continue
                    _m = re.search(r'(\d+)%', _line)
                    if _m:
                        _bar.step(int(_m.group(1)) - _bar.value)
                    else:
                        _tail.append(_line)
                else:
                    _seg += _ch
            _proc.wait()
        finally:
            _bar.close()
        if _proc.returncode != 0:
            logger.error(
                f"_rsync_streamed {label}: rc={_proc.returncode}; "
                f"tail={' | '.join(_tail[-10:])}")
        return _proc.returncode

    def cmd_repo(self, action: str = '', *args):
        """Dispatcher for `repo <action>` commands.

        Merged from the former `package` and `repo` commands — the
        whole repo lifecycle (tunnel a pre-built .deb in, audit
        constraints, strip NMU residue, clean up obsoletes, index
        metadata, migrate layout, reload a fork after edit) lives
        under one verb.
        """
        _table = {
            'tunnel':         'pull prebuilt .debs from Debian repo '
                              '(repo tunnel [pkg…])',
            'audit':          'one-stop pre-ship gate: dep + conflict + '
                              'stale-files + content integrity + NMU '
                              'residue.  Pass `quick` to skip the slow '
                              '(~30s) integrity scan.  Pass a target '
                              'name to drill in: `repo audit lsb-base`.',
            'audit external': 'audit the published repo at AptSourceURL — '
                              'reachable + InRelease signed by our key + '
                              'index size/SHA256 consistent (no pool dl)',
            'repair':         'umbrella for repo-state fixups: `repo '
                              'repair strip` (NMU suffix backfill), '
                              '`repo repair cleanup` (drop obsoletes).',
            'index full':     'index ALL suites in-place under '
                              'repo/dists/<codename>{,-debug}/ (default)',
            'index minimal':  'build + index + sign the runtime subset '
                              '(main debs, no -dbg/-dbgsym/-source/udeb) '
                              'into publish/',
            'publish ssh full':    'rsync the full repo/ to [Repo] '
                                   'PublishSshTarget (run `repo index full` first)',
            'publish ssh minimal': 'rsync the minimal publish/ to '
                                   'PublishSshTarget (run `repo index minimal` first)',
            'publish local <path> [full|minimal]':
                                   'COMP-02: copy + reindex at a local filesystem '
                                   'destination (USB / NFS / web docroot); '
                                   'missing <path> prompts to mkdir -p',
            'summary ssh':         'COMP-02: summary of the ssh destination '
                                   '(files, suites, InRelease signature + date, '
                                   'snapshot pins, manifest tally) — read-only',
            'summary local <path>':'COMP-02: same summary against a local '
                                   'filesystem destination',
            'refresh':        'UPD-01: realise the selected current pin — rebuild '
                              'the published→current source delta, +asg-stamp, '
                              'merge-index, publish additively, prune (no target; '
                              'pick the destination via `snapshot select` first)',
            'external':       'enable/disable/status the external published repo '
                              '(repo external <status|enable|disable>)',
        }
        if action == 'tunnel':
            return self.cmd_tunnel_package(*args)
        if action == 'refresh':
            return self.cmd_repo_refresh(*args)
        if action == 'audit':
            # `repo audit external` audits the published mirror; everything
            # else is the local repo gate (with optional drill-in target).
            if args and args[0] == 'external':
                return self.cmd_audit_external(*args[1:])
            return self.cmd_audit(*args)
        if action == 'repair':
            return self.cmd_repo_repair(*args)
        if action == 'index':
            # Multi-token: `repo index full` (default) / `repo index minimal`.
            _sub = args[0] if args else 'full'
            _rest = args[1:]
            if _sub == 'minimal':
                return self.cmd_index_repo_minimal(*_rest)
            if _sub == 'full':
                return self.cmd_index_repo(*_rest)
            return self._group_help('repo', _table, f'index {_sub}')
        if action == 'publish':
            return self.cmd_repo_publish(*args)
        if action == 'summary':
            return self.cmd_repo_summary(*args)
        if action == 'external':
            return self.cmd_repo_external(*args)
        return self._group_help('repo', _table, action)

    def cmd_repo_external(self, action: str = '', *args):
        """Enable/disable/inspect the EXTERNAL published repo (UPD-01).

        Usage: repo external <status|enable|disable>

        disable → LOCAL-ONLY: `repo publish` updates only the local signed
        manifest (no rsync); +asg bumps derive from it.  For testing without a
        VM.  Local stays single-snapshot, so advancing several snapshots while
        disabled then re-enabling can jump bump numbers (you're warned).
        enable → onto an EMPTY remote: rebaseline current = base (fresh first
        publish); onto a NON-empty remote: requires published == current."""
        del args
        _table = {
            'status':  'show external on/off + base/published/current',
            'enable':  'turn the external repo ON (rebaseline if remote empty)',
            'disable': 'LOCAL-ONLY mode (local manifest is the authority)',
        }
        if action == 'status':
            _on = self._external_enabled()
            console.print(
                f"external repo: {'ENABLED' if _on else 'DISABLED (local-only)'}")
            console.print(
                f"  base / published / current: {self._snapshot_base_ts()} / "
                f"{self._snapshot_published()} / {self._snapshot_current()}")
            return
        if action == 'disable':
            utils.write_snapshot_state(self.config, external=False)
            console.print(
                "external repo: DISABLED — local-only.  `repo publish` now "
                "updates only the local manifest; +asg bumps derive from it.  "
                "Local stays single-snapshot.", tui.COLOR_WARNING)
            return
        if action == 'enable':
            _remote = repo_audit.fetch_remote_ledger(self.config)
            if _remote is None:
                console.print(
                    "repo external enable: remote unreachable (check [Repo] "
                    "AptSourceURL / network) — not enabling.", tui.COLOR_ERROR)
                return
            if not _remote:
                # boundary-D: empty remote → rebaseline as a first run.
                _base = self._snapshot_base_ts()
                utils.write_snapshot_state(self.config, external=True,
                                           current=_base, published=_base)
                self.flags.cache_ready = False
                self.flags.dep_check_ready = False
                console.print(
                    f"external repo: ENABLED — remote is empty, rebaselined "
                    f"current = base = {_base} (treat as a first publish).  Run "
                    f"`cache build` + `cache parse`, a full build, then `repo "
                    f"publish ssh full`.", tui.COLOR_WARNING)
                return
            _pub = self._snapshot_published()
            _cur = self._snapshot_current()
            if _cur and _pub and apt_pkg.version_compare(_cur, _pub) != 0:
                console.print(
                    f"repo external enable: REFUSED — current ({_cur}) != "
                    f"published ({_pub}); publish the pending delta first so "
                    f"local and remote don't diverge.", tui.COLOR_ERROR)
                return
            utils.write_snapshot_state(self.config, external=True)
            console.print("external repo: ENABLED.", tui.COLOR_HIGHLIGHT)
            return
        return self._group_help('repo external', _table, action)

    # ─────────────────────────────────────────────────────────────────────
    # UPD-01 — snapshot management + the refresh orchestrator
    # ─────────────────────────────────────────────────────────────────────

    def cmd_snapshot(self, action: str = '', *args):
        """Manage the upstream snapshot pins (base + current) and inspect the
        update workload.  Pins persist in config/snapshot.state (durable —
        survives `clean cache`) and take precedence over [Snapshot] Timestamp.

        Usage:
          snapshot                          — status overview (base/current/latest)
          snapshot list                     — base, current, and the snapshots
                                              between current and latest
          snapshot workload [<ts>|latest]   — sources changed current → target
          snapshot select                   — interactive picker: choose the next
                                              current from the in-between snapshots
          snapshot select base|current <ts|latest>  — set a pin explicitly
          snapshot advance <ts|latest>      — shorthand for `select current …`
          snapshot base [show|<ts>]         — show/set the base-archive pin

        Pins are explicit YYYYMMDDTHHMMSSZ timestamps or `latest` — no implicit
        "next".  `base` is FORWARD-ONLY (it marks what's archivable on the
        remote; moving it back would un-archive).  Changing a pin affects what
        the next build/publish ships — production-impacting, so setters
        confirm first."""
        _table = {
            'list':     'base, current + the snapshots between current and latest',
            'workload': 'sources changed current → target: snapshot workload [<ts>|latest]',
            'select':   'pick the next current interactively (no args), or set a '
                        'pin: snapshot select base|current <ts|latest>',
            'advance':  'shorthand for `select current`: snapshot advance <ts|latest>',
            'base':     'show/set the base-archive pin: snapshot base [show|<ts>]',
            'status':   'status overview (also the bare `snapshot`)',
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
            return self._cmd_snapshot_select('current', *args)
        if action == 'base':
            return self._cmd_snapshot_base(*args)
        return self._group_help('snapshot', _table, action)

    def _snapshot_current(self):
        """Effective current pin (state.current > [Snapshot] Timestamp)."""
        try:
            return utils.resolve_snapshot_timestamp(self.config)
        except Exception as e:
            logger.warning(f"_snapshot_current: {e}")
            return None

    def _snapshot_base_ts(self):
        """Base pin from config/snapshot.state; defaults to current when unset
        (base == current initially)."""
        _b = utils.read_snapshot_state(self.config).get('base', '')
        return _b or self._snapshot_current()

    def _snapshot_published(self):
        """The snapshot the REMOTE (or local manifest, external-off) currently
        reflects (config/snapshot.state 'published'); defaults to base when
        unset.  This is the floor `repo refresh` diffs FROM — so each refresh
        rebuilds only what changed between the last publish and current."""
        _p = utils.read_snapshot_state(self.config).get('published', '')
        return _p or self._snapshot_base_ts()

    def _external_enabled(self) -> bool:
        """Runtime external-repo flag: config/snapshot.state 'external' overrides
        the [Repo] ExternalEnabled default."""
        _st = utils.read_snapshot_state(self.config)
        if 'external' in _st:
            return bool(_st['external'])
        return bool(getattr(self.config, 'external_enabled', True))

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
        """Before cache build: ensure a durable CURRENT pin (and BASE) exist in
        config/snapshot.state.  On a fresh system (the state file is gitignored,
        so a new checkout has none) PROMPT the operator to select both — cache
        build depends on the snapshot.  Returns True to proceed, False to abort.

        No-op when snapshots are disabled or a current pin already exists
        (base defaults to current when unset)."""
        if not self.config.snapshot_enabled:
            return True
        if utils.read_snapshot_state(self.config).get('current'):
            return True

        console.print(
            "No snapshot pin defined (config/snapshot.state is empty).  cache "
            "build needs a CURRENT pin (the build snapshot) and a BASE pin (the "
            "archive floor) — select both now.", tui.COLOR_WARNING)
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
        _base = self._prompt_for_timestamp(
            f"base snapshot / archive floor (default = current {_current})",
            _current, allow_latest=False)
        if not _base:
            console.print("cache build aborted — no base snapshot selected.",
                          tui.COLOR_ERROR)
            return False
        if _base > _current:
            console.print(
                f"base ({_base}) is AFTER current ({_current}) — clamping base "
                f"to current.", tui.COLOR_WARNING)
            _base = _current
        utils.write_snapshot_state(self.config, base=_base, current=_current)
        console.print(
            f"snapshot pins set — base={_base}, current={_current} "
            f"(config/snapshot.state)", tui.COLOR_HIGHLIGHT)
        return True

    def _cmd_snapshot_overview(self):
        """READ-ONLY status: base / current / latest + the current source."""
        if not self.config.snapshot_enabled:
            console.print(
                "  snapshot pinning is DISABLED ([Snapshot] Enabled=false)")
            return
        _cur = self._snapshot_current()
        _base = self._snapshot_base_ts()
        _src = ('config/snapshot.state'
                if utils.read_snapshot_state(self.config).get('current')
                else f'[Snapshot] Timestamp={self.config.snapshot_timestamp_config}')
        _pub = self._snapshot_published()
        console.print("Snapshot status:")
        console.print(f"  base      (archive floor) : {_base or '(unset)'}")
        console.print(f"  published (on the remote) : {_pub or '(unset)'}")
        console.print(f"  current   (build pin)     : {_cur or '(unresolved)'}")
        console.print(f"  current source            : {_src}")
        _latest = self._snapshot_latest()
        console.print(f"  latest upstream           : {_latest or '(query failed)'}")
        if _cur and _pub and apt_pkg.version_compare(_cur, _pub) > 0:
            console.print(
                f"  → current is AHEAD of published — `repo refresh` to build "
                f"+ publish the {_cur} delta")
        if _cur and _latest and _latest > _cur:
            console.print(
                "  → latest is AHEAD of current — `snapshot workload latest` to "
                "see the delta, `repo refresh latest` to roll forward")

    def _cmd_snapshot_list(self):
        """READ-ONLY: base, current, and every snapshot BETWEEN current and
        latest (numbered) — the ones you can advance current to."""
        if not self.config.snapshot_enabled:
            console.print("  snapshot pinning is DISABLED")
            return
        _base = self._snapshot_base_ts()
        _cur = self._snapshot_current()
        _latest = self._snapshot_latest()
        _pub = self._snapshot_published()
        console.print("Snapshot timeline:")
        console.print(f"  [  base   ] {_base or '(unset)'}"
                      + (f"  ({utils.format_snapshot_timestamp(_base)})"
                         if _base else ''))
        console.print(f"  [published] {_pub or '(unset)'}"
                      + (f"  ({utils.format_snapshot_timestamp(_pub)})"
                         if _pub else ''))
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

    def _cmd_snapshot_select(self, which: str = '', *rest):
        """Set a snapshot pin.  No args → interactive picker (choose the next
        current from the snapshots between current and latest, like
        `cache select`).  Explicit: `snapshot select base|current <ts|latest>`.
        Durable (config/snapshot.state); cautioned; base AND current are
        forward-only."""
        if not which:
            return self._snapshot_select_interactive()
        _target = rest[0] if rest else ''
        if which not in ('base', 'current') or not _target:
            console.print("Usage: snapshot select [base|current <ts|latest>]  "
                          "(no args → interactive picker)")
            return
        if _target == 'latest':
            if which == 'base':
                console.print(
                    "snapshot select base: a concrete timestamp is required "
                    "(not `latest`)", tui.COLOR_ERROR)
                return
            _target = self._snapshot_latest()
            if not _target:
                console.print("snapshot select: could not resolve `latest`",
                              tui.COLOR_ERROR)
                return
        self._set_snapshot_pin(which, _target)

    def _set_snapshot_pin(self, which: str, target: str) -> bool:
        """Validate (forward-only) + caution + write a base/current pin.
        Returns True if the pin was set."""
        if not re.match(r'^\d{8}T\d{6}Z$', target):
            console.print(f"snapshot select: '{target}' is not a "
                          f"YYYYMMDDTHHMMSSZ timestamp", tui.COLOR_ERROR)
            return False
        _old = (self._snapshot_base_ts() if which == 'base'
                else self._snapshot_current())
        if _old and target < _old:
            _why = ("base is forward-only; moving it back would un-archive "
                    "already-archivable versions"
                    if which == 'base'
                    else "current can only move forward")
            console.print(
                f"snapshot select {which}: REFUSED — {_why} "
                f"({target} < {_old}).", tui.COLOR_ERROR)
            return False
        console.print(f"snapshot select {which}: {_old or '(unset)'} → {target}",
                      tui.COLOR_WARNING)
        console.print(
            "  PRODUCTION IMPACT: changes what the next build/publish ships "
            "(current) or what becomes archivable (base).")
        if which == 'current':
            _pub = self._snapshot_published()
            if _old and _pub and apt_pkg.version_compare(_old, _pub) > 0:
                console.print(
                    f"  WARNING: current ({_old}) is AHEAD of published ({_pub}) "
                    f"— that delta is UNPUBLISHED.  Advancing to {target} leaves "
                    f"it unpublished (bump numbers stay correct via the manifest, "
                    f"but the intermediate versions won't be published).",
                    tui.COLOR_WARNING)
        if Prompt(PROMPT_YESNO,
                  f"Set {which} pin to {target}?").get_response().lower() \
                not in ('y', 'yes'):
            console.print("  aborted — pin unchanged")
            return False
        if which == 'base':
            utils.write_snapshot_state(self.config, base=target)
        else:
            utils.write_snapshot_state(self.config, current=target)
        console.print(
            f"snapshot select: {which} pin set to {target} "
            f"(config/snapshot.state)")
        if which == 'current':
            # The resolved pin changed → the in-memory cache is now stale; force
            # the operator to re-resolve at the new pin before building.
            self.flags.cache_ready = False
            self.flags.dep_check_ready = False
            console.print(
                "  cache invalidated — run `cache build` + `cache parse` to "
                "resolve the dep tree at the new pin, then `repo refresh`")
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
                          "with `snapshot select current <ts>`", tui.COLOR_ERROR)
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
        self._set_snapshot_pin('current', _cands[_idx - 1])

    def _cmd_snapshot_base(self, *args):
        """Show or set the base-archive pin (forward-only; config/snapshot.state).

        Usage: snapshot base [show|<ts>]   (no arg / `show` → show; <ts> → set)

        Archive STORAGE is deferred — the pin only records which versions are
        archivable; nothing is deleted."""
        _arg = args[0] if args else ''
        if _arg and _arg != 'show':
            return self._cmd_snapshot_select('base', _arg)
        _base = self._snapshot_base_ts()
        console.print(f"snapshot base pin: {_base or '(unset)'}")
        _ledger = repo_audit.fetch_remote_ledger(self.config)
        if _ledger:
            _multi = {p: vs for p, vs in _ledger.items() if len(set(vs)) > 1}
            _tot = sum(len(set(vs)) for vs in _ledger.values())
            console.print(
                f"  remote retention: {len(_ledger)} package(s), {_tot} "
                f"version(s); {len(_multi)} with >1 retained")
            console.print(
                "  (versions from snapshots older than base are archivable; "
                "archive STORAGE is deferred — nothing is deleted)")

    def _cmd_snapshot_workload(self, *args):
        """READ-ONLY: sources that change from the CURRENT pin to the TARGET
        (latest, or an explicit <ts>) — what `repo refresh` would rebuild
        advancing current → target."""
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

    def cmd_repo_refresh(self, *args):
        """UPD-01: thin convenience for the day-2 update cycle — runs
        `source sync` → `source build all` → `repo publish ssh full`.

        Pick the destination first with `snapshot select` (or `snapshot select
        current <ts>`), then `cache build` + `cache parse`, then `repo refresh`.
        Each step is update-aware AND runnable on its own — refresh just chains
        them: `source build all` auto-detects the published→current delta and
        stamps +asg<R>u<N>; `repo publish ssh full` merges with the prior
        manifest, rsyncs additively (when external is enabled), prunes, and
        records published = current.  No target — the destination is the
        current pin."""
        if args:
            console.print(
                "repo refresh takes no target — set the destination with "
                "`snapshot select current <ts>`, then `cache build` + `cache "
                "parse`, then `repo refresh`.", tui.COLOR_WARNING)
            return
        if not self.flags.dep_check_ready:
            console.print(
                "repo refresh: run `cache build` + `cache parse` first — at the "
                "selected current pin (`snapshot select` invalidates the cache).",
                tui.COLOR_ERROR)
            return
        _cur = self._snapshot_current()
        _pub = self._snapshot_published()
        if _cur and _pub and apt_pkg.version_compare(_cur, _pub) <= 0:
            console.print(
                f"repo refresh: current ({_cur}) is not ahead of published "
                f"({_pub}) — nothing to refresh.  Pick a newer destination with "
                f"`snapshot select`.", tui.COLOR_INFO)
            return
        console.print(
            "repo refresh: source sync → source build all → repo publish ssh "
            "full", tui.COLOR_HIGHLIGHT)
        self.cmd_source_sync()
        self.cmd_source_build('all')          # auto-detects update mode + stamps
        self.cmd_repo_publish('ssh', 'full')  # merge + publish + prune + record

    def _refresh_merge_index(self) -> bool:
        """Build the suite index as the UNION of the local single-snapshot pool
        and the PRIOR PUBLISHED manifest (apt_repo.merge_remote_index), signed
        locally, then refresh the local signed manifest to the merged result.
        Works offline (the manifest, not the remote, is the merge base).
        Returns True on success."""
        import signing
        import apt_repo
        _codename = self.config.build_codename.strip('"').strip("'")
        _version = self.config.build_version.strip('"').strip("'")
        # The prior published state = the local signed manifest (empty on the
        # first publish → merge yields just the local pool).
        _prior = repo_audit.local_manifest_path(self.config)
        _password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()
        _r = subprocess.run(['sudo', '-S', '-v'], input=_password + '\n',
                            capture_output=True, text=True)
        if _r.returncode != 0:
            console.print("ERROR: incorrect sudo password")
            return False
        try:
            if not apt_repo.merge_remote_index(
                    repo_root=self.config.dir_repo,
                    suite=_codename, codename=_codename, arch=self.config.arch,
                    version=_version, remote_packages_path=_prior,
                    password=_password,
                    signing_homedir=signing.signing_home(self.config)):
                return False
        finally:
            _password = '*' * len(_password)  # noqa: F841
        # Refresh the local signed manifest = the merged multi-version index.
        # STA-21: fail loud on signing failure.
        _merged = os.path.join(self.config.dir_repo_main, 'Packages')
        try:
            with open(_merged) as _fh:
                _ok = repo_audit.write_published_manifest(self.config, _fh.read())
        except OSError as e:
            console.print(f"repo: could not refresh manifest from {_merged}: {e}",
                          tui.COLOR_ERROR)
            return False
        if not _ok:
            console.print(
                "repo: local manifest sign FAILED — `+asg uN` bump "
                "authority is now empty.  Run `key generate` / `key verify` "
                "then re-run `repo refresh`.", tui.COLOR_ERROR)
            return False
        return True

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
            'strip':   'NMU-suffix backfill across repo/',
            'cleanup': 'delete obsolete .debs/.udebs (dry-run by default; '
                       'pass `force` to actually delete)',
        }
        if action == 'strip':
            return self.cmd_strip_repo(*args)
        if action == 'cleanup':
            return self.cmd_package_cleanup(*args)
        return self._group_help('repo repair', _table, action)

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
        """Binary names a SELECTED package supersedes via Conflicts/Replaces
        (e.g. athena-setup-udeb Conflicts apt-setup-udeb), EXCLUDING names that
        are themselves selected (so genuine pool mutual-exclusions like
        grub-pc/grub-efi-amd64 are kept — both are selected pool extras).

        d-i's anna/udpkg IGNORE Conflicts, so a rename fork's superseded
        upstream binary (apt-setup-udeb) would otherwise ship + run alongside
        the fork — the security.debian.org install bug.  Used to drop such
        binaries from the cleanup AND to exclude them from the installer pool.
        """
        _selected_names: 'set[str]' = set()
        _superseded: 'set[str]' = set()
        for _tree in (self.dep_tree, self.udeb_dep_tree):
            if _tree is None:
                continue
            for _name, _pkgobj in (
                    getattr(_tree, 'selected_pkgs', None) or {}).items():
                _selected_names.add(_name)
                for _field in ('Conflicts', 'Replaces'):
                    _val = _pkgobj.get(_field) or ''
                    for _dep in _val.split(','):
                        _nm = (_dep.strip().split('(', 1)[0]
                               .split(':', 1)[0].strip())
                        if _nm:
                            _superseded.add(_nm)
        return _superseded - _selected_names

    def _scan_stale_files(self) -> 'tuple[list, list, list, int]':
        """Walk repo/{main,doc,dbgsym,tests} for .deb/.udeb files that
        shouldn't be there given the current selected_srcs + src_pkg_files.

        Returns (orphan, drift, malformed, total):
          orphan    — list of (sub, filename, source_name, size) where
                      the file's Source field doesn't name any selected
                      source.  Most common cause: source dropped from
                      the dep tree (e.g. upstream `tasksel` replaced by
                      `athena-tasksel` fork → leaves 222 task-*
                      binaries orphaned).
          drift     — list of (sub, filename, source_name, size) where
                      the source IS selected but this specific filename
                      isn't in any predicted-files list.  Most common
                      cause: source rebuilt at a new version, old .deb
                      lingers (e.g. base-files_12.4_amd64.deb left over
                      after the same-name fork bumped to
                      base-files_12.4+deb12u14+athena1_amd64.deb).
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
        for _sub in utils._REPO_SUBDIRS:
            # refresh=True: re-scan repo/ fresh so the keep/delete decision
            # reflects the CURRENT on-disk artifacts (not a stale cached
            # Packages snapshot) — deleting on stale data is dangerous, and a
            # stale snapshot is exactly what hid the orphaned upstream apt-setup
            # udebs after the rename fork.
            for _filename, _ctrl in repo_audit.iter_packages_all_versions(
                    self.config, subdir=_sub, refresh=True):
                _total += 1
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
                if _is_expected:
                    if _fn == _hi_fn:
                        continue                        # current version — KEEP
                    _drift.append((_sub, _fn, _src_name, _size))   # superseded
                    continue
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
                # production sibling (lib*-i386, lib*-l10n, etc.) that
                # ships in /cdrom/pool but isn't an install target.  KEEP.

        return _orphan, _drift, _malformed, _total

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
        finally:
            _spin.done()

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
        if _n_obsolete == 0:
            console.print("repo/ is clean — no obsolete files found")
            return
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

        if not _force:
            console.print(
                "\nDRY-RUN — no files were deleted.  "
                "Pass `repo repair cleanup force` to actually delete.",
                tui.COLOR_INFO,
            )
            return

        # Force mode: final confirmation prompt.
        _resp = Prompt(
            PROMPT_YESNO,
            f"DELETE {_n_obsolete} obsolete file(s) "
            f"({_bytes_obsolete / 1024 / 1024:.1f} MB)?  "
            f"This is IRREVERSIBLE.",
        ).get_response()
        if _resp.lower() not in ('y', 'yes'):
            console.print("Aborted — no files deleted")
            return

        # ------ Delete ------
        _deleted = 0
        _delete_failed = 0
        _bar = ProgressBar(
            label='Cleanup', maxvalue=_n_obsolete, show_rate=False,
        )
        # CONF-01 Stage D: _sub is the classify_repo_subdir label; map
        # to the on-disk dir via config.deb_dest_for_filename (which
        # handles the udeb → debian-installer/binary-<arch>/ special
        # case for us).
        for _sub, _f, *_ in _orphan:
            _bar.step(1)
            _p = os.path.join(self.config.deb_dest_for_filename(_f), _f)
            try:
                os.remove(_p)
                _deleted += 1
            except OSError as e:
                _delete_failed += 1
                logger.error(f"cleanup: cannot remove {_p}: {e}")
        for _sub, _f, *_ in _drift:
            _bar.step(1)
            _p = os.path.join(self.config.deb_dest_for_filename(_f), _f)
            try:
                os.remove(_p)
                _deleted += 1
            except OSError as e:
                _delete_failed += 1
                logger.error(f"cleanup: cannot remove {_p}: {e}")
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

        # UX-05g: reset flags on entry so a Ctrl+C / exception during this
        # run can't leave stale True values from a previous successful
        # run.  Re-set to True only on the success-tail (line ~5001 for
        # chroot_ready, the verify call sets chroot_verified separately).
        self.flags.chroot_ready = False
        self.flags.chroot_verified = False

        # Verify the project signing key before any sudo / mount / dpkg work
        if not self._ensure_signing_key_verified():
            return

        # Pre-flight audit gates (P5 2026-05-23).  Two layers, both
        # ABORT on red unless `no-gate` is in args:
        #   1. source audit — build-state per source (binaries present,
        #      .result fresh, patches not drifted)
        #   2. repo audit — install-time risks (unresolved Depends,
        #      conflict cohorts)
        # Operator can bypass both with `chroot build live no-gate`
        # for emergency / debugging — when the audits are noisy in a
        # known-acceptable way but you want to push through.
        _no_gate = 'no-gate' in args or '--no-gate' in args
        if not _no_gate:
            if not self._preflight_audit_source():
                console.print("Aborted by source audit pre-flight")
                return
            if not self._preflight_audit_repo():
                console.print("Aborted by repo audit pre-flight")
                return
        else:
            console.print(
                "chroot build live: pre-flight audits BYPASSED "
                "(no-gate)",
                tui.COLOR_WARNING,
            )

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
            console.print("Run 'cache parse' first")
            return
        if not self.flags.source_build_ready:
            console.print(
                "Run 'source build installer' first (need .udeb files in repo/)"
            )
            return
        if self.udeb_dep_tree is None:
            console.print(
                "Udeb dep tree not built — re-run 'cache parse' (it populates "
                "udeb_dep_tree alongside the deb tree)"
            )
            return
        if not self.udeb_dep_tree.selected_pkgs:
            console.print(
                "Udeb closure is empty — check installer.list contains udeb "
                "names and cache has the d-i Packages index"
            )
            return

        # Pre-flight closure audit — scoped to the installer (udeb)
        # selection.  Installer chroot uses dpkg --unpack (no apt),
        # so unmet deps don't fail until the installer runs on the
        # target — catch them here.  Two-layer gate (P5 2026-05-23) —
        # source audit first, then repo audit.  Bypass with `no-gate`.
        _no_gate = 'no-gate' in args or '--no-gate' in args
        if not _no_gate:
            if not self._preflight_audit_source():
                console.print("Aborted by source audit pre-flight")
                return
            if not self._preflight_audit_repo():
                console.print("Aborted by repo audit pre-flight")
                return
        else:
            console.print(
                "chroot build installer: pre-flight audits BYPASSED "
                "(no-gate)",
                tui.COLOR_WARNING,
            )

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
                dir_udebs=self.config.dir_repo_main_udeb,
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
            _result = build_system.build_iso(container=self.container)
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

        # Verify the project signing key BEFORE any sudo work — apt on the
        # installed target verifies our Release against this key, and
        # _sign_release_files inside build_installer_iso will fail loud if
        # the key isn't present.  Failing here is cheaper.
        import signing
        if not self._ensure_signing_key_verified():
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
            # Tag the filename with the snapshot pin so an installer ISO from
            # the base snapshot is distinguishable from one built after
            # stepping the snapshot (UPD-01).  Empty when snapshots off.
            _snap = utils.snapshot_iso_tag(self.config)
            _iso_basename = (
                f"athena-installer-{_version}-{_snap}-amd64.iso" if _snap
                else f"athena-installer-{_version}-amd64.iso")
            console.print(
                f"Building installer ISO {_iso_basename}..."
            )
            # base_include and pool_whitelist for the installer ISO
            # both drop `live_exclusive_pkg_names` — packages that
            # only exist in selected_pkgs because Pass IV resolved
            # `live.list` and pulled them in transitively.  The
            # installer ISO has nothing to do with live boot; those
            # binaries (live-boot, live-config, live-tools, etc.)
            # should not ship on the installer disc or end up on the
            # installed target.
            #
            # This was wrong earlier when pkg.list was incomplete:
            # busybox isn't in stock Debian's required/important set,
            # we didn't list it explicitly, so it got pulled in only
            # via live.list's transitive deps and got classified as
            # live-exclusive — base-installer's install_kernel then
            # failed when this filter dropped it from the target set
            # (caught 2026-05-12).  Fix: pkg.list now lists every
            # binary d-i actually apt-installs at install time (audit
            # walked buildroot/installer/ for apt-install callsites);
            # busybox is in pkg_closure after Pass III; Pass IV's
            # live.list resolution finds it already present and
            # doesn't add it to live_exclusive.
            _live_excl = self.dep_tree.live_exclusive_pkg_names
            _extras    = self.dep_tree.extras_pkg_names
            # COMP-02 phase D follow-up: pool extras (from pool.list,
            # resolved in Pass VII) ship in /cdrom/pool but are NOT
            # installed in any chroot — drop them from base_include so
            # debootstrap doesn't pull them onto the target.  They
            # remain in _pool_whitelist so they're indexed in the
            # cdrom apt pool, available for `apt-get install` on the
            # target post-install (or by grub-installer at install
            # time, the case that motivated the file).
            _pool_extras = self.dep_tree.pool_extras_pkg_names
            # GROUPS-01: pkg.list groups other than [base] ship in the
            # cdrom pool but are NOT installed at target debootstrap
            # time — tasksel apt-installs the operator-chosen groups
            # at install time from /cdrom/pool.
            _group_extras = self.dep_tree.pkg_group_extras_pkg_names

            # Pre-flight integrity check: catch operator mistakes that
            # would manifest as a silent install-time UX bug (e.g.
            # tasksel shows a checkbox for an empty task, or a group
            # silently has zero packages because every seed was a
            # typo).
            #
            # `pkg_group_pkg_names[g]` is the DELTA of canonical names
            # added by group `g` — it's empty in two distinct cases:
            #   (a) every seed name was already in selected_pkgs from an
            #       earlier group / required / important.  Not a typo;
            #       the group is REDUNDANT but the tasksel task still
            #       works because its Key entries resolve from elsewhere.
            #       Canonical example: [ssh-server] = openssh-server when
            #       openssh-server is also in [base].
            #   (b) one or more seeds failed to resolve (typo, missing
            #       from cache).  Genuine bug — operator must fix.
            # Distinguish the two by re-parsing pkg.list and checking
            # whether each seed is reachable in selected_pkgs.
            _group_pkgs = self.dep_tree.pkg_group_pkg_names
            try:
                _raw_pkg_groups = utils.parse_pkg_list_groups(
                    self.config.pkglist_path,
                )
            except Exception:
                _raw_pkg_groups = {}
            for _g, _names in _group_pkgs.items():
                if _names:
                    continue
                _seeds = list(_raw_pkg_groups.get(_g, []))
                _unresolved = [
                    _s for _s in _seeds
                    if _s not in self.dep_tree.selected_pkgs
                ]
                if _seeds and not _unresolved:
                    console.print(
                        f"INFO: pkg.list group [{_g}] adds 0 unique "
                        f"packages — all {len(_seeds)} seed(s) already "
                        "pulled in by an earlier group or required/"
                        "important.  Tasksel task remains valid (Key "
                        "entries resolve from elsewhere).",
                        tui.COLOR_INFO,
                    )
                    logger.info(
                        f"iso build installer: group [{_g}] redundant "
                        f"with earlier groups (all {len(_seeds)} seed(s) "
                        "already selected)"
                    )
                else:
                    _detail = (', '.join(_unresolved)
                               if _unresolved else '(empty seed list)')
                    console.print(
                        f"WARNING: pkg.list group [{_g}] resolved to ZERO "
                        "canonical packages — "
                        f"{len(_unresolved)}/{max(1, len(_seeds))} seed(s) "
                        f"not in cache: {_detail}.  Check seed names "
                        "against your cache.",
                        tui.COLOR_WARNING,
                    )
                    logger.warning(
                        f"iso build installer: group [{_g}] has empty "
                        f"closure; unresolved seeds: {_detail}"
                    )
            _non_base_groups = [
                _g for _g in _group_pkgs.keys() if _g != 'base'
            ]
            if _non_base_groups and not _group_extras:
                console.print(
                    f"WARNING: {len(_non_base_groups)} non-[base] group(s) "
                    "declared but pkg_group_extras_pkg_names is empty — every "
                    "package got credited to an earlier group (probably "
                    "[base]).  The non-base group(s) will be empty in tasksel.",
                    tui.COLOR_WARNING,
                )
                logger.warning(
                    "iso build installer: non-base groups exist but all "
                    "packages credited to earlier groups"
                )
            _canonical = {
                _name for _name in self.dep_tree.selected_pkgs
                if _name == self.dep_tree.selected_pkgs[_name]['Package']
            }
            _base_include = sorted(
                _canonical - _extras - _live_excl - _pool_extras - _group_extras
            )
            # Pool keeps Recommends-only extras, pool extras, AND
            # group extras so the operator (or grub-installer /
            # tasksel) can apt-install them post-install via the
            # cdrom: source.
            _pool_whitelist = _canonical - _live_excl

            # Snapshot-aware kernel pick: tell _find_kernel which
            # linux-image-<ABI>-amd64 the cache expects.  Without
            # this, _find_kernel falls back to highest sorted on
            # disk — which can be a stale higher-ABI .deb left over
            # from a pre-rollback snapshot, breaking the installer
            # because the ramdisk's modules won't match (CONF-13
            # symptom from 2026-05-19).
            import re as _re
            _kernel_pat = _re.compile(
                r'^linux-image-\d+\.\d+\.\d+-\d+-amd64$'
            )
            _kernel_candidates = sorted(
                _n for _n in self.cache.package_hashtable.keys()
                if _kernel_pat.match(_n)
            )
            _expected_kernel = _kernel_candidates[-1] if _kernel_candidates else None
            if _expected_kernel:
                console.print(
                    f"Cache predicts kernel binary: {_expected_kernel}",
                    tui.COLOR_INFO,
                )

            _ok = iso_installer.build_installer_iso(
                dir_chroot_installer=self.config.dir_chroot_installer,
                dir_repo=self.config.dir_repo_main,
                dir_repo_main_udeb=self.config.dir_repo_main_udeb,
                dir_image=self.config.dir_image,
                installer_dir=os.path.join(self.config.working_dir, 'installer'),
                password=_password,
                iso_basename=_iso_basename,
                container=self.container,
                suite=_suite,
                codename=_codename,
                version=_version,
                snapshot=_snap,
                base_include_pkgs=_base_include,
                deb_whitelist=_pool_whitelist,
                signing_homedir=signing.signing_home(self.config),
                signing_pubkey_path=signing.signing_pubkey_path(self.config),
                pkg_groups=self.dep_tree.pkg_group_pkg_names,
                group_meta=self.dep_tree.pkg_group_meta,
                expected_kernel_pkg=_expected_kernel,
                # Drop upstream binaries a shipped fork supersedes (e.g.
                # apt-setup-udeb vs athena-setup-udeb) from the pool — anna
                # ignores Conflicts, so leaving them ships + runs both.
                exclude_names=self._superseded_binary_names(),
                # Non-main component dirs (tunneled firmware/microcode and
                # any future contrib/non-free binaries) so they reach the
                # cdrom pool — finish-install.d/08hw-detect apt-installs
                # microcode from cdrom on offline/no-mirror installs.
                dir_repo_extras=[
                    self.config.dir_repo_non_free_firmware,
                    self.config.dir_repo_non_free,
                    self.config.dir_repo_contrib,
                ],
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
    # installer / recommended / all are mutually exclusive; named pkgs
    # are a sixth (also exclusive) mode.  'pkg' is the default when no
    # subset and no names are given (Phase 4 — used to be 'live' pre-pivot).
    # 'pkg' = pkg.list closure only; 'live' = live extras only; 'installer'
    # = udeb closure + installer.list deb-arm extras; 'recommended' = extras
    # pulled by depth-1 Recommends; 'all' = union of every selected source
    # in dep_tree + udeb_dep_tree (no exclusions — equivalent to running
    # pkg + live + installer + recommended back-to-back, deduped).
    _SOURCE_SUBSETS = ('pkg', 'live', 'installer', 'recommended', 'all')

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

    def cmd_build_iso_disk(self, *args):
        """COMP-09 — Build a pre-installed bootable qcow2 disk image
        from the LIVE chroot.

        Usage: iso build disk [size_gb] [force]

          size_gb — disk image size in GB (default from
                    `[Build] DiskImageSizeGB`, fallback 5).  Sparse
                    qcow2 — actual on-disk footprint depends on the
                    chroot's payload, not this number.
          force   — bypass chroot_verified gate, same semantics as
                    `iso build live force`.

        Output: image/<distribution>-<version>-<arch>.qcow2

        Boots directly into the running OS (no installer step).
        Suitable for VM / cloud deployment.

        Prerequisites:
          - Chroot built + verified (chroot_verified flag).
          - Host packages: rsync, dosfstools (mkfs.fat), qemu-utils
            (qemu-img).  Plus losetup/sfdisk/mkfs.ext4/grub-install/
            blkid from util-linux + grub-* (all in default Debian
            install).  Helper checks at entry and surfaces the first
            missing tool with an actionable message.

        Known v1 limitation: grub-install runs on the build host, so
        the produced disk image's GRUB binaries reflect the host's
        GRUB version (analogous to pre-COMP-14 ISO leakage).  Follow-
        up will move grub-install into the build container.
        """
        import disk_image

        _force = 'force' in args
        # First non-flag arg is the size; ignore unknown flags.
        _size_gb = self.config.disk_image_size_gb
        for _a in args:
            if _a == 'force':
                continue
            try:
                _size_gb = int(_a)
                break
            except ValueError:
                console.print(
                    f"Ignoring unknown arg: {_a!r} (expected size_gb "
                    f"integer or `force`)"
                )

        if not _force and not self.flags.chroot_verified:
            if self.flags.chroot_ready:
                console.print(
                    "Chroot built but verification failed — re-run "
                    "`chroot verify` after fixing, or pass `force`"
                )
            else:
                console.print("Run `chroot build` first")
            return

        self.flags.iso_disk_ready = False  # reset before work; set True only on success

        # Cache sudo password — same pattern as cmd_build_iso_live.
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
            logger.error("cmd_build_iso_disk: sudo -v failed")
            _password = '*' * len(_password)
            return

        try:
            # Force mode re-verifies the on-disk chroot before building —
            # same contract as `iso build live force`.  Without this, force
            # would master an UNVERIFIED chroot into a bootable image (the
            # gate above is bypassed but nothing re-checks the 8 invariants).
            if _force:
                console.print("Force mode: re-verifying chroot before disk image...")
                _passed, _failed = self._verify_chroot(
                    _password, self.config.dir_chroot)
                if _failed > 0:
                    console.print(
                        f"ERROR: chroot verification failed "
                        f"({_failed} of {_passed + _failed} checks) — "
                        f"refusing to build disk image"
                    )
                    logger.error(
                        f"build_iso_disk force: verify failed "
                        f"{_failed}/{_passed + _failed}"
                    )
                    return
                # Truthful: we just ran the 8 checks, so refresh the flag
                # for subsequent (non-force) calls — mirrors iso build live.
                self.flags.chroot_verified = True

            _version  = self.config.build_version.strip('"').strip("'")
            _distro   = self.config.build_distribution.strip('"').strip("'")
            _arch     = self.config.arch
            _out_name = f'{_distro.lower()}-{_version}-{_arch}.qcow2'
            _out_path = os.path.join(self.config.dir_image, _out_name)

            console.print(
                f"Building {_size_gb} GB pre-installed disk image: "
                f"{_out_path}"
            )
            _ok = disk_image.build_disk_image(
                dir_chroot=self.config.dir_chroot,
                output_qcow2=_out_path,
                size_gb=_size_gb,
                password=_password,
                container=self.container,
            )
            if not _ok:
                console.print(
                    "ERROR: disk image build failed — see logs for details"
                )
                logger.error("cmd_build_iso_disk: build_disk_image returned False")
                return
            self.flags.iso_disk_ready = True
        finally:
            _password = '*' * len(_password)  # noqa: F841

    # ─────────────────────────────────────────────────────────────────
    # Source state classifier — shared by cmd_source_audit (reports)
    # and cmd_source_repair (acts).  Pure read-only: walks disk only,
    # never mutates .result / .patchhash / source files.
    # ─────────────────────────────────────────────────────────────────

    def _patchhash_status(self, pkg: str, src) -> str:
        """Pure-read patch-baseline status for one source.  Returns:

          'matches'  — .patchhash exists AND equals patch_set_hash of
                       the current on-disk patch set.  Strongest signal:
                       binaries produced under this baseline are
                       provably current.

          'differs'  — .patchhash exists AND disagrees.  Means the
                       patch set on disk has changed since the build
                       that wrote .patchhash.  Binaries are stale
                       w.r.t. the current patch set.

          'absent'   — no .patchhash file (pre-baseline-policy build,
                       or operator wiped log/build/).  We can't prove
                       binaries are fresh OR stale — caller decides
                       how to treat this.
        """
        _buildlog = os.path.join(self.config.dir_log, 'build')
        _hash_file = os.path.join(_buildlog, pkg + '.patchhash')
        try:
            with open(_hash_file, 'r') as fh:
                _stored = fh.read().strip()
        except OSError:
            return 'absent'
        if not _stored:
            return 'absent'
        _patch_dir = os.path.join(
            self.config.dir_patch_source, pkg,
            utils.version_no_epoch(src.version),
        )
        _current = utils.patch_set_hash(_patch_dir, src.patch_list or [])
        return 'matches' if _stored == _current else 'differs'

    _SOURCE_STATES = (
        'ok',             # binaries present + valid, .result=PASS,
                          # patches unchanged
        'no_pkgs',        # source declares no main-tier binaries
        'tunneled',       # .result=TUNNELED (third-party pull, not built)
        'fail',           # .result=FAIL — explicit prior-build failure
                          # marker.  Repair LEAVES ALONE (operator
                          # signal: "this is known broken; don't
                          # second-guess").
        'needs_sync',     # source files missing or .verified absent
        'needs_build',    # binaries missing or invalid (legitimate rebuild)
        'stale_pass',     # .result=PASS but state has drifted (binaries
                          # gone OR patches changed) — repair would CLEAR
        'repairable',     # binaries present + valid but .result MISSING
                          # — repair would WRITE PASS
    )

    def _source_state(self, pkg: str, src) -> str:
        """Classify one source's current state.  Returns one of
        `_SOURCE_STATES`.  Pure-read.

        Two axes of evidence feed the classification:

          A. .result content — PASS / FAIL / TUNNELED / missing.
          B. .patchhash status — matches / differs / absent.

        And one filesystem check: do predicted binaries exist + open
        as valid ar archives?

        Resolution table (binaries valid case):

          | .result | .patchhash | state       |
          |---------|------------|-------------|
          | PASS    | matches    | ok          |
          | PASS    | differs    | stale_pass  ← patches drifted
          | PASS    | absent     | ok           (migration; presume current)
          | missing | matches    | repairable  ← write PASS
          | missing | differs    | needs_build ← binaries stale, must rebuild
          | missing | absent     | needs_build ← no proof of freshness
          | FAIL    | any        | fail         (operator marker; no-op)
          | TUNNELED| any        | tunneled

        Why 'needs_build' rather than 'repairable' when .patchhash is
        absent: without a baseline we can't prove the on-disk binaries
        match the current patch set.  Conservatively assume they're
        stale; require a rebuild.  This makes the audit/repair cycle
        SETTLE in one round when repair clears stale_pass (which
        deletes both .result and .patchhash) — the next audit reports
        'needs_build' and the operator knows to rebuild, not repair
        again.

        Resolution order (short-circuits):
          1. no predicted binaries → 'no_pkgs'
          2. .result=TUNNELED → 'tunneled'
          3. .result=FAIL → 'fail'
          4. any source file missing on disk OR .verified absent → 'needs_sync'
          5. any predicted binary missing/not-ar → 'stale_pass' (if PASS)
             else 'needs_build'
          6. classify via the table above
        """
        _expected = self._predicted_files_for_source(pkg)
        if not _expected:
            return 'no_pkgs'
        _buildlog = os.path.join(self.config.dir_log, 'build')
        _result_file = os.path.join(_buildlog, pkg + '.result')
        _result_first_line = ''
        _result_exists = False
        try:
            with open(_result_file) as fh:
                _result_first_line = fh.readline().strip()
                _result_exists = True
        except OSError:
            pass
        if _result_first_line == 'TUNNELED':
            return 'tunneled'
        if _result_first_line == 'FAIL':
            return 'fail'

        # (4) source files — every entry in src.files must exist on
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

        # (5) predicted binaries.
        _all_present = True
        # Caller has gated on build_container_ready (see source audit /
        # source repair) — self.container is set.  is_ar_file is a
        # staticmethod so accessing it through the class works without
        # the optional-None unwrap mypy wants.
        assert self.container is not None
        for _f in _expected:
            # UPD-01: accept the predicted pristine name OR a +asg<R>u<N>
            # stamped variant (find_matching_artifact) — same reconciliation
            # check_build uses, so a stamped delta isn't seen as "missing".
            _dst_dir = self.config.deb_dest_for_filename(_f)
            _match = utils.find_matching_artifact(_dst_dir, _f)
            if _match is None:
                _all_present = False
                break
            if not self.container.is_ar_file(_match):
                _all_present = False
                break

        if not _all_present:
            return 'stale_pass' if _result_first_line == 'PASS' else 'needs_build'

        # (6) binaries valid; classify via .result × .patchhash table.
        _patchhash = self._patchhash_status(pkg, src)
        if _result_first_line == 'PASS':
            # PASS row.  Matches OR absent (migration) → ok.  Differs
            # → stale_pass.
            return 'stale_pass' if _patchhash == 'differs' else 'ok'

        # .result missing (or atypical content).  Without .patchhash
        # matching, we can't prove binaries are current — must rebuild.
        if _patchhash == 'matches':
            return 'repairable'
        return 'needs_build'


    def cmd_source_repair(self, *args):
        """Align .result files with current source state.  MUTATOR.

        Usage: source repair [verbose]

        Walks every source via `_source_state` and acts on each state:

          repairable  — binaries valid but .result missing  → WRITE PASS.
                        (Original 2026-05-19 use case: recover from the
                        cmd_source_rescan/_refresh_patches incident
                        that wiped ~47 .result files.)
          stale_pass  — .result=PASS but state drifted (binaries gone
                        OR patches changed)  → CLEAR .result (and its
                        .patchhash if patches changed).  Next
                        `source build` will rebuild.
          others      — leave alone.  needs_build / needs_sync /
                        tunneled / ok / no_pkgs are not repair's
                        concern; `source audit` reports them.

        `source repair` does NOT trigger a rebuild itself — it only
        adjusts the .result sidecars so the NEXT `source build` does
        the right thing.

        Prereqs: cache build + cache parse + container init.
        """
        if not (self.flags.cache_ready and self.flags.dep_check_ready
                and self.flags.build_container_ready):
            console.print(
                "source repair needs cache build + cache parse + container "
                "init to have run first.",
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

        _restored: 'list[str]' = []
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
                _result_file = os.path.join(
                    self.container.buildlog_path, _name + '.result')
                if _state == 'repairable':
                    try:
                        with open(_result_file, 'w') as fh:
                            fh.write('PASS\n')
                        _restored.append(_name)
                        logger.info(f"source repair: restored {_result_file}")
                    except OSError as e:
                        console.print(
                            f"ERROR: cannot write {_result_file}: {e}",
                            tui.COLOR_ERROR,
                        )
                elif _state == 'stale_pass':
                    # Clear .result + .patchhash so next source build
                    # rebuilds.  Both removals are best-effort; logging
                    # captures any rmdir-style edge cases.
                    _hash_file = _result_file.replace(
                        '.result', '.patchhash',
                    )
                    _removed_any = False
                    for _f in (_result_file, _hash_file):
                        try:
                            os.remove(_f)
                            _removed_any = True
                        except FileNotFoundError:
                            pass
                        except OSError as e:
                            logger.warning(
                                f"source repair: cannot remove {_f}: {e}"
                            )
                    if _removed_any:
                        _cleared.append(_name)
                        logger.info(
                            f"source repair: cleared stale {_result_file}"
                        )
                else:
                    _other[_state] = _other.get(_state, 0) + 1
        finally:
            _bar.close()

        console.print("Source repair:")
        console.print(
            f"  {len(_restored):5d}  .result restored to PASS "
            "(binaries present, .result was missing)"
        )
        console.print(
            f"  {len(_cleared):5d}  stale .result cleared "
            "(binaries gone or patches changed)"
        )
        for _state in self._SOURCE_STATES:
            if _state in ('repairable', 'stale_pass'):
                continue
            _n = _other.get(_state, 0)
            if _n:
                console.print(f"  {_n:5d}  {_state}  (no action)")

        if _verbose and (_restored or _cleared):
            if _restored:
                console.print("")
                console.print(f"Restored ({len(_restored)}):")
                for _n in _restored:
                    console.print(f"  {_n}")
            if _cleared:
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

    def _workload_since_published(self, ledger: dict) -> 'set[str]':
        """Selected source names whose pristine BASE advanced beyond what the
        remote (the `ledger`) has published — the genuine rebuild workload for
        the current snapshot (UPD-01 §4).

        `ledger` is {package: [published version, ...]} from
        repo_audit.fetch_remote_ledger.  A source is in the workload if any of
        its predicted binaries has a base greater than the highest base
        published for that binary (apt_pkg.version_compare), or unpublished.
        The closure is inherent: the dep-tree resolved consistently at the
        target snapshot, so rebuilding every base-advanced source yields a
        consistent pool (no separate synthesized-stanza solve needed)."""
        _published = repo_audit.published_base_versions(ledger)
        assert self.dep_tree is not None
        _srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _n, _s in self.udeb_dep_tree.selected_srcs.items():
                _srcs.setdefault(_n, _s)
        _out: 'set[str]' = set()
        for _name in _srcs:
            for _f in self._predicted_files_for_source(_name):
                if not (_f.endswith(('.deb', '.udeb'))):
                    continue
                _parts = _f.rsplit('.', 1)[0].split('_')
                if len(_parts) != 3:
                    continue
                _bin, _ver, _ = _parts
                _base = utils.pristine_base(_ver)
                _pub = _published.get(_bin)
                if _pub is None or apt_pkg.version_compare(_base, _pub) > 0:
                    _out.add(_name)
                    break
        return _out

    def _workload_current_to_target(self, target_ts: str):
        """Sources whose upstream SOURCE version at `target_ts` is NEWER than
        the current pin's selected source version — what you'd rebuild
        advancing current → target (UPD-01 `snapshot workload` / `repo refresh`).

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
        — i.e. what changed between the published snapshot and current.  This
        is the `repo refresh` rebuild set (floor = published, destination =
        current).  Full source-version compare (catches +debNuN; ignores
        binNMU).  A source absent from `from_ts` (new since) also counts.
        Returns (sorted_names, None) or (None, error)."""
        _from = repo_audit.fetch_source_versions_at(self.config, from_ts)
        if _from is None:
            return None, (f"could not fetch the published snapshot ({from_ts}) "
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
            _fv = _from.get(_name)
            _cur_v = utils.version_no_epoch(str(_src.version))
            if _fv is None or apt_pkg.version_compare(
                    _cur_v, utils.version_no_epoch(_fv)) > 0:
                _out.append(_name)
        return sorted(_out), None

    def _update_build_pending(self) -> bool:
        """True when `source build` should run in UPDATE mode: a published base
        exists (local manifest non-empty) and current is ahead of published."""
        if not self.flags.dep_check_ready:
            return False
        if not repo_audit.published_ledger(self.config):
            return False
        _pub = self._snapshot_published()
        _cur = self._snapshot_current()
        return bool(_cur and _pub and apt_pkg.version_compare(_cur, _pub) > 0)

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
            _dst = self.config.deb_dest_for_filename(_f)
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
        _published = self._snapshot_published()
        _current = self._snapshot_current()
        console.print(
            f"source build: UPDATE mode — published {_published} → current "
            f"{_current}; rebuilding the changed source delta (+asg-stamped, "
            f"per-file N) plus any other source needing a build.",
            tui.COLOR_HIGHLIGHT)
        _workload, _err = self._workload_since_snapshot(_published)
        if _err:
            console.print(f"source build (update): {_err}", tui.COLOR_ERROR)
            return
        # N authority = the PUBLISHED manifest only.  N is a published-generation
        # counter (advances on `repo publish`, not per build): the next-to-mint is
        # one past the highest PUBLISHED uN; local rebuilds of the still-pending
        # update keep the same N.  (A build-ledger union was tried + reverted —
        # recording locally-built uN made asg_next_n overshoot by 1, turning every
        # same-base delta into a perpetual bump-target.)
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
                return
            # Pass the full delta workload (the loop skips the up-to-date and
            # rebuilds bump-targets) plus the extra needs_build sources.  Ledger
            # loaded → delta respins get +asg-stamped; forks build pristine.
            self.cmd_source_build(*(list(_workload) + _extra))
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

        Usage: source audit [verbose] [summary] [--since-published]

        `--since-published` intersects the rebuild queue with the genuine
        upstream advances vs the remote ledger (UPD-01 §4) — "what changed
        since the published archive", the `repo refresh` workload view.

        Audits dep_tree + udeb_dep_tree (merged).  Classifies each
        source into one of `_SOURCE_STATES` via `_source_state` —
        the same classifier `source repair` consults — and reports
        counts per state, optionally broken down by subset
        (base / live / installer / pool / recommended).

        States surfaced:
          ok            — source files synced + binaries present + valid,
                          .result=PASS, patches unchanged.  Nothing to do.
          needs_sync    — source files missing or .verified sidecar
                          absent → run `source sync <pkg>` (or `source
                          sync` for bulk).
          needs_build   — binaries missing or invalid → run `source build`.
          stale_pass    — WARN: .result=PASS but state has drifted
                          (binaries gone OR patches changed since last
                          successful build) → run `source repair` to
                          clear the lie; next `source build` will rebuild.
          repairable    — WARN: binaries valid but .result missing
                          (accidental wipe / pre-policy build) → run
                          `source repair` to write PASS without rebuilding.
          tunneled      — .result=TUNNELED (third-party pull).  Not built.
          no_pkgs       — source declares no main-tier binary in either
                          tree (rare; side-artifact-only sources).

        `summary` arg prints only the rebuild-queue count + subset
        breakdown — the operator's "how much work remains" view.

        Read-only by design: never writes .result, never invokes
        BuildContainer.build, never calls _refresh_patches.  Mutating
        the build state is `source repair`'s job (which uses the same
        classifier).

        Prereqs: cache + cache parse + container init.
        """
        if not (self.flags.cache_ready and self.flags.dep_check_ready
                and self.flags.build_container_ready):
            console.print(
                "source audit needs cache build + cache parse + container "
                "init to have run first.",
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

        # ── --since-published: genuine upstream advances vs the remote ──
        if '--since-published' in args:
            _ledger = repo_audit.fetch_remote_ledger(self.config)
            if _ledger is None:
                console.print(
                    "source audit --since-published: remote ledger "
                    "unreachable (check [Repo] AptSourceURL / network)",
                    tui.COLOR_ERROR)
                return
            _workload = self._workload_since_published(_ledger)
            _advanced = sorted(set(_rebuild_candidates) & _workload)
            console.print(
                f"Source audit (since published): {len(_advanced)} of "
                f"{len(_rebuild_candidates)} rebuild candidate(s) are genuine "
                f"upstream advances vs the published archive.")
            for _n in _advanced:
                console.print(f"    {_n}  [{_subset_for(_n)}]")
            return

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
                      "(synced, built, .result=PASS, patches fresh)")
        console.print(f"  {len(_by_state.get('needs_build',[])):5d}  "
                      "needs_build (binaries missing/invalid)")
        if _by_state.get('stale_pass'):
            console.print(
                f"  {len(_by_state['stale_pass']):5d}  "
                "stale_pass (WARN: .result=PASS but state drifted; "
                "run `source repair`)",
                tui.COLOR_WARNING,
            )
        if _by_state.get('repairable'):
            console.print(
                f"  {len(_by_state['repairable']):5d}  "
                "repairable (binaries valid but .result missing; "
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
                "(.result=TUNNELED, third-party pull)"
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
        _actionable = ('needs_build', 'stale_pass', 'repairable',
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
        # both check_build (skip-rebuild gate) and _do_tunnel.
        _expected_files = self._predicted_files_for_source(_src_pkg.package)

        # Tunneled packages: download Debian's pristine binary instead
        # of building locally.  check_build accepts 'TUNNELED' as a
        # valid result so we can skip packages already tunneled in a
        # previous run.  Pass UPSTREAM filenames (Filename: from cache).
        if _src_pkg.package in self.config.tunnel_packages:
            _tunnel_expected = self._tunnel_filenames_for_source(
                _src_pkg.package)
            if self.container.check_build(_src_pkg, _tunnel_expected):
                logger.warning(
                    f"Package {_src_pkg.package} already tunneled [SKIPPED]")
                return ('skipped', 0)
            _build_result = self._do_tunnel(_src_pkg)
            if _build_result:
                logger.warning(f"Tunnel {_src_pkg.package} [TUNNELED]")
                return ('tunneled', 0)
            logger.error(f"Tunnel {_src_pkg.package} [FAIL]")
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
            return ('failed', 0)
        logger.error(f"Building Package {_src_pkg.package} [FAIL]")
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
        if not self.flags.download_ready:
            console.print("Run 'source sync' first")
            return

        if not self.flags.build_container_ready:
            console.print("Run 'container init' first")
            return

        # UX-05g: reset on entry so an interrupted partial build can't
        # leak a stale True from a previous successful run.  Re-set to
        # True on the success-tail (~L7115).
        self.flags.source_build_ready = False

        # Parse args via the static helper for testability.
        _err, _force, _subset, _names, _profile_override = \
            self._parse_source_build_args(args)
        if _err:
            console.print(_err)
            return

        # UPD-01 (gotcha G): a subset/all/bare invocation AUTO-DETECTS update
        # mode — when a published base exists and current is ahead of it,
        # rebuild the published→current SOURCE delta (stamped +asg<R>u<N>)
        # instead of a plain subset build.  Explicit package names or a
        # [profile] override opt out (manual builds).  The operator runs the
        # same `source build [all|live|installer]`; we tell them which we ran.
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

        # UPD-01 bump-aware build: when a ledger is loaded (only
        # `_do_update_build` does so), un-forced builds additionally rebuild a
        # same-base security/NMU re-spin whose THIS-generation +asg<R>u<N>
        # artifact is missing — the one case the filename-based skip gate can't
        # see (the rebuilt pristine name collides with the prior build).  N is
        # per-file; the post-build stamp (buildcontainer) applies it.
        _bump_active = (self.container is not None
                        and getattr(self.container, 'asg_ledger', None) is not None)
        _bump_release = None
        if _bump_active:
            try:
                _bump_release = int(
                    str(self.config.build_version).strip('"').strip("'"))
            except (TypeError, ValueError):
                _bump_active = False   # can't derive N → stamper skips too

        # COMP-03 Phase 4: split the work into tunneled (serial — network-
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
                # COMP-03 Phase 6: heavy-package scheduler.  Sources in
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
        """Cache + package-universe operations.

        `build` / `purge` manage the local apt cache; `parse`, `select`,
        and `info` are read-side operations over it (dep-closure
        resolution, interactive package selection, per-package info).
        Merged here because all five operate on the same cached package
        universe (was `cache`, `cache parse`, `select` as three groups)."""
        _table = {
            'build':  'build apt cache from configured mirrors',
            'purge':  'delete every file in the cache directory (re-fetched on next build)',
            'parse':  'resolve full dep closure for selected packages',
            'select': 'interactively toggle packages in pkg.list (curses only)',
            'info':   'show concise info + relations for a package: cache info <pkg>',
        }
        if action == 'build':
            return self.cmd_build_cache(*args)
        if action == 'purge':
            return self.cmd_cache_purge(*args)
        if action == 'parse':
            return self.cmd_parse_dependency(*args)
        if action == 'select':
            return self.cmd_cache_select(*args)
        if action == 'info':
            return self.cmd_cache_info(*args)
        return self._group_help('cache', _table, action)

    def cmd_patch(self, action: str = '', *args):
        _table = {'refresh': 're-scan patch/source/ tree (after out-of-band edits)'}
        if action == 'refresh':
            return self.cmd_patch_refresh(*args)
        return self._group_help('patch', _table, action)

    def cmd_cache_select(self, *args):
        """COMP-06 — interactive package-set selector (`cache select`).

        Opens a `select` tab where the operator toggles packages in
        `config/pkg.list` and adds new ones from the cache, then saves.
        Requires the cache (for metadata) — gate on cache_ready.
        Interactive-only: needs the curses tab + key-interceptor API,
        absent on the headless Cli backend."""
        if not self.flags.cache_ready or self.cache is None:
            console.print("Run 'cache build' first (selector needs package metadata)")
            return
        if not hasattr(tui.tui_instance, 'set_tab_key_handler'):
            console.print("cache select: interactive selector needs the curses TUI "
                          "(not available in --headless mode); edit "
                          f"{self.config.pkglist_path} by hand")
            return
        from select_packages import SelectPackages
        SelectPackages(self.config, self.cache, tui.tui_instance).activate()

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

    def cmd_source(self, action: str = '', *args):
        _table = {
            'sync':     'fetch source tarballs: `source sync` (bulk) or '
                        '`source sync <pkg> [force]` (per-pkg)',
            'build':    'build sources: source build [force] [pkg | live | installer | recommended | all | <pkg>…] [[profile,…]]',
            'audit':    'READ-ONLY: report build-state of every source — '
                        'ok / needs_sync / needs_build / stale_pass / '
                        'repairable / tunneled.  Add `summary` for terse '
                        'count + subset breakdown.',
            'repair':   'MUTATOR: align .result with current state.  '
                        'Writes PASS where binaries are valid but .result '
                        'is missing; clears stale PASS where binaries '
                        'are gone or patches changed.',
            'fork':     'manage fork packages: `source fork <pkg>` '
                        'creates or reloads; `source fork <pkg> '
                        'enabled|disabled` toggles the .disabled marker',
        }
        if action == 'sync':
            return self.cmd_source_sync(*args)
        if action == 'build':
            return self.cmd_source_build(*args)
        if action == 'audit':
            return self.cmd_source_audit(*args)
        if action == 'repair':
            return self.cmd_source_repair(*args)
        if action == 'fork':
            return self.cmd_source_fork(*args)
        return self._group_help('source', _table, action)

    def cmd_container(self, action: str = '', *args):
        _table = {
            'init':  'build the Docker build sandbox image',
            'purge': 'stop+remove athenalinux containers + images (force rebuild on next init)',
        }
        if action == 'init':
            return self.cmd_init_container(*args)
        if action == 'purge':
            return self.cmd_container_purge(*args)
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
            'build disk':      'pre-installed bootable qcow2 disk image from '
                               'live chroot (`iso build disk [size_gb]`)',
        }
        if action == 'build':
            if not args:
                console.print("Usage: iso build <live | installer | disk>")
                return self._group_help('iso', _table)
            _sub = args[0]
            _rest = args[1:]
            if _sub == 'live':
                return self.cmd_build_iso_live(*_rest)
            if _sub == 'installer':
                return self.cmd_build_iso_installer(*_rest)
            if _sub == 'disk':
                return self.cmd_build_iso_disk(*_rest)
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

    def cmd_clean(self, action: str = '', *args):
        """Wipe per-stage working state.  Each sub-action is
        idempotent (safe to run on already-clean dirs) and resets the
        BuildFlags + drops in-memory pipeline references that pointed
        at the deleted files.  `force` skips the YESNO confirmation.
        Pairs with the early-exit guards on `cache build` and `dep
        parse`: re-runs of long resolves no-op until the operator
        explicitly cleans or passes `force`."""
        _table = {
            'cache':     'wipe cache/ (re-fetched on next `cache build`)',
            'source':    'wipe source/ (re-downloaded on next `source sync`)',
            'repo':      'wipe repo/ (rebuilt on next `source build`)',
            'buildroot': 'wipe buildroot/{live,installer} (sudo)',
            'image':     'wipe image/ (rebuilt on next `iso build`)',
            'download':  'wipe download/ (tunnel debs, re-fetched on demand)',
            'container': 'stop+remove athenalinux Docker containers + images',
            'all':       'wipe all of the above + reset every flag (one prompt)',
        }
        if action == 'cache':
            return self.cmd_cache_purge(*args)
        if action == 'source':
            return self.cmd_clean_source(*args)
        if action == 'repo':
            return self.cmd_clean_repo(*args)
        if action == 'buildroot':
            return self.cmd_clean_buildroot(*args)
        if action == 'image':
            return self.cmd_clean_image(*args)
        if action == 'download':
            return self.cmd_clean_download(*args)
        if action == 'container':
            return self.cmd_container_purge(*args)
        if action == 'all':
            return self.cmd_clean_all(*args)
        return self._group_help('clean', _table, action)

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
            'live':      'cache→parse→download→container→source build (+live)→chroot build live→iso build live',
            'installer': 'cache→parse→download→container→source build (+installer)→chroot build installer→iso build installer',
            'disk':      'cache→parse→download→container→source build (+live)→chroot build live→iso build disk (qcow2)',
        }
        if action in ('', 'live'):
            return self.cmd_auto_run_live(*args)
        if action == 'installer':
            return self.cmd_auto_run_installer(*args)
        if action == 'disk':
            return self.cmd_auto_run_disk(*args)
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

    def _run_autorun_steps(self, label: str, _steps: list) -> None:
        """Common driver shared by cmd_auto_run_{live,installer}.

        Walks _steps sequentially, calls each function, gates on its
        success flag.  On the first failure logs + breaks.  Emits the
        autorun summary (via print_commands.summary) on every exit path,
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

    # UX-05a: --yes auto-answers informational YESNO prompts (e.g.
    # "There are source build failures, Proceed?", "Generate a new
    # signing key now?").  Hard prompts (sudo password, conflict-
    # resolution OPTIONS, security-audit gates) still wait for the
    # operator regardless.
    _auto_yes = '--yes' in sys.argv
    if _auto_yes:
        sys.argv.remove('--yes')

    # UX-05e: `--cmd <cmd>` queues one or more commands to run sequentially
    # then exit, no REPL.  Multiple --cmd allowed; order preserved.
    # Implies --headless (the TUI's curses screen makes no sense for one-
    # shot).  Each <cmd> is one full command line (e.g. `--cmd "cache build"`
    # `--cmd "cache parse"`); shell-quoting separates args within one --cmd.
    # `-c` is taken by build-system.sh for --config-file, hence --cmd here.
    _one_shot_cmds: 'list[str]' = []
    _i = 0
    while _i < len(sys.argv):
        if sys.argv[_i] == '--cmd' and _i + 1 < len(sys.argv):
            _one_shot_cmds.append(sys.argv[_i + 1])
            del sys.argv[_i:_i + 2]
            continue
        _i += 1
    if _one_shot_cmds and not _headless:
        # -c implies headless — the curses screen would race the
        # one-shot dispatcher.
        _headless = True

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

    # Backend is either a Tui or a Cli depending on `--headless`.  Both
    # implement the same duck-typed Console-facade surface; typed Any
    # so mypy isn't forced to inspect every consumer's narrow assumption.
    from typing import Any as _Any
    tui_inst: _Any
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

    # UX-05a/e: thread the parsed flags onto the backend.  Both Cli and
    # Tui accept auto_yes; one_shot_cmds is Cli-only (Tui ignores).
    tui_inst.auto_yes = _auto_yes
    if hasattr(tui_inst, 'one_shot_cmds'):
        tui_inst.one_shot_cmds = list(_one_shot_cmds)

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

    # Tips are constrained: 4 (indent) + 14 (name pad in `help`) +
    # tip ≤ 80 cols.  Tips that need to enumerate many sub-actions
    # use `<subcmd>` and direct the operator at `<cmd>` (bare) which
    # prints the full per-action table via _group_help.
    tui.register_command('cache',     session.cmd_cache,     'Cache:      cache <build|purge|parse|select|info <pkg>>')
    tui.register_command('clean',     session.cmd_clean,     'Clean:      clean <subcmd> — run `clean` for the list')
    tui.register_command('patch',     session.cmd_patch,     'Patches:    patch refresh')
    tui.register_command('source',    session.cmd_source,    'Sources:    source <sync|build|audit|repair|fork>')
    tui.register_command('repo',      session.cmd_repo,      'Repo:       repo <index|publish|audit|repair|tunnel|refresh>')
    tui.register_command('snapshot',  session.cmd_snapshot,  'Snapshot:   snapshot <list|advance|workload|base>')
    tui.register_command('container', session.cmd_container, 'Container:  container <init|purge>')
    tui.register_command('chroot',    session.cmd_chroot,    'Chroot:     chroot build [live|installer] | chroot verify')
    tui.register_command('iso',       session.cmd_iso,       'ISO:        iso build <live|installer>')
    tui.register_command('key',       session.cmd_key,       'Signing:    key <generate|verify>')
    tui.register_command('autorun',   session.cmd_auto_run,  'Autorun:    autorun [live|installer]')
    tui.register_command('print',     session.cmd_print,     'Print:      print build state — try `print help`')

    console.print(asciiart_logo, tui.COLOR_ERROR)
    console.print("Starting Athena Build System...", tui.COLOR_HIGHLIGHT)
    console.print(f"\tArch\t\t\t{config.arch}")
    console.print(f"\tParent Distribution\t{config.release} {config.baseversion}")
    console.print(f"\tBuild Distribution\t{config.build_distribution} {config.build_version} ({config.build_codename})")

    tui_inst.wait()
    Exit(0)


if __name__ == '__main__':
    build_banner = "Athena Build System v0.1"
    print(asciiart_logo)
    main(build_banner)
