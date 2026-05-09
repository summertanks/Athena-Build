"""Handlers for the `print` TUI command.

`cmd_print()` in build.py is a thin dispatcher into this module — the actual
view code lives here.  Each handler receives the live ``BuildSession`` so it
can read ``self.config`` / ``self.cache`` / ``self.dep_tree`` and writes to
the console tab via ``tui.console.print``.

Adding a new view: write a ``_print_<category>(session, *extras)`` function
and register it in :data:`CATEGORIES` with a group + one-line description.
The help screen (``print help``) and the unknown-category error path pick
it up automatically; no changes to build.py or the TUI registration line
needed.

Group field controls where a category appears in the help screen.  Add a
new group by appending to :data:`_HELP_GROUP_ORDER` so the section shows up.
"""
import os
from collections import defaultdict
from typing import TYPE_CHECKING

import tui
import utils

if TYPE_CHECKING:
    from build import BuildSession


# ─── helpers ────────────────────────────────────────────────────────────────

def _fmt_dep(dep_tuple) -> str:
    """Turn a parsed dep tuple (name, ver, op) into 'name' or 'name (op ver)'.

    apt_pkg.parse_depends returns each constraint as (name, version, op)
    where unconstrained deps have op == '' and version == ''.
    """
    name, ver, op = dep_tuple[0], dep_tuple[1] if len(dep_tuple) > 1 else '', \
                    dep_tuple[2] if len(dep_tuple) > 2 else ''
    if op and ver:
        return f"{name} ({op} {ver})"
    return name


def _fmt_dep_group(group) -> str:
    """An alt-dep group renders as 'a | b (>= 2) | c'."""
    return ' | '.join(_fmt_dep(t) for t in group)


def _require_dep_check(session) -> bool:
    """Print 'run parse_dependency first' and return False if not ready."""
    if not session.flags.dep_check_ready:
        tui.console.print("Run 'parse_dependency' first")
        return False
    return True


def _require_cache(session) -> bool:
    """Print 'run build_cache first' and return False if not ready."""
    if not session.flags.cache_ready or session.cache is None:
        tui.console.print("Run 'build_cache' first")
        return False
    return True


# ─── Configuration views ────────────────────────────────────────────────────

def _print_config(session, *_extras) -> None:
    """Overall build configuration.  Calls out to mirrors / snapshot / paths
    sub-views for the nested sections to keep this digestible."""
    cfg = session.config
    tui.console.print("Build Configuration:")
    tui.console.print(f"    Arch                : {cfg.arch}")
    tui.console.print(f"    Release             : {cfg.release}")
    tui.console.print(f"    Base ID (default)   : {cfg.baseid}")
    tui.console.print(f"    Parent version      : {cfg.baseversion}")
    tui.console.print(f"    Build codename      : {cfg.build_codename}")
    tui.console.print(f"    Build version       : {cfg.build_version}")
    tui.console.print(f"    Recommends pulled   : {getattr(cfg, 'select_recommended', False)}")
    if getattr(cfg, 'build_profiles', None):
        tui.console.print(f"    Build profiles      : {', '.join(sorted(cfg.build_profiles))}")
    if getattr(cfg, 'build_options', None):
        tui.console.print(f"    Build options       : {', '.join(sorted(cfg.build_options))}")
    tui.console.print("")
    tui.console.print(f"  Mirrors             : {len(cfg.mirrors)}  (use `print mirrors` for detail)")
    if cfg.snapshot_enabled:
        tui.console.print("  Snapshot            : enabled    (use `print snapshot` for detail)")
    else:
        tui.console.print("  Snapshot            : disabled (live mirrors)")
    tui.console.print(f"  Tunneled packages   : {len(getattr(cfg, 'tunnel_packages', []))}  (use `print tunneled`)")
    tui.console.print(f"  Paths               : working dir {cfg.working_dir}  (use `print paths`)")


def _print_mirrors(session, *_extras) -> None:
    """All configured mirrors with URL, suite, component."""
    cfg = session.config
    tui.console.print(f"Configured mirrors ({len(cfg.mirrors)}):")
    for _m in cfg.mirrors:
        tui.console.print(f"  [{_m.id:8}]")
        tui.console.print(f"    URL        : {_m.url}")
        tui.console.print(f"    Suite      : {_m.suite}")
        tui.console.print(f"    Component  : {_m.component}")
        if hasattr(_m, 'baseurl'):
            tui.console.print(f"    Base URL   : {_m.baseurl}")


