"""Repo state scanner + closure auditor + bump planner.

Shared substrate for `repo audit` and `repo repair strip`.

Replaces the per-file DebFile walk in build.py with a single
`dpkg-scanpackages` subprocess that produces a canonical Packages
file from repo/, then parses it via `apt_pkg.TagFile`.  ~10x faster
than the per-file approach on a 5000-pkg repo; lets the audit run as
a chroot-build pre-flight gate instead of a manual operator command.

Primitives:
  scan_repo_state(config) -> RepoState
      Generate + parse + cache.  Cache invalidates on repo/ mtime change.

  audit_dep_closure(state, ...) -> list[(pkg, field, relation, why), ...]
      Resolve every hard Depends/Pre-Depends against the state
      (whole-repo — apt can pull transitive deps from any tier).

  audit_conflict_cohort(state, cohort) -> list[(pkg, field, other, rel), ...]
      Conflicts/Breaks where BOTH ends are in the given install cohort.

  audit_nmu_residue(state) -> list[(pkg, field, value, why), ...]
      Derive what `repo repair strip` should do: which pkgs need a Version
      bump, which need their Depends `(=)` refs rewritten, and the full
      bumped_pkg_set (already-bumped ∪ to-bump).

All three operate on the same RepoState — single source of truth.
"""
import dataclasses
import hashlib
import logging
import os
import re
import subprocess
from typing import Callable, Iterator, Optional, Tuple, TYPE_CHECKING

import apt_pkg
from debian.deb822 import PkgRelation

import utils
from utils import _NMU_STRIP_FIELDS, strip_nmu_suffix

if TYPE_CHECKING:
    # Forward-references for type hints — utils.BuildConfig is the actual
    # type passed for `config` throughout this module.
    from utils import BuildConfig

logger = logging.getLogger('athena')

# Fields we extract from each Packages stanza.  Anything else is
# discarded so the in-memory state stays compact (a full Packages file
# on a 5000-pkg repo is ~3 MB; keeping every field would balloon to
# ~30 MB once split into Python dicts).
_FIELDS_TO_KEEP = (
    'Package', 'Version', 'Architecture', 'Filename', 'Source',
    'Depends', 'Pre-Depends', 'Recommends', 'Suggests',
    'Enhances', 'Conflicts', 'Breaks', 'Replaces', 'Provides',
)

# Relation fields walked by the auditor + bump planner.
_HARD_DEP_FIELDS = ('Depends', 'Pre-Depends')
_CONFLICT_FIELDS = ('Conflicts', 'Breaks')
_WEAK_DEP_FIELDS = ('Recommends',)


@dataclasses.dataclass(frozen=True)
class RepoState:
    """Snapshot of repo/ contents at a point in time.

    packages         — {pkg_name: {control field → value}}; highest
                       version wins on duplicate names (flat repo can
                       hold both pkg_X and pkg_X+asgard1 simultaneously
                       during partial-state recoveries).
    provides_index   — {virtual_name: [(provider_pkg_name, opt_version)]}
                       built from Provides fields.  Used by the auditor
                       to resolve virtual-target deps per Debian Policy
                       §7.5.
    packages_file    — path to the generated Packages stanza on disk
                       (kept around for debugging; same file is cached
                       and reused across audit + rebump within a session).
    repo_mtime       — max mtime across repo/ contents at scan time.
                       Cache invalidation key.
    """
    packages: dict
    provides_index: dict
    packages_file: str
    repo_mtime: float


# Per-process cache.  Key: repo_dir abs path.  Value: (mtime, RepoState).
# Invalidates when repo/ mtime advances OR when invalidate_cache() is
# called explicitly (e.g. after rebump finishes editing files).
_CACHE: 'dict[str, tuple[float, RepoState]]' = {}


def _repo_max_mtime(repo_dir: str) -> float:
    """Max mtime across the repo directory's own stat AND each direct
    child entry.  Including the directory's own mtime catches change
    events that don't bump any surviving file's mtime:

      - Pure deletes (`rm foo.deb`) — surviving files' mtimes unchanged,
        but the dir's mtime bumps when the entry is removed
      - Pure renames (`mv foo.deb bar.deb`) — inode's mtime unchanged
        (rename only touches dir + inode's ctime), but the dir's
        mtime bumps

    File-content modifications, additions, and rebuilds-in-place are
    caught by the per-entry stat fallback.  Together: any visible
    change to repo/ → max_mtime advances → cache invalidates.

    Cheap O(n) scandir; no recursion (repo/ has no subdirs in our flat
    archive).
    """
    _max = 0.0
    try:
        _max = os.stat(repo_dir).st_mtime
    except OSError:
        pass
    try:
        for _entry in os.scandir(repo_dir):
            try:
                _m = _entry.stat().st_mtime
                if _m > _max:
                    _max = _m
            except OSError:
                continue
    except OSError:
        pass
    return _max


def invalidate_cache(repo_dir: Optional[str] = None) -> None:
    """Drop cached RepoState.  Call after operations that edit repo/
    contents (rebump, source build copy-in, etc.) so the next audit
    sees the post-edit state."""
    if repo_dir is None:
        _CACHE.clear()
    else:
        _CACHE.pop(os.path.abspath(repo_dir), None)


def _scan_packages_with_progress(
    argv: 'list[str]', output_path: str,
    cwd: 'Optional[str]' = None,
    *, label_subdir: str = '',
    sudo_password: 'Optional[str]' = None,
) -> bool:
    """Thin wrapper around `apt_repo._run_dpkg_scan` (consolidation).

    Kept under this name because `scan_repo_state` calls it; signature
    simplified — the five "Reserved (kept for caller stability)" params
    from the pre-consolidation shape are gone since there's only one
    caller and it doesn't pass them.

    Returns True on success, False on subprocess failure (logs stderr).
    """
    import apt_repo
    return apt_repo._run_dpkg_scan(
        argv, output_path,
        cwd=cwd, password=sudo_password,
        label=label_subdir,
        # repo-audit scan is allowed empty; caller checks the parsed
        # state and treats no-pkgs as a legitimate "fresh repo".
        allow_empty=True,
    )


