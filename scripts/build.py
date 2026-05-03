# (C) Athena Linux Project

# External imports

import faulthandler
faulthandler.enable(open('/tmp/athena_crash.log', 'w'))

import tui
import os
import shutil
import cache
import sys

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

# module-level variables
build_config: BuildConfig
build_cache: Cache
dependency_tree: dependencytree.DependencyTree
build_container: buildcontainer.BuildContainer
console: Console

# module-level variable for TUI instance
_tui: Tui


class BuildFlags:
    def __init__(self):
        self.cache_ready: bool = False
        self.dep_check_ready: bool = False
        self.download_ready: bool = False
        self.build_container_ready: bool = False

    def __str__(self) -> str:
        fields = ['cache_ready', 'dep_check_ready', 'download_ready', 'build_container_ready']
        return '  '.join(f"[{'✓' if getattr(self, f) else '·'}] {f.replace('_ready', '')}" for f in fields)

_progress_flags = BuildFlags()


def cmd_build_cache():
    global build_cache

    console.print("Building Cache...", tui.COLOR_INFO)
    _progress_flags.cache_ready = False

    try:
        build_cache = Cache(build_config)
    except Exception as e:
        console.print(f"ERROR: build cache - {e}")
        console.error(f"Cache() raised: {e}")
        return

    if not build_cache.is_valid:
        console.print(f"ERROR: build cache - {build_cache.error_str}")
        console.error(f"Cache invalid: {build_cache.error_str}")
        return

    _progress_flags.cache_ready = True


def cmd_parse_dependency():
    global dependency_tree

    if not _progress_flags.cache_ready:
        console.print("Cache not ready, Run 'build_cache' first")
        return

    _spiner = Spinner("Parsing Dependencies")
    _progress_flags.dep_check_ready = False

    console.print("Preparing Parsing Tree...")
    dependency_tree = dependencytree.DependencyTree(build_cache, select_recommended=False,
                                                     arch=build_config.arch,
                                                     build_profiles=build_config.build_profiles)

    required_packages = build_cache.required
    console.print("Pass I: Checking dependency for required packages", tui.COLOR_INFO)
    dependency_tree.resolve_packages(required_packages)

    __num_required = dependency_tree.selected_count
    console.print(f"Dependencies Selected for 'required' : {__num_required}")

    # Marking all dependencies of required packages as required
    # skipping virtuals to avoid marking them frivolously
    for _pkg in dependency_tree.selected_pkgs:
        if _pkg != dependency_tree.selected_pkgs[_pkg]['Package']:
            continue
        dependency_tree.selected_pkgs[_pkg].priority = 'required'

    # Adding 'important' packages too, not really mandatory for a bare-bones system
    # but too much manual intervention if these packages are not installed.
    # if stable, we may look at a skimmed down manual list
    important_packages = build_cache.important

    # Option to manually add additional packages we think are important, e.g. dialog
    # important_packages.extend(['dialog'])

    console.print("Pass II: Checking dependency for important packages", tui.COLOR_INFO)
    dependency_tree.resolve_packages(important_packages)

    __num_now = dependency_tree.selected_count
    console.print(f"Dependencies Selected for 'important' : {__num_now - __num_required}")
    __num_required = __num_now

    # Similar to 'required', just that if it is not 'required' has to be important
    # Manually forcing priority for other packages
    for _pkg in dependency_tree.selected_pkgs:
        if _pkg != dependency_tree.selected_pkgs[_pkg]['Package']:
            continue
        if dependency_tree.selected_pkgs[_pkg].priority != 'required':
            dependency_tree.selected_pkgs[_pkg].priority = 'important'

    # sanity check
    for _pkg in dependency_tree.selected_pkgs:
        _priority = dependency_tree.selected_pkgs[_pkg].priority
        if _priority != 'required' and _priority != 'important':
            console.print(f"Package {_pkg} with unexpected priority :{_priority}")

    console.print("Pass III: Checking dependency for manually selected packages", tui.COLOR_INFO)
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

    console.print(f"Added {len(manual_list)} unique manually selected packages")

    dependency_tree.resolve_packages(manual_list)

    __num_total = dependency_tree.selected_count
    console.print(f"Dependencies for manually added packages : {__num_total - __num_required}")
    console.print(f"Total Selected Packages : {__num_total}", tui.COLOR_HIGHLIGHT)
    _spiner.done()

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

    console.print("Parsing Source Packages...", tui.COLOR_INFO)

    if not dependency_tree.parse_sources():
        _resp = Prompt(PROMPT_YESNO, "There are one or more source parse failures, Proceed?").get_response()
        if _resp.lower() not in ('y', 'yes'):
            return

    for _pkg in build_config.skip_build_test:
        if _pkg in dependency_tree.selected_srcs:
            dependency_tree.selected_srcs[_pkg].skip_test = True

    for _pkg in dependency_tree.selected_srcs:
        _ver = str(dependency_tree.selected_srcs[_pkg].version)
        _patch_path = os.path.join(build_config.dir_patch_source, _pkg, _ver)
        try:
            if os.path.exists(_patch_path):
                _patch_files = [f for f in os.listdir(_patch_path) if f.endswith('.patch')]
                dependency_tree.selected_srcs[_pkg].patch_list = sorted(_patch_files, key=lambda x: x[:5])
                console.info(f"[patch] {_pkg} {_ver}: {_patch_files}")
        except OSError as e:
            console.print(f"WARNING: cannot list patches for '{_pkg}'")
            console.warning(f"patch discovery {_patch_path}: {e}")

    _patched = sum(1 for _s in dependency_tree.selected_srcs.values() if _s.patch_list)
    console.print(f"Found patches for {_patched} source package(s)", tui.COLOR_INFO)

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

    console.print(f"Selected {len(dependency_tree.selected_srcs)} source packages", tui.COLOR_HIGHLIGHT)
    _progress_flags.dep_check_ready = True


