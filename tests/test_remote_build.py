"""Athena tests — remote builders (remote_agent.py, remote_build.py, remote_localmirror.py, remote_orchestrate.py).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile

from _test_helpers import (  # noqa: F401
    _BASE_CONF_BODY,
    _MINIMAL_MIRROR_BLOCK,
    _ROOT,
    _build_config_from,
    _remote_agent_req,
    _remote_agent_spawn,
    _remote_agent_wait_terminal,
    _run_remote_agent_fakes,
    _session_source,
    _write_test_config,
)




def test_remote_conf_helpers_round_trip():
    """REMOTE-CONF: add_remote / list_remotes / delete_remote round-trip
    through config/remote.conf, validate names, and BuildConfig derives
    remote_build_host from the first configured remote."""
    import utils
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.remotes == []
        assert cfg.remote_build_host == ''
        ok, _ = utils.add_remote(
            cfg, name='athena', host='ssh://u@10.0.0.7',
            ssh_key='config/remote_athena.key', max_parallel_builds=4,
            build_memory='24g')
        assert ok
        # invalid names rejected (becomes a section/filename component)
        assert utils.add_remote(cfg, name='bad/name', host='x')[0] is False
        assert utils.add_remote(cfg, name='ok', host='')[0] is False
        _rs = utils.list_remotes(cfg)
        assert len(_rs) == 1 and _rs[0]['name'] == 'athena'
        assert _rs[0]['host'] == 'ssh://u@10.0.0.7'
        assert _rs[0]['max_parallel_builds'] == 4
        assert _rs[0]['build_memory'] == '24g'
        # BuildConfig picks up the first remote as remote_build_host.
        cfg2 = _build_config_from(tmp, cfg_path)
        assert cfg2.remote_build_host == 'ssh://u@10.0.0.7'
        assert len(cfg2.remotes) == 1
        # delete: gone from the registry, missing-name reported.
        assert utils.delete_remote(cfg, 'athena')[0] is True
        assert utils.delete_remote(cfg, 'athena')[0] is False
        assert utils.list_remotes(cfg) == []



def test_remote_token_generate_and_path():
    """REMOTE-API: generate_remote_token writes a 64-hex token 0600 to
    config/<name>.remote-token; remote_token_path derives that path; a second
    call rotates it."""
    import utils
    import stat
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        _path, _tok = utils.generate_remote_token(cfg, 'bs7')
        assert _path == utils.remote_token_path(cfg, 'bs7')
        assert _path.endswith('bs7.remote-token')
        assert len(_tok) == 64 and all(_c in '0123456789abcdef' for _c in _tok)
        assert open(_path).read().strip() == _tok
        assert stat.S_IMODE(os.stat(_path).st_mode) == 0o600
        _, _tok2 = utils.generate_remote_token(cfg, 'bs7')
        assert _tok2 != _tok



def test_remote_localmirror_cum_total_guards_null_total_size():
    """Regression (audit #165): the cumulative total must coerce a JSON-null
    total_size like line 187 — int(None) raises."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_localmirror
    assert "int(plan.get('total_size', 0) or 0) or 1" in inspect.getsource(
        remote_localmirror), "_cum_total must guard a null total_size with `or 0`"



def test_remote_orchestrate_open_tunnel_sets_strict_host_key_checking():
    """Regression (audit #168): _open_tunnel's ssh argv must include
    StrictHostKeyChecking=accept-new like _ssh_base."""
    import inspect
    import re
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_orchestrate
    # ExitOnForwardFailure is unique to _open_tunnel; assert StrictHostKey
    # follows it in the same argv.
    assert re.search(
        r"ExitOnForwardFailure=yes',\s*\n?\s*'-o',"
        r"\s*'StrictHostKeyChecking=accept-new'",
        inspect.getsource(remote_orchestrate)), (
        "_open_tunnel argv must carry StrictHostKeyChecking=accept-new")



def test_remote_build_run_container_command_shape():
    """remote_build.run_container builds a `docker run` with the three local
    bind mounts (source/patch/repo), the cpu/mem caps, and `bash -c <cmd_str>`."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_build
    with tempfile.TemporaryDirectory() as _b:
        os.makedirs(os.path.join(_b, 'source'))
        with mock.patch.object(remote_build, 'subprocess') as _sp:
            _sp.run.return_value = mock.Mock(returncode=0)
            _rc = remote_build.run_container(
                _b, 'athenalinux:build-x', 'set -e; dpkg-buildpackage',
                7.0, '28g')
        assert _rc == 0
        _argv = _sp.run.call_args[0][0]
        _joined = ' '.join(_argv)
        assert _argv[:3] == ['docker', 'run', '--rm']
        assert '/source:rw' in _joined and '/patch:rw' in _joined and \
            '/repo:rw' in _joined
        assert '--cpus' in _argv and '7.0' in _argv
        assert '--memory' in _argv and '28g' in _argv
        assert _argv[-3:] == ['bash', '-c', 'set -e; dpkg-buildpackage']
        # out/ was created world-writable for the container's copy-out
        assert os.path.isdir(os.path.join(_b, 'out'))



def test_remote_agent_helpers_name_and_log_offsets():
    """REMOTE-API: container_name_for sanitises; read_log returns
    (bytes-from-offset, next, size) for incremental / resumable polling."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_agent as _ra
    assert _ra.container_name_for('/tmp/athena-remote-adduser-0') == \
        'athena-build-athena-remote-adduser-0'
    assert _ra.container_name_for('/p/weird name!') == 'athena-build-weird-name-'
    with tempfile.TemporaryDirectory() as _d:
        _f = os.path.join(_d, 'build.log')
        with open(_f, 'wb') as _fh:
            _fh.write(b'hello\nworld\n')
        _data, _next, _size = _ra.read_log(_f, 0)
        assert _data == b'hello\nworld\n' and _next == 12 and _size == 12
        # resume from an offset → only the tail
        _data2, _next2, _size2 = _ra.read_log(_f, 6)
        assert _data2 == b'world\n' and _next2 == 12
        # at/after EOF → empty, offset unchanged
        assert _ra.read_log(_f, 12) == (b'', 12, 12)
        # missing file → benign empties
        assert _ra.read_log(os.path.join(_d, 'nope'), 0) == (b'', 0, 0)



def test_remote_agent_lifecycle_success_logs_and_auth():
    """REMOTE-API: a successful build reaches phase=done with its outputs;
    /logs serves from an offset; a missing/wrong token is rejected 401."""
    import json as _j
    import urllib.error as _ue
    with tempfile.TemporaryDirectory() as _b:
        _p, _port = _remote_agent_spawn(
            _b, 'echo hello; : > out/pkg_1.0_amd64.deb; echo done')
        try:
            _st = _remote_agent_wait_terminal(_port)
            assert _st['phase'] == 'done', _st
            assert _st['outputs'] == ['pkg_1.0_amd64.deb'], _st
            assert _st['exit_code'] == 0
            # logs from offset 0, then resume from next → empty
            _code, _body, _hdrs = _remote_agent_req(_port, '/logs?from=0')
            assert b'hello' in _body and b'done' in _body
            _nxt = int(_hdrs['X-Log-Next'])
            assert _remote_agent_req(_port, f'/logs?from={_nxt}')[1] == b''
            # wrong token → 401
            try:
                _remote_agent_req(_port, '/status', token='wrong')
                raise AssertionError('expected 401 for a bad token')
            except _ue.HTTPError as _e:
                assert _e.code == 401
            # no token → 401
            try:
                _remote_agent_req(_port, '/status', token=None)
                raise AssertionError('expected 401 for a missing token')
            except _ue.HTTPError as _e:
                assert _e.code == 401
        finally:
            try:
                _remote_agent_req(_port, '/shutdown', 'POST')
            except Exception:
                pass
            try:
                _p.wait(timeout=10)
            except Exception:
                _p.kill()
        assert _j is not None  # keep import used



def test_remote_agent_watchdog_and_abort():
    """REMOTE-API: a no-progress build is killed by the watchdog
    (phase=timeout, watchdog=hang-killed); POST /abort stops a build
    (phase=aborted)."""
    # watchdog: a sleeping build emits no log growth → hang-killed after ~1s
    with tempfile.TemporaryDirectory() as _b:
        _p, _port = _remote_agent_spawn(_b, 'sleep 30', hang=1)
        try:
            _st = _remote_agent_wait_terminal(_port)
            assert _st['phase'] == 'timeout', _st
            assert _st['watchdog'] == 'hang-killed', _st
        finally:
            try:
                _remote_agent_req(_port, '/shutdown', 'POST')
            except Exception:
                pass
            try:
                _p.wait(timeout=10)
            except Exception:
                _p.kill()
    # abort: a long build, explicitly aborted → phase=aborted
    with tempfile.TemporaryDirectory() as _b:
        _p, _port = _remote_agent_spawn(_b, 'sleep 30', hang=300)
        try:
            _remote_agent_req(_port, '/abort', 'POST')
            _st = _remote_agent_wait_terminal(_port)
            assert _st['phase'] == 'aborted', _st
        finally:
            try:
                _remote_agent_req(_port, '/shutdown', 'POST')
            except Exception:
                pass
            try:
                _p.wait(timeout=10)
            except Exception:
                _p.kill()



def test_run_remote_agent_polls_to_done_ships_token_and_reaps():
    """REMOTE-API: run_remote_agent ships the agent + token, starts it
    detached, tunnels + polls to phase=done, recovers the .deb, and reaps the
    named container in the finally."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_orchestrate as _ro
    with tempfile.TemporaryDirectory() as _bundle, \
            tempfile.TemporaryDirectory() as _out:
        open(os.path.join(_bundle, 'Dockerfile'), 'w').write('FROM x\n')
        _agentpy = os.path.join(_bundle, '_agent_src.py')
        open(_agentpy, 'w').write('# agent\n')
        _fsp, _req, _calls = _run_remote_agent_fakes(
            _out, phase_seq=['running', 'done'])
        with mock.patch.object(_ro, 'subprocess', _fsp), \
                mock.patch.object(_ro, '_agent_request', _req), \
                mock.patch.object(_ro, 'AGENT_POLL_INTERVAL', 0):
            _exit, _outputs = _ro.run_remote_agent(
                'u@h', _bundle, '~/athena-remote-adduser-0', _out,
                token='deadbeef', ssh_key='k.key',
                remote_agent_py=_agentpy, hang_secs=1800)
        assert (_exit, _outputs) == (0, ['adduser_1_amd64.deb']), (_exit, _outputs)
        # token written 0600 into the bundle + agent staged
        assert os.path.isfile(os.path.join(_bundle, 'agent.token'))
        assert os.path.isfile(os.path.join(_bundle, 'remote_agent.py'))
        _runs = [' '.join(_a) for _a in _calls['run']]
        assert any('nohup python3 remote_agent.py' in _r and
                   '--token-file agent.token' in _r for _r in _runs)
        # tunnel forwards to the agent's reported remote port
        assert any('-L' in _a and '127.0.0.1:54321' in ' '.join(_a)
                   for _a in _calls['popen'])
        # finally reaps the named container + wipes the scratch dir
        assert any('docker rm -f athena-build-athena-remote-adduser-0' in _r and
                   'rm -rf' in _r for _r in _runs)



def test_run_remote_agent_watchdog_timeout_is_transport():
    """REMOTE-API: a phase=timeout (remote watchdog killed a hung build) maps
    to exit 12 so the fan-out scheduler re-queues it."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_orchestrate as _ro
    with tempfile.TemporaryDirectory() as _bundle, \
            tempfile.TemporaryDirectory() as _out:
        open(os.path.join(_bundle, 'Dockerfile'), 'w').write('FROM x\n')
        _fsp, _req, _calls = _run_remote_agent_fakes(
            _out, phase_seq=['running', 'timeout'])
        with mock.patch.object(_ro, 'subprocess', _fsp), \
                mock.patch.object(_ro, '_agent_request', _req), \
                mock.patch.object(_ro, 'AGENT_POLL_INTERVAL', 0):
            _exit, _outputs = _ro.run_remote_agent(
                'u@h', _bundle, '~/athena-remote-x', _out, token='t')
        assert _exit == 12 and _outputs == [], (_exit, _outputs)



def test_remote_build_image_uses_args_and_skips_when_present():
    """build_image passes --build-arg for each param and SKIPS the build when
    the tag already exists (cached on the remote after run 1)."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_build
    with tempfile.TemporaryDirectory() as _b:
        with open(os.path.join(_b, 'Dockerfile'), 'w') as _fh:
            _fh.write('FROM debian:bookworm-slim\n')
        # present → no docker build
        with mock.patch.object(remote_build, '_image_exists', return_value=True), \
                mock.patch.object(remote_build, 'subprocess') as _sp:
            remote_build.build_image(_b, 'tag', {'RELEASE': 'bookworm'})
            _sp.run.assert_not_called()
        # absent → docker build with --build-arg
        with mock.patch.object(remote_build, '_image_exists', return_value=False), \
                mock.patch.object(remote_build, 'subprocess') as _sp:
            _sp.run.return_value = mock.Mock(returncode=0)
            remote_build.build_image(
                _b, 'tag', {'RELEASE': 'bookworm', 'SNAPSHOT_TS': '20260602T0Z'})
            _argv = _sp.run.call_args[0][0]
            assert _argv[:3] == ['docker', 'build', '-t']
            assert '--build-arg' in _argv and 'RELEASE=bookworm' in _argv
            assert 'SNAPSHOT_TS=20260602T0Z' in _argv



def test_remote_build_main_emits_result_marker():
    """main() loads build.json, runs the build, and prints a single machine-
    readable result line listing the produced artifacts; exit 0 on success,
    exit 3 on a clean run that produced nothing."""
    from unittest import mock
    import io
    import json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_build
    with tempfile.TemporaryDirectory() as _b:
        with open(os.path.join(_b, 'Dockerfile'), 'w') as _fh:
            _fh.write('FROM debian:bookworm-slim\n')
        with open(os.path.join(_b, 'build.json'), 'w') as _fh:
            json.dump({'package': 'adduser', 'image_tag': 'tag',
                       'cmd_str': 'true', 'build_args': {}}, _fh)
        os.makedirs(os.path.join(_b, 'out'))
        # success: an artifact present → exit 0 + marker lists it
        with open(os.path.join(_b, 'out', 'adduser_3.1_all.deb'), 'w') as _fh:
            _fh.write('x')
        with mock.patch.object(remote_build, '_image_exists', return_value=True), \
                mock.patch.object(remote_build, 'subprocess') as _sp, \
                mock.patch('sys.stdout', new=io.StringIO()) as _out:
            _sp.run.return_value = mock.Mock(returncode=0)
            _rc = remote_build.main([_b])
        _printed = _out.getvalue()
        assert _rc == 0
        assert remote_build.RESULT_MARKER in _printed
        _line = [ln for ln in _printed.splitlines()
                 if ln.startswith(remote_build.RESULT_MARKER)][0]
        _res = json.loads(_line[len(remote_build.RESULT_MARKER):])
        assert _res['exit_code'] == 0
        assert _res['outputs'] == ['adduser_3.1_all.deb']
        # clean exit but no artifacts → exit 3
        os.remove(os.path.join(_b, 'out', 'adduser_3.1_all.deb'))
        with mock.patch.object(remote_build, '_image_exists', return_value=True), \
                mock.patch.object(remote_build, 'subprocess') as _sp, \
                mock.patch('sys.stdout', new=io.StringIO()):
            _sp.run.return_value = mock.Mock(returncode=0)
            assert remote_build.main([_b]) == 3



def test_remote_orchestrate_parse_host_stage_and_result():
    """parse_ssh_host normalises ssh:// URLs; stage_bundle lays out the bundle
    (Dockerfile, source/, patch/, remote_build.py, build.json params)."""
    import json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_orchestrate as _ro
    assert _ro.parse_ssh_host('ssh://h@1.2.3.4') == 'h@1.2.3.4'
    assert _ro.parse_ssh_host('h@1.2.3.4') == 'h@1.2.3.4'
    assert _ro.parse_ssh_host('ssh://u@h/') == 'u@h'
    with tempfile.TemporaryDirectory() as _t:
        _df = os.path.join(_t, 'Dockerfile')
        with open(_df, 'w') as _f:
            _f.write('FROM debian:bookworm-slim\n')
        _rb = os.path.join(_t, 'remote_build.py')
        with open(_rb, 'w') as _f:
            _f.write('# rb\n')
        _sd = os.path.join(_t, 'src')
        os.makedirs(_sd)
        _s1 = os.path.join(_sd, 'adduser_3.134.dsc')
        with open(_s1, 'w') as _f:
            _f.write('d')
        _pd = os.path.join(_t, 'patch')
        os.makedirs(_pd)
        with open(os.path.join(_pd, '9001-x.patch'), 'w') as _f:
            _f.write('p')
        _recipe = {'filename_prefix': 'adduser', 'image_tag': 'tag',
                   'build_args': {'RELEASE': 'bookworm'}, 'cmd_str': 'CMD'}
        _bundle = os.path.join(_t, 'bundle')
        os.makedirs(_bundle)
        _ro.stage_bundle(_bundle, dockerfile=_df, source_files=[_s1],
                         patch_dir=_pd, recipe=_recipe, build_cpus=7.0,
                         build_memory='28g', remote_build_py=_rb)
        assert os.path.isfile(os.path.join(_bundle, 'Dockerfile'))
        assert os.path.isfile(os.path.join(_bundle, 'remote_build.py'))
        assert os.path.isfile(
            os.path.join(_bundle, 'source', 'adduser_3.134.dsc'))
        assert os.path.isfile(os.path.join(_bundle, 'patch', '9001-x.patch'))
        with open(os.path.join(_bundle, 'build.json')) as _f:
            _bj = json.load(_f)
        assert _bj['image_tag'] == 'tag' and _bj['cmd_str'] == 'CMD'
        assert _bj['build_args']['RELEASE'] == 'bookworm'
        assert _bj['build_cpus'] == 7.0 and _bj['build_memory'] == '28g'



def test_ensure_remote_image_lan_transfer_paths():
    """ensure_remote_image: remote-has → 'present'; remote-lacks + local-has →
    LAN docker save|load → 'transferred'; neither → 'build' (remote builds)."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_orchestrate as _ro
    _quiet = lambda *_a: None       # noqa: E731
    # remote already has it → present (one inspect, returncode 0)
    with mock.patch.object(_ro.subprocess, 'run',
                           return_value=mock.Mock(returncode=0)):
        assert _ro.ensure_remote_image('h', 'tag', log=_quiet) == 'present'
    # neither has it → build (both inspects non-zero)
    with mock.patch.object(_ro.subprocess, 'run',
                           return_value=mock.Mock(returncode=1)):
        assert _ro.ensure_remote_image('h', 'tag', log=_quiet) == 'build'
    # remote lacks, local has → LAN transfer
    _seq = [mock.Mock(returncode=1),    # remote inspect: absent
            mock.Mock(returncode=0)]    # local inspect: present

    class _P:
        returncode = 0
        stdout = mock.Mock()

        def wait(self):
            return 0

    with mock.patch.object(_ro.subprocess, 'run', side_effect=_seq), \
            mock.patch.object(_ro.subprocess, 'Popen', return_value=_P()):
        assert _ro.ensure_remote_image('h', 'tag', log=_quiet) == 'transferred'



def test_orchestrator_ssh_key_threads_into_argv():
    """`remote_orchestrate._ssh_base`/`_scp_base` insert `-i <key>` so every
    ssh/scp call honours the per-remote key; _has_image builds the keyed argv."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_orchestrate as _ro
    _akn = ['-o', 'StrictHostKeyChecking=accept-new']
    assert _ro._ssh_base('u@h', '/k') == [
        'ssh', '-o', 'BatchMode=yes', *_akn, '-i', '/k', 'u@h']
    assert _ro._ssh_base('u@h', None) == [
        'ssh', '-o', 'BatchMode=yes', *_akn, 'u@h']
    assert _ro._scp_base('/k') == ['scp', '-q', *_akn, '-i', '/k']
    assert _ro._scp_base(None) == ['scp', '-q', *_akn]
    # _has_image(remote) routes through _ssh_base → the key reaches the argv.
    import subprocess as _sp
    _seen = {}

    def _fake_run(argv, **k):
        _seen['argv'] = argv
        return type('R', (), {'returncode': 0})()
    _orig = _sp.run
    _sp.run = _fake_run
    try:
        _ro._has_image('img:t', ssh_host='u@h', ssh_key='/k')
    finally:
        _sp.run = _orig
    assert '-i' in _seen['argv'] and '/k' in _seen['argv']



def test_remotebuild_shares_workload_resolver_and_fans_out():
    """`source remotebuild` reuses `_resolve_build_workload` (remote=True, so
    same subset/all/force/names surface as `source build`) and fans out across
    `config.remotes` via `_remotebuild_fanout`."""
    import re
    _body = _session_source()
    _rb = re.search(r'def cmd_source_remotebuild\(self.*?(?=\n    def )',
                    _body, re.DOTALL).group(0)
    assert '_resolve_build_workload(' in _rb and 'remote=True' in _rb, (
        "remotebuild must share source build's arg/workload front-half")
    assert '_remotebuild_fanout(' in _rb, "remotebuild must fan out"
    assert "getattr(self.config, 'remotes'" in _rb, (
        "remotebuild must target every configured remote")
    # source build shares the SAME resolver (remote=False).
    _sb = re.search(r'def cmd_source_build\(self.*?(?=\n    def )',
                    _body, re.DOTALL).group(0)
    assert '_resolve_build_workload(args, remote=False)' in _sb



def test_remotebuild_handles_update_mode_cons10():
    """CONS-10: `source remotebuild` honours update mode end-to-end.  A pending
    delta routes through `_do_update_build(remote=True)`, which fans the delta
    out to the remotes; `_remotebuild_one_source` carries the locally-computed
    bump decision into a `not _bump_target` skip-gate exception so a same-base
    re-spin is never wrongly skipped (and so never ships unstamped)."""
    import re
    _body = _session_source()
    # update-mode detection is no longer gated on `not remote`.
    _rw = re.search(r'def _resolve_build_workload\(self.*?(?=\n    def )',
                    _body, re.DOTALL).group(0)
    assert 'not remote and not _names' not in _rw, (
        "update-mode detection must apply to remote builds too (CONS-10)")
    # remotebuild routes a pending update to the remote update path.
    _rb = re.search(r'def cmd_source_remotebuild\(self.*?(?=\n    def )',
                    _body, re.DOTALL).group(0)
    assert "_status == 'update'" in _rb and \
        '_do_update_build(remote=True)' in _rb, (
        "remotebuild must route a pending update through "
        "_do_update_build(remote=True)")
    # _do_update_build dispatches to the remote fan-out when remote=True.
    assert 'def _do_update_build(self, remote: bool = False)' in _body
    _du = re.search(r'def _do_update_build\(self.*?(?=\n    def )',
                    _body, re.DOTALL).group(0)
    assert 'cmd_source_remotebuild(' in _du, (
        "_do_update_build(remote=True) must dispatch to the remote fan-out")
    # the worker carries the bump decision into the skip-gate exception.
    _one = re.search(r'def _remotebuild_one_source\(self.*?(?=\n    def )',
                     _body, re.DOTALL).group(0)
    assert 'bump_active' in _one and 'bump_release' in _one, (
        "_remotebuild_one_source must accept the bump decision as parameters")
    assert '_needs_bump_build(' in _one and 'not _bump_target' in _one, (
        "_remotebuild_one_source must exempt a bump-target from the skip gate")



def test_remote_localmirror_populate_downloads_skips_indexes():
    """remote_localmirror.populate: download (resumable, sha-verified) + write a
    flat apt index (Origin: AthenaLocalMirror) + .snapshot marker; re-run skips
    complete files; a sha256 mismatch is reported as a failure."""
    import hashlib
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_localmirror as rlm
    with tempfile.TemporaryDirectory() as tmp:
        _origin = os.path.join(tmp, 'origin')
        os.makedirs(_origin)
        _target = os.path.join(tmp, 'lm')
        _entries = []
        _stanzas = []
        _total = 0
        for _name, _size in [('aaa_1_amd64.deb', 2000), ('bbb_2_amd64.deb', 3000)]:
            _data = bytes((_i * 7) % 256 for _i in range(_size))
            with open(os.path.join(_origin, _name), 'wb') as _f:
                _f.write(_data)
            _sha = hashlib.sha256(_data).hexdigest()
            _entries.append({'basename': _name,
                             'url': 'file://' + os.path.join(_origin, _name),
                             'sha256': _sha, 'size': _size})
            _stanzas.append(f"Package: {_name.split('_')[0]}\n"
                            f"Filename: ./{_name}\nSize: {_size}\n"
                            f"SHA256: {_sha}\n")
            _total += _size
        _plan = {'snapshot_ts': '20260101T000000Z', 'total_size': _total,
                 'entries': _entries, 'packages_index': '\n'.join(_stanzas)}
        _r = rlm.populate(_plan, _target)
        assert _r['downloaded'] == 2 and _r['failed'] == 0
        assert _r['indexed'] is True
        for _n in ('Packages', 'Packages.gz', 'Packages.xz', 'Release',
                   '.snapshot'):
            assert os.path.isfile(os.path.join(_target, _n)), _n
        with open(os.path.join(_target, 'Release')) as _f:
            assert 'Origin: AthenaLocalMirror' in _f.read()
        with open(os.path.join(_target, '.snapshot')) as _f:
            assert _f.read().strip() == '20260101T000000Z'
        # re-run → all skipped (size-match, no re-download)
        _r2 = rlm.populate(_plan, _target)
        assert _r2['skipped'] == 2 and _r2['downloaded'] == 0
        # sha256 mismatch → failure (force re-download by removing the file)
        os.remove(os.path.join(_target, 'aaa_1_amd64.deb'))
        _bad = {'snapshot_ts': 'x', 'total_size': _total,
                'entries': [{'basename': 'aaa_1_amd64.deb',
                             'url': _entries[0]['url'],
                             'sha256': 'deadbeef' * 8, 'size': 2000}],
                'packages_index': 'Package: x\nFilename: ./x\n'}
        _r3 = rlm.populate(_bad, _target)
        assert _r3['failed'] == 1



def test_remote_localmirror_hardening_cons13():
    """CONS-13: populate does a disk-space preflight (refuse a run we can't
    finish) returning a clean __disk__ failure, and _download catches
    http.client.HTTPException so a dropped chunked transfer (IncompleteRead)
    records ONE failure instead of aborting the whole population."""
    import inspect as _inspect
    import types as _types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_localmirror as rlm
    # _download catches HTTPException (http.client.IncompleteRead subclasses it)
    assert 'http.client.HTTPException' in _inspect.getsource(rlm._download), (
        "_download must catch HTTPException so IncompleteRead → one failure")
    # disk preflight: free < total*1.05 → refuse up front
    with tempfile.TemporaryDirectory() as _target:
        _plan = {'snapshot_ts': 'TS', 'total_size': 10 ** 9,
                 'entries': [{'basename': 'a.deb', 'url': 'x',
                              'sha256': '', 'size': 10 ** 9}],
                 'packages_index': 'Package: a\n'}
        _orig = rlm.shutil.disk_usage
        rlm.shutil.disk_usage = lambda _p: _types.SimpleNamespace(
            total=0, used=0, free=1000)
        try:
            _r = rlm.populate(_plan, _target)
        finally:
            rlm.shutil.disk_usage = _orig
        assert _r['indexed'] is False and _r['failed'] == 1, _r
        assert any(_f[0] == '__disk__' for _f in _r['failures']), _r
        assert not os.path.isfile(os.path.join(_target, '.snapshot'))



def test_stage_remote_localmirror_parses_progress_and_result():
    """remote_orchestrate.stage_remote_localmirror streams the runner's PROGRESS
    markers to on_progress and returns the parsed RESULT dict."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_orchestrate as ro

    class _FakePopen:
        def __init__(self, *a, **k):
            self.stdout = iter([
                ro.LM_PROGRESS_MARKER + ' {"i":1,"n":2,"basename":"a.deb",'
                '"file_done":10,"file_total":10,"cum_done":10,"cum_total":20}\n',
                '[remote-localmirror] working\n',
                ro.LM_RESULT_MARKER + ' {"downloaded":2,"skipped":0,'
                '"failed":0,"indexed":true,"failures":[]}\n',
            ])

        def wait(self):
            return 0

    class _R:
        returncode = 0

    _seen = []
    _orig_run, _orig_popen = ro.subprocess.run, ro.subprocess.Popen
    ro.subprocess.run = lambda *a, **k: _R()
    ro.subprocess.Popen = _FakePopen
    try:
        with tempfile.NamedTemporaryFile(suffix='.py') as _runner:
            _result = ro.stage_remote_localmirror(
                'u@h', {'entries': [], 'total_size': 20}, _runner.name,
                on_progress=_seen.append)
    finally:
        ro.subprocess.run, ro.subprocess.Popen = _orig_run, _orig_popen
    assert _result == {'downloaded': 2, 'skipped': 0, 'failed': 0,
                       'indexed': True, 'failures': []}
    assert len(_seen) == 1 and _seen[0]['basename'] == 'a.deb'



def test_remote_build_run_container_mounts_localmirror():
    """remote_build.run_container bind-mounts an existing localmirror dir at
    /localmirror:ro, and skips the mount (with a warning) when it's absent."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_build as rb

    class _R:
        returncode = 0

    _cap = {}
    _orig = rb.subprocess.run
    rb.subprocess.run = lambda argv, **k: (_cap.__setitem__('argv', argv)
                                           or _R())
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _lm = os.path.join(tmp, 'lm')
            os.makedirs(_lm)
            _bundle = os.path.join(tmp, 'b')
            os.makedirs(_bundle)
            rb.run_container(_bundle, 'img:t', 'true', None, None, _lm)
            assert f'{os.path.abspath(_lm)}:/localmirror:ro' in _cap['argv']
            # absent dir → no mount
            _cap.clear()
            rb.run_container(_bundle, 'img:t', 'true', None, None,
                             os.path.join(tmp, 'nope'))
            assert not any('/localmirror:ro' in str(_a)
                           for _a in _cap['argv'])
    finally:
        rb.subprocess.run = _orig



def test_stage_bundle_writes_localmirror_dir_into_build_json():
    """stage_bundle records localmirror_dir in build.json when given, omits it
    otherwise (so remote_build only mounts when the recipe emits the source)."""
    import json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_orchestrate as ro
    _recipe = {'filename_prefix': 'foo', 'image_tag': 'img:t',
               'build_args': {}, 'cmd_str': 'true'}
    with tempfile.TemporaryDirectory() as tmp:
        _df = os.path.join(tmp, 'Dockerfile')
        open(_df, 'w').write('FROM x')
        _rb = os.path.join(tmp, 'remote_build.py')
        open(_rb, 'w').write('x')
        _b1 = os.path.join(tmp, 'b1')
        ro.stage_bundle(_b1, dockerfile=_df, source_files=[], patch_dir='',
                        recipe=_recipe, build_cpus=None, build_memory=None,
                        remote_build_py=_rb,
                        localmirror_dir='~/athena-localmirror')
        with open(os.path.join(_b1, 'build.json')) as _f:
            assert json.load(_f)['localmirror_dir'] == '~/athena-localmirror'
        _b2 = os.path.join(tmp, 'b2')
        ro.stage_bundle(_b2, dockerfile=_df, source_files=[], patch_dir='',
                        recipe=_recipe, build_cpus=None, build_memory=None,
                        remote_build_py=_rb)
        with open(os.path.join(_b2, 'build.json')) as _f:
            assert 'localmirror_dir' not in json.load(_f)



def test_add_remote_persists_per_remote_local_mirror_flag():
    """RMIRROR-01: add_remote stores a per-remote LocalMirror flag; list_remotes
    returns it (default off)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    with tempfile.TemporaryDirectory() as _tmp:
        _cfgdir = os.path.join(_tmp, 'config')
        os.makedirs(_cfgdir)

        class _Cfg:
            config_path = os.path.join(_cfgdir, 'build.conf')
        _cfg = _Cfg()
        assert utils.add_remote(_cfg, name='r1', host='ssh://u@h1',
                                local_mirror=True)[0]
        assert utils.add_remote(_cfg, name='r2', host='ssh://u@h2')[0]
        _rs = {_r['name']: _r for _r in utils.list_remotes(_cfg)}
        assert _rs['r1']['local_mirror'] is True
        assert _rs['r2']['local_mirror'] is False



def test_remotebuild_composes_recipe_per_slot_localmirror():
    """The fan-out worker composes the recipe with THIS slot's per-remote
    localmirror flag (so file:///localmirror + the /localmirror mount only
    appear for builds dispatched to a remote that has a build mirror)."""
    import re
    _body = _session_source()
    _one = re.search(r'def _remotebuild_one_source\(self.*?(?=\n    def )',
                     _body, re.DOTALL).group(0)
    assert "_slot.get('local_mirror')" in _one
    assert 'compose_recipe(_src, localmirror=_slot_lm)' in _one
    _fan = re.search(r'def _remotebuild_fanout\(self.*?(?=\n    def )',
                     _body, re.DOTALL).group(0)
    assert "'local_mirror': bool(_r.get('local_mirror'))" in _fan



def test_ensure_local_mirror_no_per_run_prompt():
    """The local-container mirror decision is made ONCE (container local init);
    `_ensure_local_mirror` must NOT re-prompt on every cache parse."""
    import re
    with open(os.path.join(_ROOT, 'scripts', 'commands', 'cmd_cache.py')) as _f:
        _src = _f.read()
    _fn = re.search(r'def _ensure_local_mirror\(self.*?(?=\n    def )',
                    _src, re.DOTALL).group(0)
    assert 'Download / refresh the local build mirror now?' not in _fn, (
        "must not re-prompt each cache parse")
    assert 'PROMPT_YESNO' not in _fn, "no y/n in the per-parse mirror refresh"



def test_remote_agent_read_log_offset_survives_concurrent_growth():
    """Audit #162: read_log's next-offset is frm + bytes-actually-read, not the
    stale pre-read getsize — so a file that grows between getsize() and read()
    yields a lossless, overlap-free next offset (no duplicate bytes on the
    following poll)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_agent as _ra
    with tempfile.TemporaryDirectory() as _d:
        _p = os.path.join(_d, 'log')
        with open(_p, 'wb') as _fh:
            _fh.write(b'0123456789')          # 10 bytes on disk
        _orig = _ra.os.path.getsize
        _ra.os.path.getsize = lambda _x: 5    # STALE smaller size (grew after)
        try:
            _data, _next, _size = _ra.read_log(_p, 0)
        finally:
            _ra.os.path.getsize = _orig
        assert _data == b'0123456789'
        assert _next == 10                    # frm + len(data), not stale 5
        _d2, _n2, _ = _ra.read_log(_p, _next)  # next poll → nothing, no dup
        assert _d2 == b'' and _n2 == _next



def test_remote_localmirror_download_resume_206_and_restart_200():
    """Audit #164: _download resumes a partial via HTTP Range (206 → append
    the remaining bytes) and restarts when the server ignores Range (200 →
    truncate + rewrite); both verify the final sha256."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import hashlib as _hl
    import urllib.request as _ur
    import remote_localmirror as _rlm
    _full = b'0123456789'
    _sha = _hl.sha256(_full).hexdigest()

    class _Resp:
        def __init__(self, data, code):
            self._d = data
            self._c = code
            self._i = 0

        def getcode(self):
            return self._c

        def read(self, n=-1):
            _c = self._d[self._i:self._i + n] if n and n > 0 \
                else self._d[self._i:]
            self._i += len(_c)
            return _c

        def close(self):
            pass

    _orig = _ur.urlopen
    with tempfile.TemporaryDirectory() as _d:
        _dest = os.path.join(_d, 'a')
        with open(_dest, 'wb') as _fh:
            _fh.write(_full[:5])                 # partial on disk
        _ur.urlopen = lambda req, timeout=120: _Resp(_full[5:], 206)
        try:
            _ok, _det = _rlm._download('http://x/a', _dest, _sha, 10,
                                       lambda _n: None)
        finally:
            _ur.urlopen = _orig
        assert _ok, _det
        with open(_dest, 'rb') as _fh:
            assert _fh.read() == _full
        _dest2 = os.path.join(_d, 'b')
        with open(_dest2, 'wb') as _fh:
            _fh.write(b'XXXXX')                   # stale partial
        _ur.urlopen = lambda req, timeout=120: _Resp(_full, 200)
        try:
            _ok2, _det2 = _rlm._download('http://x/b', _dest2, _sha, 10,
                                         lambda _n: None)
        finally:
            _ur.urlopen = _orig
        assert _ok2, _det2
        with open(_dest2, 'rb') as _fh:
            assert _fh.read() == _full



