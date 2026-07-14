import contextlib
import dataclasses
import datetime
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import subprocess
import tempfile
import configparser
import argparse

import requests
import tui
import _version
from tui import ProgressBar
from typing import (
    Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple,
    TYPE_CHECKING)

# Re-exported from bump (the versioning module).  Explicit `as` aliases
# mark these as intentional re-exports so `utils.<name>` call sites keep
# resolving; bump.py is the single reviewable home for the logic.
from bump import (
    ASG_SUFFIX_RE as ASG_SUFFIX_RE,
    _NMU_STRIP_FIELDS as _NMU_STRIP_FIELDS,
    _NMU_SUFFIX_RE as _NMU_SUFFIX_RE,
    apply_asg_suffix as apply_asg_suffix,
    asg_filename as asg_filename,
    find_matching_artifact as find_matching_artifact,
    match_pristine_base as match_pristine_base,
    normalize_repo_filename as normalize_repo_filename,
    parse_asg_suffix as parse_asg_suffix,
    parse_deb_filename as parse_deb_filename,
    pristine_base as pristine_base,
    strip_build_version as strip_build_version,
    EMPTY_PATCH_SET_HASH as EMPTY_PATCH_SET_HASH,
    compute_transposed_versions as compute_transposed_versions,
    decide_patch_bump_count as decide_patch_bump_count,
    strip_binNMU as strip_binNMU,
    is_source_artifact as is_source_artifact,
    source_package_version as source_package_version,
    strip_nmu_from_control_text as strip_nmu_from_control_text,
    strip_nmu_from_deb as strip_nmu_from_deb,
    strip_nmu_suffix as strip_nmu_suffix,
    transpose as transpose,
    transpose_control_text as transpose_control_text,
    transpose_deb as transpose_deb,
    transposed_version as transposed_version,
)

if TYPE_CHECKING:
    # Forward-reference target for type hints — dependencytree imports utils
    # so the runtime import would cycle.
    import dependencytree

logger = logging.getLogger('athena.cache')


# Version constraint operators accepted by apt_pkg.check_dep — the seven
# Debian-defined comparison sigils.  Anything else falls back to '>='
# at call sites (defensive default for malformed input).  Centralised
# here so the cache + dependency-tree modules import from one source of
# truth rather than each carrying its own identical copy.
VALID_CONSTRAINTS = frozenset({'=', '>=', '<=', '>>', '<<', '>', '<'})

# _DEFAULT_SECURITY_KEYRING — InRelease verification keyring.  The
# host-installed debian-archive-keyring is the canonical default on
# any Debian-derived build host.  A fork shipping its own keyring
# would override via [Security] Keyring.
_DEFAULT_SECURITY_KEYRING = '/usr/share/keyrings/debian-archive-keyring.gpg'


# repo/ is segregated by package role.  Layout:
#   repo/main/     installable binaries (base + pkg.list + live + installer +
#                  pool, plus udebs).  ALSO includes -dev side artifacts —
#                  see classify_repo_subdir for the rationale.
#   repo/doc/      `-doc` side artifacts (mostly absent under build profile
#                  `nodoc`; kept for the rare source that produces docs
#                  regardless)
#   repo/dbgsym/   `-dbgsym` side artifacts (mostly absent under build
#                  option `noautodbgsym`)
#   repo/tests/    `-test` / `-tests` side artifacts
#
# Each subdir has its own Packages index.  `repo audit` scopes repo/main.
_REPO_SUBDIRS = ('main', 'doc', 'dbgsym', 'tests')

# the BUILD-OUTPUT component labels (deb_dir_for keys) that the
# maintenance walks — strip (all_deb_dirs) AND stale-artifact detection
# (_scan_stale_files, feeding cleanup + the chroot pre-flight gate) — must
# both cover.  Single canon so the two walks can't drift apart: the bug
# was `_scan_stale_files` walking only _REPO_SUBDIRS (missing `main-udeb`),
# so a superseded `+asg<R>u<N>` udeb in main/debian-installer/ (our built
# e2fsprogs-udeb / debian-archive-keyring-udeb) survived BOTH cleanup and
# the gate — exactly the silent-stale-consumption the gate exists to block.
#
# Deliberately EXCLUDES the non-main components (contrib / non-free /
# non-free-firmware): those hold PRISTINE TUNNELED binaries that the
# maintenance walks must not touch (don't re-strip pristine; the stale
# classifier also can't predict tunneled filenames, so it would KEEP both
# current and superseded tunneled copies anyway).  Tunneled-component
# stale detection is a separate problem (needs tunnel-aware expected-files).
_STALE_SCAN_SUBDIRS = ('main', 'main-udeb', 'doc', 'dbgsym', 'tests')


# User-Agent for every outbound HTTP request.  snapshot.debian.org rate-
# limits aggressively against the default `python-requests/X.Y.Z` UA — to
# the point of routinely timing out our 10s HEAD probes.  curl / apt /
# wget never hit this throttle because they advertise distinct UAs;
# snapshot's docs explicitly ask clients to identify themselves so abuse
# can be triaged.  Setting this here makes every requests.{get,head}
# call use the same identifier, so we never re-introduce the throttle
# by adding a new call site that forgets the header.
# Uses the stable SemVer base (NOT the full git-describe string) so the
# identifier doesn't churn per-commit on snapshot.debian.org's logs.
_HTTP_HEADERS = {'User-Agent': f'athena-build/{_version.base_version()}'}

# Outbound HTTP timeouts (seconds).  Single-value (not (connect, read)
# tuple) on purpose: urllib3 2.x has a real bug where, against
# snapshot.debian.org's HEAD endpoint, a tuple timeout with connect <
# read leaves the socket's recv timeout at the CONNECT value, so reads
# silently use the wrong (smaller) timeout.  Empirically verified:
# `timeout=60` → 302 in 0.2s; `timeout=(10, 60)` → 16s hang then
# "read timeout=10" error.  Single value sets connect AND read to the
# same number, sidestepping the bug.
#
# Trade-off: a dead/unreachable mirror takes _HTTP_TIMEOUT_FAST seconds
# to fail instead of 10s.  Acceptable — cache build runs rarely, and a
# truly dead mirror is a config problem worth surfacing slowly.
#
# Values: 60s for HEAD probes / JSON APIs / small index files,
# 120s for streamed downloads (matches apt's per-file budget).
# snapshot.debian.org's CDN can take 30-60s for cold-cache responses;
# our prior 10s flat tripped on those even when the connection was
# healthy.
_HTTP_TIMEOUT_FAST = 60     # HEAD probes, JSON APIs, small files
_HTTP_TIMEOUT_STREAM = 120  # streamed downloads (InRelease, .deb, …)

# Retry budget for outbound HTTP.  snapshot.debian.org's CDN can be
# intermittently slow on cold-cache files — same URL may return in
# 300ms or stall past the timeout depending on whether the edge node
# has the file cached.  (Note: the dominant stall we actually hit was
# unrouted IPv6, not cold cache — see force_ipv4_http.)  A single retry
# is usually enough; we do 3
# attempts with exponential backoff (1s, 2s) to cover the rare
# multi-stall case.  Retries fire on:
#   - ReadTimeout       (server stalled mid-response)
#   - ConnectTimeout    (TCP handshake didn't complete in time)
#   - ConnectionError   (transient network disruption, TLS reset)
# Retries do NOT fire on HTTPError (4xx/5xx) — those are deterministic
# and a retry would just hit the same response.
_HTTP_RETRY_COUNT = 3


def force_ipv4_http() -> None:
    """Pin the requests/urllib3 HTTP stack to IPv4-only address resolution.

    Python's urllib3 has no Happy-Eyeballs (RFC 8305): it walks ``getaddrinfo``
    results sequentially — IPv6 first when a host publishes both — so a machine
    with SLAAC-assigned but UNROUTED IPv6 (DHCP autoconf on a bridged/NAT VM
    NIC: a global v6 address exists, but packets to it black-hole) stalls every
    fresh connection on the dead v6 address until the connect timeout, then
    falls back to IPv4.  Measured: a 0.3s snapshot.debian.org fetch became a
    4-minute cache-build stall this way (curl avoids it via Happy-Eyeballs).

    snapshot.debian.org and the Debian mirrors are all IPv4-reachable, so we
    pin AF_INET process-wide at startup.  Opt out with ``ATHENA_ALLOW_IPV6=1``
    once the host has working IPv6 routing.  No effect on the API server's
    listen socket (that's not a urllib3 client connection)."""
    if os.environ.get('ATHENA_ALLOW_IPV6') == '1':
        return
    try:
        import socket as _socket
        import urllib3.util.connection as _u3conn
        _u3conn.allowed_gai_family = lambda: _socket.AF_INET
    except Exception as _e:
        logger.warning(f"force_ipv4_http: could not pin IPv4 ({_e})")


# Module-level requests.Session — reuses TCP/TLS connections across
# requests via HTTP keep-alive.  Critical for snapshot.debian.org:
# our cache build / source sync hits ~2000+ files on one host, and
# a fresh TLS handshake per file costs 300-500ms × 2000 = ~15 min
# wasted before any actual content transfer.  With keep-alive,
# subsequent requests on the same connection skip the handshake.
#
# Also amortizes snapshot's per-connection rate limit (~1 req/sec
# sustained): one connection serving 2000 requests is far better
# than 2000 connections each rate-limited independently.
#
# Lazy-initialized so importing utils doesn't open a session
# unnecessarily (e.g., tests that don't make network calls).
# Thread-safe: requests.Session.get / .head are safe to call
# concurrently; urllib3's connection pool serialises per-connection.
import threading as _threading
_HTTP_SESSION_LOCK = _threading.Lock()
_http_session_instance: 'Optional[requests.Session]' = None


def _http_session() -> 'requests.Session':
    """Lazy module-level requests.Session with our standard headers
    pre-set.  Callers that pass `headers=` per-request will get the
    union (per-call headers override session-default).
    """
    global _http_session_instance
    if _http_session_instance is None:
        with _HTTP_SESSION_LOCK:
            if _http_session_instance is None:
                _s = requests.Session()
                _s.headers.update(_HTTP_HEADERS)
                _http_session_instance = _s
    return _http_session_instance


def classify_repo_subdir(filename: str) -> str:
    """Return the subdir under repo/ where `filename` belongs.

    Classification is by binary PACKAGE NAME suffix (everything before
    the first underscore in the filename):

        libfoo_1.0-2_amd64.deb         → 'main'
        libfoo-dev_1.0-2_amd64.deb     → 'main'   (see note)
        libfoo-doc_1.0-2_all.deb       → 'doc'
        libfoo-dbgsym_1.0-2_amd64.deb  → 'dbgsym'
        libfoo-tests_1.0-2_amd64.deb   → 'tests'  (plural)
        libfoo-test_1.0-2_amd64.deb    → 'tests'  (singular)
        foo-udeb_1.0-2_amd64.udeb      → 'main'   (udebs are installable)

    `-dev` packages live in `main` even though they're "side artifacts"
    by traditional Debian taxonomy.  Reason: install-corpus packages
    hard-depend on them at runtime — `build-essential` Depends
    `libc6-dev`, `gcc-12` Depends `libgcc-12-dev`, `g++-12` Depends
    `libstdc++-12-dev`.  Without -dev in main, those hard deps go
    unresolved at install time.  Moving -dev to its own subdir
    (tried 2026-05-20 then reverted) just shifted the problem.

    Unknown extensions / malformed filenames default to 'main'.
    """
    _base = filename.rsplit('.', 1)[0]
    _pkg = _base.split('_', 1)[0]
    if _pkg.endswith('-doc'):
        return 'doc'
    if _pkg.endswith('-dbgsym'):
        return 'dbgsym'
    if _pkg.endswith(('-tests', '-test')):
        return 'tests'
    return 'main'


def tunneled_binary_names(config: 'Any', cache: 'Any') -> 'frozenset[str]':
    """Binary names produced by every ``[Source] Tunneled`` source — the
    ``keep_binnmu_names`` set for :func:`bump.transpose_control_text` /
    :func:`bump.transpose_deb`.

    A tunnelled binary ships at its upstream version VERBATIM when a binNMU
    layer is trailing (transpose_deb keeps the ``+bN``), so any constraint
    bound targeting one must keep that layer instead of having it stripped.
    Resolved via the cache's source records (``Source.binary``); returns the
    empty set when the cache isn't loaded — callers should union in any
    locally-known sibling names (the tunnel path knows its own filenames).
    """
    _names: 'set[str]' = set()
    if cache is None or config is None:
        return frozenset()
    for _src_name in getattr(config, 'tunnel_packages', ()) or ():
        for _src in cache.source_hashtable.get(_src_name) or ():
            _names.update(getattr(_src, 'binary', ()) or ())
    return frozenset(_names)


def cache_universe_lookup(cache: 'Any') -> 'Optional[Callable[[str], Optional[str]]]':
    """A memoised ``binary name → highest Debian version`` lookup over the
    loaded cache — the universe hook for bump's target-aware negative-field
    rewrite (STA-55).  None when no cache is loaded (the rewrite then
    degrades to the standard op; the verdict oracle monitors the residue).
    """
    if cache is None:
        return None
    _memo: 'Dict[str, Optional[str]]' = {}

    def _lookup(name: str) -> 'Optional[str]':
        if name in _memo:
            return _memo[name]
        _v: 'Optional[str]' = None
        try:
            _versions = cache.package_hashtable.get(name)
            if _versions:
                _v = str(max(_versions.keys()))
        except Exception:                                  # noqa: BLE001
            _v = None
        _memo[name] = _v
        return _v

    return _lookup


def version_no_epoch(version: object) -> str:
    """Return a Debian Version's string form with the epoch stripped.

    Debian versions have the grammar `[epoch:]upstream[-revision]`.
    Filenames (`.dsc`, `.deb`, `.udeb`) and convention-based directory
    layouts strip the epoch entirely — `git_2.39.5-0+deb12u3.dsc`
    matches the source whose `Version: 1:2.39.5-0+deb12u3`.  This
    helper produces the no-epoch form for any code that maps a
    Source/Package Version onto a filename or filesystem path.

    Accepts either a `debian.debian_support.Version` instance or a
    plain string — both are coerced via `str()` first, then split
    on the first `:` and the remainder taken.  No `:` → returned
    unchanged.

    Surfaced 2026-05-15 when patches under
    `patch/source/git/2.39.5-0+deb12u3/` and
    `patch/source/llvm-toolchain-15/15.0.6-4/` were silently ignored
    by `_refresh_patches` and `BuildContainer.build()`'s fresh-disk
    read because both call sites used `str(src.version)` (epoch
    intact) to compose the directory path.  Patches at sibling
    paths for sources without an epoch (libevdev, librsvg, bluez,
    lilv, libde265, firefox-esr) discovered fine.
    """
    _s = str(version)
    _colon = _s.find(':')
    if _colon < 0:
        return _s
    return _s[_colon + 1:]


# version-aware kernel selection.  A plain `sorted(...)[-1]` over
# kernel artifacts orders LEXICALLY, so `6.1.0-9` sorts ABOVE `6.1.0-47`
# ('9' > '4') and `6.9` above `6.10` — picking the OLDER kernel.  Two
# co-resident ABIs (stale pre-rollback .deb, kernel refresh) then ship the
# wrong vmlinuz, and independently-sorted vmlinuz/initrd can MISMATCH →
# unbootable image.  These helpers sort by the kernel ABI via
# apt_pkg.version_compare and pair the initrd to the chosen kernel by its
# exact suffix.
_KERNEL_ABI_RE = re.compile(r'(\d+\.\d+\.\d+-\d+-[a-z][a-z0-9-]*)')


def kernel_abi_of(name: str) -> str:
    """Extract the kernel ABI (e.g. ``6.1.0-47-amd64``) from a
    ``vmlinuz-`` / ``initrd.img-`` boot artifact, a ``linux-image-…``
    package name, or a ``linux-image-…_<ver>_<arch>.deb`` filename.
    Returns '' when no numeric-ABI kernel version is present (meta /
    flavor packages).  The leading ``\\d+\\.\\d+\\.\\d+-\\d+`` anchors on
    the ABI, not the trailing deb-version, so the .deb filename case picks
    the package ABI."""
    _m = _KERNEL_ABI_RE.search(os.path.basename(name))
    return _m.group(1) if _m else ''


def _kernel_sort_key() -> 'Callable[[str], Any]':
    """functools sort key ordering kernel names by ABI (apt_pkg version
    semantics); non-ABI names sort below ABI ones, ties broken lexically."""
    import apt_pkg
    import functools
    apt_pkg.init_system()

    def _cmp(a: str, b: str) -> int:
        _va, _vb = kernel_abi_of(a), kernel_abi_of(b)
        if _va and _vb:
            return apt_pkg.version_compare(_va, _vb)
        if _va:
            return 1
        if _vb:
            return -1
        return -1 if a < b else (1 if a > b else 0)
    return functools.cmp_to_key(_cmp)


def latest_kernel_name(names: 'list[str]') -> 'Optional[str]':
    """Highest-ABI entry from a list of kernel artifact / package / .deb
    names, version-aware.  None if the list is empty."""
    if not names:
        return None
    return max(names, key=_kernel_sort_key())


def select_latest_kernel(boot_dir: str) -> 'Optional[Tuple[str, str]]':
    """Return ``(vmlinuz_basename, initrd_basename)`` for the
    highest-version kernel in ``boot_dir``, with the initrd PAIRED to the
    chosen kernel by its exact version suffix (NOT an independent sort).

    Returns None if ``boot_dir`` is unreadable, has no ``vmlinuz-*``, or
    the matching ``initrd.img-<suffix>`` is absent — callers MUST treat
    None as a hard failure rather than ship a kernel without its initrd
    (or a mismatched pair)."""
    try:
        _names = os.listdir(boot_dir)
    except OSError:
        return None
    _vmlinuz = [_n for _n in _names if _n.startswith('vmlinuz-')]
    if not _vmlinuz:
        return None
    _best = latest_kernel_name(_vmlinuz)
    assert _best is not None  # _vmlinuz is non-empty
    _suffix = _best[len('vmlinuz-'):]
    _initrd = 'initrd.img-' + _suffix
    if _initrd not in _names:
        return None
    return (_best, _initrd)


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


