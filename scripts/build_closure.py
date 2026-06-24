"""Build-closure computation + tier segregation.

When ``[Source] IncludeBuildClosure`` is on, the package selection is widened
to include the *build* closure — the ``Build-Depends`` of every selected
source, resolved transitively over the package universe — so the toolchain
that builds the distro is itself built from and served by our own mirror:
reproducible builds with no reach-out to snapshot.debian.org.

The added set is segregated into three tiers so it can be built in stages,
toolchain first:

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


def _install_expand(seeds: 'Iterable[str]', bin_index: dict,
                    provides_index: dict) -> 'Set[str]':
    """The install closure of *seeds* over hard edges (Depends + Pre-Depends),
    resolved within ``bin_index``.  Unknown seeds and unsatisfiable groups are
    silently skipped — this is a closure, not a validator."""
    _inset: 'Set[str]' = {s for s in seeds if s in bin_index}
    _frontier: 'List[str]' = list(_inset)
    while _frontier:
        _n = _frontier.pop()
        _entry = bin_index.get(_n)
        if not _entry:
            continue
        for _field in ('Depends', 'Pre-Depends'):
            for _grp in _rels(_entry.get(_field, '')):
                if not _grp:
                    continue
                # already satisfied by something in the set?
                if any((_r.get('name') in _inset)
                       or (set(provides_index.get(_r.get('name', ''), ()))
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
    runtime_closure: 'Optional[Iterable[str]]' = None,
) -> 'Dict[str, object]':
    """Compute the tiered build closure.

    Args:
      selected_srcs:     source names whose Build-Depends seed the closure.
      src_build_depends: {src_name: raw Build-Depends string} (Build-Depends +
                         Build-Depends-Indep + Build-Depends-Arch, joined).
      bin_index:         {bin_name: {'Depends':.., 'Pre-Depends':..}} universe.
      provides_index:    {virtual_name: [provider_name, ...]}.
      runtime_closure:   optional set of binaries already shipped — used only
                         to report the NEW additions (``added``), not to limit
                         the closure itself.

    Returns a dict:
      'toolchain' / 'language' / 'leaf' : disjoint Set[str] partition of 'all'
      'all'                              : Set[str] full build closure
      'added'                            : Set[str] = all - runtime_closure
      'unsatisfiable'                    : List[(src, group_str)] dropped groups
    """
    _runtime: 'Set[str]' = set(runtime_closure or ())

    # 1. direct build-dep binaries across every selected source
    _direct: 'Set[str]' = set()
    _unsat: 'List[tuple]' = []
    for _src in selected_srcs:
        for _grp in _rels(src_build_depends.get(_src, '')):
            if not _grp:
                continue
            _pk = _pick(_grp, bin_index, provides_index)
            if _pk:
                _direct.add(_pk)
            else:
                _unsat.append((_src, PkgRelation.str([_grp])))

    # 2. transitive install closure of the direct build-deps + toolchain base
    _all = _install_expand(_direct | set(TOOLCHAIN_SEEDS),
                           bin_index, provides_index)

    # 3. tier partition (toolchain ⊂ language-seed-closure ⊂ all)
    _toolchain = _install_expand(TOOLCHAIN_SEEDS, bin_index, provides_index) & _all
    _language = (_install_expand(LANGUAGE_SEEDS, bin_index, provides_index)
                 & _all) - _toolchain
    _leaf = _all - _toolchain - _language

    return {
        'toolchain': _toolchain,
        'language': _language,
        'leaf': _leaf,
        'all': _all,
        'added': _all - _runtime,
        'unsatisfiable': _unsat,
    }
