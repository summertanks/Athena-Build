import os
import re

import apt_pkg

import utils


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
                package_name = utils.search(r'Package: ([^\n]+)', package)
                package_version = utils.search(r'Version: ([^\n]+)', package)

                # TODO: get Build-breaks

                # On Match
                if package_name == required_package:
                    if apt_pkg.check_dep(source_packages[required_package].version, '=', package_version):
                        if not source_packages[required_package].found:
                            completed += 1
                            status.update(f'Parsing Source Packages: {completed}/{total}')

                            # Get all files
                            package_directory = utils.search(r'Directory:\s*(.+)', package)

                            # TODO: Currently using md5, should enable SHA256 also
                            files = re.findall(r'\s+([a-fA-F\d]{32})\s+(\d+)\s+(\S+)', package)
                            for file in files:
                                file_list[file[2]] = {
                                    'path': os.path.join(package_directory, file[2]), 'size': file[1], 'md5': file[0]}
                                download_size += int(file[1])

                            # set as package found
                            source_packages[required_package].found = True

                            # Parse Build Depends
                            build_depends = utils.search(r'Build-Depends: ([^\n]+)', package)
                            build_depends_indep = utils.search(r'Build-Depends-Indep: ([^\n]+)', package)
                            build_depends_arch = utils.search(r'Build-Depends-Arch: ([^\n]+)', package)

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


