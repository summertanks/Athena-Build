# (C) Athena Linux Project

# External imports

import faulthandler
faulthandler.enable()

import os
import shutil
import cache

import apt_pkg

# Local imports
import utils
from utils import BuildConfig
from cache import Cache

import buildcontainer
import dependencytree
import buildsystem
import signal


from tui import Tui, Console, Prompt, PROMPT_YESNO, Spinner, ProgressBar, Exit

asciiart_logo = '╔══╦╗╔╗─────────╔╗╔╗\n' \
                '║╔╗║╚╣╚╦═╦═╦╦═╗─║║╠╬═╦╦╦╦╦╗\n' \
                '║╠╣║╔╣║║╩╣║║║╬╚╗║╚╣║║║║║╠║╣\n' \
                '╚╝╚╩═╩╩╩═╩╩═╩══╝╚═╩╩╩═╩═╩╩╝'

# TODO: make all apt_pkg.parse functions arch specific

# module-level variable
build_config: BuildConfig
build_cache: Cache
dependency_tree: dependencytree.DependencyTree

# module-level variable for TUI instance
_tui : Tui


class BuildFlags:
    def __init__(self):
        self.apt_ready: bool = False
        self.config_ready: bool = False
        self.cache_ready: bool = False
        self.dep_check_ready: bool = False
        self.source_ready: bool = False
        self.download_ready: bool = False

    def __str__(self) -> str:
        fields = ['apt_ready', 'config_ready', 'cache_ready', 'dep_check_ready', 'source_ready', 'download_ready']
        return '  '.join(f"[{'✓' if getattr(self, f) else '·'}] {f.replace('_ready', '')}" for f in fields)

_progress_flags = BuildFlags()

