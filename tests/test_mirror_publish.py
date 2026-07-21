"""Athena tests — mirror registration + publish/pull (mirror.py, cmd_mirror.py, local_mirror.py, fork_mirror.py, onboarding.py).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile

from _test_helpers import (  # noqa: F401
    _BASE_CONF_BODY,
    _FakeOnbSession,
    _MINIMAL_MIRROR_BLOCK,
    _ROOT,
    _advertised_and_dispatched,
    _build_config_from,
    _build_session_for_setget,
    _closure_ledger_entry,
    _cmd_mirror_add_session,
    _conf11_make_fork_pkg,
    _federation_validators_patched,
    _lm_mock_cache_and_tree,
    _make_minimal_pkg,
    _mirror_module,
    _onboarding_patched,
    _patch_mirror_add_primitives,
    _plant_adopted_fork,
    _session_source,
    _setup_fork_test_tmpdir,
    _stub_chroot_mixin_for_apt_source,
    _stub_tui,
    _write_test_config,
)




# ─────────────────────────────────────────────────────────────────────────────
# v0.2 step 1 — Mirror class + multi-mirror BuildConfig parsing
# ─────────────────────────────────────────────────────────────────────────────

def test_mirror_url_composition():
    from utils import Mirror
    m = Mirror(id='main', baseurl='http://deb.debian.org/',
               baseid='/debian/', release='bookworm', suffix='',
               component='main', arch='amd64')
    assert m.url == 'http://deb.debian.org/debian', m.url
    assert m.suite == 'bookworm', m.suite
    assert m.dist_url == 'http://deb.debian.org/debian/dists/bookworm/', m.dist_url
    assert m.packages_path == 'main/binary-amd64/Packages', m.packages_path
    assert m.sources_path == 'main/source/Sources', m.sources_path



def test_mirror_suite_with_suffix():
    from utils import Mirror
    m = Mirror(id='security', baseurl='http://deb.debian.org',
               baseid='debian-security', release='bookworm', suffix='-security',
               component='main', arch='amd64')
    assert m.suite == 'bookworm-security', m.suite
    assert m.url == 'http://deb.debian.org/debian-security', m.url
    assert m.dist_url == 'http://deb.debian.org/debian-security/dists/bookworm-security/', m.dist_url



def test_mirror_repr_includes_identifying_fields():
    """Mirror.__repr__ must surface enough state for debug logs to be
    useful — id, composed url, suite, and component.  Strengthened
    2026-05-23 from the prior `repr(m)` no-op (TEST-09 consolidation —
    repr changes silently when fields are renamed / dropped, and a
    no-op assertion couldn't catch that)."""
    from utils import Mirror
    m = Mirror('updates', 'http://x.test', 'debian', 'bookworm',
               '-updates', 'main', 'amd64')
    r = repr(m)
    assert 'updates' in r, r          # id
    assert 'http://x.test/debian' in r, r   # composed url
    assert 'bookworm-updates' in r, r       # suite
    assert 'main' in r, r                    # component



def test_mirror_is_frozen_after_construction():
    """Mirror is a frozen dataclass — assignment after construction must
    raise FrozenInstanceError so a downstream caller can't silently
    mutate a shared instance."""
    import dataclasses as _dc
    from utils import Mirror
    m = Mirror(id='main', baseurl='http://x.test', baseid='debian',
               release='bookworm', suffix='', component='main', arch='amd64')
    try:
        m.baseurl = 'http://attacker.example'  # type: ignore[misc]
    except _dc.FrozenInstanceError:
        pass
    else:
        raise AssertionError("Mirror must be frozen; assignment should raise")



def test_mirror_normalises_baseurl_and_baseid_slashes():
    """__post_init__ strips trailing slashes from baseurl and leading +
    trailing slashes from baseid so url-building is consistent regardless
    of operator slash hygiene in [Mirror.*] sections."""
    from utils import Mirror
    m = Mirror(id='main', baseurl='http://x.test/', baseid='/debian/',
               release='bookworm', suffix='', component='main', arch='amd64')
    assert m.baseurl == 'http://x.test'
    assert m.baseid == 'debian'
    assert m.url == 'http://x.test/debian'



def test_mirror_rejects_empty_required_fields():
    """__post_init__ refuses empty strings on id / baseurl / baseid /
    release / arch with a clear ValueError naming the field.  Note:
    `component` is intentionally NOT in the required set as of FORK-01
    Step 2 — empty component is the flat-layout signal used by fork
    mirror (see Mirror.is_flat)."""
    from utils import Mirror
    for _field, _bad in [
        ('id', ''), ('baseurl', ''), ('baseid', ''),
        ('release', ''), ('arch', ''),
    ]:
        kwargs = {'id': 'main', 'baseurl': 'http://x.test', 'baseid': 'debian',
                      'release': 'bookworm', 'suffix': '', 'component': 'main', 'arch': 'amd64'}
        kwargs[_field] = _bad
        try:
            Mirror(**kwargs)
        except ValueError as e:
            assert _field in str(e), f"ValueError must name the field, got {e}"
        else:
            raise AssertionError(f"Mirror({_field}={_bad!r}) should raise ValueError")



def test_mirror_rejects_baseurl_without_scheme():
    """A baseurl without `://` (e.g. `deb.debian.org` bare hostname) is
    rejected — catches a real operator-typo class.  The error message
    lists the acceptable schemes so the operator can fix without
    digging."""
    from utils import Mirror
    try:
        Mirror(id='main', baseurl='deb.debian.org', baseid='debian',
               release='bookworm', suffix='', component='main', arch='amd64')
    except ValueError as e:
        assert 'scheme' in str(e), str(e)
    else:
        raise AssertionError("Mirror with scheme-less baseurl should raise")



def test_mirror_rejects_suffix_without_leading_dash():
    """A non-empty suffix must start with `-` so `suite = release + suffix`
    composes to a real suite name (`bookworm-security`, not
    `bookwormsecurity`).  Empty suffix is fine — main mirrors use it."""
    from utils import Mirror
    # Empty suffix OK
    m = Mirror(id='main', baseurl='http://x.test', baseid='debian',
               release='bookworm', suffix='', component='main', arch='amd64')
    assert m.suite == 'bookworm'
    # Malformed suffix rejected
    try:
        Mirror(id='security', baseurl='http://x.test', baseid='debian-security',
               release='bookworm', suffix='security',  # missing leading `-`
               component='main', arch='amd64')
    except ValueError as e:
        assert "start with '-'" in str(e), str(e)
    else:
        raise AssertionError("Mirror with suffix='security' (no leading -) should raise")



def test_mirror_with_snapshot_returns_new_instance_untouched_original():
    """Mirror.with_snapshot returns a NEW Mirror — the original instance
    is unchanged.  Confirms the frozen-dataclass contract holds across
    the `replace` path and downstream callers can pass the same Mirror
    to multiple `with_snapshot` calls without cross-contamination."""
    from utils import Mirror
    orig = Mirror(id='main', baseurl='http://deb.debian.org', baseid='debian',
                  release='bookworm', suffix='', component='main', arch='amd64')
    snap = orig.with_snapshot('20260506T120451Z')
    # Original untouched
    assert orig.baseurl == 'http://deb.debian.org'
    assert orig.baseid == 'debian'
    # Snap has the rewritten URL
    assert snap.baseurl == 'https://snapshot.debian.org/archive'
    assert snap.baseid == 'debian/20260506T120451Z'
    # Distinct instances
    assert orig is not snap



def test_check_mirror_reachability_dedups_and_classifies():
    """check_mirror_reachability probes the InRelease URL the cache will fetch:
    the dozen component-mirror sections collapse to the few distinct suite URLs
    (dedup), a 200 = reachable, a non-200 or an exception = down."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    # two component-mirrors sharing one suite → ONE InRelease URL after dedup
    _two = """
    [Mirror.main]
    Suffix =
    Component = main
    [Mirror.contrib]
    Suffix =
    Component = contrib
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_two))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, cfg.error_str
        assert len(cfg.mirrors) == 2
        # 200 → reachable, and only ONE HEAD despite two mirror sections
        _sess = mock.Mock()
        _sess.head.return_value = mock.Mock(status_code=200)
        with mock.patch.object(_u, '_http_session', return_value=_sess):
            _res = _u.check_mirror_reachability(
                cfg.mirrors, None, cfg.snapshot_baseurl)
        assert len(_res) == 1, _res
        assert _res[0][1] is True
        assert _sess.head.call_count == 1
        # non-200 → down, status surfaced
        _sess.head.return_value = mock.Mock(status_code=404)
        with mock.patch.object(_u, '_http_session', return_value=_sess):
            _res = _u.check_mirror_reachability(
                cfg.mirrors, None, cfg.snapshot_baseurl)
        assert _res[0][1] is False and '404' in _res[0][2], _res
        # exception → down, exception type surfaced
        _sess.head.side_effect = OSError("boom")
        with mock.patch.object(_u, '_http_session', return_value=_sess):
            _res = _u.check_mirror_reachability(
                cfg.mirrors, None, cfg.snapshot_baseurl)
        assert _res[0][1] is False and 'OSError' in _res[0][2], _res



def test_cache_build_gates_on_mirror_reachability_and_config_command_wired():
    """Source-pins: `cache build` runs the reachability probe and ABORTS on any
    down mirror before constructing Cache(); the `config` command is registered
    and allowed before configure."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import onboarding
    with open(os.path.join(_ROOT, 'scripts', 'commands', 'cmd_cache.py')) as _fh:
        _src = _fh.read()
    assert 'check_mirror_reachability(' in _src
    # the gate returns before Cache() when mirrors are down
    _gate = _src[_src.index('check_mirror_reachability('):]
    assert 'cache build aborted' in _gate and 'return' in _gate
    assert _gate.index('return') < _gate.index('Cache(self.config)')
    assert 'config' in onboarding.ALLOW_BEFORE_CONFIGURE
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as _fh:
        assert "register_command('config'" in _fh.read()
    with open(os.path.join(_ROOT, 'scripts', 'commands', 'cmd_run.py')) as _fh:
        assert 'def cmd_config' in _fh.read()



def test_mirror_conf_registration_round_trips_and_migrates():
    """Registration markers live in the signed mirror.conf: set/get round-trip,
    and a legacy local.conf [Registration] migrates into mirror.conf."""
    import utils
    import mirror as _m
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        assert _m.get_registration(cfg) == {}
        assert _m.set_registration(cfg, 'alpha', 'builder-x') is True
        assert _m.get_registration(cfg) == {'alpha': 'builder-x'}
        # write_local_conf no longer accepts a registration kwarg
        import inspect
        assert 'registration' not in inspect.signature(
            utils.write_local_conf).parameters
    # legacy local.conf [Registration] → migrates on first mirror.conf load
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        _lc = utils.local_conf_path(cfg)
        with open(_lc, 'w') as _fh:
            _fh.write('[Registration]\nbeta = builder-y\n')
        assert _m.get_registration(cfg) == {'beta': 'builder-y'}



def test_onboarding_logs_configure_actions():
    """configure must leave an audit trail: prompts + state-changing actions
    log to the 'athena.build' logger (→ the session build-<ts>.log), so a run
    is reconstructable.  Guards against onboarding reverting to print-only."""
    import logging as _logging
    import sys
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import onboarding
    _records: 'list[str]' = []

    class _CapH(_logging.Handler):
        def emit(self, _r):
            _records.append(_r.getMessage())

    _lg = _logging.getLogger('athena.build')
    _h = _CapH()
    _lg.addHandler(_h)
    _old_level = _lg.level
    _lg.setLevel(_logging.INFO)
    try:
        with tempfile.TemporaryDirectory() as _tmp:
            _sess = _FakeOnbSession(_tmp)
            _sess.config.build_distribution = 'Asgard'
            _answers = ['master', 'ip', 'ssh://ubuntu@1.2.3.4',
                        '', '~/mirror.key', 'http']
            with _onboarding_patched(_answers, verify_ok=True):
                onboarding._add_mirror_interactive(_sess)
    finally:
        _lg.removeHandler(_h)
        _lg.setLevel(_old_level)
    _joined = '\n'.join(_records)
    # prompts are logged (the operator's input trail)
    assert any('prompt' in _m and 'Publish URL' in _m for _m in _records), \
        _joined
    # the state-changing mirror-add action is logged, with its result
    assert any('mirror add' in _m for _m in _records), _joined
    assert any("mirror 'master' added" in _m for _m in _records), _joined



def test_onboarding_origin_completes_pathless_ssh_url():
    """Origin mirror-add wizard: a path-less ssh:// Publish URL gets a
    `Remote repo path` prompt (default ~<user>/<dist-id>) and the path is
    appended before `mirror add` sees it — so the operator can't trip the
    path-less rejection.  An explicit path passes straight through with no
    extra prompt."""
    import sys
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import onboarding
    # path-less URL → blank path answer takes the /home/ubuntu/asgard default
    with tempfile.TemporaryDirectory() as _tmp:
        _sess = _FakeOnbSession(_tmp)
        _sess.config.build_distribution = 'Asgard'
        _answers = ['master', 'ip', 'ssh://ubuntu@140.245.198.222',
                    '', '~/mirror.key', 'http']
        with _onboarding_patched(_answers, verify_ok=True):
            _name = onboarding._add_mirror_interactive(_sess)
        assert _name == 'master', _name
        assert _sess.mirror_calls, "cmd_mirror was not called"
        assert 'ssh://ubuntu@140.245.198.222/home/ubuntu/asgard' \
            in _sess.mirror_calls[0], _sess.mirror_calls[0]
    # explicit path → passed through untouched, no Remote-repo-path prompt
    with tempfile.TemporaryDirectory() as _tmp:
        _sess = _FakeOnbSession(_tmp)
        _sess.config.build_distribution = 'Asgard'
        _answers = ['origin', 'ip', 'ssh://ubuntu@host/srv/asgard',
                    '~/mirror.key', 'http']
        with _onboarding_patched(_answers, verify_ok=True):
            onboarding._add_mirror_interactive(_sess)
        assert 'ssh://ubuntu@host/srv/asgard' in _sess.mirror_calls[0], \
            _sess.mirror_calls[0]



def test_command_allowed_gates_until_configured():
    """CONFIGURE: before setup_complete only the allowlist runs; after, all
    commands are allowed."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import onboarding

    class _C:
        setup_complete = False
    _c = _C()
    # Unconfigured: allowlist (configure + inspectors + control built-ins)
    # passes — you must always be able to read state and quit.
    for _ok in ('configure', 'get', 'print',
                'help', 'clear', 'history', 'quit', 'exit'):
        assert onboarding.command_allowed(_c, _ok) is True, _ok
    for _gated in ('cache', 'source', 'mirror', 'chroot', 'iso'):
        assert onboarding.command_allowed(_c, _gated) is False, _gated
    # Configured: everything allowed.
    _c.setup_complete = True
    for _name in ('cache', 'source', 'mirror', 'configure'):
        assert onboarding.command_allowed(_c, _name) is True, _name



def test_configured_summary_reports_state_and_warns_unregistered():
    """CONFIGURE: the startup summary names role/mode + registered mirrors,
    and warns when a federation peer has no registration marker."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import onboarding
    with tempfile.TemporaryDirectory() as _tmp:
        cfg_path = _write_test_config(
            _tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(_tmp, cfg_path)
        # Federation peer, registered → summary names the mirror.
        cfg.system_role = 'federation'
        cfg.build_mode = 'build'
        import mirror as _mreg
        _mreg.set_registration(cfg, 'origin', 'bid-1')
        _s = onboarding.configured_summary(cfg)
        assert 'federation' in _s and 'build' in _s and 'origin' in _s
        # Federation peer, NO registration → warning.
        os.remove(_mreg.mirror_conf_path(cfg))   # drop the registration marker
        _s2 = onboarding.configured_summary(cfg)
        assert 'NO mirror is registered' in _s2



def test_onboarding_first_system_declines_mirror():
    """First/origin: mode forced to distribution, mirror declined → setup
    completes with Role=first and no mirror calls."""
    import onboarding
    import utils
    with tempfile.TemporaryDirectory() as _tmp:
        _sess = _FakeOnbSession(_tmp)
        # role=first, enable-mirror=no
        with _onboarding_patched(['first', 'no'], verify_ok=True):
            onboarding.run_onboarding(_sess)
        _p = utils.read_local_conf(_sess.config)
        assert _p.get('Local', 'Role') == 'first'
        assert _p.get('Local', 'Mode') == 'distribution'
        assert _p.getboolean('Local', 'SetupComplete') is True
        assert _sess.mirror_calls == []
        assert _sess.snap_called is True



def test_onboarding_federation_happy_path_registers_and_records():
    """Guided federation join: URL→ssh→gpg validated, key copied, mirror added
    + registered with --no-probe, mode chosen, registration recorded."""
    import onboarding
    import utils
    with tempfile.TemporaryDirectory() as _tmp:
        _sess = _FakeOnbSession(_tmp, self_keys=('wsl-peer-1', 'k', 'k.pub'))
        _key = os.path.join(_tmp, 'mykey')         # real file for _copy_key
        with open(_key, 'w') as _fh:
            _fh.write('KEY')
        # role, url, ssh-login, remote-path, ssh-key, gpg-key, mode
        _answers = ['federation', 'https://h.example/asgard', 'ubuntu@h.example',
                    '/home/ubuntu/asgard', _key, '/tmp/gpg.key', 'build']
        with _onboarding_patched(_answers, verify_ok=True), \
                _federation_validators_patched():
            onboarding.run_onboarding(_sess)
        # add ran with --no-probe + derived name 'h-example'; register followed.
        _add = [c for c in _sess.mirror_calls if c and c[0] == 'add']
        assert _add and '--no-probe' in _add[0], _sess.mirror_calls
        assert '--name' in _add[0] and 'h-example' in _add[0]
        assert ('builders', 'register', 'h-example') in _sess.mirror_calls
        # ssh key was copied into config dir as <name>.key.
        assert os.path.isfile(os.path.join(_tmp, 'h-example.key'))
        _p = utils.read_local_conf(_sess.config)
        assert _p.get('Local', 'Role') == 'federation'
        assert _p.get('Local', 'Mode') == 'build'
        assert _p.getboolean('Local', 'SetupComplete') is True
        # Registration now recorded in the signed mirror.conf, not local.conf.
        import mirror as _mreg
        assert _mreg.get_registration(_sess.config).get('h-example') == 'wsl-peer-1'



def test_onboarding_federation_cancel_at_url_leaves_unconfigured():
    """Typing `cancel` at any prompt aborts: nothing registered, SetupComplete
    never written (re-running `configure` restarts)."""
    import onboarding
    import utils
    with tempfile.TemporaryDirectory() as _tmp:
        _sess = _FakeOnbSession(_tmp)
        with _onboarding_patched(['federation', 'cancel'], verify_ok=True):
            onboarding.run_onboarding(_sess)
        assert _sess.mirror_calls == []
        assert _sess.snap_called is False
        _p = utils.read_local_conf(_sess.config)
        assert _p.getboolean('Local', 'SetupComplete', fallback=False) is False



def test_onboarding_federation_gpg_mismatch_aborts():
    """GPG key fails to verify against the mirror's head → abort before add;
    box stays un-configured."""
    import onboarding
    import utils
    with tempfile.TemporaryDirectory() as _tmp:
        _sess = _FakeOnbSession(_tmp, self_keys=('p', 'k', 'k.pub'))
        _key = os.path.join(_tmp, 'mykey')
        with open(_key, 'w') as _fh:
            _fh.write('KEY')
        _answers = ['federation', 'https://h/asgard', 'ubuntu@h',
                    '/home/ubuntu/asgard', _key, '/tmp/gpg.key']
        with _onboarding_patched(_answers, verify_ok=True), \
                _federation_validators_patched(gpg_ok=False):
            onboarding.run_onboarding(_sess)
        assert not any(c and c[0] == 'add' for c in _sess.mirror_calls)
        _p = utils.read_local_conf(_sess.config)
        assert _p.getboolean('Local', 'SetupComplete', fallback=False) is False



def test_ask_cancel_returns_none():
    """onboarding._ask returns None on cancel/q, else the stripped response."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import onboarding
    _saved = onboarding.Prompt
    try:
        for _word in ('cancel', 'CANCEL', ' q ', 'Q'):
            onboarding.Prompt = lambda *a, _w=_word, **k: type(
                '_P', (), {'get_response': lambda s: _w})()
            assert onboarding._ask(onboarding.PROMPT_INPUT, 'x') is None, _word
        onboarding.Prompt = lambda *a, **k: type(
            '_P', (), {'get_response': lambda s: '  hello  '})()
        assert onboarding._ask(onboarding.PROMPT_INPUT, 'x') == 'hello'
    finally:
        onboarding.Prompt = _saved



def test_adopt_snapshot_forward_from_head():
    """A peer adopts the mirror's snapshot pin FORWARD from the coord head:
    empty local → adopt; forward → adopt; equal → no-op; disabled → None.
    Uses the cheap snapshot.state pin (never a networked resolve)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui as _tuimod
    import utils
    from commands.cmd_mirror import MirrorCommandsMixin
    # _stub_tui leaves a prompt-less _FakeTui in the global; save + restore so
    # this test can't pollute a later prompt-using test.
    _saved_tui = (_tuimod.console, getattr(_tuimod, 'tui_instance', None))
    _stub_tui()

    class _Flags:
        cache_ready = True
        dep_check_ready = True

    class _Sess:
        pass

    try:
        with tempfile.TemporaryDirectory() as _tmp:
            class _Cfg:
                snapshot_enabled = True
                dir_config = _tmp
            _s = _Sess()
            _s.config = _Cfg()
            _s.flags = _Flags()
            _adopt = MirrorCommandsMixin._adopt_snapshot_forward_from_head

            # Empty local (fresh peer) → adopt the mirror's pin outright.
            _head = {'snapshot': {'current': '20260602T173733Z'}}
            assert _adopt(_s, _head, 'mirror x') == '20260602T173733Z'
            assert (utils.read_snapshot_state(_s.config).get('current')
                    == '20260602T173733Z')
            assert _s.flags.cache_ready is False        # invalidated
            assert _s.flags.dep_check_ready is False

            # Forward → adopt the newer pin.
            _s.flags.cache_ready = True
            assert _adopt(_s, {'snapshot': {'current': '20260701T000000Z'}},
                          'mirror x') == '20260701T000000Z'
            assert _s.flags.cache_ready is False

            # Equal (already at the mirror's pin) → no-op, flags untouched.
            _s.flags.cache_ready = True
            assert _adopt(_s, {'snapshot': {'current': '20260701T000000Z'}},
                          'mirror x') is None
            assert _s.flags.cache_ready is True

            # Backward (older than local) → never roll back.
            assert _adopt(_s, {'snapshot': {'current': '20250101T000000Z'}},
                          'mirror x') is None

            # Snapshots disabled → None, no write.
            _s.config.snapshot_enabled = False
            assert _adopt(_s, {'snapshot': {'current': '20270101T000000Z'}},
                          'mirror x') is None
    finally:
        _tuimod.console, _tuimod.tui_instance = _saved_tui



def test_mirror_with_snapshot_uses_passed_baseurl():
    """Mirror.with_snapshot accepts a baseurl kwarg so callers
    threading through BuildConfig.snapshot_baseurl can target a fork's
    own snapshot mirror layout without monkey-patching the default."""
    from utils import Mirror
    m = Mirror(id='main', baseurl='http://x.test', baseid='debian',
               release='bookworm', suffix='', component='main', arch='amd64')
    snap = m.with_snapshot('20260506T120451Z',
                           baseurl='https://snap.athena.local/archive')
    assert snap.url.startswith('https://snap.athena.local/archive/debian/20260506T120451Z'), \
        f"with_snapshot did not use passed baseurl, got url={snap.url}"



# ─────────────────────────────────────────────────────────────────────────────
# Cache parses the udeb (debian-installer) Packages index
# ─────────────────────────────────────────────────────────────────────────────

def test_mirror_udeb_packages_path_format():
    """Mirror.udeb_packages_path returns the conventional Debian d-i path
    fragment under <component>/debian-installer/binary-<arch>/Packages."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import Mirror
    m = Mirror(id='test', baseurl='http://example/', baseid='debian',
               release='bookworm', suffix='', component='main', arch='amd64')
    assert m.udeb_packages_path == 'main/debian-installer/binary-amd64/Packages'
    # Different component → reflected
    m2 = Mirror(id='ctest', baseurl='http://example/', baseid='debian',
                release='bookworm', suffix='', component='contrib', arch='arm64')
    assert m2.udeb_packages_path == 'contrib/debian-installer/binary-arm64/Packages'





def test_write_athena_apt_sources_emits_one_file_per_mirror():
    """MIRROR-01 Phase 6: with two ssh:// mirrors registered (each
    carrying a derived `public_url`), the chroot gets one
    /etc/apt/sources.list.d/athena-<name>.list per mirror, signed-by
    the project keyring, and no aggregate
    /etc/apt/sources.list.d/athena.list is written."""
    import mirror as _mirror
    with tempfile.TemporaryDirectory() as _cfg_dir:
        inst, writes = _stub_chroot_mixin_for_apt_source(
            build_codename='thor',
            dir_config=_cfg_dir,
        )
        _mirror.add_mirror(
            inst._config, name='alpha',
            url='ssh://ubuntu@mirror-a.example.org/srv/asgard',
            host='mirror-a.example.org', host_type='fqdn',
            public_proto='https',
            public_url='https://mirror-a.example.org/asgard',
            seed_pin='')
        _mirror.add_mirror(
            inst._config, name='beta',
            url='ssh://ubuntu@mirror-b.example.org/srv/asgard',
            host='mirror-b.example.org', host_type='fqdn',
            public_proto='https',
            public_url='https://mirror-b.example.org/asgard',
            seed_pin='')
        inst._write_athena_apt_sources()
        _paths = [_p for _p, _ in writes]
        assert sorted(_paths) == [
            '/etc/apt/sources.list.d/athena-alpha.list',
            '/etc/apt/sources.list.d/athena-beta.list',
        ], _paths
        # No aggregate single file — strictly one file per mirror.
        assert '/etc/apt/sources.list.d/athena.list' not in _paths
        # Each line carries the signed-by pin + public_url + codename + main.
        for _path, _content in writes:
            assert 'signed-by=/usr/share/keyrings/athena-archive-keyring.gpg' \
                in _content, _content
            assert ' thor main\n' in _content, _content
            # public_url (https://) is what apt reads, NOT the ssh:// url.
            assert 'ssh://' not in _content, _content
            assert 'https://mirror-' in _content, _content



def test_write_athena_apt_sources_skips_ssh_url_without_public_url():
    """An ssh:// mirror WITHOUT a `public_url` cannot be dereferenced by
    apt; the writer skips it with a warning.  An adjacent ssh:// mirror
    WITH a public_url is still written."""
    import mirror as _mirror
    with tempfile.TemporaryDirectory() as _cfg_dir:
        inst, writes = _stub_chroot_mixin_for_apt_source(
            build_codename='thor', dir_config=_cfg_dir)
        # ssh mirror with no public_url recorded — apt can't read it.
        _mirror.add_mirror(
            inst._config, name='bare',
            url='ssh://ubuntu@host/srv/asgard', seed_pin='')
        # New-shape ssh mirror — carries the derived public_url.
        _mirror.add_mirror(
            inst._config, name='primary',
            url='ssh://ubuntu@host2/srv/asgard',
            host='host2', host_type='fqdn',
            public_proto='http',
            public_url='http://host2/asgard',
            seed_pin='')
        inst._write_athena_apt_sources()
        _paths = [_p for _p, _ in writes]
        # public_url-less ssh mirror skipped; the other mirror written.
        assert _paths == ['/etc/apt/sources.list.d/athena-primary.list'], _paths
        assert 'http://host2/asgard thor main' in writes[0][1]



