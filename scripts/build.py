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
import json
import os
import re
import shutil
import subprocess
import threading
import time
import sys
from typing import Callable, Optional

import apt_pkg

# Local imports
import utils
from utils import BuildConfig
from buildlog import BuildLog, human_size, safe_size
from cache import Cache

import buildcontainer
import dependencytree
import buildsystem
import installer_chroot
import iso_installer
import persistence
import repo_audit
import signal


import logging
from tui import Tui, console, Prompt, PROMPT_YESNO, PROMPT_INPUT, PROMPT_PASSWORD, Spinner, ProgressBar, Exit
from tui import setup_file_logging

# Command clusters — BuildSession is assembled from one mixin per functional
# area (see scripts/commands/).  build.py keeps __init__, shared state and
# the command dispatcher; each mixin owns its cluster's handlers + helpers.
from commands.cmd_audit import AuditCommandsMixin
from commands.cmd_build import BuildCommandsMixin
from commands.cmd_mirror import MirrorCommandsMixin
from commands.cmd_repo import RepoCommandsMixin
from commands.cmd_snapshot import SnapshotCommandsMixin
from commands.cmd_source import SourceCommandsMixin

logger = logging.getLogger('athena.build')

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

    UX-04: when constructed via `BuildFlags.load(path)`, every flag
    transition autosaves to a JSON sidecar (cheap, ~1 ms).  `restored_summary()`
    remains (dormant) for a future relook; the startup banner that consumed
    it was removed alongside the `resume` command 2026-06-08.
    """

    # Class-level annotations — mypy uses these to see the attributes that
    # __init__ sets via object.__setattr__.  They don't create class
    # attributes; assignment still goes to the instance __dict__.
    cache_ready:             bool
    dep_check_ready:         bool
    download_ready:          bool
    build_container_ready:   bool
    source_build_ready:      bool
    signing_key_verified:    bool
    chroot_ready:            bool
    chroot_verified:         bool
    chroot_installer_ready:  bool
    iso_live_ready:          bool
    iso_installer_ready:     bool
    iso_disk_ready:          bool
    _save_path:              'Optional[str]'

    _FIELDS = (
        'cache_ready', 'dep_check_ready', 'download_ready',
        'build_container_ready', 'source_build_ready',
        'signing_key_verified',
        'chroot_ready', 'chroot_verified', 'chroot_installer_ready',
        'iso_live_ready', 'iso_installer_ready', 'iso_disk_ready',
    )

    # Flags whose meaning depends on in-memory state (Cache, DT,
    # BuildContainer, signing key validity) that has to be re-established
    # at session start.  Persisted to disk but reset to False on load —
    # `cache parse` rebuilds Cache + DT and flips cache_ready /
    # dep_check_ready True; `cmd_init_container` flips
    # build_container_ready; `_ensure_signing_key_verified` does the key.
    _IN_MEMORY_ONLY = frozenset((
        'cache_ready', 'dep_check_ready',
        'build_container_ready', 'signing_key_verified',
    ))

    _FILENAME = 'buildflags.json'
    _FORMAT_VERSION = 1

    def __init__(self, save_path: 'Optional[str]' = None):
        # _save_path must exist BEFORE the field-set loop so that
        # __setattr__'s autosave check sees None (= disabled) during
        # initial assignment.
        object.__setattr__(self, '_save_path', None)
        for _f in self._FIELDS:
            object.__setattr__(self, _f, False)
        # NOW enable autosave (if requested).
        object.__setattr__(self, '_save_path', save_path)

    def __setattr__(self, name: str, value) -> None:
        object.__setattr__(self, name, value)
        if name in self._FIELDS and self._save_path:
            self._autosave()

    def _autosave(self) -> None:
        # __setattr__ only calls this when _save_path is truthy.
        assert self._save_path is not None
        try:
            _payload = {
                '_format_version': self._FORMAT_VERSION,
                'flags': {_f: getattr(self, _f) for _f in self._FIELDS},
            }
            _tmp = self._save_path + '.tmp'
            with open(_tmp, 'w') as _fh:
                json.dump(_payload, _fh, indent=2, sort_keys=True)
            os.replace(_tmp, self._save_path)
        except OSError as _e:
            logger.warning(f"BuildFlags autosave: {_e}")

    @classmethod
    def default_path(cls, config) -> str:
        return os.path.join(config.dir_cache, cls._FILENAME)

    @classmethod
    def load(cls, save_path: str) -> 'BuildFlags':
        """Read the JSON sidecar (if present), construct a BuildFlags with
        autosave enabled at `save_path`, and reset `_IN_MEMORY_ONLY` flags
        to False (their backing in-memory state isn't loaded yet)."""
        _flags = cls(save_path=None)
        if os.path.isfile(save_path):
            try:
                with open(save_path) as _fh:
                    _payload = json.load(_fh)
                if (_payload.get('_format_version') == cls._FORMAT_VERSION
                        and isinstance(_payload.get('flags'), dict)):
                    for _f, _v in _payload['flags'].items():
                        if _f in cls._FIELDS:
                            object.__setattr__(_flags, _f, bool(_v))
            except (OSError, json.JSONDecodeError) as _e:
                logger.warning(
                    f"BuildFlags.load({save_path}): {_e}; starting fresh")
        # In-memory flags reset — see _IN_MEMORY_ONLY rationale.
        for _f in cls._IN_MEMORY_ONLY:
            object.__setattr__(_flags, _f, False)
        # Enable autosave now that all initial values are settled.
        object.__setattr__(_flags, '_save_path', save_path)
        return _flags

    def restored_summary(self) -> str:
        """Comma-separated list of persisted-True flags (excluding the
        in-memory-only ones) for the startup banner."""
        _kept = [
            _f.replace('_ready', '')
            for _f in self._FIELDS
            if _f not in self._IN_MEMORY_ONLY and getattr(self, _f)
        ]
        return ', '.join(_kept)

    def __str__(self) -> str:
        """Return a compact one-line status string for display in the TUI."""
        return '  '.join(
            f"[{'✓' if getattr(self, _f) else '·'}] {_f.replace('_ready', '')}"
            for _f in self._FIELDS
        )


def _safe_filesize(path: str) -> int:
    """os.path.getsize that returns 0 instead of raising on missing /
    permission errors.  Used by status/summary printers that want to
    sum a list of paths without partial failures interrupting output."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _humansize(n: int) -> str:
    """Render a byte count as B / KiB / MiB / GiB with 1 decimal."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MiB"
    return f"{n / (1024 * 1024 * 1024):.1f} GiB"


def _shorten_origin(url: str, max_len: int = 70) -> str:
    """Compact a long pool URL: keep the host and the last 5 path
    components, drop the middle.  No-op when under max_len."""
    if len(url) <= max_len:
        return url
    if '://' not in url:
        return url
    _scheme, _rest = url.split('://', 1)
    _host, _, _path = _rest.partition('/')
    _parts = [_p for _p in _path.split('/') if _p]
    if len(_parts) <= 5:
        return url
    return f"{_host}/.../{'/'.join(_parts[-5:])}"


class BuildSession(AuditCommandsMixin, BuildCommandsMixin, MirrorCommandsMixin,
                   RepoCommandsMixin, SnapshotCommandsMixin, SourceCommandsMixin):
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
        # UX-04: BuildFlags.load reads buildflags.json (created on every
        # flag transition) and resets _IN_MEMORY_ONLY flags to False —
        # they need `cache parse` to actually rebuild Cache + DT before
        # they're true again.
        self.flags: BuildFlags = BuildFlags.load(
            BuildFlags.default_path(config))
        self.last_source_build_counts: 'Optional[dict]' = None
        # True only inside _do_update_build → cmd_source_build.  Gates
        # `_bump_active` (which forces rebuild of NMU-versioned sources
        # missing their expected `+asg<R>u<N>` artifact).  In NORMAL
        # mode the ledger is still loaded — for post-build stamping
        # lineage continuation — but bump-target detection MUST stay
        # off: the predicate would otherwise flag every NMU-suffixed
        # upstream source on every cmd_source_build call.
        self._in_update_build: bool = False

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

    def cmd_clean_all(self, *args):
        """Wipe every working dir + reset every BuildFlag + drop every
        in-memory pipeline reference.  Equivalent to `clean cache` +
        `clean source` + `clean repo` + `clean image` + `clean
        buildroot`, but with a single up-front
        confirmation and a single sudo unlock for the buildroot wipe.
        Preserved: gnupg/ (signing key), log/ (build history),
        patch/ (patch series)."""
        _force = 'force' in args
        if not _force:
            _resp = Prompt(PROMPT_YESNO,
                "clean all: wipes cache/, source/, repo/, "
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
        # UX-04: keep autosave wiring so the wipe is reflected on disk.
        self.flags = BuildFlags.load(BuildFlags.default_path(self.config))
        # After a buildroot wipe every flag should be False even if the
        # JSON still carries True (the underlying state is gone).  Set
        # each one through the property to trigger autosave.
        for _f in BuildFlags._FIELDS:
            setattr(self.flags, _f, False)
        self.last_source_build_counts = None
        # Scrub the password we just collected — same hygiene the rest
        # of the codebase uses for sudo passwords.
        _password = '*' * len(_password)
        console.print("clean all: complete — pipeline state reset", tui.COLOR_INFO)


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

        console.print("Preparing Parsing Tree...", tui.COLOR_INFO)
        self.dep_tree = dependencytree.DependencyTree(self.cache, select_recommended=False,
                    arch=self.config.arch, build_profiles=self.config.build_profiles)

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
        # UX-04: persist Cache + DT to dir_cache/session.pkl.gz so
        # `resume` (next process) can skip cache build + cache parse.
        # Best-effort: a save failure is logged but the build continues.
        persistence.save_session(self, self.config.dir_cache)


    # --------------------------------------Command: patch_refresh-------------------------------------

    def _refresh_patches(self) -> int:
        # ⚠️  DESTRUCTIVE — DELETES build.json records when patch-set
        # content has changed since the last build (see ~line 1336 below).
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
        # If you need ONLY the patch_list population (not the record
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

        # Invalidate stale build records when the patch SET for a source
        # has changed since the last successful build.  Without this,
        # autorun's source-build step happily skips packages with
        # `[SKIPPED] already built` even after the operator drops a new
        # patch in patch/source/<pkg>/<ver>/ — and the patch never takes
        # effect.  Caught 2026-05-13 with the base-installer Phase C
        # keyring patch: the patch was on disk, _refresh_patches
        # discovered it, but check_build saw the older .udeb and
        # skipped the rebuild.  Install booted the unpatched
        # base-installer → `gpgv: Can't check signature: No public key`.
        #
        # Two-stage check.  Stage 1 (cheap) is mtime: if no patch is
        # newer than the record, nothing to do.  Stage 2 (precise) is
        # content hash: only invalidate when the patch CONTENT actually
        # changed, not just the mtime.  This avoids spurious rebuilds
        # from header-only edits (DEP-3 commentary, comment tweaks) that
        # bump the mtime but produce an identical diff.
        #
        # Also catches patch REMOVAL: empty patch_list with a non-empty
        # stored hash → hash differs → invalidate.
        _buildlog = os.path.join(self.config.dir_log, 'build')
        _invalidated = []
        for _pkg, _src in _unified_srcs.items():
            _record_path = os.path.join(
                _buildlog, _pkg + utils.BUILD_RECORD_SUFFIX)
            _record = utils.read_build_record(_buildlog, _pkg)
            if _record is None:
                continue  # nothing to invalidate
            _stored_hash = str(_record.get('patch_set_hash') or '')
            _patch_dir = os.path.join(
                self.config.dir_patch_source, _pkg,
                utils.version_no_epoch(_src.version),
            )

            # Stage 1: mtime gate.  Re-hashing every source's patch tree
            # on every patch_refresh is wasteful when nothing changed —
            # gate on "is any patch file newer than the record?"
            try:
                _record_mtime = os.path.getmtime(_record_path)
            except OSError:
                continue
            _newer = any(
                os.path.getmtime(os.path.join(_patch_dir, _pf)) > _record_mtime
                for _pf in (_src.patch_list or [])
                if os.path.exists(os.path.join(_patch_dir, _pf))
            )
            # Patch-removal case: no patches now, but record carries a
            # non-empty hash → still need to compare (empty-set hash vs
            # stored).  Bypass the mtime gate for that case.
            if not _newer and not (_stored_hash and not _src.patch_list):
                continue

            # Stage 2: content hash.
            _current_hash = utils.patch_set_hash(
                _patch_dir, _src.patch_list or [])
            if _stored_hash == _current_hash:
                # Cosmetic edit (header / comment) — content unchanged.
                # Touch the record so the next refresh's mtime gate
                # doesn't keep tripping.  Computed >= every patch
                # mtime so kernel-clock vs time.time() drift can't
                # leave it behind.
                _newest_patch_mtime = max(
                    (os.path.getmtime(os.path.join(_patch_dir, _pf))
                     for _pf in (_src.patch_list or [])
                     if os.path.exists(os.path.join(_patch_dir, _pf))),
                    default=0.0,
                )
                _touch_mtime = max(time.time(), _newest_patch_mtime + 1.0)
                try:
                    os.utime(_record_path, (_touch_mtime, _touch_mtime))
                except OSError:
                    pass
                continue

            # Real content change — drop the record.  Next source build
            # will rebuild and write a fresh one with the current hash.
            try:
                os.remove(_record_path)
                _invalidated.append(_pkg)
            except OSError as e:
                logger.warning(
                    f"[patch] {_pkg}: cannot remove stale {_record_path}: {e}"
                )
        if _invalidated:
            _names = ', '.join(sorted(_invalidated))
            console.print(
                f"Invalidated {len(_invalidated)} stale build record(s) — "
                f"these will rebuild next source_build: {_names}",
                tui.COLOR_INFO,
            )
            logger.info(f"[patch] invalidated stale build records: {_names}")
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
        """Download upstream's prebuilt .deb files for src_pkg, then run
        the same post-build normalisation a from-source build would: strip
        NMU layers to pristine version + filename, and (when this is a
        delta and an asg ledger is loaded) stamp `+asg<R>u<N>`.

        Net effect: a tunneled binary is, on disk and in repo audit, a
        legitimately-built binary — pristine-named or +asg-stamped, byte-
        for-byte rebuilt by us.  The ONLY artefact preserved from the
        upstream form is `republished_from = {url, upstream_sha256}` on
        the build record (and the federation claim), which the mirror
        sidecar uses to mark the claim as "no owner" — the federation's
        ownership projection sees tunneled packages as un-owned without
        affecting any apt / repo-audit / source-audit interpretation.

        Returns True iff every output landed and normalised successfully.
        """
        # Upstream Filename: required for the snapshot.debian.org URL —
        # the pristine prediction names don't exist on the server.  We
        # rename after download.
        _upstream_files = self._tunnel_filenames_for_source(src_pkg.package)
        if not _upstream_files:
            logger.error(f"tunnel {src_pkg.package}: no binary packages known (run parse_dependency first)")
            return False

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

        import time as _time
        _buildlog_path = os.path.join(self.config.dir_log, 'build')
        _t_tunnel_start = _time.monotonic()
        # OBS-04 observability accumulators (tunnel path) — best-effort,
        # consumed by the verbose .buildlog written at the terminal.
        _purged_stale: 'list[str]' = []
        _strip_events: 'list[tuple[str, str]]' = []
        _stamp_events: 'list[tuple[str, str, str]]' = []
        try:
            utils.write_build_record(
                _buildlog_path,
                utils.new_build_record(
                    package=src_pkg.package,
                    intended_version=str(src_pkg.version),
                    patch_set_hash='',
                    component=_comp,
                ),
            )
        except OSError as _e:
            logger.warning(f"tunnel {src_pkg.package}: build-record entry: {_e}")

        # Download phase: pull every upstream-named .deb to its routed
        # pool dir.  Hash each freshly-downloaded file BEFORE strip so
        # we can record the upstream SHA-256 (federation provenance).
        _upstream_paths: 'dict[str, str]' = {}
        _upstream_urls: 'dict[str, str]' = {}
        _upstream_sha256s: 'dict[str, str]' = {}
        for _filename in _upstream_files:
            _dst_dir = self.config.deb_dest_for_filename(_filename, _comp)
            _dest = os.path.join(_dst_dir, _filename)

            # Stale-file wipe: same binary basename but a DIFFERENT pristine
            # version (e.g. a prior tunnel of an older upstream).  Files
            # whose pristine base matches the target's pristine base
            # (post-strip target, or +asg variant of it) are KEPT — they
            # are the legitimate skip-gate target downstream.
            _bin_name = _filename.split('_', 1)[0]
            _target_ver = _filename.split('_')[1]
            _target_pristine = utils.strip_nmu_suffix(_target_ver)
            try:
                for _existing in os.listdir(_dst_dir):
                    if not _existing.endswith(('.deb', '.udeb')):
                        continue
                    if _existing.split('_', 1)[0] != _bin_name:
                        continue
                    if _existing == _filename:
                        continue
                    _ex_ver = _existing.split('_')[1]
                    if utils.pristine_base(_ex_ver) == _target_pristine:
                        continue
                    _stale = os.path.join(_dst_dir, _existing)
                    logger.info(
                        f"tunnel {src_pkg.package}: removing stale "
                        f"non-matching {_existing} (target pristine "
                        f"{_target_pristine})")
                    try:
                        os.remove(_stale)
                        _purged_stale.append(_existing)
                    except OSError as _e:
                        logger.warning(
                            f"tunnel {src_pkg.package}: rm {_stale}: {_e}")
            except OSError:
                pass

            if os.path.isfile(_dest):
                logger.info(f"tunnel {src_pkg.package}: {_filename} already present, skipping download")
                _upstream_paths[_filename] = _dest
                _upstream_urls[_filename] = (
                    f"{_base}/{src_pkg.directory}/{_filename}")
                _h = utils.get_sha256(_dest, use_cache=False)
                if _h:
                    _upstream_sha256s[_filename] = _h
                continue

            _url = f"{_base}/{src_pkg.directory}/{_filename}"
            _upstream_urls[_filename] = _url
            logger.info(f"tunnel {src_pkg.package}: downloading {_url}")
            _bytes, _detail = utils.download_file(_url, _dest)
            if _bytes < 0:
                logger.error(f"tunnel {src_pkg.package}: failed to download {_filename}: {_detail or 'unknown'}")
                _success = False
                continue
            _upstream_paths[_filename] = _dest
            _h = utils.get_sha256(_dest, use_cache=False)
            if _h:
                _upstream_sha256s[_filename] = _h

        # Normalisation phase: strip NMU + asg-stamp, mirroring
        # BuildContainer._normalize_built_artifacts on the tunnel path.
        # final_paths is keyed by the FINAL on-disk filename; final_to_upstream
        # remembers which upstream filename each post-normalize file came
        # from so we can attach `republished_from` provenance.
        _final_paths: 'dict[str, str]' = {}
        _final_to_upstream: 'dict[str, str]' = {}
        _strips_count = 0
        _stamps_count = 0
        if _success and _upstream_paths:
            _post_strip: 'list[tuple[str, str]]' = []   # (path, upstream_fn)
            _any_stripped = False
            for _ups_fn, _ups_path in _upstream_paths.items():
                try:
                    _r = utils.strip_nmu_from_deb(_ups_path)
                except Exception as _e:
                    logger.warning(
                        f"tunnel strip_nmu: {os.path.basename(_ups_path)} "
                        f"failed: {_e}")
                    _post_strip.append((_ups_path, _ups_fn))
                    continue
                _new_path = _r.get('new_path', _ups_path)
                if _r.get('status') == 'rewritten':
                    _any_stripped = True
                    _strips_count += 1
                    if _new_path != _ups_path:
                        logger.info(
                            f"tunnel strip_nmu: {os.path.basename(_ups_path)}"
                            f" → {os.path.basename(_new_path)}")
                        _strip_events.append((
                            os.path.basename(_ups_path),
                            os.path.basename(_new_path)))
                _post_strip.append((_new_path, _ups_fn))

            _ledger = (getattr(self.container, 'asg_ledger', None)
                       if self.container is not None else None)
            _src_is_delta = (
                utils.strip_nmu_suffix(str(src_pkg.version))
                != str(src_pkg.version))
            _was_delta = _any_stripped or _src_is_delta
            _current = _post_strip
            if _was_delta and _ledger is not None:
                try:
                    _release = int(
                        str(self.config.build_version).strip('"').strip("'"))
                except (TypeError, ValueError):
                    logger.warning(
                        f"tunnel asg-stamp: [Build] VERSION not an integer "
                        f"({self.config.build_version!r}) — skipping stamp "
                        f"for {src_pkg.package}")
                    _release = None
                if _release is not None:
                    # Uniform per-source N: mirrors the rationale in
                    # BuildContainer._normalize_built_artifacts.  Take the
                    # MAX of every sibling binary's individual asg_next_n
                    # candidate so intra-source sibling pins (`Depends: X
                    # (= ver+asg<R>u<N>)`) all resolve.
                    _per_file_n: 'list[int]' = []
                    _stampable: 'list[tuple[str, str, str]]' = []
                    for _path, _ups_fn in _post_strip:
                        _b = os.path.basename(_path)
                        _name, _ext = os.path.splitext(_b)
                        _parts = _name.split('_')
                        if len(_parts) != 3:
                            continue
                        _pkg_n, _ver, _arch = _parts
                        _base_ver = utils.pristine_base(_ver)
                        _per_file_n.append(utils.asg_next_n(
                            _ledger.get(_pkg_n, []), _base_ver, _release))
                        _stampable.append((_path, _ups_fn, _b))
                    _stampable_paths = {_p for _p, _, _ in _stampable}
                    _stamped: 'list[tuple[str, str]]' = []
                    _n_uniform = max(_per_file_n) if _per_file_n else 1
                    for _path, _ups_fn in _post_strip:
                        if _path not in _stampable_paths:
                            _stamped.append((_path, _ups_fn))
                            continue
                        _b = os.path.basename(_path)
                        try:
                            _r = utils.restamp_asg_deb(
                                _path, _release, _n_uniform)
                        except Exception as _e:
                            logger.warning(
                                f"tunnel asg-stamp: {_b} failed: {_e}")
                            _stamped.append((_path, _ups_fn))
                            continue
                        _new_path = _r.get('new_path', _path)
                        if _r.get('status') == 'rewritten':
                            _stamps_count += 1
                            logger.info(
                                f"tunnel asg-stamp: {_b} → "
                                f"{os.path.basename(_new_path)} "
                                f"(+asg{_release}u{_n_uniform})")
                            _stamp_events.append((
                                _b, os.path.basename(_new_path),
                                f"+asg{_release}u{_n_uniform}"))
                        _stamped.append((_new_path, _ups_fn))
                    _current = _stamped

            for _final_path, _ups_fn in _current:
                _final_fn = os.path.basename(_final_path)
                _final_paths[_final_fn] = _final_path
                _final_to_upstream[_final_fn] = _ups_fn

        # Build-record terminal: outputs/output_hashes are FINAL
        # post-normalize names + SHA-256 of the rewritten on-disk file.
        # republished_from provenance keys by FINAL name → upstream URL +
        # upstream SHA-256 (pre-strip — the actual hash at the remote URL).
        _output_hashes: 'dict[str, str]' = {}
        if _success:
            for _fn, _dst in _final_paths.items():
                _h = utils.get_sha256(_dst, use_cache=False)
                if _h:
                    _output_hashes[_fn] = _h
        _republished_from: 'dict[str, dict]' = {}
        if _success:
            for _final_fn, _ups_fn in _final_to_upstream.items():
                _ups_url = _upstream_urls.get(_ups_fn)
                _ups_sha = _upstream_sha256s.get(_ups_fn)
                if not _ups_url or not _ups_sha:
                    continue
                _republished_from[_final_fn] = {
                    'url':             _ups_url,
                    'upstream_sha256': _ups_sha,
                }

        _outputs_sorted = sorted(_final_paths.keys()) if _final_paths \
            else sorted(_upstream_files)
        try:
            utils.update_build_record(
                _buildlog_path, src_pkg.package,
                phase=('tunneled' if _success else 'failed'),
                built_version=(
                    utils.strip_nmu_suffix(str(src_pkg.version))
                    if _success else None),
                finished=utils._utc_now_iso(),
                elapsed_seconds=round(_time.monotonic() - _t_tunnel_start, 3),
                output_count=len(_outputs_sorted),
                outputs=_outputs_sorted,
                output_hashes=_output_hashes,
                republished_from=_republished_from,
            )
        except (OSError, FileNotFoundError) as _e:
            logger.warning(f"tunnel {src_pkg.package}: build-record terminal: {_e}")

        # OBS-04: verbose tunnel narrative (log/build/<pkg>.buildlog).
        # Fully guarded — never reaches the tunnel control flow.
        try:
            _elapsed_t = round(_time.monotonic() - _t_tunnel_start, 3)
            _tblog = BuildLog(_buildlog_path, src_pkg.package, kind='tunnel')
            _tblog.header(
                status=('TUNNELED' if _success else 'FAIL'),
                intended_version=str(src_pkg.version),
                arch=self.config.arch,
                component=_comp,
                base_url=_base,
            )
            _tblog.section(
                f"EXPECTED (upstream binaries: {len(_upstream_files)})")
            for _uf in sorted(_upstream_files):
                _tblog.bullet(_uf)

            _tblog.section(
                f"DOWNLOADED ({len(_upstream_paths)})")
            if _upstream_paths:
                for _uf in sorted(_upstream_paths):
                    _tblog.file(
                        _uf, size=safe_size(_upstream_paths[_uf]),
                        sha256=_upstream_sha256s.get(_uf, ''))
            else:
                _tblog.empty()

            _tblog.section(f"PURGED stale ({len(_purged_stale)})")
            if _purged_stale:
                for _ps in sorted(_purged_stale):
                    _tblog.bullet(_ps)
            else:
                _tblog.empty()

            _tblog.section(f"NMU STRIP ({len(_strip_events)})")
            if _strip_events:
                for _old, _new in sorted(_strip_events):
                    _tblog.bullet(f"{_old}  →  {_new}")
            else:
                _tblog.empty()

            _tblog.section(f"ASG STAMP ({len(_stamp_events)})")
            if _stamp_events:
                for _old, _new, _tag in sorted(_stamp_events):
                    _tblog.bullet(f"{_old}  →  {_new}  ({_tag})")
            else:
                _tblog.empty()

            _tblog.section(
                f"FINAL ARTIFACTS (post-normalize: {len(_final_paths)})")
            _tot = 0
            if _final_paths:
                for _fn in sorted(_final_paths):
                    _sz = safe_size(_final_paths[_fn])
                    if _sz >= 0:
                        _tot += _sz
                    _prov = ' republished' if _fn in _republished_from else ''
                    _tblog.file(
                        _fn, size=_sz, sha256=_output_hashes.get(_fn, ''),
                        detail=_prov.strip())
            else:
                _tblog.empty()

            _tblog.footer(
                status=('TUNNELED' if _success else 'FAIL'),
                files=len(_final_paths),
                size=human_size(_tot),
                elapsed=f"{_elapsed_t}s")
            _tblog.write()
        except Exception as _e:
            logger.warning(f"tunnel buildlog {src_pkg.package}: {_e}")

        if _success:
            _upstream_ver = str(src_pkg.version)
            _pristine_ver = utils.strip_nmu_suffix(_upstream_ver)
            _total_bytes = sum(
                _safe_filesize(_dst) for _dst in _final_paths.values())
            if _pristine_ver == _upstream_ver and _stamps_count == 0:
                _ver_line = f"{_pristine_ver} (pristine)"
            else:
                _ver_line = f"{_upstream_ver} → {_pristine_ver}"
            console.print(
                f"  {src_pkg.package}  TUNNELED  {_ver_line}")
            console.print(
                f"    files     {len(_outputs_sorted)}  "
                f"({_humansize(_total_bytes)}, "
                f"{_strips_count} stripped, "
                f"{_stamps_count} asg-stamped)")
            _pool_dir: 'Optional[str]' = None
            for _fn in _outputs_sorted:
                _dst = _final_paths.get(_fn, '')
                _rel = os.path.relpath(_dst, self.config.dir_repo) \
                    if _dst else _fn
                _dir_part, _base = os.path.split(_rel)
                if _pool_dir is None:
                    _pool_dir = _dir_part
                    console.print(f"    pool      {_pool_dir}/")
                console.print(f"            + {_base}")
            if _upstream_urls:
                _origin = sorted(_upstream_urls.values())[0].rsplit('/', 1)[0]
                console.print(f"    origin    {_shorten_origin(_origin)}/")

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
        _failed_names: 'list[str]' = []
        progress_bar = ProgressBar(label='Tunnel', maxvalue=len(packages), show_rate=False)

        for _src_pkg in packages:
            _result = self._do_tunnel(_src_pkg)
            if _result:
                logger.warning(f"Tunnel {_src_pkg.package} [TUNNELED]")
                _success += 1
            else:
                logger.error(f"Tunnel {_src_pkg.package} [FAIL]")
                _failed += 1
                _failed_names.append(_src_pkg.package)
            progress_bar.step(1)
        progress_bar.close()

        console.print(
            f"Tunnel complete: {_success} tunneled, {_failed} failed "
            f"(of {len(packages)} requested)")
        if _failed_names:
            console.print(
                f"  failed: {', '.join(_failed_names)}  "
                f"(see log/build/<pkg> for details)")


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

    def _ensure_repo_indexed_for_chroot(self) -> bool:
        """MIRROR-01 Phase 8: auto-index the local repo before chroot
        bring-up.  `repo index` is no longer operator-visible — chroot
        build owns the side-effect.

        Skips when `repo/dists/<codename>/InRelease` already exists
        (no forced re-index; operator runs `repo repair` for that).
        Returns True on success or skip; False on auto-index failure.
        """
        _codename = str(self.config.build_codename).strip('"').strip("'")
        _inrelease = os.path.join(
            self.config.dir_repo, 'dists', _codename, 'InRelease')
        if os.path.isfile(_inrelease):
            return True
        console.print(
            "Local InRelease missing — auto-indexing repo "
            "(folded `repo index full`)…", tui.COLOR_INFO)
        if not self.cmd_index_repo():
            console.print(
                "Auto-index failed (see log).  Use `repo audit` to "
                "diagnose.", tui.COLOR_ERROR)
            return False
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

        OPTION A (2026-06-08): tunnel the source's FULL declared binary
        set, filtered by arch + active build PROFILES — the SAME gates
        virtual_build uses to predict — NOT just the dep-closure subset.
        A built source emits (and we keep) its whole binary set; a
        tunneled source must contribute the same complete set, so e.g.
        every firmware-nonfree blob lands in /cdrom/pool (not only the
        few the installed system happens to need) and the `.vbuildlog`
        prediction matches the on-disk reality.  Binaries outside the
        closure resolve against the FULL cache universe, not just
        selected_pkgs.  Falls back to the closure subset when the cache
        isn't loaded yet (tunnel still works pre-`cache parse`).
        """
        import virtual_build as _vb
        _cache = getattr(self, 'cache', None)
        _src = None
        if _cache is not None:
            _cands = _cache.source_hashtable.get(src_name, [])
            _src = _cands[0] if _cands else None
        if _src is None:
            return self._tunnel_filenames_subset(src_name)
        _pl_idx = _vb._package_list_index(
            _src, fork_dsc_dir=getattr(
                self.config, 'dir_fork_source_repo', None))
        _profiles = frozenset(
            getattr(self.config, 'build_profiles', frozenset()))
        _arch = self.config.arch
        _actual: 'list[str]' = []
        _seen: 'set[str]' = set()
        for _bin in (getattr(_src, 'binary', []) or []):
            _entry = _pl_idx.get(_bin, _bin)
            if not _vb._binary_active_for_arch(_entry, _arch):
                continue
            if not _vb._binary_active_under_profiles(_entry, _profiles):
                continue
            _fn = self._resolve_tunnel_filename(_bin, _entry)
            if not _fn:
                logger.warning(
                    f"tunnel: binary {_bin!r} of source {src_name!r} "
                    f"unresolvable in cache — skipped")
                continue
            if _fn not in _seen:
                _seen.add(_fn)
                _actual.append(_fn)
        return _actual

    def _tunnel_filenames_subset(self, src_name: str) -> 'list[str]':
        """Pre-Option-A behaviour: upstream filenames for the dep-closure
        binaries only.  Fallback path when the cache isn't loaded."""
        _actual: 'list[str]' = []
        for _f in self._predicted_files_for_source(src_name):
            _bin_name = _f.split('_', 1)[0]
            _pkg = None
            if self.dep_tree is not None:
                _pkg = self.dep_tree.selected_pkgs.get(_bin_name)
            if _pkg is None and self.udeb_dep_tree is not None:
                _pkg = self.udeb_dep_tree.selected_pkgs.get(_bin_name)
            if _pkg is None:
                _actual.append(_f)
                continue
            _fn = (_pkg.get('Filename') or '').rsplit('/', 1)[-1]
            _actual.append(_fn or _f)
        return _actual

    def _resolve_tunnel_filename(self, bin_name: str,
                                pl_entry: str) -> str:
        """Upstream Filename basename for one binary.  selected_pkgs
        (closure — already version-resolved) first; otherwise the full
        cache universe (the extra non-closure binaries Option A adds),
        picking the highest-version record.  '' when unresolvable.

        The Package-List type token (`deb`/`udeb`) routes the cache
        lookup to the right table so a udeb resolves against the udeb
        universe, not the deb one."""
        _pkg = None
        if self.dep_tree is not None:
            _pkg = self.dep_tree.selected_pkgs.get(bin_name)
        if _pkg is None and self.udeb_dep_tree is not None:
            _pkg = self.udeb_dep_tree.selected_pkgs.get(bin_name)
        _cache = getattr(self, 'cache', None)
        if _pkg is None and _cache is not None:
            _tokens = pl_entry.split()
            _is_udeb = len(_tokens) >= 2 and _tokens[1] == 'udeb'
            _view = _cache.udeb_view() if _is_udeb else _cache
            _best = None
            for _rec in _view.get_packages(bin_name):
                if (_best is None or apt_pkg.version_compare(
                        str(_rec.get('Version') or '0'),
                        str(_best.get('Version') or '0')) > 0):
                    _best = _rec
            _pkg = _best
        if _pkg is None:
            return ''
        return (_pkg.get('Filename') or '').rsplit('/', 1)[-1]

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

    # --------------------------------------Group dispatchers-------------------------------------
    # Each top-level command is a noun (cache, dep, patch, source, package,
    # container, chroot, iso, key); the second token is the verb.  Mirrors
    # the existing `print <category>` pattern.  The actual implementations
    # remain on cmd_<old_name> methods unchanged — these are thin forwarders.

    def _group_help(self, group: str, table: dict, unknown: str = '') -> None:
        """Print the action table for a command group.

        `unknown` is whatever the operator typed as the subcommand: empty
        when they ran the bare group, a recognised help token (`help`,
        `?`, `-h`, `--help`) when they asked for help explicitly, or an
        actual mis-typed action when they got it wrong.  Only the third
        case emits the "Unknown action" warning.

        The action table is printed one row per action; the previous
        single-line summary (`group: a | b | c | ...`) was dropped after
        it started hard-wrapping mid-token on wide command groups like
        `mirror` (12 actions).  The per-row table is the canonical
        reference; nothing is lost.
        """
        _help_tokens = {'', 'help', '?', '-h', '--help'}
        if unknown and unknown not in _help_tokens:
            console.print(f"Unknown {group} action: {unknown!r}")
        console.print(f"{group} — actions:")
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
            'tunnel':   'pull prebuilt .debs from upstream Debian repo '
                        'for packages we do NOT build from source: '
                        '`source tunnel [pkg…]`.  Records a tunneled '
                        'build.json claim so the package round-trips '
                        'through `mirror publish` like any built .deb.',
            'audit':    'READ-ONLY: report build-state of every source — '
                        'ok / needs_sync / needs_build / stale_pass / '
                        'interrupted / tunneled.  Add `summary` for terse '
                        'count + subset breakdown.  Add `verbose` to list '
                        'names in tunneled / fail / no_pkgs buckets too.',
            'repair':   'MUTATOR: clear stale_pass / interrupted build '
                        'records so next `source build` rebuilds.',
            'fork':     'manage fork packages: `source fork <pkg>` '
                        'creates or reloads; `source fork <pkg> '
                        'enabled|disabled` toggles the .disabled marker',
        }
        if action == 'sync':
            return self.cmd_source_sync(*args)
        if action == 'build':
            return self.cmd_source_build(*args)
        if action == 'tunnel':
            return self.cmd_tunnel_package(*args)
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
        if action == 'container':
            return self.cmd_container_purge(*args)
        if action == 'all':
            return self.cmd_clean_all(*args)
        return self._group_help('clean', _table, action)

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
        import repo_audit as _ra
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
        _asg_ledger = _ra.published_ledger(self.config) or {}
        _universe = _vb.from_cache(self.cache)
        # Canonical-source map: binary_name -> upstream `Source:` (the REAL
        # producer), built from the deb + udeb indices.  Lets validate
        # attribute on-disk emissions to their true source rather than to
        # whichever source declares them in `Binary:` — fixes the
        # linux / linux-signed-amd64 installer-udeb attribution split.
        import apt_pkg as _ap
        _canon_map: 'dict[str, str]' = {}
        for _table in (getattr(self.cache, 'package_hashtable', {}),
                       getattr(self.cache, 'udeb_hashtable', {})):
            for _bn, _vers in _table.items():
                _best_v = _best_r = None
                for _v, _rec in (_vers.items()
                                 if hasattr(_vers, 'items') else []):
                    _r = _rec[0] if isinstance(_rec, list) else _rec
                    if (_best_v is None
                            or _ap.version_compare(str(_v), str(_best_v)) > 0):
                        _best_v, _best_r = _v, _r
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
            package_universe=_universe, asg_ledger=_asg_ledger,
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
        """OBS-04 companion: write `log/build/<pkg>.vbuildlog` — the virtual
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
          3. source build — virtual (compute_post_build_versions math
             + per-binary upstream-inherit + sibling pin rewrite)
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
        import repo_audit as _ra
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
        _asg_ledger = _ra.published_ledger(self.config) or {}
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
                asg_ledger=_asg_ledger, release=_release,
                arch=_arch, was_patched=_was_patched,
                peer_sources=set(_scope_names),
                active_profiles=frozenset(
                    getattr(self.config, 'build_profiles', frozenset())),
                fork_dsc_dir=getattr(
                    self.config, 'dir_fork_source_repo', None),
            )
            _records.extend(_src_records)
            # OBS-04 companion: persist the PREDICTED artifact set as
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
        _install_corpus: 'frozenset[str]' = frozenset()
        if self.dep_tree is not None:
            _install_corpus |= frozenset(
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
        console.print("\nvirtual publish dry-run:", tui.COLOR_HIGHLIGHT)
        if not _remote_by_builder:
            console.print(
                "  WARNING  no cached remote state — run `mirror pull` "
                "first for ownership / cross-builder checks.  Continuing "
                "with intra-our-claims hash-conflict scan only.",
                tui.COLOR_WARNING)
        _merged, _pub_findings = _vb.virtual_publish_dry_run(
            _records, our_builder_id=_our_bid, snapshot=_snapshot,
            remote_by_builder=(_remote_by_builder or None),
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

    def cmd_sbom(self, *args):
        """CONF-07: emit a CycloneDX 1.5 JSON Software Bill of Materials.

        Walks dep_tree.selected_srcs (∪ udeb_dep_tree.selected_srcs)
        and writes one component per source — name + version + DSC
        sha256 + patch-set hash + PURL (`pkg:deb/<base-id>/<name>@
        <version>`).  Top-level metadata records distribution +
        version + codename + arch + snapshot timestamp.

        Usage:
          sbom               — write to <dir_image>/<distro-version-
                                snapshot-arch>.cdx.json (next to ISOs)
          sbom <path>        — write to the given path

        Requires a parsed dep tree; run `cache parse` first.
        """
        if not self.flags.dep_check_ready:
            console.print(
                "sbom: dep tree not built — run `cache parse` first"
            )
            return
        if self.dep_tree is None:
            console.print(
                "sbom: dep_tree is None even though dep_check_ready is set "
                "— this should not happen; rerun `cache parse`"
            )
            return

        import sbom as _sbom_mod
        if args:
            _out = args[0]
        else:
            _snap = utils.snapshot_iso_tag(self.config)
            _distro = str(self.config.build_distribution).strip(
                '"').strip("'").lower()
            _version = str(self.config.build_version).strip(
                '"').strip("'")
            _arch = self.config.arch
            _basename = (
                f"{_distro}-{_version}-{_snap}-{_arch}"
                if _snap
                else f"{_distro}-{_version}-{_arch}"
            )
            _out = os.path.join(
                self.config.dir_image, f"{_basename}.cdx.json",
            )

        _path = _sbom_mod.generate_cdx(
            self.config,
            self.dep_tree,
            udeb_dep_tree=self.udeb_dep_tree,
            out_path=_out,
            container=self.container,
        )
        if not _path:
            console.print("ERROR: sbom generation failed — see log")
            return

        try:
            _size_kb = os.path.getsize(_path) // 1024
        except OSError:
            _size_kb = 0
        _n = len(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            # Same dedup the generator applies.
            _udeb_only = set(self.udeb_dep_tree.selected_srcs.keys()) - set(
                self.dep_tree.selected_srcs.keys())
            _n += len(_udeb_only)
        console.print(
            f"sbom: {_path} ({_size_kb} KB, {_n} component(s))",
            tui.COLOR_HIGHLIGHT,
        )

    def cmd_cve(self, *args):
        """CVE-01: scan the latest SBOM against Grype's vulnerability
        databases (NVD + GHSA + Debian Security Tracker).

        Reads a CycloneDX 1.5 JSON SBOM (produced by `sbom`) and
        delegates to grype for the actual lookup.  Renders a severity
        summary on the console + writes the full JSON report next to
        the SBOM.

        Usage:
          cve               — scan the most recent .cdx.json in dir_image
          cve <path>        — scan the given SBOM path

        Grype is an OPTIONAL prerequisite (warned at startup).  When
        absent this command prints install instructions + returns
        without scanning.

        Note: scans the SBOM, NOT the live dpkg DB.  Live-system
        scanning produces false positives for our NMU-stripped
        binaries (see docs/cve-tracking.md).  The
        X-Athena-Upstream-Version field on each .deb preserves the
        upstream version for future custom matchers; until then,
        SBOM-side scanning is the source of truth.
        """
        import shlex as _shlex
        import shutil as _shutil
        import subprocess as _subprocess
        from collections import Counter as _Counter

        _grype = _shutil.which('grype')
        if not _grype:
            console.print(
                "cve: `grype` not on PATH — install via Anchore's "
                "static binary release: "
                "curl -sSfL https://raw.githubusercontent.com/anchore/grype/"
                "main/install.sh | sudo sh -s -- -b /usr/local/bin",
                tui.COLOR_WARNING,
            )
            console.print(
                "Once installed, re-run `cve [path]` to scan the SBOM.",
                tui.COLOR_INFO,
            )
            return

        # Resolve SBOM path.
        if args:
            _sbom_path = args[0]
        else:
            try:
                _candidates = sorted(
                    (_f for _f in os.listdir(self.config.dir_image)
                     if _f.endswith('.cdx.json')),
                    key=lambda _f: os.path.getmtime(
                        os.path.join(self.config.dir_image, _f)),
                    reverse=True,
                )
            except OSError as _e:
                console.print(f"cve: cannot list {self.config.dir_image}: {_e}")
                return
            if not _candidates:
                console.print(
                    f"cve: no .cdx.json found in {self.config.dir_image} — "
                    f"run `sbom` first"
                )
                return
            _sbom_path = os.path.join(self.config.dir_image, _candidates[0])
            console.print(f"cve: using {_candidates[0]} (most recent SBOM)")

        if not os.path.isfile(_sbom_path):
            console.print(f"cve: SBOM not found: {_sbom_path}")
            return

        _report_path = _sbom_path.replace('.cdx.json', '.cve.json')
        _cmd = [_grype, f'sbom:{_sbom_path}', '-o', 'json']
        logger.info(f"cve: {' '.join(_shlex.quote(_p) for _p in _cmd)}")

        # grype's first run downloads the vuln DB (~30s); spinner so
        # the operator sees progress.
        _spin = tui.Spinner(
            f"grype scan {os.path.basename(_sbom_path)}"
        )
        try:
            _r = _subprocess.run(_cmd, capture_output=True, text=True)
        finally:
            _spin.done()
        if _r.returncode != 0:
            console.print(
                f"cve: grype exited {_r.returncode}: "
                f"{_r.stderr.strip()[:200]}",
                tui.COLOR_ERROR,
            )
            logger.error(
                f"cve: grype stderr_tail={_r.stderr.strip().splitlines()[-5:]}"
            )
            return

        try:
            import json as _json
            _doc = _json.loads(_r.stdout)
        except (ValueError, TypeError) as _e:
            console.print(f"cve: grype output not JSON-parseable: {_e}")
            return

        # Persist full report.
        try:
            with open(_report_path, 'w', encoding='utf-8') as _fh:
                _fh.write(_r.stdout)
        except OSError as _e:
            logger.warning(f"cve: could not write report sidecar {_report_path}: {_e}")

        # Render severity summary on console.
        _matches = _doc.get('matches', []) or []
        if not _matches:
            console.print(
                "cve: clean — no vulnerabilities reported",
                tui.COLOR_HIGHLIGHT,
            )
            console.print(f"cve: report → {_report_path}")
            return

        _by_sev: _Counter = _Counter()
        for _m in _matches:
            _sev = (_m.get('vulnerability', {}) or {}).get('severity', 'Unknown')
            _by_sev[_sev] += 1

        console.print(
            f"cve: {len(_matches)} finding(s) across "
            f"{len({_m.get('artifact', {}).get('name', '') for _m in _matches})}"
            f" component(s)",
            tui.COLOR_WARNING,
        )
        for _sev in ('Critical', 'High', 'Medium', 'Low', 'Negligible', 'Unknown'):
            if _by_sev.get(_sev, 0):
                console.print(f"  {_sev:11s} {_by_sev[_sev]}")

        # Show the top critical/high findings so the operator has
        # actionable context without leaving the TUI.
        _top = [
            _m for _m in _matches
            if (_m.get('vulnerability', {}) or {}).get('severity')
            in ('Critical', 'High')
        ]
        if _top:
            console.print(f"\nTop {min(10, len(_top))} Critical/High:")
            for _m in _top[:10]:
                _vuln = _m.get('vulnerability', {}) or {}
                _art = _m.get('artifact', {}) or {}
                _fix = (_vuln.get('fix') or {}).get('versions') or []
                _fix_str = (', '.join(_fix) if _fix else '—')
                console.print(
                    f"  [{_vuln.get('severity', '?'):8s}] "
                    f"{_vuln.get('id', '?'):16s} "
                    f"{_art.get('name', '?')}@{_art.get('version', '?')}  "
                    f"fix: {_fix_str}"
                )
        console.print(f"\ncve: report → {_report_path}")

    # ─────────────────────────────────────────────────────────────────
    # set / get — session-local config parameter manipulation
    # ─────────────────────────────────────────────────────────────────

    def _set_mode(self, value: str) -> None:
        """`set mode <distribution|build>` — switch build mode in
        the running session.  Clears dep_check_ready so the next
        pipeline step re-resolves under the new mode; does NOT
        persist to build.conf (operator commits the change explicitly
        if they want it durable)."""
        _valid = ('distribution', 'build')
        if value not in _valid:
            console.print(
                f"  invalid mode: {value!r}  (try: {' | '.join(_valid)})",
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
        console.print(
            f"  mode  {_prev}  →  {value}  (session-local, "
            "build.conf unchanged)", tui.COLOR_HIGHLIGHT)
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

    _SETTABLE: 'dict[str, Callable]' = {}    # populated below
    _GETTABLE: 'dict[str, Callable]' = {}

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
            'disk':       'cache→parse→download→container→source build (+live)→chroot build live→iso build disk (qcow2)',
            'build': 'cache→parse→download→container→source build (indl) — STOPS at source_build_ready (no chroot/ISO)',
        }
        # MIRROR-02: in [Build] Mode = build, bare `autorun`
        # routes to the build pipeline (the live/installer/disk
        # variants would refuse at their chroot/ISO steps anyway).
        # Defensive against missing .config (test doubles).
        _mode = getattr(getattr(self, 'config', None), 'build_mode',
                        'distribution')
        if action == '' and _mode == 'build':
            return self.cmd_auto_run_build(*args)
        if action in ('', 'live'):
            return self.cmd_auto_run_live(*args)
        if action == 'installer':
            return self.cmd_auto_run_installer(*args)
        if action == 'disk':
            return self.cmd_auto_run_disk(*args)
        if action == 'build':
            return self.cmd_auto_run_build(*args)
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

    def cmd_auto_run_build(self):
        """MIRROR-02: run the build-mode pipeline through to a complete
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
            (self.cmd_init_container,    'build_container_ready', 'container init'),
            (lambda: self.cmd_source_build('build'),
                                          'source_build_ready',    'source build indl'),
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
        # MIRROR-02: surface the build mode at the top of the autorun
        # run so the operator can never mistake a 5-step indl chain
        # for a broken 8-step live chain.
        _mode = getattr(self.config, 'build_mode', 'distribution')
        console.print(
            f"{label}: starting (MODE = {_mode})", tui.COLOR_HIGHLIGHT)
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


# Late-bind the set/get registries to the methods declared on the class.
# Kept here so the methods are unambiguously bound before BuildSession is
# instantiated.  Adding a new param: drop a `_set_X` method on BuildSession
# and add it here.
BuildSession._SETTABLE = {
    'mode':                BuildSession._set_mode,
    'include-recommends':  BuildSession._set_include_recommends,
}
BuildSession._GETTABLE = {
    'mode':                lambda s: getattr(s.config, 'build_mode',
                                              'distribution'),
    'include-recommends':  lambda s: getattr(s.config,
                                              'include_recommends', False),
    'build-version':       lambda s: getattr(s.config, 'build_version', '?'),
    'codename':            lambda s: getattr(s.config, 'build_codename', '?'),
    'distribution':        lambda s: getattr(s.config,
                                              'build_distribution', '?'),
    'arch':                lambda s: getattr(s.config, 'arch', '?'),
    'snapshot':            lambda s: (
        getattr(s.config, 'snapshot_timestamp_config', '?')
        if getattr(s.config, 'snapshot_enabled', False) else 'disabled'),
    'max-parallel-builds': lambda s: getattr(s.config,
                                              'max_parallel_builds', 1),
}


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

    # API-01: `--api [--api-port N]` starts the FastAPI server as the
    # session's frontend (third backend besides Tui/Cli).  Commands
    # arrive via POST /api/v1/command and run on the main thread's job
    # loop — single-writer preserved.  Binds 127.0.0.1 only; see
    # docs/plans/api-01-web-api.md for the exposure model.
    _api = '--api' in sys.argv
    if _api:
        sys.argv.remove('--api')
    _api_port = 8765
    if '--api-port' in sys.argv:
        _i = sys.argv.index('--api-port')
        try:
            _api_port = int(sys.argv[_i + 1])
            del sys.argv[_i:_i + 2]
        except (IndexError, ValueError):
            print("ERROR: --api-port needs an integer argument, Exiting...")
            sys.exit(1)

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
    if _api:
        print("Initialising API backend...")
        try:
            from webapi.jobs import ApiBackend
            tui_inst = ApiBackend()
            # ApiBackend extends Cli: registers as tui.tui_instance and
            # binds logging in __init__; wait() runs the job loop on the
            # main thread (where the REPL would sit).
            signal.signal(signal.SIGINT, tui_inst.sig_shutdown)
        except Exception as e:
            print(f"FATAL: API backend initialisation failed: {e}")
            sys.exit(1)
    elif _headless:
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

    # Persistent mode indicator on the TUI footer banner.  In build mode
    # mode the operator must never confuse a partial pipeline for a
    # broken dist build; the footer carries `[build]` for every screen.
    if (not _headless
            and getattr(config, 'build_mode', 'distribution') == 'build'
            and hasattr(tui_inst, 'dispatcher')):
        try:
            tui_inst.dispatcher.state.banner = (
                f"{banner} [build]")[:50]
        except AttributeError:
            pass

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
        console.print(f"WARNING: could not open run log ({e}); "
                      f"continuing without file logging", tui.COLOR_WARNING)

    session = BuildSession(config, tui_inst)

    # Tips are constrained: 4 (indent) + 14 (name pad in `help`) +
    # tip ≤ 80 cols.  Tips that need to enumerate many sub-actions
    # use `<subcmd>` and direct the operator at `<cmd>` (bare) which
    # prints the full per-action table via _group_help.
    tui.register_command('cache',     session.cmd_cache,     'Cache:      \tcache <build|purge|parse|select|info <pkg>>')
    tui.register_command('clean',     session.cmd_clean,     'Clean:      \tclean <subcmd> — run `clean` for the list')
    tui.register_command('patch',     session.cmd_patch,     'Patches:    \tpatch refresh')
    tui.register_command('source',    session.cmd_source,    'Sources:    \tsource <sync|build|audit|repair|fork>')
    tui.register_command('repo',      session.cmd_repo,      'Repo:       \trepo <index|publish|audit|repair|tunnel|refresh>')
    tui.register_command('snapshot',  session.cmd_snapshot,  'Snapshot:   \tsnapshot <list|advance|workload|base>')
    tui.register_command('container', session.cmd_container, 'Container:  \tcontainer <init|purge>')
    tui.register_command('chroot',    session.cmd_chroot,    'Chroot:     \tchroot build [live|installer] | chroot verify')
    tui.register_command('iso',       session.cmd_iso,       'ISO:        \tiso build <live|installer>')
    tui.register_command('key',       session.cmd_key,       'Signing:    \tkey <generate|verify>')
    tui.register_command('mirror',    session.cmd_mirror,    'Mirror:     \tmirror <init|add|remove|list|summary|status|publish|pull|audit|query|builders|conflict|reconcile-neighbours>')
    tui.register_command('virtual',   session.cmd_virtual,   'Virtual:    \tvirtual build [scope] — dry-run pipeline simulation')
    tui.register_command('sbom',      session.cmd_sbom,      'SBOM:       \tsbom [path] — emit CycloneDX 1.5 JSON')
    tui.register_command('cve',       session.cmd_cve,       'CVE:        \tcve [path] — scan latest SBOM via grype (optional)')
    tui.register_command('autorun',   session.cmd_auto_run,  'Autorun:    \tautorun [live|installer]')
    tui.register_command('set',       session.cmd_set,       'Set:        \tset <param> <value> — session-local config change')
    tui.register_command('get',       session.cmd_get,       'Get:        \tget [param] — show current config value(s)')
    tui.register_command('print',     session.cmd_print,     'Print:      \tprint build state — try `print help`')

    console.print(asciiart_logo, tui.COLOR_ERROR)
    console.print("Starting Athena Build System...", tui.COLOR_HIGHLIGHT)
    console.print(f"\tArch\t\t\t{config.arch}")
    console.print(f"\tParent Distribution\t{config.release} {config.baseversion}")
    console.print(f"\tBuild Distribution\t{config.build_distribution} {config.build_version} ({config.build_codename})")
    _mode = getattr(config, 'build_mode', 'distribution')
    _mode_color = (tui.COLOR_HIGHLIGHT if _mode == 'build'
                   else tui.COLOR_INFO)
    if _mode == 'build':
        _build_pkg_names = utils.parse_build_pkg_list(
            getattr(config, 'build_pkg_list_path', '') or '')
        console.print(
            f"\tMode\t\t\tbuild  [{len(_build_pkg_names)} pkg(s) in build_pkg.list]",
            _mode_color)
    else:
        console.print("\tMode\t\t\tdistribution", _mode_color)

    # API-01: with the session + every command registered, raise the
    # HTTP server (daemon thread) and hand the main thread to the job
    # loop.  uvicorn only touches the queue; jobs execute HERE.
    if _api:
        try:
            import uvicorn  # type: ignore[import-not-found]
            import webapi
        except ImportError:
            from webapi import APT_HINT as _hint
            print(f"FATAL: {_hint}")
            sys.exit(1)
        try:
            _app = webapi.create_app(
                buildlog_dir=os.path.join(config.dir_log, 'build'),
                flags_path=os.path.join(config.dir_cache,
                                        'buildflags.json'),
                api_key_path=os.path.join(config.working_dir, 'config',
                                          'api.key'),
                conf_path=os.path.join(config.working_dir, 'config',
                                       'build.conf'),
                repo_dir=config.dir_repo,
                config_dir=os.path.join(config.working_dir, 'config'),
                coord_dir=getattr(config, 'dir_coord',
                                  os.path.join(config.working_dir,
                                               'coord')),
                backend=tui_inst,
            )
        except RuntimeError as e:
            print(f"FATAL: {e}")
            sys.exit(1)
        _server = uvicorn.Server(uvicorn.Config(
            _app, host='127.0.0.1', port=_api_port, log_level='warning'))
        threading.Thread(target=_server.run, daemon=True,
                         name='webapi-uvicorn').start()
        print(f"API listening on http://127.0.0.1:{_api_port} "
              f"(docs: /docs; key: config/api.key)")

    tui_inst.wait()
    Exit(0)


if __name__ == '__main__':
    build_banner = "Athena Build System v0.1"
    print(asciiart_logo)
    main(build_banner)
