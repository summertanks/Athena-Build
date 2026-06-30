"""Build-closure computation + tier segregation.

When ``[Source] IncludeBuildClosure`` is on, the package selection is widened
to include the *build* closure — the ``Build-Depends`` of every selected
source, resolved transitively over the package universe — so the toolchain
that builds the distro is itself built from and served by our own mirror:
reproducible builds with no reach-out to snapshot.debian.org.

The cache-parse path then partitions the expanded closure into three tiers
(via ``classify_tiers`` — the single tiering path) so it can be built in
stages, toolchain first:

  ``toolchain``  the install closure of ``build-essential`` + ``dpkg-dev`` —
                 the universal base every ``dpkg-buildpackage`` needs (gcc/g++,
                 libc6-dev, make, binutils, ...).
  ``language``   build systems and language runtimes layered on the toolchain
                 (debhelper, cmake/meson/ninja, pkg-config, autotools, and the
                 python / perl / java / rust / node / go build stacks), plus
                 their install closure — minus whatever's already toolchain.
  ``leaf``       every remaining build-dep binary: the long tail of ``-dev``
                 libraries and tools specific to individual sources.

This module is pure data-in / data-out — it takes already-parsed index dicts
and returns name sets.  No I/O, no config object, no apt_pkg — so it is
unit-testable in isolation and reusable from the cache-parse call site, an
analysis script, or a test.
"""
from typing import Dict, Iterable, List, Optional, Set

from debian.deb822 import PkgRelation

# Shared immutable empty set for the provides-intersection fast path.
_EMPTY_SET: 'frozenset[str]' = frozenset()

# ── Tier seeds ──────────────────────────────────────────────────────────────
# Edit these to re-shape the tiers; they are intentionally explicit (not
# pattern-matched) so the toolchain/language boundary is auditable.  Anything
# in the build closure not reachable from these seeds falls to ``leaf``.

TOOLCHAIN_SEEDS: 'frozenset[str]' = frozenset({
    'build-essential', 'dpkg-dev',
})

# Build systems + language runtimes/compilers.  Names that may be absent from
# a given universe are fine — _install_expand simply ignores unknown seeds.
LANGUAGE_SEEDS: 'frozenset[str]' = frozenset({
    # debhelper / packaging glue
    'debhelper', 'debhelper-compat', 'dh-python', 'dh-autoreconf', 'dh-exec',
    # generic build systems
    'cmake', 'meson', 'ninja-build', 'pkg-config', 'pkgconf',
    'autoconf', 'automake', 'libtool', 'gettext', 'intltool', 'scons',
    # parser/codegen tools that gate many builds
    'bison', 'flex', 'swig', 'gperf',
    # python
    'python3', 'python3-dev', 'python3-setuptools', 'python3-pip', 'cython3',
    # perl
    'perl', 'perl-xs-dev',
    # jvm
    'default-jdk', 'default-jdk-headless', 'javahelper', 'maven',
    # rust / node / go
    'rustc', 'cargo', 'nodejs', 'golang-go', 'golang-any',
    # other compilers
    'gfortran', 'gnat', 'valac', 'ghc', 'ocaml-nox',
})


def _pick(or_group: 'list', bin_index: dict,
          provides_index: dict) -> 'Optional[str]':
    """Choose one concrete package name that satisfies an OR-group, honouring
    Provides.  Mirrors apt's first-alternative preference: the first listed
    alternative that is a real package wins; otherwise the first that has any
    provider in the universe.  Returns None when nothing in the universe can
    satisfy the group (caller records it as unsatisfiable)."""
    for _rel in or_group:
        _name = _rel.get('name', '')
        if not _name:
            continue
        if _name in bin_index:
            return _name
        _providers = provides_index.get(_name)
        if _providers:
            return sorted(_providers)[0]
    return None


def _rels(raw: str) -> 'list':
    raw = (raw or '').strip()
    return PkgRelation.parse_relations(raw) if raw else []


