"""Foreign-target cross-toolchain detector — the single authority for
deciding whether a *built binary* targets a FOREIGN architecture and so
must never reach the published pool or take a federation ownership claim.

Background
----------
The arch *predicate* over the SELECTION (which sources/packages build for
this arch) is already correct and centralized on dpkg's own
``DpkgArchTable.matches_architecture`` (commit 206c089).  The gap this
module closes is DOWNSTREAM: a legitimately-selected source can emit
cross-toolchain binaries that are ``Architecture: amd64`` yet *target* a
foreign arch — ``binutils-aarch64-linux-gnu``,
``binutils-x86-64-kfreebsd-gnu-dbg``, ``gcc-12-arm-linux-gnueabihf``,
``libc6-arm64-cross``.  Those are by-products of the build, not part of
the distribution; they should be dropped at the claim / repo / index
layers (never by rescanning build receipts).

Why a dpkg-backed map (the "assured" part)
------------------------------------------
We have repeatedly mis-filtered with hand-rolled heuristics
(``endswith('-amd64')`` matched ``kfreebsd-amd64``; substring ``arm``
matched ``apparmor``/``build-essential``/``zbar``).  The ONLY correct
authority is dpkg's own arch grammar, so the detector is built entirely
from dpkg data:

  * a {multiarch-triplet -> Debian arch} map from
    ``dpkg-architecture -a <a> -qDEB_HOST_MULTIARCH`` over every arch in
    ``dpkg-architecture -L`` (amd64 -> ``x86_64-linux-gnu`` etc.), with
    ``_`` normalised to ``-`` so it matches the package-name form;
  * augmented with GNU cpu-aliases from ``/usr/share/dpkg/cputable`` so
    the hurd package-name form ``i686-gnu`` resolves like the multiarch
    form ``i386-gnu`` (-> ``hurd-i386``);
  * the verdict itself is dpkg's ``matches_architecture(target, build)``.

Bias / fail-safe
----------------
A binary is foreign ONLY on a DEFINITIVE foreign match.  Anything else —
no triplet/cross suffix, an unparseable name, an empty map (dpkg absent),
or ``matches_architecture`` returning ``None``/``True`` — is KEPT.  We
never raise and never drop a non-toolchain package; over-keeping is
harmless (the existing closure/cleanup gates handle it), under-keeping
would break closure.
"""

import json
import logging
import os
import subprocess
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger('athena')

# Bare arch names that still encode a target even without an OS component
# are matched ONLY in the ``<lib>-<arch>-cross`` form.  Multilib packages
# such as ``libc6-i386`` / ``libc6-x32`` / ``lib32gcc-s1`` deliberately
# DO NOT match here — they are native amd64 binaries shipping foreign-ABI
# runtime libs and must be kept.
_DBG_SUFFIXES = ('-dbgsym', '-dbg')

# Module-level memo: built once per process.  ``None`` = not yet built;
# a tuple = (triplet_map, arch_set); an empty map means "tried, dpkg
# unavailable" -> detector degrades to KEEP-everything.
_MAPS: 'Optional[Tuple[Dict[str, str], Set[str]]]' = None
_ARCH_TABLE: Any = None


def _arch_table() -> Any:
    """dpkg's ``DpkgArchTable`` — the same authority ``cache.py`` uses.
    Loaded once; ``None`` if python-debian can't provide it."""
    global _ARCH_TABLE
    if _ARCH_TABLE is None:
        try:
            from debian.debian_support import DpkgArchTable
            _ARCH_TABLE = DpkgArchTable.load_arch_table()
        except Exception as _e:   # pragma: no cover — python-debian present
            logger.warning(f"arch_filter: dpkg arch table unavailable: {_e}")
            _ARCH_TABLE = False   # sentinel: tried, failed
    return _ARCH_TABLE or None


