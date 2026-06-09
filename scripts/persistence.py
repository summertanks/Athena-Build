"""UX-04: session persistence — pickle Cache + DependencyTree + small
primitives to / from disk, gated by a fingerprint of every input that
fed the saved state.

Restored on `cmd_resume`.  Mismatching fingerprint → refusal with a
"what changed" message; operator runs `cache parse` to rebuild.

Out-of-band measurement (`experiments/ux04_pickle_perf.py`) on the
bookworm workload showed T_resume / T_fresh ≈ 0.43 — a clear ship.
The pickle overrides on Cache, DependencyTree, Package, and Source
were lifted from the same script.

Files written under `<dir_cache>/`:

  session.pkl.gz                  pickle protocol 5, gzip
  session.pkl.gz.sha256           integrity sidecar (refuses on mismatch)
  session.fingerprint.json        plain JSON, drives the resume gate
  buildflags.json                 BuildFlags autosave (see build.py)
"""

import gzip
import hashlib
import io  # noqa: F401  — kept for parity with the experiment script
import json
import logging
import os
import pickle
import time
from dataclasses import dataclass
from typing import Any, Optional

import apt_pkg

import utils
from utils import BuildConfig


logger = logging.getLogger('athena')

# Bump on any change to:
# - Cache / DependencyTree / Package / Source __getstate__ / __setstate__
# - The pickle payload envelope shape
# - The fingerprint JSON shape
# v2 (SELECT-LOCK): DependencyTree gained _pins/_pinned_chosen — a v1 blob
# lacks them, so refuse-and-reparse rather than resume a pin-less tree.
_FORMAT_VERSION       = 2
_SESSION_FILENAME     = 'session.pkl.gz'
_FINGERPRINT_FILENAME = 'session.fingerprint.json'
_HASH_SUFFIX          = '.sha256'


@dataclass
class RestoredSession:
    """Bag of restored bits returned by `restore_session`.  Caller
    (`cmd_resume`) wires these into the live `BuildSession`."""
    cache:                    Any
    dep_tree:                 Any
    udeb_dep_tree:            Optional[Any]
    last_source_build_counts: Optional[dict]


# ── SHA256 sidecar primitives ─────────────────────────────────────────

def _sha256_of_file(path: str) -> str:
    """Stream a file through SHA256.  Empty string on read failure."""
    _h = hashlib.sha256()
    try:
        with open(path, 'rb') as _fh:
            while True:
                _chunk = _fh.read(1 << 16)
                if not _chunk:
                    break
                _h.update(_chunk)
    except OSError:
        return ''
    return _h.hexdigest()


def _write_sha256(blob_path: str) -> None:
    """Write a SHA256 sidecar next to `blob_path`.  Best-effort."""
    _digest = _sha256_of_file(blob_path)
    if not _digest:
        return
    try:
        with open(blob_path + _HASH_SUFFIX, 'w') as _fh:
            _fh.write(_digest + '\n')
    except OSError as _e:
        logger.warning(f"persistence: SHA256 sidecar write failed for "
                       f"{blob_path}: {_e}")


def _verify_sha256(blob_path: str) -> bool:
    """True iff `blob_path` matches the digest in its `.sha256` sidecar."""
    _sidecar = blob_path + _HASH_SUFFIX
    try:
        with open(_sidecar) as _fh:
            _expected = _fh.read().strip()
    except OSError:
        return False
    if not _expected:
        return False
    return _sha256_of_file(blob_path) == _expected


def _session_path(dir_cache: str) -> str:
    return os.path.join(dir_cache, _SESSION_FILENAME)


def _fingerprint_path(dir_cache: str) -> str:
    return os.path.join(dir_cache, _FINGERPRINT_FILENAME)


# ── Fingerprint ──────────────────────────────────────────────────────