def scan_repo_state(config: 'BuildConfig', subdir: str = 'main',
                     refresh: bool = False) -> Optional[RepoState]:
    """Generate + parse a Packages snapshot of one repo/ subdir.

    `subdir` selects which segment of the segregated repo to scan:
      'main'      installable corpus — default, what `repo audit` uses
      'main-udeb' debian-installer udebs (Section: debian-installer)
      'doc'       -doc side artifacts
      'dbgsym'    -dbgsym side artifacts
      'tests'     -test / -tests side artifacts

    (2026-05-22 fix-up): paths come from
    config.deb_dir_for(subdir), which routes to the new nested
    apt-repo layout (e.g. main → dists/<codename>/main/binary-<arch>/,
    dbgsym → dists/<codename>-debug/main/binary-<arch>/).  Pre-Stage-D
    code did `os.path.join(config.dir_repo, subdir)`, which resolved
    to the OLD flat layout and is now wrong.

    Each subdir caches independently (separate persisted Packages file
    per subdir in dir_temp, separate in-memory cache key).

    Mechanism:
      1. Snapshot the subdir's max mtime (includes dir's own mtime so
         pure-delete / pure-rename also invalidate — see _repo_max_mtime).
      2. If in-memory cache matches mtime and refresh=False → return.
      3. Else if persisted `audit-Packages-<subdir>` is newer than the
         subdir's mtime → re-parse it (skip dpkg-scanpackages).
      4. Else run `dpkg-scanpackages --multiversion <subdir>/ /dev/null`
         into dir_temp/audit-Packages-<subdir>.
      5. Parse via apt_pkg.TagFile.  Highest-version dedup per pkg name.
      6. Build Provides index for virtual-dep resolution.

    Returns None on dpkg-scanpackages failure.

    Pass refresh=True to force regen unconditionally.
    """
    try:
        _repo_dir = os.path.abspath(config.deb_dir_for(subdir))
    except ValueError:
        logger.error(
            f"scan_repo_state: unknown subdir label {subdir!r} "
            f"(expected one of main/doc/dbgsym/tests)"
        )
        return None
    _mtime = _repo_max_mtime(_repo_dir)
    if not refresh:
        _cached = _CACHE.get(_repo_dir)
        if _cached and _cached[0] == _mtime:
            return _cached[1]

    _temp_dir = config.dir_temp
    os.makedirs(_temp_dir, exist_ok=True)
    # fix-up: include a hash of the
    # source dir path in the cache filename.  Without it, a cached
    # audit-Packages-main from a pre-Stage-D scan (against the OLD
    # repo/main path, empty post-migration) would collide with the
    # NEW path's cache file → stale empty cache served forever until
    # operator runs `repo audit refresh`.  Hashing the dir path
    # decouples them; old cache files are stranded harmlessly under
    # the old name and the new path gets a fresh cache file.
    _dir_hash = hashlib.sha256(_repo_dir.encode('utf-8')).hexdigest()[:8]
    _pkg_file = os.path.join(
        _temp_dir, f'audit-Packages-{subdir}-{_dir_hash}',
    )

    # Cross-session cache: if the persisted Packages file is newer than
    # any file in repo/, skip the dpkg-scanpackages call entirely and
    # just re-parse the cached file.  Cuts the per-build gate from ~90s
    # to ~3s when repo/ hasn't changed since last session.  Honours the
    # explicit refresh flag.
    #
    # Empty-file guard: a zero-byte cache file is treated as missing
    # and re-scanned.  Defensive against pre-fix runs that landed an
    # empty Packages snapshot on disk (e.g. main-udeb scanned without
    # `-t udeb` before 2026-05-23 produced 0 records → 0 bytes).
    # Without this, the stale empty file would be served forever
    # until the operator manually deletes it or the repo's mtime
    # advances past the cache's mtime.
    _skip_scan = False
    if not refresh:
        try:
            _stat = os.stat(_pkg_file)
            if _stat.st_size > 0 and _stat.st_mtime >= _mtime:
                _skip_scan = True
        except OSError:
            pass

    if not _skip_scan:
        # dpkg-scanpackages reads each .deb's control area (no data
        # extract).  --multiversion emits every version present; we
        # dedupe by highest version in the parse step.  /dev/null is the
        # (empty) override file.
        #
        # `-t udeb` is REQUIRED when scanning a udeb-only dir: by
        # default dpkg-scanpackages picks `.deb` files only — pointed
        # at a dir of `.udeb` files it produces an empty Packages with
        # NO error.  Silent miss: every dep-lookup against the resulting
        # (empty) RepoState then reports unsatisfied.  This bit
        # `source verify` 2026-05-23 — 84 phantom udeb→udeb missed-deps
        # all from the wrong scanner type.
        #
        # Per-file ProgressBar: pre-count .debs for maxvalue, then
        # stream stdout line-by-line and step on every `Package:` header
        # (one per scanned file).  Without this the operator stares at
        # a frozen TUI for ~90s on a 5k-pkg fresh-cache audit.
        _scan_argv = ['dpkg-scanpackages', '--multiversion']
        if subdir == 'main-udeb':
            _scan_argv += ['-t', 'udeb']
        _scan_argv += [_repo_dir, '/dev/null']
        if not _scan_packages_with_progress(
                _scan_argv, _pkg_file,
                cwd=_repo_dir, label_subdir=subdir):
            return None

    apt_pkg.init_system()
    _packages: dict = {}
    with open(_pkg_file) as _fh:
        _tagfile = apt_pkg.TagFile(_fh)
        for _section in _tagfile:
            _pkg = (_section.get('Package') or '').strip()
            _ver = (_section.get('Version') or '').strip()
            if not _pkg or not _ver:
                continue
            _prev = _packages.get(_pkg)
            if _prev is not None:
                try:
                    if apt_pkg.version_compare(_ver, _prev['Version']) <= 0:
                        continue
                except Exception:
                    continue
            # Snapshot only the fields we need.  TagSection is backed by
            # the file handle and the values it returns become invalid
            # once the loop advances; coerce to str now.
            _entry = {}
            for _field in _FIELDS_TO_KEEP:
                _val = _section.get(_field)
                if _val is not None:
                    _entry[_field] = str(_val)
            _packages[_pkg] = _entry

    _provides_index: dict = _build_provides_index(_packages)

    _state = RepoState(
        packages=_packages,
        provides_index=_provides_index,
        packages_file=_pkg_file,
        repo_mtime=_mtime,
    )
    _CACHE[_repo_dir] = (_mtime, _state)
    return _state