def cmd_print(category: str = ''):
    if category not in ('config', 'required', 'important', 'selected'):
        console.print("  Usage: print <config|required|important|selected>")
        return

    if category == 'config':
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

    if category == 'selected' and not _progress_flags.dep_check_ready:
        console.print("Run 'parse_dependency' first")
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


def cmd_source_download():

    if not _progress_flags.dep_check_ready:
        console.print("Run 'parse_dependency' first")
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


def cmd_init_container():
    global build_container

    _progress_flags.build_container_ready = False
    spin = Spinner("Initialising build container")
    try:
        build_container = buildcontainer.BuildContainer(build_config, docker_server=build_config.docker_server or None)
        _progress_flags.build_container_ready = True
        spin.done()
        console.print("  Build container ready")
    except RuntimeError as e:
        spin.done()
        console.print(f"  ERROR: build container initialisation failed — {e}")
        console.error(f"BuildContainer() raised: {e}")


def _do_tunnel(src_pkg) -> bool:
    """Download binary .deb files for src_pkg from the base Debian repo into the repo directory."""
    if not src_pkg.pkgs:
        console.error(f"tunnel {src_pkg.package}: no binary packages known (run parse_dependency first)")
        return False

    _base = f"https://{build_config.baseurl}/debian"
    _success = True

    for _filename in src_pkg.pkgs:
        _dest = os.path.join(build_container.repo_path, _filename)
        if os.path.isfile(_dest):
            console.info(f"tunnel {src_pkg.package}: {_filename} already present, skipping download")
            continue
        _url = f"{_base}/{src_pkg.directory}/{_filename}"
        console.info(f"tunnel {src_pkg.package}: downloading {_url}")
        _bytes = utils.download_file(_url, _dest)
        if _bytes < 0:
            console.error(f"tunnel {src_pkg.package}: failed to download {_filename}")
            _success = False

    _result_file = os.path.join(build_container.buildlog_path, src_pkg.package + '.result')
    try:
        with open(_result_file, 'w') as fh:
            fh.write('TUNNELED\n' if _success else 'FAIL\n')
    except OSError as e:
        console.error(f"tunnel {src_pkg.package}: cannot write result file: {e}")

    return _success


def cmd_tunnel_package(*args):

    if not _progress_flags.dep_check_ready:
        console.print("Run 'parse_dependency' first")
        return

    _names = list(args) if args else build_config.tunnel_packages
    if not _names:
        console.print("No packages specified and Tunneled list in build.conf is empty")
        return

    packages = []
    for name in _names:
        src = dependency_tree.selected_srcs.get(name)
        if src is None:
            console.print(f"Unknown package: {name}")
            return
        packages.append(src)

    _success = _failed = 0
    progress_bar = ProgressBar(label='Tunnel', itr_label='pkgs', maxvalue=len(packages))
    for _src_pkg in packages:
        _result = _do_tunnel(_src_pkg)
        if _result:
            _success += 1
        else:
            _failed += 1
        console.info(f"Tunnel {_src_pkg.package} [{'TUNNELED' if _result else 'FAIL'}]")
        progress_bar.step(1)
    progress_bar.close()
    
    console.print(f"Tunnel complete: {_success} tunneled, {_failed} failed")


