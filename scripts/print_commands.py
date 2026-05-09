"""Handlers for the `print` TUI command.

`cmd_print()` in build.py is a thin dispatcher into this module — the actual
view code lives here.  Each handler receives the live ``BuildSession`` so it
can read ``self.config`` / ``self.cache`` / ``self.dep_tree`` and writes to
the console tab via ``tui.console.print``.

Adding a new view: write a ``_print_<category>(session)`` function and
register it in :data:`CATEGORIES` with a one-line description.  The help
screen (``print help``) and the unknown-category error path pick it up
automatically; no changes to build.py or the TUI registration line needed.
"""
from typing import TYPE_CHECKING

import tui
import utils

if TYPE_CHECKING:
    from build import BuildSession


# ─── Category handlers ──────────────────────────────────────────────────────

def _print_config(session: 'BuildSession') -> None:
    cfg = session.config
    tui.console.print("Build Configuration:")
    tui.console.print(f"    Arch                : {cfg.arch}")
    tui.console.print(f"    Release             : {cfg.release}")
    tui.console.print(f"    Base ID (default)   : {cfg.baseid}")
    tui.console.print(f"    Parent version      : {cfg.baseversion}")
    tui.console.print("    Mirrors             :")
    for _m in cfg.mirrors:
        tui.console.print(f"      [{_m.id:8}] {_m.url}  {_m.suite}  {_m.component}")
    if cfg.snapshot_enabled:
        try:
            _resolved = utils.resolve_snapshot_timestamp(cfg)
        except (RuntimeError, ValueError) as e:
            _resolved = f"<unresolved: {e}>"
        tui.console.print("    Snapshot            : enabled")
        tui.console.print(f"      Configured        : {cfg.snapshot_timestamp_config}")
        tui.console.print(f"      Resolved          : {_resolved}")
    else:
        tui.console.print("    Snapshot            : disabled (live mirrors)")
    tui.console.print(f"    Build codename      : {cfg.build_codename}")
    tui.console.print(f"    Build version       : {cfg.build_version}")
    tui.console.print(f"    Config file         : {cfg.config_path}")
    tui.console.print(f"    Package list        : {cfg.pkglist_path}")
    tui.console.print(f"    Working dir         : {cfg.working_dir}")


def _print_required(session: 'BuildSession') -> None:
    pkgs = session.cache.required
    tui.console.print(f"Required packages ({len(pkgs)}):")
    for pkg in sorted(pkgs):
        tui.console.print(f"  {pkg}")


def _print_important(session: 'BuildSession') -> None:
    pkgs = session.cache.important
    tui.console.print(f"Important packages ({len(pkgs)}):")
    for pkg in sorted(pkgs):
        tui.console.print(f"  {pkg}")


def _print_selected(session: 'BuildSession') -> None:
    if not session.flags.dep_check_ready:
        tui.console.print("Run 'parse_dependency' first")
        return
    pkgs = session.dep_tree.selected_pkgs
    # Filter out virtual-package alias entries — only show canonical names.
    real_pkgs = {k: v for k, v in pkgs.items() if k == v['Package']}
    tui.console.print(f"Selected packages ({len(real_pkgs)}):")
    for name in sorted(real_pkgs.keys()):
        tui.console.print(f"  {name:<40} {real_pkgs[name].version}")


def _print_help(_session) -> None:
    """Show the available `print` categories and what each prints.

    Takes a session arg for signature consistency with the other handlers
    but does not read it — help is always available, even before any
    pipeline stage has run.
    """
    tui.console.print("print — display build state")
    tui.console.print("Usage: print <category>")
    tui.console.print("")
    _max = max(len(name) for name in CATEGORIES)
    for name, (_handler, desc) in CATEGORIES.items():
        tui.console.print(f"  {name:<{_max}}  {desc}")


# ─── Dispatch table ─────────────────────────────────────────────────────────
# (handler, one-line description shown by `print help`)
CATEGORIES = {
    'config':    (_print_config,    'active build configuration values (mirrors, snapshot, paths)'),
    'required':  (_print_required,  "packages with 'required' priority from the APT cache"),
    'important': (_print_important, "packages with 'important' priority from the APT cache"),
    'selected':  (_print_selected,  "packages resolved by parse_dependency (run that first)"),
    'help':      (_print_help,      'this help'),
}


def dispatch(session: 'BuildSession', category: str) -> None:
    """Route to a category handler.  Empty / unknown categories show help."""
    if not category:
        _print_help(session)
        return
    entry = CATEGORIES.get(category)
    if entry is None:
        tui.console.print(f"Unknown print category: {category!r}")
        tui.console.print("Try: print help")
        return
    handler, _ = entry
    handler(session)
