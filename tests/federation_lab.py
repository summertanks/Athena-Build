"""Build-mode peer federation test scaffold.

Simulates a two-builder Asgard federation entirely locally — no SSH, no
Docker, no real mirror host — so the federation logic can be validated
without a two-machine setup.  A distribution OWNER and a build-mode PEER
share one "mirror" coord tree on the local filesystem; the scaffold drives
the real coord/ machinery:

  * Ed25519 claim signing + verification (coord.identity / coord.store)
  * GPG-signed coord-head (coord.head, shared tier-1 key)
  * canonical pkg.list/pool.list propagation (coord.config_manifest)
  * forward snapshot adoption (mirror.snapshot_adopt_forward)
  * ownership via claim view + generate_pending_claims `pulled_from` skip
  * decommission / revoke_builders (claims drop federation-wide)

It does NOT exercise the SSH/rsync/flock transport or the Docker build —
those need real hosts — but everything ABOVE the transport runs for real.

Run as a script:   python3 tests/federation_lab.py
Use as a harness:   from federation_lab import FederationLab
"""
import hashlib
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

import coord.identity as _identity            # noqa: E402
import coord.store as _store                  # noqa: E402
import coord.head as _head                    # noqa: E402
import coord.schema as _schema                # noqa: E402
import coord.config_manifest as _cfgman       # noqa: E402
import apt_pkg                                 # noqa: E402
import coord.publish as _publish              # noqa: E402
import coord.reconcile as _reconcile          # noqa: E402
import mirror as _mirror                      # noqa: E402
import utils as _utils                        # noqa: E402

apt_pkg.init_system()   # version_compare in the ownership matrix needs this

_DUMMY_INRELEASE_SHA = 'a' * 64


def _gen_tier1_key(signing_home):
    """Generate a throwaway tier-1 GPG key in `signing_home` (the homedir
    that write/read_coord_head use)."""
    import gnupg
    os.makedirs(signing_home, mode=0o700, exist_ok=True)
    _gpg = gnupg.GPG(gnupghome=signing_home)  # type: ignore[attr-defined]
    _key = _gpg.gen_key(_gpg.gen_key_input(
        name_real='Asgard Federation Tier-1', name_email='tier1@asgard.local',
        passphrase='', no_protection=True,
        key_type='RSA', key_length=2048, expire_date='1d'))
    assert _key.fingerprint, f"tier-1 keygen failed: {getattr(_key, 'stderr', _key)}"


