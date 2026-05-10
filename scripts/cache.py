import bz2, gzip, lzma
import logging
import os
import re
import shutil
import apt_pkg

from debian.deb822 import Release
from debian.debian_support import DpkgArchTable, Version
from typing import List, Dict, Optional
from collections import defaultdict

# Internal
import utils, package, tui
from utils import BuildConfig
from package import Package, Source

# https://github.com/romlok/python-debian/tree/master/examples
# https://www.juliensobczak.com/inspect/2021/05/15/linux-packages-under-the-hood.html

from tui import ProgressBar, Spinner

logger = logging.getLogger('athena')


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

    _arch_table: DpkgArchTable

    # Operators accepted by apt_pkg.check_dep; anything else defaults to '>='
    _VALID_CONSTRAINTS = {'=', '>=', '<=', '>>', '<<', '>', '<'}

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
        self.mirrors = [m.with_snapshot(self.snapshot_ts) for m in buildconfig.mirrors]

        # Compression: tried in this order per file; first one listed in the
        # mirror's InRelease wins.  bookworm-updates / bookworm-security ship
        # only .xz; main ships all three.
        self._compression_openers = [
            ('.xz',  lzma.open),
            ('.gz',  gzip.open),
            ('.bz2', bz2.open),
        ]

        # Per-mirror cache file paths: {mirror_id: {'Packages': path, 'Sources': path}}
        self.mirror_cache_files: Dict[str, Dict[str, str]] = {}

        # InRelease info
        self.release_info = ''

        # Cache data
        self.pkg_list = []
        self.src_list = []
        
        self.required: List[str] = []
        self.important: List[str] = []

        self.skip_src: List[str] = []

        
        self.package_hashtable = defaultdict(lambda: defaultdict(list))
        # self.provides_hashtable = defaultdict(lambda: defaultdict(list))
        self.source_hashtable = defaultdict(list) # Dict[str, List[Source]]

        # Download files
        if self.__get_files() < 0:
            return

        # Build Hashtable
        if not self.__build_cache(buildconfig.arch):
            return

        # Set when config is validated
        self._config_valid: bool = True

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
        if self._security_disabled:
            logger.warning(
                "Security: GPG verification of InRelease is DISABLED — "
                "mirror data is being trusted without signature checks. "
                "This is intended only for offline test fixtures."
            )

        for _mirror in self.mirrors:
            _base_url     = _mirror.dist_url
            _release_url  = _base_url + 'InRelease'
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
            tui.console.print(f'Downloaded {_release_file}')

            # Verify the InRelease GPG signature *before* parsing — once
            # the signature is good the SHA256 entries inside can be
            # trusted to gate the index downloads.  Skip when explicitly
            # disabled (single WARN already emitted above).
            if not self._security_disabled:
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
                    tui.console.print(f'Skipping download for {os.path.basename(_dst)}')
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
                tui.console.print(f'Downloaded {_src_url}')

                # Wrap in a Spinner — Python's lzma module is single-threaded
                # and CPU-bound; a 9 MB Packages.xz → 50 MB Packages takes
                # 1-3s with no other UI feedback during the cache build.
                # 1 MB copy buffer reduces Python-side overhead vs the
                # default 16 KB.
                _decompress_spinner = Spinner(f"Decompressing {os.path.basename(_compressed_dst)}")
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

                _mirror_files[_path.rsplit('/', 1)[-1]] = _dst

            self.mirror_cache_files[_mirror.id] = _mirror_files
            tui.console.print(f"Mirror [{_mirror.id}] {_mirror.suite}: "
                              f"{rel.get('Origin','?')} {rel.get('Codename','?')} "
                              f"{rel.get('Version','?')} {rel.get('Date','?')}", tui.COLOR_HIGHLIGHT)

        return 0

    def __build_cache(self, arch: str) -> bool:
        """Build the package + source hashtables by ingesting every mirror.

        Mirrors are walked in declaration order.  Multiple versions of the
        same package coexist in the hashtable (different version keys); the
        solver picks the highest one when resolving deps.  Each parsed
        Package/Source has its `_mirror` field stamped so consumers
        (download_source, tunnel_package) can fetch from the right pool.
        """
        parser_spinner = Spinner("Parsing Package Files")

        for _mirror in self.mirrors:
            _mirror_files = self.mirror_cache_files.get(_mirror.id, {})
            _pkg_file = _mirror_files.get('Packages', '')
            _src_file = _mirror_files.get('Sources',  '')
            if not _pkg_file or not _src_file:
                self.error_str = f"Missing cache files for mirror {_mirror.id}"
                return False

            try:
                _pkg_records = utils.readfile(_pkg_file).split('\n\n')
                _src_records = utils.readfile(_src_file).split('\n\n')
            except OSError as e:
                self.error_str = f"Failed to read cache files for {_mirror.id}: {e}"
                logger.error(self.error_str)
                return False

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

                _src._mirror = _mirror
                self.source_hashtable[_src.package].append(_src)

            progress_bar_src.close()

        # Multi-mirror ingest can record the same package under 'required'
        # or 'important' more than once (e.g. main and security both ship it);
        # dedup while preserving order for stable downstream iteration.
        self.required  = list(dict.fromkeys(self.required))
        self.important = list(dict.fromkeys(self.important))

        parser_spinner.done()
        tui.console.print(
            f'Indexed {len(self.package_hashtable)} package names, '
            f'{len(self.source_hashtable)} source names across {len(self.mirrors)} mirror(s)'
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
        
        return True

    def get_packages(self, package_name: str,
                     version: Optional[Version] = None, constraint: str = '') -> List[Package]:
        """Return packages matching name.

        If version is omitted, all versions are returned.
        If version is given without constraint, constraint defaults to '>='.
        If both are given, only packages whose version satisfies
        apt_pkg.check_dep(pkg_ver, constraint, version) are returned.
        """
        # Use .get() instead of direct bracket access: package_hashtable is a defaultdict, so
        # bracket access on a missing key silently creates an empty entry — every unresolved
        # package name would permanently bloat the table and skew len() counts.
        if version is None:
            result: List[Package] = []
            for _pkgs in self.package_hashtable.get(package_name, {}).values():
                result.extend(_pkgs)
            return result

        _constraint = constraint if constraint in self._VALID_CONSTRAINTS else '>='
        result = []
        for _pkg_version, _pkgs in self.package_hashtable.get(package_name, {}).items():
            try:
                if apt_pkg.check_dep(str(_pkg_version), _constraint, str(version)):
                    result.extend(_pkgs)
            except (SystemError, ValueError):
                # apt_pkg.Error inherits from SystemError; ValueError covers
                # str() of a Version that can't be coerced.  Silently skip
                # constraint failures — the candidate just doesn't qualify.
                pass
        return result
