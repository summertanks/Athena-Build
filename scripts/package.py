# internal modules
from debian.deb822 import Packages, Sources
from debian.debian_support import Version

import apt_pkg
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
        self._version: Version   = version
        self._constraint: str    = constraint.strip()
        
        if not self._constraint:
            self._constraint = '='
        
        if self._constraint not in ['=', '>', '<', '>=', '<=', '>>', '<<']:
            raise ValueError(f"Invalid operator: {self._constraint}")

    def __repr__(self):
        return f"{self._constraint} {self._version}"
    
    def is_satisfied_by(self, candidate: Version) -> bool:

        if self._constraint == '=':
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

    def __init__(self, section: str):
        
        # Whether the package is valid or not, set to True if all required fields are present
        self._isvalid: bool = False
        self.package:        str     = ''
        self.version:        Version = Version('0')

        # 'depends', 'pre-depends', 'recommends', 'suggests', 'breaks',
        # 'conflicts', 'provides', 'replaces', 'enhances', 'built-using']
        self.depends:        List[Tuple] = []
        self.alt_depends:    List[List[Tuple]] = []

        # dependencies that must be satisfied before the package can be unpacked
        self.pre_depends:    List[Tuple] = []
        self.recommends:     List[Tuple] = []
        self.suggests:       List[Tuple] = []
        self.breaks:         List[List[Tuple]] = []
        self.conflicts:      List[List[Tuple]] = []
        self.provides:       List[List[Tuple]] = []
        self.replaces:       List[List[Tuple]] = []
        self.enhances:       List[List[Tuple]] = []
        self.built_using:    List[List[Tuple]] = []

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

        # Setting Values post calling super()
        super().__init__(section)
        
        # List of version constraints for the package
        self._constraints: Dict[Version, VersionConstraint] = {}
        
        for _field in ['Package', 'Version', 'Architecture']:
            _pkg_name = self.get('Package', '<unknown>')
            if _field not in self:
                self._err_str = f"Missing field '{_field}' in package '{_pkg_name}'"
                tui.console.print(f"WARNING: {self._err_str}")
                return
            if self[_field] is None or self[_field].strip() == '':
                self._err_str = f"Empty field '{_field}' in package '{_pkg_name}'"
                tui.console.print(f"WARNING: {self._err_str}")
                return

        self.package = self['Package']
        try:
            self.version = Version(self['Version'])
        except (ValueError, TypeError) as e:
            self._err_str = f"Invalid version '{self['Version']}' in package '{self.package}': {e}"
            tui.console.print(f"WARNING: {self._err_str}")
            return
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

        def _parse(field):
            raw = self.get(field, '') or ''
            return apt_pkg.parse_depends(raw, strip_multi_arch=True) if raw.strip() else []

        try:
            _all_deps        = _parse('Depends')
            self.depends     = [g[0] for g in _all_deps if len(g) == 1]
            self.alt_depends = [g     for g in _all_deps if len(g) > 1]

            self.pre_depends = [g[0] for g in _parse('Pre-Depends') if len(g) == 1]
            self.recommends  = [g[0] for g in _parse('Recommends')  if len(g) == 1]
            self.suggests    = [g[0] for g in _parse('Suggests')    if len(g) == 1]
            self.breaks      = _parse('Breaks')
            self.conflicts   = _parse('Conflicts')
            self.provides    = _parse('Provides')
            self.replaces    = _parse('Replaces')
            self.enhances    = _parse('Enhances')
            self.built_using = _parse('Built-Using')
        except Exception as e:
            self._err_str = f"Failed to parse dependencies for package '{self.package}': {e}"
            tui.console.print(f"WARNING: {self._err_str}")
            return

        self._isvalid = True

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
        
        if not self.isvalid:
            return []

        if not self.provides:
            return []

        # Provides should not have alternates
        _provides_names: List[Tuple[str, Version]] = []


        # e.g. self.provides after apt_pkg.parse_depends('Provides'):
        #   [[('acorn', '8.0.5+ds+~cs19.19.27-3', '=')]]
        #   [[('node-acorn', '8.0.5', '=')]]
        #   [[('node-acorn', '8.0.5+ds+~cs19.19.27-3', '=')]]
        #   [[('node-acorn-bigint', '1.0.0', '=')]]
        #   [[('foo', '', '')]] # no version

        _version: Version
        for _grp in self.provides:
            for _dep in _grp:
                try:
                    _name = _dep[0].strip()
                    _version_str = _dep[1]
                    _operator = _dep[2]

                    if not _name:
                        tui.console.print(f"WARNING: Empty package name in provides for {self.package} {self.version}, skipping")
                        continue

                    # if we have either, both should be valid
                    if _version_str or _operator:
                        if _operator != '=':
                            # only '=' operator is permitted for provides field
                            tui.console.print(f"WARNING: Provides for {self.package} has invalid version operator ")
                            _version = self.version
                        elif not _version_str:
                            # sanity check, just in case
                            tui.console.print(f"WARNING: Provides for {self.package} has invalid version")
                            _version = self.version
                        else:
                            # set version
                            _version = Version(_version_str)
                    else:
                        # use host package version
                        _version = self.version

                    _provides_names.append((_name, _version))

                except (IndexError, ValueError, AttributeError, TypeError) as e:
                    tui.console.warning(f"Skipping malformed provides entry for '{self.package}': {e}")
                    continue
        
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
        # '>' and '<' are deprecated aliases for '>>' and '<<' per Debian policy,
        # but apt_pkg.parse_depends still emits them for legacy packages.
        constraint_action = {
            '=':   {'=': 'nc', '>=': 'xg',  '<=': 'xg', '>>': 'err', '<<': 'err', '>': 'err', '<': 'err'},
            '>=':  {'=': 'nc', '>=': 'nc',  '<=': 'eq', '>>': 'nc',  '<<': 'err', '>': 'nc',  '<': 'err'},
            '<=':  {'=': 'nc', '>=': 'eq',  '<=': 'nc', '>>': 'err', '<<': 'nc',  '>': 'err', '<': 'nc' },
            '>>':  {'=': 'err','>=': 'xg',  '<=': 'err','>>': 'nc',  '<<': 'err', '>': 'nc',  '<': 'err'},
            '<<':  {'=': 'err','>=': 'err', '<=': 'xg', '>>': 'err', '<<': 'nc',  '>': 'err', '<': 'nc' },
            '>':   {'=': 'err','>=': 'xg',  '<=': 'err','>>': 'nc',  '<<': 'err', '>': 'nc',  '<': 'err'},
            '<':   {'=': 'err','>=': 'err', '<=': 'xg', '>>': 'err', '<<': 'nc',  '>': 'err', '<': 'nc' },
        }

        if constraint == '': constraint = '='

        if constraint not in ['=', '>=', '<=', '>>', '<<', '>', '<']:
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

        self.package:    str     = ''
        self.version:    Version = Version('0')
        self.directory:  str     = ''
        self.files:      Dict[str, Dict[str, Any]] = {}
        self.arch:       List[str] = []

        # 'build-depends', 'build-depends-indep', 'build-depends-arch',
        # 'build-conflicts', 'build-conflicts-indep', 'build-conflicts-arch', 'binary'
        self.binary:             List[str] = []
        self.depends:            List[List[Dict[str, Any]]] = []
        self.depends_indep:      List[List[Dict[str, Any]]] = []
        self.depends_arch:       List[List[Dict[str, Any]]] = []
        self.conflicts:          List[List[Dict[str, Any]]] = []
        self.conflicts_indep:    List[List[Dict[str, Any]]] = []
        self.conflicts_arch:     List[List[Dict[str, Any]]] = []

        # can be derived from Package-List field, but it is tedious - correlation for versions required
        # One source provides multiple packages, package may have different version from the source version
        # Package-List may have additional information e.g. 'udeb' tag which is not there in package
        # Lets only select the package-files that the Package actually needs, the others produced are optional
        self.package_list: List[str] = []

        # Runtime: binary .deb filenames produced from this source (populated by DependencyTree.parse_sources)
        self.pkgs: List[str] = []

        self.skip_test = False
        self.patch_list = []
        self._err_str: str = ''

        # Whether the package is valid or not, set to True if all required fields are present
        self._isvalid: bool = False 
        
        super().__init__(section)
        
        for _field in ['Package', 'Version', 'Directory']:
            _pkg_name = self.get('Package', '<unknown>')
            if _field not in self:
                self._err_str = f"Missing field '{_field}' in source '{_pkg_name}'"
                tui.console.print(f"WARNING: {self._err_str}")
                return
            if self[_field] is None or self[_field].strip() == '':
                self._err_str = f"Empty field '{_field}' in source '{_pkg_name}'"
                tui.console.print(f"WARNING: {self._err_str}")
                return

        if 'Files' not in self or self['Files'] is None:
            _pkg_name = self.get('Package', '<unknown>')
            self._err_str = f"Missing 'Files' field in source '{_pkg_name}'"
            tui.console.print(f"WARNING: {self._err_str}")
            return

        # Setting Values post calling super()
        self.package = self['Package']
        try:
            self.version = Version(self['Version'])
        except (ValueError, TypeError) as e:
            self._err_str = f"Invalid version '{self['Version']}' in source '{self.package}': {e}"
            tui.console.print(f"WARNING: {self._err_str}")
            return
        self.directory = self['Directory']

        try:
            _sha256_map: Dict[str, str] = {}
            for _entry in (self.get('Checksums-Sha256') or []):
                _sha256_map[_entry['name']] = _entry['sha256']

            self.files: Dict[str, Dict[str, Any]] = {
                _entry['name']: {
                    'md5':    _entry['md5sum'],
                    'sha256': _sha256_map.get(_entry['name'], ''),
                    'size':   int(_entry['size']),
                    'path':   self['Directory'].rstrip('/') + '/' + _entry['name'],
                }
                for _entry in self['Files']
            }
        except (KeyError, ValueError, TypeError) as e:
            self._err_str = f"Failed to parse Files/Checksums for source '{self.package}': {e}"
            tui.console.print(f"WARNING: {self._err_str}")
            return

        self.binary = [p.strip() for p in self.get('Binary', '').split(',') if p.strip()]

        try:
            self.depends         = self.relations.get('build-depends', [])
            self.depends_indep   = self.relations.get('build-depends-indep', [])
            self.depends_arch    = self.relations.get('build-depends-arch', [])

            self.conflicts       = self.relations.get('build-conflicts', [])
            self.conflicts_indep = self.relations.get('build-conflicts-indep', [])
            self.conflicts_arch  = self.relations.get('build-conflicts-arch', [])
        except Exception as e:
            self._err_str = f"Failed to parse build dependencies for source '{self.package}': {e}"
            tui.console.print(f"WARNING: {self._err_str}")
            return
        
        _raw_pkg_list = self.get('Package-List') or ''
        if isinstance(_raw_pkg_list, list):
            self.package_list = [str(item).strip() for item in _raw_pkg_list if str(item).strip()]
        elif isinstance(_raw_pkg_list, str):
            self.package_list = [line for line in _raw_pkg_list.split('\n') if line.strip()]
       
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
    def err_str(self) -> str:
        return self._err_str

    @property
    def download_size(self) -> int:
        if not self._isvalid:
            return 0
        return sum(f['size'] for f in self.files.values())


    def build_depends(self, arch: str) -> List[List[Dict[str, Any]]]:
        """
        Returns combined build dependencies from build-depends, build-depends-indep,
        and build-depends-arch as a list of dependency groups (each group is a list
        of dicts with keys: name, version, arch, archqual, restrictions).
        """

        all_deps: List[List[Dict[str, Any]]] = []
        
        # Combine all relevant build-depends lists
        for _dep_group in (self.depends, self.depends_indep, self.depends_arch):
            for _dep_package in _dep_group:
                all_deps.append(_dep_package)

        return all_deps