# internal modules
import deb822

import re
import apt_pkg

Print = print


class Package(deb822.DEB822file):
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
    def __init__(self, section: str, arch: str):

        self.__version_constraints: {} = {}

        self.source: str = ''
        self.source_version: str = ''

        self.package = ''
        self.version = ''

        self.depends = []
        self.alt_depends = []
        self.conflicts = []
        self.breaks = []
        self.provides = []
        self.recommends = []
        self.alt_recommends = []
        self.installed = False
        self.configured = False

        self.depends_on = []
        self.depended_by = []

        # Not necessarily aligned to 'Priority' field, default set to 'Priority' field, may change later
        # this will be set to the highest priority of those packages that depends on them.
        # e.g. if 'required' package has a dependency, they will be 'required' too
        self.priority = ''

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
        self.priority = self['Priority']

        # Setting default as Source name and version is same as package
        self.source = self.package
        self.source_version = self.version

        # Get source data
        if 'Source' in self:
            if not self['Source'] == '':
                _source = self['Source']
                # version shown is without constraints, cant use apt_pkg - parse_depends(...) or parse_sec_depends(...)
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

        if 'Provides' in self:
            self.provides = apt_pkg.parse_depends(self['Provides'], strip_multi_arch=True, architecture=self.arch)

        if 'Recommends' in self:
            _recommends = apt_pkg.parse_depends(self['Recommends'], strip_multi_arch=True, architecture=self.arch)
            self.recommends = [_pkg for _pkg in _recommends if len(_pkg) == 1]
            self.alt_recommends = [_pkg for _pkg in _recommends if len(_pkg) > 1]

    def get_provides(self) -> []:
        if len(self.provides) == 0:
            return []

        __provides = []
        for __pkg in self.provides:
            __provides.append(__pkg[0][0])
        # __provides = [__pkg[0] for __pkg in self.provides[0]]
        return __provides

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
        # TODO: More fine grained check, e.g more constraining one selected, eg if both contain '=' => select '='; ...
        if version in self.__version_constraints and not self.__version_constraints[version] == constraint:
            Print(f"WARNING: For {self.package} version constraint for {version} already set to "
                  f"{self.__version_constraints[version]}, being reset to {constraint}")
        self.__version_constraints[version] = constraint

    def does_provide(self, pkg_name: str) -> bool:
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
