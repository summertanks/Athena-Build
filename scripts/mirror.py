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
