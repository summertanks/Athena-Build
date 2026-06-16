"""MIRROR-01 — per-mirror state CRUD and the mirror command umbrella's
non-network helpers.

This module owns the durable state for every configured publish target —
one ``config/mirror.<name>.state`` file per mirror.  Phase 1 lands the
CRUD layer and the stubbed sub-commands (`add`, `remove`, `list`,
`summary`, `status`); the network-touching commands (`publish`, `pull`,
`audit`, `query`, `reconcile-neighbours`) come in Phases 2-4.

Design rationale (separate from `scripts/mirror_sidecar/`, which is the
renamed `scripts/coord/`):

- ``scripts/mirror.py`` is the OPERATOR-FACING umbrella state — what the
  build host knows about each target.  Operator runs `mirror add` /
  `mirror remove`; this module persists and reads back.

- ``scripts/mirror_sidecar/`` (Phase 3 rename of ``scripts/coord/``) is
  the on-the-wire signed sidecar layer — claim records, coord-head
  manifest, Ed25519 identity, GPG-clearsign helpers.  Operator never
  edits these directly.

State file shape (``config/mirror.<name>.state``, JSON):

  {
    "name": "alpha",
    "url":  "ssh://ubuntu@host/asgard",  # or "file:///srv/asgard"
    "type": "ssh",                       # "ssh" or "local"
    "ssh_key": "config/repo_asgard.key", # optional, ssh only
    "base":    "<YYYYMMDDTHHMMSSZ>",     # archive floor (Phase 4)
    "current": "<YYYYMMDDTHHMMSSZ>",     # latest published (Phase 4)
    "last_publish_at": "<iso8601>",      # Phase 3
    "neighbours_known": ["url1", ...]    # last seen coord-head.neighbours (Phase 3)
  }

The `base`/`current`/`last_publish_at`/`neighbours_known` fields are
populated by later-phase commands; Phase 1's `add` writes a minimal
record with `base=current=<local snapshot.current>`.
"""

import ipaddress
import json
import logging
import os
import re
from typing import Any, List, Optional

logger = logging.getLogger('athena')


MIRROR_STATE_PREFIX = 'mirror.'
MIRROR_STATE_SUFFIX = '.state'
VALID_TYPES = frozenset({'ssh', 'local'})
HOST_TYPES = frozenset({'ip', 'fqdn', 'local'})
VALID_PROTOS = frozenset({'http', 'https'})


# ─────────────────────────── name + URL validation ──────────────────────


def _valid_name(name: str) -> bool:
    """A mirror name is ASCII alphanumerics + `-` + `_`, 1-64 chars.
    Restricted because the name becomes a filename component
    (``mirror.<name>.state``)."""
    if not isinstance(name, str):
        return False
    if not (1 <= len(name) <= 64):
        return False
    return all(_c.isalnum() or _c in '-_' for _c in name)


def _normalize_url(url: str) -> Optional[str]:
    """Strict normalisation for publish-target URLs.  Accepts ONLY
    ``ssh://user@host/path`` and ``file:///abs/path`` (plain
    ``/abs/path`` is auto-prefixed with ``file://`` for ergonomics).

    The earlier permissive shape (``http(s)://``, ``user@host:/path``
    rsync shorthand) was retired with MIRROR-01 Phase 6: HTTP(S) is
    the READ surface (`public_url`, written into
    `sources.list.d/athena-<name>.list`), never the PUBLISH surface;
    the rsync shorthand is too easy to fat-finger into a malformed
    federation entry, so we force the explicit scheme.

    Returns the normalised URL on success, None on rejection.  No
    DNS / reachability checking at this layer — `cmd_mirror_add`
    owns that.
    """
    if not isinstance(url, str) or not url:
        return None
    _stripped = url.strip()
    if not _stripped:
        return None
    if _stripped.startswith(('ssh://', 'file://')):
        return _stripped
    if _stripped.startswith('/'):
        return 'file://' + _stripped
    return None


def _infer_type(url: str) -> str:
    """Pick the transport type from the URL prefix.  ssh:// → 'ssh',
    everything else (file:// or absolute path) → 'local'."""
    if url.startswith('ssh://'):
        return 'ssh'
    return 'local'


# ─────────────────────────── host / name / public-URL derivation ─────────
#
# MIRROR-01 Phase 6 splits the URL the operator types at `mirror add` time
# into THREE derived facts that the mirror state file persists separately:
#
#   - `url`         — what we rsync to (ssh:// or file://)
#   - `host`        — the FQDN or IP we'll DNS-resolve + TCP-probe
#   - `public_url`  — what apt clients read from
#                     (= `<proto>://<host>/<dist-id>`, written into
#                       /etc/apt/sources.list.d/athena-<name>.list)
#
# The split is what lets `mirror add` (a) refuse adds that look reachable
# over SSH but unreachable over HTTP and (b) write a usable
# sources.list.d entry even though the publish URL is ssh://.


_SSH_URL_RE = re.compile(
    r'^ssh://(?:(?P<user>[^@/]+)@)?'
    r'(?P<host>\[[^\]]+\]|[^/:]+)'   # bracketed IPv6 OR bare alnum host
    r'(?::(?P<port>\d+))?'
    r'(?P<path>/.*)?$'
)
_FQDN_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?'
                      r'(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+$')


def _extract_host_from_ssh_url(url: str) -> Optional[str]:
    """`ssh://ubuntu@140.245.198.222/srv/asgard` → `140.245.198.222`.
    `ssh://user@[2001:db8::1]/srv/asgard`         → `2001:db8::1` (brackets
    stripped so downstream `ipaddress.ip_address` accepts it directly).
    Returns None if URL isn't an ssh:// scheme or host can't be
    extracted."""
    if not isinstance(url, str) or not url.startswith('ssh://'):
        return None
    _m = _SSH_URL_RE.match(url)
    if not _m:
        return None
    _host = _m.group('host') or ''
    if _host.startswith('[') and _host.endswith(']'):
        _host = _host[1:-1]
    return _host or None


def _extract_user_from_ssh_url(url: str) -> Optional[str]:
    """`ssh://ubuntu@140.245.198.222/srv/asgard` → `'ubuntu'`.
    Returns None if URL isn't ssh:// or the user@ prefix is absent
    (caller's ssh-config / default username applies)."""
    if not isinstance(url, str) or not url.startswith('ssh://'):
        return None
    _m = _SSH_URL_RE.match(url)
    if not _m:
        return None
    return _m.group('user') or None


def _extract_path_from_ssh_url(url: str) -> Optional[str]:
    """`ssh://ubuntu@140.245.198.222/home/ubuntu/asgard` →
    `'/home/ubuntu/asgard'`.  Returns None if URL isn't ssh:// or
    the path is empty."""
    if not isinstance(url, str) or not url.startswith('ssh://'):
        return None
    _m = _SSH_URL_RE.match(url)
    if not _m:
        return None
    return _m.group('path') or None


def _is_valid_ip(host: str) -> bool:
    """True for any well-formed IPv4 or IPv6 literal."""
    if not isinstance(host, str) or not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except (ValueError, TypeError):
        return False


def _is_valid_fqdn(host: str) -> bool:
    """Strict DNS-label check: lowercase ASCII alnum + hyphens, at
    least two labels separated by dots, each label ≤ 63 chars.  No
    trailing dot."""
    if not isinstance(host, str) or not host:
        return False
    if len(host) > 253:
        return False
    return bool(_FQDN_RE.match(host.lower()))


def validate_host_for_type(
    url: str, host_type: str,
) -> 'tuple[bool, str]':
    """Cross-check the URL's host portion against the operator-supplied
    keyword (`ip` / `fqdn` / `local`).  Returns (ok, detail).

    - `ip`:    URL must be ssh://; host must parse as IPv4 or IPv6
    - `fqdn`:  URL must be ssh://; host must parse as a DNS FQDN
    - `local`: URL must be file://
    """
    if host_type not in HOST_TYPES:
        return False, (
            f"unknown host type {host_type!r} "
            f"(expected one of {sorted(HOST_TYPES)})")
    if host_type == 'local':
        if not url.startswith('file://'):
            return False, (
                f"host type 'local' requires a file:// URL, got {url!r}")
        return True, ''
    # ip / fqdn → must be ssh://
    if not url.startswith('ssh://'):
        return False, (
            f"host type {host_type!r} requires an ssh:// URL, got {url!r}")
    _host = _extract_host_from_ssh_url(url)
    if not _host:
        return False, f"could not extract host from ssh URL {url!r}"
    if host_type == 'ip':
        if not _is_valid_ip(_host):
            return False, (
                f"host {_host!r} is not a valid IPv4 or IPv6 literal "
                f"(use `fqdn` keyword for hostnames)")
    else:  # fqdn
        if _is_valid_ip(_host):
            return False, (
                f"host {_host!r} looks like an IP literal — use the `ip` "
                "keyword instead of `fqdn`")
        if not _is_valid_fqdn(_host):
            return False, (
                f"host {_host!r} is not a valid DNS FQDN "
                "(needs at least two labels separated by dots)")
    return True, ''