def _print_snapshot(session, *_extras) -> None:
    """Snapshot pin status: configured value + resolved date + state file."""
    cfg = session.config
    if not cfg.snapshot_enabled:
        tui.console.print("Snapshot pinning: disabled (live mirrors)")
        tui.console.print("  (set [Snapshot] Enabled = true in config/build.conf to pin)")
        return
    tui.console.print("Snapshot pinning: enabled")
    tui.console.print(f"  Configured  : {cfg.snapshot_timestamp_config}")
    try:
        _resolved = utils.resolve_snapshot_timestamp(cfg)
    except (RuntimeError, ValueError) as e:
        tui.console.print(f"  Resolved    : <unresolved: {e}>")
        _resolved = None
    if _resolved:
        _human = utils.format_snapshot_timestamp(_resolved)
        tui.console.print(f"  Resolved    : {_resolved}  ({_human})")
    _state = os.path.join(cfg.dir_cache, 'snapshot.timestamp')
    _state_exists = os.path.exists(_state)
    tui.console.print(f"  State file  : {_state}  "
                      f"{'(present)' if _state_exists else '(absent — will resolve on next run)'}")


def _print_paths(session, *_extras) -> None:
    """Every directory the build uses, plus key file paths."""
    cfg = session.config
    tui.console.print("Build paths:")
    tui.console.print(f"  Working dir       : {cfg.working_dir}")
    _attrs = [
        ('dir_cache',            'Cache             '),
        ('dir_download',         'Download          '),
        ('dir_source',           'Source            '),
        ('dir_repo',             'Repo (.debs)      '),
        ('dir_chroot',           'Chroot            '),
        ('dir_image',            'Image (ISO)       '),
        ('dir_log',              'Log               '),
        ('dir_temp',             'Temp              '),
        ('dir_config',           'Config            '),
        ('dir_patch',            'Patch             '),
        ('dir_patch_source',     '  source patches  '),
        ('dir_patch_preinstall', '  pre-install     '),
        ('dir_patch_postinstall','  post-install    '),
        ('dir_gnupg',            'GPG home          '),
    ]
    for attr, label in _attrs:
        if hasattr(cfg, attr):
            tui.console.print(f"  {label}: {getattr(cfg, attr)}")
    tui.console.print("")
    tui.console.print(f"  Config file       : {cfg.config_path}")
    tui.console.print(f"  Package list      : {cfg.pkglist_path}")


# ─── Build state views ──────────────────────────────────────────────────────

def _print_state(session, *_extras) -> None:
    """Pipeline stage progress (BuildFlags) — what's done, what's pending."""
    flags = session.flags
    tui.console.print("Pipeline state:")
    _stages = [
        ('cache_ready',           'build_cache       ', 'fetch + index APT mirrors'),
        ('dep_check_ready',       'parse_dependency  ', 'resolve dep graph from pkg.list'),
        ('download_ready',        'source_download   ', 'fetch upstream source archives'),
        ('build_container_ready', 'build_container   ', 'init Docker build environment'),
        ('source_build_ready',    'source_build      ', 'run dpkg-buildpackage per source'),
        ('chroot_ready',          'build_chroot      ', 'install built .debs into buildroot/'),
        ('chroot_verified',       'verify_chroot     ', '8-check verifier (passes ⇒ ISO ok)'),
    ]
    for attr, label, desc in _stages:
        _ok = bool(getattr(flags, attr, False))
        _mark = '✓' if _ok else '·'
        tui.console.print(f"  [{_mark}] {label}  {desc}")
    tui.console.print("")
    tui.console.print("  build_iso runs separately once chroot_verified is set.")


