import bz2, gzip, lzma
import logging
import os
import re
import shutil
import apt_pkg

from debian.deb822 import Release
from debian.debian_support import DpkgArchTable, Version
from typing import Callable, List, Dict, Optional, Tuple
from collections import defaultdict

# Internal
import utils, package, tui, fork_mirror
from utils import BuildConfig
from package import Package, Source

# https://github.com/romlok/python-debian/tree/master/examples
# https://www.juliensobczak.com/inspect/2021/05/15/linux-packages-under-the-hood.html

from tui import ProgressBar, Spinner

logger = logging.getLogger('athena.cache')


def _lookup_packages(hashtable: 'Dict[str, Dict[Version, List[Package]]]',
                     package_name: str,
                     version: 'Optional[Version]' = None,
                     constraint: str = '') -> 'List[Package]':
    """Shared backend for Cache.get_packages and UdebCacheView.get_packages.

    If version is omitted, all versions are returned.
    If version is given without constraint, constraint defaults to '>='.
    If both are given, only packages whose version satisfies
    apt_pkg.check_dep(pkg_ver, constraint, version) are returned.

    Use .get() rather than bracket access: callers pass defaultdict
    hashtables; bracket access on a missing key would silently create
    an empty entry — every unresolved name would bloat the table and
    skew len() counts.
    """
    if version is None:
        result: 'List[Package]' = []
        for _pkgs in hashtable.get(package_name, {}).values():
            result.extend(_pkgs)
        return result

    _constraint = constraint if constraint in utils.VALID_CONSTRAINTS else '>='
    result = []
    for _pkg_version, _pkgs in hashtable.get(package_name, {}).items():
        try:
            if apt_pkg.check_dep(str(_pkg_version), _constraint, str(version)):
                result.extend(_pkgs)
        except (SystemError, ValueError):
            # apt_pkg.Error inherits from SystemError; ValueError covers
            # str() of a Version that can't be coerced.  Silently skip
            # constraint failures — the candidate just doesn't qualify.
            pass
    return result


# gcc-N or gcc-N-base — the C compiler's own package and the matching libgcc
# base symbol package for major gcc release N.  Used by Cache.build_cache to
# pick the latest gcc major present in the index and drop older majors from
# self.required (otherwise both gcc-12-base and gcc-13-base land in required
# and the chroot install conflicts).  Other gcc-prefixed names — `gcc-mingw-w64`,
# `gcc-N-multilib`, etc. — are intentionally NOT matched and stay untouched.
_GCC_BASE_RE = re.compile(r'gcc-(\d+)(?:-base)?$')