def _arch_applies(arch_list: 'Optional[list]', build_arch: str) -> bool:
    """Whether a relation qualified ``[...]`` applies when building for
    *build_arch*.  Conservative on wildcards (``any``/``linux-any``/``any-amd64``
    …): an unrecognised pattern leaves the relation IN, because over-fetching a
    build-dep is harmless whereas dropping a needed one breaks the build."""
    if not arch_list:
        return True
    _pos = [_a for (_en, _a) in arch_list if _en]
    _neg = [_a for (_en, _a) in arch_list if not _en]
    if any(('any' in _a) or ('-' in _a) for _a in _pos + _neg):
        return True
    if _pos:
        return build_arch in _pos
    if _neg:
        return build_arch not in _neg
    return True


def _profiles_apply(restr: 'Optional[list]', profiles: 'frozenset[str]') -> bool:
    """Whether a relation's build-profile restriction ``<...> <...>`` is
    satisfied by the active *profiles* set (OR across groups, AND within a
    group; an enabled term needs its profile active, a negated term needs it
    inactive).  ``<stage1>`` drops when no profiles are active; ``<!nocheck>``
    stays."""
    if not restr:
        return True
    for _grp in restr:
        if all(((_p in profiles) == _en) for (_en, _p) in _grp):
            return True
    return False


def _filter_grp(or_group: 'list', build_arch: 'Optional[str]',
                profiles: 'Optional[frozenset]') -> 'list':
    """Drop the relations in *or_group* that do not apply to the target arch /
    active build profiles.  When both filters are ``None`` the group is returned
    unchanged (the historical, unfiltered behaviour)."""
    if build_arch is None and profiles is None:
        return or_group
    _out = []
    for _r in or_group:
        if build_arch is not None and not _arch_applies(_r.get('arch'),
                                                         build_arch):
            continue
        if profiles is not None and not _profiles_apply(_r.get('restrictions'),
                                                        profiles):
            continue
        _out.append(_r)
    return _out


def _install_expand(seeds: 'Iterable[str]', bin_index: dict,
                    provides_index: dict,
                    build_arch: 'Optional[str]' = None,
                    profiles: 'Optional[frozenset]' = None) -> 'Set[str]':
    """The install closure of *seeds* over hard edges (Depends + Pre-Depends),
    resolved within ``bin_index``.  Unknown seeds and unsatisfiable groups are
    silently skipped — this is a closure, not a validator."""
    _inset: 'Set[str]' = {s for s in seeds if s in bin_index}
    # Pre-set-ify the provides lists once: the satisfied-check below otherwise
    # rebuilds a set() per relation per group per package on the hot frontier.
    _prov_sets: 'Dict[str, Set[str]]' = {
        _k: set(_v) for _k, _v in provides_index.items()}
    # sorted(), not list(): set iteration order over str keys is
    # PYTHONHASHSEED-randomized, and the OR-group satisfied-check below
    # short-circuits on whatever is already in _inset — so an unsorted frontier
    # makes a genuine OR-group (e.g. default-mta|mail-transport-agent) resolve
    # to a different provider run-to-run, drifting the closure (and the mirror
    # download manifest it feeds).  _pick is already deterministic
    # (sorted(_providers)[0]); seeding sorted makes the whole traversal so.
    _frontier: 'List[str]' = sorted(_inset)
    while _frontier:
        _n = _frontier.pop()
        _entry = bin_index.get(_n)
        if not _entry:
            continue
        for _field in ('Depends', 'Pre-Depends'):
            for _grp in _rels(_entry.get(_field, '')):
                if not _grp:
                    continue
                _grp = _filter_grp(_grp, build_arch, profiles)
                if not _grp:
                    continue
                # already satisfied by something in the set?
                if any((_r.get('name') in _inset)
                       or (_prov_sets.get(_r.get('name', ''), _EMPTY_SET)
                           & _inset)
                       for _r in _grp):
                    continue
                _pk = _pick(_grp, bin_index, provides_index)
                if _pk and _pk not in _inset:
                    _inset.add(_pk)
                    _frontier.append(_pk)
    return _inset


