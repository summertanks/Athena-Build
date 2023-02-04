import apt_pkg
from rich.console import Console

import utils
from utils import search


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
        self._depends: [] = []
        self._altdepends: [] = []
        self._breaks: [] = []
        self._conflicts: [] = []

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
        breaks_list = apt_pkg.parse_depends(breaks_string)
        self._breaks.extend(breaks_list)

    @property
    def conflicts(self):
        return self._conflicts

    def add_conflicts(self, conflicts_string: str):
        conflicts_list = apt_pkg.parse_depends(conflicts_string)
        self._conflicts.extend(conflicts_list)

    @property
    def depends(self):
        return self._depends

    def add_depends(self, depends_string: str):
        depends_list = apt_pkg.parse_depends(depends_string)
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
            package = Package(package_name)
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
                selected_packages[dep_package_name] = Package(dep_package_name)
                parse_dependencies(package_record, selected_packages, dep_package_name, con, status)
            selected_packages[dep_package_name].add_version_constraint(_pkg[1], _pkg[2])
        break

    # Went through the complete control file, couldn't find required_package
    if required_package not in selected_packages:
        # Added it to track
        selected_packages[required_package] = Package(required_package)


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
