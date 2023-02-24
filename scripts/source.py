import os
import re

import apt_pkg

import utils
import deb822


class Sources:
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
        self._conflicts: [] = []
        self._files: {} = {}
        self._alternate: [] = []
        self._build_depends: [] = []
        self._altdepends: [] = []
        self.found = False

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

    @property
    def conflicts(self):
        return self._conflicts

    def add_conflicts(self, conflict_string):
        conflict_list = apt_pkg.parse_src_depends(conflict_string, architecture='amd64')
        for conflict in conflict_list:
            if len(conflict) > 0:
                self._conflicts.append(conflict)

    @property
    def files(self):
        return self._files

    def add_files(self, file_list, pkg_directory: str) -> tuple[int, int]:
        file_count = 0
        file_size = 0
        for file in file_list:
            if not file[2] in file_list:
                self._files[file[2]] = {'path': os.path.join(pkg_directory, file[2]), 'size': file[1], 'md5': file[0]}
                file_count += 1
                file_size += int(file[1])

        return file_count, file_size

    @property
    def build_depends(self):
        return self._build_depends

    def add_build_depends(self, depends_string: str):
        depends_list = apt_pkg.parse_src_depends(depends_string, architecture='amd64')
        parsed_depends = [sublist[0] for sublist in depends_list if len(sublist) == 1]
        for _pkg in parsed_depends:
            # remove duplicates
            if _pkg not in self._build_depends:
                self._build_depends.append(_pkg)

        alt_depends = [sublist for sublist in depends_list if len(sublist) > 1]
        for _pkg in alt_depends:
            # remove duplicates
            if _pkg not in self._altdepends:
                self._altdepends.append(_pkg)

    @property
    def altdepends(self) -> []:
        return self._altdepends


def parse_sources(source_records,
                  source_packages,
                  con,
                  logger) -> tuple[int, int]:
    # Iterate over Packages for which Source package is required
    total = len(source_packages)
    completed = 0
    total_files = 0
    total_size = 0
    with con.status('') as status:
        for required_package in source_packages:
            # Search within the Source List file
            for pkg in source_records:
                package_name = utils.search(r'Package: ([^\n]+)', pkg)
                package_version = utils.search(r'Version: ([^\n]+)', pkg)

                # TODO: get Build-breaks

                # On Match
                if package_name == required_package:
                    if apt_pkg.check_dep(source_packages[required_package].version, '=', package_version):
                        if not source_packages[required_package].found:
                            completed += 1
                            status.update(f'Parsing Source Packages: {completed}/{total}')

                            # Get all files
                            package_directory = utils.search(r'Directory:\s*(.+)', pkg)

                            # TODO: Currently using md5, should enable SHA256 also
                            files = re.findall(r'\s+([a-fA-F\d]{32})\s+(\d+)\s+(\S+)', pkg)
                            count, size = source_packages[required_package].add_files(files, package_directory)
                            total_files += count
                            total_size += size

                            # set as package found
                            source_packages[required_package].found = True

                            # Parse Build Depends
                            build_depends = utils.search(r'Build-Depends: ([^\n]+)', pkg)
                            build_depends_indep = utils.search(r'Build-Depends-Indep: ([^\n]+)', pkg)
                            build_depends_arch = utils.search(r'Build-Depends-Arch: ([^\n]+)', pkg)

                            depends_string = ''
                            for dep_str in [build_depends, build_depends_indep, build_depends_arch]:
                                if not dep_str == '':
                                    depends_string += dep_str + ', '

                            if not depends_string == '':
                                source_packages[required_package].add_build_depends(depends_string)

                            build_conflicts = utils.search(r'Build-Conflicts: ([^\n]+)', pkg)
                            source_packages[required_package].add_conflicts(build_conflicts)

                            break

                    # Add in alternates
                    source_packages[required_package].add_alternate(package_version)
    return total_files, total_size


class Source(deb822.DEB822file):

    def __init__(self, section: str, arch: str):

        self.package = ''
        self.version = ''

        self._build_depends = []
        self._build_conflicts = []

        super().__init__(section)

        # Setting Values post calling super()
        if arch not in ['amd64']:
            raise ValueError(f"Current Architecture '{arch}' is not supported")
        self.arch: str = arch

        assert 'Package' in self, "Malformed Package, No Package Name"
        assert 'Version' in self, "Malformed Package, No Version Given"
        assert 'Files' in self, "Malformed Package, No Version Given"
        assert 'Directory' in self, "Malformed Package, No Version Given"

        assert not self['Package'] == '', "Malformed Package, No Package Name"
        assert not self['Version'] == '', "Malformed Package, No Version Given"
        assert not self['Files'] == '', "Malformed Package, No Files Given"
        assert not self['Directory'] == '', "Malformed Package, No Directory Given"

        self.package = self['Package']
        self.version = self['Version']
        self.directory = self['Directory']
        self.pkgs: [] = []
        self.files: {} = {}

        _depends_list = []
        _dep_string = ['Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch']
        for _dep in _dep_string:
            self._build_depends += apt_pkg.parse_src_depends(self[_dep], strip_multi_arch=True, architecture=self.arch)

        _files_list = self['Files'].split('\n')
        for _file in _files_list:
            _file = _file.split()
            if len(_file) == 3:
                self.files[_file[2]] = {'path': os.path.join(self.directory, _file[2]),
                                        'size': _file[1], 'md5': _file[0]}

        _pkg_list = self['Package-List'].split('\n')
        for _pkg in _pkg_list:
            _pkg = _pkg.split()
            if len(_pkg) == 0:
                continue
            elif len(_pkg) < 5:
                _arch = self.arch
            else:
                _arch = _pkg[4].split('=')[1]
                if self.arch in _arch or 'any' in _arch:
                    _arch = self.arch
                elif 'all' in _arch:
                    _arch = 'all'
                else:
                    continue
            self.pkgs.append(_pkg[0] + '_' + self.version + '_' + _arch + '.' + _pkg[1])

    @property
    def download_size(self) -> int:
        _download_size = 0
        for _file in self.files:
            _download_size += int(self.files[_file]['size'])
        return _download_size

    @property
    def build_depends(self) -> str:
        _dep_str = ''
        for _dep in self._build_depends:
            # by default select first package even for multi/alt dependencies
            _dep_str += _dep[0][0] + ' '

        return _dep_str
