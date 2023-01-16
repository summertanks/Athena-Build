# (C) Athena Linux Project
import re
import argparse
import json

from collections import Counter
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn, TransferSpeedColumn, TextColumn, BarColumn, \
    TaskProgressColumn, MofNCompleteColumn, DownloadColumn

import requests
import os
import hashlib
import subprocess
import configparser

consolidated_source = 0


def download_source(file_list, download_dir, download_size, total_download_progress, download_progress):
    base_url = "http://deb.debian.org/debian/"
    total_files = len(file_list)

    ttask = total_download_progress.add_task("Total Completed    ", total=total_files)
    ftask = download_progress.add_task("Downloaded         ", total=download_size)

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
                                download_progress.update(ftask, advance=len(chunk))
                else:
                    download_progress.log(f"Error Downloading {url}")
            else:
                download_progress.update(ftask, advance=int(size))
            total_download_progress.update(ttask, advance=1.0)


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

    parser.add_argument('--depends-file', type=str, help='Specify Depends File', required=True)
    parser.add_argument('--pkg-list', type=str, help='Specify Required Pkg File', required=True)
    parser.add_argument('--source-file', type=str, help='Specify Source File', required=True)
    parser.add_argument('--output-file', type=str, help='Output File', required=True)
    parser.add_argument('--download-dir', type=str, help='Dir to download source files', required=True)

    args = parser.parse_args()

    config_parser.read(args.config_file)

    try:
        with open(args.depends_file, 'r') as f:
            contents = f.read()
            package_record = contents.split('\n\n')
    except (FileNotFoundError, PermissionError) as e:
        print(f"Error: {e}")
        exit(1)

    try:
        with open(args.pkg_list, 'r') as f:
            contents = f.read()
            required_package = contents.split('\n')
    except (FileNotFoundError, PermissionError) as e:
        print(f"Error: {e}")
        exit(1)

    try:
        with open(args.source_file, 'r') as f:
            contents = f.read()
            source_records = contents.split('\n\n')
    except (FileNotFoundError, PermissionError) as e:
        print(f"Error: {e}")
        exit(1)

    # Setting up Progress Meter
    task_description = ["Parse Dependencies ",
                        "Multidep Check     ",
                        "Identifying Source ",
                        "Downloading Source ",
                        "Expanding Sources  ",
                        "Done"]

    overall_progress = Progress(TextColumn("Step {task.completed} of {task.total} - {task.description}"),
                                TaskProgressColumn())
    dependency_progress = Progress(TextColumn("{task.description} {task.total}"), TimeElapsedColumn(), SpinnerColumn())
    source_progress = Progress(TextColumn("{task.description}"), BarColumn(), TaskProgressColumn())
    total_download_progress = Progress(TextColumn("{task.description}"), BarColumn(), TaskProgressColumn(),
                                       MofNCompleteColumn())
    download_progress = Progress(TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn())
    debsource_progress = Progress(TextColumn("{task.description}"), BarColumn(), TaskProgressColumn())

    overall_task = overall_progress.add_task("All Jobs", total=len(task_description))

    progress_group = Group(Panel(Group(
        dependency_progress,
        source_progress,
        total_download_progress,
        download_progress,
        debsource_progress), title="Progress", title_align="left"), overall_progress)

    with Live(progress_group, refresh_per_second=1) as live:
        while not overall_progress.finished:
            live.console.print("[white]Starting Source Build System for Athena Linux...")
            # Step I - Parse Dependencies
            overall_progress.update(overall_task, description=task_description[0], completed=1)
            dependency_task = dependency_progress.add_task(task_description[0])

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
            overall_progress.update(overall_task, description=task_description[1], completed=2)
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
            overall_progress.update(overall_task, description=task_description[2], completed=3)
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
                match = re.match(r'^ii\s+(\S+)', line)
                if match:
                    installed_packages.append(match.group(1))
            required_builddep = [item for item in builddep if item not in installed_packages]
            live.console.print("Build Dependency required ", len(required_builddep), '/', len(builddep))

            try:
                with open('dep.list', 'w') as f:
                    for dep in required_builddep:
                        f.write(dep + ' ')
            except (FileNotFoundError, PermissionError) as e:
                print(f"Error: {e}")
                exit(1)

            # Step - IV Download Code
            overall_progress.update(overall_task, description=task_description[3], completed=4)
            live.console.print("Total File Selected are :", len(file_list))
            live.console.print("Total Download is about ", round(download_size / (1024 * 1024)), "MB")
            live.console.print("Starting Downloads...")

            download_source(file_list, args.download_dir, download_size, total_download_progress, download_progress)

            try:
                with open(args.output_file, 'w') as f:
                    f.write(json.dumps(file_list))
            except (FileNotFoundError, PermissionError) as e:
                print(f"Error: {e}")
                exit(1)

            # Step - V Expanding the Source Packages
            overall_progress.update(overall_task, description=task_description[4], completed=5)
            with open("logfile.log", "w") as logfile:
                dsc_files = [file[0] for file in file_list.items() if os.path.splitext(file[0])[1] == '.dsc']
                debsource_task = debsource_progress.add_task(task_description[4], total=len(dsc_files))
                for file in dsc_files:
                    folder_name = 'source' + '/' + os.path.splitext(file)[0]
                    dsc_file = args.download_dir + '/' + file
                    process = subprocess.Popen(
                        ["dpkg-source", "-x", dsc_file, folder_name], stdout=logfile, stderr=logfile)
                    process.wait()
                    debsource_progress.advance(debsource_task)

            # Mark everything as completed
            overall_progress.update(overall_task, description=task_description[5], completed=6)


# Main function
if __name__ == '__main__':
    main()