class Builder:
    """A federation builder with its own working/coord tree + Ed25519 id,
    sharing the federation's tier-1 GPG home."""

    def __init__(self, root, builder_id, shared_gnupg, mode='distribution'):
        self.id = builder_id
        self.root = root
        self.build_mode = mode
        self.build_codename = 'thor'
        self.snapshot_enabled = True
        self.dir_gnupg = shared_gnupg          # signing_home = dir_gnupg/signing
        self.dir_coord = os.path.join(root, 'coord')
        self.dir_coord_claims = os.path.join(self.dir_coord, 'claims')
        self.dir_coord_identity = os.path.join(self.dir_coord, 'identity')
        self.dir_coord_fetched = os.path.join(self.dir_coord, 'fetched')
        self.dir_log = os.path.join(root, 'log')
        self.dir_repo = os.path.join(root, 'repo')
        self.pkglist_path = os.path.join(root, 'config', 'pkg.list')
        self.poollist_path = os.path.join(root, 'config', 'pool.list')
        self.pin = ''                          # local snapshot pin
        for _d in (self.dir_coord_claims, self.dir_coord_identity,
                   self.dir_coord_fetched, os.path.join(self.dir_log, 'build'),
                   self.dir_repo, os.path.dirname(self.pkglist_path)):
            os.makedirs(_d, exist_ok=True)
        self.priv, self.pub = _identity.generate_keypair(
            self.dir_coord_identity, builder_id)

    # config shim: the coord/ functions read these attributes off `config`
    @property
    def config(self):
        return self

    def set_lists(self, pkg, pool):
        with open(self.pkglist_path, 'w') as _f:
            _f.write(pkg)
        with open(self.poollist_path, 'w') as _f:
            _f.write(pool)

    def read_lists(self):
        with open(self.pkglist_path) as _f:
            _pkg = _f.read()
        _pool = ''
        if os.path.isfile(self.poollist_path):
            with open(self.poollist_path) as _f:
                _pool = _f.read()
        return _pkg, _pool

    def fake_build(self, package, version, arch='all'):
        """Materialize a built .deb + a phase=done build.json record so the
        publish path sees it (stands in for a real Docker build).  The bytes
        include this builder's id, so two builders that build the SAME
        filename produce DIFFERENT sha256 — a non-reproducible-build
        collision, exactly what the hash-conflict scan must catch."""
        _fn = f'{package}_{version}_{arch}.deb'
        _deb = os.path.join(self.dir_repo, _fn)
        with open(_deb, 'wb') as _f:
            _f.write(f'fake-deb:{package}:{version}:{self.id}'.encode())
        _sha = hashlib.sha256(open(_deb, 'rb').read()).hexdigest()
        _rec = _utils.new_build_record(
            package=package, intended_version=version, patch_set_hash='-')
        _rec.update(phase='done', built_version=version,
                    outputs=[_fn], output_hashes={_fn: _sha},
                    finished='2026-06-04T00:00:00Z')
        _utils.write_build_record(os.path.join(self.dir_log, 'build'), _rec)
        return _fn


