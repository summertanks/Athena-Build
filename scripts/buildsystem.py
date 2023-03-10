# External
import os
import shlex
import subprocess
import re
from tqdm import tqdm
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

        for _dir in [self.__dir_chroot, self.__dir_image, self.__dir_repo]:
            assert os.path.exists(_dir), f"Missing essential folder {_dir}"

        # Check if directory empty
        if len(os.listdir(self.__dir_chroot)) != 0:
            Print(f"WARNING: Chroot folder '{os.path.basename(self.__dir_chroot)}' not empty,"
                  f" may end up with corrupted system. Delete manually if not certain")

        # Need Password
        Print("This needs to run as superuser, current user needs to be in sudoers group")
        self.__password = Prompt.ask("Please enter password", password=True)
        _proc = subprocess.run(['sudo', '-v'], input=self.__password, capture_output=True, text=True)
        assert _proc.returncode == 0, f"ERROR: Incorrect password or user not in sudoers file, {_proc.stdout}"

    def build_chroot(self) -> bool:

        _chroot = self.__dir_chroot

        # setting up folder structure (mess the man(1..8)
        # ref: https://www.linuxfromscratch.org/lfs/view/development/chapter07/creatingdirs.html
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

        # Setting environment variables
        env_vars = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "DPKG_ROOT": _chroot}

        # sudo dpkg --root=/home/harkirat/PycharmProjects/Athena-Build/buildroot
        # --instdir=/home/harkirat/PycharmProjects/Athena-Build/buildroot
        # --admindir=/home/harkirat/PycharmProjects/Athena-Build/buildroot/var/lib/dpkg
        # --force-script-chrootless -D1 --configure coreutils
        #
        # sudo dpkg --root=/home/harkirat/PycharmProjects/Athena-Build/buildroot
        # --instdir=/home/harkirat/PycharmProjects/Athena-Build/buildroot
        # --admindir=/home/harkirat/PycharmProjects/Athena-Build/buildroot/var/lib/dpkg
        # --force-script-chrootless --no-triggers -D1 --unpack repo/diffutils_3.7-5_amd64.deb
        # Setting up basic command structure
        _dpkg_install_cmd = f'sudo -S dpkg --root={_chroot} --instdir={_chroot} --admindir={_chroot}/var/lib/dpkg ' \
                            f'--force-script-chrootless --no-triggers --unpack'

        _dpkg_configure_cmd = f'sudo -S dpkg --root={_chroot} --instdir={_chroot} --admindir={_chroot}/var/lib/dpkg ' \
                              f'--force-script-chrootless --force-confdef --force-confnew --configure --no-triggers'

        # making them suitable for sysprocess.run
        _dpkg_install_cmd = shlex.split(_dpkg_install_cmd)
        _dpkg_configure_cmd = shlex.split(_dpkg_configure_cmd)

        # First install required
        _pkg_list = [_pkg for _pkg in self.__dependencytree.selected_pkgs
                     if self.__dependencytree.selected_pkgs[_pkg].required]

        # Lets setup default installation list, also the known circular dependency
        libc_list = ['gcc-10-base', 'libc6', 'libgcc-s1', 'libcrypt1']
        installed_list = []
        installation_sequence = [libc_list] + self.get_install_sequence(_pkg_list, libc_list)

        # Get file list
        try:
            with open(os.path.join(self.__dir_log, 'dpkg-deb.log'), 'w') as fh:
                for _set in installation_sequence:
                    _deb_list = [os.path.basename(self.__dependencytree.selected_pkgs[_pkg]['Filename'])
                                 for _pkg in _set]

                    _file_list = []
                    for _file in _deb_list:
                        # stripping build revisions, because these do not reflect on source code builds
                        _file = self.strip_build_version(_file)
                        _file_path = os.path.join(self.__dir_repo, _file)
                        assert os.path.exists(_file_path), f"ERROR: Package not build {_file}"
                        _file_list.append(os.path.join(self.__dir_repo, _file))

                    # run unpack
                    _cmd = _dpkg_install_cmd + _file_list
                    _proc = subprocess.run(_cmd, input=self.__password, capture_output=True, text=True)
                    fh.write(_proc.stdout)
                    if _proc.stderr != "":
                        Print(f'Error: Failed unpacking set - {_set} : {_proc.stderr}')
                        # return False

                    # run configure
                    _cmd = _dpkg_configure_cmd + _set
                    _proc = subprocess.run(_cmd, input=self.__password, capture_output=True, text=True)
                    fh.write(_proc.stdout)
                    if _proc.stderr != "":
                        Print(f'Error: Failed configuring set - {_set} : {_proc.stderr}')
                        # return False

                    # update install list
                    installed_list += _set

        except (FileNotFoundError, PermissionError) as e:
            Print(f"Error: {e}")
            exit(1)
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
