# internal modules
from debian.deb822 import Packages, Sources
from debian.debian_support import Version

import tui

from typing import List, Dict, Any, Tuple

class VersionConstraint:
    """
    Class to hold version constraints for a package.
    Version constraints are in the form of <constraint> <version number> or just <Version number>
    <constraints> are in form of =, <<, >>, >=, <=
    = and !<constraints> will be considered hard assignments
    """
    

    def __init__(self, version: Version, constraint: str):
        self._version: Version
        self._constraint: str

        self._version = version
        self._constraint = constraint.strip()
        
        if not self._constraint:
            self._constraint = '='
        
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
        
        # Whether the package is valid or not, set to True if all required fields are present
        self._isvalid: bool = False  
        self.package:        str = ''
        self.version:        Version

        # 'depends', 'pre-depends', 'recommends', 'suggests', 'breaks', 
        # 'conflicts', 'provides', 'replaces', 'enhances', 'built-using']
        self.depends:        List[List[Dict[str, Any]]] = []

        # dependencies that must be satisfied before the package can be unpacked
        self.pre_depends:    List[List[Dict[str, Any]]] = []  
        self.recommends:     List[List[Dict[str, Any]]] = []
        self.suggests:       List[List[Dict[str, Any]]] = []
        self.breaks:         List[List[Dict[str, Any]]] = []
        self.conflicts:      List[List[Dict[str, Any]]] = []
        self.provides:       List[List[Dict[str, Any]]] = []
        self.replaces:       List[List[Dict[str, Any]]] = []
        self.enhances:       List[List[Dict[str, Any]]] = []
        self.built_using:    List[List[Dict[str, Any]]] = []

        self.depends_on:     List[str] = []
        self.depended_by:    List[str] = []

        # Not necessarily aligned to 'Priority' field, default set to 'Priority' field, may change later
        # this will be set to the highest priority of those packages that depends on them.
        # e.g. if 'required' package has a dependency, they will be 'required' too
        self.priority:       str  = ''
                                    
        self.arch:           str  = ''   # Architecture of the package, e.g. amd64, arm64, etc.

        self.installed:  bool = False  # Whether the package is installed or not
        self.configured: bool = False  # Whether the package is configured or not

        self._err_str: str = ""
        self._pkg_valid: bool = False  # Whether the package is valid or not

        # Setting Values post calling super()
        super().__init__(section)
        
        # List of version constraints for the package
        self._constraints: Dict[Version, VersionConstraint] = {}
        
        for _field in ['Package', 'Version', 'Architecture']:
            if _field not in self:
                tui.console.print(f"WARNING: Malformed package, skipping")
                self._err_str = f"Missing field '{_field}' in package"
                return
            if self[_field] is None or self[_field].strip() == '':
                tui.console.print(f"WARNING: Malformed package, skipping")
                self._err_str = f"Empty field '{_field}' in package"
                return
        

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
        
        self._isvalid = True  

    @property
    def isvalid(self) -> bool:
        """
        Returns whether the package is valid or not.
        A package is valid if it has all the required fields and they are not empty.
        """
        return self._isvalid
    
    @property
    def err_str(self) -> str:
        """
        Returns the error string if the package is not valid.
        If the package is valid, returns an empty string.
        """
        return self._err_str

    
    def get_provides(self) -> List[Tuple[str, Version]]:
        
        if len(self.provides) == 0:
            return []

        # Provides should not have alternates
        _provides_names: List[Tuple[str, Version]] = []


        # e.g. self.provides
        # [{'name': 'acorn', 'archqual': None, 'version': ('=', '8.0.5+ds+~cs19.19.27-3'), 'arch': None, 'restrictions': None}]
        # [{'name': 'node-acorn', 'archqual': None, 'version': ('=', '8.0.5'), 'arch': None, 'restrictions': None}]
        # [{'name': 'node-acorn', 'archqual': None, 'version': ('=', '8.0.5+ds+~cs19.19.27-3'), 'arch': None, 'restrictions': None}]
        # [{'name': 'node-acorn-bigint', 'archqual': None, 'version': ('=', '1.0.0'), 'arch': None, 'restrictions': None}]

        for _grp in self.provides:
            for _dep in _grp:
                
                if _dep['version'] is not None:
                    # If version is specified, we will use it
                    _version = Version(_dep['version'][1])
                else:
                    # If version is not specified, we will use the package version
                    _version = self.version
                
                _pkg = _dep['name'].strip()

                if _pkg == '':
                    tui.console.print(f"WARNING: Empty package name in provides "
                                      f"for {self.package} {self.version}, skipping")
                    continue

                _provides_names.append((_pkg, _version))
        
        # provides a list of tupples
        # [('acorn', '8.0.5+ds+~cs19.19.27-3'), ('node-acorn', '8.0.5+ds+~cs19.19.27-3'), ('node-acorn-bigint','1.0.0'), ]
    
        return _provides_names
    
    def does_provide(self, pkg_name: str) -> bool:
        """
        Checks if the current package provides the given package name
        Args:
            pkg_name: the package name to check for

        Returns:
            bool:
        """
        if not self.isvalid:
            return False
        
        return any(name == pkg_name for name, _ in self.get_provides())

    @property
    def constraints_satisfied(self) -> bool:
        # needs a version to check against
        if not self.isvalid:
            return False

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
        
        if constraint not in ['=', '>=', '<=', '>>', '<<']:
            tui.console.print(f"WARNING: Invalid constraint '{constraint}' for package {self.package} "
                              f"version {self.version}, skipping")
            return False

        # If the constraint is not added yet, add it
        if version not in self._constraints:
            self._constraints[version] = VersionConstraint(version, constraint)
            return True
        
        old_constraint: str = self._constraints[version].constraint

        # Constraint is already there, nothing to add
        if old_constraint == constraint:
            return True
        
        action = constraint_action[constraint][old_constraint]
        
        _constraint = VersionConstraint(version, constraint)
        
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
                              f"{self.package} {_constraint} vs {old_constraint}, ignoring")
            return False