class FederationLab:
    """A local two-or-more builder federation over a filesystem 'mirror'."""

    def __init__(self, root):
        self.root = root
        self.shared_gnupg = os.path.join(root, 'shared-gnupg')
        self.signing_home = os.path.join(self.shared_gnupg, 'signing')
        _gen_tier1_key(self.signing_home)
        # the shared mirror coord tree + pool
        self.mirror_coord = os.path.join(root, 'mirror', 'coord')
        self.mirror_keyring = os.path.join(self.mirror_coord, 'keyring',
                                           'builders')
        self.mirror_claims = os.path.join(self.mirror_coord, 'claims')
        for _d in (self.mirror_keyring, self.mirror_claims):
            os.makedirs(_d, exist_ok=True)

    def builder(self, builder_id, mode='distribution'):
        return Builder(os.path.join(self.root, builder_id), builder_id,
                       self.shared_gnupg, mode=mode)

    # ── operations (model the rsync push/pull over a shared fs mirror) ──
    def register(self, b):
        """Upload the builder's pubkey + adopt the owner's canonical config."""
        shutil.copy(_identity.builder_pub_path(b.dir_coord_identity, b.id),
                    os.path.join(self.mirror_keyring, f'{b.id}.pub'))
        _h = _head.read_coord_head(self.mirror_coord, self.signing_home)
        if _h and _h.get('config_sha256'):
            _ok, _ = _cfgman.apply_canonical_config(
                self.mirror_coord, str(_h['config_sha256']),
                b.pkglist_path, b.poollist_path)
            return _ok
        return False

    def publish(self, b, snapshot):
        """local_publish into the builder's coord, then 'push' its claims +
        pubkey to the mirror and re-sign the mirror coord-head (owner writes
        the canonical config; a build peer preserves the existing pin)."""
        b.pin = snapshot
        _created, _ = _publish.local_publish(
            builder_id=b.id, config=b, private_key_path=b.priv,
            public_key_path=b.pub, snapshot_pin=snapshot,
            read_build_record=_utils.read_build_record)
        # push: copy our jsonl + pubkey to the mirror
        _jsonl = os.path.join(b.dir_coord_claims, f'{b.id}.jsonl')
        if os.path.isfile(_jsonl):
            shutil.copy(_jsonl, os.path.join(self.mirror_claims,
                                             f'{b.id}.jsonl'))
        shutil.copy(_identity.builder_pub_path(b.dir_coord_identity, b.id),
                    os.path.join(self.mirror_keyring, f'{b.id}.pub'))
        # canonical config (owner only)
        _prev = _head.read_coord_head(self.mirror_coord, self.signing_home)
        _config_sha = (_prev or {}).get('config_sha256')
        if b.build_mode != 'build':
            _sha = _cfgman.write_canonical_config(
                self.mirror_coord, b.pkglist_path, b.poollist_path)
            if _sha:
                _config_sha = _sha
        # re-sign the mirror head with refreshed last_seqs + snapshot
        _keyring = _identity.load_keyring(self.mirror_keyring)
        _revoked = (_prev or {}).get('revoked_builders') or {}
        _by = _store.read_all_claims(self.mirror_claims, _keyring, _revoked)
        _last_seqs = {_bid: max((_c.get('seq', 0) for _c in _cl), default=0)
                      for _bid, _cl in _by.items()}
        _new = _schema.new_coord_head(
            inrelease_sha256=_DUMMY_INRELEASE_SHA,
            snapshot=_schema.new_snapshot_state(
                base=snapshot, current=snapshot, published=snapshot,
                external=False),
            last_seqs=_last_seqs, head_time='2026-06-04T00:00:00Z',
            revoked_builders=_revoked or None, config_sha256=_config_sha)
        assert _head.write_coord_head(self.mirror_coord, _new,
                                      self.signing_home)
        return _created

    def pull(self, b):
        """Read the mirror head (verify), adopt its snapshot forward, refresh
        the canonical config, and return the verified claim view."""
        _h = _head.read_coord_head(self.mirror_coord, self.signing_home)
        assert _h is not None, "coord-head failed to verify"
        _remote = str((_h.get('snapshot') or {}).get('current') or '')
        _adopt = _mirror.snapshot_adopt_forward(b.pin, _remote)
        if _adopt:
            b.pin = _adopt
        if _h.get('config_sha256'):
            _cfgman.apply_canonical_config(
                self.mirror_coord, str(_h['config_sha256']),
                b.pkglist_path, b.poollist_path)
        _keyring = _identity.load_keyring(self.mirror_keyring)
        _revoked = _h.get('revoked_builders') or {}
        return _store.read_all_claims(self.mirror_claims, _keyring, _revoked)

    def decommission(self, target_id):
        """Add target_id to the mirror head's revoked_builders + re-sign."""
        _h = _head.read_coord_head(self.mirror_coord, self.signing_home)
        assert _h is not None
        _revoked = dict(_h.get('revoked_builders') or {})
        _revoked[target_id] = '2026-06-05T00:00:00Z'
        _new = _schema.new_coord_head(
            inrelease_sha256=str(_h.get('inrelease_sha256') or ''),
            snapshot=_h.get('snapshot') or {},
            last_seqs=dict(_h.get('last_seqs') or {}),
            head_time='2026-06-05T00:00:00Z',
            neighbours=_h.get('neighbours') or [],
            revoked_builders=_revoked,
            config_sha256=_h.get('config_sha256'))
        assert _head.write_coord_head(self.mirror_coord, _new,
                                      self.signing_home)

    def view(self):
        """The verified merged claim view {builder_id: [claims]} (revoked
        builders dropped per the coord-head)."""
        _h = _head.read_coord_head(self.mirror_coord, self.signing_home)
        _keyring = _identity.load_keyring(self.mirror_keyring)
        _revoked = (_h or {}).get('revoked_builders') or {}
        return _store.read_all_claims(self.mirror_claims, _keyring, _revoked)

    def scan_conflicts(self):
        """Run the real cross-builder hash-conflict scan over the mirror."""
        return _reconcile.detect_hash_conflicts(self.view())

    def owners(self):
        """Per-filename ownership projection of the mirror's claim view."""
        return _store.project_owners(self.view())


