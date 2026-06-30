"""Per-surface package-set composition.

A "surface" is one shipped artifact: the LIVE image, the DISK image, or the
installer ISO's /cdrom/pool.  Each surface is defined by a SEED set (pkg.list
groups + list files + d-i roots) and materialized as the **reachability
closure of those seeds over the RESOLVED dependency graph**.

Why a fresh closure and never the per-group deltas: ``pkg_group_pkg_names[g]``
is CREDIT-based — a dependency shared by two groups is credited to whichever
group resolved first (declaration-order dependent, same root cause as the
deferred order-independent-OR-resolution TODO).  Unioning credited deltas
silently drops shared deps from any surface that includes the later group.
The closure below walks the edges the resolver actually chose
(``Package.depends``/``pre_depends`` + the first SELECTED alternative of each
OR group, virtual names canonicalized) — order-independent, self-contained.
Edge semantics mirror ``chroot._resolve_depends``/``_resolve_pre_depends``.
"""

from typing import Iterable, List, Optional, Set

import utils


def _canonical(selected: dict, name: str) -> 'Optional[str]':
    """Canonical Package name for `name` (virtual → real), None if not
    selected."""
    _pkg = selected.get(name)
    if _pkg is None:
        return None
    return str(_pkg['Package'])


def _hard_dep_edges(selected: dict, canonical_name: str) -> 'List[str]':
    """Canonical names this package hard-depends on (Depends ∪ Pre-Depends),
    restricted to selected packages.  OR groups contribute the FIRST
    alternative present in selected_pkgs — the resolver's actual choice.
    Mirrors chroot._resolve_depends/_resolve_pre_depends semantics."""
    _pkg = selected.get(canonical_name)
    if _pkg is None:
        return []
    _out: 'List[str]' = []
    _seen: 'Set[str]' = set()

    def _add(_name: str) -> None:
        _c = _canonical(selected, _name)
        if _c is not None and _c != canonical_name and _c not in _seen:
            _seen.add(_c)
            _out.append(_c)

    for _dep in list(getattr(_pkg, 'depends', [])) + \
            list(getattr(_pkg, 'pre_depends', [])):
        _add(_dep[0])
    for _group in list(getattr(_pkg, 'alt_depends', [])) + \
            list(getattr(_pkg, 'alt_pre_depends', [])):
        for _alt in _group:
            if _alt[0] in selected:
                _add(_alt[0])
                break
    return _out


def surface_closure(dep_tree, seed_names: 'Iterable[str]',
                    include_recommends_extras: bool = False) -> 'Set[str]':
    """The surface's package set: canonical names reachable from
    `seed_names` over hard-dependency edges within the selected universe.

    Seeds may be virtual (canonicalized) or absent from the selection
    (silently skipped — callers validate seed resolution separately, e.g.
    the cmd_build pre-flight).

    ``include_recommends_extras=True`` additionally pulls every
    ``extras_pkg_names`` member (depth-1 Recommends pulled by the parse)
    that is RECOMMENDED BY a closure member, plus that extra's own hard
    closure — iterated to fixpoint, bounded by the selected universe.
    Extras nobody in the surface recommends stay out.
    """
    _selected = dep_tree.selected_pkgs
    _closure: 'Set[str]' = set()
    _stack: 'List[str]' = []
    for _s in seed_names:
        _c = _canonical(_selected, _s)
        if _c is not None and _c not in _closure:
            _closure.add(_c)
            _stack.append(_c)
    while _stack:
        _n = _stack.pop()
        for _d in _hard_dep_edges(_selected, _n):
            if _d not in _closure:
                _closure.add(_d)
                _stack.append(_d)

    if include_recommends_extras:
        _extras_canon: 'Set[str]' = set()
        for _e in getattr(dep_tree, 'extras_pkg_names', set()):
            _c = _canonical(_selected, _e)
            if _c is not None:
                _extras_canon.add(_c)
        # Scan only the FRONTIER of newly-added nodes each round (first round
        # = the whole closure) rather than rescanning the entire closure every
        # round — equivalent fixpoint, far fewer rescans on large closures.
        _frontier: 'Set[str]' = set(_closure)
        while _frontier:
            _wanted: 'Set[str]' = set()
            for _n in _frontier:
                _pkg = _selected.get(_n)
                if _pkg is None:
                    continue
                for _rec in getattr(_pkg, 'recommends', []):
                    _c = _canonical(_selected, _rec[0])
                    if _c is not None and _c in _extras_canon \
                            and _c not in _closure:
                        _wanted.add(_c)
            _new_nodes: 'Set[str]' = set()
            for _w in sorted(_wanted):
                _closure.add(_w)
                _new_nodes.add(_w)
                _stack.append(_w)
            while _stack:
                _n = _stack.pop()
                for _d in _hard_dep_edges(_selected, _n):
                    if _d not in _closure:
                        _closure.add(_d)
                        _new_nodes.add(_d)
                        _stack.append(_d)
            _frontier = _new_nodes
    return _closure


def group_seed_names(pkglist_path: str,
                     groups: 'Iterable[str]') -> 'Set[str]':
    """Union of pkg.list seed names for the requested `[group]`s.  Unknown
    group names yield nothing here — the cmd_build pre-flight validates
    group existence loudly."""
    try:
        _all = utils.parse_pkg_list_groups(pkglist_path)
    except (OSError, ValueError):
        return set()
    _out: 'Set[str]' = set()
    for _g in groups:
        _out.update(_all.get(_g, []))
    return _out


def read_flat_roots(path: str) -> 'List[str]':
    """Seed names from a flat list file (one per line, `#` comments) —
    used for config/installer-defaults.list and live.list roots.
    Missing/unreadable → empty (callers validate)."""
    _out: 'List[str]' = []
    try:
        _raw = utils.readfile(path).split('\n')
    except OSError:
        return _out
    for _line in _raw:
        _name = _line.strip()
        if _name and not _name.startswith('#'):
            _out.append(_name)
    return _out
