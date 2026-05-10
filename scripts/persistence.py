"""UX-04 commit B — pickle Cache + DependencyTree to disk for resume.

The slow stages worth skipping on a TUI restart are `build_cache`
(~30s on warm disk cache, longer cold) and `parse_dependency`
(~30s+ depending on pkg.list size).  Their outputs — the in-memory
``Cache`` and ``DependencyTree`` — are too big to JSON-serialise and
have circular / Python-specific state (debian.deb822 wrappers,
defaultdicts), so we pickle.

Pickle is unsafe to load from untrusted data (it can execute arbitrary
code), so each ``.pkl.gz`` is paired with a ``.pkl.gz.sha256`` sidecar
written from the same data we just compressed.  ``load_*`` checks the
hash before unpickling; mismatch → refuse, fall back to from-scratch.

This is integrity-only (corruption detection), NOT anti-tamper —
anyone with write access to ``dir_cache`` could update both files in
lockstep.  GPG-signing the blob (using the project signing key from
CONF-02) would close that gap; deferred until the threat model
demands it.

Each persistor carries a ``_FORMAT_VERSION`` constant.  Bump it
whenever ``Cache.__getstate__`` / ``DependencyTree.__getstate__`` /
the underlying class shape changes in a way that breaks unpickling.
Loaders refuse on version mismatch with a clear warning and return
None — caller falls back to running the cmd_X for real.

Public API:
    save_cache(cache, config)          → bool
    load_cache(config)                 → Cache or None
    save_dep_tree(dt, config)          → bool
    load_dep_tree(config, cache)       → DependencyTree or None
"""
import gzip
import hashlib
import logging
import os
import pickle
from typing import Optional

logger = logging.getLogger('athena')


# ─── File-layout constants ─────────────────────────────────────────────────

_CACHE_FILENAME    = 'cache.pkl.gz'
_DEPTREE_FILENAME  = 'deptree.pkl.gz'
_HASH_SUFFIX       = '.sha256'

# Bump on schema change.  Loaders refuse on mismatch (returns None +
# WARNING log), forcing the caller to re-run the cmd_X from scratch
# rather than risk loading garbage.  Pickled payload itself wraps the
# version: {'_format_version': N, 'data': <pickled_object>}.
_CACHE_FORMAT_VERSION   = 1
_DEPTREE_FORMAT_VERSION = 1


def _cache_path(config) -> str:
    return os.path.join(config.dir_cache, _CACHE_FILENAME)


def _deptree_path(config) -> str:
    return os.path.join(config.dir_cache, _DEPTREE_FILENAME)


# ─── Integrity helpers ─────────────────────────────────────────────────────

def _sha256_of_file(path: str) -> str:
    """Return hex digest of ``path``'s bytes.  Reads in 1 MiB chunks so
    a multi-MB pickle doesn't load in one slurp."""
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _write_sha256(blob_path: str) -> None:
    """Compute SHA256 of ``blob_path`` and write to ``blob_path.sha256``."""
    digest = _sha256_of_file(blob_path)
    with open(blob_path + _HASH_SUFFIX, 'w') as fh:
        fh.write(digest + '\n')


def _verify_sha256(blob_path: str) -> bool:
    """True iff ``blob_path.sha256`` exists and its hex matches the
    SHA256 of ``blob_path``.  False on any mismatch / missing file
    (logged at WARNING)."""
    sig_path = blob_path + _HASH_SUFFIX
    if not os.path.exists(sig_path):
        logger.warning(f"persistence: missing {sig_path} — cannot verify "
                       f"{blob_path}; refusing to load")
        return False
    try:
        with open(sig_path) as fh:
            expected = fh.read().strip()
    except OSError as e:
        logger.warning(f"persistence: cannot read {sig_path}: {e}")
        return False
    actual = _sha256_of_file(blob_path)
    if actual != expected:
        logger.warning(
            f"persistence: SHA256 mismatch on {blob_path} "
            f"(expected {expected[:16]}…, got {actual[:16]}…) — refusing to load"
        )
        return False
    return True


# ─── Generic save/load with format-version envelope ────────────────────────