def _claim_filenames(claim_view):
    return {_c.get('filename')
            for _claims in claim_view.values() for _c in _claims}


def run(verbose=True):
    """Walk the full build-mode peer workflow; raise AssertionError on any
    invariant break.  Returns a short result summary."""
    def _step(msg):
        if verbose:
            print(f"  ✓ {msg}")

    with tempfile.TemporaryDirectory() as _td:
        _lab = FederationLab(_td)
        _owner = _lab.builder('owner', mode='distribution')
        _peer = _lab.builder('peer', mode='build')

        # 1. owner publishes the base distribution (firefox) at snapshot S1
        _owner.set_lists('firefox\nvlc\n', 'grub-pc\n')
        _owner.fake_build('firefox', '1.0')
        assert _lab.publish(_owner, 'S1') == 1
        _h = _head.read_coord_head(_lab.mirror_coord, _lab.signing_home)
        assert _h and _h.get('config_sha256') and \
            (_h['snapshot']['current'] == 'S1')
        _owner_cfg_sha = _h['config_sha256']
        _step("owner published firefox @ S1; mirror head signed + config pinned")

        # 2. peer registers → adopts the owner's canonical pkg.list/pool.list
        _peer.set_lists('STALE\n', 'STALE\n')
        assert _lab.register(_peer)
        assert _peer.read_lists() == ('firefox\nvlc\n', 'grub-pc\n')
        _step("peer registered; canonical pkg.list/pool.list adopted")

        # 3. peer pulls → snapshot auto-adopts forward to S1, sees firefox
        _view = _lab.pull(_peer)
        assert _peer.pin == 'S1'
        assert any(_fn and _fn.startswith('firefox_')
                   for _fn in _claim_filenames(_view))
        _step("peer pulled; pin advanced → S1; sees owner's firefox claim")

        # 4. peer advances to S2, builds vlc (its owned subset), publishes
        _peer.fake_build('vlc', '2.0')
        assert _lab.publish(_peer, 'S2') == 1
        _h = _head.read_coord_head(_lab.mirror_coord, _lab.signing_home)
        assert _h['snapshot']['current'] == 'S2'
        # peer (build mode) must NOT have overwritten the owner's config pin
        assert _h.get('config_sha256') == _owner_cfg_sha, \
            "build-mode peer must preserve the owner's canonical config pin"
        _step("peer advanced → S2, published vlc (build-mode, owned-only)")

        # 5. owner pulls back → adopts S2, sees the peer's vlc
        _view = _lab.pull(_owner)
        assert _owner.pin == 'S2'
        _fns = _claim_filenames(_view)
        assert any(_fn and _fn.startswith('vlc_') for _fn in _fns)
        assert any(_fn and _fn.startswith('firefox_') for _fn in _fns)
        _step("owner synced back; pin advanced → S2; sees peer's vlc claim")

        # 6. decommission the peer → its claims drop, vlc becomes no-owner
        _lab.decommission(_peer.id)
        _view = _lab.pull(_owner)
        _fns = _claim_filenames(_view)
        assert not any(_fn and _fn.startswith('vlc_') for _fn in _fns), \
            "decommissioned peer's claims must drop federation-wide"
        assert any(_fn and _fn.startswith('firefox_') for _fn in _fns)
        _step("peer decommissioned; its vlc claim dropped; owner's firefox kept")

    return {'ok': True, 'builders': ['owner', 'peer']}