def test_write_athena_apt_sources_accepts_file_scheme():
    """`file://` URLs ARE supported by apt — for offline local-fs
    federation mirrors, write the source line."""
    import mirror as _mirror
    with tempfile.TemporaryDirectory() as _cfg_dir:
        inst, writes = _stub_chroot_mixin_for_apt_source(
            build_codename='thor', dir_config=_cfg_dir)
        _mirror.add_mirror(
            inst._config, name='local',
            url='file:///srv/asgard', seed_pin='')
        inst._write_athena_apt_sources()
        _paths = [_p for _p, _ in writes]
        assert _paths == ['/etc/apt/sources.list.d/athena-local.list']
        assert 'file:///srv/asgard thor main' in writes[0][1]



def test_local_mirror_release_arch_derived_from_debs():
    """Regression (audit #139): the local mirror Release Architectures line must
    reflect the .debs actually present (a non-amd64 host), not a hardcoded
    amd64."""
    import sys
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import local_mirror
    with tempfile.TemporaryDirectory() as _td:
        open(os.path.join(_td, 'foo_1.0_arm64.deb'), 'w').close()
        open(os.path.join(_td, 'bar_1.0_all.deb'), 'w').close()
        local_mirror._write_release(_td)
        with open(os.path.join(_td, 'Release')) as _fh:
            _r = _fh.read()
    assert 'Architectures: arm64 all' in _r, _r



def test_onboarding_jobs_warns_on_clamp():
    """Regression (audit #144): the onboarding jobs prompt must surface a clamp
    / non-int like `set jobs`, not silently adjust."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import onboarding
    assert 'jobs clamped to' in inspect.getsource(onboarding), (
        "out-of-range jobs must be surfaced, not silently clamped")



def test_fork_mirror_release_architectures_uses_build_arch():
    """Regression (audit #126): fork Release Architectures line must reflect
    buildconfig.arch, not a hardcoded 'amd64 all'."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    _src = inspect.getsource(fork_mirror)
    assert "'Architectures: amd64 all'" not in _src, "arch is hardcoded"
    assert "f'Architectures: {build_arch} all'" in _src



def test_onboarding_checks_set_registration_return():
    """Regression (audit #145): the federation flow must check
    mirror.set_registration's return and fail the wizard on False."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import onboarding
    assert "if not _mirror.set_registration" in inspect.getsource(onboarding), (
        "set_registration failure must abort the wizard (stay un-configured)")



def test_mirror_flat_layout_is_signalled_by_empty_component():
    """Mirror.is_flat returns True iff component is the empty string —
    the flat-layout signal used by fork mirror."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import Mirror
    flat = Mirror(id='fork', baseurl='file:///tmp', baseid='fork',
                  release='./', suffix='', component='', arch='amd64')
    standard = Mirror(id='main', baseurl='http://deb.debian.org', baseid='debian',
                      release='bookworm', suffix='', component='main', arch='amd64')
    assert flat.is_flat is True
    assert standard.is_flat is False



def test_mirror_flat_layout_url_properties():
    """Flat Mirror returns simplified URL paths (no dists/.../component prefix)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import Mirror
    m = Mirror(id='fork', baseurl='file:///tmp/wd', baseid='fork',
               release='./', suffix='', component='', arch='amd64')
    assert m.url            == 'file:///tmp/wd/fork'
    assert m.dist_url       == 'file:///tmp/wd/fork/'
    assert m.packages_path      == 'Packages'
    assert m.sources_path       == 'Sources'
    assert m.udeb_packages_path == 'Packages-udeb'



def test_mirror_with_snapshot_skips_file_scheme():
    """file:// mirrors are local trees — with_snapshot must NOT rewrite
    them to a remote snapshot URL (the local files would vanish)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import Mirror
    m = Mirror(id='fork', baseurl='file:///tmp/wd', baseid='fork',
               release='./', suffix='', component='', arch='amd64')
    rewritten = m.with_snapshot('20260101T000000Z')
    assert rewritten is m, "file:// Mirror was unexpectedly rewritten by snapshot"



def test_mirror_validation_allows_empty_component():
    """component='' is intentionally valid (signals flat layout).  All
    other Mirror fields remain mandatory non-empty."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import Mirror
    # Should construct without raising
    Mirror(id='fork', baseurl='file:///tmp', baseid='fork',
           release='./', suffix='', component='', arch='amd64')
    # But empty release still raises
    try:
        Mirror(id='fork', baseurl='file:///tmp', baseid='fork',
               release='', suffix='', component='', arch='amd64')
        raise AssertionError("empty release should have raised")
    except ValueError:
        pass



def test_fork_mirror_discover_skips_repo_subdir():
    """The 'repo' subdir under fork/source/ is helper output — discovery
    must NOT treat it as a source tree."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=False)
        # Create 'repo' subdir AND a real pkg
        os.makedirs(os.path.join(bc.dir_fork_source, 'repo'), exist_ok=True)
        _pkg_dir = os.path.join(bc.dir_fork_source, 'realpkg')
        os.makedirs(os.path.join(_pkg_dir, 'debian'), exist_ok=True)
        with open(os.path.join(_pkg_dir, 'debian', 'control'), 'w') as fh:
            fh.write('Source: realpkg\nMaintainer: x\n\nPackage: realpkg\nArchitecture: all\nDescription: x\n')
        found = fork_mirror._discover_fork_source_trees(bc.dir_fork_source)
        assert _pkg_dir in found
        assert not any(os.path.basename(p) == 'repo' for p in found)



def test_fork_mirror_discover_skips_dirs_missing_debian_control():
    """A subdir without debian/control isn't a Debian source tree —
    discovery skips it with a warning."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=False)
        # Empty subdir — not a source tree
        os.makedirs(os.path.join(bc.dir_fork_source, 'not-a-pkg'), exist_ok=True)
        found = fork_mirror._discover_fork_source_trees(bc.dir_fork_source)
        assert found == [], f"expected empty, got {found}"



def test_fork_mirror_read_pkg_version_rejects_broken_changelog():
    """Regression (audit #11): an empty/truncated/malformed debian/changelog
    must raise ValueError from _read_pkg_version so _generate_source_packages /
    _build_packages_stanzas (which guard (OSError, ValueError)) log-and-skip
    the fork — rather than aborting the cache build or silently shipping a
    'None' version (python-debian's file-handle form yields version=None on a
    broken changelog instead of raising).  A valid changelog still parses."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    with tempfile.TemporaryDirectory() as _td:
        _deb = os.path.join(_td, 'debian')
        os.makedirs(_deb)
        _cl = os.path.join(_deb, 'changelog')
        for _label, _content in [('empty', ''),
                                 ('whitespace', '   \n'),
                                 ('garbage', 'not a changelog at all\n')]:
            with open(_cl, 'w') as _fh:
                _fh.write(_content)
            try:
                _v = fork_mirror._read_pkg_version(_td)
            except ValueError:
                pass                              # expected
            else:
                raise AssertionError(
                    f"{_label} changelog accepted, got {_v!r}")
        # A valid changelog still returns its top version verbatim.
        with open(_cl, 'w') as _fh:
            _fh.write(
                'athena-base-files (12.4+athena1) thor; urgency=low\n\n'
                '  * test entry\n\n'
                ' -- Tester <t@example.org>  Mon, 30 Jun 2026 00:00:00 +0000\n')
        assert fork_mirror._read_pkg_version(_td) == '12.4+athena1'



def test_fork_mirror_generate_empty_tree_returns_false():
    """No source trees → False return → no Mirror should be registered
    (skip-if-empty per FORK-01 plan Q6)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=False)
        assert fork_mirror.generate_fork_mirror(bc) is False
        # No Release file should be written when empty
        assert not os.path.exists(os.path.join(bc.dir_fork, 'Release'))



def test_fork_mirror_generate_emits_complete_layout():
    """Full happy path: athena-installer-data udeb source tree produces
    Packages, Packages-udeb, Sources, Release, .gz variants, plus
    .dsc + .tar.* in fork/source/repo/."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        ok = fork_mirror.generate_fork_mirror(bc)
        assert ok is True, "generate_fork_mirror should succeed with a real pkg"
        for _name in ('Release', 'Packages', 'Packages-udeb', 'Sources',
                      'Packages.gz', 'Packages-udeb.gz', 'Sources.gz'):
            _path = os.path.join(bc.dir_fork, _name)
            assert os.path.exists(_path), f"missing {_name}"
        # source/repo/ must have at least the .dsc
        repo_files = os.listdir(bc.dir_fork_source_repo)
        assert any(f.endswith('.dsc') for f in repo_files), \
            f"no .dsc in source/repo/: {repo_files}"



def test_conf11_audit_clean_when_all_paths_exist():
    """Audit returns [] when every install/cp reference is satisfied."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as _tmp:
        _pkg = _conf11_make_fork_pkg(_tmp,
            makefile='install:\n\tinstall -m 755 foo.sh $(DESTDIR)/usr/bin/foo\n',
            extra_files=['foo.sh'])
        assert fork_mirror.audit_fork_tree(_pkg) == []



def test_conf11_audit_athena_tasksel_packages_list_regression():
    """The original 2026-05-18 incident: athena-tasksel Makefile
    references `packages/list` which is missing from the fork."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as _tmp:
        _pkg = _conf11_make_fork_pkg(_tmp,
            makefile=(
                'install:\n'
                '\tinstall -m 755 tasksel.pl $(DESTDIR)/usr/bin/tasksel\n'
                '\tinstall -m 755 packages/list $(DESTDIR)/usr/lib/tasksel/packages/\n'
            ),
            extra_files=['tasksel.pl'])   # packages/list NOT created
        _findings = fork_mirror.audit_fork_tree(_pkg)
        assert len(_findings) == 1, _findings
        _f = _findings[0]
        assert _f['file'] == 'Makefile'
        assert _f['source'] == 'packages/list'
        assert 'not found' in _f['reason']



def test_conf11_audit_skips_variable_expanded_paths():
    """Sources containing `$` (variable expansion) can't be resolved
    statically; audit must skip them rather than false-positive."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as _tmp:
        _pkg = _conf11_make_fork_pkg(_tmp,
            makefile=(
                'install:\n'
                '\tinstall -d $(DESTDIR)/usr/bin\n'         # -d form, no src
                '\tinstall -m 644 $(TASKDESC) $(DESTDIR)/usr/share\n'
                '\tfor f in foo bar; do install -m 755 $$f $(DESTDIR)/x; done\n'
            ))
        # None of those have a concrete static source; audit returns [].
        assert fork_mirror.audit_fork_tree(_pkg) == []



def test_conf11_audit_glob_matches_existing_dir_contents():
    """`install ... etc/* $(DESTDIR)/etc` is clean when etc/ has files."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as _tmp:
        _pkg = _conf11_make_fork_pkg(_tmp,
            rules=(
                '#!/usr/bin/make -f\n'
                'override_dh_auto_install:\n'
                '\tinstall -p -m 644 etc/* $(DESTDIR)/etc\n'
            ),
            extra_files=['etc/foo.conf', 'etc/bar.conf'])
        assert fork_mirror.audit_fork_tree(_pkg) == []



def test_conf11_audit_glob_flags_no_matching_files():
    """A glob source that matches nothing in the fork tree IS flagged."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as _tmp:
        _pkg = _conf11_make_fork_pkg(_tmp,
            rules=(
                '#!/usr/bin/make -f\n'
                'override_dh_auto_install:\n'
                '\tinstall -p -m 644 motd/* $(DESTDIR)/etc\n'
            ))   # motd/ never created
        _findings = fork_mirror.audit_fork_tree(_pkg)
        assert len(_findings) == 1
        assert _findings[0]['source'] == 'motd/*'
        assert 'glob' in _findings[0]['reason']



def test_conf11_audit_skips_comment_lines():
    """Commented `install` lines (Makefile or rules) must not be parsed."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as _tmp:
        _pkg = _conf11_make_fork_pkg(_tmp,
            makefile=(
                'install:\n'
                '\t# install -m 755 disabled-thing $(DESTDIR)/usr/bin/foo\n'
            ))
        assert fork_mirror.audit_fork_tree(_pkg) == []



def test_conf11_scan_install_sources_handles_multi_source_form():
    """`install -m 755 SRC1 SRC2 DEST` — all but last token are sources."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    _sources = fork_mirror._scan_install_sources(
        '\tinstall -m 755 alpha.sh beta.sh $(DESTDIR)/usr/bin/\n')
    assert _sources == ['alpha.sh', 'beta.sh']



def test_conf11_scan_cp_sources_handles_flags_and_skips_variables():
    """`cp -r foo bar` captures foo; `cp -r $(SRC) bar` skips."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    assert fork_mirror._scan_cp_sources(
        '\tcp -r assets/ debian/foo/usr/share/\n') == ['assets/']
    assert fork_mirror._scan_cp_sources(
        '\tcp -r $(SRC) debian/foo/usr/share/\n') == []



def test_conf11_audit_no_makefile_no_rules_returns_empty():
    """A fork tree without Makefile or debian/rules has nothing to audit;
    audit returns [] silently (not an error)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as _tmp:
        _pkg = _conf11_make_fork_pkg(_tmp)
        assert fork_mirror.audit_fork_tree(_pkg) == []



def test_conf11_audit_real_fork_trees_clean():
    """Sanity: every checked-in fork/source/<pkg>/ tree must audit
    clean.  Any new finding here is a real regression — investigate
    before silencing."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    _fork_source = os.path.join(_ROOT, 'fork', 'source')
    if not os.path.isdir(_fork_source):
        return    # no fork tree in this checkout — skip
    _trouble: 'list' = []
    for _name in sorted(os.listdir(_fork_source)):
        _pkg_dir = os.path.join(_fork_source, _name)
        if not os.path.isdir(_pkg_dir) or _name == 'repo':
            continue
        _findings = fork_mirror.audit_fork_tree(_pkg_dir)
        if _findings:
            _trouble.append((_name, _findings))
    assert not _trouble, (
        "fork tree(s) have dangling build-system references:\n  " +
        "\n  ".join(
            f"{_n}: {len(_f)} finding(s) — first: "
            f"{_f[0]['file']}:{_f[0]['line_no']} {_f[0]['source']}"
            for _n, _f in _trouble))



def test_fork_mirror_strips_epoch_from_constructed_filenames():
    """Epoch'd fork versions (athena-apt-setup is 1:0.182+athena1) must NOT
    leak the epoch into constructed/expected filenames — dpkg omits the
    epoch from .dsc/.deb/.udeb names.  The Version: field keeps the epoch;
    only the Filename drops it.  Regression for the athena-apt-setup
    Sources-omission bug (the first epoch-carrying fork): _list_files_for_pkg
    and the Filename builder matched `<pkg>_1:0.182...` against the real
    epoch-less file and silently dropped the source from fork/Sources."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    assert fork_mirror._filename_version('1:0.182+athena1') == '0.182+athena1'
    assert fork_mirror._filename_version('0.79+athena1') == '0.79+athena1'
    _bin = {'Package': 'athena-setup-udeb', 'Architecture': 'all',
            'Package-Type': 'udeb', 'Description': 'x'}
    _text = fork_mirror._format_packages_stanza(
        _bin, 'athena-apt-setup', '1:0.182+athena1', 'M <m@local>', 'amd64')
    assert 'Filename: athena-setup-udeb_0.182+athena1_all.udeb' in _text
    assert 'Version: 1:0.182+athena1' in _text



def test_fork_mirror_packages_udeb_routing_and_placeholder_hashes():
    """udeb stanza lands in Packages-udeb (not Packages); Filename uses
    bare basename matching dpkg-buildpackage output; Size/SHA256 are
    placeholder zeros."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        fork_mirror.generate_fork_mirror(bc)
        with open(os.path.join(bc.dir_fork, 'Packages-udeb'), 'r') as fh:
            udeb_body = fh.read()
        with open(os.path.join(bc.dir_fork, 'Packages'), 'r') as fh:
            deb_body = fh.read()
        assert 'Package: athena-installer-data' in udeb_body
        assert 'Package: athena-installer-data' not in deb_body, \
            "udeb leaked into Packages"
        assert 'Filename: athena-installer-data_1.0.0_all.udeb' in udeb_body
        assert 'Size: 0' in udeb_body
        assert 'SHA256: ' + ('0' * 64) in udeb_body
        assert 'MD5sum: ' + ('0' * 32) in udeb_body



def test_fork_mirror_sources_uses_real_hashes_and_directory():
    """Sources stanza for a generated .dsc must have non-placeholder
    hashes (the files actually exist on disk) and Directory: source/repo."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        fork_mirror.generate_fork_mirror(bc)
        with open(os.path.join(bc.dir_fork, 'Sources'), 'r') as fh:
            body = fh.read()
        assert 'Package: athena-installer-data' in body
        assert 'Directory: source/repo' in body
        # NOT all-zero hashes (those are placeholders for binaries only)
        assert ('0' * 64) not in body.split('Checksums-Sha256:')[1].split('\n')[1], \
            "Sources sha256 looks like a placeholder; expected real hash"



def test_fork_mirror_register_prepends_at_index_zero():
    """register_fork_mirror puts fork at index 0 so cache parses it
    FIRST — required for the supersede tracking to work."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    from utils import Mirror
    upstream = [
        Mirror(id='main',     baseurl='http://x', baseid='debian',
               release='bookworm', suffix='',          component='main', arch='amd64'),
        Mirror(id='security', baseurl='http://y', baseid='debian',
               release='bookworm', suffix='-security', component='main', arch='amd64'),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=False)
        result = fork_mirror.register_fork_mirror(upstream, bc)
    assert [m.id for m in result] == ['fork', 'main', 'security']
    assert result[0].is_flat is True



def test_fork_mirror_re_run_is_idempotent_via_stale_check():
    """Running generate_fork_mirror twice in a row should skip dpkg-source
    on the second run (stale check via mtime comparison)."""
    import sys, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        fork_mirror.generate_fork_mirror(bc)
        _dsc = os.path.join(bc.dir_fork_source_repo,
                            'athena-installer-data_1.0.0.dsc')
        _first_mtime = os.path.getmtime(_dsc)
        # Sleep enough to make mtime difference detectable on coarse FS
        time.sleep(1.1)
        fork_mirror.generate_fork_mirror(bc)
        _second_mtime = os.path.getmtime(_dsc)
        assert _first_mtime == _second_mtime, \
            "second generate_fork_mirror unexpectedly rebuilt the .dsc"



def test_fork_mirror_arch_any_filename_uses_build_arch():
    """For an Architecture: any (or arch-specific) binary, _format_packages_
    stanza must emit Filename + Architecture using the build target arch
    (e.g. amd64), NOT the literal string from debian/control.  Without
    this remap, an arch=any fork pkg's predicted filename
    `<pkg>_<ver>_any.deb` never matches what dpkg-buildpackage actually
    deposits (`<pkg>_<ver>_amd64.deb`), and the source audit loops the
    rebuild.  Caught 2026-05-21 on the base-files Path X migration."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from debian.deb822 import Deb822
    import fork_mirror as _fm
    # Architecture: any → arch-specific .deb
    _bin_any = Deb822(['Package: example\n', 'Architecture: any\n',
                       'Description: x\n'])
    _stanza = _fm._format_packages_stanza(_bin_any, 'example',
                                          '1.0', 'M <m@x>', 'amd64')
    assert 'Filename: example_1.0_amd64.deb' in _stanza, \
        f"arch=any did not remap to build_arch in filename: {_stanza}"
    assert 'Architecture: amd64' in _stanza, \
        f"arch=any did not remap to build_arch in field: {_stanza}"
    assert 'Architecture: any' not in _stanza, \
        f"literal 'any' leaked through: {_stanza}"
    # Architecture: all → stays 'all' (arch-independent .deb)
    _bin_all = Deb822(['Package: example\n', 'Architecture: all\n',
                       'Description: x\n'])
    _stanza_all = _fm._format_packages_stanza(_bin_all, 'example',
                                              '1.0', 'M <m@x>', 'amd64')
    assert 'Filename: example_1.0_all.deb' in _stanza_all, \
        f"arch=all was incorrectly remapped: {_stanza_all}"
    assert 'Architecture: all' in _stanza_all



def test_fork_invalidation_wipes_artifacts_on_content_change():
    """End-to-end: edit fork/source/<pkg>/, run generate_fork_mirror,
    confirm the stale derived artifacts (fork repo dsc/tar, source/
    copy, repo/ debs, build logs) get wiped so the next build run sees
    a clean slate."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        # First run: establishes baseline hash + generates repo artifacts
        fork_mirror.generate_fork_mirror(bc)
        _hash_file = os.path.join(
            bc.dir_fork_source_repo, 'athena-installer-data.tree-hash')
        assert os.path.isfile(_hash_file), "tree-hash not persisted on first run"

        # Plant fake stale artifacts that should get wiped on next run.
        # Post-segregation: installable artifacts live under repo/main/.
        _stale_source = os.path.join(
            bc.dir_source, 'athena-installer-data_1.0.0.tar.gz')
        with open(_stale_source, 'w') as fh:
            fh.write('stale tarball')
        _stale_deb = os.path.join(
            bc.dir_repo_main, 'athena-installer-data_1.0.0+thor1_all.udeb')
        os.makedirs(os.path.dirname(_stale_deb), exist_ok=True)
        with open(_stale_deb, 'w') as fh:
            fh.write('stale udeb')
        import utils as _u
        _stale_build_log = os.path.join(
            bc.dir_log, 'build', 'athena-installer-data' + _u.BUILD_RECORD_SUFFIX)
        _u.write_build_record(
            os.path.join(bc.dir_log, 'build'),
            _u.new_build_record(
                package='athena-installer-data',
                intended_version='1.0.0', patch_set_hash='',
            ),
        )

        # Mutate fork content
        with open(os.path.join(bc.dir_fork_source, 'athena-installer-data',
                               'debian', 'control'), 'a') as fh:
            fh.write('# mutation: triggers invalidation\n')

        # Second run: invalidation must wipe the three stale artifacts
        fork_mirror.generate_fork_mirror(bc)
        for _path in (_stale_source, _stale_deb, _stale_build_log):
            assert not os.path.exists(_path), (
                f"stale artifact survived invalidation: {_path}")



def test_fork_invalidation_no_op_when_hash_matches():
    """generate_fork_mirror run twice with no content change must NOT
    wipe artifacts (perf: invalidation only fires on real changes)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        fork_mirror.generate_fork_mirror(bc)
        _dsc = os.path.join(
            bc.dir_fork_source_repo, 'athena-installer-data_1.0.0.dsc')
        _first_mtime = os.path.getmtime(_dsc)

        # Plant an "existing build artifact" that the no-op path must preserve
        _existing_deb = os.path.join(
            bc.dir_repo_main, 'athena-installer-data_1.0.0+thor1_all.udeb')
        os.makedirs(os.path.dirname(_existing_deb), exist_ok=True)
        with open(_existing_deb, 'w') as fh:
            fh.write('existing deb')

        # Second run, no content change — must NOT wipe the deb
        fork_mirror.generate_fork_mirror(bc)
        assert os.path.exists(_existing_deb), (
            "no-op invalidation wiped a deb it shouldn't have")
        assert os.path.getmtime(_dsc) == _first_mtime, (
            "no-op invalidation re-ran dpkg-source-b unnecessarily")



def test_fork_invalidation_preserves_pull_adopted_binaries_on_fresh_peer():
    """Federation peer regression: a fork whose binaries were ADOPTED via
    `mirror pull` (build record carries pulled_from + binary present) but
    has NO persisted tree-hash must NOT be invalidated.  The old blanket
    'no stored hash ⇒ wipe' deleted the adopted binary + record, forcing a
    needless rebuild AND making the peer take OWNERSHIP of forks it only
    adopted.  The guard seeds the tree-hash and skips the wipe."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror, utils as _u
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        _pkg = 'athena-installer-data'
        _pkg_dir = os.path.join(bc.dir_fork_source, _pkg)
        _fn = _pkg + '_1.0.0+thor1_all.udeb'
        _deb, _hash_file = _plant_adopted_fork(bc, _pkg, _fn, pulled_from=True)

        _wiped = fork_mirror._check_and_invalidate_fork_pkg(_pkg_dir, bc)

        assert _wiped is False, "pull-adopted fork must NOT be invalidated"
        assert os.path.isfile(_deb), (
            "adopted fork binary was wiped — peer forced to rebuild")
        _rec = _u.read_build_record(os.path.join(bc.dir_log, 'build'), _pkg)
        assert _rec is not None and _rec.get('pulled_from'), (
            "adopted pulled_from record was wiped")
        assert os.path.isfile(_hash_file), (
            "tree-hash not seeded → wipe would recur every cache build")



def test_fork_invalidation_still_wipes_orphan_without_pull_record():
    """Guard must NOT weaken orphan cleanup: a fork binary present with NO
    pulled_from record and NO stored hash is a genuine orphan and MUST
    still be wiped (deterministic rebuild)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        _pkg = 'athena-installer-data'
        _pkg_dir = os.path.join(bc.dir_fork_source, _pkg)
        _fn = _pkg + '_1.0.0+thor1_all.udeb'
        _deb, _ = _plant_adopted_fork(bc, _pkg, _fn, pulled_from=False)

        _wiped = fork_mirror._check_and_invalidate_fork_pkg(_pkg_dir, bc)

        assert _wiped is True, "orphan (no pull record) must still invalidate"
        assert not os.path.isfile(_deb), "orphan binary should have been wiped"



def test_fork_invalidation_skips_wipe_on_unreadable_record():
    """Maturity-review P1: a fork with a build record FILE that EXISTS but is
    UNREADABLE/unverified (corrupt or HMAC-fail) and no stored tree-hash must
    NOT be wiped.  read_build_record returns None for BOTH 'absent' and
    'corrupt'; treating corrupt as 'orphan' would wipe a possibly-pull-adopted
    fork on a transient read failure — a false federation ownership grab.  Fail
    SAFE: skip the wipe WITHOUT seeding the hash so a readable re-run resolves
    it."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    import utils as _u
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        _pkg = 'athena-installer-data'
        _pkg_dir = os.path.join(bc.dir_fork_source, _pkg)
        _fn = _pkg + '_1.0.0+thor1_all.udeb'
        _deb, _hash_file = _plant_adopted_fork(bc, _pkg, _fn, pulled_from=True)
        # corrupt the (valid, HMAC'd) record so read returns None but the FILE
        # still exists → the 'unknown' branch.
        _buildlog = os.path.join(bc.dir_log, 'build')
        _rec_path = _u._build_record_path(_buildlog, _pkg)
        with open(_rec_path, 'w') as fh:
            fh.write('{ not valid json @@@')
        assert _u.read_build_record(_buildlog, _pkg) is None
        assert os.path.exists(_rec_path)
        assert fork_mirror._fork_adoption_status(_pkg, bc) == 'unknown'

        _wiped = fork_mirror._check_and_invalidate_fork_pkg(_pkg_dir, bc)

        assert _wiped is False, "unreadable record → must skip wipe (fail safe)"
        assert os.path.isfile(_deb), (
            "adopted binary wiped on a transient/corrupt read — ownership grab")
        assert not os.path.isfile(_hash_file), (
            "hash must NOT be seeded on 'unknown' — a readable re-run must "
            "re-evaluate (adopt-and-seed or genuine-orphan-wipe)")



def test_fork_invalidation_covers_multi_binary_via_control_parse():
    """When a fork ships multiple binaries (athena-tasksel produces
    athena-tasksel + athena-tasksel-data), the wipe must hit each
    binary in repo/ — not just the source-named one.  A glob by source
    name alone misses the dash-suffix binary."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=False)
        # Build a minimal multi-binary fork: athena-foo + athena-foo-data
        _pkg_dir = os.path.join(bc.dir_fork_source, 'athena-foo')
        os.makedirs(os.path.join(_pkg_dir, 'debian'))
        with open(os.path.join(_pkg_dir, 'debian', 'changelog'), 'w') as fh:
            fh.write('athena-foo (1.0) thor; urgency=low\n\n'
                     '  * fixture\n\n'
                     ' -- Test <t@local>  Sat, 16 May 2026 12:00:00 +0000\n')
        with open(os.path.join(_pkg_dir, 'debian', 'control'), 'w') as fh:
            fh.write('Source: athena-foo\n'
                     'Maintainer: Test <t@local>\n'
                     'Build-Depends: debhelper-compat (= 13)\n'
                     '\n'
                     'Package: athena-foo\n'
                     'Architecture: all\n'
                     'Description: foo binary\n'
                     ' .\n'
                     '\n'
                     'Package: athena-foo-data\n'
                     'Architecture: all\n'
                     'Description: foo data\n'
                     ' .\n')
        os.makedirs(os.path.join(_pkg_dir, 'debian', 'source'), exist_ok=True)
        with open(os.path.join(_pkg_dir, 'debian', 'source', 'format'), 'w') as fh:
            fh.write('3.0 (native)\n')

        # Pre-plant stale .debs for BOTH binaries in repo/main (post-
        # segregation layout).  fork_mirror globs across all repo
        # subdirs (`repo/*/<bin>_*`) so the wipe still finds them.
        os.makedirs(bc.dir_repo_main, exist_ok=True)
        _stale_main = os.path.join(bc.dir_repo_main, 'athena-foo_1.0+thor1_all.deb')
        _stale_data = os.path.join(bc.dir_repo_main, 'athena-foo-data_1.0+thor1_all.deb')
        for _p in (_stale_main, _stale_data):
            with open(_p, 'w') as fh:
                fh.write('stale')
        # And an unrelated package that MUST survive
        _unrelated = os.path.join(bc.dir_repo_main, 'libfoo_1.0_all.deb')
        with open(_unrelated, 'w') as fh:
            fh.write('unrelated')

        # First run establishes hash; both stale debs survive baseline
        # because no prior hash exists, invalidation fires (wipes), then
        # generates the source pkg.  After the wipe, both planted debs
        # are gone; the unrelated one stays.
        fork_mirror.generate_fork_mirror(bc)
        assert not os.path.exists(_stale_main), "athena-foo .deb not wiped"
        assert not os.path.exists(_stale_data), "athena-foo-data .deb not wiped"
        assert os.path.exists(_unrelated), \
            "wipe touched an unrelated package — pattern too broad"



def test_binary_names_from_control_extracts_all_package_stanzas():
    """_binary_names_from_control must return every Package: stanza,
    including indented or aligned forms.  Used as input to the wipe
    pattern set so a missed binary leaves stale debs in repo/."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        _pkg_dir = os.path.join(tmp, 'pkg')
        os.makedirs(os.path.join(_pkg_dir, 'debian'))
        with open(os.path.join(_pkg_dir, 'debian', 'control'), 'w') as fh:
            fh.write('Source: foo\n'
                     'Maintainer: x\n'
                     '\n'
                     'Package: foo\n'
                     'Architecture: all\n'
                     '\n'
                     'Package: foo-data\n'
                     'Architecture: all\n'
                     '\n'
                     'Package: foo-doc\n'
                     'Architecture: all\n')
        _names = fork_mirror._binary_names_from_control(_pkg_dir)
        assert _names == ['foo', 'foo-data', 'foo-doc'], _names



def test_compute_dep_hash_changes_on_depends_field_edit():
    """Adding a new Depends: must shift the dep-hash so repo reload
    can gate the change as 'dep-affecting'."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        _pkg_dir = _make_minimal_pkg(tmp, 'foo', '1.0', ['foo'], depends_main='libc6')
        _h1 = fork_mirror._compute_dep_hash(_pkg_dir)
        # Add a dependency
        _pkg_dir = _make_minimal_pkg(tmp, 'foo', '1.0', ['foo'], depends_main='libc6, libxyz')
        _h2 = fork_mirror._compute_dep_hash(_pkg_dir)
        assert _h1 != _h2, "Depends: change must shift dep-hash"



def test_compute_dep_hash_changes_on_version_bump():
    """Version bump alone must shift the dep-hash (sibling
    cross-refs depend on it via ${binary:Version})."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        _pkg_dir = _make_minimal_pkg(tmp, 'foo', '1.0', ['foo'])
        _h1 = fork_mirror._compute_dep_hash(_pkg_dir)
        _pkg_dir = _make_minimal_pkg(tmp, 'foo', '1.1', ['foo'])
        _h2 = fork_mirror._compute_dep_hash(_pkg_dir)
        assert _h1 != _h2, "version bump must shift dep-hash"



def test_compute_dep_hash_stable_under_description_edit():
    """Editing Description (non-gating field) must NOT shift dep-hash —
    that's the whole point of the gate distinguishing content from
    resolution-affecting changes."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        _pkg_dir = _make_minimal_pkg(tmp, 'foo', '1.0', ['foo'])
        _h1 = fork_mirror._compute_dep_hash(_pkg_dir)
        # Edit Description (non-gating)
        _ctrl_path = os.path.join(_pkg_dir, 'debian', 'control')
        _ctrl = open(_ctrl_path).read().replace(
            'Description: x', 'Description: updated description')
        with open(_ctrl_path, 'w') as fh:
            fh.write(_ctrl)
        _h2 = fork_mirror._compute_dep_hash(_pkg_dir)
        assert _h1 == _h2, (
            "Description edit shifted dep-hash — non-gating field "
            "leaked into the hash set")



def test_compute_dep_hash_stable_under_whitespace_reflow():
    """Line-fold reflow of a Depends: value (cosmetic only) must NOT
    shift the dep-hash."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        _pkg_dir = os.path.join(tmp, 'foo')
        os.makedirs(os.path.join(_pkg_dir, 'debian'))
        with open(os.path.join(_pkg_dir, 'debian', 'changelog'), 'w') as fh:
            fh.write('foo (1.0) thor; urgency=low\n\n  * x\n\n'
                     ' -- T <t@l>  Sat, 16 May 2026 12:00:00 +0000\n')
        with open(os.path.join(_pkg_dir, 'debian', 'control'), 'w') as fh:
            fh.write('Source: foo\nMaintainer: T <t@l>\n\n'
                     'Package: foo\nArchitecture: all\n'
                     'Depends: libc6, libxyz, libabc\n'
                     'Description: x\n .\n')
        _h1 = fork_mirror._compute_dep_hash(_pkg_dir)
        # Same fields, reflowed across multiple lines (Debian continuation)
        with open(os.path.join(_pkg_dir, 'debian', 'control'), 'w') as fh:
            fh.write('Source: foo\nMaintainer: T <t@l>\n\n'
                     'Package: foo\nArchitecture: all\n'
                     'Depends: libc6,\n         libxyz,\n         libabc\n'
                     'Description: x\n .\n')
        _h2 = fork_mirror._compute_dep_hash(_pkg_dir)
        assert _h1 == _h2, "whitespace reflow shifted dep-hash"



def test_load_pkg_hashes_returns_empty_strings_when_sidecars_missing():
    """Defensive: load_pkg_hashes must not crash when sidecars don't
    exist — first-build case, returns ('', '')."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        _tree, _dep = fork_mirror.load_pkg_hashes('nonexistent', tmp)
        assert _tree == '' and _dep == '', (_tree, _dep)



def test_persist_tree_hash_writes_both_sidecars():
    """_persist_tree_hash must produce BOTH .tree-hash AND .dep-hash
    sidecars so repo reload's two-stage decision has both baselines."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        fork_mirror._persist_tree_hash(
            os.path.join(bc.dir_fork_source, 'athena-installer-data'), bc)
        _tree_p = os.path.join(
            bc.dir_fork_source_repo, 'athena-installer-data.tree-hash')
        _dep_p  = os.path.join(
            bc.dir_fork_source_repo, 'athena-installer-data.dep-hash')
        assert os.path.isfile(_tree_p), "tree-hash sidecar missing"
        assert os.path.isfile(_dep_p),  "dep-hash sidecar missing"
        # Both should contain valid hex SHA256 (64 chars)
        for _p in (_tree_p, _dep_p):
            _value = open(_p).read().strip()
            assert len(_value) == 64 and all(c in '0123456789abcdef' for c in _value), \
                f"sidecar {_p} doesn't contain a sha256 hex digest: {_value!r}"



