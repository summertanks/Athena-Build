# (C) Athena Linux Project
import bz2
import gzip
import re
import argparse
import json

from collections import Counter

from requests import Timeout, TooManyRedirects, HTTPError, RequestException
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import Task, Progress, SpinnerColumn, TimeElapsedColumn, TransferSpeedColumn, TextColumn, BarColumn, \
    TaskProgressColumn, MofNCompleteColumn, DownloadColumn

import requests
import os
import hashlib
import subprocess
import configparser

consolidated_source = 0


class BaseDistribution:
    def __init__(self, url: str, id: str, codename: str, version: str, arch: str):
        self.url = url
        self.id = id
        self.codename = codename
        self.version = version
        self.arch = arch


def download_file(url: str, filename: str, progressbar: Progress, task) -> None:
    """Download file and updates progressbar in incremental manner.
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
    """Download file and updates progressbar in incremental manner.
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
    base_url = 'http://' + base.url + '/' + base.id + '/dists/' + base.codename
    basefilename = base.url + '_' + base.id + '_dists_' + base.codename

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
                #  35eb7de95c102ffbea4818ea91e470962ddafc97ae539384d7f95d2836d7aa2e 45534962 main/binary-amd64/Packages
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
    f_task = download_progress.add_task("Downloaded".ljust(20, ' '), total=download_size)

    while not download_progress.finished:
        for file_name, data in file_list.items():
            url = base_url + data['path']
            size = data['size']
            md5 = data['md5']

            download_path = download_dir + '/' + file_name

            if os.path.isfile(download_path):
                # Open the file and calculate the MD5 hash
                with open(download_path, 'rb') as f:
                    fdata = f.read()
                    md5_check = hashlib.md5(fdata).hexdigest()
            else:
                md5_check = ''

            if md5 != md5_check:
                response = requests.head(url)
                response = requests.get(url, stream=True)
                if response.status_code == 200:
                    with open(download_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=1024):
                            if chunk:
                                f.write(chunk)
                                download_progress.update(f_task, advance=len(chunk))
                else:
                    download_progress.log(f"Error Downloading {url}")
            else:
                download_progress.update(f_task, advance=int(size))
            total_download_progress.update(t_task, advance=1.0)


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
            _package_name = re.search(r'Package: ([^\n]+)', package)
            if _package_name is None:
                continue
            package_name = _package_name.group(1)

            # On Match
            if package_name == required_package:
                source_progress.advance(source_task)
                # Get all files
                package_version = re.search(r'Version: ([^\n]+)', package).group(1)
                package_directory = re.search(r'Directory:\s*(.+)', package).group(1)
                files = re.findall(r'\s+([a-fA-F\d]{32})\s+(\d+)\s+(\S+)', package)
                for file in files:
                    file_list[file[2]] = {'path': package_directory + '/' + file[2], 'size': file[1], 'md5': file[0]}
                    download_size += int(file[1])
                # set as package found
                source_packages[package_name] = package_version

                # Parse Build Depends
                _build_depends = re.search(r'Build-Depends: ([^\n]+)', package)
                if _build_depends is None:
                    continue
                build_depends = re.split(', ', _build_depends.group(1))
                # Data can be of form libselinux-dev (>= 2.31) [linux-any] <!stage2>
                # where other than package name, everything is optional
                # We have to Initially check for package and if it matches our arch
                # r"([^ ]+)( \([^[:digit:]]*([^)]+)\))?( \[(.+)\])?( <(.+)>)?"
                for dep in build_depends:
                    arr = re.search(r'([^ ]+)( \([^[:digit:]]*([^)]+)\))?( \[(.+)\])?(.*)', dep)
                    if arr is not None:
                        # we dont know which combination is valid so go iteratively
                        builddep_package_name = arr.group(1)
                        builddep_version = arr.group(3)
                        builddep_arch = arr.group(5)
                        if builddep_arch is not None:
                            if builddep_version != 'amd64' or builddep_arch != 'linux-any':
                                continue
                        if builddep_package_name not in builddep:
                            builddep.append(builddep_package_name)
                break

    return download_size