def iter_packages_all_versions(
    config: 'BuildConfig', subdir: str = 'main',
    refresh: bool = False,
) -> 'Iterator[Tuple[str, dict]]':
    """Yield every (filename, control_dict) pair across ALL versions of
    every package in `subdir`.

    Unlike scan_repo_state (which dedupes by highest version per name),
    this iterator yields a separate record per .deb file on disk.
    Needed by `repo cleanup` and any other consumer that has to
    operate per-file (e.g. to delete older versions that scan_repo_state
    would hide).

    Replaces the per-file DebFile walk
    in cmd_package_cleanup's _scan_for_obsoletes — was ~N×fork+exec+
    tar-extract+parse; now a single dpkg-scanpackages run (cached
    by scan_repo_state) + a fast apt_pkg.TagFile pass.  Order of
    magnitude faster on a 5k-pkg repo.

    The cached Packages file is the dpkg-scanpackages --multiversion
    output — it already contains every version's stanza.
    scan_repo_state's parser dedupes; this function reuses the same
    cached file but skips the dedup step.

    Args:
        config:  BuildConfig (used for the dir-resolution + cache file).
        subdir:  'main' / 'doc' / 'dbgsym' / 'tests'.
        refresh: Force regen of the Packages snapshot (passed through
                 to scan_repo_state).

    Yields:
        (filename, control_dict) — filename is the basename from the
        Filename: field; control_dict has Package, Version, Source,
        Filename, Size and every other field dpkg-scanpackages emitted.
    """
    # Reuse scan_repo_state for the cache + Packages-file generation.
    # Discard its deduped `.packages` dict; we want the raw per-file
    # stanzas from `.packages_file`.
    _state = scan_repo_state(config, subdir=subdir, refresh=refresh)
    if _state is None:
        return
    try:
        with open(_state.packages_file) as _fh:
            _tagfile = apt_pkg.TagFile(_fh)
            for _section in _tagfile:
                _filename = (_section.get('Filename') or '').strip()
                if not _filename:
                    continue
                _basename = os.path.basename(_filename)
                yield (_basename, dict(_section))
    except OSError as e:
        logger.error(
            f"iter_packages_all_versions: cannot read {_state.packages_file}: {e}"
        )
        return


# ─────────────────────────────────────────────────────────────────────────────
# remote ledger (the published archive of record)
#
# Local repo/ is single-snapshot; the REMOTE accumulates every version ever
# published (base_snapshot..current_snapshot).  These helpers read the remote
# Packages index into a {package: [version, ...]} "ledger" that drives:
#   - N-derivation for +asg<R>u<N> (utils.highest_asg_update)
#   - change-detection (published_base_versions)
#   - the multi-version remote audit + archivable-below-base reporting
# ─────────────────────────────────────────────────────────────────────────────

def parse_packages_to_ledger(packages_path: str) -> 'dict[str, list[str]]':
    """Parse a dpkg Packages file into {package: [version, ...]}.

    Versions are stored epoch-stripped (utils.version_no_epoch) to match the
    filename-derived bases the build-side N-derivation uses.  Multi-version
    aware — every stanza contributes a version."""
    _ledger: 'dict[str, list[str]]' = {}
    try:
        with open(packages_path) as _fh:
            _tf = apt_pkg.TagFile(_fh)
            for _sec in _tf:
                _name = (_sec.get('Package') or '').strip()
                _ver = (_sec.get('Version') or '').strip()
                if _name and _ver:
                    _ledger.setdefault(_name, []).append(
                        utils.version_no_epoch(_ver))
    except OSError as e:
        logger.error(f"parse_packages_to_ledger: {packages_path}: {e}")
    return _ledger


# ─────────────────────────────────────────────────────────────────────────────
# Local signed published-manifest — a Packages-format mirror of what's
# published, the AUTHORITY for +asg bump numbers and offline merge-index.  When
# external is enabled it mirrors the remote; when disabled it's the only record.
# Durable (config/), gitignored, signed with the project key for integrity.
# ─────────────────────────────────────────────────────────────────────────────

def local_manifest_path(config: 'BuildConfig') -> str:
    return os.path.join(config.dir_config, 'published.manifest')


