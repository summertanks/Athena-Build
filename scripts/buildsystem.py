# External
import os
import pathlib
import shlex
import subprocess
import re
from rich.prompt import Confirm, Prompt

# Internal
import dependencytree
import utils

Print = print


class BuildSystem:
    def __init__(self, dependency_tree: dependencytree.DependencyTree, dir_list: utils.DirectoryListing):
        self.__dependencytree = dependency_tree
        self.__dir_image = dir_list.dir_image
        self.__dir_chroot = dir_list.dir_chroot
        self.__dir_repo = dir_list.dir_repo
        self.__dir_log = dir_list.dir_log
        self.__dir_preinstall_patch = dir_list.dir_patch_preinstall
        self.__dir_postinstall_patch = dir_list.dir_patch_postinstall

        # Sanity Check - Just making sure folders exist, typically created by utils.DirectoryListing
        for _dir in [self.__dir_chroot, self.__dir_image, self.__dir_repo]:
            assert os.path.exists(_dir), f"Missing essential folder {_dir}"

        # Check if directory empty, if there are files from previous install the results will vary significantly
        if len(os.listdir(self.__dir_chroot)) != 0:
            Print(f"WARNING: Chroot folder '{os.path.basename(self.__dir_chroot)}' not empty,"
                  f" may end up with corrupted system. Delete manually if not certain")

        # Need Password for sudo,
        Print("This needs to run as superuser, current user needs to be in sudoers group")
        self.__password = Prompt.ask("Please enter password", password=True)
        # Checking if password is valid
        _proc = subprocess.run(['sudo', '-v'], input=self.__password, capture_output=True, text=True)
        assert _proc.returncode == 0, f"ERROR: Incorrect password or user not in sudoers file, {_proc.stdout}"

        # Create Directory Structure
        self.build_chroot_directories()

        # Run pre-Install
        self.pre_install()

    def build_chroot(self) -> bool:
        """
        This builds the chroot environment with the selected packages in dependency tree
        Returns:
        boot: True on success, False otherwise
        """
        _chroot = self.__dir_chroot

        # First install 'required' - it is the easiest (and most predictable) the handle
        _pkg_list = [_pkg for _pkg in self.__dependencytree.selected_pkgs
                     if self.__dependencytree.selected_pkgs[_pkg].priority == 'required']

        # Lets setup default installation list, also the known circular dependency
        libc_list = ['gcc-10-base', 'libc6', 'libgcc-s1', 'libcrypt1']
        installed_list = []

        # build installation sequence -
        # since we are using dpkg and not apt, it is up to us to make sure that pre-requisites (Depends) and
        # especially (Pre-Depends) are already unpacked. it gets more tricky since Pre-Depends need also to be
        # configured before package is unpacked (and thus the distinction between 'Depends' and 'Pre-Depends')

        # installation sequence is a list of lists, where each list is a block of independent packages
        # which have prerequisites satisfied
        # this will fail in circular dependencies. Hence, calling it with assumption libc_list is already installed
        installation_sequence = [libc_list] + self.get_install_sequence(_pkg_list, libc_list)

        Print(f"Installing {len(_pkg_list)} 'required' packages in {len(installation_sequence)} iterations")
        installed_list += self.install_packages(installation_sequence, 'chroot-required.log')

        # Starting the remaining Installation, this required preparing of the chroot system
        # selecting the not 'important' packages now
        _pkg_list = [_pkg for _pkg in self.__dependencytree.selected_pkgs
                     if not self.__dependencytree.selected_pkgs[_pkg].priority == 'important']
        # New installation sequence based on packages installed
        installation_sequence = self.get_install_sequence(_pkg_list, installed_list)

        # Install
        Print(f"Installing {len(_pkg_list)} 'important' packages in {len(installation_sequence)} iterations")
        # installed_list += self.install_packages(installation_sequence, 'chroot-important.log')

        return True

    def get_install_sequence(self, selected_pkgs: [], installed_pkgs: []) -> []:
        sequence = []
        collection = []
        # let's first build dependency tree for each package
        for _pkg in selected_pkgs:
            # build tree and add root node
            tree = utils.Tree()
            tree.add_node(_pkg)

            # Add to collection
            collection.append(tree)

            # just add leaves
            leaves = self.__dependencytree.selected_pkgs[_pkg].depends_on
            for leaf in leaves:
                tree.add_node(leaf, tree.root.value)

        # first parse installed packages, remove those dependencies
        for _pkg in installed_pkgs:
            for _tree in collection:
                if _tree.find_node(_pkg):
                    _tree.delete_node(_pkg)

        while True:
            sub_sequence = []
            pkg_list = [_tree.root.value for _tree in collection if _tree.is_childless and not _tree.is_empty]
            # anything to process this iteration
            if not len(pkg_list):
                if len([_tree.root for _tree in collection if _tree.is_childless and not _tree.is_empty]):
                    # Something was not addressed, maybe circular dependency?
                    raise ValueError(f"Packages exist which dont have dependencies fulfilled {collection}")
                # No packages left to process
                break

            for _pkg in pkg_list:
                for _tree in collection:
                    if _tree.find_node(_pkg):
                        _tree.delete_node(_pkg)
                sub_sequence.append(_pkg)

            sequence.append(sub_sequence)

        return sequence

    def build_chroot_directories(self):
        """
        Function creates the standard directory structure expected on GNU (Debian) Linux
        ref: https://www.linuxfromscratch.org/lfs/view/development/chapter07/creatingdirs.html

        Not all directories done though, less the man(1..8)

        will raise 'assert' if there are failures
        """

        # TODO: build man(1..8)
        dir_structure = ['/{boot,home,mnt,opt,srv,sys,proc,dev}', '/etc/{opt,sysconfig}', '/lib/{firmware}',
                         '/media/{floppy,cdrom}', '/usr/{local,include,src,share}',
                         '/usr/local/{bin,lib,sbin,include,src,share}',
                         '/usr/share/{color,dict,doc,info,locale,man,misc,terminfo,zoneinfo}',
                         '/usr/local/share/{color,dict,doc,info,locale,man,misc,terminfo,zoneinfo}',
                         '/var/{cache,local,log,mail,opt,spool}', '/var/lib/{color,misc,locate}']

        for _dir in dir_structure:
            utils.create_folders(self.__dir_chroot + _dir)

        misc_structure = [f'sudo ln -sfv {self.__dir_chroot}/run {self.__dir_chroot}/var/run',
                          f'sudo ln -sfv {self.__dir_chroot}/run/lock {self.__dir_chroot}/var/lock',
                          f'sudo install -dv -m 0750 {self.__dir_chroot}/root',
                          f'sudo install -dv -m 1777 {self.__dir_chroot}/tmp {self.__dir_chroot}/var/tmp']

        for _cmd in misc_structure:
            _proc = subprocess.run(shlex.split(_cmd), input=self.__password, capture_output=True, text=True)
            assert _proc.returncode == 0, f"ERROR: Failed executing {_cmd}, {_proc.stdout}"

    @staticmethod
    def strip_build_version(file: str) -> str:
        # stripping build revisions, because these do not reflect on source code builds
        _name, _ext = os.path.splitext(file)
        _name = _name.split('_')
        assert len(_name) == 3, f"Incorrectly formatted package name {file}"
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
                                   f'--force-script-chrootless --no-triggers --unpack'

                # dpkg command to configure package in chroot directory - doesn't work
                _dpkg_configure_cmd = f'sudo -S dpkg --root={_chroot} ' \
                                      f'--instdir={_chroot} --admindir={_chroot}/var/lib/dpkg ' \
                                      f'--force-script-chrootless --force-confdef --force-confnew ' \
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

                        # confirm the source has been built and deb package is available in repo
                        assert os.path.exists(_file_path), f"ERROR: Package not build {_file}"
                        _file_list.append(os.path.join(self.__dir_repo, _file))

                    fh.write(f'Installing package set {" ".join(_set)}\n')

                    # run unpack
                    _cmd = _dpkg_unpack_cmd + _file_list
                    _proc = subprocess.run(_cmd, input=self.__password, capture_output=True, text=True, env=os.environ)
                    fh.write(_proc.stdout)
                    if _proc.returncode != 0:
                        Print(f'Error: Failed unpacking set - {_set} : {_proc.stderr}')
                        fh.write(_proc.stderr)

                    # run configure
                    _cmd = _dpkg_configure_cmd + _set
                    _proc = subprocess.run(_cmd, input=self.__password, capture_output=True, text=True, env=os.environ)
                    fh.write(_proc.stdout)
                    if _proc.returncode != 0:
                        Print(f'Error: Failed configuring set - {_set} : {_proc.stderr}')
                        fh.write(_proc.stderr)

                    # update install list
                    installed_list += _set

        except (FileNotFoundError, PermissionError) as e:
            Print(f"Error: {e}")
            exit(1)

        return installed_list

    def pre_install(self):
        for root, dirs, files in os.walk(self.__dir_preinstall_patch):

            if len(files) == 0:
                continue

            # reached the list of files, here on three steps will be taken
            # use relative path and create if not existing in chroot dir
            chroot_relative_dir = root.replace(self.__dir_preinstall_patch, self.__dir_chroot)
            pathlib.Path(chroot_relative_dir).mkdir(parents=True, exist_ok=True)

            for _file in files:
                _orig_file = os.path.join(root, _file)
                if os.path.splitext(_file) != '.patch':
                    # non patch files (any other extension) are copied into that folder
                    _proc = subprocess.run(['sudo', '-S', 'cp', _orig_file, chroot_relative_dir],
                                           input=self.__password, capture_output=True, text=True, env=os.environ)
                    if _proc.returncode != 0:
                        Print(f'Error: Failed copying file - {_file} : {_proc.stderr}')
                else:
                    # patch files (.patch extension) are applied to that folder
                    # TODO: Test
                    _proc = subprocess.run(['patch', '-p1', '<', _orig_file], cwd=chroot_relative_dir,
                                           input=self.__password, capture_output=True, text=True, env=os.environ)
                    if _proc.returncode != 0:
                        Print(f'Error: Failed Patching file - {_file} : {_proc.stderr}')

    def generate_system_configs(self):
        pass
