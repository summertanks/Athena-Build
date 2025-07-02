# (C) Athena Linux Project

# External imports

import os
import shutil
import cache

import apt_pkg
from rich.prompt import Confirm

# Local imports
import utils
from utils import BuildConfig
from cache import Cache

import buildcontainer
import dependencytree
import buildsystem
import tui
import signal

# from tui import Tui, Print, Prompt, Spinner, ProgressBar, Pause, Exit

asciiart_logo = '╔══╦╗╔╗─────────╔╗╔╗\n' \
                '║╔╗║╚╣╚╦═╦═╦╦═╗─║║╠╬═╦╦╦╦╦╗\n' \
                '║╠╣║╔╣║║╩╣║║║╬╚╗║╚╣║║║║║╠║╣\n' \
                '╚╝╚╩═╩╩╩═╩╩═╩══╝╚═╩╩╩═╩═╩╩╝'

# TODO: make all apt_pkg.parse functions arch specific



build_config: BuildConfig  # module-level variable

def main(banner: str):
    """main - the primary function being called"""
    from tui import Tui, Print, Prompt, Spinner, ProgressBar, Pause, Exit

    # Config
    global build_config
    
    # Set up the TUI system
    _tui = Tui(banner)
    _tui.run()  # Start the TUI event loop

    # Register the signal handler for SIGINT (Ctrl+C)
    signal.signal(signal.SIGINT, _tui.sig_shutdown)
    
    import tui
    
    # Assign the TUI functions to the global namespace
    tui.Print = _tui.print
    tui.Prompt = _tui.prompt
    tui.Spinner = _tui.spinner
    tui.ProgressBar = _tui.progressbar
    tui.Pause = _tui.pause
    tui.Exit = _tui.exit


    # Exit function to handle cleanup and exit
    # This function is called when the script is exiting, either normally or due to an error
    # It ensures that the TUI is properly exited and then calls exit with the provided error code
    # If no error code is provided, it defaults to 0 (indicating success)
    def Exit(err_code: int = 0):
        """Exit function to handle cleanup and exit"""
        _tui.exit(err_code)
        exit(err_code)

    # External modules initialisation
    tui.Print("Initialising apt_pkg...")
    apt_pkg.init_system()

    tui.Print("Parsing config...")
    build_config = BuildConfig()

    if not build_config.is_valid:
        tui.Print(f"Error: build configuration - {build_config.error_str}")
        Exit(1)


    # --------------------------------------------------------------------------------------------------------------
    # Setting up common systems
    # dir_list = utils.DirectoryListing( build_config.working_dir, config_parser)
    # base_distribution = utils.BaseDistribution(baseurl, baseid, basecodename, baseversion, arch)

    # log_format = "%(message)s"
    # logging.basicConfig(level="INFO", format=log_format, datefmt="[%X]", handlers=[RichHandler()])
    # logger = logging.getLogger('rich')

    # --------------------------------------------------------------------------------------------------------------
    tui.Print(asciiart_logo)
    tui.Print("Starting Source Build System for Athena Linux...")
    tui.Print("Building for ...")
    tui.Print(f"\t Arch\t\t\t{build_config.arch}")
    tui.Print(f"\t Parent Distribution\t{build_config.basecodename} {build_config.baseversion}")
    tui.Print(f"\t Build Distribution\t{build_config.build_codename} {build_config.build_version}")


    # --------------------------------------------------------------------------------------------------------------
    # Step I - Building Cache
    tui.Print("Building Cache...")
    build_cache = cache.Cache(build_config)
    _tui.wait()
    Exit(0)

    # Special case - if gcc-10 already selected, e.g. both gcc-9-base & gcc-10-base are marked required
    gcc_versions = [pkg for pkg in build_cache.required if pkg.startswith('gcc-')]
    latest_gcc_versions = sorted(gcc_versions, key=lambda x: tuple(int(num) for num in x.split('-')[1].split('.')))[-1:]
    latest_gcc = set(latest_gcc_versions)
    build_cache.required = [pkg for pkg in build_cache.required if not pkg.startswith('gcc-') or pkg in latest_gcc]

    Print(f"Required Package Count : {len(build_cache.required)}")
    Print(f"Important Package Count : {len(build_cache.important)}")
    # -------------------------------------------------------------------------------------------------------------
    # Step II - Parse Dependencies

    Print("Preparing Parsing Tree...")
    dependency_tree = dependencytree.DependencyTree(build_cache, select_recommended=False, arch=base_distribution.arch)

    required_packages = build_cache.required
    dependency_tree.add_lookahead(required_packages)
    for pkg in required_packages:
        dependency_tree.parse_dependency(pkg)
    __num_required = len(dependency_tree.selected_pkgs)
    Print(f"Dependencies Selected for 'required' : {__num_required}")

    # Cheeky but works, ideally, parsing should have identified and marked required and their dependencies as required
    for _pkg in dependency_tree.selected_pkgs:
        dependency_tree.selected_pkgs[_pkg].priority = 'required'

    # Adding 'important' packages too, not really mandatory for a bare-bones system but too much manual intervention
    # if these packages are not installed. if stable, we may look at a skimmed down manual list
    important_packages = build_cache.important
    # Option to manually add additional packages we think are important, e.g. dialog
    important_packages.extend(['dialog'])
    dependency_tree.add_lookahead(important_packages)
    for pkg in important_packages:
        dependency_tree.parse_dependency(pkg)
    Print(f"Dependencies Selected for 'important' : {len(dependency_tree.selected_pkgs) - __num_required}")

    # Similar to 'required', just that if it is not 'required' has to be important
    for _pkg in dependency_tree.selected_pkgs:
        if not dependency_tree.selected_pkgs[_pkg].priority == 'required':
            dependency_tree.selected_pkgs[_pkg].priority = 'important'

    Print(f"Parsing {args.pkg_list}...")
    required_packages_list = utils.readfile(pkglist_path).split('\n')
    for pkg in required_packages_list:
        if pkg and not pkg.startswith('#') and not pkg.isspace():
            pkg = pkg.strip()
            if pkg not in required_packages:
                required_packages.append(pkg)
    Print(f"Total Selected Packages {len(required_packages)}")

    # Iterate through package list and identify dependencies
    dependency_tree.add_lookahead(required_packages)
    for pkg in required_packages:
        dependency_tree.parse_dependency(pkg)

    Print(f"Total Dependencies Selected are : {len(dependency_tree.selected_pkgs)}")

    # -------------------------------------------------------------------------------------------------------------
    # Step III - Checking Breaks, Conflicts and version constraints
    Print("Checking Breaks and Conflicts...")
    if not dependency_tree.validate_selection():
        if not Confirm.ask("There are one or more dependency validation failures, Proceed?", default=True):
            exit(1)

    try:
        with open(os.path.join(dir_list.dir_log, 'selected_packages.list'), 'w') as f:
            for pkg in dependency_tree.selected_pkgs:
                f.write(str(dependency_tree.selected_pkgs[pkg].raw) + '\n\n')
    except (FileNotFoundError, PermissionError) as e:
        Print(f"Error: {e}")
        exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - IV Parse Source Dependencies
    Print("Parsing Source Packages...")
    if not dependency_tree.parse_sources():
        if not Confirm.ask("There are one or more source parse failures, Proceed?", default=True):
            exit(1)

    # patch to not run build tests
    for _pkg in skip_build_test:
        if _pkg in dependency_tree.selected_srcs:
            dependency_tree.selected_srcs[_pkg].skip_test = True

    # iterate over packages as see if we have any patches on our end
    for _pkg in dependency_tree.selected_srcs:
        _patch_path = os.path.join(dir_list.dir_patch_source, _pkg)
        _patch_path = os.path.join(_patch_path, dependency_tree.selected_srcs[_pkg].version)
        if os.path.exists(_patch_path):
            _patch_files = [f for f in os.listdir(_patch_path) if f.endswith('.patch')]
            _sorted_patch_files = sorted(_patch_files, key=lambda x: x[:5])
            dependency_tree.selected_srcs[_pkg].patch_list = _sorted_patch_files

    try:
        with open(os.path.join(dir_list.dir_log, 'selected_sources.list'), 'w') as fa:
            with open(os.path.join(dir_list.dir_log, 'source_file.list'), 'w') as fb:
                for _pkg in dependency_tree.selected_srcs:
                    fa.write(str(dependency_tree.selected_srcs[_pkg].raw) + '\n\n')
                    for _file in dependency_tree.selected_srcs[_pkg].files:
                        fb.write(f"{_file}: {dependency_tree.selected_srcs[_pkg].files[_file]}\n")

    except (FileNotFoundError, PermissionError) as e:
        Print(f"Error: {e}")
        exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - V Download source packages
    Print("Download source packages...")
    _src_download_size = dependency_tree.download_size
    Print("Total Download is about ", _src_download_size // (2**20), "MB")
    _total, _used, _free = shutil.disk_usage(dir_list.dir_source)
    print(f"Disk Space - Total: {_total // (2**30)}GiB, Used: {_used // (2**30)}GiB, Free: {_free // (2**30)}GiB")
    Print("Starting Downloads...")
    _downloaded_size = utils.download_source(dependency_tree, dir_list.dir_source, base_distribution)
    if _src_download_size != _downloaded_size:
        Confirm.ask("Download size mismatch, continue?", default=True)

    # -------------------------------------------------------------------------------------------------------------
    # Step - VI Source Build Dependency Check
    Print("Creating Build System...")
    build_container = buildcontainer.BuildContainer(dir_list)

    # -------------------------------------------------------------------------------------------------------------
    # Step - VII Starting Source Build
    Print("Starting Source Packages...")
    import tqdm
    _failed = _success = 0
    progress_format = '{percentage:3.0f}%[{bar:30}]{n_fmt}/{total_fmt} - {desc}'

    progress_bar = tqdm.tqdm(ncols=80, total=len(dependency_tree.selected_srcs), bar_format=progress_format)
    with open(os.path.join(dir_list.dir_log, 'dpkg-build.log'), "w") as dpkg_build_log:
        for _pkg in dependency_tree.selected_srcs:
            progress_bar.set_description_str(f"{_success}/{_failed} {_pkg}")
            progress_bar.update(1)
            _src_pkg = dependency_tree.selected_srcs[_pkg]
            _exit_code = build_container.build(_src_pkg)
            if not _exit_code:
                dpkg_build_log.write(f"FAIL: {_pkg}\n")
                _failed += 1
            else:
                dpkg_build_log.write(f"PASS: {_pkg}\n")
                _success += 1
            dpkg_build_log.flush()
    progress_bar.set_description_str(f"{_success}/{_failed}")
    progress_bar.close()

    Print(f"WARNING: build tests skipped for : {skip_build_test}")
    if _failed > 0:
        if not Confirm.ask("There are one or more source build failures, Proceed?", default=True):
            exit(1)

    # -------------------------------------------------------------------------------------------------------------
    # Step - VII Building chroot environment
    Print("Building chroot environment...")
    build_system = buildsystem.BuildSystem(dependency_tree, dir_list)
    if not build_system.build_chroot():
        Print("ERROR: Building chroot failed...")
        exit(1)


# Main function
if __name__ == '__main__':
    build_banner = "Athena Build Environment v0.1"
    print(asciiart_logo)
    main(build_banner)
