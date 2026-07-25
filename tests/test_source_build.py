"""Athena tests — source builds + build records (buildcontainer.py, virtual_build.py, cmd_source.py, cmd_tunnel.py, build.json lifecycle).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile
import threading

from _test_helpers import (  # noqa: F401
    _BASE_CONF_BODY,
    _MINIMAL_MIRROR_BLOCK,
    _ROOT,
    _build_config_from,
    _build_minimal_deb,
    _make_buildcontainer_stub,
    _session_source,
    _utils_module,
    _write_test_config,
)




def test_container_release_defaults_to_release_unless_overridden():
    """CONTAINER_RELEASE auto-tracks RELEASE when absent (container + target
    stay in lockstep), and an explicit value pins it independently."""
    with tempfile.TemporaryDirectory() as tmp:
        # absent → defaults to RELEASE (bookworm in the base fixture)
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, f"BuildConfig invalid: {cfg.error_str}"
        assert cfg.container_release == cfg.release == 'bookworm'
    # explicit override holds the container behind/ahead of the target
    _body = _BASE_CONF_BODY.replace(
        'BASEVERSION = 12.0',
        'BASEVERSION = 12.0\n    CONTAINER_RELEASE = trixie')
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _body.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, f"BuildConfig invalid: {cfg.error_str}"
        assert cfg.release == 'bookworm' and cfg.container_release == 'trixie'



def test_tunnel_filenames_prefers_resolved_source_not_parse_order():
    """Regression (audit #14): _tunnel_filenames_for_source must enumerate the
    RESOLVED source's binary set — dep_tree.selected_srcs first, else the
    HIGHEST-version cache candidate — not source_hashtable[0] (arbitrary
    cache-parse order).  When a source coexists across snapshots with a
    differing binary set, _cands[0] could enumerate the wrong (non-selected)
    set, drifting the tunnel set away from virtual_build's prediction."""
    import sys
    import types
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    sys.path.insert(0, os.path.join(_ROOT, 'scripts', 'commands'))
    import cohorts
    import virtual_build
    from debian.debian_support import Version

    class _Src:
        def __init__(self, ver, binaries):
            self.version = Version(ver)
            self.binary = binaries

    _low = _Src('1.0', ['oldbin'])
    _high = _Src('2.0', ['newbin'])

    obj = cohorts.CohortResolverMixin.__new__(cohorts.CohortResolverMixin)
    obj.dep_tree = types.SimpleNamespace(selected_srcs={}, selected_pkgs={})
    obj.udeb_dep_tree = None
    # source_hashtable in cache-PARSE order: low version first (the trap).
    obj.cache = types.SimpleNamespace(source_hashtable={'foo': [_low, _high]})
    obj.config = types.SimpleNamespace(
        arch='amd64', build_profiles=frozenset(), dir_fork_source_repo=None)
    obj._resolve_tunnel_filename = lambda b, e: f"{b}.deb"

    with mock.patch.object(virtual_build, '_package_list_index',
                           return_value={}), \
         mock.patch.object(virtual_build, '_binary_active_for_arch',
                           return_value=True), \
         mock.patch.object(virtual_build, '_binary_active_under_profiles',
                           return_value=True):
        # (a) not in selected_srcs -> highest-version cache candidate wins,
        #     NOT _cands[0] (the low version).
        assert obj._tunnel_filenames_for_source('foo') == ['newbin.deb']
        # (b) selected_srcs wins outright.
        obj.dep_tree.selected_srcs = {'foo': _high}
        assert obj._tunnel_filenames_for_source('foo') == ['newbin.deb']



def test_render_install_cmd_uses_no_install_recommends():
    """Build-dep install must pass --no-install-recommends (sbuild/pbuilder
    behaviour) so the env installs only Build-Depends + hard Depends — matching
    compute_build_closure (Depends+Pre-Depends) so the localmirror covers it
    completely.  Applies to both the plain-deps install and the OR-group chains,
    and to the --simulate preview."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from buildcontainer import BuildContainer
    _cmd = BuildContainer._render_install_cmd(
        ['libfoo-dev', 'libbar-dev'], [['libqt5-dev', 'libqt6-dev']],
        simulate=False)
    # both the plain-deps install and the OR-group chains carry the flag; no
    # bare `install -y ` / `install <dep>` without it.
    assert 'apt -y --no-install-recommends' in _cmd
    assert 'apt-get install -y --no-install-recommends' in _cmd
    assert ' install -y -o' not in _cmd and 'install -y libfoo' not in _cmd
    # preserved under simulate
    _sim = BuildContainer._render_install_cmd(['libfoo-dev'], [], simulate=True)
    assert '--no-install-recommends' in _sim and '--simulate' in _sim



# ─────────────────────────────────────────────────────────────────────────────
# DOCKER_SERVER guard refuses unsafe network-reachable daemons
# ─────────────────────────────────────────────────────────────────────────────

def test_docker_server_guard_accepts_safe_targets():
    """Loopback tcp + unix sockets + TLS-marked URLs all pass."""
    from buildcontainer import BuildContainer
    safe = [
        'unix:///var/run/docker.sock',
        'tcp://127.0.0.1:2375',
        'tcp://[::1]:2375',
        'tcp://localhost:2375',
        'https://docker.example.com:2376',
        'tcp://docker.example.com:2376?tls=true',
        'tcp://docker.example.com:2376?tls=1',
    ]
    for url in safe:
        BuildContainer._guard_docker_server(url)   # raises on reject



def test_docker_server_guard_refuses_unsafe_targets():
    """Bare tcp:// to a non-loopback host without a TLS marker raises."""
    from buildcontainer import BuildContainer
    unsafe = [
        'tcp://192.168.1.100:2375',
        'tcp://10.0.0.5:2375',
        'tcp://docker.example.com:2375',
        'http://192.168.1.100:2375',
    ]
    for url in unsafe:
        raised = False
        try:
            BuildContainer._guard_docker_server(url)
        except RuntimeError as e:
            raised = True
            assert 'TLS' in str(e), str(e)
            assert 'docs/security.md' in str(e), str(e)
        assert raised, f"expected RuntimeError for {url!r}"



def test_arch16_buildcontainer_consults_per_pkg_overrides():
    """ARCH-16: BuildContainer.build's profile/option resolution honours
    `config.build_{options,profiles}_for(src_pkg.package)` between the
    explicit override kwarg and the global default.  Source-grep pin so a
    future refactor can't silently regress to `self.build_options` etc."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    # The build() method must call the accessors with src_pkg.package.
    assert 'config.build_profiles_for(src_pkg.package)' in _src, (
        "ARCH-16: BuildContainer.build must use config.build_profiles_for "
        "(per-pkg override + global fallback) when profiles_override is None")
    assert 'config.build_options_for(src_pkg.package)' in _src, (
        "ARCH-16: BuildContainer.build must use config.build_options_for "
        "(per-pkg override + global fallback) when options_override is None")



def test_sec05_render_install_cmd_injects_simulate_flag():
    """SEC-05: the shared install-cmd renderer (single source of truth
    for both the real install in build() and the SEC-05 preview)
    correctly threads `--simulate` into every apt-get install invocation
    when simulate=True, and omits it when simulate=False."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from buildcontainer import BuildContainer
    _real = BuildContainer._render_install_cmd(
        ['libfoo-dev', 'libbar-dev'], [['gawk', 'mawk']], simulate=False)
    _sim = BuildContainer._render_install_cmd(
        ['libfoo-dev', 'libbar-dev'], [['gawk', 'mawk']], simulate=True)
    assert '--simulate' not in _real
    assert _real.count('apt -y --no-install-recommends -o') >= 1, (
        "plain-dep install present")
    assert _real.count('apt-get install -y --no-install-recommends -o') >= 2, (
        "OR-group fallback chain")
    # --simulate is injected per apt-get install invocation:
    # 1 plain install + 2 OR-alt invocations = 3 occurrences
    assert _sim.count('--simulate') == 3, _sim
    # Empty deps = empty string
    assert BuildContainer._render_install_cmd([], [], simulate=False) == ''
    assert BuildContainer._render_install_cmd([], [], simulate=True) == ''



def test_sec05_build_gates_on_audit_when_enabled_in_source():
    """SEC-05: build() consults self.audit_build_deps BEFORE rendering
    the install cmd, calls _audit_build_deps_gate(src_pkg, plain, ors),
    and returns False without proceeding when the gate returns False.
    Source-grep so a future refactor can't silently regress."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    # The gate must be invoked from build() BEFORE the real install
    # command is rendered for the container shell.
    assert 'self._audit_build_deps_gate(' in _src, (
        "SEC-05: BuildContainer.build() must invoke _audit_build_deps_gate")
    # On gate decline, build() must return False (no container.run).
    _build_start = _src.index('def build(')
    _build_end = _src.index('def ', _build_start + 1)
    _build_body = _src[_build_start:_build_end]
    assert 'if not self._audit_build_deps_gate(' in _build_body, (
        "SEC-05: gate must be guarded by an `if not ...` so a False return "
        "from the gate skips the build")
    assert 'return False' in _build_body, (
        "SEC-05: build() must `return False` on operator decline (no "
        "container.run, no .result side-effect)")
    # Deps are now resolved in compose_recipe(); the gate must run AFTER that
    # call so plain_deps + or_groups are available to preview.
    _gate_call_idx = _build_body.index('self._audit_build_deps_gate(')
    _recipe_idx = _build_body.index('self.compose_recipe(')
    assert _recipe_idx < _gate_call_idx, (
        "SEC-05: gate must run AFTER compose_recipe() so deps are available "
        "to preview")
    # The build-dep enumeration itself lives in compose_recipe().
    _cr_start = _src.index('def compose_recipe(')
    _cr_end = _src.index('\n    def ', _cr_start + 1)
    assert 'for _grp in src_pkg.build_depends(' in _src[_cr_start:_cr_end], (
        "SEC-05: build-dep enumeration must live in compose_recipe()")



def test_sec05_gate_short_circuits_when_no_deps():
    """SEC-05: a source with zero build-deps must not prompt the operator
    (nothing to audit).  Returns True without invoking the preview."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import MagicMock
    import buildcontainer
    _bc = MagicMock()
    _bc.audit_build_deps = True
    _result = buildcontainer.BuildContainer._audit_build_deps_gate(
        _bc, MagicMock(package='nothing'), [], [])
    assert _result is True
    # No preview should have been generated — gate short-circuits.
    _bc._capture_apt_simulate.assert_not_called()



def test_compose_recipe_assembles_image_args_and_cmd_str():
    """compose_recipe() assembles the full build recipe (image tag/args,
    cmd_str, per-pkg descriptors) WITHOUT running anything — the single source
    both build() (local) and `source remotebuild` (remote) consume, so a remote
    build is byte-identical to a local one."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc.arch = 'amd64'
    _bc.cache = None
    _bc.codename = 'thor'
    _bc.build_distribution = 'Asgard'
    _bc.build_base_id = 'asgard'
    _bc.patch_path = '/nonexistent/patch'
    _bc.patch_empty = '/nonexistent/patch-empty'
    _bc.snapshot_ts = '20260602T173733Z'
    _bc._image_tag = 'athenalinux:build-bookworm-20260602T173733Z'
    _bc.config = mock.Mock(container_release='bookworm',
                           snapshot_baseurl='https://snapshot.debian.org/archive')
    _bc.config.build_profiles_for.return_value = frozenset({'nodoc', 'nocheck'})
    _bc.config.build_options_for.return_value = frozenset({'nodoc', 'nocheck'})
    _bc._render_install_cmd = mock.Mock(return_value='APT_INSTALL; ')
    _bc._write_snapshot_sources_cmd = mock.Mock(return_value='WRITE_SOURCES; ')
    _src = mock.Mock(package='adduser', version='3.134',
                     files=['adduser_3.134.dsc', 'adduser_3.134.tar.xz'])
    _src.build_depends.return_value = [[('debhelper', '', '')]]
    _src._mirror = mock.Mock(component='main')

    _recipe = _bc.compose_recipe(_src)
    assert _recipe is not None
    assert _recipe['image_tag'] == 'athenalinux:build-bookworm-20260602T173733Z'
    assert _recipe['build_args']['RELEASE'] == 'bookworm'
    assert _recipe['build_args']['SNAPSHOT_TS'] == '20260602T173733Z'
    assert _recipe['build_args']['ARCHIVE_NAME'] == 'debian'
    assert _recipe['dsc_file'] == 'adduser_3.134.dsc'
    assert _recipe['filename_prefix'] == 'adduser'
    assert _recipe['component'] == 'main'
    assert _recipe['plain_deps'] == ['debhelper']
    _cmd = _recipe['cmd_str']
    assert 'WRITE_SOURCES;' in _cmd and 'APT_INSTALL;' in _cmd
    assert 'dpkg-source -x adduser_3.134.dsc adduser' in _cmd
    assert 'dpkg-buildpackage -a amd64 -b -us -uc -nc' in _cmd
    assert 'cp *.deb /repo/' in _cmd
    # a source with no .dsc → None (hard skip)
    _nodsc = mock.Mock(package='x', version='1', files=['x_1.tar.xz'])
    _nodsc.build_depends.return_value = []
    assert _bc.compose_recipe(_nodsc) is None



def test_compose_recipe_prebuild_sourced_hashed_and_mounted():
    """Version-independent `patch/source/<pkg>/prebuild.sh` — package-
    specific build environment that must NOT land in the image (where it
    would affect every build): (a) sourced into the build shell AFTER
    unpack+patches and BEFORE dpkg-checkbuilddeps/dpkg-buildpackage, so
    its exports reach the build; (b) folded into patch_set_hash so an
    edit invalidates the build record; (c) carried in the recipe and
    bind-mounted READ-ONLY at /prebuild.sh.  Absent script → zero effect."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer

    def _fixture(patch_path):
        _bc = buildcontainer.BuildContainer.__new__(
            buildcontainer.BuildContainer)
        _bc.arch = 'amd64'
        _bc.cache = None
        _bc.codename = 'thor'
        _bc.build_distribution = 'Asgard'
        _bc.build_base_id = 'asgard'
        _bc.patch_path = patch_path
        _bc.patch_empty = '/nonexistent/patch-empty'
        _bc.snapshot_ts = '20260602T173733Z'
        _bc._image_tag = 'athenalinux:build-bookworm-20260602T173733Z'
        _bc.config = mock.Mock(
            container_release='bookworm',
            snapshot_baseurl='https://snapshot.debian.org/archive')
        _bc.config.build_profiles_for.return_value = frozenset()
        _bc.config.build_options_for.return_value = frozenset()
        _bc._render_install_cmd = mock.Mock(return_value='APT_INSTALL; ')
        _bc._write_snapshot_sources_cmd = mock.Mock(
            return_value='WRITE_SOURCES; ')
        _src = mock.Mock(package='adduser', version='3.134',
                         files=['adduser_3.134.dsc', 'adduser_3.134.tar.xz'])
        _src.build_depends.return_value = [[('debhelper', '', '')]]
        _src._mirror = mock.Mock(component='main')
        return _bc, _src

    with tempfile.TemporaryDirectory() as _pp:
        _bc, _src = _fixture(_pp)
        _r0 = _bc.compose_recipe(_src)
        assert _r0['prebuild'] == ''
        assert '. /prebuild.sh' not in _r0['cmd_str']

        os.makedirs(os.path.join(_pp, 'adduser'), exist_ok=True)
        _pb = os.path.join(_pp, 'adduser', 'prebuild.sh')
        with open(_pb, 'w') as _fh:
            _fh.write('export FOO=1\n')
        _r1 = _bc.compose_recipe(_src)
        assert _r1['prebuild'] == _pb
        _cmd = _r1['cmd_str']
        assert '. /prebuild.sh; ' in _cmd
        # sourced inside the unpacked tree, after patches, before the build
        assert _cmd.index('dpkg-source -x') < _cmd.index('. /prebuild.sh')
        assert _cmd.index('. /prebuild.sh') < _cmd.index('dpkg-checkbuilddeps')
        # hash folds the script: presence and content both matter
        assert _r1['patch_set_hash'] != _r0['patch_set_hash']
        with open(_pb, 'w') as _fh:
            _fh.write('export FOO=2\n')
        assert _bc.compose_recipe(_src)['patch_set_hash'] \
            != _r1['patch_set_hash']

    # the build() bind mount is read-only (MAT-04 discipline)
    import re
    with open(os.path.join(_ROOT, 'scripts', 'buildcontainer.py')) as _fh:
        _body = _fh.read()
    assert re.search(r"'/prebuild\.sh',\s*'mode':\s*'ro'", _body), (
        "/prebuild.sh must be bind-mounted read-only")


def test_sec05_gate_returns_false_when_operator_declines():
    """SEC-05: the gate must return False when the operator answers 'n'
    to the YESNO prompt — caller (build()) returns False and skips."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import MagicMock, patch
    import buildcontainer
    _bc = MagicMock(spec=buildcontainer.BuildContainer)
    _bc.audit_build_deps = True
    _bc._capture_apt_simulate.return_value = (
        'NOTE: This is only a simulation!\n'
        'Inst libfoo (1.0-1 Debian:bookworm [amd64])\n')

    # Stub Prompt -> 'n' via the import-name `tui.Prompt` (the gate uses
    # `from tui import Prompt`).
    class _StubPrompt:
        def __init__(self, *_a, **_kw): pass
        def get_response(self): return 'n'

    with patch.object(buildcontainer.tui, 'Prompt', _StubPrompt):
        _result = buildcontainer.BuildContainer._audit_build_deps_gate(
            _bc, MagicMock(package='libfoo'), ['libfoo-dev'], [])
    assert _result is False



def test_sec05_gate_returns_false_when_preview_infrastructure_fails():
    """SEC-05: if the simulate preview can't even start (Docker error,
    None return), the gate must fail closed — return False so the build
    is skipped rather than silently proceeding with un-audited deps."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import MagicMock
    import buildcontainer
    _bc = MagicMock(spec=buildcontainer.BuildContainer)
    _bc.audit_build_deps = True
    _bc._capture_apt_simulate.return_value = None
    _result = buildcontainer.BuildContainer._audit_build_deps_gate(
        _bc, MagicMock(package='libfoo'), ['libfoo-dev'], [])
    assert _result is False



def test_comp03_buildconfig_defaults_for_new_keys():
    """COMP-03 Phase 0: BuildCpus / BuildMemory / HeavyPackages are
    optional; absent keys land at 0.0 / '' / frozenset() so a
    build.conf without them keeps current (uncapped) serial behaviour."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, cfg.error_str
        assert cfg.build_cpus == 0.0
        assert cfg.build_memory == ''
        assert cfg.heavy_packages == frozenset()



def test_comp03_buildconfig_parses_explicit_values():
    """COMP-03 Phase 0: explicit BuildCpus / BuildMemory / HeavyPackages
    values surface on the BuildConfig."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    body = _BASE_CONF_BODY.replace(
        'MaxParallelBuilds = 1',
        ('MaxParallelBuilds = 4\n'
         '    BuildCpus = 3.5\n'
         '    BuildMemory = 8g\n'
         '    HeavyPackages = firefox-esr, gcc-12, llvm-toolchain-15'),
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, cfg.error_str
        assert cfg.max_parallel_builds == 4
        assert cfg.build_cpus == 3.5
        assert cfg.build_memory == '8g'
        assert cfg.heavy_packages == frozenset(
            {'firefox-esr', 'gcc-12', 'llvm-toolchain-15'})



def test_comp03_max_parallel_builds_capped_at_8():
    """COMP-03 Phase 0: MaxParallelBuilds > 8 fails closed at config
    load because docker-py's default urllib3 pool size is 10 — higher
    worker counts exhaust the pool under streaming logs."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    body = _BASE_CONF_BODY.replace(
        'MaxParallelBuilds = 1', 'MaxParallelBuilds = 9')
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert not cfg.is_valid
        assert 'MaxParallelBuilds must be <= 8' in cfg.error_str



def test_comp03_max_parallel_builds_rejects_zero():
    """COMP-03 Phase 0: MaxParallelBuilds < 1 fails closed (the serial
    path is `== 1`, parallel is `> 1` — `== 0` is an operator typo)."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    body = _BASE_CONF_BODY.replace(
        'MaxParallelBuilds = 1', 'MaxParallelBuilds = 0')
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert not cfg.is_valid
        assert 'MaxParallelBuilds must be >= 1' in cfg.error_str



def test_comp03_buildcpus_rejects_negative():
    """COMP-03 Phase 0: BuildCpus < 0 is meaningless (docker --cpus
    rejects it); fail closed at config load."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    body = _BASE_CONF_BODY.replace(
        'MaxParallelBuilds = 1',
        'MaxParallelBuilds = 1\n    BuildCpus = -1.0',
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert not cfg.is_valid
        assert 'BuildCpus must be >= 0.0' in cfg.error_str



def test_comp03_audit_build_deps_and_parallel_mutex_fails_closed():
    """COMP-03 Phase 0: AuditBuildDeps + MaxParallelBuilds > 1 fails
    closed.  Reason: the audit gate is an interactive PROMPT_YESNO
    inside BuildContainer.build(); under parallel workers, multiple
    prompts would collide on stdin."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    body = (_BASE_CONF_BODY.replace(
        'MaxParallelBuilds = 1', 'MaxParallelBuilds = 4')
        + '\n    [Security]\n'
          '    Disabled = true\n'
          '    AuditBuildDeps = true\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert not cfg.is_valid
        assert 'mutually exclusive' in cfg.error_str
        assert 'AuditBuildDeps' in cfg.error_str
        assert 'MaxParallelBuilds' in cfg.error_str



def test_comp03_audit_build_deps_with_serial_is_allowed():
    """COMP-03 Phase 0: AuditBuildDeps + MaxParallelBuilds == 1 is the
    intended pairing — serial path, one prompt at a time.  Must NOT
    trip the mutex."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    body = _BASE_CONF_BODY + (
        '\n    [Security]\n'
        '    Disabled = true\n'
        '    AuditBuildDeps = true\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, cfg.error_str
        assert cfg.audit_build_deps is True
        assert cfg.max_parallel_builds == 1



# ─────────────────────────────────────────────────────────────────────────────
# DEP-3 header check
# ─────────────────────────────────────────────────────────────────────────────

def test_check_dep3_header_clean_patch_returns_empty():
    """Patch with Description + Origin/Author headers passes."""
    from utils import check_dep3_header
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'good.patch')
        with open(path, 'w') as fh:
            fh.write(
                'Description: tighten foo\n'
                ' Long-form prose lines here.\n'
                'Author: Test <test@example.org>\n'
                'Forwarded: no\n'
                '---\n'
                '--- a/foo\n'
                '+++ b/foo\n'
            )
        assert check_dep3_header(path) == []



def test_check_dep3_header_missing_origin_returns_field():
    """A patch without Origin or Author flags Origin as missing."""
    from utils import check_dep3_header
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'no-origin.patch')
        with open(path, 'w') as fh:
            fh.write(
                'Description: only one header\n'
                '--- a/foo\n'
                '+++ b/foo\n'
            )
        assert check_dep3_header(path) == ['Origin']



def test_check_dep3_header_subject_satisfies_description():
    """Subject: alias is accepted in place of Description:."""
    from utils import check_dep3_header
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'subject.patch')
        with open(path, 'w') as fh:
            fh.write(
                'Subject: alias header\n'
                'Author: t\n'
                '---\n'
            )
        assert check_dep3_header(path) == []



def test_conf15_dockerfile_pins_toolchain_to_snapshot():
    """CONF-15 (+ 2026-07-07 own-base revision): every layer of the image
    is pinned to OUR snapshot.  The base is a `FROM scratch` +
    `ADD base-rootfs.tar` of the mmdebstrap buildd bootstrap
    (base_rootfs.py) — no Docker Hub pull, no doc-stripped slim base —
    and the toolchain extras install against explicitly-written snapshot
    sources.  Pre-CONF-15 the toolchain layer pulled from live
    deb.debian.org which contaminated dpkg-shlibdeps.

    Required shape:
      - FROM scratch + ADD base-rootfs.tar (no external base image)
      - ARG SNAPSHOT_BASEURL / ARCHIVE_NAME / SNAPSHOT_TS declared
      - sources.list wiped THEN written with the three snapshot pockets,
        BEFORE any apt-get install
      - no live-mirror reference anywhere
      - SNAPSHOT_TS validated non-empty (fails the build, not silent)"""
    _df = os.path.join(_ROOT, 'config', 'Dockerfile')
    with open(_df) as fh:
        _body = fh.read()
    # 1. The four new build-args declared (both pre-FROM and re-declared
    # post-FROM, since Docker scopes them that way).  SECURITY_ARCHIVE_NAME
    # joins ARCHIVE_NAME because the bookworm-security suite uses a
    # different archive prefix on snapshot.debian.org.
    for _arg in ('SNAPSHOT_BASEURL', 'ARCHIVE_NAME',
                 'SECURITY_ARCHIVE_NAME', 'SNAPSHOT_TS'):
        assert f'ARG {_arg}' in _body, f"Dockerfile missing ARG {_arg}"
    # 2. The legacy sources.list AND deb822 sources.list.d/*.{sources,list}
    # are both wiped (bookworm-slim ships deb822 by default).
    assert 'rm -f /etc/apt/sources.list' in _body
    assert '/etc/apt/sources.list.d/*.sources' in _body
    assert '/etc/apt/sources.list.d/*.list' in _body
    # 3. THREE snapshot URLs are written into sources.list: main, -updates,
    # and -security.  Mirrors the per-build _write_snapshot_sources_cmd
    # shape — necessary because packages can be present in any of the
    # three pockets at a given snapshot TS, and the toolchain layer must
    # cover all of them (e.g. Architecture: all packages drift across
    # main vs -updates around point releases).  No trailing slash after
    # SNAPSHOT_TS — apt resolves `<URI>/dists/<suite>/InRelease` and a
    # trailing slash here produces `<TS>//dists/...` that snapshot.d.o
    # 404s on.
    assert '${SNAPSHOT_BASEURL}/${ARCHIVE_NAME}/${SNAPSHOT_TS} ${RELEASE} main' in _body, (
        "Dockerfile must write the main snapshot URL")
    assert '${SNAPSHOT_BASEURL}/${ARCHIVE_NAME}/${SNAPSHOT_TS} ${RELEASE}-updates main' in _body, (
        "Dockerfile must write the -updates snapshot URL — Architecture: all "
        "packages (debhelper, autoconf, python3, …) can be in -updates rather "
        "than main at a given TS")
    assert '${SNAPSHOT_BASEURL}/${SECURITY_ARCHIVE_NAME}/${SNAPSHOT_TS} ${RELEASE}-security main' in _body, (
        "Dockerfile must write the -security snapshot URL (different archive "
        "prefix on snapshot.debian.org: debian-security, not debian)")
    assert '${SNAPSHOT_BASEURL}/${ARCHIVE_NAME}/${SNAPSHOT_TS}/ ' not in _body, (
        "Dockerfile must NOT have a trailing slash after SNAPSHOT_TS — "
        "snapshot.debian.org 404s on the resulting //dists/ double slash")
    # 4. The Pin-Priority 1001 preferences file is written (forces
    # downgrade when our snapshot is older than the base-image pkg set).
    assert 'Pin-Priority: 1001' in _body
    # 5. Own-base shape: FROM scratch + ADD of the mmdebstrap bootstrap —
    # never an external registry base.  The old slim base was the one
    # unpinned third-party layer (and its doc-stripping broke libpsl /
    # init-system-helpers testsuites; its docker dpkg config broke dpkg's).
    # Strip comment lines so historical references in the explanatory
    # block don't false-positive on the keywords.
    _code_only = '\n'.join(
        _line for _line in _body.splitlines()
        if not _line.lstrip().startswith('#'))
    assert 'FROM scratch' in _code_only, "base must be FROM scratch"
    assert 'ADD base-rootfs.tar /' in _code_only, (
        "base must be the bootstrapped rootfs tar (base_rootfs.py)")
    assert 'debian:${RELEASE}-slim' not in _code_only, (
        "no Docker Hub base image — the base is our snapshot bootstrap")
    assert 'deb.debian.org' not in _code_only, (
        "no live-mirror reference may survive in the Dockerfile")
    # The snapshot-bootstrapped base is ALREADY at snapshot, so sources are
    # written BEFORE the toolchain install (the old install-then-realign
    # dance existed only because slim's base was at the live mirror) and no
    # dist-upgrade realign remains.
    _idx_rewrite = _code_only.find(
        'echo "deb [check-valid-until=no] ${SNAPSHOT_BASEURL}')
    _idx_install = _code_only.find('apt-get install -y')
    assert 0 < _idx_rewrite < _idx_install, (
        "Dockerfile ordering must be: write snapshot sources → "
        "apt-get install (the base is born at snapshot; no live step)")
    assert 'dist-upgrade' not in _code_only, (
        "no realign dist-upgrade — the bootstrapped base starts at snapshot")
    assert 'apt-get update -o APT::Update::Error-Mode=any' in _code_only, (
        "index-fetch failures must FAIL the update loudly — apt's default "
        "warns and leaves empty lists (the 'Unable to locate' class)")
    # 6. SNAPSHOT_TS guard — empty TS must fail the build loudly, not
    # silently default to live URLs.
    assert '-z "${SNAPSHOT_TS}"' in _body, (
        "Dockerfile must reject an empty SNAPSHOT_TS instead of silently "
        "falling back to live mirrors")



def test_base_rootfs_bootstrap_command_and_cache():
    """base_rootfs.ensure_base_rootfs: (a) cache hit returns without
    running mmdebstrap; (b) a bootstrap runs `mmdebstrap --variant=buildd`
    for the config arch against the THREE snapshot pockets (same shape as
    the Dockerfile sources), writing to a .partial then renaming — a
    killed bootstrap never leaves a truncated tar for ADD to consume;
    (c) a failing mmdebstrap raises RuntimeError and removes the partial."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import base_rootfs
    _ts = '20260705T190150Z'
    with tempfile.TemporaryDirectory() as _d:
        _cfg = mock.Mock(dir_cache=_d, container_release='bookworm',
                         arch='amd64',
                         snapshot_baseurl='https://snapshot.debian.org/archive')
        _out = base_rootfs.rootfs_path(_cfg, _ts)
        assert _ts in _out and 'bookworm' in _out and 'amd64' in _out

        # (a) cache hit — no subprocess
        with open(_out, 'wb') as _fh:
            _fh.write(b'cached')
        with mock.patch.object(base_rootfs, 'subprocess') as _sp:
            assert base_rootfs.ensure_base_rootfs(_cfg, _ts) == _out
            _sp.run.assert_not_called()
        os.remove(_out)

        # (b) bootstrap — command shape + atomic rename
        def _fake_run(cmd, **_kw):
            with open(cmd[cmd.index('bookworm') + 1], 'wb') as _fh:
                _fh.write(b'rootfs')
            return mock.Mock(returncode=0, stderr=b'')
        with mock.patch.object(base_rootfs.subprocess, 'run',
                               side_effect=_fake_run) as _run:
            assert base_rootfs.ensure_base_rootfs(_cfg, _ts) == _out
        _cmd = _run.call_args[0][0]
        assert _cmd[0] == 'mmdebstrap'
        assert '--variant=buildd' in _cmd
        assert '--architectures=amd64' in _cmd
        assert '--format=tar' in _cmd, (
            "format must be EXPLICIT — mmdebstrap infers it from the "
            "target extension and .partial hides the .tar (rc=25)")
        assert '--include=ca-certificates' in _cmd, (
            "buildd variant has no trust store — https snapshot fetches "
            "fail silently without ca-certificates (the CONF-15 anomaly)")
        assert _cmd[_cmd.index('bookworm') + 1].endswith('.partial'), (
            "mmdebstrap must write to .partial; rename happens on success")
        _srcs = [_a for _a in _cmd if _a.startswith('deb ')]
        assert len(_srcs) == 3, _srcs
        assert any(f'/debian/{_ts} bookworm main' in _s for _s in _srcs)
        assert any(f'{_ts} bookworm-updates main' in _s for _s in _srcs)
        assert any(f'/debian-security/{_ts} bookworm-security main' in _s
                   for _s in _srcs)
        assert os.path.isfile(_out) and not os.path.exists(_out + '.partial')
        os.remove(_out)

        # (c) failure — RuntimeError, no partial left behind
        def _fail_run(cmd, **_kw):
            with open(cmd[cmd.index('bookworm') + 1], 'wb') as _fh:
                _fh.write(b'trunc')
            return mock.Mock(returncode=1, stderr=b'boom')
        with mock.patch.object(base_rootfs.subprocess, 'run',
                               side_effect=_fail_run):
            try:
                base_rootfs.ensure_base_rootfs(_cfg, _ts)
                raise AssertionError('expected RuntimeError')
            except RuntimeError as _e:
                assert 'boom' in str(_e)
        assert not os.path.exists(_out + '.partial')
        assert not os.path.exists(_out)


