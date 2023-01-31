# Internal
import os
# External
import re

import apt_pkg
from rich.console import Console

import utils
from package import Package
from utils import search


# Iterative Function
def parse_dependencies(
        package_record: list[str],
        selected_packages: {},
        required_package: str,
        multi_dep: [],
        con: Console,
        status: Console.status):
    """Parse Dependencies for required_packages based on package_record in recursive manner
    populates the selected_packages[] from the list and cases of Alt dependencies

            Parameters:
                package_record: Taken from the Package file
                selected_packages: populates based on dependencies recursively
                required_package: the package to find dependencies for
                multi_dep: populates with packages which have alt dependencies'
                con: Console
                status: for status update

            Returns:
                None
        """
    # Package: Record is typically of the format, other records not shown
    # Package: Only one package name, could contain numbers, hyphen, underscore, dot, etc.
    # Source:  one source package, optional - version in brackets separated by space
    # Version: single version string, may contain alphanumeric and ': - ~ . +'
    # Provides: may provide one or more packages,  list is comma separated, may have versions in () preceded by '='
    # Replaces: one or more packages or even self, may include version in (), versions have prefix << >> <= >= =
    # Breaks: one or more packages, may include version in (), versions have prefix << >> <= >= =
    # Depends: one or more packages, may include version in (), versions have prefix << >> <= >= =
    #          may have arch specified as name:arch e.g. gcc:amd64, python3:any
    #          dependencies which can be satisfied by multiple packages separated by |

    for _package in package_record:
        # Get Package Name
        package_name = utils.search(r'Package: ([^\n]+)', _package)

        # Dependencies are Satisfied on Provides also
        package_provides = utils.search(r'Provides: ([^\n]+)', _package)
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
        # Problem remains that for provides - more than one package may satisfy it, cant go with first come first serve
        # TODO: Parse complete set and give user option to select which package option they want selected.
        for _pkg in provides_list:
            if not _pkg == '':
                selected_packages[_pkg] = package

        # Get Package Version
        package_version = utils.search(r'Version: ([^\n]+)', _package)
        if package_version == '':
            raise ValueError(f"There doesnt seem to be a Version \n {_package}")

        # Get source if available, else assume the source package is same as package name
        # TODO: Check if this assumption is true
        package_source = utils.search(r'Source: ([^\n]+)', _package)
        if package_source == '':
            package_source = package_name

        # Get dependency from both Depends: & Pre-Depends:
        depends_group = utils.search(r'\nDepends: ([^\n]+)', _package)
        pre_depends_group = utils.search(r'\nPre-Depends: ([^\n]+)', _package)

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
        status.update(f"Selected {completed} Packages")

        # skip the ones which have more than one packages satisfying dependency
        parsed_depends = [sublist[0] for sublist in package.depends if len(sublist) == 1]
        for _pkg in parsed_depends:
            # _pkg is usually in [name, version, constraint] tuple format
            dep_package_name = _pkg[0]

            # Check if not already parsed
            if selected_packages.get(dep_package_name) is None:
                selected_packages[dep_package_name] = Package(dep_package_name)
                parse_dependencies(package_record, selected_packages, dep_package_name, multi_dep, con, status)
            selected_packages[dep_package_name].add_version_constraint(_pkg[1], _pkg[2])
        break


def parse_sources(source_records,
                  source_packages,
                  file_list,
                  builddep,
                  con,
                  logger):
    download_size = 0

    # Iterate over Packages for which Source package is required
    total = len(source_packages)
    completed = 0
    with con.status('') as status:
        for required_package in source_packages:
            # Search within the Source List file
            for package in source_records:
                package_name = search(r'Package: ([^\n]+)', package)
                package_version = search(r'Version: ([^\n]+)', package)

                # On Match
                if package_name == required_package:
                    if apt_pkg.check_dep(source_packages[required_package].version, '=', package_version):
                        if not source_packages[required_package].found:
                            completed += 1
                            status.update(f'Parsing Source Packages: {completed}/{total}')

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