def derive_name_from_url(url: str, host_type: str) -> Optional[str]:
    """Auto-derive a mirror name from the URL + host type.

    - ssh://...@140.245.198.222/...     + ip    → '140-245-198-222'
    - ssh://...@mirror.example.com/...  + fqdn  → 'mirror-example-com'
    - file:///srv/asgard-staging        + local → 'asgard-staging'

    The result is sanitised against `_valid_name`; if sanitisation
    yields an invalid name, returns None.  Colons and dots are mapped
    to dashes; IPv6 brackets are stripped.
    """
    if host_type not in HOST_TYPES:
        return None
    if host_type == 'local':
        if not url.startswith('file://'):
            return None
        _path = url[len('file://'):].rstrip('/')
        _tail = os.path.basename(_path) or 'local'
        _name = re.sub(r'[^a-z0-9_-]', '-', _tail.lower()).strip('-')
        return _name if _valid_name(_name) else None
    _host = _extract_host_from_ssh_url(url)
    if not _host:
        return None
    # IPv6 hosts may be `[::1]` — strip brackets before mapping.
    _clean = _host.strip('[]').lower()
    _name = re.sub(r'[.:]', '-', _clean)
    _name = re.sub(r'[^a-z0-9_-]', '-', _name).strip('-')
    return _name if _valid_name(_name) else None


def derive_public_url(
    url: str, dist_id: str, proto: str,
) -> Optional[str]:
    """For an ssh:// publish URL, return the canonical apt-readable
    URL ``<proto>://<host>/<dist-id>``.  For file:// URLs, returns
    None — file:// publish targets don't get a sources.list.d entry
    in the chroot.

    `dist_id` is `[Build] DISTRIBUTION` lowercased (e.g. 'asgard').
    `proto` must be 'http' or 'https'.
    """
    if proto not in VALID_PROTOS:
        return None
    if not isinstance(dist_id, str) or not dist_id:
        return None
    _dist = re.sub(r'[^a-z0-9_-]', '', dist_id.lower())
    if not _dist:
        return None
    if not url.startswith('ssh://'):
        return None
    _host = _extract_host_from_ssh_url(url)
    if not _host:
        return None
    # IPv6 must be re-bracketed inside a URL authority — RFC 3986 §3.2.2.
    if ':' in _host and not _host.startswith('['):
        _host = f'[{_host}]'
    return f"{proto}://{_host}/{_dist}"


# ─────────────────────────── probe pipeline ────────────────────────────
#
# Each probe returns ``(ok: bool, detail: str)``.  Higher-level orchestration
# (`cmd_mirror_add`) calls them in sequence and surfaces a per-step progress
# line.  Probes are mock-friendly: tests patch `subprocess.run` or the
# `urllib` opener rather than monkey-patching mirror.py internals.


def probe_dns_and_tcp(
    host: str, port: int, timeout_s: float = 5.0,
) -> 'tuple[bool, str]':
    """Resolve ``host`` and open a TCP socket to ``host:port`` — the
    cheapest "host is alive on this port" check.  Used for SSH (22) and
    HTTP/HTTPS (80/443) preflight."""
    import socket
    try:
        _addrs = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as _e:
        return False, f"DNS resolution failed: {_e}"
    if not _addrs:
        return False, "DNS returned no addresses"
    _last_err = ''
    for _af, _socktype, _proto_, _canon, _sa in _addrs:
        try:
            with socket.socket(_af, _socktype, _proto_) as _s:
                _s.settimeout(timeout_s)
                _s.connect(_sa)
                return True, f"reachable at {_sa[0]}:{port}"
        except OSError as _e:
            _last_err = str(_e)
            continue
    return False, f"TCP connect failed: {_last_err}"


def probe_ssh_auth(
    host: str, user: str, key_path: Optional[str],
    timeout_s: int = 10,
) -> 'tuple[bool, str]':
    """Non-interactive SSH auth probe.  Returns (ok, detail).  Uses
    BatchMode=yes so a missing-key / wrong-key path fails fast rather
    than prompting for a password."""
    import subprocess
    _argv = [
        'ssh', '-o', 'BatchMode=yes',
        '-o', f'ConnectTimeout={timeout_s}',
        '-o', 'StrictHostKeyChecking=accept-new',
    ]
    if key_path:
        _argv += ['-i', key_path]
    _target = f"{user}@{host}" if user else host
    _argv += [_target, 'echo', 'ssh-ok']
    try:
        _r = subprocess.run(
            _argv, capture_output=True, text=True, timeout=timeout_s + 5)
    except subprocess.TimeoutExpired:
        return False, f"ssh timed out after {timeout_s}s"
    except OSError as _e:
        return False, f"ssh spawn failed: {_e}"
    if _r.returncode != 0:
        return False, (
            f"ssh exit={_r.returncode}: {_r.stderr.strip()[:200] or 'no stderr'}")
    if 'ssh-ok' not in (_r.stdout or ''):
        return False, f"ssh succeeded but no expected output: {_r.stdout!r}"
    return True, "ssh auth + echo round-trip ok"


def probe_remote_writable(
    host: str, user: str, key_path: Optional[str],
    remote_pool_path: str, timeout_s: int = 15,
) -> 'tuple[bool, str]':
    """Verify the remote ssh user can mkdir + write under both
    ``<remote_pool_path>`` (apt pool) and ``<remote_pool_path>-coord``
    (sidecar tree).  Run as one ssh call so the operator sees a single
    consistent verdict.

    Returns (ok, detail).  Failure usually means the operator's ssh user
    lacks write permission on the parent dir — fix on the remote, retry.
    """
    import shlex
    import subprocess
    _pool = remote_pool_path.rstrip('/')
    _coord = _pool + '-coord'
    _remote_cmd = (
        f"mkdir -p {shlex.quote(_pool)} {shlex.quote(_coord)} && "
        f"test -w {shlex.quote(_pool)} && test -w {shlex.quote(_coord)} && "
        "echo writable-ok"
    )
    _argv = [
        'ssh', '-o', 'BatchMode=yes',
        '-o', f'ConnectTimeout={timeout_s}',
        '-o', 'StrictHostKeyChecking=accept-new',
    ]
    if key_path:
        _argv += ['-i', key_path]
    _target = f"{user}@{host}" if user else host
    _argv += [_target, _remote_cmd]
    try:
        _r = subprocess.run(
            _argv, capture_output=True, text=True, timeout=timeout_s + 5)
    except subprocess.TimeoutExpired:
        return False, f"ssh timed out after {timeout_s}s"
    except OSError as _e:
        return False, f"ssh spawn failed: {_e}"
    if _r.returncode != 0:
        return False, (
            f"remote write probe exit={_r.returncode}: "
            f"{_r.stderr.strip()[:200] or 'no stderr'}")
    if 'writable-ok' not in (_r.stdout or ''):
        return False, (
            f"remote probe returned no verdict — pool={_pool} coord={_coord}; "
            f"stdout={_r.stdout!r}")
    return True, f"{_pool} + {_coord} both mkdir-able + writable"


def probe_http_inrelease(
    public_url: str, codename: str, timeout_s: int = 10,
) -> 'tuple[bool, str]':
    """HEAD-style probe of the apt-readable InRelease at
    ``<public_url>/dists/<codename>/InRelease``.  Returns (True,
    detail) on HTTP 200; (True, 'no-release-yet') on 404 (= fresh
    mirror, first publish bootstraps it); (False, detail) otherwise.
    """
    import urllib.error
    import urllib.request
    _target = (
        public_url.rstrip('/')
        + f'/dists/{codename}/InRelease'
    )
    _req = urllib.request.Request(_target, method='HEAD')
    try:
        with urllib.request.urlopen(_req, timeout=timeout_s) as _resp:
            _code = getattr(_resp, 'status', None) or _resp.getcode()
            if _code == 200:
                return True, f"InRelease present at {_target}"
            return False, f"unexpected HTTP {_code} from {_target}"
    except urllib.error.HTTPError as _e:
        if _e.code == 404:
            return True, (
                f"no InRelease yet at {_target} (HTTP 404) — fresh "
                "mirror, first publish will bootstrap it")
        return False, f"HTTP {_e.code} from {_target}: {_e.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as _e:
        return False, f"HTTP probe failed for {_target}: {_e}"


def probe_sidecar_head(
    coord_url: str, *,
    signing_homedir: str, stage_dir: str,
    ssh_key: Optional[str] = None,
) -> 'tuple[bool, str, Optional[dict]]':
    """Pull the peer's coord-head + sig into ``stage_dir`` and GPG-verify
    against ``signing_homedir`` (the local tier-1 keyring).  Returns
    ``(ok, detail, parsed_head_or_None)``.

    Three outcomes:

      - Peer has NO coord-head yet (fresh remote, first publish will
        bootstrap): ``(True, "no head yet", None)``.
      - Peer has a coord-head AND verify succeeds: ``(True, "verified",
        head_dict)``.  Caller reads ``head_dict['neighbours']`` etc. for
        the federation summary.
      - Peer has a coord-head but verify FAILS: ``(False, "sig
        verify failed: ...", None)``.  This is the "federation signing
        key not in our keyring" gate — operator must import the
        federation's tier-1 pubkey out-of-band before `mirror add` will
        succeed.
    """
    import coord.head as _head_mod
    import coord.transport as _transport
    try:
        os.makedirs(stage_dir, mode=0o755, exist_ok=True)
    except OSError as _e:
        return False, f"could not prepare staging dir {stage_dir}: {_e}", None
    _spec, _ssh_host = rsync_spec_for_url(coord_url)
    _ok, _detail = _transport.pull_remote_coord(
        local_dest=stage_dir, remote_spec=_spec, ssh_key=ssh_key)
    if not _ok:
        return False, f"sidecar pull failed: {_detail}", None
    _head_path = os.path.join(stage_dir, 'coord-head.json')
    if not os.path.isfile(_head_path):
        return True, "no head yet (peer hasn't been published to)", None
    _head = _head_mod.read_coord_head(stage_dir, signing_homedir)
    if _head is None:
        return False, (
            "coord-head present but GPG verify FAILED against local "
            "tier-1 keyring — either the federation's signing key is "
            "missing from your keyring (import it out-of-band) or this "
            "peer was signed by a key you don't trust"), None
    return True, "coord-head verified", _head