def test_image_build_stages_rootfs_context_not_config_dir():
    """The image build assembles a minimal staging context (Dockerfile +
    hard-linked base-rootfs.tar) and passes THAT to images.build — never
    config/ (which must not carry the ~250MB tar, and whose whole content
    would otherwise ship as docker context).  The staging dir is removed
    on every exit path.  Source-grep pin."""
    import re
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    _m = re.search(r'if _needs_build:(.*?)\n        self\.image = image',
                   _src, re.DOTALL)
    assert _m, 'image build block not found'
    _blk = _m.group(1)
    assert 'base_rootfs.ensure_base_rootfs(' in _blk
    assert re.search(r'images\.build\(\s*\n?\s*path=_ctx', _blk), (
        'images.build must consume the staging context, not dir_config')
    assert 'path=config.dir_config' not in _blk
    assert 'shutil.rmtree(_ctx, ignore_errors=True)' in _blk, (
        'staging context must be cleaned up in the finally')


def test_conf15_buildcontainer_image_tag_carries_snapshot_ts():
    """CONF-15: image tag MUST include the snapshot TS so a `[Snapshot]
    Timestamp` change invalidates the image cache automatically.
    Pre-CONF-15 the tag was `athenalinux:build-<release>`; an operator's
    snapshot advance left the old image in place against an older snapshot."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    # The assignment spans a few lines (f-string with two interpolations).
    # Grab from `self._image_tag =` through the closing `)` / `"`.
    _m = re.search(
        r'self\._image_tag\s*=[^\n]*(?:\n[ ]+[^\n]+){0,4}',
        _body)
    assert _m, "self._image_tag assignment not found"
    _snippet = _m.group(0)
    assert 'config.container_release' in _snippet, (
        f"image tag should still include container_release; got:\n{_snippet}")
    assert 'self.snapshot_ts' in _snippet, (
        f"image tag must include self.snapshot_ts to invalidate on "
        f"snapshot advance; got:\n{_snippet}")



def test_mat04_build_container_mounts_source_and_patch_readonly():
    """MAT-04: /source and /patch are bind-mounted READ-ONLY into the build
    container.  The build copies the source out (`cp /source/<prefix>* .`) and
    reads patches, never writing back — so a passwordless-root `debian/rules`
    must not be able to corrupt the host's source/patch trees through the
    mount.  /repo (the build OUTPUT) stays rw."""
    import re
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    assert re.search(r"'/source',\s*'mode':\s*'ro'", _body), "/source must be ro"
    assert re.search(r"'/patch',\s*'mode':\s*'ro'", _body), "/patch must be ro"
    assert re.search(r"'/repo',\s*'mode':\s*'rw'", _body), "/repo (output) stays rw"
    # guard against a regression to rw on either input mount
    assert not re.search(r"'/source',\s*'mode':\s*'rw'", _body)
    assert not re.search(r"'/patch',\s*'mode':\s*'rw'", _body)



def test_conf15_buildcontainer_buildargs_pass_snapshot_triplet():
    """CONF-15: client.images.build(buildargs=…) MUST pass
    SNAPSHOT_BASEURL / ARCHIVE_NAME / SNAPSHOT_TS so the Dockerfile's
    sources.list rewrite has the values it needs to compose the snapshot
    URL.  Without these, the Dockerfile's guard fails the build (`-z
    SNAPSHOT_TS`) — that's the intended loud failure mode."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r"self\.client\.images\.build\([\s\S]+?buildargs\s*=\s*\{[\s\S]+?\}",
        _body)
    assert _m, "client.images.build(... buildargs={...}) not found"
    _kwargs = _m.group(0)
    for _arg in ("'SNAPSHOT_BASEURL'", "'ARCHIVE_NAME'",
                 "'SECURITY_ARCHIVE_NAME'", "'SNAPSHOT_TS'"):
        assert _arg in _kwargs, (
            f"buildargs missing {_arg} — Dockerfile guard will fail "
            f"the image build")
    # ARCHIVE_NAME is the primary archive (debian), not security.  The
    # per-build sources.list still spans the full mirror set.
    assert "'ARCHIVE_NAME':" in _kwargs and "'debian'" in _kwargs, (
        "ARCHIVE_NAME should be the primary 'debian' archive")
    assert 'self.snapshot_ts' in _kwargs, (
        "SNAPSHOT_TS must be self.snapshot_ts (the resolved pin), not a literal")



def test_sta46_was_patched_keys_on_patch_files_not_dir_existence():
    """STA-46: _was_patched (→ _was_delta → asg-stamp) must derive from the
    ACTUAL applied .patch files (_live_patch_list), not the patch-dir's mere
    existence — an empty dir would else asg-stamp byte-pristine artifacts
    (violating Position-X) and diverge from the bool(patch_list) sites, so
    `virtual validate` would flag the real build as drift."""
    import inspect, sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _src = inspect.getsource(buildcontainer.BuildContainer.build)
    assert '_was_patched = bool(_live_patch_list)' in _src, _src
    assert '_was_patched = (src_patch_path != self.patch_empty)' not in _src, \
        "still keys _was_patched off patch-dir existence (STA-46)"



def test_sta51_snapshot_pin_origin_tracks_baseurl_host():
    """STA-51: the apt Pin-Priority 1001 `origin` must be the HOST of
    `[Snapshot] BaseUrl`, not a hardcoded snapshot.debian.org — otherwise a
    custom snapshot mirror's sources don't match the pin, priority falls back
    to 500, and the downgrade-refusal loop the pin exists to break returns."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer

    class _M:
        suite = 'bookworm'
        component = 'main'
        def __init__(self, base):
            self.url = f'{base}/20260101T000000Z'

    def _gen(baseurl):
        _stub = type('S', (), {
            'config': type('C', (), {'snapshot_baseurl': baseurl})(),
            'mirrors': [_M(baseurl)],
        })()
        return buildcontainer.BuildContainer._write_snapshot_sources_cmd(_stub)

    # custom snapshot mirror → pin tracks its host
    _custom = _gen('http://snap.example.com/archive')
    assert 'Pin: origin snap.example.com' in _custom, _custom
    assert 'Pin: origin snapshot.debian.org' not in _custom, _custom
    # default Debian service → unchanged behaviour
    _default = _gen('https://snapshot.debian.org/archive')
    assert 'Pin: origin snapshot.debian.org' in _default, _default
    # empty/odd baseurl → safe fallback (no bare `Pin: origin\n`)
    _fallback = _gen('')
    assert 'Pin: origin snapshot.debian.org' in _fallback, _fallback



def test_conf15_per_build_dist_upgrade_workaround_removed():
    """CONF-15: the per-build `apt-get --allow-downgrades dist-upgrade`
    step in cmd_str was the workaround for the toolchain-layer-not-pinned
    bug.  Once the Dockerfile pins the toolchain to snapshot at image-build
    time, this per-build step is redundant — the image's installed
    libraries are already at snapshot.  Removed to keep the per-build
    flow minimal."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    # Find the build() method body (~200-line chunk).
    import re
    _m = re.search(
        r'def build\(self, src_pkg.*?(?=\n    def )', _body, re.DOTALL)
    assert _m, "build() method body not found"
    _build_body = _m.group(0)
    # Strip comment lines so the historical note in the docstring/comments
    # explaining the removal doesn't false-positive.  Only the executable
    # cmd_str shell strings should be searched.
    _code_only = '\n'.join(
        _line for _line in _build_body.splitlines()
        if not _line.lstrip().startswith('#'))
    # Pin: the dist-upgrade command no longer appears in the cmd_str
    # assembly inside build().  (The Dockerfile still uses dist-upgrade
    # — that's correct and lives outside build().)
    assert '--allow-downgrades dist-upgrade' not in _code_only, (
        "Per-build --allow-downgrades dist-upgrade should be removed in "
        "CONF-15 — the toolchain layer is now pinned at image-build time, "
        "making this step redundant")



def test_buildcontainer_injects_athena_codename_env():
    """FORK-01 Step 4: BuildContainer.deb_build_env must set ATHENA_CODENAME
    from BuildConfig.build_codename so debian/rules in fork pkgs can
    substitute the distribution codename into shipped files
    (lsb-release, default-release, debootstrap symlink).  Pin the wiring
    so a refactor of deb_build_env doesn't silently drop it."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    assert 'ATHENA_CODENAME' in _body, "ATHENA_CODENAME not present in buildcontainer.py"
    # Whitespace-tolerant — alignment in __init__ may vary as fields grow.
    import re
    assert re.search(r'self\.codename\s*=\s*config\.build_codename', _body), \
        "self.codename not initialised from config.build_codename"



def test_buildcontainer_emits_token_substitution_snippet():
    """BuildContainer.build assembles a cmd_str that substitutes
    @DISTRIBUTION@, @BASE_ID@, @CODENAME@ in fork content (debian/
    and data/ subdirs of the extracted source) before
    dpkg-buildpackage runs.  This is the central mechanism by which
    fork pkgs get branded — pinning here so a refactor doesn't drop
    it silently.

    Three things pinned:
      1. self.build_distribution / self.build_base_id initialised
         from BuildConfig in __init__.
      2. cmd_str contains a grep-lE for the three tokens (selectivity
         filter: only files actually carrying tokens get sed'd).
      3. sed -i lines exist for all three tokens.
    """
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    assert re.search(
        r'self\.build_distribution\s*=\s*config\.build_distribution', _body), (
        "BuildContainer.build_distribution not initialised from config")
    assert re.search(
        r'self\.build_base_id\s*=\s*config\.build_base_id', _body), (
        "BuildContainer.build_base_id not initialised from config")
    assert "@(DISTRIBUTION|BASE_ID|CODENAME)@" in _body, (
        "token substitution grep filter missing — fork branding will not "
        "resolve")
    for _token in ('@DISTRIBUTION@', '@BASE_ID@', '@CODENAME@'):
        assert f"'s|{_token}|" in _body, (
            f"sed substitution for {_token} not found in build cmd_str")



def test_buildcontainer_token_subst_uses_if_not_short_circuit_and():
    """The optional-dir guards in _token_subst (for data/, tasks/) MUST
    use `if [ -d X ]; then ... fi`, NOT `[ -d X ] && find X`.

    Why: cmd_str runs under `set -e -o pipefail`.  With `&&` short-
    circuit, a missing data/ or tasks/ dir leaves the brace group's
    last command exit at 1.  The pipeline inherits that via pipefail,
    set -e kills the build — every upstream package (which has no
    data/ or tasks/) crashes the substitution step.  Found 2026-05-18.

    The `if`-form returns 0 when the condition is false (no else),
    so missing dirs don't blow up the pipeline."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    assert '[ -d data ] && find' not in _body, (
        "regression: data/ guard uses && (short-circuit fails the build "
        "for upstream pkgs lacking data/); use `if ...; then ...; fi`")
    assert '[ -d tasks ] && find' not in _body, (
        "regression: tasks/ guard uses && (same failure as data/); use "
        "`if ...; then ...; fi`")
    assert 'if [ -d data ]' in _body, "data/ guard missing if-form"
    assert 'if [ -d tasks ]' in _body, "tasks/ guard missing if-form"



def test_buildcontainer_token_subst_no_double_braces_in_regular_strings():
    """The _token_subst opening brace-group MUST be single `{` / `}` in
    regular (non-f) Python string literals.  Doubled `{{` / `}}` is an
    f-string escape — in a REGULAR string they pass through to bash
    as literal `{{`, which bash rejects with `command not found`
    (exit 127).  Found 2026-05-18 (third token-subst bug in the same
    snippet — third regression test in the same file).

    Asserts the source emits the brace pattern correctly by simulating
    the relevant lines.  Direct substring check on buildcontainer.py
    is fragile (any f-string would have `{{` legitimately); instead
    construct the actual cmd_str fragment Python would emit and check
    the bash-visible output.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    # Re-import in case earlier tests cached an older module.
    if 'buildcontainer' in sys.modules:
        import importlib
        importlib.reload(sys.modules['buildcontainer'])
    # Walk buildcontainer.py's source looking for the _token_subst
    # block; ensure no `{{ find` or `}} |` shape (regression sentinel).
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    assert '{{ find debian' not in _body, (
        "regression: _token_subst opening brace doubled (`{{ find ...`) "
        "in a regular string — bash will reject `{{` as command not found "
        "and the build dies with exit 127.  Use single `{ ` (with space).")
    assert '; }} ' not in _body, (
        "regression: _token_subst closing brace doubled (`; }}`) in a "
        "regular string — same exit 127 failure.  Use single `} `.")



def test_buildcontainer_token_subst_grep_rescue_or_true():
    """The grep filter in _token_subst MUST be wrapped to rescue its
    no-match exit (1).  `grep -l` exits 1 when nothing matches —
    true for every upstream package (no @TOKENS@ in their tree).
    Under set -e -o pipefail, that 1 surfaces through the pipeline
    and kills the build.  Found 2026-05-18 on the glibc rebuild
    after the &&-form fix.

    Pattern asserted: `xargs ... grep ... || true` (with or without
    the subshell parens).  This rescue must NOT be removed without
    a replacement strategy for the no-match case (e.g. capture +
    conditional sed)."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    # The grep call must end with `|| true` somewhere before the next
    # pipe stage.  Match across whitespace + the trailing 2>/dev/null
    # so this stays valid if redirection moves.
    _grep_line_pattern = re.compile(
        r"grep -lE '@\(DISTRIBUTION\|BASE_ID\|CODENAME\)@'"
        r"[^|]*?\|\|\s*true",
        re.DOTALL,
    )
    assert _grep_line_pattern.search(_body), (
        "regression: grep filter is no longer wrapped with `|| true` — "
        "upstream pkgs (with no @TOKENS@) will crash the build at the "
        "token-substitution step because grep -l exits 1 on no matches "
        "and pipefail surfaces it under set -e")



def test_buildcontainer_changelog_uses_codename_field():
    """The _changelog_bump snippet's stanza distribution field
    (after the version, before urgency=) must use self.codename, not
    a hardcoded literal.  Bug shape: hardcoded 'thor' wouldn't roll
    over if [Build] CODENAME changes to a new release codename
    (Debian's bookworm → trixie analog).  Pinned so future renames
    update the right slot."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    # Either the f-string form interpolating self.codename, or a
    # plain string composition referring to it.  Hardcoded 'thor'
    # in the printf is the failure shape we reject.
    assert 'printf "%s (%s) thor;' not in _body, (
        "_changelog_bump hardcodes 'thor' in stanza distribution field — "
        "must read self.codename so [Build] CODENAME rolls correctly")



def test_strip_nmu_from_built_artifacts_does_not_scan_repo():
    """STA-21 anti-regression: _strip_nmu_from_built_artifacts must
    NOT walk repo/ subdirs nor open .debs to filter by Source field.
    Both were O(N) over the whole repo and caused a 2-3 minute
    post-build stall on every source build (CPU + I/O saturation
    from DebFile-opening ~4500 files to find the 1-50 we care about).

    Pin via code inspection — function body must not contain the
    tell-tale walk markers or DebFile import.  If a future refactor
    legitimately needs to re-introduce a walk (unlikely), it should
    do so behind a guard that proves the just-emitted list isn't
    enough — and update this test accordingly."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    # Renamed _strip_nmu_from_built_artifacts → _normalize_built_artifacts
    # (strip + asg-stamp folded into one just-emitted-artifacts pass).
    _m = re.search(
        r'def _normalize_built_artifacts\(.*?(?=\n    def )',
        _body, re.DOTALL)
    assert _m, "_normalize_built_artifacts not found"
    _fn_body = _m.group(0)
    # Strip docstrings + comments so anti-pattern checks don't match
    # the explanatory text describing the OLD bug.  Crude but adequate
    # for this purpose — we want to flag CODE that imports/uses the
    # anti-patterns, not prose that mentions them.
    _code_only = re.sub(r'""".*?"""', '', _fn_body, flags=re.DOTALL)
    _code_only = re.sub(r"'''.*?'''", '', _code_only, flags=re.DOTALL)
    _code_only = '\n'.join(
        _ln for _ln in _code_only.splitlines()
        if not _ln.lstrip().startswith('#')
    )
    # The anti-patterns we removed:
    assert 'DebFile(' not in _code_only and 'import DebFile' not in _code_only, (
        "STA-21 REGRESSION: _strip_nmu_from_built_artifacts opens "
        "files with DebFile — the slow code path (4500 fork+exec+tar "
        "cycles per build) we explicitly removed.  The just-emitted "
        "file list from segregate already identifies what to touch."
    )
    assert 'os.listdir' not in _code_only, (
        "STA-21 REGRESSION: _strip_nmu_from_built_artifacts walks the "
        "repo via os.listdir — the slow code path.  Iterate the "
        "built_files arg directly."
    )
    # Hard-coded subdir-name iteration is the tell-tale repo-walk
    # pattern: `for _sub in ('main', 'doc', ...)`.  Looking at the
    # tuple-literal form catches it without flagging the prose that
    # mentions the subdirs in passing.
    assert not re.search(r"for\s+\w+\s+in\s+\(\s*'main'", _code_only), (
        "STA-21 REGRESSION: hard-coded subdir-name iteration suggests "
        "a repo walk has been re-introduced.  Iterate built_files only."
    )
    # And the new signature must accept the file list.
    assert re.search(
        r'def _normalize_built_artifacts\(self,\s*src_pkg,\s*\n?\s*built_files',
        _fn_body
    ), (
        "_normalize_built_artifacts must accept `built_files` as "
        "its second positional arg (the list from segregate)."
    )



# ─────────────────────────────────────────────────────────────────────────────
# COMP-14 — grub-mkrescue runs inside the BuildContainer (bookworm GRUB
# toolchain instead of build host's).  Helper method + call-site wiring.
# ─────────────────────────────────────────────────────────────────────────────


def test_buildcontainer_run_grub_mkrescue_constructs_correct_docker_call():
    """COMP-14: BuildContainer.run_grub_mkrescue must invoke
    docker.containers.run with the right image, command, and volume
    mounts so the produced ISO embeds bookworm's GRUB toolchain.

    Pin the load-bearing pieces of the docker invocation:
      - apt-get update + install of grub-common + grub-pc-bin +
        grub-efi-amd64-bin + xorriso + mtools + dosfstools
      - grub-mkrescue with -o /output/<basename> /staging
      - chown back to host uid:gid so the ISO is operator-readable
      - staging dir mounted at /staging
      - output dir (parent of iso_path) mounted at /output
    """
    import sys
    from unittest.mock import MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer

    # Build a minimal BuildContainer without going through __init__ (which
    # touches docker + snapshot + apt).  Inject only the attributes
    # run_grub_mkrescue reads.
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc._image_tag = 'athenalinux:build-test'
    _bc.arch = 'amd64'
    _bc._container_labels = {}  # set by __init__ (bypassed here)
    import threading as _threading
    _bc._live_lock = _threading.Lock()
    _bc._live = {}
    _bc.mirrors = []   # empty → empty sources.list; OK for argv-shape pin
    # _write_snapshot_sources_cmd (called by run_grub_mkrescue) reads
    # config.snapshot_baseurl for the apt pin origin (STA-51).
    _bc.config = type('C', (), {
        'snapshot_baseurl': 'https://snapshot.debian.org/archive'})()
    _bc.client = MagicMock()
    _fake_container = MagicMock()
    _fake_container.short_id = 'fakecid'
    _fake_container.wait.return_value = {'StatusCode': 0}
    _fake_container.logs.return_value = b'log line'
    _bc.client.containers.run.return_value = _fake_container

    _ok, _stdout, _stderr = _bc.run_grub_mkrescue(
        '/some/staging', '/host/image/out.iso', password='unused',
    )
    assert _ok is True, (_ok, _stdout, _stderr)

    # docker.containers.run was called once with our expected args
    _call = _bc.client.containers.run.call_args
    assert _call is not None, 'docker.containers.run was never called'
    _args, _kwargs = _call.args, _call.kwargs
    # image is positional arg 0; bash command via kwarg
    assert _args[0] == 'athenalinux:build-test', _args
    _cmd_arr = _kwargs.get('command')
    assert _cmd_arr and _cmd_arr[:2] == ['/bin/bash', '-c'], _cmd_arr
    _cmd_str = _cmd_arr[2]
    # apt-install of the GRUB toolchain
    for _pkg in ('grub-common', 'grub-pc-bin', 'grub-efi-amd64-bin',
                 'xorriso', 'mtools', 'dosfstools'):
        assert _pkg in _cmd_str, (
            f"missing {_pkg} in apt-install — broken COMP-14 toolchain: "
            f"{_cmd_str!r}"
        )
    # grub-mkrescue with the right output path + staging
    assert 'grub-mkrescue -o /output/out.iso /staging' in _cmd_str, _cmd_str
    # chown back to host uid:gid (so resulting ISO is operator-readable)
    assert f'chown {os.getuid()}:{os.getgid()}' in _cmd_str, _cmd_str

    # Volume mounts: staging → /staging, parent(iso) → /output
    _vol = _kwargs.get('volumes')
    assert _vol == {
        '/some/staging': {'bind': '/staging', 'mode': 'rw'},
        '/host/image':   {'bind': '/output',  'mode': 'rw'},
    }, _vol

    # Container is removed (no leak — same lifecycle as build())
    _fake_container.remove.assert_called_once_with(force=True)


def test_run_grub_mkrescue_mounts_localmirror_when_active():
    """When the local build mirror is active, run_grub_mkrescue writes a
    `file:///localmirror` apt source (via _write_snapshot_sources_cmd) and
    apt-get update reads it BEFORE installing the grub toolchain — so the
    container MUST bind-mount /localmirror, exactly as build() does.  Without
    it apt fails 'file:/localmirror/./Packages File not found' and
    grub-mkrescue exits 100 (regression once the mirror was enabled)."""
    from unittest.mock import MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc._image_tag = 'athenalinux:build-test'
    _bc.arch = 'amd64'
    _bc._container_labels = {}
    import threading as _threading
    _bc._live_lock = _threading.Lock()
    _bc._live = {}
    _bc.mirrors = []
    _bc._localmirror_active = True
    _bc.config = type('C', (), {
        'snapshot_baseurl': 'https://snapshot.debian.org/archive',
        'dir_localmirror': '/host/localmirror'})()
    _bc.client = MagicMock()
    _fc = MagicMock()
    _fc.short_id = 'c'; _fc.wait.return_value = {'StatusCode': 0}
    _fc.logs.return_value = b''
    _bc.client.containers.run.return_value = _fc

    _bc.run_grub_mkrescue('/some/staging', '/host/image/out.iso',
                          password='unused')
    _kwargs = _bc.client.containers.run.call_args.kwargs
    # the file:///localmirror source is written into the cmd
    assert 'file:///localmirror' in _kwargs['command'][2]
    # ...and the mount is present, ro, matching the written source
    assert _kwargs['volumes'].get('/host/localmirror') == {
        'bind': '/localmirror', 'mode': 'ro'}, _kwargs['volumes']


def test_buildcontainer_run_grub_mkrescue_propagates_failure():
    """Non-zero exit from docker container → (False, stdout, stderr)."""
    import sys
    from unittest.mock import MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer

    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc._image_tag = 'athenalinux:build-test'
    _bc.arch = 'amd64'
    _bc._container_labels = {}  # set by __init__ (bypassed here)
    import threading as _threading
    _bc._live_lock = _threading.Lock()
    _bc._live = {}
    _bc.mirrors = []
    # run_grub_mkrescue → _write_snapshot_sources_cmd reads
    # config.snapshot_baseurl for the apt pin origin (STA-51).
    _bc.config = type('C', (), {
        'snapshot_baseurl': 'https://snapshot.debian.org/archive'})()
    _bc.client = MagicMock()
    _fake_container = MagicMock()
    _fake_container.short_id = 'failcid'
    _fake_container.wait.return_value = {'StatusCode': 42}
    _fake_container.logs.return_value = b'grub-mkrescue: error: foo'
    _bc.client.containers.run.return_value = _fake_container

    _ok, _stdout, _stderr = _bc.run_grub_mkrescue(
        '/some/staging', '/host/image/out.iso', password='unused',
    )
    assert _ok is False
    # Container still removed on failure
    _fake_container.remove.assert_called_once_with(force=True)



def test_cohort_resolvers_route_through_canonical_names():
    """Every cohort/corpus resolver must route through _canonical_names.
    Anti-regression for any future resolver that re-introduces raw
    selected_pkgs.keys() and brings back the virtual-alias false
    positives."""
    _body = _session_source()
    import re
    for _fn in ('_resolve_live_cohort',
                '_resolve_deb_cohort',
                '_resolve_udeb_cohort',
                '_resolve_install_corpus',
                '_resolve_installer_cohort'):
        _m = re.search(
            rf"\n    def {_fn}\b.*?(?=\n    def \w)",
            _body, re.DOTALL,
        )
        assert _m is not None, f"{_fn} not found"
        _method = _m.group(0)
        assert '_canonical_names' in _method, (
            f"{_fn} no longer uses _canonical_names — virtual aliases "
            f"would leak into cohort, false-positive conflicts on the "
            f"fork-replaces-upstream idiom"
        )
        assert 'selected_pkgs.keys()' not in _method, (
            f"{_fn} reverted to raw selected_pkgs.keys() — would "
            f"include virtual aliases.  Route through _canonical_names."
        )



def test_source_build_pkg_subset_excludes_live_installer_extras():
    """Phase 4: the 'pkg' subset selection rule excludes everything in
    live_exclusive_src_names ∪ installer_exclusive_src_names ∪
    extras_src_names.  This tests the set-arithmetic the cmd_source_build
    'pkg' branch performs."""
    # Pure data-shape test of the exclusion rule; doesn't run cmd_source_build
    # itself (which needs flags + container).
    selected_srcs = {
        'kernel-src':    object(),  # pkg
        'live-config':   object(),  # live extra
        'cdebconf':      object(),  # installer (udeb-producing)
        'firefox-l10n':  object(),  # extras
        'libc6':         object(),  # pkg
    }
    live_exclusive      = {'live-config'}
    installer_exclusive = {'cdebconf'}
    extras              = {'firefox-l10n'}
    _exclude = live_exclusive | installer_exclusive | extras
    pkg_layer = [
        _s for _name, _s in selected_srcs.items() if _name not in _exclude
    ]
    pkg_layer_names = [n for n, _s in selected_srcs.items() if _s in pkg_layer]
    assert sorted(pkg_layer_names) == ['kernel-src', 'libc6']



def test_source_build_installer_subset_unions_udeb_tree_with_deb_arm():
    """Phase 4: the 'installer' subset selection rule unions
    udeb_dep_tree.selected_srcs.keys() with installer_exclusive_src_names
    (deb-arm of installer.list).  Pin the union semantic so a future
    refactor can't silently drop one or the other."""
    # Simulate two trees: deb tree has installer-exclusive efibootmgr;
    # udeb tree has cdebconf, partman-base, hw-detect.
    deb_installer_exclusive = {'efibootmgr', 'grub-pc-bin'}
    udeb_selected_srcs_keys = {'cdebconf', 'partman-base', 'hw-detect'}
    _src_names_set = set(deb_installer_exclusive) | set(udeb_selected_srcs_keys)
    assert _src_names_set == {
        'efibootmgr', 'grub-pc-bin', 'cdebconf', 'partman-base', 'hw-detect',
    }
    # Order doesn't matter but caller iterates sorted() for deterministic output
    assert sorted(_src_names_set) == [
        'cdebconf', 'efibootmgr', 'grub-pc-bin', 'hw-detect', 'partman-base',
    ]



def test_obs02_record_phase_archives_prior_run_before_overwrite():
    """OBS-02: _record_phase archives the PRIOR completed run to the journal at
    entry — before write_build_record overwrites build.json — so each run is
    stored exactly once (current in build.json, prior in the journal)."""
    import inspect
    import buildcontainer
    _src = inspect.getsource(buildcontainer.BuildContainer._record_phase)
    assert 'archive_build_record' in _src
    # archived from the prior record, before the overwriting write
    assert _src.index('archive_build_record') < _src.index('write_build_record')
    assert "_prior.get('phase') in ('done', 'failed')" in _src



def test_source_build_all_selection_unions_both_trees_no_exclusions():
    """The 'all' branch of cmd_source_build's selection logic must yield
    the union of dep_tree.selected_srcs + udeb_dep_tree.selected_srcs
    with NO live/installer/extras exclusion.  Anti-regression for the
    intent of the 'all' subset: convenience over staging — running it
    is equivalent to pkg + live + installer + recommended in one pass."""
    # Replicate the selection logic the 'all' branch uses (set union
    # across both trees, sorted, no exclusion filter).  Data-shape test
    # — doesn't run cmd_source_build (which needs container + flags).
    deb_srcs = {'kernel-src': 1, 'live-config': 2, 'libksba': 3,
                'cdebconf': 4}                # cdebconf overlaps with udeb
    udeb_srcs = {'cdebconf': 4, 'partman-base': 5}
    expected_names = sorted(set(deb_srcs) | set(udeb_srcs))
    # No exclusion sets applied — this is the contract.
    assert expected_names == [
        'cdebconf', 'kernel-src', 'libksba',
        'live-config', 'partman-base'], (
        "'all' must not exclude live/installer/extras — it's the "
        "one-shot 'build everything we selected' mode")



def test_buildcontainer_build_signature_accepts_profile_override_kwargs():
    """BuildContainer.build must accept profiles_override + options_override
    as keyword-only args.  Catches accidental signature regressions."""
    import inspect
    from buildcontainer import BuildContainer
    sig = inspect.signature(BuildContainer.build)
    assert 'profiles_override' in sig.parameters
    assert 'options_override' in sig.parameters
    assert sig.parameters['profiles_override'].kind == \
           inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters['options_override'].kind == \
           inspect.Parameter.KEYWORD_ONLY



def test_worker_exception_surfaces_console_fail():
    """A worker exception (ENOSPC, an unexpected raise) is counted in
    _failed, so it MUST also emit a per-package `[FAIL]` on the console —
    else the console shows fewer named failures than the "N failed" total
    (the ENOSPC-libreoffice gap, 2026-07-08: counted but never surfaced).
    Both the local and remote parallel loops' `except` handlers must
    console.print, not just logger.error."""
    _body = _session_source()
    import re
    for _marker in ('worker for {_pkg.package} raised',
                    'remote worker {_src.package} raised'):
        _m = re.search(
            re.escape(_marker) + r".*?(?=\n\s+_(?:result|res) = 'failed')",
            _body, re.DOTALL)
        assert _m, f"handler for {_marker!r} not found"
        assert 'console.print(' in _m.group(0) and '[FAIL]' in _m.group(0), (
            f"the {_marker!r} handler must console.print a [FAIL] line so "
            "named console failures match the failed count")


def test_source_audit_uses_per_source_progress_bar():
    """`cmd_source_audit` wraps its per-cohort selected_srcs walk in
    a ProgressBar — 500+ sources × multi-file ar-validity checks each
    is enough to look frozen without progress feedback."""
    _body = _session_source()
    import re
    _m = re.search(
        r"\n    def cmd_source_audit\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m, "cmd_source_audit body not found"
    _method = _m.group(0)
    assert 'ProgressBar(' in _method, (
        "cmd_source_audit must wrap its selected_srcs walk in a ProgressBar"
    )
    assert 'show_rate=False' in _method, (
        "cmd_source_audit ProgressBar must use show_rate=False "
        "(per-source ar-validity check time variance is high)"
    )



def test_tier3_cleanup_source_pins():
    """Pins for Tier-3 fixes #26/#36/#177 (best-effort cleanup / doc accuracy /
    empty-file guard) where a behavioural harness is disproportionate."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    import buildlog
    import select_packages
    # #26: copied SSH key removed on the add_remote failure path
    assert 'os.remove(_keydst)' in inspect.getsource(build), '#26'
    # #36: the over-broad "MUST NOT raise" claim is softened to be accurate
    assert 'MUST NOT raise into the build path' not in inspect.getsource(
        buildlog), '#36'
    # #177: don't create an empty pool.list
    assert 'if _pool_sel or os.path.isfile(self._poolpath):' in inspect.getsource(
        select_packages), '#177'



def test_cmd_source_audit_pool_remediation_is_runnable():
    """Regression (audit #76): the source-audit rebuild-queue command map must
    not suggest 'source build (pool)' — 'pool' is not a valid _SOURCE_SUBSETS
    verb, so the command can't be run; pool extras build via 'source build
    all'."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import commands.cmd_source as _cs
    _src = inspect.getsource(_cs)
    assert 'source build (pool)' not in _src, (
        "audit remediation must not suggest the non-runnable "
        "'source build (pool)' — map pool to 'source build all'")



def test_resolve_live_cohort_subtracts_pool_and_installer():
    """live cohort = dep_tree.selected_pkgs − pool_extras −
    installer_exclusive.  Pin via code inspection."""
    _body = _session_source()
    import re
    _m = re.search(
        r'def _resolve_live_cohort\(self.*?(?=\n    def )',
        _body, re.DOTALL)
    assert _m, "_resolve_live_cohort not found"
    _method = _m.group(0)
    assert 'pool_extras_pkg_names' in _method, (
        "live cohort must subtract pool_extras_pkg_names — pool pkgs "
        "don't get auto-installed in the live chroot, apt arbitrates")
    assert 'installer_exclusive_pkg_names' in _method, (
        "live cohort must subtract installer_exclusive_pkg_names — "
        "installer-support debs not in the live chroot's install set")



def test_resolve_installer_cohort_uses_udeb_tree():
    """installer cohort = udeb_dep_tree.selected_pkgs (the d-i ramdisk).
    Pin via code inspection."""
    _body = _session_source()
    import re
    _m = re.search(
        r'def _resolve_installer_cohort\(self.*?(?=\n    def )',
        _body, re.DOTALL)
    assert _m, "_resolve_installer_cohort not found"
    _method = _m.group(0)
    assert 'udeb_dep_tree.selected_pkgs' in _method, (
        "installer cohort is just udeb_dep_tree.selected_pkgs — udebs "
        "in the ramdisk only; debs install on target separately")



def test_verify_pkg_artifact_ok_when_all_fields_match():
    """Happy path: synthetic .deb whose internal Pkg/Version/Arch
    match the filename + no Depends → (True, 'ok-nocache') when no
    cache supplied, or (True, 'ok') when cache supplied."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return  # skip on hosts without dpkg-deb
    with tempfile.TemporaryDirectory() as _tmp:
        _path = os.path.join(_tmp, 'foo_1.0+thor1_amd64.deb')
        _build_minimal_deb(_path, 'foo', '1.0+thor1', 'amd64')
        _bc = _make_buildcontainer_stub(repo=_tmp)
        _ok, _why = _bc.verify_pkg_artifact(_path, 'foo_1.0+thor1_amd64.deb')
        assert _ok, f"expected OK, got ({_ok}, {_why})"



def test_verify_pkg_artifact_fails_on_internal_version_mismatch():
    """When the .deb's internal Version doesn't match the expected
    filename's version, repair/check_build MUST treat the artifact
    as stale and refuse PASS.  This is the kernel-rebump shape: a
    .deb whose filename was renamed +thor1 but whose internal
    Version field wasn't updated would fail this check."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    with tempfile.TemporaryDirectory() as _tmp:
        # .deb's internal version is 1.0 — but filename claims 1.0+thor1
        _real_path = os.path.join(_tmp, 'foo_1.0+thor1_amd64.deb')
        _build_minimal_deb(_real_path, 'foo', '1.0', 'amd64')
        _bc = _make_buildcontainer_stub(repo=_tmp)
        _ok, _why = _bc.verify_pkg_artifact(_real_path, 'foo_1.0+thor1_amd64.deb')
        assert not _ok, "expected version mismatch to fail verify"
        assert 'version-mismatch' in _why, _why



def test_verify_pkg_artifact_fails_on_unsatisfied_depends():
    """When the .deb declares a Depends that's not present in the
    cache, verify must fail with unsatisfied-Depends diagnostic.

    Synthesizes a cache that only has libfoo 1.0 in its hashtable;
    .deb declares Depends: libnonexistent (>= 2.0) → not in cache →
    fail."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

    with tempfile.TemporaryDirectory() as _tmp:
        _path = os.path.join(_tmp, 'foo_1.0_amd64.deb')
        _build_minimal_deb(_path, 'foo', '1.0', 'amd64',
                            depends='libnonexistent (>= 2.0)')

        # Cache with only libfoo, NOT libnonexistent
        from collections import defaultdict
        class _Cache:
            pass
        _cache = _Cache()
        _cache.package_hashtable = defaultdict(lambda: defaultdict(list))
        _cache.package_hashtable['libfoo']['1.0'] = ['<placeholder>']

        _bc = _make_buildcontainer_stub(cache=_cache, repo=_tmp)
        _ok, _why = _bc.verify_pkg_artifact(_path, 'foo_1.0_amd64.deb')
        assert not _ok, "expected unsatisfied Depends to fail verify"
        assert 'unsatisfied-Depends' in _why, _why



def test_verify_pkg_artifact_passes_on_satisfied_depends():
    """When Depends are present in the cache at a satisfying version,
    verify must pass."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    with tempfile.TemporaryDirectory() as _tmp:
        _path = os.path.join(_tmp, 'foo_1.0_amd64.deb')
        _build_minimal_deb(_path, 'foo', '1.0', 'amd64',
                            depends='libbar (>= 2.0)')
        from collections import defaultdict
        class _Cache: pass
        _cache = _Cache()
        _cache.package_hashtable = defaultdict(lambda: defaultdict(list))
        # 2.5 satisfies >= 2.0
        _cache.package_hashtable['libbar']['2.5'] = ['<placeholder>']

        _bc = _make_buildcontainer_stub(cache=_cache, repo=_tmp)
        _ok, _why = _bc.verify_pkg_artifact(_path, 'foo_1.0_amd64.deb')
        assert _ok, f"expected OK, got ({_ok}, {_why})"



def test_verify_pkg_artifact_strips_epoch_from_internal_version():
    """Debian filename convention strips epoch (`libc6_2.36-9_amd64.deb`
    for an internal `Version: 2:2.36-9`).  My verify_pkg_artifact
    must canonicalise the internal version before comparing — raw
    `==` would false-positive every epoch-bearing package.

    Caught 2026-05-19: source rescan reported 283 'would rebuild' vs
    repair's 0 restored, because the verify was rejecting every
    epoch-bearing .deb on a spurious version-mismatch."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    with tempfile.TemporaryDirectory() as _tmp:
        # Filename strips epoch; internal Version has it.
        _path = os.path.join(_tmp, 'foo_1.0+thor1_amd64.deb')
        _build_minimal_deb(_path, 'foo', '2:1.0+thor1', 'amd64')
        _bc = _make_buildcontainer_stub(repo=_tmp)
        _ok, _why = _bc.verify_pkg_artifact(_path, 'foo_1.0+thor1_amd64.deb')
        assert _ok, (
            "epoch-bearing internal Version must match filename's "
            f"no-epoch version part; got ({_ok}, {_why})")



def test_verify_pkg_artifact_resolves_udeb_deps_via_udeb_hashtable():
    """For a .udeb artifact, Depends must be resolved against
    cache.udeb_hashtable (the parallel d-i namespace), NOT
    cache.package_hashtable.  Mixing namespaces masks real deps
    AND can false-positive when a name exists in one table but
    not the other.

    Test shape: a .udeb declares Depends: foo-udeb.  Cache has
    foo-udeb ONLY in udeb_hashtable, not in package_hashtable.
    The check must consult the udeb hashtable and pass."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    with tempfile.TemporaryDirectory() as _tmp:
        _path = os.path.join(_tmp, 'foo-udeb_1.0_amd64.udeb')
        _build_minimal_deb(_path, 'foo-udeb', '1.0', 'amd64',
                            depends='bar-udeb (>= 1.0)')
        from collections import defaultdict
        class _Cache:
            pass
        _cache = _Cache()
        # bar-udeb is in UDEB table only — NOT in deb table.
        _cache.package_hashtable = defaultdict(lambda: defaultdict(list))
        _cache.udeb_hashtable    = defaultdict(lambda: defaultdict(list))
        _cache.udeb_hashtable['bar-udeb']['2.0'] = ['<placeholder>']
        _bc = _make_buildcontainer_stub(cache=_cache, repo=_tmp)
        _ok, _why = _bc.verify_pkg_artifact(_path, 'foo-udeb_1.0_amd64.udeb')
        assert _ok, (
            ".udeb Depends must resolve via udeb_hashtable; got "
            f"({_ok}, {_why})")



def test_verify_pkg_artifact_substvar_skip_logic_present():
    """A .deb whose Depends contains `${shlibs:Depends}` is a build-
    time bug (dpkg-gencontrol didn't expand the substvar), not a
    cache-drift bug.  verify_pkg_artifact must skip these entries
    rather than fail with a confusing unsatisfied-Depends naming a
    pseudo-package.

    Code-inspection test: dpkg-deb refuses to package a control file
    with literal `${...}` in Depends (validates syntax during build),
    so a runtime fixture isn't constructible.  Instead, scan the
    verify_pkg_artifact source for the substvar-skip guard."""
    _bp = open(os.path.join(_ROOT, 'scripts', 'buildcontainer.py')).read()
    import re
    _m = re.search(
        r'def verify_pkg_artifact\(self.*?\n    def ',
        _bp, re.DOTALL,
    )
    assert _m, "verify_pkg_artifact not found"
    _body = _m.group(0)
    assert "'${' in _deps_str" in _body or "${' in _deps_str" in _body, (
        "verify_pkg_artifact must skip Depends with unresolved "
        "substvars (defensive guard against half-built .debs).")



def test_verify_pkg_artifact_or_group_satisfied_by_any_alternative():
    """An OR-group (`A | B`) is satisfied if ANY alternative resolves
    in cache, even if the first doesn't.  Mirrors dpkg's resolution
    semantics."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    with tempfile.TemporaryDirectory() as _tmp:
        _path = os.path.join(_tmp, 'foo_1.0_amd64.deb')
        _build_minimal_deb(_path, 'foo', '1.0', 'amd64',
                            depends='libmissing | libpresent')
        from collections import defaultdict
        class _Cache: pass
        _cache = _Cache()
        _cache.package_hashtable = defaultdict(lambda: defaultdict(list))
        _cache.package_hashtable['libpresent']['1.0'] = ['<placeholder>']

        _bc = _make_buildcontainer_stub(cache=_cache, repo=_tmp)
        _ok, _why = _bc.verify_pkg_artifact(_path, 'foo_1.0_amd64.deb')
        assert _ok, f"OR-group with one satisfying alternative should pass; got ({_ok}, {_why})"



def test_mat10c_bump_decision_parity_triggers():
    """MAT-10(c): the version decision is duplicated in the real-build path
    (buildcontainer._normalize_built_artifacts) and the virtual-build path
    (virtual_build.synthesize_source_binaries) — guard against DRIFT.

    Under the TRANSPOSE scheme both paths must reference the SAME machinery:
    the trailing-token `transpose` and the per-source patch level P (the
    `patch_bump_count` / was_patched signal).  A future edit that switches one
    path back to a ship-order/strip decision fails here, forcing a re-sync."""
    import inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer as _bc
    import virtual_build as _vb
    _real = inspect.getsource(_bc.BuildContainer._normalize_built_artifacts)
    _virt = inspect.getsource(_vb.synthesize_source_binaries)
    for _src, _name in ((_real, 'real-build'), (_virt, 'virtual-build')):
        assert 'transpose' in _src, \
            f'{_name}: transpose machinery missing (ship-order regression?)'
        assert ('patch_level' in _src or 'patch_bump_count' in _src
                or 'was_patched' in _src), \
            f'{_name}: patch-level P signal missing'



def test_validate_canonical_attribution_tunnel_and_build_config():
    """virtual validate, deterministic comparison:
      - a binary DECLARED by two sources but whose canonical upstream
        Source is a different in-scope source is attributed to the producer
        on BOTH sides (the linux / linux-signed-amd64 installer-udeb split),
        so neither source shows phantom under/over-prediction;
      - a declared binary absent from disk is BUILD-CONFIG divergence, not
        drift;
      - a tunneled source's declared-but-unmaterialised binaries are
        expected (tunnel subset), not drift.
    """
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    import sys, types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    import utils as _u
    with tempfile.TemporaryDirectory() as _root:
        _repo = os.path.join(_root, 'repo', 'main')
        _log = os.path.join(_root, 'log', 'build')
        os.makedirs(_repo)
        os.makedirs(_log)
        # On disk: c-main (consumer), shared (produced by prod), tun-a (tun).
        for _n, _v in (('c-main', '1.0'), ('shared', '1.0'), ('tun-a', '1.0')):
            _build_minimal_deb(os.path.join(_repo, f'{_n}_{_v}_amd64.deb'),
                               _n, _v, 'amd64')
        for _pkg in ('prod', 'consumer', 'tun'):
            _rec = _u.new_build_record(package=_pkg, intended_version='1.0',
                                       patch_set_hash='',
                                       started='2026-06-08T00:00:00Z')
            _rec.update({'phase': 'done', 'status': 'PASS'})
            _u.write_build_record(_log, _rec)

        def _src(name, binaries):
            return types.SimpleNamespace(
                package=name, version='1.0', binary=binaries,
                patch_list=[], package_list=[], files={}, _mirror=None)
        _sources = {
            'prod':     _src('prod', ['shared']),
            'consumer': _src('consumer', ['c-main', 'shared', 'c-doc']),
            'tun':      _src('tun', ['tun-a', 'tun-b']),
        }
        # Universe: every binary exists upstream; `shared` is produced by
        # `prod`; `c-doc` exists upstream (so it's a build-config skip).
        def _rec_for(name, src):
            return {'Package': name, 'Source': src, 'Version': '1.0',
                    'Architecture': 'amd64'}
        _universe = {
            'shared':  {'1.0': _rec_for('shared', 'prod')},
            'c-main':  {'1.0': _rec_for('c-main', 'consumer')},
            'c-doc':   {'1.0': _rec_for('c-doc', 'consumer')},
            'tun-a':   {'1.0': _rec_for('tun-a', 'tun')},
            'tun-b':   {'1.0': _rec_for('tun-b', 'tun')},
        }
        _canon = {'shared': 'prod', 'c-main': 'consumer', 'c-doc': 'consumer',
                  'tun-a': 'tun', 'tun-b': 'tun'}
        _stats, _findings = _vb.validate_against_build_records(
            source_names=['prod', 'consumer', 'tun'],
            source_lookup=lambda n: _sources.get(n),
            package_universe=_universe, release=1,
            arch='amd64', buildlog_dir=_log, repo_dir=os.path.join(_root, 'repo'),
            canonical_src_map=_canon, tunnel_sources=frozenset({'tun'}))
        # prod: shared declared + on disk (canonical=prod) → matched.
        # tun:  tun-b declared-not-on-disk but tunneled → matched.
        # consumer: shared attributed to prod (canonical) on BOTH sides; c-doc
        #           declared-not-built → build-config, NOT drift.
        assert _stats['sources_drifted'] == 0, _findings
        assert _stats['sources_matched'] == 2, _stats          # prod + tun
        assert _stats.get('buildcfg_sources') == 1, _stats     # consumer
        assert _stats.get('buildcfg_binaries') == 1, _stats    # c-doc
        assert _stats.get('missing_sources', 0) == 0, _stats   # no under-predict
        _kinds = {_k for _s, _k, _m in _findings}
        assert 'virtual_validate_build_config_divergence' in _kinds, _findings
        assert 'virtual_validate_synth_over_predicted' not in _kinds, _findings



def test_validate_transpose_prediction_matches_built_asg():
    """TRANSPOSE validate round-trip: the cache universe holds the +debNuK
    upstream version; the build emitted +asgK.  The synth transposes the
    universe version to the SAME +asgK, so the source MATCHES with no drift —
    deterministic from the upstream version, no ledger reconstruction needed."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    import sys, types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    import utils as _u
    with tempfile.TemporaryDirectory() as _root:
        _repo = os.path.join(_root, 'repo', 'main')
        _log = os.path.join(_root, 'log', 'build')
        os.makedirs(_repo)
        os.makedirs(_log)
        # On disk: foosrc emitted two binaries transposed to +asg1u2.
        for _n in ('foo', 'foo-lib'):
            _build_minimal_deb(
                os.path.join(_repo, f'{_n}_1.0-1+asg1u2_amd64.deb'),
                _n, '1.0-1+asg1u2', 'amd64')
        _rec = _u.new_build_record(package='foosrc', intended_version='1.0-1',
                                   patch_set_hash='',
                                   started='2026-06-08T00:00:00Z')
        _rec.update({'phase': 'done', 'status': 'PASS'})
        _u.write_build_record(_log, _rec)

        _sources = {
            'foosrc': types.SimpleNamespace(
                package='foosrc', version='1.0-1+deb12u2',
                binary=['foo', 'foo-lib'],
                patch_list=[], package_list=[], files={}, _mirror=None),
        }
        # The cache universe holds the +deb upstream the binaries were built
        # from; transpose maps it to the +asg the build emitted.
        _universe = {
            'foo':     {'1.0-1+deb12u2': {
                'Package': 'foo', 'Source': 'foosrc',
                'Version': '1.0-1+deb12u2', 'Architecture': 'amd64'}},
            'foo-lib': {'1.0-1+deb12u2': {
                'Package': 'foo-lib', 'Source': 'foosrc',
                'Version': '1.0-1+deb12u2', 'Architecture': 'amd64'}},
        }
        _led = {'foo': ['1.0-1+asg1u2'], 'foo-lib': ['1.0-1+asg1u2']}
        _canon = {'foo': 'foosrc', 'foo-lib': 'foosrc'}
        _stats, _findings = _vb.validate_against_build_records(
            source_names=['foosrc'],
            source_lookup=lambda n: _sources.get(n),
            package_universe=_universe, release=1,
            arch='amd64', buildlog_dir=_log,
            repo_dir=os.path.join(_root, 'repo'),
            canonical_src_map=_canon)
        assert _stats['sources_drifted'] == 0, (_stats, _findings)
        assert _stats['sources_matched'] == 1, (_stats, _findings)
        assert _stats.get('asg_drift_sources', 0) == 0, _stats



def test_verify_output_hashes_flags_only_present_drift_not_pruned_absent():
    """verify_output_hashes flags a record output whose on-disk file's
    SHA-256 disagrees with the recorded hash (re-pack drift), but does NOT
    flag an output that is simply ABSENT from the pruned repo.  A matching
    output is silent."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    with tempfile.TemporaryDirectory() as _root:
        _repo = os.path.join(_root, 'repo', 'main')
        _log = os.path.join(_root, 'log', 'build')
        os.makedirs(_repo)
        os.makedirs(_log)
        # Two real debs on disk: 'good' (hash will match) + 'drift' (we will
        # record a WRONG hash for it).  A third output 'pruned' is named in
        # the record but never written to disk.
        _good = os.path.join(_repo, 'good_1.0-1_amd64.deb')
        _drift = os.path.join(_repo, 'drift_1.0-1_amd64.deb')
        _build_minimal_deb(_good, 'good', '1.0-1', 'amd64')
        _build_minimal_deb(_drift, 'drift', '1.0-1', 'amd64')
        _rec = _u.new_build_record(
            package='src', intended_version='1.0-1',
            patch_set_hash='', started='2026-06-08T00:00:00Z')
        _rec.update({
            'phase': 'done', 'status': 'PASS',
            'outputs': ['good_1.0-1_amd64.deb', 'drift_1.0-1_amd64.deb',
                        'pruned_1.0-1_amd64.deb'],
            'output_hashes': {
                'good_1.0-1_amd64.deb': _u.get_sha256(_good),     # correct
                'drift_1.0-1_amd64.deb': 'f' * 64,                # WRONG
                'pruned_1.0-1_amd64.deb': 'a' * 64,               # absent
            },
            'output_count': 3,
        })
        _u.write_build_record(_log, _rec)

        _stats = _u.verify_output_hashes(_log, _repo)
        assert _stats['scanned'] == 3, _stats
        assert _stats['mismatched'] == [('src', 'drift_1.0-1_amd64.deb')], (
            f"only the present-but-wrong file should be flagged: {_stats}")
        assert ('src', 'pruned_1.0-1_amd64.deb') in _stats['absent'], _stats
        # the matching + absent ones are NOT mismatched
        assert ('src', 'good_1.0-1_amd64.deb') not in _stats['mismatched']
        assert ('src', 'pruned_1.0-1_amd64.deb') not in _stats['mismatched']

        # refresh_output_hashes overwrites the present-but-stale hash from
        # disk (unlike backfill, which skips present hashes), so a following
        # verify_output_hashes reports zero drift — the `repo repair strip`
        # RECORD↔DISK recovery.  The pruned/absent output is left alone.
        _rstats = _u.refresh_output_hashes(_log, _repo)
        assert _rstats['updated'] == 1, _rstats            # only 'src' changed
        assert _rstats['absent'] == 1, _rstats             # the pruned output
        _refreshed = _u.read_build_record(_log, 'src')['output_hashes']
        assert _refreshed['drift_1.0-1_amd64.deb'] == _u.get_sha256(_drift)
        assert _refreshed['good_1.0-1_amd64.deb'] == _u.get_sha256(_good)
        # pruned entry unchanged (file absent, placeholder kept)
        assert _refreshed['pruned_1.0-1_amd64.deb'] == 'a' * 64
        assert _u.verify_output_hashes(_log, _repo)['mismatched'] == []



# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 step 2 — append-only enforcement (additive publish + segregate)
# ─────────────────────────────────────────────────────────────────────────────


def test_docker_wait_for_exit_survives_read_timeouts():
    """A multi-hour build's quiet phase makes container.wait() raise a
    requests Timeout though the container is alive.  _wait_for_exit must
    keep polling (reload + re-wait) and return the real exit code — never
    propagate the timeout as a build failure.  NotFound (reaped) still
    propagates."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import requests.exceptions as _rexc
    import docker
    import buildcontainer

    class _Flaky:
        """wait() times out N times, then the container has exited."""
        short_id = 'abc123'

        def __init__(self, timeouts):
            self._left = timeouts
            self.attrs = {'State': {'Status': 'running', 'Running': True}}

        def wait(self):
            if self._left > 0:
                self._left -= 1
                raise _rexc.ReadTimeout('read timeout=60')
            return {'StatusCode': 0}

        def reload(self):
            if self._left == 0:
                self.attrs = {'State': {'Status': 'exited',
                                        'Running': False, 'ExitCode': 0}}

    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    # 3 timeouts then exited(0) — must return 0, not raise
    assert _bc._wait_for_exit(_Flaky(3)) == {'StatusCode': 0}
    # non-zero exit code is preserved
    _f = _Flaky(1)

    def _reload_fail():
        _f.attrs = {'State': {'Status': 'exited', 'Running': False,
                              'ExitCode': 137}}
    _f.reload = _reload_fail
    assert _bc._wait_for_exit(_f) == {'StatusCode': 137}

    # NotFound (container reaped externally) propagates, never swallowed
    class _Gone:
        short_id = 'x'

        def wait(self):
            raise docker.errors.NotFound('gone')
    _propagated = False
    try:
        _bc._wait_for_exit(_Gone())
    except docker.errors.NotFound:
        _propagated = True
    assert _propagated, "NotFound should propagate, not be swallowed"



def test_docker_wait_for_exit_survives_raw_urllib3_timeout():
    """Regression (firefox-esr -j1, 2026-06-21): docker-py's low-level
    wait()/log-stream can raise the RAW urllib3 variants —
    ReadTimeoutError (subclasses urllib3 TimeoutError, NOT requests.Timeout)
    and ProtocolError (dropped keep-alive) — which the old _DOCKER_TRANSIENT
    (requests-only) missed, so a quiet multi-hour build phase escaped
    keep-polling and force-removed a healthy container.  _wait_for_exit must
    keep-poll on these too and return the real exit code."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import urllib3.exceptions as _u3
    import buildcontainer

    class _Flaky:
        short_id = 'u3'

        def __init__(self, mk, timeouts):
            self._left = timeouts
            self._mk = mk
            self.attrs = {'State': {'Status': 'running', 'Running': True}}

        def wait(self):
            if self._left > 0:
                self._left -= 1
                raise self._mk()
            return {'StatusCode': 0}

        def reload(self):
            if self._left == 0:
                self.attrs = {'State': {'Status': 'exited',
                                        'Running': False, 'ExitCode': 0}}

    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    # raw urllib3 ReadTimeoutError → keep-poll to real exit, never propagate
    assert _bc._wait_for_exit(
        _Flaky(lambda: _u3.ReadTimeoutError(None, '/', 'Read timed out'), 3)
    ) == {'StatusCode': 0}
    # urllib3 ProtocolError (dropped keep-alive mid-stream) → also keep-poll
    assert _bc._wait_for_exit(
        _Flaky(lambda: _u3.ProtocolError('Connection broken'), 2)
    ) == {'StatusCode': 0}



def test_sta32_wait_for_exit_tolerates_transient_reload():
    """STA-32(b): the recovery container.reload() is another HTTP GET — in
    the same daemon hiccup that timed out wait() it can raise a TRANSIENT
    (requests ConnectionError/Timeout), NOT an APIError.  The old guard
    caught only APIError, so that escaped _wait_for_exit and recorded a
    still-running build as failed.  reload() raising a transient must be
    tolerated and the loop must return the real exit code."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import requests.exceptions as _rexc
    import buildcontainer

    class _ReloadHiccup:
        short_id = 'r1'
        # already exited; the loop just needs to read this after reload
        attrs = {'State': {'Status': 'exited', 'Running': False,
                           'ExitCode': 0}}

        def wait(self):
            raise _rexc.Timeout('read timeout=1800')

        def reload(self):
            raise _rexc.ConnectionError('daemon hiccup during reload')

    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    # Must NOT propagate the ConnectionError from reload().
    assert _bc._wait_for_exit(_ReloadHiccup()) == {'StatusCode': 0}



def test_sta32_pid_alive_and_connect_error_tuples():
    """STA-32(a)/(c): _pid_alive probes process existence (signal 0); the
    connect-error tuple includes the BASE DockerException (which an
    unreachable daemon raises) — `except APIError` alone would miss it."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import os as _os
    import docker
    import buildcontainer
    # our own pid is alive; a very high pid almost certainly is not
    assert buildcontainer._pid_alive(_os.getpid()) is True
    assert buildcontainer._pid_alive(2 ** 22 - 1) is False
    # the connect tuple catches the base DockerException + transients
    assert docker.errors.DockerException in buildcontainer._DOCKER_CONNECT_ERRORS
    assert issubclass(docker.errors.APIError, docker.errors.DockerException)
    # the reload tuple tolerates a transient (not just APIError)
    import requests.exceptions as _rexc
    assert _rexc.ConnectionError in buildcontainer._DOCKER_RELOAD_ERRORS



def test_sta32_connect_and_reap_use_widened_handling():
    """STA-32 source pins: both connect paths catch _DOCKER_CONNECT_ERRORS
    (not bare APIError), and the startup orphan reap is PID-aware (skips
    containers a live concurrent session owns)."""
    import inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _init = inspect.getsource(buildcontainer.BuildContainer.__init__)
    # Both connect except clauses widened.
    assert _init.count('except _DOCKER_CONNECT_ERRORS') >= 2, _init
    # Reap consults the owner pid + liveness and skips live-owned.
    assert '_pid_alive(' in _init and 'com.athena.pid' in _init, _init
    assert 'owned by a live concurrent' in _init.lower() or \
           'live concurrent session' in _init.lower(), _init



def test_docker_stream_and_wait_backfills_log_on_timeout():
    """Log streaming is best-effort: a read timeout mid-stream must NOT
    fail the build — _stream_and_wait stops tailing, waits for exit, then
    dumps the complete logs (non-streaming) so nothing is lost."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import requests.exceptions as _rexc
    import buildcontainer

    class _C:
        short_id = 'c1'
        attrs = {'State': {'Status': 'exited', 'Running': False,
                           'ExitCode': 0}}

        def logs(self, stream=False, **_kw):
            if stream:
                def _gen():
                    yield b'building...\n'
                    raise _rexc.ReadTimeout('quiet phase')
                return _gen()
            return b'building...\ndone, full transcript\n'   # backfill

        def reload(self):
            pass

        def wait(self):
            return {'StatusCode': 0}

    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    with tempfile.TemporaryDirectory() as _tmp:
        _log = os.path.join(_tmp, 'pkg')
        _exit = _bc._stream_and_wait(_C(), _log)
        assert _exit == {'StatusCode': 0}, _exit
        # the streamed partial was replaced by the complete backfill dump
        assert 'full transcript' in open(_log).read()



def test_build_source_cleanup_timeout_does_not_mask_success():
    """Regression (BS2 3-way parallel, 2026-06-22): a build that SUCCEEDED
    (phase=done + asg-stamp already written) was tallied as FAILED because
    the `finally` container.remove(force=True) — caught only by
    docker.errors.APIError — let a RAW urllib3 ReadTimeoutError escape.  When
    a heavy container (libreoffice) saturates dockerd, a sibling's
    force-remove blocks past the 1800s client read timeout and surfaces a
    urllib3 ReadTimeout, which is NOT an APIError.  Both the body except and
    the finally-remove except must catch _DOCKER_RELOAD_ERRORS (APIError +
    the raw transient timeouts) so cleanup failure never masks the result."""
    import inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    import requests.exceptions as _rexc
    import urllib3.exceptions as _u3
    # The reload tuple must already include the raw urllib3 ReadTimeoutError
    # (subclasses urllib3.TimeoutError) — that's what escaped the finally.
    assert _u3.TimeoutError in buildcontainer._DOCKER_RELOAD_ERRORS or \
        any(issubclass(_u3.ReadTimeoutError, _t)
            for _t in buildcontainer._DOCKER_RELOAD_ERRORS), \
        buildcontainer._DOCKER_RELOAD_ERRORS
    assert _rexc.ConnectionError in buildcontainer._DOCKER_RELOAD_ERRORS
    # Source-pin: the build() method must NOT catch a bare
    # `docker.errors.APIError` at either the body or the cleanup site —
    # both must be widened to _DOCKER_RELOAD_ERRORS.
    _src = inspect.getsource(buildcontainer.BuildContainer.build)
    assert 'except docker.errors.APIError' not in _src, \
        "build() still narrows an except to bare APIError — a urllib3 " \
        "ReadTimeout would escape and mismark a successful build as failed"
    # both widened catches are present (body + finally remove)
    assert _src.count('except _DOCKER_RELOAD_ERRORS') >= 2, _src
    # the cleanup widening sits right on the force-remove
    assert 'container.remove(force=True)' in _src and \
        'reap_all_live to sweep' in _src, _src



def test_recipe_only_container_skips_local_docker():
    """BuildContainer(connect=False) is a recipe-only container — sets the
    image tag + config-derived attrs but NO local Docker client / image build,
    so `source remotebuild` works on a host with no local build image."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _cfg = mock.Mock(
        dir_repo='/r', dir_source='/s', dir_log='/l', arch='amd64',
        container_release='bookworm', snapshot_baseurl='https://snap',
        build_distribution='Asgard', build_base_id='asgard',
        build_codename='thor', dir_config='/c', dir_patch_source='/p',
        dir_patch_empty='/pe', build_profiles=frozenset(),
        build_options=frozenset(), audit_build_deps=False,
        docker_timeout=1800, mirrors=[])
    with mock.patch('utils.resolve_snapshot_timestamp',
                    return_value='20260602T0Z'), \
            mock.patch.object(buildcontainer.docker, 'from_env') as _fe:
        _bc = buildcontainer.BuildContainer(_cfg, cache=None, connect=False)
    assert _bc.client is None                 # no local daemon connection
    _fe.assert_not_called()                   # never even tried
    assert _bc._image_tag == 'athenalinux:build-bookworm-amd64-20260602T0Z'
    assert _bc.arch == 'amd64' and _bc.codename == 'thor'



def test_segregate_never_deletes_existing_published_deb():
    """An exact-name collision in a published dir KEEPs the existing artifact
    (append-only) and drops the freshly-built dup at repo/ root — never
    overwrites the published file."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _dest_dir = os.path.join(_tmp, 'dists', 'test', 'main', 'binary-amd64')
        os.makedirs(_dest_dir)
        _fn = 'openssl_3.0.15-1+asg1u1_amd64.deb'
        with open(os.path.join(_dest_dir, _fn), 'w') as fh:
            fh.write('PUBLISHED-ORIGINAL')           # already-published artifact
        with open(os.path.join(_tmp, _fn), 'w') as fh:
            fh.write('REBUILT-DUP')                  # fresh dup at repo/ root

        _bc = _make_buildcontainer_stub(repo=_tmp)

        class _FakeConfig:
            def deb_dest_for_filename(self, _f, component='main'):
                return _dest_dir

        class _Src:
            package = 'openssl'

        _bc.config = _FakeConfig()
        # COMP-03 Phase 1: segregate reads from the worker's per-build
        # scratch dir (the rebuilt-dup .deb above), NOT self.repo_path.
        _moved = _bc._segregate_built_artifacts(_Src(), _tmp)

        with open(os.path.join(_dest_dir, _fn)) as fh:
            assert fh.read() == 'PUBLISHED-ORIGINAL', (
                "published deb was overwritten — append-only violated!")
        assert not os.path.exists(os.path.join(_tmp, _fn)), (
            "rebuilt dup should be dropped from repo/ root")
        # The kept-existing destination MUST still appear in _moved so
        # downstream tracking (output_hashes, build record outputs)
        # sees the artifact.  Bug from 2026-06-06: the old code didn't
        # add it, silently losing 497 udebs and any other byte-identical
        # rebuild from build records.
        assert os.path.join(_dest_dir, _fn) in _moved, (
            "kept-existing collision must still be tracked in _moved so "
            "downstream output_hashes / repo audit sees the artifact")



def test_comp03_segregate_signature_takes_source_dir():
    """COMP-03 Phase 1: _segregate_built_artifacts now takes an explicit
    source_dir arg.  Pinned via inspect.signature so a future refactor
    that drops the arg (and silently reverts to reading self.repo_path)
    fails the test instead of silently re-introducing the segregate
    race that would misroute concurrent workers' .debs."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import buildcontainer
    _params = list(inspect.signature(
        buildcontainer.BuildContainer._segregate_built_artifacts
    ).parameters.keys())
    # Leading positionals are pinned (source_dir must stay an explicit
    # arg).  OBS-04 added a trailing optional `events` accumulator; allow
    # additive params after the pinned three.
    assert _params[:3] == ['self', 'src_pkg', 'source_dir'], (
        f"signature regression: expected leading (self, src_pkg, "
        f"source_dir), got {_params}")



def test_buildlog_writes_header_sections_and_files():
    """OBS-04: BuildLog renders a verbose narrative to <pkg>.buildlog."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildlog
    with tempfile.TemporaryDirectory() as _tmp:
        _b = buildlog.BuildLog(_tmp, 'libzstd', kind='build')
        _b.header(status='PASS', arch='amd64')
        _b.section('EMITTED (2)')
        _b.file('libzstd-dev_1.5.4_amd64.deb', size=4096,
                sha256='deadbeef' * 8)
        _b.relocation('zstd_1.5.4_amd64.deb', '/repo/main/binary-amd64')
        _b.footer(status='PASS', files=2)
        _b.write()
        _p = os.path.join(_tmp, 'libzstd.buildlog')
        assert os.path.isfile(_p)
        _txt = open(_p).read()
        assert 'BUILD LOG: libzstd' in _txt
        assert 'EMITTED (2)' in _txt
        assert 'libzstd-dev_1.5.4_amd64.deb' in _txt
        assert 'size=4.0 KB' in _txt
        assert 'zstd_1.5.4_amd64.deb' in _txt and '→' in _txt
        assert 'END libzstd' in _txt



def test_buildlog_write_never_raises_on_unwritable_dir():
    """OBS-04 load-bearing safety invariant: a logging IO failure MUST
    NOT propagate.  The 24-36h repo rebuild cannot be lost to a log write
    error — write() swallows everything."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildlog
    _b = buildlog.BuildLog('/nonexistent/dir/xyz123', 'pkg', kind='build')
    _b.header(status='PASS')
    _b.section('X')
    _b.bullet('y')
    _b.write()   # must not raise — no assertion needed; reaching here passes



def test_buildlog_write_encodes_utf8_under_ascii_locale():
    """Regression (audit #34): BuildLog.write must encode the narrative as
    UTF-8 — it carries '→'/'…' glyphs (relocation/file + buildcontainer
    bullets), so under a C/ASCII locale the default text encoder would raise
    UnicodeEncodeError, the broad except would swallow it, and the .buildlog
    would silently never be written.  Simulate the C locale by making a
    no-encoding text open() default to ASCII; assert the log is still written
    and round-trips the glyph."""
    import sys
    import tempfile
    import builtins
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildlog
    _real_open = builtins.open

    def _ascii_default_open(file, mode='r', *_a, **_kw):
        # A C/ASCII locale: a text open with no explicit encoding uses ASCII
        # (what locale.getpreferredencoding(False) would return).
        if 'b' not in mode and 'encoding' not in _kw:
            _kw['encoding'] = 'ascii'
        return _real_open(file, mode, *_a, **_kw)

    with tempfile.TemporaryDirectory() as _tmp:
        _bl = buildlog.BuildLog(_tmp, 'foo', kind='build')
        _bl.relocation('foo_1.0_amd64.deb', 'dists/thor/main')   # emits '→'
        with mock.patch.object(buildlog, 'open', _ascii_default_open,
                               create=True):
            _bl.write()
        assert os.path.isfile(_bl._path), (
            "buildlog silently not written — UTF-8 encoding lost under the "
            "simulated ASCII locale")
        with _real_open(_bl._path, encoding='utf-8') as _fh:
            _content = _fh.read()
        assert '→' in _content, repr(_content)              # '→' survived



def test_buildlog_methods_tolerate_bad_input_and_helpers():
    """OBS-04 safety: accumulation methods + size helpers never raise on
    odd inputs (None, non-numeric, missing paths)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildlog
    with tempfile.TemporaryDirectory() as _tmp:
        _b = buildlog.BuildLog(_tmp, 'p')
        _b.kv('k', object())
        _b.file('f', size=None)
        _b.file('f', size=-1)
        _b.line(12345)
        _b.empty()
        _b.write()
    assert buildlog.human_size('notanumber') == '?'
    assert buildlog.safe_size('/nope/nope/xyz') == -1
    assert buildlog.human_size(0) == '0 B'
    assert buildlog.human_size(1536) == '1.5 KB'
    assert buildlog.human_size(4096) == '4.0 KB'



def test_segregate_appends_relocate_and_purge_events():
    """OBS-04: when an events list is passed, segregate records a
    ('relocate', file, dst) per move and ('purge', file, reason) per
    dropped duplicate — additive, no change to the move/append-only
    behaviour the other segregate tests pin."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _dest_dir = os.path.join(
            _tmp, 'dists', 'test', 'main', 'binary-amd64')
        os.makedirs(_dest_dir)
        _fresh = 'libzstd1_1.5.4_amd64.deb'       # no collision → relocate
        _dup = 'openssl_3.0.15-1+asg1u1_amd64.deb'  # collision → purge
        with open(os.path.join(_dest_dir, _dup), 'w') as fh:
            fh.write('PUBLISHED')
        for _f in (_fresh, _dup):
            with open(os.path.join(_tmp, _f), 'w') as fh:
                fh.write('X')
        _bc = _make_buildcontainer_stub(repo=_tmp)

        class _FakeConfig:
            def deb_dest_for_filename(self, _f, component='main'):
                return _dest_dir

        class _Src:
            package = 'mix'

        _bc.config = _FakeConfig()
        _events: list = []
        _bc._segregate_built_artifacts(_Src(), _tmp, events=_events)
        _kinds = {e[0] for e in _events}
        assert 'relocate' in _kinds, _events
        assert 'purge' in _kinds, _events
        assert any(e[0] == 'relocate' and e[1] == _fresh for e in _events)
        assert any(e[0] == 'purge' and e[1] == _dup for e in _events)



def test_comp03_segregate_does_not_read_self_repo_path():
    """COMP-03 Phase 1 invariant: _segregate_built_artifacts must
    read its files from the source_dir parameter, NOT
    self.repo_path.  Source-grep so a future refactor can't silently
    regress to the parallel-mode segregate race."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import buildcontainer
    _src = inspect.getsource(
        buildcontainer.BuildContainer._segregate_built_artifacts)
    # Reading self.repo_path inside segregate is the regression.  Match
    # specific read shapes (listdir, path.join) so a stray docstring
    # reference doesn't false-positive.
    _bad = (
        'os.listdir(self.repo_path)',
        'os.path.join(self.repo_path,',
        'os.path.isfile(os.path.join(self.repo_path,',
    )
    for _b in _bad:
        assert _b not in _src, (
            f"COMP-03 Phase 1: segregate must not read from "
            f"self.repo_path (found `{_b}`) — concurrent workers' "
            f".debs would be misclassified.  Use the source_dir "
            f"argument instead.")



def test_comp03_segregate_cross_source_isolation_via_scratch_dir():
    """COMP-03 Phase 1 behaviour: segregate ignores foreign .debs sitting
    in self.repo_path and only acts on the source_dir argument.  Simulates
    worker A finishing while worker B's .debs sit unmoved in the shared
    repo root (under the old code, A would mis-classify B's binaries)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _repo = os.path.join(_tmp, 'repo')
        _scratch_a = os.path.join(_tmp, 'stage', 'worker-a')
        _dest_dir = os.path.join(_repo, 'dists', 'thor', 'main', 'binary-amd64')
        os.makedirs(_repo)
        os.makedirs(_scratch_a)
        os.makedirs(_dest_dir)
        # Worker A's just-built file lives in its scratch dir.
        with open(os.path.join(_scratch_a, 'foo_1.0_amd64.deb'), 'w') as fh:
            fh.write('A')
        # Worker B's just-built file sits unmoved in the shared repo root —
        # under the OLD os.listdir(self.repo_path) code, A would pick this up.
        with open(os.path.join(_repo, 'bar_2.0_amd64.deb'), 'w') as fh:
            fh.write('B')

        _bc = _make_buildcontainer_stub(repo=_repo)

        class _FakeConfig:
            def deb_dest_for_filename(self, _f, component='main'):
                return _dest_dir

        class _SrcA:
            package = 'foo'

        _bc.config = _FakeConfig()
        _moved = _bc._segregate_built_artifacts(_SrcA(), _scratch_a)
        # A's file is segregated.
        assert _moved == [os.path.join(_dest_dir, 'foo_1.0_amd64.deb')]
        assert os.path.exists(os.path.join(_dest_dir, 'foo_1.0_amd64.deb'))
        # B's file in the shared repo root is UNTOUCHED — worker A must
        # not have touched it (the race that would silently misroute it
        # to A's component dir is precisely what Phase 1 eliminates).
        assert os.path.exists(os.path.join(_repo, 'bar_2.0_amd64.deb'))
        with open(os.path.join(_repo, 'bar_2.0_amd64.deb')) as fh:
            assert fh.read() == 'B'



def test_comp03_segregate_rolls_back_on_move_failure():
    """COMP-03 Phase 1 all-or-nothing: if any move fails mid-iteration,
    prior successful moves are reversed and the function returns []
    so the caller can't observe a half-populated set.  Simulates by
    using a deb_dest_for_filename that points the second file at a
    non-existent / unwritable parent dir."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _scratch = os.path.join(_tmp, 'stage')
        _good_dest = os.path.join(_tmp, 'dists', 'thor', 'main', 'binary-amd64')
        os.makedirs(_scratch)
        os.makedirs(_good_dest)
        with open(os.path.join(_scratch, 'first_1.0_amd64.deb'), 'w') as fh:
            fh.write('FIRST')
        with open(os.path.join(_scratch, 'second_1.0_amd64.deb'), 'w') as fh:
            fh.write('SECOND')

        _bc = _make_buildcontainer_stub(repo=_tmp)

        # makedirs is best-effort idempotent for normal paths; to FORCE
        # an OSError during the move, we make the destination dir
        # read-only AFTER the first successful rename — using a tiny
        # FakeConfig that flips a flag after the first call.
        _calls = {'n': 0}

        class _FakeConfig:
            def deb_dest_for_filename(self, _f, component='main'):
                _calls['n'] += 1
                if _calls['n'] >= 2:
                    # second file points at a file (not a dir) → makedirs
                    # raises FileExistsError that isn't suppressed by
                    # exist_ok=True (it suppresses only existing
                    # directories), so segregate's makedirs raises OSError.
                    return os.path.join(_tmp, 'first_1.0_amd64.deb')
                return _good_dest

        class _Src:
            package = 'multi'

        _bc.config = _FakeConfig()
        # Pre-create the file the second call's dest would shadow.
        with open(os.path.join(_tmp, 'first_1.0_amd64.deb'), 'w') as fh:
            fh.write('SHADOW')

        _moved = _bc._segregate_built_artifacts(_Src(), _scratch)
        # Rollback returns empty list (no caller may observe partial set).
        assert _moved == [], _moved
        # The first file was moved into _good_dest then rolled back into
        # _scratch.  Either way it must NOT remain in _good_dest.
        assert not os.path.exists(
            os.path.join(_good_dest, 'first_1.0_amd64.deb')), (
            "rollback failed: first file still in dest after rollback")
        assert os.path.exists(os.path.join(_scratch, 'first_1.0_amd64.deb'))



def test_segregate_rollback_never_destroys_kept_existing_published_deb():
    """Regression (audit #7, buildcontainer:2015-2045): on a multi-binary
    rebuild where one binary is a byte-identical (kept-existing) collision
    and a LATER sibling's move fails, the rollback must NOT rename the
    already-published kept-existing .deb out of the repo.  The old code
    appended the kept-existing path to _moved_paths, so the rollback loop
    renamed it into the scratch dir — which build()'s finally then rmtrees,
    silently destroying a previously-published artifact.  Kept-existing files
    are now tracked separately (_kept_existing) and excluded from rollback."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    from unittest import mock
    with tempfile.TemporaryDirectory() as _tmp:
        _scratch = os.path.join(_tmp, 'stage')
        _dest = os.path.join(_tmp, 'dists', 'thor', 'main', 'binary-amd64')
        os.makedirs(_scratch)
        os.makedirs(_dest)
        # binary A: collides with an already-published artifact (kept-existing)
        _kept = 'liba_1.0_amd64.deb'
        with open(os.path.join(_dest, _kept), 'w') as fh:
            fh.write('PUBLISHED-A')           # already on disk in a published dir
        with open(os.path.join(_scratch, _kept), 'w') as fh:
            fh.write('REBUILT-A-dup')         # byte-identical rebuild dup
        # binary B: its move fails (dest is a file → makedirs raises OSError)
        _boom = 'libb_1.0_amd64.deb'
        with open(os.path.join(_scratch, _boom), 'w') as fh:
            fh.write('REBUILT-B')
        _boom_blocker = os.path.join(_tmp, 'blocker')
        with open(_boom_blocker, 'w') as fh:
            fh.write('x')                      # a FILE → makedirs() on it raises

        _bc = _make_buildcontainer_stub(repo=_tmp)

        class _FakeConfig:
            def deb_dest_for_filename(self, _f, component='main'):
                if _f == _boom:
                    return _boom_blocker        # makedirs raises → rollback
                return _dest

        class _Src:
            package = 'multi'

        _bc.config = _FakeConfig()
        # Force the order that triggers the bug: kept-existing collision FIRST
        # (so its dest is recorded), then the failing sibling.  segregate calls
        # os.listdir(source_dir) exactly once.
        with mock.patch.object(buildcontainer.os, 'listdir',
                               return_value=[_kept, _boom]):
            _moved = _bc._segregate_built_artifacts(_Src(), _scratch)

        # All-or-nothing: the failed source returns [].
        assert _moved == [], _moved
        # THE bug: the already-published kept-existing file must survive intact.
        _kept_path = os.path.join(_dest, _kept)
        assert os.path.exists(_kept_path), (
            "rollback destroyed the already-published kept-existing .deb!")
        with open(_kept_path) as fh:
            assert fh.read() == 'PUBLISHED-A', (
                "kept-existing published .deb was modified during rollback")



def test_comp03_build_uses_per_worker_scratch_dir_in_volume_bind():
    """COMP-03 Phase 1 AST contract: BuildContainer.build() must:
      1. Compute a per-build _scratch_dir under config.dir_build_stage
         using uuid (so concurrent workers never collide).
      2. Bind _scratch_dir → /repo in containers.run() (NOT self.repo_path).
      3. rmtree _scratch_dir in the finally block.

    All three are silent-regression vectors: dropping any of them
    silently re-introduces the parallel-mode race or leaks scratch
    dirs across runs."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    import re as _re
    _m = _re.search(r'def build\(self, src_pkg.*?(?=\n    @staticmethod|\n    def )',
                    _src, _re.DOTALL)
    assert _m, "BuildContainer.build not found"
    _body = _m.group(0)
    # 1. uuid-derived per-build scratch dir under dir_build_stage.
    assert _re.search(
        r'_scratch_dir\s*=\s*os\.path\.join\(\s*self\.config\.dir_build_stage,\s*uuid\.uuid4\(\)',
        _body), (
        "COMP-03 Phase 1: build() must allocate a uuid-named scratch "
        "dir under config.dir_build_stage")
    # 2. /repo bind points at the scratch dir.
    assert _re.search(
        r"_scratch_dir:\s*\{\s*'bind':\s*'/repo'", _body), (
        "COMP-03 Phase 1: the /repo bind mount must use _scratch_dir, "
        "not self.repo_path — regression to the parallel-mode race")
    assert "self.repo_path:   {'bind': '/repo'" not in _body, (
        "regression: the old shared-repo bind is back")
    # 3. rmtree in finally.
    assert 'shutil.rmtree(_scratch_dir' in _body, (
        "COMP-03 Phase 1: build()'s finally block must rmtree the "
        "per-worker scratch dir — leaks pile up otherwise")



def test_comp03_buildconfig_creates_and_validates_dir_build_stage():
    """COMP-03 Phase 1: dir_build_stage must be auto-created and
    writability-checked at config load (mirrors the dir-ensure
    pattern for every other build-tree directory)."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, cfg.error_str
        # Attribute exists, sits under dir_temp, and the dir is real + writable.
        assert hasattr(cfg, 'dir_build_stage')
        assert cfg.dir_build_stage == os.path.join(cfg.dir_temp, 'build-stage')
        assert os.path.isdir(cfg.dir_build_stage)
        assert os.access(cfg.dir_build_stage, os.W_OK)



def test_comp03_register_live_adds_under_lock_and_is_idempotent():
    """COMP-03 Phase 2: _register_live adds the container to the
    registry under self._live_lock; re-registering the same short_id
    is a no-op (idempotent — pinned by overwriting the same key)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc._live = {}
    _bc._live_lock = threading.Lock()

    class _FakeContainer:
        def __init__(self, sid):
            self.short_id = sid

    _c1 = _FakeContainer('abc123')
    _c2 = _FakeContainer('def456')
    _bc._register_live(_c1)
    _bc._register_live(_c2)
    assert set(_bc._live.keys()) == {'abc123', 'def456'}
    # Idempotent — same short_id re-registered is a no-op
    _bc._register_live(_c1)
    assert len(_bc._live) == 2



def test_comp03_deregister_live_removes_and_tolerates_missing_key():
    """COMP-03 Phase 2: _deregister_live drops the container from
    the registry; a missing key is fine (Phase 3's reap_all_live
    may have removed it concurrently)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc._live = {}
    _bc._live_lock = threading.Lock()

    class _FakeContainer:
        def __init__(self, sid):
            self.short_id = sid

    _c = _FakeContainer('abc123')
    _bc._register_live(_c)
    assert 'abc123' in _bc._live
    _bc._deregister_live(_c)
    assert 'abc123' not in _bc._live
    # Re-deregistering is fine (no KeyError) — race-tolerance contract
    _bc._deregister_live(_c)
    assert _bc._live == {}



def test_comp03_all_containers_run_sites_carry_athena_label():
    """COMP-03 Phase 2 source-grep: every `self.client.containers.run(`
    invocation in buildcontainer.py must pass `labels=self._container_labels`
    so the startup orphan reap (and the `docker ps --filter` operator
    workflow) can identify our containers reliably."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    import re as _re
    # Find every containers.run(...) invocation (multi-line) and assert
    # labels= appears inside the call's argument list.
    _calls = list(_re.finditer(
        r'self\.client\.containers\.run\((.*?)\)\s*(?:\n\s*self\._register_live|\n\s*logger|\n\s*_buf|\n\s*\w+\s*=|\Z)',
        _src, _re.DOTALL))
    assert len(_calls) >= 3, (
        f"expected >=3 containers.run sites (build / SEC-05 preview / "
        f"grub-mkrescue); found {len(_calls)}")
    for _m in _calls:
        _args = _m.group(1)
        assert 'labels=self._container_labels' in _args, (
            f"containers.run site missing `labels=self._container_labels`:\n"
            f"{_args[:300]}")



def test_all_containers_run_sites_carry_user_env():
    """Source-grep: every `self.client.containers.run(` invocation in
    buildcontainer.py must pass `environment=_CONTAINER_ENV` — docker does
    not populate USER/LOGNAME from the image's `USER athena`, and package
    test suites read them (curl's runtests.pl dies under fatal-warnings on
    undefined $USER instead of skipping its ssh tests; a real buildd always
    has both set)."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    import re as _re
    _calls = list(_re.finditer(
        r'self\.client\.containers\.run\((.*?)\)\s*(?:\n\s*self\._register_live|\n\s*logger|\n\s*_buf|\n\s*\w+\s*=|\Z)',
        _src, _re.DOTALL))
    assert len(_calls) >= 3, (
        f"expected >=3 containers.run sites; found {len(_calls)}")
    for _m in _calls:
        assert 'environment=_CONTAINER_ENV' in _m.group(1), (
            f"containers.run site missing `environment=_CONTAINER_ENV`:\n"
            f"{_m.group(1)[:300]}")
        # buildd parity: no seccomp filter — testsuites assert KERNEL error
        # semantics (glibc tst-personality EPERM-vs-EINVAL, keyutils keyctl)
        assert 'security_opt=_CONTAINER_SECURITY_OPT' in _m.group(1), (
            f"containers.run site missing seccomp unconfine:\n"
            f"{_m.group(1)[:300]}")
    assert "_CONTAINER_ENV = {'USER': 'athena', 'LOGNAME': 'athena'}" in _src
    assert ("_CONTAINER_SECURITY_OPT = "
            "['seccomp=unconfined', 'apparmor=unconfined']") in _src, (
        "both LSM opt-outs required — seccomp for kernel errno semantics, "
        "apparmor for test-container mount() (glibc, 2026-07-08)")


def test_comp03_register_live_called_after_each_containers_run():
    """COMP-03 Phase 2 source-grep: every `containers.run` immediately
    feeds the result into `_register_live(...)` so SIGINT-driven reap
    can find every owned container."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    # Three pairings expected; each `containers.run(...)` block should
    # be followed within a few lines by `self._register_live(container)`.
    _runs = _src.count('self.client.containers.run(')
    _regs = _src.count('self._register_live(container)')
    assert _regs >= _runs, (
        f"expected >= {_runs} _register_live calls (one per containers.run "
        f"site); found {_regs}")



def test_comp03_deregister_live_called_in_every_finally():
    """COMP-03 Phase 2: every container-owning finally block deregisters
    via `self._deregister_live(container)` — without it, a stale entry
    in self._live would survive a successful build and reap_all_live
    would issue a doomed remove(force=True) against a non-existent
    container next time it fires."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    _runs = _src.count('self.client.containers.run(')
    _deregs = _src.count('self._deregister_live(container)')
    assert _deregs >= _runs, (
        f"expected >= {_runs} _deregister_live calls (one per finally "
        f"block); found {_deregs}")



def test_comp03_startup_orphan_reap_filters_by_athena_label():
    """COMP-03 Phase 2: __init__'s orphan-reap must pass
    filters={'label': 'com.athena.build=1'} to containers.list so it
    NEVER touches unrelated containers running on the host."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    import re as _re
    assert _re.search(
        r"containers\.list\(\s*all=True\s*,\s*filters=\{\s*'label'\s*:\s*'com\.athena\.build=1'\s*\}",
        _src), (
        "COMP-03 Phase 2: orphan-reap must filter by "
        "label=com.athena.build=1 — without that filter, it would "
        "force-remove unrelated containers on the host")



def test_comp03_container_labels_include_pid_for_disambiguation():
    """COMP-03 Phase 2: the labels dict carries both com.athena.build
    AND com.athena.pid so operators inspecting via `docker ps` can
    tell apart containers from concurrent athena-build processes."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    import re as _re
    _m = _re.search(
        r"self\._container_labels\s*=\s*\{(.*?)\}",
        _src, _re.DOTALL)
    assert _m, "self._container_labels initialisation not found"
    _body = _m.group(1)
    assert "'com.athena.build'" in _body and "'1'" in _body
    assert "'com.athena.pid'" in _body and 'os.getpid()' in _body



def test_comp03_reap_all_live_force_removes_every_registered_container():
    """COMP-03 Phase 3: reap_all_live() iterates a snapshot of the
    registry and force-removes each container.  Returns the count
    reaped — caller logs it."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc._live = {}
    _bc._live_lock = threading.Lock()
    _bc.shutdown_event = threading.Event()

    class _FakeContainer:
        def __init__(self, sid):
            self.short_id = sid
            self.remove_called_with = None

        def remove(self, force=False):
            self.remove_called_with = force

    _c1 = _FakeContainer('aaa')
    _c2 = _FakeContainer('bbb')
    _c3 = _FakeContainer('ccc')
    _bc._register_live(_c1)
    _bc._register_live(_c2)
    _bc._register_live(_c3)
    _n = _bc.reap_all_live()
    assert _n == 3, _n
    for _c in (_c1, _c2, _c3):
        assert _c.remove_called_with is True, (
            "every reaped container must be removed with force=True so "
            "still-running containers actually die")



def test_comp03_reap_all_live_is_idempotent_and_tolerates_api_error():
    """COMP-03 Phase 3: reap_all_live() called twice is fine; a
    container raising docker.errors.APIError on remove is logged
    and skipped, not propagated (race with the worker's own finally
    block having already cleaned up)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    import docker as _dk
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc._live = {}
    _bc._live_lock = threading.Lock()
    _bc.shutdown_event = threading.Event()

    class _RaisingContainer:
        short_id = 'raise'

        def remove(self, force=False):
            raise _dk.errors.APIError("simulated race")

    _bc._register_live(_RaisingContainer())
    # Should not raise; counts only successful removes.
    _n = _bc.reap_all_live()
    assert _n == 0, _n
    # Idempotent — second call against an empty registry returns 0.
    # (We left the raising container in the registry intentionally;
    # the iteration uses a snapshot so it does NOT auto-deregister.
    # In production, the worker's own finally will deregister.)
    _bc._live.clear()
    assert _bc.reap_all_live() == 0



def test_comp03_request_shutdown_sets_event_and_reaps():
    """COMP-03 Phase 3: request_shutdown() flips shutdown_event AND
    calls reap_all_live() in one step — workers between jobs see the
    event flip, workers inside container.wait/logs unblock because
    their containers were just force-removed."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc._live = {}
    _bc._live_lock = threading.Lock()
    _bc.shutdown_event = threading.Event()

    class _FakeContainer:
        short_id = 'sid'

        def remove(self, force=False):
            return None

    _bc._register_live(_FakeContainer())
    assert not _bc.shutdown_event.is_set()
    _n = _bc.request_shutdown()
    assert _bc.shutdown_event.is_set(), (
        "request_shutdown must flip the shutdown_event so workers "
        "checking between jobs bail out")
    assert _n == 1, _n



def test_comp03_shutdown_event_initialised_in_init():
    """COMP-03 Phase 3: BuildContainer.__init__ initialises
    self.shutdown_event as a threading.Event so workers can consult
    it without worrying about attribute existence."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    assert 'self.shutdown_event = threading.Event()' in _src, (
        "COMP-03 Phase 3: BuildContainer.__init__ must initialise "
        "self.shutdown_event = threading.Event()")



def test_comp03_reap_all_live_uses_snapshot_not_live_iteration():
    """COMP-03 Phase 3 safety: reap_all_live() must iterate a SNAPSHOT
    taken under _live_lock, not the live dict — otherwise a concurrent
    _deregister_live() during iteration would raise
    `dictionary changed size during iteration`.  Pinned via source-grep."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    import re as _re
    _m = _re.search(
        r'def reap_all_live\(self\).*?(?=\n    def )',
        _src, _re.DOTALL)
    assert _m, "reap_all_live not found"
    _body = _m.group(0)
    # The lock-acquire must take a snapshot (list of values) before
    # the iteration loop, not call .remove inside the with-block.
    assert _re.search(
        r"with self\._live_lock:\s*\n\s+_snapshot\s*=\s*list\(self\._live\.values\(\)\)",
        _body), (
        "reap_all_live must snapshot self._live.values() under the lock, "
        "then iterate the snapshot outside the lock — iterating the live "
        "dict while another thread deregisters would raise")



def test_comp03_phase4_parallel_path_uses_thread_pool_executor():
    """COMP-03 Phase 4: when MaxParallelBuilds > 1, cmd_source_build's
    build sub-phase uses ThreadPoolExecutor.  Pinned via source-grep
    so a future refactor that switches to multiprocessing or asyncio
    (each carry distinct subtle bugs around Docker SDK handle sharing)
    must update the test deliberately."""
    _src = _session_source()
    import re as _re
    _m = _re.search(
        r'def cmd_source_build\(self.*?(?=\n    def )',
        _src, _re.DOTALL)
    assert _m, "cmd_source_build not found"
    _body = _m.group(0)
    assert 'concurrent.futures' in _body, (
        "Phase 4 parallel path must use concurrent.futures")
    assert 'ThreadPoolExecutor' in _body
    assert 'max_workers=_n_parallel' in _body, (
        "executor must size max_workers from MaxParallelBuilds")
    # Phase 4 used as_completed; Phase 6 switched to cf.wait(FIRST_COMPLETED)
    # to drive the heavy-package drain-then-submit scheduler — accept either.
    assert ('as_completed' in _body
            or 'FIRST_COMPLETED' in _body), (
        "Phase 4: results must be consumed via concurrent.futures "
        "(as_completed or wait/FIRST_COMPLETED) for parallel "
        "accounting in the main thread")



def test_comp03_phase4_serial_path_kept_for_max_parallel_builds_one():
    """COMP-03 Phase 4 backward-compat: MaxParallelBuilds == 1 must
    take a serial for-loop path (no executor, no scoped signal
    handler), so the existing serial behaviour is preserved verbatim
    as the debugging mode + zero-risk fallback."""
    _src = _session_source()
    import re as _re
    _m = _re.search(
        r'def cmd_source_build\(self.*?(?=\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    # Branch guard on max_parallel_builds <= 1 must exist.
    assert 'if _n_parallel <= 1:' in _body, (
        "Phase 4: MaxParallelBuilds <= 1 must short-circuit to the "
        "serial path (zero executor overhead, easy debugging)")



def test_comp03_phase4_installs_scoped_sigint_handler_around_executor():
    """COMP-03 Phase 4: the SIGINT handler is installed JUST around
    the executor block (not globally) so REPL Ctrl+C semantics
    elsewhere are unaffected; restored in a finally."""
    _src = _session_source()
    import re as _re
    _m = _re.search(
        r'def cmd_source_build\(self.*?(?=\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    assert _re.search(
        r'_old_sigint\s*=\s*_signal\.signal\(_signal\.SIGINT', _body), (
        "Phase 4: must save the previous SIGINT handler before installing "
        "the scoped one")
    assert '_signal.signal(_signal.SIGINT, _old_sigint)' in _body, (
        "Phase 4: must restore the previous SIGINT handler in finally")
    assert 'self.container.request_shutdown()' in _body, (
        "Phase 4: SIGINT handler must call request_shutdown() so reap "
        "unblocks workers (Phase 3 contract)")
    # Anti-regression: signal.signal() raises ValueError when called off
    # the main thread.  In TUI mode cmd_source_build runs on the shell
    # worker thread, so the install MUST be guarded by try/except
    # ValueError and the restore MUST be gated on whether the install
    # succeeded (else NameError on _old_sigint).
    assert 'except ValueError' in _body, (
        "Phase 4: scoped SIGINT install must be wrapped in try/except "
        "ValueError — signal.signal() can only be called from the main "
        "thread; off-main-thread (TUI shell worker) raises ValueError "
        "and crashes the build")
    assert 'if _old_sigint is not None' in _body, (
        "Phase 4: restore must be gated on whether the install succeeded "
        "so an off-main-thread skip path doesn't NameError on _old_sigint")



def test_comp03_phase4_executor_shutdown_uses_wait_true_and_cancel_futures():
    """COMP-03 Phase 4: executor.shutdown(wait=True, cancel_futures=True)
    on the way out — wait=True so workers drain their finally blocks
    (container reap, scratch-dir rmtree); cancel_futures=True so queued
    builds don't run after shutdown."""
    _src = _session_source()
    import re as _re
    _m = _re.search(
        r'def cmd_source_build\(self.*?(?=\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    assert _re.search(
        r'_executor\.shutdown\(\s*wait=True,\s*cancel_futures=True\s*\)',
        _body), (
        "Phase 4: executor.shutdown must use wait=True AND "
        "cancel_futures=True — wait=False races worker cleanup; missing "
        "cancel_futures lets queued builds run after shutdown")



def test_comp03_phase4_pool_breaks_on_shutdown_event():
    """COMP-03 Phase 4: the as_completed loop checks
    self.container.shutdown_event between completions, breaking out
    once SIGINT-driven request_shutdown has fired so we don't tally
    more results than we'll actually wait for."""
    _src = _session_source()
    import re as _re
    _m = _re.search(
        r'def cmd_source_build\(self.*?(?=\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    assert 'self.container.shutdown_event.is_set()' in _body, (
        "Phase 4: as_completed loop must check shutdown_event for "
        "early-break on SIGINT")



def test_comp03_phase4_tunnel_sub_phase_runs_serial_before_parallel():
    """COMP-03 Phase 4: tunneled sources (network-bound, dest-dir
    locked) run as a SERIAL pre-phase, before the parallel build
    pool.  Avoids parallel tunneling racing on rsync into the same
    component dir + simplifies progress reporting."""
    _src = _session_source()
    import re as _re
    _m = _re.search(
        r'def cmd_source_build\(self.*?(?=\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    # _tunnel_pkgs partition exists BEFORE the parallel branch.
    _tunnel_idx = _body.find('_tunnel_pkgs')
    _parallel_idx = _body.find('ThreadPoolExecutor')
    assert _tunnel_idx > 0, "tunnel partition missing"
    assert _parallel_idx > _tunnel_idx, (
        "Phase 4: tunneled sub-phase must run BEFORE the parallel "
        "ThreadPoolExecutor block")



def test_comp03_phase5_resource_kwargs_empty_when_uncapped():
    """COMP-03 Phase 5: _resource_kwargs() returns {} when both
    BuildCpus and BuildMemory are at their defaults (0.0 / '').
    Preserves current uncapped behaviour for operators who haven't
    set the knobs."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)

    class _FakeConfig:
        build_cpus = 0.0
        build_memory = ''
    _bc.config = _FakeConfig()
    assert _bc._resource_kwargs() == {}



def test_comp03_phase5_resource_kwargs_translates_build_cpus_to_nano_cpus():
    """COMP-03 Phase 5: BuildCpus=3.5 -> nano_cpus=3_500_000_000
    (docker's billionth-of-a-CPU quota).  Pinned because the unit
    confusion (cpus vs nano_cpus) is a classic foot-gun."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)

    class _FakeConfig:
        build_cpus = 3.5
        build_memory = ''
    _bc.config = _FakeConfig()
    _kw = _bc._resource_kwargs()
    assert _kw == {'nano_cpus': 3_500_000_000}, _kw



def test_comp03_phase5_resource_kwargs_passes_mem_limit_string_through():
    """COMP-03 Phase 5: BuildMemory='8g' lands as mem_limit='8g' on
    containers.run (docker-py accepts size strings directly)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)

    class _FakeConfig:
        build_cpus = 0.0
        build_memory = '8g'
    _bc.config = _FakeConfig()
    _kw = _bc._resource_kwargs()
    assert _kw == {'mem_limit': '8g'}, _kw



def test_comp03_phase5_build_passes_resource_kwargs_to_containers_run():
    """COMP-03 Phase 5 source-grep: build()'s containers.run() call
    must splat `**self._resource_kwargs()` so cpu/mem caps reach
    docker.  Sentinel test against a future refactor that drops
    the splat or names the helper differently."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    import re as _re
    _m = _re.search(
        r'def build\(self, src_pkg.*?(?=\n    @staticmethod|\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    assert '**self._resource_kwargs()' in _body, (
        "Phase 5: build() must splat **self._resource_kwargs() into "
        "containers.run so BuildCpus / BuildMemory reach docker")



def test_comp03_phase5_exit_137_logs_oomkilled_hint():
    """COMP-03 Phase 5: container exit code 137 (SIGKILL — the
    cgroup OOM-killer's signal) emits an operator-friendly hint
    pointing at BuildMemory.  Pinned via source-grep so a future
    edit can't quietly drop the hint and force operators to dig
    through log/build/<src> to find out what hit them."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    import re as _re
    _m = _re.search(
        r'def build\(self, src_pkg.*?(?=\n    @staticmethod|\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    assert 'if _exit_code == 137:' in _body, (
        "Phase 5: build() must detect exit code 137 post-wait()")
    assert 'OOMKilled' in _body and 'BuildMemory' in _body, (
        "Phase 5: 137 hint must mention OOMKilled + BuildMemory so "
        "the operator can act without grepping logs")



def test_comp03_phase6_scheduler_reads_heavy_packages_from_config():
    """COMP-03 Phase 6: the parallel scheduler reads the heavy-package
    set from self.config.heavy_packages (parsed in Phase 0).  Pinned
    so a future refactor that renames the attribute or hardcodes the
    list breaks the test."""
    _src = _session_source()
    import re as _re
    _m = _re.search(
        r'def cmd_source_build\(self.*?(?=\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    assert 'self.config.heavy_packages' in _body, (
        "Phase 6: scheduler must source the heavy set from "
        "self.config.heavy_packages")



def test_comp03_phase6_heavy_in_flight_blocks_new_submissions():
    """COMP-03 Phase 6 contract: while a heavy build is in flight,
    NO new builds start (pause-new-during).  _can_submit_next must
    short-circuit when _heavy_active() returns True."""
    _src = _session_source()
    import re as _re
    _m = _re.search(
        r'def cmd_source_build\(self.*?(?=\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    assert _re.search(
        r'def _can_submit_next.*?if _heavy_active\(\):\s*\n\s*return False',
        _body, _re.DOTALL), (
        "Phase 6: _can_submit_next must short-circuit when "
        "_heavy_active() returns True — otherwise lights get "
        "scheduled alongside a heavy build")



def test_comp03_phase6_heavy_waits_for_drain_before_starting():
    """COMP-03 Phase 6 contract: a heavy build does NOT start while
    any lights are in flight (drain-before-start)."""
    _src = _session_source()
    import re as _re
    _m = _re.search(
        r'def cmd_source_build\(self.*?(?=\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    assert _re.search(
        r'if _nxt\.package in _heavy and _in_flight:\s*\n\s*return False',
        _body), (
        "Phase 6: if the next pending source is heavy AND there is "
        "anything in flight, _can_submit_next must defer it (the "
        "drain-before-start contract)")



def test_comp03_phase6_uses_cf_wait_first_completed_for_drain():
    """COMP-03 Phase 6: the scheduler waits for the first in-flight
    future to complete via concurrent.futures.wait(return_when=
    FIRST_COMPLETED), NOT as_completed (which doesn't compose with
    the staged submit-then-wait scheduler shape)."""
    _src = _session_source()
    import re as _re
    _m = _re.search(
        r'def cmd_source_build\(self.*?(?=\n    def )',
        _src, _re.DOTALL)
    _body = _m.group(0)
    assert 'FIRST_COMPLETED' in _body, (
        "Phase 6: scheduler must wait via cf.wait(return_when="
        "FIRST_COMPLETED) so each completion is observed and the "
        "next submit pass can fire")



def test_comp03_phase6_empty_heavy_set_does_not_change_serial_for_heavy():
    """COMP-03 Phase 6: with HeavyPackages empty (default), the
    _heavy_active() / heavy-waits-for-drain branches are all False,
    so the scheduler degenerates to "submit up to N, wait, submit
    more" — identical to a plain fill-to-N drainer.  Behaviour test
    via direct evaluation of the predicates with an empty frozenset."""
    _heavy: 'frozenset[str]' = frozenset()
    # Simulate the _can_submit_next predicate.  With empty heavy set,
    # the heavy-related branches never fire.
    _in_flight = {'fut1': type('Pkg', (), {'package': 'foo'})()}

    def _heavy_active():
        return any(_p.package in _heavy for _p in _in_flight.values())

    assert _heavy_active() is False, (
        "with empty HeavyPackages, _heavy_active must always be False "
        "and the scheduler degenerates to fill-to-N")



def test_comp03_buildcontainer_init_sweeps_build_stage_survivors():
    """COMP-03 Phase 1 source contract: BuildContainer.__init__ must
    sweep the contents of config.dir_build_stage on startup.  Belt-
    and-braces for kill -9 / OOM / docker-daemon crash paths where
    the build()'s finally-block rmtree didn't run."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _src = fh.read()
    import re as _re
    # Find the __init__ method.
    _m = _re.search(r'def __init__\(self,\s*config:.*?(?=\n    @staticmethod|\n    def )',
                    _src, _re.DOTALL)
    assert _m, "BuildContainer.__init__ not found"
    _body = _m.group(0)
    # Listing under dir_build_stage + shutil.rmtree of each survivor.
    assert 'config.dir_build_stage' in _body, (
        "COMP-03 Phase 1: __init__ must reference config.dir_build_stage "
        "to sweep survivors")
    assert _re.search(r'shutil\.rmtree\(', _body), (
        "COMP-03 Phase 1: __init__ must shutil.rmtree leftover build-stage dirs")



# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 step 3 — build-side stamping + check_build matching + Guard B
# ─────────────────────────────────────────────────────────────────────────────

def test_virtual_arch_gate_dpkg_table_semantics():
    """virtual_build._binary_active_for_arch delegates to dpkg's
    DpkgArchTable.matches_architecture — pins the two delta families the
    thor1 rebuild exposed in the old hand-rolled endswith logic:
      * kfreebsd-amd64 / hurd-amd64 are FOREIGN arches, must NOT match
        target amd64 (glibc libc0.1* over-prediction);
      * 3-component tuples gnu-any-any (match) / musl-any-any (no match)
        must parse (libxcrypt libcrypt1 under-prediction)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    def active(arch_field, target='amd64'):
        # entry shape: "<name> <type> <section> <pri> arch=<field> [profile=…]"
        return _vb._binary_active_for_arch(
            f"foo deb libs optional arch={arch_field}", target)

    # baseline patterns
    assert active('any') and active('all') and active('amd64')
    assert active('amd64,i386') and active('linux-any') and active('any-amd64')
    assert not active('i386') and not active('arm64') and not active('mips')
    # no arch= → always active
    assert _vb._binary_active_for_arch('foo deb libs optional', 'amd64')
    # FAMILY 1 — foreign-kernel amd64 must NOT match (the glibc bug)
    assert not active('kfreebsd-amd64,kfreebsd-i386')
    assert not active('hurd-amd64')
    assert not active('hurd-i386')
    # FAMILY 2 — 3-component abi tuples (the libxcrypt bug)
    assert active('gnu-any-any'), 'gnu-any-any must match a glibc amd64'
    assert not active('musl-any-any'), 'musl-any-any must NOT match glibc amd64'
    # negative lists: built everywhere except excluded
    assert active('!hurd-any')                   # amd64 not excluded
    assert active('!alpha,!hppa,!ia64')
    assert not active('!linux-any')              # amd64 IS linux → excluded
    # the actual glibc + libxcrypt Package-List rows, verbatim
    assert not _vb._binary_active_for_arch(
        'libc0.1 deb libs optional arch=kfreebsd-amd64,kfreebsd-i386 '
        'profile=!stage1', 'amd64')
    assert _vb._binary_active_for_arch(
        'libcrypt1 deb libs optional arch=gnu-any-any protected=yes',
        'amd64')
    assert not _vb._binary_active_for_arch(
        'libcrypt2 deb libs optional arch=musl-any-any protected=yes',
        'amd64')



def test_virtual_fork_package_list_from_dsc():
    """FORK SOURCES ONLY: when source.package_list is empty (the
    locally-generated fork/Sources omits Package-List), the synth reads
    the fork's .dsc so udeb binaries keep their 'udeb' type token —
    athena-installer-data / choose-mirror were predicting .deb before.
    Upstream sources are never touched."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    with tempfile.TemporaryDirectory() as _tmp:
        _dsc = os.path.join(_tmp, 'athena-installer-data_1.3.0.dsc')
        with open(_dsc, 'w') as _fh:
            _fh.write("Format: 3.0 (native)\n"
                      "Source: athena-installer-data\n"
                      "Version: 1.3.0\n"
                      "Package-List:\n"
                      " athena-installer-data udeb debian-installer "
                      "optional arch=all\n"
                      "Checksums-Sha1:\n abc 1 x\n")

        class _ForkMirror:
            id = 'fork'

        class _Src:
            package = 'athena-installer-data'
            version = '1.3.0'
            package_list: list = []          # fork/Sources carries none
            files = {'athena-installer-data_1.3.0.dsc': {}}
            _mirror = _ForkMirror()

        # no fork_dsc_dir → no fallback (legacy behaviour preserved)
        assert _vb._package_list_index(_Src()) == {}
        # with fork_dsc_dir → reads the .dsc, udeb type recovered
        _idx = _vb._package_list_index(_Src(), fork_dsc_dir=_tmp)
        assert 'athena-installer-data' in _idx, _idx
        assert _idx['athena-installer-data'].split()[1] == 'udeb', _idx
        # construct-from-pkg_version path (no .dsc in files) still resolves
        class _SrcNoFiles(_Src):
            files: dict = {}
        assert _vb._package_list_index(
            _SrcNoFiles(), fork_dsc_dir=_tmp)['athena-installer-data'
                                             ].split()[1] == 'udeb'
        # UPSTREAM (non-fork) source is NOT read from the fork dir
        class _UpMirror:
            id = 'main'

        class _Up:
            package = 'zlib'
            version = '1.0'
            package_list: list = []
            files: dict = {}
            _mirror = _UpMirror()
        assert _vb._package_list_index(_Up(), fork_dsc_dir=_tmp) == {}



def test_virtual_build_synthesize_binary_record_inherits_upstream_deps():
    """Synthesized record carries upstream Depends/Conflicts/Provides
    verbatim except for sibling pins, which rewrite to virtual ver."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _upstream = {
        'Package': 'libfoo3', 'Version': '3.0-1', 'Architecture': 'amd64',
        'Depends': 'libc6 (>= 2.36), libfoo-data (= 3.0-1)',
        'Conflicts': 'libfoo2',
        'Provides': 'libfoo-api3',
    }
    _rec = _vb.synthesize_binary_record(
        source_name='libfoo',
        binary_name='libfoo3', virtual_version='3.0-1+asg1u1',
        arch='amd64', upstream_record=_upstream,
        sibling_ver_map={'libfoo3': '3.0-1+asg1u1',
                         'libfoo-data': '3.0-1+asg1u1'},
        sibling_pristine_map={'libfoo3': '3.0-1',
                              'libfoo-data': '3.0-1'},
    )
    # Version + sibling pin rewritten to virtual; external (libc6) kept.
    assert _rec['Version'] == '3.0-1+asg1u1'
    assert 'libc6 (>= 2.36)' in _rec['Depends']
    assert 'libfoo-data (= 3.0-1+asg1u1)' in _rec['Depends']
    assert _rec['Conflicts'] == 'libfoo2'
    assert _rec['Provides'] == 'libfoo-api3'
    # Filename + sha synthesized deterministically.
    assert _rec['Filename'].endswith('libfoo3_3.0-1+asg1u1_amd64.deb')
    assert len(_rec['SHA256']) == 64



def test_virtual_build_synthesize_record_no_upstream_minimal_skeleton():
    """When upstream cache doesn't carry this binary (kernel ABI name,
    fork-only binary), synthesizer falls back to minimal record."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _rec = _vb.synthesize_binary_record(
        source_name='linux',
        binary_name='linux-headers-6.1.0-49-amd64',
        virtual_version='6.1.174-1+asg1u1', arch='amd64',
        upstream_record=None, sibling_ver_map={},
        sibling_pristine_map={},
    )
    assert _rec['Package'] == 'linux-headers-6.1.0-49-amd64'
    assert _rec['Version'] == '6.1.174-1+asg1u1'
    assert _rec['Architecture'] == 'amd64'
    assert _rec['Source'] == 'linux'
    assert 'Depends' not in _rec   # nothing to inherit
    assert _rec['Filename'].startswith('pool/main/')



def test_virtual_build_sibling_pin_only_rewritten_when_pristine_matches():
    """Sibling pin (= V) is rewritten ONLY when V's pristine base equals
    the source's pristine.  An external constraint of the SAME version
    (rare but possible) targeting a non-sibling name stays untouched."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _upstream = {
        'Package': 'a', 'Version': '1.0-1',
        # b is sibling — rewrite.  zzz is not — leave.
        'Depends': 'b (= 1.0-1), zzz (= 1.0-1)',
    }
    _rec = _vb.synthesize_binary_record(
        source_name='libfoo',
        binary_name='a', virtual_version='1.0-1+asg1u1', arch='amd64',
        upstream_record=_upstream,
        sibling_ver_map={'a': '1.0-1+asg1u1', 'b': '1.0-1+asg1u1'},
        sibling_pristine_map={'a': '1.0-1', 'b': '1.0-1'},
    )
    assert 'b (= 1.0-1+asg1u1)' in _rec['Depends']
    assert 'zzz (= 1.0-1)' in _rec['Depends']



def test_virtual_build_sibling_pin_with_nmu_constraint_pristine_matches():
    """Upstream constraint reads `b (= 1.0-1+deb12u1)` — pristine base
    is 1.0-1 (matches source pristine) AND target is sibling → rewrite
    to virtual.  Catches the path where upstream binaries from a security
    snapshot have NMU-stamped intra-source pins."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _upstream = {
        'Package': 'a', 'Version': '1.0-1+deb12u1',
        'Depends': 'b (= 1.0-1+deb12u1)',
    }
    _rec = _vb.synthesize_binary_record(
        source_name='libfoo',
        binary_name='a', virtual_version='1.0-1+asg1u1', arch='amd64',
        upstream_record=_upstream,
        sibling_ver_map={'a': '1.0-1+asg1u1', 'b': '1.0-1+asg1u1'},
        sibling_pristine_map={'a': '1.0-1', 'b': '1.0-1'},
    )
    assert 'b (= 1.0-1+asg1u1)' in _rec['Depends']



def test_virtual_build_synthesize_source_binaries_end_to_end():
    """One Source → list of per-binary records via version math + cache
    lookup.  Uses a stub Source with .package / .version / .binary."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _StubSrc:
        package = 'openssl'
        version = '3.0.15-1+deb12u1'   # NMU-stamped → delta → stamp
        binary = ['openssl', 'libssl3']

    _universe = {
        'openssl': {
            '3.0.15-1+deb12u1': {
                'Package': 'openssl', 'Version': '3.0.15-1+deb12u1',
                'Source': 'openssl',
                'Architecture': 'amd64',
                'Depends': 'libssl3 (= 3.0.15-1+deb12u1), libc6 (>= 2.36)',
            },
        },
        'libssl3': {
            '3.0.15-1+deb12u1': {
                'Package': 'libssl3', 'Version': '3.0.15-1+deb12u1',
                'Source': 'openssl',
                'Architecture': 'amd64',
                'Depends': 'libc6 (>= 2.36)',
            },
        },
    }
    _recs = _vb.synthesize_source_binaries(
        source=_StubSrc(), package_universe=_universe,
        release=1, arch='amd64',
    )
    assert len(_recs) == 2
    _by_name = {_r['Package']: _r for _r in _recs}
    # NMU strip → delta → asg-stamp.  Both binaries at uniform N=1.
    assert _by_name['openssl']['Version'] == '3.0.15-1+asg1u1'
    assert _by_name['libssl3']['Version'] == '3.0.15-1+asg1u1'
    # Sibling pin in openssl's Depends rewritten to virtual libssl3.
    assert 'libssl3 (= 3.0.15-1+asg1u1)' in _by_name['openssl']['Depends']
    # External constraint preserved.
    assert 'libc6 (>= 2.36)' in _by_name['openssl']['Depends']



def test_virtual_build_metapackage_uses_binary_upstream_version():
    """gcc-defaults source @ 1.213 produces gcc/cpp binaries that
    Debian stamps at the COMPILER version (4:12.2.0-3), not the
    source version.  Synthesizer must use the per-binary upstream
    Version as the base; sibling pin rewriting must use per-target
    pristine.  Catches the actual 2026-06-05 closure-break in live
    `virtual build`."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _StubGccDefaults:
        package = 'gcc-defaults'
        version = '1.213'   # source version — NOT what binaries carry
        binary = ['cpp', 'gcc', 'g++']

    _universe = {
        'cpp': {'4:12.2.0-3': {
            'Package': 'cpp', 'Version': '4:12.2.0-3',
            'Source': 'gcc-defaults', 'Architecture': 'amd64',
            'Depends': 'cpp-12 (>= 12.2.0-14~)',
        }},
        'gcc': {'4:12.2.0-3': {
            'Package': 'gcc', 'Version': '4:12.2.0-3',
            'Source': 'gcc-defaults', 'Architecture': 'amd64',
            'Depends': 'cpp (= 4:12.2.0-3), gcc-12 (>= 12.2.0-14~)',
        }},
        'g++': {'4:12.2.0-3': {
            'Package': 'g++', 'Version': '4:12.2.0-3',
            'Source': 'gcc-defaults', 'Architecture': 'amd64',
            'Depends': 'gcc (= 4:12.2.0-3), cpp (= 4:12.2.0-3)',
        }},
    }
    _recs = _vb.synthesize_source_binaries(
        source=_StubGccDefaults(), package_universe=_universe,
        release=1, arch='amd64',
    )
    _by_name = {_r['Package']: _r for _r in _recs}
    # Versions match upstream binary Version (NOT source 1.213).
    assert _by_name['cpp']['Version'] == '4:12.2.0-3'
    assert _by_name['gcc']['Version'] == '4:12.2.0-3'
    assert _by_name['g++']['Version'] == '4:12.2.0-3'
    # Sibling pins rewritten correctly (no asg-stamp because this is
    # a clean pristine build with no ledger, no delta, no patches).
    assert 'cpp (= 4:12.2.0-3)' in _by_name['gcc']['Depends']
    # External (gcc-12) preserved untouched.
    assert 'gcc-12 (>= 12.2.0-14~)' in _by_name['gcc']['Depends']



def test_virtual_build_metapackage_per_binary_transpose():
    """gcc-defaults case under TRANSPOSE: each binary transposes its OWN
    upstream version.  cpp/gcc at 4:12.2.0-3 carry NO trailing +deb, so they
    ship pristine (the ledger is irrelevant — K is intrinsic, no lineage), and
    the sibling pin stays at the (transpose-no-op) sibling version."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _StubGccDefaults:
        package = 'gcc-defaults'
        version = '1.213'
        binary = ['cpp', 'gcc']

    _universe = {
        'cpp': {'4:12.2.0-3': {
            'Package': 'cpp', 'Version': '4:12.2.0-3',
            'Source': 'gcc-defaults',
            'Architecture': 'amd64', 'Depends': '',
        }},
        'gcc': {'4:12.2.0-3': {
            'Package': 'gcc', 'Version': '4:12.2.0-3',
            'Source': 'gcc-defaults',
            'Architecture': 'amd64',
            'Depends': 'cpp (= 4:12.2.0-3)',
        }},
    }
    # A prior +asg in the ledger is now ignored (no lineage trigger).
    _ledger = {
        'cpp': ['12.2.0-3+asg1u1'],
        'gcc': ['12.2.0-3+asg1u1', '12.2.0-3+asg1u2'],
    }
    _recs = _vb.synthesize_source_binaries(
        source=_StubGccDefaults(), package_universe=_universe,
        release=1, arch='amd64',
    )
    _by_name = {_r['Package']: _r for _r in _recs}
    # No trailing +deb on either binary → both ship pristine.
    assert _by_name['cpp']['Version'] == '4:12.2.0-3'
    assert _by_name['gcc']['Version'] == '4:12.2.0-3'
    # gcc's sibling pin stays at the sibling's (pristine) version.
    assert 'cpp (= 4:12.2.0-3)' in _by_name['gcc']['Depends']



def test_virtual_build_transposes_inherited_depends_constraint():
    """Upstream consumer pins to a +deb target version
    (``grub-common (>= 2.06-13+deb12u2)``).  Under TRANSPOSE the synth
    rewrites this to ``(>= 2.06-13+asg1u2)`` — exactly what our rebuilt
    grub-common (2.06-13+asg1u2) ships at — so the floor stays satisfiable
    and the ordinal is preserved (the old strip-to-pristine discarded it)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _StubSrc:
        package = 'grub-efi-amd64-signed'
        version = '1+2.06+13'
        binary = ['grub-efi-amd64-signed']

    _universe = {
        'grub-efi-amd64-signed': {'1+2.06+13': {
            'Package': 'grub-efi-amd64-signed',
            'Version': '1+2.06+13',
            'Source': 'grub-efi-amd64-signed',
            'Architecture': 'amd64',
            'Depends': 'grub-common (>= 2.06-13+deb12u2)',
        }},
    }
    _recs = _vb.synthesize_source_binaries(
        source=_StubSrc(), package_universe=_universe,
        release=1, arch='amd64',
    )
    assert len(_recs) == 1
    # Constraint transposed: now '2.06-13+asg1u2', not '+deb12u2'.
    assert 'grub-common (>= 2.06-13+asg1u2)' in _recs[0]['Depends']
    assert '+deb12u2' not in _recs[0]['Depends']



def test_virtual_build_skips_binaries_from_other_canonical_source():
    """Source A's `Binary:` field declares a binary whose upstream
    Package record carries `Source: B`.  A doesn't actually emit it
    (the linux-signed-amd64 over-declaration pattern).  Synth must
    SKIP this binary when processing A; B's synth will emit it
    canonically.  Pinned to prevent the linux-headers-amd64 closure
    break regressing."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _StubSignedSrc:
        package = 'linux-signed-amd64'
        version = '6.1.174+1'
        binary = ['kernel-image-6.1.0-49-amd64-di',
                  'linux-image-amd64-signed-template']

    _universe = {
        # This udeb's upstream record says it's from `linux`, NOT
        # linux-signed-amd64 — so linux-signed should SKIP it.
        'kernel-image-6.1.0-49-amd64-di': {'6.1.174-1': {
            'Package': 'kernel-image-6.1.0-49-amd64-di',
            'Version': '6.1.174-1', 'Source': 'linux',
            'Architecture': 'amd64',
        }},
        # This one is genuinely from linux-signed-amd64.
        'linux-image-amd64-signed-template': {'6.1.174+1': {
            'Package': 'linux-image-amd64-signed-template',
            'Version': '6.1.174+1', 'Source': 'linux-signed-amd64',
            'Architecture': 'amd64',
        }},
    }
    # peer_sources includes the canonical producer (`linux`) — filter
    # fires because linux IS being synthesized elsewhere in this run.
    _recs = _vb.synthesize_source_binaries(
        source=_StubSignedSrc(), package_universe=_universe,
        release=1, arch='amd64',
        peer_sources={'linux', 'linux-signed-amd64'},
    )
    _names = {_r['Package'] for _r in _recs}
    # Only the genuinely-produced binary made it through.
    assert _names == {'linux-image-amd64-signed-template'}, _names



def test_virtual_build_ambiguous_dedup_emits_warning_and_tracks_name():
    """Two records collide on Package name AND neither carries an
    upstream-canonical Source signal (`_canonical_known` unset).
    Data doesn't tell us which source canonically produces this
    binary.  Dedup picks the higher-version record (no naming
    heuristic) and emits WARNING virtual_dedup_ambiguous so the
    operator knows downstream findings against this name are
    uncertain."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        # Both lack _canonical_known — both upstream-cache misses
        # (kernel ABI floats, fork-only renames).
        {'Package': 'linux-headers-6.1.0-49-amd64',
         'Version': '6.1.174-1+asg1u1', 'Source': 'linux',
         'Architecture': 'amd64',
         'Filename': 'pool/main/l/linux/...', 'SHA256': 'x' * 64,
         'Size': '0'},
        {'Package': 'linux-headers-6.1.0-49-amd64',
         'Version': '6.1.174+1', 'Source': 'linux-signed-amd64',
         'Architecture': 'amd64',
         'Filename': 'pool/main/l/linux-signed-amd64/...',
         'SHA256': 'y' * 64, 'Size': '0'},
    ]
    _state, _f, _ambig = _vb.synthesize_repo_state(_recs)
    # Highest-version dedup; no naming heuristic.
    assert _state.packages['linux-headers-6.1.0-49-amd64']['Version'] == '6.1.174+1'
    # Ambiguity tracked and surfaced.
    assert 'linux-headers-6.1.0-49-amd64' in _ambig
    _amb = [_t for _t in _f if _t[1] == 'virtual_dedup_ambiguous']
    assert len(_amb) == 1
    assert _amb[0][0] == 'WARNING'



def test_virtual_build_unambiguous_dedup_no_ambiguity_warning():
    """When ONE colliding record has _canonical_known (upstream told us
    the producer), dedup is NOT ambiguous — the canonical filter would
    have prevented the collision in the normal synth flow.  No
    ambiguity warning, just the standard duplicate-name INFO."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Source': 'sa',
         '_canonical_known': '1', 'Architecture': 'amd64',
         'Filename': 'pool/a.deb', 'SHA256': 'x' * 64, 'Size': '0'},
        {'Package': 'a', 'Version': '2.0', 'Source': 'sb',
         '_canonical_known': '1', 'Architecture': 'amd64',
         'Filename': 'pool/a2.deb', 'SHA256': 'y' * 64, 'Size': '0'},
    ]
    _state, _f, _ambig = _vb.synthesize_repo_state(_recs)
    assert _ambig == set()
    assert not any(_t[1] == 'virtual_dedup_ambiguous' for _t in _f)



def test_virtual_build_global_pin_rewrite_resolves_cross_source_pin():
    """One source's binary `Depends:` pins a binary from ANOTHER source
    at the target's UPSTREAM version.  After asg-stamping the target,
    the constraint reads stale; the post-dedup global pin rewriter
    must update it to the target's actual Version so closure check
    resolves.

    This is the linux-signed.linux-headers-amd64 →
    linux.linux-headers-6.1.0-49-amd64 (= 6.1.174-1) case.
    """
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'linux-headers-6.1.0-49-amd64',
         'Version': '6.1.174-1+asg1u2',  # stamped
         'Source': 'linux', 'Architecture': 'amd64',
         'Filename': 'pool/main/l/linux/headers-49.deb',
         'SHA256': 'a' * 64, 'Size': '0'},
        {'Package': 'linux-headers-amd64',
         'Version': '6.1.174+1', 'Source': 'linux-signed-amd64',
         'Architecture': 'amd64',
         # Cross-source pin to a binary in DIFFERENT source at
         # pristine version (would fail strict equality without
         # global pin rewriting).
         'Depends': 'linux-headers-6.1.0-49-amd64 (= 6.1.174-1)',
         'Filename': 'pool/main/l/linux-signed-amd64/meta.deb',
         'SHA256': 'b' * 64, 'Size': '0'},
    ]
    _state, _f = _vb.virtual_repo_audit(
        _recs, install_corpus=frozenset({'linux-headers-amd64'}))
    # Pin got rewritten to target's actual stamped version.
    _dep = _state.packages['linux-headers-amd64']['Depends']
    assert '(= 6.1.174-1+asg1u2)' in _dep
    # Closure now resolves.
    _crit = [_t for _t in _f if _t[0] == 'CRITICAL']
    assert _crit == [], _crit



def test_virtual_build_absent_target_is_silent():
    """Target absent from synth state must produce NO finding.
    Cache parse decides scope for the installed system; build-time
    `dh_shlibdeps` runs in the build chroot (upstream Debian today)
    and resolves substvars there, not against our scope.  Virtual
    build cannot statically determine whether an absent target
    will or won't end up in the consumer's actual Depends — so it
    stays silent.  Real `repo audit` post-build reads the on-disk
    Depends and surfaces any genuine closure break."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'consumer', 'Version': '1.0',
         'Architecture': 'amd64', 'Source': 's',
         'Depends': 'libout-of-scope (>= 1.0)',
         'Filename': 'pool/c.deb', 'SHA256': 'x' * 64, 'Size': '0'},
    ]
    _, _f = _vb.virtual_repo_audit(
        _recs, install_corpus=frozenset({'consumer'}))
    # No CRITICAL, no WARNING, no INFO for the absent-target case.
    _audit_kinds = [_t[1] for _t in _f
                    if _t[1] not in (
                        'virtual_binary_list_overlap',
                        'virtual_dedup_ambiguous')]
    assert _audit_kinds == [], _audit_kinds



def test_virtual_build_version_mismatch_stays_critical():
    """Target IS in state but version doesn't match → CRITICAL
    virtual_closure_break (real synth bug, post-asg-stamp math is
    wrong somewhere).  This case is preserved as the only blocking
    closure finding virtual can prove pre-build."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'consumer', 'Version': '1.0',
         'Architecture': 'amd64', 'Source': 'sb',
         'Depends': 'libpresent (= 99.99)',
         'Filename': 'pool/c.deb', 'SHA256': 'x' * 64, 'Size': '0'},
        {'Package': 'libpresent', 'Version': '1.0',
         'Architecture': 'amd64', 'Source': 'sl',
         'Filename': 'pool/l.deb', 'SHA256': 'y' * 64, 'Size': '0'},
    ]
    _, _f = _vb.virtual_repo_audit(
        _recs, install_corpus=frozenset({'consumer'}))
    _crits = [_t[1] for _t in _f if _t[0] == 'CRITICAL']
    assert _crits == ['virtual_closure_break'], _crits



def test_virtual_build_extract_relation_targets_handles_alternatives():
    """`_extract_relation_targets` returns every name in an OR-group
    so out-of-scope classification only fires when ALL are absent.
    `a | b` with `a` present should NOT be classified out_of_scope."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    assert _vb._extract_relation_targets('foo') == ['foo']
    assert _vb._extract_relation_targets('foo (>= 1.0)') == ['foo']
    _alt = _vb._extract_relation_targets('a | b (>= 1.0)')
    assert sorted(_alt) == ['a', 'b']



def test_virtual_build_recommends_suppressed_by_default():
    """Recommends are non-gating per Debian Policy §7.2.  virtual_repo_audit
    must NOT emit virtual_recommends_miss findings unless explicitly
    asked (report_recommends=True).  Operator output is too noisy
    otherwise — every consumer pkg has 0-N Recommends targets we
    don't ship."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Recommends': 'missing-pkg',
         'Filename': 'pool/a.deb', 'SHA256': 'x' * 64, 'Size': '0'},
    ]
    _, _f = _vb.virtual_repo_audit(
        _recs, install_corpus=frozenset({'a'}))
    assert not any(_t[1] == 'virtual_recommends_miss' for _t in _f), _f
    # Explicit opt-in still surfaces them as INFO.
    _, _f2 = _vb.virtual_repo_audit(
        _recs, install_corpus=frozenset({'a'}),
        report_recommends=True)
    _info = [_t for _t in _f2 if _t[1] == 'virtual_recommends_miss']
    assert len(_info) == 1
    assert _info[0][0] == 'INFO'



def test_virtual_build_canonical_filter_off_when_peer_not_in_scope():
    """Fork case: athena-cdrom-setup declares apt-cdrom-setup binary;
    upstream Source: apt-cdrom-setup.  apt-cdrom-setup source is NOT
    in scope (we replaced it with our fork).  Filter must NOT skip —
    our fork IS the producer."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _StubFork:
        package = 'athena-cdrom-setup'
        version = '0.6'
        binary = ['apt-cdrom-setup']

    _universe = {
        'apt-cdrom-setup': {'0.6': {
            'Package': 'apt-cdrom-setup', 'Version': '0.6',
            'Source': 'apt-cdrom-setup',  # upstream canonical
            'Architecture': 'amd64',
        }},
    }
    # peer_sources contains only OUR fork; upstream's apt-cdrom-setup
    # source is absent → filter MUST NOT fire.
    _recs = _vb.synthesize_source_binaries(
        source=_StubFork(), package_universe=_universe,
        release=1, arch='amd64',
        peer_sources={'athena-cdrom-setup'},
    )
    assert len(_recs) == 1
    assert _recs[0]['Package'] == 'apt-cdrom-setup'



def test_virtual_build_synthesize_repo_state_resolves_closed_graph():
    """Two virtual binaries where one depends on the other → closure
    audit silent (no CRITICAL)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Depends': 'b', 'Filename': 'pool/a.deb',
         'SHA256': 'x' * 64, 'Size': '0'},
        {'Package': 'b', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/b.deb',
         'SHA256': 'y' * 64, 'Size': '0'},
    ]
    _state, _f = _vb.virtual_repo_audit(
        _recs, install_corpus=frozenset({'a', 'b'}))
    _crit = [_t for _t in _f if _t[0] == 'CRITICAL']
    assert _crit == []
    assert 'a' in _state.packages
    assert 'b' in _state.packages



def test_virtual_build_absent_target_emits_no_finding():
    """Target absent from synth state produces no finding at all
    (virtual cannot determine outcome pre-build — see
    test_virtual_build_absent_target_is_silent for full
    rationale)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Depends': 'missing-dep', 'Filename': 'pool/a.deb',
         'SHA256': 'x' * 64, 'Size': '0'},
    ]
    _state, _f = _vb.virtual_repo_audit(
        _recs, install_corpus=frozenset({'a'}))
    _kinds = [_t[1] for _t in _f
              if _t[1] not in ('virtual_binary_list_overlap',
                               'virtual_dedup_ambiguous')]
    assert _kinds == [], _kinds



def test_virtual_build_synthesize_repo_state_flags_duplicate_name():
    """Cross-source binary-name overlaps → higher version wins;
    instead of one INFO per overlap (spammy on Debian kernel-signed
    chains where linux + linux-signed-amd64 both declare ~100 same
    -di udebs), emit ONE summary INFO with count + source-pair
    preview."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's1', 'Filename': 'pool/a-1.0.deb',
         'SHA256': 'x' * 64, 'Size': '0'},
        {'Package': 'a', 'Version': '2.0', 'Architecture': 'amd64',
         'Source': 's2', 'Filename': 'pool/a-2.0.deb',
         'SHA256': 'y' * 64, 'Size': '0'},
        {'Package': 'b', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's1', 'Filename': 'pool/b-1.0.deb',
         'SHA256': 'z' * 64, 'Size': '0'},
        {'Package': 'b', 'Version': '2.0', 'Architecture': 'amd64',
         'Source': 's2', 'Filename': 'pool/b-2.0.deb',
         'SHA256': 'w' * 64, 'Size': '0'},
    ]
    _state, _f, _ = _vb.synthesize_repo_state(_recs)
    assert _state.packages['a']['Version'] == '2.0'   # higher wins
    assert _state.packages['b']['Version'] == '2.0'
    _infos = [_t for _t in _f if _t[1] == 'virtual_binary_list_overlap']
    # Exactly ONE summary line, not one per overlap.
    assert len(_infos) == 1
    # Summary mentions count + source pair.
    _msg = _infos[0][2]
    assert '2 binary names' in _msg
    assert 's1+s2' in _msg



def test_virtual_build_no_duplicate_findings_when_no_overlap():
    """Clean case — no duplicate names → no virtual_binary_list_overlap
    finding at all (the summary line is conditional on
    _dup_count > 0)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's1', 'Filename': 'pool/a.deb',
         'SHA256': 'x' * 64, 'Size': '0'},
        {'Package': 'b', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's2', 'Filename': 'pool/b.deb',
         'SHA256': 'y' * 64, 'Size': '0'},
    ]
    _state, _f, _ = _vb.synthesize_repo_state(_recs)
    assert not any(_t[1] == 'virtual_binary_list_overlap' for _t in _f)



def test_virtual_build_invalid_record_skipped_with_critical():
    """Record missing Package or Version → CRITICAL
    virtual_invalid_record, dropped from state."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Version': '1.0', 'Architecture': 'amd64'},  # no Package
        {'Package': 'b', 'Architecture': 'amd64'},    # no Version
        {'Package': 'c', 'Version': '1.0',            # valid
         'Architecture': 'amd64', 'Source': 's',
         'Filename': 'pool/c.deb', 'SHA256': 'z' * 64,
         'Size': '0'},
    ]
    _state, _f, _ = _vb.synthesize_repo_state(_recs)
    _crit = [_t for _t in _f if _t[0] == 'CRITICAL']
    assert len(_crit) == 2
    assert 'c' in _state.packages
    assert 'b' not in _state.packages



def test_virtual_build_live_cohort_conflict_critical():
    """Two binaries in live_cohort, one Conflicts the other → CRITICAL
    virtual_cohort_conflict.  Same conflict pair outside cohort → silent."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Conflicts': 'b', 'Filename': 'pool/a.deb',
         'SHA256': 'x' * 64, 'Size': '0'},
        {'Package': 'b', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/b.deb',
         'SHA256': 'y' * 64, 'Size': '0'},
    ]
    # Both in live cohort → fires.
    _state, _f = _vb.virtual_repo_audit(
        _recs, install_corpus=frozenset({'a', 'b'}),
        live_cohort=frozenset({'a', 'b'}))
    _conf = [_t for _t in _f if _t[1] == 'virtual_cohort_conflict']
    assert len(_conf) >= 1
    # b alone → no conflict (a is not in cohort).
    _state2, _f2 = _vb.virtual_repo_audit(
        _recs, install_corpus=frozenset({'b'}),
        live_cohort=frozenset({'b'}))
    _conf2 = [_t for _t in _f2 if _t[1] == 'virtual_cohort_conflict']
    assert _conf2 == []