def test_run_remote_agent_partial_scp_and_abort_paths():
    """Audit #167: a partial scp-down recovery (only some declared outputs
    materialize) maps to exit 12 (re-queue), and an aborted phase maps to
    exit 130 — neither returns the (misleading) success tuple."""
    from unittest import mock
    import types as _t
    import json as _js
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_orchestrate as _ro

    def _fakes(out_dir, phase_seq, outputs, scp_creates):
        _calls = {'status': 0}

        class _P:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        def _run(argv, **k):
            _j = ' '.join(argv)
            if 'agent.port' in _j:
                return _t.SimpleNamespace(returncode=0, stdout='54321\n',
                                          stderr='')
            if 'out/*.deb' in _j:
                for _f in scp_creates:
                    open(os.path.join(out_dir, _f), 'w').close()
            return _t.SimpleNamespace(returncode=0, stdout='', stderr='')

        def _req(port, path, token, method='GET', timeout=10):
            if path == '/status':
                _i = min(_calls['status'], len(phase_seq) - 1)
                _calls['status'] += 1
                _ph = phase_seq[_i]
                return (200, _js.dumps({
                    'phase': _ph, 'outputs': outputs,
                    'exit_code': 0 if _ph == 'done' else None}).encode(), {})
            if path.startswith('/logs'):
                return (200, b'', {'X-Log-Next': '0'})
            return (200, b'{}', {})

        _sp = _t.SimpleNamespace(run=_run, Popen=lambda a, **k: _P(),
                                 DEVNULL=-3, PIPE=-1, STDOUT=-2,
                                 SubprocessError=Exception)
        return _sp, _req

    def _drive(phase_seq, outputs, scp_creates):
        with tempfile.TemporaryDirectory() as _b, \
                tempfile.TemporaryDirectory() as _o:
            open(os.path.join(_b, 'Dockerfile'), 'w').write('FROM x\n')
            _sp, _req = _fakes(_o, phase_seq, outputs, scp_creates)
            with mock.patch.object(_ro, 'subprocess', _sp), \
                    mock.patch.object(_ro, '_agent_request', _req), \
                    mock.patch.object(_ro, 'AGENT_POLL_INTERVAL', 0):
                return _ro.run_remote_agent('u@h', _b, '~/x', _o, token='t')

    # partial recovery: 2 declared outputs, scp materializes only 1 → exit 12
    assert _drive(['running', 'done'],
                  ['a_1_amd64.deb', 'b_1_amd64.deb'],
                  ['a_1_amd64.deb']) == (12, [])
    # aborted phase → exit 130
    assert _drive(['running', 'aborted'], [], []) == (130, [])

