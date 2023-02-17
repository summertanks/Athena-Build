# Internal modules
import utils
import deb822
import package

# External Modules
from rich.prompt import Prompt

Print = print


class DependencyTree:

    def __init__(self, pkg_filename: str, src_filename: str, select_recommended: bool, arch: str):
        self.__pkg_filename = pkg_filename
        self.__src_filename = src_filename
        self.__recommended = select_recommended

        self.package_record = utils.readfile(self.__pkg_filename).split('\n\n')
        self.source_records = utils.readfile(self.__src_filename).split('\n\n')
        self.selected_pkgs: {} = {}
        self.alternate_pkgs: {} = {}
        self.required_pkgs: [] = []

        self.arch = arch

    def parse_dependency(self, required_pkg: str, status):

        if required_pkg not in self.selected_pkgs:
            _provide_candidates = []
            _pkg_candidates = []
            _selected_pkg: package.Package

            # TODO: enable required package to specify version also

            # iterate through the package records
            for _pkg_record in self.package_record:
                if _pkg_record.strip() == '':
                    continue
                _pkg = package.Package(_pkg_record, self.arch)
                # search for packages
                if required_pkg == _pkg.package:
                    _pkg_candidates.append(_pkg)
                elif _pkg.does_provide(required_pkg):
                    _provide_candidates.append(_pkg)

            # Case - I  : No match for Package or Provides - Raise Value Error
            if len(_provide_candidates) == 0 and len(_pkg_candidates) == 0:
                raise ValueError(f"Package could not be found: {required_pkg}")

            # Case - II : One or more Package, One or more Provides - Unknown condition, currently raise error
            elif len(_provide_candidates) > 0 and len(_pkg_candidates) > 0:
                raise ValueError(f"Exact package could not selected: {required_pkg}")

            # Case - III: No Package, One Provides - "Selecting <Package> for <Provides> - Proceed with Package
            elif len(_provide_candidates) == 1 and len(_pkg_candidates) == 0:
                Print(f"Note: Selecting {_provide_candidates[0]['Package']} for {required_pkg}")
                _selected_pkg = _provide_candidates[0]

            # Case - IV : No Package, Multiple Provides (different Packages) - Ask User to manually select
            # TODO: Look forward, see if required package list already solves the problem
            elif len(_provide_candidates) > 1 and len(_pkg_candidates) == 0:
                _options = [_pkg['Package'] for _pkg in _provide_candidates]
                _pkg = Prompt.ask(f"Multiple provides for {required_pkg}, select Package", choices=_options)
                _index = _options.index(_pkg)
                _selected_pkg = _provide_candidates[_index]

            # Case - V  : Multiple Package, No Provides - Select based on version, if still more than one, ask user
            elif len(_provide_candidates) == 0 and len(_pkg_candidates) > 1:
                _options = [_pkg['Version'] for _pkg in _pkg_candidates]
                _pkg = Prompt.ask(f"Multiple Package for {required_pkg}, select Version", choices=_options)
                _index = _options.index(_pkg)
                _selected_pkg = _pkg_candidates[_index]

            # Case - VI : One Package, No Provides - Simplest, move ahead parsing the given package
            elif len(_provide_candidates) == 0 and len(_pkg_candidates) == 1:
                _selected_pkg = _pkg_candidates[0]

            else:  # Do not know how we got here
                raise ValueError(f"Unknown Error in Parsing dependencies: {required_pkg}")

            # We have the selected package in _selected_pkg, adding to internal list
            self.selected_pkgs[_selected_pkg['Package']] = _selected_pkg

            # list packages to get dependencies for
            _depends = _selected_pkg.depends

            # check if we should include recommended packages
            if self.__recommended:
                _depends += _selected_pkg.recommends

            # recursively
            for _pkg in _depends:
                status.update(f"Selected {len(self.selected_pkgs)} Packages")
                self.parse_dependency(_pkg[0], status)
                self.selected_pkgs[_pkg[0]].add_version_constraint(_pkg[1], _pkg[2])
