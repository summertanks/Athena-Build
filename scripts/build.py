# (C) Athena Linux Project
"""
build.py — top-level orchestrator for the Athena Linux build system.

Responsibilities:
  - Parse build configuration and APT cache
  - Resolve the full dependency tree for the target package list
  - Download upstream source archives
  - Manage the Docker build container image
  - Build each selected source package inside a clean container
  - Optionally tunnel (download prebuilt) binary packages from the base Debian repo
  - Expose all of the above as interactive TUI commands

Typical operator workflow:
    build_cache → parse_dependency → source_download → build_container → source_build

Each step sets a flag in _progress_flags so later commands can verify prerequisites
without re-running earlier work.
"""

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

# Module-level singletons — populated by the command handlers and referenced by later stages.
# Declaring types here lets type checkers catch use-before-init across the module.
build_config: BuildConfig
build_cache: Cache
dependency_tree: dependencytree.DependencyTree
build_container: buildcontainer.BuildContainer
console: Console

# TUI instance — kept separate from console so shutdown signals can reach it directly.
_tui: Tui


class BuildFlags:
    """Tracks which pipeline stages have completed successfully.

    Each flag is set to True at the end of its corresponding command handler
    and checked as a prerequisite by later stages.  This prevents commands
    from running on stale or missing state without repeating the earlier work.
    """

    def __init__(self):
        self.cache_ready: bool = False             # build_cache completed
        self.dep_check_ready: bool = False         # parse_dependency completed
        self.download_ready: bool = False          # source_download completed
        self.build_container_ready: bool = False   # build_container initialised
        self.source_build_ready: bool = False      # source_build completed

    def __str__(self) -> str:
        """Return a compact one-line status string for display in the TUI."""
        fields = ['cache_ready', 'dep_check_ready', 'download_ready',
                  'build_container_ready', 'source_build_ready']
        return '  '.join(f"[{'✓' if getattr(self, f) else '·'}] {f.replace('_ready', '')}" for f in fields)

_progress_flags = BuildFlags()


# ---------------------------------------------------------------------------
# Command: build_cache
# ---------------------------------------------------------------------------

def cmd_build_cache():
    """Fetch and parse the upstream APT package indices into an in-memory cache.

    Downloads the binary and source Packages files for the configured base
    distribution and architecture, then indexes them for fast lookup during
    dependency resolution.  Must be run before parse_dependency.
    """
    global build_cache

    console.print("Building Cache...", tui.COLOR_INFO)
    _progress_flags.cache_ready = False  # reset in case we're re-running

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


# ---------------------------------------------------------------------------
# Command: parse_dependency
# ---------------------------------------------------------------------------

