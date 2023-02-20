# (C) Athena Linux Project

# External imports
import argparse
import configparser
import os
import re
import subprocess
import cache

import apt_pkg
from rich.console import Console
from rich.prompt import Prompt, Confirm

import package
import source

# Local imports
import utils
import buildsystem
import dependencytree
import package
import source

asciiart_logo = '╔══╦╗╔╗─────────╔╗╔╗\n' \
                '║╔╗║╚╣╚╦═╦═╦╦═╗─║║╠╬═╦╦╦╦╦╗\n' \
                '║╠╣║╔╣║║╩╣║║║╬╚╗║╚╣║║║║║╠║╣\n' \
                '╚╝╚╩═╩╩╩═╩╩═╩══╝╚═╩╩╩═╩═╩╩╝'

# TODO: make all apt_pkg.parse functions arch specific

Print = print


def main():
    source_packages: {str: source.Source} = {}
    multi_dep = []

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
    console = Console()
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

    build_container = buildsystem.BuildContainer(dir_list)
    # build_container.container_execute("")
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

    # -------------------------------------------------------------------------------------------------------------
    # Step - VI Check for discrepancy between source version and package version
    console.print("[bright_white]Checking for discrepancy between source package version...")

    for pkg_name in selected_packages:
        source_name = selected_packages[pkg_name].source[0]
        source_version = selected_packages[pkg_name].source[1]
        pkg_version = selected_packages[pkg_name].version

        if pkg_version == '-1':
            Print(f"Skipping Package {pkg_name}, package wasn't parsed")
            continue

        # Where Source version is not given, it is assumed same as package version
        if source_version == '':
            source_version = pkg_version

        # Add package to source list
        if source_name not in source_packages:
            source_packages[source_name] = source.Source(source_name, source_version)

        if not source_version == pkg_version:
            Print(f"Package and Source version mismatch "
                  f"{pkg_name}: {pkg_version} -> {source_name}: {source_version} Using {source_version}")
            selected_packages[pkg_name].reset_source_version(source_version)

    console.print("Source requested for : ", len(source_packages), " packages")

    # -------------------------------------------------------------------------------------------------------------
    # Step - VI Parse Source Packages
    console.print("[bright_white]Parsing Source Packages...")

    # Parse Sources Control file
    total_src_count, total_src_size = source.parse_sources(source_records, source_packages)

    missing_source = [_pkg for _pkg in source_packages if not source_packages[_pkg].found]

    if not len(missing_source) == 0:
        for pkg in missing_source:
            console.print(f"Source not found for : {source_packages[pkg].name} {source_packages[pkg].version} "
                          f"Alternates: {source_packages[pkg].alternates}")
            if Confirm.ask("Select from Alternates? if N, source package will be ignored(y/n"):
                new_version = Prompt.ask(f"Enter Alt Version", choices=source_packages[pkg].alternates)
                source_packages[pkg].reset_version(new_version)

        # rerun parse_source only for the missing source
        source.parse_sources(source_records, missing_source)

    try:
        with open(os.path.join(dir_list.dir_log, 'source_packages.list'), 'w') as f:
            for pkg in source_packages:
                f.write(str(source_packages[pkg]) + '\n')
    except (FileNotFoundError, PermissionError) as e:
        Print(f"Error: {e}")
        exit(1)

    try:
        with open(os.path.join(dir_list.dir_log, 'source_file.list'), 'w') as f:
            for src_pkg in source_packages:
                if source_packages[src_pkg].found:
                    for file in source_packages[src_pkg].files:
                        _filename = file
                        _filepath = source_packages[src_pkg].files[file]['path']
                        _filesize = source_packages[src_pkg].files[file]['size']
                        _filehash = source_packages[src_pkg].files[file]['md5']
                        f.write(f"{_filename} {_filepath} {_filesize} {_filehash}\n")
    except (FileNotFoundError, PermissionError) as e:
        Print(f"Error: {e}")
        exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - VII Source Build Dependency Check
    console.print("[bright_white]Source Build Dependency Check...")

    # TODO: use dpkg-checkbuilddeps -d build-depends-string -c build-conflicts-string
    installed_packages = {}
    result = subprocess.run(['dpkg', '--list'], stdout=subprocess.PIPE).stdout.decode('utf-8').split('\n')
    for line in result:
        match = re.match(r'^ii\s+(\S+)\s+(\S+)', line)
        if match:
            # TODO: strip the arch string ':amd64' if exists
            package_name = match.group(1).split(':')[0]
            package_version = match.group(2)
            installed_packages[package_name] = package_version

    failed_dep = []
    failed_dep_version = ''
    conflicts_pkg = ''
    build_alt_dep = []

    for src_pkg in source_packages:
        # Check - if build dependency is installed, and installed package is right version
        for dep in source_packages[src_pkg].build_depends:
            _pkg = dep[0]
            if _pkg not in installed_packages:
                if _pkg not in failed_dep:
                    failed_dep.append(_pkg)
            else:
                if not dep[2] == '':  # no comparison to do
                    if not apt_pkg.check_dep(installed_packages[_pkg], dep[2], dep[1]):
                        Print(f"Build Dependency version check failed for {src_pkg}: {_pkg} {dep[1]}")
                        failed_dep_version += f'{_pkg} ({dep[2]} {dep[1]}) '

        # Check - if conflict is installed, and installed package matches conflict version
        for dep in source_packages[src_pkg].conflicts:
            _pkg = dep[0]
            if _pkg in installed_packages:
                if dep[2] == '' or apt_pkg.check_dep(installed_packages[_pkg], dep[2], dep[1]):
                    Print(f"Build Dependency conflict {src_pkg}: {_pkg} {dep[1]}")
                    conflicts_pkg += f'{_pkg} ({dep[2]} {dep[1]}) '

        # Check from alternates if at least one package is installed
        for section in source_packages[src_pkg].altdepends:
            found = False
            for dep in section:
                pkg_name = dep[0]
                if pkg_name in installed_packages:
                    pkg_version = dep[1]
                    pkg_constraint = dep[2]
                    if apt_pkg.check_dep(installed_packages[pkg_name], pkg_constraint, pkg_version):
                        found = True
                    else:
                        Print(f"Alt Build Dependency Check - Version constraint failed for {pkg_name}")
            if not found:
                if section not in build_alt_dep:
                    build_alt_dep.append(section)

    # TODO: Check conflict within build dependency
    if not failed_dep == '':
        console.print("Build Dependency failed for ", ' '.join(failed_dep))
    else:
        console.print("PASSED: Build Dependency")
    if not failed_dep_version == '':
        console.print("Build Dependency version check failed for ", failed_dep_version)
    else:
        console.print("PASSED: Build Dependency version check")
    if not conflicts_pkg == '':
        console.print("Build Dependency conflict for ", conflicts_pkg)
    else:
        console.print("PASSED: Build Dependency Conflict")
    if len(build_alt_dep):
        for section in build_alt_dep:
            Print(f"Build dependency unresolved between {section}")
    else:
        console.print("PASSED: Build Alt Dependency Check")

    try:
        with open(os.path.join(dir_list.dir_log, 'build_dependency.list'), 'w') as f:
            f.write("Build Dependencies Failed:\n")
            f.write(f"{' '.join(failed_dep)}\n")
            f.write("\nDependencies Version Check Failed:\n")
            f.write(f"{failed_dep_version}\n")
            f.write("\nDependencies Version Check Failed:\n")
            f.write(f"{conflicts_pkg}\n")
            f.write("\nAlt Dependencies Check Failed:\n")
            f.write(f"{str(build_alt_dep)}\n")
    except (FileNotFoundError, PermissionError) as e:
        Print(f"Error: {e}")
        exit(1)

    if not (failed_dep == '' and failed_dep_version == '' and conflicts_pkg == ''):
        if not Confirm.ask("There are pending Build Dependencies issues, Manual check required. Proceed", default=True):
            exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - VIII Download Source files
    console.print("[bright_white]Download Source files...")

    console.print("Total File Selected are :", total_src_count)
    console.print("Total Download is about ", round(total_src_size / (1024 * 1024)), "MB")
    console.print("Starting Downloads...")
    utils.download_source(source_packages, dir_list.dir_download, base_distribution)

    # -------------------------------------------------------------------------------------------------------------
    # Step - IX Expanding the Source Packages
    console.print("[bright_white]Expanding the Source Packages...")

    folder_list = {}
    dsc_files = []
    try:
        with open(os.path.join(dir_list.dir_log, 'dpkg-source.log'), "w") as logfile:
            with console.status('') as status:

                for pkg in source_packages:
                    for file in source_packages[pkg].files:
                        if os.path.splitext(file)[1] == '.dsc':
                            dsc_files.append(file)
                            folder_name = os.path.join(dir_list.dir_source, os.path.splitext(file)[0])
                            folder_list[file] = folder_name

                # dsc_files = [file[0] for file in file_list.items() if os.path.splitext(file[0])[1] == '.dsc']
                _total = len(dsc_files)
                _completed = 0
                _errors = 0
                for file in dsc_files:
                    _completed += 1
                    status.update(f"{_completed}/{_total} Expanding Source Package {file}")
                    # folder_name = os.path.join(dir_source, os.path.splitext(file)[0])
                    folder_name = folder_list[file]
                    # folder_list.append(folder_name)
                    dsc_file = os.path.join(dir_list.dir_download, file)
                    process = subprocess.Popen(
                        ["dpkg-source", "-x", dsc_file, folder_name], stdout=logfile, stderr=logfile)
                    if process.wait():
                        _errors += 1
                if _errors:
                    console.print(f"dpkg-source failed for {_errors} instances, please check dpkg-source.log")

        with open(os.path.join(dir_list.dir_log, 'source_folder.list'), 'w') as f:
            for folder in folder_list:
                f.write(f"{folder} {folder_list[folder]} \n")

    except (FileNotFoundError, PermissionError) as e:
        Print(f"Error: {e}")
        exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - X Starting Build
    if not Confirm.ask("Proceed with Build ", default=True):
        exit(1)

    console.print("[bright_white]Starting Build...")

    console.print("Starting Package Build with --no-clean option...")
    _errors = 0
    _total = len(folder_list)
    _completed = 0

    try:
        with open(os.path.join(dir_list.dir_log, 'dpkg-build.log'), "w") as dpkg_build_log:
            with console.status('') as status:
                for dsc_file in dsc_files:
                    _completed += 1
                    folder_name = os.path.basename(folder_list[dsc_file])
                    status.update(f"{_completed}/{_total} - Building {folder_name}")

                    log_filename = os.path.join(dir_list.dir_log, "build", folder_name + '.log')
                    with open(log_filename, "w") as logfile:
                        process = subprocess.Popen(["dpkg-checkbuilddeps"],
                                                   cwd=folder_list[dsc_file], stdout=logfile, stderr=logfile)
                        if not process.wait():
                            process = subprocess.Popen(
                                ["dpkg-buildpackage", "-b", "-uc", "-us", "-nc", "-a", "amd64"],
                                cwd=folder_list[dsc_file], stdout=logfile, stderr=logfile)
                            if not process.wait():
                                dpkg_build_log.write(f"PASS: {folder_name}\n")
                                continue

                        dpkg_build_log.write(f"FAIL: {os.path.basename(folder_name)}\n")
                        console.print(f"Build failed for {os.path.basename(folder_name)}")
                        _errors += 1

                        dpkg_build_log.flush()

        if _errors:
            console.print(f"dpkg-buildpackage failed for {_errors} instances")
        else:
            console.print("Completed Build")

    except (FileNotFoundError, PermissionError) as e:
        Print(f"Error: {e}")
        exit(1)


# Main function
if __name__ == '__main__':
    print(asciiart_logo)
    main()
