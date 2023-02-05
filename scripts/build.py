# (C) Athena Linux Project

# External imports
import argparse
import bz2
import configparser
import gzip
import hashlib
import logging
import os
import re
import subprocess
from logging import Logger

import apt_pkg
from rich.console import Console
from rich.logging import RichHandler
from rich.prompt import Prompt, Confirm

import package
import source
# Local imports
import utils
from package import Package
from source import Source

asciiart_logo = '╔══╦╗╔╗─────────╔╗╔╗\n' \
                '║╔╗║╚╣╚╦═╦═╦╦═╗─║║╠╬═╦╦╦╦╦╗\n' \
                '║╠╣║╔╣║║╩╣║║║╬╚╗║╚╣║║║║║╠║╣\n' \
                '╚╝╚╩═╩╩╩═╩╩═╩══╝╚═╩╩╩═╩═╩╩╝'


# TODO: make all apt_pkg.parse functions arch specific

def build_cache(base: utils.BaseDistribution, dir_cache: str, con: Console, logger: Logger) -> dict[str, str]:
    """Builds the Cache. Release file is used based on BaseDistribution defined
        Args:
            base (BaseDistribution): details of the system being derived from
            dir_cache (str): Dir where cache files are to be downloaded
            con (Console): default output
            logger (Logger): logger

        Returns:
            dict {}:
    """
    cache_files = {}

    # TODO: Support https
    base_url = 'http://' + base.url + '/' + base.baseid + '/dists/' + base.codename
    base_filename = base.url + '_' + base.baseid + '_dists_' + base.codename

    # Default release file
    release_url = base_url + '/InRelease'
    release_file = os.path.join(dir_cache, base_filename + '_InRelease')

    # By default download
    # TODO: have override - offline flag
    if utils.download_file(release_url, release_file, con, logger) <= 0:
        exit(1)

    # sequence is Packages, Translation & Sources
    # you change it you break it
    # TODO: Enable to be configurable, should not hardcode
    # TODO: Use the apt_pkg functions & maybe apt_cache
    cache_source = [base_url + '/main/binary-' + base.arch + '/Packages.gz',
                    base_url + '/main/source/Sources.gz']

    cache_filename = ['main/binary-' + base.arch + '/Packages',
                      'main/source/Sources']

    cache_destination = [os.path.join(dir_cache, base_filename + '_main_binary-' + base.arch + '_Packages.gz'),
                         os.path.join(dir_cache, base_filename + '_main_source_Sources.gz')]

    md5 = []
    # Extract the md5 for the files
    # TODO: Enable Optional SHA256 also
    try:
        with open(release_file, 'r') as f:
            contents = f.read()
            for file in cache_filename:
                #  Typical format - [space] [32 char md5 hash] [space] [file size] [space] [relative path] [eol]
                re_pattern = r' ([a-f0-9]{32})\s+([^\s]+)\s+' + file + '$'
                match = re.search(re_pattern, contents, re.MULTILINE)
                if match:
                    md5.append(match.group(1))
                else:
                    logger.critical(f"Error finding hash for {file}")
                    exit(1)
    except (FileNotFoundError, PermissionError) as e:
        logger.exception(f"Error: {e}")
        exit(1)

    # Iterate over destination files
    for file in cache_destination:
        # searching for the decompressed files - stripping extensions
        base = os.path.splitext(file)[0]
        if os.path.isfile(base):
            # Open the file and calculate the MD5 hash
            with open(base, 'rb') as f:
                fdata = f.read()
                md5_check = hashlib.md5(fdata).hexdigest()
        else:
            md5_check = ''

        index = cache_destination.index(file)
        if md5[index] != md5_check:
            # download given file to location
            if (utils.download_file(cache_source[index], cache_destination[index], con, logger)) <= 0:
                exit(1)

            # decompress file based on extension
            base, ext = os.path.splitext(file)
            if ext == '.gz':
                with gzip.open(file, 'rb') as f_in:
                    with open(base, 'wb') as f_out:
                        f_out.write(f_in.read())
            elif ext == '.bz2':
                with bz2.BZ2File(file, 'rb') as f_in:
                    with open(base, 'wb') as f_out:
                        f_out.write(f_in.read())
            else:
                # if no ext leave as such
                # TODO: check if other extensions are required to be supported
                continue

        # List of cache files are in the sequence specified earlier
        cache_files[os.path.basename(cache_filename[index])] = base

    return cache_files


