"""COORD-01 builder identity — Ed25519 sign/verify, keyring load.

Backed by openssl(1) — no Python crypto dep required on the operator's
host.  openssl 1.1.1+ supports Ed25519 natively (`openssl genpkey
-algorithm Ed25519`, `openssl pkeyutl -sign -rawin`).

Key layout:

  <coord_local>/identity/<builder-id>.pem      private key (mode 0600)
  <coord_local>/identity/<builder-id>.pub      public key (mode 0644)

`coord_local` is config.dir_coord_local (the per-builder workdir
under cache/, NOT the repo-coord/ tree).  The PUBLIC key gets
uploaded out-of-band to the repo's repo-coord/keyring/builders/<id>.pub
when the operator registers the builder.

A registered keyring loaded from <repo-coord-mirror>/keyring/builders/
gives builder-id → public-key-bytes.  Verifying a claim line scans
the keyring for the claim's `builder` field and runs openssl
pkeyutl -verify against that pubkey.

Signatures are raw 64 bytes (Ed25519 fixed-size); hex-encoded for
embedding in the JSONL `sig` field (128 hex chars).
"""

import os
import subprocess
import tempfile
import logging
from typing import Dict, Optional, Tuple

from . import schema as _schema

logger = logging.getLogger('athena')


# ───────────────────────── key generation ─────────────────────────


def builder_priv_path(identity_dir: str, builder_id: str) -> str:
    return os.path.join(identity_dir, builder_id + '.pem')


def builder_pub_path(identity_dir: str, builder_id: str) -> str:
    return os.path.join(identity_dir, builder_id + '.pub')


def generate_keypair(identity_dir: str, builder_id: str,
                     overwrite: bool = False) -> Tuple[str, str]:
    """Create a fresh Ed25519 keypair for `builder_id`.

    Returns (private_path, public_path) on success; raises OSError
    on subprocess failure or write failure.

    Without overwrite, refuses to clobber an existing private key —
    rotation is an explicit operator step.  The corresponding public
    key is always regenerated from the private (idempotent).
    """
    os.makedirs(identity_dir, mode=0o700, exist_ok=True)
    os.chmod(identity_dir, 0o700)
    _priv = builder_priv_path(identity_dir, builder_id)
    _pub = builder_pub_path(identity_dir, builder_id)
    if os.path.exists(_priv) and not overwrite:
        raise OSError(
            f"private key already exists at {_priv}; pass overwrite=True "
            "or delete it first (rotation discards old signatures)")
    _r = subprocess.run(
        ['openssl', 'genpkey', '-algorithm', 'Ed25519', '-out', _priv],
        capture_output=True, text=True,
    )
    if _r.returncode != 0:
        raise OSError(
            f"openssl genpkey failed (rc={_r.returncode}): "
            f"{_r.stderr.strip()[:200]}")
    os.chmod(_priv, 0o600)
    # Export public key in PEM (so the registered keyring is one stable
    # format).  openssl pkey -pubout is the inverse of genpkey.
    _r = subprocess.run(
        ['openssl', 'pkey', '-in', _priv, '-pubout', '-out', _pub],
        capture_output=True, text=True,
    )
    if _r.returncode != 0:
        raise OSError(
            f"openssl pkey -pubout failed (rc={_r.returncode}): "
            f"{_r.stderr.strip()[:200]}")
    os.chmod(_pub, 0o644)
    return _priv, _pub


# ───────────────────────── sign / verify ─────────────────────────


def _sign_bytes(private_path: str, data: bytes) -> bytes:
    """Raw Ed25519 signature over `data` (64 bytes).  Subprocess via
    openssl pkeyutl with -rawin (don't pre-hash; Ed25519 specifies
    SHA-512 internally per RFC 8032)."""
    _fd_in, _in = tempfile.mkstemp(prefix='.coord_sign_in.', suffix='.bin')
    _fd_out, _out = tempfile.mkstemp(prefix='.coord_sign_out.', suffix='.bin')
    try:
        os.write(_fd_in, data)
        os.close(_fd_in)
        os.close(_fd_out)
        _r = subprocess.run(
            ['openssl', 'pkeyutl', '-sign', '-inkey', private_path,
             '-rawin', '-in', _in, '-out', _out],
            capture_output=True, text=True,
        )
        if _r.returncode != 0:
            raise OSError(
                f"openssl pkeyutl -sign failed (rc={_r.returncode}): "
                f"{_r.stderr.strip()[:200]}")
        with open(_out, 'rb') as _fh:
            return _fh.read()
    finally:
        for _p in (_in, _out):
            try:
                os.unlink(_p)
            except OSError:
                pass