def cmd_parse_dependency():
    """Resolve the full closure of packages needed to build the target system.

    Runs three dependency-resolution passes in priority order:

      Pass I   — 'required' packages (essential base; every package they pull
                  in is also marked required so it survives any later pruning)
      Pass II  — 'important' packages (strongly recommended by Debian policy;
                  avoids excessive manual intervention on a bare system)
      Pass III — manually listed packages from the configured pkglist file
                  (distro-specific selections on top of the Debian base)

    After resolution, validates the selection for Breaks/Conflicts, then
    maps every selected binary package back to its source package so that
    source_download and source_build know what to fetch and build.

    Patch files are discovered at this stage so that buildcontainer.build()
    can mount them at container start time without a second disk scan.
    """
    global dependency_tree

    if not _progress_flags.cache_ready:
        console.print("Cache not ready, Run 'build_cache' first")
        return

    _spiner = Spinner("Parsing Dependencies")
    _progress_flags.dep_check_ready = False  # reset before the long parse

    console.print("Preparing Parsing Tree...")
    dependency_tree = dependencytree.DependencyTree(build_cache, select_recommended=False,
                                                     arch=build_config.arch,
                                                     build_profiles=build_config.build_profiles)

    # --- Pass I: required ---------------------------------------------------
    required_packages = build_cache.required
    console.print("Pass I: Checking dependency for required packages", tui.COLOR_INFO)
    dependency_tree.resolve_packages(required_packages)

    __num_required = dependency_tree.selected_count
    console.print(f"Dependencies Selected for 'required' : {__num_required}")

    # Mark every package pulled in by 'required' as required too.
    # Virtual packages (aliases) are skipped — the canonical name carries the priority.
    for _pkg in dependency_tree.selected_pkgs:
        if _pkg != dependency_tree.selected_pkgs[_pkg]['Package']:
            continue
        dependency_tree.selected_pkgs[_pkg].priority = 'required'

    # --- Pass II: important --------------------------------------------------
    # 'Important' packages are not strictly needed for a minimal system but
    # omitting them causes enough breakage that manual intervention is required
    # for almost every subsequent step.  If the list is ever stabilised we
    # could replace it with a curated hand-picked set.
    important_packages = build_cache.important

    # Option to manually add additional packages we think are important, e.g. dialog
    # important_packages.extend(['dialog'])

    console.print("Pass II: Checking dependency for important packages", tui.COLOR_INFO)
    dependency_tree.resolve_packages(important_packages)

    __num_now = dependency_tree.selected_count
    console.print(f"Dependencies Selected for 'important' : {__num_now - __num_required}")
    __num_required = __num_now

    # Mark everything not already 'required' as 'important'.
    for _pkg in dependency_tree.selected_pkgs:
        if _pkg != dependency_tree.selected_pkgs[_pkg]['Package']:
            continue
        if dependency_tree.selected_pkgs[_pkg].priority != 'required':
            dependency_tree.selected_pkgs[_pkg].priority = 'important'

    # Sanity check — no package should carry any other priority string at this point.
    for _pkg in dependency_tree.selected_pkgs:
        _priority = dependency_tree.selected_pkgs[_pkg].priority
        if _priority != 'required' and _priority != 'important':
            console.print(f"Package {_pkg} with unexpected priority :{_priority}")

    # --- Pass III: manual list ----------------------------------------------
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

    # Strip comments and blank lines; only add packages not already selected.
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

    # --- Validation ---------------------------------------------------------
    console.print("Checking Breaks and Conflicts...")
    if not dependency_tree.validate_selection():
        _resp = Prompt(PROMPT_YESNO, "There are one or more dependency validation failures, Proceed?").get_response()
        if _resp.lower() not in ('y', 'yes'):
            _progress_flags.dep_check_ready = False
            return

    # Write the resolved binary package list to disk for post-mortem inspection.
    try:
        with open(os.path.join(build_config.dir_log, 'selected_packages.list'), 'w') as f:
            for pkg in dependency_tree.selected_pkgs:
                # Skip virtual-package aliases — log only canonical names.
                if pkg != dependency_tree.selected_pkgs[pkg]['Package']:
                    continue
                f.write(str(dependency_tree.selected_pkgs[pkg]) + '\n\n')
    except OSError as e:
        console.print(f"ERROR: cannot write selected_packages.list")
        console.error(f"selected_packages.list write: {e}")
        return

    # --- Source mapping -----------------------------------------------------
    # Map each selected binary package to its upstream source.  This populates
    # dependency_tree.selected_srcs which all subsequent stages consume.
    console.print("Parsing Source Packages...", tui.COLOR_INFO)

    if not dependency_tree.parse_sources():
        _resp = Prompt(PROMPT_YESNO, "There are one or more source parse failures, Proceed?").get_response()
        if _resp.lower() not in ('y', 'yes'):
            return

    # Apply per-package skip_test flag from config (suppresses 'nocheck' build opt).
    for _pkg in build_config.skip_build_test:
        if _pkg in dependency_tree.selected_srcs:
            dependency_tree.selected_srcs[_pkg].skip_test = True

    # --- Patch discovery ----------------------------------------------------
    # Scan the patch tree for files matching <package>/<version>/*.patch.
    # Sorting by the first five characters preserves the numeric prefix ordering
    # (e.g. 9001-, 9002-) used to control application order.
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

    # Write source lists to disk for auditing.
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


# ---------------------------------------------------------------------------
# Command: print
# ---------------------------------------------------------------------------

def cmd_print(category: str = ''):
    """Display summary information about the current build state.

    Usage: print <config|required|important|selected>

      config    — active build configuration values
      required  — packages with 'required' priority from the APT cache
      important — packages with 'important' priority from the APT cache
      selected  — all packages resolved by parse_dependency (needs dep check)
    """
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
        # Filter out virtual-package alias entries — only show canonical names.
        real_pkgs = {k: v for k, v in pkgs.items() if k == v['Package']}
        console.print(f"Selected packages ({len(real_pkgs)}):")
        for name in sorted(real_pkgs.keys()):
            console.print(f"  {name:<40} {real_pkgs[name].version}")


# ---------------------------------------------------------------------------
# Command: source_download
# ---------------------------------------------------------------------------

def cmd_source_download():
    """Download upstream source archives for all selected source packages.

    Fetches .dsc, .orig.tar.*, and .debian.tar.* files from the configured
    base mirror into dir_source.  Skips files that are already present and
    have correct checksums.  Prompts if the total downloaded size does not
    match the expected size reported by the APT indices (indicates a partial
    or corrupt download).
    """
    if not _progress_flags.dep_check_ready:
        console.print("Run 'parse_dependency' first")
        return

    _progress_flags.download_ready = False  # reset before starting

    _src_download_size = dependency_tree.download_size
    console.print(f"Total download is about {_src_download_size // (2**20)} MB")

    _total, _used, _free = shutil.disk_usage(build_config.dir_source)
    console.print(f"Disk space — Total: {_total // (2**30)} GiB, "
                  f"Used: {_used // (2**30)} GiB, Free: {_free // (2**30)} GiB")

    console.print("Starting downloads...")
    _downloaded_size = utils.download_source(dependency_tree, build_config.dir_source, build_cache.base)

    # A size mismatch usually means a network interruption or a package whose
    # expected size in the index differs from what the mirror actually served.
    if _src_download_size != _downloaded_size:
        _resp = Prompt(PROMPT_YESNO, "Download size mismatch, continue?").get_response()
        if _resp.lower() not in ('y', 'yes'):
            return

    _progress_flags.download_ready = True