# Iterative Function
def parse_dependencies(
        package_record,
        selected_packages,
        required_package,
        source_packages,
        multi_dep,
        dependency_progress,
        dependency_task):
    global consolidated_source

    for package in package_record:
        # Get Package Name
        _package_name = re.search(r'Package: ([^\n]+)', package)
        if _package_name is None:
            continue
        package_name = _package_name.group(1)

        # Dependencies are Satisfied on Provides also
        _package_provides = re.search(r'Provides: ([^\s]+)', package)
        if _package_provides is None:
            package_provides = ""
        else:
            package_provides = _package_provides.group(1)

        # Check id Dependency is satisfied either through 'Package' or 'Provides'
        if required_package != package_name:
            if required_package != package_provides:
                continue

        # Get Package Version
        package_version = re.search(r'Version: ([^\n]+)', package).group(1)

        # If Not already parsed, un-parsed packages are set as -1
        if selected_packages.get(required_package) == -1:
            # Mark as Parsed by setting version
            selected_packages[required_package] = package_version

            # setting the same for package provides is not same as package_name
            # Saves the situation where there is a separate iteration for 'provides'
            if package_provides != "" and package_name != package_provides:
                selected_packages[package_provides] = package_version

            # Update Progress bar
            completed = Counter(selected_packages.values())[-1]
            dependency_progress.update(dependency_task, total=len(selected_packages), completed=completed)

            # Get Source
            # not all packages have sources ???
            _package_source = re.search(r'Source: ([^\s]+)', package)
            if _package_source is None:
                package_source = package_name
            else:
                package_source = _package_source.group(1)

            if source_packages.get(package_source):
                consolidated_source += 1
            else:
                source_packages[package_source] = -1

            # Get dependency from both Depends: & Pre-Depends:
            depends_group = re.search(r'\nDepends: ([^\n]+)', package)
            pre_depends_group = re.search(r'\nPre-Depends: ([^\n]+)', package)

            depends = []
            if depends_group is not None:
                depends.extend(re.split(", ", depends_group.group(1)))
            if pre_depends_group is not None:
                depends.extend(re.split(", ", pre_depends_group.group(1)))

            # If Dependencies exist
            if len(depends) != 0:
                for dep in depends:
                    # check if the package defined as ':any', assume that as no version given
                    dep = re.sub(':any', '', dep)

                    # Check for dependencies that can be satisfied by multiple packages
                    multi = re.search(r'\|', dep)
                    if multi is not None:
                        multi_dep.append(re.split(' \| ', dep))
                        continue

                    arr = re.search(r'([^ ]+)( \([^[:digit:]]*([^)]+)\))?', dep)
                    dep_package_name = arr.group(1)

                    dep_package_version = 0
                    if arr.group(2) is not None:
                        dep_package_version = arr.group(3)
                    if dep_package_name not in selected_packages:
                        selected_packages[dep_package_name] = -1
                        parse_dependencies(
                            package_record,
                            selected_packages,
                            dep_package_name,
                            source_packages,
                            multi_dep,
                            dependency_progress,
                            dependency_task)
        break