def main():
    selected_packages: {str: Package} = {}
    source_packages: {str: Source} = {}
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

        base_distribution = utils.BaseDistribution(baseurl, baseid, basecodename, baseversion, arch)

        dir_download = os.path.join(working_dir, config_parser.get('Directories', 'Download'))
        dir_log = os.path.join(working_dir, config_parser.get('Directories', 'Log'))
        dir_cache = os.path.join(working_dir, config_parser.get('Directories', 'Cache'))
        # dir_temp = os.path.join(working_dir, config_parser.get('Directories', 'Temp'))
        dir_source = os.path.join(working_dir, config_parser.get('Directories', 'Source'))

    except configparser.Error as e:
        print(f"Athena Linux: Config Parser Error: {e}")
        exit(1)

    try:
        with open(pkglist_path, 'r') as f:
            contents = f.read()
            required_packages = contents.split('\n')
    except (FileNotFoundError, PermissionError) as e:
        print(f"Error: {e}")
        exit(1)

    # --------------------------------------------------------------------------------------------------------------
    # Setting up common systems
    apt_pkg.init_system()
    console = Console()
    log_format = "%(message)s"
    logging.basicConfig(level="INFO", format=log_format, datefmt="[%X]", handlers=[RichHandler()])
    logger = logging.getLogger('rich')

    # --------------------------------------------------------------------------------------------------------------
    console.print("[white]Starting Source Build System for Athena Linux...")
    console.print("Building for ...")
    console.print(f"\t Arch\t\t\t{arch}")
    console.print(f"\t Parent Distribution\t{basecodename} {baseversion}")
    console.print(f"\t Build Distribution\t{build_codename} {build_version}")

    # --------------------------------------------------------------------------------------------------------------
    # Step I - Building Cache
    console.print("[bright_white]Building Cache...")
    cache_files = build_cache(base_distribution, dir_cache, console, logger)

    # get file names from cache
    package_file = cache_files['Packages']
    source_file = cache_files['Sources']

    # load data from the files
    try:
        console.print(f"Using Package List: {os.path.basename(package_file)}")
        with open(package_file, 'r') as f:
            contents = f.read()
            package_record = contents.split('\n\n')
    except (FileNotFoundError, PermissionError) as e:
        logger.exception(f"Error: {e}")
        exit(1)

    try:
        console.print(f"Using Source List: {os.path.basename(source_file)}")
        with open(source_file, 'r') as f:
            contents = f.read()
            source_records = contents.split('\n\n')
    except (FileNotFoundError, PermissionError) as e:
        logger.exception(f"Error: {e}")
        exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step II - Parse Dependencies
    console.print("[bright_white]Parsing Dependencies...")

    count_pkgs = 0
    # This is recursive function, status cant be created local to the function
    with console.status('') as status:
        # Iterate through package list and identify dependencies
        for pkg in required_packages:
            if pkg and not pkg.startswith('#') and not pkg.isspace():
                # Skip if added from previously parsed dependency tree
                if pkg not in selected_packages.keys():
                    # remove spaces
                    pkg = pkg.strip()
                    count_pkgs += 1
                    package.parse_dependencies(package_record, selected_packages, pkg, console, status)

    not_parsed = [obj.name for obj in selected_packages.values() if obj.version == '-1']
    console.print(f"Total Required Packages {count_pkgs}")
    console.print(f"Total Dependencies Selected are : {len(selected_packages)}")
    console.print(f"Dependencies Not Parsed: {len(not_parsed)}")

    for pkg_name in not_parsed:
        logger.warning(f"Not parsed: {pkg_name}")

    # -------------------------------------------------------------------------------------------------------------
    # Step III - Checking Breaks and Conflicts
    console.print("[bright_white]Checking Breaks and Conflicts...")
    for pkg in selected_packages:
        # Breaks will still allow to install - Warning
        for breaks in selected_packages[pkg].breaks:
            if breaks[0] in selected_packages:
                pkg_name = breaks[0]
                pkg_ver = selected_packages[pkg_name].version
                break_version = breaks[1]
                break_comparator = breaks[2]

                if break_comparator == '' or apt_pkg.check_dep(break_version, break_comparator, pkg_ver):
                    logger.warning(f"Package {pkg} breaks {pkg_name}")

        # Conflicts will break installation - Error
        for conflicts in selected_packages[pkg].conflicts:
            if conflicts[0] in selected_packages:
                pkg_name = conflicts[0]
                pkg_ver = selected_packages[pkg_name].version
                conflicts_version = conflicts[1]
                conflicts_comparator = conflicts[2]

                if conflicts_comparator == '' or apt_pkg.check_dep(conflicts_version, conflicts_comparator, pkg_ver):
                    logger.error(f"Package {pkg} conflicts with {pkg_name}")

    # -------------------------------------------------------------------------------------------------------------
    # Step IV - Checking Version Constraints
    console.print("[bright_white]Checking Version Constraints...")
    for pkg in selected_packages:
        if not selected_packages[pkg].constraints_satisfied:
            logger.warning(f"Version Constraint failed for {pkg}:{selected_packages[pkg].name}")

    try:
        with open(os.path.join(dir_log, 'selected_packages.list'), 'w') as f:
            for pkg in selected_packages:
                f.write(str(selected_packages[pkg]) + '\n')
    except (FileNotFoundError, PermissionError) as e:
        logger.exception(f"Error: {e}")
        exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - V Check Alternate dependency
    console.print("[bright_white]Alternate Dependency Check...")

    # Check for dependencies that can be satisfied by multiple packages
    for pkg in selected_packages:
        for altdepends in selected_packages[pkg].altdepends:
            if altdepends not in multi_dep:
                multi_dep.append(altdepends)

    for section in multi_dep:
        found = False
        for pkg in section:
            pkg_name = pkg[0]
            if pkg_name in selected_packages:
                pkg_version = pkg[1]
                pkg_constraint = pkg[2]
                if apt_pkg.check_dep(selected_packages[pkg_name].version, pkg_constraint, pkg_version):
                    found = True
                else:
                    logger.warning(f"Alt Dependency Check - Version constraint failed for {pkg_name}")
        if not found:
            logger.warning(f"dependency unresolved between {section}")

    # -------------------------------------------------------------------------------------------------------------
    # Step - VI Check for discrepancy between source version and package version
    console.print("[bright_white]Checking for discrepancy between source package version...")

    for pkg_name in selected_packages:
        source_name = selected_packages[pkg_name].source[0]
        source_version = selected_packages[pkg_name].source[1]
        pkg_version = selected_packages[pkg_name].version

        # Where Source version is not given, it is assumed same as package version
        if source_version == '':
            source_version = pkg_version

        # Add package to source list
        if source_name not in source_packages:
            source_packages[source_name] = Source(source_name, source_version)

        if not source_version == pkg_version:
            console.print(f"Package and Source version mismatch "
                          f"{pkg_name}: {pkg_version} -> {source_name}: {source_version} Using {source_version}")
            selected_packages[pkg_name].reset_source_version(source_version)

    console.print("Source requested for : ", len(source_packages), " packages")

    # -------------------------------------------------------------------------------------------------------------
    # Step - VI Parse Source Packages
    console.print("[bright_white]Parsing Source Packages...")

    # Parse Sources Control file
    total_src_count, total_src_size = source.parse_sources(source_records, source_packages, console, logger)

    missing_source = [_pkg for _pkg in source_packages if not source_packages[_pkg].found]

    if not len(missing_source) == 0:
        for pkg in missing_source:
            console.print(f"Source not found for : {source_packages[pkg].name} {source_packages[pkg].version} "
                          f"Alternates: {source_packages[pkg].alternates}")
            if Confirm.ask("Select from Alternates? if N, source package will be ignored(y/n"):
                new_version = Prompt.ask(f"Enter Alt Version", choices=source_packages[pkg].alternates)
                source_packages[pkg].reset_version(new_version)

        # rerun parse_source only for the missing source
        source.parse_sources(source_records, missing_source, console, logger)

    try:
        with open(os.path.join(dir_log, 'source_packages.list'), 'w') as f:
            for pkg in source_packages:
                f.write(str(source_packages[pkg]) + '\n')
    except (FileNotFoundError, PermissionError) as e:
        logger.exception(f"Error: {e}")
        exit(1)

    try:
        with open(os.path.join(dir_log, 'source_file.list'), 'w') as f:
            for src_pkg in source_packages:
                if source_packages[src_pkg].found:
                    for file in source_packages[src_pkg].files:
                        _filename = file
                        _filepath = source_packages[src_pkg].files[file]['path']
                        _filesize = source_packages[src_pkg].files[file]['size']
                        _filehash = source_packages[src_pkg].files[file]['md5']
                        f.write(f"{_filename} {_filepath} {_filesize} {_filehash}\n")
    except (FileNotFoundError, PermissionError) as e:
        logger.exception(f"Error: {e}")
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

    failed_dep = ''
    failed_dep_version = ''
    conflicts_pkg = ''
    build_alt_dep = []

    for src_pkg in source_packages:
        # Check - if build dependency is installed, and installed package is right version
        for dep in source_packages[src_pkg].build_depends:
            _pkg = dep[0]
            if _pkg not in installed_packages:
                if not re.search(r"\b{}\b".format(_pkg), failed_dep):
                    failed_dep += f'{_pkg} '
            else:
                if not dep[2] == '':  # no comparison to do
                    if not apt_pkg.check_dep(installed_packages[_pkg], dep[2], dep[1]):
                        logger.error(f"Build Dependency version check failed for {src_pkg}: {_pkg} {dep[1]}")
                        failed_dep_version += f'{_pkg} ({dep[2]} {dep[1]}) '

        # Check - if conflict is installed, and installed package matches conflict version
        for dep in source_packages[src_pkg].conflicts:
            _pkg = dep[0]
            if _pkg in installed_packages:
                if dep[2] == '' or apt_pkg.check_dep(installed_packages[_pkg], dep[2], dep[1]):
                    logger.error(f"Build Dependency conflict {src_pkg}: {_pkg} {dep[1]}")
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
                        logger.warning(f"Alt Build Dependency Check - Version constraint failed for {pkg_name}")
            if not found:
                if section not in build_alt_dep:
                    build_alt_dep.append(section)

    # TODO: Check conflict within build dependency
    if not failed_dep == '':
        console.print("Build Dependency failed for ", failed_dep)
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
            logger.warning(f"Build dependency unresolved between {section}")
    else:
        console.print("PASSED: Build Alt Dependency Check")

    try:
        with open(os.path.join(dir_log, 'build_dependency.list'), 'w') as f:
            f.write("Build Dependencies Failed:\n")
            f.write(f"{failed_dep}\n")
            f.write("\nDependencies Version Check Failed:\n")
            f.write(f"{failed_dep_version}\n")
            f.write("\nDependencies Version Check Failed:\n")
            f.write(f"{conflicts_pkg}\n")
            f.write("\nAlt Dependencies Check Failed:\n")
            f.write(f"{str(build_alt_dep)}\n")
    except (FileNotFoundError, PermissionError) as e:
        logger.exception(f"Error: {e}")
        exit(1)

    if not (failed_dep == '' and failed_dep_version == '' and conflicts_pkg == ''):
        logger.error("There are pending Build Dependencies issues, Manual check is required")
        if not Confirm.ask("Proceed: (y/n)"):
            exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - VIII Download Source files
    console.print("[bright_white]Download Source files...")

    console.print("Total File Selected are :", total_src_count)
    console.print("Total Download is about ", round(total_src_size / (1024 * 1024)), "MB")
    console.print("Starting Downloads...")
    utils.download_source(source_packages, dir_download, base_distribution, console, logger)

    # -------------------------------------------------------------------------------------------------------------
    # Step - IX Expanding the Source Packages
    console.print("[bright_white]Expanding the Source Packages...")

    folder_list = {}
    dsc_files = []
    try:
        with open(os.path.join(dir_log, 'dpkg-source.log'), "w") as logfile:
            with console.status('') as status:

                for pkg in source_packages:
                    for file in source_packages[pkg].files:
                        if os.path.splitext(file)[1] == '.dsc':
                            dsc_files.append(file)
                            folder_name = os.path.join(dir_source, os.path.splitext(file)[0])
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
                    dsc_file = os.path.join(dir_download, file)
                    process = subprocess.Popen(
                        ["dpkg-source", "-x", dsc_file, folder_name], stdout=logfile, stderr=logfile)
                    if process.wait():
                        _errors += 1
                if _errors:
                    logger.error(f"dpkg-source failed for {_errors} instances, please check dpkg-source.log")

        with open(os.path.join(dir_log, 'source_folder.list'), 'w') as f:
            for folder in folder_list:
                f.write(f"{folder} {folder_list[folder]} \n")

    except (FileNotFoundError, PermissionError) as e:
        logger.exception(f"Error: {e}")
        exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - X Starting Build
    if not Confirm.ask("Proceed with Build: (y/n)", default=True):
        exit(1)

    console.print("[bright_white]Starting Build...")

    console.print("Starting Package Build with --no-clean option...")
    _errors = 0
    _total = len(folder_list)
    _completed = 0

    try:
        with open(os.path.join(dir_log, 'dpkg-build.log'), "w") as dpkg_build_log:
            with console.status('') as status:
                for dsc_file in dsc_files:
                    _completed += 1
                    folder_name = os.path.basename(folder_list[dsc_file])
                    status.update(f"{_completed}/{_total} - Building {folder_name}")

                    log_filename = os.path.join(dir_log, "build", folder_name + '.log')
                    with open(log_filename, "w") as logfile:
                        process = subprocess.Popen(["dpkg-checkbuilddeps"],
                                                   cwd=folder_list[dsc_file], stdout=logfile, stderr=logfile)
                        if not process.wait():
                            process = subprocess.Popen(
                                ["dpkg-buildpackage", "-b", "-uc", "-us", "-nc", "-a", "amd64", "-J"],
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
        logger.exception(f"Error: {e}")
        exit(1)


# Main function
if __name__ == '__main__':
    print(asciiart_logo)
    main()