# ---------------------------------------------------------------------------
# Command: build_container
# ---------------------------------------------------------------------------

def cmd_init_container():
    """Initialise the Docker build container image.

    Builds the image from config/Dockerfile if it does not exist or if the
    Dockerfile has changed since the last build (detected via SHA-256 label).
    Optionally connects to an external Docker daemon if DOCKER_SERVER is set
    in build.conf; falls back to the local daemon on connection failure.
    """
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


# ---------------------------------------------------------------------------
# Internal helper: tunnel download
# ---------------------------------------------------------------------------

def _do_tunnel(src_pkg) -> bool:
    """Download prebuilt binary .deb files for src_pkg from the base Debian repo.

    Used when building a package from source is impractical (e.g. toolchain
    bootstrap packages, packages with architecture-specific build failures).
    Files are written directly into the repo directory alongside locally built
    packages so the rest of the pipeline treats them identically.

    The result file written at the end uses the 'TUNNELED' tag (rather than
    'PASS') so that check_build() can distinguish tunneled packages from
    locally built ones if needed.

    Args:
        src_pkg: Source package object with .pkgs (list of .deb filenames),
                 .directory (pool path), and .package (source name).

    Returns:
        True if every binary package was downloaded successfully, False otherwise.
    """
    if not src_pkg.pkgs:
        console.error(f"tunnel {src_pkg.package}: no binary packages known (run parse_dependency first)")
        return False

    # Construct the pool base URL from the configured mirror.
    _base = f"https://{build_config.baseurl}/debian"
    _success = True

    for _filename in src_pkg.pkgs:
        _dest = os.path.join(build_container.repo_path, _filename)

        # Skip files already on disk — no integrity check here; the repo
        # directory is trusted to contain only valid packages.
        if os.path.isfile(_dest):
            console.info(f"tunnel {src_pkg.package}: {_filename} already present, skipping download")
            continue

        _url = f"{_base}/{src_pkg.directory}/{_filename}"
        console.info(f"tunnel {src_pkg.package}: downloading {_url}")
        _bytes = utils.download_file(_url, _dest)
        if _bytes < 0:
            console.error(f"tunnel {src_pkg.package}: failed to download {_filename}")
            _success = False

    # Write a result file so check_build() can skip re-tunneling on the next run.
    _result_file = os.path.join(build_container.buildlog_path, src_pkg.package + '.result')
    try:
        with open(_result_file, 'w') as fh:
            fh.write('TUNNELED\n' if _success else 'FAIL\n')
    except OSError as e:
        console.error(f"tunnel {src_pkg.package}: cannot write result file: {e}")

    return _success


# ---------------------------------------------------------------------------
# Command: tunnel_package
# ---------------------------------------------------------------------------

def cmd_tunnel_package(*args):
    """Download prebuilt binary .debs from the base Debian repo for named packages.

    Usage: tunnel_package [pkg ...]

    If no package names are given, uses the 'Tunneled' list from build.conf.
    Packages must already be present in the dependency tree (run parse_dependency
    first).  Skips packages whose result file already says TUNNELED or PASS.
    """
    if not _progress_flags.dep_check_ready:
        console.print("Run 'parse_dependency' first")
        return

    # Fall back to the config list if no names were given on the command line.
    _names = list(args) if args else build_config.tunnel_packages
    if not _names:
        console.print("No packages specified and Tunneled list in build.conf is empty")
        return

    # Validate all names up front before starting any downloads.
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


# ---------------------------------------------------------------------------
# Command: build_bootable
# ---------------------------------------------------------------------------

def cmd_build_bootable(*args):
    """Assemble the resolved package set into a bootable chroot environment.

    Takes the .deb files produced by source_build from dir_repo and installs
    them into a chroot tree at dir_chroot using dpkg.  The resulting chroot
    can be packaged into an ISO or disk image.

    Prerequisites: source_build must have completed (source_build_ready flag).
    The sudo password is collected interactively at the start of this command.
    """
    if not _progress_flags.source_build_ready:
        console.print("Run 'source_build' first")
        return

    console.print("Initialising build system...")
    try:
        build_system = buildsystem.BuildSystem(dependency_tree, build_config)
    except RuntimeError as e:
        console.print(f"ERROR: build system initialisation failed — {e}")
        console.error(f"BuildSystem() raised: {e}")
        return

    console.print("Building chroot environment...")
    _result = build_system.build_chroot()
    if not _result:
        console.print("ERROR: chroot build failed — check logs for details")
        console.error("build_chroot() returned False")


