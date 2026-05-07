# External
import os
import pathlib
import shlex
import subprocess
import re
import tempfile
# Internal
import tui
from tui import Prompt, PROMPT_PASSWORD, PROMPT_YESNO
import dependencytree
import utils
from utils import BuildConfig


class BuildSystem:
    def __init__(self, dependency_tree: dependencytree.DependencyTree, config: BuildConfig):
        self.__dependencytree = dependency_tree
        self.__config = config
        self.__dir_image = config.dir_image
        self.__dir_chroot = config.dir_chroot
        self.__dir_repo = config.dir_repo
        self.__dir_log = config.dir_log
        self.__dir_preinstall_patch = config.dir_patch_preinstall
        self.__dir_postinstall_patch = config.dir_patch_postinstall

        # Sanity check — directories must exist before anything else runs.
        for _dir in [self.__dir_chroot, self.__dir_image, self.__dir_repo]:
            if not os.path.exists(_dir):
                raise RuntimeError(f"Missing essential directory: {_dir}")

        # If the chroot is not empty, require the user to confirm a wipe before
        # proceeding.  Leftover files from a failed previous run leave dpkg's
        # database in an inconsistent state and will corrupt the new build.
        # Continuing without a wipe is not supported — if the user declines,
        # abort so they can inspect or manually clean the directory first.
        _chroot_nonempty = len(os.listdir(self.__dir_chroot)) != 0
        _wipe_chroot = False
        if _chroot_nonempty:
            tui.console.print(
                f"WARNING: '{os.path.basename(self.__dir_chroot)}' is not empty — "
                f"leftover files from a previous run will corrupt the build."
            )
            _resp = Prompt(PROMPT_YESNO, "Wipe chroot and start clean?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                raise RuntimeError(
                    "Aborted: chroot is not empty and wipe was declined. "
                    "Manually clean the directory or re-run and confirm the wipe."
                )
            _wipe_chroot = True

        # dpkg and the install scripts run under sudo; collect the password once
        # here and reuse it for all subprocess calls via sudo -S (stdin).
        # Using tui.Prompt keeps input masked and routed through the TUI layer
        # rather than writing directly to the terminal, which would corrupt the
        # TUI rendering.
        tui.console.print("Build system needs sudo — current user must be in the sudoers group")
        self.__password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()

        # Validate the password immediately so we fail fast rather than
        # discovering a bad credential mid-install.
        _proc = subprocess.run(['sudo', '-S', '-v'], input=self.__password + '\n',
                               capture_output=True, text=True)
        if _proc.returncode != 0:
            raise RuntimeError(f"Incorrect password or user not in sudoers file: {_proc.stdout.strip()}")

        # Wipe the chroot now that we have a validated sudo credential.
        # rm -rf the contents (not the directory itself) so dir_chroot remains.
        if _wipe_chroot:
            tui.console.print("Wiping chroot...")
            _proc = subprocess.run(['sudo', '-S', 'find', self.__dir_chroot,
                 '-mindepth', '1', '-delete'], input=self.__password + '\n',
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

    @classmethod
    def for_iso(cls, config: BuildConfig) -> 'BuildSystem':
        """Factory: create a BuildSystem for ISO assembly only.

        Does NOT touch the chroot — safe to call on an already-assembled
        chroot.  Sets only the attributes that build_iso() needs: paths and
        the sudo password.  Use instead of the normal constructor when you
        want to run build_iso() without triggering the wipe-chroot prompt.
        """
        _self = cls.__new__(cls)
        # Set only the attributes build_iso() accesses.
        # Name-mangling: __x inside the class body → _BuildSystem__x on the object.
        _self._BuildSystem__config    = config
        _self._BuildSystem__dir_chroot = config.dir_chroot
        _self._BuildSystem__dir_image  = config.dir_image
        _self._BuildSystem__dir_log    = config.dir_log

        for _dir in [config.dir_chroot, config.dir_image]:
            if not os.path.exists(_dir):
                raise RuntimeError(f"Missing essential directory: {_dir}")

        tui.console.print("Build system needs sudo — current user must be in the sudoers group")
        _password = Prompt(PROMPT_PASSWORD, "Enter sudo password").get_response()
        _proc = subprocess.run(['sudo', '-S', '-v'], input=_password + '\n',
                               capture_output=True, text=True)
        if _proc.returncode != 0:
            raise RuntimeError(
                f"Incorrect password or user not in sudoers file: {_proc.stdout.strip()}"
            )
        _self._BuildSystem__password = _password
        return _self

    @property
    def password(self) -> str:
        """Validated sudo password collected at construction. Reused by callers
        (e.g. cmd_build_chroot's verify step) to avoid prompting twice."""
        return self.__password

    def build_chroot(self, debug: bool = False) -> bool:
        """Install all selected packages into the chroot in dependency order.

        Args:
            debug: When True, generate_system_configs() also writes a
                journald drop-in to forward all entries to /dev/console.
                Off by default — opt in for serial-debug builds only.

        Unpack phase (chrootless, all rounds):
          An unpack forest tracks Pre-Depends ordering.  Each round unpacks
          packages whose Pre-Depends are all already configured, then runs
          `dpkg --configure -a` INSIDE a real chroot so maintainer scripts
          run in the correct environment without DPKG_ROOT workarounds.

        Configure phase (real chroot, after each unpack round):
          `dpkg --configure -a` is run inside the chroot.  dpkg itself handles
          Depends ordering; we only track what got configured so we can update
          the unpack forest (removing satisfied Pre-Depends edges).

        Returns:
            True on completion.  Individual package failures are logged but do
            not abort the run — a partial chroot is still useful for diagnosis.
        """
        # All canonical packages — filter out virtual-package alias entries
        # (entries where the key differs from Package['Package']).
        all_pkgs = [
            p for p in self.__dependencytree.selected_pkgs
            if p == self.__dependencytree.selected_pkgs[p]['Package']
        ]

        # gcc-NN-base bootstrap: libc6 → libgcc-s1 → gcc-NN-base forms a
        # Pre-Depends cycle that the forest algorithm cannot break on its own.
        # We treat these packages as already installed (batch 0) so all other
        # packages see their deps satisfied from the start.
        _gcc_base = next(
            (p for p in all_pkgs if re.fullmatch(r'gcc-\d+-base', p)), None
        )
        if _gcc_base is None:
            tui.console.print(
                "WARNING: no gcc-NN-base found in selected set — "
                "circular Pre-Depends bootstrap may fail"
            )
            tui.console.warning("build_chroot: gcc-NN-base not found in selected_pkgs")
            libc_seed = ['libc6', 'libgcc-s1', 'libcrypt1']
        else:
            libc_seed = [_gcc_base, 'libc6', 'libgcc-s1', 'libcrypt1']

        libc_seed_set = set(libc_seed)

        # --- Build unpack forest ---
        # Each tree: root=pkg, children=Pre-Depends present in selected set
        # that are NOT in libc_seed (seed deps treated as already configured).
        unpack_forest: dict = {}
        for pkg in all_pkgs:
            if pkg in libc_seed_set:
                continue
            tree = utils.Tree()
            tree.add_node(pkg)
            for dep in self._resolve_pre_depends(pkg):
                if dep not in libc_seed_set:
                    try:
                        tree.add_node(dep, pkg)
                    except ValueError:
                        pass  # dep listed twice in Pre-Depends
            unpack_forest[pkg] = tree

        # --- Install ---
        self._setup_chroot_env()
        self._init_dpkg_database()
        _log_path = os.path.join(self.__dir_log, 'chroot-install.log')

        # /proc, /sys, /dev, /dev/pts are needed by many postinstall scripts
        # (systemctl detection, uname, pty operations).  Mounted before the
        # first dpkg --configure -a call, unmounted in the finally block.
        self._mount_chroot_fs()

        _result = True
        try:
            with open(_log_path, 'w') as fh:

                # Batch 0: libc circular-dep bootstrap unpacked first.
                tui.console.print(f"Batch 0 (bootstrap): {libc_seed}")
                fh.write(f'Batch 0 (bootstrap): {libc_seed}\n')
                self._unpack_packages(libc_seed, fh)
                unpacked = set(libc_seed)

                fh.write('\n--- Batch 0 configure ---\n')
                _boot_configured = self._configure_chroot(fh)
                configured = set(libc_seed) | _boot_configured

                _round = 1
                while unpack_forest:
                    _progress = False

                    ready_to_unpack = [
                        pkg for pkg, tree in unpack_forest.items()
                        if tree.is_childless and not tree.is_empty
                    ]

                    if not ready_to_unpack and unpack_forest:
                        # Circular Pre-Depends — force all remaining through.
                        tui.console.print(
                            f"WARNING: circular Pre-Depends detected — "
                            f"forcing batch of {len(unpack_forest)} packages"
                        )
                        tui.console.warning(
                            f"build_chroot round {_round}: forced Pre-Depends batch: "
                            f"{list(unpack_forest.keys())}"
                        )
                        ready_to_unpack = list(unpack_forest.keys())

                    if ready_to_unpack:
                        tui.console.print(
                            f"Round {_round} — unpacking {len(ready_to_unpack)} packages"
                        )
                        fh.write(f'\n--- Round {_round} unpack ---\n')
                        _newly_unpacked = self._unpack_packages(ready_to_unpack, fh)
                        unpacked.update(_newly_unpacked)
                        _progress = bool(_newly_unpacked)
                        for pkg in _newly_unpacked:
                            unpack_forest.pop(pkg, None)

                    # dpkg --configure -a handles Depends ordering internally.
                    # We only need to know what got configured to update the
                    # unpack forest (removing satisfied Pre-Depends edges).
                    fh.write(f'\n--- Round {_round} configure ---\n')
                    _newly_configured = self._configure_chroot(fh)
                    _new = _newly_configured - configured
                    if _new:
                        configured.update(_new)
                        for pkg in _new:
                            for other_tree in unpack_forest.values():
                                if other_tree.find_node(pkg):
                                    other_tree.delete_node(pkg)
                        _progress = True

                    if not _progress:
                        stuck_all = list(unpack_forest.keys())
                        tui.console.print(
                            f"ERROR: no progress in round {_round} — "
                            f"{len(stuck_all)} packages stuck in unpack forest"
                        )
                        tui.console.error(
                            f"build_chroot stuck in round {_round}: {stuck_all}"
                        )
                        _result = False
                        break

                    _round += 1

                # After the last unpack round the unpack forest is empty but
                # some packages may still be in the "unpacked" (not configured)
                # state.  One final dpkg --configure -a picks them all up.
                if _result:
                    tui.console.print("Final configure pass...")
                    fh.write('\n--- Final configure ---\n')
                    _final_configured = self._configure_chroot(fh, is_final=True)
                    configured.update(_final_configured)

        finally:
            self._umount_chroot_fs()

        if not _result:
            return False

        # Query dpkg inside the chroot for the definitive list of packages
        # that are NOT fully configured so the user gets a clear summary.
        _status_cmd = ['sudo', '-S', 'chroot', self.__dir_chroot,
                       'dpkg', '--get-selections']
        _status = subprocess.run(_status_cmd, input=self.__password + '\n',
                                 capture_output=True, text=True)
        _not_installed = [
            l.split()[0] for l in _status.stdout.splitlines()
            if l.endswith('\thalf-configured') or l.endswith('\tunpacked')
        ] if _status.returncode == 0 else []

        if _not_installed:
            tui.console.print(
                f"Chroot build done — {len(configured)} packages configured, "
                f"{len(_not_installed)} incomplete: {', '.join(_not_installed[:8])}"
                f"{'…' if len(_not_installed) > 8 else ''}"
            )
            tui.console.warning(
                f"build_chroot: {len(_not_installed)} packages not fully configured: "
                f"{_not_installed}"
            )
        else:
            tui.console.print(
                f"Chroot build complete — {len(configured)} packages installed "
                f"in {_round - 1} rounds — all packages fully configured"
            )

        # Ensure every installed kernel has an initramfs.  With --force-depends
        # linux-image can configure before initramfs-tools is ready, causing
        # update-initramfs to fail silently and leave no initrd.img-*.  We
        # detect and repair that here after all packages are confirmed configured.
        self._ensure_initramfs()

        # Apply post-install patches and overlay files now that all packages
        # are configured.  This is the right moment to override config files
        # or apply distro-specific fixups that would be clobbered by dpkg.
        self.post_install()

        # Write base system configuration files that dpkg does not create:
        # OS identity, hostname, hosts, fstab, machine-id, apt sources.
        self.generate_system_configs(debug=debug)

        return True

    # ── chroot configure helpers ─────────────────────────────────────────────

    def _mount_chroot_fs(self):
        """Bind-mount /proc, /sys, /dev, /dev/pts into the chroot.

        Required before running `dpkg --configure -a` inside the chroot.
        Many postinstall scripts check /proc/1/environ, call systemctl,
        or open ptys — they fail silently or loudly without these mounts.
        Always paired with _umount_chroot_fs() in a try/finally block.
        """
        _chroot = self.__dir_chroot
        _mounts = [
            (['-t', 'proc',   'proc',       f'{_chroot}/proc']),
            (['-t', 'sysfs',  'sysfs',      f'{_chroot}/sys']),
            (['--bind',       '/dev',        f'{_chroot}/dev']),
            (['--bind',       '/dev/pts',    f'{_chroot}/dev/pts']),
        ]
        for _args in _mounts:
            _dst = _args[-1]
            subprocess.run(['sudo', '-S', 'mkdir', '-p', _dst],
                           input=self.__password + '\n', capture_output=True, text=True)
            _proc = subprocess.run(['sudo', '-S', 'mount'] + _args,
                                   input=self.__password + '\n', capture_output=True, text=True)
            if _proc.returncode != 0:
                tui.console.warning(f'_mount_chroot_fs: mount {_dst}: {_proc.stderr.strip()}')

    def _umount_chroot_fs(self):
        """Unmount virtual filesystems from the chroot (reverse of _mount_chroot_fs).

        Uses -lf (lazy force) so a partial mount state from a previous failed
        run does not prevent cleanup.  Safe to call even if nothing was mounted.
        """
        _chroot = self.__dir_chroot
        for _dst in [
            f'{_chroot}/dev/pts',
            f'{_chroot}/dev',
            f'{_chroot}/sys',
            f'{_chroot}/proc',
        ]:
            subprocess.run(['sudo', '-S', 'umount', '-lf', _dst],
                           input=self.__password + '\n', capture_output=True, text=True)

    def _configure_chroot(self, fh, is_final: bool = False) -> set:
        """Run `dpkg --configure -a` and return the set of successfully configured package names.

        Automatically selects between two modes based on whether dpkg is present
        inside the chroot yet:

          Chroot mode (dpkg present in chroot):
            sudo env VAR=val chroot <dir> dpkg --configure -a
            `env` runs on the HOST (always available) and sets the environment
            before entering the chroot.  Scripts run inside the real chroot so
            tools like addgroup, shadowconfig, etc. operate on the chroot's files
            directly with no DPKG_ROOT confusion.

          Chrootless fallback (dpkg not yet in chroot — early bootstrap rounds):
            sudo env VAR=val dpkg --root=<dir> --force-script-chrootless --configure -a
            Used for the first few rounds before coreutils/dpkg are unpacked.
            DPKG_ROOT workarounds apply here, but the packages that configure in
            these early rounds (libc6, libssl3, …) do not trigger them.

        Args:
            is_final: True only for the post-loop cleanup pass.  Intermediate
                      round failures are expected (packages waiting on deps that
                      arrive in a later round) and are silently logged to file.
                      Only the final pass surfaces errors on the console so the
                      user gets a single, definitive signal.

        Returns a set of base package names (arch suffix stripped) that dpkg
        reported as "Setting up X (version)" — i.e., successfully configured.
        Failed packages are NOT in the returned set.
        """
        _chroot = self.__dir_chroot
        _dpkg_in_chroot = os.path.exists(os.path.join(_chroot, 'usr/bin/dpkg'))

        if _dpkg_in_chroot:
            # Real chroot: env runs on host (no /usr/bin/env needed inside chroot).
            # TEMPORARY: --force-depends bypasses version-constraint checks so
            # packages whose deps have a cache/deb skew (e.g. openssh needs
            # libssl3 >= 3.0.19 but repo has 3.0.18) still configure.
            # Remove once packages are refreshed from the security repo.
            _cmd = shlex.split(
                f'sudo -S env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true '
                f'chroot {_chroot} '
                f'dpkg --configure -a --force-confdef --force-confnew --force-depends'
            )
        else:
            # Chrootless fallback for early bootstrap rounds.
            # TEMPORARY: --force-depends — same reason as chroot mode above.
            _cmd = shlex.split(
                f'sudo -S env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true '
                f'dpkg --root={_chroot} --instdir={_chroot} '
                f'--admindir={_chroot}/var/lib/dpkg '
                f'--force-script-chrootless --force-confdef --force-confnew '
                f'--force-depends --configure -a'
            )

        _proc = subprocess.run(_cmd, input=self.__password + '\n',
                               capture_output=True, text=True, env=os.environ)
        fh.write(_proc.stdout)
        if _proc.returncode != 0:
            fh.write(_proc.stderr)
            _mode = 'chroot' if _dpkg_in_chroot else 'chrootless'
            if is_final:
                # Final pass failures are real — surface them immediately.
                _failed = [
                    l.split()[-1] for l in _proc.stdout.splitlines()
                    if l.startswith('dpkg: error processing package')
                ]
                tui.console.print(
                    f'Final configure had errors — {len(_failed)} package(s) failed: '
                    f'{", ".join(_failed[:5])}{"…" if len(_failed) > 5 else ""}'
                )
                tui.console.error(
                    f'_configure_chroot final ({_mode}): '
                    f'{len(_failed)} failed — see log for details'
                )
            else:
                # Intermediate round: expected for packages waiting on deps from
                # a later round.  Log to file only; do not clutter the console.
                tui.console.warning(
                    f'Round configure ({_mode}): some packages deferred — '
                    f'will retry in next round'
                )

        _configured: set = set()
        for _line in _proc.stdout.splitlines():
            _m = re.match(r'^Setting up (\S+?)(?::\S+)? \(', _line)
            if _m:
                _configured.add(_m.group(1))
        return _configured

    def _ensure_initramfs(self):
        """Generate initramfs for any kernel that is missing one.

        With --force-depends, linux-image may configure before initramfs-tools
        is ready.  update-initramfs then exits non-zero and leaves no initrd.
        This method scans /boot/ for vmlinuz-* files and, for each one that has
        no matching initrd.img-*, runs update-initramfs -c inside the chroot to
        create it.
        """
        _chroot = self.__dir_chroot
        _boot = os.path.join(_chroot, 'boot')

        _kernels = sorted(
            f for f in os.listdir(_boot) if f.startswith('vmlinuz-')
        ) if os.path.isdir(_boot) else []

        for _vmlinuz in _kernels:
            _kver  = _vmlinuz.replace('vmlinuz-', '')
            _initrd = os.path.join(_boot, f'initrd.img-{_kver}')
            if os.path.exists(_initrd):
                continue

            tui.console.print(f"Generating initramfs for {_kver} (was missing)...")
            _proc = subprocess.run(
                ['sudo', '-S', 'chroot', _chroot,
                 'update-initramfs', '-c', '-k', _kver],
                input=self.__password + '\n',
                capture_output=True, text=True
            )
            if _proc.returncode != 0:
                tui.console.print(f"WARNING: update-initramfs failed for {_kver} — see log")
                tui.console.warning(
                    f"_ensure_initramfs {_kver}: {_proc.stderr.strip()[:300]}"
                )
            else:
                tui.console.print(f"Initramfs generated: initrd.img-{_kver}")

    # ── Dependency sync from .deb files ──────────────────────────────────────

    def _check_dep_drift(self):
        """Patch Package dep fields from the on-disk .debs and verify resolution.

        The Packages cache and the downloaded .debs may be out of sync when a
        binNMU rebuild has shifted runtime deps under the same source version.
        Two stanzas can describe the "same" package with different Depends:

          Cache index entry (fetched from deb.debian.org)
              Package: libcom-err2
              Version: 1.47.0-2+b2
              Depends: libc6 (>= 2.17)

          Locally-built .deb in repo/
              Package: libcom-err2
              Version: 1.47.0-2
              Depends: libnsl2, libc6 (>= 2.17)

        The buildd's binNMU (+b2) was rebuilt after upstream silently dropped
        the libnsl2 link; we built from the unsuffixed source and still link
        against libnsl2.  If the configure-ordering forest is built from the
        cache, libcom-err2 looks like it only needs libc6 and is scheduled
        right after it — but dpkg installs the on-disk .deb, which actually
        Pre-Depends on libnsl2 being configured first, and refuses with:
            libcom-err2 depends on libnsl2; however:
            Package libnsl2 is not configured yet.

        Pass 1 — sync.  Overwrites depends / alt_depends / pre_depends /
        alt_pre_depends on every canonical Package object with what dpkg-deb
        reports.  All other fields (Version, Arch, Filename, …) are left
        untouched.  Drift is logged at info level (log tab only) in the form
        "Dep drift seen for package <pkg> from <cache_ver> to <disk_ver>".

        Pass 2 — verify.  After every package is synced, walk every (now-
        synced) dep field and check that each named dep is in selected_pkgs
        and that any version constraint is satisfied via apt_pkg.check_dep.
        OR-groups (alt_*) pass if at least one alternative resolves.  Any
        unresolved or version-mismatched dep is collected and the build
        aborts via RuntimeError — proceeding would just fail at install time
        with a less actionable message.
        """
        import package as _pkg_module
        for _pkg_name, _pkg_obj in self.__dependencytree.canonical_pkgs.items():
            _filename = os.path.basename(_pkg_obj.get('Filename', ''))
            if not _filename:
                continue
            _filename  = self.strip_build_version(_filename)
            _deb_path  = os.path.join(self.__dir_repo, _filename)
            if not os.path.exists(_deb_path):
                continue
            _proc = subprocess.run(
                ['dpkg-deb', '-f', _deb_path],
                capture_output=True, text=True
            )
            if _proc.returncode != 0 or not _proc.stdout.strip():
                continue
            _deb_pkg = _pkg_module.Package(_proc.stdout)
            if not _deb_pkg.isvalid:
                continue

            _drift = []
            for _field in ('pre_depends', 'depends', 'alt_pre_depends', 'alt_depends'):
                _cache_val = getattr(_pkg_obj, _field)
                _disk_val  = getattr(_deb_pkg, _field)
                if _cache_val != _disk_val:
                    _drift.append((_field, _cache_val, _disk_val))
            if _drift:
                _cache_ver = _pkg_obj.get('Version', '?')
                _disk_ver  = _deb_pkg.get('Version', '?')
                tui.console.info(
                    f"Dep drift seen for package {_pkg_name} "
                    f"from {_cache_ver} to {_disk_ver}"
                )
                for _field, _cache_val, _disk_val in _drift:
                    tui.console.info(f"  {_field}: from {_cache_val} to {_disk_val}")

            _pkg_obj.depends         = _deb_pkg.depends
            _pkg_obj.alt_depends     = _deb_pkg.alt_depends
            _pkg_obj.pre_depends     = _deb_pkg.pre_depends
            _pkg_obj.alt_pre_depends = _deb_pkg.alt_pre_depends

        self._verify_dep_resolution()

    def _verify_dep_resolution(self):
        """Second pass: confirm every synced dep resolves in selected_pkgs.

        For each canonical package, walk pre_depends + depends as hard
        requirements and alt_pre_depends + alt_depends as OR-groups.  A hard
        requirement passes when its name is in selected_pkgs (real or virtual
        alias) and the version constraint, if any, is satisfied by the
        provider's own version.  An OR-group passes when at least one
        alternative passes.  Aggregate all violations and raise at the end so
        a single run reports every problem rather than failing on the first.
        """
        import apt_pkg
        _selected = self.__dependencytree.selected_pkgs

        def _resolves(dep_tuple):
            _name, _ver, _op = dep_tuple[0], dep_tuple[1], dep_tuple[2]
            _provider = _selected.get(_name)
            if _provider is None:
                return False, 'unresolved'
            if not _op:
                return True, None
            try:
                if apt_pkg.check_dep(str(_provider.version), _op, str(_ver)):
                    return True, None
                return False, f'version mismatch (have {_provider.version}, need {_op} {_ver})'
            except SystemError as e:
                return False, f'check_dep error: {e}'

        _violations = []
        for _pkg_name, _pkg_obj in self.__dependencytree.canonical_pkgs.items():
            for _field in ('pre_depends', 'depends'):
                for _dep in getattr(_pkg_obj, _field):
                    _ok, _why = _resolves(_dep)
                    if not _ok:
                        _violations.append(
                            f"{_pkg_name} {_field}: {_dep[0]} "
                            f"({_dep[2]} {_dep[1]}) — {_why}"
                        )
            for _field in ('alt_pre_depends', 'alt_depends'):
                for _group in getattr(_pkg_obj, _field):
                    if not any(_resolves(_alt)[0] for _alt in _group):
                        _alts = ' | '.join(
                            f"{_alt[0]} ({_alt[2]} {_alt[1]})" if _alt[2] else _alt[0]
                            for _alt in _group
                        )
                        _violations.append(
                            f"{_pkg_name} {_field}: [{_alts}] — no alternative resolves"
                        )

        if _violations:
            tui.console.error(
                f"_verify_dep_resolution: {len(_violations)} unresolved dep(s) after sync"
            )
            for _v in _violations:
                tui.console.error(f"  {_v}")
            raise RuntimeError(
                f"_verify_dep_resolution: {len(_violations)} unresolved dep(s); "
                "see log for details"
            )

    # ── Dependency resolution helpers ─────────────────────────────────────────

    def _resolve_pre_depends(self, pkg_name: str) -> list:
        """Return Pre-Depends canonical package names for pkg_name present in selected_pkgs.

        Handles both single-alternative and OR-alternative Pre-Depends.  For OR
        groups (e.g. 'systemd-sysv | sysvinit-core'), the first alternative that
        is present in selected_pkgs is used as the ordering constraint.  Names not
        in selected_pkgs are omitted — satisfied by something already on the host.

        Virtual-package names (e.g. 'awk') are resolved to the real Package name
        (e.g. 'mawk') so that the unpack-forest node deletion in build_chroot()
        works correctly: dpkg reports "Setting up mawk", not "Setting up awk".
        Without this resolution the virtual-name child node would never be deleted
        and the parent would stay non-childless forever.
        """
        pkg = self.__dependencytree.selected_pkgs.get(pkg_name)
        if pkg is None:
            return []
        selected = self.__dependencytree.selected_pkgs
        result: list = []
        seen: set = set()
        for dep in pkg.pre_depends:
            dep_name = dep[0]
            if dep_name in selected:
                canonical = selected[dep_name]['Package']
                if canonical not in seen:
                    seen.add(canonical)
                    result.append(canonical)
        for group in pkg.alt_pre_depends:
            for alt in group:
                dep_name = alt[0]
                if dep_name in selected:
                    canonical = selected[dep_name]['Package']
                    if canonical not in seen:
                        seen.add(canonical)
                        result.append(canonical)
                    break
        return result

    def _resolve_depends(self, pkg_name: str) -> list:
        """Return Depends names for pkg_name that are present in selected_pkgs.

        Handles both single-alternative and OR-alternative Depends.  For OR
        groups, the first alternative present in selected_pkgs is used so that
        configure ordering tracks the actual package we installed.
        """
        pkg = self.__dependencytree.selected_pkgs.get(pkg_name)
        if pkg is None:
            return []
        selected = self.__dependencytree.selected_pkgs
        result = [dep[0] for dep in pkg.depends if dep[0] in selected]
        for group in pkg.alt_depends:
            for alt in group:
                if alt[0] in selected:
                    result.append(alt[0])
                    break
        return result

    # ── dpkg execution helpers ────────────────────────────────────────────────

    def _init_dpkg_database(self):
        """Create the dpkg database structure inside the chroot.

        dpkg refuses to run if /var/lib/dpkg and its subdirectories do not
        exist, and requires /var/lib/dpkg/status and /var/lib/dpkg/available
        to be present (even if empty) before the first package is installed.

        All operations run via sudo -S because after a failed or partial
        previous run the chroot tree may be owned by root, making os.makedirs
        silently pass exist_ok but leave files in an inconsistent state.
        sudo touch is idempotent — safe to call on every startup.
        """
        _dpkg_dirs = [
            'var/lib/dpkg',
            'var/lib/dpkg/info',
            'var/lib/dpkg/updates',
            'var/lib/dpkg/triggers',
            'var/cache/apt/archives',
        ]
        for _d in _dpkg_dirs:
            _path = os.path.join(self.__dir_chroot, _d)
            subprocess.run(
                ['sudo', '-S', 'mkdir', '-p', _path],
                input=self.__password + '\n',
                capture_output=True, text=True
            )

        # status and available must exist before dpkg's first invocation.
        for _f in ['var/lib/dpkg/status', 'var/lib/dpkg/available']:
            _path = os.path.join(self.__dir_chroot, _f)
            subprocess.run(
                ['sudo', '-S', 'touch', _path],
                input=self.__password + '\n',
                capture_output=True, text=True
            )

        # --force-script-chrootless runs pre/post-install scripts on the HOST
        # but exports DPKG_ROOT=<chroot> to them.  Debconf's Config.pm reads
        # $DPKG_ROOT/etc/debconf.conf — if it doesn't exist, every package
        # whose maintainer script uses debconf (libc6, bash, apt, …) fails
        # its pre-install script with "No config file found".
        # Writing a minimal debconf.conf into the chroot's /etc/ before the
        # first dpkg call makes debconf find its config and proceed silently
        # in noninteractive mode.
        _debconf_conf_content = (
            'Config: configdb\n'
            'Templates: templatedb\n'
            '\n'
            'Name: config\n'
            'Driver: File\n'
            'Mode: 644\n'
            'Filename: /var/cache/debconf/config.dat\n'
            '\n'
            'Name: passwords\n'
            'Driver: File\n'
            'Mode: 600\n'
            'Backup: false\n'
            'Required: false\n'
            'Filename: /var/cache/debconf/passwords.dat\n'
            '\n'
            'Name: templates\n'
            'Driver: File\n'
            'Mode: 644\n'
            'Filename: /var/cache/debconf/templates.dat\n'
            '\n'
            'Name: configdb\n'
            'Driver: Stack\n'
            'Stack: config, passwords\n'
            '\n'
            'Name: templatedb\n'
            'Driver: Stack\n'
            'Stack: templates\n'
        )
        self._write_chroot_file('/etc/debconf.conf', _debconf_conf_content)

        # Ensure the debconf data directory and seed files exist so debconf
        # does not fail trying to create them during installation.
        _debconf_dir = os.path.join(self.__dir_chroot, 'var/cache/debconf')
        subprocess.run(
            ['sudo', '-S', 'mkdir', '-p', _debconf_dir],
            input=self.__password + '\n', capture_output=True, text=True
        )
        for _f in ['config.dat', 'passwords.dat', 'templates.dat']:
            subprocess.run(
                ['sudo', '-S', 'touch', os.path.join(_debconf_dir, _f)],
                input=self.__password + '\n', capture_output=True, text=True
            )

    def _setup_chroot_env(self):
        """Set environment variables required for non-interactive dpkg in a chroot."""
        os.environ['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
        os.environ['DPKG_ROOT'] = self.__dir_chroot
        os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
        os.environ['DEBCONF_NONINTERACTIVE_SEEN'] = 'true'

    def _get_deb_files(self, pkg_list: list) -> list:
        """Resolve package names to absolute .deb paths in the repo directory.

        Uses the Filename field from the binary Packages index and strips any
        binNMU suffix (+bN) via strip_build_version() so the path matches what
        dpkg-buildpackage actually produced on disk.
        """
        file_list = []
        for pkg in pkg_list:
            _filename = os.path.basename(
                self.__dependencytree.selected_pkgs[pkg]['Filename']
            )
            _filename = self.strip_build_version(_filename)
            _filepath = os.path.join(self.__dir_repo, _filename)
            if not os.path.exists(_filepath):
                tui.console.print(f"WARNING: .deb not found, skipping: {_filename}")
                tui.console.warning(f"_get_deb_files: missing {_filepath}")
                continue
            file_list.append(_filepath)
        return file_list

    def _unpack_packages(self, pkg_list: list, fh) -> set:
        """Run dpkg --unpack for pkg_list inside the chroot.

        --force-script-chrootless allows maintainer scripts to run without
        being inside a chroot bind-mount.  --no-triggers defers trigger
        processing to the configure phase.

        Returns the set of base package names that were successfully unpacked
        (parsed from dpkg's "Unpacking NAME:arch (ver)" stdout lines).
        Packages that dpkg rejected (pre-dep failures, etc.) are not in the
        returned set and will be retried in a later round.
        """
        if not pkg_list:
            return set()
        _chroot = self.__dir_chroot
        _cmd = shlex.split(
            f'sudo -S env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true '
            f'dpkg --root={_chroot} --instdir={_chroot} '
            f'--admindir={_chroot}/var/lib/dpkg '
            f'--force-script-chrootless -D1 --no-triggers --unpack'
        ) + self._get_deb_files(pkg_list)

        fh.write(f'Unpack: {" ".join(pkg_list)}\n')
        _proc = subprocess.run(_cmd, input=self.__password + '\n',
                               capture_output=True, text=True, env=os.environ)
        fh.write(_proc.stdout)
        if _proc.returncode != 0:
            fh.write(_proc.stderr)
            tui.console.print(
                f'Error unpacking {pkg_list[:3]}{"…" if len(pkg_list) > 3 else ""}: '
                f'{_proc.stderr[:200]}'
            )

        # Parse successfully unpacked package names from dpkg output.
        # dpkg prints "Unpacking NAME:arch (version) ..." for each success.
        _unpacked: set = set()
        for _line in _proc.stdout.splitlines():
            _m = re.match(r'^Unpacking (\S+)', _line)
            if _m:
                _unpacked.add(_m.group(1).split(':')[0])
        return _unpacked

    def get_install_sequence(self, selected_pkgs: [], installed_pkgs: []) -> []:
        """Produce a topologically sorted installation order for selected_pkgs.

        Algorithm — iterative leaf-removal (Kahn's algorithm on a forest):
          1. Build one dependency tree per package: root = package, children =
             its direct dependencies (Package.depends_on).
          2. Strip any dependency node that is already in installed_pkgs — those
             constraints are already satisfied and don't affect ordering.
          3. Repeatedly harvest all trees whose root has no remaining children
             (i.e. all dependencies satisfied).  Each harvest is one installation
             batch; packages within a batch are mutually independent and can be
             installed together.  After harvesting, remove those package names
             from every other tree's dependency lists so their dependents may
             become eligible in the next round.
          4. Terminate when no trees remain (success) or when a round produces
             nothing despite non-empty trees still existing (failure).

        The failure case arises in two situations:
          - True circular dependency (A→B→A): neither tree ever becomes childless.
          - Dependency on a package outside selected_pkgs that is also not in
            installed_pkgs: the child node is never removed, so the parent tree
            never clears.

        Args:
            selected_pkgs:  Package names to install, in any order.
            installed_pkgs: Package names already present in the chroot; their
                            dependency obligations are treated as satisfied.

        Returns:
            List of batches, where each batch is a list of package names that
            can be installed together (all their deps satisfied by earlier batches
            or by installed_pkgs).

        Raises:
            ValueError: if a circular or unresolvable dependency is detected,
                        with a message naming each stuck package and its unmet deps.
        """
        sequence = []
        collection = []

        # Build one tree per package with its direct dependencies as children.
        for _pkg in selected_pkgs:
            tree = utils.Tree()
            tree.add_node(_pkg)
            collection.append(tree)

            for leaf in self.__dependencytree.selected_pkgs[_pkg].depends_on:
                tree.add_node(leaf, tree.root.value)

        # Remove already-installed packages from all dependency lists so they
        # don't block packages that only depend on them.
        for _pkg in installed_pkgs:
            for _tree in collection:
                if _tree.find_node(_pkg):
                    _tree.delete_node(_pkg)

        while True:
            # Childless non-empty trees: root's dependencies are all satisfied.
            pkg_list = [_tree.root.value for _tree in collection
                        if _tree.is_childless and not _tree.is_empty]

            if not pkg_list:
                # No childless trees this round.  If any trees are still
                # non-empty their deps can never be cleared — circular or
                # unresolvable dependency.
                #
                # NOTE: the original code checked `is_childless and not is_empty`
                # here, which is the same predicate as pkg_list and is therefore
                # always False when pkg_list is empty.  The correct check is
                # simply `not is_empty`.
                stuck = [_tree for _tree in collection if not _tree.is_empty]
                if stuck:
                    _details = '; '.join(
                        f"{_tree.root.value} waiting on "
                        f"[{', '.join(c.value for c in _tree.root.children)}]"
                        for _tree in stuck
                    )
                    raise ValueError(
                        f"Circular or unresolvable dependencies detected: {_details}"
                    )
                # All trees are empty — every package was placed in a batch.
                break

            # Remove this batch's packages from every tree so their dependents
            # may become childless in the next round.
            for _pkg in pkg_list:
                for _tree in collection:
                    if _tree.find_node(_pkg):
                        _tree.delete_node(_pkg)

            sequence.append(pkg_list)

        return sequence

    def _build_chroot_directories(self):
        """Create the standard FHS directory structure inside the chroot.

        Sequence matters here — usrmerge symlinks must be created before any
        directory tree expansion, otherwise os.makedirs will create a real /lib
        (and /bin, /sbin) which conflicts with dpkg's expectation that those
        paths are symlinks into /usr.

        Step 1 — usrmerge symlinks
            Since Debian bookworm all of /lib, /lib32, /lib64, /bin, /sbin are
            symlinks into /usr (the 'usrmerge' change).  dpkg and the packages
            it installs hard-depend on this layout: if any of those names exist
            as a real directory, package installation into /usr/lib (for example)
            will not be visible through /lib, breaking ld.so and any binary that
            embeds the old path.

            The symlink targets are relative (e.g. 'usr/lib' not '/usr/lib') so
            they remain valid when the chroot is bind-mounted or moved.  The
            /usr/<name> target directories are created with os.makedirs before
            os.symlink so the link always points at an existing directory.

        Step 2 — remaining FHS tree
            Only directories that cannot exist as symlinks are listed here.
            /usr/bin, /usr/sbin, /usr/lib* are already created in step 1, so
            they appear in the expansion list but os.makedirs(exist_ok=True)
            will simply skip them.

        Ref: https://www.linuxfromscratch.org/lfs/view/development/chapter07/creatingdirs.html
        Note: man(1..8) directories are intentionally omitted for now.
        TODO: load dir/owner/permission list from a config file rather than hardcoding.
        """

        # Step 1: usrmerge — create /usr/<name> targets then symlink the legacy
        # top-level names to them.  Order: mkdir target → symlink, so the link
        # is never dangling even if a later step fails partway through.
        _usrmerge = ['bin', 'sbin', 'lib', 'lib32', 'lib64']
        for _name in _usrmerge:
            _target = os.path.join(self.__dir_chroot, 'usr', _name)
            _link   = os.path.join(self.__dir_chroot, _name)
            os.makedirs(_target, exist_ok=True)
            if not os.path.lexists(_link):
                # Relative target keeps the symlink valid after the chroot is
                # moved or bind-mounted; 'usr/lib' resolves relative to chroot/
                # (the parent of the symlink), giving chroot/usr/lib.
                os.symlink(os.path.join('usr', _name), _link)

        # Step 2: remaining FHS tree.  /usr/{bin,sbin,lib,...} are listed
        # explicitly so the expansion is self-documenting, even though step 1
        # already created them — makedirs with exist_ok=True is idempotent.
        # /lib/{firmware} is intentionally absent: firmware lives at
        # /usr/lib/firmware and is reachable via the /lib symlink.
        dir_structure = [
            '/{boot,home,mnt,opt,srv,sys,proc,dev}',
            '/etc/{opt,sysconfig}',
            '/media/{floppy,cdrom}',
            '/usr/{bin,sbin,lib,lib32,lib64,local,include,src,share}',
            '/usr/lib/{firmware}',
            '/usr/local/{bin,lib,sbin,include,src,share}',
            '/usr/share/{color,dict,doc,info,locale,man,misc,terminfo,zoneinfo}',
            '/usr/local/share/{color,dict,doc,info,locale,man,misc,terminfo,zoneinfo}',
            '/var/{cache,local,log,mail,opt,spool}',
            '/var/lib/{color,misc,locate}',
        ]

        for _dir in dir_structure:
            utils.create_folders(self.__dir_chroot + _dir)

    @staticmethod
    def strip_build_version(file: str) -> str:
        # stripping build revisions, because these do not reflect on source code builds
        _name, _ext = os.path.splitext(file)
        _name = _name.split('_')
        if len(_name) != 3:
            raise ValueError(f"Incorrectly formatted package filename: {file!r}")
        _pkg_name = _name[0]
        _version = _name[1]
        _arch = _name[2]

        _version = re.sub(r"\+b\d+$", "", _version)
        file = _pkg_name + '_' + _version + '_' + _arch + _ext
        return file

    def pre_install(self):
        # Two parts here - copy files and then run commands
        # TODO: Let it load from file rather than hard coding it, risk of something malicious coming in though
        # Parse files to copy
        for root, dirs, files in os.walk(self.__dir_preinstall_patch):

            if len(files) == 0:
                continue

            # reached the list of files, here on three steps will be taken
            # use relative path and create if not existing in chroot dir
            chroot_relative_dir = root.replace(self.__dir_preinstall_patch, self.__dir_chroot)
            pathlib.Path(chroot_relative_dir).mkdir(parents=True, exist_ok=True)

            for _file in files:
                _orig_file = os.path.join(root, _file)
                if os.path.splitext(_file)[1] != '.patch':
                    # Non-patch files are copied directly into the chroot directory.
                    # Note: cp does not preserve all permissions — packages that
                    # need specific ownership must be handled in the cmd_list below.
                    _proc = subprocess.run(['sudo', '-S', 'cp', _orig_file, chroot_relative_dir],
                                           input=self.__password + '\n', capture_output=True, text=True, env=os.environ)
                    if _proc.returncode != 0:
                        tui.console.print(f'Error: Failed copying pre-install file — {_file}')
                        tui.console.error(f'pre_install cp {_file}: {_proc.stderr}')
                else:
                    # Patch files are applied relative to chroot_relative_dir.
                    # Use -i to pass the patch file directly; '<' is a shell
                    # redirect and is not valid as a subprocess argument.
                    _proc = subprocess.run(['patch', '-p1', '-i', _orig_file],
                                           cwd=chroot_relative_dir,
                                           capture_output=True, text=True, env=os.environ)
                    if _proc.returncode != 0:
                        tui.console.print(f'Error: Failed applying pre-install patch — {_file}')
                        tui.console.error(f'pre_install patch {_file}: {_proc.stderr}')

        # Parse commands to execute, since nothing else has been till now, it is usually used to set right permission
        # All commands use sudo -S so the password is read from stdin rather than
        # the terminal (which would corrupt TUI rendering).  The '\n' terminator
        # is required by sudo -S — without it sudo blocks waiting for more input.
        cmd_list = [f'sudo -S ln -sfv /run {self.__dir_chroot}/var/run',
                    f'sudo -S ln -sfv /run/lock {self.__dir_chroot}/var/lock',
                    f'sudo -S install -dv -m 0750 {self.__dir_chroot}/root',
                    f'sudo -S install -dv -m 1777 {self.__dir_chroot}/tmp {self.__dir_chroot}/var/tmp',
                    f'sudo -S chgrp -v utmp {self.__dir_chroot}/var/log/lastlog',
                    f'sudo -S chmod -v 664 {self.__dir_chroot}/var/log/lastlog',
                    f'sudo -S chmod -v 600 {self.__dir_chroot}/var/log/btmp',
                    f'sudo -S chmod -R 755 {self.__dir_chroot}/etc/'
                    ]

        for _cmd in cmd_list:
            _proc = subprocess.run(shlex.split(_cmd), input=self.__password + '\n',
                                   capture_output=True, text=True)
            if _proc.returncode != 0:
                tui.console.print(f"WARNING: pre-install command failed: {_cmd}")
                tui.console.warning(f"pre_install cmd: {_cmd}\n{_proc.stdout.strip()}")

    def post_install(self):
        """Apply post-install overlay files and patches into the chroot.

        Runs after all packages are unpacked and configured.  Use this for:
          - Distro-specific config file overrides that dpkg would otherwise
            reset (e.g. /etc/os-release, /etc/hostname, /etc/locale.conf)
          - Files that must land after dpkg creates the target path
          - Patches against files installed by packages

        Directory layout mirrors pre_install: files under dir_patch_postinstall
        are placed at the same relative path inside the chroot.  Files with a
        .patch extension are applied with patch -p1 -i; all other files are
        copied verbatim.

        Unlike pre_install there is no hardcoded cmd_list — post-install
        permission fixups belong in the overlay files themselves or in
        generate_system_configs().
        """
        for root, dirs, files in os.walk(self.__dir_postinstall_patch):
            if not files:
                continue

            # Mirror the source directory structure into the chroot.
            chroot_relative_dir = root.replace(self.__dir_postinstall_patch, self.__dir_chroot)
            pathlib.Path(chroot_relative_dir).mkdir(parents=True, exist_ok=True)

            for _file in files:
                _orig_file = os.path.join(root, _file)
                if os.path.splitext(_file)[1] != '.patch':
                    _proc = subprocess.run(
                        ['sudo', '-S', 'cp', _orig_file, chroot_relative_dir],
                        input=self.__password + '\n', capture_output=True, text=True, env=os.environ
                    )
                    if _proc.returncode != 0:
                        tui.console.print(f'Error: Failed copying post-install file — {_file}')
                        tui.console.error(f'post_install cp {_file}: {_proc.stderr}')
                else:
                    _proc = subprocess.run(
                        ['patch', '-p1', '-i', _orig_file],
                        cwd=chroot_relative_dir,
                        capture_output=True, text=True, env=os.environ
                    )
                    if _proc.returncode != 0:
                        tui.console.print(f'Error: Failed applying post-install patch — {_file}')
                        tui.console.error(f'post_install patch {_file}: {_proc.stderr}')

    def generate_system_configs(self, debug: bool = False):
        """Write base system configuration files into the chroot.

        Args:
            debug: When True, also write
                /etc/systemd/journald.conf.d/50-console.conf to forward all
                journal entries to /dev/console (= ttyS0 with serial boot).
                Off by default to keep production images quiet.

        Covers files that dpkg does not create but the OS needs to be
        functional and identifiable.  All files are written via
        _write_chroot_file() which uses sudo -S tee so ownership is correct
        even after dpkg has made the chroot root-owned.

        Files written:
          /etc/os-release      — OS identity (name, version, codename)
          /etc/hostname        — default hostname
          /etc/hosts           — localhost resolution
          /etc/fstab           — virtual filesystem mount points
          /etc/machine-id      — empty; systemd generates on first boot
          /etc/apt/sources.list — APT repository for the installed system
        """
        cfg = self.__config

        # /etc/os-release — standard file read by systemd, lsb_release, etc.
        self._write_chroot_file('/etc/os-release', (
            f'# Athena Linux — (c) 2025-26 Harkirat S Virk\n'
            f'NAME="{cfg.build_codename}"\n'
            f'VERSION="{cfg.build_version}"\n'
            f'ID=athena\n'
            f'ID_LIKE=debian\n'
            f'VERSION_CODENAME={cfg.build_codename.lower()}\n'
            f'PRETTY_NAME="{cfg.build_codename} {cfg.build_version}"\n'
            f'HOME_URL="https://athenalinux.org"\n'
            f'VENDOR_NAME="Harkirat S Virk"\n'
        ))

        # /etc/issue — pre-login banner on TTY consoles. Uses agetty escapes:
        #   \S = PRETTY_NAME from os-release, \r = kernel, \l = tty
        self._write_chroot_file('/etc/issue', (
            f'{cfg.build_codename} {cfg.build_version} \\n \\l\n'
            f'\n'
            f'(c) 2025-26 Harkirat S Virk\n'
            f'\n'
        ))

        # /etc/hostname — a sensible default; can be changed post-install.
        self._write_chroot_file('/etc/hostname', 'athena\n')

        # /etc/hosts — minimal localhost entries required for basic name
        # resolution before a real DNS resolver is configured.
        self._write_chroot_file('/etc/hosts', (
            '127.0.0.1   localhost\n'
            '127.0.1.1   athena\n'
            '::1         localhost ip6-localhost ip6-loopback\n'
            'ff02::1     ip6-allnodes\n'
            'ff02::2     ip6-allrouters\n'
        ))

        # /etc/fstab — virtual filesystems that systemd or init needs at boot.
        # Real block devices (root, swap, efi) are left out — they are image-
        # or hardware-specific and should be added by the image-build stage.
        self._write_chroot_file('/etc/fstab', (
            '# <file system>  <mount point>  <type>     <options>  <dump>  <pass>\n'
            'proc             /proc          proc       defaults   0       0\n'
            'sysfs            /sys           sysfs      defaults   0       0\n'
            'devtmpfs         /dev           devtmpfs   defaults   0       0\n'
            'tmpfs            /tmp           tmpfs      defaults   0       0\n'
        ))

        # /etc/machine-id — empty file; systemd-machine-id-setup will populate
        # it on first boot.  Must exist (even empty) for systemd to start.
        self._write_chroot_file('/etc/machine-id', '')

        # /etc/apt/sources.list — lets the installed system update itself
        # from the same mirrors used to build it.  Composed from the
        # configured mirrors so a config change rebases the live system too.
        # Extra components (contrib/non-free/non-free-firmware) added to each
        # line so the running system can install firmware/non-free pkgs even
        # though our build only consumes 'main'.
        _extra_comp = 'contrib non-free non-free-firmware'
        _lines = [
            f'deb {_m.url} {_m.suite} {_m.component} {_extra_comp}\n'
            for _m in cfg.mirrors
        ]
        self._write_chroot_file('/etc/apt/sources.list', ''.join(_lines))

        # Forward all journal entries to /dev/console (= ttyS0 via console=ttyS0
        # kernel cmdline) so auth/PAM failures are visible on serial output
        # even when autologin is broken and no interactive login is possible.
        # Opt-in via 'build_chroot with_debug' — production builds skip this.
        if debug:
            self._write_chroot_file(
                '/etc/systemd/journald.conf.d/50-console.conf',
                '[Journal]\n'
                'ForwardToConsole=yes\n'
                'MaxLevelConsole=info\n'
            )
            tui.console.print("journald console forwarding configured (ttyS0) — debug mode")

        _proc = subprocess.run(
            ['sudo', '-S', 'systemd-firstboot',
             f'--root={self.__dir_chroot}',
             '--root-password=root',
             '--hostname=athena',
             '--timezone=UTC',
             '--locale=C.UTF-8',
             '--setup-machine-id',
             '--force'],
            input=self.__password + '\n',
            capture_output=True, text=True
        )
        if _proc.returncode != 0:
            tui.console.print(f"WARNING: systemd-firstboot failed: {_proc.stderr.strip()}")
            tui.console.warning(f"systemd-firstboot stderr: {_proc.stderr.strip()}")
        else:
            tui.console.print("systemd-firstboot: root password / hostname / machine-id configured")

        tui.console.print("System configuration files written")

    def _write_chroot_file(self, rel_path: str, content: str):
        """Write content to rel_path inside the chroot as root.

        Uses a NamedTemporaryFile + sudo cp rather than sudo tee with stdin,
        because when sudo's credential is already cached it does NOT consume the
        password line from stdin, so tee would receive and write the raw password
        as the first line of the file.

        Args:
            rel_path: Path relative to the chroot root (e.g. '/etc/hostname').
            content:  Text content to write; may be empty.
        """
        _dest = os.path.join(self.__dir_chroot, rel_path.lstrip('/'))

        _parent = os.path.dirname(_dest)
        subprocess.run(
            ['sudo', '-S', 'mkdir', '-p', _parent],
            input=self.__password + '\n', capture_output=True, text=True
        )

        with tempfile.NamedTemporaryFile(mode='w', suffix='.chroot-write', delete=False) as _tf:
            _tf.write(content)
            _tmp_path = _tf.name

        try:
            _proc = subprocess.run(
                ['sudo', '-S', 'cp', _tmp_path, _dest],
                input=self.__password + '\n', capture_output=True, text=True
            )
            if _proc.returncode != 0:
                tui.console.print(f"ERROR: Failed to write {rel_path} into chroot")
                tui.console.error(f"_write_chroot_file {rel_path}: {_proc.stderr}")
        finally:
            os.unlink(_tmp_path)

    # ── ISO build ─────────────────────────────────────────────────────────────

    def build_iso(self) -> bool:
        """Create a bootable hybrid BIOS/EFI ISO from the assembled chroot.

        Steps:
          1. Locate the latest kernel (vmlinuz-*) and initramfs (initrd.img-*)
             installed in chroot/boot/ by the linux-image package.
          2. Create a staging tree under dir_image/staging/.
          3. Copy kernel and initramfs into staging/boot/.
          4. Write staging/boot/grub/grub.cfg configured for live-boot — the
             'boot=live' parameter tells live-boot to mount the squashfs as root
             with an overlayfs for writes.
          5. Create staging/live/filesystem.squashfs from the chroot via
             mksquashfs, excluding runtime virtual directories (proc, sys, dev,
             run, tmp) that live-boot mounts fresh at boot.
          6. Run grub-mkrescue to produce a hybrid BIOS+EFI ISO.

        Logs for the long-running steps are written to dir_log/mksquashfs.log
        and dir_log/grub-mkrescue.log.

        Returns:
            True on success, False if any step fails.
        """
        import glob
        import shutil

        # ── Step 1: locate kernel and initramfs ───────────────────────────────
        _boot = os.path.join(self.__dir_chroot, 'boot')
        _kernels = sorted(glob.glob(os.path.join(_boot, 'vmlinuz-*')))
        _initrds = sorted(glob.glob(os.path.join(_boot, 'initrd.img-*')))

        if not _kernels:
            tui.console.print("ERROR: no kernel found in chroot/boot/ — is linux-image installed?")
            tui.console.error("build_iso: no vmlinuz-* in chroot/boot/")
            return False
        if not _initrds:
            tui.console.print("ERROR: no initramfs found in chroot/boot/ — is initramfs-tools installed?")
            tui.console.error("build_iso: no initrd.img-* in chroot/boot/")
            return False

        # Use the latest kernel version (highest sort order).
        _kernel = _kernels[-1]
        _initrd = _initrds[-1]
        tui.console.print(f"Kernel  : {os.path.basename(_kernel)}")
        tui.console.print(f"Initrd  : {os.path.basename(_initrd)}")

        # ── Step 2: create staging tree ───────────────────────────────────────
        _staging      = os.path.join(self.__dir_image, 'staging')
        _staging_boot = os.path.join(_staging, 'boot')
        _staging_grub = os.path.join(_staging, 'boot', 'grub')
        _staging_live = os.path.join(_staging, 'live')

        for _d in [_staging_boot, _staging_grub, _staging_live]:
            os.makedirs(_d, exist_ok=True)

        # ── Step 3: copy kernel and initramfs ─────────────────────────────────
        shutil.copy2(_kernel, os.path.join(_staging_boot, 'vmlinuz'))
        shutil.copy2(_initrd, os.path.join(_staging_boot, 'initrd.img'))
        tui.console.print("Kernel and initramfs copied to staging")

        # ── Step 4: write grub.cfg ────────────────────────────────────────────
        # 'boot=live' is the live-boot trigger; live-boot locates the squashfs
        # under /live/filesystem.squashfs on the boot device and mounts it as
        # the root filesystem with overlayfs.
        cfg = self.__config
        # Strip any stray quotes that may be embedded in the config values
        # (e.g. VERSION = "0.1" parsed with the surrounding quotes intact).
        _codename = cfg.build_codename.strip('"').strip("'")
        _version  = cfg.build_version.strip('"').strip("'")
        _grub_cfg = (
            'set default=0\n'
            'set timeout=5\n'
            '\n'
            f'menuentry "{_codename} {_version}" {{\n'
            # boot=live   — triggers live-boot to find and mount the squashfs root
            # components  — tells live-boot to activate all its hook scripts
            # console=tty0 — ensures kernel messages go to the screen (visible in QEMU)
            # nomodeset   — disables KMS; prevents blank/garbled screen in QEMU/VMs
            '    linux  /boot/vmlinuz boot=live components username=root console=tty0 nomodeset\n'
            '    initrd /boot/initrd.img\n'
            '}\n'
        )
        with open(os.path.join(_staging_grub, 'grub.cfg'), 'w') as fh:
            fh.write(_grub_cfg)
        tui.console.print("grub.cfg written")

        # ── Step 5: create squashfs ───────────────────────────────────────────
        # Runtime virtual directories (proc, sys, dev, run, tmp) must NOT be
        # excluded as directories — live-boot's initramfs bind-mounts /dev,
        # /proc, /sys, /run into the new root and needs those directories to
        # exist as mount points inside the squashfs.  We only exclude their
        # CONTENTS (which are empty after _umount_chroot_fs anyway) by passing
        # each entry inside the dir rather than the dir itself.
        # -noappend overwrites any previous squashfs.
        _squashfs   = os.path.join(_staging_live, 'filesystem.squashfs')
        _squash_log = os.path.join(self.__dir_log, 'mksquashfs.log')

        # Collect any actual files inside the runtime dirs to exclude (normally
        # empty after unmounting, but defensive in case something was left).
        _exclude_args = []
        for _d in ['proc', 'sys', 'dev', 'run', 'tmp']:
            _dir_path = os.path.join(self.__dir_chroot, _d)
            if not os.path.isdir(_dir_path):
                continue
            try:
                for _entry in os.listdir(_dir_path):
                    _exclude_args += ['-e', os.path.join(_dir_path, _entry)]
            except PermissionError:
                # dev/* may have root-owned nodes — exclude the whole dir's
                # contents via a glob pattern as a fallback
                _exclude_args += ['-e', os.path.join(_dir_path, '*')]

        tui.console.print("Creating squashfs — this may take several minutes...")
        _cmd = (
            ['sudo', '-S', 'mksquashfs', self.__dir_chroot, _squashfs,
             '-comp', 'xz', '-noappend'] + _exclude_args
        )
        with open(_squash_log, 'w') as fh:
            _proc = subprocess.run(
                _cmd, input=self.__password + '\n',
                stdout=fh, stderr=subprocess.STDOUT, text=True
            )

        if _proc.returncode != 0:
            tui.console.print(f"ERROR: mksquashfs failed — see {_squash_log}")
            tui.console.error(f"build_iso: mksquashfs exited {_proc.returncode}")
            return False

        _sq_mb = os.path.getsize(_squashfs) // (2 ** 20)
        tui.console.print(f"squashfs created: {_sq_mb} MB")

        # ── Step 6: run grub-mkrescue ─────────────────────────────────────────
        # grub-mkrescue produces a hybrid image bootable on BIOS and UEFI
        # systems.  It requires grub-pc-bin, grub-efi-amd64-bin, and xorriso
        # to be installed on the host.
        _iso_name   = f"athena-{_version}-amd64.iso"
        _iso_path   = os.path.join(self.__dir_image, _iso_name)
        _grub_log   = os.path.join(self.__dir_log, 'grub-mkrescue.log')

        tui.console.print("Running grub-mkrescue...")
        with open(_grub_log, 'w') as fh:
            _proc = subprocess.run(
                ['grub-mkrescue', '-o', _iso_path, _staging],
                stdout=fh, stderr=subprocess.STDOUT, text=True
            )

        if _proc.returncode != 0:
            tui.console.print(f"ERROR: grub-mkrescue failed — see {_grub_log}")
            tui.console.error(f"build_iso: grub-mkrescue exited {_proc.returncode}")
            return False

        _iso_mb = os.path.getsize(_iso_path) // (2 ** 20)
        tui.console.print(f"ISO built: {_iso_path} ({_iso_mb} MB)")
        return True
