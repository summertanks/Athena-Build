"""UX-04 out-of-band pickle perf measurement.

Times Cache + DependencyTree pickle save/load against fresh cmd_build_cache
+ cmd_parse_dependency on the operator's actual bookworm workload.  Does
NOT modify any base code — monkey-patches __getstate__/__setstate__ on
Cache + DependencyTree at runtime so pickle round-trips don't blow up on
the non-picklable bits (_arch_table DpkgArchTable instance, lambda-factory
defaultdicts, DT's back-ref to Cache).

Prints T_fresh, T_pickle_save, T_pickle_load + T_resume, blob size, and a
verdict based on T_resume / T_fresh.

Run from the repo root with -u for unbuffered output:
    python3 -u experiments/ux04_pickle_perf.py [--trials N]

Phase 1 of UX-04 (see ~/.claude/plans/proud-juggling-aurora.md).
"""

from __future__ import annotations

import argparse
import gzip
import os
import pickle
import statistics
import sys
import tempfile
import time
from collections import defaultdict


# ───────────────────────────────────────────────────────────────────────
# Bootstrap: add scripts/ to sys.path, then clear argv so BuildConfig's
# argparse doesn't choke on our --trials flag.
# ───────────────────────────────────────────────────────────────────────

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'scripts'))

# Parse our own args before BuildConfig sees argv.
_ap = argparse.ArgumentParser(description=__doc__)
_ap.add_argument('--trials', type=int, default=1,
                 help='How many round-trips to time (default 1).  Median is reported when N>1.')
_ap.add_argument('--keep-blob', action='store_true',
                 help="Don't delete the pickle blob at the end (for inspection).")
_args = _ap.parse_args()
del sys.argv[1:]   # leave argv[0] (program name) so argparse in BuildConfig is happy

# Now safe to import the production modules.
import apt_pkg                       # noqa: E402
import cli as _cli                   # noqa: E402  (registers as tui.tui_instance)
import tui as _tui                   # noqa: E402
import utils                         # noqa: E402,F401
from utils import BuildConfig        # noqa: E402
from cache import Cache              # noqa: E402
from dependencytree import DependencyTree  # noqa: E402
import build as _build               # noqa: E402
from tui import Prompt, PROMPT_YESNO, PROMPT_OPTIONS  # noqa: E402


# ───────────────────────────────────────────────────────────────────────
# Monkey-patches: make Cache + DependencyTree pickleable.
# Mirrors what the reverted commit's __getstate__/__setstate__ did.
# ───────────────────────────────────────────────────────────────────────

def _cache_getstate(self):
    """Drop non-picklable bits and flatten lambda-factory defaultdicts."""
    state = self.__dict__.copy()
    state.pop('_arch_table', None)
    state.pop('_compression_openers', None)
    state['package_hashtable'] = {
        k: dict(v) for k, v in self.package_hashtable.items()
    }
    state['udeb_hashtable'] = {
        k: dict(v) for k, v in self.udeb_hashtable.items()
    }
    state['source_hashtable'] = dict(self.source_hashtable)
    state['_upstream_collisions'] = dict(self._upstream_collisions)
    state['_upstream_udeb_collisions'] = dict(self._upstream_udeb_collisions)
    return state


def _cache_setstate(self, state):
    """Restore defaultdict factories + recompute _arch_table."""
    self.__dict__.update(state)
    _ph = defaultdict(lambda: defaultdict(list))
    for k, v in self.package_hashtable.items():
        _inner = defaultdict(list)
        _inner.update(v)
        _ph[k] = _inner
    self.package_hashtable = _ph

    _uh = defaultdict(lambda: defaultdict(list))
    for k, v in self.udeb_hashtable.items():
        _inner = defaultdict(list)
        _inner.update(v)
        _uh[k] = _inner
    self.udeb_hashtable = _uh

    _sh = defaultdict(list)
    _sh.update(self.source_hashtable)
    self.source_hashtable = _sh

    _uc = defaultdict(list)
    _uc.update(self._upstream_collisions)
    self._upstream_collisions = _uc

    _uuc = defaultdict(list)
    _uuc.update(self._upstream_udeb_collisions)
    self._upstream_udeb_collisions = _uuc

    from debian.debian_support import DpkgArchTable
    try:
        self._arch_table = DpkgArchTable.load_arch_table()
    except Exception:
        self._arch_table = None

    import lzma as _lzma
    import gzip as _gzip
    import bz2 as _bz2
    self._compression_openers = [
        ('.xz',  _lzma.open),
        ('.gz',  _gzip.open),
        ('.bz2', _bz2.open),
    ]


