"""Athena tests — federation coordination (coord/ - schema, identity, store, head, transport, publish, reconcile).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile

from _test_helpers import (  # noqa: F401
    _ROOT,
    _coord_modules,
    _coord_schema_v2_modules,
    _mirror_module,
    _mirror_pull_harness,
    _new_pending_claim,
    _openssl_available,
    _sta30_publish_cfg,
    _write_clearsigned_inrelease,
)




def test_publish_obsolescence_view_includes_deprecations():
    """Regression (audit #96): step 6c's supersession-obsolescence view must
    include the 6b deprecation claims, so a file just deprecated (ownership
    released to the commons) is not re-asserted as obsolete (ownership
    retained) within the same publish."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _p
    _src = inspect.getsource(_p)
    assert '+ list(_pending) + _appended_deprecations' in _src, (
        "step 6c view must merge the 6b deprecation claims so they win the fold")



def test_append_claim_writes_all_bytes():
    """Regression (audit #106): append_claim must loop os.write until the whole
    JSONL line is written — a short write would otherwise silently truncate a
    ledger line, breaking the one-complete-line-per-write invariant."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.store as _store
    assert 'while _written < len(_line)' in inspect.getsource(_store), (
        "append_claim must write-all (loop os.write), not a single os.write "
        "whose return value is discarded")



def test_run_rsync_streaming_flushes_residual_tail():
    """Regression (audit #108): _run_rsync_streaming must flush the residual
    _buf after wait() — rsync's final error line may not be newline-terminated
    and would otherwise be dropped from the failure tail."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.transport as _t
    assert '_resid = _buf.strip()' in inspect.getsource(_t), (
        "the residual un-newline-terminated tail line must be flushed after "
        "_proc.wait()")



def test_push_dist_tree_excludes_pool_artifacts():
    """STA-28: push_dist_tree rsyncs dists/<codename>/ with --delete, and
    since CONF-01 the pool .debs live INSIDE that tree.  Pool artifacts
    must be EXCLUDED (not merely `--filter=P` protected): a protect rule
    only stops receiver-side DELETION, but rsync -aH still transfers/
    OVERWRITES a .deb whose bytes differ — so a local-ahead rebuild
    silently rewrote frozen remote bytes (bypassing RECLAIM-01).
    `--exclude` removes pool artifacts from the transfer set entirely,
    so this pass neither overwrites NOR deletes them; the pool is owned
    solely by push_single_deb.  --delete still reaps stale index files."""
    import sys as _sys
    import types as _types
    import tempfile
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.transport as _tr

    _captured = {}

    def _fake_run(argv, **k):
        _captured['argv'] = argv
        return _types.SimpleNamespace(returncode=0, stdout='', stderr='')

    _orig = _tr.subprocess
    _tr.subprocess = _types.SimpleNamespace(run=_fake_run)
    try:
        with tempfile.TemporaryDirectory() as _tmp:
            _ok, _detail = _tr.push_dist_tree(
                local_dist_dir=_tmp,
                remote_dir_spec='user@host:/srv/asgard/dists/thor')
    finally:
        _tr.subprocess = _orig
    assert _ok, _detail
    _argv = _captured['argv']
    assert '--delete' in _argv, _argv
    assert '--exclude=*.deb' in _argv, _argv
    assert '--exclude=*.udeb' in _argv, _argv
    # The old protect-only rules (which allowed overwrite) must be gone.
    assert '--filter=P *.deb' not in _argv, _argv
    assert '--filter=P *.udeb' not in _argv, _argv



def test_transport_remote_sha256_parses_digest_and_guards():
    """remote_sha256 runs `ssh … sha256sum <path>`, returns the 64-hex digest
    on success, and None for an unparseable spec / non-zero rc / short output —
    so the release-asset verify can distinguish mismatch from unverifiable."""
    import types as _types

    import coord.transport as _tr
    _digest = 'a' * 64
    _orig = _tr.subprocess

    def _run_factory(rc, out):
        def _run(argv, **k):
            _run.argv = argv
            return _types.SimpleNamespace(returncode=rc, stdout=out, stderr='')
        return _run
    try:
        # success → digest; the path lands as a single ssh arg
        _tr.subprocess = _types.SimpleNamespace(
            run=_run_factory(0, f'{_digest}  asgard/iso/x.iso\n'),
            SubprocessError=Exception)
        assert _tr.remote_sha256('user@host:asgard/iso/x.iso', 'k.key') == _digest
        # no colon → can't form host:path → None (no ssh attempted)
        assert _tr.remote_sha256('not-a-spec') is None
        # non-zero rc (missing file) → None
        _tr.subprocess.run = _run_factory(1, '')
        assert _tr.remote_sha256('user@host:nope') is None
        # short/garbage output → None (the 64-char guard)
        _tr.subprocess.run = _run_factory(0, 'deadbeef  x\n')
        assert _tr.remote_sha256('user@host:x') is None
    finally:
        _tr.subprocess = _orig



def test_fed04_flock_scripts_shape():
    """FED-04: the held-shell records builder-id + PID sidecars and a
    heartbeat; the status probe checks held-ness + heartbeat age; the break
    script kills the holder + clears sidecars."""
    import coord.transport as _tr
    _lock = '/var/lock/repo-coord.lock'
    _hold = _tr._flock_hold_script(_lock, 'BS1', heartbeat_sec=15)
    assert f'echo BS1 > {_lock}.holder' in _hold
    assert f'echo $$ > {_lock}.pid' in _hold
    assert 'sleep 15' in _hold and f'touch {_lock}.holder' in _hold
    assert 'COORD_LOCK_ACQUIRED' in _hold and 'cat;' in _hold
    assert f'rm -f {_lock}.holder {_lock}.pid' in _hold

    _status = _tr._flock_status_script(_lock)
    assert f'flock -n {_lock} -c true' in _status
    assert 'HELD=' in _status and 'AGE=' in _status

    _break = _tr._flock_break_script(_lock)
    assert 'kill -9' in _break and 'fuser -k' in _break
    assert f'rm -f {_lock}.holder {_lock}.pid' in _break
    assert 'COORD_LOCK_BROKEN' in _break



def test_fed04_parse_flock_status_and_staleness():
    """FED-04: status parsing + the stale decision — held + old heartbeat is
    stale; held + fresh heartbeat (slow-but-live publish) is NOT; unheld or
    unparseable never is."""
    import coord.transport as _tr
    _held_old = _tr._parse_flock_status("HELD=1\nHOLDER=BS2\nPID=12345\nAGE=2460\n")
    assert _held_old == {'held': True, 'holder': 'BS2',
                         'pid': '12345', 'age_sec': 2460}
    assert _tr.lock_is_stale(_held_old) is True
    # fresh heartbeat → live, not stale
    assert _tr.lock_is_stale(
        _tr._parse_flock_status("HELD=1\nHOLDER=BS2\nPID=9\nAGE=20\n")) is False
    # not held → never stale; empty sidecars parse to None
    _free = _tr._parse_flock_status("HELD=0\nHOLDER=\nPID=\nAGE=999999\n")
    assert _free['held'] is False and _free['holder'] is None
    assert _tr.lock_is_stale(_free) is False
    # missing/garbage AGE → not stale, no crash
    assert _tr.lock_is_stale(
        _tr._parse_flock_status("HELD=1\nHOLDER=BS2\n")) is False
    assert _tr.lock_is_stale(None) is False



def test_fed04_remote_flock_lock_status_and_break():
    """FED-04: lock_status parses a held+stale probe; break succeeds on the
    BROKEN token and surfaces the error otherwise."""
    import types as _types
    import coord.transport as _tr
    _orig = _tr.subprocess
    try:
        _tr.subprocess = _types.SimpleNamespace(
            run=lambda argv, **k: _types.SimpleNamespace(
                returncode=0, stdout="HELD=1\nHOLDER=BS2\nPID=42\nAGE=3000\n",
                stderr=''),
            SubprocessError=Exception)
        _st = _tr.remote_flock_lock_status(
            ssh_host='u@h', lock_path='/var/lock/repo-coord.lock', ssh_key='k')
        assert _st['held'] and _st['holder'] == 'BS2' and _st['pid'] == '42'
        assert _tr.lock_is_stale(_st)
        # break success
        _tr.subprocess.run = lambda argv, **k: _types.SimpleNamespace(
            returncode=0, stdout='COORD_LOCK_BROKEN\n', stderr='')
        _ok, _detail = _tr.remote_flock_break(
            ssh_host='u@h', lock_path='/var/lock/repo-coord.lock')
        assert _ok, _detail
        # break failure → surfaces stderr
        _tr.subprocess.run = lambda argv, **k: _types.SimpleNamespace(
            returncode=1, stdout='', stderr='ssh: connect failed')
        _ok2, _detail2 = _tr.remote_flock_break(ssh_host='u@h', lock_path='/x')
        assert not _ok2 and 'connect failed' in _detail2
    finally:
        _tr.subprocess = _orig



def test_fed03_remote_pubkey_state_local_and_ssh():
    """FED-03: remote_pubkey_state distinguishes present/absent/error for both
    a local-fs spec and an ssh `user@host:path` spec (mocked)."""
    import types as _types
    import hashlib as _hl
    import coord.transport as _tr
    # local-fs mirror: real file present / missing
    with tempfile.TemporaryDirectory() as _d:
        _f = os.path.join(_d, 'BS1.pub')
        with open(_f, 'wb') as _fh:
            _fh.write(b'ssh-ed25519 AAAA...\n')
        _sha = _hl.sha256(b'ssh-ed25519 AAAA...\n').hexdigest()
        assert _tr.remote_pubkey_state(spec=_f) == ('present', _sha)
        assert _tr.remote_pubkey_state(
            spec=os.path.join(_d, 'nope.pub')) == ('absent', '')
    # ssh spec: mock subprocess for present / absent / ssh-error
    _orig = _tr.subprocess
    try:
        _digest = 'c' * 64
        _tr.subprocess = _types.SimpleNamespace(
            run=lambda a, **k: _types.SimpleNamespace(
                returncode=0, stdout=_digest + '\n', stderr=''),
            SubprocessError=Exception)
        assert _tr.remote_pubkey_state(
            spec='u@h:/k/BS1.pub', ssh_key='k') == ('present', _digest)
        _tr.subprocess.run = lambda a, **k: _types.SimpleNamespace(
            returncode=0, stdout='__ABSENT__\n', stderr='')
        assert _tr.remote_pubkey_state(spec='u@h:/k/BS1.pub') == ('absent', '')
        _tr.subprocess.run = lambda a, **k: _types.SimpleNamespace(
            returncode=255, stdout='', stderr='ssh: connect refused')
        _st, _why = _tr.remote_pubkey_state(spec='u@h:/k/BS1.pub')
        assert _st == 'error' and 'connect refused' in _why
    finally:
        _tr.subprocess = _orig



def test_fed03d_builder_bindings_and_enforcement():
    """FED-03 D: build_builder_bindings + strict enforce_bindings —
    a swapped/injected pubkey or one absent from the signed map is rejected;
    verified_keyring_from_head rejects ALL keys when the head carries no
    `builders` map (keys are never trusted without signed bindings)."""
    import hashlib as _hl
    import coord.identity as _id
    with tempfile.TemporaryDirectory() as _d:
        _alice = os.path.join(_d, 'alice.pub')
        _bob = os.path.join(_d, 'bob.pub')
        with open(_alice, 'wb') as _f:
            _f.write(b'ALICEKEY\n')
        with open(_bob, 'wb') as _f:
            _f.write(b'BOBKEY\n')
        _keyring = {'alice': _alice, 'bob': _bob}
        _bindings = _id.build_builder_bindings(_keyring)
        assert _bindings['alice'] == _hl.sha256(b'ALICEKEY\n').hexdigest()
        # both match the signed map → both kept
        _v, _dropped = _id.enforce_bindings(_keyring, _bindings)
        assert set(_v) == {'alice', 'bob'} and not _dropped
        # an INJECTED alice key (sha no longer matches the binding) → dropped
        with open(_alice, 'wb') as _f:
            _f.write(b'INJECTED\n')
        _v, _dropped = _id.enforce_bindings(_keyring, _bindings)
        assert set(_v) == {'bob'} and 'alice' in _dropped
        # a builder absent from the signed map → dropped
        _v, _dropped = _id.enforce_bindings({'carol': _bob}, _bindings)
        assert not _v and 'carol' in _dropped
        # head with no `builders` map → ALL dropped (strict, no bare trust)
        _vk, _dr = _id.verified_keyring_from_head(
            {'bob': _bob}, {'inrelease_sha256': 'x'})
        assert not _vk and 'bob' in _dr
        assert 'no signed builder bindings' in _dr['bob']
        # head WITH bindings → enforced
        _vk, _dr = _id.verified_keyring_from_head(
            {'bob': _bob}, {'builders': _bindings})
        assert set(_vk) == {'bob'} and not _dr
        # summaries
        assert 'not matching' in _id.binding_drop_summary({'a': 'r'})
        assert _id.binding_drop_summary({}) == ''



def test_fed03d_new_coord_head_carries_builders():
    """FED-03 D: new_coord_head embeds the builder binding map (additive +
    back-compat — absent when none given)."""
    from coord.schema import new_coord_head
    _h = new_coord_head(
        inrelease_sha256='a', snapshot={}, last_seqs={}, head_time='t',
        builders={'bs1': 'd' * 64})
    assert _h['builders'] == {'bs1': 'd' * 64}
    _h2 = new_coord_head(
        inrelease_sha256='a', snapshot={}, last_seqs={}, head_time='t')
    assert 'builders' not in _h2



def test_transport_list_remote_debs_parses_and_guards():
    """list_remote_debs runs `find dists … *.deb/*.udeb` on the pool host and
    returns the relative paths as a set; None on bad spec / non-zero rc."""
    import types as _types

    import coord.transport as _tr
    _seen = {}
    _orig = _tr.subprocess

    def _run_factory(rc, out):
        def _run(argv, **k):
            _seen['argv'] = argv
            return _types.SimpleNamespace(returncode=rc, stdout=out, stderr='')
        return _run
    try:
        _tr.subprocess = _types.SimpleNamespace(
            run=_run_factory(
                0,
                'dists/thor/main/binary-amd64/a.deb\n'
                'dists/thor/main-udeb/binary-amd64/b.udeb\n'),
            SubprocessError=Exception)
        _got = _tr.list_remote_debs('ubuntu@host:asgard', 'k.key')
        assert _got == {'dists/thor/main/binary-amd64/a.deb',
                        'dists/thor/main-udeb/binary-amd64/b.udeb'}
        # finds both .deb and .udeb under dists, from the pool path
        _cmd = _seen['argv'][-1]
        assert 'find dists' in _cmd and "*.deb" in _cmd and "*.udeb" in _cmd
        assert 'asgard' in _cmd
        # no colon → None (no ssh attempted)
        assert _tr.list_remote_debs('not-a-spec') is None
        # non-zero rc → None
        _tr.subprocess.run = _run_factory(1, '')
        assert _tr.list_remote_debs('ubuntu@host:asgard') is None
    finally:
        _tr.subprocess = _orig



def test_virtual_publish_dry_run_skips_own_already_published_filenames():
    """When the remote sidecar already has our claim for filename X
    at our recorded sha, virtual_publish_dry_run must SKIP synthesizing
    a duplicate claim for X.  Otherwise the merge contains TWO
    athena-primary claims for X (one real, one synthetic with
    deterministic-by-triple sha) → detect_hash_conflicts false-positive
    on every previously-published binary."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    from coord import schema as _sch
    _real_sha = 'a' * 64
    _existing = _sch.new_claim(
        builder='athena-primary', seq=1, package='openssl',
        intended_version='3.0', built_version='3.0',
        filename='openssl_3.0_amd64.deb',
        sha256=_real_sha, size=0, snapshot='T0',
        built_at='1970-01-01T00:00:00Z',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    # Virtual record for the SAME filename with the SYNTHETIC sha
    # (deterministic by name+ver+arch, not the real sha).
    _virt = [
        {'Package': 'openssl', 'Version': '3.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/main/o/s/openssl_3.0_amd64.deb',
         'SHA256': _vb._virtual_sha256('openssl', '3.0', 'amd64'),
         'Size': '0'},
    ]
    _merged, _f = _vb.virtual_publish_dry_run(
        _virt, our_builder_id='athena-primary', snapshot='T1',
        remote_by_builder={'athena-primary': [_existing]})
    # No hash conflict (no duplicate claim added).
    _hash = [_t for _t in _f if _t[1] == 'virtual_hash_conflict']
    assert _hash == [], _hash
    # Existing claim count unchanged.
    assert len(_merged['athena-primary']) == 1



def test_virtual_publish_dry_run_skips_adopted_pulled_from_filenames():
    """A peer that ADOPTED a file (its build record carries `pulled_from`)
    must not re-synthesize a claim for it.  Otherwise the placeholder SHA
    collides with the OWNER's real published SHA on every adopted file —
    the BS2-fully-synced federation case (6027 false conflicts).  This
    mirrors the real publish, which skips `pulled_from` records
    (generate_pending_claims, publish.py:95)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    from coord import schema as _sch
    _fn = 'liba52-0.7.4_0.7.4-20_amd64.deb'
    _owner_claim = _sch.new_claim(
        builder='athena-primary', seq=1, package='a52dec',
        intended_version='0.7.4-20', built_version='0.7.4-20',
        filename=_fn, sha256='f' * 64, size=0, snapshot='T0',
        built_at='1970-01-01T00:00:00Z',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _virt = [
        {'Package': 'liba52-0.7.4', 'Version': '0.7.4-20',
         'Architecture': 'amd64', 'Source': 'a52dec',
         'Filename': f'pool/main/liba/a52dec/{_fn}',
         'SHA256': _vb._virtual_sha256('liba52-0.7.4', '0.7.4-20', 'amd64'),
         'Size': '0'},
    ]
    # Baseline: WinDev owns nothing published; without the adopted set the
    # synthetic SHA false-conflicts with athena-primary's real SHA.
    _m1, _f1 = _vb.virtual_publish_dry_run(
        _virt, our_builder_id='WinDev', snapshot='T1',
        remote_by_builder={'athena-primary': [_owner_claim]})
    assert [_t for _t in _f1 if _t[1] == 'virtual_hash_conflict'], (
        "baseline must conflict without adoption (synthetic vs real)")
    # With the file marked adopted (pulled_from), it's skipped → clean,
    # and WinDev mints no claim for it.
    _m2, _f2 = _vb.virtual_publish_dry_run(
        _virt, our_builder_id='WinDev', snapshot='T1',
        remote_by_builder={'athena-primary': [_owner_claim]},
        local_adopted_fns={_fn})
    assert [_t for _t in _f2 if _t[1] == 'virtual_hash_conflict'] == [], _f2
    assert not _m2.get('WinDev'), _m2.get('WinDev')



def test_virtual_publish_dry_run_synthesizes_only_new_filenames():
    """We own filename X on remote AND we have a new virtual record
    for filename Y (never published).  Only Y should be synthesized.
    """
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    from coord import schema as _sch
    _existing = _sch.new_claim(
        builder='athena-primary', seq=1, package='openssl',
        intended_version='3.0', built_version='3.0',
        filename='openssl_3.0_amd64.deb',
        sha256='r' * 64, size=0, snapshot='T0',
        built_at='1970-01-01T00:00:00Z',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _virt = [
        {'Package': 'openssl', 'Version': '3.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/main/o/s/openssl_3.0_amd64.deb',
         'SHA256': 'p' * 64, 'Size': '0'},
        {'Package': 'newpkg', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/main/n/s/newpkg_1.0_amd64.deb',
         'SHA256': 'q' * 64, 'Size': '0'},
    ]
    _merged, _f = _vb.virtual_publish_dry_run(
        _virt, our_builder_id='athena-primary', snapshot='T1',
        remote_by_builder={'athena-primary': [_existing]})
    # 1 existing + 1 new (newpkg only).
    assert len(_merged['athena-primary']) == 2
    _new_fns = [_c['filename']
                for _c in _merged['athena-primary']
                if _c.get('seq', 0) > 1]
    assert _new_fns == ['newpkg_1.0_amd64.deb']



def test_virtual_publish_dry_run_ownership_blocked_critical():
    """A peer currently owns the same filename at the SAME version →
    virtual publish would be REFUSED → CRITICAL virtual_ownership_blocked.

    Both claims share the same synthetic sha (reproducible-build
    scenario) so we isolate the ownership check from the hash-conflict
    check — both real gates run, the test only inspects ownership."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    from coord import schema as _sch
    _shared_sha = 'x' * 64
    _peer_claim = _sch.new_claim(
        builder='athena-peer', seq=1, package='a',
        intended_version='1.0', built_version='1.0',
        filename='a_1.0_amd64.deb',
        sha256=_shared_sha, size=0, snapshot='T0',
        built_at='1970-01-01T00:00:00Z',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/main/a/s/a_1.0_amd64.deb',
         'SHA256': _shared_sha, 'Size': '0'},
    ]
    _merged, _f = _vb.virtual_publish_dry_run(
        _recs, our_builder_id='athena-primary', snapshot='T1',
        remote_by_builder={'athena-peer': [_peer_claim]})
    _blocked = [_t for _t in _f if _t[1] == 'virtual_ownership_blocked']
    assert len(_blocked) == 1
    assert 'athena-peer' in _blocked[0][2]



def test_virtual_publish_dry_run_tunneled_target_emits_transfer_and_hash_conflict():
    """Tunneled peer claim (republished_from set) → INFO ownership
    transfer.  Real-world tunneled+local-build SHAs differ → ALSO
    CRITICAL hash conflict (correct behaviour — operator must resolve
    the hash divergence before the transfer can land).  Test asserts
    BOTH findings appear so neither check regresses silently."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    from coord import schema as _sch
    _peer_claim = _sch.new_claim(
        builder='athena-peer', seq=1, package='a',
        intended_version='1.0', built_version='1.0',
        filename='a_1.0_amd64.deb',
        sha256='p' * 64, size=0, snapshot='T0',
        built_at='1970-01-01T00:00:00Z',
        claim_state=_sch.CLAIM_STATE_PUBLISHED,
        republished_from={'url': 'http://u', 'upstream_sha256': 'p'})
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/main/a/s/a_1.0_amd64.deb',
         'SHA256': 'x' * 64, 'Size': '0'},
    ]
    _merged, _f = _vb.virtual_publish_dry_run(
        _recs, our_builder_id='athena-primary', snapshot='T1',
        remote_by_builder={'athena-peer': [_peer_claim]})
    _info = [_t for _t in _f if _t[1] == 'virtual_ownership_transfer']
    assert len(_info) == 1
    assert 'tunneled' in _info[0][2]
    _hash = [_t for _t in _f if _t[1] == 'virtual_hash_conflict']
    assert len(_hash) == 1



def test_virtual_publish_dry_run_same_sha_no_hash_conflict():
    """Cross-builder duplicate filename with SAME sha → reproducible
    build outcome; no CRITICAL hash conflict.  Ownership is still
    blocked (same version, different builder) but that's a separate
    finding."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    from coord import schema as _sch
    _shared = 'z' * 64
    _peer_claim = _sch.new_claim(
        builder='athena-peer', seq=1, package='a',
        intended_version='1.0', built_version='1.0',
        filename='a_1.0_amd64.deb',
        sha256=_shared, size=0, snapshot='T0',
        built_at='1970-01-01T00:00:00Z',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/main/a/s/a_1.0_amd64.deb',
         'SHA256': _shared, 'Size': '0'},
    ]
    _merged, _f = _vb.virtual_publish_dry_run(
        _recs, our_builder_id='athena-primary', snapshot='T1',
        remote_by_builder={'athena-peer': [_peer_claim]})
    _hash_crit = [_t for _t in _f
                  if _t[1] == 'virtual_hash_conflict']
    assert _hash_crit == []



def test_new_claim_threads_component_field():
    """schema.new_claim(component=…) writes it to the claim dict, with
    'main' default for back-compat.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    _c_default = _sch.new_claim(
        builder='b', seq=1, package='p', intended_version='1.0',
        built_version='1.0', filename='p_1.0_amd64.deb', sha256='a' * 64,
        size=10, snapshot='20260602T000000Z', built_at='2026-06-02T00:00:00Z',
    )
    assert _c_default['component'] == 'main', _c_default
    _c_explicit = _sch.new_claim(
        builder='b', seq=1, package='p', intended_version='1.0',
        built_version='1.0', filename='p_1.0_amd64.deb', sha256='a' * 64,
        size=10, snapshot='20260602T000000Z', built_at='2026-06-02T00:00:00Z',
        component='non-free-firmware',
    )
    assert _c_explicit['component'] == 'non-free-firmware', _c_explicit



def test_generate_pending_claims_threads_component_from_build_record():
    """End-to-end: a build record's `component` field flows into the
    pending claim's `component` field via generate_pending_claims.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import publish as _pub
    from unittest.mock import patch

    _rec = {
        'phase':            'tunneled',
        'status':           'TUNNELED',
        'intended_version': '3.20250311.1~deb12u1',
        'built_version':    '3.20250311.1',
        'outputs':          ['amd64-microcode_3.20250311.1_amd64.deb'],
        'output_hashes':    {
            'amd64-microcode_3.20250311.1_amd64.deb': 'a' * 64,
        },
        'republished_from': {},
        'pulled_from':      None,
        'component':        'non-free-firmware',
        'finished':         '2026-06-04T00:00:00Z',
    }

    with tempfile.TemporaryDirectory() as _tmp:
        _claims_dir = os.path.join(_tmp, 'claims')
        os.makedirs(_claims_dir, exist_ok=True)
        # listdir wants a real .build.json file under a real buildlog dir.
        _buildlog = os.path.join(_tmp, 'log', 'build')
        os.makedirs(_buildlog, exist_ok=True)
        with open(os.path.join(_buildlog,
                                'amd64-microcode.build.json'), 'w') as _fh:
            _fh.write('{}')

        # store.read_builder_claims expects a pubkey path — stub it.
        with patch.object(_pub._store, 'read_builder_claims',
                          return_value=[]):
            _pending = _pub.generate_pending_claims(
                builder_id='test-builder',
                buildlog_dir=_buildlog,
                claims_dir=_claims_dir,
                public_key_path='/nonexistent',
                snapshot_pin='20260602T173733Z',
                read_build_record=lambda _bl, _n: _rec,
            )

    assert len(_pending) == 1, _pending
    assert _pending[0]['component'] == 'non-free-firmware', _pending[0]



def test_b7_federated_patch_level_and_claim_hash():
    """COMP-04 B7: claims carry patch_set_hash (additive);
    federated_patch_level adopts the federation's +pP for a matching
    base+hash, raises on hash divergence (sync patch/, don't mint),
    returns None with no comparable claim; generate_pending_claims
    threads the record's hash; the buildcontainer P-decision consults
    the federation on non-primary builders."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import bump
    from coord import schema as _sch
    _c = _sch.new_claim(
        builder='b', seq=0, package='p', intended_version='1.0-1+asg1u0+p2',
        built_version='1.0-1', filename='p_1.0-1+asg1u0+p2_amd64.deb',
        sha256='x', size=0, snapshot='s', built_at='t',
        patch_set_hash='h' * 64)
    assert _c['patch_set_hash'] == 'h' * 64
    _claims = [_c]
    assert bump.federated_patch_level(
        _claims, '1.0-1+asg1u0', 'h' * 64) == 2
    assert bump.federated_patch_level([], '1.0-1', 'zz') is None
    try:
        bump.federated_patch_level(_claims, '1.0-1+asg1u0', 'z' * 64)
        raise AssertionError('hash divergence must raise')
    except ValueError as _e:
        assert 'sync patch/' in str(_e)
    with open(os.path.join(_ROOT, 'scripts', 'coord',
                           'publish.py')) as _fh:
        _p = _fh.read()
    assert "patch_set_hash=str(_rec.get('patch_set_hash') or '')" in _p
    with open(os.path.join(_ROOT, 'scripts', 'buildcontainer.py')) as _fh:
        _bc = _fh.read()
    assert 'federated_patch_level(' in _bc
    assert '_federation_claims_for(package)' in _bc
    _gate = _bc.index('federated_patch_level(')
    assert "arch_all_owner', True)" in _bc[_gate - 400:_gate], \
        'federated P adoption must be gated to non-primary builders'


def test_b3_b5_b11_per_arch_coord_plumbing():
    """COMP-04 chunk B pure functions: head per-arch sha resolution
    (map wins, missing arch is empty, legacy scalar fallback);
    union_ledger_entries preserves foreign slices and replaces own
    scope wholesale (incl. dropping own out-of-scope entries);
    claim_in_arch_scope matrix; coord_root_for_state explicit-vs-
    derived."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    import mirror as _mirror

    _h = _sch.new_coord_head(
        inrelease_sha256='aa', snapshot={}, last_seqs={}, head_time='t',
        inrelease_sha256_by_arch={'amd64': 'aa', 'i386': 'bb'},
        closure_ledger_sha256_by_arch={'amd64': 'la'})
    assert _sch.head_inrelease_sha(_h, 'i386') == 'bb'
    assert _sch.head_inrelease_sha(_h, 'arm64') == ''
    assert _sch.head_ledger_sha(_h, 'amd64') == 'la'
    assert _sch.head_inrelease_sha({'inrelease_sha256': 'zz'}, 'i386') == 'zz'

    from coord import publish as _pub
    _fetched = {
        'pkg|amd64':   {'v': 1}, 'lib|all': {'v': 1},
        'src.dsc|source': {'v': 1}, 'pkg|i386': {'v': 'stale'},
    }
    _own = {'pkg|i386': {'v': 'new'}, 'pulled|all': {'v': 'x'}}
    _u = _pub.union_ledger_entries(_fetched, _own, 'i386', False)
    assert _u['pkg|amd64'] == {'v': 1}, 'foreign slice preserved'
    assert _u['lib|all'] == {'v': 1}, 'primary-owned all preserved'
    assert _u['src.dsc|source'] == {'v': 1}, 'primary source preserved'
    assert _u['pkg|i386'] == {'v': 'new'}, 'own slice replaced'
    assert 'pulled|all' not in _u, 'own out-of-scope entry dropped'
    _u2 = _pub.union_ledger_entries(_fetched, {'lib|all': {'v': 2}},
                                    'amd64', True)
    assert _u2['lib|all'] == {'v': 2} and 'pkg|amd64' not in _u2, \
        'primary scope (amd64+all+source) replaced wholesale'
    assert _u2['pkg|i386'] == {'v': 'stale'}, 'i386 slice preserved'

    assert _sch.claim_in_arch_scope({'arch': 'i386'}, 'i386', False)
    assert _sch.claim_in_arch_scope({'arch': 'all'}, 'i386', False)
    assert not _sch.claim_in_arch_scope({'arch': 'amd64'}, 'i386', False)
    assert not _sch.claim_in_arch_scope({'arch': 'source'}, 'i386', False)
    assert _sch.claim_in_arch_scope({'arch': 'source'}, 'amd64', True)
    # legacy claims: filename fallback; unparseable stays in scope
    assert not _sch.claim_in_arch_scope(
        {'filename': 'x_1_amd64.deb'}, 'i386', False)
    assert _sch.claim_in_arch_scope({'filename': 'weird'}, 'i386', False)

    assert _mirror.coord_root_for_state(
        {'url': 'ssh://u@h/srv/repo-i386',
         'coord_url': 'ssh://u@h/srv/repo-coord'}) == 'ssh://u@h/srv/repo-coord'
    assert _mirror.coord_root_for_state(
        {'url': 'ssh://u@h/srv/asgard'}) == 'ssh://u@h/srv/asgard-coord'


def test_b3_b4_wiring_pins():
    """Source pins: publish preserves other arches' head-map slots and
    updates only its own; audits filter claims by arch scope; the
    ledger source-side audit is gated on the primary role; mirror add
    parses --coord-url and add_mirror validates+stores it."""
    with open(os.path.join(_ROOT, 'scripts', 'coord',
                           'publish.py')) as _fh:
        _p = _fh.read()
    assert "_ir_by_arch[_own_arch] = _ir_sha" in _p
    assert 'inrelease_sha256_by_arch=_ir_by_arch or None' in _p
    assert 'union_ledger_entries(' in _p
    with open(os.path.join(_ROOT, 'scripts', 'mirror.py')) as _fh:
        _m = _fh.read()
    assert 'claim_in_arch_scope(' in _m, \
        'audit_claims_vs_packages must scope-filter'
    with open(os.path.join(_ROOT, 'scripts', 'commands',
                           'cmd_mirror.py')) as _fh:
        _cm = _fh.read()
    assert _cm.count('claim_in_arch_scope(') >= 1, \
        'missing_on_disk fold must scope-filter'
    assert "own_arch=self.config.arch" in _cm
    assert "'--coord-url'" in _cm
    assert "src_idx=(_src_idx if getattr(" in _cm
    assert _cm.count('coord_root_for_state') >= 8, \
        'state-bearing coord sites must honor the explicit coord_url'


def test_d3_non_primary_drops_all_and_source_claims_and_d4_arch():
    """COMP-04 D3/D4: with arch_all_owner=False the pending set drops
    every _all.deb and every source artifact (concrete-arch binaries
    kept); with the default True everything is claimed.  Every emitted
    claim carries the D4 arch field: filename token for binaries,
    'source' for source artifacts."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import publish as _pub
    from coord import schema as _schema
    from unittest.mock import patch

    assert _schema.artifact_arch('x_1_all.deb') == 'all'
    assert _schema.artifact_arch('x_1_i386.udeb') == 'i386'
    assert _schema.artifact_arch('x_1.dsc') == 'source'
    assert _schema.artifact_arch('x_1.0.orig.tar.gz') == 'source'

    _rec = {
        'phase':            'done',
        'status':           'PASS',
        'intended_version': '1.0-1+asg1u0',
        'built_version':    '1.0-1',
        'outputs':          ['tool_1.0-1+asg1u0_i386.deb',
                             'tool-data_1.0-1+asg1u0_all.deb'],
        'output_hashes':    {
            'tool_1.0-1+asg1u0_i386.deb': 'a' * 64,
            'tool-data_1.0-1+asg1u0_all.deb': 'b' * 64,
        },
        'source_outputs':   ['tool_1.0-1+asg1u0.dsc'],
        'source_output_hashes': {'tool_1.0-1+asg1u0.dsc': 'c' * 64},
        'republished_from': {},
        'pulled_from':      None,
        'component':        'main',
        'finished':         '2026-07-26T00:00:00Z',
    }
    def _run(owner):
        with tempfile.TemporaryDirectory() as _tmp:
            _claims_dir = os.path.join(_tmp, 'claims')
            os.makedirs(_claims_dir, exist_ok=True)
            _buildlog = os.path.join(_tmp, 'log', 'build')
            os.makedirs(_buildlog, exist_ok=True)
            with open(os.path.join(_buildlog, 'tool.build.json'), 'w') as _fh:
                _fh.write('{}')
            with patch.object(_pub._store, 'read_builder_claims',
                              return_value=[]):
                return _pub.generate_pending_claims(
                    builder_id='bs3-i386',
                    buildlog_dir=_buildlog,
                    claims_dir=_claims_dir,
                    public_key_path='/nonexistent',
                    snapshot_pin='20260705T190150Z',
                    read_build_record=lambda _bl, _n: _rec,
                    build_arch='i386',
                    arch_all_owner=owner,
                )
    _foreign = _run(False)
    _fns = sorted(_c['filename'] for _c in _foreign)
    assert _fns == ['tool_1.0-1+asg1u0_i386.deb'], \
        f'non-primary must claim ONLY concrete-arch binaries: {_fns}'
    assert _foreign[0]['arch'] == 'i386'
    _primary = _run(True)
    _fns_p = sorted(_c['filename'] for _c in _primary)
    assert _fns_p == ['tool-data_1.0-1+asg1u0_all.deb',
                      'tool_1.0-1+asg1u0.dsc',
                      'tool_1.0-1+asg1u0_i386.deb']
    _by = {_c['filename']: _c for _c in _primary}
    assert _by['tool-data_1.0-1+asg1u0_all.deb']['arch'] == 'all'
    assert _by['tool_1.0-1+asg1u0.dsc']['arch'] == 'source'


def test_d3_config_role_default_and_emit_gates():
    """arch_all_owner defaults to role=='first' (BS1 keeps today's
    behavior with zero config; role=federation defaults non-owner) with
    [Local] ArchAllOwner as explicit override; the post-build emit hook
    and the manual `source emit` actions are role-gated (verify stays
    available)."""
    with open(os.path.join(_ROOT, 'scripts', 'utils.py')) as _fh:
        _u = _fh.read()
    assert "'ArchAllOwner'" in _u
    assert "fallback=(self.system_role == 'first')" in _u
    with open(os.path.join(_ROOT, 'scripts', 'commands',
                           'cmd_source.py')) as _fh:
        _cs = _fh.read()
    assert _cs.count("getattr(self.config, 'arch_all_owner', True)") >= 2, \
        'both the post-build hook and cmd_source_emit must be gated'
    _gate = _cs.index("getattr(self.config, 'arch_all_owner', True)")
    assert _cs.index("args and args[0] == 'verify'") < _gate, \
        'verify must be dispatched BEFORE the non-primary gate'


def test_generate_pending_claims_covers_source_outputs():
    """MAT-02 stage 5: a record's source_outputs / source_output_hashes
    (written by `source emit`) generate claims like any output — same
    component, record versions, no republished_from — and a source file
    already in the live claim set is skipped (_known)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import publish as _pub
    from unittest.mock import patch

    _rec = {
        'phase':            'done',
        'status':           'PASS',
        'intended_version': '5.2.15-2+asg1u0+p1',
        'built_version':    '5.2.15-2',
        'outputs':          ['bash_5.2.15-2+asg1u0+p1_amd64.deb'],
        'output_hashes':    {
            'bash_5.2.15-2+asg1u0+p1_amd64.deb': 'a' * 64,
        },
        'source_outputs':   ['bash_5.2.15-2+asg1u0+p1.dsc',
                             'bash_5.2.15-2+asg1u0+p1.debian.tar.xz',
                             'bash_5.2.15.orig.tar.gz'],
        'source_output_hashes': {
            'bash_5.2.15-2+asg1u0+p1.dsc': 'b' * 64,
            'bash_5.2.15-2+asg1u0+p1.debian.tar.xz': 'c' * 64,
            'bash_5.2.15.orig.tar.gz': 'd' * 64,
        },
        'republished_from': {},
        'pulled_from':      None,
        'component':        'main',
        'finished':         '2026-07-12T00:00:00Z',
    }
    _existing = [{'filename': 'bash_5.2.15.orig.tar.gz',
                  'claim_state': 'published', 'seq': 1}]
    with tempfile.TemporaryDirectory() as _tmp:
        _claims_dir = os.path.join(_tmp, 'claims')
        os.makedirs(_claims_dir, exist_ok=True)
        _buildlog = os.path.join(_tmp, 'log', 'build')
        os.makedirs(_buildlog, exist_ok=True)
        with open(os.path.join(_buildlog, 'bash.build.json'), 'w') as _fh:
            _fh.write('{}')
        with patch.object(_pub._store, 'read_builder_claims',
                          return_value=_existing):
            _pending = _pub.generate_pending_claims(
                builder_id='test-builder',
                buildlog_dir=_buildlog,
                claims_dir=_claims_dir,
                public_key_path='/nonexistent',
                snapshot_pin='20260705T190150Z',
                read_build_record=lambda _bl, _n: _rec,
                build_arch='amd64',
            )
    _fns = sorted(_c['filename'] for _c in _pending)
    assert 'bash_5.2.15-2+asg1u0+p1.dsc' in _fns, _fns
    assert 'bash_5.2.15-2+asg1u0+p1.debian.tar.xz' in _fns
    assert 'bash_5.2.15.orig.tar.gz' not in _fns      # already claimed
    _dsc = [_c for _c in _pending
            if _c['filename'].endswith('.dsc')][0]
    assert _dsc['sha256'] == 'b' * 64
    assert _dsc['component'] == 'main'
    assert _dsc.get('republished_from') is None


def test_scan_pool_files_includes_source_artifacts_under_source_dirs():
    """MAT-02 stage 5: the pool scan sees source artifacts ONLY under
    source/ dirs (index files and stray tarballs elsewhere never enter the
    pool view); binaries are picked up anywhere."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord.reconcile import scan_pool_files
    with tempfile.TemporaryDirectory() as _tmp:
        _bin = os.path.join(_tmp, 'dists', 'thor', 'main', 'binary-amd64')
        _src = os.path.join(_tmp, 'dists', 'thor', 'main', 'source')
        os.makedirs(_bin)
        os.makedirs(_src)
        for _p, _c in (
                (os.path.join(_bin, 'x_1.0_amd64.deb'), b'd'),
                (os.path.join(_src, 'x_1.0.dsc'), b's'),
                (os.path.join(_src, 'x_1.0.orig.tar.gz'), b't'),
                (os.path.join(_src, 'Sources.gz'), b'i'),
                (os.path.join(_tmp, 'stray.tar.gz'), b'n')):
            with open(_p, 'wb') as _fh:
                _fh.write(_c)
        _pool = scan_pool_files(_tmp)
    assert 'x_1.0_amd64.deb' in _pool
    assert 'x_1.0.dsc' in _pool
    assert 'x_1.0.orig.tar.gz' in _pool
    assert 'Sources.gz' not in _pool          # index, not pool
    assert 'stray.tar.gz' not in _pool        # not under a source/ dir


def test_audit_closure_ledger_validates_source_entries():
    """MAT-02 stage 5, check (c): with src_idx supplied, a published source
    file missing from the ledger is CRITICAL; agreeing entries are clean;
    a ledger source entry naming an unpublished file flags
    closure_ledger_entry_not_published ('source' joins the audited set)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    _src_idx = {'demo_1.0-1+asg1u1.dsc': {
        'sha256': 'a' * 64, 'size': '100', 'source': 'demo',
        'version': '1.0-1+asg1u1', 'component': 'main'}}
    _entry = {'filename': 'demo_1.0-1+asg1u1.dsc', 'sha256': 'a' * 64,
              'size': 100, 'version': '1.0-1+asg1u1', 'component': 'main',
              'package': 'demo', 'arch': 'source'}
    # agree → clean
    _f = _mir.audit_closure_ledger(
        {'entries': {'demo_1.0-1+asg1u1.dsc|source': dict(_entry)}},
        pkg_idx={}, closure_bins=set(), packages_text='',
        src_idx=_src_idx)
    assert not [_x for _x in _f if _x[0] == 'CRITICAL'], _f
    # missing from ledger → CRITICAL
    _f2 = _mir.audit_closure_ledger(
        {'entries': {}}, pkg_idx={}, closure_bins=set(),
        packages_text='', src_idx=_src_idx)
    assert any(_k == 'closure_ledger_entry_missing'
               for _s, _k, _m in _f2), _f2
    # ledger names an unpublished source file → not_published
    _f3 = _mir.audit_closure_ledger(
        {'entries': {'ghost_9.9.dsc|source': dict(
            _entry, filename='ghost_9.9.dsc')}},
        pkg_idx={}, closure_bins=set(), packages_text='',
        src_idx={})
    assert any(_k == 'closure_ledger_entry_not_published'
               for _s, _k, _m in _f3), _f3


def test_coord_schema_canonical_bytes_stable_across_key_order():
    """schema.canonical_bytes is sort-keys'd → two dicts with the same
    fields in different insertion order produce identical bytes."""
    _s, *_ = _coord_modules()
    _a = {'package': 'x', 'v': 1, 'seq': 7}
    _b = {'seq': 7, 'v': 1, 'package': 'x'}
    assert _s.canonical_bytes(_a) == _s.canonical_bytes(_b)



def test_coord_schema_canonical_bytes_excludes_sig():
    """schema.canonical_bytes drops the `sig` field so a signed record's
    canonical bytes match what was originally signed."""
    _s, *_ = _coord_modules()
    _rec = {'v': 1, 'package': 'x', 'sig': 'deadbeef'}
    _bytes = _s.canonical_bytes(_rec)
    assert b'sig' not in _bytes
    assert b'deadbeef' not in _bytes



def test_coord_claim_to_jsonl_round_trip():
    """claim → jsonl line → parsed claim."""
    _s, *_ = _coord_modules()
    _claim = _s.new_claim(
        builder='alice', seq=1, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo_1.0_amd64.deb',
        sha256='a' * 64, size=1024,
        snapshot='20260601T000000Z',
        built_at='2026-06-01T00:00:00Z',
    )
    _claim['sig'] = 'b' * 128
    _line = _s.claim_to_jsonl(_claim)
    assert _line.endswith(b'\n')
    _parsed = _s.claim_from_jsonl(_line)
    assert _parsed == _claim



def test_coord_claim_from_jsonl_rejects_missing_required():
    """Lines lacking required fields parse to None."""
    _s, *_ = _coord_modules()
    assert _s.claim_from_jsonl(b'{"v": 1, "builder": "alice"}\n') is None
    assert _s.claim_from_jsonl(b'not json\n') is None
    assert _s.claim_from_jsonl(b'') is None



def test_coord_identity_keypair_and_sign_verify():
    """End-to-end: generate Ed25519 keypair, sign a claim, verify; tamper
    → verify fails."""
    if not _openssl_available():
        return
    _s, _i, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _priv, _pub = _i.generate_keypair(_td, 'alice')
        assert os.path.isfile(_priv)
        assert os.path.isfile(_pub)
        # Private key must be 0600
        import stat
        _mode = stat.S_IMODE(os.stat(_priv).st_mode)
        assert _mode == 0o600, f"private key mode {oct(_mode)} != 0o600"
        # Sign + verify
        _claim = _s.new_claim(
            builder='alice', seq=1, package='foo',
            intended_version='1.0', built_version='1.0',
            filename='foo.deb', sha256='a' * 64, size=1,
            snapshot='S', built_at='T',
        )
        _signed = _i.sign_claim(_claim, _priv)
        assert isinstance(_signed.get('sig'), str)
        assert len(_signed['sig']) == 128  # 64 bytes hex
        assert _i.verify_claim(_signed, _pub) is True
        # Tamper detection
        _tampered = dict(_signed)
        _tampered['built_version'] = '99.0'
        assert _i.verify_claim(_tampered, _pub) is False



def test_coord_identity_refuses_clobber_without_overwrite():
    """generate_keypair refuses to clobber an existing private key
    unless overwrite=True."""
    if not _openssl_available():
        return
    _s, _i, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _i.generate_keypair(_td, 'alice')
        try:
            _i.generate_keypair(_td, 'alice')
        except OSError as _e:
            assert 'already exists' in str(_e)
        else:
            raise AssertionError(
                "generate_keypair must refuse to clobber without overwrite")



def test_coord_identity_load_keyring():
    """Keyring loader picks up only .pub files; returns {id: path}."""
    _s, _i, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        with open(os.path.join(_td, 'alice.pub'), 'w') as _fh:
            _fh.write('dummy')
        with open(os.path.join(_td, 'bob.pub'), 'w') as _fh:
            _fh.write('dummy')
        with open(os.path.join(_td, 'README.txt'), 'w') as _fh:
            _fh.write('skip me')
        _r = _i.load_keyring(_td)
        assert set(_r.keys()) == {'alice', 'bob'}
        assert all(_p.endswith('.pub') for _p in _r.values())



def test_coord_store_append_and_max_seq():
    """append_claim writes a signed line; max_seq returns the high seq."""
    if not _openssl_available():
        return
    _s, _i, _st, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _id_dir = os.path.join(_td, 'identity')
        _claims_dir = os.path.join(_td, 'claims')
        os.makedirs(_id_dir)
        _priv, _pub = _i.generate_keypair(_id_dir, 'alice')
        for _seq in (1, 2, 5):
            _claim = _s.new_claim(
                builder='alice', seq=_seq, package=f'pkg{_seq}',
                intended_version='1.0', built_version='1.0',
                filename=f'pkg{_seq}.deb',
                sha256='a' * 64, size=10, snapshot='S',
                built_at='T',
            )
            _ret_seq = _st.append_claim(_claims_dir, 'alice', _claim, _priv)
            assert _ret_seq == _seq
        assert _st.max_seq(_claims_dir, 'alice') == 5
        _back = _st.read_builder_claims(_claims_dir, 'alice', _pub)
        assert [_c['seq'] for _c in _back] == [1, 2, 5]



def test_coord_store_rejects_builder_mismatch():
    """append_claim refuses to write a claim whose builder field
    doesn't match the target file."""
    if not _openssl_available():
        return
    _s, _i, _st, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _priv, _ = _i.generate_keypair(_td, 'alice')
        _claim = _s.new_claim(
            builder='bob',  # wrong
            seq=1, package='x', intended_version='1',
            built_version='1', filename='x.deb', sha256='a' * 64,
            size=1, snapshot='S', built_at='T',
        )
        try:
            _st.append_claim(_td, 'alice', _claim, _priv)
        except ValueError as _e:
            assert 'builder' in str(_e)
        else:
            raise AssertionError(
                "append_claim must reject mismatched builder")



def test_coord_store_tamper_drops_line_on_read():
    """A jsonl line whose canonical bytes were modified post-sign
    fails verify and is dropped silently (logged)."""
    if not _openssl_available():
        return
    _s, _i, _st, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _id_dir = os.path.join(_td, 'identity')
        os.makedirs(_id_dir)
        _priv, _pub = _i.generate_keypair(_id_dir, 'alice')
        _claims_dir = os.path.join(_td, 'claims')
        _claim = _s.new_claim(
            builder='alice', seq=1, package='x',
            intended_version='1', built_version='1',
            filename='x.deb', sha256='a' * 64, size=1,
            snapshot='S', built_at='T',
        )
        _st.append_claim(_claims_dir, 'alice', _claim, _priv)
        # Tamper the on-disk line
        _path = _st.claims_path(_claims_dir, 'alice')
        with open(_path, 'rb') as _fh:
            _bytes = _fh.read()
        _tampered = _bytes.replace(b'"x.deb"', b'"evil.deb"')
        with open(_path, 'wb') as _fh:
            _fh.write(_tampered)
        # Verified read drops the tampered line
        _verified = _st.read_builder_claims(_claims_dir, 'alice', _pub)
        assert _verified == [], (
            f"tampered claim must be dropped on verified read; got {_verified}")



def test_coord_store_project_live_claims_collapses_retraction():
    """A retraction line for seq=N erases that claim from the live
    projection."""
    _s, _i, _st, *_ = _coord_modules()
    _c1 = _s.new_claim(
        builder='alice', seq=1, package='x', intended_version='1',
        built_version='1', filename='x.deb', sha256='a' * 64,
        size=1, snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _r1 = _s.new_retraction(
        builder='alice', seq=2, package='x',
        retracts_seq=1, filename='x.deb', snapshot='S', built_at='T',
    )
    _proj = _st.project_live_claims({'alice': [_c1, _r1]})
    assert _proj == {}, f"retracted claim must vanish from projection; got {_proj}"



def test_coord_store_project_live_claims_cross_builder_conflict_key():
    """Audit #105: two builders publishing the SAME (package, built_version)
    surface as TWO entries — the first under (pkg, ver), the second under the
    builder-suffixed conflict key (pkg, ver+'!'+builder) — so the reconcile
    layer can flag the cross-builder hash conflict.  Pins the otherwise-
    untested cross-builder merge branch (no production caller passes >1
    builder today, but the documented shape must hold for a future one)."""
    _s, _i, _st, *_ = _coord_modules()
    _alice = _s.new_claim(
        builder='alice', seq=1, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo_1.0_amd64.deb', sha256='a' * 64,
        size=1, snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _bob = _s.new_claim(
        builder='bob', seq=1, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo_1.0_amd64.deb', sha256='b' * 64,
        size=1, snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _proj = _st.project_live_claims({'alice': [_alice], 'bob': [_bob]})
    assert len(_proj) == 2, f"both builders' claims must survive; got {_proj}"
    _plain = [_k for _k in _proj if '!' not in _k[1]]
    _conflict = [_k for _k in _proj if '!' in _k[1]]
    assert len(_plain) == 1 and len(_conflict) == 1, _proj
    assert _plain[0] == ('foo', '1.0')
    assert _conflict[0][0] == 'foo'
    assert _conflict[0][1].endswith(('!alice', '!bob'))
    # the two surviving entries carry the two distinct hashes
    assert {_proj[_plain[0]]['sha256'], _proj[_conflict[0]]['sha256']} == {
        'a' * 64, 'b' * 64}



def test_mirror_recompute_base_returns_oldest_snapshot_across_claims():
    """MIRROR-02 chunk 12: _mirror_recompute_base reads the fetched
    claims jsonl and returns the oldest non-retracted snapshot
    timestamp.  Empty when no claims (fresh mirror); skips
    retracted entries; reads across all builders."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    import coord.schema as _schema
    import coord.store as _store
    import coord.identity as _identity
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        _cache_dir = os.path.join(_td, 'cache')
        os.makedirs(_cache_dir)
        _fetched = os.path.join(_cache_dir, 'mirror', 'primary',
                                'fetched')
        _claims_dir = os.path.join(_fetched, 'claims')
        _keyring_dir = os.path.join(_fetched, 'keyring', 'builders')
        os.makedirs(_claims_dir)
        os.makedirs(_keyring_dir)

        class _Cfg:
            dir_cache = _cache_dir
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()

        # Two builders, three claims at different snapshots; one retracted
        _c_old = _schema.new_claim(
            builder='team-a', seq=1, package='foo',
            intended_version='1.0', built_version='1.0',
            filename='foo_1.0.deb', sha256='a' * 64, size=1,
            snapshot='20260201T000000Z', built_at='T',
            claim_state=_schema.CLAIM_STATE_PUBLISHED,
        )
        _c_old_retracted = _schema.new_claim(
            builder='team-a', seq=2, package='bar',
            intended_version='1.0', built_version='1.0',
            filename='bar_1.0.deb', sha256='b' * 64, size=1,
            snapshot='20260101T000000Z',   # OLDEST — but retracted
            built_at='T',
            claim_state=_schema.CLAIM_STATE_PUBLISHED,
        )
        _r = _schema.new_retraction(
            builder='team-a', seq=3, package='bar',
            retracts_seq=2, filename='bar_1.0.deb',
            snapshot='S', built_at='T',
        )
        _c_newest = _schema.new_claim(
            builder='team-b', seq=1, package='baz',
            intended_version='2.0', built_version='2.0',
            filename='baz_2.0.deb', sha256='c' * 64, size=1,
            snapshot='20260601T000000Z', built_at='T',
            claim_state=_schema.CLAIM_STATE_PUBLISHED,
        )

        _fake_by_builder = {
            'team-a': [_c_old, _c_old_retracted, _r],
            'team-b': [_c_newest],
        }
        with patch.object(_identity, 'load_keyring', return_value={}), \
             patch.object(_store, 'read_all_claims',
                          return_value=_fake_by_builder):
            _base = _sess._mirror_recompute_base('primary')
        # Oldest non-retracted = team-a's first claim (2026-02-01)
        assert _base == '20260201T000000Z', _base



def test_generate_pending_claims_skips_adopted_tunnel_but_keeps_self_tunnel():
    """Federation: an ADOPTED tunnel (phase=tunneled + republished_from +
    pulled_from, written by `mirror pull`) must NOT be re-claimed — the
    original republisher still ships it.  A SELF-tunnel (same shape but NO
    pulled_from, written by `cmd_tunnel_package` as the first shipper)
    stays claimable.  Prevents a peer re-pushing byte-identical copies of
    no-owner firmware it merely pulled."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _pub

    _recs = {}

    def _mk(pkg, fn, *, pulled):
        _recs[pkg] = {
            'package': pkg, 'intended_version': '1.0', 'built_version': '1.0',
            'phase': 'tunneled', 'status': 'TUNNELED',
            'outputs': [fn], 'output_hashes': {fn: 'a' * 64},
            'component': 'non-free-firmware', 'finished': 'S',
            'republished_from': {fn: {'url': 'http://x/' + fn,
                                      'upstream_sha256': 'a' * 64}},
            'pulled_from': ({'mirror_name': 'm', 'owner_builder': 'origin'}
                            if pulled else None),
        }

    _mk('firmware-adopted', 'firmware-adopted_1.0_all.deb', pulled=True)
    _mk('firmware-self', 'firmware-self_1.0_all.deb', pulled=False)

    with tempfile.TemporaryDirectory() as _td:
        _log = os.path.join(_td, 'log')
        _claims_dir = os.path.join(_td, 'claims')
        os.makedirs(_log)
        os.makedirs(_claims_dir)
        for _pkg in _recs:
            with open(os.path.join(_log, _pkg + '.build.json'), 'w') as _fh:
                _fh.write('{}')

        _pending = _pub.generate_pending_claims(
            builder_id='BS2', buildlog_dir=_log, claims_dir=_claims_dir,
            public_key_path='/nonexistent.pub', snapshot_pin='S',
            read_build_record=lambda _d, _p: _recs.get(_p),
            build_arch='amd64')
        _fns = {c.get('filename') for c in _pending}
        assert 'firmware-self_1.0_all.deb' in _fns, (
            "self-tunnel (no pulled_from) must stay claimable")
        assert 'firmware-adopted_1.0_all.deb' not in _fns, (
            "adopted tunnel (pulled_from set) must NOT be re-claimed")



def test_generate_pending_claims_threads_republished_from_per_output():
    """MIRROR-02 chunk 9: when a build.json record carries
    `republished_from = {filename: {url, upstream_sha256}}` (the
    tunneled-package case), each emitted claim's republished_from
    matches the right per-file entry.  Verifies the build.json →
    coord claim plumbing."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish

    # Fake a buildlog dir + custom read_build_record stub
    def _fake_read(_dir, _pkg):
        if _pkg != 'libvlc':
            return None
        return {
            'package': 'libvlc',
            'intended_version': '3.0-1',
            'built_version':    '3.0-1',
            'phase':            'tunneled',
            'outputs':          [
                'libvlc_3.0-1_amd64.deb',
                'libvlc-dev_3.0-1_amd64.deb',
            ],
            'output_hashes':    {
                'libvlc_3.0-1_amd64.deb':     'a' * 64,
                'libvlc-dev_3.0-1_amd64.deb': 'b' * 64,
            },
            'republished_from': {
                'libvlc_3.0-1_amd64.deb': {
                    'url':             'http://deb.debian.org/.../libvlc_3.0-1_amd64.deb',
                    'upstream_sha256': 'a' * 64,
                },
                'libvlc-dev_3.0-1_amd64.deb': {
                    'url':             'http://deb.debian.org/.../libvlc-dev_3.0-1_amd64.deb',
                    'upstream_sha256': 'b' * 64,
                },
            },
            'finished': '2026-06-04T12:00:00Z',
        }

    with tempfile.TemporaryDirectory() as _td:
        # buildlog dir needs a .build.json entry name for our stub to match
        _log = os.path.join(_td, 'log')
        os.makedirs(_log)
        with open(os.path.join(_log, 'libvlc.build.json'), 'w') as _fh:
            _fh.write('{}')  # content ignored; read overridden

        # claims dir empty → no _known dedup
        _claims = os.path.join(_td, 'claims')
        os.makedirs(_claims)
        # public_key_path; coord.store reads it to verify, but for empty
        # claims dir the read is short-circuited.  Pass a non-existent
        # path; read_builder_claims returns [] when there's no jsonl.
        _pending = _publish.generate_pending_claims(
            builder_id='alice',
            buildlog_dir=_log,
            claims_dir=_claims,
            public_key_path='/nonexistent.pub',
            snapshot_pin='S',
            read_build_record=_fake_read,
        )
        assert len(_pending) == 2
        _by_fn = {_c['filename']: _c for _c in _pending}
        assert _by_fn['libvlc_3.0-1_amd64.deb']['republished_from'] == {
            'url':             'http://deb.debian.org/.../libvlc_3.0-1_amd64.deb',
            'upstream_sha256': 'a' * 64,
        }
        assert _by_fn['libvlc-dev_3.0-1_amd64.deb']['republished_from'] == {
            'url':             'http://deb.debian.org/.../libvlc-dev_3.0-1_amd64.deb',
            'upstream_sha256': 'b' * 64,
        }



def test_generate_pending_claims_non_tunneled_record_carries_none():
    """A normal (non-tunneled) build.json carries empty
    `republished_from`; emitted claims must set the field to None
    so project_owners treats us as the owner."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish

    def _fake_read(_dir, _pkg):
        return {
            'package': 'lsb-base',
            'intended_version': '11.4',
            'built_version':    '11.4+asg1u1',
            'phase':            'done',
            'outputs':          ['lsb-base_11.4+asg1u1_all.deb'],
            'output_hashes':    {
                'lsb-base_11.4+asg1u1_all.deb': 'c' * 64,
            },
            'republished_from': {},   # MIRROR-02 v3 default for built records
            'finished': '2026-06-04T12:00:00Z',
        }

    with tempfile.TemporaryDirectory() as _td:
        _log = os.path.join(_td, 'log')
        os.makedirs(_log)
        with open(os.path.join(_log, 'lsb-base.build.json'), 'w') as _fh:
            _fh.write('{}')
        _claims = os.path.join(_td, 'claims')
        os.makedirs(_claims)
        _pending = _publish.generate_pending_claims(
            builder_id='alice',
            buildlog_dir=_log,
            claims_dir=_claims,
            public_key_path='/nonexistent.pub',
            snapshot_pin='S',
            read_build_record=_fake_read,
        )
        assert len(_pending) == 1
        # new_claim omits republished_from from the dict when None
        # (schema.py:119); project_owners' check `republished_from is None`
        # works on dict.get() which returns None for absent keys.
        assert _pending[0].get('republished_from') is None



def test_generate_pending_claims_skips_pulled_from_peer():
    """A build record stamped `pulled_from` (pulled from a peer that owns it)
    generates NO claim — we never take ownership of a peer's package on our
    own publish (the reverse-sync footgun)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish

    def _fake_read(_dir, _pkg):
        return {
            'package': 'foo', 'intended_version': '1.0',
            'built_version': '1.0', 'phase': 'done',
            'outputs': ['foo_1.0_all.deb'],
            'output_hashes': {'foo_1.0_all.deb': 'd' * 64},
            'pulled_from': {'mirror': 'm1', 'builder': 'bob'},
            'finished': '2026-06-04T12:00:00Z',
        }
    with tempfile.TemporaryDirectory() as _td:
        _log = os.path.join(_td, 'log')
        os.makedirs(_log)
        with open(os.path.join(_log, 'foo.build.json'), 'w') as _fh:
            _fh.write('{}')
        _claims = os.path.join(_td, 'claims')
        os.makedirs(_claims)
        _pending = _publish.generate_pending_claims(
            builder_id='alice', buildlog_dir=_log, claims_dir=_claims,
            public_key_path='/nonexistent.pub', snapshot_pin='S',
            read_build_record=_fake_read)
        assert _pending == [], _pending



def test_generate_pending_claims_reclaims_rebuilt_pulled_from():
    """Ownership-divergence fix: a pulled_from record whose adopted outputs are
    GONE from the pool — superseded by a local rebuild at a new version — gets
    the REBUILT pool file claimed (filename + recomputed hash + version derived
    from the filename).  If the adopted output is still present, it stays
    skipped (genuinely adopted)."""
    import hashlib
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish

    def _fake_read(_dir, _pkg):
        return {
            'package': 'foo', 'intended_version': '1.0',
            'built_version': '1.0', 'phase': 'done',
            'outputs': ['foo_1.0_all.deb'],               # the ADOPTED version
            'output_hashes': {'foo_1.0_all.deb': 'd' * 64},
            'pulled_from': {'mirror': 'm1', 'builder': 'bob'},
            'finished': '2026-06-04T12:00:00Z',
        }
    with tempfile.TemporaryDirectory() as _td:
        _log = os.path.join(_td, 'log')
        os.makedirs(_log)
        with open(os.path.join(_log, 'foo.build.json'), 'w') as _fh:
            _fh.write('{}')
        _claims = os.path.join(_td, 'claims')
        os.makedirs(_claims)
        _reb = os.path.join(_td, 'foo_1.0+asg1u3_all.deb')
        with open(_reb, 'wb') as _fh:
            _fh.write(b'rebuilt-deb-bytes')
        _want_sha = hashlib.sha256(b'rebuilt-deb-bytes').hexdigest()
        # adopted output GONE from pool, rebuilt present → claim the rebuilt file
        _p = _publish.generate_pending_claims(
            builder_id='alice', buildlog_dir=_log, claims_dir=_claims,
            public_key_path='/nonexistent.pub', snapshot_pin='S',
            read_build_record=_fake_read,
            pool={'foo_1.0+asg1u3_all.deb': _reb})
        assert len(_p) == 1, _p
        assert _p[0]['filename'] == 'foo_1.0+asg1u3_all.deb'
        assert _p[0]['built_version'] == '1.0+asg1u3'      # derived from filename
        assert _p[0]['sha256'] == _want_sha                # recomputed, not stale
        # adopted output IS the latest in the pool (no rebuild) → genuinely
        # adopted → skip
        _p2 = _publish.generate_pending_claims(
            builder_id='alice', buildlog_dir=_log, claims_dir=_claims,
            public_key_path='/nonexistent.pub', snapshot_pin='S',
            read_build_record=_fake_read,
            pool={'foo_1.0_all.deb': _reb})
        assert _p2 == [], _p2



def test_federation_lab_build_mode_peer_workflow():
    """End-to-end build-mode peer federation simulation over a local-fs
    'mirror' (no SSH/Docker): owner publish → peer register + pull (snapshot
    auto-adopt + canonical-config adopt) → peer build + publish (build-mode,
    owned-only) → owner sync-back → decommission (claims drop).  Exercises the
    real coord/ machinery; full harness in tests/federation_lab.py."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'tests'))
    import federation_lab
    assert federation_lab.run(verbose=False)['ok']



def test_federation_lab_hash_conflict():
    """Two builders build one filename with different bytes (non-reproducible
    build) → the real cross-builder hash-conflict scan flags CRITICAL.
    Harness: tests/federation_lab.py."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'tests'))
    import federation_lab
    assert federation_lab.run_hash_conflict(verbose=False)['critical']



def test_federation_lab_ownership_transfer():
    """The publish ownership decision matrix against a real owner record:
    no-owner → take, other-owner-higher-version → transfer, same/lower →
    ownership_blocked, different-bytes → frozen_bytes_blocked.  Harness:
    tests/federation_lab.py."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'tests'))
    import federation_lab
    assert federation_lab.run_ownership_transfer(verbose=False)['ok']



def test_federation_lab_snapshot_divergence():
    """Snapshot divergence: a peer behind the mirror is warned to pull, the
    pull forward-adopts and clears it, a publisher ahead gets an ADVANCE
    note.  Harness: tests/federation_lab.py."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'tests'))
    import federation_lab
    assert federation_lab.run_divergence(verbose=False)['ok']



def test_federation_lab_reclaim():
    """RECLAIM-01: a same-version rebuild with different bytes is
    frozen_bytes_blocked unless it's a reclaim (reclaims_seq), which folds
    the old claim so the conflict scan stays clean.  Harness:
    tests/federation_lab.py."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'tests'))
    import federation_lab
    assert federation_lab.run_reclaim(verbose=False)['ok']



def test_federation_lab_publish_halt_recovery():
    """PUBLISH_HALT blocks publish; clearing it (the `mirror conflict
    resolve` recovery) restores publishing.  Harness:
    tests/federation_lab.py."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'tests'))
    import federation_lab
    assert federation_lab.run_publish_halt(verbose=False)['ok']



def test_canonical_config_round_trip_and_verify():
    """v2: owner writes the 4 lists (raw, byte-faithful) + selection payload
    into canonical.json (sha pinned in the head); a peer applies only on sha
    match, gets the 4 lists verbatim + the pins/closure payload; mismatch /
    missing head pin → refused, local lists untouched."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import config_manifest as _cm
    from coord import schema as _schema
    with tempfile.TemporaryDirectory() as _td:
        # Owner lists — pkg.list with COMMENTS + group header to prove the raw
        # text survives byte-for-byte (the fidelity guarantee).
        _o = {n: os.path.join(_td, f'o_{n}.list')
              for n in ('pkg', 'pool', 'live', 'installer')}
        _pkg_text = '[base]\n# a comment\nfirefox\nvlc\n'
        with open(_o['pkg'], 'w') as _f:
            _f.write(_pkg_text)
        with open(_o['pool'], 'w') as _f:
            _f.write('grub-pc\n')
        with open(_o['live'], 'w') as _f:
            _f.write('gnome-shell\n')
        with open(_o['installer'], 'w') as _f:
            _f.write('partman\n')
        _sel = {'pins': {'x-terminal-emulator': 'xterm'},
                'closure': {'bins': {'firefox': ['base']}},
                'flags': {'IncludeRecommends': True},
                'arch': 'amd64', 'snapshot': '20260602T173733Z'}
        _coord = os.path.join(_td, 'coord')
        _sha = _cm.write_canonical_config(
            _coord, _o['pkg'], _o['pool'], _o['live'], _o['installer'],
            selection_state=_sel)
        assert len(_sha) == 64 and os.path.isfile(_cm.manifest_path(_coord))

        # head carries the pin (back-compat: absent when not set)
        _h = _schema.new_coord_head(
            inrelease_sha256='a' * 64, snapshot={}, last_seqs={},
            head_time='T', config_sha256=_sha)
        assert _h.get('config_sha256') == _sha
        _h2 = _schema.new_coord_head(
            inrelease_sha256='a' * 64, snapshot={}, last_seqs={},
            head_time='T')
        assert 'config_sha256' not in _h2

        # peer applies with matching sha → all 4 lists overwritten verbatim,
        # payload returned.
        _p = {n: os.path.join(_td, f'p_{n}.list')
              for n in ('pkg', 'pool', 'live', 'installer')}
        with open(_p['pkg'], 'w') as _f:
            _f.write('OLD\n')
        _ok, _det, _payload = _cm.apply_canonical_config(
            _coord, _sha, _p['pkg'], _p['pool'], _p['live'], _p['installer'])
        assert _ok, _det
        with open(_p['pkg']) as _f:
            assert _f.read() == _pkg_text          # byte-faithful, comments kept
        with open(_p['pool']) as _f:
            assert _f.read() == 'grub-pc\n'
        with open(_p['live']) as _f:
            assert _f.read() == 'gnome-shell\n'
        with open(_p['installer']) as _f:
            assert _f.read() == 'partman\n'
        assert _payload['pins'] == {'x-terminal-emulator': 'xterm'}
        assert _payload['closure'] == {'bins': {'firefox': ['base']}}
        assert _payload['snapshot'] == '20260602T173733Z'

        # sha mismatch → refused, peer list unchanged
        with open(_p['pkg'], 'w') as _f:
            _f.write('KEEP\n')
        _ok2, _d2, _pl2 = _cm.apply_canonical_config(
            _coord, 'b' * 64, _p['pkg'], _p['pool'])
        assert not _ok2 and 'mismatch' in _d2 and _pl2 is None
        with open(_p['pkg']) as _f:
            assert _f.read() == 'KEEP\n'

        # no head pin → refused (never apply unverified config)
        _ok3, _d3, _pl3 = _cm.apply_canonical_config(
            _coord, '', _p['pkg'], _p['pool'])
        assert not _ok3 and 'no config_sha256' in _d3 and _pl3 is None

        # An unsupported (pre-v2) manifest shape is REFUSED — the peer's
        # lists stay untouched (pre-release policy: no legacy formats).
        import json as _json
        import hashlib as _hl
        _v1 = _json.dumps({'v': 1, 'pkg.list': 'V1PKG\n',
                           'pool.list': 'V1POOL\n'},
                          sort_keys=True, indent=2).encode()
        with open(_cm.manifest_path(_coord), 'wb') as _f:
            _f.write(_v1)
        _v1sha = _hl.sha256(_v1).hexdigest()
        _ok4, _d4, _pl4 = _cm.apply_canonical_config(
            _coord, _v1sha, _p['pkg'], _p['pool'])
        assert not _ok4 and _pl4 is None and 'unsupported schema' in _d4
        with open(_p['pkg']) as _f:
            assert _f.read() == 'KEEP\n'      # untouched from the case above



def test_apply_canonical_config_is_all_or_nothing():
    """Regression (audit #86): apply_canonical_config applies the four list
    files ALL-OR-NOTHING.  If a later list's write fails (bad dir / ENOSPC),
    pkg.list must NOT be left rewritten with federation content while the
    others keep stale content and the caller is told 'NOT applied' — a silent
    split brain where no re-resolve is forced."""
    import sys
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import config_manifest as _cm
    with tempfile.TemporaryDirectory() as _td:
        _o = {n: os.path.join(_td, f'o_{n}.list')
              for n in ('pkg', 'pool', 'live', 'installer')}
        for _n, _txt in (('pkg', 'FED-PKG\n'), ('pool', 'FED-POOL\n'),
                         ('live', 'FED-LIVE\n'), ('installer', 'FED-INST\n')):
            with open(_o[_n], 'w') as _f:
                _f.write(_txt)
        _coord = os.path.join(_td, 'coord')
        _sha = _cm.write_canonical_config(
            _coord, _o['pkg'], _o['pool'], _o['live'], _o['installer'])
        # peer's PRIOR on-disk content that MUST survive a failed apply
        _p = {n: os.path.join(_td, f'p_{n}.list')
              for n in ('pkg', 'pool', 'live')}
        for _n in _p:
            with open(_p[_n], 'w') as _f:
                _f.write(f'PRIOR-{_n.upper()}\n')
        # installer target lives in a NON-EXISTENT dir → staging raises mid-way
        _bad_installer = os.path.join(_td, 'nope', 'p_installer.list')

        _ok, _det, _payload = _cm.apply_canonical_config(
            _coord, _sha, _p['pkg'], _p['pool'], _p['live'], _bad_installer)
        assert _ok is False, (_ok, _det)
        # all-or-nothing: NONE of the existing lists were mutated
        for _n in ('pkg', 'pool', 'live'):
            with open(_p[_n]) as _f:
                assert _f.read() == f'PRIOR-{_n.upper()}\n', (
                    f'{_n}.list partially applied on a failed apply: {_det}')
        assert not os.path.exists(_bad_installer)

        # sanity: a fully-valid apply DOES overwrite all four
        _good_installer = os.path.join(_td, 'p_installer.list')
        _ok2, _, _ = _cm.apply_canonical_config(
            _coord, _sha, _p['pkg'], _p['pool'], _p['live'], _good_installer)
        assert _ok2 is True
        with open(_p['pkg']) as _f:
            assert _f.read() == 'FED-PKG\n'
        with open(_good_installer) as _f:
            assert _f.read() == 'FED-INST\n'



def test_closure_ledger_write_read_verify_round_trip():
    """Owner writes closure_ledger.json (sha pinned in head); a peer reads it
    only on sha match; empty pin / mismatch / malformed → refused with None."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import config_manifest as _cm
    from coord import schema as _schema
    with tempfile.TemporaryDirectory() as _td:
        _coord = os.path.join(_td, 'coord')
        _doc = _schema.new_closure_ledger(
            codename='thor', snapshot='20260602T173733Z',
            generated_at='2026-06-20T00:00:00Z',
            entries={'libfoo|amd64': {
                'filename': 'libfoo_1.2_amd64.deb', 'sha256': 'a' * 64,
                'size': 100, 'component': 'main', 'version': '1.2',
                'package': 'libfoo', 'arch': 'amd64'}})
        _sha = _cm.write_closure_ledger(_coord, _doc)
        assert len(_sha) == 64
        assert os.path.isfile(_cm.closure_ledger_path(_coord))

        # head carries the pin
        _h = _schema.new_coord_head(
            inrelease_sha256='a' * 64, snapshot={}, last_seqs={},
            head_time='T', closure_ledger_sha256=_sha)
        assert _h['closure_ledger_sha256'] == _sha

        # matching sha → verified, doc returned intact
        _ok, _det, _got = _cm.read_verified_closure_ledger(_coord, _sha)
        assert _ok, _det
        assert _got == _doc

        # empty pin (old head, no ledger) → refused
        _ok2, _d2, _g2 = _cm.read_verified_closure_ledger(_coord, '')
        assert not _ok2 and 'no closure_ledger_sha256' in _d2 and _g2 is None

        # sha mismatch → refused
        _ok3, _d3, _g3 = _cm.read_verified_closure_ledger(_coord, 'b' * 64)
        assert not _ok3 and 'mismatch' in _d3 and _g3 is None

        # malformed (no entries map) → refused
        import json as _json
        with open(_cm.closure_ledger_path(_coord), 'wb') as _f:
            _f.write(_json.dumps({'v': 1}, sort_keys=True,
                                 indent=2).encode())
        import hashlib as _hl
        _bad = _hl.sha256(
            _json.dumps({'v': 1}, sort_keys=True, indent=2).encode()
        ).hexdigest()
        _ok4, _d4, _g4 = _cm.read_verified_closure_ledger(_coord, _bad)
        assert not _ok4 and 'malformed' in _d4 and _g4 is None

        # absent file → refused
        _ok5, _d5, _g5 = _cm.read_verified_closure_ledger(
            os.path.join(_td, 'nope'), _sha)
        assert not _ok5 and 'no closure ledger' in _d5 and _g5 is None



def test_mirror03_publish_hazard_gate_blocks_unsanctioned_local_ahead():
    """MIRROR-03: a same-version rebuild that diverged on disk (local-ahead)
    but isn't being reclaimed must BLOCK publish — otherwise the dist-tree
    rsync overwrites the frozen remote bytes while no claim is emitted,
    stranding the signed claim (claim_apt_sha_mismatch).  The pure gate
    returns the not-reclaimed rows; remote_publish aborts on them BEFORE the
    dist-tree push and exempts reclaim intents."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import coord.publish as _publish
    _ahead = [{'filename': 'a_1_all.deb'}, {'filename': 'b_1_all.deb'}]
    # reclaiming only 'a' leaves 'b' unsanctioned → blocks
    assert _publish.unsanctioned_local_ahead(
        _ahead, [{'filename': 'a_1_all.deb'}]) == [{'filename': 'b_1_all.deb'}]
    # every divergence reclaimed → nothing blocks (the reclaim path)
    assert _publish.unsanctioned_local_ahead(
        _ahead, [{'filename': 'a_1_all.deb'},
                 {'filename': 'b_1_all.deb'}]) == []
    # nothing ahead → nothing blocks (the normal publish)
    assert _publish.unsanctioned_local_ahead([], []) == []
    # the gate runs ahead of the dist-tree push inside remote_publish
    _src = inspect.getsource(_publish.remote_publish)
    assert 'unsanctioned_local_ahead' in _src and 'local_ahead_fn' in _src
    assert _src.index('unsanctioned_local_ahead') < _src.index('pushing dist tree')



def test_filter_pending_by_ownership_no_existing_owner_keeps():
    """Decision matrix row 1: no claim → KEEP (we become owner)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    _c = _new_pending_claim('alice', 'foo', 'foo.deb', '1.0')
    _kept, _blocked = _publish.filter_pending_by_ownership(
        'alice', [_c], existing_owners={})
    assert _kept == [_c]
    assert _blocked == []



def test_filter_pending_by_ownership_own_claim_keeps():
    """Decision matrix row 2: we already own → KEEP."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    _c = _new_pending_claim('alice', 'foo', 'foo.deb', '1.0')
    _existing = {
        'foo.deb': {
            'builder': 'alice', 'version': '1.0', 'sha256': 'f' * 64,
            'claim_state': 'published', 'seq': 5,
            'republished_from': None, 'claim': {},
        }
    }
    _kept, _blocked = _publish.filter_pending_by_ownership(
        'alice', [_c], existing_owners=_existing)
    assert _kept == [_c]
    assert _blocked == []



def test_filter_pending_by_ownership_tunneled_keeps_takes_ownership():
    """Decision matrix row 3: tunneled (no owner) → KEEP, take
    ownership.  Our claim ships without republished_from, which means
    on next read project_owners will see us as the owner."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    _c = _new_pending_claim('alice', 'vlc', 'vlc.deb', '3.0-1')
    _existing = {
        'vlc.deb': {
            'builder': None,            # tunneled
            'version': '3.0-1',
            'sha256': 'f' * 64,
            'claim_state': 'published',
            'seq': 2,
            'republished_from': {'url': 'http://x', 'upstream_sha256': 'b' * 64},
            'claim': {},
        }
    }
    _kept, _blocked = _publish.filter_pending_by_ownership(
        'alice', [_c], existing_owners=_existing)
    assert _kept == [_c]
    assert _blocked == []



def test_filter_pending_by_ownership_higher_version_transfers_ownership():
    """Decision matrix row 4: owned by other, our version strictly
    higher → KEEP, ownership transfers."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    _c = _new_pending_claim('alice', 'foo', 'foo.deb', '2.0')
    _existing = {
        'foo.deb': {
            'builder': 'bob', 'version': '1.0', 'sha256': 'f' * 64,
            'claim_state': 'published', 'seq': 3,
            'republished_from': None, 'claim': {},
        }
    }
    _kept, _blocked = _publish.filter_pending_by_ownership(
        'alice', [_c], existing_owners=_existing)
    assert _kept == [_c]
    assert _blocked == []



def test_filter_pending_by_ownership_same_version_other_owner_blocks():
    """Decision matrix row 5: owned by other, same version → BLOCK
    with ownership_blocked finding."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    _c = _new_pending_claim('alice', 'foo', 'foo.deb', '1.0')
    _existing = {
        'foo.deb': {
            'builder': 'bob', 'version': '1.0', 'sha256': 'f' * 64,
            'claim_state': 'published', 'seq': 3,
            'republished_from': None, 'claim': {},
        }
    }
    _kept, _blocked = _publish.filter_pending_by_ownership(
        'alice', [_c], existing_owners=_existing)
    assert _kept == []
    assert len(_blocked) == 1
    _b = _blocked[0]
    assert _b['filename'] == 'foo.deb'
    assert _b['owner'] == 'bob'
    assert _b['our_version'] == '1.0'
    assert _b['owner_version'] == '1.0'
    assert 'not strictly higher' in _b['reason']



def test_filter_pending_by_ownership_lower_version_blocks():
    """Decision matrix row 6: owned by other, our version is LOWER →
    BLOCK (don't downgrade their pkg)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    _c = _new_pending_claim('alice', 'foo', 'foo.deb', '1.0')
    _existing = {
        'foo.deb': {
            'builder': 'bob', 'version': '2.0', 'sha256': 'f' * 64,
            'claim_state': 'published', 'seq': 3,
            'republished_from': None, 'claim': {},
        }
    }
    _kept, _blocked = _publish.filter_pending_by_ownership(
        'alice', [_c], existing_owners=_existing)
    assert _kept == []
    assert len(_blocked) == 1



def test_filter_pending_by_ownership_partial_publish_continues():
    """Mixed bag: 3 candidates, 1 owned-by-other-same-version (blocked),
    2 OK.  Blocked candidate doesn't abort the others — partial-success
    is the design."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    _c1 = _new_pending_claim('alice', 'foo', 'foo.deb', '1.0')
    _c2 = _new_pending_claim('alice', 'bar', 'bar.deb', '1.0')
    _c3 = _new_pending_claim('alice', 'baz', 'baz.deb', '1.0')
    _existing = {
        'bar.deb': {
            'builder': 'bob', 'version': '1.0', 'sha256': 'f' * 64,
            'claim_state': 'published', 'seq': 3,
            'republished_from': None, 'claim': {},
        }
        # foo.deb + baz.deb have no claim → KEEP
    }
    _kept, _blocked = _publish.filter_pending_by_ownership(
        'alice', [_c1, _c2, _c3], existing_owners=_existing)
    _kept_fns = {_k['filename'] for _k in _kept}
    assert _kept_fns == {'foo.deb', 'baz.deb'}
    assert len(_blocked) == 1
    assert _blocked[0]['filename'] == 'bar.deb'



def test_filter_pending_by_ownership_blocks_frozen_byte_mismatch():
    """STA-47: an existing owner record means the remote pool already
    holds FROZEN bytes under this filename.  If our rebuilt bytes differ
    (builds aren't bit-reproducible) and this isn't a sanctioned reclaim,
    publishing must be BLOCKED — push_single_deb(--size-only) would
    skip the upload, so the remote keeps the old bytes while our new claim
    pins a sha the pool doesn't serve (claim_apt_sha_mismatch + peer
    delete-on-mismatch).  Covers the keep-despite-existing-owner cases:
    owner-is-us and tunneled.  A reclaim (reclaims_seq set) is exempt."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish

    # candidate default sha = 'f'*64; owners below carry a DIFFERENT sha.
    for _owner_builder, _label in (('alice', 'owner-is-us'),
                                   (None, 'tunneled')):
        _c = _new_pending_claim('alice', 'foo', 'foo.deb', '1.0')
        _existing = {
            'foo.deb': {
                'builder': _owner_builder, 'version': '1.0',
                'sha256': 'd' * 64,      # frozen remote bytes differ
                'claim_state': 'published', 'seq': 5,
                'republished_from': ({'url': 'http://x'} if _owner_builder
                                     is None else None),
                'claim': {},
            }
        }
        _kept, _blocked = _publish.filter_pending_by_ownership(
            'alice', [_c], existing_owners=_existing)
        assert _kept == [], f"{_label}: must block frozen-byte mismatch"
        assert len(_blocked) == 1, _label
        assert 'frozen pool copy' in _blocked[0]['reason'], _blocked
        assert 'mirror reclaim' in _blocked[0]['reason'], _blocked

    # A reclaim (reclaims_seq set) is the sanctioned same-filename path —
    # NOT blocked even though the sha differs.
    _rc = _new_pending_claim('alice', 'foo', 'foo.deb', '1.0')
    _rc['reclaims_seq'] = 5
    _existing = {
        'foo.deb': {
            'builder': 'alice', 'version': '1.0', 'sha256': 'd' * 64,
            'claim_state': 'published', 'seq': 5,
            'republished_from': None, 'claim': {},
        }
    }
    _kept, _blocked = _publish.filter_pending_by_ownership(
        'alice', [_rc], existing_owners=_existing)
    assert _kept == [_rc], "reclaim must pass the frozen-byte guard"
    assert _blocked == []

    # Matching bytes (reproducible rebuild) → NOT blocked by this guard.
    _same = _new_pending_claim('alice', 'foo', 'foo.deb', '1.0',
                               sha='f' * 64)
    _existing = {
        'foo.deb': {
            'builder': 'alice', 'version': '1.0', 'sha256': 'f' * 64,
            'claim_state': 'published', 'seq': 5,
            'republished_from': None, 'claim': {},
        }
    }
    _kept, _blocked = _publish.filter_pending_by_ownership(
        'alice', [_same], existing_owners=_existing)
    assert _kept == [_same] and _blocked == []



def test_coord_store_project_owners_single_owner_per_filename():
    """MIRROR-02 chunk 7: project_owners picks the latest claim per
    filename across all builders and sets `builder` correctly."""
    _s, _i, _st, *_ = _coord_modules()
    _c1 = _s.new_claim(
        builder='alice', seq=5, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo_1.0.deb', sha256='a' * 64,
        size=1, snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _owners = _st.project_owners({'alice': [_c1]})
    assert 'foo_1.0.deb' in _owners
    _rec = _owners['foo_1.0.deb']
    assert _rec['builder'] == 'alice'
    assert _rec['version'] == '1.0'
    assert _rec['republished_from'] is None  # non-tunneled
    assert _rec['claim_state'] == 'published'



def test_coord_store_project_owners_tunneled_has_no_owner():
    """Tunneled claim (republished_from set) → owner is None.  This is
    how chunk 8's publish decision sees 'no current owner — anyone
    can claim'."""
    _s, _i, _st, *_ = _coord_modules()
    _tunneled = _s.new_claim(
        builder='alice', seq=1, package='vlc',
        intended_version='3.0-1', built_version='3.0-1',
        filename='vlc_3.0-1.deb', sha256='c' * 64,
        size=1, snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
        republished_from={'url': 'http://deb.debian.org/.../vlc_3.0-1.deb',
                          'upstream_sha256': 'd' * 64},
    )
    _owners = _st.project_owners({'alice': [_tunneled]})
    _rec = _owners['vlc_3.0-1.deb']
    assert _rec['builder'] is None     # tunneled → no owner
    assert _rec['republished_from'] is not None
    assert _rec['republished_from']['upstream_sha256'] == 'd' * 64



def test_coord_store_project_owners_picks_latest_by_seq():
    """Two builders with claims for the same filename — higher seq wins.
    Stable tiebreak by builder lexical sort when seqs are equal."""
    _s, _i, _st, *_ = _coord_modules()
    _alice_lower = _s.new_claim(
        builder='alice', seq=1, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo.deb', sha256='a' * 64, size=1,
        snapshot='S1', built_at='T1',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _bob_higher = _s.new_claim(
        builder='bob', seq=7, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo.deb', sha256='a' * 64, size=1,
        snapshot='S1', built_at='T1',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _owners = _st.project_owners({
        'alice': [_alice_lower], 'bob': [_bob_higher]})
    assert _owners['foo.deb']['builder'] == 'bob'
    assert _owners['foo.deb']['seq'] == 7



def test_project_owners_published_takeover_beats_higher_seq_deprecation():
    """Regression (audit #104): a builder's higher-seq DEPRECATION marker must
    NOT outrank another builder's lower-seq live PUBLISHED takeover for the same
    filename.  seq is builder-local, so ranking purely by (-seq, builder) let an
    established builder A's deprecation win and report the file as un-owned even
    though newcomer B legitimately republished and owns it."""
    _s, _i, _st, *_ = _coord_modules()
    _alice_dep = _s.new_claim(
        builder='alice', seq=9, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo.deb', sha256='a' * 64, size=1,
        snapshot='S1', built_at='T1',
        claim_state=_s.CLAIM_STATE_DEPRECATED,
    )
    _bob_pub = _s.new_claim(
        builder='bob', seq=2, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo.deb', sha256='a' * 64, size=1,
        snapshot='S1', built_at='T1',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _owners = _st.project_owners({'alice': [_alice_dep], 'bob': [_bob_pub]})
    # B's live PUBLISHED claim wins over A's higher-seq deprecation marker.
    assert _owners['foo.deb']['builder'] == 'bob', _owners['foo.deb']
    assert _owners['foo.deb']['seq'] == 2
    # The all-deprecation case (no takeover) still yields no owner.
    _owners2 = _st.project_owners({'alice': [_alice_dep]})
    assert _owners2['foo.deb']['builder'] is None, _owners2['foo.deb']



def test_coord_store_project_owners_skips_retracted():
    """A retracted claim isn't a candidate for ownership."""
    _s, _i, _st, *_ = _coord_modules()
    _c1 = _s.new_claim(
        builder='alice', seq=1, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo.deb', sha256='a' * 64, size=1,
        snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _r1 = _s.new_retraction(
        builder='alice', seq=2, package='foo',
        retracts_seq=1, filename='foo.deb',
        snapshot='S', built_at='T',
    )
    _owners = _st.project_owners({'alice': [_c1, _r1]})
    assert 'foo.deb' not in _owners



def test_coord_store_project_owners_handles_empty_input():
    """Empty by_builder → empty projection (no errors)."""
    _s, _i, _st, *_ = _coord_modules()
    assert _st.project_owners({}) == {}
    assert _st.project_owners({'alice': []}) == {}



def test_snapshot_divergence_note():
    """Publish-time snapshot divergence note: local pin ahead → ADVANCE note;
    mirror ahead → behind-WARNING; equal/unknown → None."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord.publish import snapshot_divergence_note as _n
    _old, _new = '20260101T000000Z', '20260601T000000Z'
    assert _n(_new, _old).startswith('snapshot: this publish ADVANCES')
    assert _n(_old, _new).startswith('WARNING: mirror snapshot')
    assert _n(_old, _old) is None
    assert _n('', _new) is None
    assert _n(_new, '') is None



def test_publish_lock_records_and_reports_holder():
    """The publish flock writes a `<lock>.holder` sidecar with our builder-id
    so a blocked peer can be told who is publishing; remote_flock_holder
    reads it back (blank/error → None)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import patch, MagicMock
    from coord import transport as _tx

    # acquire builds the holder write/remove into the flock'd command
    _cap = {}

    def _popen(cmd, **kw):
        _cap['cmd'] = ' '.join(cmd)
        raise OSError('stop')   # spawn-fail path → returns None fast
    with patch('coord.transport.subprocess.Popen', _popen):
        assert _tx.remote_flock_acquire(
            ssh_host='h', lock_path='/var/lock/x', builder_id='bob') is None
    assert '/var/lock/x.holder' in _cap['cmd'] and 'echo bob >' in _cap['cmd']

    # holder reader parses the sidecar
    def _mkrun(out):
        _m = MagicMock(); _m.stdout = out; return _m
    with patch('coord.transport.subprocess.run', return_value=_mkrun('alice\n')):
        assert _tx.remote_flock_holder(
            ssh_host='h', lock_path='/l') == 'alice'
    with patch('coord.transport.subprocess.run', return_value=_mkrun('')):
        assert _tx.remote_flock_holder(ssh_host='h', lock_path='/l') is None
    with patch('coord.transport.subprocess.run', side_effect=OSError):
        assert _tx.remote_flock_holder(ssh_host='h', lock_path='/l') is None



def test_remote_publish_validates_under_lock_before_push():
    """TOCTOU: remote_publish acquires the flock, then fetches the remote
    tree, re-scans hash conflicts + closure, and only then pushes — in that
    order, all under one lock.  So a publish that landed after an operator's
    earlier `mirror audit` is re-validated here, not raced."""
    import sys as _sys
    import inspect
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    _src = inspect.getsource(_publish.remote_publish)

    def _pos(_tok):
        _i = _src.find(_tok)
        assert _i != -1, f"{_tok} not found in remote_publish"
        return _i
    _lock = _pos('remote_flock_acquire')
    _fetch = _pos('pull_remote_coord')
    _conf = _pos('detect_hash_conflicts')
    _closure = _pos('find_publish_closure_breaks')
    _push = _pos('push_single_deb')
    assert _lock < _fetch < _conf < _closure < _push, (
        f"publish steps out of order: lock={_lock} fetch={_fetch} "
        f"conflict={_conf} closure={_closure} push={_push}")



def test_audit_sources_chain_verifies_and_indexes_source_files():
    """MAT-02 stage 4: audit_sources_chain pulls every <comp>/source/Sources
    the verified InRelease declares, verifies the sha pin, and indexes every
    referenced source FILE (dsc + tarballs) for the claim cross-check.
    A sha mismatch is CRITICAL; a release with no source indexes yields an
    empty index and no findings (sources not yet published)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import hashlib as _hashlib
    from unittest.mock import patch
    import mirror as _mir
    from coord import transport as _tx

    _sources = (
        'Package: demo\n'
        'Version: 1.0-1+asg1u1\n'
        'Directory: pool\n'
        'Files:\n'
        ' 0123456789abcdef0123456789abcdef 100 demo_1.0-1+asg1u1.dsc\n'
        'Checksums-Sha256:\n'
        f' {"a" * 64} 100 demo_1.0-1+asg1u1.dsc\n'
        f' {"b" * 64} 200 demo_1.0.orig.tar.gz\n'
        '\n').encode()
    _pin = _hashlib.sha256(_sources).hexdigest()

    def _fake_pull(*, remote_spec, local_path, ssh_key=None):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, 'wb') as _fh:
            _fh.write(_sources)
        return True, ''

    _release = {'SHA256': [
        {'name': 'main/source/Sources', 'sha256': _pin, 'size': '9'},
        {'name': 'main/binary-amd64/Packages', 'sha256': 'c' * 64,
         'size': '9'},
    ]}
    with tempfile.TemporaryDirectory() as _td:
        with patch.object(_tx, 'pull_single_file', side_effect=_fake_pull):
            _idx, _findings = _mir.audit_sources_chain(
                pool_url='file:///fake', codename='thor',
                release=_release, fetched_dir=_td)
        assert _findings == [], _findings
        assert _idx['demo_1.0-1+asg1u1.dsc']['sha256'] == 'a' * 64
        assert _idx['demo_1.0.orig.tar.gz']['size'] == '200'
        assert _idx['demo_1.0.orig.tar.gz']['source'] == 'demo'
        assert _idx['demo_1.0.orig.tar.gz']['component'] == 'main'
        # sha mismatch → CRITICAL, nothing indexed from that file
        _release_bad = {'SHA256': [
            {'name': 'main/source/Sources', 'sha256': 'd' * 64,
             'size': '9'}]}
        with patch.object(_tx, 'pull_single_file', side_effect=_fake_pull):
            _idx2, _f2 = _mir.audit_sources_chain(
                pool_url='file:///fake', codename='thor',
                release=_release_bad, fetched_dir=_td)
        assert _idx2 == {}
        assert any(_k == 'sources_sha_mismatch' and _s == 'CRITICAL'
                   for _s, _k, _m in _f2), _f2
        # no source indexes declared → clean empty
        _idx3, _f3 = _mir.audit_sources_chain(
            pool_url='file:///fake', codename='thor',
            release={'SHA256': [{'name': 'main/binary-amd64/Packages',
                                 'sha256': 'c' * 64, 'size': '9'}]},
            fetched_dir=_td)
        assert _idx3 == {} and _f3 == []


def test_audit_inrelease_against_head_sha_match_returns_parsed_release():
    """Happy path: InRelease pulls successfully, sha matches the
    coord-head pin, deb822.Release parses cleanly, no findings."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import hashlib as _hashlib
    from unittest.mock import patch
    import mirror as _mir
    from coord import transport as _tx

    with tempfile.TemporaryDirectory() as _td:
        # Build a fake InRelease in a sibling dir; rely on the rsync
        # stub to "pull" it (just copies path-to-path).
        _src_dir = os.path.join(_td, 'src')
        os.makedirs(_src_dir)
        _src = os.path.join(_src_dir, 'InRelease')
        _write_clearsigned_inrelease(_src, [(
            'a' * 64, 100, 'main/binary-amd64/Packages')])
        _expected_sha = _hashlib.sha256(open(_src, 'rb').read()).hexdigest()

        _fetched = os.path.join(_td, 'fetched')

        def _fake_pull(*, remote_spec, local_path, ssh_key=None):
            # remote_spec ends with `/InRelease`; copy from our src.
            import shutil as _sh
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            _sh.copy(_src, local_path)
            return True, ''

        with patch.object(_tx, 'pull_single_file', side_effect=_fake_pull):
            _release, _findings = _mir.audit_inrelease_against_head(
                pool_url='file:///fake/pool',
                codename='thor',
                expected_sha256=_expected_sha,
                fetched_dir=_fetched,
            )

        assert _findings == [], f"unexpected findings: {_findings}"
        assert _release is not None
        assert _release.get('Codename') == 'thor'
        # SHA256 block should be parseable.
        _sha_block = _release.get('SHA256')
        assert isinstance(_sha_block, list) and len(_sha_block) == 1



def test_audit_inrelease_against_head_sha_mismatch_critical():
    """coord-head pins sha=X, on-pool InRelease hashes to Y → CRITICAL
    inrelease_sha_mismatch.  Returned release is None so caller
    short-circuits the chain."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import patch
    import mirror as _mir
    from coord import transport as _tx

    with tempfile.TemporaryDirectory() as _td:
        _src_dir = os.path.join(_td, 'src')
        os.makedirs(_src_dir)
        _src = os.path.join(_src_dir, 'InRelease')
        _write_clearsigned_inrelease(_src, [(
            'a' * 64, 100, 'main/binary-amd64/Packages')])

        def _fake_pull(*, remote_spec, local_path, ssh_key=None):
            import shutil as _sh
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            _sh.copy(_src, local_path)
            return True, ''

        with patch.object(_tx, 'pull_single_file', side_effect=_fake_pull):
            _release, _findings = _mir.audit_inrelease_against_head(
                pool_url='file:///fake/pool',
                codename='thor',
                expected_sha256='0' * 64,  # deliberate mismatch
                fetched_dir=os.path.join(_td, 'fetched'),
            )

        assert _release is None
        assert len(_findings) == 1
        _sev, _kind, _ = _findings[0]
        assert _sev == 'CRITICAL'
        assert _kind == 'inrelease_sha_mismatch'



def test_audit_ownership_summary_buckets_correctly():
    """Summary counts: we_own (our_builder_id), peers_own (others),
    tunneled (republished_from set → builder=None in project_owners)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    from coord import schema as _sch

    def _claim(builder, seq, fn, sha, republished_from=None):
        _c = _sch.new_claim(
            builder=builder, seq=seq, package=fn.split('_', 1)[0],
            intended_version='1.0', built_version='1.0',
            filename=fn, sha256=sha, size=1,
            snapshot='S', built_at='T',
            claim_state=_sch.CLAIM_STATE_PUBLISHED,
            republished_from=republished_from,
        )
        return _c

    _by_builder = {
        'athena-primary': [
            _claim('athena-primary', 1, 'pkg-ours-1_1.0_amd64.deb',
                   'a' * 64),
            _claim('athena-primary', 2, 'pkg-ours-2_1.0_amd64.deb',
                   'b' * 64),
            _claim('athena-primary', 3, 'pkg-tunneled_1.0_amd64.deb',
                   'c' * 64,
                   republished_from={'url': 'http://u', 'upstream_sha256': 'c'}),
        ],
        'athena-secondary': [
            _claim('athena-secondary', 1, 'pkg-peer_1.0_amd64.deb',
                   'd' * 64),
        ],
    }

    _summary, _findings = _mir.audit_ownership_summary(
        _by_builder, our_builder_id='athena-primary')

    assert _summary['total'] == 4
    assert _summary['we_own'] == 2
    assert _summary['peers_own'] == 1
    assert _summary['tunneled'] == 1
    assert _summary['by_peer'] == {'athena-secondary': 1}
    assert _findings == []



def test_audit_federation_walk_unreachable_peer_is_critical():
    """A neighbour URL that isn't configured locally — walker attempts
    rsync pull; on fail emits CRITICAL federation_neighbour_unreachable."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import patch
    import mirror as _mir
    from coord import transport as _tx
    _per_mirror = [
        {'name': 'm1', 'url': 'ssh://a/repo', 'head': {
            'neighbours': ['ssh://a/repo', 'ssh://c/repo'],  # c not local
        }, 'by_builder': {}},
    ]

    def _fake_pull(*, local_dest, remote_spec, ssh_key=None):
        return False, 'connection refused'

    with tempfile.TemporaryDirectory() as _td:
        with patch.object(_tx, 'pull_remote_coord',
                          side_effect=_fake_pull):
            _walked, _findings = _mir.audit_federation_walk(
                _per_mirror, signing_home='/dev/null', cache_dir=_td)
    assert _walked == []
    assert len(_findings) == 1
    _sev, _kind, _msg = _findings[0]
    assert _sev == 'CRITICAL'
    assert _kind == 'federation_neighbour_unreachable'
    assert 'connection refused' in _msg



def test_audit_federation_walk_unverified_peer_is_critical():
    """Pull succeeds but coord-head signature can't be verified
    (read_coord_head returns None) → CRITICAL federation_neighbour_unverified."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import patch
    import mirror as _mir
    from coord import head as _head_mod
    from coord import transport as _tx
    _per_mirror = [
        {'name': 'm1', 'url': 'ssh://a/repo', 'head': {
            'neighbours': ['ssh://a/repo', 'ssh://c/repo'],
        }, 'by_builder': {}},
    ]

    def _fake_pull(*, local_dest, remote_spec, ssh_key=None):
        return True, ''

    with tempfile.TemporaryDirectory() as _td:
        with patch.object(_tx, 'pull_remote_coord',
                          side_effect=_fake_pull), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=None):
            _walked, _findings = _mir.audit_federation_walk(
                _per_mirror, signing_home='/dev/null', cache_dir=_td)
    assert _walked == []
    assert len(_findings) == 1
    assert _findings[0][1] == 'federation_neighbour_unverified'



def test_audit_federation_walk_asymmetric_membership_is_critical():
    """Peer's coord-head is reachable + verified, but its declared
    neighbours don't overlap with the source mirror's federation view
    (excluding the peer itself) → CRITICAL federation_neighbour_asymmetric."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import patch
    import mirror as _mir
    from coord import head as _head_mod
    from coord import transport as _tx
    _per_mirror = [
        {'name': 'm1', 'url': 'ssh://a/repo', 'head': {
            'neighbours': ['ssh://a/repo', 'ssh://b/repo', 'ssh://c/repo'],
        }, 'by_builder': {}},
        {'name': 'm2', 'url': 'ssh://b/repo', 'head': {
            'neighbours': ['ssh://a/repo', 'ssh://b/repo', 'ssh://c/repo'],
        }, 'by_builder': {}},
    ]
    # Peer C's coord-head — DOES NOT list a or b → asymmetric.
    _peer_head = {
        'neighbours': ['ssh://c/repo', 'ssh://d/repo'],
        'revoked_builders': {},
    }

    def _fake_pull(*, local_dest, remote_spec, ssh_key=None):
        return True, ''

    with tempfile.TemporaryDirectory() as _td:
        with patch.object(_tx, 'pull_remote_coord',
                          side_effect=_fake_pull), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=_peer_head):
            _walked, _findings = _mir.audit_federation_walk(
                _per_mirror, signing_home='/dev/null', cache_dir=_td)
    assert len(_walked) == 1
    assert _walked[0]['head'] is _peer_head
    # Both m1 and m2 declare c; cycle guard means we walk c once and
    # emit at most one asymmetric finding (against the first declarer).
    _asym = [_f for _f in _findings
             if _f[1] == 'federation_neighbour_asymmetric']
    assert len(_asym) == 1



def test_coord_reconcile_detect_hash_conflicts_critical_and_info():
    """Same (pkg, ver), different hash → CRITICAL; same hash → INFO."""
    _s, _i, _st, _p, _h, _r, *_ = _coord_modules()
    _ca = _s.new_claim(
        builder='alice', seq=1, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo.deb', sha256='a' * 64, size=1,
        snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _cb_diff = _s.new_claim(
        builder='bob', seq=1, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo.deb', sha256='b' * 64, size=1,
        snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _findings = _r.detect_hash_conflicts({'alice': [_ca], 'bob': [_cb_diff]})
    _crits = [_f for _f in _findings if _f.severity == 'CRITICAL']
    assert len(_crits) == 1
    assert 'hash conflict' in _crits[0].message
    # Same hash → INFO (reproducible)
    _cb_same = _s.new_claim(
        builder='bob', seq=1, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo.deb', sha256='a' * 64, size=1,
        snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _findings2 = _r.detect_hash_conflicts({'alice': [_ca], 'bob': [_cb_same]})
    _infos = [_f for _f in _findings2 if _f.severity == 'INFO']
    assert len(_infos) == 1
    assert 'reproducible' in _infos[0].kind



def test_coord_reconcile_detect_hash_conflicts_multi_binary_same_source_no_false_positive():
    """Regression 2026-06-05: a single source builds N binaries, all
    sharing the same source name + source version but with DIFFERENT
    filenames + DIFFERENT SHA-256 (normal — different files, naturally
    different bytes).  Hash-conflict detector must NOT flag these as
    a conflict.

    Previously grouped by (package=source_name, built_version) which
    false-positived every multi-binary source.  Now grouped by
    `filename` (the unique-per-pkg+ver+arch key).
    """
    _s, _i, _st, _p, _h, _r, *_ = _coord_modules()
    # Mirror the user's actual case: one source `firmware-nonfree`
    # version `20230210-5` produces 6 distinct binaries (different
    # filenames, different SHA-256s — perfectly normal).
    _bins = [
        ('firmware-amd-graphics_20230210-5_all.deb', 'a' * 64),
        ('firmware-atheros_20230210-5_all.deb',      'b' * 64),
        ('firmware-brcm80211_20230210-5_all.deb',    'c' * 64),
        ('firmware-iwlwifi_20230210-5_all.deb',      'd' * 64),
        ('firmware-misc-nonfree_20230210-5_all.deb', 'e' * 64),
        ('firmware-realtek_20230210-5_all.deb',      'f' * 64),
    ]
    _claims = [
        _s.new_claim(
            builder='athena-primary', seq=_seq, package='firmware-nonfree',
            intended_version='20230210-5', built_version='20230210-5',
            filename=_fn, sha256=_sha, size=1,
            snapshot='S', built_at='T',
            claim_state=_s.CLAIM_STATE_PUBLISHED,
        )
        for _seq, (_fn, _sha) in enumerate(_bins, start=1)
    ]
    _findings = _r.detect_hash_conflicts({'athena-primary': _claims})
    _crits = [_f for _f in _findings if _f.severity == 'CRITICAL']
    assert _crits == [], (
        f"multi-binary same-source must not raise hash conflicts; "
        f"got {[f.message for f in _crits]}")



def test_coord_reconcile_detect_hash_conflicts_same_filename_diff_sha_is_critical():
    """Direct sanity: same filename + different SHA-256 across two
    builders → CRITICAL (the case the detector actually exists for).
    """
    _s, _i, _st, _p, _h, _r, *_ = _coord_modules()
    _a = _s.new_claim(
        builder='alice', seq=1, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo_1.0_amd64.deb', sha256='a' * 64, size=1,
        snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _b = _s.new_claim(
        builder='bob', seq=1, package='foo',
        intended_version='1.0', built_version='1.0',
        filename='foo_1.0_amd64.deb', sha256='b' * 64, size=1,
        snapshot='S', built_at='T',
        claim_state=_s.CLAIM_STATE_PUBLISHED,
    )
    _findings = _r.detect_hash_conflicts({'alice': [_a], 'bob': [_b]})
    _crits = [_f for _f in _findings if _f.severity == 'CRITICAL']
    assert len(_crits) == 1, _crits
    assert 'hash conflict on foo_1.0_amd64.deb' in _crits[0].message, (
        _crits[0].message)



def test_coord_reconcile_publish_halt_round_trip():
    """write + read_publish_halt round-trips reason text."""
    _s, _i, _st, _p, _h, _r, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        assert _r.publish_halt_reason(_td) is None
        _r.write_publish_halt(_td, 'test conflict: alpha vs beta')
        _back = _r.publish_halt_reason(_td)
        assert _back == 'test conflict: alpha vs beta'



def test_publish_halt_reason_fails_closed_on_unreadable_sentinel():
    """Regression (audit #99, reconcile.py:542-550): an unreadable PUBLISH_HALT
    sentinel (present but open() raises a non-FileNotFoundError OSError) must
    fail CLOSED — return a non-None reason so the publish caller refuses — not
    return None ('no halt → publish allowed') and silently publish past an
    active halt.  Only a genuine absence (FileNotFoundError) maps to None."""
    _s, _i, _st, _p, _h, _r, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        assert _r.publish_halt_reason(_td) is None            # absent → allowed
        # Make the sentinel NAME a directory so open() raises IsADirectoryError
        # — an OSError that is NOT FileNotFoundError.
        os.makedirs(os.path.join(_td, _p.PUBLISH_HALT_FILENAME))
        _reason = _r.publish_halt_reason(_td)
        assert _reason is not None, (
            "unreadable PUBLISH_HALT must fail closed (non-None) so callers "
            "refuse — got None, which would allow publishing past an active "
            "halt")
        assert 'unreadable' in _reason.lower()



def test_coord_reconcile_audit_local_orphan_detection():
    """A claim for a file absent from the pool surfaces as a WARN
    orphan finding."""
    if not _openssl_available():
        return
    _s, _i, _st, _p, _h, _r, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _id_dir = os.path.join(_td, 'identity')
        _claims_dir = os.path.join(_td, 'claims')
        _pool = os.path.join(_td, 'repo')
        os.makedirs(_id_dir)
        os.makedirs(_pool)
        _priv, _pub = _i.generate_keypair(_id_dir, 'alice')
        # Claim for a file we never put in the pool
        _claim = _s.new_claim(
            builder='alice', seq=1, package='ghost',
            intended_version='1.0', built_version='1.0',
            filename='ghost.deb', sha256='a' * 64, size=1,
            snapshot='S', built_at='T',
            claim_state=_s.CLAIM_STATE_PUBLISHED,
        )
        _st.append_claim(_claims_dir, 'alice', _claim, _priv)
        _report = _r.audit_local(
            repo_root=_pool, claims_dir=_claims_dir,
            builder_id='alice', public_key_path=_pub,
            get_sha256=lambda _p: '',
        )
        assert _report.warn >= 1
        assert any(_f.kind == 'orphan' for _f in _report.findings)



def test_coord_reconcile_audit_local_hash_mismatch_critical():
    """Claim hash != on-disk hash → CRITICAL finding."""
    if not _openssl_available():
        return
    _s, _i, _st, _p, _h, _r, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _id_dir = os.path.join(_td, 'identity')
        _claims_dir = os.path.join(_td, 'claims')
        _pool = os.path.join(_td, 'repo/main')
        os.makedirs(_id_dir)
        os.makedirs(_pool)
        _priv, _pub = _i.generate_keypair(_id_dir, 'alice')
        # Plant a fake .deb on disk
        _path = os.path.join(_pool, 'x.deb')
        with open(_path, 'wb') as _fh:
            _fh.write(b'on-disk')
        _claim = _s.new_claim(
            builder='alice', seq=1, package='x',
            intended_version='1', built_version='1',
            filename='x.deb', sha256='z' * 64, size=7,
            snapshot='S', built_at='T',
            claim_state=_s.CLAIM_STATE_PUBLISHED,
        )
        _st.append_claim(_claims_dir, 'alice', _claim, _priv)
        # Stub get_sha256 to return a different hash than the claim
        _report = _r.audit_local(
            repo_root=os.path.join(_td, 'repo'),
            claims_dir=_claims_dir,
            builder_id='alice', public_key_path=_pub,
            get_sha256=lambda _p: 'a' * 64,  # not 'z'*64
        )
        assert _report.critical >= 1
        assert any(_f.kind == 'hash_mismatch' for _f in _report.findings)



def test_coord_publish_retract_and_re_audit_collapses():
    """After retracting a claim, the live projection no longer carries
    it; reconcile shows no orphan for that filename."""
    if not _openssl_available():
        return
    _s, _i, _st, _p, _h, _r, *_, _pub_mod = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _id_dir = os.path.join(_td, 'identity')
        _claims_dir = os.path.join(_td, 'claims')
        _coord = _td
        os.makedirs(_id_dir)
        _priv, _pub = _i.generate_keypair(_id_dir, 'alice')

        class _FakeConfig:
            dir_coord = _coord
            dir_coord_identity = _id_dir
            dir_coord_claims = _claims_dir
        _cfg = _FakeConfig()
        # Plant a claim
        _claim = _s.new_claim(
            builder='alice', seq=1, package='ghost',
            intended_version='1', built_version='1',
            filename='ghost.deb', sha256='a' * 64, size=1,
            snapshot='S', built_at='T',
            claim_state=_s.CLAIM_STATE_PUBLISHED,
        )
        _st.append_claim(_claims_dir, 'alice', _claim, _priv)
        # Retract it
        _ok, _detail = _pub_mod.retract_claim(
            builder_id='alice', config=_cfg,
            private_key_path=_priv, public_key_path=_pub,
            package='ghost',
        )
        assert _ok, _detail
        # Re-read; live projection is empty for ghost
        _claims = _st.read_builder_claims(_claims_dir, 'alice', _pub)
        _live = _st.project_live_claims({'alice': _claims})
        assert _live == {}, f"retracted claim must vanish; got {_live}"



def test_coord_publish_local_skips_when_halt_set():
    """local_publish refuses when PUBLISH_HALT sentinel exists."""
    _s, _i, _st, _p, _h, _r, *_, _pub_mod = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _r.write_publish_halt(_td, 'mocked conflict')

        class _FakeConfig:
            dir_coord = _td
            dir_log = _td  # buildlog walk will find nothing — irrelevant
            dir_repo = _td
            dir_coord_claims = os.path.join(_td, 'claims')

        _created, _skipped = _pub_mod.local_publish(
            builder_id='alice', config=_FakeConfig(),
            private_key_path='/dev/null', public_key_path='/dev/null',
            snapshot_pin='S', read_build_record=lambda *_a: None,
        )
        assert (_created, _skipped) == (0, 0)



def test_coord_publish_generate_pending_skips_already_claimed():
    """generate_pending_claims excludes outputs already in the local
    jsonl (so re-running publish is idempotent)."""
    if not _openssl_available():
        return
    _s, _i, _st, _p, _h, _r, *_, _pub_mod = _coord_modules()
    import utils as _u
    with tempfile.TemporaryDirectory() as _td:
        _id_dir = os.path.join(_td, 'identity')
        _claims_dir = os.path.join(_td, 'claims')
        _log = os.path.join(_td, 'log/build')
        os.makedirs(_id_dir)
        os.makedirs(_log)
        _priv, _pub = _i.generate_keypair(_id_dir, 'alice')
        # Plant a phase=done build.json with one output + its hash
        _rec = _u.new_build_record(
            package='libfoo', intended_version='1.0',
            patch_set_hash='', started='T',
        )
        _u.write_build_record(_log, _rec)
        _u.update_build_record(
            _log, 'libfoo', phase='done', built_version='1.0',
            finished='T', elapsed_seconds=1.0, exit_code=0,
            output_count=1, outputs=['libfoo_1.0.deb'],
            output_hashes={'libfoo_1.0.deb': 'a' * 64},
        )
        # First call: returns one pending claim
        _pending = _pub_mod.generate_pending_claims(
            builder_id='alice', buildlog_dir=_log,
            claims_dir=_claims_dir, public_key_path=_pub,
            snapshot_pin='S', read_build_record=_u.read_build_record,
        )
        assert len(_pending) == 1
        # Plant the corresponding claim
        _pending[0]['seq'] = 1
        _pending[0]['claim_state'] = _s.CLAIM_STATE_PUBLISHED
        _st.append_claim(_claims_dir, 'alice', _pending[0], _priv)
        # Second call: nothing pending
        _again = _pub_mod.generate_pending_claims(
            builder_id='alice', buildlog_dir=_log,
            claims_dir=_claims_dir, public_key_path=_pub,
            snapshot_pin='S', read_build_record=_u.read_build_record,
        )
        assert _again == []



def test_probe_sidecar_head_no_head_verified_and_fail():
    """Three outcomes: no coord-head on remote (bootstrap-pending,
    ok+None); head present + verified (ok+dict); head present + verify
    fails (not-ok)."""
    _m = _mirror_module()
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.head as _head_mod
    import coord.transport as _transport
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        _stage = os.path.join(_td, 'stage')
        # Case 1: pull succeeds, no coord-head.json materialised
        with patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=None):
            _ok, _det, _head = _m.probe_sidecar_head(
                'ssh://ubuntu@host/srv/asgard-coord',
                signing_homedir='/fake/gpg', stage_dir=_stage)
            assert _ok and _head is None
            assert 'no head yet' in _det

        # Case 2: head.json present + verify succeeds → dict returned
        _stage2 = os.path.join(_td, 'stage2')
        os.makedirs(_stage2, exist_ok=True)
        with open(os.path.join(_stage2, 'coord-head.json'), 'w') as _fh:
            _fh.write('{"v":2,"neighbours":["ssh://x/y"]}')
        with patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value={'v': 2,
                                        'neighbours': ['ssh://x/y']}):
            _ok, _det, _head = _m.probe_sidecar_head(
                'ssh://ubuntu@host/srv/asgard-coord',
                signing_homedir='/fake/gpg', stage_dir=_stage2)
            assert _ok and _head is not None
            assert _head['neighbours'] == ['ssh://x/y']

        # Case 3: head.json present but verify FAILS → not ok
        _stage3 = os.path.join(_td, 'stage3')
        os.makedirs(_stage3, exist_ok=True)
        with open(os.path.join(_stage3, 'coord-head.json'), 'w') as _fh:
            _fh.write('{"v":2}')
        with patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=None):
            _ok, _det, _head = _m.probe_sidecar_head(
                'ssh://ubuntu@host/srv/asgard-coord',
                signing_homedir='/fake/gpg', stage_dir=_stage3)
            assert not _ok and _head is None
            assert 'GPG verify FAILED' in _det



def test_neighbour_urls_lowercases_strips_slash_sorts_dedups():
    """Canonical form: lowercase + trailing-slash-strip + sort + dedup.
    Used by both sides of the federation gate so trivial differences
    (case, slash, order) don't cause false BLOCK signals."""
    _s, _ = _coord_schema_v2_modules()
    _got = _s.neighbour_urls([
        'SSH://USER@HOST/path/',
        'file:///srv/asgard',
        'ssh://user@host/path',       # dup after lowercase + slash strip
        'file:///srv/asgard/',         # dup after slash strip
        '',                            # dropped
        42,                            # not a str — dropped
    ])
    assert _got == [
        'file:///srv/asgard',
        'ssh://user@host/path',
    ], _got



def test_canonicalize_neighbour_records_promotes_v2_and_passes_v3():
    """v3 helper: v2 strings auto-promoted to records with empty meta;
    v3 dicts pass-through with their public_url/public_proto preserved.
    Mixed input handled — dict overrides str if the same URL appears
    in both shapes."""
    _s, _ = _coord_schema_v2_modules()
    _got = _s.canonicalize_neighbour_records([
        'ssh://a/p',                                          # v2 string
        {'url': 'SSH://B/p',                                  # v3 dict
         'public_url': 'https://b/asgard',
         'public_proto': 'https'},
        {'url': 'ssh://b/p/'},                                # dup of B; first wins
        {'url': '', 'public_url': 'x'},                       # empty url dropped
        42,                                                   # non-str/dict dropped
        None,                                                 # ditto
    ])
    assert _got == [
        {'url': 'ssh://a/p', 'public_url': '', 'public_proto': ''},
        {'url': 'ssh://b/p',
         'public_url': 'https://b/asgard', 'public_proto': 'https'},
    ], _got



def test_neighbour_urls_projects_to_flat_str_list():
    """URL projection of canonical records.  Federation-gate uses this
    for set comparison (dicts aren't hashable, so the gate works on the
    flat str projection)."""
    _s, _ = _coord_schema_v2_modules()
    _got = _s.neighbour_urls([
        {'url': 'ssh://b/p', 'public_url': 'https://b/asgard',
         'public_proto': 'https'},
        'ssh://A/p',                                          # v2 string
    ])
    assert _got == ['ssh://a/p', 'ssh://b/p']



def test_new_coord_head_includes_neighbours_field():
    """new_coord_head always produces a `neighbours` key (empty list when
    not passed), in canonicalised v3 record form.  Accepts both v2
    list-of-strings input (auto-promoted, empty public_url/proto) and
    v3 list-of-dicts input."""
    _s, _ = _coord_schema_v2_modules()
    _head = _s.new_coord_head(
        inrelease_sha256='a' * 64,
        snapshot={}, last_seqs={}, head_time='T',
        neighbours=['SSH://A/x/', 'file:///srv'],
    )
    assert _head['v'] == 3
    assert 'neighbours' in _head
    # v3 records — sorted by url; v2 strings auto-promoted to records.
    assert _head['neighbours'] == [
        {'url': 'file:///srv',  'public_url': '', 'public_proto': ''},
        {'url': 'ssh://a/x',    'public_url': '', 'public_proto': ''},
    ]
    # v3 dict input — public_url/proto preserved
    _h_v3 = _s.new_coord_head(
        inrelease_sha256='a' * 64,
        snapshot={}, last_seqs={}, head_time='T',
        neighbours=[
            {'url': 'ssh://b/y',
             'public_url': 'https://b/asgard', 'public_proto': 'https'},
        ],
    )
    assert _h_v3['neighbours'] == [{
        'url': 'ssh://b/y',
        'public_url': 'https://b/asgard',
        'public_proto': 'https',
    }]
    # Omitted → []
    _h2 = _s.new_coord_head(
        inrelease_sha256='a' * 64,
        snapshot={}, last_seqs={}, head_time='T',
    )
    assert _h2['neighbours'] == []



def test_new_closure_ledger_shape():
    """new_closure_ledger produces a v1 doc carrying codename/snapshot/
    generated_at + the entries dict verbatim."""
    _s, _ = _coord_schema_v2_modules()
    _entries = {
        'libfoo|amd64': {
            'filename': 'libfoo_1.2_amd64.deb', 'sha256': 'a' * 64,
            'size': 100, 'component': 'main', 'version': '1.2',
            'package': 'libfoo', 'arch': 'amd64',
        },
    }
    _doc = _s.new_closure_ledger(
        codename='thor', snapshot='20260101T000000Z',
        entries=_entries, generated_at='2026-01-01T00:00:00Z')
    assert _doc['v'] == _s.CLOSURE_LEDGER_SCHEMA_VERSION == 1
    assert _doc['codename'] == 'thor'
    assert _doc['snapshot'] == '20260101T000000Z'
    assert _doc['generated_at'] == '2026-01-01T00:00:00Z'
    assert _doc['entries'] == _entries
    # top-level entries dict is copied (caller can't add/remove keys after)
    _entries['libbar|amd64'] = {}
    assert 'libbar|amd64' not in _doc['entries']



def test_new_coord_head_closure_ledger_pin_optional():
    """closure_ledger_sha256 is an additive optional field — present when
    passed, absent otherwise, and the head v stays at 3 (no bump)."""
    _s, _ = _coord_schema_v2_modules()
    _h = _s.new_coord_head(
        inrelease_sha256='a' * 64, snapshot={}, last_seqs={}, head_time='T',
        closure_ledger_sha256='b' * 64)
    assert _h['v'] == 3
    assert _h['closure_ledger_sha256'] == 'b' * 64
    # Omitted → absent (old peer reads None → fallback)
    _h2 = _s.new_coord_head(
        inrelease_sha256='a' * 64, snapshot={}, last_seqs={}, head_time='T')
    assert 'closure_ledger_sha256' not in _h2
    # Falsy → absent
    _h3 = _s.new_coord_head(
        inrelease_sha256='a' * 64, snapshot={}, last_seqs={}, head_time='T',
        closure_ledger_sha256='')
    assert 'closure_ledger_sha256' not in _h3



def test_check_federation_consistency_match_returns_no_findings():
    """Local URL set == coord-head.neighbours (after canonicalisation)
    → empty findings."""
    _s, _r = _coord_schema_v2_modules()
    _head = _s.new_coord_head(
        inrelease_sha256='a' * 64,
        snapshot={}, last_seqs={}, head_time='T',
        neighbours=['ssh://h/p', 'file:///srv/a'],
    )
    _findings = _r.check_federation_consistency(
        ['ssh://H/p/', 'file:///srv/a'], _head)
    assert _findings == [], _findings



def test_check_federation_consistency_missing_on_remote_is_critical():
    """Local has 2, coord-head has 1 → CRITICAL missing_on_remote."""
    _s, _r = _coord_schema_v2_modules()
    _head = _s.new_coord_head(
        inrelease_sha256='a' * 64,
        snapshot={}, last_seqs={}, head_time='T',
        neighbours=['ssh://h1/p'],
    )
    _f = _r.check_federation_consistency(
        ['ssh://h1/p', 'ssh://h2/p'], _head)
    assert len(_f) == 1
    assert _f[0].severity == 'CRITICAL'
    assert _f[0].kind == 'federation_missing_neighbour'
    assert 'ssh://h2/p' in _f[0].message



def test_check_federation_consistency_extra_on_remote_is_critical():
    """Coord-head has 2, local has 1 → CRITICAL extra_on_remote."""
    _s, _r = _coord_schema_v2_modules()
    _head = _s.new_coord_head(
        inrelease_sha256='a' * 64,
        snapshot={}, last_seqs={}, head_time='T',
        neighbours=['ssh://h1/p', 'ssh://h2/p'],
    )
    _f = _r.check_federation_consistency(['ssh://h1/p'], _head)
    assert len(_f) == 1
    assert _f[0].severity == 'CRITICAL'
    assert _f[0].kind == 'federation_extra_neighbour'
    assert 'ssh://h2/p' in _f[0].message



def test_check_federation_consistency_both_directions_emits_two_findings():
    """Bidirectional drift → both missing and extra findings."""
    _s, _r = _coord_schema_v2_modules()
    _head = _s.new_coord_head(
        inrelease_sha256='a' * 64,
        snapshot={}, last_seqs={}, head_time='T',
        neighbours=['ssh://h1/p', 'ssh://h2/p'],
    )
    _f = _r.check_federation_consistency(
        ['ssh://h1/p', 'ssh://h3/p'], _head)
    _kinds = sorted(_x.kind for _x in _f)
    assert _kinds == ['federation_extra_neighbour',
                      'federation_missing_neighbour'], _kinds



def test_check_federation_consistency_none_head_returns_no_findings():
    """No coord-head yet (first publish) → empty findings; publish path
    initialises neighbours from local config."""
    _s, _r = _coord_schema_v2_modules()
    _f = _r.check_federation_consistency(['ssh://a', 'ssh://b'], None)
    assert _f == []



def test_reconcile_neighbours_no_op_when_already_in_sync():
    """When the fetched coord-head's neighbours already match local config,
    reconcile reports `already in sync` and pushes nothing."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
            dir_cache = os.path.join(_td, 'cache')
        os.makedirs(_Cfg.dir_cache)
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='alpha', url='file:///srv/a', seed_pin='')
        _m.add_mirror(_cfg, name='beta',  url='file:///srv/b', seed_pin='')

        # Mock the transport + head modules — reconcile uses lazy imports.
        import sys
        sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
        import coord.transport as _t
        import coord.head as _h
        from unittest.mock import patch

        _desired = ['file:///srv/a', 'file:///srv/b']
        # Each peer's fetched head already has the right neighbours
        with patch.object(_t, 'pull_remote_coord', return_value=(True, '')), \
             patch.object(_h, 'read_coord_head',
                          return_value={'v': 2, 'neighbours': _desired}), \
             patch.object(_h, 'write_coord_head') as _w, \
             patch.object(_t, 'push_coord_head') as _p:
            _ok, _summary, _results = _m.reconcile_neighbours(
                _cfg, signing_homedir='/fake/gpg')
        assert _ok, _results
        assert _w.call_count == 0
        assert _p.call_count == 0
        assert all(_r['detail'] == 'already in sync' for _r in _results)
        assert all(not _r['changed'] for _r in _results)



def test_reconcile_neighbours_rewrites_when_diff():
    """When the fetched head's neighbours differ from local config, reconcile
    updates the head in-place, re-signs locally, pushes back."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
            dir_cache = os.path.join(_td, 'cache')
        os.makedirs(_Cfg.dir_cache)
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='alpha', url='file:///srv/a', seed_pin='')
        _m.add_mirror(_cfg, name='beta',  url='file:///srv/b', seed_pin='')

        import sys
        sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
        import coord.transport as _t
        import coord.head as _h
        from unittest.mock import patch

        # Each peer's fetched head has STALE neighbours (just one URL)
        _stale = {'v': 2, 'neighbours': ['file:///srv/a']}
        with patch.object(_t, 'pull_remote_coord', return_value=(True, '')), \
             patch.object(_h, 'read_coord_head', return_value=_stale), \
             patch.object(_h, 'write_coord_head', return_value=True) as _w, \
             patch.object(_t, 'push_coord_head', return_value=(True, '')) as _p:
            _ok, _summary, _results = _m.reconcile_neighbours(
                _cfg, signing_homedir='/fake/gpg')
        assert _ok, _results
        # Two peers, each got a rewrite + push
        assert _w.call_count == 2
        assert _p.call_count == 2
        assert all(_r['changed'] for _r in _results)
        # The head passed to write_coord_head must carry the desired
        # neighbours as v3 records, sorted by url.  File:// mirrors keep
        # empty public_url / public_proto (no apt-readable URL).
        for _call in _w.call_args_list:
            _passed_head = _call.args[1]
            assert _passed_head['neighbours'] == [
                {'url': 'file:///srv/a',
                 'public_url': '', 'public_proto': ''},
                {'url': 'file:///srv/b',
                 'public_url': '', 'public_proto': ''},
            ]



def test_reconcile_neighbours_first_contact_skipped_friendly():
    """A peer with no coord-head yet (e.g., never published to) returns
    `no coord-head on remote (first publish will initialise neighbours)`;
    no write, no push, overall ok."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
            dir_cache = os.path.join(_td, 'cache')
        os.makedirs(_Cfg.dir_cache)
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='alpha', url='file:///srv/a', seed_pin='')

        import sys
        sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
        import coord.transport as _t
        import coord.head as _h
        from unittest.mock import patch

        # Pull "succeeds" but no head present → read_coord_head returns None
        with patch.object(_t, 'pull_remote_coord', return_value=(True, '')), \
             patch.object(_h, 'read_coord_head', return_value=None), \
             patch.object(_h, 'write_coord_head') as _w, \
             patch.object(_t, 'push_coord_head') as _p:
            _ok, _summary, _results = _m.reconcile_neighbours(
                _cfg, signing_homedir='/fake/gpg')
        assert _ok
        assert _w.call_count == 0
        assert _p.call_count == 0
        assert _results[0]['detail'].startswith('no coord-head on remote')



def test_reconcile_neighbours_unreachable_peer_is_critical_failure():
    """pull_remote_coord failure → reachable=False, ok=False, overall ok=False."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
            dir_cache = os.path.join(_td, 'cache')
        os.makedirs(_Cfg.dir_cache)
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='alpha', url='file:///srv/a', seed_pin='')

        import sys
        sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
        import coord.transport as _t
        from unittest.mock import patch

        with patch.object(_t, 'pull_remote_coord',
                          return_value=(False, 'connection refused')):
            _ok, _summary, _results = _m.reconcile_neighbours(
                _cfg, signing_homedir='/fake/gpg')
        assert not _ok
        assert not _results[0]['ok']
        assert not _results[0]['reachable']
        assert 'connection refused' in _results[0]['detail']



def test_remote_publish_blocks_on_federation_drift():
    """When `local_mirror_urls` differs from the fetched head's neighbours,
    remote_publish refuses to proceed (federation gate)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    import coord.transport as _transport
    import coord.head as _head_mod
    import coord.store as _store
    import coord.identity as _identity
    import coord.reconcile as _reconcile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_coord = _td
            dir_coord_fetched = os.path.join(_td, 'fetched')
            dir_coord_claims = os.path.join(_td, 'claims')
            dir_log = _td
            dir_repo = _td
            dir_gnupg = _td  # signing.signing_home() walks this
        os.makedirs(_Cfg.dir_coord_fetched)
        os.makedirs(_Cfg.dir_coord_claims)

        # Mock the network/sign primitives.  The fetched head has only
        # one neighbour but local config has two → federation mismatch.
        _existing_head = {
            'v': 2,
            'inrelease_sha256': 'a' * 64,
            'snapshot': {}, 'last_seqs': {}, 'head_time': 'T',
            'neighbours': ['ssh://a/p'],
        }
        _lock = object()  # any non-None sentinel
        with patch.object(_transport, 'remote_flock_acquire',
                          return_value=_lock), \
             patch.object(_transport, 'remote_flock_release'), \
             patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=_existing_head), \
             patch.object(_identity, 'load_keyring', return_value={}), \
             patch.object(_store, 'read_all_claims', return_value={}), \
             patch.object(_reconcile, 'publish_halt_reason',
                          return_value=None):
            _ok, _detail = _publish.remote_publish(
                builder_id='alice', config=_Cfg(),
                private_key_path='/fake/priv', public_key_path='/fake/pub',
                snapshot_pin='20260601T000000Z',
                remote_coord_spec='user@h:/asgard',
                inrelease_local_path='/fake/InRelease',
                read_build_record=lambda *_: None,
                local_mirror_urls=['ssh://a/p', 'ssh://b/p'],  # 2; remote has 1
                ssh_host='user@h',
            )
        assert not _ok
        assert 'federation gate BLOCK' in _detail
        assert 'reconcile-neighbours' in _detail



def test_remote_publish_bootstrap_uploads_pubkey_when_no_head():
    """When the remote has no coord-head (first publish), remote_publish
    uploads our pubkey to keyring/builders/<id>.pub before writing claims,
    and initialises the new head's neighbours from local_mirror_urls."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    import coord.transport as _transport
    import coord.head as _head_mod
    import coord.store as _store
    import coord.identity as _identity
    import coord.reconcile as _reconcile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        # Build a complete fake config dir
        class _Cfg:
            dir_coord = _td
            dir_coord_fetched = os.path.join(_td, 'fetched')
            dir_coord_claims = os.path.join(_td, 'claims')
            dir_log = _td
            dir_repo = _td
            dir_config = _td
            dir_gnupg = _td
        os.makedirs(_Cfg.dir_coord_fetched)
        os.makedirs(_Cfg.dir_coord_claims)
        # Fake InRelease with a sha
        _inrelease = os.path.join(_td, 'InRelease')
        with open(_inrelease, 'wb') as _fh:
            _fh.write(b'Date: 2026-06-01\nFake: contents\n')

        # Capture push_jsonl calls + the head dict passed to write_coord_head
        _pushed: 'list[dict]' = []
        def _fake_push(*, local_path, remote_spec, ssh_key=None, overwrite=False,
                       on_bytes=None):
            _pushed.append({'local': local_path, 'remote': remote_spec})
            return True, ''
        _written_head: 'list[dict]' = []
        def _fake_write_head(coord_dir, head, signing_home):
            _written_head.append(head)
            return True
        _lock = object()
        with patch.object(_transport, 'remote_flock_acquire',
                          return_value=_lock), \
             patch.object(_transport, 'remote_flock_release'), \
             patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=None), \
             patch.object(_identity, 'load_keyring', return_value={}), \
             patch.object(_store, 'read_all_claims', return_value={}), \
             patch.object(_store, 'max_seq', return_value=0), \
             patch.object(_reconcile, 'publish_halt_reason',
                          return_value=None), \
             patch.object(_transport, 'push_jsonl', side_effect=_fake_push), \
             patch.object(_head_mod, 'write_coord_head',
                          side_effect=_fake_write_head), \
             patch.object(_transport, 'push_coord_head',
                          return_value=(True, '')), \
             patch.object(_publish, 'generate_pending_claims',
                          return_value=[]):
            _ok, _detail = _publish.remote_publish(
                builder_id='alice', config=_Cfg(),
                private_key_path='/fake/priv', public_key_path='/fake/pub',
                snapshot_pin='20260601T000000Z',
                remote_coord_spec='user@h:/asgard',
                inrelease_local_path=_inrelease,
                read_build_record=lambda *_: None,
                local_mirror_urls=['ssh://a/p', 'ssh://b/p'],
                ssh_host='user@h',
            )
        assert _ok, _detail
        # Pubkey upload must hit keyring/builders/alice.pub
        _pubkey_pushes = [_p for _p in _pushed
                          if _p['remote'].endswith('/keyring/builders/alice.pub')]
        assert len(_pubkey_pushes) == 1, _pushed
        # The new head must carry the bootstrap neighbours (canonicalised
        # to v3 records — empty public_url/proto since the publish call
        # passed a flat list[str] of local_mirror_urls).
        assert _written_head, "write_coord_head never called"
        _new_head = _written_head[-1]
        assert _new_head['neighbours'] == [
            {'url': 'ssh://a/p', 'public_url': '', 'public_proto': ''},
            {'url': 'ssh://b/p', 'public_url': '', 'public_proto': ''},
        ]



def test_remote_publish_bootstrap_carries_v3_per_peer_records():
    """When the caller passes v3 records (each peer's apt-readable URL
    + protocol), bootstrap writes them VERBATIM into the new
    coord-head's `neighbours` so a heterogeneous federation
    round-trips per-peer apt URLs instead of every joiner inheriting
    the operator's --proto."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    import coord.transport as _transport
    import coord.head as _head_mod
    import coord.store as _store
    import coord.identity as _identity
    import coord.reconcile as _reconcile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_coord = _td
            dir_coord_fetched = os.path.join(_td, 'fetched')
            dir_coord_claims = os.path.join(_td, 'claims')
            dir_log = _td
            dir_repo = _td
            dir_config = _td
            dir_gnupg = _td
        os.makedirs(_Cfg.dir_coord_fetched)
        os.makedirs(_Cfg.dir_coord_claims)
        _inrelease = os.path.join(_td, 'InRelease')
        with open(_inrelease, 'wb') as _fh:
            _fh.write(b'Date: 2026-06-01\nFake: contents\n')

        _local_records = [
            {'url': 'ssh://a/p',
             'public_url': 'http://a/asgard',  'public_proto': 'http'},
            {'url': 'ssh://b/p',
             'public_url': 'https://b/asgard', 'public_proto': 'https'},
        ]
        _written_head: 'list[dict]' = []
        def _fake_write_head(coord_dir, head, signing_home):
            _written_head.append(head)
            return True
        _lock = object()
        with patch.object(_transport, 'remote_flock_acquire',
                          return_value=_lock), \
             patch.object(_transport, 'remote_flock_release'), \
             patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=None), \
             patch.object(_identity, 'load_keyring', return_value={}), \
             patch.object(_store, 'read_all_claims', return_value={}), \
             patch.object(_store, 'max_seq', return_value=0), \
             patch.object(_reconcile, 'publish_halt_reason',
                          return_value=None), \
             patch.object(_transport, 'push_jsonl',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'write_coord_head',
                          side_effect=_fake_write_head), \
             patch.object(_transport, 'push_coord_head',
                          return_value=(True, '')), \
             patch.object(_publish, 'generate_pending_claims',
                          return_value=[]):
            _ok, _detail = _publish.remote_publish(
                builder_id='alice', config=_Cfg(),
                private_key_path='/fake/priv', public_key_path='/fake/pub',
                snapshot_pin='20260601T000000Z',
                remote_coord_spec='user@h:/asgard',
                inrelease_local_path=_inrelease,
                read_build_record=lambda *_: None,
                local_mirror_urls=_local_records,
                ssh_host='user@h',
            )
        assert _ok, _detail
        assert _written_head, "write_coord_head never called"
        _new_head = _written_head[-1]
        # Per-peer apt URLs round-trip into the signed federation head.
        assert _new_head['neighbours'] == [
            {'url': 'ssh://a/p',
             'public_url': 'http://a/asgard',  'public_proto': 'http'},
            {'url': 'ssh://b/p',
             'public_url': 'https://b/asgard', 'public_proto': 'https'},
        ]




def test_transport_pull_single_file_primitive_exists():
    """coord.transport.pull_single_file is wired (used by mirror pull)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.transport as _t
    assert hasattr(_t, 'pull_single_file')
    # Negative path: rsync invocation against a non-existent source fails
    # cleanly without raising.
    with tempfile.TemporaryDirectory() as _td:
        _ok, _detail = _t.pull_single_file(
            remote_spec=os.path.join(_td, 'nonexistent'),
            local_path=os.path.join(_td, 'dest', 'file.deb'),
        )
        assert not _ok
        assert _detail  # rsync stderr tail surfaced



def test_transport_push_single_deb_primitive_exists():
    """coord.transport.push_single_deb is wired (used by remote_publish's
    per-file push)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.transport as _t
    assert hasattr(_t, 'push_single_deb')
    # Missing local file fails cleanly without raising.
    with tempfile.TemporaryDirectory() as _td:
        _ok, _detail = _t.push_single_deb(
            local_path=os.path.join(_td, 'nope.deb'),
            remote_spec=os.path.join(_td, 'dest', 'nope.deb'),
        )
        assert not _ok
        assert 'missing' in _detail



def test_transport_push_single_deb_roundtrip_with_local_fs():
    """push_single_deb to a local-fs destination succeeds; the file lands
    at the predicted path with matching contents."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.transport as _t
    with tempfile.TemporaryDirectory() as _td:
        _src = os.path.join(_td, 'source.deb')
        _dst = os.path.join(_td, 'dest_dir', 'pushed.deb')
        with open(_src, 'wb') as _fh:
            _fh.write(b'fake-deb-bytes')
        os.makedirs(os.path.dirname(_dst), exist_ok=True)
        _ok, _detail = _t.push_single_deb(
            local_path=_src, remote_spec=_dst)
        assert _ok, _detail
        with open(_dst, 'rb') as _fh:
            assert _fh.read() == b'fake-deb-bytes'



def test_transport_push_single_deb_on_bytes_streams_progress():
    """push_single_deb(on_bytes=...) streams rsync --info=progress2 and
    reports the cumulative transferred-byte count — the byte-granular
    progress the ISO push drives its MB ProgressBar from.  The file still
    lands intact and the final reported count reaches the file size."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.transport as _t
    with tempfile.TemporaryDirectory() as _td:
        _src = os.path.join(_td, 'big.iso')
        _dst = os.path.join(_td, 'dest_dir', 'big.iso')
        os.makedirs(os.path.dirname(_dst), exist_ok=True)
        # multi-MB so progress2 emits a measurable transfer
        _size = 6 * 1024 * 1024
        with open(_src, 'wb') as _fh:
            _fh.write(b'\x5a' * _size)
        _seen: 'list[int]' = []
        _ok, _detail = _t.push_single_deb(
            local_path=_src, remote_spec=_dst,
            on_bytes=lambda _n: _seen.append(_n))
        assert _ok, _detail
        with open(_dst, 'rb') as _fh:
            assert len(_fh.read()) == _size
        # at least one progress callback, monotonic, reaching the size
        assert _seen, "on_bytes never fired"
        assert _seen == sorted(_seen), _seen
        assert max(_seen) >= _size * 0.9, (max(_seen), _size)



def test_transport_push_dist_tree_roundtrip_with_local_fs():
    """push_dist_tree rsyncs the WHOLE dists/<codename>/ subtree to a
    local-fs destination.  Verifies InRelease + Packages + by-hash
    arrive intact and stale files on the remote get pruned (--delete)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.transport as _t
    with tempfile.TemporaryDirectory() as _td:
        _src = os.path.join(_td, 'src_dist')
        _dst = os.path.join(_td, 'dst_dist')
        os.makedirs(os.path.join(_src, 'main', 'binary-amd64'))
        with open(os.path.join(_src, 'InRelease'), 'w') as _fh:
            _fh.write('clearsigned-fake')
        with open(os.path.join(_src, 'Release'), 'w') as _fh:
            _fh.write('Suite: test\n')
        with open(os.path.join(_src, 'main', 'binary-amd64',
                               'Packages'), 'w') as _fh:
            _fh.write('Package: foo\nVersion: 1.0\n')
        # Pre-existing stale file on dst that --delete should remove.
        os.makedirs(_dst)
        with open(os.path.join(_dst, 'stale'), 'w') as _fh:
            _fh.write('old')
        _ok, _detail = _t.push_dist_tree(
            local_dist_dir=_src, remote_dir_spec=_dst)
        assert _ok, _detail
        with open(os.path.join(_dst, 'InRelease')) as _fh:
            assert _fh.read() == 'clearsigned-fake'
        with open(os.path.join(_dst, 'main', 'binary-amd64',
                               'Packages')) as _fh:
            assert _fh.read().startswith('Package: foo')
        # --delete pruned the stale file.
        assert not os.path.isfile(os.path.join(_dst, 'stale'))



def test_transport_push_dist_tree_missing_local_dir_fails_clean():
    """Missing local dist dir → returns (False, helpful detail)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.transport as _t
    with tempfile.TemporaryDirectory() as _td:
        _ok, _detail = _t.push_dist_tree(
            local_dist_dir=os.path.join(_td, 'nope'),
            remote_dir_spec=os.path.join(_td, 'dst'))
        assert not _ok
        assert 'does not exist' in _detail



def test_remote_publish_pushes_debs_per_file_and_calls_progress():
    """When `pool_remote_spec` is given, remote_publish pushes each
    pending claim's .deb via transport.push_single_deb and invokes
    on_progress per attempt."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    import coord.transport as _transport
    import coord.head as _head_mod
    import coord.store as _store
    import coord.identity as _identity
    import coord.reconcile as _reconcile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_coord = _td
            dir_coord_fetched = os.path.join(_td, 'fetched')
            dir_coord_claims = os.path.join(_td, 'claims')
            dir_log = _td
            dir_repo = os.path.join(_td, 'repo')
            dir_gnupg = _td
        for _d in (_Cfg.dir_coord_fetched, _Cfg.dir_coord_claims,
                   os.path.join(_Cfg.dir_repo, 'dists/thor/main/binary-amd64')):
            os.makedirs(_d)
        # Plant 3 fake .debs in the local pool
        _pool_files = {}
        for _name in ('foo_1.0_amd64.deb', 'bar_2.0_amd64.deb',
                      'baz_3.0_amd64.deb'):
            _path = os.path.join(_Cfg.dir_repo,
                                 'dists/thor/main/binary-amd64', _name)
            with open(_path, 'wb') as _fh:
                _fh.write(_name.encode())
            _pool_files[_name] = _path
        # InRelease stub
        _inrelease = os.path.join(_td, 'InRelease')
        with open(_inrelease, 'wb') as _fh:
            _fh.write(b'Date: 2026-06-01\nFake: contents\n')

        _existing_head = {
            'v': 2,
            'inrelease_sha256': 'a' * 64,
            'snapshot': {}, 'last_seqs': {}, 'head_time': 'T',
            'neighbours': ['file:///srv/m1'],
        }
        # Pending claims for all three files
        _pending = [
            {'builder': 'alice', 'package': _n.split('_')[0],
             'intended_version': '1', 'built_version': '1',
             'filename': _n, 'sha256': 'x' * 64, 'size': 0,
             'snapshot': 'S', 'built_at': 'T',
             'claim_state': 'pending', 'seq': 0, 'v': 1}
            for _n in _pool_files.keys()
        ]
        # Track push calls + progress callback observations
        _pushed: 'list[str]' = []
        _progress_events: 'list[tuple]' = []
        def _fake_push(*, local_path, remote_spec, ssh_key=None, overwrite=False,
                       on_bytes=None):
            _pushed.append(remote_spec)
            return True, ''
        def _fake_write_head(coord_dir, head, signing_home):
            return True
        def _capture_progress(current, total, fn, ok):
            _progress_events.append((current, total, fn, ok))
        _lock = object()
        with patch.object(_transport, 'remote_flock_acquire',
                          return_value=_lock), \
             patch.object(_transport, 'remote_flock_release'), \
             patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=_existing_head), \
             patch.object(_identity, 'load_keyring', return_value={}), \
             patch.object(_store, 'read_all_claims', return_value={}), \
             patch.object(_store, 'max_seq', return_value=0), \
             patch.object(_store, 'append_claim'), \
             patch.object(_reconcile, 'publish_halt_reason',
                          return_value=None), \
             patch.object(_transport, 'push_single_deb',
                          side_effect=_fake_push), \
             patch.object(_transport, 'push_jsonl',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'write_coord_head',
                          side_effect=_fake_write_head), \
             patch.object(_transport, 'push_coord_head',
                          return_value=(True, '')), \
             patch.object(_publish, 'generate_pending_claims',
                          return_value=_pending):
            _ok, _detail = _publish.remote_publish(
                builder_id='alice', config=_Cfg(),
                private_key_path='/fake/priv', public_key_path='/fake/pub',
                snapshot_pin='20260601T000000Z',
                remote_coord_spec='file:///srv/m1-coord',
                pool_remote_spec='file:///srv/m1',
                inrelease_local_path=_inrelease,
                read_build_record=lambda *_: None,
                local_mirror_urls=['file:///srv/m1'],
                ssh_host=None,  # local-fs needs no host
                on_progress=_capture_progress,
            )
        assert _ok, _detail
        # Three .debs pushed; their remote_spec paths preserve the
        # `dists/thor/main/binary-amd64/<filename>` relative shape.
        assert len(_pushed) == 3, _pushed
        for _name in _pool_files:
            assert any(_name in _p for _p in _pushed), (_name, _pushed)
        # Three progress events, all ok=True; total==3 for all
        assert len(_progress_events) == 3
        for _ev in _progress_events:
            assert _ev[1] == 3  # total
            assert _ev[3] is True  # ok
        # Success detail surfaces the push count
        assert 'pushed 3 .deb' in _detail



def test_remote_publish_refuses_on_closure_break():
    """STA-27: the publish-time installability gate must REFUSE a publish
    whose pending set would leave the mirror with an unresolved hard
    Depends.  Regression for the indentation bug where the `if _breaks:`
    refusal lived inside the no-pending `else:` branch (where _breaks is
    always []), so the `elif _pending:` branch computed the breaks and
    silently discarded them — a broken closure published clean.  Drives
    the real `remote_publish` end-to-end (a unit test of
    find_publish_closure_breaks would NOT have caught the dead wiring —
    which is exactly how it shipped)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    import coord.transport as _transport
    import coord.head as _head_mod
    import coord.store as _store
    import coord.identity as _identity
    import coord.reconcile as _reconcile
    import mirror as _mirror_mod
    import repo_audit as _repo_audit
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_coord = _td
            dir_coord_fetched = os.path.join(_td, 'fetched')
            dir_coord_claims = os.path.join(_td, 'claims')
            dir_log = _td
            dir_repo = _td
            dir_gnupg = _td
        os.makedirs(_Cfg.dir_coord_fetched)
        os.makedirs(_Cfg.dir_coord_claims)
        _inrelease = os.path.join(_td, 'InRelease')
        with open(_inrelease, 'wb') as _fh:
            _fh.write(b'Date: 2026-06-01\nFake: contents\n')

        _existing_head = {
            'v': 2, 'inrelease_sha256': 'a' * 64,
            'snapshot': {}, 'last_seqs': {}, 'head_time': 'T',
            'neighbours': ['file:///srv/m1'],
        }
        # One pending claim — enough to reach the `elif _pending:` branch.
        _pending = [{
            'builder': 'alice', 'package': 'foo', 'intended_version': '1',
            'built_version': '1', 'filename': 'foo_1_amd64.deb',
            'sha256': 'x' * 64, 'size': 0, 'snapshot': 'S', 'built_at': 'T',
            'claim_state': 'pending', 'seq': 0, 'v': 1,
        }]
        # The closure check reports a break (foo Depends an unsatisfiable
        # name).  With the bug this is computed then discarded; with the
        # fix remote_publish must return False before any push.
        _pushed: 'list[str]' = []
        _lock = object()
        with patch.object(_transport, 'remote_flock_acquire',
                          return_value=_lock), \
             patch.object(_transport, 'remote_flock_release'), \
             patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=_existing_head), \
             patch.object(_identity, 'load_keyring', return_value={}), \
             patch.object(_store, 'read_all_claims', return_value={}), \
             patch.object(_store, 'max_seq', return_value=0), \
             patch.object(_store, 'append_claim'), \
             patch.object(_reconcile, 'publish_halt_reason',
                          return_value=None), \
             patch.object(_publish, 'generate_pending_claims',
                          return_value=_pending), \
             patch.object(_repo_audit, 'scan_repo_state', return_value=None), \
             patch.object(_mirror_mod, 'find_publish_closure_breaks',
                          return_value=[('foo', 'Depends', 'libmissing',
                                         'not satisfiable on mirror')]), \
             patch.object(_transport, 'push_single_deb',
                          side_effect=lambda **kw: _pushed.append(1) or (True, '')), \
             patch.object(_transport, 'push_jsonl', return_value=(True, '')), \
             patch.object(_head_mod, 'write_coord_head', return_value=True), \
             patch.object(_transport, 'push_coord_head',
                          return_value=(True, '')):
            # write_coord_head mocked too: with the bug the publish would
            # sail PAST the gate to a clean success — so `_ok is False`
            # below is caused by the gate alone, not a downstream stub gap.
            _ok, _detail = _publish.remote_publish(
                builder_id='alice', config=_Cfg(),
                private_key_path='/fake/priv', public_key_path='/fake/pub',
                snapshot_pin='20260601T000000Z',
                remote_coord_spec='file:///srv/m1-coord',
                pool_remote_spec='file:///srv/m1',
                inrelease_local_path=_inrelease,
                read_build_record=lambda *_: None,
                local_mirror_urls=['file:///srv/m1'],
                ssh_host=None,
                install_corpus=frozenset({'foo'}),
            )
        assert _ok is False, "publish must be refused on a closure break"
        assert 'mirror_closure_break' in _detail, _detail
        assert 'libmissing' in _detail, _detail
        # Refused BEFORE any .deb was pushed to the remote pool.
        assert _pushed == [], "must refuse before pushing bytes"



def test_remote_flock_acquire_confirms_lock_token():
    """STA-30(c): remote_flock_acquire returns the Popen ONLY after the
    flock'd shell echoes COORD_LOCK_ACQUIRED; on timeout/busy (no token)
    it terminates the session and returns None — never a phantom lock."""
    import sys as _sys, io as _io
    from unittest.mock import patch, MagicMock
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.transport as _tr

    # success: stdout yields the ACQUIRED token, select reports it ready
    _proc = MagicMock()
    _proc.stdout = _io.StringIO("COORD_LOCK_ACQUIRED\n")
    with patch.object(_tr.subprocess, 'Popen', return_value=_proc), \
         patch('select.select',
               return_value=([_proc.stdout], [], [])):
        _r = _tr.remote_flock_acquire(
            ssh_host='u@h', lock_path='/l', timeout_sec=1)
    assert _r is _proc, "must return the held-lock session"
    _proc.terminate.assert_not_called()

    # failure: select times out (lock busy / dead session), no token
    _proc2 = MagicMock()
    _proc2.stdout = _io.StringIO("")
    with patch.object(_tr.subprocess, 'Popen', return_value=_proc2), \
         patch('select.select', return_value=([], [], [])):
        _r2 = _tr.remote_flock_acquire(
            ssh_host='u@h', lock_path='/l', timeout_sec=1)
    assert _r2 is None, "must NOT return a phantom lock on timeout"
    _proc2.terminate.assert_called()



def test_remote_publish_refuses_verify_failed_head():
    """STA-30(b): a coord-head present on the remote but failing GPG verify
    (read_coord_head → None while the file exists) must REFUSE — NOT be
    treated as first-publish bootstrap (which would rebuild the head from
    our local view, dropping revoked_builders + peers' last_seqs)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    import coord.transport as _transport
    import coord.head as _head_mod
    import coord.identity as _identity
    import coord.store as _store
    import coord.reconcile as _reconcile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        _cfg = _sta30_publish_cfg(_td)
        # The head FILE exists on the fetched tree, but read_coord_head
        # returns None (verify failed).
        _head_path = _head_mod.coord_head_path(_cfg.dir_coord_fetched)
        os.makedirs(os.path.dirname(_head_path), exist_ok=True)
        with open(_head_path, 'wb') as _fh:
            _fh.write(b'{"v":2}')
        _inrelease = os.path.join(_td, 'InRelease')
        with open(_inrelease, 'wb') as _fh:
            _fh.write(b'Date: 2026-06-01\n')
        _lock = object()
        with patch.object(_transport, 'remote_flock_acquire',
                          return_value=_lock), \
             patch.object(_transport, 'remote_flock_release'), \
             patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head', return_value=None), \
             patch.object(_identity, 'load_keyring', return_value={}), \
             patch.object(_store, 'read_all_claims', return_value={}), \
             patch.object(_reconcile, 'publish_halt_reason',
                          return_value=None):
            _ok, _detail = _publish.remote_publish(
                builder_id='alice', config=_cfg,
                private_key_path='/fake/priv', public_key_path='/fake/pub',
                snapshot_pin='20260601T000000Z',
                remote_coord_spec='user@h:/asgard',
                inrelease_local_path=_inrelease,
                read_build_record=lambda *_: None,
                ssh_host='user@h',
            )
        assert _ok is False
        assert 'FAILED to verify' in _detail, _detail



def test_remote_publish_refuses_stale_local_jsonl():
    """STA-30(a): when our LOCAL jsonl max seq is behind what the remote
    knows about us (wiped/restored working dir), publishing would re-seq
    low and overwrite the remote's append-only history → REFUSE before any
    push."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    import coord.transport as _transport
    import coord.head as _head_mod
    import coord.identity as _identity
    import coord.store as _store
    import coord.reconcile as _reconcile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        _cfg = _sta30_publish_cfg(_td)
        # Local claims dir is EMPTY (max_seq → 0), but the remote view of
        # us carries claims up to seq 7.
        _remote_view = {'alice': [
            {'builder': 'alice', 'seq': 7, 'claim_state': 'published',
             'filename': 'foo_1_amd64.deb'},
        ]}
        _existing_head = {
            'v': 2, 'inrelease_sha256': 'a' * 64, 'snapshot': {},
            'last_seqs': {'alice': 7}, 'head_time': 'T',
            'neighbours': ['user@h:/asgard'],
        }
        _inrelease = os.path.join(_td, 'InRelease')
        with open(_inrelease, 'wb') as _fh:
            _fh.write(b'Date: 2026-06-01\n')
        _lock = object()
        _pushed: 'list' = []
        with patch.object(_transport, 'remote_flock_acquire',
                          return_value=_lock), \
             patch.object(_transport, 'remote_flock_release'), \
             patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=_existing_head), \
             patch.object(_identity, 'load_keyring', return_value={}), \
             patch.object(_store, 'read_all_claims',
                          return_value=_remote_view), \
             patch.object(_transport, 'push_single_deb',
                          side_effect=lambda **k: _pushed.append(1) or (True, '')), \
             patch.object(_reconcile, 'publish_halt_reason',
                          return_value=None):
            _ok, _detail = _publish.remote_publish(
                builder_id='alice', config=_cfg,
                private_key_path='/fake/priv', public_key_path='/fake/pub',
                snapshot_pin='20260601T000000Z',
                remote_coord_spec='user@h:/asgard',
                inrelease_local_path=_inrelease,
                read_build_record=lambda *_: None,
                ssh_host='user@h',
            )
        assert _ok is False
        assert 'stale local claims ledger' in _detail, _detail
        assert _pushed == [], "must refuse before pushing bytes"



def test_local_publish_no_seq_gap_on_append_failure():
    """STA-30(d): a transient append failure must NOT consume a seq — the
    counter advances only on success, so the ledger stays contiguous (no
    permanent sidecar_seq_gap CRITICAL)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    import coord.store as _store
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_coord = _td
            dir_log = _td
            dir_repo = _td
            dir_coord_claims = os.path.join(_td, 'claims')
        os.makedirs(_Cfg.dir_coord_claims, exist_ok=True)

        _pending = [
            {'package': 'a', 'filename': 'a_1_amd64.deb', 'sha256': 'a' * 64},
            {'package': 'b', 'filename': 'b_1_amd64.deb', 'sha256': 'b' * 64},
            {'package': 'c', 'filename': 'c_1_amd64.deb', 'sha256': 'c' * 64},
        ]
        _appended_seqs: 'list[int]' = []

        def _fake_append(claims_dir, builder_id, claim, priv):
            if claim.get('package') == 'b':
                raise OSError("disk hiccup")   # transient failure on b
            _appended_seqs.append(claim['seq'])

        with patch.object(_publish, 'generate_pending_claims',
                          return_value=_pending), \
             patch.object(_publish, 'fill_sizes_from_pool'), \
             patch.object(_store, 'append_claim', side_effect=_fake_append):
            _created, _skipped = _publish.local_publish(
                builder_id='alice', config=_Cfg(),
                private_key_path='/fake/priv', public_key_path='/fake/pub',
                snapshot_pin='S', read_build_record=lambda *_: None,
            )
        assert (_created, _skipped) == (2, 1), (_created, _skipped)
        # a→1, b fails (seq 2 NOT consumed), c reuses 2 → contiguous, no gap.
        assert _appended_seqs == [1, 2], _appended_seqs



def test_cmd_mirror_reclaim_lists_resolves_and_confirms():
    """RECLAIM-01 Chunk 5 behavior: no target → list candidates, never
    publish; source target + force → publish called with exactly the
    matching intents; unmatched target → False, no publish; YESNO 'n'
    → no publish."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    import commands.cmd_mirror as _cm

    _cands = [
        {'filename': 'a_1.0_amd64.deb', 'package': 'srca',
         'component': 'main', 'old_seq': 1, 'remote_sha': 'a' * 64,
         'local_sha': 'b' * 64, 'size': 5, 'intended_version': '1.0',
         'built_version': '1.0', 'path': '/x/a_1.0_amd64.deb'},
        {'filename': 'b_1.0_amd64.deb', 'package': 'srcb',
         'component': 'main', 'old_seq': 2, 'remote_sha': 'c' * 64,
         'local_sha': 'd' * 64, 'size': 5, 'intended_version': '1.0',
         'built_version': '1.0', 'path': '/x/b_1.0_amd64.deb'},
    ]

    class _Cfg:
        dir_cache = tempfile.gettempdir()
        dir_repo = '/nonexistent-repo'
        dir_log = '/nonexistent-log'

    # Patch ATTRIBUTES on the real modules (not sys.modules): the
    # command does `import coord.transport as _t` at call time, which
    # binds via the coord package attribute when the real submodule was
    # already imported by an earlier test — a sys.modules swap is
    # silently ignored in that case.
    import mirror as _mirror_mod
    import signing as _signing_mod
    import coord.head as _head_mod
    import coord.identity as _id_mod
    import coord.store as _store_mod
    import coord.transport as _transport_mod
    _patches = [
        (_mirror_mod, 'read_mirror_state',
         lambda cfg, n: ({'url': 'ssh://u@h/pool', 'ssh_key': ''}
                         if n == 'm1' else None)),
        (_mirror_mod, 'list_mirrors', lambda cfg: ['m1']),
        (_mirror_mod, 'coord_root_for', lambda url: url + '-coord'),
        (_mirror_mod, 'rsync_spec_for_url',
         lambda url: ('u@h:/pool-coord', None)),
        (_mirror_mod, 'local_ahead_candidates',
         lambda bb, bid, repo, blog: list(_cands)),
        (_signing_mod, 'signing_home', lambda cfg: '/s'),
        (_head_mod, 'read_coord_head',
         lambda fetched, home: {'revoked_builders': {}}),
        (_id_mod, 'load_keyring', lambda d: {}),
        (_store_mod, 'read_all_claims', lambda d, k, r: {'bid': []}),
        (_transport_mod, 'pull_remote_coord',
         lambda **k: (True, '')),
    ]
    _saved_attrs = [(_m, _a, getattr(_m, _a)) for _m, _a, _ in _patches]

    _published: 'list' = []

    def _mk():
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess._coord_self_keys = lambda: ('bid', 'priv', 'pub')
        _sess.cmd_mirror_publish = (
            lambda *a, reclaim_intents=None:
            (_published.append((a, reclaim_intents)), True)[1])
        return _sess

    class _FakePrompt:
        _answer = 'y'

        def __init__(self, *a, **k):
            pass

        def get_response(self):
            return _FakePrompt._answer

    _orig_prompt = _cm.Prompt
    _orig_print = build.console.print
    _lines: 'list[str]' = []
    build.console.print = lambda *a, **k: _lines.append(
        ' '.join(str(x) for x in a))
    _cm.Prompt = _FakePrompt
    try:
        for _m, _a, _f in _patches:
            setattr(_m, _a, _f)
        # 1. no target → listing only, no publish
        _published.clear()
        _r = _mk().cmd_mirror_reclaim()
        assert _r is True and _published == [], (_r, _published, _lines[-6:])
        assert any('a_1.0_amd64.deb' in _l for _l in _lines), _lines
        # 2. source target + force → publish with the matching intent
        _published.clear()
        _r = _mk().cmd_mirror_reclaim('srcb', 'force')
        assert _r is True and len(_published) == 1, (_r, _published)
        _a, _intents = _published[0]
        # audit #64: reclaim must publish with --no-iso so the release-media
        # gate doesn't refuse it when current-snapshot ISOs are absent.
        assert _a == ('m1', '--no-iso'), _a
        assert [_i['filename'] for _i in _intents] == ['b_1.0_amd64.deb']
        # 3. unmatched target → False, no publish
        _published.clear()
        _r = _mk().cmd_mirror_reclaim('nope.deb', 'force')
        assert _r is False and _published == []
        # 4. confirmation declined → no publish
        _published.clear()
        _FakePrompt._answer = 'n'
        _r = _mk().cmd_mirror_reclaim('srca')
        assert _r is True and _published == []
    finally:
        for _m, _a, _orig in _saved_attrs:
            setattr(_m, _a, _orig)
        _cm.Prompt = _orig_prompt
        build.console.print = _orig_print



def test_mirror_pull_ledger_drives_mixed_snapshot_download():
    """CLOSURE-LEDGER: a fresh peer pulling a MIXED-SNAPSHOT mirror downloads
    the latest live version of each closure binary off the signed ledger —
    liba@TS4 (snapshot != current) IS pulled, obsolete liba@TS0 (absent from
    the ledger) is NOT.  This is exactly what the old strict snapshot-equality
    filter stranded."""
    import sys as _sys
    import hashlib as _hashlib
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.config_manifest as _cfgman
    _a = _hashlib.sha256(b'liba-2.0').hexdigest()
    _e = _hashlib.sha256(b'libe-1.0').hexdigest()
    _contents = {'liba_2.0_amd64.deb': b'liba-2.0',
                 'libe_1.0_amd64.deb': b'libe-1.0',
                 'liba_1.0_amd64.deb': b'liba-1.0'}
    # liba_2.0 obsoletes liba_1.0 (folded); libe live.  All at different snaps.
    _claims = {'peer': [
        {'builder': 'peer', 'seq': 1, 'package': 'liba',
         'filename': 'liba_1.0_amd64.deb', 'sha256': 'x' * 64,
         'claim_state': 'published', 'component': 'main', 'snapshot': 'TS0'},
        {'builder': 'peer', 'seq': 5, 'package': 'liba',
         'filename': 'liba_2.0_amd64.deb', 'sha256': _a,
         'claim_state': 'published', 'component': 'main', 'snapshot': 'TS4',
         'obsoletes_seq': 1},
        {'builder': 'peer', 'seq': 9, 'package': 'libe',
         'filename': 'libe_1.0_amd64.deb', 'sha256': _e,
         'claim_state': 'published', 'component': 'main', 'snapshot': 'TS12'},
    ]}
    _head = {'revoked_builders': {}, 'closure_ledger_sha256': 'd' * 64}
    _ledger = {'entries': {
        'liba|amd64': {'filename': 'liba_2.0_amd64.deb', 'sha256': _a,
                       'size': 8, 'component': 'main', 'version': '2.0',
                       'package': 'liba', 'arch': 'amd64'},
        'libe|amd64': {'filename': 'libe_1.0_amd64.deb', 'sha256': _e,
                       'size': 8, 'component': 'main', 'version': '1.0',
                       'package': 'libe', 'arch': 'amd64'},
    }}
    with tempfile.TemporaryDirectory() as _td:
        _sess, _pulled, _lines, _rec, _run, _undo = _mirror_pull_harness(
            _td, _claims, _head, _contents)
        _saved_rv = _cfgman.read_verified_closure_ledger
        _cfgman.read_verified_closure_ledger = (
            lambda fetched, sha: (True, 'ok', _ledger))
        try:
            _r = _run()
        finally:
            _cfgman.read_verified_closure_ledger = _saved_rv
            _undo()
        assert _r is True, _lines[-4:]
        # latest versions across snapshots pulled; obsolete TS0 file NOT.
        assert set(_pulled) == {'liba_2.0_amd64.deb', 'libe_1.0_amd64.deb'}, \
            _pulled
        assert 'liba_1.0_amd64.deb' not in _pulled, _pulled
        _summary = [_l for _l in _lines if 'downloaded=' in _l]
        assert _summary and 'downloaded=2' in _summary[-1], _lines[-4:]



def test_mirror_pull_falls_back_to_live_claims_when_no_ledger():
    """Back-compat: a head WITHOUT a closure_ledger pin → pull walks the
    live-claim set with NO snapshot-equality filter, so older-snapshot live
    claims (which the old :1541 filter stranded on a mixed-snapshot mirror)
    are downloaded."""
    import hashlib as _hashlib
    _a = _hashlib.sha256(b'liba').hexdigest()
    _e = _hashlib.sha256(b'libe').hexdigest()
    _contents = {'liba_1.0_amd64.deb': b'liba', 'libe_1.0_amd64.deb': b'libe'}
    # both live; liba stamped at an OLDER snapshot than the local pin (TS12)
    _claims = {'peer': [
        {'builder': 'peer', 'seq': 1, 'package': 'liba',
         'filename': 'liba_1.0_amd64.deb', 'sha256': _a,
         'claim_state': 'published', 'component': 'main', 'snapshot': 'TS0'},
        {'builder': 'peer', 'seq': 2, 'package': 'libe',
         'filename': 'libe_1.0_amd64.deb', 'sha256': _e,
         'claim_state': 'published', 'component': 'main', 'snapshot': 'TS12'},
    ]}
    _head = {'revoked_builders': {}}   # no closure_ledger_sha256 → fallback
    with tempfile.TemporaryDirectory() as _td:
        _sess, _pulled, _lines, _rec, _run, _undo = _mirror_pull_harness(
            _td, _claims, _head, _contents)
        try:
            _r = _run()
        finally:
            _undo()
        assert _r is True, _lines[-4:]
        # liba@TS0 NOT stranded despite local pin TS12 — both pulled.
        assert set(_pulled) == {'liba_1.0_amd64.deb', 'libe_1.0_amd64.deb'}, \
            _pulled



def test_cmd_mirror_pull_redownloads_stale_reclaimed_file():
    """RECLAIM-01 Chunk 6: the pull skip-present fast path stays
    hash-free for normal claims, but a claim carrying reclaims_seq
    sha-checks the present local file and re-downloads on mismatch
    (the publisher rewrote the bytes under the same filename) —
    counted as refreshed."""
    import sys as _sys
    import hashlib as _hashlib
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    import mirror as _mirror_mod
    import signing as _signing_mod
    import coord.head as _head_mod
    import coord.identity as _id_mod
    import coord.store as _store_mod
    import coord.transport as _transport_mod

    with tempfile.TemporaryDirectory() as _td:
        _pool = os.path.join(_td, 'pool')
        os.makedirs(_pool)
        # plain claim: file present, content does NOT match claim sha —
        # must still be skipped without any sha check
        with open(os.path.join(_pool, 'plain_1.0_amd64.deb'), 'wb') as _f:
            _f.write(b'whatever')
        # reclaimed claim: present file holds the SUPERSEDED bytes
        with open(os.path.join(_pool, 'recl_1.0_amd64.deb'), 'wb') as _f:
            _f.write(b'old-bytes')
        _new_sha = _hashlib.sha256(b'new-bytes').hexdigest()
        _claims = {'peer': [
            {'builder': 'peer', 'seq': 1, 'package': 'plain',
             'filename': 'plain_1.0_amd64.deb', 'sha256': 'f' * 64,
             'claim_state': 'published', 'component': 'main',
             'snapshot': ''},
            {'builder': 'peer', 'seq': 9, 'package': 'recl',
             'filename': 'recl_1.0_amd64.deb', 'sha256': _new_sha,
             'claim_state': 'published', 'component': 'main',
             'snapshot': '', 'reclaims_seq': 3},
        ]}

        _pulled: 'list[str]' = []

        def _fake_pull_single(*, remote_spec, local_path, ssh_key=None):
            _pulled.append(os.path.basename(local_path))
            with open(local_path, 'wb') as _fh:
                _fh.write(b'new-bytes')
            return True, ''

        class _Cfg:
            dir_cache = _td
            dir_repo = _pool
            build_codename = 'thor'

            def deb_dest_for_filename(self, fn, comp):
                return _pool

        _patches = [
            (_mirror_mod, 'read_mirror_state',
             lambda cfg, n: {'url': 'ssh://u@h/pool', 'ssh_key': ''}),
            (_mirror_mod, 'list_mirrors', lambda cfg: ['m1']),
            (_mirror_mod, 'coord_root_for', lambda url: url + '-coord'),
            (_mirror_mod, 'rsync_spec_for_url',
             lambda url: ('u@h:/x', None)),
            (_signing_mod, 'signing_home', lambda cfg: '/s'),
            (_head_mod, 'read_coord_head',
             lambda fetched, home: {'revoked_builders': {}}),
            (_id_mod, 'load_keyring', lambda d: {}),
            (_store_mod, 'read_all_claims', lambda d, k, r: _claims),
            (_transport_mod, 'pull_remote_coord',
             lambda **k: (True, '')),
            (_transport_mod, 'pull_single_file', _fake_pull_single),
        ]
        _saved = [(_m, _a, getattr(_m, _a)) for _m, _a, _ in _patches]
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess._coord_self_keys = lambda: ('us', 'priv', 'pub')
        _sess._snapshot_current = lambda: ''
        _recorded: 'list' = []
        _sess._mirror_pull_write_build_records = (
            lambda n, d: _recorded.append(d))
        _lines: 'list[str]' = []
        _orig_print = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            for _m, _a, _f2 in _patches:
                setattr(_m, _a, _f2)
            _r = _sess.cmd_mirror_pull('m1')
            assert _r is True, (_r, _lines[-4:])
        finally:
            for _m, _a, _orig in _saved:
                setattr(_m, _a, _orig)
            build.console.print = _orig_print
        # Only the reclaimed file was re-downloaded.
        assert _pulled == ['recl_1.0_amd64.deb'], _pulled
        with open(os.path.join(_pool, 'recl_1.0_amd64.deb'), 'rb') as _fh:
            assert _fh.read() == b'new-bytes'
        # plain file untouched despite sha disagreement (no hash check).
        with open(os.path.join(_pool, 'plain_1.0_amd64.deb'), 'rb') as _fh:
            assert _fh.read() == b'whatever'
        _summary = [_l for _l in _lines if 'downloaded=' in _l]
        assert _summary and 'refreshed=1' in _summary[-1], _lines
        assert 'skipped_present=1' in _summary[-1], _summary
        assert 'downloaded=1' in _summary[-1], _summary



def test_cmd_mirror_pull_folds_superseded_old_claim():
    """STA-29: the pull walk must FOLD supersession back-refs, not skip
    only marker states.  After a peer's reclaim the jsonl carries the OLD
    published claim (state 'published', old sha) AND the new reclaim claim
    (reclaims_seq → old, new sha) for the same filename.  Without the fold
    the walk processes the OLD claim first, downloads the REWRITTEN remote
    bytes, sha-mismatches the old claim, unlinks + fails (and re-downloads
    under the reclaim claim).  With the fold the old claim is skipped and
    only the reclaim drives a single, sha-correct download."""
    import sys as _sys
    import hashlib as _hashlib
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    import mirror as _mirror_mod
    import signing as _signing_mod
    import coord.head as _head_mod
    import coord.identity as _id_mod
    import coord.store as _store_mod
    import coord.transport as _transport_mod

    with tempfile.TemporaryDirectory() as _td:
        _pool = os.path.join(_td, 'pool')
        os.makedirs(_pool)
        # recl file is ABSENT locally → the walk takes the download path.
        _old_sha = _hashlib.sha256(b'old-bytes').hexdigest()
        _new_sha = _hashlib.sha256(b'new-bytes').hexdigest()
        # OLD published claim FIRST (lower seq), then the reclaim — file order
        # matters: the fold must skip the old one regardless.
        _claims = {'peer': [
            {'builder': 'peer', 'seq': 3, 'package': 'recl',
             'filename': 'recl_1.0_amd64.deb', 'sha256': _old_sha,
             'claim_state': 'published', 'component': 'main', 'snapshot': ''},
            {'builder': 'peer', 'seq': 9, 'package': 'recl',
             'filename': 'recl_1.0_amd64.deb', 'sha256': _new_sha,
             'claim_state': 'published', 'component': 'main', 'snapshot': '',
             'reclaims_seq': 3},
        ]}

        _pulled: 'list[str]' = []

        def _fake_pull_single(*, remote_spec, local_path, ssh_key=None):
            _pulled.append(os.path.basename(local_path))
            with open(local_path, 'wb') as _fh:
                _fh.write(b'new-bytes')   # remote serves the REWRITTEN bytes
            return True, ''

        class _Cfg:
            dir_cache = _td
            dir_repo = _pool
            build_codename = 'thor'

            def deb_dest_for_filename(self, fn, comp):
                return _pool

        _patches = [
            (_mirror_mod, 'read_mirror_state',
             lambda cfg, n: {'url': 'ssh://u@h/pool', 'ssh_key': ''}),
            (_mirror_mod, 'list_mirrors', lambda cfg: ['m1']),
            (_mirror_mod, 'coord_root_for', lambda url: url + '-coord'),
            (_mirror_mod, 'rsync_spec_for_url', lambda url: ('u@h:/x', None)),
            (_signing_mod, 'signing_home', lambda cfg: '/s'),
            (_head_mod, 'read_coord_head',
             lambda fetched, home: {'revoked_builders': {}}),
            (_id_mod, 'load_keyring', lambda d: {}),
            (_store_mod, 'read_all_claims', lambda d, k, r: _claims),
            (_transport_mod, 'pull_remote_coord', lambda **k: (True, '')),
            (_transport_mod, 'pull_single_file', _fake_pull_single),
        ]
        _saved = [(_m, _a, getattr(_m, _a)) for _m, _a, _ in _patches]
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess._coord_self_keys = lambda: ('us', 'priv', 'pub')
        _sess._snapshot_current = lambda: ''
        _sess._mirror_pull_write_build_records = lambda n, d: None
        _lines: 'list[str]' = []
        _orig_print = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            for _m, _a, _f2 in _patches:
                setattr(_m, _a, _f2)
            _r = _sess.cmd_mirror_pull('m1')
        finally:
            for _m, _a, _orig in _saved:
                setattr(_m, _a, _orig)
            build.console.print = _orig_print
        # The old claim was folded → exactly ONE download (the reclaim),
        # sha-correct, no mismatch failure.
        assert _r is True, (_r, _lines[-5:])
        assert _pulled == ['recl_1.0_amd64.deb'], _pulled
        with open(os.path.join(_pool, 'recl_1.0_amd64.deb'), 'rb') as _fh:
            assert _fh.read() == b'new-bytes'
        _summary = [_l for _l in _lines if 'downloaded=' in _l]
        assert _summary and 'downloaded=1' in _summary[-1], _lines
        assert 'mismatch=0' in _summary[-1] or 'mismatch' not in _summary[-1], \
            _summary



def test_remote_publish_drops_claim_when_push_fails():
    """A failed push for one .deb drops that claim from the publish set;
    the others land normally."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    import coord.transport as _transport
    import coord.head as _head_mod
    import coord.store as _store
    import coord.identity as _identity
    import coord.reconcile as _reconcile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_coord = _td
            dir_coord_fetched = os.path.join(_td, 'fetched')
            dir_coord_claims = os.path.join(_td, 'claims')
            dir_log = _td
            dir_repo = os.path.join(_td, 'repo')
            dir_gnupg = _td
        for _d in (_Cfg.dir_coord_fetched, _Cfg.dir_coord_claims,
                   os.path.join(_Cfg.dir_repo, 'dists/thor/main/binary-amd64')):
            os.makedirs(_d)
        # Two fake .debs locally; one will "push ok", one will "push fail"
        _names = ['ok_1.0_amd64.deb', 'bad_2.0_amd64.deb']
        for _n in _names:
            with open(os.path.join(
                _Cfg.dir_repo, 'dists/thor/main/binary-amd64', _n),
                    'wb') as _fh:
                _fh.write(_n.encode())
        _inrelease = os.path.join(_td, 'InRelease')
        with open(_inrelease, 'wb') as _fh:
            _fh.write(b'Date: 2026-06-01\nFake: contents\n')

        _existing_head = {
            'v': 2, 'inrelease_sha256': 'a' * 64,
            'snapshot': {}, 'last_seqs': {}, 'head_time': 'T',
            'neighbours': ['file:///srv/m1'],
        }
        _pending = [
            {'builder': 'alice', 'package': _n.split('_')[0],
             'intended_version': '1', 'built_version': '1',
             'filename': _n, 'sha256': 'x' * 64, 'size': 0,
             'snapshot': 'S', 'built_at': 'T',
             'claim_state': 'pending', 'seq': 0, 'v': 1}
            for _n in _names
        ]
        def _fake_push(*, local_path, remote_spec, ssh_key=None, overwrite=False,
                       on_bytes=None):
            if 'bad' in remote_spec:
                return False, 'simulated rsync failure'
            return True, ''
        # Track append_claim calls to see which claims actually landed.
        _appended_filenames: 'list[str]' = []
        def _fake_append(claims_dir, builder_id, claim, priv):
            _appended_filenames.append(claim.get('filename', ''))
            return claim.get('seq', 0)
        _lock = object()
        with patch.object(_transport, 'remote_flock_acquire',
                          return_value=_lock), \
             patch.object(_transport, 'remote_flock_release'), \
             patch.object(_transport, 'pull_remote_coord',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'read_coord_head',
                          return_value=_existing_head), \
             patch.object(_identity, 'load_keyring', return_value={}), \
             patch.object(_store, 'read_all_claims', return_value={}), \
             patch.object(_store, 'max_seq', return_value=0), \
             patch.object(_store, 'append_claim',
                          side_effect=_fake_append), \
             patch.object(_reconcile, 'publish_halt_reason',
                          return_value=None), \
             patch.object(_transport, 'push_single_deb',
                          side_effect=_fake_push), \
             patch.object(_transport, 'push_jsonl',
                          return_value=(True, '')), \
             patch.object(_head_mod, 'write_coord_head', return_value=True), \
             patch.object(_transport, 'push_coord_head',
                          return_value=(True, '')), \
             patch.object(_publish, 'generate_pending_claims',
                          return_value=_pending):
            _ok, _detail = _publish.remote_publish(
                builder_id='alice', config=_Cfg(),
                private_key_path='/fake/priv', public_key_path='/fake/pub',
                snapshot_pin='20260601T000000Z',
                remote_coord_spec='file:///srv/m1-coord',
                pool_remote_spec='file:///srv/m1',
                inrelease_local_path=_inrelease,
                read_build_record=lambda *_: None,
                local_mirror_urls=['file:///srv/m1'],
                ssh_host=None,
            )
        assert _ok, _detail
        # Only the OK file got a claim appended
        assert _appended_filenames == ['ok_1.0_amd64.deb'], _appended_filenames
        # Success detail surfaces the partial: 1 pushed, 1 failed
        assert 'pushed 1 .deb' in _detail
        assert '1 failed' in _detail



def test_claim_schema_deprecated_state_and_new_deprecation():
    """SELECT-LOCK Chunk 7: 'deprecated' is a valid + inactive claim state;
    new_deprecation keeps the file identity (unlike a tombstone) + records
    deprecates_seq; the schema version bumped to >=2."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    assert _sch.CLAIM_STATE_DEPRECATED in _sch.CLAIM_STATES
    assert _sch.CLAIM_STATE_DEPRECATED in _sch.INACTIVE_CLAIM_STATES
    assert _sch.CLAIM_STATE_RETRACTED in _sch.INACTIVE_CLAIM_STATES
    assert _sch.CLAIM_RECORD_SCHEMA_VERSION >= 2
    _dep = _sch.new_deprecation(
        builder='b1', seq=10, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo_1.0_amd64.deb', sha256='aa',
        size=42, snapshot='s', built_at='t', deprecates_seq=5)
    assert _dep['claim_state'] == _sch.CLAIM_STATE_DEPRECATED
    assert _dep['deprecates_seq'] == 5
    # KEEPS identity (unlike new_retraction which strips it)
    assert _dep['filename'] == 'foo_1.0_amd64.deb' and _dep['sha256'] == 'aa'
    assert _dep['size'] == 42 and _dep['built_version'] == '1.0'



def test_claim_from_jsonl_gates_on_fields_not_version():
    """claim_from_jsonl never compares the `v` field — it gates on required
    fields + a valid claim_state.  The version stamp is informational (it
    records what the writer knew), so a well-formed line parses regardless
    of its `v` value."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    # a published line with an arbitrary v stamp + a sig field
    _c = _sch.new_claim(
        builder='b1', seq=1, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo_1.0_amd64.deb', sha256='aa',
        size=1, snapshot='s', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _c['v'] = 1
    _c['sig'] = 'deadbeef'
    assert _sch.claim_from_jsonl(_sch.claim_to_jsonl(_c)) is not None
    # a deprecated line
    _dep = _sch.new_deprecation(
        builder='b1', seq=2, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo_1.0_amd64.deb', sha256='aa',
        size=1, snapshot='s', built_at='t', deprecates_seq=1)
    _dep['sig'] = 'deadbeef'
    _parsed = _sch.claim_from_jsonl(_sch.claim_to_jsonl(_dep))
    assert _parsed is not None
    assert _parsed['claim_state'] == _sch.CLAIM_STATE_DEPRECATED



def test_project_owners_deprecated_releases_ownership():
    """A deprecated claim (higher seq) supersedes the published claim for the
    same filename and reports builder=None — no-owner, like tunneled — so
    another builder can take it over."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    from coord import store as _store
    _pub = _sch.new_claim(
        builder='b1', seq=5, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo_1.0_amd64.deb', sha256='aa',
        size=1, snapshot='s', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _dep = _sch.new_deprecation(
        builder='b1', seq=10, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo_1.0_amd64.deb', sha256='aa',
        size=1, snapshot='s', built_at='t', deprecates_seq=5)
    _owners = _store.project_owners({'b1': [_pub, _dep]})
    _o = _owners['foo_1.0_amd64.deb']
    assert _o['builder'] is None, _o
    assert _o['claim_state'] == _sch.CLAIM_STATE_DEPRECATED, _o
    # A still-published, non-deprecated claim keeps its owner.
    _pub2 = _sch.new_claim(
        builder='b1', seq=3, package='bar', intended_version='1.0',
        built_version='1.0', filename='bar_1.0_amd64.deb', sha256='bb',
        size=1, snapshot='s', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _owners2 = _store.project_owners({'b1': [_pub2]})
    assert _owners2['bar_1.0_amd64.deb']['builder'] == 'b1'



def test_emit_deprecation_claims_keys_off_source_not_binary():
    """SELECT-LOCK Chunk 8 (fixed): deprecation is keyed on the SOURCE, not the
    binary.  A -dev binary whose SOURCE is still selected is KEPT (a source
    build publishes many binaries outside the install closure); a binary whose
    SOURCE left the selection deprecates; already-released ⇒ no re-emit."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    from coord import publish as _pub
    # source 'foo' dropped → its binaries deprecate (incl a -dev not installed)
    _foo_dev = _sch.new_claim(
        builder='b1', seq=5, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo-dev_1.0_amd64.deb', sha256='aa',
        size=7, snapshot='s', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    # source 'bar' still selected → its -dev binary is KEPT even though the
    # binary name is not in the install closure
    _bar_dev = _sch.new_claim(
        builder='b1', seq=6, package='bar', intended_version='1.0',
        built_version='1.0', filename='bar-dev_1.0_amd64.deb', sha256='bb',
        size=7, snapshot='s', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _by_builder = {'b1': [_foo_dev, _bar_dev]}
    _dep = _pub.emit_deprecation_claims(
        builder_id='b1', by_builder=_by_builder,
        closure_srcs={'bar'},        # source foo dropped, bar kept
        snapshot_pin='snap', built_at='now', start_seq=6)
    assert len(_dep) == 1, _dep
    assert _dep[0]['filename'] == 'foo-dev_1.0_amd64.deb'
    assert _dep[0]['package'] == 'foo'
    assert _dep[0]['claim_state'] == _sch.CLAIM_STATE_DEPRECATED
    assert _dep[0]['deprecates_seq'] == 5 and _dep[0]['seq'] == 7
    # idempotent: re-run with the deprecation already present ⇒ no re-emit
    _dep[0]['sig'] = 'x'
    _by_builder['b1'].append(_dep[0])
    _again = _pub.emit_deprecation_claims(
        builder_id='b1', by_builder=_by_builder, closure_srcs={'bar'},
        snapshot_pin='snap', built_at='now', start_seq=8)
    assert _again == [], _again



def test_audit_selection_coherence_flags_owned_with_dropped_source():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    from coord import schema as _sch
    # a -dev binary whose SOURCE 'foo' is gone from the selection → CRITICAL
    _owned = _sch.new_claim(
        builder='b1', seq=5, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo-dev_1.0_amd64.deb', sha256='aa',
        size=1, snapshot='s', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _f = _sl.audit_selection_coherence({'bar'}, {'b1': [_owned]}, 'b1')
    assert len(_f) == 1 and _f[0][0] == 'CRITICAL'
    assert _f[0][1] == 'selection_claim_not_in_closure', _f
    # source still selected → clean, even though the BINARY name isn't a source
    assert _sl.audit_selection_coherence({'foo'}, {'b1': [_owned]}, 'b1') == []



def test_claim_schema_obsolete_state_and_new_obsolescence():
    """LEDGER-01 Chunk 4: 'obsolete' is a valid claim state that is NOT
    inactive (ownership retained) but IS presence-skipped; new_obsolescence
    keeps file identity + obsoletes_seq; schema v3; jsonl round-trips."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    assert _sch.CLAIM_RECORD_SCHEMA_VERSION >= 3
    assert _sch.CLAIM_STATE_OBSOLETE in _sch.CLAIM_STATES
    assert _sch.CLAIM_STATE_OBSOLETE not in _sch.INACTIVE_CLAIM_STATES
    assert _sch.CLAIM_STATE_OBSOLETE in _sch.PRESENCE_SKIP_CLAIM_STATES
    assert _sch.CLAIM_STATE_DEPRECATED in _sch.PRESENCE_SKIP_CLAIM_STATES
    _obs = _sch.new_obsolescence(
        builder='b1', seq=10, package='foo', intended_version='1.0',
        built_version='1.0-1+asg1u1', filename='foo_1.0-1+asg1u1_amd64.deb',
        sha256='aa', size=42, snapshot='s', built_at='t', obsoletes_seq=5)
    assert _obs['claim_state'] == _sch.CLAIM_STATE_OBSOLETE
    assert _obs['obsoletes_seq'] == 5
    assert _obs['filename'] == 'foo_1.0-1+asg1u1_amd64.deb'
    assert _obs['sha256'] == 'aa' and _obs['size'] == 42
    _obs['sig'] = 'deadbeef'
    assert _sch.claim_from_jsonl(_sch.claim_to_jsonl(_obs)) is not None



def test_schema_superseded_seqs_and_is_superseded_claim():
    """STA-29: the canonical supersession fold.  superseded_seqs collects
    every seq named by a *_seq back-ref (retracts/deprecates/obsoletes/
    reclaims) across one builder's claims; is_superseded_claim folds a
    claim that is either a presence-skip marker OR whose seq is superseded
    — including a still-'published' claim a reclaim/obsolescence replaced."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.schema as _schema

    _claims = [
        # the live current claims
        {'seq': 10, 'claim_state': 'published', 'filename': 'live_2_amd64.deb'},
        # an OLD published claim superseded by the reclaim below
        {'seq': 3, 'claim_state': 'published', 'filename': 'recl_1_amd64.deb'},
        # the reclaim — a LIVE published claim carrying reclaims_seq=3
        {'seq': 9, 'claim_state': 'published', 'filename': 'recl_1_amd64.deb',
         'reclaims_seq': 3},
        # an obsolete marker superseding seq 4
        {'seq': 50, 'claim_state': 'obsolete', 'obsoletes_seq': 4,
         'filename': 'old_1_amd64.deb'},
        {'seq': 4, 'claim_state': 'published', 'filename': 'old_1_amd64.deb'},
    ]
    _dead = _schema.superseded_seqs(_claims)
    assert _dead == {3, 4}, _dead

    # The reclaim itself (seq 9) is LIVE; the old published claim (seq 3),
    # though state 'published', is folded by the back-ref.
    _by_seq = {_c['seq']: _c for _c in _claims}
    assert _schema.is_superseded_claim(_by_seq[3], _dead) is True
    assert _schema.is_superseded_claim(_by_seq[9], _dead) is False
    assert _schema.is_superseded_claim(_by_seq[10], _dead) is False
    # marker states fold regardless of dead_seqs
    assert _schema.is_superseded_claim(_by_seq[50], _dead) is True
    assert _schema.is_superseded_claim(_by_seq[4], _dead) is True
    # empty / seq-less claims don't crash and aren't dead by seq
    assert _schema.superseded_seqs([]) == set()
    assert _schema.is_superseded_claim(
        {'claim_state': 'published'}, _dead) is False



def test_claim_schema_reclaim_shape_and_jsonl_roundtrip():
    """RECLAIM-01 Chunk 1: new_reclaim is a LIVE claim (not a marker) —
    claim_state starts PENDING (pipeline stamps PUBLISHED at append),
    carries reclaims_seq + full file identity; schema v4; round-trips
    through claim_to_jsonl/claim_from_jsonl with reclaims_seq preserved
    (v3-reader tolerance: state is a known value, unknown keys ride
    along)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    assert _sch.CLAIM_RECORD_SCHEMA_VERSION >= 4
    _rc = _sch.new_reclaim(
        builder='b1', seq=20, package='athena-setup',
        intended_version='0.182', built_version='0.182+athena1',
        filename='athena-setup-udeb_0.182+athena1_amd64.udeb',
        sha256='3baa4783798a', size=42, snapshot='s', built_at='t',
        reclaims_seq=7, component='main')
    # LIVE claim semantics: a known state, NOT a new marker state.
    assert _rc['claim_state'] == _sch.CLAIM_STATE_PENDING
    assert _rc['claim_state'] in _sch.CLAIM_STATES
    assert _rc['reclaims_seq'] == 7
    assert _rc['filename'] == 'athena-setup-udeb_0.182+athena1_amd64.udeb'
    assert _rc['sha256'] == '3baa4783798a' and _rc['size'] == 42
    assert _rc['component'] == 'main'
    # Round-trip: reclaims_seq survives serialization; the published
    # form (post-append stamp) parses too.
    _rc['sig'] = 'deadbeef'
    _back = _sch.claim_from_jsonl(_sch.claim_to_jsonl(_rc))
    assert _back is not None and _back['reclaims_seq'] == 7
    _rc['claim_state'] = _sch.CLAIM_STATE_PUBLISHED
    _back = _sch.claim_from_jsonl(_sch.claim_to_jsonl(_rc))
    assert _back is not None and _back['claim_state'] == 'published'
    assert _back['reclaims_seq'] == 7



def test_reclaim_pair_folds_across_conflict_owner_and_disk_audits():
    """RECLAIM-01 Chunk 2: a reclaim pair (old published seq N + live
    reclaim carrying reclaims_seq=N, SAME filename, different shas)
    folds everywhere:
      - detect_hash_conflicts: NO finding (without the fold the pair
        reads as a same-filename hash conflict → publish halt)
      - project_owners: the reclaim claim wins the filename
      - project_live_claims: (pkg, ver) maps to the reclaim claim
      - audit_own_claims_on_disk: only the reclaim is audited — a file
        at the NEW sha yields no findings (without the fold the old
        claim CRITICALs own_claim_disk_sha_mismatch)
    """
    import sys as _sys
    import hashlib as _hashlib
    import tempfile
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    from coord import store as _store
    from coord import reconcile as _rec
    import mirror as _mirror

    _fn = 'athena-setup-udeb_0.182+athena1_amd64.udeb'
    _new_sha = _hashlib.sha256(b'new-bytes').hexdigest()
    _old = _sch.new_claim(
        builder='b1', seq=5, package='athena-setup',
        intended_version='0.182', built_version='0.182+athena1',
        filename=_fn, sha256='aa' * 32, size=1, snapshot='s',
        built_at='t', claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _new = _sch.new_reclaim(
        builder='b1', seq=9, package='athena-setup',
        intended_version='0.182', built_version='0.182+athena1',
        filename=_fn, sha256=_new_sha, size=9, snapshot='s',
        built_at='t', reclaims_seq=5)
    _new['claim_state'] = _sch.CLAIM_STATE_PUBLISHED
    _bb = {'b1': [_old, _new]}

    # 1. hash-conflict scan stays silent (no CRITICAL, no INFO dup).
    _f = _rec.detect_hash_conflicts(_bb)
    assert _f == [], [getattr(_x, 'message', _x) for _x in _f]

    # 2. ownership: the reclaim wins the filename with the new sha.
    _own = _store.project_owners(_bb)
    assert _own[_fn]['seq'] == 9 and _own[_fn]['sha256'] == _new_sha
    assert _own[_fn]['builder'] == 'b1'

    # 3. live (pkg, ver) map carries the reclaim claim.
    _live = _store.project_live_claims(_bb)
    _lc = _live[('athena-setup', '0.182+athena1')]
    assert _lc['seq'] == 9 and _lc.get('reclaims_seq') == 5

    # 4. disk audit: pool file at the NEW sha → completely clean.
    with tempfile.TemporaryDirectory() as _td:
        with open(os.path.join(_td, _fn), 'wb') as _fh:
            _fh.write(b'new-bytes')
        _findings = _mirror.audit_own_claims_on_disk(_bb, 'b1', _td)
        assert _findings == [], _findings



def test_validate_reclaim_intents_constructs_and_skips():
    """RECLAIM-01 Chunk 4: validate_reclaim_intents re-checks each
    intent against the just-fetched remote view — happy path yields a
    pending new_reclaim (seq=0, reclaims_seq=old_seq, local sha);
    every drift/staleness mode is SKIPPED with a reason, never fatal:
    missing back-ref seq, filename mismatch, non-published state,
    already-superseded, remote sha drift, already-in-sync."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    from coord import publish as _pub

    def _claim(seq, fn, sha, state='published', **kw):
        _c = {'builder': 'b1', 'seq': seq, 'package': 'src',
              'intended_version': '1.0', 'built_version': '1.0',
              'filename': fn, 'sha256': sha, 'claim_state': state,
              'component': 'main'}
        _c.update(kw)
        return _c

    def _intent(fn, old_seq, remote_sha, local_sha):
        return {'filename': fn, 'package': 'src', 'component': 'main',
                'old_seq': old_seq, 'remote_sha': remote_sha,
                'local_sha': local_sha, 'size': 5,
                'intended_version': '1.0', 'built_version': '1.0'}

    _bb = {'b1': [
        _claim(1, 'good_1.0_amd64.deb', 'aa' * 32),
        _claim(2, 'depr_1.0_amd64.deb', 'bb' * 32),
        _claim(3, 'depr_1.0_amd64.deb', 'bb' * 32, state='deprecated',
               deprecates_seq=2),
        _claim(4, 'drift_1.0_amd64.deb', 'cc' * 32),
        _claim(5, 'sync_1.0_amd64.deb', 'dd' * 32),
        # already reclaimed once: seq 6 superseded by seq 7
        _claim(6, 'twice_1.0_amd64.deb', 'ee' * 32),
        _claim(7, 'twice_1.0_amd64.deb', 'ff' * 32, reclaims_seq=6),
    ]}
    _intents = [
        _intent('good_1.0_amd64.deb', 1, 'aa' * 32, '11' * 32),   # OK
        _intent('gone_1.0_amd64.deb', 99, 'aa' * 32, '11' * 32),  # no seq
        _intent('good_1.0_amd64.deb', 4, 'cc' * 32, '11' * 32),   # fn mismatch
        _intent('depr_1.0_amd64.deb', 2, 'bb' * 32, '11' * 32),   # superseded
        _intent('drift_1.0_amd64.deb', 4, '99' * 32, '11' * 32),  # sha drift
        _intent('sync_1.0_amd64.deb', 5, 'dd' * 32, 'dd' * 32),   # in sync
        _intent('twice_1.0_amd64.deb', 6, 'ee' * 32, '22' * 32),  # superseded
    ]
    _ok, _skipped = _pub.validate_reclaim_intents(
        builder_id='b1', by_builder=_bb, intents=_intents,
        snapshot_pin='snap')
    assert len(_ok) == 1, (_ok, _skipped)
    _rc = _ok[0]
    assert _rc['filename'] == 'good_1.0_amd64.deb'
    assert _rc['reclaims_seq'] == 1 and _rc['seq'] == 0
    assert _rc['sha256'] == '11' * 32 and _rc['size'] == 5
    assert _rc['claim_state'] == _sch.CLAIM_STATE_PENDING
    assert _rc['snapshot'] == 'snap'
    assert len(_skipped) == 6, _skipped
    # not-ours: intents against a different builder's view yield nothing
    _ok2, _skipped2 = _pub.validate_reclaim_intents(
        builder_id='other', by_builder=_bb,
        intents=[_intents[0]], snapshot_pin='snap')
    assert _ok2 == [] and len(_skipped2) == 1



def test_push_single_deb_overwrite_drops_ignore_existing():
    """push_single_deb skips an unchanged file by SIZE (`--size-only`) by
    default — a correct immutable .deb skips, but a wrong-size (truncated)
    remote file is re-sent instead of being trusted by name (CONS-12) — and
    drops the flag under overwrite=True, where a reclaim's remote file exists
    with OLD bytes and skipping the transfer would publish a claim whose bytes
    never shipped."""
    import sys as _sys
    import types as _types
    import tempfile
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.transport as _tr

    _argvs = []

    def _fake_run(argv, **k):
        _argvs.append(argv)
        return _types.SimpleNamespace(returncode=0, stdout='', stderr='')

    _orig = _tr.subprocess
    _tr.subprocess = _types.SimpleNamespace(run=_fake_run)
    try:
        with tempfile.NamedTemporaryFile(suffix='.deb') as _f:
            _ok, _ = _tr.push_single_deb(
                local_path=_f.name, remote_spec='u@h:/p/x.deb')
            assert _ok and '--size-only' in _argvs[-1]
            assert '--ignore-existing' not in _argvs[-1]
            _ok, _ = _tr.push_single_deb(
                local_path=_f.name, remote_spec='u@h:/p/x.deb',
                overwrite=True)
            assert _ok and '--size-only' not in _argvs[-1]
    finally:
        _tr.subprocess = _orig



def test_project_owners_obsolete_retains_ownership():
    """An obsolescence (higher seq) supersedes the old version's published
    claim for that filename, and the winner KEEPS its builder — unlike a
    deprecated winner which reports builder=None."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    from coord import store as _store
    _pub_old = _sch.new_claim(
        builder='b1', seq=5, package='foo', intended_version='1.0',
        built_version='1.0-1+asg1u1', filename='foo_1.0-1+asg1u1_amd64.deb',
        sha256='aa', size=1, snapshot='s', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _pub_new = _sch.new_claim(
        builder='b1', seq=6, package='foo', intended_version='1.0',
        built_version='1.0-1+asg1u2', filename='foo_1.0-1+asg1u2_amd64.deb',
        sha256='bb', size=1, snapshot='s', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _obs = _sch.new_obsolescence(
        builder='b1', seq=7, package='foo', intended_version='1.0',
        built_version='1.0-1+asg1u1', filename='foo_1.0-1+asg1u1_amd64.deb',
        sha256='aa', size=1, snapshot='s', built_at='t', obsoletes_seq=5)
    _owners = _store.project_owners({'b1': [_pub_old, _pub_new, _obs]})
    _old = _owners['foo_1.0-1+asg1u1_amd64.deb']
    assert _old['claim_state'] == _sch.CLAIM_STATE_OBSOLETE
    assert _old['builder'] == 'b1', "obsolete must RETAIN ownership"
    _new = _owners['foo_1.0-1+asg1u2_amd64.deb']
    assert _new['claim_state'] == _sch.CLAIM_STATE_PUBLISHED
    assert _new['builder'] == 'b1'
    # fold: the live (pkg,ver) map drops the obsoleted published claim
    _live = _store.project_live_claims({'b1': [_pub_old, _pub_new, _obs]})
    assert ('foo', '1.0-1+asg1u1') not in _live
    assert ('foo', '1.0-1+asg1u2') in _live



def test_obsolete_cascade_audits_skip_and_no_findings():
    """LEDGER-01 Chunk 5 sweep: a published+obsolete pair (old version
    superseded, old file possibly pruned) produces NO findings in
    detect_hash_conflicts or audit_claims_vs_packages, and the obsolete
    line is not an asserted owner for filename-uniqueness."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    from coord import reconcile as _rc
    import mirror as _mirror
    _pub_old = _sch.new_claim(
        builder='b1', seq=5, package='foo', intended_version='1.0',
        built_version='1.0-1+asg1u1', filename='foo_1.0-1+asg1u1_amd64.deb',
        sha256='aa', size=1, snapshot='20260101T000000Z', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _pub_new = _sch.new_claim(
        builder='b1', seq=6, package='foo', intended_version='1.0',
        built_version='1.0-1+asg1u2', filename='foo_1.0-1+asg1u2_amd64.deb',
        sha256='bb', size=1, snapshot='20260201T000000Z', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _obs = _sch.new_obsolescence(
        builder='b1', seq=7, package='foo', intended_version='1.0',
        built_version='1.0-1+asg1u1', filename='foo_1.0-1+asg1u1_amd64.deb',
        sha256='aa', size=1, snapshot='20260101T000000Z', built_at='t',
        obsoletes_seq=5)
    _by = {'b1': [_pub_old, _pub_new, _obs]}
    # hash-conflict scan: the obsolete line shares the OLD filename with the
    # published claim it supersedes — must NOT read as a conflict.
    assert _rc.detect_hash_conflicts(_by) == []
    # apt-index audit: index only carries the NEW version (old pruned from
    # Packages) — the obsolete claim must not fire claim_not_in_apt_index.
    _idx = {'foo_1.0-1+asg1u2_amd64.deb': {'sha256': 'bb'}}
    _findings = _mirror.audit_claims_vs_packages(_by, _idx)
    assert _findings == [], _findings



def test_emit_supersession_obsolescence_groups_by_name_arch_and_idempotent():
    """LEDGER-01: within a (binary, arch) group only the older
    version(s) obsolete; same version across DIFFERENT arches never
    obsolete each other; once obsoleted the second run emits nothing.
    (Same-owner case of the single obsolescence emitter — the older and
    newer versions are both ours.)"""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sch
    from coord import publish as _pub
    def _claim(seq, ver, fn):
        return _sch.new_claim(
            builder='b1', seq=seq, package='foo', intended_version='1.0',
            built_version=ver, filename=fn, sha256='aa', size=1,
            snapshot='s', built_at='t',
            claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _old = _claim(5, '1.0-1+asg1u1', 'foo_1.0-1+asg1u1_amd64.deb')
    _new = _claim(6, '1.0-1+asg1u2', 'foo_1.0-1+asg1u2_amd64.deb')
    # same version, different arch — must NOT obsolete each other
    _all_a = _claim(7, '2.0-1', 'bar_2.0-1_all.deb')
    _amd_a = _claim(8, '2.0-1', 'bar_2.0-1_amd64.deb')
    _by = {'b1': [_old, _new, _all_a, _amd_a]}
    _obs = _pub.emit_supersession_obsolescence(
        builder_id='b1', by_builder=_by, snapshot_pin='snap',
        built_at='now', start_seq=8)
    assert len(_obs) == 1, _obs
    assert _obs[0]['filename'] == 'foo_1.0-1+asg1u1_amd64.deb'
    assert _obs[0]['claim_state'] == _sch.CLAIM_STATE_OBSOLETE
    assert _obs[0]['obsoletes_seq'] == 5 and _obs[0]['seq'] == 9
    # idempotent: append the obsolescence, re-run → nothing new
    _obs[0]['sig'] = 'x'
    _by['b1'].append(_obs[0])
    assert _pub.emit_supersession_obsolescence(
        builder_id='b1', by_builder=_by, snapshot_pin='snap',
        built_at='now', start_seq=9) == []
    # a peer's claims are never touched
    assert _pub.emit_supersession_obsolescence(
        builder_id='b2', by_builder=_by, snapshot_pin='snap',
        built_at='now', start_seq=0) == []



def test_emit_supersession_obsolescence_retires_own_when_peer_supersedes():
    """Pull-side LEDGER-01: when a PEER owns a NEWER version of a binary we still
    publish an OLDER version of, emit_supersession_obsolescence retires OUR old
    claim (so `repo repair cleanup` prunes it without a republish).  No successor
    present → nothing (publish-before-prune); peer owns both → we touch nothing;
    idempotent."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import publish as _pub
    from coord import schema as _sch

    def _claim(builder, seq, ver, fn):
        return _sch.new_claim(
            builder=builder, seq=seq, package='poppler', intended_version='1',
            built_version=ver, filename=fn, sha256='aa', size=1,
            snapshot='s', built_at='t', claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _u1 = 'libpoppler126_22.12.0-2+asg1u1_amd64.deb'
    _u2 = 'libpoppler126_22.12.0-2+asg1u2_amd64.deb'
    _by = {'athena-primary': [_claim('athena-primary', 2639,
                                     '22.12.0-2+asg1u1', _u1)],
           'BS2': [_claim('BS2', 10, '22.12.0-2+asg1u2', _u2)]}
    _obs = _pub.emit_supersession_obsolescence(
        builder_id='athena-primary', by_builder=_by, snapshot_pin='s',
        built_at='t', start_seq=5000)
    assert len(_obs) == 1, _obs
    assert _obs[0]['filename'] == _u1
    assert _obs[0]['claim_state'] == _sch.CLAIM_STATE_OBSOLETE
    assert _obs[0]['obsoletes_seq'] == 2639
    assert _obs[0]['builder'] == 'athena-primary'
    # idempotent: fold the obsolescence back → no re-emit
    _obs[0]['sig'] = 'x'
    _by['athena-primary'].append(_obs[0])
    assert _pub.emit_supersession_obsolescence(
        builder_id='athena-primary', by_builder=_by, snapshot_pin='s',
        built_at='t', start_seq=5001) == []
    # no successor present (only our u1) → nothing (publish-before-prune)
    _only = {'athena-primary': [_claim('athena-primary', 1,
                                       '22.12.0-2+asg1u1', _u1)]}
    assert _pub.emit_supersession_obsolescence(
        builder_id='athena-primary', by_builder=_only, snapshot_pin='s',
        built_at='t', start_seq=1) == []
    # peer owns BOTH versions → we own nothing → retire nothing
    _peer = {'BS2': [_claim('BS2', 1, '22.12.0-2+asg1u1', _u1),
                     _claim('BS2', 2, '22.12.0-2+asg1u2', _u2)]}
    assert _pub.emit_supersession_obsolescence(
        builder_id='athena-primary', by_builder=_peer, snapshot_pin='s',
        built_at='t', start_seq=1) == []



def test_remote_publish_on_published_only_on_full_success():
    """on_published fires with the published package set ONLY after pool +
    jsonl + coord-head pushes ALL succeed; any failure path skips it."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    import coord.transport as _transport
    import coord.head as _head_mod
    import coord.store as _store
    import coord.identity as _identity
    import coord.reconcile as _reconcile
    from unittest.mock import patch

    def _run(jsonl_ok: bool):
        with tempfile.TemporaryDirectory() as _td:
            class _Cfg:
                dir_coord = _td
                dir_coord_fetched = os.path.join(_td, 'fetched')
                dir_coord_claims = os.path.join(_td, 'claims')
                dir_log = _td
                dir_repo = os.path.join(_td, 'repo')
                dir_gnupg = _td
            os.makedirs(_Cfg.dir_coord_fetched)
            os.makedirs(_Cfg.dir_coord_claims)
            _bin = os.path.join(_Cfg.dir_repo, 'dists/thor/main/binary-amd64')
            os.makedirs(_bin)
            open(os.path.join(_bin, 'ok_1.0_amd64.deb'), 'wb').write(b'x')
            _inrelease = os.path.join(_td, 'InRelease')
            open(_inrelease, 'wb').write(b'Date: D\n')
            # jsonl must exist for the Step 7 push branch to run
            open(os.path.join(_Cfg.dir_coord_claims, 'alice.jsonl'),
                 'wb').write(b'')
            _head = {'v': 2, 'inrelease_sha256': 'a' * 64, 'snapshot': {},
                     'last_seqs': {}, 'head_time': 'T',
                     'neighbours': ['file:///srv/m1']}
            _pending = [{'builder': 'alice', 'package': 'ok',
                         'intended_version': '1', 'built_version': '1',
                         'filename': 'ok_1.0_amd64.deb', 'sha256': 'x' * 64,
                         'size': 0, 'snapshot': 'S', 'built_at': 'T',
                         'claim_state': 'pending', 'seq': 0, 'v': 1}]
            _seen: 'list[set]' = []
            with patch.object(_transport, 'remote_flock_acquire',
                              return_value=object()), \
                 patch.object(_transport, 'remote_flock_release'), \
                 patch.object(_transport, 'pull_remote_coord',
                              return_value=(True, '')), \
                 patch.object(_head_mod, 'read_coord_head',
                              return_value=_head), \
                 patch.object(_identity, 'load_keyring', return_value={}), \
                 patch.object(_store, 'read_all_claims', return_value={}), \
                 patch.object(_store, 'max_seq', return_value=0), \
                 patch.object(_store, 'append_claim',
                              side_effect=lambda *a, **k: 1), \
                 patch.object(_reconcile, 'publish_halt_reason',
                              return_value=None), \
                 patch.object(_transport, 'push_single_deb',
                              return_value=(True, '')), \
                 patch.object(_transport, 'push_jsonl',
                              return_value=(jsonl_ok, 'x')), \
                 patch.object(_head_mod, 'write_coord_head',
                              return_value=True), \
                 patch.object(_transport, 'push_coord_head',
                              return_value=(True, '')), \
                 patch.object(_publish, 'generate_pending_claims',
                              return_value=_pending):
                _ok, _detail = _publish.remote_publish(
                    builder_id='alice', config=_Cfg(),
                    private_key_path='/fake/priv',
                    public_key_path='/fake/pub',
                    snapshot_pin='20260601T000000Z',
                    remote_coord_spec='file:///srv/m1-coord',
                    pool_remote_spec='file:///srv/m1',
                    inrelease_local_path=_inrelease,
                    read_build_record=lambda *_: None,
                    local_mirror_urls=['file:///srv/m1'],
                    ssh_host=None,
                    on_published=lambda pkgs: _seen.append(pkgs),
                )
            return _ok, _seen
    _ok, _seen = _run(jsonl_ok=True)
    assert _ok and _seen == [{'ok'}], (_ok, _seen)
    _ok, _seen = _run(jsonl_ok=False)
    assert not _ok and _seen == [], (_ok, _seen)



def test_coherence_audit_pending_deprecation_vs_untracked_drop():
    """LEDGER-01 Chunk 7: a dropped source with intent recorded on its build
    record (selection='deprecated') is a WARNING pending-publish window; the
    same drop with NO recorded intent (or no reader wired) stays CRITICAL."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    import utils as _u
    from coord import schema as _sch
    _owned = _sch.new_claim(
        builder='b1', seq=5, package='foo', intended_version='1.0',
        built_version='1.0', filename='foo-dev_1.0_amd64.deb', sha256='aa',
        size=1, snapshot='s', built_at='t',
        claim_state=_sch.CLAIM_STATE_PUBLISHED)
    _by = {'b1': [_owned]}
    with tempfile.TemporaryDirectory() as _root:
        # intent recorded → WARNING selection_deprecation_pending
        _u.lifecycle_mark_deprecated(_root, {'foo'}, 'snapA')
        _f = _sl.audit_selection_coherence(
            {'bar'}, _by, 'b1',
            buildlog_dir=_root, read_build_record=_u.read_build_record)
        assert len(_f) == 1 and _f[0][0] == 'WARNING', _f
        assert _f[0][1] == 'selection_deprecation_pending', _f
        # no intent (record says selected) → CRITICAL
        _u.lifecycle_touch_selected(_root, {'foo': '1.0'}, 'snapB')
        _f = _sl.audit_selection_coherence(
            {'bar'}, _by, 'b1',
            buildlog_dir=_root, read_build_record=_u.read_build_record)
        assert len(_f) == 1 and _f[0][0] == 'CRITICAL', _f
        assert _f[0][1] == 'selection_claim_not_in_closure', _f
    # back-compat: no reader wired → CRITICAL
    _f = _sl.audit_selection_coherence({'bar'}, _by, 'b1')
    assert len(_f) == 1 and _f[0][0] == 'CRITICAL', _f
    # source in closure → clean either way
    assert _sl.audit_selection_coherence({'foo'}, _by, 'b1') == []



def test_generate_pending_claims_skips_deprecated_records():
    """LEDGER-01 resurrection guard: a phase=done record whose lifecycle says
    selection='deprecated' must NOT regenerate claims (the deprecation
    excluded its old claims from _known — without the guard the next publish
    would re-claim the files and silently undo the deprecation).  Flipping
    back to 'selected' restores the re-claim path."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    from coord import publish as _pub
    with tempfile.TemporaryDirectory() as _root:
        _claims = os.path.join(_root, 'claims'); os.makedirs(_claims)
        _rec = _u.new_build_record(package='foo', intended_version='1.0',
                                   patch_set_hash='')
        _rec.update({'phase': 'done', 'status': 'PASS', 'built_version': '1.0',
                     'outputs': ['foo_1.0_amd64.deb'],
                     'output_hashes': {'foo_1.0_amd64.deb': 'aa'},
                     'selection': 'deprecated',
                     'deprecated_at': '2026-06-10T00:00:00Z'})
        _u.write_build_record(_root, _rec)
        _pending = _pub.generate_pending_claims(
            builder_id='b1', buildlog_dir=_root, claims_dir=_claims,
            public_key_path='/fake', snapshot_pin='s',
            read_build_record=_u.read_build_record)
        assert _pending == [], f"deprecated record regenerated claims: {_pending}"
        # re-selected → claims regenerate (the legitimate re-claim path)
        _u.update_build_record(_root, 'foo', selection='selected',
                               deprecated_at=None)
        _pending = _pub.generate_pending_claims(
            builder_id='b1', buildlog_dir=_root, claims_dir=_claims,
            public_key_path='/fake', snapshot_pin='s',
            read_build_record=_u.read_build_record)
        assert [c['filename'] for c in _pending] == ['foo_1.0_amd64.deb']
        # legacy record (no selection field) keeps publishing as before
        _legacy = _u.new_build_record(package='bar', intended_version='1.0',
                                      patch_set_hash='')
        _legacy.update({'phase': 'done', 'status': 'PASS',
                        'built_version': '1.0',
                        'outputs': ['bar_1.0_amd64.deb'],
                        'output_hashes': {'bar_1.0_amd64.deb': 'bb'}})
        _legacy.pop('lifecycle_v'); _legacy.pop('history')
        _u.write_build_record(_root, _legacy)
        _pending = _pub.generate_pending_claims(
            builder_id='b1', buildlog_dir=_root, claims_dir=_claims,
            public_key_path='/fake', snapshot_pin='s',
            read_build_record=_u.read_build_record)
        assert 'bar_1.0_amd64.deb' in [c['filename'] for c in _pending]



def test_generate_pending_claims_skips_foreign_target():
    """Phase-2 claim gate: with build_arch set, a foreign cross-toolchain
    output is never turned into a pending claim; without it (legacy
    callers) the filter is disabled."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    from coord import publish as _pub
    with tempfile.TemporaryDirectory() as _root:
        _claims = os.path.join(_root, 'claims'); os.makedirs(_claims)
        _rec = _u.new_build_record(package='binutils',
                                   intended_version='2.40-2',
                                   patch_set_hash='')
        _native = 'binutils-x86-64-linux-gnu_2.40-2_amd64.deb'
        _foreign = 'binutils-aarch64-linux-gnu_2.40-2_amd64.deb'
        _rec.update({'phase': 'done', 'status': 'PASS',
                     'built_version': '2.40-2',
                     'outputs': [_native, _foreign],
                     'output_hashes': {_native: 'aa', _foreign: 'bb'}})
        _u.write_build_record(_root, _rec)
        _pending = _pub.generate_pending_claims(
            builder_id='b1', buildlog_dir=_root, claims_dir=_claims,
            public_key_path='/fake', snapshot_pin='s',
            read_build_record=_u.read_build_record, build_arch='amd64')
        assert sorted(c['filename'] for c in _pending) == [_native], _pending
        _pending2 = _pub.generate_pending_claims(
            builder_id='b1', buildlog_dir=_root, claims_dir=_claims,
            public_key_path='/fake', snapshot_pin='s',
            read_build_record=_u.read_build_record)
        assert len(_pending2) == 2, _pending2



def test_retract_claim_by_filename_targets_single_file():
    """Phase-5 primitive: retract ONE foreign filename while the source's
    native sibling claim survives; idempotent on re-run."""
    if not _openssl_available():
        return
    _s, _i, _st, _p, _h, _r, _t, _pub_mod = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _id_dir = os.path.join(_td, 'identity')
        _claims_dir = os.path.join(_td, 'claims')
        os.makedirs(_id_dir)
        _priv, _pub = _i.generate_keypair(_id_dir, 'alice')

        class _FakeConfig:
            dir_coord = _td
            dir_coord_identity = _id_dir
            dir_coord_claims = _claims_dir
        _cfg = _FakeConfig()
        _foreign = 'binutils-aarch64-linux-gnu_2.40-2_amd64.deb'
        _native = 'binutils-x86-64-linux-gnu_2.40-2_amd64.deb'
        for _seq, _fn in ((1, _foreign), (2, _native)):
            _claim = _s.new_claim(
                builder='alice', seq=_seq, package='binutils',
                intended_version='2.40-2', built_version='2.40-2',
                filename=_fn, sha256='a' * 64, size=1,
                snapshot='S', built_at='T',
                claim_state=_s.CLAIM_STATE_PUBLISHED)
            _st.append_claim(_claims_dir, 'alice', _claim, _priv)
        _ok, _detail = _pub_mod.retract_claim_by_filename(
            builder_id='alice', config=_cfg,
            private_key_path=_priv, public_key_path=_pub, filename=_foreign)
        assert _ok, _detail
        _claims = _st.read_builder_claims(_claims_dir, 'alice', _pub)
        _owners = _st.project_owners({'alice': _claims})
        assert _foreign not in _owners, f"foreign not retracted: {_owners}"
        assert _native in _owners, f"native sibling lost: {_owners}"
        # idempotent — no live claim for the filename on a second pass
        _ok2, _ = _pub_mod.retract_claim_by_filename(
            builder_id='alice', config=_cfg,
            private_key_path=_priv, public_key_path=_pub, filename=_foreign)
        assert not _ok2



def test_canonicalize_neighbour_records_richer_wins_regardless_of_order():
    """Audit #103: when the same URL appears as a bare v2 str AND a v3 dict
    carrying public_url, the richer dict record wins in EITHER order."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from coord import schema as _sc
    for _items in (
        ['ssh://b/p', {'url': 'ssh://b/p', 'public_url': 'https://b',
                       'public_proto': 'https'}],
        [{'url': 'ssh://b/p', 'public_url': 'https://b',
          'public_proto': 'https'}, 'ssh://b/p'],       # reversed order
    ):
        _recs = _sc.canonicalize_neighbour_records(_items)
        assert len(_recs) == 1, _recs
        assert _recs[0] == {'url': 'ssh://b/p', 'public_url': 'https://b',
                            'public_proto': 'https'}, _recs



def test_coord_reconcile_audit_local_multi_binary_all_verified():
    """Audit #101: audit_local hash-verifies EVERY binary of a multi-binary
    source (two published claims share package + built_version but have
    distinct filenames, both present in the pool with matching hashes) and
    reports neither as orphan / hash_mismatch / unclaimed (validates #98)."""
    if not _openssl_available():
        return
    import hashlib as _hl
    _s, _i, _st, _p, _h, _r, *_ = _coord_modules()
    with tempfile.TemporaryDirectory() as _td:
        _id_dir = os.path.join(_td, 'identity')
        _claims_dir = os.path.join(_td, 'claims')
        _pool = os.path.join(_td, 'repo')
        os.makedirs(_id_dir)
        os.makedirs(_pool)
        _priv, _pub = _i.generate_keypair(_id_dir, 'alice')

        def _sha(_path):
            with open(_path, 'rb') as _fh:
                return _hl.sha256(_fh.read()).hexdigest()

        _seq = 0
        for _fn in ('libfoo_1.0_amd64.deb', 'libfoo-dev_1.0_amd64.deb'):
            _fp = os.path.join(_pool, _fn)
            with open(_fp, 'wb') as _fh:
                _fh.write(_fn.encode())
            _seq += 1
            _claim = _s.new_claim(
                builder='alice', seq=_seq, package='foo',
                intended_version='1.0', built_version='1.0',
                filename=_fn, sha256=_sha(_fp), size=1, snapshot='S',
                built_at='T', claim_state=_s.CLAIM_STATE_PUBLISHED)
            _st.append_claim(_claims_dir, 'alice', _claim, _priv)

        _report = _r.audit_local(
            repo_root=_pool, claims_dir=_claims_dir, builder_id='alice',
            public_key_path=_pub, get_sha256=_sha)
        _kinds = [_f.kind for _f in _report.findings]
        assert 'orphan' not in _kinds, _kinds
        assert 'hash_mismatch' not in _kinds, _kinds
        assert 'unclaimed_pool_file' not in _kinds, _kinds
        # both filenames counted as live-claimed (the #98 filename-keyed view)
        _summary = next(_f for _f in _report.findings if _f.kind == 'summary')
        assert 'live_claims=2' in _summary.message, _summary.message

TESTS = [
    test_push_dist_tree_excludes_pool_artifacts,
    test_transport_remote_sha256_parses_digest_and_guards,
    test_fed04_flock_scripts_shape,
    test_fed04_parse_flock_status_and_staleness,
    test_fed04_remote_flock_lock_status_and_break,
    test_fed03_remote_pubkey_state_local_and_ssh,
    test_fed03d_builder_bindings_and_enforcement,
    test_fed03d_new_coord_head_carries_builders,
    test_transport_list_remote_debs_parses_and_guards,
    test_claim_schema_deprecated_state_and_new_deprecation,
    test_claim_from_jsonl_gates_on_fields_not_version,
    test_project_owners_deprecated_releases_ownership,
    test_emit_deprecation_claims_keys_off_source_not_binary,
    test_audit_selection_coherence_flags_owned_with_dropped_source,
    test_claim_schema_obsolete_state_and_new_obsolescence,
    test_schema_superseded_seqs_and_is_superseded_claim,
    test_claim_schema_reclaim_shape_and_jsonl_roundtrip,
    test_reclaim_pair_folds_across_conflict_owner_and_disk_audits,
    test_validate_reclaim_intents_constructs_and_skips,
    test_push_single_deb_overwrite_drops_ignore_existing,
    test_project_owners_obsolete_retains_ownership,
    test_obsolete_cascade_audits_skip_and_no_findings,
    test_emit_supersession_obsolescence_groups_by_name_arch_and_idempotent,
    test_emit_supersession_obsolescence_retires_own_when_peer_supersedes,
    test_remote_publish_on_published_only_on_full_success,
    test_coherence_audit_pending_deprecation_vs_untracked_drop,
    test_generate_pending_claims_skips_deprecated_records,
    test_virtual_publish_dry_run_skips_own_already_published_filenames,
    test_virtual_publish_dry_run_skips_adopted_pulled_from_filenames,
    test_virtual_publish_dry_run_synthesizes_only_new_filenames,
    test_virtual_publish_dry_run_ownership_blocked_critical,
    test_virtual_publish_dry_run_tunneled_target_emits_transfer_and_hash_conflict,
    test_virtual_publish_dry_run_same_sha_no_hash_conflict,
    test_new_claim_threads_component_field,
    test_generate_pending_claims_threads_component_from_build_record,
    test_b7_federated_patch_level_and_claim_hash,
    test_b3_b5_b11_per_arch_coord_plumbing,
    test_b3_b4_wiring_pins,
    test_d3_non_primary_drops_all_and_source_claims_and_d4_arch,
    test_d3_config_role_default_and_emit_gates,
    test_generate_pending_claims_covers_source_outputs,
    test_scan_pool_files_includes_source_artifacts_under_source_dirs,
    test_audit_closure_ledger_validates_source_entries,
    test_coord_schema_canonical_bytes_stable_across_key_order,
    test_coord_schema_canonical_bytes_excludes_sig,
    test_coord_claim_to_jsonl_round_trip,
    test_coord_claim_from_jsonl_rejects_missing_required,
    test_coord_identity_keypair_and_sign_verify,
    test_coord_identity_refuses_clobber_without_overwrite,
    test_coord_identity_load_keyring,
    test_coord_store_append_and_max_seq,
    test_coord_store_rejects_builder_mismatch,
    test_coord_store_tamper_drops_line_on_read,
    test_coord_store_project_live_claims_collapses_retraction,
    test_coord_store_project_live_claims_cross_builder_conflict_key,
    test_mirror_recompute_base_returns_oldest_snapshot_across_claims,
    test_generate_pending_claims_threads_republished_from_per_output,
    test_generate_pending_claims_non_tunneled_record_carries_none,
    test_generate_pending_claims_skips_pulled_from_peer,
    test_generate_pending_claims_reclaims_rebuilt_pulled_from,
    test_generate_pending_claims_skips_adopted_tunnel_but_keeps_self_tunnel,
    test_canonical_config_round_trip_and_verify,
    test_apply_canonical_config_is_all_or_nothing,
    test_closure_ledger_write_read_verify_round_trip,
    test_federation_lab_build_mode_peer_workflow,
    test_federation_lab_hash_conflict,
    test_federation_lab_ownership_transfer,
    test_federation_lab_snapshot_divergence,
    test_federation_lab_reclaim,
    test_federation_lab_publish_halt_recovery,
    test_mirror03_publish_hazard_gate_blocks_unsanctioned_local_ahead,
    test_filter_pending_by_ownership_no_existing_owner_keeps,
    test_filter_pending_by_ownership_own_claim_keeps,
    test_filter_pending_by_ownership_tunneled_keeps_takes_ownership,
    test_filter_pending_by_ownership_higher_version_transfers_ownership,
    test_filter_pending_by_ownership_same_version_other_owner_blocks,
    test_filter_pending_by_ownership_lower_version_blocks,
    test_filter_pending_by_ownership_partial_publish_continues,
    test_filter_pending_by_ownership_blocks_frozen_byte_mismatch,
    test_coord_store_project_owners_single_owner_per_filename,
    test_coord_store_project_owners_tunneled_has_no_owner,
    test_coord_store_project_owners_picks_latest_by_seq,
    test_project_owners_published_takeover_beats_higher_seq_deprecation,
    test_coord_store_project_owners_skips_retracted,
    test_coord_store_project_owners_handles_empty_input,
    test_coord_reconcile_detect_hash_conflicts_critical_and_info,
    test_coord_reconcile_detect_hash_conflicts_multi_binary_same_source_no_false_positive,
    test_coord_reconcile_detect_hash_conflicts_same_filename_diff_sha_is_critical,
    test_coord_reconcile_publish_halt_round_trip,
    test_publish_halt_reason_fails_closed_on_unreadable_sentinel,
    test_coord_reconcile_audit_local_orphan_detection,
    test_coord_reconcile_audit_local_hash_mismatch_critical,
    test_coord_publish_retract_and_re_audit_collapses,
    test_coord_publish_local_skips_when_halt_set,
    test_coord_publish_generate_pending_skips_already_claimed,
    test_probe_sidecar_head_no_head_verified_and_fail,
    test_neighbour_urls_lowercases_strips_slash_sorts_dedups,
    test_canonicalize_neighbour_records_promotes_v2_and_passes_v3,
    test_neighbour_urls_projects_to_flat_str_list,
    test_new_coord_head_includes_neighbours_field,
    test_new_closure_ledger_shape,
    test_new_coord_head_closure_ledger_pin_optional,
    test_check_federation_consistency_match_returns_no_findings,
    test_check_federation_consistency_missing_on_remote_is_critical,
    test_check_federation_consistency_extra_on_remote_is_critical,
    test_check_federation_consistency_both_directions_emits_two_findings,
    test_check_federation_consistency_none_head_returns_no_findings,
    test_reconcile_neighbours_no_op_when_already_in_sync,
    test_reconcile_neighbours_rewrites_when_diff,
    test_reconcile_neighbours_first_contact_skipped_friendly,
    test_reconcile_neighbours_unreachable_peer_is_critical_failure,
    test_remote_publish_blocks_on_federation_drift,
    test_remote_publish_bootstrap_uploads_pubkey_when_no_head,
    test_remote_publish_bootstrap_carries_v3_per_peer_records,
    test_transport_pull_single_file_primitive_exists,
    test_transport_push_single_deb_primitive_exists,
    test_transport_push_single_deb_roundtrip_with_local_fs,
    test_transport_push_single_deb_on_bytes_streams_progress,
    test_transport_push_dist_tree_roundtrip_with_local_fs,
    test_transport_push_dist_tree_missing_local_dir_fails_clean,
    test_remote_publish_pushes_debs_per_file_and_calls_progress,
    test_remote_publish_refuses_on_closure_break,
    test_remote_flock_acquire_confirms_lock_token,
    test_remote_publish_refuses_verify_failed_head,
    test_remote_publish_refuses_stale_local_jsonl,
    test_local_publish_no_seq_gap_on_append_failure,
    test_remote_publish_drops_claim_when_push_fails,
    test_cmd_mirror_reclaim_lists_resolves_and_confirms,
    test_mirror_pull_ledger_drives_mixed_snapshot_download,
    test_mirror_pull_falls_back_to_live_claims_when_no_ledger,
    test_cmd_mirror_pull_redownloads_stale_reclaimed_file,
    test_cmd_mirror_pull_folds_superseded_old_claim,
    test_snapshot_divergence_note,
    test_publish_lock_records_and_reports_holder,
    test_remote_publish_validates_under_lock_before_push,
    test_audit_sources_chain_verifies_and_indexes_source_files,
    test_audit_inrelease_against_head_sha_match_returns_parsed_release,
    test_audit_inrelease_against_head_sha_mismatch_critical,
    test_audit_ownership_summary_buckets_correctly,
    test_audit_federation_walk_unreachable_peer_is_critical,
    test_audit_federation_walk_unverified_peer_is_critical,
    test_audit_federation_walk_asymmetric_membership_is_critical,
    test_publish_obsolescence_view_includes_deprecations,
    test_append_claim_writes_all_bytes,
    test_run_rsync_streaming_flushes_residual_tail,
    test_canonicalize_neighbour_records_richer_wins_regardless_of_order,
    test_coord_reconcile_audit_local_multi_binary_all_verified,
    test_generate_pending_claims_skips_foreign_target,
    test_retract_claim_by_filename_targets_single_file,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