def compute_fingerprint(config: BuildConfig, cache, dep_tree) -> dict:
    """Compose the session fingerprint: every input that fed the saved
    Cache + DependencyTree.  Mismatching values on restore → refusal.

    Composition (reuses existing utils helpers — no new hashing code):

      config_hash         compute_tree_hash(config.dir_config)
      mirror_inreleases   {mirror_id: sha256(InRelease | Release)}
      fork_tree_hashes    {fork_pkg: tree_hash from fork_mirror sidecars}
      patch_set_hashes    {source_name: patch_set_hash for its patches}
      snapshot_ts         the resolved snapshot timestamp
      arch                config.arch
      build_profiles      sorted list
      include_recommends  the [Build] flag
    """
    _config_hash = utils.compute_tree_hash(
        config.dir_config, skip_dirs=('.git', '__pycache__'))

    # Per-mirror InRelease (or plain Release for file:// mirrors).  The
    # InRelease SHA covers downstream Packages/Sources via its signed
    # block; rehashing those would add seconds for no extra signal.
    _mirror_inreleases: dict = {}
    for _m in cache.mirrors:
        _is_local = _m.baseurl.startswith('file://')
        _name = 'Release' if _is_local else 'InRelease'
        _url = _m.dist_url + _name
        _path = os.path.join(config.dir_cache, apt_pkg.uri_to_filename(_url))
        _mirror_inreleases[_m.id] = utils.get_sha256(_path)

    # Reuse fork_mirror's per-pkg .tree-hash sidecars — no recomputation.
    _fork_tree_hashes: dict = {}
    try:
        from fork_mirror import load_pkg_hashes
        _fork_names = (
            set(getattr(cache, '_fork_pkg_names', set())) |
            set(getattr(cache, '_fork_src_names', set())) |
            set(getattr(cache, '_fork_udeb_names', set()))
        )
        for _pkg in _fork_names:
            try:
                _tree, _ = load_pkg_hashes(_pkg, config.dir_fork_source_repo)
                if _tree:
                    _fork_tree_hashes[_pkg] = _tree
            except (OSError, ValueError, KeyError):
                # Sidecar missing or malformed — skip; the absence itself
                # invalidates a saved fingerprint that DID record one.
                pass
    except ImportError:
        pass

    # Per-source patch set hashes.  Only sources with patches contribute
    # (no entry == no patches to track).
    _patch_set_hashes: dict = {}
    for _src_name, _src in dep_tree.selected_srcs.items():
        _patches = getattr(_src, 'patch_list', None) or []
        if not _patches:
            continue
        _ver = utils.version_no_epoch(str(_src.version))
        _patch_dir = os.path.join(config.dir_patch_source, _src_name, _ver)
        if os.path.isdir(_patch_dir):
            _patch_set_hashes[_src_name] = utils.patch_set_hash(
                _patch_dir, _patches)

    return {
        '_format_version':    _FORMAT_VERSION,
        'config_hash':        _config_hash,
        'mirror_inreleases':  _mirror_inreleases,
        'fork_tree_hashes':   _fork_tree_hashes,
        'patch_set_hashes':   _patch_set_hashes,
        'snapshot_ts':        getattr(cache, 'snapshot_ts', '') or '',
        'arch':               config.arch,
        'build_profiles':     sorted(config.build_profiles),
        'include_recommends': config.include_recommends,
    }


def fingerprint_diff(saved: dict, current: dict) -> list:
    """Return a human-readable list of changed fingerprint fields.

    Scalar fields are reported by name (e.g. `"config_hash changed"`).
    Dict-valued fields are reported with the first changed key
    (e.g. `"mirror_inreleases.security changed"`).  Empty list ⇒
    fingerprints match.
    """
    _diffs: list = []
    for _key in ('config_hash', 'snapshot_ts', 'arch',
                 'build_profiles', 'include_recommends'):
        if saved.get(_key) != current.get(_key):
            _diffs.append(f"{_key} changed")
    for _kind in ('mirror_inreleases', 'fork_tree_hashes',
                  'patch_set_hashes'):
        _saved_d = saved.get(_kind, {}) or {}
        _cur_d = current.get(_kind, {}) or {}
        for _k in sorted(set(_saved_d) | set(_cur_d)):
            if _saved_d.get(_k) != _cur_d.get(_k):
                _diffs.append(f"{_kind}.{_k} changed")
                break    # one per kind is enough signal
    return _diffs


# ── Save / restore ───────────────────────────────────────────────────