Cache.__getstate__ = _cache_getstate
Cache.__setstate__ = _cache_setstate


def _dt_getstate(self):
    """Drop the back-ref to Cache and flatten __lookahead."""
    state = self.__dict__.copy()
    state.pop('_DependencyTree__cache', None)
    _la = state.get('_DependencyTree__lookahead')
    if _la is not None:
        state['_DependencyTree__lookahead'] = {k: dict(v) for k, v in _la.items()}
    return state


def _dt_setstate(self, state):
    """Restore __lookahead defaultdict; leave __cache None (rewired post-load)."""
    self.__dict__.update(state)
    _la_flat = self.__dict__.get('_DependencyTree__lookahead', {})
    _la = defaultdict(dict)
    _la.update(_la_flat)
    self.__dict__['_DependencyTree__lookahead'] = _la
    self.__dict__['_DependencyTree__cache'] = None


DependencyTree.__getstate__ = _dt_getstate
DependencyTree.__setstate__ = _dt_setstate


# Package + Source: python-debian's Deb822 base uses an OrderedSet whose
# __keys nodes hold weakrefs — those can't pickle.  Workaround: snapshot
# the field-value pairs as a plain dict + the parsed __dict__ attributes,
# then re-init the Deb822 base on load with no args (avoids reparsing
# control text) and replay the field→value writes.
import package as _package_mod  # noqa: E402
from debian.deb822 import Packages as _Deb822Packages  # noqa: E402
from debian.deb822 import Sources as _Deb822Sources    # noqa: E402

_DEB822_INTERNAL_PREFIXES = ('_Deb822', '_PkgRelationMixin',
                              '_VersionAccessorMixin')
_DEB822_INTERNAL_ATTRS = {
    'encoding', 'decoder', 'gpg_info', '_err_str',
}


def _make_pkg_getsetstate(deb822_base, restore_from_raw_text=False):
    """Build (__getstate__, __setstate__) for Package/Source.

    restore_from_raw_text=False (Package path):
      getstate captures field dict + filtered __dict__; setstate re-inits
      Deb822 from the field dict.  Faster because no re-parse of dep strings.

    restore_from_raw_text=True (Source path):
      Source has multi-valued fields (Files, Checksums-Sha256, etc.) whose
      python-debian wrapper objects carry weakrefs that can't pickle.  Use
      the `raw_text` attribute (the original Sources stanza) and re-init
      Deb822 from that text instead.  Costs the parse on load but
      sidesteps the weakref problem.
    """
    import io as _io
    if restore_from_raw_text:
        def _getstate(self):
            # Drop the field dict — raw_text in _attrs is enough.
            _attrs = {
                k: v for k, v in self.__dict__.items()
                if not k.startswith(_DEB822_INTERNAL_PREFIXES)
                and k not in _DEB822_INTERNAL_ATTRS
            }
            return {'_fields': None, '_attrs': _attrs}

        def _setstate(self, state):
            _attrs = state['_attrs']
            _raw = _attrs.get('raw_text', b'')
            if isinstance(_raw, str):
                _raw = _raw.encode('utf-8', errors='replace')
            deb822_base.__init__(self, _io.BytesIO(_raw))
            self.__dict__.update(_attrs)
    else:
        def _getstate(self):
            _fields = {k: self[k] for k in self.keys()}
            _attrs = {
                k: v for k, v in self.__dict__.items()
                if not k.startswith(_DEB822_INTERNAL_PREFIXES)
                and k not in _DEB822_INTERNAL_ATTRS
            }
            return {'_fields': _fields, '_attrs': _attrs}

        def _setstate(self, state):
            deb822_base.__init__(self)
            for k, v in state['_fields'].items():
                self[k] = v
            self.__dict__.update(state['_attrs'])
    return _getstate, _setstate