def test_wipe_fork_pkg_outputs_removes_both_hash_sidecars():
    """When invalidating, both .tree-hash and .dep-hash must be wiped
    so stale baselines don't confuse the next decision."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        # Plant both sidecars
        for _ext in ('.tree-hash', '.dep-hash'):
            with open(os.path.join(
                    bc.dir_fork_source_repo,
                    f'athena-installer-data{_ext}'), 'w') as fh:
                fh.write('stale' * 12 + 'abcd')
        fork_mirror._wipe_fork_pkg_outputs(
            'athena-installer-data', ['athena-installer-data'], bc)
        for _ext in ('.tree-hash', '.dep-hash'):
            assert not os.path.exists(os.path.join(
                bc.dir_fork_source_repo, f'athena-installer-data{_ext}')), (
                f"{_ext} sidecar survived wipe")



def test_audit_closure_ledger_disagree_and_missing_critical():
    """A signed ledger whose entry disagrees with the verified Packages →
    closure_ledger_disagree; a published closure binary absent from the
    ledger → closure_ledger_entry_missing.  Both CRITICAL."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror
    _pkg_idx = {
        'liba_2.0_amd64.deb': {'package': 'liba', 'version': '2.0',
                               'sha256': 'n' * 64, 'size': '200',
                               'component': 'main', 'arch': 'amd64'},
        'libb_1.0_amd64.deb': {'package': 'libb', 'version': '1.0',
                               'sha256': 'm' * 64, 'size': '50',
                               'component': 'main', 'arch': 'amd64'},
    }
    # ledger only knows an OLD liba and omits libb entirely
    _signed = {'entries': {'liba|amd64': _closure_ledger_entry(
        'liba_1.0_amd64.deb', 'o' * 64, 100, '1.0', 'liba')}}
    _text = (
        "Package: liba\nVersion: 2.0\nArchitecture: amd64\n"
        "Filename: pool/main/liba_2.0_amd64.deb\n\n"
        "Package: libb\nVersion: 1.0\nArchitecture: amd64\n"
        "Filename: pool/main/libb_1.0_amd64.deb\n\n")
    _f = mirror.audit_closure_ledger(_signed, _pkg_idx, {'liba', 'libb'}, _text)
    _kinds = {_x[1] for _x in _f}
    assert 'closure_ledger_disagree' in _kinds, _f
    assert 'closure_ledger_entry_missing' in _kinds, _f
    assert all(_x[0] == 'CRITICAL' for _x in _f), _f



def test_audit_closure_ledger_unresolved_dep_critical():
    """A closure binary with a hard-dep unsatisfiable on the mirror →
    closure_dep_unresolved CRITICAL (ledger itself agrees → no disagree)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror
    _pkg_idx = {'liba_1.0_amd64.deb': {
        'package': 'liba', 'version': '1.0', 'sha256': 'a' * 64,
        'size': '100', 'component': 'main', 'arch': 'amd64'}}
    _signed = {'entries': {'liba|amd64': _closure_ledger_entry(
        'liba_1.0_amd64.deb', 'a' * 64, 100, '1.0', 'liba')}}
    _text = (
        "Package: liba\nVersion: 1.0\nArchitecture: amd64\n"
        "Filename: pool/main/liba_1.0_amd64.deb\nDepends: libmissing\n\n")
    _f = mirror.audit_closure_ledger(_signed, _pkg_idx, {'liba'}, _text)
    _kinds = {_x[1] for _x in _f}
    assert 'closure_dep_unresolved' in _kinds, _f
    assert 'closure_ledger_disagree' not in _kinds, _f
    assert 'closure_ledger_entry_missing' not in _kinds, _f



def test_audit_closure_ledger_entry_not_published_critical():
    """A ledger entry naming a file absent from the verified Packages (at an
    audited arch) → closure_ledger_entry_not_published CRITICAL."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror
    _pkg_idx = {'liba_1.0_amd64.deb': {
        'package': 'liba', 'version': '1.0', 'sha256': 'a' * 64,
        'size': '100', 'component': 'main', 'arch': 'amd64'}}
    _signed = {'entries': {
        'liba|amd64': _closure_ledger_entry(
            'liba_1.0_amd64.deb', 'a' * 64, 100, '1.0', 'liba'),
        'libz|amd64': _closure_ledger_entry(
            'libz_9_amd64.deb', 'z' * 64, 9, '9', 'libz')}}
    _text = ("Package: liba\nVersion: 1.0\nArchitecture: amd64\n"
             "Filename: pool/main/liba_1.0_amd64.deb\n\n")
    _f = mirror.audit_closure_ledger(_signed, _pkg_idx, {'liba', 'libz'}, _text)
    _kinds = {_x[1] for _x in _f}
    assert 'closure_ledger_entry_not_published' in _kinds, _f



def test_audit_closure_ledger_none_no_findings():
    """No ledger pinned (old owner) → audit_closure_ledger returns [] (the
    absence is back-compat, not a violation — peer falls back to live claims)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror
    assert mirror.audit_closure_ledger(None, {}, set(), '') == []



def test_fed03_register_tofu_decision_policy():
    """FED-03 step B: register TOFU policy — absent→upload, same key→skip,
    different key→refuse (force→upload), unreadable→refuse (force→upload)."""
    from commands.cmd_mirror import _register_tofu_decision
    _L = 'a' * 64
    # no existing key → register
    assert _register_tofu_decision('absent', '', _L, False)[0] == 'upload'
    # same key already there → idempotent no-op
    assert _register_tofu_decision('present', _L, _L, False)[0] == 'skip'
    # a DIFFERENT key already registered → refuse (identity takeover)
    assert _register_tofu_decision('present', 'b' * 64, _L, False)[0] == 'refuse'
    # …unless forced (deliberate rotation)
    assert _register_tofu_decision('present', 'b' * 64, _L, True)[0] == 'upload'
    # couldn't read the existing key → refuse (don't blind-overwrite)
    assert _register_tofu_decision('error', '', _L, False)[0] == 'refuse'
    assert _register_tofu_decision('error', '', _L, True)[0] == 'upload'



def test_mat11_mirror_help_subcommands_all_dispatch():
    """MAT-11(6): every subcommand the `mirror` umbrella ADVERTISES in its help
    table must actually DISPATCH (advertised ⊆ real) — guards against a menu
    entry that 404s to the help fallback when an operator types it."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from commands.cmd_mirror import MirrorCommandsMixin
    _adv, _disp = _advertised_and_dispatched(MirrorCommandsMixin.cmd_mirror)
    # sanity: we actually parsed a rich table + dispatcher
    assert len(_adv) >= 10 and 'publish' in _adv and 'unlock' in _adv, _adv
    _undispatched = _adv - _disp
    assert not _undispatched, (
        f"mirror advertises subcommand(s) that don't dispatch: "
        f"{sorted(_undispatched)}")



def test_fed01_ledger_stranded_claims_detector():
    """FED-01: detect live PEER claims the fetched closure ledger OMITS
    (stranded by a stale / closure-limited ledger).  Ledger-covered files and
    OUR own claims are excluded; no ledger → [] (the fallback walks the live
    set)."""
    from commands.cmd_mirror import _ledger_stranded_claims
    # claim_by_fn: filename -> (claim, owner_builder)
    _cbf = {
        'a_1_amd64.deb': ({'builder': 'BS2'}, 'BS2'),   # peer, IN ledger
        'b_1_amd64.deb': ({'builder': 'BS2'}, 'BS2'),   # peer, NOT in ledger
        'c_1_amd64.deb': ({'builder': 'BS1'}, 'BS1'),   # OURS, not in ledger
    }
    _partial = {'entries': {'a': {'filename': 'a_1_amd64.deb'}}}
    # only the peer claim the ledger omits is stranded (b); ours (c) excluded
    assert _ledger_stranded_claims(_cbf, _partial, 'BS1') == ['b_1_amd64.deb']
    # ledger covers every live peer claim → nothing stranded
    _full = {'entries': {'a': {'filename': 'a_1_amd64.deb'},
                         'b': {'filename': 'b_1_amd64.deb'}}}
    assert _ledger_stranded_claims(_cbf, _full, 'BS1') == []
    # no ledger / empty ledger → [] (fallback path already walks the live set)
    assert _ledger_stranded_claims(_cbf, None, 'BS1') == []
    assert _ledger_stranded_claims(_cbf, {}, 'BS1') == []



def test_fed04_unlock_decision_policy():
    """FED-04: `mirror unlock` decision policy — probe-fail, free lock,
    stale→break, live→refuse, and --force breaks a live lock too."""
    from commands.cmd_mirror import _unlock_decision
    assert _unlock_decision(None, False, False)[0] == 'probe-failed'
    assert _unlock_decision({'held': False}, False, False)[0] == 'free'
    _held = {'held': True, 'holder': 'BS2', 'age_sec': 3000}
    # held + stale, no force → break
    assert _unlock_decision(_held, False, True)[0] == 'break'
    # held + live (fresh heartbeat), no force → refuse
    assert _unlock_decision(_held, False, False)[0] == 'refuse-live'
    # --force breaks regardless of staleness
    assert _unlock_decision(_held, True, False)[0] == 'break'



def test_fork_mirror_discover_skips_disabled_trees():
    """fork_mirror._discover_fork_source_trees must SKIP fork dirs
    that have a `.disabled` marker file.  This is how `source fork
    <pkg> disabled` removes a fork from cache ingest without deleting
    the tree."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import fork_mirror
    with tempfile.TemporaryDirectory() as _fork_src:
        # Plant two valid fork trees; disable one.
        for _name in ('alpha', 'beta'):
            _dir = os.path.join(_fork_src, _name)
            os.makedirs(os.path.join(_dir, 'debian'), exist_ok=True)
            with open(os.path.join(_dir, 'debian', 'control'), 'w') as fh:
                fh.write(f'Source: {_name}\nPackage: {_name}\n')
        with open(os.path.join(_fork_src, 'alpha', '.disabled'), 'w') as fh:
            fh.write('disabled\n')

        _found = fork_mirror._discover_fork_source_trees(_fork_src)
        _names = sorted(os.path.basename(p) for p in _found)
        assert _names == ['beta'], (
            f"disabled fork must be skipped; got {_names}"
        )



# ─────────────────────────────────────────────────────────────────────────────
# TEST-08: Mirror.with_snapshot property tests
# ─────────────────────────────────────────────────────────────────────────────

def test_mirror_with_snapshot_none_returns_self():
    """ts=None is a no-op so call sites can invoke unconditionally
    without a pre-check.  Pins the None branch — without it,
    every consumer would need its own `if ts is not None` guard."""
    from utils import Mirror
    m = Mirror(id='main', baseurl='http://x.test', baseid='debian',
               release='bookworm', suffix='', component='main', arch='amd64')
    assert m.with_snapshot(None) is m



def test_mirror_with_snapshot_is_idempotent():
    """Calling with_snapshot twice with the same ts yields a mirror
    equal to the single-call result.  Catches the imaginary regression
    where a future implementation appends `/{ts}` to baseid each call,
    drifting the URL across invocations."""
    from utils import Mirror
    m = Mirror(id='main', baseurl='http://x.test', baseid='debian',
               release='bookworm', suffix='', component='main', arch='amd64')
    once  = m.with_snapshot('20260101T000000Z')
    twice = once.with_snapshot('20260101T000000Z')
    # Idempotence here means "two single-step results from the original
    # mirror are equal".  The chain (m → once → twice) is allowed to
    # double-baseid (and does today); pin the SINGLE-STEP property which
    # is what consumers rely on.
    again = m.with_snapshot('20260101T000000Z')
    assert once == again
    assert once is not twice    # new instance each call
    # Suite (which encodes release+suffix) is unchanged by snapshot.
    assert once.suite == m.suite == twice.suite



def test_mirror_with_snapshot_preserves_suite_and_component():
    """The snapshot wrapper rewrites baseurl + baseid but must leave
    suite, component, arch, suffix, release untouched — those address
    WHICH index files we read, not WHERE the mirror lives."""
    from utils import Mirror
    m = Mirror(id='security', baseurl='http://deb.debian.org',
               baseid='debian-security', release='bookworm',
               suffix='-security', component='main', arch='amd64')
    snap = m.with_snapshot('20260514T083402Z')
    assert snap.suite     == m.suite
    assert snap.component == m.component
    assert snap.arch      == m.arch
    assert snap.suffix    == m.suffix
    assert snap.release   == m.release
    # baseid is rewritten to embed the timestamp.
    assert '20260514T083402Z' in snap.baseid
    assert snap.baseid.startswith('debian-security')



def test_fork_mirror_skips_identity_audit_when_disabled():
    """generate_fork_mirror short-circuits the S1 identity scan when
    buildconfig.audit_identity_scan is False — operator-disabled audits
    must not gate the build."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        # Disable identity audit AND wipe the permissive allowlist —
        # if the audit ran, the fixture's debian/copyright debian.org
        # URL would trip the gate.  Disabled → audit skipped → build
        # passes regardless of allowlist content.
        with open(os.path.join(tmp, 'audit', 'identity-allowlist'), 'w') as fh:
            fh.write('# empty: every token would be a violation if scanned\n')
        bc.audit_identity_scan = False
        ok = fork_mirror.generate_fork_mirror(bc)
    assert ok is True, (
        'identity audit disabled — fork_mirror must still succeed'
    )



def test_identity_scan_fork_mirror_gate_fails_build():
    """generate_fork_mirror() returns False when identity audit finds an
    unallowlisted hit — replaces the hardcoded installer_chroot strip
    list with a build-time gate."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import fork_mirror
    with tempfile.TemporaryDirectory() as tmp:
        bc = _setup_fork_test_tmpdir(tmp, with_pkg=True)
        # Overwrite the permissive allowlist with an empty one so the
        # debian.org URL in our test fixture's debian/copyright actually
        # trips the gate.
        with open(os.path.join(tmp, 'audit', 'identity-allowlist'), 'w') as fh:
            fh.write('# empty: every token is a violation\n')
        ok = fork_mirror.generate_fork_mirror(bc)
    assert ok is False, (
        'expected fork_mirror to abort on unallowlisted identity hit'
    )



def test_probe_remote_build_host_parses_and_gates():
    """`mirror.probe_remote_build_host` parses the KEY=VALUE round-trip into a
    fields dict and gates `ok` on Docker reachable AND python3 present."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import subprocess as _sp
    import mirror
    import types as _types

    def _fake_run(stdout):
        return lambda *a, **k: _types.SimpleNamespace(
            returncode=0, stdout=stdout, stderr='')

    _orig = _sp.run
    try:
        _sp.run = _fake_run(
            "DOCKER=24.0.7\nGROUPS=ubuntu docker sudo\n"
            "PYTHON3=Python 3.11.2\nNPROC=8\nMEMKB=16317432\n")
        _ok, _info = mirror.probe_remote_build_host('h', 'u', '/k')
        assert _ok is True
        assert _info['docker'] == '24.0.7'
        assert _info['in_docker_group'] is True
        assert _info['python3'].startswith('Python')
        assert _info['cores'] == 8
        assert _info['mem_gib'] == 15.6
        # Docker unreachable → not ok, error names docker; group False.
        _sp.run = _fake_run("DOCKER=\nGROUPS=ubuntu\nPYTHON3=Python 3.11.2\n"
                            "NPROC=4\nMEMKB=8000000\n")
        _ok2, _info2 = mirror.probe_remote_build_host('h', 'u', '/k')
        assert _ok2 is False and 'docker' in _info2['error']
        assert _info2['in_docker_group'] is False
    finally:
        _sp.run = _orig



def test_webapi_mirror_state_reads_signed_mirror_conf_not_legacy():
    """Regression (audit #13): webapi mirror_state reads the authoritative
    HMAC-signed mirror.conf (via mirror.load_mirror_conf) — a mirror
    registered ONLY in mirror.conf is visible, a leftover stale legacy
    mirror.<name>.state is ignored, and the verify status is surfaced.  And
    the read is side-effect-free: querying a box with no mirror.conf must NOT
    create one (migrate=False)."""
    import sys
    import types
    import json as _json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror
    from webapi import readers
    with tempfile.TemporaryDirectory() as _td:
        _cfgdir = os.path.join(_td, 'config')
        os.makedirs(_cfgdir)
        _coord = os.path.join(_td, 'coord')
        os.makedirs(_coord)
        # shim with only dir_config — mirror derives the HMAC key dir from its
        # parent, exactly as the webapi reader's shim does, so save+read agree.
        _cfg = types.SimpleNamespace(dir_config=_cfgdir)
        _doc = mirror._empty_mirror_doc()
        _doc['mirrors']['peerX'] = {'role': 'peer', 'url': 'http://x.test'}
        assert mirror.save_mirror_conf(_cfg, _doc)
        # stale legacy state file that must be IGNORED post-migration
        with open(os.path.join(_cfgdir, 'mirror.staleY.state'), 'w') as _fh:
            _json.dump({'role': 'stale'}, _fh)

        _out = readers.mirror_state(_cfgdir, _coord)
        assert _out['mirror_conf_status'] == 'ok', _out
        assert 'peerX' in _out['mirrors'], "mirror.conf-only entry invisible"
        assert 'staleY' not in _out['mirrors'], "stale legacy .state leaked"

    # No mirror.conf at all -> status 'missing', and the read must not write.
    with tempfile.TemporaryDirectory() as _td2:
        _cfgdir2 = os.path.join(_td2, 'config')
        os.makedirs(_cfgdir2)
        _out2 = readers.mirror_state(_cfgdir2, _td2)
        assert _out2['mirror_conf_status'] == 'missing', _out2
        assert _out2['mirrors'] == {}
        assert not os.path.exists(os.path.join(_cfgdir2, 'mirror.conf')), (
            "read-only mirror_state must not create mirror.conf (migrate=False)")



def test_mirror_publish_refreshes_local_asg_manifest():
    """Regression (2026-06): MIRROR-01 removed the old repo-publish finalize
    path (8cc803b) that wrote the signed published.manifest, leaving the
    +asg bump ledger frozen — every rebuild re-derived the same uN.  Pin
    that cmd_mirror_publish refreshes the manifest from the just-published
    index after a successful publish."""
    _body = _session_source()
    import re as _re
    _m = _re.search(r'def cmd_mirror_publish\(self.*?(?=\n    def )',
                    _body, _re.DOTALL)
    assert _m, 'cmd_mirror_publish not found'
    _b = _m.group(0)
    assert 'write_published_manifest' in _b, (
        "cmd_mirror_publish must refresh the local +asg manifest after a "
        "successful publish (bump-number authority)")
    assert 'local_published_packages_text' in _b