class Cache:

    package_hashtable:  Dict[str, Dict[Version, List[Package]]]
    source_hashtable:   Dict[str, List[Source]]
    # parallel hashtable for udeb (Section: debian-installer)
    # records, indexed from dists/<suite>/main/debian-installer/binary-<arch>/
    # Packages.  Same shape as package_hashtable so the existing
    # DependencyTree class can be instantiated against it for the udeb world.
    udeb_hashtable:     Dict[str, Dict[Version, List[Package]]]

    _arch_table: DpkgArchTable

    # Operators accepted by apt_pkg.check_dep; anything else defaults to '>='.
    # Sourced from utils.VALID_CONSTRAINTS so cache + dependencytree stay
    # in lockstep (HK-01b consolidation).
    _VALID_CONSTRAINTS = utils.VALID_CONSTRAINTS

    def __init__(self, buildconfig: BuildConfig):
        """Builds the Cache by fetching Packages + Sources for every Mirror
        configured in [Mirror.*] (typically main + updates + security).
        Records are merged into a single hashtable; multiple versions for the
        same package name coexist as separate version keys, so the solver can
        pick the higher one (security/updates always > main).
        """

        # Set when config is validated
        self._config_valid: bool = False
        self.error_str = ''

        try:
            self._arch_table = DpkgArchTable.load_arch_table()
        except (OSError, RuntimeError, ValueError, KeyError) as e:
            self.error_str = f"Failed to load dpkg arch table: {e}"
            logger.error(self.error_str)
            return

        # All configured mirrors (main, updates, security) — ingested in
        # declaration order.  If snapshot pinning is enabled, every mirror's
        # baseurl is rewritten to point at snapshot.debian.org/archive/...
        # before any URL is composed downstream; with_snapshot(None) is a
        # no-op so the assignment is unconditional.
        self.cache_dir = buildconfig.dir_cache

        # Security fields are read once here so __get_files doesn't need
        # the full BuildConfig object — just keyring + work_dir + flag.
        self._security_keyring  = buildconfig.security_keyring
        self._security_work_dir = buildconfig.dir_gnupg
        self._security_disabled = buildconfig.security_disabled

        try:
            self.snapshot_ts: Optional[str] = utils.resolve_snapshot_timestamp(buildconfig)
        except (RuntimeError, ValueError) as e:
            self.error_str = f"Snapshot resolution failed: {e}"
            logger.error(self.error_str)
            return
        self.mirrors = [
            m.with_snapshot(self.snapshot_ts, baseurl=buildconfig.snapshot_baseurl)
            for m in buildconfig.mirrors
        ]

        # Fork mirror — generate fork/{Release, Packages, Sources} from
        # fork/source/*/, prepend as a file:// Mirror so the cache parses
        # it FIRST.  Fork is skipped (no Mirror registered) when
        # fork/source/ has no source trees.  See scripts/fork_mirror.py.
        # Mirror.with_snapshot() detects file:// scheme and skips snapshot
        # rewriting, so the fork Mirror added here is not snapshot-pinned.
        if fork_mirror.generate_fork_mirror(buildconfig):
            self.mirrors = fork_mirror.register_fork_mirror(self.mirrors, buildconfig)

        # Compression: tried in this order per file; first one listed in the
        # mirror's InRelease wins.  bookworm-updates / bookworm-security ship
        # only .xz; main ships all three.
        self._compression_openers: 'List[Tuple[str, Callable]]' = [
            ('.xz',  lzma.open),
            ('.gz',  gzip.open),
            ('.bz2', bz2.open),
        ]

        # Per-mirror cache file paths: {mirror_id: {'Packages': path, 'Sources': path}}
        self.mirror_cache_files: Dict[str, Dict[str, str]] = {}
        # per-mirror decompressed udeb Packages path.
        # Only set for mirrors that publish a debian-installer index (typically
        # main only — updates/security don't ship udebs).  Absence means
        # "this mirror has no udebs to contribute"; not an error.
        self.mirror_udeb_cache_files: Dict[str, str] = {}

        # InRelease info
        self.release_info = ''

        # Cache data
        self.pkg_list: List[str] = []
        self.src_list: List[str] = []

        self.required: List[str] = []
        self.important: List[str] = []
        # udeb-world equivalents.  Sparse — bookworm has
        # only 3 required udebs (base-installer, bootstrap-base, finish-install)
        # and 3 important udebs (kmod-udeb, libkmod2-udeb, libpcre3-udeb).
        self.udeb_required: List[str] = []
        self.udeb_important: List[str] = []

        self.skip_src: List[str] = []


        self.package_hashtable = defaultdict(lambda: defaultdict(list))
        # self.provides_hashtable = defaultdict(lambda: defaultdict(list))
        self.source_hashtable = defaultdict(list) # Dict[str, List[Source]]
        # parallel hashtable for udeb records.
        self.udeb_hashtable = defaultdict(lambda: defaultdict(list))

        # Fork supersede tracking — populated during the FORK mirror walk
        # (which runs first since fork is prepended to self.mirrors), then
        # consulted during upstream walks to drop same-named records and
        # warn the operator.  Per the FORK-01 plan Q1: tracks BINARY
        # Package: names only — NOT Provides:.  Virtual-package bridging
        # is the operator's responsibility (declare needed Provides: in
        # fork debian/control); a missing virtual will fail loudly at
        # dep tree time, by design.
        self._fork_pkg_names: set  = set()
        self._fork_udeb_names: set = set()
        self._fork_src_names: set  = set()

        # Collision-gate tracking — records every upstream record that
        # got dropped by the fork supersede above, so a final pass at
        # end-of-build can compare upstream version vs fork version
        # and FAIL if upstream dominates (would-be-hidden bugfix).
        # See docs/collision-gate.md and
        # memory/project_fork_collision_no_bump_mitigation.md.
        #
        # Per-name list of (mirror_id, dropped_upstream_version) tuples.
        # Lists rather than scalars because the same name can be dropped
        # from multiple upstream mirrors (main, updates, security all
        # ship pkgsel) — the gate's worst-case comparison is against
        # the HIGHEST recorded upstream version.
        self._upstream_collisions:      Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        self._upstream_udeb_collisions: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        # follow-up 2026-05-31: parallel for source records.  The
        # binary/udeb collision gate fails the build when upstream >= fork
        # (build can't ship correctly), but sources are only metadata —
        # too noisy as a build-killer.  Track them anyway so the audit
        # can warn the operator that upstream source has advanced past
        # the fork (= rebase recommended; not a hard gate).
        self._upstream_source_collisions: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

        # Download files
        if self.__get_files() < 0:
            return

        # Build Hashtable
        if not self.__build_cache(buildconfig.arch):
            return

        # Set when config is validated
        self._config_valid = True

    @property
    def is_valid(self) -> bool:
        return self._config_valid

    def __get_files(self) -> int:
        """Fetch InRelease + Packages + Sources for every configured mirror.

        Each mirror writes to its own cache files (filenames disambiguated by
        apt_pkg.uri_to_filename, which encodes the full URL).  Per-mirror
        SHA256 from the mirror's own InRelease gates the (re)download.

        Security: every mirror's InRelease is GPG-verified against the
        configured keyring before its SHA256 list is trusted.  Verification
        can be opted out via [Security] Disabled = true; doing so emits a
        single conspicuous WARN at the start of the cache build so the
        bypass is never silent.
        """
        logger.info(
            f"fetch InRelease + Packages + Sources for {len(self.mirrors)} mirror(s)"
        )
        if self._security_disabled:
            logger.warning(
                "Security: GPG verification of InRelease is DISABLED — "
                "mirror data is being trusted without signature checks. "
                "This is intended only for offline test fixtures."
            )

        for _mirror in self.mirrors:
            _base_url     = _mirror.dist_url
            # file:// mirrors (fork) ship plain Release — no GPG signature,
            # no InRelease wrapper.  Local trees are trusted by definition.
            _is_local     = _base_url.startswith('file://')
            _release_name = 'Release' if _is_local else 'InRelease'
            _release_url  = _base_url + _release_name
            _release_file = os.path.join(self.cache_dir, apt_pkg.uri_to_filename(_release_url))

            # Per-mirror control files: relative path → expected sha256 (filled below)
            _ctrl: Dict[str, str] = {
                _mirror.packages_path: '',
                _mirror.sources_path:  '',
            }

            _size, _detail = utils.download_file(_release_url, _release_file)
            if _size <= 0:
                self.error_str = (
                    f"Error downloading release file from {_release_url}: "
                    f"{_detail or 'empty response'}"
                )
                return -1
            tui.console.print(f'Downloaded {os.path.basename(_release_url)}')
            logger.info(f'Downloaded {_release_file}')

            # Verify the InRelease GPG signature *before* parsing — once
            # the signature is good the SHA256 entries inside can be
            # trusted to gate the index downloads.  Skip when explicitly
            # disabled (single WARN already emitted above) OR when the
            # mirror is local file:// (fork mirror — content we just
            # generated on this host, trusted without signature).
            if _is_local:
                logger.info(
                    f"[{_mirror.id}] file:// mirror — GPG verification skipped (local)"
                )
            if not self._security_disabled and not _is_local:
                _ok, _detail = utils.verify_inrelease(
                    _release_file,
                    self._security_keyring,
                    self._security_work_dir,
                )
                if not _ok:
                    self.error_str = (
                        f"InRelease GPG verification failed for "
                        f"[{_mirror.id}] {_release_url}: {_detail}"
                    )
                    logger.error(self.error_str)
                    return -1
                logger.info(
                    f"InRelease verified for [{_mirror.id}]: {_detail}"
                )

            try:
                with open(_release_file, 'r') as fh:
                    rel = Release(fh)
            except (FileNotFoundError, PermissionError, OSError) as e:
                self.error_str = f"Cannot read release file: {e}"
                logger.error(self.error_str)
                return -1
            except (ValueError, KeyError, AttributeError) as e:
                self.error_str = f"Error parsing release file: {e}"
                logger.error(self.error_str)
                return -1

            try:
                _rel_sha = {line['name']: line['sha256'] for line in rel['SHA256']}
            except KeyError as e:
                self.error_str = f"Missing SHA256 field in {_release_file}: {e}"
                logger.error(self.error_str)
                return -1

            _mirror_files: Dict[str, str] = {}
            for _path in _ctrl:
                _expected_uncompressed_sha = _rel_sha.get(_path, '')
                _dst = os.path.join(self.cache_dir, apt_pkg.uri_to_filename(_base_url + _path))

                if _expected_uncompressed_sha and utils.get_sha256(_dst) == _expected_uncompressed_sha:
                    logger.debug(f'Skipping download (cached) for {os.path.basename(_dst)}')
                    _mirror_files[_path.rsplit('/', 1)[-1]] = _dst
                    continue

                # Pick a compressed variant the mirror actually publishes.
                # bookworm-updates / bookworm-security ship .xz only; main
                # ships all three.
                _chosen_ext = None
                _chosen_opener = None
                for _ext, _opener in self._compression_openers:
                    if (_path + _ext) in _rel_sha:
                        _chosen_ext = _ext
                        _chosen_opener = _opener
                        break
                if _chosen_ext is None:
                    self.error_str = (f"No supported compression for {_path} in {_release_file} — "
                                      f"tried {[e for e, _ in self._compression_openers]}")
                    logger.error(self.error_str)
                    return -1

                _src_url = _base_url + _path + _chosen_ext
                _compressed_dst = _dst + _chosen_ext
                _size, _detail = utils.download_file(_src_url, _compressed_dst)
                if _size <= 0:
                    self.error_str = (
                        f"Error downloading file {_src_url}: "
                        f"{_detail or 'empty response'}"
                    )
                    return -1
                tui.console.print(f'Downloaded {os.path.basename(_path)}')
                logger.info(f'Downloaded {_src_url}')

                # Wrap in a Spinner — Python's lzma module is single-threaded
                # and CPU-bound; a 9 MB Packages.xz → 50 MB Packages takes
                # 1-3s with no other UI feedback during the cache build.
                # 1 MB copy buffer reduces Python-side overhead vs the
                # default 16 KB.
                _decompress_spinner = Spinner(f"Decompressing {os.path.basename(_compressed_dst)}")
                # `_chosen_opener is not None` is guaranteed by the loop
                # above's None-check, but assert it so mypy narrows the
                # Optional and accepts the call.
                assert _chosen_opener is not None
                try:
                    with _chosen_opener(_compressed_dst, 'rb') as f_in:
                        with open(_dst, 'wb') as f_out:
                            shutil.copyfileobj(f_in, f_out, length=1 << 20)
                except (OSError, EOFError, lzma.LZMAError) as e:
                    _decompress_spinner.done()
                    self.error_str = f"Failed to decompress {os.path.basename(_compressed_dst)}: {e}"
                    logger.error(self.error_str)
                    return -1
                _decompress_spinner.done()

                # the InRelease sha gated only the skip above — verify
                # the freshly written index against it so a corrupt/tampered
                # download fails LOUD instead of being parsed silently.
                _vok, _vdetail = self._verify_index_against_release(
                    _path=_path, _chosen_ext=_chosen_ext, _rel_sha=_rel_sha,
                    _compressed_dst=_compressed_dst, _dst=_dst)
                if not _vok:
                    for _f in (_dst, _compressed_dst):
                        try:
                            os.remove(_f)
                        except OSError:
                            pass
                    self.error_str = (
                        f"[{_mirror.id}] index integrity check FAILED for "
                        f"{_path}: {_vdetail} — corrupt or tampered download")
                    logger.error(self.error_str)
                    return -1

                _mirror_files[_path.rsplit('/', 1)[-1]] = _dst

            self.mirror_cache_files[_mirror.id] = _mirror_files

            # optional udeb index fetch.  The d-i Packages
            # index is at <component>/debian-installer/binary-<arch>/Packages
            # and is published by main only (updates/security don't ship
            # udebs).  If absent from this mirror's Release, skip silently —
            # not an error.  If present, fetch + decompress same as regular
            # Packages, but stash the path in mirror_udeb_cache_files (a
            # parallel dict) so __build_cache can route it to udeb_hashtable.
            _udeb_path = _mirror.udeb_packages_path
            _udeb_dst = self._fetch_optional_index(
                _udeb_path, _base_url, _rel_sha, _mirror,
            )
            if _udeb_dst:
                self.mirror_udeb_cache_files[_mirror.id] = _udeb_dst

            tui.console.print(f"Mirror [{_mirror.id}] {_mirror.suite}: "
                              f"{rel.get('Origin','?')} {rel.get('Codename','?')} "
                              f"{rel.get('Version','?')} {rel.get('Date','?')}", tui.COLOR_HIGHLIGHT)

        return 0

    def _verify_index_against_release(
        self, *, _path: str, _chosen_ext: str, _rel_sha: dict,
        _compressed_dst: str, _dst: str,
    ) -> 'tuple[bool, str]':
        """Verify a freshly downloaded + decompressed index against
        the GPG-verified InRelease SHA256.

        The InRelease sha gates only the re-download SKIP (the cached-file
        fast path); without this, a truncated / corrupted / tampered transfer
        is decompressed and INGESTED with no check this run (and silently
        re-fetched every subsequent run, since the corrupt bytes never match
        the skip-sha).  Prefers the UNCOMPRESSED sha (the value the skip-check
        keys on); falls back to the compressed artifact's sha when the mirror
        lists only the compressed variant.  Returns (ok, detail).
        `use_cache=False` forces a fresh hash of the just-written bytes (no
        stale .verified sidecar)."""
        _unc = _rel_sha.get(_path, '')
        if _unc:
            _got = utils.get_sha256(_dst, use_cache=False)
            if _got == _unc:
                return True, ''
            return False, (f"{os.path.basename(_dst)} sha256 {_got[:12]}… != "
                           f"InRelease {_unc[:12]}…")
        _comp = _rel_sha.get(_path + _chosen_ext, '')
        if _comp:
            _got = utils.get_sha256(_compressed_dst, use_cache=False)
            if _got == _comp:
                return True, ''
            return False, (
                f"{os.path.basename(_compressed_dst)} sha256 {_got[:12]}… != "
                f"InRelease {_comp[:12]}…")
        # Reachable only if neither form has a sha in Release — but we only
        # get here with a chosen (Release-listed) compressed variant, so this
        # is defensive.
        return True, 'no InRelease SHA256 entry to verify against'

    def _fetch_optional_index(self, _path: str, _base_url: str,
                              _rel_sha: dict, _mirror) -> Optional[str]:
        """Download + decompress an OPTIONAL Packages-style index.

        Returns the on-disk path of the decompressed file, or None if the
        path is not in this mirror's Release (i.e. the mirror doesn't
        publish this index — common for updates/security with the d-i
        index).  Errors during fetch/decompress raise the normal
        download_file / opener exceptions; the optionality is purely about
        the Release-file presence check, not about transport failures.

        Mirrors the existing required-index fetch logic in __get_files but
        scoped to a single path and with a "missing-from-Release is fine"
        early return.  Used by Phase 2's udeb index handling; structured
        as a helper so additional optional indices (translations, contrib,
        ...) can ride the same path later.
        """
        _expected_uncompressed_sha = _rel_sha.get(_path, '')
        _dst = os.path.join(self.cache_dir, apt_pkg.uri_to_filename(_base_url + _path))

        # Already on disk + sha matches: no re-download.
        if _expected_uncompressed_sha and utils.get_sha256(_dst) == _expected_uncompressed_sha:
            logger.debug(f'Skipping download (cached) for {os.path.basename(_dst)}')
            return _dst

        # Find a compressed variant that this mirror's Release lists.
        _chosen_ext = None
        _chosen_opener = None
        for _ext, _opener in self._compression_openers:
            if (_path + _ext) in _rel_sha:
                _chosen_ext = _ext
                _chosen_opener = _opener
                break

        # Neither uncompressed nor any compressed variant present in
        # Release → mirror genuinely doesn't publish this index.  Skip.
        if not _expected_uncompressed_sha and _chosen_ext is None:
            logger.info(
                f"[{_mirror.id}] no {_path} in Release — skipping (optional index)"
            )
            return None

        # If only uncompressed sha was present (rare for these indices but
        # technically allowed) the cached-file branch above would have
        # taken it; we wouldn't get here without an expected_uncompressed_sha
        # AND a missing cached file.  Fall through to download.
        if _chosen_ext is None:
            # Uncompressed shipped but not on disk and we have no compressed
            # variant to fetch — that's actually unusual but not catastrophic;
            # treat as missing.
            logger.warning(
                f"[{_mirror.id}] {_path} listed uncompressed in Release but "
                f"no compressed variant available — skipping"
            )
            return None

        _src_url = _base_url + _path + _chosen_ext
        _compressed_dst = _dst + _chosen_ext
        _size, _detail = utils.download_file(_src_url, _compressed_dst)
        if _size <= 0:
            logger.error(
                f"[{_mirror.id}] failed to download optional index "
                f"{_src_url}: {_detail or 'empty response'}"
            )
            return None
        tui.console.print(f'Downloaded {os.path.basename(_path)}')
        logger.info(f'Downloaded {_src_url}')

        _decompress_spinner = Spinner(f"Decompressing {os.path.basename(_compressed_dst)}")
        # Guaranteed by the matching loop above's None-check; assert for mypy.
        assert _chosen_opener is not None
        try:
            with _chosen_opener(_compressed_dst, 'rb') as f_in:
                with open(_dst, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out, length=1 << 20)
        except (OSError, EOFError, lzma.LZMAError) as e:
            _decompress_spinner.done()
            logger.error(
                f"[{_mirror.id}] failed to decompress "
                f"{os.path.basename(_compressed_dst)}: {e}"
            )
            return None
        _decompress_spinner.done()

        # verify against InRelease (same as the required-index path).
        _vok, _vdetail = self._verify_index_against_release(
            _path=_path, _chosen_ext=_chosen_ext, _rel_sha=_rel_sha,
            _compressed_dst=_compressed_dst, _dst=_dst)
        if not _vok:
            for _f in (_dst, _compressed_dst):
                try:
                    os.remove(_f)
                except OSError:
                    pass
            logger.error(
                f"[{_mirror.id}] optional index integrity check FAILED for "
                f"{_path}: {_vdetail} — corrupt or tampered download (skipped)")
            return None
        return _dst

    def __build_cache(self, arch: str) -> bool:
        """Build the package + source hashtables by ingesting every mirror.

        Mirrors are walked in declaration order.  Multiple versions of the
        same package coexist in the hashtable (different version keys); the
        solver picks the highest one when resolving deps.  Each parsed
        Package/Source has its `_mirror` field stamped so consumers
        (download_source, tunnel_package) can fetch from the right pool.
        """
        logger.info(
            f"build cache: arch={arch}, mirrors={[m.id for m in self.mirrors]}"
        )
        parser_spinner = Spinner("Parsing Package Files")

        for _mirror in self.mirrors:
            _mirror_files = self.mirror_cache_files.get(_mirror.id, {})
            _pkg_file = _mirror_files.get('Packages', '')
            _src_file = _mirror_files.get('Sources',  '')
            if not _pkg_file or not _src_file:
                self.error_str = f"Missing cache files for mirror {_mirror.id}"
                parser_spinner.done()   # don't leave the spinner spinning
                return False

            try:
                _pkg_records = utils.readfile(_pkg_file).split('\n\n')
                _src_records = utils.readfile(_src_file).split('\n\n')
            except OSError as e:
                self.error_str = f"Failed to read cache files for {_mirror.id}: {e}"
                logger.error(self.error_str)
                parser_spinner.done()   # don't leave the spinner spinning
                return False

            # Fork supersede: fork mirror walks first (it's prepended in
            # __init__), so by the time any upstream mirror walks here,
            # _fork_pkg_names is populated.  See FORK-01 plan Step 2 Q1.
            _is_fork = (_mirror.id == 'fork')

            progress_bar_pkg = ProgressBar(
                label=f"Indexing {_mirror.id}/Packages",
                itr_label='rec/s', maxvalue=len(_pkg_records))
            for _pkg_record in _pkg_records:
                progress_bar_pkg.step(1)
                _pkg_record = _pkg_record.strip()
                if not _pkg_record:
                    continue

                try:
                    _pkg = package.Package(_pkg_record)
                except (ValueError, KeyError, AttributeError, SystemError) as e:
                    _first_line = _pkg_record.splitlines()[0] if _pkg_record else '<empty>'
                    logger.warning(f"Skipping record ({type(e).__name__}: {e}) — {_first_line}")
                    continue

                if not _pkg.isvalid:
                    continue

                # 'all' = arch-independent package; always compatible.
                if _pkg.arch != 'all' and self._arch_table.matches_architecture(_pkg.arch, arch) is False:
                    continue

                # Fork supersede check: tracks only the
                # binary Package: name, NOT Provides:.  Operator must
                # declare needed virtuals explicitly in fork debian/control.
                if _is_fork:
                    self._fork_pkg_names.add(_pkg.package)
                elif _pkg.package in self._fork_pkg_names:
                    # Record the upstream version BEFORE dropping so the
                    # end-of-build collision gate can compare it against
                    # the fork's version.  See docs/collision-gate.md.
                    self._upstream_collisions[_pkg.package].append(
                        (_mirror.id, str(_pkg.version))
                    )
                    tui.console.print(
                        f"Local supersedes upstream {_pkg.package} v{_pkg.version} [{_mirror.id}]"
                    )
                    logger.info(
                        f"[{_mirror.id}] dropped {_pkg.package} v{_pkg.version} — superseded by fork"
                    )
                    continue

                _pkg._mirror = _mirror

                _package_name = _pkg.package
                _package_ver  = _pkg.version
                self.package_hashtable[_package_name][_package_ver].append(_pkg)

                try:
                    for _provided_name, _provided_ver in _pkg.get_provides():
                        if _provided_name != _package_name:
                            self.package_hashtable[_provided_name][_provided_ver].append(_pkg)
                except (ValueError, KeyError, AttributeError, SystemError) as e:
                    logger.warning(f"Skipping malformed provides for '{_pkg.package}': {e}")

                if _pkg.priority == 'required':
                    self.required.append(_package_name)
                if _pkg.priority == 'important':
                    self.important.append(_package_name)

            progress_bar_pkg.close()

            progress_bar_src = ProgressBar(
                label=f"Indexing {_mirror.id}/Sources",
                itr_label='rec/s', maxvalue=len(_src_records))
            for _src_record in _src_records:
                progress_bar_src.step(1)
                if _src_record.strip() == '':
                    continue

                try:
                    _src = package.Source(_src_record)
                except (ValueError, KeyError, AttributeError, SystemError) as e:
                    logger.warning(f"Skipping malformed source record: {e}")
                    continue
                if not _src.isvalid:
                    continue

                _arch_match: bool = False
                for _pkt_arch in _src.arch:
                    # Explicit guards for arch wildcards matches_architecture
                    # handles unreliably; fall back via `is not False` so None
                    # (unrecognised wildcard) is a pass.
                    if (_pkt_arch in ('all', 'any', 'linux-any', arch,
                                      f'any-{arch}', f'linux-{arch}') or
                            self._arch_table.matches_architecture(_pkt_arch, arch) is not False):
                        _arch_match = True
                        break
                if not _arch_match:
                    continue

                # Fork supersede for source records (parallels the binary
                # supersede above).  Fork sources are walked first; upstream
                # sources of the same name get dropped.  Track the dropped
                # upstream version so _audit_fork_source_drift can warn
                # when upstream has advanced past the fork (CONF-11
                # follow-up 2026-05-31).
                if _is_fork:
                    self._fork_src_names.add(_src.package)
                elif _src.package in self._fork_src_names:
                    self._upstream_source_collisions[_src.package].append(
                        (_mirror.id, str(_src.version))
                    )
                    logger.info(
                        f"[{_mirror.id}] dropped source {_src.package} v{_src.version} — superseded by fork"
                    )
                    continue

                _src._mirror = _mirror
                self.source_hashtable[_src.package].append(_src)

            progress_bar_src.close()

        # parallel udeb pass.  Mirrors that don't publish
        # a debian-installer index (typically updates/security) are skipped
        # silently — mirror_udeb_cache_files only carries entries for those
        # mirrors that did.  Same parsing logic as the regular Packages
        # pass, just routed to udeb_hashtable + udeb_required/udeb_important.
        self._ingest_udeb_indices(arch)

        # Multi-mirror ingest can record the same package under 'required'
        # or 'important' more than once (e.g. main and security both ship it);
        # dedup while preserving order for stable downstream iteration.
        self.required  = list(dict.fromkeys(self.required))
        self.important = list(dict.fromkeys(self.important))
        self.udeb_required  = list(dict.fromkeys(self.udeb_required))
        self.udeb_important = list(dict.fromkeys(self.udeb_important))

        parser_spinner.done()
        tui.console.print(
            f'Indexed {len(self.package_hashtable)} package names, '
            f'{len(self.source_hashtable)} source names, '
            f'{len(self.udeb_hashtable)} udeb names across {len(self.mirrors)} mirror(s)'
        )

        # Pick the latest gcc major present in the index and drop older majors
        # from self.required.  We scan the *whole* index (not just self.required)
        # because Bookworm's gcc-N-base is Priority: optional — it won't show up
        # in self.required even when it ships, and we still need to keep the
        # matching pair { gcc-N, gcc-N-base } together.  See _GCC_BASE_RE for the
        # exact name pattern (other gcc-prefixed packages like gcc-mingw-w64 are
        # NOT matched and pass through self.required untouched).
        _gcc_majors: Dict[int, set] = defaultdict(set)
        for _pkg_name in self.package_hashtable:
            _m = _GCC_BASE_RE.fullmatch(_pkg_name)
            if _m:
                _gcc_majors[int(_m.group(1))].add(_pkg_name)
        if _gcc_majors:
            latest_gcc = _gcc_majors[max(_gcc_majors)]
        else:
            latest_gcc = set()
        self.required = [
            _pkg for _pkg in self.required
            if not _GCC_BASE_RE.fullmatch(_pkg) or _pkg in latest_gcc
        ]
        tui.console.print(f"Selected : {latest_gcc}")
        tui.console.print(f"Required Package Count : {len(self.required)}")
        tui.console.print(f"Important Package Count : {len(self.important)}")
        tui.console.print(
            f"Udeb Required / Important : "
            f"{len(self.udeb_required)} / {len(self.udeb_important)}"
        )
        logger.info(
            f"cache build complete: pkgs={len(self.package_hashtable)}, "
            f"srcs={len(self.source_hashtable)}, "
            f"udebs={len(self.udeb_hashtable)}, "
            f"required={len(self.required)}, important={len(self.important)}"
        )

        # Collision gate (must be the LAST step in cache build — runs
        # after both mirror walks complete so it has a holistic picture
        # of every fork↔upstream version comparison).  Failures set
        # error_str and return False so the calling Cache.__init__
        # leaves _config_valid=False; downstream code already exits on
        # is_valid==False.
        # follow-up 2026-05-31: warn on upstream source drift
        # before the collision gate.  Binary/udeb collisions are fatal
        # (the gate fails the build); source collisions are advisory
        # (build still ships, but the fork is stale relative to upstream
        # source — rebase recommended).  Run first so the warning shows
        # even when the binary/udeb gate goes on to fail the build.
        self._audit_fork_source_drift()

        if not self._verify_no_fork_collisions():
            return False

        return True

    def _audit_fork_source_drift(self) -> None:
        """Warn when upstream's max version of a forked source has caught
        up to or advanced past the fork's version.

        Sources don't have an existing collision gate (unlike binaries
        and udebs, where _verify_no_fork_collisions fails the build).
        This audit fills the gap: if upstream source has moved past the
        fork's source, the fork is shipping stale code — rebase
        recommended.  Advisory only, never fails the build.
        """
        if not self._upstream_source_collisions:
            return
        _findings: 'List[Tuple[str, str, str, str]]' = []
        # (name, fork_ver, dropped_mirror, dropped_upstream_ver)
        for _name, _entries in self._upstream_source_collisions.items():
            _fork_sources = self.source_hashtable.get(_name, [])
            if not _fork_sources:
                # Shouldn't happen — fork name was recorded but no source
                # record landed.  Skip.
                continue
            _fork_ver = max((_s.version for _s in _fork_sources), key=Version)
            for _mirror_id, _up_ver in _entries:
                if Version(_up_ver) >= Version(_fork_ver):
                    _findings.append(
                        (_name, str(_fork_ver), _mirror_id, _up_ver)
                    )
        if not _findings:
            return
        _msg_lines = [
            f"  {_name}: fork {_fork} ≤ upstream {_up} [{_mirror}]"
            for _name, _fork, _mirror, _up in _findings
        ]
        _msg = (
            f"fork source drift: {len(_findings)} fork(s) at or behind "
            f"upstream — rebase recommended:\n" + "\n".join(_msg_lines) +
            "\n  (advisory only; build continues.  Binary/udeb collisions "
            "would still fail the build via the existing collision gate.)"
        )
        logger.warning(_msg)
        tui.console.print(_msg)

    def _verify_no_fork_collisions(self) -> bool:
        """End-of-build gate: for every upstream record dropped by the
        fork supersede walk, verify the fork's version actually beats
        the dropped upstream version.

        Today's supersede behavior unconditionally drops upstream when
        a fork has the same name — even if the upstream version is
        higher (which would mean apt's "fork wins" only because we
        hid the better record).  That's a silent regression vector:
        the fork no longer tracks upstream's bugfixes/security but
        ships under a higher-looking version.

        This gate fails the cache build when any dropped upstream
        version is greater-or-equal to the fork's version.  The fix is
        NOT a local version bump (would lie about the codebase — see
        memory/project_fork_collision_no_bump_mitigation.md); it's
        either rebase the fork onto upstream's new source, or rename
        the fork.  See docs/collision-gate.md.

        Returns True when no collisions, or False after setting
        self.error_str (cache becomes is_valid=False).
        """
        collisions: List[Tuple[str, str, str, str, str]] = []  # (kind, name, fork_ver, up_mirror, up_ver)

        def _check(kind: str, drops: Dict[str, List[Tuple[str, str]]],
                   hashtable: Dict[str, Dict[Version, List]]) -> None:
            for _name, _entries in drops.items():
                # Fork's version = max version present in the hashtable
                # for this name (after supersede, only fork records
                # remain for these names — by construction of the walk).
                _fork_versions = list(hashtable.get(_name, {}).keys())
                if not _fork_versions:
                    # Should not happen: name was added to _fork_pkg_names
                    # only when a fork record was parsed; either the
                    # parser dropped it post-name-add (arch filter etc.)
                    # OR the walk order assumption broke.  Skip silently
                    # — no fork record means no comparison possible.
                    continue
                _fork_ver = max(_fork_versions, key=Version)
                for _mirror_id, _up_ver in _entries:
                    if Version(_up_ver) >= Version(_fork_ver):
                        collisions.append(
                            (kind, _name, str(_fork_ver), _mirror_id, _up_ver)
                        )

        _check('deb',  self._upstream_collisions,      self.package_hashtable)
        _check('udeb', self._upstream_udeb_collisions, self.udeb_hashtable)

        if not collisions:
            return True

        # Compact diagnostic — one line per collision + pointer to
        # docs/collision-gate.md.  Multiple collisions emit as a block;
        # the doc explains the two mitigations (rebase / rename) and
        # rejects version-bump as a non-fix.
        _lines = [
            f"  {_kind}: {_name} (fork {_fork} < upstream {_up} [{_mirror_id}])"
            for _kind, _name, _fork, _mirror_id, _up in collisions
        ]
        self.error_str = (
            f"cache build aborted — {len(collisions)} fork collision(s) "
            "where upstream version >= fork version:\n"
            + "\n".join(_lines)
            + "\nSee docs/collision-gate.md for the two mitigations "
              "(rebase fork onto upstream / rename fork).  "
              "Do NOT bump the fork's local version — it lies about "
              "the codebase."
        )
        logger.error(self.error_str)
        tui.console.print(self.error_str)
        return False

    def _ingest_udeb_indices(self, arch: str) -> None:
        """Parse the per-mirror udeb Packages files and route their
        records into self.udeb_hashtable.

        Mirrors that didn't publish a d-i index (no entry in
        mirror_udeb_cache_files) are skipped silently.  Per-record parsing
        mirrors the regular Packages pass in __build_cache; the only
        differences are the destination hashtable and the udeb_required /
        udeb_important tracking.

        Extracted as a sibling method so unit tests can drive it without
        going through __build_cache's full mirror loop.
        """
        for _mirror in self.mirrors:
            _udeb_file = self.mirror_udeb_cache_files.get(_mirror.id, '')
            if not _udeb_file:
                continue

            try:
                _udeb_records = utils.readfile(_udeb_file).split('\n\n')
            except OSError as e:
                logger.error(
                    f"[{_mirror.id}] failed to read udeb index {_udeb_file}: {e}"
                )
                continue

            _is_fork = (_mirror.id == 'fork')

            progress_bar_udeb = ProgressBar(
                label=f"Indexing {_mirror.id}/d-i Packages",
                itr_label='rec/s', maxvalue=len(_udeb_records))
            for _udeb_record in _udeb_records:
                progress_bar_udeb.step(1)
                _udeb_record = _udeb_record.strip()
                if not _udeb_record:
                    continue

                try:
                    _udeb = package.Package(_udeb_record)
                except (ValueError, KeyError, AttributeError, SystemError) as e:
                    _first_line = _udeb_record.splitlines()[0] if _udeb_record else '<empty>'
                    logger.warning(
                        f"Skipping udeb record ({type(e).__name__}: {e}) — {_first_line}"
                    )
                    continue

                if not _udeb.isvalid:
                    continue

                if _udeb.arch != 'all' and self._arch_table.matches_architecture(_udeb.arch, arch) is False:
                    continue

                # Fork supersede for udebs (separate namespace from debs).
                # Per FORK-01 Step 2 Q1: track by Package: name only.
                if _is_fork:
                    self._fork_udeb_names.add(_udeb.package)
                elif _udeb.package in self._fork_udeb_names:
                    # Record upstream udeb version for end-of-build
                    # collision gate (parallels the deb-side check).
                    self._upstream_udeb_collisions[_udeb.package].append(
                        (_mirror.id, str(_udeb.version))
                    )
                    tui.console.print(
                        f"Local supersedes upstream udeb {_udeb.package} v{_udeb.version} [{_mirror.id}]"
                    )
                    logger.info(
                        f"[{_mirror.id}] dropped udeb {_udeb.package} v{_udeb.version} — superseded by fork"
                    )
                    continue

                _udeb._mirror = _mirror

                _udeb_name = _udeb.package
                _udeb_ver  = _udeb.version
                self.udeb_hashtable[_udeb_name][_udeb_ver].append(_udeb)

                # udebs typically don't carry Provides; still mirror the
                # regular pass for safety in case any do.
                try:
                    for _provided_name, _provided_ver in _udeb.get_provides():
                        if _provided_name != _udeb_name:
                            self.udeb_hashtable[_provided_name][_provided_ver].append(_udeb)
                except (ValueError, KeyError, AttributeError, SystemError) as e:
                    logger.warning(f"Skipping malformed udeb provides for '{_udeb.package}': {e}")

                if _udeb.priority == 'required':
                    self.udeb_required.append(_udeb_name)
                if _udeb.priority == 'important':
                    self.udeb_important.append(_udeb_name)

            progress_bar_udeb.close()

    def get_packages(self, package_name: str,
                     version: Optional[Version] = None, constraint: str = '') -> List[Package]:
        """Return packages matching name from the deb hashtable.  See
        :func:`_lookup_packages` for semantics."""
        return _lookup_packages(self.package_hashtable, package_name,
                                version, constraint)

    def udeb_view(self) -> 'UdebCacheView':
        """Return a thin Cache-shaped view that exposes
        udeb_hashtable as package_hashtable.  Lets the existing
        DependencyTree class be instantiated against the udeb world
        unchanged (DependencyTree only reads .get_packages,
        .package_hashtable, .skip_src, .source_hashtable from its cache).
        """
        return UdebCacheView(self)

    # ── pickle support ────────────────────────────────────────────
    # Drop the non-picklable bits and flatten lambda-factory defaultdicts.
    # Restored to original shape on __setstate__.  _arch_table (DpkgArchTable
    # from python-debian) carries C-extension state that doesn't round-trip
    # reliably and is cheap to recompute on load.  _compression_openers is
    # a list of (suffix, open-callable) — lambdas can't pickle.
    def __getstate__(self):
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
        state['_upstream_source_collisions'] = dict(self._upstream_source_collisions)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        # Restore defaultdict factories with original shapes.
        _ph: 'Dict[str, Dict[Version, List[Package]]]' = defaultdict(
            lambda: defaultdict(list))
        for k, v in self.package_hashtable.items():
            _inner: 'Dict[Version, List[Package]]' = defaultdict(list)
            _inner.update(v)
            _ph[k] = _inner
        self.package_hashtable = _ph

        _uh: 'Dict[str, Dict[Version, List[Package]]]' = defaultdict(
            lambda: defaultdict(list))
        for k, v in self.udeb_hashtable.items():
            _inner = defaultdict(list)
            _inner.update(v)
            _uh[k] = _inner
        self.udeb_hashtable = _uh

        _sh: 'Dict[str, List[Source]]' = defaultdict(list)
        _sh.update(self.source_hashtable)
        self.source_hashtable = _sh

        _uc: 'Dict[str, List[Tuple[str, str]]]' = defaultdict(list)
        _uc.update(self._upstream_collisions)
        self._upstream_collisions = _uc

        _uuc: 'Dict[str, List[Tuple[str, str]]]' = defaultdict(list)
        _uuc.update(self._upstream_udeb_collisions)
        self._upstream_udeb_collisions = _uuc

        # Same shape for the source-side dict (CONF-11 follow-up).  Restore
        # safely even on pickles produced before this attr was added.
        _usc: 'Dict[str, List[Tuple[str, str]]]' = defaultdict(list)
        _usc.update(getattr(self, '_upstream_source_collisions', {}))
        self._upstream_source_collisions = _usc

        try:
            self._arch_table = DpkgArchTable.load_arch_table()
        except (OSError, RuntimeError, ValueError, KeyError):
            # Best-effort load; downstream arch checks fall through to
            # wildcard handling when this is None.  Matches package.py.
            self._arch_table = None

        # Re-derive _compression_openers (lambdas; never serialised).
        import lzma as _lzma
        import gzip as _gzip
        import bz2 as _bz2
        self._compression_openers = [
            ('.xz',  _lzma.open),
            ('.gz',  _gzip.open),
            ('.bz2', _bz2.open),
        ]