def run_hash_conflict(verbose=True):
    """Two builders independently build the SAME filename with different
    bytes (non-reproducible build); both publish; the cross-builder
    hash-conflict scan must flag it CRITICAL."""
    def _step(msg):
        if verbose:
            print(f"  ✓ {msg}")
    with tempfile.TemporaryDirectory() as _td:
        _lab = FederationLab(_td)
        _a = _lab.builder('alice', mode='distribution')
        _b = _lab.builder('bob', mode='build')
        _a.set_lists('foo\n', '')

        _a.fake_build('foo', '1.0')           # alice's bytes
        _lab.publish(_a, 'S1')
        assert not [_f for _f in _lab.scan_conflicts()
                    if _f.severity == 'CRITICAL'], "single publisher: no conflict"
        _step("alice published foo_1.0 — no conflict")

        _b.fake_build('foo', '1.0')           # bob's bytes (different sha)
        _lab.publish(_b, 'S1')
        _crit = [_f for _f in _lab.scan_conflicts()
                 if _f.severity == 'CRITICAL']
        assert _crit, "two distinct shas for one filename must be CRITICAL"
        assert any('foo_1.0' in (_f.message or '') for _f in _crit)
        _step("bob published foo_1.0 with different bytes — hash conflict "
              "detected CRITICAL")
    return {'ok': True, 'critical': True}


def run_ownership_transfer(verbose=True):
    """Exercise the publish ownership decision matrix against a real owner
    record: no-owner → take; other-owner higher version → transfer;
    same/lower → ownership_blocked; different bytes → frozen_bytes_blocked."""
    def _step(msg):
        if verbose:
            print(f"  ✓ {msg}")

    def _cand(filename, sha, version):
        return {'filename': filename, 'sha256': sha, 'built_version': version}

    with tempfile.TemporaryDirectory() as _td:
        _lab = FederationLab(_td)
        _a = _lab.builder('alice', mode='distribution')
        _lab.builder('bob', mode='build')     # bob need only exist as a name
        _a.set_lists('foo\n', '')
        _fn = _a.fake_build('foo', '1.0')
        _lab.publish(_a, 'S1')
        _owners = _lab.owners()
        assert _fn in _owners and _owners[_fn]['builder'] == 'alice'
        _a_sha = _owners[_fn]['sha256']
        _step(f"alice owns {_fn} (sha {_a_sha[:12]})")

        # no existing owner → bob takes a brand-new filename
        _kept, _blk = _publish.filter_pending_by_ownership(
            'bob', [_cand('bar_1.0_all.deb', 'c' * 64, '1.0')], _owners)
        assert _kept and not _blk
        _step("no owner → bob takes bar_1.0")

        # other owner, strictly higher version, same bytes → ownership transfers
        _kept, _blk = _publish.filter_pending_by_ownership(
            'bob', [_cand(_fn, _a_sha, '2.0')], _owners)
        assert _kept and not _blk, "higher version must transfer ownership"
        _step("alice-owned, bob version 2.0 > 1.0 → ownership transfers")

        # other owner, not strictly higher → ownership_blocked
        _kept, _blk = _publish.filter_pending_by_ownership(
            'bob', [_cand(_fn, _a_sha, '0.9')], _owners)
        assert not _kept and _blk and 'not strictly higher' in _blk[0]['reason']
        _step("alice-owned, bob version 0.9 ≤ 1.0 → ownership_blocked")

        # other owner, different bytes → frozen_bytes_blocked
        _kept, _blk = _publish.filter_pending_by_ownership(
            'bob', [_cand(_fn, 'd' * 64, '2.0')], _owners)
        assert not _kept and _blk and 'frozen pool copy' in _blk[0]['reason']
        _step("alice-owned, different bytes → frozen_bytes_blocked "
              "(use `mirror reclaim` / bump version)")
    return {'ok': True}


if __name__ == '__main__':
    print("federation_lab: build-mode peer end-to-end simulation")
    print(f"\nPASS — happy path: {run(verbose=True)}")
    print("\nfederation_lab: hash-conflict scenario")
    print(f"\nPASS — hash conflict: {run_hash_conflict(verbose=True)}")
    print("\nfederation_lab: ownership-transfer scenario")
    print(f"\nPASS — ownership matrix: {run_ownership_transfer(verbose=True)}")