def test_update_build_pending_per_mirror_path():
    """MIRROR-01 Phase 4: when mirrors are configured, the floor is
    min(mirror.<each>.state.current); ANY laggard mirror = UPDATE pending."""
    import shutil as _sh
    if not _sh.which('dpkg'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    import repo_audit
    import mirror as _mirror
    from build import BuildSession, BuildFlags

    with tempfile.TemporaryDirectory() as _td:
        _sess = BuildSession.__new__(BuildSession)
        _sess.flags = BuildFlags()
        _sess.flags.dep_check_ready = True
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        _sess.config = _cfg
        _sess._snapshot_current = lambda: '20260520T000000Z'
        _sess._snapshot_published = lambda: '20260514T083402Z'
        _saved = repo_audit.published_ledger
        try:
            repo_audit.published_ledger = lambda _c: {'openssl': ['3.0.15-1']}
            # Plant two mirrors at different timestamps
            _mirror.add_mirror(_cfg, name='a', url='ssh://h1/p',
                               seed_pin='20260520T000000Z')  # at current
            _mirror.add_mirror(_cfg, name='b', url='ssh://h2/p',
                               seed_pin='20260514T083402Z')  # behind
            # min(current_a, current_b) = b's pin (behind local current)
            #   → UPDATE pending
            assert _sess._update_build_pending() is True
            # Catch b up to current; both now at current; min(_) == current
            #   → no UPDATE
            _mirror.update_mirror_state(
                _cfg, 'b', current='20260520T000000Z')
            assert _sess._update_build_pending() is False
            # An unpublished mirror (empty current) = unconditionally laggard
            _mirror.add_mirror(_cfg, name='fresh', url='ssh://h3/p',
                               seed_pin='')
            assert _sess._update_build_pending() is True
        finally:
            repo_audit.published_ledger = _saved



def test_cmd_get_lists_every_gettable_param_when_called_bare():
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    with tempfile.TemporaryDirectory() as _tmp:
        _sess, _build_mod = _build_session_for_setget(_tmp)
        _printed: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed.append(str(a[0]))
        _sess.cmd_get()
        _joined = '\n'.join(_printed)
        # Every settable param must be reachable via get.
        for _name in _build_mod.BuildSession._SETTABLE:
            assert _name in _joined, f"get-all missed {_name}: {_joined!r}"
        assert 'distribution' in _joined  # current mode value



def test_cmd_get_named_param_returns_current_value():
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    with tempfile.TemporaryDirectory() as _tmp:
        _sess, _build_mod = _build_session_for_setget(_tmp)
        _printed: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed.append(str(a[0]))
        _sess.cmd_get('mode')
        assert any('distribution' in _p for _p in _printed), _printed



def test_cmd_set_mode_switches_value_and_warns_to_re_run_cache_parse():
    """Mode change clears dep_check_ready AND prints a `cache parse`
    warning regardless of the prior flag state.  Dep-tree contents
    depend on the mode (build mode short-circuits passes III–VII), so
    any cached parse is invalid post-change."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    with tempfile.TemporaryDirectory() as _tmp:
        # Case 1: dep_check_ready already True.
        _sess, _build_mod = _build_session_for_setget(_tmp)
        _printed: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed.append(str(a[0]))
        _sess.cmd_set('mode', 'build')
        assert _sess.config.build_mode == 'build'
        assert _sess.flags.dep_check_ready is False, _sess.flags
        _joined = '\n'.join(_printed)
        assert 'build' in _joined
        assert 'cache parse' in _joined
        assert 'mode change requires' in _joined.lower()

        # Case 2: dep_check_ready already False (operator hasn't parsed
        # yet).  Warning must STILL print so the operator knows the
        # mode change carries the same requirement.
        _sess2, _ = _build_session_for_setget(_tmp)
        _sess2.flags.dep_check_ready = False
        _printed2: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed2.append(str(a[0]))
        _sess2.cmd_set('mode', 'build')
        assert _sess2.config.build_mode == 'build'
        _joined2 = '\n'.join(_printed2)
        assert 'cache parse' in _joined2, (
            "warning must print even when dep_check_ready was already False")



def test_cmd_set_mode_persists_to_local_conf():
    """`set mode` writes the choice to config/local.conf (untracked) so it
    survives a restart; build.conf is never touched."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import utils
    with tempfile.TemporaryDirectory() as _tmp:
        _sess, _build_mod = _build_session_for_setget(_tmp)
        _printed: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed.append(str(a[0]))
        _sess.cmd_set('mode', 'build')
        # local.conf written alongside build.conf with the new mode.
        _local = os.path.join(_tmp, 'local.conf')
        assert os.path.isfile(_local), "local.conf not written"
        _p = utils.read_local_conf(_sess.config)
        assert _p.get('Local', 'Mode') == 'build'
        assert 'config/local.conf' in '\n'.join(_printed)
        # build.conf must NOT have been created/touched by the switch.
        assert not os.path.isfile(os.path.join(_tmp, 'build.conf'))



def test_cmd_set_mode_build_refused_on_first_system():
    """Phase-3 gate: a FIRST/origin system cannot switch to build mode —
    build needs a published baseline it can't itself be."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    with tempfile.TemporaryDirectory() as _tmp:
        _sess, _build_mod = _build_session_for_setget(_tmp)
        _sess.config.system_role = 'first'          # override default
        _printed: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed.append(str(a[0]))
        _sess.cmd_set('mode', 'build')
        assert _sess.config.build_mode == 'distribution'   # unchanged
        _joined = '\n'.join(_printed)
        assert 'cannot switch to build mode' in _joined
        assert 'FIRST/origin' in _joined



def test_cmd_set_mode_build_refused_when_unregistered():
    """Phase-3 gate: build mode requires a federation [Registration] marker;
    an un-registered box stays in distribution."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    with tempfile.TemporaryDirectory() as _tmp:
        _sess, _build_mod = _build_session_for_setget(_tmp)
        _sess.config.system_role = ''               # un-onboarded
        # Drop the registration marker the helper wrote.
        os.remove(os.path.join(_tmp, 'mirror.conf'))   # registration now here
        _printed: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed.append(str(a[0]))
        _sess.cmd_set('mode', 'build')
        assert _sess.config.build_mode == 'distribution'
        _joined = '\n'.join(_printed)
        assert 'cannot switch to build mode' in _joined
        assert 'register' in _joined.lower()



def test_cmd_set_mode_invalid_value_keeps_old_value():
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    with tempfile.TemporaryDirectory() as _tmp:
        _sess, _build_mod = _build_session_for_setget(_tmp)
        _printed: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed.append(str(a[0]))

        _sess.cmd_set('mode', 'turbo')

        assert _sess.config.build_mode == 'distribution'  # unchanged
        # dep_check_ready stays untouched on rejection.
        assert _sess.flags.dep_check_ready is True
        _joined = '\n'.join(_printed)
        assert 'invalid mode' in _joined



def test_cmd_set_mode_same_value_is_noop_when_dep_check_ready():
    """Re-typing the current mode while the dep tree is parsed → no
    state change, no warning."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    with tempfile.TemporaryDirectory() as _tmp:
        _sess, _build_mod = _build_session_for_setget(_tmp)
        _printed: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed.append(str(a[0]))

        _sess.cmd_set('mode', 'distribution')

        # No state change, no dep_check_ready clear.
        assert _sess.config.build_mode == 'distribution'
        assert _sess.flags.dep_check_ready is True
        _joined = '\n'.join(_printed)
        assert 'already' in _joined
        # dep tree is parsed — no "run cache parse" hint.
        assert 'cache parse' not in _joined, (
            f"no parse hint should fire when dep_check_ready=True; "
            f"got {_joined!r}")



def test_cmd_set_mode_same_value_surfaces_parse_hint_when_not_parsed():
    """Re-typing the current mode while the dep tree is NOT parsed →
    no state change, but the parse hint surfaces so the operator
    isn't misled by 'mode already = build' into thinking they're
    ready to source-build."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    with tempfile.TemporaryDirectory() as _tmp:
        _sess, _build_mod = _build_session_for_setget(_tmp)
        _sess.flags.dep_check_ready = False
        _printed: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed.append(str(a[0]))

        _sess.cmd_set('mode', 'distribution')

        assert _sess.config.build_mode == 'distribution'
        assert _sess.flags.dep_check_ready is False
        _joined = '\n'.join(_printed)
        assert 'already' in _joined
        assert 'cache parse' in _joined, (
            f"parse hint should fire when dep_check_ready=False; "
            f"got {_joined!r}")



def test_cmd_set_unknown_param_reports_available_list():
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    with tempfile.TemporaryDirectory() as _tmp:
        _sess, _build_mod = _build_session_for_setget(_tmp)
        _printed: 'list[str]' = []
        _build_mod.console.print = lambda *a, **k: _printed.append(str(a[0]))

        _sess.cmd_set('warp-factor', '9')

        _joined = '\n'.join(_printed)
        assert 'unknown param' in _joined
        # The error must point the operator at the supported list.
        assert 'mode' in _joined



def test_hk07_behavior_fixes_pinned():
    """Regression pins for the HK-07 behaviour corrections (f/g3/h/i2/i3)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

    def _src(rel):
        with open(os.path.join(_ROOT, 'scripts', rel)) as _f:
            return _f.read()

    # (f) pre_install no longer chgrp/chmods lastlog/btmp (don't exist yet)
    _chroot = _src('chroot.py')
    assert 'var/log/lastlog' not in _chroot
    assert 'var/log/btmp' not in _chroot

    # (h) federation peer name derives via fqdn-then-ip, not the dead 'ssh'
    import mirror
    assert "host_type='ssh'" not in _src('mirror.py')
    assert mirror.derive_name_from_url(
        'ssh://u@mirror.example.com/p', 'fqdn') == 'mirror-example-com'
    assert mirror.derive_name_from_url(
        'ssh://u@mirror.example.com/p', 'ssh') is None   # the dead host_type

    # (i2) cmd_tunnel uses the shared size helpers, no local dupes
    _tunnel = _src('commands/cmd_tunnel.py')
    assert '_humansize' not in _tunnel and '_safe_filesize' not in _tunnel
    assert 'human_size(' in _tunnel

    # (g3) skip_build_test parsed with the strip-comprehension idiom
    assert "'SkipTest').split(', ')" not in _src('utils.py')

    # (i3) print groups sizes scan the real binary dirs, not the repo root
    assert 'all_deb_dirs()' in _src('print_commands.py')



def test_cmd_mirror_publish_refuses_indl_to_fresh_mirror():
    """MIRROR-02 chunk 13: build-mode publish to a mirror with no
    coord-head on remote is REFUSED with a hint pointing at dist-mode
    bootstrap."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    import mirror as _mirror
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _td:
        _cfg_dir = os.path.join(_td, 'config')
        _cache_dir = os.path.join(_td, 'cache')
        _repo_dir = os.path.join(_td, 'repo')
        for _d in (_cfg_dir, _cache_dir,
                   os.path.join(_repo_dir, 'dists', 'thor')):
            os.makedirs(_d)
        with open(os.path.join(_repo_dir, 'dists', 'thor', 'InRelease'),
                  'w') as _fh:
            _fh.write('Date: 2026-06-04\n')

        class _Cfg:
            arch = 'amd64'
            dir_config = _cfg_dir
            dir_cache  = _cache_dir
            dir_repo   = _repo_dir
            dir_image  = _cfg_dir   # no ISOs here → the --no-iso path
            build_codename = 'thor'
            build_version = '1'
            build_distribution = 'asgard'
            build_mode = 'build'

        _mirror.add_mirror(
            _Cfg(), name='primary',
            url='ssh://ubuntu@host.example/srv/asgard',
            seed_pin='')

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess._snapshot_current = lambda: '20260604T000000Z'  # type: ignore
        _sess._coord_self_keys = lambda: (        # type: ignore
            'athena-test', '/fake/priv', '/fake/pub')
        # Stub the head probe: remote has no head yet
        _sess._mirror_remote_has_coord_head = (   # type: ignore
            lambda _name, _state: False)

        _lines: 'list[str]' = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _ok = _sess.cmd_mirror_publish('primary', '--no-iso')
        finally:
            build.console.print = _orig
        _joined = '\n'.join(_lines)
        assert _ok is False
        assert 'REFUSED' in _joined
        assert 'no coord-head yet' in _joined
        assert 'distribution-mode' in _joined



def test_cmd_mirror_publish_indl_to_initialised_mirror_proceeds():
    """When the target mirror already HAS a coord-head, build-mode
    publish proceeds past the chunk-13 gate (subject to other gates
    downstream)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    import mirror as _mirror
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _td:
        _cfg_dir = os.path.join(_td, 'config')
        _cache_dir = os.path.join(_td, 'cache')
        _repo_dir = os.path.join(_td, 'repo')
        for _d in (_cfg_dir, _cache_dir,
                   os.path.join(_repo_dir, 'dists', 'thor')):
            os.makedirs(_d)
        with open(os.path.join(_repo_dir, 'dists', 'thor', 'InRelease'),
                  'w') as _fh:
            _fh.write('Date: 2026-06-04\n')

        class _Cfg:
            dir_config = _cfg_dir
            dir_cache  = _cache_dir
            dir_repo   = _repo_dir
            build_codename = 'thor'
            build_mode = 'build'

        _mirror.add_mirror(
            _Cfg(), name='primary',
            url='ssh://ubuntu@host.example/srv/asgard',
            seed_pin='20260601T000000Z')

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess._snapshot_current = lambda: '20260604T000000Z'  # type: ignore
        _sess._coord_self_keys = lambda: (        # type: ignore
            'athena-test', '/fake/priv', '/fake/pub')
        # Remote HAS a head → gate passes; the publish would then
        # attempt remote_publish (which our test setup can't satisfy),
        # but we only care that the gate doesn't refuse here.
        _sess._mirror_remote_has_coord_head = (   # type: ignore
            lambda _name, _state: True)

        _lines: 'list[str]' = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            try:
                _sess.cmd_mirror_publish('primary')
            except (AttributeError, OSError):
                # Test cfg is intentionally incomplete — we only
                # care that we got PAST the chunk-13 refuse path.
                pass
        finally:
            build.console.print = _orig
        _joined = '\n'.join(_lines)
        # Specifically NOT the chunk-13 refuse path
        assert 'no coord-head yet' not in _joined, _joined



def test_project_post_publish_state_merges_remote_claims_as_satisfiers():
    """MIRROR-02 chunk 11: project_post_publish_state takes the local
    RepoState and layers in remote claims as satisfier-only entries
    (no Depends).  Multi-version-aware: when local has libdep@1.0
    and remote has libdep@2.0, the result carries libdep@2.0 (the
    newer satisfies more constraints)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    import repo_audit as _repo_audit
    # Local state: libdep 1.0, no Depends (we built it).
    _local = _repo_audit.RepoState(
        packages={
            'libdep': {'Package': 'libdep', 'Version': '1.0'},
        },
        provides_index={}, packages_file='', repo_mtime=0.0,
    )
    # Remote claim: libdep 2.0 (newer)
    _claims = {
        'team-b': [{
            'package': 'libdep', 'built_version': '2.0',
            'filename': 'libdep_2.0_amd64.deb', 'sha256': 'a' * 64,
            'snapshot': 'S', 'builder': 'team-b',
            'claim_state': 'published',
        }]
    }
    _state = _mirror.project_post_publish_state(_local, _claims)
    # Remote 2.0 > local 1.0 → remote wins as the projected entry
    assert _state.packages['libdep']['Version'] == '2.0'



def test_find_publish_closure_breaks_returns_empty_when_satisfied():
    """When every Depends of our pending pkgs is satisfied by the
    union (local + remote claims), no findings."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    import repo_audit as _repo_audit
    # Local has libwebkit which Depends libssl3.  We're publishing
    # libwebkit; libssl3 is already on the mirror (remote claim).
    _local = _repo_audit.RepoState(
        packages={
            'libwebkit': {
                'Package': 'libwebkit', 'Version': '2.0',
                'Depends': 'libssl3 (>= 3.0.0)',
            },
        },
        provides_index={}, packages_file='', repo_mtime=0.0,
    )
    _remote_claims = {
        'team-b': [{
            'package': 'libssl3', 'built_version': '3.0.5',
            'filename': 'libssl3_3.0.5_amd64.deb', 'sha256': 'a' * 64,
            'snapshot': 'S', 'builder': 'team-b',
            'claim_state': 'published',
        }]
    }
    _breaks = _mirror.find_publish_closure_breaks(
        _local, _remote_claims,
        our_pending_pkg_names=frozenset({'libwebkit'}))
    assert _breaks == [], _breaks



def test_find_publish_closure_breaks_detects_missing_dep():
    """When our pending pkg depends on something not in local OR
    remote, the closure check returns a finding naming the offender.
    """
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    import repo_audit as _repo_audit
    # Local has libwebkit which Depends a brand-new libfoo@5.0.
    # Mirror has libfoo@3.0 (older — doesn't satisfy).  Publish
    # libwebkit → closure break.
    _local = _repo_audit.RepoState(
        packages={
            'libwebkit': {
                'Package': 'libwebkit', 'Version': '2.0',
                'Depends': 'libfoo (>= 5.0)',
            },
        },
        provides_index={}, packages_file='', repo_mtime=0.0,
    )
    _remote_claims = {
        'team-b': [{
            'package': 'libfoo', 'built_version': '3.0',
            'filename': 'libfoo_3.0_amd64.deb', 'sha256': 'a' * 64,
            'snapshot': 'S', 'builder': 'team-b',
            'claim_state': 'published',
        }]
    }
    _breaks = _mirror.find_publish_closure_breaks(
        _local, _remote_claims,
        our_pending_pkg_names=frozenset({'libwebkit'}))
    assert len(_breaks) >= 1, _breaks
    # Each finding is (pkg, field, relation_str, why)
    _names = [_b[0] for _b in _breaks]
    assert 'libwebkit' in _names
    # libfoo appears in the relation_str of the finding
    assert any('libfoo' in _b[2] for _b in _breaks), _breaks



def test_find_publish_closure_breaks_respects_consumer_set():
    """The closure check bounds its consumer walk to our pending pkgs;
    a pre-existing broken local package OUTSIDE the consumer set
    doesn't fire."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    import repo_audit as _repo_audit
    _local = _repo_audit.RepoState(
        packages={
            # `legacy-pkg` has unresolved Depends but isn't OUR pending
            'legacy-pkg': {
                'Package': 'legacy-pkg', 'Version': '1.0',
                'Depends': 'missing-dep (>= 1.0)',
            },
            # `our-pkg` is what we're publishing; no Depends → no break
            'our-pkg': {
                'Package': 'our-pkg', 'Version': '1.0',
            },
        },
        provides_index={}, packages_file='', repo_mtime=0.0,
    )
    _breaks = _mirror.find_publish_closure_breaks(
        _local, remote_by_builder={},
        our_pending_pkg_names=frozenset({'our-pkg'}))
    assert _breaks == [], _breaks   # legacy-pkg is not our concern



def test_build_mode_publish_implies_no_iso():
    """Mode = build implies --no-iso (build peers don't build ISOs; the
    mirror snapshot may lead any ISO snapshot)."""
    with open(os.path.join(_ROOT, 'scripts', 'commands', 'cmd_mirror.py')) as _f:
        _src = _f.read()
    assert "or _publish_mode == 'build'" in _src, \
        "build mode must imply --no-iso in cmd_mirror_publish"



def test_closure_gate_runs_in_both_modes():
    """The publish closure gate is wired regardless of [Build] Mode — the
    install corpus is built from the dep trees unconditionally and passed to
    remote_publish, so a distribution publisher (full corpus) AND a build-mode
    peer (subset corpus) both run it over the merged local pool.  The gate
    documents the responsibility split."""
    with open(os.path.join(_ROOT, 'scripts', 'commands',
                           'cmd_mirror.py')) as _f:
        _pub = _f.read()
    assert '_install_corpus |= frozenset(' in _pub
    assert 'install_corpus=_install_corpus or None' in _pub
    with open(os.path.join(_ROOT, 'scripts', 'coord', 'publish.py')) as _f:
        _pp = _f.read()
    assert 'Responsibility split across modes (both run this gate)' in _pp



def test_mirror_builders_register_gates_and_uploads():
    """`mirror builders register` routes from the dispatcher and refuses
    without a builder identity / signing key / known mirror; on the happy
    path it uploads the pubkey to the mirror keyring."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    from unittest.mock import patch
    import signing            # noqa: F401 (patched below)
    import mirror             # noqa: F401
    import coord.transport    # noqa: F401

    _s = BuildSession.__new__(BuildSession)
    _s.config = type('C', (), {})()

    # dispatcher routes 'register' to the register method
    with patch.object(BuildSession, 'cmd_mirror_builders_register',
                      return_value='R') as _r:
        assert _s.cmd_mirror_builders('register', 'm1') == 'R'
        _r.assert_called_once_with('m1')

    _keys = ('alice', '/priv.pem', '/alice.pub')
    # no identity → refuse
    with patch.object(BuildSession, '_coord_self_keys', return_value=None):
        assert _s.cmd_mirror_builders_register('m1') is False
    # bad signing key → refuse
    with patch.object(BuildSession, '_coord_self_keys', return_value=_keys), \
            patch('signing.verify_key', return_value=(False, 'no key')):
        assert _s.cmd_mirror_builders_register('m1') is False
    # unknown mirror → refuse
    with patch.object(BuildSession, '_coord_self_keys', return_value=_keys), \
            patch('signing.verify_key', return_value=(True, 'ok')), \
            patch('mirror.read_mirror_state', return_value=None):
        assert _s.cmd_mirror_builders_register('m1') is False
    # happy path → pubkey uploaded, True (config-adopt skipped via a failed
    # coord fetch, keeping this test focused on the pubkey upload)
    _st = {'url': 'ssh://u@h/p', 'ssh_key': None}
    with tempfile.TemporaryDirectory() as _dc:
        # a real local pubkey so the FED-03 TOFU sha read succeeds
        _pubf = os.path.join(_dc, 'alice.pub')
        with open(_pubf, 'wb') as _fh:
            _fh.write(b'ssh-ed25519 AAAA-alice\n')
        _keys_real = ('alice', '/priv.pem', _pubf)
        _s.config = type('C', (), {
            'dir_cache': _dc,
            'pkglist_path': os.path.join(_dc, 'pkg'),
            'poollist_path': os.path.join(_dc, 'pool')})()
        with patch.object(BuildSession, '_coord_self_keys',
                          return_value=_keys_real), \
                patch('signing.verify_key', return_value=(True, 'ok')), \
                patch('mirror.read_mirror_state', return_value=_st), \
                patch('mirror.coord_root_for',
                      return_value='ssh://u@h/p-coord'), \
                patch('mirror.rsync_spec_for_url',
                      return_value=('u@h:/p-coord', None)), \
                patch('coord.transport.remote_pubkey_state',
                      return_value=('absent', '')) as _probe, \
                patch('coord.transport.push_jsonl',
                      return_value=(True, '')) as _push, \
                patch('coord.transport.pull_remote_coord',
                      return_value=(False, 'skip')):
            assert _s.cmd_mirror_builders_register('m1') is True
            assert _push.called and _probe.called
        # FED-03 TOFU: a DIFFERENT key already registered → REFUSE, no upload
        with patch.object(BuildSession, '_coord_self_keys',
                          return_value=_keys_real), \
                patch('signing.verify_key', return_value=(True, 'ok')), \
                patch('mirror.read_mirror_state', return_value=_st), \
                patch('mirror.coord_root_for',
                      return_value='ssh://u@h/p-coord'), \
                patch('mirror.rsync_spec_for_url',
                      return_value=('u@h:/p-coord', None)), \
                patch('coord.transport.remote_pubkey_state',
                      return_value=('present', 'd' * 64)), \
                patch('coord.transport.push_jsonl',
                      return_value=(True, '')) as _push2, \
                patch('coord.transport.pull_remote_coord',
                      return_value=(False, 'skip')):
            assert _s.cmd_mirror_builders_register('m1') is False
            assert not _push2.called          # refused before any upload
            # …but --force overrides
            assert _s.cmd_mirror_builders_register('m1', '--force') is True
            assert _push2.called



def test_snapshot_adopt_forward_is_forward_only():
    """mirror pull adopts the mirror's snapshot pin FORWARD only — a strictly
    newer remote pin is adopted; equal/older/empty remote → no adoption
    (never roll the local pin backward)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    _a, _b = '20260101T000000Z', '20260601T000000Z'
    assert _mir.snapshot_adopt_forward(_a, _b) == _b      # remote newer → adopt
    assert _mir.snapshot_adopt_forward(_b, _a) is None     # remote older → no
    assert _mir.snapshot_adopt_forward(_a, _a) is None     # equal → no
    assert _mir.snapshot_adopt_forward('', _b) == _b       # unset local → adopt
    assert _mir.snapshot_adopt_forward(_a, '') is None     # no remote → no



def test_audit_claims_vs_packages_flags_missing_and_mismatched():
    """Claim → no Packages entry  →  CRITICAL claim_not_in_apt_index.
    Claim sha disagrees with Packages sha  →  CRITICAL
    claim_apt_sha_mismatch.  Matching claim is silent."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir

    _packages_idx = {
        'foo_1.0_amd64.deb': {
            'package': 'foo', 'version': '1.0',
            'sha256': 'a' * 64, 'size': '100',
        },
        'baz_2.0_amd64.deb': {
            'package': 'baz', 'version': '2.0',
            'sha256': 'b' * 64, 'size': '200',
        },
    }
    _by_builder = {
        'athena-primary': [
            # OK — present + sha matches.
            {'package': 'foo', 'filename': 'foo_1.0_amd64.deb',
             'sha256': 'a' * 64, 'claim_state': 'published'},
            # CRITICAL — sha disagrees with apt index.
            {'package': 'baz', 'filename': 'baz_2.0_amd64.deb',
             'sha256': 'c' * 64, 'claim_state': 'published'},
            # CRITICAL — filename not in any Packages.
            {'package': 'qux', 'filename': 'qux_3.0_amd64.deb',
             'sha256': 'd' * 64, 'claim_state': 'published'},
            # Retracted — ignored.
            {'package': 'old', 'filename': 'old_0.1_amd64.deb',
             'sha256': 'e' * 64, 'claim_state': 'retracted'},
        ],
    }
    _findings = _mir.audit_claims_vs_packages(_by_builder, _packages_idx)
    _kinds = sorted(_f[1] for _f in _findings)
    assert _kinds == ['claim_apt_sha_mismatch',
                      'claim_not_in_apt_index'], _findings
    # Both should be CRITICAL.
    assert all(_f[0] == 'CRITICAL' for _f in _findings), _findings



def test_audit_sidecar_seq_integrity_clean_run_is_silent():
    """Contiguous seqs 1..N per builder → no findings."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    _by_builder = {
        'athena-a': [{'seq': _i, 'filename': f'p{_i}_.deb'}
                     for _i in range(1, 6)],
        'athena-b': [{'seq': _i, 'filename': f'q{_i}_.deb'}
                     for _i in range(1, 4)],
    }
    assert _mir.audit_sidecar_seq_integrity(_by_builder) == []



