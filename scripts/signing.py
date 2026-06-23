"""Project signing key — generate, locate, inspect, verify.

Phase 1: the *key management* surface.  Sign-Release wiring
into ``cmd_index_repo`` is a follow-on (CONF-01 / CONF-02 phase 2).

What this module owns:

- A separate gpg homedir at ``<dir_gnupg>/signing/`` for the project's
  private signing key.  Kept distinct from ``dir_gnupg`` itself (which
  holds the upstream debian-archive-keyring used by
  ``utils.verify_inrelease`` to verify mirror InRelease files).  Mixing
  the two would cross trust scopes — our signing key has no business
  being trusted as a mirror-verification key, and vice-versa.

- A stable export path at ``<dir_gnupg>/signing/athena-archive-keyring.gpg``
  for the public key.  Stable so future callers (cmd_index_repo,
  build_chroot's keyring install) can read from a known location
  without re-deriving it.

- Subprocess wrappers around gpg for: generate, list-secret-keys
  parsing, sign+verify roundtrip.

What this module deliberately does NOT do:

- Sign Release files.  That's CONF-01 / CONF-02 phase 2's job; lives
  with the indexing flow once it exists.
- Auto-generate on first call from another module.  Key generation is
  a security-relevant action; the operator runs ``generate_signing_key``
  explicitly (or rejects via prompt), and overwriting an existing key
  needs a ``force`` arg.
- Import an externally-generated key.  Future BYOK ticket if/when an
  operator wants to bring an existing GPG identity to Athena.
"""
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('athena')


# ─── Paths ──────────────────────────────────────────────────────────────────

def signing_home(config) -> str:
    """Path to the signing key's gnupg homedir.  Sibling of (not equal
    to) ``config.dir_gnupg`` — see module docstring for the trust-scope
    separation rationale."""
    return os.path.join(config.dir_gnupg, 'signing')


def signing_pubkey_path(config) -> str:
    """Stable path where the exported public keyring lives.  Read by
    cmd_index_repo (when it lands) to ship into ``repo/`` so consumers
    can pin via ``[signed-by=...]``, and by build_chroot to install
    into ``/usr/share/keyrings/athena-archive-keyring.gpg`` (when that
    wiring lands too)."""
    return os.path.join(signing_home(config), 'athena-archive-keyring.gpg')


def _ensure_homedir(config) -> bool:
    """Create signing_home() with mode 0700 if absent.  Idempotent."""
    path = signing_home(config)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        return True
    except OSError as e:
        logger.error(f"signing._ensure_homedir({path}): {e}")
        return False


# ─── UID parsing helpers ────────────────────────────────────────────────────

def _parse_uid_name(uid: str) -> str:
    """'Athena Build <athena@local>' → 'Athena Build'.  Bare names
    pass through (used as both Name-Real and Name-Email by gpg's batch
    parameter file)."""
    return uid.split('<', 1)[0].strip()


def _parse_uid_email(uid: str) -> str:
    """'Athena Build <athena@local>' → 'athena@local'.  Bare strings
    pass through unchanged."""
    if '<' in uid and '>' in uid:
        return uid.split('<', 1)[1].split('>', 1)[0].strip()
    return uid.strip()


# ─── Colons-format parser ──────────────────────────────────────────────────

def parse_secret_keys_colons(output: str) -> List[Dict[str, str]]:
    """Parse ``gpg --list-secret-keys --with-colons`` output into a list
    of key-info dicts.

    GPG's colons format is documented at
    https://www.gnupg.org/documentation/manuals/gnupg/cli/Listing-Keys.html
    Each line is colon-separated with a leading record-type marker:

        sec  — start of a new secret key (we collect created/expires here)
        fpr  — fingerprint (belongs to the most recent sec)
        uid  — user id (we keep the first uid record per key)
        grp  — grip; ignored
        ssb  — secret subkey; ignored at this level

    This parser is deliberately minimal — it pulls only the fields
    ``cmd_verify_signing_key`` and ``print signing`` actually display.
    Versions of gpg-mfd that grow / rename columns are tolerated by
    the bounds-checked indexing.
    """
    keys: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for line in output.splitlines():
        parts = line.split(':')
        if not parts:
            continue
        rec = parts[0]
        if rec == 'sec':
            if current is not None:
                keys.append(current)
            current = {
                'keyid':       parts[4] if len(parts) > 4 else '',
                'created':     parts[5] if len(parts) > 5 else '',
                'expires':     parts[6] if len(parts) > 6 else '',
                'fingerprint': '',
                'uid':         '',
            }
        elif rec == 'fpr' and current is not None and not current['fingerprint']:
            current['fingerprint'] = parts[9] if len(parts) > 9 else ''
        elif rec == 'uid' and current is not None and not current['uid']:
            # The first uid record is the primary uid for this key.
            current['uid'] = parts[9] if len(parts) > 9 else ''
    if current is not None:
        keys.append(current)
    return keys