def discover_federation_peers(
    head: dict, *,
    signing_homedir: str, stage_root: str,
    ssh_key: Optional[str] = None,
) -> 'list[dict]':
    """For every URL in ``head['neighbours']``, probe its sidecar and
    classify.  Returns a list of dicts, one per neighbour:

      {url, reachable, verified, head, detail,
       public_url, public_proto}

    A neighbour is ``reachable`` iff `probe_sidecar_head` returned ok;
    ``verified`` iff the peer's coord-head GPG-verified with our
    keyring (a peer with no coord-head yet is reachable but not
    verified).

    ``public_url`` and ``public_proto`` come from the UPSTREAM head's
    v3 per-peer record (the federation's signed source of truth for
    each peer's apt-readable URL).  v2-shaped peers (URL-only strings
    in `neighbours`) surface empty strings — `cmd_mirror_add` falls
    back to the operator's `--proto` flag for those.
    """
    import coord.schema as _schema
    _records = _schema.canonicalize_neighbour_records(
        head.get('neighbours') or [])
    _out: 'list[dict]' = []
    for _idx, _rec in enumerate(_records):
        _url = _rec.get('url') or ''
        if not _url:
            continue
        _coord = coord_root_for(_url)
        _stage = os.path.join(stage_root, f"probe-{_idx:02d}")
        _ok, _det, _peer_head = probe_sidecar_head(
            _coord, signing_homedir=signing_homedir, stage_dir=_stage,
            ssh_key=ssh_key)
        _out.append({
            'url':           _url,
            'reachable':     _ok,
            'verified':      _peer_head is not None,
            'head':          _peer_head,
            'detail':        _det,
            'public_url':    _rec.get('public_url') or '',
            'public_proto':  _rec.get('public_proto') or '',
        })
    return _out


# ─────────────────────────── state IO ──────────────────────────


def mirror_state_path(config, name: str) -> str:
    return os.path.join(
        config.dir_config, f"{MIRROR_STATE_PREFIX}{name}{MIRROR_STATE_SUFFIX}")


def read_mirror_state(config, name: str) -> Optional[dict]:
    """Return the mirror's state dict, or None if absent/malformed.
    Caller treats None as `mirror not registered`."""
    _path = mirror_state_path(config, name)
    try:
        with open(_path) as fh:
            _d = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as _e:
        logger.warning(f"read_mirror_state {name}: {_e}")
        return None
    if not isinstance(_d, dict):
        return None
    return _d


def write_mirror_state(config, name: str, state: dict) -> bool:
    """Atomic write.  Always overwrites — operators call `add`/`remove`
    to lifecycle the mirror, and the publish path uses
    `update_mirror_state` for field merges."""
    if not _valid_name(name):
        logger.error(f"write_mirror_state: invalid name {name!r}")
        return False
    _path = mirror_state_path(config, name)
    try:
        os.makedirs(os.path.dirname(_path), exist_ok=True)
        # Reuse the project's atomic-write idiom for state files.
        import utils as _utils
        _utils._atomic_write_bytes(
            _path, (json.dumps(state, indent=2, sort_keys=True) + '\n').encode('utf-8'),
        )
    except OSError as _e:
        logger.error(f"write_mirror_state {name}: {_e}")
        return False
    return True


def update_mirror_state(config, name: str, **fields) -> bool:
    """Read-merge-write convenience.  Returns False if the mirror isn't
    registered."""
    _cur = read_mirror_state(config, name)
    if _cur is None:
        return False
    _cur.update(fields)
    return write_mirror_state(config, name, _cur)


def delete_mirror_state(config, name: str) -> bool:
    """Remove the state file.  Returns True if it existed; False otherwise."""
    _path = mirror_state_path(config, name)
    try:
        os.unlink(_path)
        return True
    except FileNotFoundError:
        return False
    except OSError as _e:
        logger.error(f"delete_mirror_state {name}: {_e}")
        return False


def list_mirrors(config) -> List[str]:
    """Return the sorted list of configured mirror names (one per
    `config/mirror.<name>.state` file)."""
    _dir = config.dir_config
    _out: List[str] = []
    try:
        for _entry in os.listdir(_dir):
            if (_entry.startswith(MIRROR_STATE_PREFIX)
                    and _entry.endswith(MIRROR_STATE_SUFFIX)):
                _name = _entry[len(MIRROR_STATE_PREFIX):-len(MIRROR_STATE_SUFFIX)]
                if _valid_name(_name):
                    _out.append(_name)
    except OSError:
        return []
    _out.sort()
    return _out


def find_mirror_by_url(config, url: str) -> Optional[str]:
    """Return the mirror name registered under `url` (after URL
    normalisation), or None.  Used by `mirror remove <url>` to look up
    which `<name>.state` file to delete."""
    _norm = _normalize_url(url)
    if _norm is None:
        return None
    for _name in list_mirrors(config):
        _st = read_mirror_state(config, _name)
        if _st and _st.get('url') == _norm:
            return _name
    return None


# ─────────────────────────── add / remove helpers ──────────────────────


def add_mirror(
    config, *, name: str, url: str,
    type: Optional[str] = None,
    ssh_key: Optional[str] = None,
    seed_pin: str = '',
    host: str = '',
    host_type: str = '',
    public_proto: str = '',
    public_url: str = '',
) -> 'tuple[bool, str]':
    """Register a new mirror.  Returns (ok, detail).

    Writes ``config/mirror.<name>.state`` ONLY — federation propagation
    to peers (`reconcile-neighbours`) is the caller's next step.

    `seed_pin` is the timestamp to plant in `base` and `current` —
    typically the caller passes the local snapshot's current pin so the
    mirror starts at parity.

    `host` / `host_type` / `public_proto` / `public_url` (MIRROR-01
    Phase 6) record the operator-supplied split the new `cmd_mirror_add`
    flow derives: rsync target URL (`url`), DNS-resolvable host (`host`)
    + its kind (`ip`/`fqdn`/`local`), apt read protocol (`public_proto`),
    and the canonical apt URL the chroot's
    `sources.list.d/athena-<name>.list` line uses (`public_url`).  All
    default to empty strings for state files written by an older flow.
    """
    if not _valid_name(name):
        return False, (
            f"invalid mirror name {name!r} "
            "(use ASCII alphanumerics, '-', '_'; 1-64 chars)")
    if read_mirror_state(config, name) is not None:
        return False, f"mirror {name!r} already registered"
    _norm = _normalize_url(url)
    if _norm is None:
        return False, (
            f"invalid URL {url!r} "
            "(publish URLs must be ssh:// or file:///abs/path)")
    # Refuse duplicate URLs across mirror names
    for _other in list_mirrors(config):
        _ost = read_mirror_state(config, _other)
        if _ost and _ost.get('url') == _norm:
            return False, (
                f"URL {_norm!r} already registered as mirror {_other!r}")
    _type = type or _infer_type(_norm)
    if _type not in VALID_TYPES:
        return False, f"invalid type {_type!r}; expected one of {sorted(VALID_TYPES)}"
    if host_type and host_type not in HOST_TYPES:
        return False, (
            f"invalid host_type {host_type!r} "
            f"(expected one of {sorted(HOST_TYPES)})")
    if public_proto and public_proto not in VALID_PROTOS:
        return False, (
            f"invalid public_proto {public_proto!r} "
            f"(expected one of {sorted(VALID_PROTOS)})")
    _state = {
        'name':             name,
        'url':              _norm,
        'type':             _type,
        'ssh_key':          ssh_key or '',
        'host':             host or '',
        'host_type':        host_type or '',
        'public_proto':     public_proto or '',
        'public_url':       public_url or '',
        'base':             seed_pin,
        'current':          seed_pin,
        'last_publish_at':  '',
        'neighbours_known': [],
    }
    if not write_mirror_state(config, name, _state):
        return False, f"failed to write {mirror_state_path(config, name)}"
    return True, f"mirror {name!r} registered ({_norm}, type={_type})"


def all_mirror_urls(config) -> 'list[str]':
    """Return the canonical URL set across every configured mirror.
    Used as the SOURCE OF TRUTH for the federation `neighbours` list
    when the URL-only projection is sufficient (federation-gate
    set-comparison, federation-drift detection)."""
    _out: 'list[str]' = []
    for _n in list_mirrors(config):
        _st = read_mirror_state(config, _n)
        if _st and _st.get('url'):
            _out.append(_st['url'])
    return _out


