"""SELECT-02 prototype — order-independent OR-dependency resolution.

The live resolver (`dependencytree.parse_dependency`) decides each
``a | b`` alternative group *inline* during a depth-first walk: it uses an
alternative that is already in the in-progress closure, otherwise it pulls
the first declared alternative.  Because "already in the closure" depends
on what has been resolved *so far*, the final closure depends on seed /
pkg-group ORDER, not just the seed SET — e.g. ``xorg`` Depends
``xterm | x-terminal-emulator`` while ``gnome-terminal`` Provides
``x-terminal-emulator``, so whether ``xterm`` (+ its dep ``libutempter0``)
is pulled flips with group order.

This module is a SELF-CONTAINED reference implementation of an
order-independent resolution, proven against the live greedy behaviour in
the test-suite (``test_or_resolve_*``).  It is NOT yet wired into the live
multi-pass resolver — that integration is a separate, risk-budgeted step
(the live resolver spreads resolution across ~10 ``resolve_packages``
passes with intermediate tier snapshots; this function is the algorithm
those passes would adopt).

Algorithm (closure as a pure function of the seed SET):

  Pass A — pull the seeds and their HARD (non-alternative) dependencies to
           a fixpoint.  Alternative groups are deferred.
  Pass B — scan every selected package's OR groups; a group is satisfied if
           ANY alternative is in the closure (directly or via Provides).
           If any group is unsatisfied, pull the first alternative of the
           canonically-smallest unsatisfied group, then loop back to Pass A.

  The canonical tie-break (smallest group tuple) is what removes the
  order-dependence: which unsatisfied group is resolved first is a function
  of the group contents, never of the input ordering.  Iterating A/B to a
  fixpoint yields the minimal closure for the set.
"""
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

# A dependency entry is either a hard dep (a single name) or an OR group
# (a sequence of alternative names).  This mirrors how a package's parsed
# dependency list naturally reads: ['libc6', ('xterm', 'x-terminal-emulator')].
DepEntry = Union[str, Sequence[str]]


def _provider_index(provides: Dict[str, Iterable[str]]) -> Dict[str, List[str]]:
    """virtual-name -> sorted list of real packages that Provide it."""
    _idx: Dict[str, List[str]] = defaultdict(list)
    for _pkg, _names in provides.items():
        for _n in _names:
            _idx[_n].append(_pkg)
    for _n in _idx:
        _idx[_n].sort()
    return dict(_idx)


def _or_groups(entry_list: Iterable[DepEntry]) -> List[Tuple[str, ...]]:
    """The OR groups in a dependency list (hard single-name deps dropped)."""
    _out: List[Tuple[str, ...]] = []
    for _d in entry_list:
        if not isinstance(_d, str):
            _out.append(tuple(_d))
    return _out


