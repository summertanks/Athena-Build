import hashlib
import os
import pathlib
import re
import configparser
import argparse

import requests
import tui
from tui import Prompt, Spinner, ProgressBar
from typing import List, Optional, Any


def _strip_quotes(s: str) -> str:
    """Remove a single matching pair of surrounding quotes from a config value.

    configparser does not unquote values: `KEY = "value"` reads back as the
    literal 6-char string `"value"`. Apply to string values where the operator
    may have wrapped them in quotes by INI convention from other tools.
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


class Mirror:
    """A single archive source (e.g. bookworm main, bookworm-security main).

    A mirror is composed from defaults + per-mirror overrides at parse time:
        url   = <baseurl>/<baseid>          # e.g. http://deb.debian.org/debian
        suite = <release><suffix>           # e.g. bookworm, bookworm-security
    The split exists so rebasing to a different release only requires changing
    one [Base].RELEASE field, not every [Mirror.*] section.
    """

    def __init__(self, mirror_id: str, baseurl: str, baseid: str,
                 release: str, suffix: str, component: str, arch: str):
        self.id        = mirror_id
        self.baseurl   = baseurl.rstrip('/')
        self.baseid    = baseid.strip('/')
        self.release   = release
        self.suffix    = suffix or ''
        self.component = component
        self.arch      = arch

    @property
    def suite(self) -> str:
        return f'{self.release}{self.suffix}'

    @property
    def url(self) -> str:
        return f'{self.baseurl}/{self.baseid}'

    @property
    def dist_url(self) -> str:
        # 'http://deb.debian.org/debian/dists/bookworm/'
        return f'{self.url}/dists/{self.suite}/'

    @property
    def packages_path(self) -> str:
        return f'{self.component}/binary-{self.arch}/Packages'

    @property
    def sources_path(self) -> str:
        return f'{self.component}/source/Sources'

    def with_snapshot(self, ts):
        """Return a copy of this Mirror rewritten to snapshot.debian.org.

        snapshot.debian.org preserves the full archive layout under
            /archive/<baseid>/<TS>/dists/<suite>/...
        so we only need to rewrite baseurl + baseid; everything else
        (suite, component, packages_path, sources_path) is unchanged.
        Passing ts=None returns self — call sites can use this method
        unconditionally.
        """
        if ts is None:
            return self
        return Mirror(
            mirror_id = self.id,
            baseurl   = 'http://snapshot.debian.org/archive',
            baseid    = f'{self.baseid}/{ts}',
            release   = self.release,
            suffix    = self.suffix,
            component = self.component,
            arch      = self.arch,
        )

    def __repr__(self) -> str:
        return f"Mirror({self.id}: {self.url} {self.suite} {self.component})"


# Module-level memo so resolve_snapshot_timestamp() doesn't repeat the
# network query within one process.  Keyed by (state_file_path, config_ts)
# so different BuildConfig instances under different cache dirs don't
# collide in tests.
_SNAPSHOT_TS_CACHE: dict = {}

# Format check: Debian snapshot timestamps are YYYYMMDDTHHMMSSZ (15 chars).
_SNAPSHOT_TS_RE = re.compile(r'^\d{8}T\d{6}Z$')

# Module-level memo for GPG verifier instances.  Keyed by (keyring_path,
# work_dir) so re-using the same keyring across multiple verify_inrelease()
# calls in one Cache build avoids re-importing the (large) Debian keyring
# per mirror.
_GPG_VERIFIER_CACHE: dict = {}


def verify_inrelease(signed_path: str, keyring_path: str, work_dir: str) -> tuple:
    """Verify the inline GPG signature on a Debian InRelease file.

    InRelease is an inline-signed (cleartext-signed) document that lists
    SHA256 sums for the index files (Packages, Sources) it covers.  Once
    this signature is verified against a trusted keyring, those SHA256
    sums extend trust to the index files via the existing per-file hash
    check in Cache.__get_files — same chain apt uses.

    Args:
        signed_path:  Path to the downloaded InRelease file.
        keyring_path: Path to a keyring file (legacy .gpg / .kbx keybox /
                      OpenPGP cert dump — gpg --import accepts all three).
        work_dir:     Existing gnupg homedir, mode 0700, writable.  Created
                      by build-system.sh / BuildConfig; this function does
                      not create or chmod it (single-source-of-truth for
                      project directory layout).

    Returns:
        (ok, detail) — ok is True when the signature is mathematically
        valid and the signing key is in the keyring; detail is a short
        human-readable status string suitable for both console and log.
    """
    import gnupg

    if not os.path.isfile(signed_path):
        return False, f"signed file missing: {signed_path}"
    if not os.path.isfile(keyring_path):
        return False, f"keyring missing: {keyring_path}"
    if not os.access(keyring_path, os.R_OK):
        return False, f"keyring unreadable: {keyring_path}"
    if not os.path.isdir(work_dir):
        return False, f"gnupg work_dir missing: {work_dir}"

    # Per-build cache: one GPG instance per (keyring, work_dir) pair.
    # Re-importing the full Debian keyring (1k+ keys) for every mirror
    # would be wasteful — a single import_keys is plenty.
    cache_key = (os.path.realpath(keyring_path), os.path.realpath(work_dir))
    gpg = _GPG_VERIFIER_CACHE.get(cache_key)
    if gpg is None:
        try:
            gpg = gnupg.GPG(gnupghome=work_dir)
        except (OSError, ValueError) as e:
            return False, f"gnupg init failed: {e}"

        try:
            with open(keyring_path, 'rb') as fh:
                import_result = gpg.import_keys(fh.read())
        except OSError as e:
            return False, f"keyring read failed: {e}"

        # import_keys returns an ImportResult; .count is the number of
        # keys processed.  Zero means the file parsed as 0 keys —
        # malformed or empty file — and verification cannot succeed.
        if not getattr(import_result, 'count', 0):
            return False, (
                f"no keys imported from {keyring_path} "
                f"(stderr: {getattr(import_result, 'stderr', '')[:120]})"
            )

        _GPG_VERIFIER_CACHE[cache_key] = gpg

    try:
        with open(signed_path, 'rb') as fh:
            v = gpg.verify_file(fh)
    except OSError as e:
        return False, f"signed file read failed: {e}"

    if not v.valid:
        # python-gnupg surfaces the gpg --status-fd reason in v.status
        # when available; fall back to the human-readable v.stderr line.
        _why = getattr(v, 'status', None) or (
            (v.stderr or '').splitlines()[-1] if getattr(v, 'stderr', '') else ''
        ) or 'signature invalid'
        return False, str(_why)[:200]

    _ident = getattr(v, 'username', '') or getattr(v, 'fingerprint', '') or 'unknown'
    return True, f"signed by {_ident}"


def _query_snapshot_latest() -> str:
    """Fetch the latest snapshot timestamp covering both `debian` and
    `debian-security` archives on snapshot.debian.org.

    Returns min(latest_debian, latest_debian-security) so the chosen TS is
    valid for both archive trees (snapshot.d.o resolves a missing exact TS
    to the nearest snapshot ≤ TS via 302, but we want the symmetric guarantee
    that both archives have a snapshot at or before the timestamp).

    Endpoint:  GET https://snapshot.debian.org/mr/timestamp/
    Response:  {"result": {"debian": [...sorted ts list...], "debian-security": [...]}}
    The list is sorted lexicographically which matches chronological order
    for the YYYYMMDDTHHMMSSZ format, so `[-1]` is the latest.
    """
    URL = 'https://snapshot.debian.org/mr/timestamp/'
    try:
        resp = requests.get(URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"Failed to query {URL}: {e}") from e

    try:
        result = data['result']
        debian_latest   = result['debian'][-1]
        security_latest = result['debian-security'][-1]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(
            f"Unexpected response shape from {URL}: {e}; top-level keys={list(data.keys())}"
        ) from e

    for _label, _ts in (('debian', debian_latest), ('debian-security', security_latest)):
        if not _SNAPSHOT_TS_RE.match(_ts):
            raise RuntimeError(
                f"snapshot.d.o returned malformed timestamp for {_label}: {_ts!r}"
            )

    # Lexical min == chronological min for YYYYMMDDTHHMMSSZ
    chosen = min(debian_latest, security_latest)
    tui.console.print(
        f"snapshot.d.o latest: debian={debian_latest}, "
        f"debian-security={security_latest}, picking {chosen}"
    )
    return chosen


def _validate_snapshot_timestamp(ts: str, mirrors: 'List[Mirror]') -> bool:
    """HEAD-validate that every mirror has an InRelease available at the
    given snapshot timestamp.  Catches typos and timestamps that predate a
    given suite (e.g. picking 20180101 when bookworm didn't exist yet).

    snapshot.debian.org responds 302 → /file/<sha> for any timestamp it can
    serve (it nearest-≤ resolves arbitrary timestamps), so we follow the
    redirect and check the final 200.  A 404 at the redirect target means
    the file doesn't exist for that timestamp and the validation fails.
    """
    for m in mirrors:
        snap = m.with_snapshot(ts)
        url  = snap.dist_url + 'InRelease'
        try:
            resp = requests.head(url, timeout=15, allow_redirects=True)
        except Exception as e:
            tui.console.error(f"snapshot validate: HEAD {url} failed: {e}")
            return False
        if resp.status_code != 200:
            tui.console.error(
                f"snapshot validate: {url} returned HTTP {resp.status_code} — "
                f"timestamp {ts} does not cover suite {snap.suite} on archive {m.baseid}"
            )
            return False
        tui.console.print(f"snapshot validate: OK {url}")
    return True


def resolve_snapshot_timestamp(config: 'BuildConfig') -> Optional[str]:
    """Resolve the effective snapshot timestamp for this build.

    Returns None when snapshot pinning is disabled — call sites can pass
    the result straight to Mirror.with_snapshot() unconditionally.

    Resolution rules:
      - snapshot_enabled is False           → None
      - snapshot_timestamp_config = 'latest':
          * if cache/snapshot.timestamp exists and looks valid, use it
            (reproducible across runs; delete the file to advance)
          * otherwise call _query_snapshot_latest() and persist
      - snapshot_timestamp_config is explicit:
          * format-check, validate via HEAD, return as-is (do NOT persist —
            the explicit config is already the source of truth)

    Memoised per (state_file, config_ts) so this is safe to call from
    multiple sites in one run without re-resolving.
    """
    if not config.snapshot_enabled:
        return None

    state_file = os.path.join(config.dir_cache, 'snapshot.timestamp')
    cfg_ts     = config.snapshot_timestamp_config
    cache_key  = (state_file, cfg_ts)
    if cache_key in _SNAPSHOT_TS_CACHE:
        return _SNAPSHOT_TS_CACHE[cache_key]

    if cfg_ts == 'latest':
        # Prefer persisted value for reproducibility
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r') as fh:
                    persisted = fh.read().strip()
                if _SNAPSHOT_TS_RE.match(persisted):
                    tui.console.print(f"Snapshot pin: using persisted {persisted} from {state_file}")
                    _SNAPSHOT_TS_CACHE[cache_key] = persisted
                    return persisted
                tui.console.warning(
                    f"Snapshot state file {state_file} contains invalid timestamp "
                    f"{persisted!r}; re-resolving"
                )
            except OSError as e:
                tui.console.warning(f"Cannot read {state_file}: {e}; re-resolving")

        # Cold path: ask snapshot.debian.org
        ts = _query_snapshot_latest()
        try:
            with open(state_file, 'w') as fh:
                fh.write(ts + '\n')
            tui.console.print(f"Snapshot pin: resolved 'latest' → {ts}, persisted to {state_file}")
        except OSError as e:
            tui.console.warning(
                f"Cannot persist snapshot timestamp to {state_file}: {e}; "
                f"build will re-resolve next run"
            )
        _SNAPSHOT_TS_CACHE[cache_key] = ts
        return ts

    # Explicit timestamp from config
    if not _SNAPSHOT_TS_RE.match(cfg_ts):
        raise ValueError(
            f"Snapshot.Timestamp = {cfg_ts!r} is not a valid Debian snapshot "
            f"timestamp (expected YYYYMMDDTHHMMSSZ, e.g. 20260506T120451Z, or 'latest')"
        )
    if not _validate_snapshot_timestamp(cfg_ts, config.mirrors):
        raise ValueError(
            f"Snapshot.Timestamp = {cfg_ts!r} does not cover all configured "
            f"mirrors on snapshot.debian.org (see prior log lines)"
        )
    tui.console.print(f"Snapshot pin: explicit {cfg_ts} validated")
    _SNAPSHOT_TS_CACHE[cache_key] = cfg_ts
    return cfg_ts


class BuildConfig:

    arch: str
    mirrors: 'List[Mirror]'
    baseid: str
    release: str
    baseversion: str
    snapshot_enabled: bool
    snapshot_timestamp_config: str
    build_codename: str
    build_version: str
    container_release: str
    docker_server: str

    skip_build_test: list[str]
    tunnel_packages: list[str]
    max_parallel_builds: int

    security_keyring: str
    security_disabled: bool

    error_str: str
    config_path: str

    dir_working: str
    dir_pkglist: str
    dir_download: str
    dir_log: str
    dir_cache: str
    dir_temp: str
    dir_source: str
    dir_repo: str
    dir_config: str
    dir_patch: str
    dir_gnupg: str
    dir_image: str
    dir_chroot: str
    
    dir_patch_source: str
    dir_patch_preinstall: str
    dir_patch_postinstall: str
    dir_patch_empty: str

    _config_valid: bool
    
    def __init__(self):

        # Set when config is validated
        self._config_valid: bool = False

        # Setting up config parsers
        config_parser = configparser.ConfigParser()
        
        self.error_str = ''

        try:
            # let defaults be relative to current working directory
            working_dir = os.path.abspath(os.path.curdir)
            config_path = os.path.join(working_dir, 'config/build.conf')
            pkglist_path = os.path.join(working_dir, 'config/pkg.list')

            parser = argparse.ArgumentParser(description='Dependency Parser - Athena Linux')
            parser.add_argument('--working-dir', type=str, help='Specify Working directory', required=False, default=working_dir)
            parser.add_argument('--config-file', type=str, help='Specify Configs File', required=False, default=config_path)
            parser.add_argument('--pkg-list', type=str, help='Specify Required Pkg File', required=False, default=pkglist_path)
            args = parser.parse_args()

            # if paths are specified, they are absolute
            self.working_dir = os.path.abspath(args.working_dir)
            self.config_path = os.path.abspath(args.config_file)
            self.pkglist_path = os.path.abspath(args.pkg_list)

            if not os.access(self.config_path, os.R_OK):
                raise PermissionError(f'Config file is not readable: {self.config_path}')

        except (argparse.ArgumentError, OSError, SystemExit) as e:
            self.error_str = f"Failed to parse arguments: {e}"
            return

        # read config file
        try:
            config_parser.read(self.config_path)
            self.arch = config_parser.get('Build', 'ARCH')

            # [Base] defaults — per-mirror sections may override BASEURL/BASEID
            _default_baseurl = config_parser.get('Base', 'BASEURL')
            _default_baseid  = config_parser.get('Base', 'BASEID')
            self.release     = config_parser.get('Base', 'RELEASE')
            self.baseid      = _default_baseid
            self.baseversion = config_parser.get('Base', 'BASEVERSION')

            self.mirrors = []
            for _section in config_parser.sections():
                if not _section.startswith('Mirror.'):
                    continue
                _id = _section.split('.', 1)[1]
                self.mirrors.append(Mirror(
                    mirror_id = _id,
                    baseurl   = config_parser.get(_section, 'BASEURL', fallback=_default_baseurl),
                    baseid    = config_parser.get(_section, 'BASEID',  fallback=_default_baseid),
                    release   = self.release,
                    suffix    = config_parser.get(_section, 'Suffix',    fallback=''),
                    component = config_parser.get(_section, 'Component', fallback='main'),
                    arch      = self.arch,
                ))
            if not self.mirrors:
                self.error_str = "No [Mirror.*] sections in config"
                return

            # Snapshot pinning — opt-in.  Default off keeps the existing
            # live-mirror behaviour for users who haven't migrated yet.
            self.snapshot_enabled = config_parser.getboolean('Snapshot', 'Enabled', fallback=False)
            self.snapshot_timestamp_config = config_parser.get('Snapshot', 'Timestamp', fallback='latest').strip()
            self.build_codename = _strip_quotes(config_parser.get('Build', 'CODENAME'))
            self.build_version  = _strip_quotes(config_parser.get('Build', 'VERSION'))

            self.container_release = config_parser.get('Build', 'CONTAINER_RELEASE', fallback='bookworm')
            self.docker_server = config_parser.get('Build', 'DOCKER_SERVER', fallback='')
            self.skip_build_test = config_parser.get('Source', 'SkipTest').split(', ')
            _tunneled_raw = config_parser.get('Source', 'Tunneled', fallback='')
            self.tunnel_packages: list[str] = [p.strip() for p in _tunneled_raw.split(',') if p.strip()]
            _profiles_raw = config_parser.get('Source', 'BuildProfiles', fallback='')
            self.build_profiles: frozenset[str] = frozenset(
                p.strip() for p in _profiles_raw.split(',') if p.strip()
            )
            self.max_parallel_builds = config_parser.getint('Build', 'MaxParallelBuilds', fallback=4)

            # Mirror InRelease GPG verification.  Default: enabled,
            # using the host-provided debian-archive-keyring.  Disabled
            # = true is for offline test fixtures and dev sandboxes only;
            # Cache.__get_files emits a per-build WARN when bypassed.
            self.security_keyring = config_parser.get(
                'Security', 'Keyring',
                fallback='/usr/share/keyrings/debian-archive-keyring.gpg'
            ).strip()
            self.security_disabled = config_parser.getboolean(
                'Security', 'Disabled', fallback=False
            )
            if not self.security_disabled:
                if not os.path.isfile(self.security_keyring):
                    self.error_str = (
                        f"Security.Keyring not found: {self.security_keyring} "
                        f"(install debian-archive-keyring, point Keyring "
                        f"at a different file, or set Disabled=true)"
                    )
                    return
                if not os.access(self.security_keyring, os.R_OK):
                    self.error_str = (
                        f"Security.Keyring not readable: {self.security_keyring}"
                    )
                    return

            # NOTE: The directories are relative to the working directory
            self.dir_download = os.path.join(self.working_dir, config_parser.get('Directories', 'Download'))
            self.dir_log = os.path.join(self.working_dir, config_parser.get('Directories', 'Log'))
            self.dir_cache = os.path.join(self.working_dir, config_parser.get('Directories', 'Cache'))
            self.dir_temp = os.path.join(self.working_dir, config_parser.get('Directories', 'Temp'))
            self.dir_source = os.path.join(self.working_dir, config_parser.get('Directories', 'Source'))
            self.dir_repo = os.path.join(self.working_dir, config_parser.get('Directories', 'Repo'))
            self.dir_config = os.path.join(self.working_dir, config_parser.get('Directories', 'Config'))
            self.dir_image = os.path.join(self.working_dir, config_parser.get('Directories', 'Image'))
            self.dir_chroot = os.path.join(self.working_dir, config_parser.get('Directories', 'Chroot'))
            
            self.dir_patch = os.path.join(self.working_dir, config_parser.get('Directories', 'Patch'))
            self.dir_patch_source = os.path.join(self.dir_patch, 'source')
            self.dir_patch_preinstall = os.path.join(self.dir_patch, 'pre-install')
            self.dir_patch_postinstall = os.path.join(self.dir_patch, 'post-install')
            self.dir_patch_empty = os.path.join(self.dir_patch, 'empty')

            # Isolated gnupg homedir for InRelease verification.  The
            # build-system.sh bootstrap creates this with mode 0700;
            # mirror that here so a Python-only invocation (e.g. tests)
            # gets the same layout without depending on the bash script.
            self.dir_gnupg = os.path.join(self.working_dir, config_parser.get('Directories', 'Gnupg'))

        except (configparser.Error, OSError) as e:
            self.error_str = str(e)
            return
        
        try:
            if not os.access(self.working_dir, os.W_OK):
                raise PermissionError(f'Working directory is not writable: {self.working_dir}')

            pathlib.Path(self.dir_download).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_log).mkdir(parents=True, exist_ok=True)
            
            pathlib.Path(self.dir_cache).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_temp).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_repo).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_patch).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_empty).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_preinstall).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_postinstall).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_image).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_chroot).mkdir(parents=True, exist_ok=True)

            # gpg refuses any homedir whose mode is broader than 0700,
            # so create with mode 0700 directly.  os.makedirs respects
            # the umask, so we follow with an explicit chmod to be safe
            # on hosts whose umask leaves the dir 0755.
            pathlib.Path(self.dir_gnupg).mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(self.dir_gnupg, 0o700)

            for _dir in (
                self.dir_download, self.dir_log, self.dir_cache, self.dir_temp,
                self.dir_source, self.dir_repo, self.dir_patch, self.dir_patch_empty,
                self.dir_patch_source, self.dir_patch_preinstall, self.dir_patch_postinstall,
                self.dir_image, self.dir_chroot, self.dir_gnupg,
            ):
                if not os.access(_dir, os.W_OK):
                    raise PermissionError(f'Build directory is not writable: {_dir}')

            pathlib.Path(os.path.join(self.dir_log, 'build')).mkdir(parents=True, exist_ok=True)

        except OSError as e:
            self.error_str = f"Failed to prepare build directories: {e}"
            return
        
        self._config_valid = True
    
    @property
    def is_valid(self) -> bool:
        """
        Returns:
            bool: True if config is valid, False otherwise
        """
        return self._config_valid
    
    def error(self) -> str:
        """
        Returns:
            str: Error string if config is invalid, empty string otherwise
        """
        return self.error_str
    
def download_file(url: str, filename: str) -> tuple:
    """Downloads file and updates progressbar in incremental manner.

    Args:
        url: URL to download from.
        filename: Local path to write to; location must be writable.

    Returns:
        (size, detail) — size is bytes on success or -1 on failure;
        detail is '' on success or a short human-readable cause on
        failure (e.g. 'HTTP 404 Not Found', 'connection timeout',
        'OS write error: ...').  Callers should surface detail in
        their own error_str so the operator sees the actual reason
        rather than a generic "download failed".
    """
    from urllib.parse import urlsplit
    from requests import Timeout, TooManyRedirects, HTTPError, RequestException

    try:
        name_strip: str = urlsplit(url).path.split('/')[-1].ljust(15, ' ')
        head = requests.head(url, timeout=10)
        file_size = int(head.headers.get('content-length', 0))

        with requests.get(url, stream=True, timeout=10) as response:
            response.raise_for_status()

            progress_bar = tui.ProgressBar(label=name_strip, itr_label='B/s', maxvalue=file_size)

            with open(filename, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        progress_bar.step(len(chunk))

            progress_bar.close()
            return file_size, ''

    except HTTPError as e:
        # raise_for_status path — preserve the HTTP status line so the
        # operator sees "HTTP 404 Not Found" rather than the generic
        # "download failed" the legacy single-int return produced.
        _resp = getattr(e, 'response', None)
        if _resp is not None:
            _detail = f"HTTP {_resp.status_code} {_resp.reason}"
        else:
            _detail = f"HTTPError: {e}"
        tui.console.print(f"ERROR: {_detail} for {url}")
        tui.console.error(f"download_file({url}): {_detail}")
        return -1, _detail
    except (ConnectionError, Timeout, TooManyRedirects, RequestException) as e:
        _detail = f"{type(e).__name__}: {e}"
        tui.console.print(f"ERROR: download failed for {url}")
        tui.console.error(f"download_file({url}): {_detail}")
        return -1, _detail
    except OSError as e:
        _detail = f"OS write error: {e}"
        tui.console.print(f"ERROR: cannot write to {filename}")
        tui.console.error(f"download_file write {filename}: {e}")
        return -1, _detail
    except ValueError as e:
        _detail = f"malformed response: {e}"
        tui.console.print(f"ERROR: malformed response from {url}")
        tui.console.error(f"download_file parse {url}: {e}")
        return -1, _detail
    except Exception as e:
        _detail = f"{type(e).__name__}: {e}"
        tui.console.print(f"ERROR: unexpected failure downloading {url}")
        tui.console.error(f"download_file({url}): {_detail}")
        return -1, _detail


def download_source(dependency_tree, dir_download):
    from urllib.parse import urljoin
    from requests import Timeout, TooManyRedirects, HTTPError, RequestException

    _downloaded_size = 0
    _download_size = dependency_tree.download_size

    # Per-file: {filename: file_meta}, parallel {filename: Mirror}.  Each
    # Source object carries the mirror it was parsed from so we hit the
    # correct pool (sources in bookworm-security live under a different
    # baseid than main).
    _file_list: dict = {}
    _file_mirror: dict = {}
    for _pkg_name in dependency_tree.selected_srcs:
        _src = dependency_tree.selected_srcs[_pkg_name]
        if _src._mirror is None:
            tui.console.error(f"download_source: source {_pkg_name} has no _mirror — cache ingest bug")
            continue
        _file_list.update(_src.files)
        for _fname in _src.files:
            _file_mirror[_fname] = _src._mirror

    _index = 1
    _skipped = 0
    _total = len(_file_list)

    try:
        progress_bar = ProgressBar(label='Downloading', itr_label='B/s', maxvalue=max(1, _download_size))
    except Exception as e:
        progress_bar = None
        tui.console.print(f"WARNING: progress bar unavailable, continuing without it")
        tui.console.error(f"download_source ProgressBar: {type(e).__name__}: {e}")

    for _file in _file_list:
        if progress_bar is not None:
            progress_bar.label(f'({_index}/{_total}) {_file[:20]}')

        _mirror = _file_mirror[_file]
        _base_url = _mirror.url + '/'
        _url = urljoin(_base_url, _file_list[_file]['path'])
        _sha256 = _file_list[_file]['sha256']
        _expected_size = int(_file_list[_file]['size'])
        _download_path = os.path.join(dir_download, _file)

        if get_sha256(_download_path) != _sha256:
            # Use the size from the InRelease-verified Sources index instead
            # of a HEAD probe — saves one round-trip per file and removes
            # the prior bug where a HEAD that returned 0 still produced
            # `_downloaded_size += 0` while the GET silently 404ed.
            try:
                with requests.get(_url, stream=True, timeout=30) as response:
                    # raise_for_status surfaces 4xx/5xx as HTTPError so the
                    # existing requests-exception handler logs a clear
                    # "HTTP <status>" message instead of the prior cryptic
                    # downstream "Hash mismatch" the user saw on 404s.
                    response.raise_for_status()
                    with open(_download_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=1024):
                            if chunk:
                                f.write(chunk)
                                if progress_bar is not None:
                                    progress_bar.step(len(chunk))

            except (ConnectionError, Timeout, TooManyRedirects, HTTPError, RequestException) as e:
                tui.console.print(f"ERROR: HTTP failure for {_url}")
                tui.console.error(f"download_source({_url}): {e}")
                continue
            except OSError as e:
                tui.console.print(f"ERROR: cannot write {_download_path}")
                tui.console.error(f"download_source write {_download_path}: {e}")
                continue
            except ValueError as e:
                tui.console.print(f"ERROR: malformed response for {_url}")
                tui.console.error(f"download_source parse {_url}: {e}")
                continue
            except Exception as e:
                tui.console.print(f"ERROR: unexpected failure for {_url}")
                tui.console.error(f"download_source({_url}): {type(e).__name__}: {e}")
                continue

            # Validate what landed on disk *before* the sha256 check, so a
            # short/truncated 200 surfaces as a precise byte-count error
            # rather than the more cryptic "hash mismatch".  Skipping the
            # check when expected size is 0 lets older Sources stanzas
            # without size metadata still pass through to the sha256 gate.
            try:
                _on_disk = os.path.getsize(_download_path)
            except OSError as e:
                tui.console.print(f"ERROR: cannot stat downloaded {_download_path}")
                tui.console.error(f"download_source stat {_download_path}: {e}")
                continue

            if _expected_size > 0 and _on_disk != _expected_size:
                tui.console.print(
                    f"ERROR: short download for {_file} — "
                    f"got {_on_disk} bytes, expected {_expected_size}"
                )
                tui.console.error(
                    f"short_download {_url}: {_on_disk}/{_expected_size} bytes"
                )
                continue

            if get_sha256(_download_path) != _sha256:
                tui.console.print(f"ERROR: Hash mismatch for {_file} — download may be corrupt")
                tui.console.error(f"sha256 mismatch: {_download_path} expected {_sha256}")
                continue

            _downloaded_size += _on_disk

        else:
            _skipped += 1
            if progress_bar is not None:
                progress_bar.step(_expected_size)
            _downloaded_size += _expected_size

        _index += 1

    if progress_bar is not None:
        progress_bar.close(persist=True)

    tui.console.print(f"Downloading {_total - _skipped} files, Skipped {_skipped} files")
    return _downloaded_size


def search(re_string: str, base_string: str) -> str:
    """
    Internal function to simplify re.search() execution
    Args:
        re_string: the regex to execute
        base_string: the content on which it is to be executed

    Returns:
        str: Match group, empty string on no match
    """
    _match = re.search(re_string, base_string)
    if _match is not None:
        return _match.group(1)
    return ''


def get_md5(filepath: str) -> str:
    """
    Internal function to calculate the md5 of given file
    Args:
        filepath: The file to calculate md5 hash of

    Returns:
        str: md5
    """
    if not os.path.isfile(filepath):
        return ''
    try:
        h = hashlib.md5()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        tui.console.warning(f"get_md5: cannot read {filepath}: {e}")
        return ''


def get_sha256(filepath: str) -> str:
    """
    Calculate the SHA256 hash of a file.
    Args:
        filepath: The file to hash
    Returns:
        str: hex digest, or empty string if file does not exist
    """
    if not os.path.isfile(filepath):
        return ''
    try:
        with open(filepath, 'rb') as f:
            return hashlib.file_digest(f, 'sha256').hexdigest()
    except OSError as e:
        tui.console.warning(f"get_sha256: cannot read {filepath}: {e}")
        return ''


def readfile(filename: str) -> str:
    try:
        with open(filename, 'r') as f:
            return f.read()
    except OSError as e:
        raise OSError(f'Cannot read file {filename}: {e}') from e


def create_folders(folder_structure: str):
    if not os.path.isabs(folder_structure):
        raise ValueError(f'create_folders requires an absolute path, got: {folder_structure!r}')

    # split the folder structure string into individual path components
    components = folder_structure.split('/')

    # iterate over the path components and create the directories
    path = '/'
    try:
        for component in components:
            if '{' in component:
                # expand the braces and create directories for each combination
                subcomponents = component.strip('{}').split(',')
                for subcomponent in subcomponents:
                    new_path = os.path.join(path, subcomponent)
                    os.makedirs(new_path, exist_ok=True)
            else:
                path = os.path.join(path, component)
                os.makedirs(path, exist_ok=True)
    except Exception as e:
        tui.console.print(f"ERROR: Failed to build folder structure: {e}")
        tui.console.error(f"create_folders({folder_structure}): {e}")


class Node:
    def __init__(self, value: Any):
        self.value = value
        self.children: List['Node'] = []

    def add_child(self, child: 'Node') -> None:
        """Add a child node, avoiding duplicates"""
        if child not in self.children:
            self.children.append(child)

    def remove_child(self, child: 'Node') -> bool:
        """Remove a child node, return True if removed, False if not found"""
        try:
            self.children.remove(child)
            return True
        except ValueError:
            return False
    
    def has_child(self, child: 'Node') -> bool:
        """Check if node has a specific child"""
        return child in self.children
    
    def get_child_by_value(self, value: Any) -> Optional['Node']:
        """Find child by value"""
        for child in self.children:
            if child.value == value:
                return child
        return None
    
    def is_leaf(self) -> bool:
        """Check if node has no children"""
        return len(self.children) == 0
    
    def __repr__(self) -> str:
        return f"Node(value={self.value}, children={len(self.children)})"
    
    def __eq__(self, other: Any) -> bool:
        """Enable equality comparison"""
        if not isinstance(other, Node):
            return False
        return self.value == other.value
    
    def __hash__(self) -> int:
        """Enable hashing for set operations"""
        return hash(self.value)

class Tree:
    def __init__(self):
        self.root: Optional[Node] = None

    def add_node(self, value: Any, parent_value: Optional[Any] = None) -> Node:
        """Add a node to the tree"""
        # Check if node with this value already exists
        if self.find_node(value) is not None:
            raise ValueError(f"Node with value '{value}' already exists")
            
        node = Node(value)
        
        if parent_value is None:
            if self.root is None:
                self.root = node
            else:
                raise ValueError("Cannot add root node as it already exists")
        else:
            parent_node = self.find_node(parent_value)
            if parent_node is None:
                raise ValueError(f"Parent node with value '{parent_value}' does not exist")
            parent_node.add_child(node)
            
        return node

    def delete_node(self, value: Any) -> bool:
        """Delete a node and handle its children"""
        node = self.find_node(value)
        if node is None:
            return False
            
        parent = self.find_parent_node(value)
        
        if parent is not None:
            # Remove from parent
            parent.remove_child(node)
            # Move children to parent (or could delete them - depends on use case)
            for child in node.children:
                parent.add_child(child)
        else:
            # Deleting root node
            if len(node.children) == 0:
                self.root = None
            elif len(node.children) == 1:
                self.root = node.children[0]
            else:
                raise ValueError("Cannot delete root node with multiple children")
        
        return True

    def find_node(self, value: Any) -> Optional[Node]:
        """Find a node by value"""
        return self._find_node_helper(self.root, value)

    def _find_node_helper(self, node: Optional[Node], value: Any) -> Optional[Node]:
        """Recursive helper for finding nodes"""
        if node is None:
            return None
        if node.value == value:
            return node
        for child in node.children:
            result = self._find_node_helper(child, value)
            if result is not None:
                return result
        return None

    def find_parent_node(self, value: Any) -> Optional[Node]:
        """Find the parent of a node with given value"""
        return self._find_parent_node_helper(None, self.root, value)

    def _find_parent_node_helper(self, parent: Optional[Node], node: Optional[Node], value: Any) -> Optional[Node]:
        """Recursive helper for finding parent nodes"""
        if node is None:
            return None
        if node.value == value:
            return parent
        for child in node.children:
            result = self._find_parent_node_helper(node, child, value)
            if result is not None:
                return result
        return None

    def size(self) -> int:
        """Return total number of nodes in the tree"""
        return self._count_nodes(self.root)
    
    def _count_nodes(self, node: Optional[Node]) -> int:
        """Recursively count all nodes"""
        if node is None:
            return 0
        count = 1  # Count current node
        for child in node.children:
            count += self._count_nodes(child)
        return count

    def depth(self) -> int:
        """Return the maximum depth of the tree"""
        return self._calculate_depth(self.root)
    
    def _calculate_depth(self, node: Optional[Node]) -> int:
        """Recursively calculate tree depth"""
        if node is None:
            return 0
        if not node.children:
            return 1
        return 1 + max(self._calculate_depth(child) for child in node.children)

    def get_leaves(self) -> List[Node]:
        """Return all leaf nodes"""
        leaves = []
        self._collect_leaves(self.root, leaves)
        return leaves
    
    def _collect_leaves(self, node: Optional[Node], leaves: List[Node]) -> None:
        """Recursively collect leaf nodes"""
        if node is None:
            return
        if node.is_leaf():
            leaves.append(node)
        else:
            for child in node.children:
                self._collect_leaves(child, leaves)

    def get_path_to_node(self, value: Any) -> Optional[List[Any]]:
        """Get path from root to node with given value"""
        path = []
        if self._find_path_helper(self.root, value, path):
            return path
        return None
    
    def _find_path_helper(self, node: Optional[Node], value: Any, path: List[Any]) -> bool:
        """Recursive helper for finding path to node"""
        if node is None:
            return False
        
        path.append(node.value)
        
        if node.value == value:
            return True
        
        for child in node.children:
            if self._find_path_helper(child, value, path):
                return True
        
        path.pop()  # Backtrack
        return False

    @property
    def is_childless(self) -> bool:
        """Check if root has no children"""
        if not self.root:
            return True
        return len(self.root.children) == 0

    @property
    def is_empty(self) -> bool:
        """Check if tree is empty"""
        return self.root is None
    
    def __repr__(self) -> str:
        return f"Tree(size={self.size()}, depth={self.depth()}, empty={self.is_empty})"
    
    def print_tree(self, node: Optional[Node] = None, indent: str = "") -> None:
        """Print tree structure"""
        if node is None:
            node = self.root
        if node is None:
            tui.console.print("Empty tree")
            return

        tui.console.print(f"{indent}{node.value}")
        for child in node.children:
            self.print_tree(child, indent + "  ")