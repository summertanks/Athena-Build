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
from commands.cohorts import CohortResolverMixin
from commands.cmd_audit import AuditCommandsMixin
from commands.cmd_build import BuildCommandsMixin
from commands.cmd_cache import CacheCommandsMixin
from commands.cmd_mirror import MirrorCommandsMixin
from commands.cmd_run import ConfigRunCommandsMixin
from commands.cmd_repo import RepoCommandsMixin
from commands.cmd_snapshot import SnapshotCommandsMixin
from commands.cmd_source import SourceCommandsMixin
from commands.cmd_supply_chain import SupplyChainCommandsMixin
from commands.cmd_tunnel import TunnelCommandsMixin
from commands.cmd_virtual import VirtualCommandsMixin

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
    chroot_disk_ready:       bool
    iso_live_ready:          bool
    iso_installer_ready:     bool
    iso_disk_ready:          bool
    _save_path:              'Optional[str]'

    _FIELDS = (
        'cache_ready', 'dep_check_ready', 'download_ready',
        'build_container_ready', 'source_build_ready',
        'signing_key_verified',
        'chroot_ready', 'chroot_verified', 'chroot_installer_ready',
        'chroot_disk_ready',
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


class BuildSession(AuditCommandsMixin, BuildCommandsMixin, CacheCommandsMixin,
                   CohortResolverMixin, ConfigRunCommandsMixin, MirrorCommandsMixin,
                   RepoCommandsMixin, SnapshotCommandsMixin, SourceCommandsMixin,
                   SupplyChainCommandsMixin, TunnelCommandsMixin,
                   VirtualCommandsMixin):
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
        # BuildFlags.load reads buildflags.json (created on every
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

    # --------------------------------------clean dispatchers---------------------------------
    # Each `clean <X>` wipes the working dir for stage X and resets the
    # corresponding BuildFlags + drops in-memory state pointing at the
    # deleted files.  Sudo for buildroot/* (root-owned chroot content);
    # plain Python for user-owned dirs (cache/, source/, repo/, image/,
    # download/).  `force` arg skips the YESNO prompt — used by `clean
    # all` to consolidate confirmation into a single up-front prompt.

    def _wipe_dir_contents(self, label: str, path: str,
                           sudo: bool,
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

    def _refresh_sudo(self, password: str, context: str = 'clean') -> bool:
        """Validate the sudo password without consuming any other input.
        Returns True iff `sudo -v` succeeds.  Used by clean handlers
        that need root before doing the actual `sudo find ... -exec rm`,
        and by `_collect_validated_sudo_password` (ARCH-19) — the single
        `sudo -v` subprocess in the codebase.  `context` tags the log line.
        """
        _r = subprocess.run(['sudo', '-S', '-v'],
            input=password + '\n', capture_output=True, text=True)
        if _r.returncode != 0:
            console.print("ERROR: incorrect sudo password")
            logger.error(f"{context}: sudo -v failed")
            return False
        return True

    def _collect_validated_sudo_password(self, context: str = 'sudo') -> 'Optional[str]':
        """ARCH-19: prompt for a sudo password and validate it via
        `_refresh_sudo` (`sudo -v`).  Returns the validated password, or
        None when the credential is rejected — in which case the local
        copy is scrubbed and the caller must return.  Every command
        handler that gates privileged work funnels through here so the
        prompt -> validate -> scrub sequence lives in exactly one place.
        """
        _password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()
        if not self._refresh_sudo(_password, context):
            _password = '*' * len(_password)
            return None
        return _password

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
        _ok_disk = self._wipe_dir_contents(
            'buildroot/disk', self.config.dir_chroot_disk,
            sudo=True, password=_password, skip_prompt=_force)
        if _ok_live:
            self.flags.chroot_ready = False
            self.flags.chroot_verified = False
        if _ok_inst:
            self.flags.chroot_installer_ready = False
        if _ok_disk:
            self.flags.chroot_disk_ready = False

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
                "image/, buildroot/{live,installer,disk}, and athenalinux "
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
        # the disk chroot (its own minimal SURFACES-01 root) is
        # root-owned too — wipe it, else buildroot/disk survives while the
        # flag reset below clears chroot_disk_ready (claiming it's gone).
        self._wipe_dir_contents('buildroot/disk',
            self.config.dir_chroot_disk, sudo=True, password=_password, skip_prompt=True)
        # Docker side: kills running athenalinux containers + removes
        # images so next `container init` rebuilds from Dockerfile.
        self.cmd_container_purge('force')

        # Drop in-memory state and reset every flag.  cache_ready and
        # dep_check_ready already cleared by cmd_cache_purge — explicit
        # here so the operator-visible end-state is "everything zero".
        self.cache = None
        self.dep_tree = None
        self.udeb_dep_tree = None
        # keep autosave wiring so the wipe is reflected on disk.
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
            informational=True,   # signing-key one-time setup, OK under --yes
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
        except (RuntimeError,
                buildcontainer.docker.errors.DockerException) as e:
            # connect failures are wrapped in RuntimeError, but a
            # later init step (image build) can raise a bare DockerException
            # — catch the base too so the operator sees this message instead
            # of a raw traceback.
            spin.done()
            console.print(f"  ERROR: build container initialisation failed — {e}")
            logger.error(f"BuildContainer() raised: {e}")


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
            informational=True,   # pre-chroot gate, OK under --yes
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
            'select': 'toggle packages in pkg.list (curses); `select accept` re-baselines a shrink',
            'info':   'show concise info + relations for a package: cache info <pkg>',
            'restore':     'regenerate the list files from the signed selection.state',
            'purge-state': 'delete selection.state to re-baseline the selection (heavy mirror impact)',
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
        if action == 'restore':
            return self.cmd_cache_restore(*args)
        if action == 'purge-state':
            return self.cmd_cache_purge_state(*args)
        return self._group_help('cache', _table, action)

    def cmd_patch(self, action: str = '', *args):
        _table = {'refresh': 're-scan patch/source/ tree (after out-of-band edits)'}
        if action == 'refresh':
            return self.cmd_patch_refresh(*args)
        return self._group_help('patch', _table, action)

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
            'build [live]':    'install the [Live] Groups closure into buildroot/live (default)',
            'build installer': 'unpack udeb closure into buildroot-installer/ (no postinst configure)',
            'build disk':      'install the [Disk] Groups closure into buildroot/disk (minimal)',
            'verify':          '8-check chroot health verifier',
        }
        if action == 'build':
            # Default to live; explicit `live`/`installer`/`disk` consumes
            # the next token as the sub-action.  Anything else is treated as
            # args to the live build (preserves `chroot build with_debug`).
            if args and args[0] in ('live', 'installer', 'disk'):
                _sub = args[0]
                _rest = args[1:]
                if _sub == 'installer':
                    return self.cmd_build_chroot_installer(*_rest)
                if _sub == 'disk':
                    return self.cmd_build_chroot_disk(*_rest)
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
                               'the disk chroot (`iso build disk [size_gb]`)',
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
    # Pin the HTTP stack to IPv4 before any mirror fetch — a host with
    # unrouted SLAAC IPv6 otherwise stalls every snapshot.debian.org connect
    # on the dead v6 address (urllib3 has no Happy-Eyeballs).  ATHENA_ALLOW_IPV6=1
    # opts out.  See utils.force_ipv4_http.
    utils.force_ipv4_http()

    # B: detect --headless before BuildConfig sees argv.  Strip
    # it after detection — BuildConfig uses argparse and would error on
    # unknown flags.
    _headless = '--headless' in sys.argv
    if _headless:
        sys.argv.remove('--headless')

    # `--api [--api-port N]` starts the FastAPI server as the
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

    # --yes auto-answers informational YESNO prompts (e.g.
    # "There are source build failures, Proceed?", "Generate a new
    # signing key now?").  Hard prompts (sudo password, conflict-
    # resolution OPTIONS, security-audit gates) still wait for the
    # operator regardless.
    _auto_yes = '--yes' in sys.argv
    if _auto_yes:
        sys.argv.remove('--yes')

    # `--cmd <cmd>` queues one or more commands to run sequentially
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
    tui.register_command('repo',      session.cmd_repo,      'Repo:       \trepo <audit|repair>')
    tui.register_command('snapshot',  session.cmd_snapshot,  'Snapshot:   \tsnapshot <list|workload|select|advance|history>')
    tui.register_command('container', session.cmd_container, 'Container:  \tcontainer <init|purge>')
    tui.register_command('chroot',    session.cmd_chroot,    'Chroot:     \tchroot build [live|installer] | chroot verify')
    tui.register_command('iso',       session.cmd_iso,       'ISO:        \tiso build <live|installer>')
    tui.register_command('key',       session.cmd_key,       'Signing:    \tkey <generate|verify>')
    tui.register_command('mirror',    session.cmd_mirror,    'Mirror:     \tmirror <init|add|remove|list|summary|status|publish|pull|reclaim|audit|query|builders|conflict|reconcile-neighbours>')
    tui.register_command('virtual',   session.cmd_virtual,   'Virtual:    \tvirtual build [scope] — dry-run pipeline simulation')
    tui.register_command('sbom',      session.cmd_sbom,      'SBOM:       \tsbom [path] — emit CycloneDX 1.5 JSON')
    tui.register_command('cve',       session.cmd_cve,       'CVE:        \tcve [path] — scan latest SBOM via grype (optional)')
    tui.register_command('autorun',   session.cmd_auto_run,  'Autorun:    \tautorun [live|installer]')
    tui.register_command('build',     session.cmd_build,     'Build hist: \tbuild history [pkg] — cross-run pass/fail ledger')
    tui.register_command('set',       session.cmd_set,       'Set:        \tset <param> <value> — session-local config change')
    tui.register_command('get',       session.cmd_get,       'Get:        \tget [param] — show current config value(s)')
    tui.register_command('print',     session.cmd_print,     'Print:      \tprint build state — try `print help`')

    # Status tab (curses TUI only): a live build-environment snapshot
    # refreshed after every command.  Cli / API backends have no tabs, so
    # gate on the method's presence.
    if hasattr(tui_inst, 'set_status_provider'):
        import print_commands as _pc
        tui_inst.set_status_provider(lambda: _pc.status_lines(session))

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

    # Keep build.conf honest: if the durable snapshot.state pin (set via
    # `snapshot select`) differs from [Snapshot] Timestamp, rewrite the
    # config to match — the state pin is authoritative at build time — and
    # warn the operator that build.conf changed.  No state file → build.conf
    # stays authoritative as-is.
    _snap_recon = utils.reconcile_snapshot_pin(config)
    if _snap_recon is not None:
        _old_ts, _new_ts = _snap_recon
        console.print(
            f"WARNING: snapshot.state pin ({_new_ts}) differs from "
            f"build.conf [Snapshot] Timestamp ({_old_ts}) — updated "
            "build.conf to match.  snapshot.state (set via `snapshot "
            "select`) is the authoritative durable pin.",
            tui.COLOR_WARNING)

    # with the session + every command registered, raise the
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
    # Propagate the backend's resolved exit code to the process — 1 on a
    # one-shot (`--cmd`) failure, 130 on SIGINT.  Hardcoding 0 here made
    # every headless run report success regardless of outcome.
    Exit(getattr(tui_inst, '_exit_code', 0) or 0)


if __name__ == '__main__':
    build_banner = "Athena Build System v0.1"
    print(asciiart_logo)
    main(build_banner)