def _read_cputable_aliases() -> 'Dict[str, Set[str]]':
    """Parse ``/usr/share/dpkg/cputable`` into a symmetric cpu-alias map
    {name -> {other names}} pairing each Debian cpu (col 1) with its GNU
    cpu (col 2), e.g. ``i386 <-> i686``.  Used to teach the triplet map
    the GNU-cpu package-name forms the multiarch query omits (the hurd
    ``i686-gnu`` case)."""
    alias: 'Dict[str, Set[str]]' = {}
    try:
        with open('/usr/share/dpkg/cputable') as _f:
            for _ln in _f:
                _ln = _ln.split('#', 1)[0].strip()
                if not _ln:
                    continue
                _cols = _ln.split()
                if len(_cols) < 2:
                    continue
                _deb = _cols[0].replace('_', '-')
                _gnu = _cols[1].replace('_', '-')
                alias.setdefault(_deb, set()).add(_gnu)
                alias.setdefault(_gnu, set()).add(_deb)
    except OSError:
        pass
    return alias


_DPKG_TABLES = ('/usr/share/dpkg/tupletable', '/usr/share/dpkg/cputable',
                '/usr/share/dpkg/ostable')


def _tables_fingerprint() -> str:
    """A cheap fingerprint of the dpkg architecture tables (mtime + size), so a
    cached triplet map is invalidated when dpkg is upgraded."""
    _parts = []
    for _f in _DPKG_TABLES:
        try:
            _st = os.stat(_f)
            _parts.append(f'{_f}:{int(_st.st_mtime)}:{_st.st_size}')
        except OSError:
            _parts.append(f'{_f}:missing')
    return '|'.join(_parts)


def _cache_path() -> str:
    _base = (os.environ.get('XDG_CACHE_HOME')
             or os.path.join(os.path.expanduser('~'), '.cache'))
    return os.path.join(_base, 'athena-build', 'arch_triplets.json')


def _build_maps() -> 'Tuple[Dict[str, str], Set[str]]':
    """(triplet_map, arch_set), cached on disk to avoid re-forking
    dpkg-architecture ~200x on EVERY process (audit #20).

    NOTE: the map is derived from dpkg-architecture (authoritative), NOT from a
    hand-rolled parse of tupletable — the multiarch triplet has dpkg-internal
    special cases (e.g. i386 stays ``i386`` though its GNU CPU is ``i686``;
    mips64 uses ``gnuabi64``) that a naive table join gets wrong for ~15 of the
    ~206 arches.  So the FIRST process still forks dpkg once per arch, then
    caches the result keyed by the tables' fingerprint; later processes (and
    later cache/build runs) read the cache instead of re-forking."""
    _fp = _tables_fingerprint()
    _cp = _cache_path()
    try:
        with open(_cp) as _fh:
            _doc = json.load(_fh)
        if (isinstance(_doc, dict) and _doc.get('fingerprint') == _fp
                and isinstance(_doc.get('triplets'), dict)
                and isinstance(_doc.get('arches'), list)):
            return dict(_doc['triplets']), set(_doc['arches'])
    except (OSError, ValueError):
        pass
    _triplets, _arches = _build_maps_from_dpkg()
    if _triplets or _arches:
        try:
            os.makedirs(os.path.dirname(_cp), exist_ok=True)
            _tmp = _cp + '.tmp'
            with open(_tmp, 'w') as _fh:
                json.dump({'fingerprint': _fp, 'triplets': _triplets,
                           'arches': sorted(_arches)}, _fh)
            os.replace(_tmp, _cp)          # atomic; concurrent builders are safe
        except OSError as _e:
            logger.warning(f"arch_filter: could not cache triplet map: {_e}")
    return _triplets, _arches


