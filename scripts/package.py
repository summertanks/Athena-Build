# internal modules
from debian.deb822 import Packages, Sources
from debian.debian_support import Version

import logging
import threading
import apt_pkg
import tui
import utils

from typing import List, Dict, Any, Optional, Tuple

# `apt_pkg.config['APT::Build-Profiles']` is a PROCESS-GLOBAL that
# `Source.build_depends` sets and then reads via `parse_src_depends`.
# Under COMP-03 parallel builds (per-package profile overrides, ARCH-16),
# worker A's set could be overwritten by worker B before A's parse ran,
# filtering A's `<!nocheck>`-style build-deps under B's profiles — a
# silently wrong build-dep install set.  Serialise the set+parse pair.
_BUILD_PROFILES_LOCK = threading.Lock()

logger = logging.getLogger('athena.cache')

# python-debian's Deb822 base uses an OrderedSet whose __keys
# nodes hold weakrefs.  Those plus the parsed-relation mixin caches
# can't pickle.  Drop them on __getstate__ — they're reconstructed on
# load by re-initialising Deb822 with the captured field dict (Package)
# or raw_text (Source — its multi-valued field wrappers also carry
# weakrefs, so field-replay isn't enough).
_DEB822_INTERNAL_PREFIXES = ('_Deb822', '_PkgRelationMixin',
                              '_VersionAccessorMixin')
