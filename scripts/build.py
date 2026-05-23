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
import repo_audit
import signal


import logging
from tui import Tui, console, Prompt, PROMPT_YESNO, PROMPT_PASSWORD, Spinner, ProgressBar, Exit
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

    def __str__(self) -> str:
        """Return a compact one-line status string for display in the TUI."""
        fields = ['cache_ready', 'dep_check_ready', 'download_ready',
                  'build_container_ready', 'source_build_ready',
                  'signing_key_verified',
                  'chroot_ready', 'chroot_verified', 'chroot_installer_ready',
                  'iso_live_ready', 'iso_installer_ready']
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
        the next `source download` re-fetches.  Source bytes are
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
        source download and source build know what to fetch and build.  Both
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
                "dep parse: already complete — pass `force` to re-resolve, "
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

        # parse_sources for the udeb tree too.  Sources are universal
        # (same source produces both .deb and .udeb), so the shared
        # source_hashtable already has the records we need.
        # udeb_dep_tree.selected_srcs is populated independently;
        # downstream consumers (source download / source build) will
        # iterate over the UNION of both trees' selected_srcs.
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
        # Read-only commands (cmd_source_rescan, cmd_print*, anything
        # whose name suggests "show / scan / status") are pinned by
        # test_readonly_named_commands_have_no_destructive_calls() to
        # never reach here.
        #
        # Discovered the hard way 2026-05-19: cmd_source_rescan called
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
        console.print(f"  Created     : {signing.format_gpg_time(_info['created'])}")
        console.print(f"  Expires     : {signing.format_gpg_time(_info['expires'], '(never — manual rotation)')}")

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

        Downloads from BOTH the deb tree AND the udeb
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
        _files = self._predicted_files_for_source(src_pkg.package)
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
        _success = True

        # Caller gates this on build_container_ready, so self.container
        # is non-None by the time we get here.
        assert self.container is not None
        for _filename in _files:
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
        run `dep parse`).  Caller should fall back to a coarser check
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
        cmd_source_audit, cmd_source_repair, cmd_source_verify,
        cmd_source_rescan, _do_tunnel.  Pulled here because the two
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
        run `dep parse`); caller falls back to whole-repo audit with
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
        cohort check is skipped with a hint to run `dep parse`.  Dep
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
        """Single repo audit covering all three install-correctness gates,
        scoped to repo/main (the installable subdir).

          DEP GATE (hard) — every pkg in repo/main MUST have its hard
            Depends/Pre-Depends satisfiable in repo/main.  Resolution
            honours Provides per Debian Policy §7.5.

          LIVE CONFLICTS (hard) — within the live chroot install set
            (dep_tree.selected_pkgs − pool_extras − installer_exclusive),
            no two pkgs may co-install with mutual Conflicts/Breaks.
            Self-conflict-via-Provides is filtered.

          INSTALLER CONFLICTS (hard) — same shape, cohort is the d-i
            ramdisk udebs (udeb_dep_tree.selected_pkgs).

        Conflicts outside a cohort (e.g. grub-pc in pool conflicting
        with grub-efi-amd64 in live) are NOT flagged — apt arbitrates.

        Per-target drill-in mode: pass a target package name as the
        first non-flag argument.  Shows full state for that target
        (cache + dep_tree + repo/main + Provides + consumers).

        Gap classification: when the dep gate finds any unresolved
        target, each is classified as build_failed / missed_by_parse /
        transitional / other (formerly the `audit_gap` command —
        merged in here for a single audit interface).

        Usage:
          package audit                    — full overview
          package audit <target>           — drill into one target
          package audit verbose            — full lists, all categories
          package audit strict             — also list Recommends
          package audit refresh            — force re-scan
        """
        _verbose = 'verbose' in args
        _strict = 'strict' in args
        _refresh = 'refresh' in args
        # Anything other than known flags is treated as a drill-in target.
        _flags = {'verbose', 'strict', 'refresh'}
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
                "wide dep gate.  Run `dep parse` first to scope by cohort."
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
                "run `dep parse` first"
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
                "not built; run `dep parse` first"
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
        # the categorisation `repo cleanup` uses, without the deletion
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
                "run `dep parse` first"
            )

    def _report_stale_files_warning(self, *, verbose: bool) -> None:
        """Soft-warning STALE FILES section for package audit.

        Lists counts (and a short preview) of orphan-source and
        version-drift residue under repo/.  Doesn't delete — the
        operator runs `repo cleanup` when they want to act.
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
            # detail lives in `repo cleanup` (dry-run).
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
                "  Run `repo cleanup` to review/remove (dry-run by "
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
                "`dep parse` first for full drill-in"
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

    def cmd_audit_nmu(self, *args):
        """Walk repo/ for residual NMU/binNMU/backport suffixes.

        After enabling post-build strip in BuildContainer, fresh builds
        enter repo/ already normalised (Version + every dep constraint
        at pristine source version).  This command verifies that:
          - Every .deb/.udeb's own Version field is stripped
          - Every version constraint in Depends/Pre-Depends/Recommends/
            Suggests/Enhances/Provides/Conflicts/Breaks/Replaces is
            stripped

        Anything reported here means a .deb in repo/ slipped the
        normaliser (manually staged, ingested without going through
        BuildContainer.build, OR a regression in the post-build hook).

        Usage: package audit_nmu [verbose] [refresh]
        """
        _verbose = 'verbose' in args
        _refresh = 'refresh' in args
        _state = repo_audit.scan_repo_state(self.config, refresh=_refresh)
        if _state is None:
            return
        if not _state.packages:
            console.print("repo/ has no .deb/.udeb files — nothing to audit")
            return
        _findings = repo_audit.audit_nmu_residue(_state)
        if not _findings:
            console.print(
                f"NMU audit OK: {len(_state.packages)} pkgs scanned, "
                f"no residue."
            )
            return
        # Group by field for a quick top-line, then list.
        from collections import Counter
        _by_field = Counter(f[1] for f in _findings)
        console.print(
            f"NMU audit: {len(_findings)} residue(s) across "
            f"{len(set(f[0] for f in _findings))} pkg(s).\n"
            f"  Broken down by field: " +
            ', '.join(f'{_k}={_v}' for _k, _v in _by_field.most_common())
        )
        _show = len(_findings) if _verbose else min(30, len(_findings))
        for _pkg, _field, _raw, _why in _findings[:_show]:
            console.print(f"  {_pkg}  {_field}: {_raw}")
        if not _verbose and len(_findings) > _show:
            console.print(
                f"  … and {len(_findings) - _show} more "
                f"(run `repo audit_nmu verbose` for full list)"
            )
        console.print(
            "Fix: `repo strip` re-applies the strip to every "
            "non-conforming .deb in repo/."
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

        REQUIRES Stage C (`repo migrate_layout`) to have run
        first — Stage C is what creates the
        dists/<suite>/<comp>/binary-<arch>/ directories with .debs at
        their new paths.  Until Stage C runs, this command errors clean
        with a "missing directory" message pointing at the next step.

        Args: none (reads config.build_codename for the suite name).
        """
        del args   # no positional args today
        import signing
        import apt_repo

        _codename = self.config.build_codename.strip('"').strip("'")
        _suites_spec: 'dict[str, list[str]]' = {
            _codename: ['main', 'doc', 'tests'],
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

    def cmd_migrate_repo_layout(self, *args):
        """Migrate repo/ from segregated-by-role layout to apt-conformant
        unified layout.

        CONF-01 Stage C (2026-05-22) — ONE-SHOT operation.

        Pre-migration:
          repo/main/<pkg>.deb        ← regular binaries
          repo/main/<pkg>.udeb       ← installer udebs
          repo/main/<pkg>.dsc        ← source descriptions
          repo/main/<pkg>.tar.*      ← source tarballs
          repo/doc/<pkg>.deb         ← doc artifacts
          repo/dbgsym/<pkg>.deb      ← debug symbols
          repo/tests/<pkg>.deb       ← test artifacts

        Post-migration:
          repo/dists/<codename>/main/binary-amd64/<pkg>.deb
          repo/dists/<codename>/main/debian-installer/binary-amd64/<pkg>.udeb
          repo/dists/<codename>/main/source/<pkg>.{dsc,tar.*}
          repo/dists/<codename>/doc/binary-amd64/<pkg>.deb
          repo/dists/<codename>-debug/main/binary-amd64/<pkg>.deb
          repo/dists/<codename>/tests/binary-amd64/<pkg>.deb

        Per Q2 (docs/plans/conf-01-repo-layout-migration.md), dbgsym
        moves to a SEPARATE suite `<codename>-debug` rather than a
        component under the main suite — matches Debian's
        `bookworm-debug` / Ubuntu's `jammy-debug` convention.

        Safety:
          1. Snapshot of repo/ to /tmp/repo-pre-migration-<ts>.tar
             before any file moves (operator can restore via
             `rm -rf repo && tar xf <snap> -C $(dirname $repo)`).
          2. os.rename per file (atomic on same filesystem; half-
             migrated state impossible).
          3. PROMPT_YESNO confirmation unless `force` is passed.
          4. Skip-if-dest-exists guard so a re-run after partial
             migration doesn't clobber already-moved files.

        Usage:
          repo migrate_layout              — interactive (snapshot, prompt, migrate)
          repo migrate_layout dry-run      — preview moves without touching fs
          repo migrate_layout force        — skip the YESNO prompt
        """
        _dry_run = 'dry-run' in args or '--dry-run' in args
        _force   = 'force' in args
        import datetime as _dt
        from collections import defaultdict

        _codename = self.config.build_codename.strip('"').strip("'")
        _repo = self.config.dir_repo
        _arch = self.config.arch

        # Migration map: (src_subdir, predicate, dst_subpath)
        # predicate is either a single extension string or a tuple of
        # extensions to match (.endswith).  For .deb specifically, also
        # exclude .udeb (since "endswith('.deb')" matches both).
        _SOURCE_EXTS = (
            '.dsc',
            '.tar.gz', '.tar.xz', '.tar.bz2', '.tar.zst',
            '.debian.tar.gz', '.debian.tar.xz', '.debian.tar.bz2',
            '.orig.tar.gz', '.orig.tar.xz', '.orig.tar.bz2',
            # .changes / .buildinfo / .build are build-side artifacts
            # — left in repo/main/ for now (separate ticket if we want
            # to surface them somewhere).  Source clients (apt-get source)
            # only need .dsc + tarballs.
        )
        _migrations = [
            # (src_subdir, extension(s), destination_subpath_under_repo)
            ('main',   '.udeb',
             f'dists/{_codename}/main/debian-installer/binary-{_arch}'),
            ('main',   '.deb',
             f'dists/{_codename}/main/binary-{_arch}'),
            ('main',   _SOURCE_EXTS,
             f'dists/{_codename}/main/source'),
            ('doc',    '.deb',
             f'dists/{_codename}/doc/binary-{_arch}'),
            ('dbgsym', '.deb',
             f'dists/{_codename}-debug/main/binary-{_arch}'),
            ('tests',  '.deb',
             f'dists/{_codename}/tests/binary-{_arch}'),
        ]

        # Walk: build the full move list.
        _moves: 'list[tuple[str, str]]' = []   # (src_abs, dst_abs)
        for _src_subdir, _match, _dst_subpath in _migrations:
            _src_dir = os.path.join(_repo, _src_subdir)
            if not os.path.isdir(_src_dir):
                continue
            for _f in sorted(os.listdir(_src_dir)):
                _src_abs = os.path.join(_src_dir, _f)
                if not os.path.isfile(_src_abs):
                    continue
                # Match by extension(s).  Special-case .deb to NOT match
                # .udeb (since 'foo.udeb'.endswith('.deb') is False
                # but a future predicate could trip on it).
                if isinstance(_match, str):
                    if not _f.endswith(_match):
                        continue
                else:   # tuple
                    if not any(_f.endswith(_ext) for _ext in _match):
                        continue
                _dst_abs = os.path.join(_repo, _dst_subpath, _f)
                _moves.append((_src_abs, _dst_abs))

        if not _moves:
            console.print(
                f"{_repo}/{{main,doc,dbgsym,tests}} has no files to "
                f"migrate — already migrated, or repo is empty"
            )
            return

        # Group for summary display
        _by_dst = defaultdict(list)
        for _src, _dst in _moves:
            _key = os.path.relpath(os.path.dirname(_dst), _repo)
            _by_dst[_key].append((_src, _dst))

        console.print(f"Migration plan for {_repo}/:")
        for _dst_subdir, _items in sorted(_by_dst.items()):
            console.print(f"  → {_dst_subdir:60s} {len(_items)} file(s)")
        console.print(f"Total: {len(_moves)} files to move")

        if _dry_run:
            console.print(
                "[dry-run] No files moved.  Re-run without `dry-run` "
                "to perform migration.",
                tui.COLOR_HIGHLIGHT,
            )
            return

        # Confirm unless --force
        if not _force:
            _resp = Prompt(
                PROMPT_YESNO,
                f"Migrate {len(_moves)} files in {_repo}/?  "
                f"Snapshot will be created at /tmp/ first.",
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                console.print("Aborted.")
                return

        # Snapshot before any file moves.  Uncompressed tar — fastest;
        # /tmp space is usually plentiful and the snapshot is only
        # kept until the operator confirms migration succeeded.
        # Microsecond resolution — eliminates collision risk if two
        # migrations run within the same second (test ordering or
        # accidental operator double-invocation).
        _ts = _dt.datetime.now().strftime('%Y%m%dT%H%M%S%f')
        _snapshot = f'/tmp/repo-pre-migration-{_ts}.tar'
        console.print(f"Snapshot: tar -cf {_snapshot} {_repo}/ ...")
        _r = subprocess.run(
            ['tar', '-cf', _snapshot, '-C', os.path.dirname(_repo),
             os.path.basename(_repo)],
            capture_output=True, text=True,
        )
        if _r.returncode != 0:
            console.print(
                f"ERROR: snapshot failed (rc={_r.returncode}): "
                f"{_r.stderr.strip()[:200]}"
            )
            logger.error(
                f"cmd_migrate_repo_layout snapshot: "
                f"rc={_r.returncode}, stderr={_r.stderr.strip()}"
            )
            return
        try:
            _snap_mb = os.path.getsize(_snapshot) // (2 ** 20)
            console.print(
                f"Snapshot: {_snap_mb} MB written "
                f"({_snapshot}) — restore via `rm -rf {_repo} && "
                f"tar xf {_snapshot} -C {os.path.dirname(_repo)}`",
                tui.COLOR_HIGHLIGHT,
            )
        except OSError:
            pass

        # Create all destination dirs up-front.
        _dst_dirs = sorted({os.path.dirname(_dst) for _, _dst in _moves})
        for _d in _dst_dirs:
            try:
                os.makedirs(_d, exist_ok=True)
            except OSError as e:
                console.print(f"ERROR: mkdir {_d}: {e}")
                logger.error(f"cmd_migrate_repo_layout mkdir {_d}: {e}")
                return

        # Move each file with os.rename — atomic on same filesystem.
        # If a destination already exists (operator re-ran after a
        # partial migration), skip with a warning rather than clobber.
        _n_moved = 0
        _n_skipped = 0
        _n_failed = 0
        for _src, _dst in _moves:
            if os.path.exists(_dst):
                logger.warning(f"migrate: skip {_dst} (already exists)")
                _n_skipped += 1
                continue
            try:
                os.rename(_src, _dst)
                _n_moved += 1
            except OSError as e:
                console.print(
                    f"ERROR: rename {os.path.basename(_src)}: {e}"
                )
                logger.error(
                    f"cmd_migrate_repo_layout rename "
                    f"{_src} → {_dst}: {e}"
                )
                _n_failed += 1

        # Best-effort rmdir of now-empty source subdirs.  Don't fail
        # if non-empty (residual files we don't recognise — operator
        # can investigate manually).
        for _src_subdir in ('main', 'doc', 'dbgsym', 'tests'):
            _src_dir = os.path.join(_repo, _src_subdir)
            if os.path.isdir(_src_dir):
                try:
                    os.rmdir(_src_dir)
                    console.print(f"  → removed empty {_src_subdir}/")
                except OSError:
                    _remaining = (sorted(os.listdir(_src_dir))[:5]
                                  if os.path.isdir(_src_dir) else [])
                    if _remaining:
                        console.print(
                            f"  → {_src_subdir}/ not empty after "
                            f"migration; first 5: {_remaining}",
                            tui.COLOR_WARNING,
                        )

        console.print(
            f"Migration complete: {_n_moved} moved, "
            f"{_n_skipped} skipped (dest existed), {_n_failed} failed",
            tui.COLOR_HIGHLIGHT if _n_failed == 0 else tui.COLOR_ERROR,
        )
        if _n_failed > 0:
            console.print(
                f"Restore via `rm -rf {_repo} && "
                f"tar xf {_snapshot} -C {os.path.dirname(_repo)}`"
            )

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
            'reload':         'rebuild a fork pkg after a local edit '
                              '(repo reload <pkg>...)',
            'audit':          'dep + conflict audit (repo/main) + gap '
                              'classification.  Pass a target name to '
                              'drill in: `repo audit lsb-base`.',
            'audit_nmu':      'walk repo/ for any .deb whose Version or '
                              'dep constraints still carry an NMU/binNMU/'
                              'backport suffix (+bN, +debNuN, ~bpoN+N, '
                              'etc.)',
            'strip':          'one-time backfill: strip NMU suffixes from '
                              'every .deb/.udeb in repo/.  Future fresh '
                              'builds get stripped automatically '
                              'post-dpkg-buildpackage.',
            'cleanup':        'delete obsolete .debs/.udebs from repo/ '
                              '(orphan source / version drift).  Dry-run '
                              'by default; pass `force` to actually '
                              'delete.',
            'index':          'generate apt-repo metadata in-place under '
                              'repo/dists/<codename>{,-debug}/',
            'migrate_layout': 'one-shot migration of repo/{main,doc,dbgsym,'
                              'tests}/ to apt-conformant unified layout '
                              'under repo/dists/<codename>{,-debug}/.  '
                              'Args: dry-run | force',
        }
        if action == 'tunnel':
            return self.cmd_tunnel_package(*args)
        if action == 'reload':
            return self.cmd_reload_fork(*args)
        if action == 'audit':
            return self.cmd_audit(*args)
        if action == 'audit_nmu':
            return self.cmd_audit_nmu(*args)
        if action == 'strip':
            return self.cmd_strip_repo(*args)
        if action == 'cleanup':
            return self.cmd_package_cleanup(*args)
        if action == 'index':
            return self.cmd_index_repo(*args)
        if action == 'migrate_layout':
            return self.cmd_migrate_repo_layout(*args)
        return self._group_help('repo', _table, action)

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
        _files: 'list[str]' = []
        for _deb_dir in self.config.all_deb_dirs():
            try:
                for _f in os.listdir(_deb_dir):
                    if _f.endswith('.deb') or _f.endswith('.udeb'):
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

        _rewritten = _unchanged = _failed = 0
        _total_strips = 0
        _bar = ProgressBar(
            label='Strip NMU', maxvalue=len(_files), show_rate=False,
        )
        for _path in _files:
            _bar.step(1)
            _f = os.path.basename(_path)
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

        console.print(
            f"Strip complete: {_rewritten} rewritten, "
            f"{_unchanged} unchanged, {_failed} failed.  "
            f"{_total_strips} suffix(es) stripped in total.  "
            f"Run `repo audit_nmu` to confirm zero residue."
        )

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
        _selected_pkg_names: 'set[str]' = set()
        _selected_srcs: 'set[str]' = set()
        for _tree in (self.dep_tree, self.udeb_dep_tree):
            if _tree is None:
                continue
            _selected_srcs.update(_tree.selected_srcs.keys())
            for _files in _tree.src_pkg_files.values():
                _expected_files.update(_files)
                for _fn in _files:
                    _selected_pkg_names.add(_fn.split('_', 1)[0])

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
        for _sub in utils._REPO_SUBDIRS:
            for _filename, _ctrl in repo_audit.iter_packages_all_versions(
                    self.config, subdir=_sub):
                _total += 1
                if _filename in _expected_files:
                    continue
                _pkg = (_ctrl.get('Package') or '').strip()
                _src_field = (_ctrl.get('Source') or '').strip()
                # Source field is "name" or "name (version)" — drop the
                # version qualifier; fall back to Package name when
                # the control omits Source (single-binary sources).
                _src_name = (_src_field.split(' ', 1)[0].strip()
                             if _src_field else _pkg)
                _file_pkg = _filename.split('_', 1)[0]
                # Size comes from the index field; falls back to
                # statting the on-disk file if missing (shouldn't
                # happen — dpkg-scanpackages always emits Size).
                try:
                    _size = int(_ctrl.get('Size') or 0)
                except (TypeError, ValueError):
                    _size = 0
                if _src_name not in _selected_srcs:
                    _orphan.append((_sub, _filename, _src_name, _size))
                elif _file_pkg in _selected_pkg_names:
                    _drift.append((_sub, _filename, _src_name, _size))
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
                "Run `dep parse` first — cleanup needs selected_srcs "
                "to know what's NOT obsolete"
            )
            return

        _force = 'force' in args
        _verbose = 'verbose' in args

        _orphan, _drift, _malformed, _total_files = self._scan_stale_files()

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
                "Pass `repo cleanup force` to actually delete.",
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
                   cache build force → dep parse force →
                   source download force → source build <pkg>
             - tree-hash differs but dep-hash matches: LIGHT PATH:
                 a. Wipe the pkg's derived artifacts (fork tarball,
                    source/ copy, repo/ debs, build log sidecars).
                 b. Regenerate the fork tarball via generate_fork_mirror.
                 c. Copy the fresh tarball into source/ so BuildContainer
                    can `cp /source/<pkg>_* .` it.
                 d. Invoke `source build force <pkg>` to rebuild.
                 e. Persist updated hashes (done by generate_fork_mirror).

        Prereqs: cache build + dep parse + container init must have
        run earlier in the session.  The reload only avoids RE-RUNNING
        them; it doesn't bypass them entirely.

        Why this exists: editing a fork file (e.g. fix a typo in
        debian/rules) used to require `cache build force` →
        `dep parse force` → `source download force` →
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
                "package reload requires cache build + dep parse + container "
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
                    "    dep parse force\n"
                    "    source download force\n"
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

        # Verify the project signing key before any sudo / mount / dpkg work
        if not self._ensure_signing_key_verified():
            return

        # Pre-flight closure audit — scoped to the live selection so
        # alternatives in the flat repo (busybox vs busybox-static,
        # grub-pc vs grub-efi) don't surface as false-positive conflicts.
        # Fast path (cached Packages snapshot) when repo/ unchanged.
        if not self._preflight_audit_repo():
            console.print("Aborted by repo audit pre-flight")
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

        # Pre-flight closure audit — scoped to the installer (udeb)
        # selection.  Installer chroot uses dpkg --unpack (no apt),
        # so unmet deps don't fail until the installer runs on the
        # target — catch them here.
        if not self._preflight_audit_repo():
            console.print("Aborted by repo audit pre-flight")
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
            _iso_basename = f"athena-installer-{_version}-amd64.iso"
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
                base_include_pkgs=_base_include,
                deb_whitelist=_pool_whitelist,
                signing_homedir=signing.signing_home(self.config),
                signing_pubkey_path=signing.signing_pubkey_path(self.config),
                pkg_groups=self.dep_tree.pkg_group_pkg_names,
                group_meta=self.dep_tree.pkg_group_meta,
                expected_kernel_pkg=_expected_kernel,
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
        finally:
            _password = '*' * len(_password)  # noqa: F841

    def cmd_source_rescan(self, *args):
        """Report what `source build` would rebuild against the current
        cache + repo state, without triggering any builds.

        Usage: source rescan [verbose]

        Iterates every source package in the resolved dep tree (deb +
        udeb sides merged).  For each, runs the same `check_build`
        gate that `source build` uses to decide skip-vs-rebuild: PASS
        result file present AND every expected binary filename present
        in repo/ as a valid `.deb`.  Prints counts and, with `verbose`,
        the list of source-package names that would rebuild.

        Use cases:
          * After a snapshot drift, see how many packages have shifted
            and would queue for rebuild before committing to the time.
          * After a fork edit, confirm the rebuild surface narrowed
            to what you expected.
          * Pre-flight a long `autorun` so you know roughly how much
            work is queued.

        Prereqs: `cache build` + `dep parse` + `container init` must
        have run earlier in the session — same as source build's own
        gates (we share `check_build`, which lives on BuildContainer).
        """
        if not (self.flags.cache_ready and self.flags.dep_check_ready
                and self.flags.build_container_ready):
            console.print(
                "source rescan needs cache build + dep parse + container "
                "init to have run first.",
                tui.COLOR_ERROR,
            )
            return

        _verbose = 'verbose' in args

        # Merge deb + udeb dep trees (shared source_hashtable means
        # duplicates auto-dedupe by source name).
        _srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _name, _src in self.udeb_dep_tree.selected_srcs.items():
                if _name not in _srcs:
                    _srcs[_name] = _src

        _ok = []
        _needs_rebuild = []
        _no_pkgs = []  # source declares no binaries (unusual)
        _tunneled = []

        # NOTE: do NOT call the patch-refresh helper here despite the
        # temptation.  That helper DELETES .result files when it
        # detects patch-set content changes (line ~1261 of build.py)
        # — a destructive side effect inappropriate for a read-only
        # scan command.  Trade-off: if the operator added/removed
        # patches since the last `dep parse`, this count misses those
        # invalidations.  Run `dep parse` first if patches changed.
        # Found 2026-05-19: rescan was over-counting because the
        # patch-refresh side effect was wiping PASS state for any
        # source whose patch hash diverged.

        # Progress feedback — even though each check_build is fast
        # (a few stat() calls + ar magic check), a 1500+ source corpus
        # is ~2-5s and operator wants to see motion.
        _bar = ProgressBar(
            label='Rescan', maxvalue=len(_srcs), show_rate=False,
        )
        for _name, _src in sorted(_srcs.items()):
            _bar.step(1)
            _expected = self._predicted_files_for_source(_name)
            if not _expected:
                _no_pkgs.append(_name)
                continue
            # Tunneled packages register .result = TUNNELED and have
            # check_build return True regardless of repo/ presence —
            # they're pulled at chroot-build time, not produced.
            _result_file = os.path.join(
                self.container.buildlog_path, _name + '.result')
            try:
                with open(_result_file) as fh:
                    _first_line = fh.readline().strip()
                    if _first_line == 'TUNNELED':
                        _tunneled.append(_name)
                        continue
            except OSError:
                pass

            if self.container.check_build(_src, _expected):
                _ok.append(_name)
            else:
                _needs_rebuild.append(_name)
        _bar.close()

        # Classify each rebuild candidate by which `source build <mode>`
        # would address it.  Priority order (only ONE label per source):
        #   1. pkg          : in selected_srcs but NOT in any exclusion set
        #                     → `source build` (bare, or `source build pkg`)
        #   2. installer    : in installer_exclusive OR in udeb dep tree
        #                     → `source build installer`
        #   3. live         : in live_exclusive_src_names
        #                     → `source build live`
        #   4. recommended  : in extras_src_names (Recommends-only depth-1)
        #                     → `source build recommended`
        # Overlapping sources (e.g. shared between pkg-list and live) get
        # the highest-priority tag.
        _live_set    = self.dep_tree.live_exclusive_src_names
        _inst_set    = self.dep_tree.installer_exclusive_src_names
        _extras_set  = self.dep_tree.extras_src_names
        _udeb_names: set = set()
        if self.udeb_dep_tree is not None:
            _udeb_names = set(self.udeb_dep_tree.selected_srcs.keys())

        def _subset_for(name: str) -> str:
            if (name not in _live_set and name not in _inst_set
                    and name not in _extras_set and name not in _udeb_names):
                return 'pkg'
            if name in _inst_set or name in _udeb_names:
                return 'installer'
            if name in _live_set:
                return 'live'
            if name in _extras_set:
                return 'recommended'
            return 'unclassified'

        from collections import defaultdict as _dd
        _by_subset = _dd(list)
        for _n in _needs_rebuild:
            _by_subset[_subset_for(_n)].append(_n)

        _total = len(_srcs)
        console.print("Source rescan against current cache + repo:")
        console.print(f"  {len(_ok):5d}  built + verified")
        console.print(f"  {len(_needs_rebuild):5d}  would rebuild")
        if _tunneled:
            console.print(f"  {len(_tunneled):5d}  tunneled (pulled, not built)")
        if _no_pkgs:
            console.print(f"  {len(_no_pkgs):5d}  no binaries declared (skipped)")
        console.print(f"  {_total:5d}  total source packages")

        # Subset breakdown — tells operator which `source build <mode>`
        # addresses each chunk of the rebuild queue.
        if _needs_rebuild:
            _cmd_map = {
                'pkg':          'source build',
                'installer':    'source build installer',
                'live':         'source build live',
                'recommended':  'source build recommended',
                'unclassified': '(none — investigate)',
            }
            console.print("")
            console.print("Rebuild queue by subset:")
            for _subset in ('pkg', 'installer', 'live', 'recommended',
                            'unclassified'):
                _names = _by_subset.get(_subset, [])
                if not _names:
                    continue
                console.print(
                    f"  {len(_names):5d}  {_subset:<13s}  →  {_cmd_map[_subset]}"
                )

        if _verbose and _needs_rebuild:
            console.print("")
            console.print(f"Would rebuild ({len(_needs_rebuild)}), grouped by subset:")
            for _subset in ('pkg', 'installer', 'live', 'recommended',
                            'unclassified'):
                _names = sorted(_by_subset.get(_subset, []))
                if not _names:
                    continue
                console.print(f"  [{_subset}] ({len(_names)}):")
                for _n in _names:
                    console.print(f"    {_n}")
        elif _needs_rebuild and not _verbose:
            console.print("")
            console.print(
                "(pass `source rescan verbose` to list the "
                f"{len(_needs_rebuild)} rebuild candidates by subset + name)",
                tui.COLOR_INFO,
            )

    def cmd_source_repair(self, *args):
        """One-shot: restore `.result=PASS` files for sources whose
        binaries already exist in repo/ but whose .result is missing.

        Usage: source repair [verbose]

        Designed for recovering from accidental .result deletion —
        notably the 2026-05-19 incident where `cmd_source_rescan`
        called the destructive `_refresh_patches` and wiped ~47
        .result files whose corresponding .debs were still valid in
        repo/.  Without this, those sources would re-enter the source
        build queue and waste hours rebuilding artifacts that already
        exist.

        Algorithm (per source in the merged deb+udeb dep tree):

          1. If .result already exists → skip (don't overwrite).
          2. If every predicted filename (union across both dep_trees'
             src_pkg_files maps) exists in repo/ AND each is a
             syntactically valid .deb (ar archive with the right
             members per BuildContainer.is_ar_file) → write PASS.
          3. Otherwise → leave alone (genuinely needs rebuild).

        Repair is the ONLY thing this command mutates.  Doesn't touch
        repo/, doesn't invoke BuildContainer.build, doesn't refresh
        patches.  Predicted-filename match comes from `dep parse`'s
        resolution — for the repair to be correct, the dep tree's
        view of expected filenames must match what's actually in
        repo/ (which is true post-rebump for any pkg whose source
        version hasn't drifted).

        Prereqs: cache build + dep parse + container init (same as
        source build's gates).
        """
        if not (self.flags.cache_ready and self.flags.dep_check_ready
                and self.flags.build_container_ready):
            console.print(
                "source repair needs cache build + dep parse + container "
                "init to have run first.",
                tui.COLOR_ERROR,
            )
            return

        _verbose = 'verbose' in args

        _srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _name, _src in self.udeb_dep_tree.selected_srcs.items():
                if _name not in _srcs:
                    _srcs[_name] = _src

        _repaired = []
        _already_ok = 0      # .result already present, skipped
        _need_rebuild = []   # binaries missing — will rebuild as expected
        _no_pkgs = 0         # source declares no binaries

        _bar = ProgressBar(
            label='Repair', maxvalue=len(_srcs), show_rate=False,
        )
        for _name, _src in sorted(_srcs.items()):
            _bar.step(1)
            _expected = self._predicted_files_for_source(_name)
            if not _expected:
                _no_pkgs += 1
                continue
            _result_file = os.path.join(
                self.container.buildlog_path, _name + '.result')
            if os.path.exists(_result_file):
                _already_ok += 1
                continue
            # Shallow check — same as BuildContainer.check_build.
            # Filename + ar magic.  Doesn't verify internal Version
            # or Depends; that's `source verify`'s job (opt-in).
            _all_present = True
            for _f in _expected:
                # CONF-01 Stage D: deb_dest_for_filename returns the
                # correct nested dir for this artifact's role/type.
                _path = os.path.join(
                    self.config.deb_dest_for_filename(_f), _f,
                )
                if not os.path.isfile(_path):
                    _all_present = False
                    break
                if not self.container.is_ar_file(_path):
                    _all_present = False
                    break
            if not _all_present:
                _need_rebuild.append(_name)
                continue
            try:
                with open(_result_file, 'w') as fh:
                    fh.write('PASS\n')
                _repaired.append(_name)
                logger.info(f"source repair: restored {_result_file}")
            except OSError as e:
                console.print(
                    f"ERROR: cannot write {_result_file}: {e}",
                    tui.COLOR_ERROR,
                )
        _bar.close()

        console.print("Source repair:")
        console.print(
            f"  {len(_repaired):5d}  .result restored to PASS "
            "(binaries present, will skip rebuild)"
        )
        console.print(
            f"  {len(_need_rebuild):5d}  binaries missing "
            "(legitimate rebuilds — left alone)"
        )
        console.print(
            f"  {_already_ok:5d}  .result already present (untouched)"
        )
        if _no_pkgs:
            console.print(
                f"  {_no_pkgs:5d}  source declares no binaries (skipped)"
            )

        if _verbose and _repaired:
            console.print("")
            console.print(f"Restored ({len(_repaired)}):")
            for _n in _repaired:
                console.print(f"  {_n}")

    def cmd_source_audit(self, *args):
        """Walk dep_tree.selected_srcs; for each source, check whether
        its main-tier binaries are present in repo/main at the right
        filename.  Reports sources that still need building.

        Categories per source:
          missing    — at least one main binary not in repo/main
          mismatched — file exists but is not a valid ar archive
                       (broken from a partial build / disk issue)
          ok         — all main binaries present + readable

        -dev / -doc / -dbgsym / -tests binaries are NOT checked here —
        they're side artifacts and shouldn't gate the rebuild decision
        (matches BuildContainer.check_build semantics).

        Usage: source audit [verbose]

        Prerequisite: cache + dep parse must have run.

        Companion to `source rescan`: rescan uses BuildContainer's
        check_build per-source (gated by ar-validity); this command
        gives the operator a top-level view of "how much rebuilding
        remains" without driving any state.
        """
        _verbose = 'verbose' in args
        if not (self.dep_tree and self.dep_tree.selected_srcs):
            console.print(
                "source audit requires dep_tree.selected_srcs — run "
                "`dep parse` first."
            )
            return

        from buildcontainer import BuildContainer
        # CONF-01 Stage D: main-tier binaries live at the new nested
        # path; helper resolves the right dir (handles .deb vs .udeb).
        _main = self.config.dir_repo_main
        _any_findings = False

        # Audit deb and udeb cohorts SEPARATELY.  src_pkg_files lives
        # per-tree (see dependencytree.py:src_pkg_files docstring); the
        # old shared-Source.pkgs design let the udeb pass overwrite the
        # deb pass's list, hiding deb-cohort gaps.  Split report keeps
        # each cohort's "missing" surface visible.
        _cohorts = [('deb', self.dep_tree)]
        if self.udeb_dep_tree is not None and self.udeb_dep_tree.selected_srcs:
            _cohorts.append(('udeb', self.udeb_dep_tree))

        for _cohort_label, _tree in _cohorts:
            _missing_srcs: 'list[tuple]' = []
            _mismatch_srcs: 'list[tuple]' = []
            _ok = 0
            # Per-source ProgressBar — each iteration does at least
            # one is_ar_file open+seek per predicted main binary
            # (often 3-10 files per source).  500+ source corpus =
            # multi-second silence.  show_rate=False since per-source
            # cost varies (single .deb vs. multi-deb sources).
            _bar = ProgressBar(
                label=f'Audit {_cohort_label} sources',
                maxvalue=max(1, len(_tree.selected_srcs)),
                show_rate=False,
            )
            try:
                for _src_name in _tree.selected_srcs:
                    _bar.step(1)
                    _expected_main = [
                        _f for _f in (_tree.src_pkg_files.get(_src_name) or [])
                        if utils.classify_repo_subdir(_f) == 'main'
                    ]
                    if not _expected_main:
                        # Source has no main-tier predicted binary in
                        # THIS cohort (e.g. a source whose deb cohort is
                        # all -doc files).  Not a miss for this audit.
                        _ok += 1
                        continue
                    _missing: 'list[str]' = []
                    _mismatch: 'list[str]' = []
                    for _f in _expected_main:
                        # deb_dest_for_filename handles the .deb / .udeb
                        # split (both classify as 'main' but live in
                        # different dirs post-Stage D).
                        _p = os.path.join(
                            self.config.deb_dest_for_filename(_f), _f,
                        )
                        if not os.path.isfile(_p):
                            _missing.append(_f)
                            continue
                        if not BuildContainer.is_ar_file(_p):
                            _mismatch.append(_f)
                    if _missing:
                        _missing_srcs.append((_src_name, _missing))
                    elif _mismatch:
                        _mismatch_srcs.append((_src_name, _mismatch))
                    else:
                        _ok += 1
            finally:
                _bar.close()

            console.print(
                f"\n=== Source audit ({_cohort_label} cohort, "
                f"repo/main scope) ===\n"
                f"  ok         : {_ok} sources\n"
                f"  missing    : {len(_missing_srcs)} sources "
                f"(main binary not in repo/main)\n"
                f"  mismatched : {len(_mismatch_srcs)} sources "
                f"(file exists but not a valid ar archive)"
            )
            _show = (len(_missing_srcs) if _verbose
                     else min(30, len(_missing_srcs)))
            if _show:
                console.print(f"\nFirst {_show} missing sources ({_cohort_label}):")
                for _src_name, _files in _missing_srcs[:_show]:
                    _sample = ', '.join(_files[:3])
                    if len(_files) > 3:
                        _sample += f', … (+{len(_files) - 3})'
                    console.print(f"  {_src_name:30s} → {_sample}")
            _show = (len(_mismatch_srcs) if _verbose
                     else min(30, len(_mismatch_srcs)))
            if _show:
                console.print(f"\nFirst {_show} mismatched sources ({_cohort_label}):")
                for _src_name, _files in _mismatch_srcs[:_show]:
                    _sample = ', '.join(_files[:3])
                    if len(_files) > 3:
                        _sample += f', … (+{len(_files) - 3})'
                    console.print(f"  {_src_name:30s} → {_sample}")
            if _missing_srcs or _mismatch_srcs:
                _any_findings = True

        if _any_findings:
            console.print(
                "\nNext: `source build` to rebuild missing/mismatched."
            )

    def cmd_source_verify(self, *args):
        """Opt-in deep audit: report .debs whose internal Version
        mismatches the predicted filename, or whose Depends no longer
        resolve in the current cache.

        Usage: source verify [verbose]

        Read-only.  Doesn't touch .result, doesn't rebuild anything.
        Use as a pre-ship sanity check: BEFORE making a release ISO,
        run this to surface artifacts that exist in repo/ but would
        fail to install on a clean Thor system (e.g. Depends pointing
        at a cache version that has since drifted away).

        Why this is opt-in:  source build / check_build only gate on
        filename + ar-magic for performance (~5s scan over 1500 srcs).
        Deep verify is ~30-40s and tends to over-report on the
        rebump-vs-cross-source-strict-equal scenario: a binary's
        `Depends: libfoo (= 5.4-1)` won't satisfy against a cached
        `libfoo 5.4-1+thor1` even though both sides are equivalent
        modulo our distro suffix.  Run verify, look at the per-
        binary diagnostic in log/athena.log, decide which findings
        are real vs noise.

        Reports (verbose):
          - per-source list of failing binaries + first-failure
            diagnostic from verify_pkg_artifact (version-mismatch:X!=Y,
            unsatisfied-Depends:libfoo, etc).

        Prereqs: cache + dep + container (same as source build's gates).
        """
        if not (self.flags.cache_ready and self.flags.dep_check_ready
                and self.flags.build_container_ready):
            console.print(
                "source verify needs cache build + dep parse + container "
                "init to have run first.",
                tui.COLOR_ERROR,
            )
            return

        _verbose = 'verbose' in args

        _srcs = dict(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            for _name, _src in self.udeb_dep_tree.selected_srcs.items():
                if _name not in _srcs:
                    _srcs[_name] = _src

        _ok = 0
        _failed = []         # [(pkg_name, first_failing_binary, diagnostic)]
        _skipped_tunneled = 0
        _skipped_missing = 0  # binaries absent — not verify's concern, repair handles
        _no_pkgs = 0

        _bar = ProgressBar(
            label='Verify', maxvalue=len(_srcs), show_rate=False,
        )
        for _name, _src in sorted(_srcs.items()):
            _bar.step(1)
            _expected = self._predicted_files_for_source(_name)
            if not _expected:
                _no_pkgs += 1
                continue
            # Skip TUNNELED — verify doesn't apply to third-party pulls.
            _result_file = os.path.join(
                self.container.buildlog_path, _name + '.result')
            try:
                with open(_result_file) as fh:
                    if fh.readline().strip() == 'TUNNELED':
                        _skipped_tunneled += 1
                        continue
            except OSError:
                pass
            # Quick precheck: skip if any binary is missing (verify
            # only judges present binaries; missing ones are repair /
            # rebuild's concern).
            _any_missing = False
            _failing = None
            for _f in _expected:
                _path = os.path.join(
                    self.config.deb_dest_for_filename(_f), _f,
                )
                if not os.path.isfile(_path):
                    _any_missing = True
                    break
                _verify_ok, _reason = self.container.verify_pkg_artifact(_path, _f)
                if not _verify_ok:
                    _failing = (_f, _reason)
                    break
            if _any_missing:
                _skipped_missing += 1
                continue
            if _failing is None:
                _ok += 1
            else:
                _failed.append((_name,) + _failing)
                logger.info(
                    f"source verify {_name}: {_failing[0]}: {_failing[1]}"
                )
        _bar.close()

        console.print("Source verify (deep audit):")
        console.print(f"  {_ok:5d}  pass deep verify (binaries internally consistent)")
        console.print(f"  {len(_failed):5d}  FAIL — present in repo/ but verify rejected")
        if _skipped_tunneled:
            console.print(f"  {_skipped_tunneled:5d}  skipped (TUNNELED — third-party pull)")
        if _skipped_missing:
            console.print(
                f"  {_skipped_missing:5d}  skipped (binaries missing — repair/rebuild concern)"
            )
        if _no_pkgs:
            console.print(f"  {_no_pkgs:5d}  no binaries declared")

        if _failed:
            # Aggregate by failure-type prefix for quick triage.
            from collections import Counter
            _types = Counter()
            for _, _, _diag in _failed:
                _prefix = _diag.split(':', 1)[0] if ':' in _diag else _diag
                _types[_prefix] += 1
            console.print("")
            console.print("Failure types:")
            for _k, _v in _types.most_common():
                console.print(f"  {_v:5d}  {_k}")

        if _verbose and _failed:
            console.print("")
            console.print(f"Failing sources ({len(_failed)}):")
            for _src_name, _f, _diag in _failed:
                console.print(f"  {_src_name}: {_f}: {_diag}")

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

        for _index, _src_pkg in enumerate(packages, start=1):
            progress_bar.label(_src_pkg.package)
            _ = _index   # surfaced via {value} on the bar


            # Packages on the skip_src list are excluded unconditionally — typically
            # packages that are known to be unbuildable in the current environment.
            if _src_pkg.package in self.cache.skip_src:
                logger.warning(f"Package {_src_pkg.package} in skip_list")
                _skipped = _skipped + 1
                progress_bar.step(1)
                continue

            # Predicted artefacts (union across both dep_trees) — used by
            # both check_build (skip-rebuild gate) and _do_tunnel.  Source
            # objects no longer carry .pkgs; the per-tree maps live on
            # DependencyTree.src_pkg_files.
            _expected_files = self._predicted_files_for_source(_src_pkg.package)

            # Tunneled packages are always downloaded rather than built locally.
            # check_build() accepts 'TUNNELED' as a valid result so we can skip
            # packages that were already tunneled in a previous run.
            if _src_pkg.package in self.config.tunnel_packages:
                if self.container.check_build(_src_pkg, _expected_files):
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
            if not _force and self.container.check_build(_src_pkg, _expected_files):
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
            'build':    'build sources: source build [force] [pkg | live | installer | recommended | all | <pkg>…] [[profile,…]]',
            'rescan':   'report what source build would rebuild (source rescan [verbose])',
            'repair':   'restore .result=PASS for sources whose binaries exist in repo/ '
                        '(recovers from accidental .result deletion)',
            'verify':   'opt-in deep audit: report .debs whose internal Version mismatches the '
                        'filename or whose Depends no longer resolve in cache (source verify [verbose])',
            'audit':    'walk dep_tree.selected_srcs; report sources whose main '
                        'binaries are missing from repo/main or whose versions '
                        'mismatch.  Tells you which sources still need building.',
        }
        if action == 'download':
            return self.cmd_source_download(*args)
        if action == 'build':
            return self.cmd_source_build(*args)
        if action == 'rescan':
            return self.cmd_source_rescan(*args)
        if action == 'repair':
            return self.cmd_source_repair(*args)
        if action == 'verify':
            return self.cmd_source_verify(*args)
        if action == 'audit':
            return self.cmd_source_audit(*args)
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
            'source':    'wipe source/ (re-downloaded on next `source download`)',
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

        Bare `source build` builds pkg.list closure only.  For a
        complete live ISO, we need pkg + live extras;
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
    tui.register_command('clean',     session.cmd_clean,     '\tClean:      clean cache | source | repo | buildroot | image | download | container | all')
    tui.register_command('dep',       session.cmd_dep,       '\tDeps:       dep parse')
    tui.register_command('patch',     session.cmd_patch,     '\tPatches:    patch refresh')
    tui.register_command('source',    session.cmd_source,    '\tSources:    source download | source build [pkg|live|installer|recommended|all]')
    tui.register_command('repo',      session.cmd_repo,      '\tRepo:       repo index | audit | audit_nmu | strip | cleanup | tunnel | reload | migrate_layout')
    tui.register_command('container', session.cmd_container, '\tContainer:  container init')
    tui.register_command('chroot',    session.cmd_chroot,    '\tChroot:     chroot build [live|installer] | chroot verify')
    tui.register_command('iso',       session.cmd_iso,       '\tISO:        iso build live | iso build installer')
    tui.register_command('key',       session.cmd_key,       '\tSigning:    key generate | key verify')
    tui.register_command('autorun',   session.cmd_auto_run,  '\tAutorun:    autorun [live] | autorun installer')
    tui.register_command('print',     session.cmd_print,     '\tPrint build state — try: print help')

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