class Source(Sources):
    """ Class to hold source package information.
    Source package is the original package from which binary packages are built
    It contains information about the source package, its version, architecture,
    and the binary packages that are built from it.
    """

    def __init__(self, section: str):

        self.package:    str = ''
        self.version:    Version
        self.arch:       List[str]

        # 'build-depends', 'build-depends-indep', 'build-depends-arch', 
        # 'build-conflicts', 'build-conflicts-indep', 'build-conflicts-arch', 'binary'
        self.binary:             List[List[Dict[str, Any]]]
        self.depends:            List[List[Dict[str, Any]]]
        self.depends_indep:      List[List[Dict[str, Any]]]
        self.depends_arch:       List[List[Dict[str, Any]]]
        self.conflicts:          List[List[Dict[str, Any]]]
        self.conflicts_indep:    List[List[Dict[str, Any]]]
        self.conflicts_arch:     List[List[Dict[str, Any]]]
        
        # can be derived from Package-List field, but it is tedious - correlation for versions required
        # One source provides multiple packages, package may have different version from the source version
        # Package-List may have additional information e.g. 'udeb' tag which is not there in package
        # Lets only select the package-files that the Package actually needs, the others produced are optional
        self.package_list: List[str]

        self.skip_test = False
        self.patch_list = []
        
        # Whether the package is valid or not, set to True if all required fields are present
        self._isvalid: bool = False 
        
        super().__init__(section)
        
        for _field in ['Package', 'Version', 'Directory']:
            if _field not in self:
                tui.console.print(f"WARNING: Malformed package, skipping")
                self._err_str = f"Missing field '{_field}' in package"
                return
            if self[_field] is None or self[_field].strip() == '':
                tui.console.print(f"WARNING: Malformed package, skipping")
                self._err_str = f"Empty field '{_field}' in package"
                return
        
        if 'Files' not in self or self['Files'] is None:
            tui.console.print(f"WARNING: Malformed package, skipping")
            self._err_str = "Missing 'Files' field in package"
            return

        # Setting Values post calling super()
        self.package = self['Package']
        self.version = Version(self['Version'])
        self.directory = self['Directory']
        self.files = self['Files']

        self.binary = self.relations.get('binary', [])

        self.depends = self.relations.get('depends', [])
        self.depends_indep = self.relations.get('depends-indep', [])
        self.depends_arch = self.relations.get('depends-arch', [])
        
        self.conflicts = self.relations.get('conflicts', [])
        self.conflicts_indep = self.relations.get('conflicts-indep', [])
        self.conflicts_arch = self.relations.get('conflicts-arch', [])
        
        _package_list = self.get('Package-List', '').strip()
        if _package_list:
            self.package_list = [line for line in self['package-list'].split('\n') if line.strip()]
       
        _arch_field = self.get('Architecture', '').strip()
        if not _arch_field:
            self.arch = ['any']
        else:
            self.arch = _arch_field.split()
        
        self._isvalid = True  # Package is valid if all required fields are present

    @property
    def isvalid(self) -> bool:
        """
        Returns whether the source package is valid or not.
        A package is valid if it has all the required fields and they are not empty.
        """
        return self._isvalid
    
    @property
    def download_size(self) -> int:
        _download_size = 0
        for _file in self.files:
            _download_size += int(_file['size'])
        return _download_size


    def build_depends(self, arch: str) -> List[List[Dict[str, Any]]]:
        """
        Returns a list of tuples: (package_name, version_constraint)
        from build-depends, build-depends-indep, and build-depends-arch.
        """

        all_deps: List[List[Dict[str, Any]]] = []
        
        # Combine all relevant build-depends lists
        for _dep_group in (self.depends, self.depends_indep, self.depends_arch):
            for _dep_package in _dep_group:
                all_deps.append(_dep_package)

        return all_deps