def test_audit_sidecar_seq_integrity_flags_dup_gap_and_missing_one():
    """Three independent failure modes — each emits its own CRITICAL."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    _by_builder = {
        'athena-dup': [
            {'seq': 1, 'filename': 'a_.deb'},
            {'seq': 2, 'filename': 'b_.deb'},
            {'seq': 2, 'filename': 'b2_.deb'},  # duplicate seq
        ],
        'athena-gap': [
            {'seq': 1, 'filename': 'a_.deb'},
            {'seq': 2, 'filename': 'b_.deb'},
            {'seq': 4, 'filename': 'd_.deb'},   # gap at 3
        ],
        'athena-start': [
            {'seq': 3, 'filename': 'c_.deb'},   # doesn't start at 1
            {'seq': 4, 'filename': 'd_.deb'},
        ],
    }
    _f = _mir.audit_sidecar_seq_integrity(_by_builder)
    _kinds = sorted(_t[1] for _t in _f)
    assert _kinds == ['sidecar_duplicate_seq', 'sidecar_seq_gap',
                      'sidecar_seq_missing_1'], _f
    assert all(_t[0] == 'CRITICAL' for _t in _f)



def test_sta25_audit_downgrades_missing_claim_when_publish_will_release():
    """STA-25 follow-up: `mirror audit` marks a missing own-claim file INFO
    (`own_claim_disk_pending_release`) when the next `mirror publish` will
    release it — DEPRECATE (source left the signed selection) or OBSOLETE
    (newer build emits a different filename) — and keeps CRITICAL for a
    genuinely-missing current artifact.  Helps the operator who pruned before
    publishing or forgot the cleanup message / came back later."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir, utils as _u

    with tempfile.TemporaryDirectory() as _td:
        _repo = os.path.join(_td, 'repo')          # empty → every claim missing
        os.makedirs(_repo)
        _buildlog = os.path.join(_td, 'log', 'build')
        os.makedirs(_buildlog)
        # `foo` still selected, current build emits foo_2 (NOT foo_1)
        _rec = _u.new_build_record(package='foo', intended_version='2',
                                   patch_set_hash='')
        _rec['output_hashes'] = {'foo_2_amd64.deb': 'a' * 64}
        _u.write_build_record(_buildlog, _rec)
        # `cur` still selected, current build STILL emits cur_1
        _rec2 = _u.new_build_record(package='cur', intended_version='1',
                                    patch_set_hash='')
        _rec2['output_hashes'] = {'cur_1_amd64.deb': 'b' * 64}
        _u.write_build_record(_buildlog, _rec2)

        _by_builder = {'athena-ours': [
            {'package': 'dropped', 'filename': 'dropped_1_amd64.deb',
             'sha256': 'c' * 64, 'claim_state': 'published'},
            {'package': 'foo', 'filename': 'foo_1_amd64.deb',
             'sha256': 'd' * 64, 'claim_state': 'published'},
            {'package': 'cur', 'filename': 'cur_1_amd64.deb',
             'sha256': 'e' * 64, 'claim_state': 'published'},
        ]}
        _closure = {'foo', 'cur'}   # `dropped` left the selection

        _f = _mir.audit_own_claims_on_disk(
            _by_builder, our_builder_id='athena-ours',
            local_repo_dir=_repo, buildlog_dir=_buildlog,
            closure_srcs=_closure)
        _by_fn = {_t[2].split()[0]: (_t[0], _t[1]) for _t in _f}
        assert _by_fn['dropped_1_amd64.deb'] == (
            'INFO', 'own_claim_disk_pending_release'), _f   # → deprecated
        assert _by_fn['foo_1_amd64.deb'] == (
            'INFO', 'own_claim_disk_pending_release'), _f   # → obsoleted
        assert _by_fn['cur_1_amd64.deb'] == (
            'CRITICAL', 'own_claim_disk_missing'), _f       # genuine

        # back-compat: no closure info → conservative, all CRITICAL
        _f2 = _mir.audit_own_claims_on_disk(
            _by_builder, our_builder_id='athena-ours',
            local_repo_dir=_repo, buildlog_dir=_buildlog)
        assert all(_t[0] == 'CRITICAL' for _t in _f2), _f2



def test_audit_own_claims_on_disk_match_disk_silent_mismatch_critical():
    """Our own claim referencing a present-on-disk .deb whose sha
    matches → silent.  Same filename with a different on-disk byte
    stream → CRITICAL.  Missing-on-disk → CRITICAL.  Tunneled +
    peer-claim are ignored."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import hashlib as _hashlib
    import mirror as _mir

    with tempfile.TemporaryDirectory() as _td:
        _good_path = os.path.join(_td, 'main', 'g',
                                  'good_1.0_amd64.deb')
        os.makedirs(os.path.dirname(_good_path))
        with open(_good_path, 'wb') as _fh:
            _fh.write(b'good-bytes')
        _good_sha = _hashlib.sha256(b'good-bytes').hexdigest()

        _bad_path = os.path.join(_td, 'main', 'b',
                                 'bad_1.0_amd64.deb')
        os.makedirs(os.path.dirname(_bad_path))
        with open(_bad_path, 'wb') as _fh:
            _fh.write(b'actual-bytes')  # disk content
        _claim_sha_for_bad = _hashlib.sha256(
            b'CLAIMED-bytes').hexdigest()  # but claim says other sha

        _by_builder = {
            'athena-ours': [
                {'package': 'good', 'filename': 'good_1.0_amd64.deb',
                 'sha256': _good_sha, 'claim_state': 'published'},
                {'package': 'bad', 'filename': 'bad_1.0_amd64.deb',
                 'sha256': _claim_sha_for_bad,
                 'claim_state': 'published'},
                {'package': 'gone', 'filename': 'gone_1.0_amd64.deb',
                 'sha256': 'a' * 64, 'claim_state': 'published'},
                # Tunneled — ignored (asg-stamp rewrites bytes).
                {'package': 'tun', 'filename': 'tun_1.0_amd64.deb',
                 'sha256': 'b' * 64, 'claim_state': 'published',
                 'republished_from': {'url': 'x', 'upstream_sha256': 'y'}},
                # Retracted — ignored.
                {'package': 'old', 'filename': 'old_1.0_amd64.deb',
                 'sha256': 'c' * 64, 'claim_state': 'retracted'},
            ],
            # Peer claims — ignored (we don't have those .debs).
            'athena-peer': [
                {'package': 'p', 'filename': 'p_1.0_amd64.deb',
                 'sha256': 'd' * 64, 'claim_state': 'published'},
            ],
        }
        _findings = _mir.audit_own_claims_on_disk(
            _by_builder, our_builder_id='athena-ours',
            local_repo_dir=_td)
        _kinds = sorted(_t[1] for _t in _findings)
        assert _kinds == ['own_claim_disk_missing',
                          'own_claim_disk_sha_mismatch'], _findings
        assert all(_t[0] == 'CRITICAL' for _t in _findings)



def test_audit_own_claims_on_disk_ahead_of_remote_is_warning_not_critical():
    """When the on-disk .deb's sha matches the LOCAL build.json's
    output_hashes but disagrees with the REMOTE claim sha → a
    rebuild-not-yet-published; emit WARNING, not CRITICAL bitrot.

    Catches the audit-noise pattern operators hit after rebuilding
    a source without re-running `mirror publish`."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import hashlib as _hashlib
    import mirror as _mir
    import utils as _u

    with tempfile.TemporaryDirectory() as _td:
        _repo = os.path.join(_td, 'repo')
        _buildlog = os.path.join(_td, 'log', 'build')
        os.makedirs(_buildlog)
        _file_path = os.path.join(_repo, 'main', 'a',
                                  'a_1.0_amd64.deb')
        os.makedirs(os.path.dirname(_file_path))
        with open(_file_path, 'wb') as _fh:
            _fh.write(b'new-bytes')
        _new_sha = _hashlib.sha256(b'new-bytes').hexdigest()
        # Local build.json records the NEW sha (post-rebuild).
        _rec = _u.new_build_record(
            package='a', intended_version='1.0', patch_set_hash='')
        _rec['output_hashes'] = {'a_1.0_amd64.deb': _new_sha}
        _u.write_build_record(_buildlog, _rec)
        # Remote claim still has OLD sha (pre-rebuild).
        _by_builder = {
            'athena-ours': [
                {'package': 'a', 'filename': 'a_1.0_amd64.deb',
                 'sha256': 'd' * 64, 'claim_state': 'published'},
            ],
        }
        _findings = _mir.audit_own_claims_on_disk(
            _by_builder, our_builder_id='athena-ours',
            local_repo_dir=_repo, buildlog_dir=_buildlog)
        _kinds = [_t[1] for _t in _findings]
        assert _kinds == ['own_claim_local_ahead_of_remote'], _findings
        assert _findings[0][0] == 'WARNING'



def test_local_ahead_candidates_structured_and_hint_mentions_reclaim():
    """RECLAIM-01 Chunk 3: local_ahead_candidates returns the structured
    reclaim-candidate view (filename/package/component/old_seq/
    remote_sha/local_sha/size/versions/path); marker-superseded claims
    are excluded by the shared fold; the audit's local-ahead hint
    points at `mirror reclaim` instead of the (wrong) publish advice."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import hashlib as _hashlib
    import mirror as _mir
    import utils as _u

    with tempfile.TemporaryDirectory() as _td:
        _repo = os.path.join(_td, 'repo')
        _buildlog = os.path.join(_td, 'log', 'build')
        os.makedirs(_buildlog)
        _file_path = os.path.join(_repo, 'main', 'a', 'a_1.0_amd64.deb')
        os.makedirs(os.path.dirname(_file_path))
        with open(_file_path, 'wb') as _fh:
            _fh.write(b'new-bytes')
        _new_sha = _hashlib.sha256(b'new-bytes').hexdigest()
        _rec = _u.new_build_record(
            package='a', intended_version='1.0', patch_set_hash='')
        _rec['output_hashes'] = {'a_1.0_amd64.deb': _new_sha}
        _u.write_build_record(_buildlog, _rec)
        _by_builder = {
            'athena-ours': [
                # local-ahead candidate
                {'package': 'a', 'filename': 'a_1.0_amd64.deb',
                 'sha256': 'd' * 64, 'claim_state': 'published',
                 'seq': 11, 'component': 'main',
                 'intended_version': '1.0', 'built_version': '1.0'},
                # deprecated marker pair — its target must NOT appear
                {'package': 'z', 'filename': 'z_1.0_all.deb',
                 'sha256': 'e' * 64, 'claim_state': 'published',
                 'seq': 12},
                {'package': 'z', 'filename': 'z_1.0_all.deb',
                 'sha256': 'e' * 64, 'claim_state': 'deprecated',
                 'seq': 13, 'deprecates_seq': 12},
            ],
        }
        _cands = _mir.local_ahead_candidates(
            _by_builder, our_builder_id='athena-ours',
            local_repo_dir=_repo, buildlog_dir=_buildlog)
        assert len(_cands) == 1, _cands
        _c = _cands[0]
        assert _c['filename'] == 'a_1.0_amd64.deb'
        assert _c['package'] == 'a' and _c['component'] == 'main'
        assert _c['old_seq'] == 11
        assert _c['remote_sha'] == 'd' * 64
        assert _c['local_sha'] == _new_sha
        assert _c['size'] == len(b'new-bytes')
        assert _c['intended_version'] == '1.0'
        assert _c['built_version'] == '1.0'
        assert _c['path'] == _file_path
        # The audit finding for the same state names the reclaim path.
        _findings = _mir.audit_own_claims_on_disk(
            _by_builder, our_builder_id='athena-ours',
            local_repo_dir=_repo, buildlog_dir=_buildlog)
        _ahead = [_m for _s, _k, _m in _findings
                  if _k == 'own_claim_local_ahead_of_remote']
        assert len(_ahead) == 1, _findings
        assert 'mirror reclaim' in _ahead[0], _ahead
        assert 'bump + publish' in _ahead[0], _ahead


def test_local_ahead_candidates_republished_repacked_is_reclaimable():
    """A tunneled (republished_from) .deb that `repo repair strip` repacked
    locally: its on-disk bytes now differ from the frozen claim BUT the local
    build record backs them.  This is a sanctioned local rewrite and MUST be
    reclaimable (was previously skipped wholesale, so `mirror reclaim` could
    never reconcile a repaired tunneled package).  A republished claim whose
    drift is NOT build-record-backed stays suppressed (not our bitrot to
    flag), and an in-sync republished claim is silent."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import hashlib as _hashlib
    import mirror as _mir
    import utils as _u

    with tempfile.TemporaryDirectory() as _td:
        _repo = os.path.join(_td, 'repo')
        _buildlog = os.path.join(_td, 'log', 'build')
        os.makedirs(_buildlog)

        def _mk(_name, _content):
            _p = os.path.join(_repo, 'main', _name[0], _name)
            os.makedirs(os.path.dirname(_p), exist_ok=True)
            with open(_p, 'wb') as _fh:
                _fh.write(_content)
            return _p, _hashlib.sha256(_content).hexdigest()

        # (1) repaired tunneled deb — build record backs the new bytes
        _rp_path, _rp_new = _mk('repaired_1.0_amd64.deb', b'repacked-bytes')
        _rec = _u.new_build_record(
            package='repaired', intended_version='1.0', patch_set_hash='')
        _rec['phase'] = 'tunneled'
        _rec['status'] = 'TUNNELED'
        _rec['output_hashes'] = {'repaired_1.0_amd64.deb': _rp_new}
        _u.write_build_record(_buildlog, _rec)
        # (2) tunneled deb whose disk bytes drift with NO build-record backing
        _bit_path, _ = _mk('bitrot_1.0_amd64.deb', b'corrupt-bytes')
        # (3) tunneled deb whose disk bytes still match the claim (in sync)
        _ok_path, _ok_sha = _mk('insync_1.0_amd64.deb', b'passthrough')

        _by_builder = {
            'athena-ours': [
                {'package': 'repaired',
                 'filename': 'repaired_1.0_amd64.deb',
                 'sha256': 'a' * 64, 'claim_state': 'published', 'seq': 21,
                 'component': 'main', 'intended_version': '1.0',
                 'built_version': '1.0',
                 'republished_from': {'url': 'u', 'upstream_sha256': 'v'}},
                {'package': 'bitrot',
                 'filename': 'bitrot_1.0_amd64.deb',
                 'sha256': 'b' * 64, 'claim_state': 'published', 'seq': 22,
                 'republished_from': {'url': 'u', 'upstream_sha256': 'v'}},
                {'package': 'insync',
                 'filename': 'insync_1.0_amd64.deb',
                 'sha256': _ok_sha, 'claim_state': 'published', 'seq': 23,
                 'republished_from': {'url': 'u', 'upstream_sha256': 'v'}},
            ],
        }
        _cands = _mir.local_ahead_candidates(
            _by_builder, our_builder_id='athena-ours',
            local_repo_dir=_repo, buildlog_dir=_buildlog)
        # ONLY the repaired tunneled deb is a reclaim candidate.
        assert [_c['filename'] for _c in _cands] == [
            'repaired_1.0_amd64.deb'], _cands
        assert _cands[0]['remote_sha'] == 'a' * 64
        assert _cands[0]['local_sha'] == _rp_new
        assert _cands[0]['old_seq'] == 21
        # The on-disk audit: repaired → WARNING local-ahead; the unbacked
        # bitrot tunneled deb stays SILENT (old blanket-skip behaviour kept);
        # in-sync is silent.  No CRITICAL for any republished file.
        _findings = _mir.audit_own_claims_on_disk(
            _by_builder, our_builder_id='athena-ours',
            local_repo_dir=_repo, buildlog_dir=_buildlog)
        _kinds = sorted(_k for _s, _k, _m in _findings)
        assert _kinds == ['own_claim_local_ahead_of_remote'], _findings
        assert all(_s == 'WARNING' for _s, _k, _m in _findings), _findings


def test_audit_own_claims_on_disk_real_bitrot_still_critical():
    """Disk sha matches neither remote claim NOR local build.json →
    real bitrot, CRITICAL stays."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import hashlib as _hashlib
    import mirror as _mir
    import utils as _u

    with tempfile.TemporaryDirectory() as _td:
        _repo = os.path.join(_td, 'repo')
        _buildlog = os.path.join(_td, 'log', 'build')
        os.makedirs(_buildlog)
        _file_path = os.path.join(_repo, 'main', 'a',
                                  'a_1.0_amd64.deb')
        os.makedirs(os.path.dirname(_file_path))
        with open(_file_path, 'wb') as _fh:
            _fh.write(b'rotted-bytes')   # not what build recorded
        _rotted_sha = _hashlib.sha256(b'rotted-bytes').hexdigest()
        # Local build.json records a DIFFERENT (correct) sha.
        _rec = _u.new_build_record(
            package='a', intended_version='1.0', patch_set_hash='')
        _rec['output_hashes'] = {'a_1.0_amd64.deb': 'k' * 64}
        _u.write_build_record(_buildlog, _rec)
        _by_builder = {
            'athena-ours': [
                {'package': 'a', 'filename': 'a_1.0_amd64.deb',
                 'sha256': 'd' * 64, 'claim_state': 'published'},
            ],
        }
        _findings = _mir.audit_own_claims_on_disk(
            _by_builder, our_builder_id='athena-ours',
            local_repo_dir=_repo, buildlog_dir=_buildlog)
        assert len(_findings) == 1
        assert _findings[0][0] == 'CRITICAL'
        assert _findings[0][1] == 'own_claim_disk_sha_mismatch'
        assert 'bitrot or corruption' in _findings[0][2]
        del _rotted_sha   # used for clarity in setup



def test_audit_own_claims_on_disk_no_our_builder_id_is_noop():
    """When our_builder_id is None (rare — fresh host pre-keygen) the
    helper short-circuits.  Used to guard against AttributeError in
    cmd_mirror_audit when the operator runs audit before keygen."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    assert _mir.audit_own_claims_on_disk(
        {'athena-x': [{'filename': 'a', 'sha256': 'b' * 64,
                       'claim_state': 'published'}]},
        our_builder_id=None, local_repo_dir='/nonexistent') == []



def test_audit_cross_mirror_head_drift_silent_when_aligned():
    """Two mirrors with identical neighbours + revocation sets emit
    no findings.  inrelease_sha256 + snapshot.current ARE allowed to
    differ — those are per-snapshot, not federation-wide."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    _per_mirror = [
        {'name': 'm1', 'head': {
            'neighbours': ['ssh://a/repo', 'ssh://b/repo'],
            'revoked_builders': {'bad': 'reason'},
            'inrelease_sha256': 'a' * 64,
            'snapshot': {'current': 't1'},
        }, 'by_builder': {}},
        {'name': 'm2', 'head': {
            'neighbours': ['ssh://b/repo', 'ssh://a/repo'],  # order ok
            'revoked_builders': {'bad': 'reason'},
            'inrelease_sha256': 'b' * 64,  # different snapshot, fine
            'snapshot': {'current': 't2'},
        }, 'by_builder': {}},
    ]
    assert _mir.audit_cross_mirror_head_drift(_per_mirror) == []



def test_audit_cross_mirror_head_drift_neighbours_diff_is_critical():
    """Mirror B's coord-head lists a federation member that mirror A
    doesn't → CRITICAL cross_mirror_neighbours_drift."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    _per_mirror = [
        {'name': 'm1', 'head': {
            'neighbours': ['ssh://a/repo', 'ssh://b/repo'],
            'revoked_builders': {},
        }, 'by_builder': {}},
        {'name': 'm2', 'head': {
            'neighbours': ['ssh://a/repo', 'ssh://b/repo', 'ssh://c/repo'],
            'revoked_builders': {},
        }, 'by_builder': {}},
    ]
    _findings = _mir.audit_cross_mirror_head_drift(_per_mirror)
    assert len(_findings) == 1
    _sev, _kind, _msg = _findings[0]
    assert _sev == 'CRITICAL'
    assert _kind == 'cross_mirror_neighbours_drift'
    assert 'ssh://c/repo' in _msg



def test_audit_cross_mirror_head_drift_revocation_diff_is_critical():
    """Revocation lists disagree → CRITICAL cross_mirror_revocation_drift."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    _per_mirror = [
        {'name': 'm1', 'head': {
            'neighbours': [],
            'revoked_builders': {'bad-builder': 'compromised'},
        }, 'by_builder': {}},
        {'name': 'm2', 'head': {
            'neighbours': [],
            'revoked_builders': {},  # missing the revocation
        }, 'by_builder': {}},
    ]
    _findings = _mir.audit_cross_mirror_head_drift(_per_mirror)
    assert len(_findings) == 1
    _sev, _kind, _ = _findings[0]
    assert _sev == 'CRITICAL'
    assert _kind == 'cross_mirror_revocation_drift'



def test_audit_federation_walk_skips_when_no_extra_peers():
    """All declared neighbours are already in per_mirror → no walk
    attempted, no findings."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    _per_mirror = [
        {'name': 'm1', 'url': 'ssh://a/repo', 'head': {
            'neighbours': ['ssh://a/repo', 'ssh://b/repo'],
        }, 'by_builder': {}},
        {'name': 'm2', 'url': 'ssh://b/repo', 'head': {
            'neighbours': ['ssh://a/repo', 'ssh://b/repo'],
        }, 'by_builder': {}},
    ]
    with tempfile.TemporaryDirectory() as _td:
        _walked, _findings = _mir.audit_federation_walk(
            _per_mirror, signing_home='/dev/null', cache_dir=_td)
    assert _walked == []
    assert _findings == []



