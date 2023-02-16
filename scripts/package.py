import re

import apt_pkg
from rich.console import Console
from rich.prompt import Prompt

import utils
from utils import search

Print = print


class Packages:
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
        self._depends: [] = []
        self._altdepends: [] = []
        self._breaks: [] = []
        self._conflicts: [] = []

    def __str__(self):
        return str(
            f"{self._name} {self._version} {self.version_constraints} {self._provides} {self._source} {self._depends}")

    def __eq__(self, other):
        if not isinstance(other, Packages):
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

    def reset_source_version(self, version_string: str):
        self._source = (self._source[0], version_string)

    @property
    def provides(self) -> {}:
        return self._provides

    def add_provides(self, provides: [str]):
        for pkg in provides:
            if pkg == '':
                continue
            self._provides.append(pkg)

    @property
    def breaks(self):
        return self._breaks

    def add_breaks(self, breaks_string: str):
        breaks_list = apt_pkg.parse_depends(breaks_string, architecture='amd64')
        self._breaks.extend(breaks_list)

    @property
    def conflicts(self):
        return self._conflicts

    def add_conflicts(self, conflicts_string: str):
        conflicts_list = apt_pkg.parse_depends(conflicts_string, architecture='amd64')
        self._conflicts.extend(conflicts_list)

    @property
    def depends(self):
        return self._depends

    def add_depends(self, depends_string: str):
        depends_list = apt_pkg.parse_depends(depends_string, architecture='amd64')
        parsed_depends = [sublist[0] for sublist in depends_list if len(sublist) == 1]
        for _pkg in parsed_depends:
            # remove duplicates
            if _pkg not in self._depends:
                self._depends.append(_pkg)

        alt_depends = [sublist for sublist in depends_list if len(sublist) > 1]
        for _pkg in alt_depends:
            # remove duplicates
            if _pkg not in self._altdepends:
                self._altdepends.append(_pkg)

    @property
    def altdepends(self) -> []:
        return self._altdepends

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

    # TODO: Confirm that this works
    def check_version_format(self, version) -> bool:
        return apt_pkg.check_dep(version, '=', version)

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


