# (C) Athena Linux Project
import argparse
import bz2
import configparser
import gzip
import hashlib
import os
import re
import subprocess

import apt_pkg
import requests
from requests import Timeout, TooManyRedirects, HTTPError, RequestException
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn, TransferSpeedColumn, TextColumn, BarColumn, \
    TaskProgressColumn, MofNCompleteColumn, DownloadColumn

asciiart_logo = '╔══╦╗╔╗─────────╔╗╔╗\n' \
                '║╔╗║╚╣╚╦═╦═╦╦═╗─║║╠╬═╦╦╦╦╦╗\n' \
                '║╠╣║╔╣║║╩╣║║║╬╚╗║╚╣║║║║║╠║╣\n' \
                '╚╝╚╩═╩╩╩═╩╩═╩══╝╚═╩╩╩═╩═╩╩╝'


# TODO: make all apt_pkg.parse functions arch specific

# TODO: combine the two classes Package & Source
class Source:
    """
    Source is being used to track which packages need to be selected for satisfying the selected_package list
    Args:
        name: the name of the source package, once set should not be changed
        version: version expected, maybe reset to alternates
    """

    def __init__(self, name, version):
        if name == '':
            raise ValueError(f"Package being created with empty package name")

        self._name = name
        self._version = version
        self.found = False
        self._alternate = []

    def __str__(self):
        return str(f"{self._name} {self._version} {self.found} {self._alternate}")

    @property
    def name(self):
        return self._name

    @property
    def version(self):
        return self._version

    def reset_version(self, version: str):
        if not version == '':
            raise ValueError(f"Package being set with empty version")
        self._version = version

    @property
    def alternates(self):
        return self._alternate

    def add_alternate(self, version: str):
        if not version == '':
            self._alternate.append(version)


class Package:
    """
        Package is being used to track which packages need to be parsed for dependencies,
        It enables, checking version constraints, version is set to -1 is it is not net parsed
        Args:
            name: the name of the package, once set should not be changed
        """

    def __init__(self, name):
        if name == '':
            raise ValueError(f"Package being created with empty package name")

        self._name: str = name
        self._version: str = '-1'
        self.version_constraints: {} = {}
        self._provides: [] = []
        self._source: () = ('', '')
        self._depends: {} = {}

    def __str__(self):
        return str(
            f"{self._name} {self._version} {self.version_constraints} {self._provides} {self._source} {self._depends}")

    def __eq__(self, other):
        if not isinstance(other, Package):
            raise TypeError(f"Trying to compare to a different object type {type(other)}")

        if other.name == self.name:
            return True

        if other.name in self._provides:
            return True

        return False

    @property
    def source(self):
        return self._source

    @source.setter
    def source(self, source_string):
        source_list = apt_pkg.parse_depends(source_string)
        for source in source_list:
            if source[0] == '':
                continue
            self._source = (source[0][0], source[0][1])

    @property
    def provides(self) -> {}:
        return self._provides

    def add_provides(self, provides: [str]):
        for pkg in provides:
            if pkg == '':
                continue
            self._provides.append(pkg)

    @property
    def depends(self):
        return self._depends

    def add_depends(self, depends_string):
        depends = apt_pkg.parse_depends(depends_string)
        self._depends = depends

    @property
    def constraints_satisfied(self) -> bool:
        # needs a version to check against
        if self.version == '':
            return False
        return self.check_version(self.version)

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        raise RuntimeWarning("Cant change the name once set, will be ignored")

    @property
    def version(self):
        return self._version

    @version.setter
    def version(self, version: str):
        if not self.check_version_format(version):
            raise ValueError(f"Incorrect Version Format being set {version}")

        # Version is set even if constraints fail
        self._version = version

    # TODO: Write function to check version formatting
    def check_version_format(self, version) -> bool:
        if self.version == self.version:
            pass
        return True

    def add_version_constraint(self, version, constraint):
        # version can in the form of <constraint> <version number> or just <Version number>
        # <constraints> are in form of =, <<, >>, >=, <=
        # = and !<constraints> will be considered hard assignments
        if version == '':
            return
        if constraint == '':
            constraint = '='

        if constraint not in {'=', '>', '<', '>=', '<='}:
            raise ValueError(f"Unspecified Constraint being set {constraint}")

        if not self.check_version_format(version):
            raise ValueError(f"Incorrect Version Format being set {version}")
        # add constraint
        # TODO: Check if different constraint is already set
        self.version_constraints[version] = constraint

    def check_version(self, check_version: str) -> bool:
        # check version against the saved constraints
        for _version, _constraint in self.version_constraints.items():
            if not apt_pkg.check_dep(check_version, _constraint, _version):
                return False
        return True


