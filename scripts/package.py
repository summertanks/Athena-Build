import apt_pkg


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
