"""BuildSystem orchestrator.

Composition entry point for the chroot install / dep-drift check / ISO
assembly pipeline.  Body methods live in `chroot.py`, `dep_drift.py`,
and `iso.py` mixins that this class composes; `__init__`, the `for_iso`
factory, the sudo-password lifecycle, and the static
`strip_build_version` helper stay here because they orchestrate the
shared lifecycle rather than belonging to any one phase.
"""

import os
import subprocess

import tui
from tui import Prompt, PROMPT_PASSWORD, PROMPT_YESNO
import dependencytree
import utils
from utils import BuildConfig

from chroot import _ChrootMixin
from dep_drift import _DepDriftMixin
from iso import _IsoMixin


class BuildSystem(_ChrootMixin, _IsoMixin, _DepDriftMixin):
    def __init__(self, dependency_tree: dependencytree.DependencyTree,
                 config: BuildConfig,
                 dir_chroot: 'str | None' = None):
        """`dir_chroot`: override the chroot target dir —
        the disk surface builds into config.dir_chroot_disk while the
        live surface keeps the default config.dir_chroot."""
        self._dependencytree = dependency_tree
        self._config = config
        self._dir_image = config.dir_image
        self._dir_chroot = dir_chroot or config.dir_chroot
        # seed the per-instance chroot env now (no global os.environ
        # mutation) so methods invoked directly always find it; build_chroot
        # refreshes it at install time.
        self._setup_chroot_env()
        self._dir_repo = config.dir_repo
        # the new apt-repo layout nests .debs under
        # dists/<codename>/main/binary-<arch>/.  Mixins (chroot, dep-
        # drift) used to construct `repo/main/<deb>` paths inline; now
        # they read this attr instead.
        self._dir_repo_main = config.dir_repo_main
        self._dir_log = config.dir_log
        self._dir_preinstall_patch = config.dir_patch_preinstall
        self._dir_postinstall_patch = config.dir_patch_postinstall

        # Sanity check — directories must exist before anything else runs.
        for _dir in [self._dir_chroot, self._dir_image, self._dir_repo]:
            if not os.path.exists(_dir):
                raise RuntimeError(f"Missing essential directory: {_dir}")

        # If the chroot is not empty, require the user to confirm a wipe before
        # proceeding.  Leftover files from a failed previous run leave dpkg's
        # database in an inconsistent state and will corrupt the new build.
        # Continuing without a wipe is not supported — if the user declines,
        # abort so they can inspect or manually clean the directory first.
        _chroot_nonempty = len(os.listdir(self._dir_chroot)) != 0
        _wipe_chroot = False
        if _chroot_nonempty:
            tui.console.print(
                f"WARNING: '{os.path.basename(self._dir_chroot)}' is not empty — "
                f"leftover files from a previous run will corrupt the build."
            )
            _resp = Prompt(PROMPT_YESNO, "Wipe chroot and start clean?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                raise RuntimeError(
                    "Aborted: chroot is not empty and wipe was declined. "
                    "Manually clean the directory or re-run and confirm the wipe."
                )
            _wipe_chroot = True

        # sudo password.  Source order:
        #   1. $ATHENA_SUDO_PASSWORD env var (scripted / CI runs).  Pop'd
        #      from os.environ after read so it doesn't leak into child
        #      subprocesses or appear in /proc/<pid>/environ for the
        #      remainder of the run.
        #   2. tui.Prompt (PROMPT_PASSWORD, masked) — interactive.
        # dpkg and the install scripts run under sudo via sudo -S (stdin),
        # cached for all subprocess calls.
        # Collect + validate the sudo credential (shared with for_iso) so we
        # fail fast rather than discovering a bad credential mid-install.
        self._password = self._collect_and_validate_sudo()
        # Initialised here so the .password property's post-scrub check
        # behaves the same on a fresh instance and a scrubbed one.
        self._password_scrubbed = False

        # Wipe the chroot now that we have a validated sudo credential.
        # rm -rf the contents (not the directory itself) so dir_chroot remains.
        if _wipe_chroot:
            tui.console.print("Wiping chroot...")
            # a SIGKILL'd / OOM-killed prior run can leave
            # <chroot>/dev still BIND-MOUNTED to the host's real /dev (same
            # inodes).  Two independent guards so the recursive wipe can
            # never delete host device nodes as root:
            #   1. unmount first (idempotent; peels stacked layers), and
            #   2. `-xdev` so `find -delete` never descends across a
            #      surviving mount boundary into host /dev/{null,sda,…}.
            # Either alone closes the hole; together they're belt-and-braces.
            # MAT-10(a): if a mount is WEDGED (survives umount -lf), refuse to
            # wipe — `find -delete` would still be pointed at a tree carrying a
            # live host bind-mount.  Better to abort loudly than risk it.
            if not self._umount_chroot_fs():
                raise RuntimeError(
                    "Refusing to wipe chroot: a host bind-mount is WEDGED "
                    "under it (see log) — unmount it manually "
                    "(`sudo umount -lf` the listed paths) and retry.")
            _proc = subprocess.run(
                ['sudo', '-S', 'find', self._dir_chroot, '-xdev',
                 '-mindepth', '1', '-delete'],
                input=self._password + '\n',
                capture_output=True, text=True
            )

            if _proc.returncode != 0:
                raise RuntimeError(f"Failed to wipe chroot: {_proc.stderr.strip()}")
            tui.console.print("Chroot wiped")

        # Create Directory Structure
        self._build_chroot_directories()

        # Patch dependency metadata from the actual .deb files.  The Packages
        # cache may be from a different point in time than the downloaded .debs
        # (e.g. a binNMU rebuild happened between cache fetch and package
        # download).  When deps differ, dpkg's configure ordering will fail even
        # though our resolver computed a valid order based on the cache.
        self._check_dep_drift()

        # Run pre-Install
        self.pre_install()

    @staticmethod
    def _collect_and_validate_sudo() -> str:
        """Collect the sudo password and validate it via `sudo -S -v`.

        Source order: 1. $ATHENA_SUDO_PASSWORD (scripted/CI), pop'd from
        os.environ after read so it doesn't leak into child subprocesses or
        /proc/<pid>/environ; 2. an interactive masked Prompt.  Raises
        RuntimeError on a bad credential.  Shared by __init__ and for_iso so the
        pickup+prompt+validate sequence lives in one place.
        """
        _env_pw = os.environ.pop('ATHENA_SUDO_PASSWORD', None)
        if _env_pw is not None:
            tui.console.print(
                "Build system: using sudo password from ATHENA_SUDO_PASSWORD")
            _password = _env_pw
        else:
            tui.console.print(
                "Build system needs sudo — current user must be in the "
                "sudoers group")
            _password = Prompt(
                PROMPT_PASSWORD, "Enter sudo password").get_response()
        _proc = subprocess.run(['sudo', '-S', '-v'], input=_password + '\n',
                               capture_output=True, text=True)
        if _proc.returncode != 0:
            raise RuntimeError(f"Incorrect password or user not in sudoers file: {_proc.stderr.strip() or _proc.stdout.strip()}")
        return _password

    @classmethod
    def for_iso(cls, config: BuildConfig) -> 'BuildSystem':
        """Factory: create a BuildSystem for ISO assembly only.

        Does NOT touch the chroot — safe to call on an already-assembled
        chroot.  Sets only the attributes that build_iso() needs: paths and
        the sudo password.  Use instead of the normal constructor when you
        want to run build_iso() without triggering the wipe-chroot prompt.
        """
        _self = cls.__new__(cls)
        # Set only the attributes build_iso() reads.  Single-underscore
        # convention; methods of mixin sub-modules see them directly.
        _self._config    = config
        _self._dir_chroot = config.dir_chroot
        _self._dir_image  = config.dir_image
        _self._dir_log    = config.dir_log

        for _dir in [config.dir_chroot, config.dir_image]:
            if not os.path.exists(_dir):
                raise RuntimeError(f"Missing essential directory: {_dir}")

        # same pickup + validate as the main constructor (shared helper).
        _self._password = cls._collect_and_validate_sudo()
        _self._password_scrubbed = False
        return _self

    @property
    def password(self) -> str:
        """Validated sudo password collected at construction. Reused by callers
        (e.g. cmd_build_chroot_live's verify step) to avoid prompting twice.

        Raises RuntimeError after scrub_password() has been called: a
        BuildSystem instance is single-use w.r.t. its sudo credential, and
        a stale read after scrub almost always indicates a missed cleanup
        in a command handler.
        """
        if getattr(self, '_password_scrubbed', False):
            raise RuntimeError(
                "BuildSystem.password accessed after scrub_password() — "
                "this BuildSystem is single-use; create a fresh one"
            )
        return self._password

    def scrub_password(self) -> None:
        """Drop the cached sudo password from this instance.

        Python strings are immutable, so we cannot truly zero the bytes
        in place — assigning '' only drops this instance's reference and
        leaves GC to reclaim the original.  The point is to bracket the
        password's lifetime to the command that needed it (build_chroot,
        build_iso) instead of carrying it in BuildSystem state for the
        remainder of the TUI session.

        Idempotent — safe to call from a finally block alongside a
        successful exit path.
        """
        self._password = ''
        self._password_scrubbed = True

    # Filename predictors — both exposed on the BuildSystem so the
    # _ChrootMixin / dep_drift mixin contracts can call them without
    # importing utils directly.  Single source of truth lives in utils:
    #
    #   strip_build_version    strips ONLY +bN binNMU (legacy primitive)
    #   normalize_repo_filename strips +bN AND +debNuN/~bpoN+N/+rpiN/-Nb
    #                          (full match for our post-build strip step;
    #                           use this for path resolution under
    #                           repo/main).
    strip_build_version = staticmethod(utils.strip_build_version)
    normalize_repo_filename = staticmethod(utils.normalize_repo_filename)