# ─── Public API ────────────────────────────────────────────────────────────

def format_gpg_time(s: str, default: str = '') -> str:
    """Render a gpg `--with-colons` timestamp field as a human-readable
    UTC date.

    gpg's colon-format output (per `doc/DETAILS` in upstream gnupg)
    encodes the `created` and `expires` fields as Unix epoch seconds
    in column 6 and 7 of the `sec`/`pub` record.  When the field is
    empty, the key has no expiration (a deliberate operator choice
    in our gen-key flow — Athena keys are rotated manually).

    Returns `default` when `s` is empty/falsy; this lets the expires
    field render as "(never — manual rotation)" without an `or`-chain
    in every f-string call site that touches it.  Returns `s`
    unmodified when it doesn't parse as an integer (degrades to the
    raw upstream value rather than swallowing the field).
    """
    if not s:
        return default
    try:
        return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime(
            '%Y-%m-%d %H:%M UTC',
        )
    except (ValueError, OSError):
        return s


def get_key_info(config) -> Optional[Dict[str, str]]:
    """Return the parsed key-info dict for the configured signing uid,
    or None if no matching secret key exists in ``signing_home(config)``.

    None is also returned if the homedir doesn't exist yet (operator
    hasn't run generate_signing_key) or if gpg itself is missing.
    """
    home = signing_home(config)
    if not os.path.isdir(home):
        return None
    if shutil.which('gpg') is None:
        return None
    proc = subprocess.run(
        ['gpg', '--homedir', home,
         '--list-secret-keys', '--with-colons',
         config.signing_key_uid],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        # gpg returns 2 when the requested uid isn't present — not an
        # error from the operator's perspective, just "no key yet".
        return None
    keys = parse_secret_keys_colons(proc.stdout)
    return keys[0] if keys else None


def actual_signing_uid(config) -> Optional[str]:
    """The primary UID of whatever signing key is ACTUALLY present in
    signing_home — listed WITHOUT a UID filter, so it reports the truth even
    when config.signing_key_uid is stale or wrong.  None if no key / no gpg.

    Onboarding uses this so a federation peer adopts the IMPORTED tier-1 key's
    real UID instead of trusting a (possibly mismatched) configured value."""
    try:
        home = signing_home(config)
    except AttributeError:                # config without a signing homedir
        return None
    if not home or not os.path.isdir(home) or shutil.which('gpg') is None:
        return None
    proc = subprocess.run(
        ['gpg', '--homedir', home, '--list-secret-keys', '--with-colons'],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    keys = parse_secret_keys_colons(proc.stdout)
    return keys[0]['uid'] if keys and keys[0].get('uid') else None


def import_key(homedir: str, key_path: str) -> 'Tuple[bool, str]':
    """Import a GPG key file (public or secret) into the gnupg ``homedir``
    via ``gpg --import``.  Returns ``(ok, detail)``.

    Used by the `configure` federation flow: a peer copies the federation's
    tier-1 key off the origin and points the wizard at the file; this imports
    it (first into a throwaway homedir to validate against the mirror's signed
    coord-head, then into ``signing_home`` once it checks out).  Implements the
    long-deferred BYOK import noted in this module's header."""
    if shutil.which('gpg') is None:
        return False, "gpg not on PATH"
    if not os.path.isfile(key_path):
        return False, f"key file not found: {key_path}"
    try:
        os.makedirs(homedir, mode=0o700, exist_ok=True)
        os.chmod(homedir, 0o700)
    except OSError as e:
        return False, f"cannot create gnupg homedir {homedir}: {e}"
    proc = subprocess.run(
        ['gpg', '--homedir', homedir, '--batch', '--import', key_path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, f"gpg --import failed: {proc.stderr.strip()[:200]}"
    return True, 'imported'


def generate_key(config, _key_length: int = 4096) -> bool:
    """Generate a fresh signing keypair under ``signing_home(config)``.

    Caller (``cmd_generate_signing_key``) is responsible for the
    "already exists, use force?" gate and the operator confirmation
    prompt — this function unconditionally writes a new key.  Returns
    True on success.  On failure logs the gpg stderr and returns False.

    ``_key_length`` is a private knob for tests (RSA-2048 in tests is
    ~10× faster than RSA-4096 production).  Production callers should
    leave it at the default.
    """
    if shutil.which('gpg') is None:
        logger.error("signing.generate_key: gpg not on PATH")
        return False
    if not _ensure_homedir(config):
        return False

    uid = config.signing_key_uid
    home = signing_home(config)
    params = (
        # %no-protection — no passphrase, since cmd_index_repo signs
        # non-interactively in batch mode.  Operator who wants a
        # passphrased key can BYOK (future ticket) or wrap with
        # gpg-agent caching.
        '%no-protection\n'
        'Key-Type: RSA\n'
        f'Key-Length: {_key_length}\n'
        'Subkey-Type: RSA\n'
        f'Subkey-Length: {_key_length}\n'
        f'Name-Real: {_parse_uid_name(uid)}\n'
        f'Name-Email: {_parse_uid_email(uid)}\n'
        # Expire-Date: 0 = never expires.  Rotation is manual via
        # `generate_signing_key force` once docs/release.md
        # documents the procedure.
        'Expire-Date: 0\n'
        '%commit\n'
    )
    proc = subprocess.run(
        ['gpg', '--homedir', home, '--batch', '--gen-key'],
        input=params, capture_output=True, text=True
    )
    if proc.returncode != 0:
        logger.error(
            f"signing.generate_key: gpg --gen-key failed: "
            f"{proc.stderr.strip()}"
        )
        return False

    # Export the public key to the stable path so downstream callers
    # (cmd_index_repo, build_chroot keyring install) don't need to
    # re-derive it.  Overwrites any prior export.
    pub_path = signing_pubkey_path(config)
    proc = subprocess.run(
        ['gpg', '--homedir', home, '--batch', '--yes',
         '--export', '--output', pub_path, uid],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        logger.error(
            f"signing.generate_key: gpg --export failed: "
            f"{proc.stderr.strip()}"
        )
        return False
    return True


def verify_key(config) -> Tuple[bool, str]:
    """Sign+verify a small test payload to confirm the current key is
    usable end-to-end.  Returns (ok, message) — ok=True if both the
    sign and the verify steps succeed.

    Catches: missing key, expired key, missing pubkey, gpg-agent
    issues, key with passphrase (which would prompt and fail in batch
    mode).  Doesn't catch: key compromised externally, key trusted by
    nobody — those need out-of-band evidence.
    """
    if get_key_info(config) is None:
        return False, ("no signing key for "
                       f"'{config.signing_key_uid}' — run "
                       "generate_signing_key first")
    if shutil.which('gpg') is None:
        return False, "gpg not on PATH"

    home = signing_home(config)
    uid = config.signing_key_uid
    test_data = b'athena signing-key roundtrip test'

    # Two named tempfiles in the same dir; auto-cleaned on exit.
    with tempfile.NamedTemporaryFile() as data_f, \
         tempfile.NamedTemporaryFile(suffix='.sig') as sig_f:
        data_f.write(test_data)
        data_f.flush()
        sig_path = sig_f.name
        # Sign — detached binary signature
        proc = subprocess.run(
            ['gpg', '--homedir', home, '--batch', '--yes',
             '--local-user', uid,
             '--detach-sign', '--output', sig_path, data_f.name],
            capture_output=True, text=True
        )
        if proc.returncode != 0:
            return False, f"sign failed: {proc.stderr.strip()}"
        # Verify — detached signature against the original data
        proc = subprocess.run(
            ['gpg', '--homedir', home, '--batch',
             '--verify', sig_path, data_f.name],
            capture_output=True, text=True
        )
        if proc.returncode != 0:
            return False, f"verify failed: {proc.stderr.strip()}"
    return True, 'sign + verify roundtrip OK'