def _build_maps_from_dpkg() -> 'Tuple[Dict[str, str], Set[str]]':
    """Build (triplet_map, arch_set) from dpkg.  NEVER raises — any
    failure yields empty structures so the detector keeps everything."""
    triplets: 'Dict[str, str]' = {}
    arches: 'Set[str]' = set()
    try:
        _listed = subprocess.run(
            ['dpkg-architecture', '-L'],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        for _a in _listed:
            arches.add(_a)
            try:
                _trip = subprocess.run(
                    ['dpkg-architecture', '-a', _a, '-qDEB_HOST_MULTIARCH'],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
            except subprocess.CalledProcessError:
                # One misbehaving arch degrades only THAT arch — don't let it
                # nuke the whole map (which would degrade the filter to
                # keep-everything for the rest of the process).
                continue
            if _trip:
                triplets[_trip.replace('_', '-')] = _a
        # Augment with GNU cpu-alias spellings of each triplet's head
        # token so package-name forms (``i686-gnu``) resolve like the
        # canonical multiarch form (``i386-gnu``).
        _alias = _read_cputable_aliases()
        for _trip, _arch in list(triplets.items()):
            _head, _, _rest = _trip.partition('-')
            if not _rest:
                continue
            for _alt in _alias.get(_head, ()):
                triplets.setdefault(f"{_alt}-{_rest}", _arch)
    except Exception as _e:   # pragma: no cover — dpkg present here
        logger.warning(f"arch_filter: could not build dpkg triplet map: {_e}")
        return {}, set()
    return triplets, arches


def _maps() -> 'Tuple[Dict[str, str], Set[str]]':
    global _MAPS
    if _MAPS is None:
        _MAPS = _build_maps()
    return _MAPS


def _strip_dbg(name: str) -> str:
    for _suf in _DBG_SUFFIXES:
        if name.endswith(_suf):
            return name[:-len(_suf)]
    return name


def _is_foreign_arch(arch: str, build_arch: str) -> bool:
    """``True`` iff dpkg DEFINITIVELY says ``arch`` is not ``build_arch``.
    Mirrors cache.py's ``matches_architecture(target, build)`` arg order;
    biases to KEEP (``False``) on ``None``/unknown."""
    _tbl = _arch_table()
    if _tbl is None:
        return False
    try:
        return _tbl.matches_architecture(arch, build_arch) is False
    except Exception:
        return False


def is_foreign_target_binary(binary_name: str, build_arch: str) -> bool:
    """Return ``True`` iff ``binary_name`` is a cross-toolchain / runtime
    binary that TARGETS an architecture other than ``build_arch``.

    Matching is purely dpkg-derived and boundary-anchored (never a bare
    substring):

      * ``<...>-<multiarch-triplet>`` (optionally ``-dbg``/``-dbgsym``):
        longest triplet suffix wins; foreign iff its Debian arch is not
        ``build_arch`` — ``binutils-aarch64-linux-gnu`` (arm64),
        ``gcc-12-arm-linux-gnueabihf`` (armhf, longest-match beats the
        nested ``linux-gnueabihf``), ``binutils-i686-gnu`` (hurd-i386).
      * ``<lib>-<debian-arch>-cross``: longest trailing bare-arch token
        group — ``libc6-arm64-cross`` (arm64).

    KEEPS native ``x86-64-linux-gnu`` (-> build arch), every non-toolchain
    name, and multilib runtimes like ``libc6-i386`` (no triplet suffix,
    and bare arches are matched only in the ``-cross`` form).  Fail-safe:
    returns ``False`` on any uncertainty; never raises.
    """
    _triplets, _arches = _maps()
    if not _triplets:
        return False
    _base = _strip_dbg(binary_name)

    # ``<lib>-<debian-arch>-cross`` form.
    if _base.endswith('-cross'):
        _toks = _base[:-len('-cross')].split('-')
        for _i in range(len(_toks)):
            _cand = '-'.join(_toks[_i:])
            if _cand in _arches:
                return _is_foreign_arch(_cand, build_arch)
        return False

    # Longest multiarch-triplet suffix.
    _best_trip: 'Optional[str]' = None
    _best_arch: 'Optional[str]' = None
    for _trip, _arch in _triplets.items():
        if _base == _trip or _base.endswith('-' + _trip):
            if _best_trip is None or len(_trip) > len(_best_trip):
                _best_trip, _best_arch = _trip, _arch
    if _best_arch is None:
        return False
    return _is_foreign_arch(_best_arch, build_arch)