def _print_stats(session, *_extras) -> None:
    """High-level counts across cache, dep tree, and source resolution."""
    tui.console.print("Build statistics:")
    cfg = session.config
    tui.console.print(f"  Mirrors configured       : {len(cfg.mirrors)}")
    tui.console.print("  Snapshot pinning         : "
                      f"{'enabled' if cfg.snapshot_enabled else 'disabled'}")
    if session.cache is not None:
        c = session.cache
        tui.console.print(f"  Cache: package names     : {len(c.package_hashtable)}")
        tui.console.print(f"  Cache: source names      : {len(c.source_hashtable)}")
        tui.console.print(f"  Cache: required priority : {len(c.required)}")
        tui.console.print(f"  Cache: important priority: {len(c.important)}")
    else:
        tui.console.print("  Cache                    : not built (run build_cache)")
    if session.dep_tree is not None and session.flags.dep_check_ready:
        dt = session.dep_tree
        canonical = sum(1 for k, v in dt.selected_pkgs.items()
                        if k == v['Package'])
        virtuals = len(dt.selected_pkgs) - canonical
        tui.console.print(f"  Dep tree: canonical pkgs : {canonical}")
        tui.console.print(f"  Dep tree: virtual aliases: {virtuals}")
        tui.console.print(f"  Dep tree: source pkgs    : {len(dt.selected_srcs)}")
        tui.console.print("  Dep tree: download size  : "
                          f"{getattr(dt, 'download_size', 0) // (2**20)} MB")
    else:
        tui.console.print("  Dep tree                 : not built (run parse_dependency)")


# ─── Package list views ─────────────────────────────────────────────────────

def _print_required(session, *_extras) -> None:
    """All packages with Priority: required from the APT cache."""
    if not _require_cache(session):
        return
    pkgs = session.cache.required
    tui.console.print(f"Required packages ({len(pkgs)}):")
    for pkg in sorted(pkgs):
        tui.console.print(f"  {pkg}")


def _print_important(session, *_extras) -> None:
    """All packages with Priority: important from the APT cache."""
    if not _require_cache(session):
        return
    pkgs = session.cache.important
    tui.console.print(f"Important packages ({len(pkgs)}):")
    for pkg in sorted(pkgs):
        tui.console.print(f"  {pkg}")


def _print_selected(session, *_extras) -> None:
    """All packages resolved by parse_dependency (canonical names only)."""
    if not _require_dep_check(session):
        return
    pkgs = session.dep_tree.selected_pkgs
    real_pkgs = {k: v for k, v in pkgs.items() if k == v['Package']}
    tui.console.print(f"Selected packages ({len(real_pkgs)}):")
    for name in sorted(real_pkgs.keys()):
        tui.console.print(f"  {name:<40} {real_pkgs[name].version}")


def _print_tunneled(session, *_extras) -> None:
    """Packages set to use prebuilt .debs from the base Debian repo."""
    cfg = session.config
    tunneled = getattr(cfg, 'tunnel_packages', [])
    if not tunneled:
        tui.console.print("No tunneled packages configured "
                          "(Tunneled list empty in config/build.conf)")
        return
    tui.console.print(f"Tunneled packages ({len(tunneled)}):")
    for name in sorted(tunneled):
        tui.console.print(f"  {name}")


def _print_pkg(session, *extras) -> None:
    """`print pkg <name>` — full record for one package from the dep tree."""
    if not extras:
        tui.console.print("Usage: print pkg <name>")
        return
    if not _require_dep_check(session):
        return
    name = extras[0]
    pkgs = session.dep_tree.selected_pkgs
    if name not in pkgs:
        tui.console.print(f"Package {name!r} is not in the current dep tree.")
        tui.console.print("  (`print selected` lists everything that is.)")
        return
    p = pkgs[name]
    _mirror = getattr(p, '_mirror', None)
    _mirror_id = getattr(_mirror, 'id', '?') if _mirror else '?'

    tui.console.print(f"Package: {p['Package']}")
    if name != p['Package']:
        tui.console.print(f"  (entry under virtual name {name!r})")
    tui.console.print(f"  Version       : {p.version}")
    tui.console.print(f"  Architecture  : {p.arch}")
    tui.console.print(f"  Priority      : {p.priority}")
    tui.console.print(f"  Mirror        : {_mirror_id}")
    if p.get('Section'):
        tui.console.print(f"  Section       : {p['Section']}")
    if p.get('Maintainer'):
        tui.console.print(f"  Maintainer    : {p['Maintainer']}")
    if p.get('Filename'):
        tui.console.print(f"  Filename      : {p['Filename']}")

    _sections = [
        ('Pre-Depends', p.pre_depends, p.alt_pre_depends),
        ('Depends',     p.depends,     p.alt_depends),
    ]
    for label, plain, alts in _sections:
        if plain or alts:
            tui.console.print(f"  {label}:")
            for d in plain:
                tui.console.print(f"      {_fmt_dep(d)}")
            for grp in alts:
                tui.console.print(f"      {_fmt_dep_group(grp)}")

    _multi_sections = [
        ('Recommends',  [[d] for d in p.recommends]),
        ('Suggests',    [[d] for d in p.suggests]),
        ('Conflicts',   p.conflicts),
        ('Breaks',      p.breaks),
        ('Provides',    p.provides),
        ('Replaces',    p.replaces),
    ]
    for label, groups in _multi_sections:
        if groups:
            tui.console.print(f"  {label}:")
            for grp in groups:
                tui.console.print(f"      {_fmt_dep_group(grp)}")