@dataclasses.dataclass(frozen=True)
class Mirror:
    """A single archive source (e.g. bookworm main, bookworm-security main).

    Immutable (frozen dataclass) so a Mirror passed around the pipeline
    can't be silently mutated by a downstream consumer.  Construction is
    the only place input shape is validated; afterwards every URL is
    composed from the same set of normalised fields via @property
    accessors.

    Composition:
        url   = <baseurl>/<baseid>          # e.g. http://deb.debian.org/debian
        suite = <release><suffix>           # e.g. bookworm, bookworm-security
    The release / suffix split exists so rebasing to a different release
    only requires changing one [Base].RELEASE field, not every
    [Mirror.*] section.

    Validation is intentionally narrow (non-empty + URL scheme +
    suffix shape) — anything that could legitimately vary across forks
    is accepted.  Operators get a clear ValueError at construct time
    rather than a confusing 404 deep in the download path.
    """

    id:        str
    baseurl:   str
    baseid:    str
    release:   str
    suffix:    str
    component: str
    arch:      str

    def __post_init__(self) -> None:
        # Normalise: strip trailing slash on baseurl, leading/trailing on
        # baseid.  Frozen dataclass needs object.__setattr__ for in-place
        # mutation — the freeze only prevents external writes.
        object.__setattr__(self, 'baseurl', self.baseurl.rstrip('/'))
        object.__setattr__(self, 'baseid',  self.baseid.strip('/'))
        # Normalise None suffix to empty string for back-compat with
        # callers that pass `suffix=None`.
        if self.suffix is None:
            object.__setattr__(self, 'suffix', '')

        # Validation — fail early with a useful message.
        # `component` is intentionally NOT in this list: the empty string
        # is a valid value (signalling flat-layout mirror, used by
        # fork mirror — see scripts/fork_mirror.py).  All other fields
        # are required non-empty strings.
        for _field in ('id', 'baseurl', 'baseid', 'release', 'arch'):
            _val = getattr(self, _field)
            if not _val or not isinstance(_val, str) or not _val.strip():
                raise ValueError(
                    f"Mirror.{_field}: non-empty string required, got {_val!r}"
                )
        if not isinstance(self.component, str):
            raise ValueError(
                "Mirror.component: string required (may be empty for "
                f"flat layout), got {self.component!r}"
            )
        if '://' not in self.baseurl:
            raise ValueError(
                f"Mirror.baseurl must include a scheme (http://, https://, "
                f"or file://), got {self.baseurl!r}"
            )
        if self.suffix and not self.suffix.startswith('-'):
            raise ValueError(
                f"Mirror.suffix must be empty or start with '-' "
                f"(e.g. '-updates', '-security'), got {self.suffix!r}"
            )

    @property
    def suite(self) -> str:
        return f'{self.release}{self.suffix}'

    @property
    def url(self) -> str:
        return f'{self.baseurl}/{self.baseid}'

    @property
    def is_flat(self) -> bool:
        """Flat-layout mirror — Release + index files sit at the URL root,
        no `dists/<suite>/<component>/...` hierarchy.  Triggered by
        component='' (the fork mirror sets this; see scripts/fork_mirror.py).
        apt's equivalent declaration is `deb file://... ./`.
        """
        return self.component == ''

    @property
    def dist_url(self) -> str:
        # Flat layout: 'file:///working_dir/fork/'
        # Standard layout: 'http://deb.debian.org/debian/dists/bookworm/'
        if self.is_flat:
            return f'{self.url}/'
        return f'{self.url}/dists/{self.suite}/'

    @property
    def packages_path(self) -> str:
        # Flat layout: 'Packages' (sits next to Release at mirror root)
        # Standard layout: 'main/binary-amd64/Packages'
        if self.is_flat:
            return 'Packages'
        return f'{self.component}/binary-{self.arch}/Packages'

    @property
    def sources_path(self) -> str:
        # Flat layout: 'Sources'.  Standard: 'main/source/Sources'.
        if self.is_flat:
            return 'Sources'
        return f'{self.component}/source/Sources'

    @property
    def udeb_packages_path(self) -> str:
        # d-i (udeb) Packages index.  Only main typically
        # publishes this; updates/security mirrors won't have it.  Cache
        # treats this path as OPTIONAL — missing-from-Release is fine.
        # Flat layout: 'Packages-udeb' (separate file from regular Packages
        # to keep deb/udeb routing simple; cache reads each into its own
        # hashtable).
        if self.is_flat:
            return 'Packages-udeb'
        return f'{self.component}/debian-installer/binary-{self.arch}/Packages'

    def with_snapshot(self, ts: 'Optional[str]',
                      baseurl: str = 'https://snapshot.debian.org/archive') -> 'Mirror':
        """Return a copy of this Mirror rewritten to the snapshot service.

        The default targets snapshot.debian.org's layout
            <baseurl>/<baseid>/<TS>/dists/<suite>/...
        so we only need to rewrite baseurl + baseid; everything else
        (suite, component, packages_path, sources_path) is unchanged.
        Passing ts=None returns self — call sites can use this method
        unconditionally.

        `baseurl` defaults to the Debian snapshot service for back-compat
        with callers that don't have a BuildConfig.  Live call sites in
        `resolve_snapshot_timestamp` thread `config.snapshot_baseurl`
        through so a fork's snapshot mirror can be configured via
        `[Snapshot] BaseUrl` without touching this method.
        """
        if ts is None:
            return self
        # file:// mirrors are local trees (e.g. fork mirror); snapshot
        # rewriting points at a remote service and would break the URL.
        if self.baseurl.startswith('file://'):
            return self
        return dataclasses.replace(
            self,
            baseurl = baseurl,
            baseid  = f'{self.baseid}/{ts}',
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
            gpg = gnupg.GPG(gnupghome=work_dir)  # type: ignore[attr-defined]
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


def check_dep3_header(patch_path: str) -> list:
    """Inspect the leading prose of a patch file for required DEP-3 headers.

    (https://dep-team.pages.debian.net/deps/dep3/) defines the
    pseudo-headers Debian expects above the `--- a/...` diff line:

      Description: (or Subject:)  what the patch does and why
      Origin: / Author:           where it came from / who wrote it
      Forwarded:                  whether the upstream knows about it

    Returns the list of required field names that are missing.  An
    empty list means the header passes; a non-empty list is what the
    caller surfaces in a warning.  We scan only the first ~40 lines —
    Headers must precede the diff hunks anyway.

    Soft check: returns missing fields, does not raise.  The caller
    (cmd_parse_dependency) logs them as warnings; the patch is still
    applied at build time.  Keep DEP-3 a guideline rather than a gate
    so an operator's ad-hoc one-off patch is not blocked, but the
    project's own patch tree is held to the convention.
    """
    REQUIRED = ('Description', 'Origin')   # Author satisfies Origin
    found: set = set()
    try:
        with open(patch_path, 'r', errors='replace') as fh:
            for _i, _line in enumerate(fh):
                if _i >= 40 or _line.startswith('---'):
                    break       # past the header, into the diff
                _s = _line.strip()
                if _s.startswith(('Description:', 'Subject:')):
                    found.add('Description')
                elif _s.startswith(('Origin:', 'Author:')):
                    found.add('Origin')
    except OSError:
        # Caller will warn separately on read failure; pretend complete.
        return []
    return [_f for _f in REQUIRED if _f not in found]


def compute_tree_hash(root: str,
                      skip_dirs: tuple = ('.git', '__pycache__')) -> str:
    """SHA256 over the content of every regular file under `root`.

    Walks recursively; sorts paths for determinism; hashes
    (relative_path + NUL + content + NUL) per file in sorted order.
    Mirrors patch_set_hash's encoding so the two functions are
    interchangeable in shape — only the scope differs.

    Used by fork_mirror's invalidation pass to detect any change to
    a fork/source/<pkg>/ tree.  Same hash → tree unchanged → reuse
    cached artifacts.  Different hash → wipe and rebuild.

    Skips directories named in `skip_dirs` (default: VCS metadata
    and Python bytecode caches — neither carries semantic content
    for source builds).  Unreadable files are skipped silently
    (their absence shifts the hash, which is the conservative
    behaviour — see patch_set_hash).

    Missing root → digest of empty input ("").
    """
    _h = hashlib.sha256()
    if not os.path.isdir(root):
        return _h.hexdigest()

    _files: list = []
    for _dirpath, _dirnames, _filenames in os.walk(root):
        _dirnames[:] = [_d for _d in _dirnames if _d not in skip_dirs]
        for _fn in _filenames:
            _abs = os.path.join(_dirpath, _fn)
            _rel = os.path.relpath(_abs, root)
            _files.append(_rel)
    _files.sort()

    for _rel in _files:
        try:
            with open(os.path.join(root, _rel), 'rb') as fh:
                _content = fh.read()
        except OSError:
            continue
        _h.update(_rel.encode('utf-8'))
        _h.update(b'\0')
        _h.update(_content)
        _h.update(b'\0')
    return _h.hexdigest()


def patch_set_hash(patch_dir: str, patch_files: list,
                   prebuild_path: 'Optional[str]' = None) -> str:
    """SHA256 over (filename + NUL + content + NUL) tuples in the given order.

    Used by `_refresh_patches` (build.py) to confirm whether a patch
    file whose mtime is newer than the corresponding build.json record
    actually changed in *content*, or merely had its mtime bumped by a
    header / comment edit.  Same hash → no rebuild needed; hash differs
    → real patch change, drop the record.

    Also stamped into the build.json record's patch_set_hash field on
    every build attempt so future refreshes have a stable baseline.

    `prebuild_path` — the package's version-independent prebuild script
    (`patch/source/<pkg>/prebuild.sh`, sourced into the build shell).
    It shapes the build exactly like a patch does, so its content folds
    into the same hash: editing, adding, or removing it invalidates the
    build record.  EVERY caller (compose_recipe, _refresh_patches, sbom)
    must pass it for the baselines to agree.

    Returns hex digest.  Empty patch_files + no prebuild → digest of "".
    Missing/unreadable files are skipped silently (mirrors
    _refresh_patches's defensive os.path.exists guard); the resulting
    hash naturally differs from a baseline taken when the file was
    readable, which is the conservative behaviour.
    """
    _h = hashlib.sha256()
    for _pf in patch_files:
        try:
            with open(os.path.join(patch_dir, _pf), 'rb') as fh:
                _content = fh.read()
        except OSError:
            continue
        _h.update(_pf.encode('utf-8'))
        _h.update(b'\0')
        _h.update(_content)
        _h.update(b'\0')
    if prebuild_path:
        try:
            with open(prebuild_path, 'rb') as fh:
                _pb: 'Optional[bytes]' = fh.read()
        except OSError:
            _pb = None
        if _pb is not None:
            _h.update(b'prebuild.sh\0')
            _h.update(_pb)
            _h.update(b'\0')
    return _h.hexdigest()


def prebuild_script_path(patch_source_dir: str, package: str) -> str:
    """Path of a package's OPTIONAL version-independent prebuild script:
    `patch/source/<pkg>/prebuild.sh`.  Sits NEXT to the version-keyed patch
    dirs (one intervention point per package) and survives version bumps.
    Sourced (not executed) into the build shell before dpkg-buildpackage so
    it can export package-specific build environment; a script error fails
    that package's build (recipe runs under set -e).  Callers check
    existence themselves."""
    return os.path.join(patch_source_dir, package, 'prebuild.sh')


# ─── canonical signed build record ──────────────────────────────────
#
# `<pkg>.build.json` replaces the older `<pkg>.result` (PASS/FAIL one-liner)
# and `<pkg>.patchhash` sidecars with a single signed JSON record per source
# build attempt.  Carries everything the older sidecars did plus:
#
#   - intended_version vs built_version  — detect drift (a build was started
#     for X, what landed on disk for Y?)
#   - phase state machine                — crash recovery: if process is
#     killed mid-build, the file shows the last completed phase, so the
#     next session can classify the source as 'interrupted' instead of
#     silently re-trying without context.
#   - elapsed_seconds                    — per-source wall time, summed
#     across the corpus gives snapshot-pivot effort estimates for free.
#   - HMAC-SHA256 signature              — local tamper-detection.  Key
#     lives at cache/.metrics.hmac.key (mode 0600, auto-generated, gitignored).
#     Threat model is "did this file change since the build wrote it"
#     (manual edit, partial write, bitflip on shared NFS), NOT adversarial.
#
# Writes are staged: every phase transition does read → merge → re-sign →
# fsync → os.replace.  POSIX rename(2) guarantees readers see either the
# old valid file or the new valid file, never a torn write.

BUILD_RECORD_SCHEMA_VERSION = 6
BUILD_RECORD_SUFFIX = '.build.json'
# v1 → v2: adds output_hashes {filename: sha256_hex}, the
# per-emitted-binary digest that the coord layer pins into its claim
# records.  v1 records survive unchanged (no field = empty hash dict);
# the backfill_output_hashes() helper upgrades them in place when run.
# v2 → v3: adds two fields covering the federation's
# tunneled + pulled provenance signals.
#   - `republished_from` {filename: {url, upstream_sha256}} —
#     populated by cmd_tunnel_package; threads through to the claim's
#     republished_from field so the federation can mark tunneled
#     packages as no-owner.
#   - `pulled_from` {mirror_name, owner_builder} | None —
#     populated by cmd_mirror_pull (chunk 10) when this builder
#     pulled a peer's .deb to satisfy a missing dep / version.
#     Distinguishes "we built it" from "we pulled it" in
#     `source audit` without losing the record.
# v2 records survive unchanged (defaults: {} and None respectively).
# v3 → v4: the record becomes the per-source LIFECYCLE
# document — tracking starts at `cache parse`, not at build.  Additive
# fields (absent = legacy v3, all consumers unchanged):
#   - `lifecycle_v` 1
#   - `selection`   'selected' | 'deprecated' | 'retracted'
#   - `selected_version` / `snapshot` — what the parse selected, under
#     which snapshot pin (str(Source.version) at last parse touch)
#   - `selected_at` / `deprecated_at` / `retracted_at` / `published_at`
#   - `history` — list of superseded episodes rolled in place when the
#     version moves (state 'obsolete') or the selection state flips
#     (state 'deprecated'/'retracted'):
#       {state, version, snapshot, rolled_at, built_version?, outputs?}
# A parse-created record for a never-built source carries the new
# non-terminal phase 'selected', which classify_build_record maps to
# 'missing' so every phase-gate consumer (publish claims, check_build,
# audits) behaves exactly as if the record did not exist.
# TRANSIENT v4 field `prior_build` {built_version, outputs}: stashed by
# preserve_lifecycle at the entry-phase rewrite (which resets the build
# fields) and resolved by roll_prior_build_history at build completion —
# version superseded → 'obsolete' history roll; same-version rebuild →
# stash dropped.  Lingers only across an unfinished rebuild.
_BUILD_RECORD_HMAC_KEY_BASENAME = '.metrics.hmac.key'

# Phase state machine.  Linear progression on the happy path:
#   entry → container_exited → segregated → normalized → done
# Terminal alternates:
#   failed   — any non-zero exit / exception / OOM / operator-decline
#   tunneled — package was passthrough-copied from upstream, no container
# A phase string not in {done, failed, tunneled} means the build was
# interrupted mid-flight; the record's `status` will be None.
# 'selected' is the parse-created pre-build phase — the source
# entered the selection but no build has started.  classify_build_record
# maps it to 'missing' (NOT 'interrupted') so build/publish/audit
# consumers treat it exactly like an absent record.
BUILD_PHASES = (
    'selected',
    'entry',
    'container_exited',
    'segregated',
    'normalized',
    'done',
    'failed',
    'tunneled',
)

# lifecycle fields — the v4 additive layer.  Record CREATORS
# (build-start, tunnel-entry) must carry these through a rewrite via
# preserve_lifecycle(); upsert_build_record() merges them in place.
LIFECYCLE_FIELDS = (
    'lifecycle_v',
    'selection',
    'selected_version',
    'snapshot',
    'selected_at',
    'deprecated_at',
    'retracted_at',
    'published_at',
    'history',
)
SELECTION_SELECTED = 'selected'
SELECTION_DEPRECATED = 'deprecated'
SELECTION_RETRACTED = 'retracted'
_TERMINAL_PHASES = frozenset({'done', 'failed', 'tunneled'})
_PHASE_TO_STATUS = {
    'done':     'PASS',
    'failed':   'FAIL',
    'tunneled': 'TUNNELED',
}

# Status values surfaced to consumers (repo audit, print summary, etc.).
# INTERRUPTED is *derived* — never written by build(); it's what the
# classifier returns when it reads a non-terminal phase across a session
# boundary (i.e. the process that wrote the record didn't finish).
BUILD_STATUSES = frozenset({'PASS', 'FAIL', 'TUNNELED', 'INTERRUPTED'})


def _build_record_path(buildlog_dir: str, package: str) -> str:
    """Absolute path to a package's build.json record."""
    return os.path.join(buildlog_dir, package + BUILD_RECORD_SUFFIX)


def _hmac_key_path(buildlog_dir: str) -> str:
    """Key file path.  Co-located with the records it signs so the
    threat model (local tamper detection) is clear from layout."""
    return os.path.join(buildlog_dir, _BUILD_RECORD_HMAC_KEY_BASENAME)


def _load_or_create_hmac_key(buildlog_dir: str) -> bytes:
    """Return the HMAC key for this build root, bootstrapping if absent.

    Generates 32 random bytes on first call, writes mode 0600.  Repeat
    callers re-read the existing file.  Concurrent first-build workers
    race here but it's idempotent — last writer wins, both end up
    verifying records they wrote against the same final key.  (A worker
    that wrote a record under key K1 and then re-reads under K2 will
    see a sig-verification failure; that's the intended signal — the
    audit treats verify-failure as 'missing', triggering rebuild.)

    Key is bytes; do not log or expose.
    """
    _path = _hmac_key_path(buildlog_dir)
    try:
        with open(_path, 'rb') as _fh:
            _data = _fh.read()
        if len(_data) >= 32:
            return _data
    except FileNotFoundError:
        pass
    except OSError as _e:
        logger.warning(f"build-record HMAC key read failed ({_path}): {_e}")
    _new = secrets.token_bytes(32)
    # Write with 0600 from the start — open(O_CREAT|O_EXCL|O_WRONLY, 0o600)
    # via os.open keeps the mode atomic with creation, avoiding the
    # umask race that plain open() has.
    try:
        _fd = os.open(_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(_fd, _new)
            os.fsync(_fd)
        finally:
            os.close(_fd)
        os.chmod(_path, 0o600)
    except OSError as _e:
        logger.warning(f"build-record HMAC key bootstrap failed ({_path}): {_e}")
    return _new


def _canonical_record_bytes(record: dict) -> bytes:
    """Serialize a record (minus the `sig` field) to canonical JSON bytes
    suitable for HMAC.  sort_keys + ASCII-safe + no whitespace variance
    means two semantically-identical records hash identically across
    Python versions and locales."""
    _payload = {_k: _v for _k, _v in record.items() if _k != 'sig'}
    return json.dumps(
        _payload, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
    ).encode('utf-8')


def _sign_record(record: dict, key: bytes) -> dict:
    """Return `record` with its `sig` field set to HMAC-SHA256 hex digest
    over the canonical-JSON encoding of every other field.  Pure: caller
    owns the dict, we return a copy with `sig` added."""
    _out = dict(record)
    _out.pop('sig', None)
    _mac = hmac.new(key, _canonical_record_bytes(_out), hashlib.sha256)
    _out['sig'] = _mac.hexdigest()
    return _out


def _verify_record(record: dict, key: bytes) -> bool:
    """True iff `record['sig']` matches HMAC-SHA256 over the rest.
    Uses hmac.compare_digest for constant-time comparison."""
    _sig = record.get('sig')
    if not isinstance(_sig, str):
        return False
    _expected = hmac.new(
        key, _canonical_record_bytes(record), hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(_sig, _expected)


def sudo(
    cmd_args: 'List[str]', password: str, capture: bool = True,
) -> 'subprocess.CompletedProcess':
    """Run `sudo -S <cmd_args>`, feeding `password` on stdin — the single
    canonical sudo wrapper, replacing the four byte-near-identical
    module-local `_sudo` copies (apt_repo / iso_installer / installer_chroot /
    disk_image).  `capture=False` lets the child inherit the parent's
    stdout/stderr for commands whose output should stream live."""
    return subprocess.run(
        ['sudo', '-S', *cmd_args],
        input=password + '\n',
        capture_output=capture, text=True,
    )


@contextlib.contextmanager
def sudo_askpass_env(password: str) -> 'Iterator[Dict[str, str]]':
    """Yield an environment dict for `sudo -A <cmd>` so the password comes
    from a short-lived 0700 askpass helper instead of the command's stdin —
    freeing stdin for the command's OWN data (a partition script piped to
    sfdisk, a `find | cpio` pipeline) and dropping the `sudo -S bash -c
    "… < file"` / `"… > file"` shell interpolation entirely.

    Robust regardless of the operator's sudo `timestamp_timeout` (no
    credential-cache dependency, unlike a pre-`sudo -v` then bare `sudo`).
    The helper reads the password from an env var so a shell metacharacter
    in the password can't break it.  The script + env entry live only for
    the `with` body."""
    _fd, _path = tempfile.mkstemp(prefix='.athena-askpass-', suffix='.sh')
    try:
        os.write(_fd, b'#!/bin/sh\nprintf %s "$ATHENA_SUDO_ASKPASS_PW"\n')
        os.close(_fd)
        os.chmod(_path, 0o700)
        yield dict(os.environ, SUDO_ASKPASS=_path,
                   ATHENA_SUDO_ASKPASS_PW=password)
    finally:
        try:
            os.remove(_path)
        except OSError:
            pass


def write_iso_md5sum_manifest(staging_dir: str, password: str) -> bool:
    """Write a Debian-style ``md5sum.txt`` at the ISO staging ROOT — one
    ``<md5hex>  ./relative/path`` line per file, sorted (deterministic),
    excluding the manifest itself.

    This is the on-media integrity manifest that d-i's ``cdrom-checker``
    ("Check disc integrity") and live-boot's ``live-media-check`` read to verify
    the burned media, so a corrupt USB/DVD write is caught at boot rather than
    failing confusingly mid-install (MAT-08).

    Runs under sudo — the staged tree carries root-owned files (the squashfs,
    the generated apt repo): the hashing runs as root, and the password comes
    via SUDO_ASKPASS so the ``tee`` write receives ONLY the manifest on stdin
    (no password/content mixing, no shell-redirection interpolation).  Returns
    True on success."""
    if not os.path.isdir(staging_dir):
        logger.error(f"write_iso_md5sum_manifest: no staging dir {staging_dir}")
        return False
    with sudo_askpass_env(password) as _env:
        # cwd=staging + `find .` → paths are './…' relative to the ISO root.
        _scan = subprocess.run(
            ['sudo', '-A', 'find', '.', '-type', 'f', '!', '-name',
             'md5sum.txt', '-exec', 'md5sum', '{}', '+'],
            cwd=staging_dir, env=_env, capture_output=True, text=True)
        if _scan.returncode != 0:
            logger.error(
                "write_iso_md5sum_manifest: find/md5sum failed: "
                f"{(_scan.stderr or '').strip()[:200]}")
            return False
        _manifest = '\n'.join(sorted(
            _l for _l in _scan.stdout.splitlines() if _l.strip()))
        if _manifest:
            _manifest += '\n'
        _tee = subprocess.run(
            ['sudo', '-A', 'tee', os.path.join(staging_dir, 'md5sum.txt')],
            input=_manifest, env=_env, capture_output=True, text=True)
        if _tee.returncode != 0:
            logger.error(
                "write_iso_md5sum_manifest: writing md5sum.txt failed: "
                f"{(_tee.stderr or '').strip()[:200]}")
            return False
    return True


def _existing_mode(path: str, default: int = 0o644) -> int:
    """The file's current permission bits, or `default` when it doesn't
    exist — so an atomic rewrite preserves operator-set perms (e.g. a
    group-writable build.conf) instead of forcing 0o644."""
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return default


def _atomic_write_bytes(path: str, data: bytes, mode: int = 0o644) -> None:
    """Write `data` to `path` via temp-file + fsync + os.replace so a
    crash mid-write leaves the prior file intact (POSIX rename(2)
    atomicity).  fsync the data before rename so an OS crash post-rename
    can't surface an empty file.

    `mode` is the final permission (mkstemp makes the temp 0o600); pass
    `_existing_mode(path)` to preserve an operator-set mode on rewrite."""
    _dir = os.path.dirname(path) or '.'
    _fd, _tmp = tempfile.mkstemp(
        prefix='.' + os.path.basename(path) + '.', suffix='.tmp', dir=_dir,
    )
    try:
        with os.fdopen(_fd, 'wb') as _fh:
            _fh.write(data)
            _fh.flush()
            os.fsync(_fh.fileno())
        os.chmod(_tmp, mode)
        os.replace(_tmp, path)
    except OSError:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise


def _utc_now_iso() -> str:
    """UTC ISO-8601 with second resolution, trailing 'Z'.  Stable across
    locales; sortable lexicographically; safe to grep."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ',
    )


def new_build_record(*, package: str,
                     intended_version: str,
                     patch_set_hash: str,
                     started: 'Optional[str]' = None,
                     component: str = 'main') -> dict:
    """Construct an initial phase=entry record.  Caller passes it to
    write_build_record to materialize on disk.

    `started` defaults to now-UTC if omitted; passed explicitly by tests
    that need deterministic timestamps.

    `component` is the apt component (main / contrib / non-free /
    non-free-firmware) of the source's origin mirror.  Pinned on the
    build record so downstream consumers (federation claims, `mirror
    pull`, audit) don't have to re-derive it from the dep tree.
    Defaults to 'main' to keep existing callers working unchanged.
    """
    return {
        'schema_version':   BUILD_RECORD_SCHEMA_VERSION,
        # Toolchain provenance: which Athena-Build version produced this record.
        # Informational/additive — no consumer gates on it and no schema bump
        # is needed (the v3/v4 migration ignores unknown keys); read defensively
        # with .get() since older on-disk records predate the field.
        'athena_build_version': _version.get_version(),
        'package':          package,
        'intended_version': intended_version,
        'built_version':    None,
        'patch_set_hash':   patch_set_hash,
        'phase':            'entry',
        'status':           None,
        'started':          started or _utc_now_iso(),
        'finished':         None,
        'elapsed_seconds':  None,
        'exit_code':        None,
        'oom_killed':       False,
        'output_count':     0,
        'outputs':          [],
        'output_hashes':    {},
        # Per-output upstream provenance for tunneled packages.
        # Shape: {filename: {url, upstream_sha256}}.  Empty for
        # normally-built records; populated by cmd_tunnel_package
        # and (read-side) by mirror pull.
        'republished_from': {},
        # Provenance marker for mirror-pulled records.  Shape:
        # {mirror_name, owner_builder} when set, None for locally-
        # built or locally-tunneled records.  Lets `source audit`
        # distinguish "we built it" from "we pulled it" without
        # losing the on-disk record.
        'pulled_from':      None,
        # Apt component for the source's binaries on disk —
        # determines repo/dists/<codename>/<comp>/binary-<arch>/.
        'component':        component,
        # v4 lifecycle baseline.  The selection/version/
        # snapshot/timestamps layer is stamped by `cache parse`
        # (upsert_build_record / lifecycle_touch_selected); a fresh
        # record only carries the migrated markers.
        'lifecycle_v':      1,
        'history':          [],
        # (v5): container resource telemetry, populated at the
        # container_exited phase — {peak_rss_bytes, peak_rss_mb,
        # mem_limit_bytes, peak_cpu_pct, samples}.  None until the build
        # runs (or for pulled/tunneled records that never ran a container).
        'resources':        None,
        # TRANSPOSE-scheme per-source sidecar counters (the algo's
        # sidecar(Source)).  patch_bump_count = P, our patch level on the
        # current upstream base (0 = faithful; bumps when patch_set_hash
        # changes within a base, resets to 1 on a base change / 0 when the
        # patch is removed).  bn_bump_count = our force-binNMU level (0 unless
        # we deliberately force-rebuild).  Additive + .get()-defaulted at every
        # read, so no schema bump / migration needed (cf. athena_build_version);
        # the patch identity itself rides in the existing patch_set_hash, and
        # the prior upstream base in built_version/intended_version.
        'patch_bump_count': 0,
        'bn_bump_count':    0,
    }


def write_build_record(buildlog_dir: str, record: dict) -> None:
    """Sign + atomic-write a record.  Overwrites any prior file for the
    same package.  Caller responsible for record shape (use
    new_build_record / update_build_record to avoid drift)."""
    _key = _load_or_create_hmac_key(buildlog_dir)
    _signed = _sign_record(record, _key)
    _path = _build_record_path(buildlog_dir, record['package'])
    _atomic_write_bytes(
        _path, json.dumps(_signed, sort_keys=True, indent=2).encode('utf-8'),
    )


def _build_history_path(buildlog_dir: str) -> str:
    """The cross-run ledger sits one level up from log/build/."""
    return os.path.join(
        os.path.dirname(os.path.normpath(buildlog_dir)), 'build-history.jsonl')


def _build_history_line(record: dict) -> dict:
    """Project a build record (live or on-disk <pkg>.build.json) down
    to the compact history summary stored one-per-run in the ledger."""
    _phase = record.get('phase')
    _status = ('PASS' if _phase == 'done'
               else 'FAIL' if _phase == 'failed'
               else str(record.get('status') or _phase or 'unknown'))
    _res = record.get('resources') or {}
    return {
        'ts': record.get('finished') or _utc_now_iso(),
        'package': record.get('package'),
        'status': _status,
        'version': (record.get('built_version')
                    or record.get('intended_version')),
        'snapshot': record.get('snapshot'),
        'elapsed_seconds': record.get('elapsed_seconds'),
        'exit_code': record.get('exit_code'),
        'oom_killed': record.get('oom_killed'),
        'peak_rss_mb': _res.get('peak_rss_mb'),     # telemetry
        'peak_cpu_pct': _res.get('peak_cpu_pct'),
    }


def archive_build_record(buildlog_dir: str, record: 'Optional[dict]') -> None:
    """Append a COMPLETED build record to the append-only journal
    ``log/build-history.jsonl`` (sibling of *buildlog_dir*) just before the
    next build of the same source overwrites its ``<pkg>.build.json``.

    The journal stores the SAME signed build.json records (one schema — the
    existing build.json tooling reads them unchanged).  The current run lives
    in ``<pkg>.build.json``; every PRIOR run lives in the journal — so each run
    is stored exactly once, no duplication.  Append-only; rotation deferred per
    the publish-before-prune discipline.

    Best-effort and never raises: observability must never break a build."""
    try:
        if not record:
            return
        with open(_build_history_path(buildlog_dir), 'a', encoding='utf-8') as _fh:
            _fh.write(json.dumps(record, sort_keys=True) + '\n')
    except Exception as _e:
        logger.warning(f"archive_build_record: {_e}")


def read_build_history(buildlog_dir: str) -> 'list[dict]':
    """The cross-run build history as display summaries, oldest first.

    Merges the append-only journal (PRIOR runs — full build.json records moved
    there at overwrite time) with the current ``<pkg>.build.json`` (the LATEST
    run, not yet archived).  The two are disjoint by construction — a run is
    archived only when the next build of that source overwrites build.json — so
    each completed run appears exactly once.  Deduped by (package, finished-ts)
    as a safety net; only completed runs (done/failed) are included; malformed
    records skipped."""
    _seen: set = set()
    _out: 'list[dict]' = []

    def _consider(_rec: object) -> None:
        if not isinstance(_rec, dict):
            return
        # only COMPLETED runs (skip interrupted / in-flight phases)
        if (_rec.get('phase') not in ('done', 'failed')
                and _rec.get('status') not in ('PASS', 'FAIL')):
            return
        _line = _build_history_line(_rec)
        _key = (_line['package'], _line['ts'])
        if _key in _seen:
            return
        _seen.add(_key)
        _out.append(_line)

    # 1. journal — every prior run (full build.json records)
    try:
        with open(_build_history_path(buildlog_dir), encoding='utf-8') as _fh:
            for _ln in _fh:
                _ln = _ln.strip()
                if not _ln:
                    continue
                try:
                    _consider(json.loads(_ln))
                except ValueError:
                    continue
    except OSError:
        pass
    # 2. current build.json records — the latest (not-yet-archived) run
    try:
        _entries = os.listdir(buildlog_dir)
    except OSError:
        _entries = []
    for _fn in _entries:
        if not _fn.endswith(BUILD_RECORD_SUFFIX):
            continue
        try:
            with open(os.path.join(buildlog_dir, _fn), encoding='utf-8') as _fh:
                _consider(json.loads(_fh.read()))
        except (OSError, ValueError):
            continue
    _out.sort(key=lambda _r: str(_r.get('ts') or ''))
    return _out


def read_build_record(buildlog_dir: str, package: str) -> 'Optional[dict]':
    """Load and verify a record.  Returns the dict on success, None on:

      - file missing
      - JSON decode error
      - HMAC verify failure  (logged as warning — indicates tamper or
        partial write that somehow survived os.replace, neither of
        which should happen in normal operation)

    Consumers should treat None as 'no record' — equivalent to today's
    'no .result file' semantics, which already drives rebuild in the
    audit classifier.
    """
    _path = _build_record_path(buildlog_dir, package)
    try:
        with open(_path, 'rb') as _fh:
            _record = json.loads(_fh.read())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as _e:
        logger.warning(f"build-record read failed ({_path}): {_e}")
        return None
    if not isinstance(_record, dict):
        logger.warning(f"build-record not a JSON object: {_path}")
        return None
    _key = _load_or_create_hmac_key(buildlog_dir)
    if not _verify_record(_record, _key):
        logger.warning(f"build-record HMAC verify failed: {_path}")
        return None
    return _record


def update_build_record(buildlog_dir: str, package: str,
                        **fields: object) -> dict:
    """Read-merge-sign-write convenience.  Loads the current record (or
    raises FileNotFoundError if absent), applies `fields` as an overlay,
    re-signs, atomically writes, returns the new record.

    Phase transitions go through here.  Caller passes the new phase
    explicitly (no auto-progression) so the call site documents the
    transition in source.

    When phase reaches a terminal value, the matching status is set
    automatically if not provided explicitly.
    """
    _current = read_build_record(buildlog_dir, package)
    if _current is None:
        raise FileNotFoundError(
            f"no build record to update for {package} in {buildlog_dir}",
        )
    _current.update(fields)
    _phase = _current.get('phase')
    if (_phase in _TERMINAL_PHASES
            and _current.get('status') is None
            and _phase in _PHASE_TO_STATUS):
        _current['status'] = _PHASE_TO_STATUS[_phase]
    write_build_record(buildlog_dir, _current)
    return _current


def upsert_build_record(buildlog_dir: str, package: str,
                        **fields: object) -> dict:
    """Read-merge-resign-write that NEVER raises on a missing
    record — the lifecycle layer's entry point.

    When no record exists (or HMAC verify fails — same treat-as-missing
    degrade the rest of the codebase uses), synthesizes a minimal
    phase='selected' record: the source entered the selection but no
    build has run.  classify_build_record maps that to 'missing', so
    publish/check_build/audit consumers behave as if the record were
    absent.  Existing records keep every build field; `fields` overlay.

    Defaults `lifecycle_v` / `history` on first lifecycle touch.
    """
    _current = read_build_record(buildlog_dir, package)
    if _current is None:
        _current = new_build_record(
            package=package,
            intended_version=str(fields.get('selected_version', '') or ''),
            patch_set_hash='',
        )
        _current['phase'] = 'selected'
    _current.update(fields)
    _current.setdefault('lifecycle_v', 1)
    _current.setdefault('history', [])
    write_build_record(buildlog_dir, _current)
    return _current


def preserve_lifecycle(old: 'Optional[dict]', new: dict) -> dict:
    """Carry the lifecycle layer through a record RE-CREATION.

    Record creators (build-start entry record, tunnel entry record)
    historically overwrite the whole record; that would wipe the
    selection/history layer the parse wrote.  Copy every LIFECYCLE_FIELD
    present on `old` into `new` unless `new` explicitly set it.  Returns
    `new` (mutated copy semantics match callers building a fresh dict).

    Also STASHES the prior build episode as the transient `prior_build`
    field ({built_version, outputs}) — the entry write resets
    built_version/outputs, so this is the only carrier that lets the
    done-phase decide whether the new build SUPERSEDED the old version
    (roll an 'obsolete' history entry via roll_prior_build_history) or
    merely rebuilt the same one (drop the stash).  An unresolved stash
    from an earlier failed rebuild is carried forward unchanged.
    """
    if old is None:
        return new
    for _f in LIFECYCLE_FIELDS:
        if _f not in old:
            continue
        # Carry old when new lacks the field OR only has the empty
        # baseline a fresh new_build_record seeds (history=[]) — a
        # creator never intends the baseline to ERASE the lived layer.
        if _f not in new or not new[_f]:
            new[_f] = old[_f]
    if 'prior_build' not in new:
        if old.get('built_version'):
            new['prior_build'] = {
                'built_version': old.get('built_version'),
                'outputs': list(old.get('outputs') or []),
            }
        elif old.get('prior_build'):
            new['prior_build'] = old['prior_build']
    return new


def roll_prior_build_history(buildlog_dir: str, package: str,
                             new_built_version: 'Optional[str]') -> bool:
    """Resolve a `prior_build` stash at build completion.

    If the prior episode's built_version differs from the new one (a
    snapshot move or +asg bump superseded it), roll an 'obsolete'
    history entry carrying the OLD built_version/outputs.  A same-version
    rebuild just drops the stash.  Returns True iff a roll happened.
    Best-effort — callers wrap like _record_phase (never mask a build).
    """
    _rec = read_build_record(buildlog_dir, package)
    if _rec is None:
        return False
    _pb = _rec.pop('prior_build', None)
    if not isinstance(_pb, dict) or not _pb:
        return False
    _old_v = _pb.get('built_version')
    _rolled = False
    if _old_v and str(_old_v) != str(new_built_version or ''):
        roll_lifecycle_history(
            _rec, state='obsolete',
            extra={'built_version': _old_v,
                   'outputs': list(_pb.get('outputs') or [])})
        _rolled = True
    write_build_record(buildlog_dir, _rec)
    return _rolled


def lifecycle_touch_selected(buildlog_dir: str, selected: 'Dict[str, str]',
                             snapshot: str) -> dict:
    """The `cache parse` lifecycle touch — tracking starts HERE.

    `selected` maps every selected SOURCE name (deb ∪ udeb trees) to its
    version string.  Per source:

      - no record            → create a phase='selected' lifecycle record
      - selection deprecated/retracted (re-selection round trip)
                             → roll the episode into history, back to selected
      - selected_version moved (snapshot change / bump)
                             → roll an 'obsolete' history entry, update current
                               (phase/built_version untouched — check_build
                               re-triggers the rebuild naturally)
      - legacy record (no lifecycle layer yet)
                             → stamp it once
      - already current      → SKIP THE WRITE entirely, so steady-state
                               parses re-sign nothing (mtime-stable)

    Returns {'created','reselected','rolled','stamped','unchanged'} counts.
    Best-effort per record — one bad record never aborts the parse.
    """
    _now = _utc_now_iso()
    _stats = {'created': 0, 'reselected': 0, 'rolled': 0,
              'stamped': 0, 'unchanged': 0}
    for _name in sorted(selected):
        _v = str(selected[_name])
        try:
            _rec = read_build_record(buildlog_dir, _name)
            if _rec is None:
                # Create directly (mirrors upsert_build_record's None branch)
                # rather than calling upsert, which would read_build_record a
                # second time for a record we already know is absent.
                _new = new_build_record(
                    package=_name, intended_version=_v, patch_set_hash='')
                _new['phase'] = 'selected'
                _new.update(selection=SELECTION_SELECTED, selected_version=_v,
                            snapshot=snapshot, selected_at=_now)
                _new.setdefault('lifecycle_v', 1)
                _new.setdefault('history', [])
                write_build_record(buildlog_dir, _new)
                _stats['created'] += 1
                continue
            _sel = _rec.get('selection')
            if _sel in (SELECTION_DEPRECATED, SELECTION_RETRACTED):
                # archive the timestamp that matches the episode's
                # state — a retracted record must keep retracted_at, not
                # deprecated_at, in its rolled history.
                _ts_field = ('retracted_at' if _sel == SELECTION_RETRACTED
                             else 'deprecated_at')
                roll_lifecycle_history(
                    _rec, state=str(_sel), now=_now,
                    extra={_ts_field: _rec.get(_ts_field)})
                _rec.update({
                    'selection': SELECTION_SELECTED,
                    'selected_version': _v, 'snapshot': snapshot,
                    'selected_at': _now,
                    'deprecated_at': None, 'retracted_at': None,
                })
                _rec.setdefault('lifecycle_v', 1)
                write_build_record(buildlog_dir, _rec)
                _stats['reselected'] += 1
                continue
            _cur_v = _rec.get('selected_version')
            if _cur_v and str(_cur_v) != _v:
                roll_lifecycle_history(_rec, state='obsolete', now=_now)
                _rec.update({'selected_version': _v, 'snapshot': snapshot,
                             'selected_at': _now})
                _rec.setdefault('selection', SELECTION_SELECTED)
                _rec.setdefault('lifecycle_v', 1)
                write_build_record(buildlog_dir, _rec)
                _stats['rolled'] += 1
                continue
            if (_rec.get('selection') == SELECTION_SELECTED
                    and _cur_v == _v
                    and _rec.get('snapshot') == snapshot):
                _stats['unchanged'] += 1          # steady state — no write
                continue
            # Legacy record (or partial lifecycle layer) — stamp once.
            _rec.update({'selection': SELECTION_SELECTED,
                         'selected_version': _v, 'snapshot': snapshot,
                         'selected_at': _now})
            _rec.setdefault('lifecycle_v', 1)
            _rec.setdefault('history', [])
            write_build_record(buildlog_dir, _rec)
            _stats['stamped'] += 1
        except OSError as _e:
            logger.warning(f"lifecycle touch {_name}: {_e}")
    return _stats


def lifecycle_mark_deprecated(buildlog_dir: str, srcs: 'Iterable[str]',
                              snapshot: str) -> int:
    """Intent-at-accept: record `selection='deprecated'` the
    moment the operator ACCEPTS a closure shrink (`cache select accept`) —
    before any mirror publish.  The mirror-side deprecation claim (publish
    Step 6b) then PROPAGATES this intent; until it does, the coherence
    audit shows the source as 'deprecation pending publish' instead of a
    CRITICAL untracked drop.  Returns the number of records marked."""
    _now = _utc_now_iso()
    _n = 0
    for _name in sorted(set(srcs)):
        try:
            upsert_build_record(
                buildlog_dir, _name,
                selection=SELECTION_DEPRECATED,
                deprecated_at=_now, snapshot=snapshot)
            _n += 1
        except OSError as _e:
            logger.warning(f"lifecycle deprecate {_name}: {_e}")
    return _n


def roll_lifecycle_history(record: dict, *, state: str,
                           now: 'Optional[str]' = None,
                           extra: 'Optional[dict]' = None) -> dict:
    """Append the record's CURRENT lifecycle episode to its
    `history` list — the single shape used for every roll:

      obsolete   — the selected version moved (snapshot change / +asg bump)
      deprecated — the source left the selection (episode archived on
                   re-selection)
      retracted  — artifact withdrawn (episode archived likewise)

    Mutates + returns `record`; caller updates the current fields and
    writes.  Build info (built_version/outputs) is snapshotted so the
    superseded episode stays reconstructable after the next build
    overwrites the top-level fields.
    """
    _entry: dict = {
        'state': state,
        'version': record.get('selected_version'),
        'snapshot': record.get('snapshot'),
        'rolled_at': now or _utc_now_iso(),
        'built_version': record.get('built_version'),
        'outputs': list(record.get('outputs') or []),
    }
    if extra:
        _entry.update(extra)
    record.setdefault('history', []).append(_entry)
    return record


def classify_build_record(record: 'Optional[dict]') -> str:
    """Translate a loaded record into the audit-classifier vocabulary.

    Returns one of:
      'missing'      — record is None (never built, or tampered/corrupt)
      'interrupted'  — record exists but phase is non-terminal (the
                       process that wrote it didn't finish)
      'ok'           — phase=done, status=PASS
      'fail'         — phase=failed (status=FAIL)
      'tunneled'     — phase=tunneled (status=TUNNELED)

    The existing classifier in build.py overlays patch_set_hash drift
    on top of this to produce stale_pass / needs_build refinements; that
    logic stays in build.py, this helper just maps the record state.
    """
    if record is None:
        return 'missing'
    _phase = record.get('phase')
    if _phase == 'selected':
        # parse-created record: source selected, never built.
        # 'missing' (not 'interrupted') so build/publish/audit treat it
        # exactly like an absent record.
        return 'missing'
    if _phase not in _TERMINAL_PHASES:
        return 'interrupted'
    _status = record.get('status')
    if _status == 'PASS':
        return 'ok'
    if _status == 'TUNNELED':
        return 'tunneled'
    return 'fail'


def verify_output_hashes(buildlog_dir: str, repo_root: str) -> dict:
    """Verify every terminal build.json record's recorded ``output_hashes``
    against the on-disk artifact.  Catches RECORD↔DISK drift: a record whose
    stored SHA-256 no longer matches the file in repo/ — e.g. a post-record
    re-pack / re-stamp (non-reproducible ``dpkg-deb -b`` timestamps) left the
    record stale.

    Distinct from :func:`backfill_output_hashes`, which only FILLS missing
    hashes and SKIPS any record that already has one — so it never notices a
    stale-but-present hash.  Distinct from ``source audit``/``_source_state``,
    which checks file existence + ar-validity but never hashes.

    Uses the cached :func:`get_sha256` (size+mtime sidecar), so a clean repo
    re-verifies in milliseconds and only files whose mtime moved since the
    last hash are recomputed — which is exactly the drift case.

    Note the repo is a PRUNED subset of the declared output set (see
    [[project_virtual_drift_is_repo_pruning_not_synth]]): an output that is
    simply absent from disk is NORMAL, not a fault.  Only a present file whose
    hash disagrees with the record is an integrity violation.  Both are
    returned, separated, so the caller can treat them differently.

    Returns {'scanned': int, 'mismatched': [(pkg, output)],
             'absent': [(pkg, output)]}.
    """
    _index: 'dict[str, str]' = {}
    for _root, _dirs, _files in os.walk(repo_root):
        for _f in _files:
            if _f.endswith(('.deb', '.udeb')):
                _index[_f] = os.path.join(_root, _f)

    _stats: dict = {'scanned': 0, 'mismatched': [], 'absent': []}
    try:
        _entries = sorted(os.listdir(buildlog_dir))
    except OSError:
        return _stats
    for _entry in _entries:
        if not _entry.endswith(BUILD_RECORD_SUFFIX):
            continue
        _pkg = _entry[:-len(BUILD_RECORD_SUFFIX)]
        _rec = read_build_record(buildlog_dir, _pkg)
        if _rec is None:
            continue
        # Terminal, successful records only — failed/interrupted have no
        # authoritative on-disk artifact to match against.
        # Terminal records that produced artifacts: phase 'done' (local
        # build) OR 'tunneled' (remote passthrough).  'failed' has no valid
        # outputs.  The old gate (status=='PASS' or phase=='done') dropped
        # tunneled records — but generate_pending_claims claims BOTH
        # done+tunneled, and `repo repair strip` repacks tunneled .debs too,
        # so a tunneled record's stored hashes go stale after a strip exactly
        # like a done one.  Skipping them left the drift invisible to the
        # verify pre-flight (chroot "hash mismatch") and hid the local-ahead
        # bytes from `mirror reclaim`.
        if (_rec.get('phase') not in ('done', 'tunneled')
                and _rec.get('status') not in ('PASS', 'TUNNELED')):
            continue
        _hashes = _rec.get('output_hashes') or {}
        for _o in _rec.get('outputs') or []:
            _stats['scanned'] += 1
            _path = _index.get(_o)
            if _path is None:
                _stats['absent'].append((_pkg, _o))   # pruned — not a fault
                continue
            _rec_h = _hashes.get(_o)
            if not _rec_h:
                continue   # no recorded hash → backfill's job, not drift
            if get_sha256(_path) != _rec_h:
                _stats['mismatched'].append((_pkg, _o))
    return _stats


def refresh_output_hashes(buildlog_dir: str, repo_root: str) -> dict:
    """Recompute and UPDATE every terminal record's ``output_hashes`` from the
    on-disk artifact — the recovery for an in-place repair (``repo repair
    strip``) that repacked a .deb and left the record's stored SHA-256 stale.

    Distinct from :func:`backfill_output_hashes` (fills MISSING hashes only,
    skips a present-but-stale one) and :func:`verify_output_hashes`
    (read-only, reports drift).  This one OVERWRITES a present-but-stale hash
    with the current on-disk value, so the RECORD↔DISK integrity gate
    (``verify_output_hashes`` in the chroot pre-flight) passes after a repair.

    Same-name assumption: an in-place strip does not rename (the .deb's own
    version is untouched — only constraint bounds change), so the record's
    ``outputs`` filenames still match disk.  Absent outputs (pruned subset)
    are left alone.  Uses the cached :func:`get_sha256`; a repacked file's
    moved mtime forces the recompute, so only actually-changed files re-hash.

    Returns {'scanned': int, 'updated': int, 'absent': int}.
    """
    _index: 'dict[str, str]' = {}
    for _root, _dirs, _files in os.walk(repo_root):
        for _f in _files:
            if _f.endswith(('.deb', '.udeb')):
                _index[_f] = os.path.join(_root, _f)
    _stats: dict = {'scanned': 0, 'updated': 0, 'absent': 0}
    try:
        _entries = sorted(os.listdir(buildlog_dir))
    except OSError:
        return _stats
    for _entry in _entries:
        if not _entry.endswith(BUILD_RECORD_SUFFIX):
            continue
        _pkg = _entry[:-len(BUILD_RECORD_SUFFIX)]
        _rec = read_build_record(buildlog_dir, _pkg)
        if _rec is None:
            continue
        # Terminal records that produced artifacts: phase 'done' (local
        # build) OR 'tunneled' (remote passthrough).  'failed' has no valid
        # outputs.  The old gate (status=='PASS' or phase=='done') dropped
        # tunneled records — but generate_pending_claims claims BOTH
        # done+tunneled, and `repo repair strip` repacks tunneled .debs too,
        # so a tunneled record's stored hashes go stale after a strip exactly
        # like a done one.  Skipping them left the drift invisible to the
        # verify pre-flight (chroot "hash mismatch") and hid the local-ahead
        # bytes from `mirror reclaim`.
        if (_rec.get('phase') not in ('done', 'tunneled')
                and _rec.get('status') not in ('PASS', 'TUNNELED')):
            continue
        _hashes = dict(_rec.get('output_hashes') or {})
        _changed = False
        for _o in _rec.get('outputs') or []:
            _stats['scanned'] += 1
            _path = _index.get(_o)
            if _path is None:
                _stats['absent'] += 1                  # pruned — not a fault
                continue
            _actual = get_sha256(_path)
            if _hashes.get(_o) != _actual:
                _hashes[_o] = _actual
                _changed = True
        if _changed:
            update_build_record(buildlog_dir, _pkg, output_hashes=_hashes)
            _stats['updated'] += 1
    return _stats


def backfill_output_hashes(buildlog_dir: str, repo_root: str) -> dict:
    """Upgrade build.json records to current schema by hashing emitted
    .debs + initialising any new v3 fields.

    For every <pkg>.build.json under `buildlog_dir`:
      - if schema_version is already current AND every output in `outputs`
        has a hash in `output_hashes` AND v3 fields are present, skip;
      - otherwise look up each output by basename under `repo_root`
        (one os.walk builds the filename→path index), hash via the
        cached get_sha256 (size+mtime sidecar), populate `output_hashes`,
        bump `schema_version`, initialise v3 fields if absent, re-sign
        + atomic-replace.

    Returns {scanned, upgraded, skipped, missing_files}.  Idempotent
    — a second invocation reports 0 upgraded.

    Failed records (phase=failed) and entries whose `outputs` list is
    empty get their schema bumped but no hashes (nothing to hash).
    v3-only fields like `republished_from` default to {} for non-tunneled
    backfilled records.
    """
    _index: 'dict[str, str]' = {}
    for _root, _dirs, _files in os.walk(repo_root):
        for _f in _files:
            if _f.endswith(('.deb', '.udeb')):
                _index[_f] = os.path.join(_root, _f)

    _stats = {'scanned': 0, 'upgraded': 0, 'skipped': 0, 'missing_files': 0}
    try:
        _entries = sorted(os.listdir(buildlog_dir))
    except OSError:
        return _stats
    for _entry in _entries:
        if not _entry.endswith(BUILD_RECORD_SUFFIX):
            continue
        _stats['scanned'] += 1
        _pkg = _entry[:-len(BUILD_RECORD_SUFFIX)]
        _rec = read_build_record(buildlog_dir, _pkg)
        if _rec is None:
            continue
        _outputs = _rec.get('outputs') or []
        _existing = _rec.get('output_hashes') or {}
        _current_version = _rec.get('schema_version', 1)
        _has_v3_fields = ('republished_from' in _rec
                          and 'pulled_from' in _rec
                          and 'component' in _rec)
        _has_v4_fields = 'lifecycle_v' in _rec and 'history' in _rec
        _has_v6_fields = ('patch_bump_count' in _rec
                          and 'bn_bump_count' in _rec)
        if (_current_version >= BUILD_RECORD_SCHEMA_VERSION
                and all(_o in _existing for _o in _outputs)
                and _has_v3_fields and _has_v4_fields and _has_v6_fields):
            _stats['skipped'] += 1
            continue
        _new_hashes = dict(_existing)
        _missing = False
        for _o in _outputs:
            if _o in _new_hashes:
                continue
            _path = _index.get(_o)
            if _path is None:
                _missing = True
                continue
            _h = get_sha256(_path)
            if _h:
                _new_hashes[_o] = _h
            else:
                _missing = True
        if _missing:
            _stats['missing_files'] += 1
        _rec['output_hashes'] = _new_hashes
        _rec['schema_version'] = BUILD_RECORD_SCHEMA_VERSION
        # v3 fields — initialise if absent.
        # republished_from stays empty for normally-built backfilled
        # records (only cmd_tunnel_package populates it at write time).
        if 'republished_from' not in _rec:
            _rec['republished_from'] = {}
        if 'pulled_from' not in _rec:
            _rec['pulled_from'] = None
        # Component defaults to 'main' on backfill — older records all
        # pre-date non-main publish.  Going forward new_build_record
        # writes the source's actual component.
        if 'component' not in _rec:
            _rec['component'] = 'main'
        # v4 fields — initialise the lifecycle layer.  The
        # selection/version/snapshot fields are stamped by the next
        # `cache parse` touch; here we only mark the record migrated.
        _rec.setdefault('lifecycle_v', 1)
        _rec.setdefault('history', [])
        # v6 fields — transpose-scheme per-source sidecar counters; default 0
        # on backfill (older records pre-date the content-order scheme).
        _rec.setdefault('patch_bump_count', 0)
        _rec.setdefault('bn_bump_count', 0)
        try:
            write_build_record(buildlog_dir, _rec)
            _stats['upgraded'] += 1
        except OSError as _e:
            logger.warning(f"backfill_output_hashes {_pkg}: write failed: {_e}")
    return _stats


def _query_snapshot_latest(api_url: str, archive_keys: 'List[str]') -> str:
    """Fetch the latest snapshot timestamp covering every archive in
    `archive_keys` (typically `['debian', 'debian-security']`).

    Returns min(latest_per_key) so the chosen TS is valid for every
    archive tree (the service resolves a missing exact TS to the nearest
    snapshot ≤ TS via 302, but we want the symmetric guarantee that
    every archive has a snapshot at or before the timestamp).

    Endpoint:  GET <api_url>
    Response:  {"result": {"<key>": [...sorted ts list...], ...}}
    The list is sorted lexicographically which matches chronological order
    for the YYYYMMDDTHHMMSSZ format, so `[-1]` is the latest.

    Both `api_url` and `archive_keys` are sourced from BuildConfig
    ([Snapshot] TimestampApi / ArchiveKeys) so a fork running its own
    snapshot mirror can configure them without code changes.
    """
    try:
        resp = _http_session().get(
            api_url, timeout=_HTTP_TIMEOUT_FAST, headers=_HTTP_HEADERS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"Failed to query {api_url}: {e}") from e

    try:
        result = data['result']
        _latest_per_key = {_k: result[_k][-1] for _k in archive_keys}
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(
            f"Unexpected response shape from {api_url}: {e}; "
            f"top-level keys={list(data.keys())}, "
            f"requested archive_keys={archive_keys}"
        ) from e

    for _label, _ts in _latest_per_key.items():
        if not _SNAPSHOT_TS_RE.match(_ts):
            raise RuntimeError(
                f"snapshot service returned malformed timestamp for {_label}: {_ts!r}"
            )

    # Lexical min == chronological min for YYYYMMDDTHHMMSSZ
    chosen = min(_latest_per_key.values())
    _per_key_str = ', '.join(f"{_k}={_v}" for _k, _v in _latest_per_key.items())
    tui.console.print(f"snapshot latest: {_per_key_str}, picking {chosen}")
    return chosen


def list_snapshots_between(config: 'BuildConfig', after_ts: str,
                           upto_ts: str) -> 'List[str]':
    """All distinct snapshot timestamps strictly AFTER `after_ts` and up to
    (inclusive) `upto_ts`, unioned across every archive key, sorted ascending.

    One GET of [Snapshot] TimestampApi returns the full per-archive lists
    (same endpoint `_query_snapshot_latest` uses).  Powers `snapshot list` /
    `snapshot select`'s "advance current → one of these" picker.  Returns []
    on query failure or when there are no snapshots in range."""
    try:
        resp = _http_session().get(
            config.snapshot_timestamp_api, timeout=_HTTP_TIMEOUT_FAST,
            headers=_HTTP_HEADERS)
        resp.raise_for_status()
        result = resp.json()['result']
    except Exception as e:
        logger.warning(f"list_snapshots_between: query failed: {e}")
        return []
    _seen: set = set()
    for _k in config.snapshot_archive_keys:
        for _ts in result.get(_k, []) or []:
            if (_SNAPSHOT_TS_RE.match(_ts)
                    and after_ts < _ts <= upto_ts):
                _seen.add(_ts)
    return sorted(_seen)


def list_snapshots_and_latest(
        config: 'BuildConfig',
        after_ts: str) -> 'Tuple[Optional[str], List[str]]':
    """One GET of [Snapshot] TimestampApi → ``(latest, between)``.

    `latest` matches :func:`_query_snapshot_latest`: the min over archive keys
    of each key's max timestamp (the covering latest valid for every archive),
    or None on query failure / malformed data.  `between` matches
    :func:`list_snapshots_between`: distinct timestamps strictly after
    `after_ts` and up to (inclusive) `latest`, unioned across keys, sorted
    ascending.  Lets a caller that needs BOTH (snapshot list / select) avoid
    the double GET those two helpers would otherwise do."""
    try:
        resp = _http_session().get(
            config.snapshot_timestamp_api, timeout=_HTTP_TIMEOUT_FAST,
            headers=_HTTP_HEADERS)
        resp.raise_for_status()
        result = resp.json()['result']
        _latest_per_key = {_k: result[_k][-1]
                           for _k in config.snapshot_archive_keys}
    except Exception as e:
        logger.warning(f"list_snapshots_and_latest: query failed: {e}")
        return None, []
    for _ts in _latest_per_key.values():
        if not _SNAPSHOT_TS_RE.match(_ts):
            logger.warning(
                f"list_snapshots_and_latest: malformed timestamp {_ts!r}")
            return None, []
    _latest = min(_latest_per_key.values())
    _seen: set = set()
    for _k in config.snapshot_archive_keys:
        for _ts in result.get(_k, []) or []:
            if _SNAPSHOT_TS_RE.match(_ts) and after_ts < _ts <= _latest:
                _seen.add(_ts)
    return _latest, sorted(_seen)


def _validate_snapshot_timestamp(
        ts: str, mirrors: 'List[Mirror]',
        snapshot_baseurl: str = 'https://snapshot.debian.org/archive',
) -> bool:
    """HEAD-validate that every mirror has an InRelease available at the
    given snapshot timestamp.  Catches typos and timestamps that predate a
    given suite (e.g. picking 20180101 when bookworm didn't exist yet).

    The snapshot service responds 302 → /file/<sha> for any timestamp it
    can serve (nearest-≤ resolves arbitrary timestamps), so we follow the
    redirect and check the final 200.  A 404 at the redirect target means
    the file doesn't exist for that timestamp and the validation fails.

    `snapshot_baseurl` defaults to the Debian service for back-compat;
    live call sites pass `config.snapshot_baseurl`.
    """
    for m in mirrors:
        snap = m.with_snapshot(ts, baseurl=snapshot_baseurl)
        url  = snap.dist_url + 'InRelease'
        try:
            resp = _http_session().head(
                url, timeout=_HTTP_TIMEOUT_FAST,
                allow_redirects=True, headers=_HTTP_HEADERS)
        except Exception as e:
            logger.error(f"snapshot validate: HEAD {url} failed: {e}")
            return False
        if resp.status_code != 200:
            logger.error(
                f"snapshot validate: {url} returned HTTP {resp.status_code} — "
                f"timestamp {ts} does not cover suite {snap.suite} on archive {m.baseid}"
            )
            return False
        tui.console.print(f"snapshot validate: OK {url}")
    return True


def check_mirror_reachability(
        mirrors: 'List[Mirror]', ts: 'Optional[str]',
        snapshot_baseurl: str) -> 'List[Tuple[str, bool, str]]':
    """HEAD-probe each mirror's top-level Release/InRelease at the URL the
    cache will actually fetch (snapshot-rewritten when `ts` is set).

    Pure read — no downloads, no mutation.  Returns
    [(label, ok, detail), ...]; ok=False means the index is not reachable
    (network down, wrong URL, or a snapshot timestamp that doesn't cover the
    suite).  Deduped by URL: the dozen [Mirror.*] component sections collapse
    to the few distinct InRelease URLs (one per suite) they actually share.
    file:// mirrors are checked for on-disk existence instead of HTTP.
    """
    _out: 'List[Tuple[str, bool, str]]' = []
    _seen: 'set[str]' = set()
    for _m in mirrors:
        _snap = _m.with_snapshot(ts, baseurl=snapshot_baseurl)
        _base = _snap.dist_url
        _is_local = _base.startswith('file://')
        _url = _base + ('Release' if _is_local else 'InRelease')
        if _url in _seen:
            continue
        _seen.add(_url)
        _label = f"{_snap.baseid} {_snap.suite}".strip()
        if _is_local:
            _path = _url[len('file://'):]
            _ok = os.path.isfile(_path)
            _out.append((_label, _ok,
                         _url if _ok else f"missing local file: {_path}"))
            continue
        try:
            _resp = _http_session().head(
                _url, timeout=_HTTP_TIMEOUT_FAST,
                allow_redirects=True, headers=_HTTP_HEADERS)
        except Exception as _e:           # noqa: BLE001 — any failure = down
            _out.append((_label, False, f"{type(_e).__name__} — {_url} ({_e})"))
            continue
        if _resp.status_code == 200:
            _out.append((_label, True, _url))
        else:
            _out.append((_label, False,
                         f"HTTP {_resp.status_code} — {_url} (wrong URL, or the "
                         f"snapshot timestamp does not cover this suite)"))
    return _out


def format_snapshot_timestamp(ts: str) -> str:
    """Render a Debian-snapshot YYYYMMDDTHHMMSSZ string as a human-readable
    UTC datetime, e.g. '20260506T120451Z' → '2026-05-06 12:04:51 UTC'.

    Falls back to returning the input unchanged if it does not match the
    expected snapshot format — caller can still display *something*.
    """
    if not _SNAPSHOT_TS_RE.match(ts):
        return ts
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]} UTC"


def snapshot_iso_tag(config: 'BuildConfig') -> str:
    """The effective snapshot timestamp as a filesystem-safe tag for ISO
    filenames and the .disk/snapshot marker — e.g. '20260514T083402Z'.

    Lets an operator tell ISOs built from different snapshots apart (
    build base → step the snapshot → build again; the two ISOs must be
    distinguishable).  Returns '' when snapshots are disabled (no pin to tag).
    Reads the already-resolved pin (cheap when the cache file exists); never
    raises — ISO naming must not fail on a snapshot-resolution hiccup."""
    try:
        _ts = resolve_snapshot_timestamp(config)
    except Exception:
        _ts = None
    return _ts or ''


def snapshot_state_path(config: 'BuildConfig') -> str:
    """Path to the durable snapshot-pin state (config/snapshot.state).

    Lives under config/ (NOT cache/) so `clean cache` can't wipe the
    operator-set current pin — has production impact.

    Schema reduced to `{current}`.  Legacy fields
    `base`, `published`, `external` are READ-tolerantly (compat shims
    in build.py) but no longer WRITTEN — they move to per-mirror
    state in Phase 4."""
    return os.path.join(config.dir_config, 'snapshot.state')


def snapshot_history_path(config: 'BuildConfig') -> str:
    """Path to the snapshot-pin history ring (config/snapshot.history).

    JSON list of YYYYMMDDTHHMMSSZ timestamps, MOST RECENT FIRST, capped
    at SNAPSHOT_HISTORY_MAX entries.  Written by `snapshot select` on
    every successful pin change; read by `snapshot history`.

    Lives next to snapshot.state — same durability rationale
    (operator-set, production-impacting, survives `clean cache`)."""
    return os.path.join(config.dir_config, 'snapshot.history')


SNAPSHOT_HISTORY_MAX = 20


def read_snapshot_history(config: 'BuildConfig') -> 'list[str]':
    """Return last 20 selected `current` timestamps, most recent first.
    Empty list on missing / malformed file (silent fallback — history
    is operator-facing only, never load-bearing)."""
    import json
    try:
        with open(snapshot_history_path(config)) as fh:
            _h = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(_h, list):
        return []
    return [_ts for _ts in _h if isinstance(_ts, str) and _SNAPSHOT_TS_RE.match(_ts)]


def append_snapshot_history(config: 'BuildConfig', ts: str) -> None:
    """Push `ts` onto the head of snapshot.history; cap at
    SNAPSHOT_HISTORY_MAX.  If `ts` already appears in the history,
    promote it to head (no duplicate entries — the same operator
    re-selecting a recent timestamp shouldn't fill the ring with
    duplicates).

    Best-effort: any write failure is logged + swallowed (history is
    informational; failure mustn't block the underlying snapshot
    select)."""
    import json
    if not isinstance(ts, str) or not _SNAPSHOT_TS_RE.match(ts):
        return
    _h = read_snapshot_history(config)
    _h = [_t for _t in _h if _t != ts]
    _h.insert(0, ts)
    _h = _h[:SNAPSHOT_HISTORY_MAX]
    _path = snapshot_history_path(config)
    try:
        os.makedirs(os.path.dirname(_path), exist_ok=True)
        _atomic_write_bytes(
            _path, (json.dumps(_h, indent=2) + '\n').encode('utf-8'),
        )
    except OSError as e:
        logger.warning(f"append_snapshot_history: {_path}: {e}")


def read_snapshot_state(config: 'BuildConfig') -> dict:
    """Read config/snapshot.state → {'current': ts}; {} if absent or
    malformed.  Legacy fields (`base`, `published`, `external`) from
    pre files are returned as-is by the reader for one
    cycle — `write_snapshot_state` drops them on the next write so
    the file shrinks to just `current` automatically."""
    import json
    try:
        with open(snapshot_state_path(config)) as fh:
            _d = json.load(fh)
        return _d if isinstance(_d, dict) else {}
    except (OSError, ValueError):
        return {}


def reconcile_snapshot_pin(config: 'BuildConfig') -> 'Optional[tuple[str, str]]':
    """Make build.conf [Snapshot] Timestamp mirror the authoritative pin.

    snapshot.state 'current' (set by `snapshot select`, survives `clean
    cache`) takes precedence over [Snapshot] Timestamp at build time
    (`resolve_snapshot_timestamp`).  This keeps the *visible* config from
    lying about which snapshot the build actually uses:

      - state 'current' set AND != build.conf Timestamp → rewrite the
        build.conf line in place (comments preserved), update the in-memory
        value, and return ``(old, new)`` so the caller can warn the operator
        that build.conf changed.
      - state 'current' absent → return None; build.conf is authoritative.
      - already in sync / file unreadable / no Timestamp line → return None.
    """
    _cur = (read_snapshot_state(config) or {}).get('current')
    if not isinstance(_cur, str) or not _cur:
        return None
    _old = config.snapshot_timestamp_config
    if _cur == _old:
        return None
    try:
        with open(config.config_path, 'r', encoding='utf-8') as _fh:
            _lines = _fh.readlines()
    except OSError:
        return None
    _in_snapshot = False
    _done = False
    for _i, _line in enumerate(_lines):
        _stripped = _line.strip()
        if _stripped.startswith('[') and _stripped.endswith(']'):
            _in_snapshot = (_stripped.lower() == '[snapshot]')
            continue
        if _in_snapshot and re.match(r'\s*Timestamp\s*=', _line):
            _indent = _line[:len(_line) - len(_line.lstrip())]
            _lines[_i] = f"{_indent}Timestamp = {_cur}\n"
            _done = True
            break
    if not _done:
        return None
    try:
        # Atomic rewrite of the operator's master build.conf — a crash
        # mid-write must not truncate it.  Preserve its existing
        # mode (e.g. group-writable 0o664) rather than forcing 0o644.
        _atomic_write_bytes(
            config.config_path, ''.join(_lines).encode('utf-8'),
            mode=_existing_mode(config.config_path),
        )
    except OSError:
        return None
    config.snapshot_timestamp_config = _cur
    return (_old, _cur)


def write_snapshot_state(config: 'BuildConfig',
                         current: 'Optional[str]' = None) -> None:
    """Write `current` to config/snapshot.state; clear the resolve memo.

    `current` is the only field: the operator-selected snapshot pin
    every build runs against.  Per-mirror state files own the
    publish-target pins.  A snapshot.state file carrying extra fields
    gets rewritten cleanly on the next write (only `current` persists)."""
    import json
    _existing = read_snapshot_state(config)
    _new_current = current if current is not None else _existing.get('current')
    _state: 'dict[str, str]' = {}
    if _new_current:
        _state['current'] = _new_current
    _path = snapshot_state_path(config)
    os.makedirs(os.path.dirname(_path), exist_ok=True)
    # Atomic write: a crash mid-write must not truncate the pin —
    # read_snapshot_state swallows the resulting ValueError and the next
    # build silently falls back to [Snapshot] Timestamp.
    _atomic_write_bytes(
        _path,
        (json.dumps(_state, indent=2) + '\n').encode('utf-8'),
        mode=_existing_mode(_path),
    )
    _SNAPSHOT_TS_CACHE.clear()


def local_conf_path(config: 'BuildConfig') -> str:
    """Path to config/local.conf — the UNTRACKED, machine-local sidecar
    holding build Mode, system Role (first|federation), the SetupComplete
    flag, and per-mirror registration markers.  Lives alongside build.conf
    so the repo (a fresh `git pull`) never inherits another machine's mode
    or mirror identity."""
    return os.path.join(os.path.dirname(config.config_path), 'local.conf')


def read_local_conf(config: 'BuildConfig') -> 'configparser.ConfigParser':
    """Read config/local.conf → ConfigParser; an empty parser if the file
    is absent (a fresh, un-onboarded checkout).  A *malformed* file is NOT
    swallowed here — BuildConfig.__init__ reads it strictly so a broken
    local.conf surfaces as an invalid config rather than a silent fallback;
    write-side callers only ever merge into a freshly-read parser."""
    _parser = configparser.ConfigParser()
    try:
        _parser.read(local_conf_path(config))
    except configparser.Error:
        pass
    return _parser


def write_local_conf(config: 'BuildConfig', *,
                     mode: 'Optional[str]' = None,
                     role: 'Optional[str]' = None,
                     setup_complete: 'Optional[bool]' = None,
                     name: 'Optional[str]' = None,
                     signing_key_uid: 'Optional[str]' = None,
                     max_parallel_builds: 'Optional[int]' = None,
                     build_cpus: 'Optional[float]' = None,
                     build_memory: 'Optional[str]' = None,
                     docker_server: 'Optional[str]' = None,
                     create_local_mirror: 'Optional[bool]' = None) -> None:
    """Read-merge-write config/local.conf (mirrors write_snapshot_state's
    shape: read existing, set only the provided fields, atomic write).

    Sections written: [Local] (Mode/Role/SetupComplete/Name), [Build] (host
    tuning: MaxParallelBuilds/BuildCpus/BuildMemory/DOCKER_SERVER), [Repo]
    (SigningKeyUid).  Every field is optional — only the provided ones are
    touched, the rest preserved.

    Per-mirror builder registration lives in the signed mirror.conf now
    (mirror.set_registration / get_registration), NOT here."""
    import io
    _path = local_conf_path(config)
    _parser = read_local_conf(config)
    if not _parser.has_section('Local'):
        _parser.add_section('Local')
    if mode is not None:
        _parser.set('Local', 'Mode', mode)
    if role is not None:
        _parser.set('Local', 'Role', role)
    if setup_complete is not None:
        _parser.set('Local', 'SetupComplete',
                    'true' if setup_complete else 'false')
    if name is not None:
        _parser.set('Local', 'Name', name)
    if create_local_mirror is not None:
        _parser.set('Local', 'CreateLocalMirror',
                    'true' if create_local_mirror else 'false')
    # [Build] host tuning — machine-specific, never tracked.
    if (max_parallel_builds is not None or build_cpus is not None
            or build_memory is not None or docker_server is not None):
        if not _parser.has_section('Build'):
            _parser.add_section('Build')
        if max_parallel_builds is not None:
            _parser.set('Build', 'MaxParallelBuilds', str(max_parallel_builds))
        if build_cpus is not None:
            _parser.set('Build', 'BuildCpus', str(build_cpus))
        if build_memory is not None:
            _parser.set('Build', 'BuildMemory', build_memory)
        if docker_server is not None:
            _parser.set('Build', 'DOCKER_SERVER', docker_server)
    if signing_key_uid is not None:
        if not _parser.has_section('Repo'):
            _parser.add_section('Repo')
        _parser.set('Repo', 'SigningKeyUid', signing_key_uid)
    _buf = io.StringIO()
    _parser.write(_buf)
    os.makedirs(os.path.dirname(_path), exist_ok=True)
    _atomic_write_bytes(
        _path, _buf.getvalue().encode('utf-8'),
        mode=_existing_mode(_path),
    )


# ── remote.conf — UNTRACKED registry of remote build hosts ────────────────
# Each [Remote.<name>] holds an SSH ship-to-host for `source remotebuild`
# (the build runs THERE, Docker local on the remote) plus that host's build
# tuning, so `source remotebuild all` can fan packages out per remote.  Path/
# read/write/CRUD mirror the local.conf helpers; managed via `container
# remote add|list|delete`.

_REMOTE_SECTION_PREFIX = 'Remote.'


def copy_ssh_key(src: str, dst: str) -> bool:
    """Copy an SSH private key from ``src`` to ``dst`` with 0600 perms.
    Returns False if ``src`` is unreadable or the copy fails.  Shared by
    `mirror`/federation onboarding and `container remote add` so a remote's
    key is stored in config/ (vs relying on the operator's ambient ~/.ssh)."""
    import shutil
    if not os.path.isfile(src):
        return False
    try:
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
        return True
    except OSError:
        # If copyfile succeeded but chmod failed, the key is on disk with the
        # default (world-readable) umask — remove it so a failed copy never
        # leaves an exposed private key behind.
        try:
            os.remove(dst)
        except OSError:
            pass
        return False


def remote_token_path(config: 'BuildConfig', name: str) -> str:
    """Path to a remote's build-agent API token — config/<name>.remote-token,
    a 0600 sidecar beside its config/<name>.key (REMOTE-API).  Derived from the
    remote name so the orchestrator can find it without a registry lookup."""
    return os.path.join(config.dir_config, f"{name}.remote-token")


def generate_remote_token(config: 'BuildConfig', name: str) -> 'tuple[str, str]':
    """Create a fresh random API token for a remote build agent, written 0600
    to config/<name>.remote-token.  Returns (path, token).  The orchestrator
    ships the token to the remote (the agent gates every request on it) and
    sends it back as the X-Athena-Token header over the SSH tunnel."""
    import secrets
    _token = secrets.token_hex(32)
    _path = remote_token_path(config, name)
    with open(_path, 'w') as _fh:
        _fh.write(_token)
    os.chmod(_path, 0o600)
    return _path, _token


def remote_conf_path(config: 'BuildConfig') -> str:
    """Path to config/remote.conf — the UNTRACKED machine-local registry of
    remote build hosts (created by `container remote add`)."""
    return os.path.join(os.path.dirname(config.config_path), 'remote.conf')


def read_remote_conf(config: 'BuildConfig') -> 'configparser.ConfigParser':
    """Read config/remote.conf → ConfigParser; empty parser if absent.  A
    malformed file is swallowed here (write-side callers merge into a freshly
    read parser); BuildConfig.__init__ reads it strictly."""
    _parser = configparser.ConfigParser()
    try:
        _parser.read(remote_conf_path(config))
    except configparser.Error:
        pass
    return _parser


def _write_remote_conf(config: 'BuildConfig',
                       parser: 'configparser.ConfigParser') -> None:
    """Atomic-write a ConfigParser to config/remote.conf."""
    import io
    _path = remote_conf_path(config)
    _buf = io.StringIO()
    parser.write(_buf)
    os.makedirs(os.path.dirname(_path), exist_ok=True)
    _atomic_write_bytes(
        _path, _buf.getvalue().encode('utf-8'),
        mode=_existing_mode(_path),
    )


def list_remotes(config: 'BuildConfig') -> 'list[dict]':
    """Every configured remote as a dict: {name, host, type, ssh_key,
    max_parallel_builds, build_cpus, build_memory, local_mirror}.  Declaration
    order."""
    _parser = read_remote_conf(config)

    def _typed(getter: Callable, section: str, key: str, fb: Any) -> Any:
        # A malformed numeric/boolean (e.g. `MaxParallelBuilds = abc`) in this
        # UNTRACKED machine-local file raises a bare ValueError — which is NOT
        # caught by BuildConfig.__init__'s `except (configparser.Error, OSError)`
        # and would brick EVERY CLI command with a traceback.  Fall back to the
        # default instead.
        try:
            return getter(section, key, fallback=fb)
        except ValueError:
            return fb

    _out: 'list[dict]' = []
    for _section in _parser.sections():
        if not _section.startswith(_REMOTE_SECTION_PREFIX):
            continue
        _name = _section[len(_REMOTE_SECTION_PREFIX):]
        _out.append({
            'name':                _name,
            'host':                _parser.get(_section, 'Host', fallback=''),
            'type':                _parser.get(_section, 'Type', fallback='ssh'),
            'ssh_key':             _parser.get(_section, 'SshKey', fallback=''),
            'max_parallel_builds': _typed(
                _parser.getint, _section, 'MaxParallelBuilds', 1),
            'build_cpus':          _typed(
                _parser.getfloat, _section, 'BuildCpus', 0.0),
            'build_memory':        _parser.get(
                _section, 'BuildMemory', fallback=''),
            # Per-remote build-mirror toggle — whether `container
            # remote init` stages a snapshot-pinned build-closure apt mirror ON
            # this remote.  Defaults off; set at `container remote add`.
            'local_mirror':        _typed(
                _parser.getboolean, _section, 'LocalMirror', False),
        })
    return _out


def add_remote(config: 'BuildConfig', *, name: str, host: str,
               type: str = 'ssh', ssh_key: str = '',
               max_parallel_builds: int = 1, build_cpus: float = 0.0,
               build_memory: str = '', local_mirror: bool = False
               ) -> 'tuple[bool, str]':
    """Add/replace a [Remote.<name>] entry in config/remote.conf.  Returns
    (ok, detail)."""
    # Name becomes a config section + (optionally) a key filename component:
    # ASCII alphanumerics + '-'/'_', 1-64 chars.
    if not (isinstance(name, str) and 1 <= len(name) <= 64
            and all(_c.isalnum() or _c in '-_' for _c in name)):
        return (False, f"invalid remote name {name!r}")
    if not host.strip():
        return (False, "remote host is required")
    _parser = read_remote_conf(config)
    _section = f"{_REMOTE_SECTION_PREFIX}{name}"
    _replaced = _parser.has_section(_section)
    if not _replaced:
        _parser.add_section(_section)
    _parser.set(_section, 'Host', host.strip())
    _parser.set(_section, 'Type', type)
    _parser.set(_section, 'SshKey', ssh_key)
    _parser.set(_section, 'MaxParallelBuilds', str(max_parallel_builds))
    _parser.set(_section, 'BuildCpus', str(build_cpus))
    _parser.set(_section, 'BuildMemory', build_memory)
    _parser.set(_section, 'LocalMirror', 'true' if local_mirror else 'false')
    _write_remote_conf(config, _parser)
    return (True, f"{'updated' if _replaced else 'added'} remote {name!r} → {host}")


def delete_remote(config: 'BuildConfig', name: str) -> 'tuple[bool, str]':
    """Remove a [Remote.<name>] entry from config/remote.conf (config only —
    does not touch the remote host's Docker state; that's `purge`)."""
    _parser = read_remote_conf(config)
    _section = f"{_REMOTE_SECTION_PREFIX}{name}"
    if not _parser.has_section(_section):
        return (False, f"no such remote {name!r}")
    _parser.remove_section(_section)
    _write_remote_conf(config, _parser)
    return (True, f"deleted remote {name!r}")


def resolve_snapshot_timestamp(config: 'BuildConfig') -> Optional[str]:
    """Resolve the effective snapshot timestamp for this build.

    Returns None when snapshot pinning is disabled — call sites can pass
    the result straight to Mirror.with_snapshot() unconditionally.

    Resolution rules:
      - snapshot_enabled is False           → None
      - config/snapshot.state 'current' set → use it (operator override,
        durable; takes precedence over [Snapshot] Timestamp)
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

    # Operator-set durable current pin (config/snapshot.state) wins over
    # [Snapshot] Timestamp — this is how `snapshot select/advance` move the
    # pin without editing build.conf, and it survives `clean cache`.
    _state_current = read_snapshot_state(config).get('current', '')
    if _state_current and _SNAPSHOT_TS_RE.match(_state_current):
        return _state_current

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
                logger.warning(
                    f"Snapshot state file {state_file} contains invalid timestamp "
                    f"{persisted!r}; re-resolving"
                )
            except OSError as e:
                logger.warning(f"Cannot read {state_file}: {e}; re-resolving")

        # Cold path: ask the snapshot service
        ts = _query_snapshot_latest(
            config.snapshot_timestamp_api,
            config.snapshot_archive_keys,
        )
        try:
            with open(state_file, 'w') as fh:
                fh.write(ts + '\n')
            tui.console.print(f"Snapshot pin: resolved 'latest' → {ts}, persisted to {state_file}")
        except OSError as e:
            logger.warning(
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
    if not _validate_snapshot_timestamp(
            cfg_ts, config.mirrors,
            snapshot_baseurl=config.snapshot_baseurl):
        raise ValueError(
            f"Snapshot.Timestamp = {cfg_ts!r} does not cover all configured "
            "mirrors on the snapshot service (see prior log lines)"
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
    # Externalised snapshot endpoints — defaults target Debian's
    # snapshot.debian.org service.  Operators running a fork's own
    # snapshot mirror override via [Snapshot] BaseUrl / TimestampApi /
    # ArchiveKeys.
    snapshot_baseurl: str
    snapshot_timestamp_api: str
    snapshot_archive_keys: list[str]
    build_distribution: str
    build_base_id: str
    build_codename: str
    build_version: str
    build_mode: str   # 'distribution' (full corpus) | 'build' (build_pkg.list only)
    container_release: str
    docker_server: str

    skip_build_test: list[str]
    tunnel_packages: list[str]
    build_profiles: frozenset
    build_options: frozenset
    max_parallel_builds: int
    build_cpus: float
    build_memory: str
    heavy_packages: 'frozenset[str]'

    security_keyring: str
    security_disabled: bool

    error_str: str
    config_path: str

    dir_log: str
    dir_cache: str
    dir_temp: str
    dir_build_stage: str
    dir_source: str
    dir_repo: str
    # The dir_repo_* attrs point at the *.deb / *.udeb dirs in the
    # unified apt-repo layout (Stage D, 2026-05-22). Each
    # binary-<arch>/ subdir is also the location of its corresponding
    # Packages index (co-located per Q1).  See
    # docs/plans/conf-01-repo-layout-migration.md.
    dir_repo_main: str           # dists/<codename>/main/binary-<arch>/
    dir_repo_main_udeb: str      # dists/<codename>/main/debian-installer/binary-<arch>/
    dir_repo_main_source: str    # dists/<codename>/main/source/  (.dsc, .tar.*)
    dir_repo_doc: str            # dists/<codename>/doc/binary-<arch>/
    dir_repo_dbgsym: str         # dists/<codename>-debug/main/binary-<arch>/   (Q2 — debug suite)
    dir_repo_tests: str          # dists/<codename>/tests/binary-<arch>/
    dir_config: str
    dir_patch: str
    dir_gnupg: str
    dir_image: str
    dir_chroot: str
    dir_publish: str             # <working>/publish — staging tree for `mirror publish` (minimal subset)

    dir_patch_source: str
    dir_patch_preinstall: str
    dir_patch_postinstall: str
    dir_patch_empty: str

    _config_valid: bool

    def __init__(self) -> None:

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
            livelist_path = os.path.join(working_dir, 'config/live.list')
            installerlist_path = os.path.join(working_dir, 'config/installer.list')
            poollist_path = os.path.join(working_dir, 'config/pool.list')
            build_pkg_list_path = os.path.join(working_dir, 'config/build_pkg.list')

            parser = argparse.ArgumentParser(description='Dependency Parser - Athena Build System')
            parser.add_argument('--working-dir', type=str, help='Specify Working directory', required=False, default=working_dir)
            parser.add_argument('--config-file', type=str, help='Specify Configs File', required=False, default=config_path)
            parser.add_argument('--pkg-list', type=str, help='Specify user-selected pkg list', required=False, default=pkglist_path)
            parser.add_argument('--live-list', type=str, help='Specify live-only pkg list', required=False, default=livelist_path)
            parser.add_argument('--installer-list', type=str, help='Specify installer-only pkg list', required=False, default=installerlist_path)
            parser.add_argument('--pool-list', type=str, help='Specify pool-only pkg list (ship in apt pool, never installed)', required=False, default=poollist_path)
            parser.add_argument('--build-pkg-list', type=str, help='Specify build-mode pkg list (flat names, only consumed when [Build] Mode = build)', required=False, default=build_pkg_list_path)
            args = parser.parse_args()

            # if paths are specified, they are absolute
            self.working_dir = os.path.abspath(args.working_dir)
            self.config_path = os.path.abspath(args.config_file)
            self.pkglist_path = os.path.abspath(args.pkg_list)
            self.livelist_path = os.path.abspath(args.live_list)
            self.installerlist_path = os.path.abspath(args.installer_list)
            self.poollist_path = os.path.abspath(args.pool_list)
            self.build_pkg_list_path = os.path.abspath(args.build_pkg_list)

            if not os.access(self.config_path, os.R_OK):
                raise PermissionError(f'Config file is not readable: {self.config_path}')

        except (argparse.ArgumentError, OSError, SystemExit) as e:
            self.error_str = f"Failed to parse arguments: {e}"
            return

        # read config file(s).  The TRACKED config is split across two files:
        #   distro.conf — the upstream we build FROM + output identity
        #                 ([Build] identity, [Base], [Mirror.*], [Security],
        #                  [Audit], [Snapshot] service, [Directories])
        #   build.conf  — the build RECIPE ([Live], [Disk], [Packages],
        #                  [Source]) — the template an end user edits
        # configparser.read() merges a file list and silently skips absent
        # ones, so an older combined build.conf (or a test fixture that writes
        # every section into one file) keeps working unchanged.  --config-file
        # points at build.conf; distro.conf is its sibling.
        try:
            _distro_path = os.path.join(
                os.path.dirname(self.config_path), 'distro.conf')
            config_parser.read([_distro_path, self.config_path])
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
                    id        = _id,
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
            # The snapshot PIN is no longer tracked in config — it lives in the
            # UNTRACKED config/snapshot.state sidecar (set by `snapshot
            # select`).  Surface the durable pin here as the effective
            # timestamp so consumers (selection lockfile, displays,
            # resolve_snapshot_timestamp's caller) see the real frozen point,
            # not the 'latest' placeholder.  Read the sidecar by path (dir_config
            # isn't set yet); fall back to any legacy [Snapshot] Timestamp, then
            # 'latest'.  resolve_snapshot_timestamp applies the same precedence
            # at fetch time (with HEAD validation) — this keeps the cached
            # attribute consistent with it.
            _snap_cfg_ts = config_parser.get(
                'Snapshot', 'Timestamp', fallback='latest').strip()
            _snap_state_path = os.path.join(
                os.path.dirname(self.config_path), 'snapshot.state')
            _snap_state_cur = ''
            try:
                with open(_snap_state_path, encoding='utf-8') as _sfh:
                    _snap_state_cur = (json.load(_sfh) or {}).get('current', '')
            except (OSError, ValueError):
                _snap_state_cur = ''
            self.snapshot_timestamp_config = (
                _snap_state_cur
                if isinstance(_snap_state_cur, str)
                and _SNAPSHOT_TS_RE.match(_snap_state_cur)
                else _snap_cfg_ts)
            # Snapshot endpoints — defaults preserve current Debian
            # behaviour; overridable for forks / derivative distros
            # running their own snapshot mirror.
            self.snapshot_baseurl = config_parser.get(
                'Snapshot', 'BaseUrl',
                fallback='https://snapshot.debian.org/archive').rstrip('/')
            self.snapshot_timestamp_api = config_parser.get(
                'Snapshot', 'TimestampApi',
                fallback='https://snapshot.debian.org/mr/timestamp/').strip()
            _archive_keys_raw = config_parser.get(
                'Snapshot', 'ArchiveKeys',
                fallback='debian, debian-security')
            self.snapshot_archive_keys = [
                _k.strip() for _k in _archive_keys_raw.split(',') if _k.strip()
            ]
            # Three-layer identity (see memory/project_three_layer_identity.md):
            #   build_distribution — the DISTRIBUTION (user-visible name on
            #     /etc/os-release NAME, GRUB menu, hostname, lsb-release).
            #     E.g. "Asgard".
            #   build_base_id      — lowercased distribution, used for
            #     /etc/os-release ID and as the dpkg ID namespace.
            #     Derived from build_distribution; no separate config slot
            #     today (override path: add BASE_ID to [Build] if a future
            #     case needs ID independent of lower(NAME), e.g. openSUSE's
            #     "opensuse-leap").
            #   build_codename     — the RELEASE codename (Debian's
            #     "bookworm" equivalent), used for /etc/os-release
            #     VERSION_CODENAME and the apt suite name.  E.g. "thor".
            #
            # Athena-Build itself (the toolchain) is a separate layer not
            # represented in config — it's the identity of THIS repository
            # and shows in fork package-name prefixes (athena-*),
            # ATHENA_CODENAME env var, and Maintainer fields.
            self.build_distribution = _strip_quotes(config_parser.get('Build', 'DISTRIBUTION'))
            self.build_base_id      = self.build_distribution.lower()
            self.build_codename     = _strip_quotes(config_parser.get('Build', 'CODENAME'))
            self.build_version      = _strip_quotes(config_parser.get('Build', 'VERSION'))

            # CONTAINER_RELEASE lives under [Base] — the Debian release the
            # build container tracks.  Defaults to RELEASE so the container
            # tracks the target unless explicitly pinned behind it; a hardcoded
            # default would silently skew the image tag + dependency tree when
            # RELEASE is rebased.
            self.container_release = config_parser.get(
                'Base', 'CONTAINER_RELEASE', fallback=self.release)
            # Machine-local sidecar (config/local.conf, UNTRACKED): holds build
            # Mode, system Role, SetupComplete, builder Name, host build tuning
            # ([Build] MaxParallelBuilds/BuildCpus/BuildMemory/DOCKER_SERVER) and
            # the signing identity ([Repo] SigningKeyUid).  Read STRICTLY — a
            # malformed file is the operator's broken machine state and must
            # fail loudly.  Absent = fresh/un-onboarded checkout.  Read here
            # (before the machine-specific keys below) so those keys can take
            # local.conf with precedence over the tracked fallback.
            _local_conf = configparser.ConfigParser()
            _local_conf_path = os.path.join(
                os.path.dirname(self.config_path), 'local.conf')
            if os.path.isfile(_local_conf_path):
                try:
                    _local_conf.read(_local_conf_path)
                except configparser.Error as _e:
                    self.error_str = f"config/local.conf is malformed: {_e}"
                    return
            # DOCKER_SERVER (local Docker daemon endpoint) is machine-specific →
            # local.conf [Build]; the config_parser read is a back-compat
            # fallback for older combined build.conf checkouts.
            self.docker_server = _local_conf.get(
                'Build', 'DOCKER_SERVER',
                fallback=config_parser.get('Build', 'DOCKER_SERVER', fallback=''))
            # Remote build hosts (config/remote.conf, UNTRACKED): the registry
            # of ssh ship-to-host targets for `source remotebuild` (the build
            # runs THERE — Docker is local on the remote — distinct from
            # DOCKER_SERVER which would drive a remote daemon's API).  Each
            # remote carries its own build tuning so `source remotebuild all`
            # can fan packages out per host.  Managed via `container remote
            # add|list|delete`.  remote_build_host = the first configured
            # remote's host (single-remote convenience); [Build] RemoteBuildHost
            # is a legacy fallback for an un-migrated checkout.  Empty = remote
            # builds disabled.
            self.remotes = list_remotes(self)
            self.remote_build_host = (
                self.remotes[0]['host'] if self.remotes
                else config_parser.get('Build', 'RemoteBuildHost', fallback=''))
            # When true, depth-1 Recommends of selected packages are
            # pulled into selected_pkgs / selected_srcs (downloaded but not
            # built by default; not installed in chroot).  See build.conf for
            # the full operator-facing rationale.
            self.include_recommends = config_parser.getboolean(
                'Build', 'IncludeRecommends', fallback=True)
            # When true, the build closure — Build-Depends of every selected
            # source, resolved transitively — is added to the selection so the
            # toolchain is built from and served by our own mirror
            # (reproducible builds with no reach-out to snapshot.debian.org).
            # OFF by default; when on, the added set is segregated into
            # toolchain-core / language-stack / leaf build-dep tiers.
            self.include_build_closure = config_parser.getboolean(
                'Source', 'IncludeBuildClosure', fallback=False)
            # Docker client read-timeout (seconds).  Optional; the floor
            # below is generous so a multi-hour build's quiet phases
            # don't trip container.wait()/logs reads (the worker survives
            # via keep-polling regardless — see buildcontainer._wait_for_exit).
            self.docker_timeout = config_parser.getint(
                'Build', 'DockerTimeout', fallback=1800)
            # Build-mode switch.  `distribution` (default) drives the
            # full corpus through pkg.list/live.list/installer.list/pool.list
            # with runtime dep closure.  `build` works against just
            # `config/build_pkg.list` (flat list of package names) — no runtime
            # closure walk, no chroot/ISO; only the named packages get built
            # and published.  Used by team builders who own a subset of the
            # distribution rather than the whole thing.
            #
            # Precedence: local.conf [Local] Mode  >  build.conf [Build] Mode
            # >  'distribution'.  Mode is a per-machine decision (a peer picks
            # build or distribution), so the machine-local file wins; build.conf
            # carries no Mode line on a fresh checkout (back-compat fallback).
            _build_mode = config_parser.get(
                'Build', 'Mode', fallback='distribution')
            self.build_mode = (
                _local_conf.get('Local', 'Mode', fallback=None)
                or _build_mode).strip().lower()
            if self.build_mode not in ('distribution', 'build'):
                self.error_str = (
                    f"Mode must be 'distribution' or 'build', "
                    f"got {self.build_mode!r}"
                )
                return
            # Onboarding state, read from the same sidecar.  system_role is
            # 'first' | 'federation' | '' (un-onboarded); setup_complete gates
            # the first-run wizard (Phase 2) so it never re-prompts.
            self.system_role = (
                _local_conf.get('Local', 'Role', fallback='') or '').strip().lower()
            self.setup_complete = _local_conf.getboolean(
                'Local', 'SetupComplete', fallback=False)
            # When true, a snapshot-pinned local apt mirror of the build
            # closure is maintained under dir_localmirror; build containers add
            # it as their first apt source so build-deps come from local disk
            # instead of re-downloading from snapshot.debian.org every time.
            # Machine-local (per-machine bandwidth/disk choice); set by the
            # container-init prompt or `set create-local-mirror`.
            self.create_local_mirror = _local_conf.getboolean(
                'Local', 'CreateLocalMirror', fallback=False)
            # Build-system name — this machine's builder identity (e.g.
            # athena-primary), set at `configure`.  Machine-local only; empty on
            # a fresh/un-onboarded checkout.
            self.system_name = (
                _local_conf.get('Local', 'Name', fallback='') or '').strip()
            # default size for `iso build disk` output.  Sparse
            # qcow2 — actual on-disk footprint is much smaller (~chroot
            # size + metadata).  Operator overrides via `iso build disk
            # <n>` arg.
            self.disk_image_size_gb = config_parser.getint(
                'Build', 'DiskImageSizeGB', fallback=5,
            )

            # per-surface pkg.list group composition.
            # [Live] Groups / [Disk] Groups — comma/space-separated group
            # names whose closure (surfaces.surface_closure) gets installed
            # in that surface's chroot.  'base' is the minimal default;
            # the shipped build.conf sets Live to `base, gnome-desktop`.
            # Validated against pkg.list groups by the iso/chroot pre-flight.
            def _parse_groups(_section: str) -> 'set[str]':
                _raw = config_parser.get(_section, 'Groups', fallback='base')
                return {_g for _g in _raw.replace(',', ' ').split() if _g}
            self.live_groups = _parse_groups('Live')
            self.disk_groups = _parse_groups('Disk')
            # config/installer-defaults.list — the d-i install ROOTS
            # (grub metas, microcode, firmware, console-setup, VM tools):
            # packages d-i hooks apt-install onto /target at install time.
            # Feeds the installer-ISO pool manifest (with the tasksel
            # groups); pool.list keeps feeding build/publish roots.
            self.installer_defaults_path = os.path.join(
                working_dir, 'config/installer-defaults.list')
            # identity for the project's signing key — used by
            # generate_signing_key / verify_signing_key / print signing.
            # Format 'Name <email>'.  Machine-specific (each builder signs with
            # its own key) → local.conf [Repo]; config_parser is the back-compat
            # fallback, then a generic default for a fresh checkout.
            self.signing_key_uid = _local_conf.get(
                'Repo', 'SigningKeyUid',
                fallback=config_parser.get(
                    'Repo', 'SigningKeyUid',
                    fallback='Athena Build <athena@local>')
            ).strip()
            # optional network apt source for the INSTALLED system.
            # When set, build_chroot writes /etc/apt/sources.list.d/athena.list
            _skiptest_raw = config_parser.get('Source', 'SkipTest', fallback='')
            self.skip_build_test = [p.strip() for p in _skiptest_raw.split(',') if p.strip()]
            _tunneled_raw = config_parser.get('Source', 'Tunneled', fallback='')
            self.tunnel_packages: list[str] = [p.strip() for p in _tunneled_raw.split(',') if p.strip()]
            # BuildProfiles → DEB_BUILD_PROFILES (which Build-Depends a
            # source package activates at build time).
            # BuildOptions  → DEB_BUILD_OPTIONS  (how the build itself
            # behaves: nodoc, nocheck, parallel=N, …).
            # The two share names like nodoc / nocheck but are distinct
            # namespaces with distinct semantics.
            #
            # Backward compat: when BuildOptions is missing, mirror
            # BuildProfiles so existing build.conf files keep working.
            _profiles_raw = config_parser.get('Source', 'BuildProfiles', fallback='')
            self.build_profiles: frozenset[str] = frozenset(
                p.strip() for p in _profiles_raw.split(',') if p.strip()
            )
            _options_raw = config_parser.get('Source', 'BuildOptions', fallback='').strip()
            if _options_raw:
                self.build_options: frozenset[str] = frozenset(
                    p.strip() for p in _options_raw.split(',') if p.strip()
                )
            else:
                self.build_options = self.build_profiles

            # per-package overrides via [Source.<pkg>] sections.
            # Walk all sections of the form [Source.<pkg-name>] and capture
            # any BuildOptions / BuildProfiles keys.  Either key may be
            # absent; absent → fall back to the global value at lookup time
            # (see build_options_for / build_profiles_for accessors).
            # Mirrors the [Mirror.<id>] discovery pattern above.
            #
            # Example operator usage:
            #   [Source.firefox-esr]
            #   BuildOptions = nodoc, nocheck, parallel=1
            #
            # Surfaced 2026-05-15 when firefox-esr needed `parallel=1` to
            # keep cc1plus under the 16 GB OOM threshold; previously the
            # only escape was a per-source debian/rules patch.
            self.build_options_per_pkg: 'dict[str, frozenset[str]]' = {}
            self.build_profiles_per_pkg: 'dict[str, frozenset[str]]' = {}
            for _section in config_parser.sections():
                if not _section.startswith('Source.'):
                    continue
                _pkg = _section.split('.', 1)[1].strip()
                if not _pkg:
                    self.error_str = (
                        f"empty package name in [{_section}] — use "
                        f"[Source.<pkg>] (e.g. [Source.firefox-esr])"
                    )
                    return
                _po = config_parser.get(
                    _section, 'BuildOptions', fallback='').strip()
                if _po:
                    self.build_options_per_pkg[_pkg] = frozenset(
                        p.strip() for p in _po.split(',') if p.strip())
                _pp = config_parser.get(
                    _section, 'BuildProfiles', fallback='').strip()
                if _pp:
                    self.build_profiles_per_pkg[_pkg] = frozenset(
                        p.strip() for p in _pp.split(',') if p.strip())

            # parallel source-build pool size, per-container
            # resource caps, and heavy-package serialization list.
            # MaxParallelBuilds is capped at 8 to stay under docker-py's
            # default urllib3 pool size (10) — without the cap, parallel
            # workers can exhaust the pool and stall on streaming logs.
            # Set MaxParallelBuilds = 1 (the default) to disable
            # parallelism entirely and take the existing serial path.
            # Host tuning → local.conf [Build]; config_parser is the
            # back-compat fallback for older combined build.conf checkouts.
            self.max_parallel_builds = _local_conf.getint(
                'Build', 'MaxParallelBuilds',
                fallback=config_parser.getint(
                    'Build', 'MaxParallelBuilds', fallback=1))
            if self.max_parallel_builds < 1:
                self.error_str = (
                    f"[Build] MaxParallelBuilds must be >= 1, got "
                    f"{self.max_parallel_builds}"
                )
                return
            if self.max_parallel_builds > 8:
                self.error_str = (
                    f"[Build] MaxParallelBuilds must be <= 8 (docker-py's "
                    f"default urllib3 pool size is 10; higher values "
                    f"exhaust the pool under streaming logs), got "
                    f"{self.max_parallel_builds}"
                )
                return

            # BuildCpus / BuildMemory: per-container ceilings, passed to
            # docker-py as nano_cpus + mem_limit on containers.run().
            # Both optional; 0.0 / '' = no cap.  Operator guidance: on
            # a host with H cpus a sane starting point is BuildCpus =
            # (H // MaxParallelBuilds) * 0.8 — but the right value is
            # workload-dependent so we don't compute a default.
            self.build_cpus = _local_conf.getfloat(
                'Build', 'BuildCpus',
                fallback=config_parser.getfloat(
                    'Build', 'BuildCpus', fallback=0.0))
            if self.build_cpus < 0:
                self.error_str = (
                    f"[Build] BuildCpus must be >= 0.0, got "
                    f"{self.build_cpus}"
                )
                return
            self.build_memory = _local_conf.get(
                'Build', 'BuildMemory',
                fallback=config_parser.get(
                    'Build', 'BuildMemory', fallback='')).strip()

            # HeavyPackages: comma-separated source names treated as
            # "build alone" by the parallel scheduler — when the next
            # ready job is in this set, all in-flight builds drain
            # first; while it runs, no new builds start.  Empty = no
            # heavy-package serialization.  Lives in build.conf [Source]
            # (a build-recipe property); the [Build] read is a back-compat
            # fallback for older combined configs.
            _heavy = (
                config_parser.get('Source', 'HeavyPackages', fallback='')
                or config_parser.get('Build', 'HeavyPackages', fallback='')
            ).strip()
            self.heavy_packages = frozenset(
                p.strip() for p in _heavy.split(',') if p.strip())

            # Mirror InRelease GPG verification.  Default: enabled,
            # using the host-provided debian-archive-keyring.  Disabled
            # = true is for offline test fixtures and dev sandboxes only;
            # Cache.__get_files emits a per-build WARN when bypassed.
            self.security_keyring = config_parser.get(
                'Security', 'Keyring',
                fallback=_DEFAULT_SECURITY_KEYRING
            ).strip()
            self.security_disabled = config_parser.getboolean(
                'Security', 'Disabled', fallback=False
            )
            # opt-in build-dep audit gate.  When true, every
            # BuildContainer.build() runs `apt-get install --simulate` in a
            # transient container BEFORE the real install, captures the
            # resolved package set + versions, prints it to the operator,
            # and prompts y/n to proceed.  Useful for hostile-mirror
            # auditing where the operator wants to see what's about to land
            # in the build sandbox.  Default false (no behaviour change).
            self.audit_build_deps = config_parser.getboolean(
                'Security', 'AuditBuildDeps', fallback=False
            )
            # identity-residue scans (S1 + S3).  Default ON —
            # rebrand drift at upstream-rebase time fails loud at build
            # time instead of silently on the installed system.  Set
            # false for dev iteration on intentionally-incomplete forks.
            # S2 chroot apt-install audit is independent + always-on
            # (it concerns install-time correctness, not Debian identity).
            self.audit_identity_scan = config_parser.getboolean(
                'Audit', 'IdentityScan', fallback=True
            )
            # mutex: AuditBuildDeps is an interactive PROMPT_YESNO
            # inside BuildContainer.build() (buildcontainer.py:708).  Under
            # parallel workers, multiple prompts would collide on stdin.
            # Fail-closed at config load — these two are mutually exclusive.
            if self.audit_build_deps and self.max_parallel_builds > 1:
                self.error_str = (
                    "[Security] AuditBuildDeps and [Build] MaxParallelBuilds "
                    "> 1 are mutually exclusive — the audit gate is an "
                    "interactive prompt; parallel workers would collide on "
                    "stdin.  Set one or the other."
                )
                return
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
            self.dir_log = os.path.join(self.working_dir, config_parser.get('Directories', 'Log'))
            self.dir_cache = os.path.join(self.working_dir, config_parser.get('Directories', 'Cache'))
            self.dir_temp = os.path.join(self.working_dir, config_parser.get('Directories', 'Temp'))
            # per-worker scratch repo dir.  Each
            # BuildContainer.build() invocation creates
            # <dir_build_stage>/<uuid>/ and bind-mounts it as /repo
            # inside the build container, isolating one source's
            # emitted .debs from every other concurrent worker (would
            # otherwise race on `os.listdir(repo_path)` in segregate).
            # Survivors from killed runs are swept at BuildContainer
            # __init__.
            self.dir_build_stage = os.path.join(self.dir_temp, 'build-stage')
            self.dir_source = os.path.join(self.working_dir, config_parser.get('Directories', 'Source'))
            self.dir_repo = os.path.join(self.working_dir, config_parser.get('Directories', 'Repo'))
            # repo/ uses the apt-conformant unified layout
            # Stage D, 2026-05-22).  classify_repo_subdir's labels
            # ('main' / 'doc' / 'dbgsym' / 'tests') still apply but now
            # map to nested paths:
            #   main   → dists/<codename>/main/binary-<arch>/
            #   doc    → dists/<codename>/doc/binary-<arch>/
            #   tests  → dists/<codename>/tests/binary-<arch>/
            #   dbgsym → dists/<codename>-debug/main/binary-<arch>/
            #            (Q2 — separate debug suite per Debian convention)
            # Plus two siblings of main:
            #   main udebs   → dists/<codename>/main/debian-installer/binary-<arch>/
            #   main sources → dists/<codename>/main/source/  (.dsc, .tar.*)
            # All dirs are mkdir+writability checked below.
            # build_codename was already quote-normalised at assignment.
            _codename = self.build_codename
            self._codename_for_repo = _codename
            self.dir_repo_main = os.path.join(
                self.dir_repo, 'dists', _codename, 'main',
                f'binary-{self.arch}')
            self.dir_repo_main_udeb = os.path.join(
                self.dir_repo, 'dists', _codename, 'main',
                'debian-installer', f'binary-{self.arch}')
            self.dir_repo_main_source = os.path.join(
                self.dir_repo, 'dists', _codename, 'main', 'source')
            self.dir_repo_doc = os.path.join(
                self.dir_repo, 'dists', _codename, 'doc',
                f'binary-{self.arch}')
            self.dir_repo_dbgsym = os.path.join(
                self.dir_repo, 'dists', f'{_codename}-debug', 'main',
                f'binary-{self.arch}')
            self.dir_repo_tests = os.path.join(
                self.dir_repo, 'dists', _codename, 'tests',
                f'binary-{self.arch}')
            # Non-main components (ingested from Debian's contrib/non-free/
            # non-free-firmware mirrors, redistributed in OUR repo as real
            # components — license-correct separation).  Same shape as the
            # doc/tests pseudo-components: dists/<codename>/<comp>/binary-<arch>/.
            # Populated by component-aware routing (deb_dest_for_filename) for
            # tunneled/built packages whose origin Mirror.component is non-main.
            self.dir_repo_contrib = os.path.join(
                self.dir_repo, 'dists', _codename, 'contrib',
                f'binary-{self.arch}')
            self.dir_repo_non_free = os.path.join(
                self.dir_repo, 'dists', _codename, 'non-free',
                f'binary-{self.arch}')
            self.dir_repo_non_free_firmware = os.path.join(
                self.dir_repo, 'dists', _codename, 'non-free-firmware',
                f'binary-{self.arch}')
            self.dir_config = os.path.join(self.working_dir, config_parser.get('Directories', 'Config'))
            # [Packages] (build.conf) formalises the package-list filenames that
            # used to be hardcoded.  Apply them now that dir_config is known —
            # but ONLY when the CLI used the default path (an explicit
            # --pkg-list/--pool-list/… still wins).  A list named in [Packages]
            # is resolved relative to config/.  Absent section / key → the
            # hardcoded default filename, so nothing changes for old configs.
            if config_parser.has_section('Packages'):
                _pkg_list_map = (
                    ('pkglist_path',        'Pkg_List',       'pkg.list'),
                    ('livelist_path',       'Live_List',      'live.list'),
                    ('installerlist_path',  'Installer_List', 'installer.list'),
                    ('poollist_path',       'Pool_List',      'pool.list'),
                    ('build_pkg_list_path', 'Build_Pkg_List', 'build_pkg.list'),
                )
                for _attr, _key, _default_fn in _pkg_list_map:
                    _default_path = os.path.join(
                        working_dir, 'config', _default_fn)
                    if getattr(self, _attr) == _default_path:   # not CLI-overridden
                        setattr(self, _attr, os.path.join(
                            self.dir_config,
                            config_parser.get('Packages', _key,
                                              fallback=_default_fn)))
            self.dir_image = os.path.join(self.working_dir, config_parser.get('Directories', 'Image'))
            # The [Directories] Chroot value is the PARENT directory holding
            # both chroots.  Live lands
            # at <parent>/live and installer at <parent>/installer — siblings
            # under one root.  Operator overrides the parent via build.conf;
            # both child paths follow.  This keeps the live chroot OUTSIDE
            # the installer chroot's content tree (and vice versa) without
            # the parent-and-sibling-with-suffix shape the original Phase 5
            # used.
            self.dir_buildroot        = os.path.join(self.working_dir, config_parser.get('Directories', 'Chroot'))
            self.dir_chroot           = os.path.join(self.dir_buildroot, 'live')
            self.dir_chroot_installer = os.path.join(self.dir_buildroot, 'installer')
            # the DISK image's own minimal chroot ([Disk]
            # Groups closure) — decoupled from the live chroot so the two
            # surfaces can diverge (live = GNOME, disk = console).
            self.dir_chroot_disk      = os.path.join(self.dir_buildroot, 'disk')

            self.dir_patch = os.path.join(self.working_dir, config_parser.get('Directories', 'Patch'))
            self.dir_patch_source = os.path.join(self.dir_patch, 'source')
            self.dir_patch_preinstall = os.path.join(self.dir_patch, 'pre-install')
            self.dir_patch_postinstall = os.path.join(self.dir_patch, 'post-install')
            self.dir_patch_empty = os.path.join(self.dir_patch, 'empty')
            # staging tree for the minimal-publish
            # workflow (pool/ + dists/).  Derived (not a [Directories]
            # entry) — regenerated by `repo index minimal`, intended as
            # the source tree for a future `mirror publish --minimal`.
            self.dir_publish = os.path.join(self.working_dir, 'publish')

            # per-builder coordination tree.  Holds Ed25519
            # claim keypair (identity/), this builder's append-only
            # claim journal (claims/), and a fetched/ cache of the
            # remote repo-coord/ tree (P2+).  Sibling of dir_publish;
            # never served over HTTP (unlike dir_repo).  Derived path —
            # no [Directories] entry — so existing deployments don't
            # need a config-file edit before coord features land.
            self.dir_coord = os.path.join(self.working_dir, 'coord')
            self.dir_coord_identity = os.path.join(self.dir_coord, 'identity')
            self.dir_coord_claims = os.path.join(self.dir_coord, 'claims')
            self.dir_coord_fetched = os.path.join(self.dir_coord, 'fetched')

            # Athena-owned Debian source packages discovered + built
            # from local trees under <dir_fork_source>/<pkg>/ instead
            # of via `apt-get source <pkg>`.  Walked at cache-build
            # time; auto-included in source build.  See
            # docs/plans/fork-source.md.
            self.dir_fork = os.path.join(self.working_dir, config_parser.get('Directories', 'Fork'))
            self.dir_fork_source = os.path.join(self.dir_fork, 'source')
            # dpkg-source -b output dir; populated by scripts/fork_mirror.py
            # at cache-build time.  Not parsed for source trees (the walker
            # explicitly skips this dir under fork/source/).
            self.dir_fork_source_repo = os.path.join(self.dir_fork_source, 'repo')

            # Isolated gnupg homedir for InRelease verification.  The
            # build-system.sh bootstrap creates this with mode 0700;
            # mirror that here so a Python-only invocation (e.g. tests)
            # gets the same layout without depending on the bash script.
            self.dir_gnupg = os.path.join(self.working_dir, config_parser.get('Directories', 'Gnupg'))
            # Local build mirror — snapshot-pinned apt repo of the build
            # closure (see local_mirror.py / CreateLocalMirror).  Fallback keeps
            # older checkouts (no LocalMirror key) working.
            self.dir_localmirror = os.path.join(
                self.working_dir,
                config_parser.get('Directories', 'LocalMirror',
                                  fallback='localmirror'))

        except (configparser.Error, OSError) as e:
            self.error_str = str(e)
            return

        try:
            if not os.access(self.working_dir, os.W_OK):
                raise PermissionError(f'Working directory is not writable: {self.working_dir}')

            pathlib.Path(self.dir_log).mkdir(parents=True, exist_ok=True)

            # five features write here (snapshot/selection/manifest/
            # mirror state, api.key) — ensure + writability-check it like the rest.
            pathlib.Path(self.dir_config).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_cache).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_temp).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_build_stage).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_repo).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_localmirror).mkdir(parents=True, exist_ok=True)
            for _sub in (
                self.dir_repo_main, self.dir_repo_main_udeb,
                self.dir_repo_main_source,
                self.dir_repo_doc, self.dir_repo_dbgsym, self.dir_repo_tests,
                self.dir_repo_contrib, self.dir_repo_non_free,
                self.dir_repo_non_free_firmware,
            ):
                pathlib.Path(_sub).mkdir(parents=True, exist_ok=True)

            # fix-up: if any of the pre-
            # Stage-D dir names exist at the repo root and are empty,
            # rmdir them.  Stage C's migration removed them once but
            # a pre-Stage-D version of this very block would have
            # silently recreated them on the next session.  Now we
            # actively clean up.  Skip non-empty dirs: those weren't
            # successfully migrated and the operator should investigate
            # manually before they're cleaned up.
            for _legacy in ('main', 'doc', 'dbgsym', 'tests'):
                _path = os.path.join(self.dir_repo, _legacy)
                try:
                    if os.path.isdir(_path) and not os.listdir(_path):
                        os.rmdir(_path)
                except OSError:
                    # Best-effort: a permission error or race
                    # condition shouldn't fail BuildConfig init.
                    pass

            # fix-up: stale audit-Packages cache files
            # in dir_temp (pre-Stage-D filename shape: no path-hash
            # suffix).  These could serve empty cached packages
            # against the new (populated) layout until the operator
            # ran `repo audit refresh`.  Hashing the path into
            # the filename eliminates new collisions; this loop
            # cleans up the old uncached files left behind.
            try:
                for _f in os.listdir(self.dir_temp):
                    # Old shape: 'audit-Packages-<subdir>' (no hash)
                    # New shape: 'audit-Packages-<subdir>-<hash>'
                    if _f.startswith('audit-Packages-') and _f.count('-') == 2:
                        try:
                            os.remove(os.path.join(self.dir_temp, _f))
                        except OSError:
                            pass
            except OSError:
                pass

            pathlib.Path(self.dir_patch).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_empty).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_preinstall).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_postinstall).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_fork).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_fork_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_fork_source_repo).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_image).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_publish).mkdir(parents=True, exist_ok=True)
            # create coord/ + substructure.  identity/ is
            # mode 0700 so the private claim key isn't world-readable
            # (mirrors dir_gnupg's 0700 discipline).
            pathlib.Path(self.dir_coord).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_coord_identity).mkdir(
                parents=True, mode=0o700, exist_ok=True)
            os.chmod(self.dir_coord_identity, 0o700)
            pathlib.Path(self.dir_coord_claims).mkdir(
                parents=True, exist_ok=True)
            pathlib.Path(self.dir_coord_fetched).mkdir(
                parents=True, exist_ok=True)
            pathlib.Path(self.dir_buildroot).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_chroot).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_chroot_installer).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_chroot_disk).mkdir(parents=True, exist_ok=True)

            # gpg refuses any homedir whose mode is broader than 0700,
            # so create with mode 0700 directly.  os.makedirs respects
            # the umask, so we follow with an explicit chmod to be safe
            # on hosts whose umask leaves the dir 0755.
            pathlib.Path(self.dir_gnupg).mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(self.dir_gnupg, 0o700)

            for _dir in (
                self.dir_log, self.dir_config, self.dir_cache, self.dir_temp,
                self.dir_build_stage,
                self.dir_source, self.dir_repo, self.dir_localmirror,
                self.dir_patch, self.dir_patch_empty,
                self.dir_patch_source, self.dir_patch_preinstall, self.dir_patch_postinstall,
                self.dir_fork, self.dir_fork_source, self.dir_fork_source_repo,
                self.dir_image, self.dir_publish, self.dir_buildroot, self.dir_chroot,
                self.dir_chroot_installer, self.dir_chroot_disk, self.dir_gnupg,
                self.dir_coord, self.dir_coord_identity,
                self.dir_coord_claims, self.dir_coord_fetched,
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

    def deb_dir_for(self, label: str) -> str:
        """Map a repo subdir label to its on-disk dir under the
        Stage D unified apt-repo layout.

        Recognised labels:
          main       → dists/<codename>/main/binary-<arch>/        (.deb)
          main-udeb  → dists/<codename>/main/debian-installer/
                        binary-<arch>/                              (.udeb)
          doc        → dists/<codename>/doc/binary-<arch>/
          dbgsym     → dists/<codename>-debug/main/binary-<arch>/
          tests      → dists/<codename>/tests/binary-<arch>/

        The four non-`main-udeb` labels match `classify_repo_subdir`
        output; `main-udeb` is the parallel udeb namespace inside main,
        added so `repo_audit.scan_repo_state` can produce a RepoState
        for the udeb side (used by `source verify`).

        Replaces the pre idiom of
        `os.path.join(config.dir_repo, label)` — which constructed
        flat paths like repo/main/.
        """
        _mapping = {
            'main':             self.dir_repo_main,
            'main-udeb':        self.dir_repo_main_udeb,
            'doc':              self.dir_repo_doc,
            'dbgsym':           self.dir_repo_dbgsym,
            'tests':            self.dir_repo_tests,
            'contrib':          self.dir_repo_contrib,
            'non-free':         self.dir_repo_non_free,
            'non-free-firmware': self.dir_repo_non_free_firmware,
        }
        if label not in _mapping:
            raise ValueError(
                f"unknown repo subdir label: {label!r} "
                f"(expected one of {sorted(_mapping)})"
            )
        return _mapping[label]

    def deb_dest_for_filename(self, filename: str,
                              component: str = 'main') -> str:
        """Compose classify_repo_subdir + deb_dir_for, with the udeb
        special-case (udebs route to main's debian-installer/ sibling,
        not main/binary-<arch>/).

        The one helper callers (buildcontainer's _segregate, audits,
        cleanups) should use to find "where does THIS .deb/.udeb go?"
        — eliminates path-construction at the call site.

        `component` is the package's apt component, derived from its origin
        Mirror (NOT by inspecting the .deb).  For a non-main component
        (contrib / non-free / non-free-firmware), a plain installable .deb
        (suffix-label 'main') routes to that component's dir instead of main.
        Side-artifact suffixes (-doc/-dbgsym/-test) keep their existing
        pooled dirs regardless of component (we don't ship non-main side
        artifacts).  Default 'main' keeps every existing caller unchanged.
        """
        _label = classify_repo_subdir(filename)
        if filename.endswith('.udeb') and _label == 'main':
            return self.dir_repo_main_udeb
        if component != 'main' and _label == 'main':
            return self.deb_dir_for(component)
        return self.deb_dir_for(_label)

    def all_deb_dirs(self) -> 'list[str]':
        """All on-disk dirs that hold .deb / .udeb files.  For repo
        walks (cmd_strip_repo, cmd_package_cleanup, cmd_audit_nmu,
        etc.) that need to find every binary artifact.

        Replaces the pre idiom of `for sub in _REPO_SUBDIRS:
        join(dir_repo, sub)`.  Order is stable (binaries first, udebs
        next, then doc/dbgsym/tests).

        Derived from the single `_STALE_SCAN_SUBDIRS` canon so
        this (strip) walk and `_scan_stale_files` (cleanup / gate) cover
        exactly the same build-output components and can't drift apart.
        Pristine tunneled binaries (contrib / non-free / non-free-firmware)
        stay excluded — maintenance walks must not touch them.
        """
        return [self.deb_dir_for(_label) for _label in _STALE_SCAN_SUBDIRS]

    def build_options_for(self, pkg_name: str) -> 'frozenset[str]':
        """Return the effective DEB_BUILD_OPTIONS set for a source pkg.

        Precedence (most-specific first):
          1. `[Source.<pkg>] BuildOptions = …` if present in build.conf
          2. global `[Source] BuildOptions = …`

        `[Source] SkipTest = a, b` then UNIONS `nocheck` in for the listed
        packages — it suppresses that package's test suite whatever the
        options above say.  (This is SkipTest's sole consumer; the old
        per-Source `skip_test` flag lost its reader in c61b0e6 and shipped
        dead until 2026-07-07 — vim built WITH tests despite being listed.)

        A per-invocation override (the `[nocheck]` bracket-token on
        `source build foo [nocheck]`) is layered ON TOP of this at the call
        site (buildcontainer.build's `options_override` kwarg) — that path
        is not visible here.
        """
        _opts = self.build_options_per_pkg.get(pkg_name, self.build_options)
        if pkg_name in self.skip_build_test:
            _opts = _opts | {'nocheck'}
        return _opts

    def build_profiles_for(self, pkg_name: str) -> 'frozenset[str]':
        """Return the effective DEB_BUILD_PROFILES set for a source pkg.
        Same precedence as build_options_for — see that docstring."""
        return self.build_profiles_per_pkg.get(pkg_name, self.build_profiles)

    def error(self) -> str:
        """
        Returns:
            str: Error string if config is invalid, empty string otherwise
        """
        return self.error_str

def download_file(url: str, filename: str) -> tuple:
    """Downloads file and updates progressbar in incremental manner.

    Args:
        url: URL to download from.  Supports http(s)://, https://, and
             file:// schemes.  file:// is a local copy fast-path used
             primarily for fork mirror metadata (see scripts/fork_mirror.py).
        filename: Local path to write to; location must be writable.

    Returns:
        (size, detail) — size is bytes on success or -1 on failure;
        detail is '' on success or a short human-readable cause on
        failure (e.g. 'HTTP 404 Not Found', 'connection timeout',
        'OS write error: ...').  Callers should surface detail in
        their own error_str so the operator sees the actual reason
        rather than a generic "download failed".
    """
    from urllib.parse import urlsplit, unquote
    # IMPORTANT: import requests.ConnectionError as RequestsConnectionError
    # — Python's builtin `ConnectionError` is NOT a base class of requests'
    # ConnectionError (they both extend OSError but have no subclass
    # relationship).  A bare `except ConnectionError` in this scope binds
    # to the builtin and silently misses every requests-raised connection
    # reset / connection aborted error — including the TCP RST that
    # snapshot.debian.org issues under load.  Caught 2026-06-02: retry
    # loop appeared to work for ReadTimeout but never retried connection
    # resets, so the user saw a single "download failed" with no retry
    # log lines.
    from requests import (
        Timeout, TooManyRedirects, HTTPError, RequestException,
        ConnectTimeout, ReadTimeout,
    )
    from requests.exceptions import ConnectionError as RequestsConnectionError

    # file:// fast-path: local copy via shutil.copy, no HTTP, no progress
    # bar (transfers are small + instant).  Returns same (size, detail)
    # contract as the HTTP path.
    _parts = urlsplit(url)
    if _parts.scheme == 'file':
        _src_path = unquote(_parts.path)
        try:
            if not os.path.isfile(_src_path):
                return -1, f"file:// source missing: {_src_path}"
            _size = os.path.getsize(_src_path)
            import shutil as _shutil
            _shutil.copyfile(_src_path, filename)
            return _size, ''
        except OSError as e:
            _detail = f"file:// copy error: {e}"
            logger.error(f"download_file({url}): {_detail}")
            return -1, _detail

    import time as _time

    progress_bar = None   # one bar across all retries; closed in finally
    try:
        name_strip: str = urlsplit(url).path.split('/')[-1].ljust(15, ' ')

        # Retry loop for transient network failures.  snapshot.debian.org's
        # CDN routinely stalls on cold-cache files for the entire timeout
        # budget, then serves the same URL in milliseconds on the next
        # attempt (different edge node, or cold fetch completed during
        # backoff).  Retries are gated to the three exception classes that
        # actually represent transient failures; HTTPError / OSError /
        # ValueError propagate to the outer except blocks unchanged.
        for _attempt in range(_HTTP_RETRY_COUNT):
            try:
                # Probe size via HEAD first (kept per memory note: pre-`f1cf373`
                # GET-only path returned 0 for some Debian mirrors and crashed the
                # bar with maxvalue=0).  Take the larger of HEAD / GET content-length
                # so whichever one knows wins, and fall back to a 1 MB seed if both
                # are 0 — the bar will expand dynamically as bytes arrive.
                head = _http_session().head(
                    url, timeout=_HTTP_TIMEOUT_FAST, headers=_HTTP_HEADERS)
                head_size = int(head.headers.get('content-length', 0))

                with _http_session().get(
                        url, stream=True, timeout=_HTTP_TIMEOUT_STREAM,
                        headers=_HTTP_HEADERS) as response:
                    response.raise_for_status()
                    get_size = int(response.headers.get('content-length', 0))
                    expected_size = max(head_size, get_size) or (1 << 20)
                    current_max  = expected_size

                    # build the bar ONCE; on a retry reuse + reset it
                    # (set_max un-freezes via) so each failed attempt
                    # doesn't leak a permanent widget row + its 10 Hz polling.
                    if progress_bar is None:
                        progress_bar = tui.ProgressBar(
                            label=name_strip, itr_label='B/s',
                            maxvalue=expected_size)
                    else:
                        progress_bar.reset()
                        progress_bar.set_max(expected_size)

                    bytes_written = 0
                    with open(filename, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                bytes_written += len(chunk)
                                # If the stream out-runs the size hint (HEAD/GET
                                # under-reported, or both were 0), grow the max so
                                # the bar keeps animating instead of freezing at
                                # 100% partway through.
                                if bytes_written > current_max:
                                    current_max = int(bytes_written * 1.25)
                                    progress_bar.set_max(current_max)
                                progress_bar.step(len(chunk))

                    # Final correction so the persisted display matches reality
                    # (in case the size hint over-reported and the bar stopped short).
                    progress_bar.set_max(bytes_written)
                    return bytes_written, ''   # bar closed in finally

            except (ConnectTimeout, ReadTimeout, RequestsConnectionError) as e:
                if _attempt < _HTTP_RETRY_COUNT - 1:
                    _backoff = 2 ** _attempt   # 1s, 2s
                    logger.warning(
                        f"download_file({url}): "
                        f"{type(e).__name__} on attempt {_attempt + 1}/"
                        f"{_HTTP_RETRY_COUNT}, retrying after {_backoff}s")
                    tui.console.print(
                        f"  retry {_attempt + 1}/{_HTTP_RETRY_COUNT - 1} after "
                        f"{_backoff}s ({type(e).__name__})")
                    _time.sleep(_backoff)
                    continue
                # Last attempt failed — propagate to the outer except
                # so the operator sees the right error classification.
                raise

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
        logger.error(f"download_file({url}): {_detail}")
        return -1, _detail
    except ConnectTimeout as e:
        # TCP handshake didn't complete within the connect timeout —
        # mirror is unreachable from this host (firewall, dead route,
        # DNS lying).  Distinct from ReadTimeout: bumping the read
        # timeout won't help; fix the network or change the mirror.
        _detail = (
            f"connect timeout (TCP handshake failed in "
            f"{_HTTP_TIMEOUT_FAST}s — mirror unreachable from this host): {e}"
        )
        tui.console.print(f"ERROR: connect timeout for {url}")
        logger.error(f"download_file({url}): {_detail}")
        return -1, _detail
    except ReadTimeout as e:
        # Connection established but server stopped sending bytes
        # before the read timeout.  snapshot.debian.org does this for
        # cold-cache files; raise [Snapshot] timeouts or retry.
        _detail = (
            f"read timeout (server stalled mid-response — "
            f"raise the read timeout if this is snapshot.debian.org): {e}"
        )
        tui.console.print(f"ERROR: read timeout for {url}")
        logger.error(f"download_file({url}): {_detail}")
        return -1, _detail
    except (RequestsConnectionError, Timeout, TooManyRedirects, RequestException) as e:
        _detail = f"{type(e).__name__}: {e}"
        tui.console.print(f"ERROR: download failed for {url}")
        logger.error(f"download_file({url}): {_detail}")
        return -1, _detail
    except OSError as e:
        _detail = f"OS write error: {e}"
        tui.console.print(f"ERROR: cannot write to {filename}")
        logger.error(f"download_file write {filename}: {e}")
        return -1, _detail
    except ValueError as e:
        _detail = f"malformed response: {e}"
        tui.console.print(f"ERROR: malformed response from {url}")
        logger.error(f"download_file parse {url}: {e}")
        return -1, _detail
    except Exception as e:
        _detail = f"{type(e).__name__}: {e}"
        tui.console.print(f"ERROR: unexpected failure downloading {url}")
        logger.error(f"download_file({url}): {_detail}")
        return -1, _detail
    finally:
        # close the single bar on EVERY exit — success, retry
        # exhaustion, or a propagated exception — not just the success path.
        if progress_bar is not None:
            progress_bar.close()
    # Defensive: the retry loop above either returns (success) or
    # raises into one of the except blocks (failure).  mypy can't see
    # that, so we add an unreachable fallback so the function's return
    # contract is provably satisfied.
    return -1, 'retry loop exited without return — unreachable'


def download_source(dependency_tree: 'dependencytree.DependencyTree',
                    dst_dir: str) -> int:
    from urllib.parse import urljoin, urlsplit, unquote
    from requests import Timeout, TooManyRedirects, HTTPError, RequestException
    import shutil as _shutil

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
            logger.error(f"download_source: source {_pkg_name} has no _mirror — cache ingest bug")
            continue
        _file_list.update(_src.files)
        for _fname in _src.files:
            _file_mirror[_fname] = _src._mirror

    _index = 1
    _skipped = 0
    _total = len(_file_list)

    # Two stacked bars: a CUMULATIVE bar (package count in the label, total
    # bytes as value/total, NO rate — a cumulative B/s figure is misleading,
    # it averages over already-downloaded files) kept persistent at the end;
    # and a PER-FILE bar (filename + that file's size, WITH the real per-file
    # rate) reset for each file and removed once all downloads finish.
    try:
        _cumulative_bar = ProgressBar(
            label=f'(0/{_total})', maxvalue=max(1, _download_size),
            show_rate=False)
        _file_bar = ProgressBar(
            label='', itr_label='B/s', maxvalue=1, show_rate=True)
    except Exception as e:
        _cumulative_bar = None
        _file_bar = None
        tui.console.print("WARNING: progress bar unavailable, continuing without it")
        logger.error(f"download_source ProgressBar: {type(e).__name__}: {e}")

    for _file in _file_list:
        _expected_size = int(_file_list[_file]['size'])
        if _cumulative_bar is not None:
            _cumulative_bar.label(f'({_index}/{_total})')
        if _file_bar is not None:
            # set_max + reset zeroes the value AND the timer, so the rate
            # shown is THIS file's rate, not a cumulative average.
            _file_bar.set_max(max(1, _expected_size))
            _file_bar.reset()
            _file_bar.label(_file[:20])

        _mirror = _file_mirror[_file]
        _base_url = _mirror.url + '/'
        _url = urljoin(_base_url, _file_list[_file]['path'])
        _sha256 = _file_list[_file]['sha256']
        _download_path = os.path.join(dst_dir, _file)

        if get_sha256(_download_path) != _sha256:
            # file:// fast-path for fork mirror — local copy via shutil
            # instead of HTTP.  Fork's Sources index ships real SHA256 +
            # Size (helper computed them at cache time), so the existing
            # verification below still gates the copy.  See
            # scripts/fork_mirror.py.
            _parts = urlsplit(_url)
            if _parts.scheme == 'file':
                _src_path = unquote(_parts.path)
                try:
                    _shutil.copyfile(_src_path, _download_path)
                except OSError as e:
                    tui.console.print(f"ERROR: file:// copy failed for {_url}")
                    logger.error(f"download_source({_url}): {e}")
                    continue
                try:
                    _sz = os.path.getsize(_download_path)
                    if _cumulative_bar is not None:
                        _cumulative_bar.step(_sz)
                    if _file_bar is not None:
                        _file_bar.step(_sz)
                except OSError:
                    pass
                # Fall through to the size + sha256 verification below
                # (which gates _file_list[_file] for downstream consumers).
            else:
                # Use the size from the InRelease-verified Sources index instead
                # of a HEAD probe — saves one round-trip per file and removes
                # the prior bug where a HEAD that returned 0 still produced
                # `_downloaded_size += 0` while the GET silently 404ed.
                #
                # Retry on transient failures (ReadTimeout, ConnectTimeout,
                # ConnectionError) — snapshot.debian.org stalls
                # intermittently on cold-cache files and a single retry
                # usually clears it.  HTTPError is NOT retried (4xx/5xx are
                # deterministic).  Matches download_file's retry policy.
                import time as _time
                from requests.exceptions import (
                    ConnectionError as _ReqConnErr,
                    ReadTimeout as _ReqReadTimeout,
                    ConnectTimeout as _ReqConnTimeout,
                )
                _retry_failed = False
                for _attempt in range(_HTTP_RETRY_COUNT):
                    try:
                        with _http_session().get(
                                _url, stream=True,
                                timeout=_HTTP_TIMEOUT_STREAM) as response:
                            response.raise_for_status()
                            with open(_download_path, 'wb') as f:
                                for chunk in response.iter_content(chunk_size=8192):
                                    if chunk:
                                        f.write(chunk)
                                        if _cumulative_bar is not None:
                                            _cumulative_bar.step(len(chunk))
                                        if _file_bar is not None:
                                            _file_bar.step(len(chunk))
                        break  # success
                    except (_ReqConnErr, _ReqReadTimeout, _ReqConnTimeout) as e:
                        if _attempt < _HTTP_RETRY_COUNT - 1:
                            _backoff = 2 ** _attempt
                            logger.warning(
                                f"download_source({_url}): "
                                f"{type(e).__name__} on attempt {_attempt + 1}/"
                                f"{_HTTP_RETRY_COUNT}, retrying after {_backoff}s")
                            _time.sleep(_backoff)
                            continue
                        tui.console.print(
                            f"ERROR: HTTP failure for {_url} (after retries)")
                        logger.error(f"download_source({_url}): {e}")
                        _retry_failed = True
                        break
                    except (Timeout, TooManyRedirects, HTTPError, RequestException) as e:
                        tui.console.print(f"ERROR: HTTP failure for {_url}")
                        logger.error(f"download_source({_url}): {e}")
                        _retry_failed = True
                        break
                    except OSError as e:
                        tui.console.print(f"ERROR: cannot write {_download_path}")
                        logger.error(f"download_source write {_download_path}: {e}")
                        _retry_failed = True
                        break
                    except ValueError as e:
                        tui.console.print(f"ERROR: malformed response for {_url}")
                        logger.error(f"download_source parse {_url}: {e}")
                        _retry_failed = True
                        break
                if _retry_failed:
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
                logger.error(f"download_source stat {_download_path}: {e}")
                continue

            if _expected_size > 0 and _on_disk != _expected_size:
                tui.console.print(
                    f"ERROR: short download for {_file} — "
                    f"got {_on_disk} bytes, expected {_expected_size}"
                )
                logger.error(
                    f"short_download {_url}: {_on_disk}/{_expected_size} bytes"
                )
                continue

            if get_sha256(_download_path) != _sha256:
                tui.console.print(f"ERROR: Hash mismatch for {_file} — download may be corrupt")
                logger.error(f"sha256 mismatch: {_download_path} expected {_sha256}")
                continue

            _downloaded_size += _on_disk

        else:
            _skipped += 1
            if _cumulative_bar is not None:
                _cumulative_bar.step(_expected_size)
            if _file_bar is not None:
                _file_bar.step(_expected_size)
            _downloaded_size += _expected_size

        _index += 1

    # Per-file bar is transient — remove it; keep the cumulative summary line.
    if _file_bar is not None:
        _file_bar.close(persist=False)
    if _cumulative_bar is not None:
        _cumulative_bar.close(persist=True)

    tui.console.print(f"Downloading {_total - _skipped} files, Skipped {_skipped} files")
    return _downloaded_size


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
        logger.warning(f"get_md5: cannot read {filepath}: {e}")
        return ''


def _compute_sha256(filepath: str) -> str:
    """Compute SHA-256 of `filepath` — pure I/O, no caching.  Returns
    empty string on missing file or read error (logged as warning).

    Separate from `get_sha256` so the cache layer in that function
    has a clean monkey-patch target for the unit tests (tests assert
    cache hits by replacing this with a counter)."""
    if not os.path.isfile(filepath):
        return ''
    try:
        with open(filepath, 'rb') as f:
            return hashlib.file_digest(f, 'sha256').hexdigest()
    except OSError as e:
        logger.warning(f"_compute_sha256: cannot read {filepath}: {e}")
        return ''


def get_sha256(filepath: str, use_cache: bool = True) -> str:
    """Return the SHA-256 hash of `filepath` (hex digest), empty
    string on missing file / read error.

    With `use_cache=True` (default), checks `<filepath>.verified` for
    a cached hash recorded against the file's `(size, mtime_ns)`.
    When the recorded pair matches the current file stat, returns
    the cached hash without re-reading the file — a no-op verify
    run over hundreds of source tarballs drops from ~30 seconds of
    To milliseconds of `stat()`+sidecar-read.

    Sidecar format — single line, space-separated:

        <size_bytes> <mtime_ns> <sha256_hex>

    Cache miss (sidecar missing OR malformed OR (size, mtime_ns)
    don't match) → recomputes via `_compute_sha256`, writes a fresh
    sidecar.  Sidecar write failures are non-fatal: the computed
    hash is returned and the next call simply recomputes again.

    Cache invariant: `(size, mtime_ns)` unchanged ⇒ file content
    unchanged.  Holds in our pipeline — downloads write fresh files
    with new mtime; we never edit in place.  Operators who replace
    a file out-of-band (e.g. manual `cp --preserve=timestamps`) must
    also delete `<file>.verified` to force a recompute.

    `use_cache=False` forces a fresh compute AND does not touch the
    sidecar — for callers that just wrote the file and want to
    round-trip it from disk to confirm what was written.
    """
    if not use_cache:
        return _compute_sha256(filepath)

    if not os.path.isfile(filepath):
        return ''

    try:
        _stat = os.stat(filepath)
        _size = _stat.st_size
        _mtime_ns = _stat.st_mtime_ns
    except OSError as e:
        logger.warning(f"get_sha256: cannot stat {filepath}: {e}")
        return ''

    _sidecar = filepath + '.verified'
    try:
        with open(_sidecar, 'r', encoding='utf-8') as fh:
            _parts = fh.readline().strip().split()
        if (len(_parts) == 3
                and int(_parts[0]) == _size
                and int(_parts[1]) == _mtime_ns):
            return _parts[2]
    except (OSError, ValueError):
        # Sidecar missing or malformed — fall through to recompute.
        pass

    _sha = _compute_sha256(filepath)
    if _sha:
        try:
            with open(_sidecar, 'w', encoding='utf-8') as fh:
                fh.write(f"{_size} {_mtime_ns} {_sha}\n")
        except OSError as e:
            # Non-fatal: caller still gets the correct hash; next
            # invocation will recompute and try the sidecar write again.
            logger.debug(
                f"get_sha256: cannot write sidecar {_sidecar}: {e}"
            )
    return _sha


def parse_build_pkg_list(path: str) -> 'list[str]':
    """Parse `config/build_pkg.list` — flat list of binary package
    names, one per line, `#` comments and blank lines tolerated.

    No grouping: build mode is narrow-scope (one team owns this list),
    grouping is overkill.  Names are stripped of whitespace and
    de-duplicated while preserving first-seen order (so the operator
    can read `mirror summary` deterministically against the file order).

    Returns an empty list when the file is absent — caller checks
    `[Build] Mode == 'build'` separately to decide whether an empty
    list is fatal.  Raises OSError on unreadable file (caller surfaces).
    """
    if not os.path.isfile(path):
        return []
    _raw = readfile(path)
    _seen: 'set[str]' = set()
    _out: 'list[str]' = []
    for _l in _raw.splitlines():
        _name = _l.strip()
        if not _name or _name.startswith('#'):
            continue
        # Inline comment after a name: `firefox-esr # OOMs on host A`
        if '#' in _name:
            _name = _name.split('#', 1)[0].strip()
            if not _name:
                continue
        if _name in _seen:
            continue
        _seen.add(_name)
        _out.append(_name)
    return _out


def parse_pkg_list_groups(
        path: str,
        lines: 'Optional[list[str]]' = None) -> 'dict[str, list[str]]':
    """Parse a pkg.list file into named groups.

    Two supported layouts:

    1. **Flat** (legacy, no `[section]` markers) — every non-comment,
       non-blank line is a seed.  Treated as a single `[base]` group so
       older configs without groups still parse cleanly.

    2. **INI-style** — `[group_name]` headers split the file into named
       groups, each containing one seed name per line.  Comments (`#`)
       and blank lines are allowed within sections; group names are
       free-form identifiers (no validation beyond non-empty).

    Returns a dict mapping group name → list of seed names (declaration
    order preserved, which `dict[str, ...]` guarantees in Python 3.7+).

    Raises:
        ValueError: in INI-style mode, a seed appears before any
        `[section]` header (operator probably forgot `[base]`).
        OSError: path is unreadable.

    ``lines`` lets a caller that already read the file (e.g. one calling both
    this and ``parse_pkg_list_group_meta``) pass the split lines so the file is
    read once; ``path`` is still used for error messages.
    """
    _lines = readfile(path).splitlines() if lines is None else lines

    # First pass: does the file contain ANY `[section]` header?  Decides
    # which mode to parse in.
    _section_re = re.compile(r'^\s*\[([^\]]*)\]\s*$')
    _has_sections = any(_section_re.match(_l) for _l in _lines)

    if not _has_sections:
        # Flat mode — every package becomes a [base] seed.
        _seeds = []
        for _l in _lines:
            _name = _l.strip()
            if not _name or _name.startswith('#'):
                continue
            _seeds.append(_name)
        return {'base': _seeds}

    # INI mode.  Track current section; reject seeds before any header.
    _groups: 'dict[str, list[str]]' = {}
    _current: 'Optional[str]' = None
    for _lineno, _l in enumerate(_lines, start=1):
        _stripped = _l.strip()
        if not _stripped or _stripped.startswith('#'):
            continue
        _m = _section_re.match(_l)
        if _m:
            _current = _m.group(1).strip()
            if not _current:
                raise ValueError(
                    f"{path}:{_lineno}: empty group name in section header"
                )
            _groups.setdefault(_current, [])
            continue
        if _current is None:
            raise ValueError(
                f"{path}:{_lineno}: package {_stripped!r} appears before any "
                "`[group]` header; INI-style pkg.list requires every "
                "package under a named section.  Add `[base]` at the top "
                "if the file was previously flat."
            )
        _groups[_current].append(_stripped)
    return _groups


def parse_pkg_list_group_meta(
        path: str,
        lines: 'Optional[list[str]]' = None) -> 'dict[str, dict[str, str]]':
    """Parse per-group metadata from a pkg.list file.

    Format: a `## Description: ...` comment line directly after a
    `[group]` header (or anywhere within the group's body, but
    convention is right under the header) becomes the group's
    `Description:` field in the generated tasksel `.desc` file.

    Returns dict mapping group name → dict of metadata.  Currently
    the only key is `'description'`; the shape is extensible (e.g.
    `'section'`, `'mandatory'`) without breaking call sites.

    A flat (legacy) pkg.list with no `[section]` markers returns
    `{'base': {}}` — implicit base group with empty metadata.

    Missing description → group is absent from the returned dict's
    entry value (caller falls back to a default).

    ``lines`` lets a caller share an already-read file with
    ``parse_pkg_list_groups`` so the pkg.list is read once.
    """
    _lines = readfile(path).splitlines() if lines is None else lines
    _section_re = re.compile(r'^\s*\[([^\]]*)\]\s*$')
    _desc_re = re.compile(r'^\s*##\s*Description:\s*(.+?)\s*$')

    _has_sections = any(_section_re.match(_l) for _l in _lines)
    if not _has_sections:
        return {'base': {}}

    _meta: 'dict[str, dict[str, str]]' = {}
    _current: 'Optional[str]' = None
    for _l in _lines:
        _stripped = _l.strip()
        if not _stripped:
            continue
        _m_sec = _section_re.match(_l)
        if _m_sec:
            _current = _m_sec.group(1).strip()
            _meta.setdefault(_current, {})
            continue
        if _current is None:
            continue
        _m_desc = _desc_re.match(_l)
        if _m_desc:
            _meta[_current]['description'] = _m_desc.group(1)
    return _meta


def readfile(filename: str) -> str:
    try:
        # encoding='utf-8' (not the locale default): the text files read
        # through here — Debian Packages/Sources indices, control, pkg lists —
        # are UTF-8 by spec.  Under a C/POSIX locale the default decoder is
        # ASCII, so a non-ASCII byte (accented maintainer name, em-dash in a
        # Description) would raise UnicodeDecodeError (a ValueError) and escape
        # callers' OSError-only guards, crashing the cache build.
        # errors='replace' degrades a stray byte instead of failing the read.
        with open(filename, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError as e:
        raise OSError(f'Cannot read file {filename}: {e}') from e


def create_folders(folder_structure: str) -> None:
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
        logger.error(f"create_folders({folder_structure}): {e}")


