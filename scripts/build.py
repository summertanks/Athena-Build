# (C) Athena Linux Project

# External imports
import argparse
import configparser
import os
import shutil
import cache

import apt_pkg
from rich.prompt import Confirm

# Local imports
import utils
import buildsystem
import dependencytree


asciiart_logo = '╔══╦╗╔╗─────────╔╗╔╗\n' \
                '║╔╗║╚╣╚╦═╦═╦╦═╗─║║╠╬═╦╦╦╦╦╗\n' \
                '║╠╣║╔╣║║╩╣║║║╬╚╗║╚╣║║║║║╠║╣\n' \
                '╚╝╚╩═╩╩╩═╩╩═╩══╝╚═╩╩╩═╩═╩╩╝'

# TODO: make all apt_pkg.parse functions arch specific

Print = print


def main():

    config_parser = configparser.ConfigParser()

    parser = argparse.ArgumentParser(description='Dependency Parser - Athena Linux')
    parser.add_argument('--config-file', type=str, help='Specify Configs File', required=True)
    parser.add_argument('--pkg-list', type=str, help='Specify Required Pkg File', required=True)
    parser.add_argument('--working-dir', type=str, help='Specify Working directory', required=True)

    args = parser.parse_args()
    working_dir = os.path.abspath(args.working_dir)

    config_path = os.path.join(working_dir, args.config_file)
    pkglist_path = os.path.join(working_dir, args.pkg_list)

    try:
        config_parser.read(config_path)

        arch = config_parser.get('Build', 'ARCH')
        baseurl = config_parser.get('Base', 'baseurl')
        basecodename = config_parser.get('Base', 'BASECODENAME')
        baseid = config_parser.get('Base', 'BASEID')
        baseversion = config_parser.get('Base', 'BASEVERSION')
        build_codename = config_parser.get('Build', 'CODENAME')
        build_version = config_parser.get('Build', 'VERSION')

    except configparser.Error as e:
        print(f"Athena Linux: Config Parser Error: {e}")
        exit(1)

    # --------------------------------------------------------------------------------------------------------------
    # Setting up common systems
    apt_pkg.init_system()
    dir_list = utils.DirectoryListing(working_dir, config_parser)
    base_distribution = utils.BaseDistribution(baseurl, baseid, basecodename, baseversion, arch)

    # log_format = "%(message)s"
    # logging.basicConfig(level="INFO", format=log_format, datefmt="[%X]", handlers=[RichHandler()])
    # logger = logging.getLogger('rich')

    # --------------------------------------------------------------------------------------------------------------
    Print("Starting Source Build System for Athena Linux...")
    Print("Building for ...")
    Print(f"\t Arch\t\t\t{arch}")
    Print(f"\t Parent Distribution\t{basecodename} {baseversion}")
    Print(f"\t Build Distribution\t{build_codename} {build_version}")

    # --------------------------------------------------------------------------------------------------------------
    # Step I - Building Cache
    Print("Building Cache...")
    build_cache = cache.Cache(base_distribution, dir_list.dir_cache)

    # -------------------------------------------------------------------------------------------------------------
    # Step II - Parse Dependencies
    Print("Parsing Dependencies...")
    required_packages = []
    required_packages_list = utils.readfile(pkglist_path).split('\n')
    for pkg in required_packages_list:
        if pkg and not pkg.startswith('#') and not pkg.isspace():
            required_packages.append(pkg.strip())
    Print(f"Total Required Packages {len(required_packages)}")

    dependency_tree = dependencytree.DependencyTree(
        build_cache, select_recommended=False, arch=base_distribution.arch, lookahead=required_packages)

    # Iterate through package list and identify dependencies
    for pkg in required_packages:
        dependency_tree.parse_dependency(pkg)

    Print(f"Total Dependencies Selected are : {len(dependency_tree.selected_pkgs)}")

    # -------------------------------------------------------------------------------------------------------------
    # Step III - Checking Breaks, Conflicts and version constraints
    Print("Checking Breaks and Conflicts...")
    if not dependency_tree.validate_selection():
        if not Confirm.ask("There are one or more dependency validation failures, Proceed?", default=True):
            exit(1)

    try:
        with open(os.path.join(dir_list.dir_log, 'selected_packages.list'), 'w') as f:
            for pkg in dependency_tree.selected_pkgs:
                f.write(str(dependency_tree.selected_pkgs[pkg].raw) + '\n\n')
    except (FileNotFoundError, PermissionError) as e:
        Print(f"Error: {e}")
        exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - IV Parse Source Dependencies
    Print("Parsing Source Packages...")
    if not dependency_tree.parse_sources():
        if not Confirm.ask("There are one or more source parse failures, Proceed?", default=True):
            exit(1)

    try:
        with open(os.path.join(dir_list.dir_log, 'selected_sources.list'), 'w') as fa:
            with open(os.path.join(dir_list.dir_log, 'source_file.list'), 'w') as fb:
                for _pkg in dependency_tree.selected_srcs:
                    fa.write(str(dependency_tree.selected_srcs[_pkg].raw) + '\n\n')
                    for _file in dependency_tree.selected_srcs[_pkg].files:
                        fb.write(f"{_file}: {dependency_tree.selected_srcs[_pkg].files[_file]}\n")

    except (FileNotFoundError, PermissionError) as e:
        Print(f"Error: {e}")
        exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - V Download source packages
    Print("Download source packages...")
    _src_download_size = dependency_tree.download_size
    Print("Total Download is about ", _src_download_size // (2**20), "MB")
    _total, _used, _free = shutil.disk_usage(dir_list.dir_source)
    print(f"Disk Space - Total: {_total // (2**30)}GiB, Used: {_used // (2**30)}GiB, Free: {_free // (2**30)}GiB")
    Print("Starting Downloads...")
    _downloaded_size = utils.download_source(dependency_tree, dir_list.dir_source, base_distribution)
    if _src_download_size != _downloaded_size:
        Confirm.ask("Download size mismatch, continue?", default=True)

    # -------------------------------------------------------------------------------------------------------------
    # Step - VII Source Build Dependency Check
    Print("Creating Build System...")
    build_container = buildsystem.BuildContainer(dir_list)

    # -------------------------------------------------------------------------------------------------------------
    # Step - X Starting Build
    Print("Starting Source Packages...")
    import tqdm
    progress_format = '{percentage:3.0f}%[{bar:30}]{n_fmt}/{total_fmt} - {desc}'
    progress_bar = tqdm.tqdm(ncols=80, total=len(dependency_tree.selected_srcs), bar_format=progress_format)
    with open(os.path.join(dir_list.dir_log, 'dpkg-build.log'), "w") as dpkg_build_log:
        for _pkg in dependency_tree.selected_srcs:
            progress_bar.update(1)
            _src_pkg = dependency_tree.selected_srcs[_pkg]
            _exit_code = build_container.build(_src_pkg)
            if not _exit_code:
                Print(f"FAIL: Build Failed for {_src_pkg}")
                dpkg_build_log.write(f"FAIL: {_pkg}")
            else:
                dpkg_build_log.write(f"PASS: {_pkg}")
            dpkg_build_log.flush()


# Main function
if __name__ == '__main__':
    print(asciiart_logo)
    main()