def project_post_publish_state(local_state, remote_by_builder: dict):
    # Return type is `repo_audit.RepoState` — import lives inside the
    # body to keep the cross-module dep lazy (repo_audit doesn't always
    # import cleanly in trivial test harnesses).
    """MIRROR-02 chunk 11: project the post-publish union state of the
    mirror.  Combines the LOCAL repo's RepoState (which carries full
    Depends/Provides info per pkg) with the REMOTE claim ledger
    (filenames + versions only — no Depends; remote pkgs satisfy
    consumer deps but cannot themselves be audited as consumers).

    Returns a `repo_audit.RepoState`-shape dataclass with `.packages`
    keyed by package name, multi-version-aware: when the same package
    name appears at multiple versions across builders, the highest
    version is retained.  This matches what apt would resolve a
    versioned `Depends:` against on the mirror's apt clients.

    Caller passes the result to `repo_audit.audit_dep_closure` with
    `consumer_set` = the package names of our pending claims, so the
    closure walk is bounded to what we're about to push.
    """
    import dataclasses as _dc
    import repo_audit as _repo_audit
    # Build a mutable copy of local packages, then layer in remote-only
    # entries.  Local entries have the rich control data (Depends,
    # Provides etc.) so they participate fully in audit_dep_closure;
    # remote-only entries are satisfier-only stubs.
    _packages: 'dict[str, dict]' = {}
    if local_state is not None and getattr(local_state, 'packages', None):
        _packages.update(local_state.packages)
    for _bid, _claims in (remote_by_builder or {}).items():
        for _c in _claims:
            if _c.get('claim_state') in ('retracted', 'deprecated', 'obsolete'):
                continue
            # STA-27: claim.package is the SOURCE name, but RepoState.packages
            # (and audit_dep_closure's Depends resolution) is keyed by BINARY
            # name.  A source emits N binaries → N claims, each with its own
            # filename — derive the binary name from filename (name_ver_arch
            # .(u)deb), the same rsplit('_', 2)[0] parse detect_hash_conflicts
            # / emit_obsolescence_claims use.  Keying by the source name made
            # remote-only binaries unfindable (false closure breaks) and let a
            # dep on a name that happened to equal a source name resolve wrong.
            _fn = str(_c.get('filename') or '')
            _pkg = _fn.rsplit('_', 2)[0] if '_' in _fn else ''
            _ver = str(_c.get('built_version') or '')
            if not _pkg or not _ver:
                continue
            _existing = _packages.get(_pkg)
            if _existing is not None:
                # Local entry wins on version compare; remote satisfies
                # only if its version is strictly higher than the local
                # one (mirror has a newer pkg we haven't built yet).
                import apt_pkg as _apt_pkg
                try:
                    _cmp = _apt_pkg.version_compare(
                        _ver, str(_existing.get('Version', '')))
                except (SystemError, TypeError):
                    _cmp = 0
                if _cmp <= 0:
                    continue
            # Stub entry — no Depends, just identity so it satisfies
            # name lookups during the audit.
            _packages[_pkg] = {
                'Package': _pkg,
                'Version': _ver,
            }
    # Provides index: keep local's (remote-only entries don't carry
    # Provides info).
    _provides = (getattr(local_state, 'provides_index', {}) or {}
                 if local_state is not None else {})
    return _repo_audit.RepoState(
        packages=_packages,
        provides_index=_provides,
        packages_file=getattr(local_state, 'packages_file', '')
            if local_state is not None else '',
        repo_mtime=getattr(local_state, 'repo_mtime', 0.0)
            if local_state is not None else 0.0,
    )


def find_publish_closure_breaks(
    local_state, remote_by_builder: dict,
    our_pending_pkg_names: 'frozenset[str]',
) -> 'list[tuple]':
    """MIRROR-02 chunk 11: the publish-time installability gate.

    Walks `audit_dep_closure` over the projected post-publish state,
    bounded to OUR pending packages as the consumer set.  Returns
    the unresolved hard-Depends findings; each is
    `(pkg, field, relation_str, why)` matching repo_audit's
    convention.

    Empty list = our publish does not break the mirror.  Non-empty
    list = at least one of our pending packages depends on something
    that won't be on the mirror after this publish; caller refuses
    with the per-finding detail.
    """
    import repo_audit as _repo_audit
    _state = project_post_publish_state(local_state, remote_by_builder)
    _unresolved, _weak = _repo_audit.audit_dep_closure(
        _state, consumer_set=our_pending_pkg_names)
    return _unresolved


# ─────────────────── audit gap (1) helpers: InRelease/Packages/ownership ──


def audit_inrelease_against_head(
    pool_url: str, codename: str, expected_sha256: str,
    fetched_dir: str, ssh_key: 'Optional[str]' = None,
) -> 'tuple[Any, list[tuple[str, str, str]]]':
    """Pull the remote ``dists/<codename>/InRelease`` and verify its
    SHA-256 matches what the coord-head pins.

    Returns ``(parsed_release_or_None, findings)`` where each finding is
    ``(severity, kind, message)``.  ``parsed_release_or_None`` is a
    ``debian.deb822.Release`` instance on success (caller uses its
    ``SHA256:`` block to verify Packages files), ``None`` on failure.
    """
    import hashlib as _hashlib
    import os as _os
    from coord import transport as _transport

    _findings: 'list[tuple[str, str, str]]' = []
    _remote = (pool_url.rstrip('/')
               + f"/dists/{codename}/InRelease")
    _rsync_spec, _ = rsync_spec_for_url(_remote)
    _os.makedirs(fetched_dir, exist_ok=True)
    _local = _os.path.join(fetched_dir, 'InRelease')
    _ok, _detail = _transport.pull_single_file(
        remote_spec=_rsync_spec, local_path=_local, ssh_key=ssh_key)
    if not _ok:
        _findings.append((
            'CRITICAL', 'inrelease_unreachable',
            f"could not pull {_remote}: {_detail}"))
        return None, _findings
    try:
        with open(_local, 'rb') as _fh:
            _bytes = _fh.read()
    except OSError as _e:
        _findings.append((
            'CRITICAL', 'inrelease_unreadable',
            f"local InRelease at {_local} unreadable: {_e}"))
        return None, _findings
    _actual = _hashlib.sha256(_bytes).hexdigest()
    if expected_sha256 and _actual != expected_sha256:
        _findings.append((
            'CRITICAL', 'inrelease_sha_mismatch',
            f"on-pool InRelease sha256={_actual[:12]} disagrees with "
            f"coord-head pin={expected_sha256[:12]}"))
        return None, _findings
    from debian.deb822 import Release as _Release
    try:
        _release = _Release(_bytes)
    except Exception as _e:
        _findings.append((
            'CRITICAL', 'inrelease_parse_failed',
            f"could not parse InRelease: {type(_e).__name__}: {_e}"))
        return None, _findings
    return _release, _findings


def audit_packages_chain(
    pool_url: str, codename: str, release: 'Any',
    fetched_dir: str, ssh_key: 'Optional[str]' = None,
    components: 'tuple[str, ...]' = (),     # () = all components in InRelease
    arches: 'tuple[str, ...]' = ('amd64',),
) -> 'tuple[dict[str, dict], list[tuple[str, str, str]]]':
    """For each ``<component>/binary-<arch>/Packages`` referenced in the
    verified InRelease, pull and verify its SHA-256 against the
    InRelease pin, then parse it.

    Returns ``(packages_index, findings)`` where ``packages_index`` maps
    on-disk filename (basename of ``Filename:`` field) → dict with
    ``sha256``, ``version``, ``package``, ``size``.  Pulls only the
    uncompressed ``Packages`` file (the apt-trusted index for the
    cross-check); ``.gz`` / ``.xz`` are skipped — InRelease already
    pins all of them and they're redundant for our purposes.
    """
    import hashlib as _hashlib
    import os as _os
    from coord import transport as _transport

    _findings: 'list[tuple[str, str, str]]' = []
    _index: 'dict[str, dict]' = {}

    _sha256_block = release.get('SHA256') if release is not None else None
    if not _sha256_block:
        _findings.append((
            'CRITICAL', 'inrelease_no_sha256_block',
            "InRelease carries no SHA256: block — cannot verify "
            "Packages chain"))
        return _index, _findings

    _by_name: 'dict[str, dict]' = {
        str(_e.get('name')): _e for _e in _sha256_block
        if _e.get('name')
    }

    # Self-discover the binary Packages indices the InRelease declares —
    # both deb components (main, doc, …) and the udeb debian-installer
    # tree (main/debian-installer).  Auditing exactly what's published
    # means intentionally-empty components (contrib/non-free with nothing
    # built) produce no spurious "missing pin" warnings — they simply
    # aren't in InRelease.  A non-empty `components` narrows to that
    # subset; `arches` always filters.
    import re as _re
    _arch_set = set(arches)
    _comp_filter = set(components)
    _targets: 'list[tuple[str, str, str]]' = []
    for _name in sorted(_by_name):
        _m = _re.match(r'^(.+)/binary-([^/]+)/Packages$', _name)
        if not _m:
            continue
        _comp, _arch = _m.group(1), _m.group(2)
        if _arch not in _arch_set:
            continue
        if _comp_filter and _comp not in _comp_filter:
            continue
        _targets.append((_comp, _arch, _name))

    for _comp, _arch, _rel in _targets:
        _pin = _by_name[_rel]
        _remote = pool_url.rstrip('/') + f"/dists/{codename}/{_rel}"
        _rsync_spec, _ = rsync_spec_for_url(_remote)
        _local = _os.path.join(
            fetched_dir, f"Packages.{_comp.replace('/', '_')}.{_arch}")
        _ok, _detail = _transport.pull_single_file(
            remote_spec=_rsync_spec, local_path=_local,
            ssh_key=ssh_key)
        if not _ok:
            _findings.append((
                'CRITICAL', 'packages_unreachable',
                f"could not pull {_rel}: {_detail}"))
            continue
        try:
            with open(_local, 'rb') as _fh:
                _data = _fh.read()
        except OSError as _e:
            _findings.append((
                'CRITICAL', 'packages_unreadable',
                f"local {_local} unreadable: {_e}"))
            continue
        _actual = _hashlib.sha256(_data).hexdigest()
        _expected = str(_pin.get('sha256', ''))
        if _expected and _actual != _expected:
            _findings.append((
                'CRITICAL', 'packages_sha_mismatch',
                f"on-pool {_rel} sha256={_actual[:12]} disagrees "
                f"with InRelease pin={_expected[:12]}"))
            continue
        # Parse entries — multi-section deb822 Packages format.
        from debian.deb822 import Packages as _Packages
        try:
            _stream = iter(_Packages.iter_paragraphs(
                _data, use_apt_pkg=False))
        except Exception as _e:
            _findings.append((
                'CRITICAL', 'packages_parse_failed',
                f"could not parse {_rel}: "
                f"{type(_e).__name__}: {_e}"))
            continue
        for _para in _stream:
            _fn_full = str(_para.get('Filename') or '')
            if not _fn_full:
                continue
            _basename = _os.path.basename(_fn_full)
            _index[_basename] = {
                'package':  str(_para.get('Package') or ''),
                'version':  str(_para.get('Version') or ''),
                'sha256':   str(_para.get('SHA256') or ''),
                'size':     str(_para.get('Size') or ''),
                'component': _comp,
                'arch':      _arch,
            }
    return _index, _findings


