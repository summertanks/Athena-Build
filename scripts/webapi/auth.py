"""API-key handling for the HTTP interface.

Deliberately fastapi-free so key generation/verification is importable
and testable on hosts without python3-fastapi installed.  Pattern
mirrors the HMAC metrics key: auto-generated secret, mode 0600,
lives under config/ and is never committed (config/*.key is the
established secret-material shape — see the gnupg/key denylist in
docs/plans/api-01-web-api.md).
"""

import hmac
import logging
import os
import secrets

logger = logging.getLogger('athena.webapi')

# 32 bytes → ~43 url-safe base64 chars (secrets.token_urlsafe), not hex;
# urlsafe tokens keep curl/header usage painless.
_KEY_BYTES = 32


def ensure_api_key(path: str) -> str:
    """Return the API key at ``path``, generating it (mode 0600) on
    first use.  Raises OSError on an unwritable location — the server
    must not start with no key, that would mean an open API."""
    _existing = load_api_key(path)
    if _existing:
        return _existing
    _key = secrets.token_urlsafe(_KEY_BYTES)
    # O_EXCL so two concurrent first-starts can't truncate each other;
    # the loser re-reads the winner's key.
    try:
        _fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # The winner created the file with O_EXCL but writes the key AFTER, so
        # a single re-read can race in before the key lands.  Poll briefly for
        # a non-empty key before giving up.
        import time
        for _ in range(50):                       # ~0.5s total
            _retry = load_api_key(path)
            if _retry:
                return _retry
            time.sleep(0.01)
        raise
    with os.fdopen(_fd, 'w') as _fh:
        _fh.write(_key + '\n')
    logger.info(f"webapi: generated new API key at {path}")
    return _key


def load_api_key(path: str) -> str:
    """Read the key at ``path``; '' when absent/empty (caller decides
    whether that's a generate-now or a hard-fail situation)."""
    try:
        with open(path) as _fh:
            return _fh.read().strip()
    except OSError:
        return ''


def verify_key(provided: str, actual: str) -> bool:
    """Constant-time comparison; empty actual NEVER verifies (an absent
    key file must not become an accidental allow-all)."""
    if not actual or not provided:
        return False
    return hmac.compare_digest(provided.encode(), actual.encode())