def test_virtual_build_synthesize_claim_ledger_one_per_record():
    """One virtual record → one claim under our builder; seq monotonic
    from seq_start; intended==built_version (virtual)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/main/a/s/a_1.0_amd64.deb',
         'SHA256': 'x' * 64, 'Size': '0'},
        {'Package': 'b', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/main/b/s/b_1.0_amd64.deb',
         'SHA256': 'y' * 64, 'Size': '0'},
    ]
    _bb = _vb.synthesize_claim_ledger(
        _recs, our_builder_id='athena-primary',
        snapshot='T1', seq_start=5)
    _claims = _bb['athena-primary']
    assert len(_claims) == 2
    assert _claims[0]['seq'] == 5
    assert _claims[1]['seq'] == 6
    assert _claims[0]['filename'] == 'a_1.0_amd64.deb'
    assert _claims[0]['built_version'] == '1.0'
    assert _claims[0]['claim_state'] == 'published'



def test_virtual_publish_dry_run_silent_against_empty_remote():
    """Empty remote → no ownership conflicts; deterministic synthetic
    SHA-256s prevent hash conflicts even with two binaries from one
    source."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    _recs = [
        {'Package': 'a', 'Version': '1.0', 'Architecture': 'amd64',
         'Source': 's', 'Filename': 'pool/main/a/s/a_1.0_amd64.deb',
         'SHA256': 'x' * 64, 'Size': '0'},
    ]
    _merged, _f = _vb.virtual_publish_dry_run(
        _recs, our_builder_id='athena-primary', snapshot='T1',
        remote_by_builder=None)
    _crit = [_t for _t in _f if _t[0] == 'CRITICAL']
    assert _crit == []
    assert 'athena-primary' in _merged
    assert len(_merged['athena-primary']) == 1