_DEB822_INTERNAL_ATTRS = frozenset({
    'encoding', 'decoder', 'gpg_info', '_err_str',
})


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

        # Origin mirror (set by Cache.__build_cache after parsing).  None for
        # synthetic Package objects (e.g. ones built from `dpkg-deb -f` output
        # in BuildSystem._check_dep_drift).  Consumers that need to download
        # this package's .deb must check that _mirror is set.
        self._mirror = None  # type: 'Optional[utils.Mirror]'

        # 'depends', 'pre-depends', 'recommends', 'suggests', 'breaks',
        # 'conflicts', 'provides', 'replaces', 'enhances', 'built-using']
        self.depends:        List[Tuple] = []
        self.alt_depends:    List[List[Tuple]] = []

        # dependencies that must be satisfied before the package can be unpacked
        self.pre_depends:     List[Tuple]       = []
        self.alt_pre_depends: List[List[Tuple]] = []
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

            _all_pre = _parse('Pre-Depends')
            self.pre_depends     = [g[0] for g in _all_pre if len(g) == 1]
            self.alt_pre_depends = [g     for g in _all_pre if len(g) > 1]
            self.recommends  = [g[0] for g in _parse('Recommends')  if len(g) == 1]
            self.suggests    = [g[0] for g in _parse('Suggests')    if len(g) == 1]
            self.breaks      = _parse('Breaks')
            self.conflicts   = _parse('Conflicts')
            self.provides    = _parse('Provides')
            self.replaces    = _parse('Replaces')
            self.enhances    = _parse('Enhances')
            self.built_using = _parse('Built-Using')
        except (ValueError, KeyError, AttributeError, SystemError) as e:
            # SystemError covers apt_pkg.Error from parse_depends inside _parse.
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

    def __hash__(self) -> int:  # type: ignore[override]
        # Deb822Dict is unhashable (sets __hash__ = None) because it's a
        # mutable mapping; we intentionally make Package hashable so it
        # can sit in sets / dict keys via (package, version, arch) — a
        # tuple that's immutable for our purposes (versions/arch don't
        # change after parsing).
        return hash((self.package, self.version, self.arch))

    @property
    def isvalid(self) -> bool:
        """
        Returns whether the package is valid or not.
        A package is valid if it has all the required fields and they are not empty.
        """
        return self._isvalid

    def explicit_provides_version(self, name: str) -> 'Optional[Version]':
        """Return the EXPLICITLY-declared Provides version for `name`,
        or None if this package provides `name` unversioned (or doesn't
        provide it at all).

        Distinct from get_provides() which substitutes self.version when
        the Provides entry is unversioned — that substitution is correct
        for Depends-satisfaction lookups (apt treats the provider's
        version as the satisfying version) but WRONG for versioned
        Breaks/Conflicts constraints, where Debian Policy §7.5 says an
        unversioned Provides cannot satisfy the constraint at all.

        Used by validate_selection's Breaks/Conflicts arms to suppress
        spurious breaks like `fwupd (Provides: fwupdate)` triggering
        `linux-image (Breaks: fwupdate (<< 12-7))` despite fwupd's
        Provides making no version claim about the fwupdate name.
        """
        if not self.isvalid or not self.provides:
            return None
        for _grp in self.provides:
            for _dep in _grp:
                try:
                    if _dep[0].strip() != name:
                        continue
                    _ver_str = _dep[1]
                    _op = _dep[2]
                    # Both ver_str and op must be present for an
                    # explicit Provides version — `Provides: foo (= X)`.
                    if _ver_str and _op == '=':
                        return Version(_ver_str)
                    # Found the Provides entry but it's unversioned
                    # (or has an invalid operator that get_provides
                    # would warn about).  Either way, no explicit
                    # version to claim.
                    return None
                except (IndexError, ValueError, AttributeError, TypeError):
                    continue
        return None

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
                    logger.warning(f"Skipping malformed provides entry for '{self.package}': {e}")
                    continue

        # provides a list of tupples
        # [('acorn', '8.0.5+ds+~cs19.19.27-3'), ('node-acorn', '8.0.5+ds+~cs19.19.27-3'), ('node-acorn-bigint','1.0.0'), ]

        return _provides_names

    @property
    def constraints_satisfied(self) -> bool:
        # needs a version to check against
        if not self.isvalid:
            return False

        _satisfied = True

        # Collect Provides versions once (Debian Policy §7.5: a Provides
        # field with version is considered when satisfying version-
        # constrained dependencies — `Depends: foo (= X)` is satisfied
        # by `bar Provides: foo (= X)` even if bar's own version is Y).
        # apt resolves this way; without it, derivative forks that
        # Provide upstream pkg names at a different own-version
        # (athena-tasksel 3.73+athena1 Provides: tasksel (= 3.73)) fail
        # downstream strict-version Depends.
        _provides_versions = []
        try:
            for _name, _prov_ver in self.get_provides():
                if _prov_ver:
                    _provides_versions.append(_prov_ver)
        except (ValueError, KeyError, AttributeError, SystemError):
            pass

        for _ver in self._constraints.keys():
            _constraint = self._constraints[_ver]

            if _constraint.is_satisfied_by(self.version):
                continue
            if any(_constraint.is_satisfied_by(_pv)
                   for _pv in _provides_versions):
                continue
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
            _msg = (f"Invalid constraint '{constraint}' for package {self.package} "
                    f"version {self.version}, skipping")
            tui.console.print(f"WARNING: {_msg}")
            logger.warning(_msg)   # persist constraint faults to the log too
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
            _msg = ("Cannot resolve conflicting constraints for "
                    f"{self.package} {_constraint} vs {old_constraint}, ignoring")
            tui.console.print(f"WARNING: {_msg}")
            logger.warning(_msg)   # persist constraint faults to the log too
            return False

    # ── pickle support ────────────────────────────────────────────
    # Replay field-by-field on load so Deb822's internal OrderedSet (with
    # weakref __keys) is reconstructed fresh rather than serialised.  Drop
    # the Deb822-internal attributes (_Deb822Dict__*, _PkgRelationMixin__*
    # caches, encoding/decoder/gpg_info) from __dict__ — they're rebuilt
    # by Packages.__init__ + the field replays.
    def __getstate__(self):
        return {
            '_fields': {k: self[k] for k in self.keys()},
            '_attrs': {
                k: v for k, v in self.__dict__.items()
                if not k.startswith(_DEB822_INTERNAL_PREFIXES)
                and k not in _DEB822_INTERNAL_ATTRS
            },
        }

    def __setstate__(self, state):
        Packages.__init__(self)
        for k, v in state['_fields'].items():
            self[k] = v
        self.__dict__.update(state['_attrs'])


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

        # Origin mirror (set by Cache.__build_cache after parsing).  Used by
        # download_source to fetch tarballs from the right pool — sources in
        # bookworm-security live under a different baseid than main.
        self._mirror = None  # type: 'Optional[utils.Mirror]'

        self.binary: List[str] = []

        self.package_list: List[str] = []

        # NOTE: predicted binary filenames live in DependencyTree.src_pkg_files
        # (per-tree map keyed by source name), NOT on the Source object.
        # Source objects are shared across deb and udeb dep-trees via
        # cache.source_hashtable; storing pkgs here let the udeb pass
        # overwrite the deb pass's list, hiding deb-cohort gaps in source
        # audit.  Removed 2026-05-20; pre-audit-split-2026-05-20 tag
        # preserves the prior layout.

        self.skip_test = False
        self.patch_list: List[str] = []
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

        # Either 'Files' (legacy MD5 list) or 'Checksums-Sha256' must be
        # present so we know what to download.  bookworm-security drops the
        # MD5 list entirely; main still ships both.  We treat
        # 'Checksums-Sha256' as canonical (it's what download_source
        # verifies against), with 'Files' contributing optional MD5s.
        _has_sha256 = bool(self.get('Checksums-Sha256'))
        _has_md5    = bool(self.get('Files'))
        if not _has_sha256 and not _has_md5:
            _pkg_name = self.get('Package', '<unknown>')
            self._err_str = (f"Source '{_pkg_name}' has neither 'Files' nor "
                             f"'Checksums-Sha256' — no way to verify downloads")
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
            _md5_map: Dict[str, str] = {}
            _size_map: Dict[str, int] = {}
            for _entry in (self.get('Files') or []):
                _md5_map[_entry['name']]  = _entry['md5sum']
                _size_map[_entry['name']] = int(_entry['size'])

            # Build authoritative list from sha256 entries; fall back to MD5
            # entries if sha256 is absent (shouldn't happen on modern Debian
            # but keeps the parser tolerant).
            _entries = list(self.get('Checksums-Sha256') or []) or [
                {'name': e['name'], 'size': e['size'], 'sha256': ''}
                for e in (self.get('Files') or [])
            ]

            self.files = {
                _entry['name']: {
                    'md5':    _md5_map.get(_entry['name'], ''),
                    'sha256': _entry.get('sha256', ''),
                    'size':   int(_entry.get('size', _size_map.get(_entry['name'], 0))),
                    'path':   self['Directory'].rstrip('/') + '/' + _entry['name'],
                }
                for _entry in _entries
            }
        except (KeyError, ValueError, TypeError) as e:
            self._err_str = f"Failed to parse Files/Checksums for source '{self.package}': {e}"
            tui.console.print(f"WARNING: {self._err_str}")
            return

        self.binary = [p.strip() for p in self.get('Binary', '').split(',') if p.strip()]

        _raw_pkg_list = self.get('Package-List') or ''
        self.package_list = []
        if isinstance(_raw_pkg_list, list):
            for _item in _raw_pkg_list:
                if hasattr(_item, 'get'):
                    # python-debian parses Package-List into Deb822Dict with keys:
                    # 'package', 'package-type', 'section', 'priority', '_other'
                    # All optional key=value pairs (arch=, profile=) are in '_other'
                    _name = (_item.get('package') or '').strip()
                    _type = (_item.get('package-type') or 'deb').strip()
                    if not _name or not _type:
                        continue
                    _line = f'{_name} {_type}'
                    _other = (_item.get('_other') or '').strip()
                    if _other:
                        _line += f' {_other}'
                    self.package_list.append(_line)
                elif str(_item).strip():
                    self.package_list.append(str(_item).strip())
        elif isinstance(_raw_pkg_list, str):
            self.package_list = [line.strip() for line in _raw_pkg_list.split('\n') if line.strip()]

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
        if not self._isvalid:
            return 0
        return sum(f['size'] for f in self.files.values())



    def build_depends(self, arch: str, active_profiles: frozenset = frozenset(),
                      cache: Optional[Any] = None) -> List[List[Tuple]]:
        """Returns combined build dependencies filtered by arch and active build profiles.

        Each entry is a list of OR-alternatives; each alternative is a tuple (name, ver, op).
        apt_pkg.parse_src_depends filters arch and profile restrictions internally.

        Virtual-package expansion (when `cache` is passed): a single-element
        group whose only name is a virtual package with multiple concrete
        providers (e.g. `libcurl4-dev` → libcurl4-{openssl,gnutls,nss}-dev,
        `libsdl-dev` → libsdl1.2-{dev,compat-dev}) is rewritten in-place to
        an alternatives group `[provider1, provider2, …, virtual_name]`,
        with concrete providers sorted alphabetically by canonical name
        and the original virtual kept as the final fallback.  This makes
        the BuildContainer's apt-install chain (which `||`-fallbacks
        across alternatives) succeed without the operator having to
        author a `debian/control` patch — apt-get refuses to disambiguate
        such virtuals from the CLI and would otherwise fail
        non-interactively with "Package 'X' has no installation
        candidate".  Without `cache`, no expansion happens (caller-driven
        opt-in, keeps the dep-tree-time parse a pure transform).
        """
        # hold the lock across the global set AND every parse that
        # reads it, so a concurrent worker can't swap the profile set out
        # from under this source's parse.  parse is microseconds; the lock
        # is uncontended in the common single-build case.
        all_deps: List[List[Tuple]] = []
        with _BUILD_PROFILES_LOCK:
            apt_pkg.config['APT::Build-Profiles'] = ' '.join(active_profiles)  # type: ignore[index]
            for field in ('Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch'):
                raw = (self.get(field) or '').strip()
                if raw:
                    try:
                        all_deps.extend(apt_pkg.parse_src_depends(raw, architecture=arch))
                    except (SystemError, ValueError) as e:
                        # apt_pkg.Error inherits from SystemError.
                        logger.warning(f"parse_src_depends({field}) for '{self.package}': {e}")
        if cache is None:
            return all_deps
        return [self._expand_virtual_alternatives(grp, cache) for grp in all_deps]

    @staticmethod
    def _expand_virtual_alternatives(group: List[Tuple], cache: Any) -> List[Tuple]:
        """Expand a single-element build-dep group whose name is a multi-
        provider virtual package into an alternatives chain.

        Multi-element groups (already alternative-typed by the maintainer)
        are returned unchanged — apt's `|` chain already handles them.

        A name is a "multi-provider virtual" iff `cache.get_packages(name)`
        returns ≥ 2 distinct canonical Package: names AND none of them is
        `name` itself.  When a REAL package exists under the name (it may
        additionally be Provided by others — libunwind-dev is real AND
        provided by LLVM's libunwind-{14,15,16,19}-dev), the group is
        returned VERBATIM: apt installs the real package directly, and
        substituting a provider inverts Debian semantics (the 2026-06-07
        gstreamer1.0 failure).  Providers are sorted alphabetically for
        determinism; the original virtual name is appended last so a
        host that *already* has any provider satisfies the dep without
        a redundant install attempt against the alphabetic-first
        provider.

        Tuple shape is preserved (name, ver, op) for each alternative —
        synthetic providers inherit the version/op constraints of the
        original virtual entry, matching how apt itself propagates
        version constraints across a `|` alternation when only one of
        the alternatives carries the constraint.

        Provider order: providers whose name starts with the virtual
        name (e.g. `imagemagick-6.q16` for virtual `imagemagick`) sort
        before name-unrelated providers (`graphicsmagick-imagemagick-compat`).
        Within each tier, alphabetic.  Without the tier, plain alphabetic
        put `graphicsmagick-imagemagick-compat` first for the `imagemagick`
        virtual — and the BuildContainer's OR-chain stops at the first
        success, so apt would install GraphicsMagick's `convert` shim
        instead of real ImageMagick, breaking fonts-noto-color-emoji's
        Makefile recipe that uses `convert -size … canvas:none …`.
        """
        if len(group) != 1:
            return group
        _name, _ver, _op = group[0]
        try:
            _candidates = cache.get_packages(_name)
        except (KeyError, AttributeError):
            return group
        _canonical = {_pkg['Package'] for _pkg in _candidates}
        if _name in _canonical:
            # A REAL package exists under this exact name — apt installs
            # it directly, and Debian semantics never substitute a
            # concrete name with a Provides alias.  Expanding anyway put
            # the providers FIRST in the ||-chain (alphabetic: LLVM's
            # libunwind-14-dev before the real libunwind-dev), so the
            # container installed llvm's libunwind (no libunwind.pc) and
            # gstreamer1.0's meson hard-failed on the missing dependency
            # (caught 2026-06-07 during the thor1 full rebuild).
            # Expansion is ONLY for purely-virtual names.
            return group
        _providers = sorted(_canonical,
                            key=lambda _p: (not _p.startswith(_name), _p))
        if len(_providers) < 2:
            return group
        return [(_p, _ver, _op) for _p in _providers] + [(_name, _ver, _op)]

    # ── pickle support ────────────────────────────────────────────
    # Source uses raw_text-based restore rather than field replay because
    # its multi-valued fields (Files, Checksums-Sha256, Build-Depends) come
    # back from python-debian as wrappers whose internal lists carry
    # weakrefs.  Source.__init__ stashes the original stanza in raw_text;
    # re-init Deb822 from that on load to get clean instances.
    def __getstate__(self):
        return {
            '_attrs': {
                k: v for k, v in self.__dict__.items()
                if not k.startswith(_DEB822_INTERNAL_PREFIXES)
                and k not in _DEB822_INTERNAL_ATTRS
            },
        }

    def __setstate__(self, state):
        import io
        _attrs = state['_attrs']
        _raw = _attrs.get('raw_text', b'')
        if isinstance(_raw, str):
            _raw = _raw.encode('utf-8', errors='replace')
        Sources.__init__(self, io.BytesIO(_raw))
        self.__dict__.update(_attrs)
