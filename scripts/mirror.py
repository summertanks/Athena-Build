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

import json
import logging
import os
from typing import List, Optional

logger = logging.getLogger('athena')


MIRROR_STATE_PREFIX = 'mirror.'
MIRROR_STATE_SUFFIX = '.state'
VALID_TYPES = frozenset({'ssh', 'local'})


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
    """Accept either a recognised scheme (`ssh://`, `file://`, `https://`)
    or a bare `user@host:/path` shorthand (rsync/ssh natively understands
    this).  Returns the normalised URL on success, None on rejection.

    No DNS / reachability check at this layer — that's `mirror status`'s
    job (Phase 3).
    """
    if not isinstance(url, str) or not url:
        return None
    _stripped = url.strip()
    if not _stripped:
        return None
    if _stripped.startswith(('ssh://', 'file://', 'https://', 'http://')):
        return _stripped
    # `user@host:/path` shorthand
    if '@' in _stripped and ':' in _stripped:
        return _stripped
    # Plain absolute path = local file mirror
    if _stripped.startswith('/'):
        return 'file://' + _stripped
    return None


def _infer_type(url: str) -> str:
    """Pick the default transport type from the URL prefix.  Operators
    can override via the `--type` arg at `mirror add` time."""
    if url.startswith(('ssh://',)):
        return 'ssh'
    if url.startswith(('file://',)):
        return 'local'
    if url.startswith(('https://', 'http://')):
        return 'local'  # treated as a webserver-served local mirror via rsync proxy
    if '@' in url and ':' in url:
        return 'ssh'
    return 'local'


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
) -> 'tuple[bool, str]':
    """Register a new mirror.  Returns (ok, detail).

    Phase 1 (this commit): writes the state file ONLY.  Federation-
    membership propagation to existing peers (the `reconcile-neighbours`
    step) lands in Phase 3 with the publish path.

    `seed_pin` is the timestamp to plant in `base` and `current` —
    typically the caller passes the local snapshot's current pin so the
    mirror starts at parity.
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
            "(use ssh://, file://, https://, /abs/path, or user@host:/path)")
    # Refuse duplicate URLs across mirror names
    for _other in list_mirrors(config):
        _ost = read_mirror_state(config, _other)
        if _ost and _ost.get('url') == _norm:
            return False, (
                f"URL {_norm!r} already registered as mirror {_other!r}")
    _type = type or _infer_type(_norm)
    if _type not in VALID_TYPES:
        return False, f"invalid type {_type!r}; expected one of {sorted(VALID_TYPES)}"
    _state = {
        'name':             name,
        'url':              _norm,
        'type':             _type,
        'ssh_key':          ssh_key or '',
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
    Used as the SOURCE OF TRUTH for the federation `neighbours` list."""
    _out: 'list[str]' = []
    for _n in list_mirrors(config):
        _st = read_mirror_state(config, _n)
        if _st and _st.get('url'):
            _out.append(_st['url'])
    return _out


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

    _desired = _schema.canonicalize_neighbours(all_mirror_urls(config))
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
            _new['neighbours'] = _desired
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