def classify_tiers(
    members: 'Iterable[str]',
    adjacency: 'Dict[str, Iterable[str]]',
    toolchain_seeds: 'frozenset[str]' = TOOLCHAIN_SEEDS,
    language_seeds: 'frozenset[str]' = LANGUAGE_SEEDS,
) -> 'Dict[str, Set[str]]':
    """Partition *members* into toolchain / language / leaf using a forward
    dependency adjacency (``{name: [dep_name, ...]}``).

    Used by the cache-parse call site, where the dep-tree resolver has already
    expanded the build closure and recorded ``package.depends_on`` edges — so
    we classify the resulting set rather than re-expanding it.

      toolchain : members reachable (over adjacency) from toolchain_seeds.
      language  : members reachable from language_seeds, minus toolchain.
      leaf      : everything else.
    """
    _members: 'Set[str]' = set(members)

    def _reach(seeds: 'Iterable[str]') -> 'Set[str]':
        _out: 'Set[str]' = set()
        _seen: 'Set[str]' = set()
        _stack: 'List[str]' = list(seeds)
        while _stack:
            _n = _stack.pop()
            if _n in _seen:
                continue
            _seen.add(_n)
            if _n in _members:
                _out.add(_n)
            for _d in adjacency.get(_n, ()):
                if _d not in _seen:
                    _stack.append(_d)
        return _out

    _toolchain = _reach(toolchain_seeds)
    _language = _reach(language_seeds) - _toolchain
    _leaf = _members - _toolchain - _language
    return {'toolchain': _toolchain, 'language': _language, 'leaf': _leaf}


def compute_build_closure(
    selected_srcs: 'Iterable[str]',
    src_build_depends: 'Dict[str, str]',
    bin_index: 'Dict[str, dict]',
    provides_index: 'Dict[str, list]',
    build_arch: 'Optional[str]' = None,
    build_profiles: 'Optional[frozenset]' = None,
) -> 'Dict[str, object]':
    """Compute the full build closure: the transitive install closure (over
    hard Depends + Pre-Depends edges) of every selected source's Build-Depends,
    plus the universal toolchain base (build-essential + dpkg-dev).

    Args:
      selected_srcs:     source names whose Build-Depends seed the closure.
      src_build_depends: {src_name: raw Build-Depends string} (Build-Depends +
                         Build-Depends-Indep + Build-Depends-Arch, joined).
      bin_index:         {bin_name: {'Depends':.., 'Pre-Depends':..}} universe.
      provides_index:    {virtual_name: [provider_name, ...]}.
      build_arch:        target architecture; when given, relations whose
                         ``[arch ...]`` qualifier excludes it are dropped
                         (wildcards are kept conservatively).  None ⇒ no arch
                         filtering (historical behaviour).
      build_profiles:    active build-profile set; when given, relations whose
                         ``<profile ...>`` restriction is unsatisfied are
                         dropped (e.g. ``<stage1>`` with an empty set).  None ⇒
                         no profile filtering.

    Returns a dict:
      'all'           : Set[str] full build closure (toolchain base included).
      'unsatisfiable' : List[(src, group_str)] Build-Depends groups dropped
                        because nothing in the universe satisfies them.

    Tier segregation (toolchain / language / leaf) is NOT done here — the
    cache-parse path expands the closure through the dep-tree resolver and
    partitions the result with ``classify_tiers``, the single tiering path.
    """
    # 1. direct build-dep binaries across every selected source
    _direct: 'Set[str]' = set()
    _unsat: 'List[tuple]' = []
    for _src in selected_srcs:
        for _grp in _rels(src_build_depends.get(_src, '')):
            if not _grp:
                continue
            _grp = _filter_grp(_grp, build_arch, build_profiles)
            if not _grp:
                # Entire group is arch-/profile-excluded — not applicable to
                # this build, so it is NOT unsatisfiable; just skip it.
                continue
            _pk = _pick(_grp, bin_index, provides_index)
            if _pk:
                _direct.add(_pk)
            else:
                _unsat.append((_src, PkgRelation.str([_grp])))

    # 2. transitive install closure of the direct build-deps + toolchain base
    _all = _install_expand(_direct | set(TOOLCHAIN_SEEDS),
                           bin_index, provides_index,
                           build_arch, build_profiles)

    return {'all': _all, 'unsatisfiable': _unsat}
