# internal modules
from debian.deb822 import Packages, Sources
from debian.debian_support import Version

import tui

from typing import List, Dict, Any

class VersionConstraint:
    """
    Class to hold version constraints for a package.
    Version constraints are in the form of <constraint> <version number> or just <Version number>
    <constraints> are in form of =, <<, >>, >=, <=
    = and !<constraints> will be considered hard assignments
    """
    _version: Version
    _constraint: str

    def __init__(self, version: Version, constraint: str):
        self._version = version
        
        if constraint not in ['=', '>', '<', '>=', '<=', '>>', '<<']:
            raise ValueError(f"Invalid operator: {constraint}")

    def __repr__(self):
        return f"{self._constraint} {self._version}"
    
    def is_satisfied_by(self, candidate: Version) -> bool:

        if self._constraint == '=':
            return candidate == self._version
        elif self._constraint in ('==',):  # for compatibility
            return candidate == self._version
        elif self._constraint == '>':
            return candidate > self._version
        elif self._constraint == '<':
            return candidate < self._version
        elif self._constraint == '>=':
            return candidate >= self._version
        elif self._constraint == '<=':
            return candidate <= self._version
        elif self._constraint == '>>':
            return candidate > self._version
        elif self._constraint == '<<':
            return candidate < self._version
        else:
            raise ValueError(f"Unknown operator: {self._constraint}")
    
    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, VersionConstraint):
            return NotImplemented
        return self._version == other.version and self._constraint == other.constraint

    def __hash__(self):
        return hash((self._version, self._constraint))
    
    @property
    def version(self) -> Version:
        return self._version
    
    @property
    def constraint(self) -> str:
        return self._constraint
    