def _superseded_seqs(claims: 'list[dict]') -> 'set[int]':
    """Per-builder supersession fold — thin alias for the canonical
    `coord.schema.superseded_seqs` (STA-29 centralised the fold so every
    consumer applies identical semantics).  Kept as a name for this
    module's callers."""
    from coord import schema as _schema
    return _schema.superseded_seqs(claims)


def audit_claims_vs_packages(
    by_builder: 'dict[str, list[dict]]',
    packages_index: 'dict[str, dict]',
) -> 'list[tuple[str, str, str]]':
    """Cross-check every non-retracted claim against the apt-trusted
    Packages index built by :func:`audit_packages_chain`.

    Findings:
      ``claim_not_in_apt_index``  CRITICAL: claim references a filename
                                  that's not in any Packages file
      ``claim_apt_sha_mismatch``  CRITICAL: same filename appears in
                                  both, but the SHAs disagree (a
                                  re-indexed apt repo would serve
                                  different bytes than the sidecar
                                  pins)
    """
    _findings: 'list[tuple[str, str, str]]' = []
    _seen_pairs: 'set[tuple[str, str]]' = set()
    for _bid, _claims in by_builder.items():
        _superseded = _superseded_seqs(_claims)
        for _c in _claims:
            if _c.get('claim_state') in ('retracted', 'deprecated', 'obsolete'):
                continue
            # A published claim a later deprecation/obsolescence targets is
            # no longer the live assertion for its filename — skip it (the
            # successor line above already carries the current state).
            if int(_c.get('seq', 0)) in _superseded:
                continue
            _fn = str(_c.get('filename') or '')
            if not _fn:
                continue
            _claim_sha = str(_c.get('sha256') or '')
            _idx = packages_index.get(_fn)
            if _idx is None:
                _key = ('not_in_apt', _fn)
                if _key in _seen_pairs:
                    continue
                _seen_pairs.add(_key)
                _findings.append((
                    'CRITICAL', 'claim_not_in_apt_index',
                    f"{_fn} (claim by {_bid}) is not in any "
                    "Packages file under the verified InRelease"))
                continue
            _apt_sha = str(_idx.get('sha256') or '')
            if _claim_sha and _apt_sha and _claim_sha != _apt_sha:
                _key = ('sha_mismatch', _fn)
                if _key in _seen_pairs:
                    continue
                _seen_pairs.add(_key)
                _findings.append((
                    'CRITICAL', 'claim_apt_sha_mismatch',
                    f"{_fn}: claim sha={_claim_sha[:12]} "
                    f"disagrees with Packages sha={_apt_sha[:12]} "
                    f"(builder {_bid})"))
    return _findings


def audit_ownership_summary(
    by_builder: 'dict[str, list[dict]]', our_builder_id: 'Optional[str]' = None,
) -> 'tuple[dict, list[tuple[str, str, str]]]':
    """Project the per-filename ownership view and bucket-count by
    owner: ``we_own`` / ``peers_own`` / ``tunneled``.  Optionally
    include a per-peer-builder breakdown.

    Returns ``(summary_dict, findings)``.  ``summary_dict`` shape::

      {
        'total':       int,                    # distinct filenames
        'we_own':      int,                    # builder == our_builder_id
        'peers_own':   int,                    # builder is some other id
        'tunneled':    int,                    # builder is None (republished)
        'by_peer':     {builder_id: int, ...}, # peer-side breakdown
      }

    No findings are emitted at this layer — cross-builder hash
    conflicts are :func:`coord.reconcile.detect_hash_conflicts`'s
    responsibility.  The empty-list return keeps the signature
    consistent with the other audit helpers.
    """
    from coord import store as _store
    _owners = _store.project_owners(by_builder)
    _we_own = 0
    _peers_own = 0
    _tunneled = 0
    _by_peer: 'dict[str, int]' = {}
    for _rec in _owners.values():
        _builder = _rec.get('builder')
        if _builder is None:
            _tunneled += 1
        elif our_builder_id is not None and _builder == our_builder_id:
            _we_own += 1
        else:
            _peers_own += 1
            _bid_str = str(_builder)
            _by_peer[_bid_str] = _by_peer.get(_bid_str, 0) + 1
    _summary: 'dict[str, Any]' = {
        'total':     len(_owners),
        'we_own':    _we_own,
        'peers_own': _peers_own,
        'tunneled':  _tunneled,
        'by_peer':   _by_peer,
    }
    return _summary, []


def _canon_url(url: str) -> str:
    """Lowercase + strip trailing slash for URL comparison.  Mirrors
    the canonical form ``coord.schema.canonicalize_neighbours`` uses."""
    return (url or '').lower().rstrip('/')


def audit_federation_walk(
    per_mirror: 'list[dict]', signing_home: str, cache_dir: str,
    ssh_key: 'Optional[str]' = None,
) -> 'tuple[list[dict], list[tuple[str, str, str]]]':
    """For every neighbour URL declared in any reachable mirror's
    coord-head that ISN'T itself one of our locally-configured mirrors
    (already fetched + verified in :func:`cmd_mirror_audit`), pull
    that neighbour's coord-head and verify:

      - it's reachable (rsync pull succeeds)
      - it's tier-1 GPG signed (``read_coord_head`` happy)
      - it lists the mirror that declared it (symmetric membership)

    This is the "recursive" half of federation gating: the local
    federation gate (``check_federation_consistency``) only checks
    that local config matches a peer's declared neighbours.  THIS
    walk asks each declared peer "does your federation view list
    the mirror that declared you back?" — catching the case where
    operator A has B in their federation but B has dropped A.

    Returns ``(walked_records, findings)`` where ``walked_records``
    carries the same shape as ``per_mirror`` entries (``name``,
    ``head``, ``by_builder``) for downstream consumers.  Single
    hop — multi-hop would require recursion + cycle guard but the
    operator can already see further hops by adding the peer as a
    local mirror.
    """
    import os as _os
    from coord import head as _head_mod
    from coord import schema as _sch
    from coord import transport as _transport

    _findings: 'list[tuple[str, str, str]]' = []
    _walked: 'list[dict]' = []
    # Configured mirrors — skip these; they're audited directly.
    _local_urls: 'set[str]' = {
        _canon_url(str(_m.get('url') or ''))
        for _m in per_mirror if _m.get('url')
    }
    # Cycle guard — start with locals so we don't re-walk.
    _visited: 'set[str]' = set(_local_urls)

    for _m in per_mirror:
        _h = _m.get('head')
        if _h is None:
            continue
        _src_name = _m['name']
        _nbrs_records = _sch.canonicalize_neighbour_records(
            _h.get('neighbours') or [])
        for _rec in _nbrs_records:
            _peer_url = _rec.get('url') if isinstance(_rec, dict) else None
            if not isinstance(_peer_url, str) or not _peer_url:
                continue
            _peer_canon = _canon_url(_peer_url)
            if _peer_canon in _visited:
                continue
            _visited.add(_peer_canon)
            # Display name — best-effort derive from host; fall back
            # to the URL itself if derive returns None.
            _peer_name = (derive_name_from_url(_peer_url, host_type='ssh')
                          or _peer_url)
            _coord_root = coord_root_for(_peer_url)
            _rsync_spec, _ = rsync_spec_for_url(_coord_root)
            _local_dest = _os.path.join(
                cache_dir, 'walked', _peer_name)
            _os.makedirs(_local_dest, exist_ok=True)
            _ok, _detail = _transport.pull_remote_coord(
                local_dest=_local_dest, remote_spec=_rsync_spec,
                ssh_key=ssh_key,
            )
            if not _ok:
                _findings.append((
                    'CRITICAL', 'federation_neighbour_unreachable',
                    f"{_peer_name} (declared by {_src_name}): could "
                    f"not pull coord — {_detail}"))
                continue
            _peer_head = _head_mod.read_coord_head(
                _local_dest, signing_home)
            if _peer_head is None:
                _findings.append((
                    'CRITICAL', 'federation_neighbour_unverified',
                    f"{_peer_name} (declared by {_src_name}): "
                    "coord-head missing or tier-1 signature failed"))
                continue
            _walked.append({
                'name': _peer_name, 'head': _peer_head,
                'url': _peer_url,
            })
            # Symmetric membership: does the peer list the mirror that
            # declared it?  We need an URL for the source mirror; use
            # the entry from this iteration's source neighbour record
            # if the peer happens to declare us by URL.  Practically,
            # check whether THIS peer's neighbours list any URL that
            # matches one of our configured mirrors' canonical URLs.
            _peer_nbrs = {
                _canon_url(str(_r.get('url') or ''))
                for _r in _sch.canonicalize_neighbour_records(
                    _peer_head.get('neighbours') or [])
                if isinstance(_r, dict)
            }
            _src_nbrs_self = {
                _canon_url(str(_r.get('url') or ''))
                for _r in _sch.canonicalize_neighbour_records(
                    _h.get('neighbours') or [])
                if isinstance(_r, dict)
            }
            # If src lists exactly one URL that matches peer (the peer
            # we just walked), peer should reciprocally list at least
            # one URL from src's set (minus peer itself).
            _src_others = _src_nbrs_self - {_peer_canon}
            if _src_others and not (_peer_nbrs & _src_others):
                _findings.append((
                    'CRITICAL', 'federation_neighbour_asymmetric',
                    f"{_peer_name}: neighbours list "
                    f"({sorted(_peer_nbrs)}) does not overlap with "
                    f"{_src_name}'s federation view "
                    f"({sorted(_src_others)})"))
    return _walked, _findings


