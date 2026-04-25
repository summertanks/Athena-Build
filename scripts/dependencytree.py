# Internal modules
import re
from typing import Dict, List

import package
from package import Version
from cache import Cache

# External Modules
import apt_pkg

import tui
from tui import Prompt, PROMPT_OPTIONS


class DependencyTree:

    def __init__(self, cache: Cache, select_recommended: bool, arch: str, lookahead=None):

        self.__recommended = select_recommended
        self.__cache = cache
        self.__lookahead: List[str] = []

        self.selected_pkgs: Dict[str, package.Package] = {}
        self.selected_srcs: Dict[str, package.Source]  = {}
        self.arch = arch

        if lookahead is not None:
            self.__lookahead = lookahead

    def add_lookahead(self, lookahead: List[str]):
        for _pkg in lookahead:
            if _pkg and not _pkg.isspace():
                _pkg = _pkg.strip()
                if _pkg not in self.__lookahead:
                    self.__lookahead.append(_pkg)

    def parse_dependency(self, required_pkg: str) -> package.Package:

        if not required_pkg:
            raise ValueError("Dependency asked for empty package name")

        # Not checking for package in selected packages here - since dependency may be satisfied by provides
        # Since search is hashed, hoping another search is trivial
        _provide_candidates = []
        _pkg_candidates = []
        _selected_pkg: package.Package

        # TODO: enable required package to specify version also

        # select packages / provides
        _pkg_candidates = self.__cache.get_packages(required_pkg)
        _provide_candidates = self.__cache.get_provides(required_pkg)

        # Slightly more complex than it needs to be, we have to check for both package & provides
        # Checking from Package Name
        if required_pkg in self.selected_pkgs:
            return self.selected_pkgs[required_pkg]
        # Checking Provides Name
        for _pkg in _provide_candidates:
            if _pkg['Package'] in self.selected_pkgs:
                # return _pkg   # Original: returned the provides candidate directly
                return self.selected_pkgs[_pkg['Package']]  # canonical instance, safer if ever multiple

        # At this point, if lookahead is available use that to select packages.
        # i.e. required_package list may clear ambiguity, but only for provides
        # since package disambiguation withing itself will require version details
        _selected_pkg_lookahead = [__pkg for __pkg in _pkg_candidates if __pkg['Package'] in self.__lookahead]
        _selected_pkg_lookahead += [__pkg for __pkg in _provide_candidates if __pkg['Package'] in self.__lookahead]

        # Pick the name if there is ONLY ONE commonality
        # could be as situation that the required package list may have (by mistake) two packages for same provides
        if len(_selected_pkg_lookahead) == 1:
            _selected_pkg = _selected_pkg_lookahead[0]
            # tui.console.print(f"Lookahead Selection of {_selected_pkg['Package']} for {required_pkg}")

        # Case - I  : No match for Package or Provides - Raise Value Error
        elif len(_provide_candidates) == 0 and len(_pkg_candidates) == 0:
            raise ValueError(f"Package could not be found: {required_pkg}")

        # Case - II : Situation with both Multiple Package Versions and Provides. Not sure if its handleable
        elif len(_provide_candidates) > 1 and len(_pkg_candidates) > 1:
            raise ValueError(f"Situation with both Multiple Package Versions and Provides: {required_pkg}")

        # Case - III: No Package, One Provides - "Selecting <Package> for <Provides> - Proceed with Package
        elif len(_provide_candidates) == 1 and len(_pkg_candidates) == 0:
            tui.console.print(f"Note: Selecting {_provide_candidates[0]['Package']} for {required_pkg}")
            _selected_pkg = _provide_candidates[0]

        # Case - IV : One Package, No Provides - Simplest, move ahead parsing the given package
        elif len(_provide_candidates) == 0 and len(_pkg_candidates) == 1:
            _selected_pkg = _pkg_candidates[0]

        # Case -  V : Multiple Package, No Provides - Ask User to select based on version
        elif len(_provide_candidates) == 0 and len(_pkg_candidates) > 1:
            _options = [__pkg['Version'] for __pkg in _pkg_candidates]
            _pkg_version = Prompt(PROMPT_OPTIONS,
                                  f"Multiple Package for {required_pkg}, select Version",
                                  _options).get_response()
            _index = _options.index(_pkg_version)
            _selected_pkg = _pkg_candidates[_index]

        # Case - VI : No Package, Multiple Provides (different Packages) - Ask User to manually select
        # Boundary condition - Multiple provides are from same package of different versions
        elif len(_provide_candidates) > 1 and len(_pkg_candidates) == 0:
            _options = [__pkg['Package'] for __pkg in _provide_candidates]
            _pkg_name = Prompt(PROMPT_OPTIONS,
                               f"Multiple provides for {required_pkg}, select Package",
                               _options).get_response()
            _index = _options.index(_pkg_name)
            _selected_pkg = _provide_candidates[_index]

        # Case - VII: Situation where have one or more provides and package
        elif len(_provide_candidates) > 0 and len(_pkg_candidates) > 0:
            # Unclear situation - show all package options to user - let them figure it out
            _options = []
            _options += [__pkg['Package'] for __pkg in _pkg_candidates]
            _options += [__pkg['Package'] for __pkg in _provide_candidates]
            _pkg_name = Prompt(PROMPT_OPTIONS,
                               f"Multiple provides for {required_pkg}, select Package",
                               _options).get_response()
            _index = _options.index(_pkg_name)
            if _index > len(_pkg_candidates) - 1:
                _selected_pkg = _provide_candidates[_index - len(_pkg_candidates)]
            else:
                _selected_pkg = _pkg_candidates[_index]

        else:  # Do not know how we got here
            raise ValueError(f"Unknown Error in Parsing dependencies: {required_pkg}")

        # Insert BEFORE recursing: cycle protection. If A depends on B and B depends on A,
        # the early-return at the top of this function (checking selected_pkgs) only works
        # if A is already recorded here before parse_dependency(B) runs.
        self.selected_pkgs[_selected_pkg['Package']] = _selected_pkg

        # list packages to get dependencies for (copy — the loop below mutates _depends,
        # aliasing _selected_pkg.depends would corrupt the Package's stored list)
        _depends = list(_selected_pkg.depends)

        # Slightly more tricky how to handle alt_depends
        _alt_depends = _selected_pkg.alt_depends
        for _alt in _alt_depends:
            # Check if one of them already in out selected list
            _selected_alt_pkg = [_pkg for _pkg in _alt if _pkg[0] in self.selected_pkgs]
            if len(_selected_alt_pkg) > 0:
                # if one or more select first - arbitrary decision
                _depends.append(_selected_alt_pkg[0])
                # on to the next
                continue

            # Also check if an alt is already satisfied via provides — avoids needless recursion
            # (e.g. 'awk | ed' where mawk is selected and provides awk)
            _provided_alt = None
            for _alt_tuple in _alt:
                _providers = self.__cache.get_provides(_alt_tuple[0])
                if any(p['Package'] in self.selected_pkgs for p in _providers):
                    _provided_alt = _alt_tuple
                    break
            if _provided_alt is not None:
                _depends.append(_provided_alt)
                continue

            # Again arbitrary decision picking the first but documentation supports the idea
            _depends.append(_alt[0])

        # check if we should include recommended packages
        if self.__recommended:
            _depends += _selected_pkg.recommends

        # recursively
        for _pkg in _depends:
            # Catch ValueError from nested parse_dependency (unfound/ambiguous/unknown cases).
            # Skip the bad dep, keep resolving siblings — don't let one missing package kill the whole tree.
            try:
                _parsed_pkg = self.parse_dependency(_pkg[0])
            except ValueError as e:
                tui.console.print(f"WARNING: unresolved dependency '{_pkg[0]}' for {_selected_pkg.package}")
                tui.console.error(f"parse_dependency({_pkg[0]}) from {_selected_pkg.package}: {e}")
                continue

            # add forward dependency
            if _parsed_pkg.package not in _selected_pkg.depends_on:
                _selected_pkg.depends_on.append(_parsed_pkg.package)
            # add reverse dependency
            if _selected_pkg.package not in _parsed_pkg.depended_by:
                _parsed_pkg.depended_by.append(_selected_pkg.package)

            # add version constraints
            # Again slightly convoluted, Between multiple package and provides, don't know which was selected.
            # Hence, expecting parse_dependency(...) to return the package selected for that required_pkg
            if _pkg[1]:
                try:
                    self.selected_pkgs[_parsed_pkg['Package']].add_constraint(Version(_pkg[1]), _pkg[2])
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
            # Breaks will still allow to install - Warning
            # Debian policy forbids alternatives in Breaks, so each group has exactly one entry at [0]
            for _break_group in self.selected_pkgs[_pkg].breaks:
                _breaks_name = _break_group[0][0]
                if _breaks_name in self.selected_pkgs:
                    _pkg_ver = str(self.selected_pkgs[_breaks_name].version)
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
                    _pkg_ver = str(self.selected_pkgs[_conflicts_name].version)
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
                        _provides_options = self.__cache.get_provides(pkg_name)
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
            if _src_name not in self.selected_srcs:
                _src_version = _src[1]

                if _src_name not in self.__cache.source_hashtable:
                    tui.console.print(f"ERROR: Source package '{_src_name}' not found in cache")
                    tui.console.error(f"CRITICAL: Source '{_src_name}' not found in cache — build cannot proceed")
                    _found = False
                    continue

                _src_candidates = self.__cache.source_hashtable[_src_name]
                # If single entry its simple
                if len(_src_candidates) == 1:
                    self.selected_srcs[_src_name] = _src_candidates[0]
                # If more than one, differentiate on version
                else:
                    _selected_pkg = [_pkg for _pkg in _src_candidates if _pkg.version == _src_version]
                    if len(_selected_pkg) == 1:
                        self.selected_srcs[_src_name] = _selected_pkg[0]
                    else:
                        tui.console.print(f"ERROR: Source '{_src_name}' version '{_src_version}' not found")
                        tui.console.error(f"Source '{_src_name}' version '{_src_version}' not found in cache")
                        _found = False
                        continue

            # ideally the following should have been sufficient
            # self.selected_srcs[_src_name].pkgs.append(os.path.basename(self.selected_pkgs[_pkg_name]['Filename']))
            # but there are some +deb11ux issues that are not getting addressed
            _raw_pkg_list = (self.selected_srcs[_src_name].get('Package-List') or '').strip()
            if not _raw_pkg_list:
                continue
            _pkg_list = _raw_pkg_list.split('\n')
            for _pkg in _pkg_list:
                _pkg = _pkg.split()

                # Malformed / empty — need at least name + type (deb/udeb) to build a filename
                if len(_pkg) < 2:
                    continue

                # Check if the Package matches
                if _pkg[0] != _src[2]['Package']:
                    continue

                # Package-List fields 5+ are optional key=value pairs (arch, profile, essential, ...)
                # in arbitrary order — scan for the 'arch=' field, fall back to self.arch if absent.
                _arch_field = next((f for f in _pkg[4:] if f.startswith('arch=')), None)

                # No arch info - assume it's the same as self.arch (by virtue of control file architecture)
                if _arch_field is None:
                    _arch = self.arch

                # Select from the list
                else:
                    # Original — reused `_arch` as string, then list, then string:
                    # _arch = _arch_field.split('=', 1)[1]
                    # _arch = _arch.split(',')
                    # _arch_type = [self.arch, 'any', 'linux-any', 'any-' + self.arch]
                    # _selected_arch = [__arch for __arch in _arch_type if __arch in _arch]
                    # if len(_selected_arch) > 0:
                    #     _arch = self.arch
                    # elif 'all' in _arch:
                    #     _arch = 'all'
                    # else:
                    #     continue
                    _arch_list = _arch_field.split('=', 1)[1].split(',')
                    _arch_type = [self.arch, 'any', 'linux-any', 'any-' + self.arch]
                    _selected_arch = [__arch for __arch in _arch_type if __arch in _arch_list]
                    if len(_selected_arch) > 0:
                        _arch = self.arch
                    elif 'all' in _arch_list:
                        _arch = 'all'
                    else:
                        # not for the arch we need, skip
                        continue

                _version = str(_src[2].version).split(':')
                if len(_version) > 1:
                    _version = _version[1]
                else:
                    _version = _version[0]

                # stripping build revisions, because these do not reflect on source code builds
                _version = re.sub(r"\+b\d+$", "", _version)

                # Now that the arch has been established,
                self.selected_srcs[_src_name].pkgs.append(
                    _src[2]['Package'] + '_' + _version + '_' + _arch + '.' + _pkg[1])

                # If we are we matched, there should be another match withing the same package list, lets break
                break

        tui.console.print(f"Selected {len(self.selected_srcs)} Source Package")
        return _found

    @property
    def download_size(self):
        _download_size = 0
        for _pkg in self.selected_srcs:
            _download_size += self.selected_srcs[_pkg].download_size
        return _download_size