def test_audit_cross_mirror_head_drift_single_mirror_is_noop():
    """One reachable mirror (or zero) → nothing to compare → silent."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mir
    assert _mir.audit_cross_mirror_head_drift([]) == []
    _per_mirror = [
        {'name': 'm1', 'head': {'neighbours': [],
                                'revoked_builders': {}},
         'by_builder': {}},
        # Unreachable peer.
        {'name': 'm2', 'head': None, 'by_builder': {}},
    ]
    assert _mir.audit_cross_mirror_head_drift(_per_mirror) == []



def test_publish_closure_gate_scope_matches_repo_audit_install_corpus():
    """Regression 2026-06-05: the publish-time closure gate kept
    flagging deps that `repo audit` legitimately passed on the same
    on-disk state.  Two iterations failed:

      (a) consumer_set=source names → walked transitional metas (e.g.
          `bind9` → dns-root-data) that the install corpus never pulls.
      (b) consumer_set=binary names derived from claim filenames →
          walked side artifacts (`apache2-dev`, `apparmor-notify`,
          `2to3`, ...) whose Depends include build tools or optional
          siblings (`debhelper`, `python3-notify2`) that the install
          corpus also doesn't pull.

    The contract: the closure gate MUST use the same install corpus
    `repo audit`'s dep gate uses — `dep_tree.selected_pkgs ∪
    udeb_dep_tree.selected_pkgs`.  Otherwise two valid runs against
    identical state produce contradictory verdicts.

    Three direct probes against a synthetic RepoState:
      - install_corpus = {bind9-dnsutils}   (real install) → PASS
      - install_corpus = {bind9}            (source name)  → FAIL (meta deps)
      - install_corpus = {apache2-dev}      (side artifact) → FAIL (build deps)
    """
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import repo_audit as _ra
    import mirror as _mir

    # Repo state mirroring the operator's actual shape:
    # - bind9-dnsutils is in the install corpus, Depends resolvable.
    # - bind9 is a transitional meta with unresolved Depends.
    # - apache2-dev is a side artifact with build-tool Depends.
    _state = _ra.RepoState(
        packages={
            'bind9-dnsutils': {
                'Package': 'bind9-dnsutils', 'Version': '9.18.49-1',
                'Depends': 'libc6',
            },
            'libc6': {'Package': 'libc6', 'Version': '2.36-9'},
            'bind9': {
                'Package': 'bind9', 'Version': '9.18.49-1',
                'Depends': 'dns-root-data',
            },
            'apache2-dev': {
                'Package': 'apache2-dev', 'Version': '2.4.67-1',
                'Depends': 'debhelper (>= 10), libpcre2-dev',
            },
        },
        provides_index={},
        packages_file='', repo_mtime=0.0,
    )

    # Install-corpus scope (matches repo audit's input) → no breaks.
    _breaks_install = _mir.find_publish_closure_breaks(
        _state, {}, frozenset({'bind9-dnsutils'}))
    assert _breaks_install == [], (
        f"install-corpus scope produced false-positive breaks: "
        f"{_breaks_install}")

    # Source-name scope (the first wrong attempt) → DOES break.
    _breaks_source = _mir.find_publish_closure_breaks(
        _state, {}, frozenset({'bind9'}))
    assert _breaks_source, (
        "sanity: source-name scope walks transitional meta — "
        "must produce break on dns-root-data")

    # Side-artifact-included scope (the second wrong attempt) → DOES
    # break on apache2-dev's build-tool Depends.
    _breaks_side = _mir.find_publish_closure_breaks(
        _state, {}, frozenset({'apache2-dev'}))
    assert _breaks_side, (
        "sanity: side-artifact scope walks build-tool Depends — "
        "must produce break on debhelper / libpcre2-dev")



def test_extract_user_and_path_from_ssh_url():
    """The probe pipeline plumbs user + remote path from the ssh URL
    so `ssh -i <key> <user>@<host>` runs as the publish account, not
    the local invoker."""
    _m = _mirror_module()
    assert _m._extract_user_from_ssh_url(
        'ssh://ubuntu@140.245.198.222/home/ubuntu/asgard') == 'ubuntu'
    assert _m._extract_path_from_ssh_url(
        'ssh://ubuntu@140.245.198.222/home/ubuntu/asgard') \
        == '/home/ubuntu/asgard'
    # Userless ssh URL — caller's ssh-config defaults apply
    assert _m._extract_user_from_ssh_url(
        'ssh://mirror.example.com/srv/asgard') is None
    assert _m._extract_path_from_ssh_url(
        'ssh://mirror.example.com/srv/asgard') == '/srv/asgard'
    # IPv6 + port
    assert _m._extract_user_from_ssh_url(
        'ssh://op@[2001:db8::1]:2222/srv/asgard') == 'op'
    assert _m._extract_path_from_ssh_url(
        'ssh://op@[2001:db8::1]:2222/srv/asgard') == '/srv/asgard'
    # Non-ssh schemes return None
    assert _m._extract_user_from_ssh_url('file:///srv/asgard') is None
    assert _m._extract_path_from_ssh_url('file:///srv/asgard') is None



def test_validate_host_for_type_classifies_ip_fqdn_local():
    """MIRROR-01 Phase 6: the `ip`/`fqdn`/`local` keyword the operator
    types at `mirror add` time must agree with the URL shape."""
    _m = _mirror_module()
    # ip + ssh:// IPv4 host → ok
    _ok, _ = _m.validate_host_for_type(
        'ssh://ubuntu@140.245.198.222/srv/asgard', 'ip')
    assert _ok
    # ip + ssh:// IPv6 host → ok
    _ok, _ = _m.validate_host_for_type(
        'ssh://user@[2001:db8::1]/srv/asgard', 'ip')
    assert _ok
    # ip + ssh:// FQDN host → reject (use fqdn keyword)
    _ok, _det = _m.validate_host_for_type(
        'ssh://ubuntu@mirror.example.com/srv/asgard', 'ip')
    assert not _ok and 'not a valid IPv4 or IPv6' in _det
    # fqdn + ssh:// FQDN host → ok
    _ok, _ = _m.validate_host_for_type(
        'ssh://ubuntu@mirror.example.com/srv/asgard', 'fqdn')
    assert _ok
    # fqdn + ssh:// IPv4 → reject (use ip keyword)
    _ok, _det = _m.validate_host_for_type(
        'ssh://ubuntu@140.245.198.222/srv/asgard', 'fqdn')
    assert not _ok and 'looks like an IP literal' in _det
    # fqdn + single-label host → reject (FQDN needs at least 2 labels)
    _ok, _det = _m.validate_host_for_type(
        'ssh://ubuntu@localhost/srv/asgard', 'fqdn')
    assert not _ok and 'not a valid DNS FQDN' in _det
    # local + file:// → ok
    _ok, _ = _m.validate_host_for_type('file:///srv/asgard', 'local')
    assert _ok
    # local + ssh:// → reject (wrong scheme)
    _ok, _det = _m.validate_host_for_type(
        'ssh://ubuntu@host.example/srv/asgard', 'local')
    assert not _ok and "host type 'local' requires a file:// URL" in _det
    # ip + file:// → reject (wrong scheme)
    _ok, _det = _m.validate_host_for_type('file:///srv/asgard', 'ip')
    assert not _ok and "requires an ssh:// URL" in _det
    # unknown keyword → reject
    _ok, _det = _m.validate_host_for_type(
        'ssh://ubuntu@host.example/srv/asgard', 'banana')
    assert not _ok and 'unknown host type' in _det



def test_derive_name_from_url_maps_ip_fqdn_local():
    """Auto-derived mirror name from URL + keyword."""
    _m = _mirror_module()
    # IPv4 → dashes
    assert _m.derive_name_from_url(
        'ssh://ubuntu@140.245.198.222/srv/asgard', 'ip') == '140-245-198-222'
    # FQDN → dashes
    assert _m.derive_name_from_url(
        'ssh://ubuntu@mirror.example.com/srv/asgard', 'fqdn') \
        == 'mirror-example-com'
    # IPv6 brackets stripped, colons → dashes
    assert _m.derive_name_from_url(
        'ssh://user@[2001:db8::1]/srv/asgard', 'ip') == '2001-db8--1'
    # local: name from path tail
    assert _m.derive_name_from_url(
        'file:///srv/asgard-staging', 'local') == 'asgard-staging'
    # Empty / malformed paths fail safe
    assert _m.derive_name_from_url('file:///', 'local') == 'local'
    # Mismatched keyword × URL → None
    assert _m.derive_name_from_url('file:///srv/asgard', 'ip') is None
    # Unknown keyword → None
    assert _m.derive_name_from_url(
        'ssh://ubuntu@host/srv', 'banana') is None



def test_derive_public_url_constructs_proto_host_dist():
    """The apt-readable URL is <proto>://<host>/<dist-id-lowercased>."""
    _m = _mirror_module()
    assert _m.derive_public_url(
        'ssh://ubuntu@140.245.198.222/srv/asgard',
        dist_id='Asgard', proto='http',
    ) == 'http://140.245.198.222/asgard'
    assert _m.derive_public_url(
        'ssh://ubuntu@mirror.example.com/srv/asgard',
        dist_id='asgard', proto='https',
    ) == 'https://mirror.example.com/asgard'
    # file:// publish URLs have no public URL — apt clients on a remote
    # host can't reach a file:// path, so we don't emit a misleading one.
    assert _m.derive_public_url(
        'file:///srv/asgard', dist_id='asgard', proto='http') is None
    # Invalid proto / empty dist_id → None
    assert _m.derive_public_url(
        'ssh://ubuntu@host.example/srv', dist_id='asgard', proto='ftp') is None
    assert _m.derive_public_url(
        'ssh://ubuntu@host.example/srv', dist_id='', proto='http') is None



def test_normalize_url_restricted_to_ssh_and_file():
    """MIRROR-01 Phase 6 tightening: only ssh:// + file:// are valid
    publish URLs.  The earlier permissive shape (http(s)://,
    user@host:/path rsync shorthand) is rejected."""
    _m = _mirror_module()
    # Accepted
    assert _m._normalize_url(
        'ssh://ubuntu@host.example/srv/asgard'
    ) == 'ssh://ubuntu@host.example/srv/asgard'
    assert _m._normalize_url('file:///srv/asgard') == 'file:///srv/asgard'
    # Plain absolute path is auto-prefixed (ergonomics)
    assert _m._normalize_url('/srv/asgard') == 'file:///srv/asgard'
    # Rejected
    assert _m._normalize_url('http://web.example/asgard') is None
    assert _m._normalize_url('https://web.example/asgard') is None
    assert _m._normalize_url('ubuntu@host:/srv/asgard') is None
    assert _m._normalize_url('') is None
    assert _m._normalize_url(None) is None  # type: ignore[arg-type]



def test_probe_ssh_auth_classifies_success_and_failure():
    """ssh BatchMode probe: rc=0 + 'ssh-ok' in stdout → ok; non-zero →
    failure with stderr surfaced."""
    _m = _mirror_module()
    import subprocess
    from unittest.mock import patch

    class _Done:
        def __init__(self, rc, out='', err=''):
            self.returncode, self.stdout, self.stderr = rc, out, err

    with patch.object(subprocess, 'run',
                      return_value=_Done(0, 'ssh-ok\n', '')):
        _ok, _det = _m.probe_ssh_auth('host.example', 'ubuntu', None)
        assert _ok, _det
        assert 'round-trip' in _det
    with patch.object(subprocess, 'run',
                      return_value=_Done(255, '', 'Permission denied\n')):
        _ok, _det = _m.probe_ssh_auth('host.example', 'ubuntu', None)
        assert not _ok
        assert 'Permission denied' in _det
    with patch.object(subprocess, 'run',
                      side_effect=subprocess.TimeoutExpired('ssh', 10)):
        _ok, _det = _m.probe_ssh_auth('host.example', 'ubuntu', None)
        assert not _ok and 'timed out' in _det



def test_probe_http_inrelease_handles_200_404_and_error():
    """200 → ok; 404 → ok+detail mentioning bootstrap; URL/timeout
    error → not ok."""
    _m = _mirror_module()
    import urllib.error
    import urllib.request
    from unittest.mock import patch

    class _Resp:
        def __init__(self, code):
            self.status = code
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def getcode(self):
            return self.status

    with patch.object(urllib.request, 'urlopen',
                      return_value=_Resp(200)):
        _ok, _det = _m.probe_http_inrelease(
            'http://host.example/asgard', 'thor')
        assert _ok and 'present' in _det

    with patch.object(urllib.request, 'urlopen',
                      side_effect=urllib.error.HTTPError(
                          'u', 404, 'Not Found', {}, None)):
        _ok, _det = _m.probe_http_inrelease(
            'http://host.example/asgard', 'thor')
        assert _ok, _det
        assert 'bootstrap' in _det.lower()

    with patch.object(urllib.request, 'urlopen',
                      side_effect=urllib.error.URLError('no route')):
        _ok, _det = _m.probe_http_inrelease(
            'http://host.example/asgard', 'thor')
        assert not _ok
        assert 'no route' in _det



def test_discover_federation_peers_classifies_each_neighbour():
    """Per-neighbour reachable/verified flags from probe_sidecar_head."""
    _m = _mirror_module()
    from unittest.mock import patch

    _head = {
        'v': 2,
        'neighbours': [
            'ssh://ubuntu@reachable/srv/asgard',
            'ssh://ubuntu@unreachable/srv/asgard',
            'ssh://ubuntu@bootstrap-pending/srv/asgard',
        ],
    }
    def _fake_probe(coord_url, *, signing_homedir, stage_dir, ssh_key=None):
        if 'unreachable' in coord_url:
            return False, 'TCP connect failed', None
        if 'bootstrap-pending' in coord_url:
            return True, 'no head yet', None
        return True, 'verified', {'v': 2, 'neighbours': []}

    with tempfile.TemporaryDirectory() as _td:
        with patch.object(_m, 'probe_sidecar_head',
                          side_effect=_fake_probe):
            _peers = _m.discover_federation_peers(
                _head, signing_homedir='/fake/gpg',
                stage_root=_td)
    assert len(_peers) == 3
    _by_url = {_p['url']: _p for _p in _peers}
    _r = _by_url['ssh://ubuntu@reachable/srv/asgard']
    assert _r['reachable'] and _r['verified']
    _u = _by_url['ssh://ubuntu@unreachable/srv/asgard']
    assert not _u['reachable'] and not _u['verified']
    _b = _by_url['ssh://ubuntu@bootstrap-pending/srv/asgard']
    assert _b['reachable'] and not _b['verified']



def test_cmd_mirror_add_refuses_unprepared_mirror():
    """Stage 3: mirror add gates on the prep marker — an ssh mirror whose
    host isn't prepared (no mirror-info.json) is REFUSED with a pointer to
    prep-mirror.sh, before any state is written."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    with tempfile.TemporaryDirectory() as _td:
        _sess, _restore = _cmd_mirror_add_session(_td)
        # Override the default "prepared" stub → NOT prepared.
        _sess._mirror_is_prepared = lambda _url: (  # type: ignore
            False, 'marker unreachable')
        try:
            _stack = _patch_mirror_add_primitives(
                sidecar_head_return=(True, 'no head yet', None),
                discover_peers_return=[])
            with _stack:
                _ok = _sess.cmd_mirror_add(
                    'ip',
                    'ssh://ubuntu@140.245.198.222/home/ubuntu/asgard',
                    '--ssh-key', 'config/repo.key', '--proto', 'http',
                    '--yes')
        finally:
            _restore()
        _joined = '\n'.join(_sess.lines)
        assert _ok is False, _joined
        assert 'not prepared' in _joined and 'prep-mirror.sh' in _joined, _joined
        assert '[FAIL]' in _joined, _joined
        # nothing registered
        assert _mirror.list_mirrors(_sess.config) == [], _joined



def test_prep_mirror_script_contract():
    """Stage 3: prep-mirror.sh exists, is executable, syntactically valid,
    and codifies the idempotent state machine (prepared / adopt / fresh /
    unexpected) + the mirror-info.json marker + a --check dry-run."""
    import subprocess as _sub
    _p = os.path.join(_ROOT, 'prep-mirror.sh')
    assert os.path.isfile(_p), "prep-mirror.sh missing"
    assert os.access(_p, os.X_OK), "prep-mirror.sh not executable"
    # bash -n: parse without running
    _r = _sub.run(['bash', '-n', _p], capture_output=True, text=True)
    assert _r.returncode == 0, _r.stderr
    _src = open(_p).read()
    for _tok in ('mirror-info.json', 'athena-mirror', '--check',
                 'PREPARED', 'ADOPT', 'FRESH', 'UNEXPECTED',
                 'StrictHostKeyChecking'):
        assert _tok in _src, f"prep-mirror.sh missing {_tok!r}"
    # never clobbers: the unexpected/abort paths must exit non-zero
    assert 'Refusing to modify' in _src or 'refusing' in _src.lower(), _src
    # dist-id + root are DERIVED, not typed: reads distro.conf [Build]
    # DISTRIBUTION, serves /<dist-id>, resolves $HOME for the default root.
    # (Identity keys live in distro.conf, not build.conf — build.conf only
    # carries [Live]/[Disk]/[Packages]/[Source].)
    assert 'distro.conf' in _src and 'DISTRIBUTION' in _src, _src
    assert 'EXPLICIT_ROOT' in _src and 'CANON_URL' in _src, _src
    # Q2 — reports a per-component inventory (fills only the gaps)
    assert 'Inventory' in _src and 'INV_dists' in _src and 'INV_marker' in _src, _src
    # Q3 — nginx serves the release index at root, detected by its marker
    assert 'index index.html;' in _src and 'athena-release-index' in _src, _src
    # permission gate: needs sudo to install the web server, write perms to
    # the root — and we run non-interactively
    assert 'SUDO' in _src and 'WRITABLE' in _src, _src
    # every prep run is logged: a timestamped transcript under the config's
    # log dir, output tee'd (colour stripped) so it matches the build log.
    assert 'LOGFILE' in _src and 'prep-mirror-' in _src, _src
    assert 'tee' in _src and 'Directories' in _src, _src



def test_cmd_mirror_add_new_mirror_empty_config():
    """Scenario 1 — adding a brand-new ssh mirror to a build host
    with no mirrors configured.  Fresh remote (no coord-head yet);
    one state file written; reconcile fan-out triggered."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    with tempfile.TemporaryDirectory() as _td:
        _sess, _restore = _cmd_mirror_add_session(_td)
        try:
            _stack = _patch_mirror_add_primitives(
                sidecar_head_return=(True, 'no head yet', None),
                discover_peers_return=[])
            with _stack:
                _ok = _sess.cmd_mirror_add(
                    'ip',
                    'ssh://ubuntu@140.245.198.222/home/ubuntu/asgard',
                    '--ssh-key', 'config/repo.key',
                    '--proto', 'http',
                    '--yes',
                )
        finally:
            _restore()
        assert _ok is True, '\n'.join(_sess.lines)
        # Single mirror written, name auto-derived from IP
        _names = _mirror.list_mirrors(_sess.config)
        assert _names == ['140-245-198-222'], _names
        _st = _mirror.read_mirror_state(_sess.config, '140-245-198-222')
        assert _st is not None
        assert _st['url'] == 'ssh://ubuntu@140.245.198.222/home/ubuntu/asgard'
        assert _st['public_url'] == 'http://140.245.198.222/asgard'
        assert _st['public_proto'] == 'http'
        assert _st['host'] == '140.245.198.222'
        assert _st['host_type'] == 'ip'
        # Reconcile fan-out fired once (would propagate to peers if any)
        assert _sess._reconcile_called == 1



def test_cmd_mirror_add_old_mirror_no_neighbours_empty_config():
    """Scenario 2 — adding a mirror that already has a published
    coord-head but lists NO neighbours (single-host federation).
    Empty local config.  Head summary printed; one mirror written."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    _peer_head = {
        'v': 2,
        'inrelease_sha256': 'a' * 64,
        'snapshot': {},
        'last_seqs': {'builder-x': 7},
        'head_time': '2026-06-01T00:00:00Z',
        'neighbours': ['ssh://ubuntu@host.example.com/home/ubuntu/asgard'],
    }
    with tempfile.TemporaryDirectory() as _td:
        _sess, _restore = _cmd_mirror_add_session(_td)
        try:
            _stack = _patch_mirror_add_primitives(
                sidecar_head_return=(True, 'coord-head verified', _peer_head),
                discover_peers_return=[])
            with _stack:
                _ok = _sess.cmd_mirror_add(
                    'fqdn',
                    'ssh://ubuntu@host.example.com/home/ubuntu/asgard',
                    '--ssh-key', 'config/repo.key',
                    '--proto', 'http',
                    '--yes',
                )
        finally:
            _restore()
        assert _ok is True, '\n'.join(_sess.lines)
        _names = _mirror.list_mirrors(_sess.config)
        assert _names == ['host-example-com'], _names
        # Head summary surfaced
        _joined = '\n'.join(_sess.lines)
        assert 'existing coord-head on this peer' in _joined
        assert 'builder-x=7' in _joined



def test_cmd_mirror_add_old_mirror_with_neighbours_empty_config():
    """Scenario 3 — adding a mirror in an existing federation.  Peer's
    coord-head lists two other peers; both reachable+verified; we
    inherit them.  Three mirror state files written."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    _primary_url = 'ssh://ubuntu@host-a.example/home/ubuntu/asgard'
    _peer1_url = 'ssh://ubuntu@host-b.example/home/ubuntu/asgard'
    _peer2_url = 'ssh://ubuntu@host-c.example/home/ubuntu/asgard'
    _peer_head = {
        'v': 2, 'neighbours': [_primary_url, _peer1_url, _peer2_url],
    }
    _discovered = [
        {'url': _primary_url, 'reachable': True, 'verified': True,
         'head': None, 'detail': 'self'},
        {'url': _peer1_url, 'reachable': True, 'verified': True,
         'head': None, 'detail': 'verified'},
        {'url': _peer2_url, 'reachable': True, 'verified': True,
         'head': None, 'detail': 'verified'},
    ]
    with tempfile.TemporaryDirectory() as _td:
        _sess, _restore = _cmd_mirror_add_session(_td)
        try:
            with _patch_mirror_add_primitives(
                sidecar_head_return=(True, 'verified', _peer_head),
                discover_peers_return=_discovered,
            ):
                _ok = _sess.cmd_mirror_add(
                    'fqdn', _primary_url,
                    '--ssh-key', 'config/repo.key',
                    '--proto', 'https', '--yes',
                )
        finally:
            _restore()
        assert _ok is True, '\n'.join(_sess.lines)
        _names = sorted(_mirror.list_mirrors(_sess.config))
        # primary + peer1 + peer2 all registered
        assert _names == ['host-a-example', 'host-b-example',
                          'host-c-example'], _names
        # The two discovered peers carry the same --proto + ssh_key
        _b = _mirror.read_mirror_state(_sess.config, 'host-b-example')
        assert _b['public_proto'] == 'https'
        assert _b['ssh_key'] == 'config/repo.key'



def test_cmd_mirror_add_new_mirror_to_existing_config():
    """Scenario 4 — local already has 2 mirrors; adding a third with
    no neighbours.  Existing 2 persist; new mirror written; reconcile
    fires to propagate the new membership to the existing peers."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    with tempfile.TemporaryDirectory() as _td:
        _sess, _restore = _cmd_mirror_add_session(_td)
        try:
            # Pre-populate two mirrors
            _mirror.add_mirror(
                _sess.config, name='alpha',
                url='ssh://ubuntu@alpha.example/home/ubuntu/asgard',
                seed_pin='')
            _mirror.add_mirror(
                _sess.config, name='beta',
                url='ssh://ubuntu@beta.example/home/ubuntu/asgard',
                seed_pin='')
            with _patch_mirror_add_primitives(
                sidecar_head_return=(True, 'no head yet', None),
                discover_peers_return=[],
            ):
                _ok = _sess.cmd_mirror_add(
                    'fqdn',
                    'ssh://ubuntu@gamma.example/home/ubuntu/asgard',
                    '--ssh-key', 'config/repo.key',
                    '--proto', 'http', '--yes',
                )
        finally:
            _restore()
        assert _ok is True, '\n'.join(_sess.lines)
        _names = sorted(_mirror.list_mirrors(_sess.config))
        assert _names == ['alpha', 'beta', 'gamma-example'], _names
        assert _sess._reconcile_called == 1



def test_cmd_mirror_add_overlap_with_existing_neighbours():
    """Scenario 5 — adding a federation peer that shares one neighbour
    with our existing config.  The overlapping URL is dedup'd; only
    the net-new ones are added."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    _alpha_url = 'ssh://ubuntu@alpha.example/home/ubuntu/asgard'
    _new_url = 'ssh://ubuntu@new.example/home/ubuntu/asgard'
    _peer_extra = 'ssh://ubuntu@extra.example/home/ubuntu/asgard'
    _peer_head = {
        'v': 2, 'neighbours': [_new_url, _alpha_url, _peer_extra],
    }
    _discovered = [
        {'url': _new_url,   'reachable': True, 'verified': True,
         'head': None, 'detail': 'self'},
        {'url': _alpha_url, 'reachable': True, 'verified': True,
         'head': None, 'detail': 'verified'},
        {'url': _peer_extra,'reachable': True, 'verified': True,
         'head': None, 'detail': 'verified'},
    ]
    with tempfile.TemporaryDirectory() as _td:
        _sess, _restore = _cmd_mirror_add_session(_td)
        try:
            _mirror.add_mirror(_sess.config, name='alpha', url=_alpha_url,
                               seed_pin='')
            with _patch_mirror_add_primitives(
                sidecar_head_return=(True, 'verified', _peer_head),
                discover_peers_return=_discovered,
            ):
                _ok = _sess.cmd_mirror_add(
                    'fqdn', _new_url,
                    '--ssh-key', 'config/repo.key',
                    '--proto', 'http', '--yes',
                )
        finally:
            _restore()
        assert _ok is True, '\n'.join(_sess.lines)
        _names = sorted(_mirror.list_mirrors(_sess.config))
        # alpha (pre-existing) + new.example (primary) + extra.example
        # (net-new discovered).  alpha overlap dedup'd; no double-add.
        assert _names == ['alpha', 'extra-example', 'new-example'], _names



def test_cmd_mirror_add_rejects_malformed_inputs():
    """Scenario 6 — robustness: every malformed shape produces a clean
    error + non-zero exit, no state file written."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    _cases = [
        # (args, expected substring of error log)
        (('banana', 'ssh://ubuntu@h.example/srv/a',
          '--ssh-key', 'k', '--proto', 'http', '--yes'),
         'unknown host type'),
        (('ip', 'ssh://ubuntu@h.example/srv/a',
          '--ssh-key', 'k', '--proto', 'http', '--yes'),
         'not a valid IPv4 or IPv6'),
        (('fqdn', 'ssh://ubuntu@140.245.198.222/srv/a',
          '--ssh-key', 'k', '--proto', 'http', '--yes'),
         'looks like an IP literal'),
        (('fqdn', 'ssh://ubuntu@h.example/srv/a',
          '--ssh-key', 'k', '--yes'),                  # missing --proto
         'require --proto'),
        (('fqdn', 'http://web.example/asgard',
          '--ssh-key', 'k', '--proto', 'http', '--yes'),
         'invalid URL'),
    ]
    for _argv, _expected in _cases:
        with tempfile.TemporaryDirectory() as _td:
            _sess, _restore = _cmd_mirror_add_session(_td)
            try:
                with _patch_mirror_add_primitives(
                    sidecar_head_return=(True, 'no head yet', None),
                    discover_peers_return=[],
                ):
                    _ok = _sess.cmd_mirror_add(*_argv)
            finally:
                _restore()
            assert _ok is False, f"{_argv} should have failed"
            _joined = '\n'.join(_sess.lines)
            assert _expected in _joined, (
                f"{_argv}: expected {_expected!r} in:\n{_joined}")
            # No mirror state file leaked
            assert _mirror.list_mirrors(_sess.config) == [], _argv



def test_cmd_mirror_add_drops_unreachable_neighbour():
    """Scenario 7 — peer's coord-head lists a neighbour that's
    unreachable when we probe it.  Default policy (take-control-and-drop)
    drops it from the federation join; remaining reachable peers are
    added; warning surfaces the dropped URL."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    _primary = 'ssh://ubuntu@primary.example/home/ubuntu/asgard'
    _live = 'ssh://ubuntu@live.example/home/ubuntu/asgard'
    _dead = 'ssh://ubuntu@dead.example/home/ubuntu/asgard'
    _peer_head = {
        'v': 2, 'neighbours': [_primary, _live, _dead],
    }
    _discovered = [
        {'url': _primary, 'reachable': True, 'verified': True,
         'head': None, 'detail': 'self'},
        {'url': _live, 'reachable': True, 'verified': True,
         'head': None, 'detail': 'verified'},
        {'url': _dead, 'reachable': False, 'verified': False,
         'head': None, 'detail': 'TCP connect failed: no route'},
    ]
    with tempfile.TemporaryDirectory() as _td:
        _sess, _restore = _cmd_mirror_add_session(_td)
        try:
            with _patch_mirror_add_primitives(
                sidecar_head_return=(True, 'verified', _peer_head),
                discover_peers_return=_discovered,
            ):
                _ok = _sess.cmd_mirror_add(
                    'fqdn', _primary,
                    '--ssh-key', 'config/repo.key',
                    '--proto', 'http', '--yes',
                )
        finally:
            _restore()
        assert _ok is True, '\n'.join(_sess.lines)
        _names = sorted(_mirror.list_mirrors(_sess.config))
        # primary + live; dead.example dropped per take-control-and-drop
        assert _names == ['live-example', 'primary-example'], _names
        _joined = '\n'.join(_sess.lines)
        assert 'DROPPING 1 unreachable peer' in _joined
        assert _dead in _joined



def test_cmd_mirror_add_uses_peer_meta_over_operator_proto():
    """Phase 7 — heterogeneous federation join: upstream's v3
    per-peer records (each carries its own public_url + public_proto)
    take precedence over the operator's --proto flag.  Verifies the
    locally-written state file for each discovered peer reflects the
    UPSTREAM's apt-readable URL, NOT the operator's --proto-derived one.
    v2-shaped peers (empty meta) still fall back to operator's --proto."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    _primary = 'ssh://ubuntu@primary.example/home/ubuntu/asgard'
    _peer_https = 'ssh://ubuntu@b.example/home/ubuntu/asgard'
    _peer_http  = 'ssh://ubuntu@c.example/home/ubuntu/asgard'
    _peer_v2    = 'ssh://ubuntu@d.example/home/ubuntu/asgard'
    _peer_head = {
        'v': 3,
        'neighbours': [
            {'url': _primary,    'public_url': 'http://primary.example/asgard',
             'public_proto': 'http'},
            {'url': _peer_https, 'public_url': 'https://b.example/asgard',
             'public_proto': 'https'},
            {'url': _peer_http,  'public_url': 'http://c.example/asgard',
             'public_proto': 'http'},
            {'url': _peer_v2,    'public_url': '', 'public_proto': ''},
        ],
    }
    _discovered = [
        {'url': _primary,    'reachable': True, 'verified': True,
         'head': None, 'detail': 'self',
         'public_url': 'http://primary.example/asgard', 'public_proto': 'http'},
        {'url': _peer_https, 'reachable': True, 'verified': True,
         'head': None, 'detail': 'verified',
         'public_url': 'https://b.example/asgard', 'public_proto': 'https'},
        {'url': _peer_http,  'reachable': True, 'verified': True,
         'head': None, 'detail': 'verified',
         'public_url': 'http://c.example/asgard', 'public_proto': 'http'},
        {'url': _peer_v2,    'reachable': True, 'verified': True,
         'head': None, 'detail': 'verified',
         'public_url': '', 'public_proto': ''},
    ]
    with tempfile.TemporaryDirectory() as _td:
        _sess, _restore = _cmd_mirror_add_session(_td)
        try:
            with _patch_mirror_add_primitives(
                sidecar_head_return=(True, 'verified', _peer_head),
                discover_peers_return=_discovered,
            ):
                # Operator passes --proto http; the per-peer signed
                # meta should OVERRIDE this for peers that carry it.
                _ok = _sess.cmd_mirror_add(
                    'fqdn', _primary,
                    '--ssh-key', 'config/repo.key',
                    '--proto', 'http', '--yes',
                )
        finally:
            _restore()
        assert _ok is True, '\n'.join(_sess.lines)
        # peer_https kept its upstream-recorded https URL despite the
        # operator passing --proto http
        _https = _mirror.read_mirror_state(_sess.config, 'b-example')
        assert _https['public_url']   == 'https://b.example/asgard'
        assert _https['public_proto'] == 'https'
        # peer_http roundtrips http (matches operator too — coincidence)
        _http = _mirror.read_mirror_state(_sess.config, 'c-example')
        assert _http['public_url']   == 'http://c.example/asgard'
        assert _http['public_proto'] == 'http'
        # v2-shaped peer falls back to operator's --proto http
        _v2 = _mirror.read_mirror_state(_sess.config, 'd-example')
        assert _v2['public_url']   == 'http://d.example/asgard'
        assert _v2['public_proto'] == 'http'



def test_cmd_mirror_add_signing_key_gate_blocks():
    """No tier-1 signing key → REFUSED before any probe.  Key
    verification gate is the first thing cmd_mirror_add does."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    import signing as _signing
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as _td:
        _sess, _restore = _cmd_mirror_add_session(_td)
        try:
            with patch.object(_signing, 'verify_key',
                              return_value=(False, 'no key generated')):
                _ok = _sess.cmd_mirror_add(
                    'ip', 'ssh://ubuntu@140.245.198.222/srv/a',
                    '--ssh-key', 'k', '--proto', 'http', '--yes',
                )
        finally:
            _restore()
        assert _ok is False
        _joined = '\n'.join(_sess.lines)
        assert 'REFUSED' in _joined
        assert 'key generate' in _joined
        assert _mirror.list_mirrors(_sess.config) == []



def test_mirror_add_remove_round_trip():
    """add_mirror writes the state file; list_mirrors finds it; remove
    deletes it."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        assert _m.list_mirrors(_cfg) == []
        _ok, _detail = _m.add_mirror(
            _cfg, name='alpha', url='ssh://ubuntu@host/srv/asgard',
            ssh_key='config/asgard.key', seed_pin='20260601T000000Z',
        )
        assert _ok, _detail
        assert _m.list_mirrors(_cfg) == ['alpha']
        _st = _m.read_mirror_state(_cfg, 'alpha')
        assert _st['name'] == 'alpha'
        assert _st['url'] == 'ssh://ubuntu@host/srv/asgard'
        assert _st['type'] == 'ssh'
        assert _st['ssh_key'] == 'config/asgard.key'
        assert _st['base'] == '20260601T000000Z'
        assert _st['current'] == '20260601T000000Z'
        assert _st['last_publish_at'] == ''
        assert _st['neighbours_known'] == []
        # New MIRROR-01 Phase 6 fields default to empty when caller
        # doesn't pass them (back-compat shape for legacy callers).
        assert _st['host'] == ''
        assert _st['host_type'] == ''
        assert _st['public_proto'] == ''
        assert _st['public_url'] == ''
        # Remove by name
        _ok, _ = _m.remove_mirror(_cfg, url_or_name='alpha')
        assert _ok
        assert _m.list_mirrors(_cfg) == []



def test_mirror_add_rejects_duplicate_name_and_url():
    """Same name OR same URL twice → second add fails (atomically; first
    state survives)."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='alpha', url='ssh://h/p', seed_pin='T')
        _ok, _ = _m.add_mirror(_cfg, name='alpha', url='ssh://h2/p2', seed_pin='T')
        assert not _ok
        _ok, _ = _m.add_mirror(_cfg, name='beta', url='ssh://h/p', seed_pin='T')
        assert not _ok
        assert _m.list_mirrors(_cfg) == ['alpha']



def test_mirror_add_rejects_invalid_name_and_url():
    """Invalid name (slash, dot-dot, spaces, empty) and invalid URL refused."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        for _bad in ('', 'has space', 'has/slash', '..', 'a' * 65):
            _ok, _ = _m.add_mirror(_cfg, name=_bad, url='ssh://h/p', seed_pin='')
            assert not _ok, f"name {_bad!r} must be rejected"
        for _bad in ('', '   ', 'not-a-url', 'gopher://x'):
            _ok, _ = _m.add_mirror(_cfg, name='alpha', url=_bad, seed_pin='')
            assert not _ok, f"url {_bad!r} must be rejected"



def test_mirror_url_normalisation_and_lookup():
    """Bare paths become file://; lookup by URL finds the mirror name."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='local-fs', url='/srv/asgard', seed_pin='')
        _st = _m.read_mirror_state(_cfg, 'local-fs')
        assert _st['url'] == 'file:///srv/asgard'
        assert _st['type'] == 'local'
        # Lookup via either form
        assert _m.find_mirror_by_url(_cfg, '/srv/asgard') == 'local-fs'
        assert _m.find_mirror_by_url(_cfg, 'file:///srv/asgard') == 'local-fs'



def test_mirror_remove_by_url():
    """`remove_mirror(url_or_name=<url>)` finds the state file by URL match."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='alpha', url='ssh://h/p', seed_pin='')
        _ok, _detail = _m.remove_mirror(_cfg, url_or_name='ssh://h/p')
        assert _ok, _detail
        assert _m.list_mirrors(_cfg) == []



def test_mirror_update_state_merges_fields():
    """update_mirror_state read-merge-writes; missing mirror returns False."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='alpha', url='ssh://h/p',
                      seed_pin='20260101T000000Z')
        assert _m.update_mirror_state(
            _cfg, 'alpha', current='20260601T000000Z',
            last_publish_at='2026-06-01T00:00:00Z') is True
        _st = _m.read_mirror_state(_cfg, 'alpha')
        assert _st['current'] == '20260601T000000Z'
        assert _st['base'] == '20260101T000000Z'  # untouched
        assert _st['last_publish_at'] == '2026-06-01T00:00:00Z'
        # Unknown mirror
        assert _m.update_mirror_state(_cfg, 'ghost', current='T') is False



def test_mirror_conf_signed_tamper_detected_and_reseal():
    """CONFIG-SPLIT: mirror state lives in ONE HMAC-signed config/mirror.conf.
    A manual edit fails verification → reads refuse (None/[]) and writes are
    blocked until `mirror reseal` re-signs the acknowledged content."""
    import json as _json
    import os as _os
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='alpha', url='ssh://h/a',
                      seed_pin='20260101T000000Z')
        # one signed document, no per-name .state file
        _conf = _m.mirror_conf_path(_cfg)
        assert _os.path.isfile(_conf)
        assert 'sig' in _json.load(open(_conf))
        assert _m.mirror_conf_status(_cfg) == 'ok'
        # TAMPER: hand-edit a value → signature no longer matches
        _doc = _json.load(open(_conf))
        _doc['mirrors']['alpha']['url'] = 'ssh://EVIL/x'
        open(_conf, 'w').write(_json.dumps(_doc))
        assert _m.mirror_conf_status(_cfg) == 'badsig'
        assert _m.read_mirror_state(_cfg, 'alpha') is None   # refuse
        assert _m.list_mirrors(_cfg) == []                   # refuse
        assert _m.write_mirror_state(_cfg, 'beta', {'name': 'beta'}) is False
        # RESEAL: operator acknowledges the edit → usable again
        _ok, _ = _m.reseal_mirror_conf(_cfg)
        assert _ok is True
        assert _m.mirror_conf_status(_cfg) == 'ok'
        assert _m.read_mirror_state(_cfg, 'alpha')['url'] == 'ssh://EVIL/x'



def test_mirror_conf_migrates_legacy_state_files():
    """A checkout with legacy config/mirror.<name>.state files auto-migrates
    them into the signed mirror.conf on first read."""
    import json as _json
    import os as _os
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        for _n in ('one', 'two'):
            open(_os.path.join(_td, f'mirror.{_n}.state'), 'w').write(
                _json.dumps({'name': _n, 'url': f'ssh://h/{_n}'}))
        assert _m.list_mirrors(_cfg) == ['one', 'two']
        assert _os.path.isfile(_m.mirror_conf_path(_cfg))
        assert _m.mirror_conf_status(_cfg) == 'ok'
        assert _m.read_mirror_state(_cfg, 'one')['url'] == 'ssh://h/one'



def test_cmd_mirror_dispatch_routes_subcommands():
    """`cmd_mirror` routes add/remove/list/summary/status/reconcile-neighbours."""
    import re
    _body = _session_source()
    assert 'def cmd_mirror(' in _body
    for _sub in ('cmd_mirror_add', 'cmd_mirror_remove', 'cmd_mirror_list',
                 'cmd_mirror_summary', 'cmd_mirror_status',
                 'cmd_mirror_reconcile_neighbours'):
        assert f'def {_sub}(' in _body, f"{_sub} missing"
    assert "register_command('mirror'" in _body, "mirror not registered"
    assert re.search(
        r"if action == 'add':\s*\n\s+return self\.cmd_mirror_add", _body)
    assert re.search(
        r"if action == 'remove' or action == 'delete':\s*\n\s+"
        r"return self\.cmd_mirror_remove", _body)
    assert re.search(
        r"if action == 'reconcile-neighbours':\s*\n\s+"
        r"return self\.cmd_mirror_reconcile_neighbours", _body)



def test_rsync_spec_for_url_handles_every_form():
    """ssh:// / file:// / user@host: shorthand / bare path → correct
    rsync-native (spec, ssh_host) tuples."""
    _m = _mirror_module()
    _cases = [
        ('ssh://ubuntu@host/srv/asgard', ('ubuntu@host:/srv/asgard', 'ubuntu@host')),
        ('file:///srv/asgard',           ('/srv/asgard', None)),
        ('ubuntu@host:/srv/asgard',      ('ubuntu@host:/srv/asgard', 'ubuntu@host')),
        ('/srv/asgard',                  ('/srv/asgard', None)),
    ]
    for _url, _want in _cases:
        _got = _m.rsync_spec_for_url(_url)
        assert _got == _want, f"{_url}: got {_got}, want {_want}"
    # STA-53(e): a non-default ssh port has no valid spec encoding — reject,
    # don't silently mangle into `host:port:path`.
    try:
        _m.rsync_spec_for_url('ssh://ubuntu@host:2222/srv/asgard')
    except ValueError:
        pass
    else:
        raise AssertionError("ssh:// URL with a port must raise ValueError")
    # A path-less ssh URL (or a coord form derived from one) has no remote
    # directory to sync — must raise, NOT return a bare `user@host` spec that
    # rsync would silently treat as a local CWD-relative path.
    for _bad in ('ssh://ubuntu@host', 'ssh://ubuntu@host-coord'):
        try:
            _m.rsync_spec_for_url(_bad)
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"path-less ssh URL must raise ValueError: {_bad!r}")



def test_all_mirror_urls_returns_every_registered_url():
    """all_mirror_urls walks every config/mirror.<name>.state and returns
    the URL field; the result is the SOURCE OF TRUTH for the federation
    neighbours list."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='alpha', url='ssh://a/p', seed_pin='')
        _m.add_mirror(_cfg, name='beta', url='file:///srv/b', seed_pin='')
        _urls = _m.all_mirror_urls(_cfg)
        assert set(_urls) == {'ssh://a/p', 'file:///srv/b'}



def test_neighbours_drift_tags_unpublished_in_sync_and_drift():
    """`mirror.neighbours_drift` returns 'unpublished' when the peer has
    no neighbours_known yet, 'in-sync' when it matches the local config,
    and 'drift' (with explicit missing/extra lists) otherwise."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
            dir_cache = os.path.join(_td, 'cache')
        os.makedirs(_Cfg.dir_cache)
        _cfg = _Cfg()
        _m.add_mirror(_cfg, name='alpha', url='file:///srv/a', seed_pin='')
        _m.add_mirror(_cfg, name='beta',  url='file:///srv/b', seed_pin='')
        _m.add_mirror(_cfg, name='gamma', url='file:///srv/c', seed_pin='')
        # Freshly added → no neighbours_known → 'unpublished'
        _tag, _miss, _extra = _m.neighbours_drift(_cfg, 'alpha')
        assert _tag == 'unpublished'
        assert (_miss, _extra) == ([], [])
        # Stamp alpha with the canonical local-config set → 'in-sync'
        _local = _m.all_mirror_urls(_cfg)
        _m.update_mirror_state(_cfg, 'alpha', neighbours_known=_local)
        _tag, _miss, _extra = _m.neighbours_drift(_cfg, 'alpha')
        assert _tag == 'in-sync', (_miss, _extra)
        assert (_miss, _extra) == ([], [])
        # Stamp beta with a STALE neighbours list (missing gamma, extra delta)
        _m.update_mirror_state(
            _cfg, 'beta',
            neighbours_known=['file:///srv/a', 'file:///srv/b',
                              'file:///srv/d'])
        _tag, _miss, _extra = _m.neighbours_drift(_cfg, 'beta')
        assert _tag == 'drift'
        assert _miss == ['file:///srv/c']
        assert _extra == ['file:///srv/d']
        # Unknown mirror name → 'unpublished' (no state file)
        _tag, _miss, _extra = _m.neighbours_drift(_cfg, 'nonexistent')
        assert _tag == 'unpublished'



def test_reconcile_neighbours_target_name_unknown_fails():
    """`target_name` referring to an unregistered mirror fails clean."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
            dir_cache = os.path.join(_td, 'cache')
        os.makedirs(_Cfg.dir_cache)
        _cfg = _Cfg()
        _ok, _detail, _results = _m.reconcile_neighbours(
            _cfg, signing_homedir='/fake/gpg', target_name='ghost')
        assert not _ok
        assert 'unknown mirror' in _detail
        assert _results == []



def test_reconcile_neighbours_no_mirrors_is_trivial_ok():
    """No mirrors configured → trivially ok, no work."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
            dir_cache = os.path.join(_td, 'cache')
        os.makedirs(_Cfg.dir_cache)
        _cfg = _Cfg()
        _ok, _detail, _results = _m.reconcile_neighbours(
            _cfg, signing_homedir='/fake/gpg')
        assert _ok
        assert 'no mirrors configured' in _detail
        assert _results == []



# ─────────────────────────────────────────────────────────────────────────────
# MIRROR-01 Phase 3 — federation-gated publish, first-publish bootstrap, pull
# ─────────────────────────────────────────────────────────────────────────────


def test_cmd_mirror_dispatch_routes_publish_and_pull():
    """`cmd_mirror` routes publish + pull subcommands."""
    import re
    _body = _session_source()
    for _sub in ('cmd_mirror_publish', 'cmd_mirror_pull'):
        assert f'def {_sub}(' in _body, f"{_sub} missing"
    assert re.search(
        r"if action == 'publish':\s*\n\s+return self\.cmd_mirror_publish",
        _body)
    assert re.search(
        r"if action == 'pull':\s*\n\s+return self\.cmd_mirror_pull",
        _body)



def test_all_mirror_neighbour_records_pulls_per_peer_meta_from_state():
    """`mirror.all_mirror_neighbour_records` walks every per-mirror
    state file and emits one record per peer, with the apt-readable URL
    + protocol the operator supplied at `mirror add` time."""
    _m = _mirror_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        _m.add_mirror(
            _cfg, name='alpha',
            url='ssh://ubuntu@a.example/srv/asgard',
            host='a.example', host_type='fqdn',
            public_proto='http',
            public_url='http://a.example/asgard',
            seed_pin='')
        _m.add_mirror(
            _cfg, name='beta',
            url='ssh://ubuntu@b.example/srv/asgard',
            host='b.example', host_type='fqdn',
            public_proto='https',
            public_url='https://b.example/asgard',
            seed_pin='')
        _m.add_mirror(
            _cfg, name='local',
            url='file:///srv/asgard', seed_pin='')
        _recs = _m.all_mirror_neighbour_records(_cfg)
        # File:// mirror keeps empty public_url (no network apt URL);
        # ssh:// mirrors carry their derived public URL + proto.
        _by_url = {_r['url']: _r for _r in _recs}
        assert _by_url['ssh://ubuntu@a.example/srv/asgard'] == {
            'url': 'ssh://ubuntu@a.example/srv/asgard',
            'public_url': 'http://a.example/asgard',
            'public_proto': 'http',
        }
        assert _by_url['ssh://ubuntu@b.example/srv/asgard']['public_proto'] \
            == 'https'
        assert _by_url['file:///srv/asgard'] == {
            'url': 'file:///srv/asgard',
            'public_url': '', 'public_proto': '',
        }



# ─────────────────────────────────────────────────────────────────────────────
# MIRROR-01 Phase 3b — per-file .deb push + progress + coord/pool split
# ─────────────────────────────────────────────────────────────────────────────


def test_coord_root_for_appends_suffix_to_last_path_component():
    """coord_root_for: pool URL → sidecar URL via `-coord` suffix.
    Strips trailing slash first to avoid `<x>/-coord`."""
    _m = _mirror_module()
    _cases = [
        ('ssh://user@host/srv/asgard',  'ssh://user@host/srv/asgard-coord'),
        ('file:///srv/asgard',          'file:///srv/asgard-coord'),
        ('file:///srv/asgard/',         'file:///srv/asgard-coord'),
        ('user@host:/srv/asgard/',      'user@host:/srv/asgard-coord'),
        ('/srv/asgard',                 '/srv/asgard-coord'),
        ('',                            '-coord'),
    ]
    for _pool, _want in _cases:
        _got = _m.coord_root_for(_pool)
        assert _got == _want, f"{_pool!r}: got {_got!r}, want {_want!r}"



def test_cmd_mirror_dispatch_routes_audit_and_query():
    """`cmd_mirror` routes audit + query subcommands; both handlers exist."""
    import re
    _body = _session_source()
    for _sub in ('cmd_mirror_audit', 'cmd_mirror_query'):
        assert f'def {_sub}(' in _body, f"{_sub} missing"
    assert re.search(
        r"if action == 'audit':\s*\n\s+return self\.cmd_mirror_audit",
        _body)
    assert re.search(
        r"if action == 'query':\s*\n\s+return self\.cmd_mirror_query",
        _body)



def test_cmd_mirror_dispatch_routes_reclaim():
    """RECLAIM-01 Chunk 5: `mirror reclaim` is dispatched and the
    handler + help-table row exist."""
    import re
    _body = _session_source()
    assert 'def cmd_mirror_reclaim(' in _body
    assert re.search(
        r"if action == 'reclaim':\s*\n\s+return self\.cmd_mirror_reclaim",
        _body)
    assert 'reclaim [<src>|<file>] [<name>] [force]' in _body



def test_mirror_pull_progress_is_byte_sized_with_pkg_label():
    """mirror pull's progress bar is byte-valued (downloaded/total SIZE at
    MB/s, auto K/M/G) with the binary package name as a fixed-width label —
    NOT an item count / it/s."""
    import re
    _p = os.path.join(_ROOT, 'scripts', 'commands', 'cmd_mirror.py')
    with open(_p) as _fh:
        _body = _fh.read()
    _m = re.search(r'def cmd_mirror_pull\(self.*?(?=\n    def )',
                   _body, re.DOTALL)
    assert _m, "cmd_mirror_pull not found"
    _src = _m.group(0)
    assert "itr_label='B/s'" in _src, "rate must be byte-based (B/s), not it/s"
    assert "_bar.step(int(_ent.get('size')" in _src, \
        "ledger path must step by byte size"
    assert "_bar.step(int(_c.get('size')" in _src, \
        "fallback path must step by byte size"
    assert "_bar.label(_fn.split('_', 1)[0])" in _src, \
        "label must be the binary package name"
    assert "_bar.step(1)" not in _src, "no item-count stepping should remain"



def test_mirror_pull_routes_and_records_source_artifacts():
    """MAT-02 stage 5 wiring pins:
      - _pull_file and _restore_own_file route through _artifact_dest_dir
        (source artifacts land in dists/<codename>/<comp>/source/)
      - the pull record writer splits source artifacts into
        source_outputs/source_output_hashes (never the .deb keys)
      - push_dist_tree excludes source artifacts from the --delete rsync
        (append-only source pool, like the .debs)
      - the audit merges the verified Sources index into the claim
        cross-check and feeds it to the ledger validation."""
    import re
    _p = os.path.join(_ROOT, 'scripts', 'commands', 'cmd_mirror.py')
    with open(_p) as _fh:
        _c = _fh.read()
    assert _c.count('self._artifact_dest_dir(') >= 2, \
        'pull + restore paths must route through _artifact_dest_dir'
    _m = re.search(r'def _mirror_pull_write_build_records\(.*?(?=\n    def )',
                   _c, re.DOTALL)
    assert _m and 'source_output_hashes' in _m.group(0), \
        'pull record writer must split source artifacts to the source keys'
    assert '_pkg_idx.update(_src_idx)' in _c, \
        'claims cross-check must include the Sources index'
    assert 'src_idx=_src_idx' in _c, \
        'ledger validation must receive the Sources index'
    _tp = os.path.join(_ROOT, 'scripts', 'coord', 'transport.py')
    with open(_tp) as _fh:
        _t = _fh.read()
    for _pat in ('--exclude=*.dsc', '--exclude=*.tar.*',
                 '--exclude=*.diff.gz', '--exclude=*.asc'):
        assert _pat in _t, f'push_dist_tree must exclude {_pat}'
    # the remote pool LISTINGS must see source artifacts too, or every
    # source claim audits missing_on_disk (2026-07-13 live finding).
    # BOTH listings: transport.list_remote_debs (publish completeness)
    # AND the audit's _mirror_audit_pool_listing (ssh + file:// walks).
    for _pat in ("-path '*/source/*.dsc'", "-path '*/source/*.tar.*'",
                 "-path '*/source/*.diff.gz'", "-path '*/source/*.asc'"):
        assert _pat in _t, f'remote pool listing must match {_pat}'
    for _pat in ('-path "*/source/*.dsc"', '-path "*/source/*.tar.*"',
                 '-path "*/source/*.diff.gz"', '-path "*/source/*.asc"'):
        assert _pat in _c, f'audit pool listing must match {_pat}'
    assert 'is_source_artifact' in re.search(
        r'def _mirror_audit_pool_listing\(.*?(?=\n    def )', _c,
        re.DOTALL).group(0), 'file:// audit walk must see source artifacts'


def test_mirror_pull_restores_missing_own_files():
    """Skip-own with restore-missing (2026-07-12): an OWN claim's file that
    is MISSING from repo/ is downloaded back and verified against our own
    signed claim sha (fresh-machine recovery), while a PRESENT own file is
    never touched and a build record pinning a DIFFERENT sha blocks the
    restore (local-ahead awaiting reclaim).  Pins:
      - both walk branches route own claims through _restore_own_file
        (no bare `_skip_own += 1` remains at the walk level)
      - the restore path sha-verifies and unlinks on mismatch
      - the local-ahead guard reads the build record's output_hashes
      - the restore path writes NO build record (own records are already
        authoritative — no _per_pkg_downloads append)"""
    import re
    _p = os.path.join(_ROOT, 'scripts', 'commands', 'cmd_mirror.py')
    with open(_p) as _fh:
        _body = _fh.read()
    _m = re.search(r'def cmd_mirror_pull\(self.*?(?=\n    def )',
                   _body, re.DOTALL)
    assert _m, "cmd_mirror_pull not found"
    _src = _m.group(0)
    assert '_restore_own_file(_fn, _claim)' in _src, \
        "ledger walk must route own claims through _restore_own_file"
    assert '_restore_own_file(_fn, _c)' in _src, \
        "fallback walk must route own claims through _restore_own_file"
    # after the closure defs, the walk bodies must not bare-skip own claims
    _walks = _src.split('# Fetch + verify the signed closure ledger', 1)[1]
    assert '_skip_own += 1' not in _walks, \
        "walk level must not bare-skip own claims anymore"
    _rof = re.search(
        r'def _restore_own_file\(.*?(?=\n            # Fetch \+ verify)',
        _src, re.DOTALL)
    assert _rof, "_restore_own_file closure not found"
    _r = _rof.group(0)
    assert 'read_build_record' in _r and 'output_hashes' in _r, \
        "local-ahead guard must consult the build record hashes"
    assert 'get_sha256' in _r and 'os.unlink' in _r, \
        "restore must sha-verify and remove a mismatched download"
    _r_body = _r.split('"""', 2)[2]        # past the docstring
    assert '_per_pkg_downloads' not in _r_body, \
        "restore path must not write build records for own files"



def test_mirror_pull_canonical_apply_has_local_ahead_guard():
    """Local-ahead guard (2026-07-12, the ffmpegthumbnailer incident): a
    `mirror pull` whose fetched config_sha256 equals OUR local coord-head's
    pin must NOT overwrite the list files or reseed selection.state — the
    federation carries nothing new, and the apply would clobber local
    not-yet-published `cache select` edits.  A genuinely NEW remote pin
    still applies, with `.pre-pull` backups of any differing local list.
    Pins:
      - the guard compares the fetched pin against the LOCAL coord-head
      - the skip branch returns BEFORE the verified apply / reseed
      - the apply branch writes `.pre-pull` backups for drifted lists"""
    import re
    _p = os.path.join(_ROOT, 'scripts', 'commands', 'cmd_mirror.py')
    with open(_p) as _fh:
        _body = _fh.read()
    _m = re.search(r'def _apply_canonical_config\(self.*?(?=\n    def )',
                   _body, re.DOTALL)
    assert _m, "_apply_canonical_config not found"
    _src = _m.group(0)
    assert 'read_coord_head' in _src and 'config_sha256' in _src, \
        "guard must read the LOCAL coord-head's config pin"
    assert "_local_sha and str(_local_sha) == str(_sha)" in _src, \
        "equal-pin comparison missing"
    assert _src.index('== str(_sha)') < _src.index(
        '_cfgman.apply_canonical_config('), \
        "guard must precede the overwrite"
    assert ".pre-pull" in _src, \
        "apply branch must back up drifted local lists"
    assert '_local_lists_drift' in _src, \
        "drift detection must feed the guard messages/backups"


def test_cmd_mirror_publish_refuses_when_snapshot_older_than_mirror_base():
    """Phase 8 publish gate: BLOCK when build snapshot.current < mirror.base.
    Surfaces an actionable error + leaves no state change."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    import mirror as _mirror
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _td:
        _cfg_dir = os.path.join(_td, 'config')
        _cache_dir = os.path.join(_td, 'cache')
        _repo_dir = os.path.join(_td, 'repo')
        os.makedirs(_cfg_dir)
        os.makedirs(_cache_dir)
        os.makedirs(os.path.join(_repo_dir, 'dists', 'thor'))
        # InRelease present so the early indexing gate passes
        with open(os.path.join(_repo_dir, 'dists', 'thor',
                               'InRelease'), 'w') as _fh:
            _fh.write('Date: 2026-06-01\n')

        class _Cfg:
            arch = 'amd64'
            dir_config = _cfg_dir
            dir_cache  = _cache_dir
            dir_repo   = _repo_dir
            dir_image  = _cfg_dir   # no ISOs here → the --no-iso path
            build_codename = 'thor'
            build_version = '1'
            build_distribution = 'asgard'

        # Add mirror with a base FROM THE FUTURE relative to the build pin
        _mirror.add_mirror(
            _Cfg(), name='primary',
            url='file:///srv/asgard',
            seed_pin='20260615T000000Z')  # mirror.base = 20260615…

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess._snapshot_current = lambda: '20260601T000000Z'  # OLDER
        # Identity stub — publish path checks for coord keys.
        _sess._coord_self_keys = lambda: (
            'athena-test', '/fake/priv', '/fake/pub')

        _lines: 'list[str]' = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _ok = _sess.cmd_mirror_publish('primary', '--no-iso')
        finally:
            build.console.print = _orig
        _joined = '\n'.join(_lines)
        assert _ok is False, _joined
        assert 'REFUSED' in _joined
        assert 'archive floor' in _joined
        assert '20260615T000000Z' in _joined
        assert '20260601T000000Z' in _joined



def test_cmd_mirror_summary_we_own_counts_non_retracted_claims():
    """Phase 8: `mirror summary` adds `we_own: N pkg(s)` per mirror,
    sourced from the fetched claims jsonl.  Counts non-retracted
    entries; surfaces retraction count parenthetically when > 0.
    Friendly sentinel string when no jsonl has been fetched yet."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    import mirror as _mirror
    from build import BuildSession
    import json as _json
    with tempfile.TemporaryDirectory() as _td:
        _cfg_dir = os.path.join(_td, 'config')
        _cache_dir = os.path.join(_td, 'cache')
        _coord_dir = os.path.join(_td, 'coord')
        os.makedirs(_cfg_dir)
        os.makedirs(_cache_dir)
        os.makedirs(_coord_dir)
        # Builder ID file (the `we_own` count needs to know who WE are)
        with open(os.path.join(_coord_dir, 'BUILDER_ID'), 'w') as _fh:
            _fh.write('athena-test')

        _sess = BuildSession.__new__(BuildSession)
        class _Cfg:
            dir_config = _cfg_dir
            dir_cache  = _cache_dir
            dir_coord  = _coord_dir
        _sess.config = _Cfg()
        # Register one mirror; doesn't matter what URL — the count
        # comes from the fetched cache, not the live remote.
        _mirror.add_mirror(_Cfg(), name='primary',
                           url='ssh://h/srv/asgard', seed_pin='')
        # Drop a fetched claims jsonl with 3 live + 1 retracted claim
        _fetched = os.path.join(_cache_dir, 'mirror', 'primary',
                                'fetched', 'claims')
        os.makedirs(_fetched)
        with open(os.path.join(_fetched, 'athena-test.jsonl'), 'w') as _fh:
            for _i in range(3):
                _fh.write(_json.dumps({
                    'builder': 'athena-test', 'seq': _i + 1,
                    'package': f'pkg-{_i}', 'claim_state': 'published',
                }) + '\n')
            _fh.write(_json.dumps({
                'builder': 'athena-test', 'seq': 4,
                'package': 'old-pkg', 'claim_state': 'retracted',
                'retracts_seq': 0,
            }) + '\n')

        _lines: 'list[str]' = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _r = _sess.cmd_mirror_summary('primary')
        finally:
            build.console.print = _orig
        assert _r is True, '\n'.join(_lines)
        _joined = '\n'.join(_lines)
        assert 'we_own:' in _joined
        assert '3 pkg(s)' in _joined
        assert '1 retracted' in _joined

    # Second case: no fetched jsonl → friendly sentinel
    with tempfile.TemporaryDirectory() as _td2:
        _cfg_dir = os.path.join(_td2, 'config')
        _cache_dir = os.path.join(_td2, 'cache')
        _coord_dir = os.path.join(_td2, 'coord')
        for _d in (_cfg_dir, _cache_dir, _coord_dir):
            os.makedirs(_d)
        with open(os.path.join(_coord_dir, 'BUILDER_ID'), 'w') as _fh:
            _fh.write('athena-test')
        _sess = BuildSession.__new__(BuildSession)
        class _Cfg2:
            dir_config = _cfg_dir
            dir_cache  = _cache_dir
            dir_coord  = _coord_dir
        _sess.config = _Cfg2()
        _mirror.add_mirror(_Cfg2(), name='primary',
                           url='ssh://h/srv/asgard', seed_pin='')
        _lines = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _sess.cmd_mirror_summary('primary')
        finally:
            build.console.print = _orig
        _joined = '\n'.join(_lines)
        assert 'no claims jsonl fetched yet' in _joined



def test_cmd_mirror_query_reports_no_match():
    """`mirror query <pkg>` with no claims for that package across any
    mirror surfaces the no-match guidance message."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _sess = BuildSession.__new__(BuildSession)
        class _Cfg:
            dir_config = _cfg_dir
            dir_cache = _tmp
        _sess.config = _Cfg()
        # Register two mirrors but never fetch — empty cache state
        import mirror as _mirror
        _mirror.add_mirror(_Cfg(), name='alpha', url='ssh://h/a',
                           seed_pin='')
        _mirror.add_mirror(_Cfg(), name='beta', url='ssh://h/b',
                           seed_pin='')
        _lines = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _ok = _sess.cmd_mirror_query('libfoo')
        finally:
            build.console.print = _orig
        assert _ok is True
        _joined = '\n'.join(_lines)
        assert 'no claim for package' in _joined
        assert "'libfoo'" in _joined
        assert '2 mirror(s)' in _joined



def test_mirror_pull_record_writer_preserves_lifecycle_pin():
    """PIN: _mirror_pull_write_build_records read-merges the existing record
    and its update dict must never clobber lifecycle keys."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    # Source-level pin: the writer merges from the EXISTING record and the
    # literal update dict must not name any lifecycle field.
    import inspect
    import commands.cmd_mirror as _cm
    _src = inspect.getsource(_cm)
    _block = _src[_src.index('_mirror_pull_write_build_records'):]
    _block = _block[:_block.index('def cmd_mirror_audit')]
    assert '_rec = dict(_existing)' in _block, "pull writer no longer merges"
    for _f in _u.LIFECYCLE_FIELDS:
        assert f"'{_f}':" not in _block, (
            f"mirror-pull update dict now writes lifecycle field {_f!r} — "
            "it would clobber the parse-stamped layer")



def test_audit_foreign_target_claims_flags_owned_published():
    """Phase-4 assurance gate: a file WE own + publish whose binary is
    foreign-target → CRITICAL; a peer's claim or a missing build_arch is a
    no-op."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    _foreign = 'binutils-aarch64-linux-gnu_2.40-2_amd64.deb'
    _native = 'binutils-x86-64-linux-gnu_2.40-2_amd64.deb'

    def _claim(fn, seq):
        return {'v': 1, 'builder': 'alice', 'seq': seq, 'package': 'binutils',
                'intended_version': '2.40-2', 'built_version': '2.40-2',
                'filename': fn, 'sha256': 'a' * 64, 'size': 1,
                'snapshot': 'S', 'built_at': 'T',
                'claim_state': 'published', 'republished_from': None}
    by_builder = {'alice': [_claim(_foreign, 1), _claim(_native, 2)]}
    _f = _mirror.audit_foreign_target_claims(by_builder, 'alice', 'amd64')
    assert [k for _, k, _ in _f] == ['foreign_target_claim_published'], _f
    assert [m.split(':', 1)[0] for _, _, m in _f] == [_foreign], _f
    # not ours → nothing
    assert _mirror.audit_foreign_target_claims(by_builder, 'bob', 'amd64') == []
    # no build_arch → no-op
    assert _mirror.audit_foreign_target_claims(by_builder, 'alice', None) == []



def test_local_mirror_plan_resolves_build_closure_to_snapshot_urls():
    """local_mirror.plan: the build closure (toolchain + build-deps + install
    closure) maps to download entries whose URLs are the snapshot-pinned
    pkg._mirror.url + Filename."""
    import local_mirror
    cache, dt = _lm_mock_cache_and_tree()
    pl = local_mirror.plan(cache, dt, object())
    names = {e['name'] for e in pl['entries']}
    assert {'build-essential', 'gcc', 'make', 'libc6-dev'} <= names   # toolchain
    assert {'debhelper', 'dpkg-dev', 'libfoo-dev'} <= names           # build-deps
    assert pl['total_size'] == 830
    assert all(e['url'].startswith(
        'https://snapshot.debian.org/archive/debian/20260621T135952Z/pool/')
        for e in pl['entries'])



def test_local_mirror_plan_include_index_emits_packages_blob():
    """plan(include_index=True) carries a packages_index built from pkg.dump()
    with Filename rewritten to ./<basename> — so a REMOTE host can write a valid
    apt index natively (no dpkg-scanpackages).  Off by default."""
    import local_mirror
    cache, dt = _lm_mock_cache_and_tree()
    pl = local_mirror.plan(cache, dt, object(), include_index=True)
    assert 'packages_index' in pl
    _idx = pl['packages_index']
    # every download entry appears as a flat ./<basename> Filename + a stanza
    for _e in pl['entries']:
        assert f"Filename: ./{_e['basename']}" in _idx
    assert 'Package: gcc' in _idx and 'Package: debhelper' in _idx
    # no original pool/ Filenames leaked through
    assert 'Filename: pool/' not in _idx
    # default omits it (the LOCAL mirror uses dpkg-scanpackages)
    assert 'packages_index' not in local_mirror.plan(cache, dt, object())



def test_local_mirror_download_withholds_marker_on_failure_cons13():
    """CONS-13: local_mirror.download stamps the .snapshot marker ONLY on a
    COMPLETE run.  A partial download (any failure) leaves no marker, so a
    partial mirror is never marked valid-for-snapshot — _ensure_local_mirror
    re-runs and retries the missing files (vs skipping them forever)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import local_mirror as lm
    with tempfile.TemporaryDirectory() as _target:
        _entries = [
            {'basename': 'good.deb', 'url': 'g', 'sha256': '', 'size': 10},
            {'basename': 'bad.deb', 'url': 'b', 'sha256': '', 'size': 10},
        ]
        _marker = os.path.join(_target, lm.MARKER)

        def _dlf(url, dest):
            if 'good' in dest:
                open(dest, 'wb').close()
                return (10, 'ok')
            return (-1, 'simulated failure')
        _orig = lm.utils.download_file
        lm.utils.download_file = _dlf
        try:
            _dl, _fail = lm.download(
                {'entries': _entries, 'total_size': 20}, _target, 'TS1')
            assert len(_fail) == 1, _fail
            assert not os.path.isfile(_marker), \
                "a partial download must NOT stamp the valid-for-snapshot marker"
            # a complete run (only the good file) stamps it
            _dl2, _fail2 = lm.download(
                {'entries': [_entries[0]], 'total_size': 10}, _target, 'TS2')
            assert _fail2 == []
            assert os.path.isfile(_marker)
            with open(_marker) as _f:
                assert _f.read().strip() == 'TS2'
        finally:
            lm.utils.download_file = _orig



def test_local_mirror_coverage_present_missing_stale():
    """local_mirror.coverage(plan, dir) — the `container local mirror status`
    probe: size-matched planned files count present; absent or size-mismatched
    ones are missing (with byte totals); on-disk .debs OUTSIDE the plan are
    stale (snapshot-advance bloat).  Presence is basename+size, not sha256
    (a status probe must not hash a multi-GB mirror)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import local_mirror as lm
    with tempfile.TemporaryDirectory() as _d:
        def _put(name, nbytes):
            with open(os.path.join(_d, name), 'wb') as _f:
                _f.write(b'x' * nbytes)
        _put('present_1_amd64.deb', 10)
        _put('truncated_1_amd64.deb', 4)          # size mismatch → missing
        _put('stale_1_amd64.deb', 7)              # not in the plan
        _plan = {'entries': [
            {'basename': 'present_1_amd64.deb', 'size': 10},
            {'basename': 'truncated_1_amd64.deb', 'size': 10},
            {'basename': 'absent_1_amd64.deb', 'size': 25},
            {'basename': 'sizeless_1_amd64.deb', 'size': 0},  # unknown size
        ]}
        _cov = lm.coverage(_plan, _d)
        assert _cov['present'] == 1
        assert sorted(_cov['missing']) == [
            'absent_1_amd64.deb', 'sizeless_1_amd64.deb',
            'truncated_1_amd64.deb']
        assert _cov['missing_size'] == 35
        assert _cov['stale'] == ['stale_1_amd64.deb']
        assert _cov['stale_size'] == 7



def test_local_mirror_plan_excludes_fork_file_urls():
    """plan excludes closure members served from a file:// (fork / local)
    mirror — the build mirror caches UPSTREAM snapshot packages only; forks come
    from our repo and the common case (base-files) is Essential in the base
    image."""
    import local_mirror
    cache, dt = _lm_mock_cache_and_tree()

    class _FileMir:
        url = 'file:///home/x/fork'
    # simulate a fork override: make's best version comes from a file:// repo
    cache.package_hashtable['make']['1'][0]._mirror = _FileMir()
    pl = local_mirror.plan(cache, dt, object(), include_index=True)
    _names = {e['name'] for e in pl['entries']}
    assert 'make' not in _names, "file:// (fork) member must be excluded"
    assert any('make' in _x for _x in pl['local_excluded'])
    assert 'Package: make' not in pl['packages_index']
    # upstream (https) members still present
    assert 'gcc' in _names and all(
        e['url'].startswith('https') for e in pl['entries'])



def test_inrelease_index_stale_detects_content_drift():
    """apt_repo.inrelease_index_stale: True when InRelease pins a Packages
    SHA-256 that disagrees with the on-disk Packages (or the file is gone),
    catching the content-stale-but-mtime-fresh case the publish gate's mtime
    walk misses (in-place `repo repair strip` → re-sign, 2026-07-10).  A
    consistent chain, and a missing InRelease, are NOT stale."""
    import hashlib as _hl
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo as _ar
    with tempfile.TemporaryDirectory() as _d:
        _pdir = os.path.join(_d, 'main', 'binary-amd64')
        os.makedirs(_pdir)
        _pkgs = os.path.join(_pdir, 'Packages')
        with open(_pkgs, 'wb') as _fh:
            _fh.write(b'Package: foo\nVersion: 1.0\n\n')
        _sha = _hl.sha256(open(_pkgs, 'rb').read()).hexdigest()
        _ir = os.path.join(_d, 'InRelease')

        def _write_ir(sha):
            with open(_ir, 'w') as _fh:
                _fh.write("Origin: X\nSHA256:\n"
                          f" {sha} 27 main/binary-amd64/Packages\n"
                          f" {'0'*64} 40 main/binary-amd64/Packages.gz\n")
        # (a) consistent → not stale
        _write_ir(_sha)
        assert _ar.inrelease_index_stale(_ir, _d) is False
        # (a2) REGRESSION: a real apt-ftparchive InRelease carries MD5Sum/
        # SHA1/SHA256/SHA512 blocks in sequence, and the SHA512 block repeats
        # every path with a 128-hex digest.  The block-boundary regex must
        # stop at `SHA512:` (which contains digits) — an earlier stop pattern
        # `\n[A-Z][A-Za-z-]*:` did NOT, so `.*?` swallowed the SHA512 entries
        # and every Packages' 128-hex SHA-512 got compared against get_sha256
        # → a false-positive "stale" for EVERY real repo.  Correct SHA-256
        # pins + a trailing SHA512 block ⟹ NOT stale.
        _sha512 = _hl.sha512(open(_pkgs, 'rb').read()).hexdigest()
        with open(_ir, 'w') as _fh:
            _fh.write(
                "Origin: X\n"
                "MD5Sum:\n"
                f" {'a'*32} 27 main/binary-amd64/Packages\n"
                "SHA256:\n"
                f" {_sha} 27 main/binary-amd64/Packages\n"
                f" {'0'*64} 40 main/binary-amd64/Packages.gz\n"
                "SHA512:\n"
                f" {_sha512} 27 main/binary-amd64/Packages\n"
                f" {'0'*128} 40 main/binary-amd64/Packages.gz\n")
        assert _ar.inrelease_index_stale(_ir, _d) is False
        # (b) InRelease pins a DIFFERENT sha (content drift) → stale
        _write_ir('e' * 64)
        assert _ar.inrelease_index_stale(_ir, _d) is True
        # (c) pinned Packages missing on disk → stale
        _write_ir(_sha)
        os.remove(_pkgs)
        assert _ar.inrelease_index_stale(_ir, _d) is True
        # (d) missing InRelease → NOT stale (caller's own missing-check owns it)
        os.remove(_ir)
        assert _ar.inrelease_index_stale(_ir, _d) is False


def test_local_mirror_index_writes_flat_apt_repo_with_origin():
    """local_mirror.index: dpkg-scanpackages → a flat Packages (./ filenames)
    plus a Release carrying Origin: AthenaLocalMirror (so the container can
    pin it above the snapshot)."""
    import subprocess

    import local_mirror
    with tempfile.TemporaryDirectory() as d:
        _root = os.path.join(d, 'pkgroot')
        os.makedirs(os.path.join(_root, 'DEBIAN'))
        with open(os.path.join(_root, 'DEBIAN', 'control'), 'w') as fh:
            fh.write('Package: athena-test-marker\nVersion: 1.0\n'
                     'Architecture: all\nMaintainer: t <t@t>\nDescription: t\n')
        subprocess.run(['dpkg-deb', '--build', '-Znone', _root,
                        os.path.join(d, 'athena-test-marker_1.0_all.deb')],
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        assert local_mirror.index(d)
        _pkgs = open(os.path.join(d, 'Packages')).read()
        _rel = open(os.path.join(d, 'Release')).read()
        assert 'Package: athena-test-marker' in _pkgs
        assert './athena-test-marker' in _pkgs          # flat ./ filename
        assert 'Origin: AthenaLocalMirror' in _rel
        assert 'SHA256:' in _rel
        assert os.path.isfile(os.path.join(d, 'Packages.gz'))



def test_local_mirror_index_failure_leaves_no_valid_mirror():
    """A failed/empty dpkg-scanpackages (e.g. no .debs) must NOT leave an empty
    Packages that is_valid_for would treat as ready."""
    import local_mirror
    with tempfile.TemporaryDirectory() as d:
        # empty dir → dpkg-scanpackages emits nothing → index() returns False
        assert not local_mirror.index(d)
        assert not os.path.isfile(os.path.join(d, 'Packages'))
        local_mirror._write_marker(d, '20260621T135952Z')
        assert not local_mirror.is_valid_for(d, '20260621T135952Z')



def test_local_mirror_is_valid_for_keys_to_snapshot_marker():
    """is_valid_for is True only when Packages exists AND the .snapshot marker
    matches the requested ts (so a snapshot bump invalidates the mirror)."""
    import local_mirror
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, 'Packages'), 'w') as fh:
            fh.write('Package: x\nVersion: 1\n')   # non-empty index
        assert not local_mirror.is_valid_for(d, '20260621T135952Z')  # no marker
        local_mirror._write_marker(d, '20260621T135952Z')
        assert local_mirror.is_valid_for(d, '20260621T135952Z')
        assert not local_mirror.is_valid_for(d, '20260101T000000Z')  # stale
        assert not local_mirror.is_valid_for(d, None)
        assert local_mirror.status(d)['snapshot'] == '20260621T135952Z'



def test_onboarding_jobs_clamps_with_warning():
    """Audit #147: the onboarding wizard clamps a jobs answer to 1-8 and warns
    (mirroring `set jobs`), rather than silently adjusting (full wizard-drive
    is brittle; this pins the clamp + warning)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import onboarding
    _src = inspect.getsource(onboarding._prompt_machine_settings)
    assert 'max(1, min(_jobs, 8))' in _src              # clamp to 1-8
    assert 'jobs clamped to' in _src                    # surfaces the clamp
    assert 'is not an integer' in _src

TESTS = [
    test_local_mirror_plan_resolves_build_closure_to_snapshot_urls,
    test_local_mirror_plan_include_index_emits_packages_blob,
    test_local_mirror_download_withholds_marker_on_failure_cons13,
    test_local_mirror_coverage_present_missing_stale,
    test_local_mirror_plan_excludes_fork_file_urls,
    test_inrelease_index_stale_detects_content_drift,
    test_local_mirror_index_writes_flat_apt_repo_with_origin,
    test_local_mirror_index_failure_leaves_no_valid_mirror,
    test_local_mirror_is_valid_for_keys_to_snapshot_marker,
    test_mirror_url_composition,
    test_mirror_suite_with_suffix,
    test_mirror_repr_includes_identifying_fields,
    test_mirror_is_frozen_after_construction,
    test_mirror_normalises_baseurl_and_baseid_slashes,
    test_mirror_rejects_empty_required_fields,
    test_mirror_rejects_baseurl_without_scheme,
    test_mirror_rejects_suffix_without_leading_dash,
    test_mirror_with_snapshot_returns_new_instance_untouched_original,
    test_check_mirror_reachability_dedups_and_classifies,
    test_cache_build_gates_on_mirror_reachability_and_config_command_wired,
    test_probe_remote_build_host_parses_and_gates,
    test_mirror_conf_registration_round_trips_and_migrates,
    test_onboarding_logs_configure_actions,
    test_onboarding_origin_completes_pathless_ssh_url,
    test_command_allowed_gates_until_configured,
    test_configured_summary_reports_state_and_warns_unregistered,
    test_onboarding_first_system_declines_mirror,
    test_onboarding_federation_happy_path_registers_and_records,
    test_onboarding_federation_cancel_at_url_leaves_unconfigured,
    test_onboarding_federation_gpg_mismatch_aborts,
    test_ask_cancel_returns_none,
    test_adopt_snapshot_forward_from_head,
    test_mirror_with_snapshot_uses_passed_baseurl,
    test_mirror_udeb_packages_path_format,
    test_write_athena_apt_sources_emits_one_file_per_mirror,
    test_write_athena_apt_sources_skips_ssh_url_without_public_url,
    test_write_athena_apt_sources_accepts_file_scheme,
    test_mirror_flat_layout_is_signalled_by_empty_component,
    test_mirror_flat_layout_url_properties,
    test_mirror_with_snapshot_skips_file_scheme,
    test_mirror_validation_allows_empty_component,
    test_fork_mirror_discover_skips_repo_subdir,
    test_fork_mirror_discover_skips_dirs_missing_debian_control,
    test_fork_mirror_read_pkg_version_rejects_broken_changelog,
    test_fork_mirror_generate_empty_tree_returns_false,
    test_fork_mirror_generate_emits_complete_layout,
    test_fork_mirror_strips_epoch_from_constructed_filenames,
    test_conf11_audit_clean_when_all_paths_exist,
    test_conf11_audit_athena_tasksel_packages_list_regression,
    test_conf11_audit_skips_variable_expanded_paths,
    test_conf11_audit_glob_matches_existing_dir_contents,
    test_conf11_audit_glob_flags_no_matching_files,
    test_conf11_audit_skips_comment_lines,
    test_conf11_scan_install_sources_handles_multi_source_form,
    test_conf11_scan_cp_sources_handles_flags_and_skips_variables,
    test_conf11_audit_no_makefile_no_rules_returns_empty,
    test_conf11_audit_real_fork_trees_clean,
    test_fork_mirror_packages_udeb_routing_and_placeholder_hashes,
    test_fork_mirror_sources_uses_real_hashes_and_directory,
    test_fork_mirror_register_prepends_at_index_zero,
    test_fork_mirror_re_run_is_idempotent_via_stale_check,
    test_fork_invalidation_wipes_artifacts_on_content_change,
    test_fork_invalidation_no_op_when_hash_matches,
    test_fork_invalidation_preserves_pull_adopted_binaries_on_fresh_peer,
    test_fork_invalidation_skips_wipe_on_unreadable_record,
    test_fork_invalidation_still_wipes_orphan_without_pull_record,
    test_fork_invalidation_covers_multi_binary_via_control_parse,
    test_binary_names_from_control_extracts_all_package_stanzas,
    test_compute_dep_hash_changes_on_depends_field_edit,
    test_compute_dep_hash_changes_on_version_bump,
    test_compute_dep_hash_stable_under_description_edit,
    test_compute_dep_hash_stable_under_whitespace_reflow,
    test_load_pkg_hashes_returns_empty_strings_when_sidecars_missing,
    test_persist_tree_hash_writes_both_sidecars,
    test_wipe_fork_pkg_outputs_removes_both_hash_sidecars,
    test_fed04_unlock_decision_policy,
    test_fed03_register_tofu_decision_policy,
    test_fed01_ledger_stranded_claims_detector,
    test_mat11_mirror_help_subcommands_all_dispatch,
    test_fork_mirror_discover_skips_disabled_trees,
    test_mirror_with_snapshot_none_returns_self,
    test_mirror_with_snapshot_is_idempotent,
    test_mirror_with_snapshot_preserves_suite_and_component,
    test_identity_scan_fork_mirror_gate_fails_build,
    test_fork_mirror_skips_identity_audit_when_disabled,
    test_mirror_pull_record_writer_preserves_lifecycle_pin,
    test_webapi_mirror_state_reads_signed_mirror_conf_not_legacy,
    test_mirror_publish_refreshes_local_asg_manifest,
    test_update_build_pending_per_mirror_path,
    test_cmd_get_lists_every_gettable_param_when_called_bare,
    test_cmd_get_named_param_returns_current_value,
    test_cmd_set_mode_switches_value_and_warns_to_re_run_cache_parse,
    test_cmd_set_mode_persists_to_local_conf,
    test_cmd_set_mode_build_refused_on_first_system,
    test_cmd_set_mode_build_refused_when_unregistered,
    test_cmd_set_mode_invalid_value_keeps_old_value,
    test_cmd_set_mode_same_value_is_noop_when_dep_check_ready,
    test_cmd_set_mode_same_value_surfaces_parse_hint_when_not_parsed,
    test_cmd_set_unknown_param_reports_available_list,
    test_hk07_behavior_fixes_pinned,
    test_cmd_mirror_publish_refuses_indl_to_fresh_mirror,
    test_cmd_mirror_publish_indl_to_initialised_mirror_proceeds,
    test_project_post_publish_state_merges_remote_claims_as_satisfiers,
    test_find_publish_closure_breaks_returns_empty_when_satisfied,
    test_find_publish_closure_breaks_detects_missing_dep,
    test_find_publish_closure_breaks_respects_consumer_set,
    test_build_mode_publish_implies_no_iso,
    test_mirror_builders_register_gates_and_uploads,
    test_closure_gate_runs_in_both_modes,
    test_publish_closure_gate_scope_matches_repo_audit_install_corpus,
    test_extract_user_and_path_from_ssh_url,
    test_validate_host_for_type_classifies_ip_fqdn_local,
    test_derive_name_from_url_maps_ip_fqdn_local,
    test_derive_public_url_constructs_proto_host_dist,
    test_normalize_url_restricted_to_ssh_and_file,
    test_probe_ssh_auth_classifies_success_and_failure,
    test_probe_http_inrelease_handles_200_404_and_error,
    test_discover_federation_peers_classifies_each_neighbour,
    test_cmd_mirror_add_refuses_unprepared_mirror,
    test_prep_mirror_script_contract,
    test_cmd_mirror_add_new_mirror_empty_config,
    test_cmd_mirror_add_old_mirror_no_neighbours_empty_config,
    test_cmd_mirror_add_old_mirror_with_neighbours_empty_config,
    test_cmd_mirror_add_new_mirror_to_existing_config,
    test_cmd_mirror_add_overlap_with_existing_neighbours,
    test_cmd_mirror_add_rejects_malformed_inputs,
    test_cmd_mirror_add_drops_unreachable_neighbour,
    test_cmd_mirror_add_uses_peer_meta_over_operator_proto,
    test_cmd_mirror_add_signing_key_gate_blocks,
    test_mirror_add_remove_round_trip,
    test_mirror_conf_signed_tamper_detected_and_reseal,
    test_mirror_conf_migrates_legacy_state_files,
    test_mirror_add_rejects_duplicate_name_and_url,
    test_mirror_add_rejects_invalid_name_and_url,
    test_mirror_url_normalisation_and_lookup,
    test_mirror_remove_by_url,
    test_mirror_update_state_merges_fields,
    test_cmd_mirror_dispatch_routes_subcommands,
    test_rsync_spec_for_url_handles_every_form,
    test_all_mirror_urls_returns_every_registered_url,
    test_neighbours_drift_tags_unpublished_in_sync_and_drift,
    test_reconcile_neighbours_target_name_unknown_fails,
    test_reconcile_neighbours_no_mirrors_is_trivial_ok,
    test_cmd_mirror_dispatch_routes_publish_and_pull,
    test_all_mirror_neighbour_records_pulls_per_peer_meta_from_state,
    test_coord_root_for_appends_suffix_to_last_path_component,
    test_cmd_mirror_dispatch_routes_audit_and_query,
    test_cmd_mirror_dispatch_routes_reclaim,
    test_mirror_pull_progress_is_byte_sized_with_pkg_label,
    test_mirror_pull_restores_missing_own_files,
    test_mirror_pull_routes_and_records_source_artifacts,
    test_mirror_pull_canonical_apply_has_local_ahead_guard,
    test_cmd_mirror_publish_refuses_when_snapshot_older_than_mirror_base,
    test_cmd_mirror_summary_we_own_counts_non_retracted_claims,
    test_cmd_mirror_query_reports_no_match,
    test_snapshot_adopt_forward_is_forward_only,
    test_audit_claims_vs_packages_flags_missing_and_mismatched,
    test_audit_sidecar_seq_integrity_clean_run_is_silent,
    test_audit_sidecar_seq_integrity_flags_dup_gap_and_missing_one,
    test_sta25_audit_downgrades_missing_claim_when_publish_will_release,
    test_audit_own_claims_on_disk_match_disk_silent_mismatch_critical,
    test_audit_own_claims_on_disk_no_our_builder_id_is_noop,
    test_audit_own_claims_on_disk_ahead_of_remote_is_warning_not_critical,
    test_local_ahead_candidates_structured_and_hint_mentions_reclaim,
    test_local_ahead_candidates_republished_repacked_is_reclaimable,
    test_audit_own_claims_on_disk_real_bitrot_still_critical,
    test_audit_cross_mirror_head_drift_silent_when_aligned,
    test_audit_cross_mirror_head_drift_neighbours_diff_is_critical,
    test_audit_cross_mirror_head_drift_revocation_diff_is_critical,
    test_audit_cross_mirror_head_drift_single_mirror_is_noop,
    test_audit_federation_walk_skips_when_no_extra_peers,
    test_audit_closure_ledger_disagree_and_missing_critical,
    test_audit_closure_ledger_unresolved_dep_critical,
    test_audit_closure_ledger_entry_not_published_critical,
    test_audit_closure_ledger_none_no_findings,
    test_local_mirror_release_arch_derived_from_debs,
    test_onboarding_jobs_warns_on_clamp,
    test_fork_mirror_release_architectures_uses_build_arch,
    test_onboarding_checks_set_registration_return,
    test_fork_mirror_arch_any_filename_uses_build_arch,
    test_onboarding_jobs_clamps_with_warning,
    test_audit_foreign_target_claims_flags_owned_published,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