def resolve_closure(
    seeds: Iterable[str],
    deps: Dict[str, Sequence[DepEntry]],
    real_pkgs: Optional[Set[str]] = None,
    provides: Optional[Dict[str, Iterable[str]]] = None,
) -> Set[str]:
    """Resolve the selection closure as a pure function of the seed SET.

    ``deps[pkg]`` is a sequence whose elements are either a hard-dep name
    (str) or an OR group (a sequence of alternative names).  ``provides``
    maps a package to the virtual names it Provides.  ``real_pkgs`` is the
    universe of real package names; when omitted every name mentioned is
    treated as real (the common case for the synthetic test graphs).

    Returns the set of selected real package names — identical for any
    ordering of ``seeds`` (this is the property the live resolver lacks).
    """
    # Materialize once: `seeds` is an Iterable and is consumed BOTH by
    # _infer_real below AND the _pending comprehension; a one-shot generator
    # would be exhausted by the first pass, yielding an empty closure on the
    # default (real_pkgs=None) path.
    seeds = list(seeds)
    _provides = provides or {}
    _prov_index = _provider_index(_provides)
    _real = real_pkgs if real_pkgs is not None else _infer_real(seeds, deps, _provides)

    def _pull_name(name: str) -> str:
        """The real package to add for a (hard) dep on ``name``: the real
        package itself (apt's real-beats-virtual), else its canonical
        Provides provider, else the name as an unknown leaf."""
        if name in _real:
            return name
        _providers = _prov_index.get(name)
        if _providers:
            return _providers[0]
        return name

    def _satisfied(name: str, closure: Set[str]) -> bool:
        """An alternative ``name`` is satisfied by the closure if the name
        itself is selected or any package Providing it is selected."""
        if name in closure:
            return True
        return any(_p in closure for _p in _prov_index.get(name, []))

    _closure: Set[str] = set()
    _pending: Set[str] = {_pull_name(_s) for _s in seeds}

    while True:
        # ── Pass A: hard-dependency fixpoint (OR groups deferred) ──────────
        _frontier = set(_pending)
        _pending = set()
        while _frontier:
            _pkg = _frontier.pop()
            if _pkg in _closure:
                continue
            _closure.add(_pkg)
            for _d in deps.get(_pkg, []):
                if isinstance(_d, str):
                    _r = _pull_name(_d)
                    if _r not in _closure:
                        _frontier.add(_r)

        # ── Pass B: resolve OR groups against the SETTLED closure ──────────
        _unsat: Set[Tuple[str, ...]] = set()
        for _pkg in _closure:
            for _group in _or_groups(deps.get(_pkg, [])):
                if not any(_satisfied(_a, _closure) for _a in _group):
                    _unsat.add(_group)
        if not _unsat:
            return _closure

        # Canonical tie-break — the smallest unsatisfied group, so the choice
        # is a function of the graph, not the input ordering.  Pull its first
        # declared alternative (Debian convention) and re-settle.
        _first = min(_unsat)
        _pending.add(_pull_name(_first[0]))


def _infer_real(
    seeds: Iterable[str],
    deps: Dict[str, Sequence[DepEntry]],
    provides: Dict[str, Iterable[str]],
) -> Set[str]:
    """Default real-package universe: every name that appears as a package
    key, a seed, or a dependency name (alternatives included).  Names that
    are ONLY ever Provided (never a real key) stay virtual."""
    _real: Set[str] = set(seeds) | set(deps.keys())
    for _entries in deps.values():
        for _d in _entries:
            if isinstance(_d, str):
                _real.add(_d)
            else:
                _real.update(_d)
    # A name that is genuinely virtual (only Provided, no real package of
    # that name) must NOT be treated as real, or _pull_name would add the
    # virtual name instead of a provider.
    _virtual_only = {
        _n for _names in provides.values() for _n in _names
    } - set(deps.keys())
    return _real - _virtual_only


def resolve_closure_greedy(
    seeds_ordered: Sequence[str],
    deps: Dict[str, Sequence[DepEntry]],
    real_pkgs: Optional[Set[str]] = None,
    provides: Optional[Dict[str, Iterable[str]]] = None,
) -> Set[str]:
    """Reference model of the LIVE inline resolver's order-sensitive
    behaviour (``dependencytree.parse_dependency`` lines 590-622): a
    depth-first walk in seed order where an OR group is satisfied by an
    already-selected alternative, else pulls the first declared one.

    Used only by the tests to demonstrate that the greedy closure DIVERGES
    across seed orderings while :func:`resolve_closure` does not.
    """
    _provides = provides or {}
    _prov_index = _provider_index(_provides)
    _real = real_pkgs if real_pkgs is not None else _infer_real(
        seeds_ordered, deps, _provides)
    _closure: Set[str] = set()

    def _pull_name(name: str) -> str:
        if name in _real:
            return name
        _providers = _prov_index.get(name)
        return _providers[0] if _providers else name

    def _satisfied(name: str) -> bool:
        if name in _closure:
            return True
        return any(_p in _closure for _p in _prov_index.get(name, []))

    def _visit(name: str) -> None:
        _pkg = _pull_name(name)
        if _pkg in _closure:
            return
        _closure.add(_pkg)
        for _d in deps.get(_pkg, []):
            if isinstance(_d, str):
                _visit(_d)
            else:                                  # OR group, resolved inline
                if any(_satisfied(_a) for _a in _d):
                    continue                       # an alt already present
                _visit(_d[0])                      # else pull the first

    for _s in seeds_ordered:
        _visit(_s)
    return _closure