def audit_cross_mirror_head_drift(
    per_mirror: 'list[dict]',
) -> 'list[tuple[str, str, str]]':
    """Compare coord-head federation-membership and revocation lists
    across every configured mirror.  All mirrors in a federation MUST
    agree on:
      - the neighbours set (federation membership)
      - the revoked_builders set (federation-wide blocklist)

    They may legitimately disagree on:
      - inrelease_sha256 / snapshot.current — mirrors can be at
        different snapshots at different moments (an indl-mode push
        bumps one mirror's snapshot before the dist-mode mirror
        catches up via a later publish)
      - epoch — same reason, drifts as publishes happen

    Strategy: pick the first reachable mirror as reference, compare
    every other reachable mirror against it.  Diff against reference
    is a CRITICAL.  Caller has already filtered ``per_mirror`` to the
    reachable entries (``head is not None``); skip the unreachable.
    """
    from coord import schema as _sch
    _findings: 'list[tuple[str, str, str]]' = []
    _reachable = [_m for _m in per_mirror if _m.get('head') is not None]
    if len(_reachable) < 2:
        return _findings

    def _nbrs_of(_h: dict) -> 'frozenset[str]':
        _raw = _h.get('neighbours') or []
        return frozenset(_sch.neighbour_urls(_raw))

    _ref = _reachable[0]
    _ref_name = _ref['name']
    _ref_head = _ref['head']
    _ref_nbrs = _nbrs_of(_ref_head)
    _ref_rev = frozenset(_ref_head.get('revoked_builders') or {})
    for _m in _reachable[1:]:
        _h = _m['head']
        _nbrs = _nbrs_of(_h)
        _rev = frozenset(_h.get('revoked_builders') or {})
        if _nbrs != _ref_nbrs:
            _only_ref = sorted(_ref_nbrs - _nbrs)
            _only_m = sorted(_nbrs - _ref_nbrs)
            _findings.append((
                'CRITICAL', 'cross_mirror_neighbours_drift',
                f"{_m['name']} vs {_ref_name}: federation members "
                f"disagree; only-on-{_ref_name}={_only_ref or '[]'}, "
                f"only-on-{_m['name']}={_only_m or '[]'}"))
        if _rev != _ref_rev:
            _only_ref = sorted(_ref_rev - _rev)
            _only_m = sorted(_rev - _ref_rev)
            _findings.append((
                'CRITICAL', 'cross_mirror_revocation_drift',
                f"{_m['name']} vs {_ref_name}: revoked_builders "
                f"disagree; only-on-{_ref_name}={_only_ref or '[]'}, "
                f"only-on-{_m['name']}={_only_m or '[]'}"))
    return _findings


def audit_sidecar_seq_integrity(
    by_builder: 'dict[str, list[dict]]',
) -> 'list[tuple[str, str, str]]':
    """Per-builder JSONL structural checks: seq numbers must be unique
    and contiguous from 1.  Catches manual jsonl edits, partial-write
    truncation, and replay attempts that slipped through signature
    verification (a re-signed line with a duplicated seq is still a
    valid Ed25519 signature; this is the layer that flags the dup).

    Findings:
      ``sidecar_duplicate_seq``  CRITICAL — same (builder, seq) appears
                                 in 2+ records
      ``sidecar_seq_gap``        CRITICAL — seq numbers have a hole
                                 (e.g. 1, 2, 4) — a record was deleted
                                 or never landed
      ``sidecar_seq_missing_1``  CRITICAL — first seq isn't 1 — early
                                 records were truncated
    """
    _findings: 'list[tuple[str, str, str]]' = []
    for _bid, _claims in by_builder.items():
        if not _claims:
            continue
        _seqs: 'list[int]' = []
        _dups: 'set[int]' = set()
        _seen: 'set[int]' = set()
        for _c in _claims:
            _s = _c.get('seq')
            if not isinstance(_s, int):
                continue
            if _s in _seen:
                _dups.add(_s)
            _seen.add(_s)
            _seqs.append(_s)
        if not _seqs:
            continue
        for _d in sorted(_dups):
            _findings.append((
                'CRITICAL', 'sidecar_duplicate_seq',
                f"{_bid}: seq={_d} appears on multiple claims — "
                "jsonl was edited or a replay slipped through"))
        _unique = sorted(_seen)
        if _unique[0] != 1:
            _findings.append((
                'CRITICAL', 'sidecar_seq_missing_1',
                f"{_bid}: lowest seq is {_unique[0]} (expected 1) — "
                "early records may have been truncated"))
        _gaps: 'list[int]' = []
        for _i in range(1, len(_unique)):
            if _unique[_i] != _unique[_i - 1] + 1:
                _gaps.extend(range(_unique[_i - 1] + 1, _unique[_i]))
        if _gaps:
            _preview = ', '.join(str(_g) for _g in _gaps[:5])
            _more = (f" (+{len(_gaps) - 5} more)"
                     if len(_gaps) > 5 else '')
            _findings.append((
                'CRITICAL', 'sidecar_seq_gap',
                f"{_bid}: missing seq(s) {_preview}{_more} — record(s) "
                "deleted or never landed"))
    return _findings


def _classify_missing_claim(
    _row: dict, local_repo_dir: str, buildlog_dir: 'Optional[str]',
    closure_srcs: 'Optional[set]',
) -> 'tuple[str, str, str]':
    """STA-25 follow-up: a live own-claim file missing on disk is CRITICAL —
    UNLESS the next `mirror publish` will RELEASE the claim, in which case it
    is a benign INFO (the operator just pruned before publishing).  Two
    CONFIRMABLE release paths (remote_publish Steps 6b/6c):

      - source left the signed selection  → next publish DEPRECATES it
      - source still selected but the current build no longer emits this exact
        filename (newer version) → next publish OBSOLETES it

    Either downgrades to INFO `own_claim_disk_pending_release`.  When release
    can't be confirmed (no closure info, or the current build still emits this
    file) it stays CRITICAL — a genuinely-missing current artifact (the
    closure-incomplete case)."""
    _fn = _row['filename']
    _src = str((_row.get('claim') or {}).get('package') or '')
    if closure_srcs is not None and _src and _src not in closure_srcs:
        return ('INFO', 'own_claim_disk_pending_release',
                f"{_fn} → deprecated on next `mirror publish` "
                f"(source {_src} left the selection)")
    if closure_srcs is not None and _src in closure_srcs and buildlog_dir:
        try:
            import utils as _utils
            _rec = _utils.read_build_record(buildlog_dir, _src) or {}
        except Exception:
            _rec = {}
        _outputs = set((_rec.get('output_hashes') or {}).keys())
        if _outputs and _fn not in _outputs:
            return ('INFO', 'own_claim_disk_pending_release',
                    f"{_fn} → obsoleted on next `mirror publish` "
                    f"(newer build of {_src})")
    return ('CRITICAL', 'own_claim_disk_missing',
            f"{_fn} (our claim) not in {local_repo_dir} — current artifact "
            "missing (publish will NOT fix; closure-incomplete)")