def test_virtual_build_from_cache_collapses_hashtable_shape():
    """`from_cache` flattens cache.package_hashtable's nested
    Package list down to a single control-dict per (name, ver)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _StubPkg(dict):
        def __init__(self, **fields):
            super().__init__()
            for _k, _v in fields.items():
                self[_k] = _v
            self.package = fields['Package']
            self.version = fields['Version']

    class _StubCache:
        package_hashtable = {
            'libfoo': {
                '1.0-1': [_StubPkg(Package='libfoo', Version='1.0-1',
                                   Architecture='amd64',
                                   Depends='libc6')],
            },
        }
    _u = _vb.from_cache(_StubCache())
    assert set(_u.keys()) == {'libfoo'}
    assert set(_u['libfoo'].keys()) == {'1.0-1'}
    _ctrl = _u['libfoo']['1.0-1']
    assert _ctrl['Package'] == 'libfoo'
    assert _ctrl['Depends'] == 'libc6'



def test_virtual_validate_canon_map_uses_filtered_universe():
    """Regression (audit #83): validate's _canon_map must be built from the
    from_cache-FILTERED _universe (standalone Package==name producer), NOT the
    raw package/udeb hashtables.  The raw walk took _rec[0] + the apt-highest
    version, which for a Provides-aliased name (telnet) grabs the epoch-bearing
    alias inetutils-telnet (2:...) over the standalone and misattributes the
    binary's source — diverging from synth, which uses the same filtered
    universe."""
    import inspect
    import re
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    import virtual_build as _vb
    import apt_pkg as _ap
    _ap.init_system()

    # (a) source pin: the canon block must iterate _universe, not the raw
    # hashtables (walking those re-introduces the Provides-alias bug).
    _src = inspect.getsource(BuildSession.cmd_virtual_validate)
    _m = re.search(r'_canon_map[^\n]*=\s*\{\}.*?(?=_tunnel_srcs)',
                   _src, re.DOTALL)
    assert _m, "canon-map construction block not found"
    _block = _m.group(0)
    assert 'for _bn, _vers in _universe' in _block, (
        "canon map must iterate the from_cache-filtered _universe")
    assert ('package_hashtable' not in _block
            and 'udeb_hashtable' not in _block), (
        "canon map must NOT walk the raw hashtables (Provides-alias "
        "misattribution)")

    # (b) behavioral: from_cache drops the Provides-aliased version, so a canon
    # map built from _universe attributes telnet to the STANDALONE source.
    class _Pkg(dict):
        def __init__(self, **_f):
            super().__init__()
            self.update(_f)
            self.package = _f['Package']
            self.version = _f['Version']

    class _Cache:
        package_hashtable = {
            'telnet': {
                # standalone transitional dummy (Package == telnet)
                '0.17+2.4-2': [_Pkg(Package='telnet', Version='0.17+2.4-2',
                                    Architecture='all',
                                    Source='netkit-telnet')],
                # a DIFFERENT binary that Provides: telnet; epoch sorts ABOVE
                '2:2.4-2': [_Pkg(Package='inetutils-telnet', Version='2:2.4-2',
                                 Architecture='amd64', Provides='telnet',
                                 Source='inetutils')],
            },
        }
        udeb_hashtable: dict = {}

    _universe = _vb.from_cache(_Cache())
    _canon: dict = {}
    for _bn, _vers in _universe.items():
        _best_v = _best_r = None
        for _v, _rec in _vers.items():
            if (_best_v is None
                    or _ap.version_compare(str(_v), str(_best_v)) > 0):
                _best_v, _best_r = _v, _rec
        if _best_r is not None:
            _canon[_bn] = (_best_r.get('Source') or _bn).split(' ', 1)[0]
    assert _canon.get('telnet') == 'netkit-telnet', (
        f"canon misattributed via Provides alias: {_canon}")



def test_virtual_build_build_profile_filter_skips_nodoc_when_active():
    """`*-doc` binaries with `profile=!nodoc` must be filtered out of
    synthesis when `nodoc` is in the active profile set.  Largest
    class of false-predicted-extras in the operator's validate run."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    PROFILES = frozenset({'nodoc', 'nocheck', 'noinsttest'})
    assert _vb._binary_active_under_profiles(
        'apt deb admin important arch=any', PROFILES) is True
    assert _vb._binary_active_under_profiles(
        'apt-doc deb doc optional arch=all profile=!nodoc',
        PROFILES) is False
    assert _vb._binary_active_under_profiles(
        'foo-tests deb test optional arch=any profile=!nocheck',
        PROFILES) is False



def test_virtual_build_build_profile_filter_positive_profile_required():
    """`profile=stage1` (positive) requires the profile IS active.
    Common in cross-compile / bootstrap chains.  With no `stage1` in
    our profile set, such a binary must be filtered out."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb
    PROFILES = frozenset({'nodoc', 'nocheck'})
    assert _vb._binary_active_under_profiles(
        'foo-stage1 deb misc optional arch=any profile=stage1',
        PROFILES) is False
    assert _vb._binary_active_under_profiles(
        'foo-stage1 deb misc optional arch=any profile=stage1',
        frozenset({'stage1'})) is True



def test_virtual_build_delta_uses_source_version_not_per_binary():
    """bash-class bug: binary upstream Version is `5.2.15-2+b13`
    (binNMU rebuild) while source.version is pristine `5.2.15-2`.
    Real build emits at source.version (no +bN, no delta from this).
    Synth's delta check MUST use source.version, NOT per-binary
    upstream versions — otherwise every +bN binary triggers spurious
    stamp on a build that was pristine."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _Bash:
        package = 'bash'
        version = '5.2.15-2'  # pristine source
        binary = ['bash', 'bash-static']

    # Upstream binaries carry +b13 binNMU; ledger has only pristine.
    _universe = {
        'bash':        {'5.2.15-2+b13': {'Package': 'bash',
                        'Version': '5.2.15-2+b13', 'Source': 'bash',
                        'Architecture': 'amd64'}},
        'bash-static': {'5.2.15-2+b13': {'Package': 'bash-static',
                        'Version': '5.2.15-2+b13', 'Source': 'bash',
                        'Architecture': 'amd64'}},
    }
    _ledger = {'bash': ['5.2.15-2'], 'bash-static': ['5.2.15-2']}
    _recs = _vb.synthesize_source_binaries(
        source=_Bash(), package_universe=_universe,
        release=1, arch='amd64',
        peer_sources={'bash'})
    # No delta (source is pristine), no lineage (ledger pristine
    # entries don't trigger).  Synth must NOT stamp.
    for _r in _recs:
        assert '+asg' not in _r['Version'], _r['Version']



def test_virtual_build_per_binary_version_is_intrinsic():
    """Under TRANSPOSE the predicted version comes from each BINARY's own
    upstream version (intrinsic K), not the source version.  curl's binary at
    7.88.1-10+deb12u14 → 7.88.1-10+asg1u14, and override_source_version (a
    validate-path device for the at-build source) no longer changes the
    binary's transposed version — the K rides in the binary's own +deb."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _Curl:
        package = 'curl'
        version = '7.88.1-10+deb12u14'
        binary = ['curl']

    _universe = {'curl': {'7.88.1-10+deb12u14': {
        'Package': 'curl', 'Version': '7.88.1-10+deb12u14',
        'Source': 'curl', 'Architecture': 'amd64'}}}
    _ledger: 'dict' = {'curl': ['7.88.1-10']}
    _r1 = _vb.synthesize_source_binaries(
        source=_Curl(), package_universe=_universe,
        release=1, arch='amd64',
        peer_sources={'curl'})
    assert _r1[0]['Version'] == '7.88.1-10+asg1u14'
    # override_source_version does not alter the per-binary transpose.
    _r2 = _vb.synthesize_source_binaries(
        source=_Curl(), package_universe=_universe,
        release=1, arch='amd64',
        peer_sources={'curl'},
        override_source_version='7.88.1-10')
    assert _r2[0]['Version'] == '7.88.1-10+asg1u14'



def test_virtual_build_transpose_preserves_epoch():
    """TRANSPOSE on an epoched source: each binary at 1:9.18.49-1+deb12u3 →
    1:9.18.49-1+asg1u3 (epoch kept in the internal Version, K=3 intrinsic, no
    ledger).  The on-disk Filename is epoch-stripped (dpkg convention)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _Bind9:
        package = 'bind9'
        version = '1:9.18.49-1'  # epoched source version
        binary = ['bind9', 'bind9-libs']

    _universe = {
        'bind9': {'1:9.18.49-1+deb12u3': {
            'Package': 'bind9', 'Version': '1:9.18.49-1+deb12u3',
            'Source': 'bind9', 'Architecture': 'amd64'}},
        'bind9-libs': {'1:9.18.49-1+deb12u3': {
            'Package': 'bind9-libs', 'Version': '1:9.18.49-1+deb12u3',
            'Source': 'bind9', 'Architecture': 'amd64'}},
    }
    _ledger = {
        'bind9':      ['9.18.47-1', '9.18.49-1+asg1u1'],
        'bind9-libs': ['9.18.47-1', '9.18.49-1+asg1u1'],
    }
    _recs = _vb.synthesize_source_binaries(
        source=_Bind9(), package_universe=_universe,
        release=1, arch='amd64',
        peer_sources={'bind9'},
    )
    _by_name = {_r['Package']: _r for _r in _recs}
    assert _by_name['bind9']['Version'] == '1:9.18.49-1+asg1u3'
    assert _by_name['bind9-libs']['Version'] == '1:9.18.49-1+asg1u3'
    # Filename is epoch-stripped.
    assert _by_name['bind9']['Filename'].endswith(
        'bind9_9.18.49-1+asg1u3_amd64.deb')



def test_virtual_build_synthesize_source_filters_by_profile():
    """End-to-end: synthesize_source_binaries with active profiles
    drops binaries whose package_list profile annotation excludes
    them.  Verified against the `apt` source's actual Package-List
    shape: apt-doc and libapt-pkg-doc filtered, others kept."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _AptSrc:
        package = 'apt'
        version = '2.6.1'
        binary = ['apt', 'apt-doc', 'apt-utils',
                  'libapt-pkg-dev', 'libapt-pkg-doc', 'libapt-pkg6.0']
        package_list = [
            'apt deb admin important arch=any',
            'apt-doc deb doc optional arch=all profile=!nodoc',
            'apt-utils deb admin important arch=any',
            'libapt-pkg-dev deb libdevel optional arch=any',
            'libapt-pkg-doc deb doc optional arch=all profile=!nodoc',
            'libapt-pkg6.0 deb libs optional arch=any',
        ]

    _univ = {b: {'2.6.1': {'Package': b, 'Version': '2.6.1',
                            'Source': 'apt', 'Architecture': 'amd64'}}
             for b in _AptSrc.binary}
    _recs = _vb.synthesize_source_binaries(
        source=_AptSrc(), package_universe=_univ,
        release=1, arch='amd64', peer_sources={'apt'},
        active_profiles=frozenset({'nodoc', 'nocheck', 'noinsttest'}))
    _names = {_r['Package'] for _r in _recs}
    # `*-doc` filtered by profile.
    assert 'apt-doc' not in _names
    assert 'libapt-pkg-doc' not in _names
    # Rest kept (no profile gate or non-matching profile).
    assert {'apt', 'apt-utils', 'libapt-pkg-dev',
            'libapt-pkg6.0'} <= _names



def test_virtual_build_from_cache_merges_udeb_hashtable():
    """from_cache MUST include cache.udeb_hashtable too.  Cache holds
    udebs (Section: debian-installer records) in a parallel
    hashtable; without merging, the canonical-source filter can't fire
    for udeb names — every kernel `*-di` lookup misses, both
    linux + linux-signed-amd64 fall through and collide on dedup."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import virtual_build as _vb

    class _P(dict):
        def __init__(self, **f):
            super().__init__()
            for k, v in f.items():
                self[k] = v
            self.package = f['Package']
            self.version = f['Version']

    class _StubCache:
        package_hashtable = {
            'libfoo': {'1.0': [_P(Package='libfoo', Version='1.0',
                                  Source='libfoo-src')]},
        }
        # Kernel udebs live here in the real Cache class.
        udeb_hashtable = {
            'kernel-image-6.1.0-49-amd64-di': {'6.1.174-1': [_P(
                Package='kernel-image-6.1.0-49-amd64-di',
                Version='6.1.174-1',
                Source='linux-signed-amd64')]},
        }
    _u = _vb.from_cache(_StubCache())
    # Both deb + udeb names present after merge.
    assert 'libfoo' in _u
    assert 'kernel-image-6.1.0-49-amd64-di' in _u
    # The udeb's Source field survives — canonical filter can fire.
    _udeb_rec = _u['kernel-image-6.1.0-49-amd64-di']['6.1.174-1']
    assert _udeb_rec['Source'] == 'linux-signed-amd64'



def test_check_build_matches_asg_variant_of_prediction():
    """check_build must accept a +asg<R>u<N> stamped artifact for a pristine
    prediction — otherwise the source rebuilds every run (CONF-13 loop)."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _dest = os.path.join(_tmp, 'dists', 'test', 'main', 'binary-amd64')
        os.makedirs(_dest)
        _log = os.path.join(_tmp, 'log')
        os.makedirs(_log)
        # on-disk artifact carries the asg stamp; prediction is pristine
        _build_minimal_deb(os.path.join(_dest, 'openssl_3.0.15-1+asg1u1_amd64.deb'),
                           'openssl', '3.0.15-1+asg1u1', 'amd64')
        import utils as _u
        _rec = _u.new_build_record(
            package='openssl', intended_version='3.0.15-1', patch_set_hash='')
        _rec.update({'phase': 'done', 'status': 'PASS'})
        _u.write_build_record(_log, _rec)

        _bc = _make_buildcontainer_stub(repo=_tmp, buildlog=_log)

        class _FakeConfig:
            def deb_dest_for_filename(self, _f, component='main'):
                return _dest

        class _Src:
            package = 'openssl'

        _bc.config = _FakeConfig()
        assert _bc.check_build(_Src(), ['openssl_3.0.15-1_amd64.deb']) is True, (
            "check_build should match the stamped variant of the prediction")
        # a prediction with no on-disk match → False
        assert _bc.check_build(_Src(), ['openssh_9.2-1_amd64.deb']) is False



def test_check_build_locates_non_main_component_deb():
    """A tunneled non-main package's binaries live in their component dir;
    check_build must use src._mirror.component to look there — otherwise it
    looks in main, never finds them, and re-tunnels every run."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _main = os.path.join(_tmp, 'dists', 'test', 'main', 'binary-amd64')
        _nff = os.path.join(
            _tmp, 'dists', 'test', 'non-free-firmware', 'binary-amd64')
        os.makedirs(_main)
        os.makedirs(_nff)
        _log = os.path.join(_tmp, 'log')
        os.makedirs(_log)
        # firmware deb lives ONLY in the non-free-firmware dir
        _build_minimal_deb(os.path.join(_nff, 'intel-microcode_3.1_amd64.deb'),
                           'intel-microcode', '3.1', 'amd64')
        import utils as _u
        _rec = _u.new_build_record(
            package='intel-microcode', intended_version='3.1', patch_set_hash='')
        _rec.update({'phase': 'tunneled', 'status': 'TUNNELED'})
        _u.write_build_record(_log, _rec)
        _bc = _make_buildcontainer_stub(repo=_tmp, buildlog=_log)

        class _FakeConfig:
            def deb_dest_for_filename(self, _f, component='main'):
                return _nff if component == 'non-free-firmware' else _main

        class _Mirror:
            component = 'non-free-firmware'

        class _Src:
            package = 'intel-microcode'
            _mirror = _Mirror()

        _bc.config = _FakeConfig()
        # Found in the component dir → True. Without the component lookup,
        # check_build would search main, miss, and return False.
        assert _bc.check_build(
            _Src(), ['intel-microcode_3.1_amd64.deb']) is True



def test_normalize_built_artifacts_transposes_deb_to_asg_no_ledger():
    """TRANSPOSE: a binary whose upstream version carries a trailing +debNuK
    is transposed to +asg<R>uK in place — K is intrinsic to the upstream
    version, so NO ledger is consulted (the old ship-order asg_next_n is gone).
    +deb12u2 → +asg1u2."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _p = os.path.join(_tmp, 'openssl_3.0.15-1+deb12u2_amd64.deb')
        _build_minimal_deb(_p, 'openssl', '3.0.15-1+deb12u2', 'amd64')
        _bc = _make_buildcontainer_stub(repo=_tmp)
        # No ledger needed — intentionally left unset.

        class _FakeConfig:
            build_version = '1'

        class _Src:
            package = 'openssl'
            version = '3.0.15-1+deb12u2'

        _bc.config = _FakeConfig()
        _bc._normalize_built_artifacts(_Src(), [_p], was_patched=False)
        assert not os.path.exists(_p), "+deb file should be transposed + renamed"
        _stamped = os.path.join(_tmp, 'openssl_3.0.15-1+asg1u2_amd64.deb')
        assert os.path.exists(_stamped), "+deb build was not transposed to +asg"
        import subprocess
        _ver = subprocess.run(['dpkg-deb', '-f', _stamped, 'Version'],
                              check=True, capture_output=True, text=True).stdout.strip()
        assert _ver == '3.0.15-1+asg1u2', _ver



def test_normalize_built_artifacts_transpose_has_no_lineage_trigger():
    """TRANSPOSE: the ship-order lineage-continuation trigger is GONE.  A
    faithful PRISTINE rebuild (binary version has no trailing +deb) stays
    pristine REGARDLESS of what the ledger holds — the old regression
    (a pristine rebuild dangling against a sibling meta's prior +asg pin)
    cannot arise, because +deb→+asg is deterministic from the upstream
    version: the sibling meta also transposes to the same pristine version,
    so the pins stay consistent without any ledger-driven stamp.
    """
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _p = os.path.join(_tmp, 'linux-headers-6.1.0-49-common_6.1.174-1_all.deb')
        _build_minimal_deb(_p, 'linux-headers-6.1.0-49-common',
                           '6.1.174-1', 'all')
        _bc = _make_buildcontainer_stub(repo=_tmp)
        # A prior +asg publish in the ledger is now IRRELEVANT — transpose does
        # not consult it.  The binary version (6.1.174-1) has no trailing +deb.
        _bc.asg_ledger = {
            'linux-headers-6.1.0-49-common': ['6.1.174-1+asg1u1'],
        }

        class _FakeConfig:
            build_version = '1'

        class _Src:
            package = 'linux'         # source name
            version = '6.1.174-1'      # PRISTINE — no redistribution suffix

        _bc.config = _FakeConfig()
        _bc._normalize_built_artifacts(_Src(), [_p], was_patched=False)

        assert os.path.exists(_p), (
            "pristine rebuild must stay pristine under transpose (no lineage "
            f"trigger); got: {sorted(os.listdir(_tmp))}")



def test_normalize_built_artifacts_no_stamp_when_ledger_has_no_lineage():
    """Mirror to the above: ledger is present but contains NO `+asg`
    entries at this binary's base (e.g. only pristine publishes, or
    entries for an unrelated source).  Then no lineage to continue;
    ship pristine.
    """
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _p = os.path.join(_tmp, 'foo_1.0-1_amd64.deb')
        _build_minimal_deb(_p, 'foo', '1.0-1', 'amd64')
        _bc = _make_buildcontainer_stub(repo=_tmp)
        # Ledger only has a PRISTINE publish for foo (no asg suffix) —
        # nothing to continue.  And an unrelated entry for `bar` to make
        # sure the lookup is keyed by binary name.
        _bc.asg_ledger = {
            'foo': ['1.0-1'],
            'bar': ['2.0-1+asg1u1'],
        }

        class _FakeConfig:
            build_version = '1'

        class _Src:
            package = 'foo'
            version = '1.0-1'

        _bc.config = _FakeConfig()
        _bc._normalize_built_artifacts(_Src(), [_p], was_patched=False)
        assert os.path.exists(_p), (
            "no lineage in ledger → should ship pristine, no rename")



def test_normalize_built_artifacts_patch_on_pristine_gets_p1_no_ledger():
    """TRANSPOSE: a PATCHED build on a pristine (no-+deb) base gets +p1 with NO
    ledger — P comes from was_patched (floored to 1 so patched bytes never ship
    unmarked), and there is no +asg because the upstream ordinal K is 0."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _p = os.path.join(_tmp, 'openssl_3.0.15-1_amd64.deb')
        _build_minimal_deb(_p, 'openssl', '3.0.15-1', 'amd64')
        _bc = _make_buildcontainer_stub(repo=_tmp)
        # no asg_ledger — irrelevant under transpose

        class _FakeConfig:
            build_version = '1'

        class _Src:
            package = 'openssl'
            version = '3.0.15-1'

        _bc.config = _FakeConfig()
        _bc._normalize_built_artifacts(_Src(), [_p], was_patched=True)
        assert not os.path.exists(_p), "patched build should be stamped + renamed"
        # Anchored to u0 so it sorts below a future upstream update (+asg1u1).
        _stamped = os.path.join(_tmp, 'openssl_3.0.15-1+asg1u0+p1_amd64.deb')
        assert os.path.exists(_stamped), (
            f"patch-on-pristine should ship +asg1u0+p1; "
            f"got {sorted(os.listdir(_tmp))}")
        import subprocess
        _ver = subprocess.run(['dpkg-deb', '-f', _stamped, 'Version'],
                              check=True, capture_output=True, text=True).stdout.strip()
        assert _ver == '3.0.15-1+asg1u0+p1', _ver



def test_normalize_built_artifacts_cross_source_dep_is_transposed():
    """TRANSPOSE: a pristine binary stays pristine (no +asg / +p), but its
    cross-source floor onto a +deb dependency is TRANSPOSED (not stripped) so
    it stays satisfiable by our +asg-versioned dependency.  This is the
    libclone-perl → perl floor: `perl (>= 5.36.0-7+deb12u3)` becomes
    `perl (>= 5.36.0-7+asg1u3)` — which our rebuilt perl (5.36.0-7+asg1u3)
    satisfies, whereas the old strip-to-pristine discarded the ordinal."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from debian.debfile import DebFile
    with tempfile.TemporaryDirectory() as _tmp:
        _p = os.path.join(_tmp, 'libclone-perl_0.46-1_amd64.deb')
        _build_minimal_deb(_p, 'libclone-perl', '0.46-1', 'amd64',
                           depends='perl (>= 5.36.0-7+deb12u3)')
        _bc = _make_buildcontainer_stub(repo=_tmp)

        class _FakeConfig:
            build_version = '1'

        class _Src:
            package = 'libclone-perl'
            version = '0.46-1'

        _bc.config = _FakeConfig()
        _final = _bc._normalize_built_artifacts(_Src(), [_p], was_patched=False)
        # (1) The binary itself is pristine (no trailing +deb) → no +asg / +p.
        assert os.path.exists(_p), (
            "pristine binary must stay pristine — no stamp")
        assert not any(('+asg' in os.path.basename(_f)
                        or '+p1' in os.path.basename(_f)) for _f in _final), _final
        # (2) The cross-source floor is TRANSPOSED to +asg (not stripped).
        with DebFile(_p) as _deb:
            _ctrl = _deb.control.get_content('control').decode()
        assert 'perl (>= 5.36.0-7+asg1u3)' in _ctrl, (
            f"dep constraint must be transposed to +asg, got: {_ctrl}")
        assert '+deb12u3' not in _ctrl, (
            "Debian suffix must be gone from the dep constraint")



def test_postbuild_convergence_hard_fails_when_check_build_still_false():
    """Guard B: the per-source build path must re-check check_build
    after a PASS and hard-fail (not silently rebuild next run) on
    non-convergence.  Logic lives in _build_one_source (COMP-03 Phase 4
    extracted the per-source unit from the cmd_source_build loop body)."""
    import re as _re
    _body = _session_source()
    _m = _re.search(r'def _build_one_source\(.*?(?=\n    def )',
                    _body, _re.DOTALL)
    assert _m, "_build_one_source not found"
    _b = _m.group(0)
    assert 'if _build_result and self.container.check_build(' in _b, (
        "Guard B: a PASS must be re-validated through check_build")
    assert ('if _build_result:' in _b
            and 'check_build still false' in _b), (
        "Guard B: a built-but-unconverged source must hard-fail with a diagnostic")





def test_source_build_autodetects_update_mode():
    """source build self-detects update mode (gotcha G): a subset/bare call
    routes to _do_update_build when a delta is pending; _do_update_build uses
    the published→current diff, and rebuilds WITHOUT blanket force — the
    per-source build path is bump-aware (already-built skipped; same-base
    re-spins rebuilt as bump-targets, their expected filename derived intrinsically
    by transpose).  COMP-03 Phase 4 extracted the per-source unit into
    _build_one_source."""
    import re
    _body = _session_source()
    # Update-mode DETECTION moved into the shared _resolve_build_workload helper
    # (so `source build` + `source remotebuild` share the front-half); the
    # ROUTING (calling _do_update_build) stays in cmd_source_build /
    # cmd_source_remotebuild.
    _m = re.search(r'def cmd_source_build\(self.*?(?=\n    def )', _body, re.DOTALL)
    _b = _m.group(0)
    assert '_do_update_build()' in _b, (
        "source build must route to update mode")
    _rw = re.search(r'def _resolve_build_workload\(self.*?(?=\n    def )',
                    _body, re.DOTALL)
    assert _rw, '_resolve_build_workload not found'
    _rwb = _rw.group(0)
    assert '_update_build_pending()' in _rwb, (
        "the shared workload resolver must auto-detect update mode")
    assert 'not _names' in _rwb, "explicit package names opt out of update mode"
    # Detection applies to BOTH local and remote builds (CONS-10): the bump is
    # decided locally and the +asg stamp applied on recovery, so a remote update
    # build needs no remote-side logic.  It must NOT be gated on `not remote`.
    assert 'not remote and not _names' not in _rwb, (
        "update-mode detection must apply to remote builds too (CONS-10)")
    # Bump-awareness lives in _build_one_source after the Phase 4 refactor.
    _w = re.search(r'def _build_one_source\(.*?(?=\n    def )', _body, re.DOTALL)
    assert _w, "_build_one_source not found"
    assert '_needs_bump_build(' in _w.group(0), (
        "the per-source build path must be bump-aware (rebuild same-base re-spins)")
    _u = re.search(r'def _do_update_build\(self.*?(?=\n    def )', _body, re.DOTALL)
    _ub = _u.group(0)
    assert '_workload_since_snapshot(' in _ub, "update build diffs published→current"
    assert "cmd_source_build('force'" not in _ub, (
        "update build must NOT blanket-force — it relies on bump-aware skipping")
    assert '_source_state(' in _ub and 'needs_build' in _ub, (
        "update build must ALSO build non-delta sources that need building "
        "(forks etc.), so `source build all` builds everything the audit lists")



def test_build_record_schema_v3_field_set():
    """Pin the v3 field set — adding a field is a schema bump that must
    be reflected in the version constant; removing one breaks OBS-02
    history readers.  This test is the contract.

    v1→v2 (COORD-01): added `output_hashes` ({filename: sha256_hex}) so
    the coord-layer claim records have a stable per-binary digest to
    pin without re-hashing every read.

    v2→v3 (MIRROR-02): added `republished_from`
    ({filename: {url, upstream_sha256}}) populated by cmd_tunnel_package
    so the federation can mark tunneled packages as no-owner; also
    added `pulled_from` (provenance for mirror-pulled records) and
    `component` (apt component of the source's origin mirror, so the
    audit / mirror-pull / publish layers don't have to re-derive it
    from the dep tree).

    v4→v5 (OBS-03): added `resources` (None at entry; populated at the
    container_exited phase with {peak_rss_bytes, peak_rss_mb,
    mem_limit_bytes, peak_cpu_pct, samples} from the per-build poll).

    v5→v6 (transpose scheme): added `patch_bump_count` (P — our patch level
    on the current upstream base) and `bn_bump_count` (our force-binNMU level),
    the per-source sidecar counters the content-order version decision reads.

    `athena_build_version` is INFORMATIONAL provenance (which Athena-Build
    produced the record), NOT a functional schema field: no consumer gates on
    it and the migration ignores it, so it does NOT carry a schema bump — but it
    is still pinned here so an accidental record-shape change is caught."""
    _u = _utils_module()
    _rec = _u.new_build_record(
        package='libwmf', intended_version='0.2.12-5.1',
        patch_set_hash='abc123', started='2026-06-02T14:00:00Z',
    )
    assert _u.BUILD_RECORD_SCHEMA_VERSION == 6
    assert _rec['schema_version'] == 6
    _required = {
        'schema_version', 'package', 'intended_version', 'built_version',
        'patch_set_hash', 'phase', 'status', 'started', 'finished',
        'elapsed_seconds', 'exit_code', 'oom_killed', 'output_count', 'outputs',
        'output_hashes',
        'republished_from', 'pulled_from', 'component',
        'lifecycle_v', 'history',   # LEDGER-01 v4 baseline
        'resources',                # OBS-03 v5
        'athena_build_version',     # toolchain provenance (informational)
        'patch_bump_count', 'bn_bump_count',   # transpose-scheme v6 sidecar
    }
    assert set(_rec.keys()) == _required, (
        f"v6 schema drift: {set(_rec.keys()) ^ _required}")
    assert _rec['phase'] == 'entry'
    assert _rec['status'] is None
    assert _rec['republished_from'] == {}
    assert _rec['pulled_from'] is None
    assert _rec['component'] == 'main'  # default when not specified
    assert _rec['outputs'] == []
    assert _rec['output_hashes'] == {}
    assert _rec['resources'] is None



def test_obs03_container_cpu_pct_and_sampler_wired():
    """OBS-03: CPU% from the cpu_stats-vs-precpu_stats delta of one
    stats(stream=False) reading; None when not computable.  And build() wires
    the sampler + stamps `resources` at the container_exited phase + stops the
    poller on every exit path."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import buildcontainer as _bc
    # cpu_delta=1000, system_delta=5000, 4 cpus → (1000/5000)*4*100 = 80%
    assert _bc._container_cpu_pct({
        'cpu_stats': {'cpu_usage': {'total_usage': 2000},
                      'system_cpu_usage': 10000, 'online_cpus': 4},
        'precpu_stats': {'cpu_usage': {'total_usage': 1000},
                         'system_cpu_usage': 5000}}) == 80.0
    # zero system delta → not computable
    assert _bc._container_cpu_pct({
        'cpu_stats': {'cpu_usage': {'total_usage': 1}, 'system_cpu_usage': 5},
        'precpu_stats': {'cpu_usage': {'total_usage': 1},
                         'system_cpu_usage': 5}}) is None
    # malformed payload → None (never raises)
    assert _bc._container_cpu_pct({}) is None
    # build() wiring: sampler started, peaks stamped, poller stopped in finally
    _src = inspect.getsource(_bc.BuildContainer.build)
    assert '_sample_resources' in _src
    assert 'resources=_resources' in _src
    assert '_res_stop.set()' in _src



def test_build_record_hmac_round_trip():
    """Sign → verify on the same record returns True.  Tamper any
    field → verify returns False."""
    _u = _utils_module()
    with tempfile.TemporaryDirectory() as _td:
        _rec = _u.new_build_record(
            package='libwmf', intended_version='0.2.12-5.1',
            patch_set_hash='abc', started='2026-06-02T14:00:00Z',
        )
        _u.write_build_record(_td, _rec)
        _loaded = _u.read_build_record(_td, 'libwmf')
        assert _loaded is not None
        assert _loaded['package'] == 'libwmf'
        assert _loaded['phase'] == 'entry'
        assert 'sig' in _loaded



def test_build_record_tamper_detection():
    """Hand-edit the on-disk file → read_build_record returns None."""
    _u = _utils_module()
    import json as _json
    with tempfile.TemporaryDirectory() as _td:
        _u.write_build_record(_td, _u.new_build_record(
            package='libwmf', intended_version='0.2.12-5.1',
            patch_set_hash='abc', started='2026-06-02T14:00:00Z',
        ))
        _path = os.path.join(_td, 'libwmf.build.json')
        with open(_path) as _fh:
            _data = _json.load(_fh)
        _data['intended_version'] = '99.99-99'    # tamper
        with open(_path, 'w') as _fh:
            _json.dump(_data, _fh)
        assert _u.read_build_record(_td, 'libwmf') is None, (
            "tampered record must fail HMAC verify")



def test_build_record_hmac_key_mode_0600():
    """Key file must be mode 0600 — readable only by owner."""
    _u = _utils_module()
    import stat as _st
    with tempfile.TemporaryDirectory() as _td:
        _u._load_or_create_hmac_key(_td)
        _kpath = os.path.join(_td, '.metrics.hmac.key')
        assert os.path.exists(_kpath)
        _mode = _st.S_IMODE(os.stat(_kpath).st_mode)
        assert _mode == 0o600, f"key mode is {oct(_mode)}, expected 0o600"



def test_build_record_atomic_write_no_temp_left_behind_on_success():
    """A successful write leaves no .tmp file in the directory."""
    _u = _utils_module()
    with tempfile.TemporaryDirectory() as _td:
        _u.write_build_record(_td, _u.new_build_record(
            package='libwmf', intended_version='0.2.12-5.1',
            patch_set_hash='abc', started='2026-06-02T14:00:00Z',
        ))
        _leftover = [_f for _f in os.listdir(_td) if _f.endswith('.tmp')]
        assert _leftover == [], f"temp files left behind: {_leftover}"



def test_build_record_phase_transition_to_done_sets_status_pass():
    """update_build_record with phase=done auto-sets status=PASS."""
    _u = _utils_module()
    with tempfile.TemporaryDirectory() as _td:
        _u.write_build_record(_td, _u.new_build_record(
            package='libwmf', intended_version='0.2.12-5.1',
            patch_set_hash='abc', started='2026-06-02T14:00:00Z',
        ))
        _new = _u.update_build_record(
            _td, 'libwmf', phase='done', built_version='0.2.12-5.1',
            finished='2026-06-02T14:02:30Z', elapsed_seconds=150.5,
            exit_code=0, output_count=3,
            outputs=['a_1_amd64.deb', 'b_1_amd64.deb', 'c_1_amd64.deb'],
        )
        assert _new['phase'] == 'done'
        assert _new['status'] == 'PASS'
        assert _new['built_version'] == '0.2.12-5.1'
        assert _new['elapsed_seconds'] == 150.5
        _reloaded = _u.read_build_record(_td, 'libwmf')
        assert _reloaded is not None and _reloaded['status'] == 'PASS'



def test_build_record_phase_transition_to_failed_sets_status_fail():
    _u = _utils_module()
    with tempfile.TemporaryDirectory() as _td:
        _u.write_build_record(_td, _u.new_build_record(
            package='libwmf', intended_version='0.2.12-5.1',
            patch_set_hash='abc', started='2026-06-02T14:00:00Z',
        ))
        _new = _u.update_build_record(
            _td, 'libwmf', phase='failed', exit_code=137, oom_killed=True,
            finished='2026-06-02T14:00:45Z', elapsed_seconds=45.0,
        )
        assert _new['status'] == 'FAIL'
        assert _new['exit_code'] == 137
        assert _new['oom_killed'] is True



def test_build_record_phase_tunneled_sets_status_tunneled():
    _u = _utils_module()
    with tempfile.TemporaryDirectory() as _td:
        _u.write_build_record(_td, _u.new_build_record(
            package='shim-signed', intended_version='1.40+15.4-7+deb12u1',
            patch_set_hash='', started='2026-06-02T14:00:00Z',
        ))
        _new = _u.update_build_record(
            _td, 'shim-signed', phase='tunneled',
            built_version='1.40+15.4-7+deb12u1',
            finished='2026-06-02T14:00:05Z', elapsed_seconds=5.0,
            output_count=2, outputs=['shim-signed_1_amd64.deb', 'shim-helpers_1_amd64.deb'],
        )
        assert _new['status'] == 'TUNNELED'



def test_classify_build_record_full_matrix():
    """Pin the audit classifier translation for every (record-state)
    combination that consumers need to disambiguate."""
    _u = _utils_module()
    # None → missing (file absent / corrupt / tampered)
    assert _u.classify_build_record(None) == 'missing'

    def _r(phase, status=None):
        return {
            'schema_version': 2, 'package': 'x', 'intended_version': '1',
            'built_version': None, 'patch_set_hash': '', 'phase': phase,
            'status': status, 'started': '', 'finished': None,
            'elapsed_seconds': None, 'exit_code': None, 'oom_killed': False,
            'output_count': 0, 'outputs': [], 'output_hashes': {},
            'sig': 'x',
        }
    # Terminal phases
    assert _u.classify_build_record(_r('done', 'PASS')) == 'ok'
    assert _u.classify_build_record(_r('failed', 'FAIL')) == 'fail'
    assert _u.classify_build_record(_r('tunneled', 'TUNNELED')) == 'tunneled'
    # Non-terminal phases all map to 'interrupted' — process was killed
    # mid-build, file shows the last completed phase.
    for _ph in ('entry', 'container_exited', 'segregated', 'normalized'):
        assert _u.classify_build_record(_r(_ph)) == 'interrupted', _ph



def test_build_record_intended_vs_built_version_drift_visible():
    """When intended_version != built_version, both are preserved in
    the record so the audit can surface the drift.  Foundation for
    'we asked for X, what shipped is Y' detection."""
    _u = _utils_module()
    with tempfile.TemporaryDirectory() as _td:
        _u.write_build_record(_td, _u.new_build_record(
            package='libfoo', intended_version='1.0-1+deb12u2',
            patch_set_hash='', started='2026-06-02T14:00:00Z',
        ))
        _new = _u.update_build_record(
            _td, 'libfoo', phase='done', built_version='1.0-1',
            finished='2026-06-02T14:01:00Z', elapsed_seconds=60.0,
            exit_code=0, output_count=1, outputs=['libfoo_1.0-1_amd64.deb'],
        )
        assert _new['intended_version'] == '1.0-1+deb12u2'
        assert _new['built_version'] == '1.0-1'
        assert _new['intended_version'] != _new['built_version']



def test_normalize_built_artifacts_returns_post_rename_paths():
    """_normalize_built_artifacts must return the POST-rename paths so
    the caller can hash the files at their actual on-disk locations.
    Strip renames pre→post.  Asg-stamp renames again.  Before this
    fix, the function returned None, so the caller iterated pre-strip
    paths whose files no longer existed → get_sha256 returned '' →
    build record's output_hashes ended up {} → generate_pending_claims
    skipped every output → ~880 of 985 sources never appeared in
    athena-primary.jsonl.  Caught 2026-06-05.
    """
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        # NMU-suffixed source — strip will rename + asg-stamp will rename.
        _p = os.path.join(_tmp, 'libabsl-dev_20220623.1-1+deb12u2_amd64.deb')
        _build_minimal_deb(_p, 'libabsl-dev', '20220623.1-1+deb12u2', 'amd64')
        _bc = _make_buildcontainer_stub(repo=_tmp)
        # Ledger has prior +asg1u1 → lineage continuation → stamp to +asg1u2.
        _bc.asg_ledger = {'libabsl-dev': ['20220623.1-1+asg1u1']}

        class _FakeConfig:
            build_version = '1'

        class _Src:
            package = 'abseil'
            version = '20220623.1-1+deb12u2'

        _bc.config = _FakeConfig()
        _final = _bc._normalize_built_artifacts(_Src(), [_p], was_patched=False)

        # Source file should be gone (strip + stamp both renamed).
        assert not os.path.exists(_p), \
            "pre-normalize path should be gone after strip + stamp"
        # The returned list should point at the actual on-disk file.
        assert _final, "normalize must return non-empty list"
        for _fp in _final:
            assert os.path.exists(_fp), (
                f"returned path {_fp!r} does not exist on disk — "
                "caller will fail to hash it")
        # And those paths should be hashable (which is the whole point).
        sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
        import utils as _u
        _hashes = {os.path.basename(_fp): _u.get_sha256(_fp, use_cache=False)
                   for _fp in _final}
        assert all(_h for _h in _hashes.values()), (
            f"every returned file must hash non-empty: {_hashes!r}")



def test_normalize_built_artifacts_uniform_p_across_siblings():
    """TRANSPOSE: every binary of a source gets the SAME patch level P (uniform
    P per source), and a same-source sibling `=` pin is restamped to the EXACT
    final version — so the meta's `Depends: <abi-sibling> (= …)` stays resolved
    on disk.  Here a PATCHED +deb kernel source (P=1): both binaries →
    6.1.174-1+asg1u1+p1, and the meta's pin tracks it exactly."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from debian.debfile import DebFile
    with tempfile.TemporaryDirectory() as _tmp:
        _meta = os.path.join(
            _tmp, 'linux-headers-amd64_6.1.174-1+deb12u1_amd64.deb')
        _abi = os.path.join(
            _tmp, 'linux-headers-6.1.0-49-amd64_6.1.174-1+deb12u1_amd64.deb')
        _build_minimal_deb(_meta, 'linux-headers-amd64', '6.1.174-1+deb12u1',
                           'amd64',
                           depends='linux-headers-6.1.0-49-amd64 '
                                   '(= 6.1.174-1+deb12u1)')
        _build_minimal_deb(_abi, 'linux-headers-6.1.0-49-amd64',
                           '6.1.174-1+deb12u1', 'amd64')

        _bc = _make_buildcontainer_stub(repo=_tmp)

        class _Cfg:
            build_version = '1'
        _bc.config = _Cfg()

        class _SrcPkg:
            package = 'linux'
            version = '6.1.174-1+deb12u1'

        _final = _bc._normalize_built_artifacts(
            _SrcPkg(), [_meta, _abi], was_patched=True)
        _names = sorted(os.path.basename(_f) for _f in _final)
        # Uniform K (intrinsic u1) + uniform P (=1) across both siblings.
        assert _names == [
            'linux-headers-6.1.0-49-amd64_6.1.174-1+asg1u1+p1_amd64.deb',
            'linux-headers-amd64_6.1.174-1+asg1u1+p1_amd64.deb',
        ], _names
        # The meta's sibling pin restamped to the EXACT final version.
        _meta_final = [_f for _f in _final if 'linux-headers-amd64_' in _f][0]
        with DebFile(_meta_final) as _deb:
            _ctrl = _deb.control.get_content('control').decode()
        assert ('linux-headers-6.1.0-49-amd64 (= 6.1.174-1+asg1u1+p1)'
                in _ctrl), _ctrl



def test_normalize_built_artifacts_cross_base_sibling_pin_gets_patch_level():
    """TRANSPOSE: a source whose binaries carry DIFFERENT bases (e2fsprogs-style:
    comerr-dev at 2.1-1.47.0-2, libcom-err2 at 1.47.0-2), patched (P=1), must
    restamp the cross-base exact `=` pin to the sibling's EXACT final version
    INCLUDING +p1.  The old own-version-only restamp dropped the +p1 here and
    shipped an unsatisfiable pin (the predictor masked it) — this guards it."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from debian.debfile import DebFile
    with tempfile.TemporaryDirectory() as _tmp:
        _dev = os.path.join(_tmp, 'comerr-dev_2.1-1.47.0-2+deb12u2_amd64.deb')
        _lib = os.path.join(_tmp, 'libcom-err2_1.47.0-2+deb12u2_amd64.deb')
        # comerr-dev pins its DIFFERENT-base sibling libcom-err2 exactly.
        _build_minimal_deb(_dev, 'comerr-dev', '2.1-1.47.0-2+deb12u2', 'amd64',
                           depends='libcom-err2 (= 1.47.0-2+deb12u2)')
        _build_minimal_deb(_lib, 'libcom-err2', '1.47.0-2+deb12u2', 'amd64')

        _bc = _make_buildcontainer_stub(repo=_tmp)

        class _Cfg:
            build_version = '1'
        _bc.config = _Cfg()

        class _SrcPkg:
            package = 'e2fsprogs'
            version = '1.47.0-2+deb12u2'

        _final = _bc._normalize_built_artifacts(
            _SrcPkg(), [_dev, _lib], was_patched=True)
        _names = sorted(os.path.basename(_f) for _f in _final)
        # Each binary transposes its OWN base; uniform P=1.
        assert _names == [
            'comerr-dev_2.1-1.47.0-2+asg1u2+p1_amd64.deb',
            'libcom-err2_1.47.0-2+asg1u2+p1_amd64.deb',
        ], _names
        # The cross-base pin tracks the sibling's EXACT final version (with +p1).
        _dev_final = [_f for _f in _final if 'comerr-dev_' in _f][0]
        with DebFile(_dev_final) as _deb:
            _ctrl = _deb.control.get_content('control').decode()
        assert 'libcom-err2 (= 1.47.0-2+asg1u2+p1)' in _ctrl, _ctrl



def test_new_build_record_threads_component_field():
    """`new_build_record(component='non-free-firmware')` writes the
    component to the record; default stays 'main' for back-compat.
    """
    _u = _utils_module()
    _rec = _u.new_build_record(
        package='amd64-microcode',
        intended_version='3.20250311.1~deb12u1',
        patch_set_hash='',
        component='non-free-firmware',
    )
    assert _rec['component'] == 'non-free-firmware', _rec['component']



def test_build_record_canonical_json_stable_across_key_order():
    """Two records with the same fields in different insertion order
    produce identical signatures (canonical-JSON serialization)."""
    _u = _utils_module()
    _key = b'k' * 32
    _a = {'package': 'x', 'phase': 'entry', 'schema_version': 2}
    _b = {'schema_version': 2, 'phase': 'entry', 'package': 'x'}
    assert _u._sign_record(_a, _key)['sig'] == _u._sign_record(_b, _key)['sig']



def test_build_record_output_hashes_round_trip():
    """COORD-01: output_hashes populated on phase=done survive read +
    HMAC verify intact; populated from update_build_record kwargs."""
    _u = _utils_module()
    with tempfile.TemporaryDirectory() as _td:
        _u.write_build_record(_td, _u.new_build_record(
            package='libwmf', intended_version='0.2.12-5.1',
            patch_set_hash='abc', started='2026-06-02T14:00:00Z',
        ))
        _hashes = {
            'libwmf_0.2.12-5.1_amd64.deb': 'a' * 64,
            'libwmf-dev_0.2.12-5.1_amd64.deb': 'b' * 64,
        }
        _new = _u.update_build_record(
            _td, 'libwmf', phase='done', built_version='0.2.12-5.1',
            finished='2026-06-02T14:02:30Z', elapsed_seconds=150.5,
            exit_code=0, output_count=2,
            outputs=sorted(_hashes.keys()),
            output_hashes=_hashes,
        )
        assert _new['output_hashes'] == _hashes
        _reloaded = _u.read_build_record(_td, 'libwmf')
        assert _reloaded is not None
        assert _reloaded['output_hashes'] == _hashes



def test_build_record_v1_legacy_record_reads_tolerantly():
    """A v1 record on disk (written before COORD-01 added output_hashes)
    must still load.  HMAC verifies on the v1 canonical bytes (which
    have no output_hashes field).  Consumers treat absent
    output_hashes as {} — the backfill helper migrates them on demand."""
    _u = _utils_module()
    import json as _json
    with tempfile.TemporaryDirectory() as _td:
        # Hand-write a v1 record (no output_hashes field).
        _legacy = {
            'schema_version': 1,
            'package': 'legacy',
            'intended_version': '1.0-1',
            'built_version': '1.0-1',
            'patch_set_hash': 'h',
            'phase': 'done',
            'status': 'PASS',
            'started': '2026-06-02T14:00:00Z',
            'finished': '2026-06-02T14:01:00Z',
            'elapsed_seconds': 60.0,
            'exit_code': 0,
            'oom_killed': False,
            'output_count': 1,
            'outputs': ['legacy_1.0-1_amd64.deb'],
        }
        _key = _u._load_or_create_hmac_key(_td)
        _signed = _u._sign_record(_legacy, _key)
        _path = _u._build_record_path(_td, 'legacy')
        with open(_path, 'wb') as _fh:
            _fh.write(_json.dumps(
                _signed, sort_keys=True, indent=2).encode('utf-8'))
        # Reader accepts it.
        _loaded = _u.read_build_record(_td, 'legacy')
        assert _loaded is not None
        assert _loaded['schema_version'] == 1
        assert 'output_hashes' not in _loaded
        # Classifier still maps correctly.
        assert _u.classify_build_record(_loaded) == 'ok'



def test_backfill_output_hashes_upgrades_v1_record():
    """backfill_output_hashes walks build.json files, locates outputs
    under repo_root, hashes them, sets output_hashes, bumps schema_version
    to 2, and re-signs."""
    _u = _utils_module()
    import json as _json
    with tempfile.TemporaryDirectory() as _td:
        _log = os.path.join(_td, 'log')
        _repo = os.path.join(_td, 'repo')
        _pool = os.path.join(_repo, 'dists/thor/main/binary-amd64')
        os.makedirs(_log)
        os.makedirs(_pool)
        # Plant a real .deb-named file (content arbitrary; we just need
        # a stable sha256).  Two outputs to confirm the walker picks
        # both.
        _f1 = os.path.join(_pool, 'libfoo_1.0-1_amd64.deb')
        _f2 = os.path.join(_pool, 'libfoo-dev_1.0-1_amd64.deb')
        with open(_f1, 'wb') as _fh:
            _fh.write(b'fake-deb-1')
        with open(_f2, 'wb') as _fh:
            _fh.write(b'fake-deb-2')
        # Plant a v1 record (no output_hashes) on disk.
        _legacy = {
            'schema_version': 1, 'package': 'libfoo',
            'intended_version': '1.0-1', 'built_version': '1.0-1',
            'patch_set_hash': 'h', 'phase': 'done', 'status': 'PASS',
            'started': '2026-06-02T14:00:00Z',
            'finished': '2026-06-02T14:01:00Z',
            'elapsed_seconds': 60.0, 'exit_code': 0, 'oom_killed': False,
            'output_count': 2,
            'outputs': [
                'libfoo-dev_1.0-1_amd64.deb',
                'libfoo_1.0-1_amd64.deb',
            ],
        }
        _key = _u._load_or_create_hmac_key(_log)
        _signed = _u._sign_record(_legacy, _key)
        with open(_u._build_record_path(_log, 'libfoo'), 'wb') as _fh:
            _fh.write(_json.dumps(_signed, sort_keys=True, indent=2).encode())
        # First backfill: should upgrade.
        _stats = _u.backfill_output_hashes(_log, _repo)
        assert _stats['scanned'] == 1
        assert _stats['upgraded'] == 1
        assert _stats['skipped'] == 0
        assert _stats['missing_files'] == 0
        # Verify the upgraded record.
        _rec = _u.read_build_record(_log, 'libfoo')
        assert _rec is not None
        assert _rec['schema_version'] == 6
        import hashlib as _hashlib
        _h1 = _hashlib.sha256(b'fake-deb-1').hexdigest()
        _h2 = _hashlib.sha256(b'fake-deb-2').hexdigest()
        assert _rec['output_hashes'] == {
            'libfoo_1.0-1_amd64.deb': _h1,
            'libfoo-dev_1.0-1_amd64.deb': _h2,
        }
        # MIRROR-02 v3: republished_from defaults to {} on backfill
        assert _rec['republished_from'] == {}
        assert _rec['pulled_from'] is None
        # Second backfill: idempotent — nothing to upgrade.
        _stats2 = _u.backfill_output_hashes(_log, _repo)
        assert _stats2['scanned'] == 1
        assert _stats2['upgraded'] == 0
        assert _stats2['skipped'] == 1



def test_backfill_output_hashes_reports_missing_files():
    """When a record's output is absent from repo_root, the helper still
    bumps schema_version (so consumers don't keep retrying) but flags
    the missing file in stats."""
    _u = _utils_module()
    import json as _json
    with tempfile.TemporaryDirectory() as _td:
        _log = os.path.join(_td, 'log')
        _repo = os.path.join(_td, 'repo')
        os.makedirs(_log)
        os.makedirs(_repo)
        _legacy = {
            'schema_version': 1, 'package': 'ghost',
            'intended_version': '1.0', 'built_version': '1.0',
            'patch_set_hash': '', 'phase': 'done', 'status': 'PASS',
            'started': 's', 'finished': 'f', 'elapsed_seconds': 1.0,
            'exit_code': 0, 'oom_killed': False, 'output_count': 1,
            'outputs': ['ghost_1.0_amd64.deb'],
        }
        _key = _u._load_or_create_hmac_key(_log)
        with open(_u._build_record_path(_log, 'ghost'), 'wb') as _fh:
            _fh.write(_json.dumps(_u._sign_record(_legacy, _key),
                                  sort_keys=True, indent=2).encode())
        _stats = _u.backfill_output_hashes(_log, _repo)
        assert _stats['missing_files'] == 1
        assert _stats['upgraded'] == 1
        _rec = _u.read_build_record(_log, 'ghost')
        assert _rec is not None
        assert _rec['schema_version'] == 6
        assert _rec['output_hashes'] == {}  # nothing on disk to hash
        assert _rec['republished_from'] == {}  # MIRROR-02 v3 default
        assert _rec['pulled_from'] is None    # MIRROR-02 v3 default
        assert _rec['lifecycle_v'] == 1       # LEDGER-01 v4 default
        assert _rec['history'] == []          # LEDGER-01 v4 default



# ─────────────────────────────────────────────────────────────────────────────
# LEDGER-01 — build.json super schema (lifecycle layer)
# ─────────────────────────────────────────────────────────────────────────────

def test_upsert_build_record_creates_selected_classified_missing():
    """upsert on an absent record synthesizes a phase='selected' lifecycle
    record that HMAC round-trips and classifies 'missing' — so publish/
    check_build/audit consumers treat it exactly like no record."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    with tempfile.TemporaryDirectory() as _root:
        _rec = _u.upsert_build_record(
            _root, 'newpkg',
            selection='selected', selected_version='1.0-1',
            snapshot='20260602T173733Z', selected_at='2026-06-10T00:00:00Z')
        assert _rec['phase'] == 'selected'
        assert _rec['lifecycle_v'] == 1 and _rec['history'] == []
        assert _rec['selected_version'] == '1.0-1'
        _back = _u.read_build_record(_root, 'newpkg')
        assert _back is not None, "HMAC round-trip failed"
        assert _u.classify_build_record(_back) == 'missing'
        # intended_version seeded from selected_version on synthesis
        assert _back['intended_version'] == '1.0-1'



def test_upsert_build_record_preserves_build_fields_on_existing():
    """upsert on an EXISTING done record overlays lifecycle fields without
    touching build fields (phase/outputs/hashes), and re-signs."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    with tempfile.TemporaryDirectory() as _root:
        _rec = _u.new_build_record(package='foo', intended_version='1.0-1',
                                   patch_set_hash='abc')
        _rec.update({'phase': 'done', 'status': 'PASS',
                     'built_version': '1.0-1',
                     'outputs': ['foo_1.0-1_amd64.deb'],
                     'output_hashes': {'foo_1.0-1_amd64.deb': 'aa'}})
        _u.write_build_record(_root, _rec)
        _up = _u.upsert_build_record(
            _root, 'foo', selection='selected', selected_version='1.0-1',
            snapshot='snap')
        assert _up['phase'] == 'done' and _up['status'] == 'PASS'
        assert _up['outputs'] == ['foo_1.0-1_amd64.deb']
        assert _up['selection'] == 'selected'
        assert _u.classify_build_record(_u.read_build_record(_root, 'foo')) == 'ok'



def test_preserve_lifecycle_and_roll_history():
    """preserve_lifecycle carries the lifecycle layer through a record
    re-creation; roll_lifecycle_history snapshots the current episode."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    _old = {'selection': 'selected', 'selected_version': '1.0-1',
            'snapshot': 's1', 'selected_at': 't0', 'lifecycle_v': 1,
            'history': [{'state': 'obsolete', 'version': '0.9'}],
            'built_version': '1.0-1', 'outputs': ['a.deb']}
    _new = _u.new_build_record(package='foo', intended_version='1.1-1',
                               patch_set_hash='')
    # new_build_record seeds the EMPTY baseline (lifecycle_v=1, history=[]);
    # preserve must still carry the OLD lived history over that baseline.
    _merged = _u.preserve_lifecycle(_old, _new)
    assert _merged['selection'] == 'selected'
    assert _merged['history'] == [{'state': 'obsolete', 'version': '0.9'}]
    assert _merged['selected_version'] == '1.0-1'
    # roll: current episode → history with build info snapshotted
    _u.roll_lifecycle_history(_merged, state='obsolete', now='t1')
    assert len(_merged['history']) == 2
    _ep = _merged['history'][-1]
    assert _ep['state'] == 'obsolete' and _ep['rolled_at'] == 't1'
    assert _ep['version'] == '1.0-1' and _ep['outputs'] == []
    # preserve with old=None is a no-op
    assert _u.preserve_lifecycle(None, {'x': 1}) == {'x': 1}



def test_record_phase_entry_preserves_lifecycle_and_stashes_prior_build():
    """LEDGER-01 Chunk 2: the build-start entry rewrite (via
    _record_phase(initial=...)) carries the parse-stamped lifecycle layer
    AND stashes the prior build episode for the done-phase roll."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    from buildcontainer import BuildContainer
    with tempfile.TemporaryDirectory() as _root:
        # Prior record: built at 1.0-1, lifecycle-stamped by the parse.
        _prior = _u.new_build_record(package='foo', intended_version='1.0-1',
                                     patch_set_hash='old')
        _prior.update({'phase': 'done', 'status': 'PASS',
                       'built_version': '1.0-1+asg1u1',
                       'outputs': ['foo_1.0-1+asg1u1_amd64.deb'],
                       'selection': 'selected', 'selected_version': '1.0-2',
                       'snapshot': 'snapA', 'lifecycle_v': 1,
                       'history': [{'state': 'obsolete', 'version': '0.9'}]})
        _u.write_build_record(_root, _prior)
        # Drive the real entry path with a minimal BuildContainer.
        _bc = BuildContainer.__new__(BuildContainer)
        _bc.buildlog_path = _root
        _entry = _u.new_build_record(package='foo', intended_version='1.0-2',
                                     patch_set_hash='new')
        _bc._record_phase('foo', initial=_entry)
        _rec = _u.read_build_record(_root, 'foo')
        assert _rec is not None and _rec['phase'] == 'entry'
        # lifecycle carried through the rewrite
        assert _rec['selection'] == 'selected'
        assert _rec['selected_version'] == '1.0-2' and _rec['snapshot'] == 'snapA'
        assert _rec['history'] == [{'state': 'obsolete', 'version': '0.9'}]
        # prior build stashed for the done-phase supersession decision
        assert _rec['prior_build'] == {
            'built_version': '1.0-1+asg1u1',
            'outputs': ['foo_1.0-1+asg1u1_amd64.deb']}



def test_roll_prior_build_history_supersede_vs_same_version():
    """A new built_version that DIFFERS rolls an 'obsolete' history entry
    carrying the OLD episode; a same-version rebuild just drops the stash."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    with tempfile.TemporaryDirectory() as _root:
        # supersession: +asg bump u1 → u2
        _rec = _u.new_build_record(package='foo', intended_version='1.0-1',
                                   patch_set_hash='')
        _rec.update({'selected_version': '1.0-1', 'snapshot': 'snapB',
                     'prior_build': {'built_version': '1.0-1+asg1u1',
                                     'outputs': ['foo_1.0-1+asg1u1_amd64.deb']}})
        _u.write_build_record(_root, _rec)
        assert _u.roll_prior_build_history(_root, 'foo', '1.0-1+asg1u2') is True
        _back = _u.read_build_record(_root, 'foo')
        assert 'prior_build' not in _back
        _ep = _back['history'][-1]
        assert _ep['state'] == 'obsolete'
        assert _ep['built_version'] == '1.0-1+asg1u1'
        assert _ep['outputs'] == ['foo_1.0-1+asg1u1_amd64.deb']
        assert _ep['snapshot'] == 'snapB'
        # same-version rebuild: stash dropped, NO history entry
        _rec2 = _u.new_build_record(package='bar', intended_version='2.0',
                                    patch_set_hash='')
        _rec2['prior_build'] = {'built_version': '2.0', 'outputs': ['b.deb']}
        _u.write_build_record(_root, _rec2)
        assert _u.roll_prior_build_history(_root, 'bar', '2.0') is False
        _back2 = _u.read_build_record(_root, 'bar')
        assert 'prior_build' not in _back2 and _back2['history'] == []
        # absent record / absent stash → False, no crash
        assert _u.roll_prior_build_history(_root, 'ghost', '1.0') is False



def test_lifecycle_touch_selected_bootstrap_and_idempotent():
    """First touch creates/stamps records; a second identical touch writes
    NOTHING (steady-state parses must not re-sign 985 records)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    with tempfile.TemporaryDirectory() as _root:
        # one legacy built record + one brand-new source
        _legacy = _u.new_build_record(package='oldpkg',
                                      intended_version='1.0', patch_set_hash='')
        _legacy.update({'phase': 'done', 'status': 'PASS',
                        'built_version': '1.0'})
        # strip the v4 baseline to simulate a true legacy record
        _legacy.pop('lifecycle_v'); _legacy.pop('history')
        _u.write_build_record(_root, _legacy)
        _sel = {'oldpkg': '1.0', 'newpkg': '2.0'}
        _st = _u.lifecycle_touch_selected(_root, _sel, 'snapA')
        assert _st['created'] == 1 and _st['stamped'] == 1, _st
        _old = _u.read_build_record(_root, 'oldpkg')
        assert _old['selection'] == 'selected'
        assert _old['selected_version'] == '1.0' and _old['snapshot'] == 'snapA'
        assert _u.classify_build_record(_old) == 'ok'      # build state intact
        _new = _u.read_build_record(_root, 'newpkg')
        assert _new['phase'] == 'selected'
        assert _u.classify_build_record(_new) == 'missing'
        # steady state: identical touch → all unchanged, mtimes stable
        _m1 = {p: os.path.getmtime(os.path.join(_root, p))
               for p in os.listdir(_root) if p.endswith('.build.json')}
        _st2 = _u.lifecycle_touch_selected(_root, _sel, 'snapA')
        assert _st2['unchanged'] == 2 and sum(
            _v for _k, _v in _st2.items() if _k != 'unchanged') == 0, _st2
        _m2 = {p: os.path.getmtime(os.path.join(_root, p))
               for p in os.listdir(_root) if p.endswith('.build.json')}
        assert _m1 == _m2, "steady-state touch re-wrote records"



def test_lifecycle_touch_version_change_rolls_obsolete():
    """A snapshot move (selected_version changes) rolls the old episode into
    history as 'obsolete' and updates the current fields."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    with tempfile.TemporaryDirectory() as _root:
        _u.lifecycle_touch_selected(_root, {'foo': '1.0-1'}, 'snapA')
        # build completes at 1.0-1
        _u.update_build_record(_root, 'foo', phase='done', status='PASS',
                               built_version='1.0-1',
                               outputs=['foo_1.0-1_amd64.deb'])
        # snapshot moves → 1.0-2
        _st = _u.lifecycle_touch_selected(_root, {'foo': '1.0-2'}, 'snapB')
        assert _st['rolled'] == 1, _st
        _rec = _u.read_build_record(_root, 'foo')
        assert _rec['selected_version'] == '1.0-2'
        assert _rec['snapshot'] == 'snapB'
        _ep = _rec['history'][-1]
        assert _ep['state'] == 'obsolete'
        assert _ep['version'] == '1.0-1' and _ep['snapshot'] == 'snapA'
        assert _ep['built_version'] == '1.0-1'
        assert _ep['outputs'] == ['foo_1.0-1_amd64.deb']
        # build state untouched by the parse roll — rebuild decided elsewhere
        assert _rec['phase'] == 'done' and _rec['built_version'] == '1.0-1'



def test_lifecycle_deprecate_then_reselect_round_trip():
    """Intent-at-accept marks deprecated; re-selection archives the episode
    and returns to selected."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    with tempfile.TemporaryDirectory() as _root:
        _u.lifecycle_touch_selected(_root, {'foo': '1.0'}, 'snapA')
        assert _u.lifecycle_mark_deprecated(_root, {'foo'}, 'snapA') == 1
        _rec = _u.read_build_record(_root, 'foo')
        assert _rec['selection'] == 'deprecated'
        assert _rec['deprecated_at']
        _dep_at = _rec['deprecated_at']
        # re-selection (operator re-adds the package, next parse)
        _st = _u.lifecycle_touch_selected(_root, {'foo': '1.0'}, 'snapB')
        assert _st['reselected'] == 1, _st
        _rec = _u.read_build_record(_root, 'foo')
        assert _rec['selection'] == 'selected'
        assert _rec['deprecated_at'] is None
        _ep = _rec['history'][-1]
        assert _ep['state'] == 'deprecated'
        assert _ep['deprecated_at'] == _dep_at   # episode archived
        # deprecating an ABSENT record synthesizes one (still works)
        assert _u.lifecycle_mark_deprecated(_root, {'ghost'}, 'snapB') == 1
        assert _u.read_build_record(_root, 'ghost')['selection'] == 'deprecated'



def test_write_snapshot_sources_localmirror_override():
    """_write_snapshot_sources_cmd's `localmirror` override emits/omits the
    file:///localmirror source independent of the container's own
    _localmirror_active — the per-remote hook (None falls back to the flag)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer

    class _M:
        suite = 'bookworm'
        component = 'main'
        url = 'http://snapshot.debian.org/archive/debian/20260101T000000Z'

    def _gen(active, override):
        _stub = type('S', (), {
            'config': type('C', (), {
                'snapshot_baseurl': 'http://snapshot.debian.org/archive'})(),
            'mirrors': [_M()],
            '_localmirror_active': active,
        })()
        return buildcontainer.BuildContainer._write_snapshot_sources_cmd(
            _stub, localmirror=override)
    assert 'file:///localmirror' in _gen(False, True)      # override on
    assert 'file:///localmirror' not in _gen(True, False)  # override off
    assert 'file:///localmirror' in _gen(True, None)       # None → flag
    assert 'file:///localmirror' not in _gen(False, None)



def test_buildlog_write_survives_ascii_locale():
    """Audit #35: BuildLog.write opens with explicit encoding='utf-8', so
    non-ASCII glyphs ('→','…') are written even when the locale's preferred
    encoding is ASCII (which would otherwise raise UnicodeEncodeError)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import locale as _locale
    import buildlog as _bl
    with tempfile.TemporaryDirectory() as _d:
        _p = os.path.join(_d, 'x.buildlog')
        _log = object.__new__(_bl.BuildLog)
        _log._path = _p
        _log._lines = ['relocation → done', 'truncated …']
        _orig = _locale.getpreferredencoding
        _locale.getpreferredencoding = lambda *a, **k: 'ascii'
        try:
            _log.write()
        finally:
            _locale.getpreferredencoding = _orig
        with open(_p, encoding='utf-8') as _fh:
            _txt = _fh.read()
        assert '→' in _txt and '…' in _txt



def test_tunnel_filenames_for_source_picks_highest_version():
    """Audit #85/#14: _tunnel_filenames_for_source falls to the HIGHEST-version
    source (max by version), never source_hashtable parse order, so a
    multi-version source enumerates the selected version's binary set (incl.
    a binary the newer version renamed/added)."""
    from unittest import mock
    import types as _t
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from commands import cohorts
    import virtual_build as _vb
    _older = _t.SimpleNamespace(version='1.0', binary=['libx'])
    _newer = _t.SimpleNamespace(version='2.0', binary=['libx', 'libx-extra'])
    _self = _t.SimpleNamespace(
        cache=_t.SimpleNamespace(source_hashtable={'x': [_older, _newer]}),
        dep_tree=_t.SimpleNamespace(selected_srcs={}),
        udeb_dep_tree=None,
        config=_t.SimpleNamespace(arch='amd64', build_profiles=frozenset(),
                                  dir_fork_source_repo=None),
        _resolve_tunnel_filename=lambda _bin, _entry: _bin + '_2.0_amd64.deb')
    with mock.patch.object(_vb, '_package_list_index', lambda *a, **k: {}), \
            mock.patch.object(_vb, '_binary_active_for_arch',
                              lambda *a, **k: True), \
            mock.patch.object(_vb, '_binary_active_under_profiles',
                              lambda *a, **k: True):
        _fns = cohorts.CohortResolverMixin._tunnel_filenames_for_source(
            _self, 'x')
    assert 'libx-extra_2.0_amd64.deb' in _fns, _fns   # newer source's binary
    assert 'libx_2.0_amd64.deb' in _fns



def test_buildcontainer_segregate_rollback_keeps_published_dup():
    """Audit #32/#7: the segregate rollback tracks a kept-existing published
    duplicate in _kept_existing (NOT _moved_paths), and the rollback loop
    reverses only _moved_paths - so a data-loss relocation of a published .deb
    on a multi-binary rebuild failure can't happen."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import buildcontainer
    _src = inspect.getsource(buildcontainer)
    assert '_kept_existing' in _src and '_moved_paths' in _src
    # the collision branch appends the dup to _kept_existing, not _moved_paths
    assert '_kept_existing.append(_dst)' in _src
    # rollback is driven by _moved_paths (the genuinely-moved set)
    assert 'rolling back' in _src and 'len(_moved_paths)' in _src



def test_render_install_cmd_shlex_quotes_pkg_names():
    """MAT-04: build-dep names are shlex.quoted in the root apt install command
    — a no-op for legit Debian names (their charset has no shell metachars),
    but a name carrying shell metacharacters is neutralized (defense-in-depth
    for an untrusted source's debian/control)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _cmd = buildcontainer.BuildContainer._render_install_cmd(
        ['libssl-dev', 'evil; rm -rf /'],
        [['gawk', 'mawk|hax']], simulate=False)
    # legit name is unquoted (rendered command identical to pre-MAT-04)
    assert 'install libssl-dev ' in _cmd, _cmd
    # a metachar-laden plain dep is single-quoted, not left bare
    assert "'evil; rm -rf /'" in _cmd, _cmd
    assert 'install evil; rm -rf /' not in _cmd, _cmd
    # OR-group alternative with a metachar is quoted too
    assert "'mawk|hax'" in _cmd, _cmd
    assert ' gawk ' in _cmd or _cmd.rstrip().endswith('gawk')  # legit alt bare

def test_d4b_base_check_refuses_native_rootfs():
    """MAT-02 D4b: the build container's base rootfs is checked before the
    image build — a DEBIAN base passes (transpose mode correct), the NATIVE
    distribution's base raises (running the transpose pipeline over already-
    +asg inputs), and a base without os-release is tolerated with a warning
    (don't brick exotic bases on the guard's account)."""
    import io
    import tarfile
    import tempfile
    import types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from buildcontainer import BuildContainer

    def _mk_tar(path, os_release=None):
        with tarfile.open(path, 'w') as _t:
            if os_release is not None:
                _data = os_release.encode()
                _info = tarfile.TarInfo('./etc/os-release')
                _info.size = len(_data)
                _t.addfile(_info, io.BytesIO(_data))
            _info2 = tarfile.TarInfo('./etc/hostname')
            _info2.size = 2
            _t.addfile(_info2, io.BytesIO(b'x\n'))

    _cfg = types.SimpleNamespace(build_base_id='asgard')
    with tempfile.TemporaryDirectory() as _tmp:
        _deb = os.path.join(_tmp, 'deb.tar')
        _mk_tar(_deb, 'PRETTY_NAME="Debian 12"\nID=debian\n')
        BuildContainer._assert_nonnative_base(_deb, _cfg)   # no raise
        _asg = os.path.join(_tmp, 'asg.tar')
        _mk_tar(_asg, 'PRETTY_NAME="Asgard 1"\nID=asgard\nID_LIKE=debian\n')
        try:
            BuildContainer._assert_nonnative_base(_asg, _cfg)
            raise AssertionError('native base must raise')
        except RuntimeError as _e:
            assert 'NATIVE' in str(_e)
        _bare = os.path.join(_tmp, 'bare.tar')
        _mk_tar(_bare, None)
        BuildContainer._assert_nonnative_base(_bare, _cfg)  # tolerated


TESTS = [
    test_d4b_base_check_refuses_native_rootfs,
    test_write_snapshot_sources_localmirror_override,
    test_container_release_defaults_to_release_unless_overridden,
    test_recipe_only_container_skips_local_docker,
    test_tunnel_filenames_prefers_resolved_source_not_parse_order,
    test_render_install_cmd_uses_no_install_recommends,
    test_docker_server_guard_accepts_safe_targets,
    test_docker_server_guard_refuses_unsafe_targets,
    test_arch16_buildcontainer_consults_per_pkg_overrides,
    test_sec05_render_install_cmd_injects_simulate_flag,
    test_sec05_build_gates_on_audit_when_enabled_in_source,
    test_sec05_gate_short_circuits_when_no_deps,
    test_compose_recipe_assembles_image_args_and_cmd_str,
    test_compose_recipe_prebuild_sourced_hashed_and_mounted,
    test_sec05_gate_returns_false_when_operator_declines,
    test_sec05_gate_returns_false_when_preview_infrastructure_fails,
    test_comp03_buildconfig_defaults_for_new_keys,
    test_comp03_buildconfig_parses_explicit_values,
    test_comp03_max_parallel_builds_capped_at_8,
    test_comp03_max_parallel_builds_rejects_zero,
    test_comp03_buildcpus_rejects_negative,
    test_comp03_audit_build_deps_and_parallel_mutex_fails_closed,
    test_comp03_audit_build_deps_with_serial_is_allowed,
    test_check_dep3_header_clean_patch_returns_empty,
    test_check_dep3_header_missing_origin_returns_field,
    test_check_dep3_header_subject_satisfies_description,
    test_buildcontainer_injects_athena_codename_env,
    test_buildcontainer_emits_token_substitution_snippet,
    test_buildcontainer_token_subst_uses_if_not_short_circuit_and,
    test_buildcontainer_token_subst_no_double_braces_in_regular_strings,
    test_buildcontainer_token_subst_grep_rescue_or_true,
    test_buildcontainer_changelog_uses_codename_field,
    test_conf15_dockerfile_pins_toolchain_to_snapshot,
    test_base_rootfs_bootstrap_command_and_cache,
    test_image_build_stages_rootfs_context_not_config_dir,
    test_conf15_buildcontainer_image_tag_carries_snapshot_ts,
    test_mat04_build_container_mounts_source_and_patch_readonly,
    test_conf15_buildcontainer_buildargs_pass_snapshot_triplet,
    test_sta46_was_patched_keys_on_patch_files_not_dir_existence,
    test_sta51_snapshot_pin_origin_tracks_baseurl_host,
    test_conf15_per_build_dist_upgrade_workaround_removed,
    test_cohort_resolvers_route_through_canonical_names,
    test_source_build_pkg_subset_excludes_live_installer_extras,
    test_source_build_installer_subset_unions_udeb_tree_with_deb_arm,
    test_source_build_all_selection_unions_both_trees_no_exclusions,
    test_buildcontainer_build_signature_accepts_profile_override_kwargs,
    test_resolve_live_cohort_subtracts_pool_and_installer,
    test_resolve_installer_cohort_uses_udeb_tree,
    test_mat10c_bump_decision_parity_triggers,
    test_verify_pkg_artifact_ok_when_all_fields_match,
    test_verify_pkg_artifact_fails_on_internal_version_mismatch,
    test_verify_pkg_artifact_fails_on_unsatisfied_depends,
    test_verify_pkg_artifact_passes_on_satisfied_depends,
    test_verify_pkg_artifact_strips_epoch_from_internal_version,
    test_verify_pkg_artifact_resolves_udeb_deps_via_udeb_hashtable,
    test_verify_pkg_artifact_substvar_skip_logic_present,
    test_verify_pkg_artifact_or_group_satisfied_by_any_alternative,
    test_validate_canonical_attribution_tunnel_and_build_config,
    test_validate_transpose_prediction_matches_built_asg,
    test_obs02_record_phase_archives_prior_run_before_overwrite,
    test_upsert_build_record_creates_selected_classified_missing,
    test_upsert_build_record_preserves_build_fields_on_existing,
    test_preserve_lifecycle_and_roll_history,
    test_record_phase_entry_preserves_lifecycle_and_stashes_prior_build,
    test_roll_prior_build_history_supersede_vs_same_version,
    test_lifecycle_touch_selected_bootstrap_and_idempotent,
    test_lifecycle_touch_version_change_rolls_obsolete,
    test_lifecycle_deprecate_then_reselect_round_trip,
    test_verify_output_hashes_flags_only_present_drift_not_pruned_absent,
    test_docker_wait_for_exit_survives_read_timeouts,
    test_docker_wait_for_exit_survives_raw_urllib3_timeout,
    test_sta32_wait_for_exit_tolerates_transient_reload,
    test_sta32_pid_alive_and_connect_error_tuples,
    test_sta32_connect_and_reap_use_widened_handling,
    test_docker_stream_and_wait_backfills_log_on_timeout,
    test_build_source_cleanup_timeout_does_not_mask_success,
    test_segregate_never_deletes_existing_published_deb,
    test_buildlog_writes_header_sections_and_files,
    test_buildlog_write_never_raises_on_unwritable_dir,
    test_buildlog_write_encodes_utf8_under_ascii_locale,
    test_buildlog_methods_tolerate_bad_input_and_helpers,
    test_segregate_appends_relocate_and_purge_events,
    test_comp03_segregate_signature_takes_source_dir,
    test_comp03_segregate_does_not_read_self_repo_path,
    test_comp03_segregate_cross_source_isolation_via_scratch_dir,
    test_comp03_segregate_rolls_back_on_move_failure,
    test_segregate_rollback_never_destroys_kept_existing_published_deb,
    test_comp03_build_uses_per_worker_scratch_dir_in_volume_bind,
    test_comp03_buildconfig_creates_and_validates_dir_build_stage,
    test_comp03_buildcontainer_init_sweeps_build_stage_survivors,
    test_comp03_register_live_adds_under_lock_and_is_idempotent,
    test_comp03_deregister_live_removes_and_tolerates_missing_key,
    test_comp03_all_containers_run_sites_carry_athena_label,
    test_all_containers_run_sites_carry_user_env,
    test_comp03_register_live_called_after_each_containers_run,
    test_comp03_deregister_live_called_in_every_finally,
    test_comp03_startup_orphan_reap_filters_by_athena_label,
    test_comp03_container_labels_include_pid_for_disambiguation,
    test_comp03_reap_all_live_force_removes_every_registered_container,
    test_comp03_reap_all_live_is_idempotent_and_tolerates_api_error,
    test_comp03_request_shutdown_sets_event_and_reaps,
    test_comp03_shutdown_event_initialised_in_init,
    test_comp03_reap_all_live_uses_snapshot_not_live_iteration,
    test_comp03_phase4_parallel_path_uses_thread_pool_executor,
    test_comp03_phase4_serial_path_kept_for_max_parallel_builds_one,
    test_comp03_phase4_installs_scoped_sigint_handler_around_executor,
    test_comp03_phase4_executor_shutdown_uses_wait_true_and_cancel_futures,
    test_comp03_phase4_pool_breaks_on_shutdown_event,
    test_comp03_phase4_tunnel_sub_phase_runs_serial_before_parallel,
    test_comp03_phase5_resource_kwargs_empty_when_uncapped,
    test_comp03_phase5_resource_kwargs_translates_build_cpus_to_nano_cpus,
    test_comp03_phase5_resource_kwargs_passes_mem_limit_string_through,
    test_comp03_phase5_build_passes_resource_kwargs_to_containers_run,
    test_comp03_phase5_exit_137_logs_oomkilled_hint,
    test_comp03_phase6_scheduler_reads_heavy_packages_from_config,
    test_comp03_phase6_heavy_in_flight_blocks_new_submissions,
    test_comp03_phase6_heavy_waits_for_drain_before_starting,
    test_comp03_phase6_uses_cf_wait_first_completed_for_drain,
    test_comp03_phase6_empty_heavy_set_does_not_change_serial_for_heavy,
    test_virtual_arch_gate_dpkg_table_semantics,
    test_virtual_fork_package_list_from_dsc,
    test_virtual_build_synthesize_binary_record_inherits_upstream_deps,
    test_virtual_build_synthesize_record_no_upstream_minimal_skeleton,
    test_virtual_build_sibling_pin_only_rewritten_when_pristine_matches,
    test_virtual_build_sibling_pin_with_nmu_constraint_pristine_matches,
    test_virtual_build_synthesize_source_binaries_end_to_end,
    test_virtual_build_metapackage_uses_binary_upstream_version,
    test_virtual_build_metapackage_per_binary_transpose,
    test_virtual_build_transposes_inherited_depends_constraint,
    test_virtual_build_skips_binaries_from_other_canonical_source,
    test_virtual_build_ambiguous_dedup_emits_warning_and_tracks_name,
    test_virtual_build_unambiguous_dedup_no_ambiguity_warning,
    test_virtual_build_global_pin_rewrite_resolves_cross_source_pin,
    test_virtual_build_absent_target_is_silent,
    test_virtual_build_version_mismatch_stays_critical,
    test_virtual_build_extract_relation_targets_handles_alternatives,
    test_virtual_build_recommends_suppressed_by_default,
    test_virtual_build_canonical_filter_off_when_peer_not_in_scope,
    test_virtual_build_synthesize_repo_state_resolves_closed_graph,
    test_virtual_build_absent_target_emits_no_finding,
    test_virtual_build_synthesize_repo_state_flags_duplicate_name,
    test_virtual_build_no_duplicate_findings_when_no_overlap,
    test_virtual_build_invalid_record_skipped_with_critical,
    test_virtual_build_live_cohort_conflict_critical,
    test_virtual_build_synthesize_claim_ledger_one_per_record,
    test_virtual_publish_dry_run_silent_against_empty_remote,
    test_virtual_build_from_cache_collapses_hashtable_shape,
    test_virtual_validate_canon_map_uses_filtered_universe,
    test_virtual_build_from_cache_merges_udeb_hashtable,
    test_virtual_build_build_profile_filter_skips_nodoc_when_active,
    test_virtual_build_build_profile_filter_positive_profile_required,
    test_virtual_build_delta_uses_source_version_not_per_binary,
    test_virtual_build_per_binary_version_is_intrinsic,
    test_virtual_build_transpose_preserves_epoch,
    test_virtual_build_synthesize_source_filters_by_profile,
    test_check_build_matches_asg_variant_of_prediction,
    test_check_build_locates_non_main_component_deb,
    test_normalize_built_artifacts_transposes_deb_to_asg_no_ledger,
    test_normalize_built_artifacts_transpose_has_no_lineage_trigger,
    test_normalize_built_artifacts_no_stamp_when_ledger_has_no_lineage,
    test_normalize_built_artifacts_patch_on_pristine_gets_p1_no_ledger,
    test_normalize_built_artifacts_cross_source_dep_is_transposed,
    test_postbuild_convergence_hard_fails_when_check_build_still_false,
    test_source_build_autodetects_update_mode,
    test_build_record_schema_v3_field_set,
    test_obs03_container_cpu_pct_and_sampler_wired,
    test_build_record_hmac_round_trip,
    test_build_record_tamper_detection,
    test_build_record_hmac_key_mode_0600,
    test_build_record_atomic_write_no_temp_left_behind_on_success,
    test_build_record_phase_transition_to_done_sets_status_pass,
    test_build_record_phase_transition_to_failed_sets_status_fail,
    test_build_record_phase_tunneled_sets_status_tunneled,
    test_classify_build_record_full_matrix,
    test_build_record_intended_vs_built_version_drift_visible,
    test_normalize_built_artifacts_returns_post_rename_paths,
    test_normalize_built_artifacts_uniform_p_across_siblings,
    test_normalize_built_artifacts_cross_base_sibling_pin_gets_patch_level,
    test_new_build_record_threads_component_field,
    test_build_record_canonical_json_stable_across_key_order,
    test_build_record_output_hashes_round_trip,
    test_build_record_v1_legacy_record_reads_tolerantly,
    test_backfill_output_hashes_upgrades_v1_record,
    test_backfill_output_hashes_reports_missing_files,
    test_strip_nmu_from_built_artifacts_does_not_scan_repo,
    test_buildcontainer_run_grub_mkrescue_constructs_correct_docker_call,
    test_run_grub_mkrescue_mounts_localmirror_when_active,
    test_buildcontainer_run_grub_mkrescue_propagates_failure,
    test_worker_exception_surfaces_console_fail,
    test_source_audit_uses_per_source_progress_bar,
    test_tier3_cleanup_source_pins,
    test_cmd_source_audit_pool_remediation_is_runnable,
    test_buildlog_write_survives_ascii_locale,
    test_render_install_cmd_shlex_quotes_pkg_names,
    test_tunnel_filenames_for_source_picks_highest_version,
    test_buildcontainer_segregate_rollback_keeps_published_dup,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