def main(banner: str):
    """main - the primary function being called"""
    import tui

    # Set up the TUI system
    try:
        _tui = Tui(banner)
        _tui.run()  # Start the TUI event loop

        # Set the global tui_instance to the current TUI instance
        tui.tui_instance = _tui

        # Register the signal handler for SIGINT (Ctrl+C)
        signal.signal(signal.SIGINT, _tui.sig_shutdown)

        console = Console()  # Initialize the console instance
        tui.console = console  # Set the global console instance
    except Exception as e:
        print(f"FATAL: TUI initialisation failed: {e}")
        Exit(1)
        return

    # External modules initialisation
    console.print("Initialising apt_pkg...")
    try:
        apt_pkg.init_system()
        _progress_flags.apt_ready = True
    except Exception as e:
        _progress_flags.apt_ready = False
        console.print(f"ERROR: Failed to initialise apt_pkg - {e}")
        console.error(f"apt_pkg.init_system() raised: {e}")

    def cmd_load_config():
        global build_config
        console.print("Parsing config...")
        try:
            build_config = BuildConfig()
        except Exception as e:
            console.print(f"ERROR: load configuration - {e}")
            console.error(f"BuildConfig() raised: {e}")
            _progress_flags.config_ready = False
            return

        if not build_config.is_valid:
            console.print(f"ERROR: load configuration - {build_config.error_str}")
            console.error(f"BuildConfig invalid: {build_config.error_str}")
            _progress_flags.config_ready = False
            return

        _progress_flags.config_ready = True
        console.print(f"\tArch\t\t\t{build_config.arch}")
        console.print(f"\tParent Distribution\t{build_config.basecodename} {build_config.baseversion}")
        console.print(f"\tBuild Distribution\t{build_config.build_codename} {build_config.build_version}")


    def cmd_build_cache():
        global build_cache
        if not _progress_flags.config_ready:
            console.print("  Run 'build_config' first")
            return
        if not _progress_flags.apt_ready:
            console.print("  ERROR: apt_pkg not initialised — cannot build cache")
            return
        console.print("Building Cache...")
        try:
            build_cache = Cache(build_config)
        except Exception as e:
            console.print(f"ERROR: build cache - {e}")
            console.error(f"Cache() raised: {e}")
            _progress_flags.cache_ready = False
            return

        if not build_cache.is_valid:
            console.print(f"ERROR: build cache - {build_cache.error_str}")
            console.error(f"Cache invalid: {build_cache.error_str}")
            _progress_flags.cache_ready = False
            return
        _progress_flags.cache_ready = True
    
    def cmd_parse_dependency():
        global dependency_tree
        
        if not _progress_flags.cache_ready:
            console.print("  Run 'build_cache' first")
            return
        _progress_flags.dep_check_ready = False

        console.print("Preparing Parsing Tree...")
        dependency_tree = dependencytree.DependencyTree(build_cache, select_recommended=False, arch=build_config.arch)

        required_packages = build_cache.required
        dependency_tree.add_lookahead(required_packages)

        for pkg in required_packages:
            if dependency_tree.parse_dependency(pkg) is None:
                console.print(f"WARNING: cannot resolve required '{pkg}'")
                console.error(f"parse_dependency({pkg}) returned None")
                
        __num_required = sum(1 for _k in dependency_tree.selected_pkgs
                             if _k == dependency_tree.selected_pkgs[_k]['Package'])
        console.print(f"Dependencies Selected for 'required' : {__num_required}")

        # Cheeky but works, ideally, parsing should have identified and marked required and their dependencies as required
        for _pkg in dependency_tree.selected_pkgs:
            if _pkg != dependency_tree.selected_pkgs[_pkg]['Package']:
                continue
            dependency_tree.selected_pkgs[_pkg].priority = 'required'

        # Adding 'important' packages too, not really mandatory for a bare-bones system but too much manual intervention
        # if these packages are not installed. if stable, we may look at a skimmed down manual list
        important_packages = build_cache.important
        
        # Option to manually add additional packages we think are important, e.g. dialog
        important_packages.extend(['dialog'])
        
        dependency_tree.add_lookahead(important_packages)
        for pkg in important_packages:
            if dependency_tree.parse_dependency(pkg) is None:
                console.print(f"WARNING: cannot resolve important '{pkg}'")
                console.error(f"parse_dependency({pkg}) returned None")

        __num_now = sum(1 for _k in dependency_tree.selected_pkgs
                        if _k == dependency_tree.selected_pkgs[_k]['Package'])
        console.print(f"Dependencies Selected for 'important' : {__num_now - __num_required}")
        __num_required = __num_now

        # Similar to 'required', just that if it is not 'required' has to be important
        # Manually forcing priotity for other packages
        for _pkg in dependency_tree.selected_pkgs:
            if _pkg != dependency_tree.selected_pkgs[_pkg]['Package']:
                continue
            if dependency_tree.selected_pkgs[_pkg].priority != 'required':
                dependency_tree.selected_pkgs[_pkg].priority = 'important'

        selected_packages = list(dependency_tree.selected_pkgs.keys())
        manual_list = []

        console.print(f"Parsing {build_config.pkglist_path}...")
        try:
            manual_packages_list = utils.readfile(build_config.pkglist_path).split('\n')
        except OSError as e:
            console.print(f"ERROR: cannot read package list {build_config.pkglist_path}")
            console.error(f"readfile({build_config.pkglist_path}): {e}")
            manual_packages_list = []
        
        for pkg in manual_packages_list:
            if pkg and not pkg.startswith('#') and not pkg.isspace():
                pkg = pkg.strip()
                if pkg not in selected_packages:
                    manual_list.append(pkg)
        console.print(f"Manual Selected Packages {len(manual_list)}")

        # Iterate through package list and identify dependencies
        dependency_tree.add_lookahead(manual_list)
        for pkg in manual_list:
            if dependency_tree.parse_dependency(pkg) is None:
                console.print(f"WARNING: cannot resolve '{pkg}'")
                console.error(f"parse_dependency({pkg}) returned None")

        __num_total = sum(1 for _k in dependency_tree.selected_pkgs
                          if _k == dependency_tree.selected_pkgs[_k]['Package'])
        console.print(f"Dependencies for manually added packages : {__num_total - __num_required}")
        console.print(f"Total Selected Packages : {__num_total}")

        # -------------------------------------------------------------------------------------------------------------
        # Step III - Checking Breaks, Conflicts and version constraints
        console.print("Checking Breaks and Conflicts...")
        if not dependency_tree.validate_selection():
            _resp = Prompt(PROMPT_YESNO, "There are one or more dependency validation failures, Proceed?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                _progress_flags.dep_check_ready = False
                return

        try:
            with open(os.path.join(build_config.dir_log, 'selected_packages.list'), 'w') as f:
                for pkg in dependency_tree.selected_pkgs:
                    if pkg != dependency_tree.selected_pkgs[pkg]['Package']:
                        continue
                    f.write(str(dependency_tree.selected_pkgs[pkg]) + '\n\n')
        except OSError as e:
            console.print(f"ERROR: cannot write selected_packages.list")
            console.error(f"selected_packages.list write: {e}")
            return

        if len(dependency_tree.selected_pkgs) > 0:
            _progress_flags.dep_check_ready = True

    def cmd_print(category: str = ''):
        if category not in ('config', 'required', 'important', 'selected'):
            console.print("  Usage: print <config|required|important|selected>")
            return

        if category == 'config':
            if not _progress_flags.config_ready:
                console.print("  No config loaded — run 'build_config' first")
                return
            console.print("Build Configuration:")
            console.print(f"    Arch                : {build_config.arch}")
            console.print(f"    Base URL            : {build_config.baseurl}")
            console.print(f"    Base ID             : {build_config.baseid}")
            console.print(f"    Parent codename     : {build_config.basecodename}")
            console.print(f"    Parent version      : {build_config.baseversion}")
            console.print(f"    Build codename      : {build_config.build_codename}")
            console.print(f"    Build version       : {build_config.build_version}")
            console.print(f"    Config file         : {build_config.config_path}")
            console.print(f"    Package list        : {build_config.pkglist_path}")
            console.print(f"    Working dir         : {build_config.working_dir}")
            return

        if not _progress_flags.cache_ready:
            console.print("Run 'build_cache' first")
            return

        if category == 'selected' and not _progress_flags.dep_check_ready:
            console.print("Run 'parse_mandatory' first")
            return

        if category == 'required':
            pkgs = build_cache.required
            console.print(f"Required packages ({len(pkgs)}):")
            for pkg in sorted(pkgs):
                console.print(f"  {pkg}")

        elif category == 'important':
            pkgs = build_cache.important
            console.print(f"Important packages ({len(pkgs)}):")
            for pkg in sorted(pkgs):
                console.print(f"  {pkg}")

        elif category == 'selected':
            pkgs = dependency_tree.selected_pkgs
            real_pkgs = {k: v for k, v in pkgs.items() if k == v['Package']}
            console.print(f"Selected packages ({len(real_pkgs)}):")
            for name in sorted(real_pkgs.keys()):
                console.print(f"  {name:<40} {real_pkgs[name].version}")

    def cmd_parse_source():
        if not _progress_flags.dep_check_ready:
            console.print("  Run 'parse_dependency' first")
            return
        _progress_flags.source_ready = False

        console.print("Parsing Source Packages...")
        if not dependency_tree.parse_sources():
            _resp = Prompt(PROMPT_YESNO, "There are one or more source parse failures, Proceed?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        for _pkg in build_config.skip_build_test:
            if _pkg in dependency_tree.selected_srcs:
                dependency_tree.selected_srcs[_pkg].skip_test = True

        for _pkg in dependency_tree.selected_srcs:
            _patch_path = os.path.join(build_config.dir_patch_source, _pkg,
                                       str(dependency_tree.selected_srcs[_pkg].version))
            try:
                if os.path.exists(_patch_path):
                    _patch_files = [f for f in os.listdir(_patch_path) if f.endswith('.patch')]
                    dependency_tree.selected_srcs[_pkg].patch_list = sorted(_patch_files, key=lambda x: x[:5])
            except OSError as e:
                console.print(f"WARNING: cannot list patches for '{_pkg}'")
                console.warning(f"patch discovery {_patch_path}: {e}")

        try:
            with open(os.path.join(build_config.dir_log, 'selected_sources.list'), 'w') as fa:
                with open(os.path.join(build_config.dir_log, 'source_file.list'), 'w') as fb:
                    for _pkg in dependency_tree.selected_srcs:
                        fa.write(str(dependency_tree.selected_srcs[_pkg]) + '\n\n')
                        for _file in dependency_tree.selected_srcs[_pkg].files:
                            fb.write(f"{_file}: {dependency_tree.selected_srcs[_pkg].files[_file]}\n")
        except OSError as e:
            console.print(f"ERROR: cannot write source lists")
            console.error(f"source lists write: {e}")
            return

        console.print(f"Selected {len(dependency_tree.selected_srcs)} source packages")
        _progress_flags.source_ready = True

    def cmd_source_download():
        if not _progress_flags.source_ready:
            console.print("  Run 'parse_sources' first")
            return
        _progress_flags.download_ready = False

        _src_download_size = dependency_tree.download_size
        console.print(f"Total download is about {_src_download_size // (2**20)} MB")

        _total, _used, _free = shutil.disk_usage(build_config.dir_source)
        console.print(f"Disk space — Total: {_total // (2**30)} GiB, "
                      f"Used: {_used // (2**30)} GiB, Free: {_free // (2**30)} GiB")

        console.print("Starting downloads...")
        _downloaded_size = utils.download_source(dependency_tree, build_config.dir_source, build_cache.base)

        if _src_download_size != _downloaded_size:
            _resp = Prompt(PROMPT_YESNO, "Download size mismatch, continue?").get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        _progress_flags.download_ready = True

    # --------------------------------------------------------------------------------------------------------------
    console.print(asciiart_logo)
    console.print("Starting Source Build System for Athena Linux...")
    cmd_load_config()

    tui.register_command('build_config',      cmd_load_config,        'Parse build configuration')
    tui.register_command('build_cache',       cmd_build_cache,        'Build cache')
    tui.register_command('parse_dependency',  cmd_parse_dependency,   'Parse dependency tree for selected packages')
    tui.register_command('parse_sources',     cmd_parse_source,       'Parse source packages for selected dependencies')
    tui.register_command('source_download',   cmd_source_download,    'Download source packages')
    tui.register_command('print',             cmd_print,              'Print info: print <config|required|important|selected>')
    
    _tui.wait()
    Exit(0)

    # Step - V moved to cmd_source_download()

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
