# External
import os
import pathlib
import shlex
import subprocess
import re
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

        # If the chroot is not empty, offer to wipe it before proceeding.
        # Leftover files from a failed previous run will corrupt the new build
        # because dpkg's database will be in an inconsistent state.
        _chroot_nonempty = len(os.listdir(self.__dir_chroot)) != 0
        _wipe_chroot = False
        if _chroot_nonempty:
            tui.console.print(
                f"WARNING: '{os.path.basename(self.__dir_chroot)}' is not empty — "
                f"leftover files from a previous run will corrupt the build."
            )
            _resp = Prompt(PROMPT_YESNO, "Wipe chroot and start clean?").get_response()
            _wipe_chroot = _resp.lower() in ('y', 'yes')
            if not _wipe_chroot:
                tui.console.print("Continuing with non-empty chroot — results may vary")

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
            raise RuntimeError(
                f"Incorrect password or user not in sudoers file: {_proc.stdout.strip()}"
            )

        # Wipe the chroot now that we have a validated sudo credential.
        # rm -rf the contents (not the directory itself) so dir_chroot remains.
        if _wipe_chroot:
            tui.console.print("Wiping chroot...")
            _proc = subprocess.run(
                ['sudo', '-S', 'find', self.__dir_chroot,
                 '-mindepth', '1', '-delete'],
                input=self.__password + '\n',
                capture_output=True, text=True
            )
            if _proc.returncode != 0:
                raise RuntimeError(f"Failed to wipe chroot: {_proc.stderr.strip()}")
            tui.console.print("Chroot wiped")

        # Create Directory Structure
        self.build_chroot_directories()

        # Run pre-Install
        self.pre_install()

    def build_chroot(self) -> bool:
        """Install all selected packages into the chroot in dependency order.

        Two Tree forests track what each package is still waiting for:

          Unpack forest    — root=pkg, children=its Pre-Depends not yet configured.
                             A package is ready to unpack when its tree is childless
                             (all Pre-Depends have been configured).

          Configure forest — root=pkg, children=its Depends not yet unpacked.
                             A package is ready to configure when its tree is childless
                             (all Depends have been unpacked) AND it has been unpacked.

        Each round alternates between two phases:
          Unpack phase   — harvest childless roots from unpack_forest → unpack
                           → delete their names from configure_forest dep lists
                           (satisfies Depends constraints for packages that need them).
          Configure phase — harvest childless roots from configure_forest that are
                           already unpacked → configure → delete their names from
                           unpack_forest dep lists (satisfies Pre-Depends constraints).

        Circular dependencies (no package can go first) are detected when a phase
        produces nothing despite the forest being non-empty.  The entire stuck set
        is forced through as a single batch to break the cycle, matching dpkg's own
        behaviour when --force-* flags allow it.

        Returns:
            True on completion (individual package failures are logged but do not
            abort the run — a partial chroot is still useful for diagnosis).
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

        # --- Build configure forest ---
        # Each tree: root=pkg, children=Depends present in selected set
        # that are NOT in libc_seed (seed deps treated as already unpacked).
        configure_forest: dict = {}
        for pkg in all_pkgs:
            if pkg in libc_seed_set:
                continue
            tree = utils.Tree()
            tree.add_node(pkg)
            for dep in self._resolve_depends(pkg):
                if dep not in libc_seed_set:
                    try:
                        tree.add_node(dep, pkg)
                    except ValueError:
                        pass  # dep listed twice in Depends
            configure_forest[pkg] = tree

        # --- Install ---
        self._setup_chroot_env()
        self._init_dpkg_database()
        _log_path = os.path.join(self.__dir_log, 'chroot-install.log')

        with open(_log_path, 'w') as fh:

            # Batch 0: libc circular-dep bootstrap installed unconditionally first.
            tui.console.print(f"Batch 0 (bootstrap): {libc_seed}")
            fh.write(f'Batch 0 (bootstrap): {libc_seed}\n')
            self._unpack_packages(libc_seed, fh)
            self._configure_packages(libc_seed, fh)
            unpacked   = set(libc_seed)
            configured = set(libc_seed)

            _round = 1
            while unpack_forest or configure_forest:
                _progress = False

                # ── Unpack phase ─────────────────────────────────────────────
                # Packages whose Pre-Depends are all configured: their tree is
                # childless because every Pre-Dep was deleted when it was
                # configured in a previous round.
                ready_to_unpack = [
                    pkg for pkg, tree in unpack_forest.items()
                    if tree.is_childless and not tree.is_empty
                ]

                if not ready_to_unpack and unpack_forest:
                    # No childless roots despite non-empty forest → circular
                    # Pre-Depends.  Force all remaining through together.
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
                    self._unpack_packages(ready_to_unpack, fh)
                    unpacked.update(ready_to_unpack)
                    _progress = True

                    for pkg in ready_to_unpack:
                        # Remove pkg's own unpack tree — it is now processed.
                        unpack_forest.pop(pkg, None)
                        # Delete pkg from configure_forest dep lists: pkg being
                        # unpacked satisfies the Depends constraint for any
                        # package that lists pkg as a dep.
                        # Guard other_pkg != pkg: configure_forest still contains
                        # pkg's own tree at this point (we only popped from
                        # unpack_forest above).  Trying to delete pkg from its
                        # own root — which has children — crashes Tree.delete_node.
                        for other_pkg, other_tree in configure_forest.items():
                            if other_pkg != pkg and other_tree.find_node(pkg):
                                other_tree.delete_node(pkg)

                # ── Configure phase ───────────────────────────────────────────
                # Packages whose Depends are all unpacked (childless configure
                # tree) AND that have themselves been unpacked already.
                ready_to_configure = [
                    pkg for pkg, tree in configure_forest.items()
                    if tree.is_childless and not tree.is_empty
                    and pkg in unpacked
                ]

                if not ready_to_configure:
                    # Check for circular Depends among already-unpacked packages.
                    stuck = [p for p in configure_forest if p in unpacked]
                    if stuck:
                        tui.console.print(
                            f"WARNING: circular Depends detected — "
                            f"forcing configure of {len(stuck)} packages"
                        )
                        tui.console.warning(
                            f"build_chroot round {_round}: forced Depends batch: {stuck}"
                        )
                        ready_to_configure = stuck

                if ready_to_configure:
                    fh.write(f'\n--- Round {_round} configure ---\n')
                    self._configure_packages(ready_to_configure, fh)
                    configured.update(ready_to_configure)
                    _progress = True

                    for pkg in ready_to_configure:
                        # Remove pkg's own configure tree — it is now processed.
                        configure_forest.pop(pkg, None)
                        # Delete pkg from unpack_forest dep lists: pkg being
                        # configured satisfies the Pre-Depends constraint for any
                        # package that lists pkg as a pre-dep.
                        # Guard other_pkg != pkg for symmetry with the unpack
                        # phase, even though pkg was already popped from
                        # unpack_forest during the unpack phase.
                        for other_pkg, other_tree in unpack_forest.items():
                            if other_pkg != pkg and other_tree.find_node(pkg):
                                other_tree.delete_node(pkg)

                if not _progress:
                    # Neither phase moved anything — unrecoverable.
                    stuck_all = list(unpack_forest.keys()) + list(configure_forest.keys())
                    tui.console.print(
                        f"ERROR: no progress in round {_round} — "
                        f"{len(stuck_all)} packages stuck"
                    )
                    tui.console.error(
                        f"build_chroot stuck in round {_round}: {stuck_all}"
                    )
                    return False

                _round += 1

        tui.console.print(
            f"Chroot build complete — {len(configured)} packages installed "
            f"in {_round - 1} rounds"
        )

        # Apply post-install patches and overlay files now that all packages
        # are configured.  This is the right moment to override config files
        # or apply distro-specific fixups that would be clobbered by dpkg.
        self.post_install()

        # Write base system configuration files that dpkg does not create:
        # OS identity, hostname, hosts, fstab, machine-id, apt sources.
        self.generate_system_configs()

        return True

    # ── Dependency resolution helpers ─────────────────────────────────────────

    def _resolve_pre_depends(self, pkg_name: str) -> list:
        """Return Pre-Depends names for pkg_name that are present in selected_pkgs.

        Only single-alternative Pre-Depends are considered (alternatives are rare
        in Pre-Depends and are not tracked in Package.pre_depends).  Names not in
        selected_pkgs are omitted — they are either already installed on the host
        or irrelevant to the chroot ordering.
        """
        pkg = self.__dependencytree.selected_pkgs.get(pkg_name)
        if pkg is None:
            return []
        # Package.pre_depends is List[Tuple[name, ver, op]] — dep[0] is the name.
        return [dep[0] for dep in pkg.pre_depends
                if dep[0] in self.__dependencytree.selected_pkgs]

    def _resolve_depends(self, pkg_name: str) -> list:
        """Return Depends names for pkg_name that are present in selected_pkgs.

        Only single-alternative Depends are considered (alt_depends with OR
        alternatives are resolved by the dependency tree and do not need explicit
        ordering here — the package will configure regardless of which alternative
        was chosen).
        """
        pkg = self.__dependencytree.selected_pkgs.get(pkg_name)
        if pkg is None:
            return []
        return [dep[0] for dep in pkg.depends
                if dep[0] in self.__dependencytree.selected_pkgs]

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

    def _unpack_packages(self, pkg_list: list, fh) -> bool:
        """Run dpkg --unpack for pkg_list inside the chroot.

        --force-script-chrootless allows maintainer scripts to run without
        being inside a chroot bind-mount.  --no-triggers defers trigger
        processing to the configure phase.

        Returns True if dpkg exited 0, False otherwise.
        """
        if not pkg_list:
            return True
        _chroot = self.__dir_chroot
        _cmd = shlex.split(
            f'sudo -S dpkg --root={_chroot} --instdir={_chroot} '
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
        return _proc.returncode == 0

    def _configure_packages(self, pkg_list: list, fh) -> bool:
        """Run dpkg --configure for pkg_list inside the chroot.

        --force-confdef/--force-confnew resolve config-file prompts
        non-interactively.  Returns True if dpkg exited 0, False otherwise.
        """
        if not pkg_list:
            return True
        _chroot = self.__dir_chroot
        _cmd = shlex.split(
            f'sudo -S dpkg --root={_chroot} --instdir={_chroot} '
            f'--admindir={_chroot}/var/lib/dpkg '
            f'--force-script-chrootless -D1 --force-confdef --force-confnew '
            f'--configure --no-triggers'
        ) + pkg_list

        fh.write(f'Configure: {" ".join(pkg_list)}\n')
        _proc = subprocess.run(_cmd, input=self.__password + '\n',
                               capture_output=True, text=True, env=os.environ)
        fh.write(_proc.stdout)
        if _proc.returncode != 0:
            fh.write(_proc.stderr)
            tui.console.print(
                f'Error configuring {pkg_list[:3]}{"…" if len(pkg_list) > 3 else ""}: '
                f'{_proc.stderr[:200]}'
            )
        return _proc.returncode == 0

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

    def build_chroot_directories(self):
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

    def install_packages(self, installation_sequence: [], log_file: str):
        _chroot = self.__dir_chroot
        installed_list = []

        try:
            with open(os.path.join(self.__dir_log, log_file), 'w') as fh:
                # Setting environment variables, though may not be required
                # non-interactive not working for some reason, currently brute forcing by pre-placing the debconf config
                os.environ['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
                os.environ['DPKG_ROOT'] = _chroot
                os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
                os.environ['DEBCONF_NONINTERACTIVE_SEEN'] = 'true'

                # dpkg command to install package in chroot directory, something are required and something may not be,
                # e.g. --root with --instdir & --admindir or --instdir with --force-script-chrootless
                # TODO: verify for both unpack and configure the exact commands
                _dpkg_unpack_cmd = f'sudo -S dpkg --root={_chroot} ' \
                                   f'--instdir={_chroot} --admindir={_chroot}/var/lib/dpkg ' \
                                   f'--force-script-chrootless -D1 --no-triggers --unpack'

                # dpkg command to configure package in chroot directory - doesn't work
                _dpkg_configure_cmd = f'sudo -S dpkg --root={_chroot} ' \
                                      f'--instdir={_chroot} --admindir={_chroot}/var/lib/dpkg ' \
                                      f'--force-script-chrootless -D1 --force-confdef --force-confnew ' \
                                      f'--configure --no-triggers'

                # making them suitable for subprocess.run
                _dpkg_unpack_cmd = shlex.split(_dpkg_unpack_cmd)
                _dpkg_configure_cmd = shlex.split(_dpkg_configure_cmd)

                # Iterate per installation set - each are internally independent and (Pre)Depends satisfied
                for _set in installation_sequence:
                    # Find all package filenames - these are specific to selected packages, cant be taken from source
                    _deb_list = [os.path.basename(self.__dependencytree.selected_pkgs[_pkg]['Filename'])
                                 for _pkg in _set]

                    _file_list = []
                    for _file in _deb_list:
                        # stripping build revisions, because these do not reflect on source code builds
                        _file = self.strip_build_version(_file)
                        _file_path = os.path.join(self.__dir_repo, _file)

                        if not os.path.exists(_file_path):
                            tui.console.print(f"WARNING: .deb not found, skipping: {_file}")
                            tui.console.warning(f"install_packages: missing {_file_path}")
                            continue
                        _file_list.append(os.path.join(self.__dir_repo, _file))

                    fh.write(f'Installing package set {" ".join(_set)}\n')

                    # run unpack
                    # sudo -S reads the password from stdin and expects a newline
                    # terminator; without it sudo blocks waiting for more input.
                    _cmd = _dpkg_unpack_cmd + _file_list
                    _proc = subprocess.run(_cmd, input=self.__password + '\n',
                                           capture_output=True, text=True, env=os.environ)
                    fh.write(_proc.stdout)
                    if _proc.returncode != 0:
                        tui.console.print(f'Error: Failed unpacking set - {_set}')
                        tui.console.error(f'install_packages unpack {_set}: {_proc.stderr}')
                        fh.write(_proc.stderr)

                    # run configure
                    _cmd = _dpkg_configure_cmd + _set
                    _proc = subprocess.run(_cmd, input=self.__password + '\n',
                                           capture_output=True, text=True, env=os.environ)
                    fh.write(_proc.stdout)
                    if _proc.returncode != 0:
                        tui.console.print(f'Error: Failed configuring set - {_set}')
                        tui.console.error(f'install_packages configure {_set}: {_proc.stderr}')
                        fh.write(_proc.stderr)

                    # update install list
                    installed_list += _set

        except (FileNotFoundError, PermissionError) as e:
            tui.console.print(f"Error: cannot write install log — {e}")
            tui.console.error(f"install_packages log write: {e}")

        return installed_list

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
        cmd_list = [f'sudo -S ln -sfv {self.__dir_chroot}/run {self.__dir_chroot}/var/run',
                    f'sudo -S ln -sfv {self.__dir_chroot}/run/lock {self.__dir_chroot}/var/lock',
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

    def generate_system_configs(self):
        """Write base system configuration files into the chroot.

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
            f'NAME="{cfg.build_codename}"\n'
            f'VERSION="{cfg.build_version}"\n'
            f'ID=athena\n'
            f'ID_LIKE=debian\n'
            f'VERSION_CODENAME={cfg.build_codename.lower()}\n'
            f'PRETTY_NAME="{cfg.build_codename} {cfg.build_version}"\n'
            f'HOME_URL="https://athenalinux.org"\n'
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

        # /etc/apt/sources.list — lets the installed system update itself from
        # the same mirror used to build it.
        _base = f'http://{cfg.baseurl}/debian'
        _sec  = f'http://security.debian.org/debian-security'
        _comp = 'main contrib non-free non-free-firmware'
        self._write_chroot_file('/etc/apt/sources.list', (
            f'deb {_base} {cfg.basecodename} {_comp}\n'
            f'deb {_base} {cfg.basecodename}-updates {_comp}\n'
            f'deb {_sec} {cfg.basecodename}-security {_comp}\n'
        ))

        tui.console.print("System configuration files written")

    def _write_chroot_file(self, rel_path: str, content: str):
        """Write content to rel_path inside the chroot as root via sudo tee.

        sudo -S reads the password from stdin (first line), then tee reads the
        remaining input as the file content.  This works because subprocess
        writes the full input string to the pipe before any reader consumes it.

        Args:
            rel_path: Absolute path relative to the chroot root (e.g. '/etc/hostname').
            content:  Text content to write; may be empty.
        """
        _dest = os.path.join(self.__dir_chroot, rel_path.lstrip('/'))
        _proc = subprocess.run(
            ['sudo', '-S', 'tee', _dest],
            input=self.__password + '\n' + content,
            capture_output=True, text=True
        )
        if _proc.returncode != 0:
            tui.console.print(f"ERROR: Failed to write {rel_path} into chroot")
            tui.console.error(f"_write_chroot_file {rel_path}: {_proc.stderr}")

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
        _grub_cfg = (
            'set default=0\n'
            'set timeout=5\n'
            '\n'
            f'menuentry "{cfg.build_codename} {cfg.build_version}" {{\n'
            '    linux  /boot/vmlinuz boot=live quiet splash\n'
            '    initrd /boot/initrd.img\n'
            '}\n'
        )
        with open(os.path.join(_staging_grub, 'grub.cfg'), 'w') as fh:
            fh.write(_grub_cfg)
        tui.console.print("grub.cfg written")

        # ── Step 5: create squashfs ───────────────────────────────────────────
        # Runtime virtual directories are excluded — live-boot mounts these
        # fresh at boot.  -noappend overwrites any previous squashfs.
        _squashfs     = os.path.join(_staging_live, 'filesystem.squashfs')
        _squash_log   = os.path.join(self.__dir_log, 'mksquashfs.log')
        _runtime_dirs = ['proc', 'sys', 'dev', 'run', 'tmp']
        _exclude_args = []
        for _d in _runtime_dirs:
            _exclude_args += ['-e', os.path.join(self.__dir_chroot, _d)]

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
        _iso_name   = f"athena-{cfg.build_version}-amd64.iso"
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
