# Internal modules
import os.path

import package
from cache import Cache

# External Modules
import apt_pkg
from rich.prompt import Prompt

Print = print


class DependencyTree:

    def __init__(self, cache: Cache, select_recommended: bool, arch: str, lookahead=None):

        self.__recommended = select_recommended
        self.__cache = cache
        self.__lookahead = []

        self.selected_pkgs: {} = {}
        self.selected_srcs: {} = {}
        self.alternate_pkgs: {} = {}
        self.arch = arch

        if lookahead is not None:
            self.__lookahead = lookahead

    def add_lookahead(self, lookahead: []):
        for _pkg in lookahead:
            if _pkg and not _pkg.isspace():
                _pkg = _pkg.strip()
                if _pkg not in self.__lookahead:
                    self.__lookahead.append(_pkg)

    def parse_dependency(self, required_pkg: str) -> package.Package:

        assert required_pkg != '', "Dependency asked for empty package name"

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
                return _pkg

        # At this point, if lookahead is available use that to select packages.
        # i.e. required_package list may clear ambiguity, but only for provides
        # since package disambiguation withing itself will require version details
        _selected_pkg_lookahead = [__pkg for __pkg in _pkg_candidates if __pkg['Package'] in self.__lookahead]
        _selected_pkg_lookahead += [__pkg for __pkg in _provide_candidates if __pkg['Package'] in self.__lookahead]

        # Pick the name if there is ONLY ONE commonality
        # could be as situation that the required package list may have (by mistake) two packages for same provides
        if len(_selected_pkg_lookahead) == 1:
            _selected_pkg = _selected_pkg_lookahead[0]
            # Print(f"Lookahead Selection of {_selected_pkg['Package']} for {required_pkg}")

        # Case - I  : No match for Package or Provides - Raise Value Error
        elif len(_provide_candidates) == 0 and len(_pkg_candidates) == 0:
            raise ValueError(f"Package could not be found: {required_pkg}")

        # Case - II : Situation with both Multiple Package Versions and Provides. Not sure if its handleable
        elif len(_provide_candidates) > 1 and len(_pkg_candidates) > 1:
            raise ValueError(f"Situation with both Multiple Package Versions and Provides: {required_pkg}")

        # Case - III: No Package, One Provides - "Selecting <Package> for <Provides> - Proceed with Package
        elif len(_provide_candidates) == 1 and len(_pkg_candidates) == 0:
            Print(f"Note: Selecting {_provide_candidates[0]['Package']} for {required_pkg}")
            _selected_pkg = _provide_candidates[0]

        # Case - IV : One Package, No Provides - Simplest, move ahead parsing the given package
        elif len(_provide_candidates) == 0 and len(_pkg_candidates) == 1:
            _selected_pkg = _pkg_candidates[0]

        # Case -  V : Multiple Package, No Provides - Ask User to select based on version
        elif len(_provide_candidates) == 0 and len(_pkg_candidates) > 1:
            _options = [__pkg['Version'] for __pkg in _pkg_candidates]
            _pkg_version = Prompt.ask(f"Multiple Package for {required_pkg}, select Version", choices=_options)
            _index = _options.index(_pkg_version)
            _selected_pkg = _pkg_candidates[_index]

        # Case - VI : No Package, Multiple Provides (different Packages) - Ask User to manually select
        # Boundary condition - Multiple provides are from same package of different versions
        elif len(_provide_candidates) > 1 and len(_pkg_candidates) == 0:
            _options = [__pkg['Package'] for __pkg in _provide_candidates]
            _pkg_name = Prompt.ask(f"Multiple provides for {required_pkg}, select Package", choices=_options)
            _index = _options.index(_pkg_name)
            _selected_pkg = _provide_candidates[_index]

        # Case - VII: Situation where have one or more provides and package
        elif len(_provide_candidates) > 0 and len(_pkg_candidates) > 0:
            # Unclear situation - show all package options to user - let them figure it out
            _options = []
            _options += [__pkg['Package'] for __pkg in _pkg_candidates]
            _options += [__pkg['Package'] for __pkg in _provide_candidates]
            _pkg_name = Prompt.ask(f"Multiple provides for {required_pkg}, select Package", choices=_options)
            _index = _options.index(_pkg_name)
            if _index > len(_pkg_candidates) - 1:
                _selected_pkg = _provide_candidates[len(_pkg_candidates) - 1]
            else:
                _selected_pkg = _pkg_candidates[_index]

        else:  # Do not know how we got here
            raise ValueError(f"Unknown Error in Parsing dependencies: {required_pkg}")

        # We have the selected package in __selected_pkg, adding to internal list
        self.selected_pkgs[_selected_pkg['Package']] = _selected_pkg

        # list packages to get dependencies for
        _depends = _selected_pkg.depends

        # Slightly more tricky how to handle alt_depends
        _alt_depends = _selected_pkg.alt_depends
        for _alt in _alt_depends:
            # Check if one of them already in out selected list
            _selected_alt_pkg = [_pkg for _pkg in _alt if _pkg[0] in self.selected_pkgs]
            if len(_selected_alt_pkg) > 0:
                # if one or more select first - arbitrary decision
                _depends += {_selected_alt_pkg[0]}
                # on to the next
                continue
            else:
                # Again arbitrary decision picking the first but documentation supports the idea
                _depends += {_alt[0]}

        # check if we should include recommended packages
        if self.__recommended:
            _depends += _selected_pkg.recommends

        # recursively
        for _pkg in _depends:
            _parsed_pkg = self.parse_dependency(_pkg[0])

            # add forward dependency
            if _parsed_pkg.package not in _selected_pkg.depends_on:
                _selected_pkg.depends_on.append(_parsed_pkg.package)
            # add reverse dependency
            if _selected_pkg.package not in _parsed_pkg.depended_by:
                _parsed_pkg.depended_by.append(_selected_pkg.package)

            # add version constraints
            # Again slightly convoluted, Between multiple package and provides, don't know which was selected.
            # Hence, expecting parse_dependency(...) to return the package selected for that required_pkg
            self.selected_pkgs[_parsed_pkg['Package']].add_version_constraint(_pkg[1], _pkg[2])

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
            for breaks in self.selected_pkgs[_pkg].breaks:
                _breaks_name = breaks[0][0]
                if _breaks_name in self.selected_pkgs:
                    _pkg_ver = self.selected_pkgs[_breaks_name].version
                    _break_version = breaks[0][1]
                    _break_comparator = breaks[0][2]

                    # Check if it breaks
                    if _break_comparator == '' or \
                            apt_pkg.check_dep(_pkg_ver, _break_comparator, _break_version):
                        Print(f"DEPENDENCY HELL: Package {_pkg} breaks {_breaks_name}")
                        _breaks = True

            # Conflicts will break installation - Error
            for conflicts in self.selected_pkgs[_pkg].conflicts:
                _conflicts_name = conflicts[0][0]
                if _conflicts_name in self.selected_pkgs:
                    _pkg_ver = self.selected_pkgs[_conflicts_name].version
                    _conflict_version = conflicts[0][1]
                    _conflict_comparator = conflicts[0][2]

                    # Check if conflicts
                    if _conflict_comparator == '' or \
                            apt_pkg.check_dep(_pkg_ver, _conflict_comparator, _conflict_version):
                        Print(f"DEPENDENCY HELL: Package {_pkg} conflicts with {_conflicts_name}")
                        _breaks = True

            # Check for package version constraints collected from upstream
            if not self.selected_pkgs[_pkg].constraints_satisfied:
                Print(f"DEPENDENCY HELL: Package {_pkg} version constrains unsatisfied")
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
                        if apt_pkg.check_dep(self.selected_pkgs[pkg_name].version, pkg_constraint, pkg_version):
                            _found = True
                        else:
                            Print(f"Alt Dependency Check - Version constraint failed for {pkg_name}")
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
                                if apt_pkg.check_dep(self.selected_pkgs[_pkg_name].version,
                                                     pkg_constraint, pkg_version):
                                    _found = True
                                else:
                                    Print(f"Alt Dependency Check - Version constraint failed for {_pkg_name}")

                if not _found:
                    Print(f"dependency unresolved between {_section}")

        return not _breaks

    def parse_sources(self) -> bool:
        import re

        _found = True
        _src_list = [(self.selected_pkgs[_pkg].source,
                      self.selected_pkgs[_pkg].source_version,
                      self.selected_pkgs[_pkg]) for _pkg in self.selected_pkgs]
        for _src in _src_list:
            _src_name = _src[0]
            if _src_name not in self.selected_srcs:
                _src_version = _src[1]

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
                        Print(f"ERROR: Not found source for {_src_list}")
                        _found = False

            # ideally the following should have been sufficient
            # self.selected_srcs[_src_name].pkgs.append(os.path.basename(self.selected_pkgs[_pkg_name]['Filename']))
            # but there are some +deb11ux issues that are not getting addressed
            _pkg_list = self.selected_srcs[_src_name]['Package-List'].split('\n')
            for _pkg in _pkg_list:
                _pkg = _pkg.split()

                # empty - Skip
                if len(_pkg) == 0:
                    continue

                # Check if the Package matches
                if _pkg[0] != _src[2]['Package']:
                    continue

                # No arch info - assume it's the same as self.arch (by virtue of control file architecture)
                if len(_pkg) < 5:
                    _arch = self.arch

                # Select from the list
                else:
                    _arch = _pkg[4].split('=')[1]
                    _arch = _arch.split(',')
                    _arch_type = [self.arch, 'any', 'linux-any', 'any-' + self.arch]
                    _selected_arch = [__arch for __arch in _arch_type if __arch in _arch]
                    if len(_selected_arch) > 0:
                        _arch = self.arch
                    elif 'all' in _arch:
                        _arch = 'all'
                    else:
                        # not for the arch we need, skip
                        continue

                _version = _src[2].version.split(':')
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

        Print(f"Selected {len(self.selected_srcs)} Source Package")
        return _found

    @property
    def download_size(self):
        _download_size = 0
        for _pkg in self.selected_srcs:
            _download_size += self.selected_srcs[_pkg].download_size
        return _download_size