def _print_deps(session, *extras) -> None:
    """`print deps <name>` — flat depends + pre-depends list (no other fields)."""
    if not extras:
        tui.console.print("Usage: print deps <name>")
        return
    if not _require_dep_check(session):
        return
    name = extras[0]
    pkgs = session.dep_tree.selected_pkgs
    if name not in pkgs:
        tui.console.print(f"Package {name!r} is not in the current dep tree.")
        return
    p = pkgs[name]
    tui.console.print(f"{name} dependencies:")
    if p.pre_depends or p.alt_pre_depends:
        tui.console.print("  Pre-Depends:")
        for d in p.pre_depends:
            tui.console.print(f"    {_fmt_dep(d)}")
        for grp in p.alt_pre_depends:
            tui.console.print(f"    {_fmt_dep_group(grp)}")
    if p.depends or p.alt_depends:
        tui.console.print("  Depends:")
        for d in p.depends:
            tui.console.print(f"    {_fmt_dep(d)}")
        for grp in p.alt_depends:
            tui.console.print(f"    {_fmt_dep_group(grp)}")
    if not (p.depends or p.alt_depends or p.pre_depends or p.alt_pre_depends):
        tui.console.print("  (no dependencies)")


# ─── Source views ───────────────────────────────────────────────────────────

def _print_sources(session, *_extras) -> None:
    """All selected source packages with mirror + version."""
    if not _require_dep_check(session):
        return
    srcs = session.dep_tree.selected_srcs
    tui.console.print(f"Selected source packages ({len(srcs)}):")
    for name in sorted(srcs):
        s = srcs[name]
        _mirror = getattr(s, '_mirror', None)
        _mirror_id = getattr(_mirror, 'id', '?') if _mirror else '?'
        tui.console.print(f"  {name:<40} {str(s.version):<24} [{_mirror_id}]")


def _print_src(session, *extras) -> None:
    """`print src <name>` — full record for one source package."""
    if not extras:
        tui.console.print("Usage: print src <name>")
        return
    if not _require_dep_check(session):
        return
    name = extras[0]
    srcs = session.dep_tree.selected_srcs
    if name not in srcs:
        tui.console.print(f"Source {name!r} is not in the current dep tree.")
        tui.console.print("  (`print sources` lists everything that is.)")
        return
    s = srcs[name]
    _mirror = getattr(s, '_mirror', None)
    _mirror_id = getattr(_mirror, 'id', '?') if _mirror else '?'

    tui.console.print(f"Source: {s.package}")
    tui.console.print(f"  Version       : {s.version}")
    tui.console.print(f"  Mirror        : {_mirror_id}")
    if getattr(s, 'directory', ''):
        tui.console.print(f"  Directory     : {s.directory}")
    if getattr(s, 'binary', None):
        tui.console.print(f"  Binary pkgs   : {len(s.binary)}")
        for b in sorted(s.binary):
            tui.console.print(f"      {b}")
    if getattr(s, 'files', None):
        tui.console.print("  Files (.dsc / .orig.tar.* / .debian.tar.*):")
        for fname, meta in sorted(s.files.items()):
            _size = meta.get('size', '?')
            tui.console.print(f"      {fname}  ({_size} B)")
    if getattr(s, 'pkgs', None):
        tui.console.print(f"  Produced .debs: {len(s.pkgs)}")
        for d in sorted(s.pkgs):
            tui.console.print(f"      {d}")


# ─── Relations / debugging views ────────────────────────────────────────────