def audit_own_claims_on_disk(
    by_builder: 'dict[str, list[dict]]',
    our_builder_id: 'Optional[str]', local_repo_dir: str,
    buildlog_dir: 'Optional[str]' = None,
    closure_srcs: 'Optional[set]' = None,
) -> 'list[tuple[str, str, str]]':
    """Rehash every .deb we ourselves published and compare with the
    claim's ``sha256``.  Catches pool bitrot on OUR side independent
    of the apt-index chain (gap #1).

    When ``buildlog_dir`` is supplied AND a disk-vs-claim mismatch is
    found, the helper additionally consults the local ``build.json``
    for the source package: if the on-disk sha matches an entry in
    the build record's ``output_hashes``, the divergence is reframed
    as **WARNING** ``own_claim_local_ahead_of_remote`` (a rebuild
    happened locally but hasn't been re-published yet — operator
    action: run ``mirror publish``).  Only when the on-disk sha
    matches NEITHER the claim nor the local build record do we
    emit **CRITICAL** ``own_claim_disk_sha_mismatch`` — that's the
    real bitrot case.

    Skipped for peer-built claims (we don't have their files locally)
    and for tunneled claims (republished_from set).

    Findings:
      ``own_claim_disk_missing``             CRITICAL — our claim
            references a file that isn't in local_repo_dir
      ``own_claim_local_ahead_of_remote``    WARNING  — on-disk sha
            matches local build.json's output_hashes but NOT the
            remote claim; operator should re-publish
      ``own_claim_disk_sha_mismatch``        CRITICAL — file present,
            disagrees with both the claim AND any local build record
            we can find (or no build record found at all)
    """
    _findings: 'list[tuple[str, str, str]]' = []
    for _row in _own_claims_disk_walk(
            by_builder, our_builder_id, local_repo_dir, buildlog_dir):
        _kind = _row['kind']
        _fn = _row['filename']
        if _kind == 'missing':
            _findings.append(_classify_missing_claim(
                _row, local_repo_dir, buildlog_dir, closure_srcs))
        elif _kind == 'unreadable':
            _findings.append((
                'CRITICAL', 'own_claim_disk_unreadable',
                f"{_fn}: cannot read {_row['path']}: {_row['error']}"))
        elif _kind == 'local_ahead':
            _findings.append((
                'WARNING', 'own_claim_local_ahead_of_remote',
                f"{_fn}: on-disk sha={_row['local_sha'][:12]} matches "
                f"local build.json; remote claim "
                f"sha={_row['remote_sha'][:12]} is older — bump + "
                "publish, or `mirror reclaim <source|file>` if this "
                "content change is deliberately version-less"))
        elif _kind == 'sha_mismatch':
            _findings.append((
                'CRITICAL', 'own_claim_disk_sha_mismatch',
                f"{_fn}: on-disk sha={_row['local_sha'][:12]} disagrees "
                f"with claim sha={_row['remote_sha'][:12]} and no "
                "matching local build record — bitrot or corruption"))
    return _findings


def local_ahead_candidates(
    by_builder: 'dict[str, list[dict]]',
    our_builder_id: 'Optional[str]', local_repo_dir: str,
    buildlog_dir: 'Optional[str]' = None,
) -> 'list[dict]':
    """RECLAIM-01: structured view of every own-claim file whose on-disk
    bytes are AHEAD of the remote claim (local sha matches our
    build.json, remote claim's sha is older) — the exact candidate set
    `mirror reclaim` operates on.  Each entry::

      {filename, package, component, old_seq, remote_sha, local_sha,
       size, intended_version, built_version, path}

    `old_seq`/`remote_sha` identify the published claim a reclaim would
    supersede (and let the publish-side validation detect remote drift
    between listing and execution); the rest seeds new_reclaim().
    Superseded/marker claims and tunneled claims are excluded by the
    same fold the audit uses."""
    import os as _os
    _out: 'list[dict]' = []
    for _row in _own_claims_disk_walk(
            by_builder, our_builder_id, local_repo_dir, buildlog_dir):
        if _row['kind'] != 'local_ahead':
            continue
        _c = _row['claim']
        try:
            _size = _os.path.getsize(_row['path'])
        except OSError:
            continue
        _out.append({
            'filename':         _row['filename'],
            'package':          str(_c.get('package') or ''),
            'component':        str(_c.get('component') or 'main'),
            'old_seq':          int(_c.get('seq', 0)),
            'remote_sha':       _row['remote_sha'],
            'local_sha':        _row['local_sha'],
            'size':             _size,
            'intended_version': str(_c.get('intended_version') or ''),
            'built_version':    str(_c.get('built_version') or ''),
            'path':             _row['path'],
        })
    _out.sort(key=lambda _r: _r['filename'])
    return _out


def _own_claims_disk_walk(
    by_builder: 'dict[str, list[dict]]',
    our_builder_id: 'Optional[str]', local_repo_dir: str,
    buildlog_dir: 'Optional[str]' = None,
) -> 'list[dict]':
    """Shared core for audit_own_claims_on_disk +
    local_ahead_candidates: walk OUR live claims, compare each against
    the on-disk pool file, classify.  Returns rows::

      {kind, claim, filename, remote_sha, local_sha, path, error}

    kind ∈ {'missing', 'unreadable', 'local_ahead', 'sha_mismatch'} —
    matching rows are omitted entirely."""
    import hashlib as _hashlib
    import os as _os

    _rows: 'list[dict]' = []
    if not our_builder_id:
        return _rows
    _claims = by_builder.get(our_builder_id) or []
    # Walk the local pool dir once to build {basename: full_path}.
    _by_name: 'dict[str, str]' = {}
    if _os.path.isdir(local_repo_dir):
        for _root, _dirs, _files in _os.walk(local_repo_dir):
            for _f in _files:
                if _f.endswith(('.deb', '.udeb')):
                    _by_name[_f] = _os.path.join(_root, _f)
    # Cache build_record lookups keyed on (source_name); each record
    # carries output_hashes for every binary it produced.  Reuse so a
    # multi-binary source (kernel: ~30 binaries) only touches disk once.
    _br_cache: 'dict[str, dict]' = {}

    def _local_build_has_sha(_claim: dict, _on_disk_sha: str) -> bool:
        """True when the local build record for this claim's source
        carries an output_hashes entry equal to _on_disk_sha."""
        if not buildlog_dir:
            return False
        _source = str(_claim.get('package') or '')
        if not _source:
            return False
        _rec = _br_cache.get(_source)
        if _rec is None:
            try:
                import utils as _utils
                _rec = _utils.read_build_record(buildlog_dir, _source) or {}
            except Exception:
                _rec = {}
            _br_cache[_source] = _rec
        _oh = _rec.get('output_hashes') or {}
        # Match by filename if present, otherwise any value match
        # (older v2 records may key on something different).
        _fn = str(_claim.get('filename') or '')
        if _fn and _oh.get(_fn) == _on_disk_sha:
            return True
        return _on_disk_sha in set(_oh.values())

    _superseded = _superseded_seqs(_claims)
    for _c in _claims:
        if _c.get('claim_state') in ('retracted', 'deprecated', 'obsolete'):
            continue
        # Skip claims a later deprecation/obsolescence/reclaim
        # superseded — the old file may legitimately be pruned from the
        # local repo (or its bytes replaced under the same filename).
        if int(_c.get('seq', 0)) in _superseded:
            continue
        if _c.get('republished_from'):
            continue
        _fn = str(_c.get('filename') or '')
        _claim_sha = str(_c.get('sha256') or '')
        if not _fn or not _claim_sha:
            continue
        _path = _by_name.get(_fn)
        if _path is None:
            _rows.append({'kind': 'missing', 'claim': _c, 'filename': _fn,
                          'remote_sha': _claim_sha, 'local_sha': '',
                          'path': '', 'error': ''})
            continue
        try:
            with open(_path, 'rb') as _fh:
                _actual = _hashlib.sha256(_fh.read()).hexdigest()
        except OSError as _e:
            _rows.append({'kind': 'unreadable', 'claim': _c,
                          'filename': _fn, 'remote_sha': _claim_sha,
                          'local_sha': '', 'path': _path,
                          'error': str(_e)})
            continue
        if _actual == _claim_sha:
            continue
        _rows.append({
            'kind': ('local_ahead' if _local_build_has_sha(_c, _actual)
                     else 'sha_mismatch'),
            'claim': _c, 'filename': _fn, 'remote_sha': _claim_sha,
            'local_sha': _actual, 'path': _path, 'error': '',
        })
    return _rows


def all_mirror_neighbour_records(config) -> 'list[dict]':
    """v3 source-of-truth for ``coord-head.neighbours``: a record per
    configured mirror carrying its publish URL plus the apt-readable
    ``public_url`` + ``public_proto`` recorded in the per-mirror state
    file.  Used by the publish path and `mirror reconcile-neighbours`
    so peers in a heterogeneous federation (mixed http / https) sign
    their actual apt URL into the federation's shared head instead of
    every joiner having to inherit the operator's ``--proto`` flag.

    File:// mirrors keep empty ``public_url`` — there's no
    network-readable apt URL to record; a joining builder falls back
    to its own ``--proto`` for them (which is N/A for file:// anyway).
    """
    _out: 'list[dict]' = []
    for _n in list_mirrors(config):
        _st = read_mirror_state(config, _n)
        if not _st:
            continue
        _url = _st.get('url') or ''
        if not _url:
            continue
        _out.append({
            'url':           _url,
            'public_url':    _st.get('public_url') or '',
            'public_proto':  _st.get('public_proto') or '',
        })
    return _out


def neighbours_drift(config, name: str) -> 'tuple[str, list[str], list[str]]':
    """Compare a mirror's last-seen ``neighbours_known`` against the
    canonical local-config mirror URL set.

    Returns ``(tag, missing_on_peer, extra_on_peer)`` where ``tag`` is:
      - ``unpublished``  — the peer has never been published to (no
        ``neighbours_known`` recorded yet); first publish will bootstrap
      - ``in-sync``      — peer's last-seen neighbours match local config
      - ``drift``        — diff exists; operator should run
                           ``mirror reconcile-neighbours``

    ``missing_on_peer`` are URLs in local config that the peer's last
    coord-head lacked; ``extra_on_peer`` are URLs the peer's coord-head
    carries that local config no longer registers.  Both lists are
    canonicalised.
    """
    import coord.schema as _schema
    _st = read_mirror_state(config, name)
    if _st is None:
        return ('unpublished', [], [])
    _known_raw = _st.get('neighbours_known') or []
    if not _known_raw:
        return ('unpublished', [], [])
    _known = set(_schema.canonicalize_neighbours(_known_raw))
    _local = set(_schema.canonicalize_neighbours(all_mirror_urls(config)))
    _missing = sorted(_local - _known)
    _extra = sorted(_known - _local)
    if not _missing and not _extra:
        return ('in-sync', [], [])
    return ('drift', _missing, _extra)


