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
        Print("This needs to run as superuser, current user needs to be un sudoers file")
        self.__password = Prompt.ask("Please enter password", password=True)
        _proc = subprocess.run(['sudo', '-v'], input=self.__password, capture_output=True, text=True)
        assert _proc.returncode == 0, f"ERROR: Incorrect password or user not in sudoers file, {_proc.stdout}"

    def build_chroot(self) -> bool:

        _chroot = self.__dir_chroot

        # setting up folder structure
        # ref: https://www.linuxfromscratch.org/lfs/view/development/chapter07/creatingdirs.html
        dir_structure = [r'/{boot,home,mnt,opt,srv}', r'/etc/{opt,sysconfig}', r'/lib/firmware',
                         r'/media/{floppy,cdrom}', r'/usr/{,local/}{include,src}', r'/usr/local/{bin,lib,sbin}',
                         r'/usr/{,local/}share/{color,dict,doc,info,locale,man}',
                         r'/usr/{,local/}share/{misc,terminfo,zoneinfo}', r'/usr/{,local/}share/man/',
                         r'/var/{cache,local,log,mail,opt,spool}', r'/var/lib/{color,misc,locate}']

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
        _dpkg_install_cmd = f'sudo -S dpkg --root={_chroot} --instdir={_chroot} ' \
                            f'--admindir={_chroot}/var/lib/dpkg --force-script-chrootless --no-triggers --unpack '

        _dpkg_configure_cmd = f'sudo dpkg --root={_chroot}  --instdir={_chroot} ' \
                              f'--admindir={_chroot}/var/lib/dpkg --force-script-chrootless --configure --pending'

        _dpkg_install_cmd = shlex.split(_dpkg_install_cmd)

        # First install required
        _pkg_list = [_pkg for _pkg in self.__dependencytree.selected_pkgs
                     if self.__dependencytree.selected_pkgs[_pkg].required]

        # Get file list
        _file_list = ''
        _deb_list = [os.path.basename(self.__dependencytree.selected_pkgs[_pkg]['Filename']) for _pkg in _pkg_list]
        for _file in _deb_list:
            # stripping build revisions, because these do not reflect on source code builds
            _name, _ext = os.path.splitext(_file)
            _name = _name.split('_')
            assert len(_name) == 3, f"Incorrectly formatted package name {_file}"
            _pkg_name = _name[0]
            _version = _name[1]
            _arch = _name[2]

            _version = re.sub(r"\+b\d+$", "", _version)
            _file = _pkg_name + '_' + _version + '_' + _arch + _ext

            _file_path = os.path.join(self.__dir_repo, _file)
            assert os.path.exists(_file_path), f"ERROR: Package not build {_file}"
            _file_list += _file_path + ' '

        # Check if it has been built
        progress_format = '{percentage:3.0f}%[{bar:30}]{n_fmt}/{total_fmt} - {desc}'
        progress_bar = tqdm(desc=f'', ncols=80, total=len(_deb_list), bar_format=progress_format)

        try:
            with open(os.path.join(self.__dir_log, 'dpkg-deb.log'), 'w') as fh:

                # un-archiving package
                _dpkg_install_cmd.append(_file_list)
                _proc = subprocess.run(_dpkg_install_cmd, input=self.__password, capture_output=True, text=True)
                fh.write(_proc.stdout)

        except (FileNotFoundError, PermissionError) as e:
            Print(f"Error: {e}")
            exit(1)
        progress_bar.clear()
        return True