_pkg_getstate, _pkg_setstate = _make_pkg_getsetstate(_Deb822Packages)
_package_mod.Package.__getstate__ = _pkg_getstate
_package_mod.Package.__setstate__ = _pkg_setstate

# Source uses raw_text-based restore because Sources stanzas have
# multi-valued fields whose python-debian wrappers carry weakrefs.
_src_getstate, _src_setstate = _make_pkg_getsetstate(
    _Deb822Sources, restore_from_raw_text=True)
_package_mod.Source.__getstate__ = _src_getstate
_package_mod.Source.__setstate__ = _src_setstate


# ───────────────────────────────────────────────────────────────────────
# Auto-respond to operator prompts so the script can run unattended.
# ───────────────────────────────────────────────────────────────────────

def _auto_prompt_get_response(self):
    if self._type == PROMPT_YESNO:
        return 'y'
    if self._type == PROMPT_OPTIONS:
        return self._options[0] if self._options else '1'
    return ''


Prompt.get_response = _auto_prompt_get_response


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────

def _fmt_seconds(s):
    if s < 1.0:
        return f"{s*1000:.0f}ms"
    if s < 60.0:
        return f"{s:.2f}s"
    return f"{int(s//60)}m{s%60:04.1f}s"


def _fmt_bytes(n):
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f}KB"
    if n < 1024 * 1024 * 1024:
        return f"{n/(1024*1024):.1f}MB"
    return f"{n/(1024*1024*1024):.2f}GB"


def _decision(ratio):
    if ratio <= 0.50:
        return "SHIP plain pickle UX-04 — clear win"
    if ratio <= 0.75:
        return "MARGINAL — ship with note; operator decides if it's enough"
    return "DON'T SHIP plain pickle — refactor needed (lightweight dataclass or SQLite+lazy)"


# ───────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────