def cmd_source_build(*args):
    
    if not _progress_flags.download_ready:
        console.print("Run 'source_download' first")
        return

    if not _progress_flags.build_container_ready:
        console.print("Run 'build_container' first")
        return

    _force = len(args) > 0 and args[0].strip().lower() == 'force'
    _names = list(args[1:]) if _force else list(args)

    if _force:
        console.print("Force mode: skipping build cache checks")

    if _names:
        packages = []
        for name in _names:
            src = dependency_tree.selected_srcs.get(name)
            if src is None:
                console.print(f"Unknown package: {name}")
                return
            packages.append(src)
    else:
        packages = list(dependency_tree.selected_srcs.values())

    if not packages:
        console.print("No source packages to build")
        return

    _success = _failed = _skipped = 0
    progress_bar = ProgressBar(label='Source Build', itr_label='pkgs', maxvalue=len(packages))
    
    for _src_pkg in packages:

        if _src_pkg.package in build_cache.skip_src:
            console.warning(f"Package {_src_pkg.package} in skip_list")
            _skipped = _skipped + 1
            progress_bar.step(1)
            continue

        if _src_pkg.package in build_config.tunnel_packages:
            if build_container.check_build(_src_pkg):
                console.info(f"Package {_src_pkg.package} already tunneled [SKIPPED]")
                _skipped += 1
                progress_bar.step(1)
                continue
            _build_result = _do_tunnel(_src_pkg)
            console.info(f"Tunnel {_src_pkg.package} [{'TUNNELED' if _build_result else 'FAIL'}]")
            if _build_result:
                _success += 1
            else:
                _failed += 1
            progress_bar.step(1)
            continue

        if not _force and build_container.check_build(_src_pkg):
            console.info(f"Package {_src_pkg.package} already built [SKIPPED]")
            _skipped = _skipped + 1
            progress_bar.step(1)
            continue

        _build_result = build_container.build(_src_pkg)

        if _build_result:
            _success = _success + 1
        else:
            _failed = _failed + 1
        
        console.info(f"Building Package {_src_pkg.package} [{'PASS' if _build_result else 'FAIL'}]")
        
        progress_bar.step(1)

    progress_bar.close()

    console.print(f"Source build complete: {_success} passed, {_failed} failed, {_skipped} skipped")
    if _failed > 0:
        console.error(f"{_failed} source build(s) failed")
        _resp = Prompt(PROMPT_YESNO, "There are source build failures, Proceed?").get_response()
        if _resp.lower() not in ('y', 'yes'):
            return
        
def main(banner: str):
    """main - the primary function being called"""
    global build_config, _tui, console

    # External modules initialisation
    try:
        print("Initialising apt_pkg...")
        apt_pkg.init_system()
    except Exception as e:
        print(f"ERROR: Failed to initialise apt_pkg - {e}, Exiting...")
        sys.exit(1)

    try:
        print("Parsing config...")
        build_config = BuildConfig()
    except Exception as e:
        print(f"ERROR: load configuration - {e}, Exiting...")
        sys.exit(1)

    if not build_config.is_valid:
        print(f"ERROR: load configuration - {build_config.error_str}, Exiting...")
        sys.exit(1)

    # Set up the TUI system
    print("Initialising TUI...")
    try:
        _tui = Tui(banner)
        _tui.run()

        tui.tui_instance = _tui
        signal.signal(signal.SIGINT, _tui.sig_shutdown)

        console = Console()
        tui.console = console

    except Exception as e:
        print(f"FATAL: TUI initialisation failed: {e}")
        Exit(1)

    tui.register_command('build_cache',       cmd_build_cache,        'Build cache')
    tui.register_command('parse_dependency',  cmd_parse_dependency,   'Parse dependency tree for selected packages')
    tui.register_command('source_download',   cmd_source_download,    'Download source packages')
    tui.register_command('build_container',   cmd_init_container,     'Initialise Docker build container')
    tui.register_command('source_build',      cmd_source_build,       'Build source packages in parallel (source_build [pkg …] [force])')
    tui.register_command('tunnel_package',    cmd_tunnel_package,     'Download binary .debs from Debian repo (tunnel_package [pkg …])')
    tui.register_command('print',             cmd_print,              'Print info: print <config|required|important|selected>')

    console.print(asciiart_logo, tui.COLOR_ERROR)
    console.print("Starting Source Build System for Athena Linux...", tui.COLOR_HIGHLIGHT)
    console.print(f"\tArch\t\t\t{build_config.arch}")
    console.print(f"\tParent Distribution\t{build_config.basecodename} {build_config.baseversion}")
    console.print(f"\tBuild Distribution\t{build_config.build_codename} {build_config.build_version}")

    _tui.wait()
    Exit(0)

    # -------------------------------------------------------------------------------------------------------------
    # Step - VII Building chroot environment
    # Print("Building chroot environment...")
    # build_system = buildsystem.BuildSystem(dependency_tree, dir_list)
    # if not build_system.build_chroot():
    #     Print("ERROR: Building chroot failed...")
    #     exit(1)


# Main function
if __name__ == '__main__':
    build_banner = "Athena Build Environment v0.1"
    print(asciiart_logo)
    main(build_banner)