def coord_root_for(pool_url: str) -> str:
    """Derive the sidecar (coord-tree) root URL from the apt-pool URL.

    Convention: append `-coord` to the LAST path component.  The pool
    is what apt clients reach for; the sidecar is operator-facing only
    and lives at a sibling path on the same host.

    Examples:
      ssh://user@host/srv/asgard           → ssh://user@host/srv/asgard-coord
      file:///srv/asgard                   → file:///srv/asgard-coord
      user@host:/srv/asgard/               → user@host:/srv/asgard-coord
      /srv/asgard                          → /srv/asgard-coord

    Trailing slashes are stripped before appending so we never produce
    a `<x>/-coord` form.
    """
    return (pool_url or '').rstrip('/') + '-coord'


def rsync_spec_for_url(url: str) -> 'tuple[str, Optional[str]]':
    """Convert our stored URL to (rsync_spec, ssh_host_or_None).

    rsync's native syntax:
      ssh://user@host/path     → user@host:/path  (ssh)
      user@host:/path          → user@host:/path  (ssh; already native)
      file:///abs/path         → /abs/path        (local fs)
      /abs/path                → /abs/path        (local fs)

    Returns the rsync-ready spec PLUS the ssh-host portion (for flock
    acquisition) or None for local-fs mirrors.
    """
    if isinstance(url, str) and url.startswith('ssh://'):
        _path = url[len('ssh://'):]
        if '/' in _path:
            _host, _rest = _path.split('/', 1)
            return (f"{_host}:/{_rest}", _host)
        return (_path, _path)
    if isinstance(url, str) and url.startswith('file://'):
        return (url[len('file://'):], None)
    if isinstance(url, str) and '@' in url and ':' in url and not url.startswith('/'):
        # user@host:/path shorthand
        _host = url.split(':', 1)[0]
        return (url, _host)
    return (url, None)


def reconcile_neighbours(
    config, *, signing_homedir: str,
    target_name: 'Optional[str]' = None,
    flock_timeout: int = 60,
) -> 'tuple[bool, str, list[dict]]':
    """MIRROR-01 Phase 2: re-propagate the canonical neighbours list to
    every peer's coord-head.

    For each registered mirror (or just `target_name` when given):
      1. Acquire remote flock (ssh mirrors only; local-fs skips)
      2. Pull coord-head + sig to cache/mirror/<name>/staging/
      3. tier-1 GPG verify; abort the peer's update on verify-fail
      4. Compare existing neighbours vs desired (`all_mirror_urls`)
      5. If different: rewrite neighbours, re-sign locally via tier-1
         GPG, push back to the remote
      6. Release flock

    Returns (overall_ok, summary, results).
      results[i] = {
        name, url, reachable: bool, changed: bool,
        ok: bool, detail: str,
      }

    Fail-loud: an unreachable peer counts as overall failure.  The
    operator must retry once peer connectivity is restored before any
    publish can succeed (federation-gate at publish time blocks until
    every peer's neighbours match local config).

    First-contact mirrors (no coord-head on the remote yet) are
    skipped with a friendly "no head yet" detail — first publish
    will initialise neighbours from local config.
    """
    import os
    import coord.head as _head_mod
    import coord.schema as _schema
    import coord.transport as _transport

    # v3 records — what we'd WRITE into each peer's coord-head.  The
    # URL-only projection (`_desired_urls`) is what we COMPARE against
    # the peer's existing head for the change-detection gate;
    # per-peer public_url/public_proto churn is tolerated silently here
    # (the next `mirror publish` from each builder rewrites its own
    # per-peer fields anyway, so noise-free reconcile only fires on
    # genuine federation-membership diff).
    _desired_records = _schema.canonicalize_neighbour_records(
        all_mirror_neighbour_records(config))
    _desired = _schema.neighbour_urls(_desired_records)
    if target_name is not None:
        if read_mirror_state(config, target_name) is None:
            return False, f"unknown mirror {target_name!r}", []
        _targets = [target_name]
    else:
        _targets = list_mirrors(config)
    if not _targets:
        return True, "no mirrors configured — nothing to reconcile", []

    _results: 'list[dict]' = []
    _stage_root = os.path.join(config.dir_cache, 'mirror')
    for _name in _targets:
        _st = read_mirror_state(config, _name)
        if _st is None:
            _results.append({
                'name': _name, 'url': '', 'reachable': False,
                'changed': False, 'ok': False,
                'detail': 'state file vanished mid-run',
            })
            continue
        _url = _st.get('url', '')
        _ssh_key = _st.get('ssh_key') or None
        # reconcile-neighbours operates on the SIDECAR tree (coord root),
        # not the apt pool — that's where coord-head lives.
        _coord_url = coord_root_for(_url)
        _spec, _ssh_host = rsync_spec_for_url(_coord_url)
        _stage = os.path.join(_stage_root, _name, 'staging')
        try:
            os.makedirs(_stage, mode=0o755, exist_ok=True)
        except OSError as _e:
            _results.append({
                'name': _name, 'url': _url, 'reachable': False,
                'changed': False, 'ok': False,
                'detail': f"could not create staging dir: {_e}",
            })
            continue
        # 1. Optional flock acquire — ssh mirrors only
        _lock_proc = None
        if _ssh_host:
            _lock_proc = _transport.remote_flock_acquire(
                ssh_host=_ssh_host,
                lock_path='/var/lock/repo-coord.lock',
                timeout_sec=flock_timeout,
                ssh_key=_ssh_key,
            )
            if _lock_proc is None:
                _results.append({
                    'name': _name, 'url': _url, 'reachable': False,
                    'changed': False, 'ok': False,
                    'detail': f"flock acquire failed for {_ssh_host}",
                })
                continue
        try:
            # 2. Pull coord-head + sig
            _ok, _detail = _transport.pull_remote_coord(
                local_dest=_stage, remote_spec=_spec, ssh_key=_ssh_key,
            )
            if not _ok:
                _results.append({
                    'name': _name, 'url': _url, 'reachable': False,
                    'changed': False, 'ok': False,
                    'detail': f"pull failed: {_detail}",
                })
                continue
            _head = _head_mod.read_coord_head(_stage, signing_homedir)
            if _head is None:
                # No head present on the remote (or verify failed).  Treat
                # as a NOOP for reconcile — first-publish bootstraps it.
                _results.append({
                    'name': _name, 'url': _url, 'reachable': True,
                    'changed': False, 'ok': True,
                    'detail': ('no coord-head on remote (first publish '
                               'will initialise neighbours)'),
                })
                continue
            _have = _schema.canonicalize_neighbours(_head.get('neighbours') or [])
            if _have == _desired:
                _results.append({
                    'name': _name, 'url': _url, 'reachable': True,
                    'changed': False, 'ok': True,
                    'detail': 'already in sync',
                })
                continue
            # 5. Rewrite + re-sign locally.  Deep-copy the fetched head so
            # this peer's mutation can't leak into the next peer's read
            # (matters mostly under test isolation but also defends
            # against any future caller passing a shared reference).
            import copy as _copy
            _new = _copy.deepcopy(_head)
            _new['neighbours'] = _desired_records
            _ok = _head_mod.write_coord_head(_stage, _new, signing_homedir)
            if not _ok:
                _results.append({
                    'name': _name, 'url': _url, 'reachable': True,
                    'changed': False, 'ok': False,
                    'detail': 'tier-1 re-sign failed (see log)',
                })
                continue
            # 6. Push back
            _ok, _detail = _transport.push_coord_head(
                local_coord_dir=_stage,
                remote_dir_spec=_spec.rstrip('/') + '/',
                ssh_key=_ssh_key,
            )
            if not _ok:
                _results.append({
                    'name': _name, 'url': _url, 'reachable': True,
                    'changed': False, 'ok': False,
                    'detail': f"push failed: {_detail}",
                })
                continue
            _results.append({
                'name': _name, 'url': _url, 'reachable': True,
                'changed': True, 'ok': True,
                'detail': (f"neighbours updated: {len(_have)} → "
                           f"{len(_desired)}"),
            })
        finally:
            if _lock_proc is not None:
                _transport.remote_flock_release(_lock_proc)

    _overall_ok = all(_r['ok'] for _r in _results)
    _changed = sum(1 for _r in _results if _r.get('changed'))
    _bad = sum(1 for _r in _results if not _r['ok'])
    if _bad:
        _summary = f"{_bad}/{len(_results)} peer(s) FAILED reconcile"
    elif _changed:
        _summary = f"{_changed}/{len(_results)} peer(s) updated"
    else:
        _summary = f"{len(_results)} peer(s) already in sync"
    return _overall_ok, _summary, _results


def remove_mirror(config, *, url_or_name: str) -> 'tuple[bool, str]':
    """Unregister a mirror by URL or by name.  Returns (ok, detail).

    Phase 1: removes the state file ONLY.  Federation-removal propagation
    (updating every other mirror's `coord-head.neighbours` to drop this
    URL) lands in Phase 3.
    """
    if not isinstance(url_or_name, str) or not url_or_name:
        return False, "expected a mirror name or URL"
    # Try as name first
    _name: Optional[str] = None
    if _valid_name(url_or_name) and read_mirror_state(config, url_or_name) is not None:
        _name = url_or_name
    else:
        _name = find_mirror_by_url(config, url_or_name)
    if _name is None:
        return False, f"no mirror registered under {url_or_name!r}"
    if not delete_mirror_state(config, _name):
        return False, f"failed to delete {mirror_state_path(config, _name)}"
    return True, f"mirror {_name!r} unregistered (state file removed)"
