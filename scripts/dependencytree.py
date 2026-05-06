# Internal modules
from collections import defaultdict
from typing import Dict, List, Optional
import re

import package
from package import Version
from cache import Cache

# External Modules
import apt_pkg

import tui
from tui import Prompt, PROMPT_OPTIONS


def _auto_pick_candidate(candidates):
    """Pick a unique candidate without prompting, when possible.

    Returns the chosen Package if every candidate has the same Package name
    (i.e. the only difference is version) — in that case the highest version
    wins, matching apt's "highest version satisfying constraints" rule.
    Within a coherent mirror set (single snapshot or a live archive at one
    moment in time) this is always the right call.

    Returns None when candidates name *different* Packages (genuine
    alternative providers like mawk vs gawk for the virtual `awk`).  The
    caller must prompt in that case — there is no mechanical rule.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    _names = {p['Package'] for p in candidates}
    if len(_names) == 1:
        return max(candidates, key=lambda p: p.version)
    return None


class DependencyTree:

    def __init__(self, cache: Cache, select_recommended: bool, arch: str,
                 build_profiles: frozenset = frozenset(), lookahead=None):

        self.__recommended = select_recommended
        self.__cache = cache
        # Dict[name_or_virtual, Dict[Version, Package]]
        # Mirrors package_hashtable structure — multiple versions per name co-exist without overwrite
        self.__lookahead: Dict[str, Dict[Version, package.Package]] = defaultdict(dict)

        self.selected_pkgs: Dict[str, package.Package] = {}
        self.selected_srcs: Dict[str, package.Source]  = {}
        self.arch = arch
        self.build_profiles = build_profiles

        if lookahead is not None:
            self.add_lookahead(lookahead)

    def add_lookahead(self, lookahead: List[str]):
        for _pkg_name in lookahead:
            if not _pkg_name or _pkg_name.isspace():
                continue
            _pkg_name = _pkg_name.strip()

            # 1. Verify package exists in cache
            _candidates = self.__cache.get_packages(_pkg_name)
            if not _candidates:
                tui.console.print(f"WARNING: add_lookahead: '{_pkg_name}' not found in cache, skipping")
                tui.console.warning(f"add_lookahead: '{_pkg_name}' not found in cache, skipping")
                continue

            # 2. Select version — prompt if multiple exist
            if len(_candidates) == 1:
                _selected = _candidates[0]
            else:
                _mark = tui.console.mark()
                tui.console.print(f"Multiple versions for '{_pkg_name}':")
                for _i, _c in enumerate(_candidates, 1):
                    tui.console.print(f"  {_i}.  {_c.package}  ({_c.version})")
                _options = [str(_i) for _i in range(1, len(_candidates) + 1)]
                _choice  = Prompt(PROMPT_OPTIONS, f"Select [1-{len(_candidates)}]", _options).get_response()
                _selected = _candidates[int(_choice) - 1]
                tui.console.trim_to(_mark)
                tui.console.print(f"Multiple versions for '{_pkg_name}': Selected {_selected.package} ({_selected.version})")

            # 3. Hard conflict check against entries already in lookahead
            _conflict_found = False
            for _conflict_group in _selected.conflicts:
                _conflict_name    = _conflict_group[0][0]
                _conflict_ver_str = _conflict_group[0][1]
                _conflict_op      = _conflict_group[0][2]
                if _conflict_name in self.__lookahead:
                    # Only block on real-package conflicts. Provides-aliases (e.g. apt registering
                    # 'debconf-tiny' as a virtual name) should not trigger here — those are deferred
                    # to validate_selection() where version-aware checks run properly.
                    _is_real = any(pkg['Package'] == _conflict_name
                                   for pkg in self.__lookahead[_conflict_name].values())
                    if not _is_real:
                        continue

                    # Check version constraint: e.g. Conflicts: apt (<< 0.5.4) must not fire
                    # against apt 2.x. Only block if the lookahead package's version actually
                    # satisfies the conflict operator. No version string = unconditional conflict.
                    if _conflict_ver_str and _conflict_op in self._VALID_CONSTRAINTS:
                        _triggered     = False
                        _triggered_ver = None
                        for _lver in self.__lookahead[_conflict_name]:
                            try:
                                if apt_pkg.check_dep(str(_lver), _conflict_op, _conflict_ver_str):
                                    _triggered     = True
                                    _triggered_ver = _lver
                                    break
                            except Exception:
                                _triggered     = True  # conservative: assume conflict on parse error
                                _triggered_ver = _lver
                                break
                    else:
                        _triggered     = True   # no version constraint = unconditional conflict
                        _triggered_ver = next(iter(self.__lookahead[_conflict_name]), None)

                    if _triggered:
                        _ver_info = f" ({_triggered_ver})" if _triggered_ver else ""
                        tui.console.print(f"ERROR: Cannot add '{_pkg_name}' — conflicts with '{_conflict_name}{_ver_info}' already in lookahead")
                        tui.console.error(f"CRITICAL: add_lookahead — '{_pkg_name}' conflicts with '{_conflict_name}{_ver_info}'")
                        _conflict_found = True
                        break
            if _conflict_found:
                continue

            # 4. Add real name under its version; add provided virtual names under their provided version
            self.__lookahead[_pkg_name][_selected.version] = _selected
            for _provided_name, _provided_ver in _selected.get_provides():
                if _provided_name != _pkg_name:
                    self.__lookahead[_provided_name][_provided_ver] = _selected

    # Operators accepted by apt_pkg.check_dep for version constraint checks
    _VALID_CONSTRAINTS = {'=', '>=', '<=', '>>', '<<', '>', '<'}

    @property
    def selected_count(self) -> int:
        return sum(1 for _k in self.selected_pkgs if _k == self.selected_pkgs[_k]['Package'])

    @property
    def canonical_pkgs(self) -> Dict[str, 'package.Package']:
        """selected_pkgs filtered to canonical entries only (key == pkg['Package']).

        selected_pkgs also stores virtual-alias entries where the key is a
        provided name (e.g. 'libcomerr2') mapping to the real Package object
        (package='libcom-err2').  Iterating canonical_pkgs skips those aliases
        so callers get exactly one entry per real package.
        """
        return {k: v for k, v in self.selected_pkgs.items() if k == v['Package']}

    def resolve_packages(self, packages: list[str]) -> list[str]:
        self.add_lookahead(packages)
        unresolved = [pkg for pkg in packages if self.parse_dependency(pkg) is None]
        for pkg in unresolved:
            tui.console.print(f"WARNING: cannot resolve '{pkg}'")
            tui.console.error(f"parse_dependency({pkg}) returned None")
        return unresolved                                    


    def parse_dependency(self, package_name: str,
                         version: Optional[Version] = None,
                         constraint: str = '') -> Optional[package.Package]:
        
        _selected_pkg: package.Package
        # Normalise constraint — fall back to '>=' for anything unrecognised
        _constraint = constraint if constraint in self._VALID_CONSTRAINTS else '>='

        def _satisfies(pkg_ver: Version) -> bool:
            """True if pkg_ver meets the requested version/constraint, or no version was requested."""
            if version is None:
                return True
            try:
                return apt_pkg.check_dep(str(pkg_ver), _constraint, str(version))
            except Exception:
                return True   # can't evaluate — assume satisfied, validate_selection will catch
            
        if not package_name:
            tui.console.print("Dependency Check: Dependency asked for empty package name")
            return None
        
        # Early return if already selected by name
        if package_name in self.selected_pkgs:
            _existing = self.selected_pkgs[package_name]
            if _satisfies(_existing.version):
                return _existing
            tui.console.print(f"WARNING: '{package_name}' already selected at {_existing.version}, cannot satisfy {_constraint} {version}")
            tui.console.warning(f"'{package_name}({_existing.version})' in selected not matching required {_constraint} {version}")
            return None


        # which package provide given package_name (pre-filtered by version if provided)
        _pkg_candidates = self.__cache.get_packages(package_name, version, constraint)

        # Early return if a candidate's real name is already in selected_pkgs
        for _pkg in _pkg_candidates:
            if _pkg['Package'] in self.selected_pkgs:
                _existing = self.selected_pkgs[_pkg['Package']]
                if not _satisfies(_existing.version):
                    tui.console.warning(f"'{package_name}({_existing.version})' in selected not matching required {_constraint} {version}")
                return _existing

        # At this point, if lookahead is available use that to select packages.
        _selected_pkg_lookahead = [pkg for pkg in _pkg_candidates if pkg['Package'] in self.__lookahead]

        # Case - I  : Incase one candidate and already in lookahead, simplified
        if len(_selected_pkg_lookahead) == 1:
            _selected_pkg = _selected_pkg_lookahead[0]

        # Case - II : No match for Package or Provides - Raise Value Error
        elif len(_pkg_candidates) == 0:
            tui.console.print(f"Dependency Check: Cant find anything that provides: {package_name}")
            return None
        
        # Case - III: One package found
        elif len(_pkg_candidates) == 1:
            _selected_pkg = _pkg_candidates[0]

        # Case - IV : Multiple candidates.  If they're all the same Package
        # name (only versions differ), auto-pick the highest — within a
        # coherent mirror set this is always correct and matches apt.
        # Prompt only when names differ (genuine alternative providers).
        elif len(_pkg_candidates) > 1:
            _auto = _auto_pick_candidate(_pkg_candidates)
            if _auto is not None:
                _selected_pkg = _auto
                tui.console.info(
                    f"auto-pick {_selected_pkg.package} {_selected_pkg.version} "
                    f"from {len(_pkg_candidates)} versions of '{package_name}'"
                )
            else:
                _mark = tui.console.mark()
                tui.console.print(f"Multiple packages satisfy '{package_name}':")
                for _i, _pkg in enumerate(_pkg_candidates, 1):
                    tui.console.print(f"  {_i}.  {_pkg.package}  ({_pkg.version})")
                _options = [str(_i) for _i in range(1, len(_pkg_candidates) + 1)]
                _choice  = Prompt(PROMPT_OPTIONS, f"Select [1-{len(_pkg_candidates)}]", _options).get_response()
                _selected_pkg = _pkg_candidates[int(_choice) - 1]
                tui.console.trim_to(_mark)
                tui.console.print(f"Multiple packages satisfy '{package_name}': Selected {_selected_pkg.package} ({_selected_pkg.version})")

        else:  # Do not know how we got here
            tui.console.error(f"Unknown Error in Parsing dependencies: {package_name}")
            return None

        # Insert BEFORE recursing: cycle protection. If A depends on B and B depends on A,
        # the early-return at the top of this function (checking selected_pkgs) only works
        # if A is already recorded here before parse_dependency(B) runs.
        self.selected_pkgs[_selected_pkg['Package']] = _selected_pkg
        
        # Also register every virtual name this package provides so that the L84 early-return
        # catches virtual-name lookups (e.g. 'awk' → gawk_pkg) without needing the L93 loop.
        for _provided_name, _ in _selected_pkg.get_provides():
            self.selected_pkgs[_provided_name] = _selected_pkg

        # list packages to get dependencies for (copy — the loop below mutates _depends,
        # aliasing _selected_pkg.depends would corrupt the Package's stored list)
        _depends = list(_selected_pkg.depends)
        
        # Pre-Depends must be satisfied before the package can unpack — treat same as Depends
        _depends += list(_selected_pkg.pre_depends)

        # Slightly more tricky how to handle alt_depends
        # Virtual names are registered in selected_pkgs at L134-135, so the name check
        # below covers both real packages and provides without a separate provides lookup.
        _alt_depends = _selected_pkg.alt_depends
        
        for _alt in _alt_depends:
            # Find alts already selected whose version constraint is also satisfied
            _selected_alt_pkg = []
            for _pkg in _alt:
                _alt_name = _pkg[0]
                if _alt_name not in self.selected_pkgs:
                    continue
                _alt_ver_str = _pkg[1]
                if not _alt_ver_str:
                    _selected_alt_pkg.append(_pkg)   # no version constraint — name match sufficient
                    continue
                _alt_op = _pkg[2] if _pkg[2] in self._VALID_CONSTRAINTS else '>='
                try:
                    if apt_pkg.check_dep(str(self.selected_pkgs[_alt_name].version), _alt_op, _alt_ver_str):
                        _selected_alt_pkg.append(_pkg)
                except Exception:
                    _selected_alt_pkg.append(_pkg)   # can't evaluate — assume satisfied

            if _selected_alt_pkg:
                # one or more already selected and satisfying — pick first, arbitrary decision
                _depends.append(_selected_alt_pkg[0])
                continue

            # No alt already selected and satisfying — default to first alternative (Debian convention)
            _depends.append(_alt[0])

        # check if we should include recommended packages
        if self.__recommended:
            _depends += _selected_pkg.recommends

        # recursively
        for _pkg in _depends:
            # Extract version info from dep tuple and build Version object safely
            _dep_ver: Optional[Version] = None
            if _pkg[1]:
                try:
                    _dep_ver = Version(_pkg[1])
                except (ValueError, TypeError):
                    tui.console.warning(f"Malformed version '{_pkg[1]}' in dep on '{_pkg[0]}', ignoring")

            _parsed_pkg = self.parse_dependency(_pkg[0], _dep_ver, _pkg[2])
            if _parsed_pkg is None:
                tui.console.print(f"WARNING: unresolved dependency '{_pkg[0]}' for {_selected_pkg.package}")
                tui.console.warning(f"parse_dependency({_pkg[0]}) from {_selected_pkg.package} returned None")
                continue

            # add forward dependency
            if _parsed_pkg.package not in _selected_pkg.depends_on:
                _selected_pkg.depends_on.append(_parsed_pkg.package)
            # add reverse dependency
            if _selected_pkg.package not in _parsed_pkg.depended_by:
                _parsed_pkg.depended_by.append(_selected_pkg.package)

            # Record version constraint in the selected package for validate_selection
            if _dep_ver is not None:
                try:
                    self.selected_pkgs[_parsed_pkg['Package']].add_constraint(_dep_ver, _pkg[2])
                except (ValueError, TypeError) as e:
                    tui.console.warning(f"Skipping invalid version constraint '{_pkg[1]}' "
                                        f"on {_parsed_pkg.package} (from {_selected_pkg.package}): {e}")

        return _selected_pkg

    def validate_selection(self) -> bool:

        # Checking breaks first
        # When one binary package declares that it breaks another, dpkg will refuse to allow the package which
        # declares Breaks to be unpacked unless the broken package is de-configured first, and it will refuse to
        # allow the broken package to be reconfigured.

        # Note: No comparator is absolute, just existence breaks, with Comparator checks if the comparator is satisfied

        _breaks = False
        for _pkg in self.selected_pkgs:
            if _pkg != self.selected_pkgs[_pkg]['Package']:
                continue
            # Breaks will still allow to install - Warning
            # Debian policy forbids alternatives in Breaks, so each group has exactly one entry at [0]
            for _break_group in self.selected_pkgs[_pkg].breaks:
                _breaks_name = _break_group[0][0]
                if _breaks_name in self.selected_pkgs:
                    # Standard Debian pattern: a package declares Provides: X and Breaks: X to
                    # satisfy X while blocking other providers. selected_pkgs registers virtual
                    # names pointing to the same Package object, so an identity check distinguishes
                    # "package breaks its own alias" (false positive) from a real break.
                    if self.selected_pkgs[_breaks_name] is self.selected_pkgs[_pkg]:
                        continue
                    _broken_obj = self.selected_pkgs[_breaks_name]
                    if _broken_obj['Package'] != _breaks_name:
                        # Provider: use the Provides version, not the provider's own version
                        _provided_ver = next(
                            (str(v) for n, v in _broken_obj.get_provides()
                             if n == _breaks_name and v is not None), None)
                        _pkg_ver = _provided_ver if _provided_ver else str(_broken_obj.version)
                    else:
                        _pkg_ver = str(_broken_obj.version)
                    _break_version = _break_group[0][1]
                    _break_comparator = _break_group[0][2]

                    # Check if it breaks
                    try:
                        _triggered = (_break_comparator == '' or
                                      apt_pkg.check_dep(_pkg_ver, _break_comparator, _break_version))
                    except Exception as e:
                        tui.console.warning(f"check_dep raised for breaks {_breaks_name} "
                                            f"({_pkg_ver} {_break_comparator} {_break_version}): {e}")
                        _triggered = True  # conservative: assume break on parse error
                    if _triggered:
                        tui.console.print(f"ERROR: Package {_pkg} breaks {_breaks_name}")
                        tui.console.error(f"DEPENDENCY HELL: {_pkg} breaks {_breaks_name} "
                                          f"(ver {_pkg_ver} {_break_comparator} {_break_version})")
                        _breaks = True

            # Conflicts will break installation - Error
            # Debian policy forbids alternatives in Conflicts, so each group has exactly one entry at [0]
            for _conflict_group in self.selected_pkgs[_pkg].conflicts:
                _conflicts_name = _conflict_group[0][0]
                if _conflicts_name in self.selected_pkgs:
                    # Same Debian pattern as Breaks above: Provides: X + Conflicts: X means
                    # "I am X and nothing else can be X". Not a real conflict with another package.
                    if self.selected_pkgs[_conflicts_name] is self.selected_pkgs[_pkg]:
                        continue
                    _conflict_obj = self.selected_pkgs[_conflicts_name]
                    if _conflict_obj['Package'] != _conflicts_name:
                        _provided_ver = next(
                            (str(v) for n, v in _conflict_obj.get_provides()
                             if n == _conflicts_name and v is not None), None)
                        _pkg_ver = _provided_ver if _provided_ver else str(_conflict_obj.version)
                    else:
                        _pkg_ver = str(_conflict_obj.version)
                    _conflict_version = _conflict_group[0][1]
                    _conflict_comparator = _conflict_group[0][2]

                    # Check if conflicts
                    try:
                        _triggered = (_conflict_comparator == '' or
                                      apt_pkg.check_dep(_pkg_ver, _conflict_comparator, _conflict_version))
                    except Exception as e:
                        tui.console.warning(f"check_dep raised for conflicts {_conflicts_name} "
                                            f"({_pkg_ver} {_conflict_comparator} {_conflict_version}): {e}")
                        _triggered = True  # conservative: assume conflict on parse error
                    if _triggered:
                        tui.console.print(f"ERROR: Package {_pkg} conflicts with {_conflicts_name}")
                        tui.console.error(f"DEPENDENCY HELL: {_pkg} conflicts with {_conflicts_name} "
                                          f"(ver {_pkg_ver} {_conflict_comparator} {_conflict_version})")
                        _breaks = True

            # Check for package version constraints collected from upstream
            if not self.selected_pkgs[_pkg].constraints_satisfied:
                tui.console.print(f"ERROR: Package {_pkg} version constraints unsatisfied")
                tui.console.error(f"DEPENDENCY HELL: {_pkg} version constraints unsatisfied")
                _breaks = True

            # Check Alt Depends
            for _section in self.selected_pkgs[_pkg].alt_depends:
                _found = False

                for pkg in _section:
                    # if one has been satisfied, don't bother with others - May have to check logic holds
                    if _found:
                        break
                    pkg_name = pkg[0]
                    # Simpler is Package in Selected Package Name
                    if pkg_name in self.selected_pkgs:
                        pkg_version = pkg[1]
                        pkg_constraint = pkg[2]
                        # Empty comparator = no version constraint, name-match alone satisfies (matches Breaks/Conflicts pattern)
                        try:
                            _satisfies = (pkg_constraint == '' or
                                          apt_pkg.check_dep(str(self.selected_pkgs[pkg_name].version),
                                                            pkg_constraint, pkg_version))
                        except Exception as e:
                            tui.console.warning(f"check_dep raised for {pkg_name} "
                                                f"({self.selected_pkgs[pkg_name].version} {pkg_constraint} {pkg_version}): {e}")
                            _satisfies = False
                        if _satisfies:
                            _found = True
                        else:
                            tui.console.warning(f"Alt-dep version constraint failed for {pkg_name} "
                                                f"({self.selected_pkgs[pkg_name].version} {pkg_constraint} {pkg_version})")
                    else:
                        # Lets try in Provides, little more complex
                        _provides_options = self.__cache.get_packages(pkg_name)
                        _pkg_names = [_pkg['Package'] for _pkg in _provides_options
                                      if _pkg['Package'] in self.selected_pkgs]
                        # Tricky - can be more than one package that don't conflict with each other.
                        # e.g. awk can be provided by both mawk & gawk without conflict.
                        if len(_pkg_names) > 0:
                            for _pkg_name in _pkg_names:
                                pkg_version = pkg[1]
                                pkg_constraint = pkg[2]
                                # Empty comparator = no version constraint, name-match alone satisfies
                                try:
                                    _satisfies = (pkg_constraint == '' or
                                                  apt_pkg.check_dep(str(self.selected_pkgs[_pkg_name].version),
                                                                    pkg_constraint, pkg_version))
                                except Exception as e:
                                    tui.console.warning(f"check_dep raised for {_pkg_name} "
                                                        f"({self.selected_pkgs[_pkg_name].version} "
                                                        f"{pkg_constraint} {pkg_version}): {e}")
                                    _satisfies = False
                                if _satisfies:
                                    _found = True
                                else:
                                    tui.console.warning(f"Alt-dep (via provides) version constraint failed for "
                                                        f"{_pkg_name} ({self.selected_pkgs[_pkg_name].version} "
                                                        f"{pkg_constraint} {pkg_version})")

                if not _found:
                    tui.console.print(f"ERROR: unresolved alt-dependency for package {_pkg}")
                    tui.console.error(f"DEPENDENCY HELL: {_pkg} unresolved alt-dep section: {_section}")
                    _breaks = True

        return not _breaks

    def parse_sources(self) -> bool:
        _found = True
        # Build (source_name, source_version, Package) tuples. Skip packages whose
        # python-debian .source / .source_version properties raise on malformed data.
        _src_list = []
        for _pkg in self.selected_pkgs:
            if _pkg != self.selected_pkgs[_pkg]['Package']:
                continue
            try:
                _src_list.append((self.selected_pkgs[_pkg].source,
                                  self.selected_pkgs[_pkg].source_version,
                                  self.selected_pkgs[_pkg]))
            except Exception as e:
                tui.console.print(f"WARNING: cannot read source info for {_pkg}, skipping")
                tui.console.error(f"parse_sources source-access for {_pkg}: {type(e).__name__}: {e}")
                _found = False
        for _src in _src_list:
            _src_name = _src[0]
            _bin_pkg   = _src[2]
            if _src_name not in self.selected_srcs:
                _src_version = _src[1]

                if _src_name not in self.__cache.source_hashtable:
                    tui.console.print(f"ERROR: Source package '{_src_name}' not found in cache")
                    tui.console.error(f"CRITICAL: Source '{_src_name}' not found in cache — build cannot proceed")
                    _found = False
                    continue

                _src_candidates = self.__cache.source_hashtable[_src_name]
                if len(_src_candidates) == 1:
                    self.selected_srcs[_src_name] = _src_candidates[0]
                else:
                    _selected_pkg = [_pkg for _pkg in _src_candidates if _pkg.version == Version(_src_version)]
                    if len(_selected_pkg) == 1:
                        self.selected_srcs[_src_name] = _selected_pkg[0]
                    else:
                        tui.console.print(f"ERROR: Source '{_src_name}' version '{_src_version}' not found")
                        tui.console.error(f"Source '{_src_name}' version '{_src_version}' not found in cache")
                        _found = False
                        continue

                self.selected_srcs[_src_name].pkgs = []

            _bin_filename = (_bin_pkg.get('Filename') or '').rsplit('/', 1)[-1]
            if _bin_filename:
                # Strip binNMU suffix (+bN) — dpkg-buildpackage produces files using
                # the source version, which never carries the +bN rebuild suffix.
                # Lookahead anchors to the arch field so +deb12uN and other legitimate
                # upstream suffixes (which don't end with +b\d+ immediately before _arch)
                # are left untouched.
                _bin_filename = re.sub(r'\+b\d+(?=_\w+\.u?deb$)', '', _bin_filename)
                if _bin_filename not in self.selected_srcs[_src_name].pkgs:
                    self.selected_srcs[_src_name].pkgs.append(_bin_filename)

        tui.console.warning(f"parse_sources: selected {len(self.selected_srcs)} source packages")
        return _found

    @property
    def download_size(self):
        _download_size = 0
        for _pkg in self.selected_srcs:
            _download_size += self.selected_srcs[_pkg].download_size
        return _download_size