def main():
    print("UX-04 pickle perf measurement", flush=True)
    print("=" * 70, flush=True)

    apt_pkg.init_system()
    config = BuildConfig()
    if not config.is_valid:
        print(f"ERROR: invalid config — {config.error_str}", file=sys.stderr, flush=True)
        return 1
    _cli.Cli()  # registers as tui.tui_instance

    session = _build.BuildSession(config, _tui.tui_instance)

    # ───── Phase 1: T_fresh ─────
    print("\n[Phase 1] T_fresh — cmd_build_cache + cmd_parse_dependency", flush=True)
    t0 = time.perf_counter()
    session.cmd_build_cache()
    print(f"  cmd_build_cache done at {_fmt_seconds(time.perf_counter()-t0)}", flush=True)
    session.cmd_parse_dependency()
    T_fresh = time.perf_counter() - t0
    if session.cache is None or session.dep_tree is None:
        print(f"ERROR: cmd_* did not populate cache/dep_tree "
              f"(cache={session.cache}, dep_tree={session.dep_tree})",
              file=sys.stderr, flush=True)
        return 2

    n_pkgs = sum(len(v) for v in session.cache.package_hashtable.values())
    n_srcs = sum(len(v) if isinstance(v, list) else 1
                 for v in session.cache.source_hashtable.values())
    n_udebs = sum(len(v) for v in session.cache.udeb_hashtable.values())
    n_selected = len(session.dep_tree.selected_pkgs)
    n_selected_srcs = len(session.dep_tree.selected_srcs)
    print(f"  T_fresh        = {_fmt_seconds(T_fresh)}", flush=True)
    print(f"  packages       = {n_pkgs:,}", flush=True)
    print(f"  sources        = {n_srcs:,}", flush=True)
    print(f"  udebs          = {n_udebs:,}", flush=True)
    print(f"  selected_pkgs  = {n_selected:,}", flush=True)
    print(f"  selected_srcs  = {n_selected_srcs:,}", flush=True)

    # ───── Phase 2 + 3: pickle save/load, N trials ─────
    saves = []
    loads = []
    blob_size = 0
    tmp = tempfile.NamedTemporaryFile(prefix='ux04-perf-', suffix='.pkl.gz', delete=False)
    tmp.close()
    tmpfile = tmp.name

    try:
        for trial in range(_args.trials):
            print(f"\n[Phase 2 trial {trial+1}/{_args.trials}] pickle.dump → gzip", flush=True)
            t1 = time.perf_counter()
            payload = {
                'cache':         session.cache,
                'dep_tree':      session.dep_tree,
                'udeb_dep_tree': session.udeb_dep_tree,
            }
            with gzip.open(tmpfile, 'wb', compresslevel=6) as fh:
                pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            T_save = time.perf_counter() - t1
            saves.append(T_save)
            blob_size = os.path.getsize(tmpfile)
            print(f"  T_pickle_save  = {_fmt_seconds(T_save)}    blob = {_fmt_bytes(blob_size)}",
                  flush=True)

            print(f"[Phase 3 trial {trial+1}/{_args.trials}] gzip → pickle.load + rewire",
                  flush=True)
            t2 = time.perf_counter()
            with gzip.open(tmpfile, 'rb') as fh:
                blob = pickle.load(fh)
            cache2 = blob['cache']
            dt2 = blob['dep_tree']
            udt2 = blob['udeb_dep_tree']
            dt2._DependencyTree__cache = cache2
            if udt2 is not None:
                udt2._DependencyTree__cache = cache2
            T_load = time.perf_counter() - t2
            loads.append(T_load)
            print(f"  T_resume       = {_fmt_seconds(T_load)}", flush=True)

            if len(cache2.package_hashtable) != len(session.cache.package_hashtable):
                print(f"  WARNING: hashtable size mismatch — "
                      f"orig {len(session.cache.package_hashtable)}, "
                      f"restored {len(cache2.package_hashtable)}", flush=True)
            if len(dt2.selected_pkgs) != n_selected:
                print(f"  WARNING: selected_pkgs size mismatch — "
                      f"orig {n_selected}, restored {len(dt2.selected_pkgs)}", flush=True)
    finally:
        if not _args.keep_blob:
            try:
                os.unlink(tmpfile)
            except OSError:
                pass

    # ───── Verdict ─────
    T_save_median = statistics.median(saves)
    T_load_median = statistics.median(loads)
    ratio = T_load_median / T_fresh
    print("\n" + "=" * 70, flush=True)
    print("=== UX-04 measurement results ===", flush=True)
    print(f"  T_fresh           = {_fmt_seconds(T_fresh)}", flush=True)
    suff = f"  (median of {len(saves)})" if len(saves) > 1 else ""
    print(f"  T_pickle_save     = {_fmt_seconds(T_save_median)}{suff}", flush=True)
    suff = f"  (median of {len(loads)})" if len(loads) > 1 else ""
    print(f"  T_resume          = {_fmt_seconds(T_load_median)}{suff}", flush=True)
    print(f"  blob size         = {_fmt_bytes(blob_size)}", flush=True)
    print(f"  T_resume / T_fresh = {ratio:.3f}", flush=True)
    print(flush=True)
    print(f"  Decision: R = {ratio:.3f}  →  {_decision(ratio)}", flush=True)
    print("=" * 70, flush=True)
    if _args.keep_blob:
        print(f"\n(Pickle blob retained at {tmpfile})", flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