# ---------------------------------------------------------------------------
# Command: source_build
# ---------------------------------------------------------------------------

def cmd_source_build(*args):
    """Build source packages inside the Docker build container.

    Usage: source_build [force] [pkg ...]

      force   — rebuild packages even if a valid result already exists
      pkg ... — limit the build to the named source packages; builds all if omitted

    Each package is built in a fresh container instance with its declared
    build-dependencies installed at runtime.  Result files (.result) and build
    logs are written to the configured log/build directory.

    Packages listed under 'Tunneled' in build.conf are downloaded from the
    base Debian repo instead of being built locally, even when running
    source_build (the force flag has no effect on tunneled packages).

    Prompts before returning if any builds fail, allowing the operator to
    decide whether to continue with the partial package set.
    """
    if not _progress_flags.download_ready:
        console.print("Run 'source_download' first")
        return

    if not _progress_flags.build_container_ready:
        console.print("Run 'build_container' first")
        return

    # Parse optional 'force' flag — must be the first argument if present.
    _force = len(args) > 0 and args[0].strip().lower() == 'force'
    _names = list(args[1:]) if _force else list(args)

    if _force:
        console.print("Force mode: skipping build cache checks")

    # Build a subset if package names were given; otherwise build everything.
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

        # Packages on the skip_src list are excluded unconditionally — typically
        # packages that are known to be unbuildable in the current environment.
        if _src_pkg.package in build_cache.skip_src:
            console.warning(f"Package {_src_pkg.package} in skip_list")
            _skipped = _skipped + 1
            progress_bar.step(1)
            continue

        # Tunneled packages are always downloaded rather than built locally.
        # check_build() accepts 'TUNNELED' as a valid result so we can skip
        # packages that were already tunneled in a previous run.
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

        # Skip packages with a valid existing build result unless force is set.
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

    _progress_flags.source_build_ready = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(banner: str):
    """Initialise all subsystems and hand control to the interactive TUI.

    Startup sequence:
      1. Initialise apt_pkg (required before any APT index parsing)
      2. Load build.conf into BuildConfig
      3. Start the TUI and register all interactive commands
      4. Display the welcome banner and block until the user exits

    All commands are registered before the banner is shown so that the TUI
    command table is complete from the moment the interface is visible.
    """
    global build_config, _tui, console

    # apt_pkg.init_system() must be called once before any apt_pkg parsing;
    # it reads /etc/apt/apt.conf and sets up architecture defaults.
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

    # Start the TUI before registering commands so that console output during
    # registration is captured by the log tab rather than going to raw stdout.
    print("Initialising TUI...")
    try:
        _tui = Tui(banner)
        _tui.run()

        tui.tui_instance = _tui
        signal.signal(signal.SIGINT, _tui.sig_shutdown)  # clean shutdown on Ctrl-C

        console = Console()
        tui.console = console

    except Exception as e:
        print(f"FATAL: TUI initialisation failed: {e}")
        Exit(1)

    # Register all interactive commands in pipeline order so the help listing
    # reflects the typical operator workflow top-to-bottom.
    tui.register_command('build_cache',       cmd_build_cache,        'Build cache')
    tui.register_command('parse_dependency',  cmd_parse_dependency,   'Parse dependency tree for selected packages')
    tui.register_command('source_download',   cmd_source_download,    'Download source packages')
    tui.register_command('build_container',   cmd_init_container,     'Initialise Docker build container')
    tui.register_command('source_build',      cmd_source_build,       'Build source packages in parallel (source_build [pkg …] [force])')
    tui.register_command('tunnel_package',    cmd_tunnel_package,     'Download binary .debs from Debian repo (tunnel_package [pkg …])')
    tui.register_command('build_bootable',    cmd_build_bootable,     'Build bootable chroot environment')
    tui.register_command('print',             cmd_print,              'Print info: print <config|required|important|selected>')

    console.print(asciiart_logo, tui.COLOR_ERROR)
    console.print("Starting Source Build System for Athena Linux...", tui.COLOR_HIGHLIGHT)
    console.print(f"\tArch\t\t\t{build_config.arch}")
    console.print(f"\tParent Distribution\t{build_config.basecodename} {build_config.baseversion}")
    console.print(f"\tBuild Distribution\t{build_config.build_codename} {build_config.build_version}")

    _tui.wait()
    Exit(0)


# Main function
if __name__ == '__main__':
    build_banner = "Athena Build Environment v0.1"
    print(asciiart_logo)
    main(build_banner)