class Package(Packages):
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
    
    # source:         str = ''
    # source_version: str = ''

    # attrs = [attr for attr in dir(package) if not callable(getattr(package, attr)) and not attr.startswith("_")]
    # ['decoder', 'encoding', 'gpg_info', 'relations', 'source', 'source_version']
    # so we cannot use these as attributes

    package:        str = ''
    version:        Version

    # 'depends', 'pre-depends', 'recommends', 'suggests', 'breaks', 'conflicts', 'provides', 'replaces', 'enhances', 'built-using']
    depends:        List[List[Dict[str, Any]]] = []
    pre_depends:    List[List[Dict[str, Any]]] = []  # dependencies that must be satisfied before the package can be unpacked
    recommends:     List[List[Dict[str, Any]]] = []
    suggests:       List[List[Dict[str, Any]]] = []
    breaks:         List[List[Dict[str, Any]]] = []
    conflicts:      List[List[Dict[str, Any]]] = []
    provides:       List[List[Dict[str, Any]]] = []
    replaces:       List[List[Dict[str, Any]]] = []
    enhances:       List[List[Dict[str, Any]]] = []
    built_using:    List[List[Dict[str, Any]]] = []

    depends_on:     List[str] = []
    depended_by:    List[str] = []

    # Not necessarily aligned to 'Priority' field, default set to 'Priority' field, may change later
    # this will be set to the highest priority of those packages that depends on them.
    # e.g. if 'required' package has a dependency, they will be 'required' too
    priority:       str  = ''
                                
    arch:           str  = ''   # Architecture of the package, e.g. amd64, arm64, etc.

    installed:  bool = False  # Whether the package is installed or not
    configured: bool = False  # Whether the package is configured or not

    _err_str: str = ""
    _pkg_valid: bool = False  # Whether the package is valid or not

    # List of version constraints for the package
    _constraints: Dict[Version, VersionConstraint]

    def __eq__(self, other: object) -> bool:
        
        if not isinstance(other, Package):
            return NotImplemented
        
        return (
            self.package == other.package and
            self.version == other.version and
            self.arch == other.arch
        )
    
    def __hash__(self) -> int:
        return hash((self.package, self.version, self.arch))

    def __init__(self, section: str):

        super().__init__(section)

        # Setting Values post calling super()
        assert 'Package' in self, "Malformed Package, No Package Name"
        assert 'Version' in self, "Malformed Package, No Version Given"
        assert 'Architecture' in self, "Malformed Package, No Architecture Given"

        assert not self['Package'] == '', "Malformed Package, No Package Name"
        assert not self['Version'] == '', "Malformed Package, No Version Given"
        assert not self['Architecture'] == '', "Malformed Package, No Architecture Given"

        self.package = self['Package']
        self.version = Version(self['Version'])
        self.arch = self['Architecture']

        if 'Priority' in self and self['Priority'].strip() != '':
            self.priority = self['Priority']
        else:
            self.priority = 'optional'

        # UPDATE: source & source_version is now in superclass as properties
        # If the source package and source package version are the same as the binary package, an explicit 
        # "Source" field will not be within the paragraph.
        #       self.source = self.package
        #       self.source_version = self.version
        # _source_group = re.search(r'^(\S+)(?:\s+\((\S+)\))?$', self['Source'].strip())
        # group[1] is the source package name, group[2] is the version if present

        # ['depends', 'pre-depends', 'recommends', 'suggests', 'breaks', 'conflicts', 'provides', 'replaces', 'enhances', 'built-using']
        self.depends = self.relations.get('depends', [])
        self.pre_depends = self.relations.get('pre-depends', [])
        self.recommends = self.relations.get('recommends', [])
        self.suggests = self.relations.get('suggests', [])
        self.breaks = self.relations.get('breaks', [])
        self.conflicts = self.relations.get('conflicts', [])
        self.provides = self.relations.get('provides', [])
        self.replaces = self.relations.get('replaces', [])
        self.enhances = self.relations.get('enhances', [])
        self.built_using = self.relations.get('built-using', [])

        # There are alternatives in above where it can be satisfied by multiple packages
        # e.g. 
        # [{'name': 'python3', 'archqual': 'any', 'version': None, 'arch': None, 'restrictions': None}]
        # [{'name': 'make', 'archqual': None, 'version': None, 'arch': None, 'restrictions': None}]
        # [{'name': 'gcc', 'archqual': None, 'version': ('>=', '4:4.9.1'), 'arch': None, 'restrictions': None}, 
        #       {'name': 'nodejs', 'archqual': None, 'version': None, 'arch': None, 'restrictions': None}]
        # Mandatory dependencies are python3 and make, but gcc is optional and can be satisfied by nodejs
        #
        # _depends_list = apt_pkg.parse_depends(self['Depends'], strip_multi_arch=True, architecture=self.arch)
        # self.depends = [sublist[0] for sublist in _depends_list if len(sublist) == 1]
        # self.alt_depends = [sublist for sublist in _depends_list if len(sublist) > 1]


    def get_provides(self) -> List[str]:
        if len(self.provides) == 0:
            return []

        # Provides should not have alternates, but still, we will flatten it        
        _provides_names: List[str] = []
        for _grp in self.provides:
            for _dep in _grp:
                _provides_names.append(_dep['name'])
    
        return _provides_names
    
    def does_provide(self, pkg_name: str) -> bool:
        """
        Checks if the current package provides the given package name
        Args:
            pkg_name: the package name to check for

        Returns:
            bool:
        """
        assert 'Package' in self, "Malformed Package, No Package Name"
        return (pkg_name in self.get_provides())

    @property
    def constraints_satisfied(self) -> bool:
        # needs a version to check against
        assert 'Version' in self, "Malformed Package, No Version Given"

        _satisfied = True
        for _ver in self._constraints.keys():
            _constraint = self._constraints[_ver]

            if not _constraint.is_satisfied_by(self.version):
                _satisfied = False
        
        return _satisfied


    def add_constraint(self, version: Version, constraint: str) -> bool:
        # version can in the form of <constraint> <version number> or just <Version number>
        # <constraints> are in form of =, <<, >>, >=, <=
        # = and !<constraints> will be considered hard assignments

        # nc = no change
        # xg = replace with newer
        # eq = replace with '='
        # err = error, cannot resolve
        constraint_action = {
            '=':   {'=': 'nc', '>=': 'xg',  '<=': 'xg', '>>': 'err', '<<': 'err' },
            '>=':  {'=': 'nc', '>=': 'nc',  '<=': 'eq', '>>': 'nc',  '<<': 'err' },
            '<=':  {'=': 'nc', '>=': 'eq',  '<=': 'nc', '>>': 'err', '<<': 'nc'  },
            '>>':  {'=': 'err','>=': 'xg',  '<=': 'err','>>': 'nc',  '<<': 'err' },
            '<<':  {'=': 'err','>=': 'err', '<=': 'xg', '>>': 'err', '<<': 'nc'  },
        }

        if constraint == '': constraint = '='

        _constraint = VersionConstraint(version, constraint)

        # If the constraint is not added yet, add it
        if version not in self._constraints:
            self._constraints[version] = _constraint
            return True
        
        _old_constraint = self._constraints[version]

        # Constraint is already there, nothing to add
        if _old_constraint == _constraint:
            return True
        
        action = constraint_action.get(_constraint.constraint, {}).get(_old_constraint.constraint, 'conflict')
        
        if action == 'nc':
            return True
        
        elif action == 'xg':
            self._constraints[version] = _constraint
            return True
        
        elif action == 'eq':
            self._constraints[version] = VersionConstraint(version, '=')
            return True
        
        else:
            tui.console.print(f"WARNING: Cannot resolve conflicting constraints for "
                              f"{self.package} {_constraint} vs {_old_constraint}, ignoring")
            return False

class Source(Sources):

    package:    str = ''
    binary:     str = ''
    version:    Version
    arch:       List[str]

    _build_depends = []
    _build_conflicts = []
    
    _files = []

    def __init__(self, section: str):

        super().__init__(section)

        assert 'Package' in self, "Malformed Package, No Package Name"
        assert 'Version' in self, "Malformed Package, No Version Given"
        assert 'Architecture' in self, "Malformed Package, No Architecture Given"

        assert 'Files' in self, "Malformed Package, No Version Given"
        assert 'Directory' in self, "Malformed Package, No Version Given"

        assert not self['Package'] == '', "Malformed Package, No Package Name"
        assert not self['Version'] == '', "Malformed Package, No Version Given"
        assert not self['Architecture'] == '', "Malformed Package, No Architecture Given"
        assert not self['Files'] == '', "Malformed Package, No Files Given"
        assert not self['Directory'] == '', "Malformed Package, No Directory Given"

        self.package = self['Package']
        self.binary = self['Binary'] if 'Binary' in self else ''
        self.version = self['Version']
        self.arch = self['Architecture']

        self.directory = self['Directory']
        self.pkgs: [] = []
        self.files: {} = {}
        self.skip_test = False
        self.patch_list = []

        # if self.package == 'glibc':
        #    print('.')

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

        # can be derived from Package-List field, but it is tedious - correlation for versions required
        # One source provides multiple packages, package may have different version from the source version
        # Package-List may have additional information e.g. 'udeb' tag which is not there in package
        # Lets only select the package-files that the Package actually needs, the others produced are optional

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