class BaseDistribution:
    def __init__(self, url: str, baseid: str, codename: str, version: str, arch: str):
        self.url: str = url
        self.baseid: str = baseid
        self.codename: str = codename
        self.version: str = version
        self.arch: str = arch


def search(re_string: str, base_string: str):
    _match = re.search(re_string, base_string)
    if _match is not None:
        return _match.group(1)
    return ''


def download_file(url: str, filename: str, progressbar: Progress, task) -> None:
    """Downloads file and updates progressbar in incremental manner.
        Args:
            url (str): url to download file from, protocol is prepended
            filename: (str): Filename to save to, location should be writable
            progressbar: (rich.Progress): Progressbar to update
            task: (rich.task_id): task for the progressbar

        Returns:
            None
    """
    total_size = progressbar.tasks[task].total
    try:
        response = requests.head(url)
        file_size = int(response.headers.get('content-length', 0))
        total_size += file_size

        progressbar.update(task, total=total_size)

        response = requests.get(url, stream=True)
        if response.status_code == 200:
            with open(filename, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
                        progressbar.update(task, advance=len(chunk))
        else:
            progressbar.log(f"Error Downloading {url}")
            exit(1)
    except (ConnectionError, Timeout, TooManyRedirects, HTTPError, RequestException) as e:
        print(f"Error connecting to {url}: {e}")
        exit(1)


def build_cache(base: BaseDistribution, dir_cache: str, cache_progress: Progress) -> dict[str, str]:
    """Builds the Cache. Release file is used based on BaseDistribution defined
        Args:
            base (BaseDistribution): details of the system being derived from
            dir_cache: (str): Dir where cache files are to be downloaded
            cache_progress: (rich.Progress): Progressbar to update

        Returns:
            None
    """
    total_size = 0
    cache_files = {}

    # TODO: Support https
    base_url = 'http://' + base.url + '/' + base.baseid + '/dists/' + base.codename
    basefilename = base.url + '_' + base.baseid + '_dists_' + base.codename

    # Default release file
    release_url = base_url + '/InRelease'
    release_file = os.path.join(dir_cache, basefilename + '_InRelease')

    c_task = cache_progress.add_task("Building Cache".ljust(20, ' '), total=1)

    # By default download
    # TODO: have override - offline flag
    download_file(release_url, release_file, cache_progress, c_task)

    # sequence is Packages, Translation & Sources
    # you change it you break it
    # TODO: Enable to be configurable, should not hardcode
    # TODO: Use the apt_pkg functions & maybe apt_cache
    cache_source = [base_url + '/main/binary-' + base.arch + '/Packages.gz',
                    base_url + '/main/i18n/Translation-en.bz2',
                    base_url + '/main/source/Sources.gz']

    cache_filename = ['main/binary-' + base.arch + '/Packages',
                      'main/i18n/Translation-en',
                      'main/source/Sources']

    cache_destination = [os.path.join(dir_cache, basefilename + '_main_binary-' + base.arch + '_Packages.gz'),
                         os.path.join(dir_cache, basefilename + '_main_i18n_Translation-en.bz2'),
                         os.path.join(dir_cache, basefilename + '_main_source_Sources.gz')]

    md5 = []
    # Extract the md5 for the files
    # TODO: Enable Optional SHA256 also
    try:
        with open(release_file, 'r') as f:
            contents = f.read()
            for file in cache_filename:
                #  Typical format - [space] [32 char md5 hash] [space] [file size] [space] [relative path] [eol]
                re_pattern = r' ([a-f0-9]{32}) (\S+) ' + file + '$'
                match = re.search(re_pattern, contents, re.MULTILINE)
                if match:
                    md5.append(match.group(1))
                else:
                    cache_progress.log(f"Error finding hash for {file}")
                    exit(1)
    except (FileNotFoundError, PermissionError) as e:
        print(f"Error: {e}")
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
            download_file(cache_source[index], cache_destination[index], cache_progress, c_task)

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


# Iterative Function
def parse_dependencies(
        package_record: list[str],
        selected_packages: {},
        required_package: str,
        multi_dep: [],
        dependency_progress,
        dependency_task):
    """Parse Dependencies for required_packages based on package_record in recursive manner
    populates the selected_packages[] from the list and cases of Alt dependencies

            Parameters:
                package_record: Taken from the Package file
                selected_packages: populates based on dependencies recursively
                required_package: the package to find dependencies for
                multi_dep: populates with packages which have alt dependencies'
                dependency_progress: Progressbar to update
                dependency_task: task for the progressbar

            Returns:
                None
        """
    # Package: Record is typically of the format, other records not shown
    # Package: Only one package name, could contain numbers, hyphen, underscore, dot, etc.
    # Source:  one source package, optional - version in brackets separated by space
    # Version: single version string, may contain alphanumeric and ': - ~ . +'
    # Provides: may provide one or more packages, package list is comma separated, may have versions in ()
    #           versions are always preceded by '=' e.g.  (= 5.0.1~5.16.0~dfsg+~4.16.8-5)
    # Replaces: one or more packages or even self, may include version in (), versions have prefix << >> <= >= =
    # Breaks: one or more packages, may include version in (), versions have prefix << >> <= >= =
    # Depends: one or more packages, may include version in (), versions have prefix << >> <= >= =
    #          may have arch specified as name:arch e.g. gcc:amd64, python3:any
    #          dependencies which can be satisfied by multiple packages separated by | e.g. libglu1-mesa | libglu1,
    # Recommends:
    # weirdest Version node-acorn (<< 6.0.2+20181021git007b08d01eff070+ds+~0.3.1+~4.0.0+~0.3.0+~5.0.0+ds+~1.6.1+ds-2~)

    for _package in package_record:
        # Get Package Name
        package_name = search(r'Package: ([^\n]+)', _package)

        # Dependencies are Satisfied on Provides also
        package_provides = search(r'Provides: ([^\n]+)', _package)
        provides_list = [pkg[0][0] for pkg in apt_pkg.parse_depends(package_provides)]

        # Check id Dependency is satisfied either through 'Package' or 'Provides'
        if required_package != package_name:
            if required_package not in provides_list:
                continue

        # if not already there, also avoids separate iteration on provides
        if selected_packages.get(package_name) is None:
            package = Package(package_name)
            selected_packages[package_name] = package
        else:
            package = selected_packages[package_name]

        # Incase version is set, it has already been parsed
        if not package.version == '-1':
            break

        # Bothersome multi-provides condition,
        # if a package is added, assume all Provides have been satisfied
        # Problem remains that for provides - more than one package may satisfy it, cant go with first come
        # TODO: Parse complete set and give user option to select which package option they want selected.
        for _pkg in provides_list:
            if not _pkg == '':
                selected_packages[_pkg] = package

        # Get Package Version
        package_version = search(r'Version: ([^\n]+)', _package)
        if package_version == '':
            raise ValueError(f"There doesnt seem to be a Version \n {_package}")

        # Get source if available, else assume the source package is same as package name
        # TODO: Check if this assumption is true
        package_source = search(r'Source: ([^\n]+)', _package)
        if package_source == '':
            package_source = package_name

        # Get dependency from both Depends: & Pre-Depends:
        depends_group = search(r'\nDepends: ([^\n]+)', _package)
        pre_depends_group = search(r'\nPre-Depends: ([^\n]+)', _package)

        depends: str = ''
        depends += depends_group

        # Let's stitch them together, none is mandatory
        if depends == '':
            depends = pre_depends_group
        else:
            if not pre_depends_group == '':
                depends += ', ' + pre_depends_group

        # TODO: Check for dependencies that can be satisfied by multiple packages
        _depends = depends.split(', ')
        for _dep in _depends:
            multi = re.search(r'\|', _dep)
            if multi is not None:
                multi_dep.append(re.split(' \| ', _dep))
                continue

        package.version = package_version
        package.source = package_source
        package.add_provides(package_provides)
        package.add_depends(depends)

        # Update Progress bar
        completed = len([obj for obj in selected_packages.values() if not obj.version == '-1'])
        dependency_progress.update(dependency_task, total=len(selected_packages), completed=completed)

        # skip the ones which have more than one packages satisfying dependency
        parsed_depends = [sublist[0] for sublist in package.depends if len(sublist) == 1]
        for _pkg in parsed_depends:
            # _pkg is usually in [name, version, constraint] tuple format
            dep_package_name = _pkg[0]

            # Check if not already parsed
            if selected_packages.get(dep_package_name) is None:
                selected_packages[dep_package_name] = Package(dep_package_name)
                parse_dependencies(
                    package_record,
                    selected_packages,
                    dep_package_name,
                    multi_dep,
                    dependency_progress,
                    dependency_task)
            selected_packages[dep_package_name].add_version_constraint(_pkg[1], _pkg[2])
        break


def download_source(file_list,
                    download_dir,
                    download_size,
                    baseurl,
                    baseid,
                    total_download_progress,
                    download_progress):
    # base_url = "http://deb.debian.org/debian/"
    base_url = 'http://' + baseurl + '/' + baseid + '/'
    total_files = len(file_list)

    t_task = total_download_progress.add_task("Total Files".ljust(20, ' '), total=total_files)
    f_task = download_progress.add_task("Overall Download".ljust(20, ' '), total=download_size)
    c_task = download_progress.add_task("Current Download".ljust(20, ' '), total=0)

    for file_name, data in file_list.items():
        url = base_url + data['path']
        size = data['size']
        md5 = data['md5']

        download_path = os.path.join(download_dir, file_name)

        if os.path.isfile(download_path):
            # Open the file and calculate the MD5 hash
            with open(download_path, 'rb') as f:
                fdata = f.read()
                md5_check = hashlib.md5(fdata).hexdigest()
        else:
            md5_check = ''

        if md5 != md5_check:
            download_file(url, download_path, download_progress, c_task)

        # TODO: Verify hash and download file size
        download_progress.advance(f_task, advance=int(size))
        total_download_progress.advance(t_task)


def parse_sources(source_records,
                  source_packages,
                  file_list,
                  builddep,
                  source_progress,
                  source_task):
    download_size = 0

    # Iterate over Packages for which Source package is required
    for required_package in source_packages:
        # Search within the Source List file
        for package in source_records:
            package_name = search(r'Package: ([^\n]+)', package)
            package_version = search(r'Version: ([^\n]+)', package)

            # On Match
            if package_name == required_package:
                if apt_pkg.check_dep(source_packages[required_package].version, '=', package_version):
                    if not source_packages[required_package].found:
                        source_progress.advance(source_task)

                        # Get all files
                        package_directory = search(r'Directory:\s*(.+)', package)

                        # TODO: Currently using md5, should enable SHA256 also
                        files = re.findall(r'\s+([a-fA-F\d]{32})\s+(\d+)\s+(\S+)', package)
                        for file in files:
                            file_list[file[2]] = {
                                'path': os.path.join(package_directory, file[2]), 'size': file[1], 'md5': file[0]}
                            download_size += int(file[1])

                        # set as package found
                        source_packages[required_package].found = True

                        # Parse Build Depends
                        build_depends = search(r'Build-Depends: ([^\n]+)', package)
                        build_depends_indep = search(r'Build-Depends-Indep: ([^\n]+)', package)
                        build_depends_arch = search(r'Build-Depends-Arch: ([^\n]+)', package)

                        depends_string = ''
                        for dep_str in [build_depends, build_depends_indep, build_depends_arch]:
                            if not dep_str == '':
                                depends_string += dep_str + ', '

                        if depends_string == '':
                            continue

                        build_depends = apt_pkg.parse_src_depends(depends_string, architecture='amd64')
                        # TODO: cater for conditions of Alt Dependency
                        build_depends = [dep[0][0] for dep in build_depends if not dep[0][0] == '']
                        for dep in build_depends:
                            if dep not in builddep:
                                builddep.append(dep)
                        break
                # Add in alternates
                source_packages[required_package].add_alternate(package_version)

    return download_size


def main():
    selected_packages: {str: Package} = {}
    source_packages = {}
    multi_dep = []
    file_list = {}
    builddep = []
    required_builddep = []
    installed_packages = []

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

        base_distribution = BaseDistribution(baseurl, baseid, basecodename, baseversion, arch)

        dir_download = os.path.join(working_dir, config_parser.get('Directories', 'Download'))
        dir_log = os.path.join(working_dir, config_parser.get('Directories', 'Log'))
        dir_cache = os.path.join(working_dir, config_parser.get('Directories', 'Cache'))
        dir_temp = os.path.join(working_dir, config_parser.get('Directories', 'Temp'))
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

    # Setting up Progress Meter
    task_description = ["Building Cache", "Parse Dependencies", "Check Alternate Dependency", "Parse Source Packages",
                        "Source Build Dependency Check", "Download Source files", "Expanding Source Packages",
                        "Building Packages"]

    overall_progress = Progress(TextColumn("Step {task.completed} of {task.total} - {task.description}"))
    cache_progress = Progress(TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn())
    dependency_progress = Progress(TextColumn("{task.description} {task.total}"), TimeElapsedColumn(), SpinnerColumn())
    source_progress = Progress(TextColumn("{task.description}"), BarColumn(), TaskProgressColumn())
    total_download_progress = Progress(
        TextColumn("{task.description}"), BarColumn(), TaskProgressColumn(), MofNCompleteColumn())
    download_progress = Progress(TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn())
    debsource_progress = Progress(TextColumn("{task.description}"), BarColumn(), TaskProgressColumn())
    build_progress = Progress(TextColumn("Building Packages".ljust(20, ' ')),
                              MofNCompleteColumn(), SpinnerColumn(), TextColumn("{task.description}"))
    progress_panel = Panel("Progress")
    progress_group = Group(Panel(Group(
        cache_progress,
        dependency_progress,
        source_progress,
        total_download_progress,
        download_progress,
        debsource_progress,
        build_progress), title="Progress", title_align="left"), progress_panel, overall_progress)

    with Live(progress_group) as live:
        live.console.print("[white]Starting Source Build System for Athena Linux...")
        live.console.print("Building for ...")
        live.console.print(f"\t Arch\t\t\t{arch}")
        live.console.print(f"\t Parent Distribution\t{basecodename} {baseversion}")
        live.console.print(f"\t Build Distribution\t{build_codename} {build_version}")
        overall_task = overall_progress.add_task("All Jobs", total=len(task_description))

        apt_pkg.init_system()
        # --------------------------------------------------------------------------------------------------------------
        # Step I - Building Cache
        overall_progress.update(overall_task, description=task_description[0])
        overall_progress.advance(overall_task)

        cache_files = build_cache(base_distribution, dir_cache, cache_progress)

        # get file names from cache
        package_file = cache_files['Packages']
        source_file = cache_files['Sources']

        # load data from the files
        try:
            with open(package_file, 'r') as f:
                contents = f.read()
                package_record = contents.split('\n\n')
        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)

        try:
            with open(source_file, 'r') as f:
                contents = f.read()
                source_records = contents.split('\n\n')
        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)

        # -------------------------------------------------------------------------------------------------------------
        # Step II - Parse Dependencies
        overall_progress.update(overall_task, description=task_description[1])
        overall_progress.advance(overall_task)

        dependency_task = dependency_progress.add_task("Parsing Dependencies".ljust(20, ' '))

        # Iterate through package list and identify dependencies
        for pkg in required_packages:
            if pkg and not pkg.startswith('#') and not pkg.isspace():
                # Skip if added from previously parsed dependency tree
                if pkg not in selected_packages.keys():
                    # remove spaces
                    pkg = pkg.strip()

                    parse_dependencies(package_record,
                                       selected_packages,
                                       pkg,
                                       multi_dep,
                                       dependency_progress,
                                       dependency_task)

        dependency_progress.update(dependency_task, total=len(selected_packages), completed=len(selected_packages))

        live.console.print("Total Dependencies Selected are :", len(selected_packages))
        live.console.print("Total Source Packages Selected are :", len(source_packages))
        not_parsed = [obj.name for obj in selected_packages.values() if obj.version == '-1']

        live.console.print("Dependencies Not Parsed: ", len(not_parsed))
        for package_name in not_parsed:
            print(f"\t Not parsed: {package_name}")

        try:
            with open(os.path.join(dir_log, 'selected_packages.list'), 'w') as f:
                for pkg in selected_packages:
                    f.write(str(selected_packages[pkg]) + '\n')
        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)

        # -------------------------------------------------------------------------------------------------------------
        # Step - III Check Alternate dependency
        overall_progress.update(overall_task, description=task_description[2])
        overall_progress.advance(overall_task)
        source_task = source_progress.add_task("Alt Dependency Check".ljust(20, ' '))

        for section in multi_dep:
            found = False
            for pkgs in section:
                arr = apt_pkg.parse_depends(pkgs)
                pkg_name = arr[0][0][0]
                if pkg_name in selected_packages:
                    pkg_version = arr[0][0][1]
                    pkg_constraint = arr[0][0][2]
                    if apt_pkg.check_dep(selected_packages[pkg_name].version, pkg_constraint, pkg_version):
                        found = True
                    else:
                        live.console.print(f"Alt Dependency Check - Version constraint failed for {pkg_name}")
            if not found:
                live.console.print(f"dependency unresolved between {section}")
        live.console.print("Multi Dep Check... Done")

        for package in selected_packages:
            if not selected_packages[package].constraints_satisfied:
                print(f"Version Constraint failed for {package}:{selected_packages[package].name}")

        # -------------------------------------------------------------------------------------------------------------
        # Step - IV Parse Source Packages
        overall_progress.update(overall_task, description=task_description[3])
        overall_progress.advance(overall_task)

        # Check for discrepancy between source version and package version
        live.console.print("Checking for discrepancy between source & "
                           "package version. If any, source version will be used...")

        for package_name in selected_packages:
            source_version = selected_packages[package_name].source[1]
            # Ignore where Source version is not given, it is assumed same as package version
            if source_version == '':
                continue
            package_version = selected_packages[package_name].version
            if not source_version == package_version:
                live.console.print(f"\tDiscrepancy {package_name} {package_version} -> {source_version}")

        # Add package to source list
        for package_name in selected_packages:
            package_source = selected_packages[package_name].source[0]
            package_version = selected_packages[package_name].source[1]
            if package_version == '':
                package_version = selected_packages[package_name].version
            if package_source not in source_packages:
                source_packages[package_source] = Source(package_source, package_version)
            else:
                if not source_packages[package_source].version == package_version:
                    live.console.print(
                        f"Multiple version of same source package being asked for {package_source} -> "
                        f"{source_packages[package_source].version} : {package_version}")

        live.console.print("Source requested for : ", len(source_packages), " packages")

        download_size = parse_sources(source_records,
                                      source_packages,
                                      file_list,
                                      builddep,
                                      source_progress,
                                      source_task)

        missing_source = [_pkg for _pkg in source_packages if not source_packages[_pkg].found]

        if not len(missing_source) == 0:
            for pkg in missing_source:
                live.console.print(f"Source not found for : {source_packages[pkg].name} {source_packages[pkg].version} "
                                   f"Alternates: {source_packages[pkg].alternates}")
                if live.console.input("Select from Alternates? if N, source package will be ignored(y/n") == 'y':
                    new_version = ''
                    while new_version not in source_packages[pkg].alternates:
                        live.console.input(f"Enter Alt Version from {source_packages[pkg].alternates}:")
                    source_packages[pkg].reset_version(new_version)

            # rerun parse_source only for the missing source
            download_size += parse_sources(source_records,
                                           missing_source,
                                           file_list,
                                           builddep,
                                           source_progress,
                                           source_task)

        try:
            with open(os.path.join(dir_log, 'source_packages.list'), 'w') as f:
                for pkg in source_packages:
                    f.write(str(source_packages[pkg]) + '\n')
        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)

        try:
            with open(os.path.join(dir_log, 'source_file.list'), 'w') as f:
                for file in file_list:
                    f.write(file + '\n')
        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)

        # -------------------------------------------------------------------------------------------------------------
        # Step - V Source Build Dependency Check
        overall_progress.update(overall_task, description=task_description[4])
        overall_progress.advance(overall_task)

        # TODO: use dpkg-checkbuilddeps -d build-depends-string -c build-conflicts-string
        result = subprocess.run(['dpkg', '--list'], stdout=subprocess.PIPE).stdout.decode('utf-8').split('\n')
        for line in result:
            match = re.match(r'^ii\s+([^\s:]+)', line)
            if match:
                installed_packages.append(match.group(1))

        required_builddep = [item for item in builddep if item not in installed_packages]
        live.console.print("Build Dependency required ", len(required_builddep), '/', len(builddep))
        if len(required_builddep):
            live.console.print("[green]WARNING: There are pending Build Dependencies, Manual check is required")
            ans = live.console.input("Proceed: (y/n)")
            if not ans == 'y':
                exit(0)

        try:
            with open(os.path.join(dir_log, 'build_dependency.list'), 'w') as f:
                f.write("Build Dependencies:\n")
                for pkg in builddep:
                    f.write(pkg + ' ')
                f.write("\nDependencies not installed:\n")
                for pkg in required_builddep:
                    f.write(pkg + ' ')
        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)

        # -------------------------------------------------------------------------------------------------------------
        # Step - VI Download Source files
        overall_progress.update(overall_task, description=task_description[5])
        overall_progress.advance(overall_task)

        live.console.print("Total File Selected are :", len(file_list))
        live.console.print("Total Download is about ", round(download_size / (1024 * 1024)), "MB")
        live.console.print("Starting Downloads...")

        download_source(file_list,
                        dir_download,
                        download_size,
                        baseurl,
                        baseid,
                        total_download_progress,
                        download_progress)
        # -------------------------------------------------------------------------------------------------------------
        # Step - VII Expanding the Source Packages
        overall_progress.update(overall_task, description=task_description[6])
        overall_progress.advance(overall_task)

        folder_list = []
        try:
            with open(os.path.join(dir_log, 'dpkg-source.log'), "w") as logfile:
                dsc_files = [file[0] for file in file_list.items() if os.path.splitext(file[0])[1] == '.dsc']
                debsource_task = debsource_progress.add_task("Expanding Sources".ljust(20, ' '), total=len(dsc_files))
                _errors = 0
                for file in dsc_files:
                    folder_name = os.path.join(dir_source, os.path.splitext(file)[0])
                    folder_list.append(folder_name)
                    dsc_file = os.path.join(dir_download, file)
                    process = subprocess.Popen(
                        ["dpkg-source", "-x", dsc_file, folder_name], stdout=logfile, stderr=logfile)
                    if process.wait():
                        _errors += 1

                    debsource_progress.advance(debsource_task)
                if _errors:
                    live.console.print(f"dpkg-source failed for {_errors} instances, please check dpkg-source.log")

            with open(os.path.join(dir_log, 'source_folder.list'), 'w') as f:
                for folder in folder_list:
                    f.write(folder + '\n')

        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)
        # -------------------------------------------------------------------------------------------------------------
        # Step - VIII Starting Build
        overall_progress.update(overall_task, description=task_description[7])
        overall_progress.advance(overall_task)
        build_task = build_progress.add_task('', total=len(folder_list))

        live.console.print("Starting Package Build with --no-clean option...")

        _errors = 0
        try:
            with open(os.path.join(dir_log, 'dpkg-build.log'), "w") as dpkg_build_log:
                for pkg in folder_list:
                    build_progress.update(build_task, description=os.path.basename(pkg))
                    build_progress.advance(build_task)

                    log_filename = os.path.join(dir_log, "build", os.path.basename(pkg) + '.log')
                    with open(log_filename, "w") as logfile:
                        process = subprocess.Popen(["dpkg-checkbuilddeps"], cwd=pkg, stdout=logfile, stderr=logfile)
                        if not process.wait():
                            process = subprocess.Popen(
                                ["dpkg-buildpackage", "-b", "-uc", "-us", "-nc", "-a", "amd64", "-J"],
                                cwd=pkg, stdout=logfile, stderr=logfile)
                            if not process.wait():
                                dpkg_build_log.write(f"PASS: {os.path.basename(pkg)}\n")
                                continue

                        dpkg_build_log.write(f"FAIL: {os.path.basename(pkg)}\n")
                        live.console.print(f"Build failed for {os.path.basename(pkg)}")
                        _errors += 1

                        dpkg_build_log.flush()

            if _errors:
                live.console.print(f"dpkg-buildpackage failed for {_errors} instances, "
                                   f"please check dpkg-buildpackage.log")

        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)

        # Mark everything as completed
        overall_progress.update(overall_task, description="Completed")


# Main function
if __name__ == '__main__':
    print(asciiart_logo)
    main()
