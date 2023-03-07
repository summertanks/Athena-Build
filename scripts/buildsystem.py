# External
import os

# Internal
import dependencytree
import utils


class BuildSystem:
    def __init__(self, dependency_tree: dependencytree.DependencyTree, dir_list: utils.DirectoryListing):
        self.__dependencytree = dependency_tree
        self.__dir_image = dir_list.dir_image
        self.__dir_chroot = dir_list.dir_chroot
        self.__dir_repo = dir_list.dir_repo

        for _dir in [self.__dir_chroot, self.__dir_image, self.__dir_repo]:
            assert os.path.exists(_dir), f"Missing essential folder {_dir}"

    def install_pkgs(self):
        pass
