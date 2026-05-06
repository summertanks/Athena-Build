import bz2, gzip
import os
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
        except Exception as e:
            self.error_str = f"Failed to load dpkg arch table: {e}"
            tui.console.error(self.error_str)
            return

        # All configured mirrors (main, updates, security) — ingested in
        # declaration order.
        self.cache_dir = buildconfig.dir_cache
        self.mirrors = buildconfig.mirrors

        # Compression
        self.supported_compression = ['.gz', '.bz2']
        self.compression = '.gz'
        if self.compression not in self.supported_compression:
            self.error_str = f"Unsupported compression '{self.compression}' specified"
            tui.console.error(self.error_str)
            return

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
        """
        for _mirror in self.mirrors:
            _base_url     = _mirror.dist_url
            _release_url  = _base_url + 'InRelease'
            _release_file = os.path.join(self.cache_dir, apt_pkg.uri_to_filename(_release_url))

            # Per-mirror control files: relative path → expected sha256 (filled below)
            _ctrl: Dict[str, str] = {
                _mirror.packages_path: '',
                _mirror.sources_path:  '',
            }

            if utils.download_file(_release_url, _release_file) <= 0:
                self.error_str = f"Error downloading release file from {_release_url}"
                return -1
            tui.console.print(f'Downloaded {_release_file}')

            try:
                with open(_release_file, 'r') as fh:
                    rel = Release(fh)
                    for _path in _ctrl:
                        _sha256 = [line['sha256'] for line in rel['SHA256'] if line['name'] == _path]
                        if len(_sha256) == 0:
                            self.error_str = f"File ({_path}) not found in {_release_file}"
                            return -1
                        if len(_sha256) > 1:
                            self.error_str = f"Multiple instances for {_path} in {_release_file}"
                            return -1
                        _ctrl[_path] = _sha256[0]
            except (FileNotFoundError, PermissionError, OSError) as e:
                self.error_str = f"Cannot read release file: {e}"
                tui.console.error(self.error_str)
                return -1
            except KeyError as e:
                self.error_str = f"Missing field in release file: {e}"
                tui.console.error(self.error_str)
                return -1
            except Exception as e:
                self.error_str = f"Error parsing release file: {e}"
                tui.console.error(self.error_str)
                return -1

            _mirror_files: Dict[str, str] = {}
            for _path, _expected_sha in _ctrl.items():
                _src_url = _base_url + _path + self.compression
                _dst     = os.path.join(self.cache_dir, apt_pkg.uri_to_filename(_base_url + _path))

                if utils.get_sha256(_dst) != _expected_sha:
                    if utils.download_file(_src_url, _dst + self.compression) <= 0:
                        self.error_str = f"Error downloading file {_src_url}"
                        return -1
                    tui.console.print(f'Downloaded {_src_url}')

                    try:
                        if self.compression == '.gz':
                            with gzip.open(_dst + self.compression, 'rb') as f_in:
                                with open(_dst, 'wb') as f_out:
                                    shutil.copyfileobj(f_in, f_out)
                        elif self.compression == '.bz2':
                            with bz2.open(_dst + self.compression, 'rb') as f_in:
                                with open(_dst, 'wb') as f_out:
                                    shutil.copyfileobj(f_in, f_out)
                        else:
                            self.error_str = f'Unsupported extension {self.compression}'
                            tui.console.error(self.error_str)
                            return -1
                    except (OSError, EOFError) as e:
                        self.error_str = f"Failed to decompress {os.path.basename(_dst)}: {e}"
                        tui.console.error(self.error_str)
                        return -1
                else:
                    tui.console.print(f'Skipping download for {os.path.basename(_dst)}')

                # Map the basename of the relative path ("Packages"/"Sources")
                # to the local file path so __build_cache can find it.
                _mirror_files[_path.rsplit('/', 1)[-1]] = _dst

            self.mirror_cache_files[_mirror.id] = _mirror_files
            tui.console.print(f"Mirror [{_mirror.id}] {_mirror.suite}: "
                              f"{rel.get('Origin','?')} {rel.get('Codename','?')} "
                              f"{rel.get('Version','?')} {rel.get('Date','?')}")

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
                tui.console.error(self.error_str)
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
                except Exception as e:
                    _first_line = _pkg_record.splitlines()[0] if _pkg_record else '<empty>'
                    tui.console.warning(f"Skipping record ({type(e).__name__}: {e}) — {_first_line}")
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
                except Exception as e:
                    tui.console.warning(f"Skipping malformed provides for '{_pkg.package}': {e}")

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
                except Exception as e:
                    tui.console.warning(f"Skipping malformed source record: {e}")
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
        
        # Special case - if gcc-10 already selected, e.g. both gcc-9-base & gcc-10-base are marked required
        # TODO: sort key x.split('-')[1] gives identical keys for gcc-10 and gcc-10-base — both yield (10,).
        # sorted(...)[-1:] keeps only one of them (last in stable sort order), silently dropping the other
        # from required even if it's a distinct needed package. Fix: find the max version number, then
        # keep ALL packages whose version number matches it, not just the last sorted element.
        # Find the latest gcc major version from all available packages (not just required),
        # since Bookworm's gcc-N-base is Priority: optional and won't appear in self.required.
        _gcc_available = [pkg for pkg in self.package_hashtable
                          if pkg.startswith('gcc-') and pkg.split('-')[1].isdigit()
                          and pkg in (f"gcc-{pkg.split('-')[1]}", f"gcc-{pkg.split('-')[1]}-base")]
        if _gcc_available:
            _max_major = max(int(pkg.split('-')[1]) for pkg in _gcc_available)
            latest_gcc = {f"gcc-{_max_major}", f"gcc-{_max_major}-base"}
        else:
            latest_gcc = set()
        self.required = [pkg for pkg in self.required if not pkg.startswith('gcc-') or pkg in latest_gcc]
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
            except Exception:
                pass
        return result