def _verify_bytes(public_path: str, data: bytes, sig: bytes) -> bool:
    """True iff `sig` is a valid Ed25519 signature of `data` by the
    holder of the private key matching `public_path`.  Subprocess via
    openssl pkeyutl -verify -pubin.

    Returns False on any subprocess failure (bad sig, missing file,
    malformed key) — callers should treat False as "untrusted" and
    discard the claim, not as a hard error.
    """
    _fd_in, _in = tempfile.mkstemp(prefix='.coord_verify_in.', suffix='.bin')
    _fd_sig, _sigpath = tempfile.mkstemp(
        prefix='.coord_verify_sig.', suffix='.bin')
    try:
        os.write(_fd_in, data)
        os.close(_fd_in)
        os.write(_fd_sig, sig)
        os.close(_fd_sig)
        _r = subprocess.run(
            ['openssl', 'pkeyutl', '-verify', '-pubin',
             '-inkey', public_path, '-rawin', '-in', _in,
             '-sigfile', _sigpath],
            capture_output=True, text=True,
        )
        # openssl returns 0 + "Signature Verified Successfully" on
        # match, non-zero otherwise.  Don't trust stdout text only —
        # rc is the source of truth.
        return _r.returncode == 0
    except OSError as _e:
        logger.warning(f"coord verify: {_e}")
        return False
    finally:
        for _p in (_in, _sigpath):
            try:
                os.unlink(_p)
            except OSError:
                pass


def sign_claim(claim: dict, private_path: str) -> dict:
    """Sign a claim record.  Returns a copy of the claim with `sig`
    set to the hex-encoded raw signature (128 hex chars).

    The signature covers schema.canonical_bytes(claim) — everything
    EXCEPT the sig field, sort-keys'd, ASCII-safe, no whitespace.
    """
    _payload = _schema.canonical_bytes(claim)
    _sig = _sign_bytes(private_path, _payload)
    _signed = dict(claim)
    _signed['sig'] = _sig.hex()
    return _signed


def verify_claim(claim: dict, public_path: str) -> bool:
    """True iff claim['sig'] (hex) is a valid Ed25519 signature over
    schema.canonical_bytes(claim) by the holder of the private key
    matching `public_path`."""
    _hex = claim.get('sig')
    if not isinstance(_hex, str):
        return False
    try:
        _sig = bytes.fromhex(_hex)
    except ValueError:
        return False
    if len(_sig) != 64:
        return False
    return _verify_bytes(public_path, _schema.canonical_bytes(claim), _sig)


# ───────────────────────── keyring load ─────────────────────────


def load_keyring(keyring_dir: str) -> Dict[str, str]:
    """Scan `keyring_dir` for *.pub files; return {builder_id: pub_path}.

    Builder-id is derived from the filename stem (`acme-builder.pub`
    → `acme-builder`).  Files lacking a `.pub` extension are skipped.
    Symlinks resolved.  An unreadable directory returns {}.
    """
    _out: Dict[str, str] = {}
    try:
        _entries = sorted(os.listdir(keyring_dir))
    except OSError:
        return _out
    for _e in _entries:
        if not _e.endswith('.pub'):
            continue
        _path = os.path.join(keyring_dir, _e)
        if not os.path.isfile(_path):
            continue
        _bid = _e[:-len('.pub')]
        _out[_bid] = _path
    return _out


def verify_claim_against_keyring(
    claim: dict, keyring: Dict[str, str],
    revoked: 'Optional[Dict[str, str]]' = None,
) -> bool:
    """Look up the claim's `builder` in the keyring and verify with that
    pubkey.  Returns False if:
      - builder not in keyring (unregistered identity)
      - builder is in revoked (revocation propagated through coord-head)
      - signature itself doesn't verify
    """
    _bid = claim.get('builder')
    if not isinstance(_bid, str):
        return False
    if revoked and _bid in revoked:
        return False
    _pub = keyring.get(_bid)
    if _pub is None:
        return False
    return verify_claim(claim, _pub)