def _write_signed_manifest(path: str, packages_text: str,
                           config: 'BuildConfig') -> bool:
    """Write a Packages-format manifest + detached GPG signature.

    Returns True iff BOTH the manifest text AND a valid detached signature
    landed on disk.  Returns False on ANY failure — disk write, signing-
    setup (missing signing module / homedir), or gpg sign command failure
    — AND removes any partially-written manifest so a future
    `_read_signed_manifest` returns '' (no false authority).

    Previously logged WARNING + fell through on
    broad exception during signing setup, leaving an UNSIGNED manifest on
    disk.  `_read_signed_manifest` only verifies when `.sig` is present,
    so a silently-unsigned write became a silently-trusted read on the
    next publish — and the local manifest is the authority for `+asg uN`
    bump derivation.  Quiet trust degradation could mint colliding bump
    numbers.  Now we fail loud: caller (`write_published_manifest` →
    `cmd_repo_publish`) checks the bool and surfaces to the operator.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as _fh:
            _fh.write(packages_text)
    except OSError as e:
        logger.error(f"_write_signed_manifest: {path}: {e}")
        return False

    def _scrub_manifest() -> None:
        # Remove the unsigned manifest + any stale .sig so the next read
        # treats it as absent rather than authoritative.
        for _f in (path, path + '.sig'):
            try:
                os.remove(_f)
            except OSError:
                pass

    try:
        import signing
        _home = signing.signing_home(config)
        if not _home or not os.path.isdir(_home):
            raise RuntimeError(
                f"signing homedir missing or unreadable: {_home!r} — "
                f"generate a signing key first (`key generate`)")
        _sig = path + '.sig'
        if os.path.exists(_sig):
            os.remove(_sig)
        subprocess.run(
            ['gpg', '--homedir', _home, '--batch', '--yes',
             '--detach-sign', '--armor', '-o', _sig, path],
            check=True, capture_output=True)
        return True
    except Exception as e:
        logger.error(
            f"_write_signed_manifest: signing FAILED for {path} ({e}) — "
            f"removing unsigned manifest to prevent false authority")
        _scrub_manifest()
        return False


def _read_signed_manifest(path: str, config: 'BuildConfig') -> str:
    """Return the manifest text (signature-verified); '' if absent, if a
    present signature FAILS to verify, or if signing setup itself fails.

    Previously, a broad exception during
    `import signing` / homedir lookup logged WARNING and fell through to
    read the file UNVERIFIED.  A configuration error in signing must
    never cause the caller to trust unverified content — especially
    since the manifest is the authority for `+asg uN` bump derivation.
    Now: any signing-setup failure with a `.sig` present returns ''
    (manifest treated as absent), the same as a verify failure.
    """
    if not os.path.isfile(path):
        return ''
    _sig = path + '.sig'
    if os.path.exists(_sig):
        try:
            import signing
            _home = signing.signing_home(config)
            if not _home or not os.path.isdir(_home):
                logger.error(
                    f"_read_signed_manifest: signing homedir missing — "
                    f"refusing to read {path} unverified")
                return ''
            _r = subprocess.run(
                ['gpg', '--homedir', _home, '--batch', '--verify', _sig, path],
                capture_output=True)
            if _r.returncode != 0:
                logger.error(
                    f"_read_signed_manifest: SIGNATURE VERIFY FAILED for "
                    f"{path} — refusing to use it")
                return ''
        except Exception as e:
            logger.error(
                f"_read_signed_manifest: signing setup failed ({e}) — "
                f"refusing to read {path} unverified")
            return ''
    try:
        with open(path) as _fh:
            return _fh.read()
    except OSError as e:
        logger.error(f"_read_signed_manifest: {path}: {e}")
        return ''


def write_published_manifest(config: 'BuildConfig',
                             packages_text: str) -> bool:
    """Write the published-manifest + detached signature.  Returns True iff
    BOTH text and signature landed on disk; False on any failure (manifest
    is removed in that case — see _write_signed_manifest)."""
    return _write_signed_manifest(
        local_manifest_path(config), packages_text, config)


def read_published_manifest(config: 'BuildConfig') -> str:
    """Return the published-manifest text (signature-verified); '' if absent or
    tampered."""
    return _read_signed_manifest(local_manifest_path(config), config)


def local_published_packages_text(config: 'BuildConfig') -> str:
    """Concatenate every binary `Packages` index under the locally-published
    tree (``repo/dists/<codename>/**/binary-*/Packages``, incl. the udeb
    ``debian-installer`` trees).

    This is the text written to the signed ``published.manifest`` after a
    successful ``mirror publish`` — the authority for +asg uN bump
    derivation (``published_ledger``).  Must be read AFTER the publish has
    (auto-)indexed the repo, so the Packages files exist and reflect the
    versions actually pushed."""
    _root = os.path.join(config.dir_repo, 'dists', config.build_codename)
    _parts: 'list[str]' = []
    for _dp, _dirs, _files in os.walk(_root):
        if 'Packages' not in _files:
            continue
        if not os.path.basename(_dp).startswith('binary-'):
            continue
        try:
            with open(os.path.join(_dp, 'Packages'),
                      encoding='utf-8', errors='replace') as _fh:
                _t = _fh.read()
        except OSError:
            continue
        if _t.strip():
            _parts.append(_t if _t.endswith('\n') else _t + '\n')
    return '\n'.join(_parts)


_PUBLISHED_LEDGER_CACHE: 'dict' = {}


def published_ledger(config: 'BuildConfig') -> 'dict[str, list[str]]':
    """The authoritative {package: [version,...]} ledger for +asg bump
    derivation + merge-index — from the LOCAL signed manifest (works offline).
    Empty dict when nothing has been published yet.

    Memoised on the manifest's (path, mtime, size): `source audit` calls this
    up to 3x and each call re-read + GPG-verified + re-parsed the whole
    manifest (the audit-startup delay).  Re-reads only when the manifest
    actually changes (e.g. after a `mirror publish` in the same process).
    Callers treat the result read-only.
    """
    _path = local_manifest_path(config)
    try:
        _st = os.stat(_path)
        _key: tuple = (_path, _st.st_mtime_ns, _st.st_size)
    except OSError:
        _key = (_path, None, None)
    if _key in _PUBLISHED_LEDGER_CACHE:
        return _PUBLISHED_LEDGER_CACHE[_key]
    _text = read_published_manifest(config)
    if not _text.strip():
        _PUBLISHED_LEDGER_CACHE[_key] = {}
        return {}
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.Packages',
                                     delete=False) as _fh:
        _fh.write(_text)
        _tp = _fh.name
    try:
        _result = parse_packages_to_ledger(_tp)
    finally:
        os.remove(_tp)
    _PUBLISHED_LEDGER_CACHE[_key] = _result
    return _result


def highest_stanza_per_pkg_arch(
    packages_text: str, component: str, arch: str,
    into: 'Optional[dict]' = None,
) -> 'dict':
    """Fold one ``binary-<arch>/Packages`` text into ``{"<pkg>|<arch>": stanza}``
    keeping the HIGHEST version per package.  Each value is
    ``{filename, sha256, size, version, component, package, arch}`` (filename
    is the basename).

    ``arch`` is the DIRECTORY arch (the ``binary-<arch>`` the index lives
    under), NOT the stanza ``Architecture:`` field — so an ``Architecture: all``
    package keys under the dir arch, matching ``audit_packages_chain``'s
    filename index (which keys arch by the same dir).  This keeps the producer
    (ledger) and validator (recompute) byte-consistent.

    Unlike ``parse_packages_to_ledger`` (versions-only) this RETAINS the
    Filename/SHA256/Size a closure-ledger download needs.  Pass ``into`` to
    merge across multiple component/arch Packages files."""
    import tempfile
    _acc = into if into is not None else {}
    apt_pkg.init_system()
    with tempfile.NamedTemporaryFile('w', suffix='.Packages',
                                     delete=False) as _fh:
        _fh.write(packages_text)
        _tp = _fh.name
    try:
        with open(_tp) as _fh:
            _tf = apt_pkg.TagFile(_fh)
            for _sec in _tf:
                _name = (_sec.get('Package') or '').strip()
                _ver = (_sec.get('Version') or '').strip()
                _fn = (_sec.get('Filename') or '').strip()
                if not (_name and _ver and _fn):
                    continue
                _key = f"{_name}|{arch}"
                _prev = _acc.get(_key)
                if _prev is not None:
                    try:
                        if apt_pkg.version_compare(_ver, _prev['version']) <= 0:
                            continue
                    except Exception:
                        continue
                try:
                    _size = int((_sec.get('Size') or '0').strip() or 0)
                except ValueError:
                    _size = 0
                _acc[_key] = {
                    'filename':  os.path.basename(_fn),
                    'sha256':    (_sec.get('SHA256') or '').strip(),
                    'size':      _size,
                    'version':   _ver,
                    'component': component,
                    'package':   _name,
                    'arch':      arch,
                }
    finally:
        os.remove(_tp)
    return _acc


def iter_published_packages_with_component(
    config: 'BuildConfig',
) -> 'Iterator[Tuple[str, str, str]]':
    """Yield ``(component, arch, packages_text)`` for every binary Packages
    index under the locally-published tree
    (``dists/<codename>/**/binary-*/Packages``, including the udeb
    ``debian-installer`` trees).  ``component`` is the path segment(s) between
    ``<codename>/`` and ``/binary-<arch>/`` (e.g. ``main`` or
    ``main/debian-installer``); ``arch`` is the ``binary-<arch>`` dir arch.

    Same tree walk as ``local_published_packages_text`` but per-file + with the
    component/arch tags, for the closure-ledger producer."""
    _root = os.path.join(config.dir_repo, 'dists', config.build_codename)
    for _dp, _dirs, _files in os.walk(_root):
        if 'Packages' not in _files:
            continue
        _base = os.path.basename(_dp)
        if not _base.startswith('binary-'):
            continue
        _arch = _base[len('binary-'):]
        _component = os.path.relpath(os.path.dirname(_dp), _root)
        try:
            with open(os.path.join(_dp, 'Packages'),
                      encoding='utf-8', errors='replace') as _fh:
                _text = _fh.read()
        except OSError:
            continue
        if _text.strip():
            yield (_component, _arch, _text)


def published_ledger_entries(config: 'BuildConfig') -> 'dict':
    """The published-ledger entries map: walk the locally-published Packages,
    fold to the highest version per (package, dir-arch), and return EVERY
    published binary — the FULL distribution set, NOT just the install closure.
    Keyed ``"<package>|<arch>"``.

    A federation peer drives its ``mirror pull`` off this, so it must cover the
    complete published set (including legitimately out-of-closure
    ``-dev``/``-doc``/``-udeb`` binaries) — limiting it to the closure strands
    those and makes a peer non-conformant (and ``virtual build`` false-conflict
    on every un-adopted file).  The ONLY exclusion is the ARCH filter: foreign
    cross-toolchain by-products (already kept out of the pool at claim time;
    re-applied defensively here).  Latest version per (pkg, arch)."""
    import arch_filter
    _acc: dict = {}
    for _component, _arch, _text in iter_published_packages_with_component(
            config):
        highest_stanza_per_pkg_arch(_text, _component, _arch, into=_acc)
    _build_arch = getattr(config, 'arch', None)
    return {_k: _v for _k, _v in _acc.items()
            if not (_build_arch and arch_filter.is_foreign_target_binary(
                str(_v.get('package') or ''), _build_arch))}


def repo_state_from_packages_text(packages_text: str) -> RepoState:
    """Build a RepoState (packages + provides_index) from raw Packages text —
    no dpkg-scanpackages / disk scan.  Highest version per name wins (same
    dedup as ``scan_repo_state``).

    Used by ``audit_closure_ledger`` to re-resolve the published closure over
    the VERIFIED remote Packages bytes, which carry Depends/Provides (unlike
    ``audit_packages_chain``'s filename-keyed index)."""
    import tempfile
    apt_pkg.init_system()
    _packages: dict = {}
    with tempfile.NamedTemporaryFile('w', suffix='.Packages',
                                     delete=False) as _fh:
        _fh.write(packages_text)
        _tp = _fh.name
    try:
        with open(_tp) as _fh:
            _tagfile = apt_pkg.TagFile(_fh)
            for _section in _tagfile:
                _pkg = (_section.get('Package') or '').strip()
                _ver = (_section.get('Version') or '').strip()
                if not _pkg or not _ver:
                    continue
                _prev = _packages.get(_pkg)
                if _prev is not None:
                    try:
                        if apt_pkg.version_compare(
                                _ver, _prev['Version']) <= 0:
                            continue
                    except Exception:
                        continue
                _entry = {}
                for _field in _FIELDS_TO_KEEP:
                    _val = _section.get(_field)
                    if _val is not None:
                        _entry[_field] = str(_val)
                _packages[_pkg] = _entry
    finally:
        os.remove(_tp)
    return RepoState(
        packages=_packages,
        provides_index=_build_provides_index(_packages),
        packages_file='',
        repo_mtime=0.0,
    )


def fetch_source_versions_at(config: 'BuildConfig',
                             target_ts: str) -> 'Optional[dict]':
    """Download each mirror's Sources index at `target_ts` and return
    {source_name: highest_version} — the upstream source versions AT that
    snapshot.  Metadata-only (no build); powers `snapshot workload`'s
    current→target diff.  Returns None on any fetch failure (caller aborts)."""
    import gzip
    import json
    import lzma
    import tempfile
    # The Sources index at a pinned snapshot timestamp is immutable, so cache
    # the parsed {name: version} on disk keyed by the timestamp.  `source audit`
    # calls this every run via _workload_since_snapshot, and re-downloading the
    # multi-MB index over a thin link was the audit-startup delay.
    _cache_file = os.path.join(
        config.dir_cache, f'source-versions-{target_ts}.json')
    try:
        with open(_cache_file) as _cf:
            _cached = json.load(_cf)
        if isinstance(_cached, dict):
            return _cached
    except (OSError, ValueError):
        pass
    _out: 'dict[str, str]' = {}
    for _m in config.mirrors:
        _snap = _m.with_snapshot(target_ts, baseurl=config.snapshot_baseurl)
        _base = _snap.dist_url.rstrip('/')
        _got = False
        for _ext, _decomp in (('.xz', lzma.decompress), ('.gz', gzip.decompress)):
            _url = f"{_base}/{_snap.sources_path}{_ext}"
            try:
                _r = utils._http_session().get(
                    _url, timeout=utils._HTTP_TIMEOUT_STREAM)
                if _r.status_code != 200:
                    continue
                _text = _decomp(_r.content).decode('utf-8', errors='replace')
            except Exception as e:
                logger.warning(f"fetch_source_versions_at {_url}: {e}")
                continue
            with tempfile.NamedTemporaryFile(
                    'w', suffix='.Sources', delete=False) as _fh:
                _fh.write(_text)
                _tp = _fh.name
            try:
                with open(_tp) as _rf:
                    for _sec in apt_pkg.TagFile(_rf):
                        _name = (_sec.get('Package') or '').strip()
                        _ver = (_sec.get('Version') or '').strip()
                        if not _name or not _ver:
                            continue
                        _prev = _out.get(_name)
                        if _prev is None or apt_pkg.version_compare(_ver, _prev) > 0:
                            _out[_name] = _ver
            finally:
                os.remove(_tp)
            _got = True
            break
        if not _got:
            logger.error(
                f"fetch_source_versions_at: no Sources for mirror {_m.id} "
                f"at {target_ts}")
            return None
    # every mirror fetched OK — persist (immutable per timestamp) so the next
    # `source audit` reads it from disk instead of re-downloading.
    try:
        with open(_cache_file, 'w') as _cf:
            json.dump(_out, _cf)
    except OSError:
        pass
    return _out


def published_base_versions(ledger: 'dict[str, list[str]]') -> 'dict[str, str]':
    """For each package in the ledger, the highest pristine BASE version
    currently published.  Drives change-detection: a source whose new base
    compares greater than this needs a rebuild."""
    _out: 'dict[str, str]' = {}
    for _pkg, _vers in ledger.items():
        _hi = None
        for _v in _vers:
            _b = utils.pristine_base(_v)
            if _hi is None or apt_pkg.version_compare(_b, _hi) > 0:
                _hi = _b
        if _hi is not None:
            _out[_pkg] = _hi
    return _out


def manifest_vs_remote_discrepancies(
    local_ledger: dict, remote_ledger: dict,
) -> 'Tuple[set, set]':
    """Compare two {package: [version,...]} ledgers (local signed manifest vs
    a peer mirror's Packages); return (only_local, only_remote) as sets of
    'pkg=version' — the integrity reconciliation behind `mirror audit`."""
    _l = {f"{_p}={_v}" for _p, _vs in local_ledger.items() for _v in _vs}
    _r = {f"{_p}={_v}" for _p, _vs in remote_ledger.items() for _v in _vs}
    return (_l - _r, _r - _l)


def group_by_pristine_base(versions: 'list[str]') -> 'dict[str, list[str]]':
    """Group an iterable of version strings by their pristine base — used for
    'N versions retained per package' reports and archivable-below-base
    listing."""
    _out: 'dict[str, list[str]]' = {}
    for _v in versions:
        _out.setdefault(utils.pristine_base(_v), []).append(_v)
    return _out


def _build_provides_index(packages: dict) -> dict:
    """{virtual_name: [(provider_pkg, opt_version)]} from Provides
    fields.  Uses python-debian's PkgRelation parser — handles the
    `name (= V)` versioned-Provides syntax that Debian Policy §7.5
    requires for satisfying versioned virtual deps."""
    _index: dict = {}
    for _pkg, _entry in packages.items():
        _raw = _entry.get('Provides', '')
        if not _raw:
            continue
        try:
            for _or_group in PkgRelation.parse_relations(_raw):
                for _rel in _or_group:
                    _vname = _rel.get('name', '')
                    _vc = _rel.get('version')
                    _vver = _vc[1] if _vc else None
                    if _vname:
                        _index.setdefault(_vname, []).append((_pkg, _vver))
        except Exception:
            continue
    return _index


def audit_nmu_residue(state: RepoState,
                       progress_cb: Optional[Callable[[], None]] = None,
                       tunnel_sources: Optional['set[str]'] = None,
                       ) -> 'list[tuple]':
    """Walk every pkg in state; report any field whose value still
    carries an NMU/binNMU/backport suffix.

    Returns a list of (pkg, field, raw_value, why) entries.  Each entry
    is one violation.  Field is either 'Version' (the pkg's own Version
    field) or a relation field name ('Depends', 'Conflicts', ...) when
    a version constraint inside it has NMU residue.

    Used as a verification pass after `repo repair strip` (and as a
    standing check that the post-build normaliser actually ran).
    Anything reported here means apt sees a non-normalised version
    constraint — install-time risk if the target ships at a stripped
    version.

    `tunnel_sources`: set of SOURCE names (matches `[Source] Tunneled` in
    build.conf).  Any binary whose `Source:` field is in this set is
    SKIPPED — tunneled packages are pristine Debian binary passthrough
    and MUST keep their upstream ~debNuN / +debNuN suffix (the .deb's
    on-disk filename has to match its internal control Version, and for
    Microsoft-signed binaries like shim-signed any rewrite would
    invalidate the signature).  Caught 2026-05-28 when audit_nmu flagged
    shim-signed / shim-signed-common / shim-helpers-amd64-signed as
    "residue" and `repo repair strip` would have corrupted them.
    """
    _findings: 'list[tuple]' = []
    _tun = tunnel_sources or set()

    _constraint_re = re.compile(
        r'\(\s*(?:<=|>=|<<|>>|=)\s*([^)]+?)\s*\)'
    )

    for _pkg, _entry in state.packages.items():
        if progress_cb is not None:
            progress_cb()
        # Tunneled packages: source name (Source field, or pkg name when
        # absent — stripped of any "(version)" trailer).
        if _tun:
            _src_field = (_entry.get('Source') or _pkg).strip()
            _src_name = _src_field.split(' ', 1)[0]
            if _src_name in _tun:
                continue
        # An +asg<R>uK-anchored version is OUR normalized transpose form; its
        # trailing +pP / +bN are legitimate patch/force layers, NOT upstream
        # NMU residue (strip_nmu_suffix would wrongly strip the +bN).  Only
        # un-anchored versions can carry the +debNuN residue this audit exists
        # to catch (audit #169).
        _ver = _entry.get('Version', '')
        if (_ver and utils.parse_asg_suffix(_ver) is None
                and strip_nmu_suffix(_ver) != _ver):
            _findings.append((_pkg, 'Version', _ver, 'pkg own Version'))
        for _field in _NMU_STRIP_FIELDS:
            _raw = _entry.get(_field, '')
            if not _raw:
                continue
            for _m in _constraint_re.finditer(_raw):
                _v = _m.group(1)
                if (utils.parse_asg_suffix(_v) is None
                        and strip_nmu_suffix(_v) != _v):
                    _findings.append(
                        (_pkg, _field, _m.group(0),
                         f'version constraint {_m.group(0)} has NMU layer')
                    )
    return _findings


def _ver_holds(actual: str, op: str, want: str) -> bool:
    """apt_pkg.check_dep wrapper.  Returns False on parse error."""
    try:
        return apt_pkg.check_dep(actual, op, want)
    except Exception:
        return False


def _rel_satisfied_in_scope(state: RepoState, rel: 'PkgRelation.ParsedRelation',
                              scope: Optional[frozenset] = None,
                              exclude_provider: str = '') -> bool:
    """True if some pkg in `scope` satisfies `rel`.

    scope=None means "anywhere in state.packages" (whole-repo resolution).
    exclude_provider: pkg name to exclude from both real-pkg + Provides
    resolution — used in conflict checks to filter the "I conflict with
    virtual X which I myself provide" self-conflict idiom.
    """
    _target = rel.get('name', '')
    _vc = rel.get('version')
    _op: 'str | None'
    _wver: 'str | None'
    if _vc:
        _op, _wver = _vc
    else:
        _op, _wver = None, None

    def _in_scope(name: str) -> bool:
        return scope is None or name in scope

    # Real pkg
    if _target != exclude_provider:
        _entry = state.packages.get(_target)
        if _entry is not None and _in_scope(_target):
            if _op is None or _wver is None or _ver_holds(_entry['Version'], _op, _wver):
                return True
    # Virtual via Provides.  Per Debian Policy §7.5: a versioned
    # Provides counts "exactly as if it were a real package" for
    # dependency satisfaction.  So `Provides: X (= 11.1.0)` satisfies
    # `Depends: X (>= 3.0-9)` because apt_pkg.check_dep('11.1.0',
    # '>=', '3.0-9') is True — any comparison operator works.
    # Unversioned Provides (`Provides: X` with no version) do NOT
    # satisfy versioned Depends — policy reserves them for matching
    # an unversioned Depends only.
    for _provider, _prov_ver in state.provides_index.get(_target, []):
        if _provider == exclude_provider:
            continue
        if not _in_scope(_provider):
            continue
        if _op is None or _wver is None:
            return True
        if _prov_ver is not None and _ver_holds(_prov_ver, _op, _wver):
            return True
    return False


def audit_dep_closure(state: RepoState,
                       consumer_set: Optional[frozenset] = None,
                       progress_cb: Optional['Callable[[], None]'] = None
                       ) -> 'tuple[list, list]':
    """Hard dependency gate.

    consumer_set — the install corpus.  Only pkgs IN this set are
        audited as consumers.  None = audit every pkg in state (whole-
        repo scan).  In practice the caller passes
        `dep_tree.selected_pkgs ∪ udeb_dep_tree.selected_pkgs` — the
        union of every tier that gets dpkg-installed somewhere: base,
        pkg.list, live.list, installer.list, pool.list.  Anything
        OUTSIDE this set is a side artifact of dpkg-buildpackage
        (libfoo-dev / libfoo-doc / libfoo-tests / libfoo-dbgsym from
        a libfoo source we built but never selected for install).
        Their hard deps are NOT install-correctness concerns.

    Resolution scope is always the WHOLE REPO — apt at install time
    can pull transitive deps from any pkg in /pool/, even if it's not
    in the install corpus.  Honours Provides per Debian Policy §7.5
    (versioned Provides count exactly as if they were real pkgs).

    Returns (unresolved, weak):
      - unresolved is a list of (pkg, field, relation_str, why) 4-tuples
      - weak       is a list of (pkg, field, relation_str) 3-tuples
    """
    _unresolved: list = []
    _weak: list = []

    def _is_consumer(name: str) -> bool:
        return consumer_set is None or name in consumer_set

    for _pkg, _entry in state.packages.items():
        if progress_cb is not None:
            progress_cb()
        if not _is_consumer(_pkg):
            continue
        for _field in _HARD_DEP_FIELDS:
            _raw = _entry.get(_field, '')
            if not _raw:
                continue
            try:
                _relations = PkgRelation.parse_relations(_raw)
            except Exception:
                continue
            for _or_group in _relations:
                if not any(_rel_satisfied_in_scope(state, _r)
                           for _r in _or_group):
                    _rel_str = PkgRelation.str([_or_group])
                    _unresolved.append(
                        (_pkg, _field, _rel_str, 'no satisfying pkg in repo/')
                    )
        for _field in _WEAK_DEP_FIELDS:
            _raw = _entry.get(_field, '')
            if not _raw:
                continue
            try:
                _relations = PkgRelation.parse_relations(_raw)
            except Exception:
                continue
            for _or_group in _relations:
                if not any(_rel_satisfied_in_scope(state, _r)
                           for _r in _or_group):
                    _rel_str = PkgRelation.str([_or_group])
                    _weak.append((_pkg, _field, _rel_str))

    return _unresolved, _weak


def detect_dangling_asg_equals_pins(
        state: RepoState,
        consumer_set: 'Optional[frozenset]' = None) -> 'list[tuple]':
    """Classify the install-breaking subclass of unresolved `=` pins.

    A hard `Depends`/`Pre-Depends` exact pin `T (= V)` that nothing
    satisfies BECAUSE the repo carries T at `V+asg<R>u<N>` (a stamped
    target) rather than at the pristine `V` the pin was stripped down to.

    This is the cross-source `=`-pin hazard: our build generates A's dep
    against the upstream snapshot's T (`= V+debNuN`), the post-build strip
    peels it to `= V`, but our published T is `V+asg<R>u<N>` — and since
    `+asg` sorts BELOW `+deb` and `V+asg…` ≠ `V`, the pin dangles and A is
    uninstallable.  Same-source SIBLING pins are NOT flagged: the uniform-N
    restamp already rewrites them to `(= V+asg<R>u<N>)`, so an exact match
    exists and they never reach this classifier.  `audit_dep_closure`
    already reports these as generic unresolved deps; this surfaces the
    specific, actionable subclass with a remedy.

    Returns list of
    (consumer, field, target, pinned_version, available_version, remedy).
    """
    _out: 'list[tuple]' = []
    for _pkg, _entry in state.packages.items():
        if consumer_set is not None and _pkg not in consumer_set:
            continue
        for _field in _HARD_DEP_FIELDS:
            _raw = _entry.get(_field, '')
            if not _raw:
                continue
            try:
                _relations = PkgRelation.parse_relations(_raw)
            except Exception:
                continue
            for _or_group in _relations:
                # An OR-group with a working alternative is satisfiable a
                # different way — not our bug.  Only single-alternative `=`
                # pins can dangle this way.
                if len(_or_group) != 1:
                    continue
                _vc = _or_group[0].get('version')
                if not _vc:
                    continue
                _op, _wver = _vc
                if _op != '=':
                    continue
                _target = _or_group[0].get('name', '')
                _tentry = state.packages.get(_target)
                if _tentry is None:
                    continue
                _avail = _tentry.get('Version', '')
                if _avail == _wver:
                    continue          # exact match → satisfied, fine
                # The actionable subclass: same pristine base, the available
                # target is +asg-stamped, and the pin is the bare pristine.
                if (utils.pristine_base(_avail) == utils.pristine_base(_wver)
                        and utils.parse_asg_suffix(_avail) is not None
                        and utils.parse_asg_suffix(_wver) is None):
                    _remedy = (f"relax to `{_target} (>= {_wver})` or rewrite "
                               f"the pin to `(= {_avail})`")
                    _out.append(
                        (_pkg, _field, _target, _wver, _avail, _remedy))
    return _out


def audit_conflict_cohort(state: RepoState, cohort: frozenset,
                            progress_cb: Optional[Callable[[], None]] = None
                            ) -> list:
    """Hard conflict gate: within a cohort (a set of pkgs that get
    installed SIMULTANEOUSLY), no pkg's Conflicts/Breaks may resolve
    to another pkg in the same cohort.

    Cohorts are tier-coherent install sets:
      live chroot           = dep_tree.selected_pkgs - pool_extras
                              - installer_exclusive
      installer ramdisk     = udeb_dep_tree.selected_pkgs

    Cross-cohort conflicts (e.g. grub-pc in pool vs grub-efi-amd64 in
    live) are NOT flagged here — apt arbitrates at install time and
    the operator can have both available in the repo.

    Returns: list of (pkg, field, other_or_virtual_label, rel_str)

    Self-conflict via Provides (`A Conflicts: X` where `A Provides: X`
    is the only provider) is filtered as a no-op.
    """
    _conflicts: list = []

    for _pkg, _entry in state.packages.items():
        if progress_cb is not None:
            progress_cb()
        if _pkg not in cohort:
            continue
        _self_name = _pkg
        for _field in _CONFLICT_FIELDS:
            _raw = _entry.get(_field, '')
            if not _raw:
                continue
            try:
                _relations = PkgRelation.parse_relations(_raw)
            except Exception:
                continue
            for _or_group in _relations:
                for _rel in _or_group:
                    _target = _rel.get('name', '')
                    if _target == _self_name:
                        continue
                    if _rel_satisfied_in_scope(
                        state, _rel, scope=cohort,
                        exclude_provider=_self_name,
                    ):
                        _other_entry = state.packages.get(_target)
                        _other = (
                            _other_entry['Package'] if _other_entry
                            else f'<virtual {_target}>'
                        )
                        _rel_str = PkgRelation.str([[_rel]])
                        _conflicts.append(
                            (_pkg, _field, _other, _rel_str)
                        )
    return _conflicts