def main():
    selected_packages = {}
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
            required_package = contents.split('\n')
    except (FileNotFoundError, PermissionError) as e:
        print(f"Error: {e}")
        exit(1)

    # Setting up Progress Meter
    task_description = ["Build Cache        ",
                        "Parse Dependencies ",
                        "Multidep Check     ",
                        "Identifying Source ",
                        "Downloading Source ",
                        "Expanding Sources  ",
                        "Done"]

    overall_progress = Progress(TextColumn("Step {task.completed} of {task.total} - {task.description}"),
                                TaskProgressColumn())
    cache_progress = Progress(TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn())
    dependency_progress = Progress(TextColumn("{task.description} {task.total}"), TimeElapsedColumn(), SpinnerColumn())
    source_progress = Progress(TextColumn("{task.description}"), BarColumn(), TaskProgressColumn())
    total_download_progress = Progress(TextColumn("{task.description}"), BarColumn(), TaskProgressColumn(),
                                       MofNCompleteColumn())
    download_progress = Progress(TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn())
    debsource_progress = Progress(TextColumn("{task.description}"), BarColumn(), TaskProgressColumn())

    progress_group = Group(Panel(Group(
        cache_progress,
        dependency_progress,
        source_progress,
        total_download_progress,
        download_progress,
        debsource_progress), title="Progress", title_align="left"), overall_progress)

    with Live(progress_group, refresh_per_second=1) as live:
        live.console.print("[white]Starting Source Build System for Athena Linux...")
        live.console.print("Building for ...")
        live.console.print(f"\t Arch\t\t\t{arch}")
        live.console.print(f"\t Parent Distribution\t{basecodename} {baseversion}")
        live.console.print(f"\t Build Distribution\t{build_codename} {build_version}")
        overall_task = overall_progress.add_task("All Jobs", total=len(task_description))

        # Step I - Building Cache
        overall_progress.update(overall_task, description=task_description[0])
        cache_files = build_cache(base_distribution, dir_cache, cache_progress)

        package_file = cache_files['Packages']
        source_file = cache_files['Sources']

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

        overall_progress.advance(overall_task)

        # Step II - Parse Dependencies
        overall_progress.update(overall_task, description=task_description[1])
        dependency_task = dependency_progress.add_task(task_description[1])

        for pkg in required_package:
            if pkg and not pkg.startswith('#') and not pkg.isspace():
                if pkg not in selected_packages:
                    selected_packages[pkg] = -1
                    parse_dependencies(package_record,
                                       selected_packages,
                                       pkg,
                                       source_packages,
                                       multi_dep,
                                       dependency_progress,
                                       dependency_task)
                    # required_package[pkg] = selected_packages[pkg]
        dependency_progress.update(dependency_task, total=len(selected_packages), completed=len(selected_packages))

        live.console.print("Total Packages Selected are :", len(selected_packages))
        _pkg = [k for k, v in selected_packages.items() if v == -1]
        for k in _pkg:
            live.console.print("Dependency Not Parsed: ", k)

        # Step - II Check multipackage dependency
        overall_progress.update(overall_task, description=task_description[2], completed=3)
        source_task = source_progress.add_task(task_description[2])

        for section in multi_dep:
            found = False
            for pkgs in section:
                arr = re.search(r'([^ ]+)( \([^[:digit:]]*([^)]+)\))?', pkgs)
                dep_package_name = arr.group(1)
                if dep_package_name in selected_packages:
                    found = True
            if not found:
                live.console.print(f"dependency unresolved between {section}")
        live.console.print("Multi Dep Check... Done")

        # Step - III Source Code
        overall_progress.update(overall_task, description=task_description[3], completed=4)
        live.console.print("Source requested for : ", len(source_packages), " packages")
        live.console.print("Consolidated Source: ", consolidated_source)

        download_size = parse_sources(source_records,
                                      source_packages,
                                      file_list,
                                      builddep,
                                      source_progress,
                                      source_task)

        _pkg = [k for k, v in source_packages.items() if v == -1]
        for k in _pkg:
            live.console.print("Source not found for :", k)

        result = subprocess.run(['dpkg', '--list'], stdout=subprocess.PIPE).stdout.decode('utf-8').split('\n')
        for line in result:
            match = re.match(r'^ii\s+([^\s:]+)', line)
            if match:
                installed_packages.append(match.group(1))
        required_builddep = [item for item in builddep if item not in installed_packages]
        live.console.print("Build Dependency required ", len(required_builddep), '/', len(builddep))
        if len(required_builddep):
            live.console.print("[green]WARNING: There are pending Build Dependencies, Manual check is required")
        try:
            with open(dir_temp + '/dep.list', 'w') as f:
                for dep in required_builddep:
                    f.write(dep + ' ')
        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)

        # Step - IV Download Code
        overall_progress.update(overall_task, description=task_description[4], completed=5)
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

        try:
            with open(dir_temp + '/filelist.txt', 'w') as f:
                f.write(json.dumps(file_list))
        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)

        # Step - V Expanding the Source Packages
        folder_list = []
        overall_progress.update(overall_task, description=task_description[5], completed=6)
        try:
            with open(dir_log + '/logfile.log', "w") as logfile:
                dsc_files = [file[0] for file in file_list.items() if os.path.splitext(file[0])[1] == '.dsc']
                debsource_task = debsource_progress.add_task(task_description[5], total=len(dsc_files))
                for file in dsc_files:
                    folder_name = os.path.join(dir_source, os.path.splitext(file)[0])
                    folder_list.append(folder_name)
                    dsc_file = os.path.join(dir_download, file)
                    process = subprocess.Popen(
                        ["dpkg-source", "-x", dsc_file, folder_name], stdout=logfile, stderr=logfile)
                    process.wait()
                    debsource_progress.advance(debsource_task)

            with open(dir_temp + '/source_folder.list', 'w') as f:
                for folder in folder_list:
                    f.write(folder + '\n')

        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: {e}")
            exit(1)

        # Mark everything as completed
        overall_progress.update(overall_task, description=task_description[6], completed=7)


# Main function
if __name__ == '__main__':
    main()