def save_session(session, dir_cache: str) -> bool:
    """Persist Cache + DT + udeb_dep_tree + last_source_build_counts +
    fingerprint to `dir_cache`.  Returns False on any failure (log only;
    save failure never aborts a build).  No-op when cache or dep_tree
    is None (nothing meaningful to save).

    Atomic via temp-file + os.replace so a kill mid-save leaves the
    previous good blob intact.
    """
    if session.cache is None or session.dep_tree is None:
        return False
    _blob_path = _session_path(dir_cache)
    _tmp = _blob_path + '.tmp'
    _fp_path = _fingerprint_path(dir_cache)
    _fp_tmp = _fp_path + '.tmp'
    try:
        _payload = {
            '_format_version':          _FORMAT_VERSION,
            '_saved_at':                time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                                       time.gmtime()),
            'cache':                    session.cache,
            'dep_tree':                 session.dep_tree,
            'udeb_dep_tree':            session.udeb_dep_tree,
            'last_source_build_counts': session.last_source_build_counts,
        }
        with gzip.open(_tmp, 'wb', compresslevel=6) as _fh:
            pickle.dump(_payload, _fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(_tmp, _blob_path)
        _write_sha256(_blob_path)

        _fp = compute_fingerprint(session.config, session.cache,
                                  session.dep_tree)
        with open(_fp_tmp, 'w') as _fh:
            json.dump(_fp, _fh, indent=2, sort_keys=True)
        os.replace(_fp_tmp, _fp_path)
        return True
    except (OSError, pickle.PickleError) as _e:
        logger.warning(f"save_session: {_e}")
        for _p in (_tmp, _fp_tmp):
            try:
                os.unlink(_p)
            except OSError:
                pass
        return False


def restore_session(config: BuildConfig, report) -> Optional[RestoredSession]:
    """Verify fingerprint → unpickle Cache + DT + udeb_dep_tree → rewire
    DT's `__cache` backref to the loaded Cache.  Returns `RestoredSession`
    on success; `None` on any failure (the operator gets a clear "what
    changed" or "what's missing" message via `report`).

    `report(message)` is a callable for user-facing output (the caller —
    typically `cmd_resume` — passes its console facade as a closure).
    """
    _blob = _session_path(config.dir_cache)
    _fp_path = _fingerprint_path(config.dir_cache)

    if not os.path.isfile(_blob):
        # No session to restore — silent.  cmd_resume reports separately.
        return None
    if not os.path.isfile(_fp_path):
        report("resume: fingerprint file missing — refusing restore. "
               "Run `cache parse` to rebuild.")
        return None
    if not _verify_sha256(_blob):
        report("resume: session.pkl.gz SHA256 mismatch (corrupt or stale "
               "sidecar) — refusing restore. Run `cache parse` to rebuild.")
        return None

    try:
        with open(_fp_path) as _fh:
            _saved_fp = json.load(_fh)
    except (OSError, json.JSONDecodeError) as _e:
        report(f"resume: cannot read fingerprint — {_e}. "
               f"Run `cache parse` to rebuild.")
        return None

    if _saved_fp.get('_format_version') != _FORMAT_VERSION:
        report(f"resume: fingerprint format v"
               f"{_saved_fp.get('_format_version')} "
               f"≠ current v{_FORMAT_VERSION} — refusing restore. "
               f"Run `cache parse` to rebuild.")
        return None

    try:
        with gzip.open(_blob, 'rb') as _fh:
            _payload = pickle.load(_fh)
    except (OSError, pickle.PickleError, EOFError, AttributeError) as _e:
        report(f"resume: pickle load failed ({_e}) — refusing restore. "
               f"Run `cache parse` to rebuild.")
        return None

    if _payload.get('_format_version') != _FORMAT_VERSION:
        report(f"resume: pickle format v"
               f"{_payload.get('_format_version')} "
               f"≠ current v{_FORMAT_VERSION} — refusing restore. "
               f"Run `cache parse` to rebuild.")
        return None

    _cache = _payload['cache']
    _dt = _payload['dep_tree']
    _udt = _payload.get('udeb_dep_tree')

    # Compare saved fingerprint vs the current on-disk state.
    _current_fp = compute_fingerprint(config, _cache, _dt)
    _diffs = fingerprint_diff(_saved_fp, _current_fp)
    if _diffs:
        report(f"resume: refused — fingerprint mismatch ({'; '.join(_diffs)}). "
               f"Run `cache parse` to rebuild.")
        return None

    # Rewire DT's name-mangled __cache backref to the loaded Cache.
    # The attribute is `self.__cache` inside DependencyTree, mangled to
    # `_DependencyTree__cache` from outside the class.
    _dt._DependencyTree__cache = _cache
    if _udt is not None:
        _udt._DependencyTree__cache = _cache

    return RestoredSession(
        cache=_cache,
        dep_tree=_dt,
        udeb_dep_tree=_udt,
        last_source_build_counts=_payload.get('last_source_build_counts'),
    )