class UdebCacheView:
    """Cache wrapper for udeb-world resolution.

    DependencyTree was written against Cache and only ever touches:
      - cache.get_packages(name, version, constraint)
      - cache.package_hashtable
      - cache.skip_src
      - cache.source_hashtable

    This view makes the udeb dimension look identical to a Cache, so the
    same DependencyTree code path resolves either world based on which
    cache it was constructed with.  source_hashtable + skip_src are
    shared (sources are universal — same source produces both .deb and
    .udeb outputs); package_hashtable is swapped to udeb_hashtable.

    Not a subclass of Cache because Cache's __init__ does heavy lifting
    (mirror parsing, downloads).  Composition keeps the view cheap.
    """

    def __init__(self, real_cache: 'Cache'):
        self._real = real_cache
        # Direct attribute hand-offs — DependencyTree accesses these as
        # plain attributes, not via getters.
        self.package_hashtable = real_cache.udeb_hashtable
        self.skip_src          = real_cache.skip_src
        self.source_hashtable  = real_cache.source_hashtable

    def get_packages(self, package_name: str,
                     version: Optional[Version] = None,
                     constraint: str = '') -> List[Package]:
        """Same semantics as Cache.get_packages but against the udeb table."""
        return _lookup_packages(self.package_hashtable, package_name,
                                version, constraint)