def _print_provides(session, *_extras) -> None:
    """Virtual-package map — only entries with multiple providers, since the
    full Provides graph is enormous and most entries have a single provider."""
    if not _require_dep_check(session):
        return
    virtuals = defaultdict(list)
    for name, pkg in session.dep_tree.selected_pkgs.items():
        canonical = pkg['Package']
        if name != canonical:
            virtuals[name].append((canonical, pkg.version))
    multi = {v: ps for v, ps in virtuals.items() if len(ps) > 1}
    tui.console.print(f"Virtual packages with multiple providers ({len(multi)}):")
    if not multi:
        tui.console.print("  (every virtual name has a single provider — no contention)")
        return
    for v in sorted(multi):
        tui.console.print(f"  {v}")
        for cn, ver in sorted(multi[v]):
            tui.console.print(f"      → {cn} ({ver})")


# ─── Help screen ────────────────────────────────────────────────────────────

def _print_help(_session=None, *_extras) -> None:
    """Show the available `print` categories grouped by intent.

    Iterates the CATEGORIES dict so any newly-registered handler appears
    automatically with no help-text edit needed.
    """
    by_group = defaultdict(list)
    for name, (_handler, group, desc) in CATEGORIES.items():
        by_group[group].append((name, desc))

    tui.console.print("print — display build state")
    tui.console.print("Usage: print <category> [args]")
    tui.console.print("")
    _max = max(len(name) for name in CATEGORIES) + 8  # room for ' <name>' suffix
    for group in _HELP_GROUP_ORDER:
        if group not in by_group:
            continue
        tui.console.print(f"{group}:")
        for name, desc in sorted(by_group[group]):
            # Categories whose description starts with 'usage:' carry an arg
            # in their displayed name so the user sees the call shape.
            tui.console.print(f"  {name:<{_max}}  {desc}")
        tui.console.print("")


# ─── Dispatch table ─────────────────────────────────────────────────────────
# Adding a category: write _print_<name>(session, *extras), append a row.
# (handler, group label for help screen, one-line description)

CATEGORIES = {
    # Configuration
    'config':    (_print_config,    'Configuration', 'overall build configuration values'),
    'mirrors':   (_print_mirrors,   'Configuration', 'configured mirrors with URL, suite, component'),
    'snapshot':  (_print_snapshot,  'Configuration', 'snapshot pin status (configured + resolved date)'),
    'paths':     (_print_paths,     'Configuration', 'every directory the build uses'),

    # Build state
    'state':     (_print_state,     'Build state',   'pipeline stage progress (which stages are done)'),
    'stats':     (_print_stats,     'Build state',   'high-level counts across cache, dep tree, sources'),

    # Packages
    'required':  (_print_required,  'Packages',      "packages with 'required' priority from APT cache"),
    'important': (_print_important, 'Packages',      "packages with 'important' priority from APT cache"),
    'selected':  (_print_selected,  'Packages',      'packages resolved by parse_dependency'),
    'tunneled':  (_print_tunneled,  'Packages',      'packages set to use prebuilt .debs (Tunneled list)'),
    'pkg':       (_print_pkg,       'Packages',      'full package detail — usage: print pkg <name>'),
    'deps':      (_print_deps,      'Packages',      'flat dep list of a package — usage: print deps <name>'),

    # Sources
    'sources':   (_print_sources,   'Sources',       'all selected source packages with mirror + version'),
    'src':       (_print_src,       'Sources',       'full source detail — usage: print src <name>'),

    # Relations
    'provides':  (_print_provides,  'Relations',     'virtual packages with multiple providers'),

    # Meta
    'help':      (_print_help,      'Meta',          'this help'),
}

# Order in which sections appear in `print help`.
_HELP_GROUP_ORDER = [
    'Configuration',
    'Build state',
    'Packages',
    'Sources',
    'Relations',
    'Meta',
]


def dispatch(session: 'BuildSession', category: str, *extras) -> None:
    """Route to a category handler.  Empty / unknown categories show help.

    Extras (additional args after the category) are forwarded to the handler
    for parametrized views like `print pkg <name>`.
    """
    if not category:
        _print_help(session)
        return
    entry = CATEGORIES.get(category)
    if entry is None:
        tui.console.print(f"Unknown print category: {category!r}")
        tui.console.print("Try: print help")
        return
    handler, _group, _desc = entry
    handler(session, *extras)