TESTS = [
    test_remote_localmirror_populate_downloads_skips_indexes,
    test_remote_localmirror_hardening_cons13,
    test_stage_remote_localmirror_parses_progress_and_result,
    test_remote_build_run_container_mounts_localmirror,
    test_stage_bundle_writes_localmirror_dir_into_build_json,
    test_add_remote_persists_per_remote_local_mirror_flag,
    test_remotebuild_composes_recipe_per_slot_localmirror,
    test_ensure_local_mirror_no_per_run_prompt,
    test_remote_build_run_container_command_shape,
    test_remote_agent_helpers_name_and_log_offsets,
    test_remote_agent_lifecycle_success_logs_and_auth,
    test_remote_agent_watchdog_and_abort,
    test_run_remote_agent_polls_to_done_ships_token_and_reaps,
    test_run_remote_agent_watchdog_timeout_is_transport,
    test_remote_build_image_uses_args_and_skips_when_present,
    test_remote_build_main_emits_result_marker,
    test_remote_orchestrate_parse_host_stage_and_result,
    test_ensure_remote_image_lan_transfer_paths,
    test_orchestrator_ssh_key_threads_into_argv,
    test_remotebuild_shares_workload_resolver_and_fans_out,
    test_remotebuild_handles_update_mode_cons10,
    test_remote_conf_helpers_round_trip,
    test_remote_token_generate_and_path,
    test_remote_localmirror_cum_total_guards_null_total_size,
    test_remote_orchestrate_open_tunnel_sets_strict_host_key_checking,
    test_remote_agent_read_log_offset_survives_concurrent_growth,
    test_remote_localmirror_download_resume_206_and_restart_200,
    test_run_remote_agent_partial_scp_and_abort_paths,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