def _save_pickled(obj, path: str, format_version: int) -> bool:
    """Pickle ``obj`` with a format-version wrapper, gzip-compress to
    ``path``, write SHA256 sidecar.  Returns True on success.  Failures
    log WARNING (don't raise) — a save failure mid-build shouldn't
    abort the build."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            '_format_version': format_version,
            'data': obj,
        }
        with gzip.open(path, 'wb') as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        _write_sha256(path)
        return True
    except (OSError, pickle.PicklingError) as e:
        logger.warning(
            f"persistence: failed to save {path}: "
            f"{type(e).__name__}: {e}"
        )
        return False


def _load_pickled(path: str, expected_version: int):
    """Verify SHA256, then gzip-decompress and unpickle.  Returns the
    unwrapped ``obj`` on success, None on any failure (missing file,
    integrity mismatch, format-version mismatch, unpickle error).

    Pickle is dangerous to load from untrusted data.  The SHA256 check
    above is a corruption gate, NOT an anti-tamper gate.  Caller
    controls the directory; we trust same-machine writes.
    """
    if not os.path.exists(path):
        return None
    if not _verify_sha256(path):
        return None
    try:
        with gzip.open(path, 'rb') as fh:
            payload = pickle.load(fh)
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError,
            ImportError, ModuleNotFoundError) as e:
        logger.warning(
            f"persistence: failed to unpickle {path}: "
            f"{type(e).__name__}: {e} — refusing to load"
        )
        return None
    if not isinstance(payload, dict) or '_format_version' not in payload:
        logger.warning(
            f"persistence: {path} is not a versioned envelope — refusing"
        )
        return None
    if payload['_format_version'] != expected_version:
        logger.warning(
            f"persistence: {path} format_version "
            f"{payload['_format_version']!r} != expected {expected_version} "
            f"— refusing (re-run the cmd_X to rewrite)"
        )
        return None
    return payload.get('data')


# ─── Public: Cache ─────────────────────────────────────────────────────────

def save_cache(cache, config) -> bool:
    """Persist a populated Cache to ``<dir_cache>/cache.pkl.gz``.
    Cache.__getstate__ handles dropping unpicklable internals
    (DpkgArchTable, defaultdict factories)."""
    path = _cache_path(config)
    ok = _save_pickled(cache, path, _CACHE_FORMAT_VERSION)
    if ok:
        logger.info(f"persistence: saved Cache to {path}")
    return ok


def load_cache(config) -> Optional[object]:
    """Load a previously-saved Cache.  Returns None if absent /
    corrupt / format-version mismatch — caller should fall back to
    running cmd_build_cache."""
    path = _cache_path(config)
    cache = _load_pickled(path, _CACHE_FORMAT_VERSION)
    if cache is not None:
        logger.info(f"persistence: loaded Cache from {path}")
    return cache


# ─── Public: DependencyTree ────────────────────────────────────────────────

def save_dep_tree(dt, config) -> bool:
    """Persist a populated DependencyTree to ``<dir_cache>/deptree.pkl.gz``.
    DependencyTree.__getstate__ drops the back-ref to Cache (we re-wire
    it on load via the explicit ``cache`` arg to ``load_dep_tree``)."""
    path = _deptree_path(config)
    ok = _save_pickled(dt, path, _DEPTREE_FORMAT_VERSION)
    if ok:
        logger.info(f"persistence: saved DependencyTree to {path}")
    return ok


def load_dep_tree(config, cache) -> Optional[object]:
    """Load a previously-saved DependencyTree and rewire its Cache
    back-reference to the supplied ``cache``.  ``cache`` should be the
    same Cache object the DT was built against (or a freshly-loaded
    one — they're equivalent up to format version).

    Returns None if absent / corrupt / format-version mismatch — caller
    should fall back to running cmd_parse_dependency.  Returns None if
    ``cache`` is None too (DT without Cache is useless; explicit refusal
    beats a self.cache=None deref later).
    """
    if cache is None:
        logger.warning(
            "persistence.load_dep_tree: cache is None — DT without "
            "its back-ref is useless; returning None"
        )
        return None
    path = _deptree_path(config)
    dt = _load_pickled(path, _DEPTREE_FORMAT_VERSION)
    if dt is None:
        return None
    # DependencyTree's __getstate__ dropped the __cache attr; re-wire
    # via the same name-mangled key the class uses internally.
    setattr(dt, '_DependencyTree__cache', cache)
    logger.info(f"persistence: loaded DependencyTree from {path}")
    return dt