# Iterative Function
def parse_dependencies(
        package_record: list[str],
        selected_packages: {},
        required_package: str,
        con: Console,
        status: Console.status):
    """Parse Dependencies for required_packages based on package_record in recursive manner
    populates the selected_packages[] from the list and cases of Alt dependencies

            Parameters:
                package_record: Taken from the Package file
                selected_packages: populates based on dependencies recursively
                required_package: the package to find dependencies for
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
            package = Packages(package_name)
            selected_packages[package_name] = package
        else:
            package = selected_packages[package_name]

        # Incase version is set, it has already been parsed
        if not package.version == '-1':
            break

        # Problem remains - more than one package may satisfy 'provides', cant go with first come, first served
        # TODO: allow user to select which package to select
        alternates = find_alternate_packages(package_record, required_package)
        if len(alternates) > 1:  # assume single matches as such have no options
            con.print(f"{required_package} has alternate sources: {alternates}, auto selected is {package_name}")

        # Bothersome multi-provides condition: if a package is added, assume all Provides have been satisfied
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

        # Get Breaks and Conflicts
        breaks = utils.search(r'\nBreaks: ([^\n]+)', _package)
        conflicts = utils.search(r'\nConflicts: ([^\n]+)', _package)

        depends = ''
        # Let's stitch them together, none is mandatory
        for dep_str in [depends_group, pre_depends_group]:
            if not dep_str == '':
                depends += dep_str + ', '

        package.version = package_version
        package.source = package_source
        package.add_provides(package_provides)
        package.add_depends(depends)
        package.add_breaks(breaks)
        package.add_conflicts(conflicts)

        # Update Progress bar
        completed = len([obj for obj in selected_packages.values() if not obj.version == '-1'])
        status.update(f"Selected {completed} Packages")

        # Parse dependencies
        for _pkg in package.depends:
            # _pkg is usually in [name, version, constraint] tuple format
            dep_package_name = _pkg[0]

            # Check if not already parsed
            if selected_packages.get(dep_package_name) is None:
                selected_packages[dep_package_name] = Packages(dep_package_name)
                parse_dependencies(package_record, selected_packages, dep_package_name, con, status)
            selected_packages[dep_package_name].add_version_constraint(_pkg[1], _pkg[2])
        break

    # Went through the complete control file, couldn't find required_package
    if required_package not in selected_packages:
        # Added it to track
        selected_packages[required_package] = Packages(required_package)


def find_alternate_packages(package_record: list[str], provides: str) -> {str, str}:
    alternates = {}
    for _package in package_record:
        # Get Package Name
        package_name = utils.search(r'Package: ([^\n]+)', _package)
        package_version = search(r'Version: ([^\n]+)', _package)

        # Dependencies are Satisfied on provides
        package_provides = utils.search(r'Provides: ([^\n]+)', _package)
        provides_list = [pkg[0][0] for pkg in apt_pkg.parse_depends(package_provides)]

        if provides in provides_list:
            alternates[package_name] = package_version

    return alternates


class MutableClass:
    def __init__(self):
        self.__dict = {}
        self.__keys = OrderedSet()

    def __iter__(self):
        for key in self.__keys:
            yield str(key)

    def __len__(self):
        return len(self.__keys)

    def __setitem__(self, key, value):
        self.__keys.add(key)
        self.__dict[key] = value

    def __getitem__(self, key):
        try:
            value = self.__dict[key]
        except KeyError:
            value = ''
        return value

    def __delitem__(self, key):
        self.__keys.remove(key)
        try:
            del self.__dict[key]
        except KeyError:
            pass

    def __contains__(self, key):
        return key in self.__keys


class DEB822file(MutableClass):
    """DEB822 - Superclass to parse Deb822 Control files.
                This is Not a full-fledged RFC822 implementation,
                bare minimum to parse the Release, Package, Source & DSC file"""

    def __init__(self, section: str):

        super().__init__()

        # Save content for reference
        self.__raw = section

        # Parse as DEB822 file
        _lines = section.split('\n')

        current_field = None
        for _line in _lines:

            # Should not happen, sections are supposed to already be split '\n\n' and no line with spaces
            if _line.strip() == '':
                raise ValueError("ERROR: Attempting to create class with malformed section")

            if _line.startswith(' '):
                if current_field is None:
                    raise
                # This line is a continuation of the previous field
                self[current_field] += _line
            else:
                # This line starts a new field
                current_field, value = _line.split(':', 1)
                self[current_field.strip()] = value.strip()


class Package(DEB822file):
    def __init__(self, section: str, arch: str):

        self.__version_constraints: {} = {}

        self.source: str = ''
        self.source_version: str = ''

        self.package = ''
        self.version = ''

        self.depends = self.alt_depends = []
        self.conflicts = []
        self.breaks = []
        self.recommends = self.alt_recommends = []

        super().__init__(section)

        # Setting Values post calling super()
        if arch not in ['amd64']:
            raise ValueError(f"Current Architecture '{arch}' is not supported")
        self.arch: str = arch

        assert 'Package' in self, "Malformed Package, No Package Name"
        assert 'Version' in self, "Malformed Package, No Version Given"
        assert not self['Package'] == '', "Malformed Package, No Package Name"
        assert not self['Version'] == '', "Malformed Package, No Version Given"

        self.package = self['Package']
        self.version = self['Version']

        # Setting default as Source name and version is same as package
        self.source = self.package
        self.source_version = self.version

        # Get source data
        if 'Source' in self:
            if not self['Source'] == '':
                _source = self['Source']
                _source_group = re.search(r'^(\S+)(?:\s+\((\S+)\))?$', _source)
                assert _source_group.group(1) is not None, "Malformed Source Name"
                self.source = _source_group.group(1)
                if _source_group.group(2) is not None:
                    self.source_version = _source_group.group(2)

        _depends_list = []
        if 'Depends' in self:
            _depends_list = apt_pkg.parse_depends(self['Depends'], strip_multi_arch=True, architecture=self.arch)
        if 'Pre-Depends' in self:
            _depends_list += apt_pkg.parse_depends(self['Pre-Depends'], strip_multi_arch=True, architecture=self.arch)

        self.depends = [sublist[0] for sublist in _depends_list if len(sublist) == 1]
        self.alt_depends = [sublist for sublist in _depends_list if len(sublist) > 1]

        if 'Breaks' in self:
            self.breaks = apt_pkg.parse_depends(self['Breaks'], strip_multi_arch=True, architecture=self.arch)

        if 'Conflicts' in self:
            self.conflicts = apt_pkg.parse_depends(self['Conflicts'], strip_multi_arch=True, architecture=self.arch)

        if 'Recommends' in self:
            _recommends = apt_pkg.parse_depends(self['Recommends'], strip_multi_arch=True, architecture=self.arch)
            self.recommends = [_pkg for _pkg in _recommends if len(_pkg) == 1]
            self.alt_recommends = [_pkg for _pkg in _recommends if len(_pkg) > 1]

    @property
    def constraints_satisfied(self) -> bool:
        # needs a version to check against
        assert 'Version' in self, "Malformed Package, No Version Given"

        # check version against the saved constraints
        for _version, _constraint in self.__version_constraints.items():
            if not apt_pkg.check_dep(self.version, _constraint, _version):
                return False
        return True

    def add_version_constraint(self, version, constraint):
        # version can in the form of <constraint> <version number> or just <Version number>
        # <constraints> are in form of =, <<, >>, >=, <=
        # = and !<constraints> will be considered hard assignments
        if version == '':
            return
        if constraint == '':
            constraint = '='

        if constraint not in ['=', '>', '<', '>=', '<=', '>>', '<<']:
            raise ValueError(f"Unspecified Constraint being set {constraint}")

        # Add constraint - Check if different constraint is already set
        if version in self.__version_constraints:
            Print(f"WARNING: For {self.package} version constraint for {version} already set to "
                  f"{self.__version_constraints[version]}, being reset to {constraint}")
        self.__version_constraints[version] = constraint

    def provides(self, pkg_name: str) -> bool:
        """
        Checks if the current package provides the given package name
        Args:
            pkg_name: the package name to check for

        Returns:
            bool:
        """
        assert 'Package' in self, "Malformed Package, No Package Name"

        if 'Provides' not in self:
            return pkg_name == self['Package']

        _provides = apt_pkg.parse_depends(self['Provides'], strip_multi_arch=True, architecture=self.arch)
        for _pkg in _provides:
            if pkg_name == _pkg[0]:
                return True
        return False


class DependencyTree:

    def __init__(self, pkg_filename: str, src_filename: str, select_recommended: bool, arch: str):
        self.__pkg_filename = pkg_filename
        self.__src_filename = src_filename
        self.__recommended = select_recommended

        self.package_record = utils.readfile(self.__pkg_filename).split('\n\n')
        self.source_records = utils.readfile(self.__src_filename).split('\n\n')
        self.selected_pkgs: {} = {}
        self.alternate_pkgs: {} = {}
        self.required_pkgs: [] = []

        self.arch = arch

    def parse_dependency(self, required_pkg: str):

        if required_pkg not in self.selected_pkgs:
            _provide_candidates = []
            _pkg_candidates = []
            _selected_pkg: Package

            # TODO: enable required package to specify version also

            # iterate through the package records
            for _pkg_record in self.package_record:
                if _pkg_record.strip() == '':
                    continue
                _pkg = Package(_pkg_record, self.arch)
                # search for packages
                if required_pkg == _pkg.package:
                    _pkg_candidates.append(_pkg)
                elif _pkg.provides(required_pkg):
                    _provide_candidates.append(_pkg)

            # Case - I  : No match for Package or Provides - Raise Value Error
            if len(_provide_candidates) == 0 and len(_pkg_candidates) == 0:
                raise ValueError(f"Package could not be found: {required_pkg}")

            # Case - II : One or more Package, One or more Provides - Unknown condition, currently raise error
            elif len(_provide_candidates) > 0 and len(_pkg_candidates) > 0:
                raise ValueError(f"Exact package could not selected: {required_pkg}")

            # Case - III: No Package, One Provides - "Selecting <Package> for <Provides> - Proceed with Package
            elif len(_provide_candidates) == 1 and len(_pkg_candidates) == 0:
                Print(f"Note: Selecting {_provide_candidates[0]['Package']} for {required_pkg}")
                _selected_pkg = _provide_candidates[0]

            # Case - IV : No Package, Multiple Provides (different Packages) - Ask User to manually select
            # TODO: Look forward, see if required package list already solves the problem
            elif len(_provide_candidates) > 1 and len(_pkg_candidates) == 0:
                _options = [_pkg['Package'] for _pkg in _provide_candidates]
                _pkg = Prompt.ask(f"Multiple provides for {required_pkg}, select Package", choices=_options)
                _index = _options.index(_pkg)
                _selected_pkg = _provide_candidates[_index]

            # Case - V  : Multiple Package, No Provides - Select based on version, if still more than one, ask user
            elif len(_provide_candidates) == 0 and len(_pkg_candidates) > 1:
                _options = [_pkg['Version'] for _pkg in _pkg_candidates]
                _pkg = Prompt.ask(f"Multiple Package for {required_pkg}, select Version", choices=_options)
                _index = _options.index(_pkg)
                _selected_pkg = _pkg_candidates[_index]

            # Case - VI : One Package, No Provides - Simplest, move ahead parsing the given package
            elif len(_provide_candidates) == 0 and len(_pkg_candidates) == 1:
                _selected_pkg = _pkg_candidates[0]

            else:  # Do not know how we got here
                raise ValueError(f"Unknown Error in Parsing dependencies: {required_pkg}")

            # We have the selected package in _selected_pkg, adding to internal list
            self.selected_pkgs[_selected_pkg['Package']] = _selected_pkg

            # list packages to get dependencies for
            _depends = _selected_pkg.depends

            # check if we should include recommended packages
            if self.__recommended:
                _depends += _selected_pkg.recommends

            # recursively
            for _pkg in _depends:
                self.parse_dependency(_pkg[0])
                self.selected_pkgs[_pkg[0]].add_version_constraint(_pkg[1], _pkg[2])


class OrderedSet(object):
    """OrderedSet - Reused from debian.DEB822
                    Set is faster than list
    """

    def __init__(self, iterable: [str] = None):
        self.__set: set[str] = set()
        self.__order: [str] = []
        if iterable is None:
            iterable = []
        for item in iterable:
            self.add(item)

    def add(self, item):
        # item is assumed hashable, otherwise set() will auto raise error
        if item not in self:
            self.__set.add(item)
            self.__order.append(item)

    def remove(self, item: str):
        # assumed to exist, set.remove will else raise KeyError
        self.__set.remove(item)
        self.__order.remove(item)

    def __iter__(self) -> iter:
        # Return an iterator of items in the order they were added
        return iter(self.__order)

    def __len__(self) -> int:
        return len(self.__order)

    def __contains__(self, item) -> bool:
        # Lookup in a set is O(1) instead of O(n) for a list.
        return item in self.__set

    # ### list-like methods
    append = add

    def extend(self, iterable):
        for item in iterable:
            self.add(item)
