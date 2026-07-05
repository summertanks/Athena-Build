"""Athena tests — session, CLI and command dispatch (build.py, cli.py, cmd_run.py, print_commands.py).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile
import textwrap
import threading

from _test_helpers import (  # noqa: F401
    _BASE_CONF_BODY,
    _FakePkg,
    _PrintSessionStub,
    _ROOT,
    _StubConsole,
    _StubDockerClient,
    _StubDockerContainer,
    _StubDockerImage,
    _bare_buildsystem_with_deps,
    _build_autorun_session_stub,
    _build_config_from,
    _build_minimal_deb,
    _capture_console_print,
    _fresh_cli,
    _make_offline_cache,
    _session_source,
    _stub_session_for_signing_gate,
    _stub_tui,
    _v2_fake_renderer,
    _write_test_config,
)




def test_startup_banner_runs_config_check():
    """Startup banner is now `config check` (build identity + mirror
    reachability) — the old static Arch/Parent/Build/Mode header is retired."""
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as _fh:
        _src = _fh.read()
    assert "session.cmd_config('check')" in _src
    assert 'Starting Athena Build System' not in _src
    assert 'Parent Distribution' not in _src



def test_cmd_run_setters_survive_local_conf_write_failure():
    """Regression (audit #71): the machine-local `set` setters must not abort
    on a local.conf write failure — the in-memory value is already applied,
    only durability is lost (mirroring _set_mode).  A raising write_local_conf
    is caught and surfaced as a warning, not bubbled to the dispatcher."""
    import sys
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    import commands.cmd_run as _cr

    _sess = BuildSession.__new__(BuildSession)

    class _Cfg:
        pass
    _sess.config = _Cfg()
    with mock.patch.object(_cr, 'console'), \
         mock.patch.object(_cr.utils, 'write_local_conf',
                           side_effect=OSError('disk full')):
        # the helper returns a warning suffix, never raises
        _suffix = _sess._persist_local(name='x')
        assert 'could not persist' in _suffix.lower(), _suffix
        # a full setter swallows the OSError AND still applies the live value
        _sess._set_name('athena-x')
        assert _sess.config.system_name == 'athena-x'
        _sess._set_jobs('4')
        assert _sess.config.max_parallel_builds == 4



def test_print_state_shows_mode_header():
    """MIRROR-02 chunk 6b: `print state` surfaces the active build
    mode at the top of the output.  In build mode the line also shows
    the build_pkg.list pkg count."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui
    import print_commands

    class _Flags:
        cache_ready = True
        dep_check_ready = True
        download_ready = False
        build_container_ready = False
        source_build_ready = False
        signing_key_verified = False
        chroot_ready = False
        chroot_verified = False
        chroot_installer_ready = False
        chroot_disk_ready = False
        iso_live_ready = False
        iso_installer_ready = False
        iso_disk_ready = False

    with tempfile.TemporaryDirectory() as _td:
        _indl = os.path.join(_td, 'build_pkg.list')
        with open(_indl, 'w') as _fh:
            _fh.write('firefox-esr\nlibreoffice\nthunderbird\n')

        class _CfgIndl:
            build_mode = 'build'
            build_pkg_list_path = _indl
        class _SessIndl:
            flags = _Flags()
            config = _CfgIndl()

        _lines: 'list[str]' = []
        _orig = tui.console.print
        tui.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            print_commands._print_state(_SessIndl())
        finally:
            tui.console.print = _orig
        _joined = '\n'.join(_lines)
        assert 'MODE: build' in _joined, _joined
        assert '3 pkg(s)' in _joined, _joined

        # Dist mode case
        class _CfgDist:
            build_mode = 'distribution'
            build_pkg_list_path = _indl
        class _SessDist:
            flags = _Flags()
            config = _CfgDist()
        _lines = []
        tui.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            print_commands._print_state(_SessDist())
        finally:
            tui.console.print = _orig
        _joined = '\n'.join(_lines)
        assert 'MODE: distribution' in _joined, _joined



def test_cmd_auto_run_dispatch_routes_build_mode():
    """MIRROR-02 chunk 6: `autorun build` routes to
    cmd_auto_run_build.  Bare `autorun` in build mode also routes
    there.  Dist mode bare `autorun` still routes to live (back-compat)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_auto_run_live = lambda *a, **k: _calls.append('live')      # type: ignore
    _sess.cmd_auto_run_installer = lambda *a, **k: _calls.append('inst') # type: ignore
    _sess.cmd_auto_run_disk = lambda *a, **k: _calls.append('disk')      # type: ignore
    _sess.cmd_auto_run_build = lambda *a, **k: _calls.append('indl')# type: ignore

    class _CfgDist:
        build_mode = 'distribution'
    class _CfgIndl:
        build_mode = 'build'

    # Explicit `autorun build` → indl runner (mode-agnostic
    # dispatcher; cmd_auto_run_build itself enforces the gate)
    _sess.config = _CfgDist()
    _calls.clear()
    _sess.cmd_auto_run('build')
    assert _calls == ['indl'], _calls

    # Bare `autorun` in dist mode → live (back-compat preserved)
    _calls.clear()
    _sess.cmd_auto_run('')
    assert _calls == ['live'], _calls

    # Bare `autorun` in build mode → indl runner (mode-driven default)
    _sess.config = _CfgIndl()
    _calls.clear()
    _sess.cmd_auto_run('')
    assert _calls == ['indl'], _calls



def test_cmd_auto_run_build_refuses_in_dist_mode():
    """`autorun build` requires Mode = build; rejected
    otherwise with an actionable hint."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    class _Cfg:
        build_mode = 'distribution'
    _sess = BuildSession.__new__(BuildSession)
    _sess.config = _Cfg()
    _lines: 'list[str]' = []
    _orig = build.console.print
    build.console.print = lambda *a, **k: _lines.append(
        ' '.join(str(x) for x in a))
    try:
        _sess.cmd_auto_run_build()
    finally:
        build.console.print = _orig
    _joined = '\n'.join(_lines)
    assert 'requires `[Build] Mode = build`' in _joined, _joined
    assert 'autorun live' in _joined or 'autorun live`/' in _joined, _joined



def test_sta34_autorun_build_calls_source_build_bare_not_invalid_token():
    """STA-34: the final autorun-build step must call cmd_source_build with
    NO args (the 'pkg' subset, relabelled 'indl' in build mode) — never
    cmd_source_build('build'), which is not a valid subset token and gets
    classified as a package name ("Unknown package: build"), aborting the
    pipeline at the last step."""
    import inspect, sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _src = inspect.getsource(BuildSession.cmd_auto_run_build)
    # Must NOT pass the invalid 'build' subset token...
    assert "cmd_source_build('build')" not in _src, (
        "autorun build must NOT pass the invalid 'build' subset token")
    assert 'cmd_source_build("build")' not in _src, _src
    # ...and the step must reference cmd_source_build BARE (no-arg call →
    # 'pkg' subset, relabelled 'indl' in build mode).
    assert '(self.cmd_source_build,' in _src, (
        "the final step must be a bare cmd_source_build reference, not a "
        "lambda passing a subset token")



def test_sta36_mirror_add_confirmation_declines_on_no():
    """STA-36: the `mirror add` confirmation must DECLINE on "n"/"no".  A
    YESNO get_response() returns a truthy string for every answer, so the
    old `if not _resp` only caught a missing backend and "n" fell through
    to registration."""
    import inspect, sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _src = inspect.getsource(BuildSession.cmd_mirror_add)
    # The confirmation must test for an explicit yes, not truthiness.
    import re
    _m = re.search(r"\.get_response\(\)\s*\n\s*(.+?)\n", _src)
    assert _m, "confirmation get_response() not found"
    # No `if not _resp:` style truthiness check guarding the abort.
    assert 'if not _resp' not in _src, (
        "STA-36: abort must not key off truthiness (every YESNO answer is "
        "truthy) — use `_resp.lower() not in ('y', 'yes')`")
    assert "not in ('y', 'yes')" in _src or 'not in ("y", "yes")' in _src, _src



def test_tunnel_transposes_and_needs_no_ledger():
    """Under TRANSPOSE the tunnel path transposes each downloaded .deb in place
    (trailing +debNuK → +asg<R>uK, K intrinsic) and therefore needs NO published
    ledger — the ship-order asg_next_n / lineage machinery is gone.  Supersedes
    STA-35 (which loaded published_ledger for the ship-order stamp)."""
    import inspect, sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _src = inspect.getsource(BuildSession._do_tunnel)
    assert 'transpose_deb' in _src, (
        "_do_tunnel must transpose each tunnelled .deb (transpose_deb)")
    assert 'asg_next_n' not in _src and 'restamp_asg_deb' not in _src, (
        "tunnel must not use the ship-order asg_next_n / restamp stack")
    assert 'published_ledger' not in _src, (
        "tunnel no longer needs the ledger (K is intrinsic)")



def test_parse_source_build_args_recognises_indl_subset():
    """`_parse_source_build_args` accepts 'indl' as a recognised
    subset name (mode-gating happens at dispatch time, not in the
    pure parser)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    assert 'indl' in BuildSession._SOURCE_SUBSETS
    _err, _force, _subset, _names, _prof = \
        BuildSession._parse_source_build_args(('indl',))
    assert _err is None, _err
    assert _subset == 'indl'
    assert _names == []
    assert _force is False
    # Mutually exclusive with named packages (same rule as other subsets)
    _err, *_ = BuildSession._parse_source_build_args(
        ('indl', 'foo'))
    assert _err is not None
    assert 'mutually exclusive' in _err



def test_cmd_source_build_indl_subset_rejected_in_dist_mode():
    """`source build indl` is rejected outside build mode with a hint
    pointing at 'all' (the dist-mode equivalent for 'build
    everything')."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession

    class _Cfg:
        build_mode = 'distribution'
        build_profiles: list = []
        build_options: list = []
    class _Flags:
        cache_ready = True
        dep_check_ready = True
        download_ready = True
        build_container_ready = True
    _sess = BuildSession.__new__(BuildSession)
    _sess.config = _Cfg()
    _sess.flags = _Flags()
    _lines: 'list[str]' = []
    _orig = build.console.print
    build.console.print = lambda *a, **k: _lines.append(
        ' '.join(str(x) for x in a))
    try:
        _sess.cmd_source_build('indl')
    finally:
        build.console.print = _orig
    _joined = '\n'.join(_lines)
    assert 'only valid under' in _joined, _joined
    assert "`source build all`" in _joined, _joined



def test_source_audit_naturally_scopes_to_indl_in_build_mode():
    """MIRROR-02 chunk 4: cmd_source_audit walks dep_tree.selected_srcs.
    Chunk 2 populates selected_srcs directly from build_pkg.list in
    build mode (no closure walk).  So source audit naturally
    scopes to the indl subset — no explicit per-mode filter needed.
    Test stubs a BuildSession where selected_srcs has just one
    source, runs cmd_source_audit, asserts the per-state counts add
    up to 1 (not the corpus size)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    class _Flags:
        cache_ready = True
        dep_check_ready = True
    _sess.flags = _Flags()

    # Fake a Source object with the minimum surface cmd_source_audit
    # walks (name + a stable .source_version for predictable hashing).
    class _Src:
        def __init__(self, name):
            self.name = name
            self.source_version = '1.0-1'

    class _DepTree:
        selected_srcs = {'firefox-esr': _Src('firefox-esr')}
        live_exclusive_src_names: set = set()
        installer_exclusive_src_names: set = set()
        pool_extras_src_names: set = set()
        extras_src_names: set = set()
    _sess.dep_tree = _DepTree()
    _sess.udeb_dep_tree = None     # build mode: installer is N/A
    _sess.container = None         # no build container — preflight audit
                                   # block at build.py:7463 is skipped
    # _source_state is a method on BuildSession; for this test just
    # return 'ok' for every source (we're asserting on the count, not
    # the classification).
    _sess._source_state = lambda _n, _s: 'ok'                # type: ignore[method-assign]
    _sess._print_obsolete_patch_warning = lambda _srcs: None # type: ignore[method-assign]
    # cmd_source_audit now consults the published manifest + the UPDATE-mode
    # 'needs_bump' check (_audit_state).  Stub both so this scope test stays
    # isolated: no real ledger file, no re-spin → still classifies 'ok'.
    _sess.config = type('_C', (), {'build_version': '1'})()
    _sess._needs_bump_build = lambda *_a, **_k: False        # type: ignore[method-assign]
    import repo_audit as _ra
    _orig_pl = _ra.published_ledger
    _ra.published_ledger = lambda _cfg: {}

    _lines: 'list[str]' = []
    _orig = build.console.print
    build.console.print = lambda *a, **k: _lines.append(
        ' '.join(str(x) for x in a))
    try:
        _sess.cmd_source_audit()
    finally:
        build.console.print = _orig
        _ra.published_ledger = _orig_pl
    _joined = '\n'.join(_lines)
    # Audit reports the corpus-size totals.  selected_srcs has 1
    # entry → total = 1 (would be hundreds in dist mode).
    assert '1  total' in _joined, _joined
    assert '1  ok' in _joined, _joined



def test_chroot_iso_builds_refuse_in_build_mode():
    """MIRROR-02 chunk 3: every chroot/ISO entry point refuses cleanly
    when [Build] Mode = build.  Verified for all five commands:
    chroot build live/installer, iso build live/installer/disk.  Each
    prints an actionable hint pointing at the mode setting and
    returns without proceeding."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession

    class _Cfg:
        build_mode = 'build'

    _commands = [
        ('cmd_build_chroot_live',       'chroot build live'),
        ('cmd_build_chroot_installer',  'chroot build installer'),
        ('cmd_build_iso_live',          'iso build live'),
        ('cmd_build_iso_installer',     'iso build installer'),
        ('cmd_build_iso_disk',          'iso build disk'),
    ]
    for _method_name, _label in _commands:
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _lines: 'list[str]' = []
        _orig = build.console.print
        # Late-binding guard: bind _lines as a default arg so each
        # loop iteration's lambda points at its own capture.
        build.console.print = lambda *a, _buf=_lines, **k: _buf.append(
            ' '.join(str(x) for x in a))
        try:
            _r = getattr(_sess, _method_name)()
        finally:
            build.console.print = _orig
        _joined = '\n'.join(_lines)
        assert _label in _joined, (_method_name, _joined)
        assert 'N/A in build mode' in _joined, (_method_name, _joined)
        assert '[Build] Mode' in _joined, (_method_name, _joined)
        # None / False both signal "did not proceed"
        assert _r in (None, False), (_method_name, _r)



def test_refuse_in_build_mode_is_a_no_op_in_distribution():
    """The gate helper is silent + returns False when mode is
    distribution — distribution-mode chroot/ISO must work unchanged."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    class _Cfg:
        build_mode = 'distribution'
    _sess = BuildSession.__new__(BuildSession)
    _sess.config = _Cfg()
    _lines: 'list[str]' = []
    _orig = build.console.print
    build.console.print = lambda *a, **k: _lines.append(
        ' '.join(str(x) for x in a))
    try:
        _refused = _sess._refuse_in_build_mode('chroot build live')
    finally:
        build.console.print = _orig
    assert _refused is False
    assert _lines == [], _lines



def test_iso_builds_gate_on_container_up_front():
    """iso build live/installer must refuse BEFORE any staging work when
    the build container is absent.  grub-mkrescue — the LAST mastering
    step — runs inside the container; without an up-front gate the
    operator pays the full ~10-minute pool/squashfs staging before the
    failure surfaces (hit live 2026-06-11)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession

    class _Cfg:
        build_mode = 'distribution'

    class _Flags:
        chroot_verified = True
        chroot_ready = True
        chroot_installer_ready = True
        chroot_disk_ready = True
        dep_check_ready = True  # STA-39 gate sits before the container gate

    for _method_name in ('cmd_build_iso_live', 'cmd_build_iso_installer'):
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess.flags = _Flags()
        _sess.container = None
        _lines: 'list[str]' = []
        _orig = build.console.print
        build.console.print = lambda *a, _buf=_lines, **k: _buf.append(
            ' '.join(str(x) for x in a))
        try:
            _r = getattr(_sess, _method_name)()
        finally:
            build.console.print = _orig
        _joined = '\n'.join(_lines)
        assert 'container local init' in _joined, (_method_name, _joined)
        assert 'grub-mkrescue' in _joined, (_method_name, _joined)
        # None / False both signal "did not proceed"
        assert _r in (None, False), (_method_name, _r)



def test_surface_builds_gate_on_dep_check_ready():
    """STA-39: chroot build live/disk and iso build installer must
    refuse up front when dep_check_ready is False.  The flag is
    in-memory-only (set by a successful `cache parse` this session,
    which also populates self.cache/self.dep_tree); the OTHER gates
    (source_build_ready, chroot_installer_ready) PERSIST across
    sessions, so before this gate a fresh session passed them,
    collected the sudo password, then crashed AttributeError on the
    None session state — and the SELECT-LOCK / stale-file gates were
    silently skipped (hit live 2026-06-13: chroot build live in a
    fresh session reached the proceed prompt with the stale gate
    blind)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession

    class _Cfg:
        build_mode = 'distribution'

    class _Flags:
        dep_check_ready = False          # fresh session
        source_build_ready = True        # persisted from a prior run
        chroot_installer_ready = True    # persisted from a prior run
        chroot_disk_ready = True
        chroot_ready = True
        chroot_verified = True

    for _method_name in ('cmd_build_chroot_live', 'cmd_build_chroot_disk',
                         'cmd_build_iso_installer'):
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess.flags = _Flags()
        _lines: 'list[str]' = []
        _orig = build.console.print
        build.console.print = lambda *a, _buf=_lines, **k: _buf.append(
            ' '.join(str(x) for x in a))
        try:
            _r = getattr(_sess, _method_name)()
        finally:
            build.console.print = _orig
        _joined = '\n'.join(_lines)
        assert 'cache parse' in _joined, (_method_name, _joined)
        assert _r in (None, False), (_method_name, _r)



def test_cache_parse_build_mode_resolves_named_pkgs_only():
    """MIRROR-02 chunk 2: in build mode, cache parse populates
    selected_pkgs directly from build_pkg.list lookups (no transitive
    closure walk).  Cache has firefox-esr + libreoffice + their fake
    Depends; build_pkg.list names just firefox-esr; closure does NOT
    include libreoffice or any of firefox-esr's Depends."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    # Three packages in the offline cache.  firefox-esr Depends on
    # libdep; if build mode walked closure, libdep would land in
    # selected_pkgs.  The test asserts it does NOT.
    _pkg_blob = (
        "Package: firefox-esr\n"
        "Source: firefox-esr\n"
        "Version: 130.0esr-1\n"
        "Architecture: amd64\n"
        "Depends: libdep (>= 1.0)\n"
        "Filename: pool/main/f/firefox-esr/firefox-esr_130.0esr-1_amd64.deb\n"
        "Size: 1\n"
        "SHA256: " + ("a" * 64) + "\n"
        "\n"
        "Package: libreoffice\n"
        "Source: libreoffice\n"
        "Version: 7.4-1\n"
        "Architecture: amd64\n"
        "Filename: pool/main/l/libreoffice/libreoffice_7.4-1_amd64.deb\n"
        "Size: 1\n"
        "SHA256: " + ("b" * 64) + "\n"
        "\n"
        "Package: libdep\n"
        "Source: libdep\n"
        "Version: 1.0-1\n"
        "Architecture: amd64\n"
        "Filename: pool/main/l/libdep/libdep_1.0-1_amd64.deb\n"
        "Size: 1\n"
        "SHA256: " + ("c" * 64) + "\n"
    )
    _src_blob = (
        "Package: firefox-esr\n"
        "Binary: firefox-esr\n"
        "Version: 130.0esr-1\n"
        "Architecture: any\n"
        "Directory: pool/main/f/firefox-esr\n"
        "Checksums-Sha256:\n"
        " " + ("a" * 64) + " 100 firefox-esr_130.0esr-1.dsc\n"
        "\n"
        "Package: libreoffice\n"
        "Binary: libreoffice\n"
        "Version: 7.4-1\n"
        "Architecture: any\n"
        "Directory: pool/main/l/libreoffice\n"
        "Checksums-Sha256:\n"
        " " + ("b" * 64) + " 100 libreoffice_7.4-1.dsc\n"
        "\n"
        "Package: libdep\n"
        "Binary: libdep\n"
        "Version: 1.0-1\n"
        "Architecture: any\n"
        "Directory: pool/main/l/libdep\n"
        "Checksums-Sha256:\n"
        " " + ("c" * 64) + " 100 libdep_1.0-1.dsc\n"
    )
    with tempfile.TemporaryDirectory() as _td:
        _cache_obj = _make_offline_cache(
            _td, packages={'main': _pkg_blob}, sources={'main': _src_blob})
        assert _cache_obj.is_valid, _cache_obj.error_str

        # build_pkg.list lists ONE package
        _indl = os.path.join(_td, 'build_pkg.list')
        with open(_indl, 'w') as _fh:
            _fh.write('firefox-esr\n')

        import dependencytree
        class _Cfg:
            build_pkg_list_path = _indl
            build_mode = 'build'
            arch = 'amd64'
            build_profiles = ['nodoc', 'nocheck']

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess.cache = _cache_obj
        _sess.dep_tree = dependencytree.DependencyTree(
            _cache_obj, select_recommended=False,
            arch='amd64', build_profiles=['nodoc', 'nocheck'])

        _lines: 'list[str]' = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _ok = _sess._cache_parse_build_mode()
        finally:
            build.console.print = _orig
        assert _ok is True, '\n'.join(_lines)
        # selected_pkgs has firefox-esr ONLY (canonical key); libdep
        # would be there if we walked closure, libreoffice would be
        # there if we used the full build_pkg.list.
        _canonical = {
            _k for _k in _sess.dep_tree.selected_pkgs
            if _k == _sess.dep_tree.selected_pkgs[_k]['Package']
        }
        assert _canonical == {'firefox-esr'}, _canonical
        # selected_srcs populated via parse_sources()
        assert 'firefox-esr' in _sess.dep_tree.selected_srcs
        assert 'libreoffice' not in _sess.dep_tree.selected_srcs
        assert 'libdep' not in _sess.dep_tree.selected_srcs



def test_cache_parse_build_mode_warns_on_missing_pkg():
    """Names in build_pkg.list that aren't in the cache get a WARNING and
    are skipped — partial resolution still proceeds for the rest."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    _pkg_blob = (
        "Package: firefox-esr\n"
        "Source: firefox-esr\n"
        "Version: 130.0esr-1\n"
        "Architecture: amd64\n"
        "Filename: pool/main/f/firefox-esr/firefox-esr_130.0esr-1_amd64.deb\n"
        "Size: 1\n"
        "SHA256: " + ("a" * 64) + "\n"
    )
    _src_blob = (
        "Package: firefox-esr\n"
        "Binary: firefox-esr\n"
        "Version: 130.0esr-1\n"
        "Architecture: any\n"
        "Directory: pool/main/f/firefox-esr\n"
        "Checksums-Sha256:\n"
        " " + ("a" * 64) + " 100 firefox-esr_130.0esr-1.dsc\n"
    )
    with tempfile.TemporaryDirectory() as _td:
        _cache_obj = _make_offline_cache(
            _td, packages={'main': _pkg_blob}, sources={'main': _src_blob})
        _indl = os.path.join(_td, 'build_pkg.list')
        with open(_indl, 'w') as _fh:
            _fh.write('firefox-esr\nnonexistent-pkg\n')

        import dependencytree
        class _Cfg:
            build_pkg_list_path = _indl
            build_mode = 'build'
            arch = 'amd64'
            build_profiles = ['nodoc', 'nocheck']
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess.cache = _cache_obj
        _sess.dep_tree = dependencytree.DependencyTree(
            _cache_obj, select_recommended=False,
            arch='amd64', build_profiles=['nodoc', 'nocheck'])
        _lines: 'list[str]' = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _ok = _sess._cache_parse_build_mode()
        finally:
            build.console.print = _orig
        # firefox-esr resolved, nonexistent-pkg surfaced WARNING
        assert _ok is True
        _joined = '\n'.join(_lines)
        assert "'nonexistent-pkg' not in cache" in _joined
        _canonical = {
            _k for _k in _sess.dep_tree.selected_pkgs
            if _k == _sess.dep_tree.selected_pkgs[_k]['Package']
        }
        assert _canonical == {'firefox-esr'}



def test_cache_parse_build_mode_empty_indl_returns_false():
    """Empty/missing build_pkg.list → returns False with a clear warning;
    operator's dep_check_ready stays False."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _td:
        # No build_pkg.list file
        _indl = os.path.join(_td, 'build_pkg.list')
        import dependencytree
        # Minimal cache object — only need the helper not to walk it
        class _FakeCache:
            package_hashtable: dict = {}
        class _Cfg:
            build_pkg_list_path = _indl
            build_mode = 'build'
            arch = 'amd64'
            build_profiles = ['nodoc', 'nocheck']
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _sess.cache = _FakeCache()
        _sess.dep_tree = dependencytree.DependencyTree.__new__(
            dependencytree.DependencyTree)
        _sess.dep_tree.selected_pkgs = {}
        _sess.dep_tree.selected_srcs = {}
        _lines: 'list[str]' = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _ok = _sess._cache_parse_build_mode()
        finally:
            build.console.print = _orig
        assert _ok is False
        _joined = '\n'.join(_lines)
        assert 'empty or missing' in _joined



def test_sta25_cleanup_guards_live_published_claims():
    """STA-25: `repo repair cleanup` must flag obsolete files still named by a
    LIVE published claim (publish-before-prune) — deleting them locally before
    the mirror is told strands a sha the mirror still serves.  A deprecated /
    obsoleted claim is itself a prune signal, so its filename is NOT flagged."""
    import sys as _sys, json as _json, inspect as _inspect
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.schema as _schema
    from build import BuildSession

    def _claim(**kw):
        _base = {
            'builder': 'b1', 'seq': 1, 'package': 'foo',
            'intended_version': '1', 'built_version': '1',
            'filename': 'foo_1_amd64.deb', 'sha256': 'a' * 64,
            'size': 10, 'snapshot': 'S', 'built_at': 't',
        }
        _base.update(kw)
        _c = _schema.new_claim(**_base)
        _c['sig'] = 'test-signature'   # claim_from_jsonl requires the sig key
        return _c

    with tempfile.TemporaryDirectory() as _td:
        _claims = os.path.join(_td, 'claims')
        os.makedirs(_claims)
        _jp = os.path.join(_claims, 'b1.jsonl')
        with open(_jp, 'w') as _fh:
            _fh.write(_json.dumps(
                _claim(claim_state=_schema.CLAIM_STATE_PUBLISHED)) + '\n')

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = type('C', (), {'dir_coord_claims': _claims})()
        # a live published claim → its filename is flagged
        assert _sess._live_published_claim_filenames() == {'foo_1_amd64.deb'}

        # deprecate it → folded out (prune signal, not flagged)
        _dep = _claim(seq=2, claim_state=_schema.CLAIM_STATE_DEPRECATED)
        _dep['deprecates_seq'] = 1
        with open(_jp, 'a') as _fh:
            _fh.write(_json.dumps(_dep) + '\n')
        assert _sess._live_published_claim_filenames() == set()

        # no coord ledger → empty (non-federated operator, cleanup unaffected)
        _sess.config = type('C', (), {'dir_coord_claims': '/nonexistent'})()
        assert _sess._live_published_claim_filenames() == set()

    # source pin: cleanup computes _claimed + gates the force-delete on it
    _src = _inspect.getsource(BuildSession.cmd_package_cleanup)
    assert '_live_published_claim_filenames()' in _src and '_claimed' in _src
    assert 'publish-before-prune' in _src.lower(), _src



def test_cleanup_publish_before_prune_gate_not_informational_cons14():
    """CONS-14: the publish-before-prune gate in `repo repair cleanup force` is
    NOT informational — `--yes` must not auto-confirm pruning a LIVE-claimed file
    (an ordering hazard); a headless run defaults to 'n' and aborts there.  The
    final IRREVERSIBLE-delete prompt STAYS informational (the gate above already
    aborts any live-claimed target, so an in-order `--yes` prune may proceed)."""
    import inspect as _inspect
    import re as _re
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _src = _inspect.getsource(BuildSession.cmd_package_cleanup)
    # the publish-before-prune Prompt block (LIVE mirror claim → get_response)
    _gate = _re.search(r'LIVE mirror claim.*?\.get_response\(\)',
                       _src, _re.DOTALL)
    assert _gate, "publish-before-prune prompt not found"
    assert 'informational=True' not in _gate.group(0), (
        "publish-before-prune gate must NOT be informational — under --yes it "
        "would auto-prune live-claimed files (CONS-14)")
    # the final IRREVERSIBLE-delete confirm stays informational
    _delete = _re.search(r'DELETE .*?IRREVERSIBLE.*?\.get_response\(\)',
                         _src, _re.DOTALL)
    assert _delete and 'informational=True' in _delete.group(0), (
        "the final delete confirm stays informational (in-order --yes prune)")



def test_sta37_build_chroot_gates_on_incomplete_set():
    """STA-37: build_chroot must RETURN FALSE when a planned package is
    broken or never-installed (the authoritative pass/fail gate the docstring
    promises) so chroot_*_ready isn't set on an incomplete surface — unless
    `no-gate` (gate_complete=False) overrides."""
    import types
    import chroot as chroot_module
    import tui as _tui

    def _run(status_lines, gate_complete):
        bs = _bare_buildsystem_with_deps([('A', [], []), ('B', [], [])])
        bs._dir_chroot = '/nonexistent-chroot'
        bs._password = 'pw'
        _tui.console = _StubConsole()
        bs._setup_chroot_env = lambda: None
        bs._init_dpkg_database = lambda: None
        bs._mount_chroot_fs = lambda: None
        bs._umount_chroot_fs = lambda: True
        bs._ensure_initramfs = lambda: None
        bs.post_install = lambda: None
        bs.generate_system_configs = lambda debug=False: None
        bs._compute_install_batches = (
            lambda seed, install_set=None: [(['A', 'B'], True)])
        bs._unpack_packages = lambda pkgs, quiet=False: set(pkgs)
        bs._configure_packages = lambda pkgs, force_deps=False: set(pkgs)
        bs._configure_chroot = lambda: set()

        class _QProc:
            returncode = 0
            stdout = status_lines
            stderr = ''
        _orig = chroot_module.subprocess
        chroot_module.subprocess = types.SimpleNamespace(
            run=lambda *a, **k: _QProc())
        try:
            return bs.build_chroot(install_set={'A', 'B'},
                                   gate_complete=gate_complete)
        finally:
            chroot_module.subprocess = _orig

    _seed = ('libc6', 'libgcc-s1', 'libcrypt1')
    _ok = lambda pkgs: ''.join(f'{p} install ok installed\n' for p in pkgs)
    # complete (seed + A + B all installed) → True
    assert _run(_ok(_seed + ('A', 'B')), gate_complete=True) is True
    # B never installed (missing) → gate → False
    assert _run(_ok(_seed + ('A',)), gate_complete=True) is False
    # B half-configured (broken) → gate → False
    _broken = _ok(_seed + ('A',)) + 'B half-configured\n'
    assert _run(_broken, gate_complete=True) is False
    # same incomplete set, no-gate escape hatch → proceeds → True
    assert _run(_ok(_seed + ('A',)), gate_complete=False) is True

    # cmd_build threads the no-gate escape hatch into the gate
    import inspect as _inspect
    from build import BuildSession
    for _m in (BuildSession.cmd_build_chroot_live,
               BuildSession.cmd_build_chroot_disk):
        assert 'gate_complete=not _no_gate' in _inspect.getsource(_m), \
            f"{_m.__name__} must gate build_chroot on no-gate"



def test_ux05a_prompt_informational_kwarg_accepted():
    """UX-05a + f: Prompt(prompt_type, msg, informational=True) is a valid
    construction; auto_yes is consulted only for informational YESNO."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.facade import Prompt, PROMPT_YESNO, PROMPT_PASSWORD
    # informational accepted as keyword
    _p = Prompt(PROMPT_YESNO, 'Proceed?', informational=True)
    assert _p._informational is True
    # default is False (back-compat)
    _q = Prompt(PROMPT_YESNO, 'Proceed?')
    assert _q._informational is False
    # PROMPT_PASSWORD ignores informational (hard prompt)
    _r = Prompt(PROMPT_PASSWORD, 'Sudo:', informational=True)
    assert _r._informational is True   # stored, but get_response won't honour for non-YESNO



def test_ux05a_auto_yes_short_circuits_informational_yesno():
    """UX-05a + f: with backend.auto_yes=True, an informational YESNO
    returns 'y' immediately without prompting; a non-informational YESNO
    still waits."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui as _tui
    from tui.facade import Prompt, PROMPT_YESNO

    class _StubBackend:
        auto_yes = True
        def __init__(self):
            self.prompt_called = False
        def prompt(self, message='', masked=False, keymode=False):
            self.prompt_called = True
            return 'n'      # would return 'n' if asked
        def print(self, *_a, **_kw): pass
        # console.print compat
        def INFO(self, *_a, **_kw): pass
        def WARNING(self, *_a, **_kw): pass
        def ERROR(self, *_a, **_kw): pass
        def console_mark(self): return 0
        def console_trim_to(self, mark): pass

    _saved = _tui.tui_instance
    _b = _StubBackend()
    _tui.tui_instance = _b
    try:
        # informational + auto_yes → auto-'y', no prompt() call
        _r = Prompt(
            PROMPT_YESNO, 'Proceed?', informational=True).get_response()
        assert _r == 'y', _r
        assert _b.prompt_called is False
        # non-informational → falls through, prompt() called
        _b.prompt_called = False
        Prompt(PROMPT_YESNO, 'Proceed?').get_response()
        assert _b.prompt_called is True
    finally:
        _tui.tui_instance = _saved



def test_ux05a_auto_yes_does_not_skip_password_or_options():
    """UX-05a + f: --yes must NOT auto-answer hard prompts (PASSWORD,
    OPTIONS, INPUT, PAUSE) — operator input is required for those
    regardless of the flag."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui as _tui
    from tui.facade import (
        Prompt, PROMPT_PASSWORD, PROMPT_OPTIONS, PROMPT_INPUT)

    class _StubBackend:
        auto_yes = True
        def __init__(self):
            self.prompt_called = 0
        def prompt(self, message='', masked=False, keymode=False):
            self.prompt_called += 1
            return 'a'      # valid for the OPTIONS test below
        def print(self, *_a, **_kw): pass
        def INFO(self, *_a, **_kw): pass
        def WARNING(self, *_a, **_kw): pass
        def ERROR(self, *_a, **_kw): pass
        def console_mark(self): return 0
        def console_trim_to(self, mark): pass

    _saved = _tui.tui_instance
    _b = _StubBackend()
    _tui.tui_instance = _b
    try:
        Prompt(PROMPT_PASSWORD, 'Sudo:',
               informational=True).get_response()
        Prompt(PROMPT_INPUT, 'Name:',
               informational=True).get_response()
        Prompt(PROMPT_OPTIONS, 'Pick:', options=['a', 'b'],
               informational=True).get_response()
        # All three should have hit the prompt path (auto-yes is YESNO-only)
        assert _b.prompt_called == 3, _b.prompt_called
    finally:
        _tui.tui_instance = _saved



def test_ux05b_atena_sudo_password_env_var_picked_up():
    """UX-05b: BuildSystem.__init__ reads $ATHENA_SUDO_PASSWORD and pops
    it from os.environ before prompting (no Prompt construction on env-
    var path; env var no longer accessible to child processes)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _bs = os.path.join(_ROOT, 'scripts', 'buildsystem.py')
    with open(_bs) as fh:
        _src = fh.read()
    # The env var must be read+pop'd before reaching for Prompt(
    # PROMPT_PASSWORD, ...).
    assert "os.environ.pop('ATHENA_SUDO_PASSWORD'" in _src, (
        "UX-05b: ATHENA_SUDO_PASSWORD must be pop'd from os.environ "
        "(read + remove in one step so the env var doesn't leak to "
        "child subprocesses)")
    # The pickup now lives in ONE shared helper (audit #38) — pop'd once —
    # called from BOTH __init__ and for_iso (the iso-only factory).
    assert _src.count(
        "os.environ.pop('ATHENA_SUDO_PASSWORD'") == 1, (
        "UX-05b: env-var pickup should live in the single "
        "_collect_and_validate_sudo helper")
    assert _src.count('_collect_and_validate_sudo') >= 3, (
        "UX-05b: _collect_and_validate_sudo must be DEFINED once and CALLED "
        "from both BuildSystem.__init__ and BuildSystem.for_iso")



def test_ux05d_cli_print_emits_ansi_when_tty():
    """UX-05d: Cli.print wraps the message in an ANSI sequence when
    _use_color is True and an attribute matches a known colour key.
    NO_COLOR env var disables (via _use_color=False)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import io
    from unittest.mock import patch
    import cli
    _c = object.__new__(cli.Cli)
    _c._use_color = True
    _buf = io.StringIO()
    with patch('builtins.print') as _p:
        _c.print('hello', cli.Cli.COLOR_ERROR)
        _args = _p.call_args.args
        # ANSI red + reset wrap
        assert _args[0].startswith('\x1b[31m'), _args[0]
        assert _args[0].endswith('\x1b[0m')
        assert 'hello' in _args[0]
    # _use_color=False → plain text
    _c._use_color = False
    with patch('builtins.print') as _p:
        _c.print('hello', cli.Cli.COLOR_ERROR)
        assert _p.call_args.args[0] == 'hello'



def test_ux08_signal_loss_fixes():
    """UX-08 (a)(c)(d): ProgressBar.set_max un-freezes a STOPPED bar; Cli's
    COLOR_* match tui.render's numbering so the constant handlers ACTUALLY pass
    (`tui.COLOR_ERROR`) prints red, not green; the LogEvent default tab is a
    real tab ('log', not the long-gone 'build' that silently dropped logs)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import patch
    import cli
    import tui
    from tui.widgets import ProgressBar
    from tui.events import LogEvent
    from tui.state import DEFAULT_TABS

    # (a) set_max un-freezes a bar that auto-STOPped at its (lazy) max
    _b = ProgressBar(label='x', maxvalue=1)
    _b.step(1)
    assert _b._state == _b.STOPPED
    _b.set_max(1000)
    assert _b._state == _b.RUNNING

    # (c) Cli colours keyed on render's numbering — passing the RENDER constant
    # (what command handlers pass) yields red, not the old cross-wired green.
    assert cli.Cli.COLOR_ERROR == tui.COLOR_ERROR
    _c = object.__new__(cli.Cli)
    _c._use_color = True
    with patch('builtins.print') as _p:
        _c.print('boom', tui.COLOR_ERROR)
        assert _p.call_args.args[0].startswith('\x1b[31m'), _p.call_args.args[0]

    # (d) LogEvent default tab is a real DEFAULT_TABS member (was 'build')
    assert LogEvent.tab == 'log'
    assert 'log' in DEFAULT_TABS and 'build' not in DEFAULT_TABS



def test_ux08_cache_info_picks_highest_version():
    """UX-08(f): the cache-info headline picks the genuinely highest version
    (apt semantics), not get_packages' mirror-parse order.  Regression: the
    old `pkgs[0]` returned the first-parsed version, and a naive string max
    picks '1.2.5' over '1.10.0' (lexical) — both wrong."""
    import apt_pkg
    import functools
    apt_pkg.init_system()
    # parse-order list: the highest version is neither first nor a string-max.
    pkgs = [{'Version': '1.2.0'}, {'Version': '1.10.0'}, {'Version': '1.2.5'}]
    pick = max(pkgs, key=functools.cmp_to_key(
        lambda _a, _b: apt_pkg.version_compare(
            str(_a.get('Version', '')), str(_b.get('Version', '')))))
    assert pick['Version'] == '1.10.0', pick
    assert pkgs[0]['Version'] == '1.2.0'           # the old buggy pick
    assert max(p['Version'] for p in pkgs) == '1.2.5'   # the lexical-max trap



def test_ux08_spinner_done_idempotent():
    """UX-08(g): a Spinner spanning multiple passes gets done() at the end of
    one pass and again on a later early-return; done() must print the
    '✓ … done' line exactly once."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui
    from tui.widgets import Spinner

    class _Backend:
        def __init__(self):
            self.prints = []

        def add_widget(self, w):
            return 1

        def del_widget(self, wid):
            pass

        def print(self, msg):
            self.prints.append(msg)

    _b = _Backend()
    _saved = tui.tui_instance
    tui.tui_instance = _b
    try:
        _s = Spinner("Parsing Dependencies")
        _s.done()
        _s.done()
    finally:
        tui.tui_instance = _saved
    assert sum('done' in _m for _m in _b.prints) == 1, _b.prints



def test_ux05e_one_shot_dispatch_runs_each_in_order_and_exits():
    """UX-05e: Cli with one_shot_cmds populated must dispatch each
    in order then exit (no REPL).  Exit code reflects worst outcome."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import patch
    import cli
    _c = object.__new__(cli.Cli)
    _c._cmds = {}
    _c._widget_ids = {}
    _c._next_widget_id = 0
    _c._exit_code = None
    _c.auto_yes = False
    _c._use_color = False
    _c.one_shot_cmds = ['foo bar baz', 'help']
    _calls = []
    _c._cmds['foo'] = (lambda *a: _calls.append(('foo', a)), '')
    with patch('builtins.print'):
        _c.wait()
    assert _calls == [('foo', ('bar', 'baz'))], _calls
    # _exit_code set to 0 because all dispatched OK
    assert _c._exit_code == 0



def test_one_shot_queue_consumed_not_replayed_on_reentry():
    """The one-shot queue runs exactly once even if wait() is re-entered
    — build.py's Exit() calls wait() again during shutdown.  Regression:
    one_shot_cmds was never cleared, so every `--cmd` executed twice
    (latent double `mirror publish` / `cache build`)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import patch
    import cli
    _c = object.__new__(cli.Cli)
    _c._cmds = {}
    _c._widget_ids = {}
    _c._next_widget_id = 0
    _c._exit_code = None
    _c.auto_yes = False
    _c._use_color = False
    _c.one_shot_cmds = ['tick']
    _runs = []
    _c._cmds['tick'] = (lambda *a: _runs.append(1), '')
    with patch('builtins.print'):
        _c.wait()      # first pass — runs the queue
        _c.wait()      # re-entry (as Exit() does) — must NOT replay
    assert _runs == [1], _runs
    assert _c.one_shot_cmds == []



def test_ux05e_one_shot_exit_code_nonzero_when_a_command_fails():
    """UX-05e: a `-c` command that's unknown OR whose handler raises must
    drive the process exit code to 1, so CI / scripted installs detect the
    failure.  Regression: `_failed` was never incremented, so a broken
    command silently exited 0."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import patch
    import cli
    _c = object.__new__(cli.Cli)
    _c._cmds = {}
    _c._widget_ids = {}
    _c._next_widget_id = 0
    _c._exit_code = None
    _c.auto_yes = False
    _c._use_color = False

    def _boom(*_a):
        raise RuntimeError('handler blew up')
    _c._cmds['ok'] = (lambda *a: None, '')
    _c._cmds['boom'] = (_boom, '')

    # (a) handler raises → exit 1
    _c.one_shot_cmds = ['ok', 'boom']
    with patch('builtins.print'):
        _c.wait()
    assert _c._exit_code == 1, _c._exit_code

    # (b) unknown command → exit 1
    _c._exit_code = None
    _c.one_shot_cmds = ['ok', 'no-such-command']
    with patch('builtins.print'):
        _c.wait()
    assert _c._exit_code == 1, _c._exit_code

    # (c) all good → exit 0 (the happy path still holds)
    _c._exit_code = None
    _c.one_shot_cmds = ['ok', 'ok']
    with patch('builtins.print'):
        _c.wait()
    assert _c._exit_code == 0, _c._exit_code



def test_ux05g_cmd_methods_reset_flags_on_entry():
    """UX-05g: every cmd_* method that sets `self.flags.X = True` must
    ALSO reset `self.flags.X = False` somewhere in the same function
    body — guards against Ctrl+C / exception leaving a stale True from
    a previous successful run.

    Explicit allowlist: cmd_build_iso_live + cmd_build_iso_disk set
    `chroot_verified = True` AFTER a successful in-line verify call
    (the "force mode: re-verifying chroot" path).  Those flag-sets are
    reachable only when verify already succeeded — honest by
    construction, no reset needed.
    """
    import ast
    _build_py = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_build_py) as fh:
        _tree = ast.parse(fh.read())

    _allowlist = {
        # (function_name, flag_attribute): rationale
        ('cmd_build_iso_live',  'chroot_verified'):
            'side-effect refresh after successful re-verify',
        ('cmd_build_iso_disk',  'chroot_verified'):
            'side-effect refresh after successful re-verify',
    }

    def _is_flags_assign(node, value: bool):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            return False
        _t = node.targets[0]
        return (isinstance(_t, ast.Attribute)
                and isinstance(_t.value, ast.Attribute)
                and isinstance(_t.value.value, ast.Name)
                and _t.value.value.id == 'self'
                and _t.value.attr == 'flags'
                and isinstance(node.value, ast.Constant)
                and node.value.value is value)

    _missing = []
    for _func in ast.walk(_tree):
        if not isinstance(_func, ast.FunctionDef):
            continue
        _resets: 'set[str]' = set()
        _sets: 'list[tuple[str, int]]' = []
        for _n in ast.walk(_func):
            if _is_flags_assign(_n, True):
                _sets.append((_n.targets[0].attr, _n.lineno))
            elif _is_flags_assign(_n, False):
                _resets.add(_n.targets[0].attr)
        for _attr, _ln in _sets:
            if _attr in _resets:
                continue
            if (_func.name, _attr) in _allowlist:
                continue
            _missing.append((_func.name, _attr, _ln))
    assert not _missing, (
        "UX-05g: every cmd_* function that sets a flag True must also "
        "reset it to False on entry (so a Ctrl+C / exception during the "
        "run can't leave a stale True from a prior success).  Missing "
        "resets:\n  " + "\n  ".join(
            f"{_n}: flags.{_a} = True at L{_l} without matching False-reset"
            for _n, _a, _l in _missing))



# ─────────────────────────────────────────────────────────────────────────────
# BuildSession encapsulates pipeline state; cmd_* handlers are
#           methods bound to it (no module-level globals).
# ─────────────────────────────────────────────────────────────────────────────

def test_buildsession_constructible_with_stub_tui():
    """BuildSession ctor takes (config, tui_inst); flags init clean,
    state pointers start as None.  No singleton, no TUI subsystem,
    no apt_pkg required — exactly the unit-test entry point the prior
    module-globals layout was blocking."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui as _tui

    # Minimum stub config + Tui to satisfy BuildSession.__init__ assertions.
    class _StubTui: pass
    saved = _tui.tui_instance
    _tui.tui_instance = _StubTui()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mirror_block = textwrap.dedent("""
                [Mirror.main]
                Suffix =
                Component = main
            """)
            cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
            cfg = _build_config_from(tmp, cfg_path)
            if not cfg.is_valid:
                # Host may lack debian-archive-keyring — skip rather than
                # fail; the construction logic itself is what we are
                # exercising and that is module-level.
                print(f"SKIP test_buildsession_constructible_with_stub_tui ({cfg.error_str})")
                return

            from build import BuildSession
            session = BuildSession(cfg, _StubTui())

            # State starts as documented.
            assert session.config is cfg
            assert session.cache is None
            assert session.dep_tree is None
            assert session.container is None
            assert session.flags.cache_ready is False
            assert session.flags.dep_check_ready is False
            assert session.flags.chroot_verified is False

            # Command handlers are bound methods on the session so the TUI
            # can register them directly without lambdas / closures.
            for _name in ('cmd_build_cache', 'cmd_parse_dependency',
                          'cmd_source_sync', 'cmd_init_container',
                          'cmd_source_build', 'cmd_tunnel_package',
                          'cmd_build_chroot_live', 'cmd_build_chroot_installer',
                          'cmd_build_iso_live', 'cmd_build_iso_installer',
                          'cmd_verify_chroot', 'cmd_auto_run',
                          'cmd_auto_run_live', 'cmd_auto_run_installer',
                          'cmd_auto_run_disk',
                          'cmd_print',
                          # Group dispatchers (noun-verb command surface).
                          'cmd_cache', 'cmd_patch',
                          'cmd_source', 'cmd_repo', 'cmd_container',
                          'cmd_chroot', 'cmd_iso', 'cmd_key', 'cmd_clean'):
                _fn = getattr(session, _name)
                assert callable(_fn), f"{_name} not callable"
                assert _fn.__self__ is session, f"{_name} not bound to this session"
    finally:
        _tui.tui_instance = saved



def test_group_dispatchers_forward_to_underlying_cmd_methods():
    """Each cmd_<group>(<verb>) forwards to the matching cmd_<old_name>.
    Unknown verb falls through to _group_help (does not raise, does not
    invoke any underlying handler).  This is the contract the noun-verb
    command surface relies on."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    # Pairs: (group_method, verb, target_method).  One entry per registered
    # action across all 9 groups.
    _matrix = [
        ('cmd_cache',     'build',    'cmd_build_cache'),
        ('cmd_cache',     'purge',    'cmd_cache_purge'),
        ('cmd_cache',     'parse',    'cmd_parse_dependency'),
        ('cmd_cache',     'select',   'cmd_cache_select'),
        ('cmd_cache',     'info',     'cmd_cache_info'),
        ('cmd_patch',     'refresh',  'cmd_patch_refresh'),
        ('cmd_source',    'sync',     'cmd_source_sync'),
        ('cmd_source',    'build',    'cmd_source_build'),
        # 'reload' removed from cmd_repo in P4 (2026-05-23) — fork
        # operations live under cmd_source now.
        ('cmd_source',    'fork',     'cmd_source_fork'),
        # 'tunnel' moved from cmd_repo → cmd_source in MIRROR-01
        # Phase 8 (2026-06-04) — endpoint is a built .deb, same
        # artefact shape as source build.  `repo tunnel` now prints
        # a deprecation hint and returns False.
        ('cmd_source',    'tunnel',   'cmd_tunnel_package'),
        ('cmd_repo',      'audit',    'cmd_audit'),
        ('cmd_repo',      'repair',   'cmd_repo_repair'),
        # cmd_repo 'index' is now multi-token ('index full' / 'index
        # minimal'), and 'publish' takes 'git minimal' — covered by their
        # own routing tests below, not this verb-only matrix.
        # cmd_container is now two-level ('container local <action>' /
        # 'container remote <action>') — covered by
        # test_container_two_level_command_surface_wired.
        # cmd_chroot 'build' is now multi-token ('build live' / 'build
        # installer') with default-to-live; covered by its own tests below.
        # 'verify' takes NO args (guarded) — covered by
        # test_ux09f_stray_token_dispatch_prints_usage, not this pass-through
        # matrix.
        # cmd_iso is multi-token ('build live' / 'build installer') —
        # not a verb-only dispatcher; covered by its own tests below.
        ('cmd_key',       'generate', 'cmd_generate_signing_key'),
        ('cmd_key',       'verify',   'cmd_verify_signing_key'),
        # clean dispatcher.  Note: 'cache' delegates to the
        # existing cmd_cache_purge (not a new cmd_clean_cache method),
        # so the matrix exercises the alias.
        ('cmd_clean',     'cache',     'cmd_cache_purge'),
        ('cmd_clean',     'source',    'cmd_clean_source'),
        ('cmd_clean',     'repo',      'cmd_clean_repo'),
        ('cmd_clean',     'buildroot', 'cmd_clean_buildroot'),
        ('cmd_clean',     'image',     'cmd_clean_image'),
        ('cmd_clean',     'container', 'cmd_container_purge'),
        ('cmd_clean',     'all',       'cmd_clean_all'),
    ]

    for _group_name, _verb, _target_name in _matrix:
        # Bypass __init__ — we don't want to construct a real config; we
        # only need the bound dispatcher methods, which are class-level.
        _sess = BuildSession.__new__(BuildSession)
        _calls = []
        # Stamp the target with a recorder; the dispatcher should call it.
        setattr(_sess, _target_name,
                lambda *a, _name=_target_name, _c=_calls, **kw: _c.append((_name, a, kw)))
        _dispatch = getattr(_sess, _group_name)
        _dispatch(_verb, 'arg1', 'arg2')
        assert _calls == [(_target_name, ('arg1', 'arg2'), {})], (
            f"{_group_name} {_verb} should forward to {_target_name}, got {_calls}")

    # Unknown verb: dispatcher must not raise and must not call any underlying
    # handler.  cmd_cache only knows 'build' and 'purge'; 'wat' should print
    # help and stop.
    _sess = BuildSession.__new__(BuildSession)
    _called = []
    _sess.cmd_build_cache = lambda *a, **kw: _called.append('build_cache')
    _sess.cmd_cache_purge = lambda *a, **kw: _called.append('cache_purge')
    # _group_help calls console.print — that's a module-level facade with a
    # fallback when no Tui is registered; no need to stub it.
    _sess.cmd_cache('wat')
    assert _called == [], f"unknown verb must not invoke any handler, got {_called}"



def test_ux09f_stray_token_dispatch_prints_usage():
    """`chroot verify now` / `autorun live extra` print a usage line instead
    of forwarding the stray token to a zero-arg handler (which raised
    TypeError)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    # chroot verify: no args → forwards once; an extra token → no forward.
    _s = BuildSession.__new__(BuildSession)
    _calls = []
    _s.cmd_verify_chroot = lambda *a, **k: _calls.append(a)
    _s.cmd_chroot('verify')
    assert _calls == [()], f"verify (no args) should forward: {_calls}"
    _calls.clear()
    _s.cmd_chroot('verify', 'now')          # must NOT raise
    assert _calls == [], "stray token must not forward to cmd_verify_chroot"

    # autorun live: no args → forwards once; an extra token → no forward.
    _s2 = BuildSession.__new__(BuildSession)
    _s2.config = type('C', (), {'build_mode': 'distribution'})()
    _ran = []
    _s2.cmd_auto_run_live = lambda *a, **k: _ran.append(a)
    _s2.cmd_auto_run('live')
    assert _ran == [()], f"autorun live (no args) should forward: {_ran}"
    _ran.clear()
    _s2.cmd_auto_run('live', 'extra')        # must NOT raise
    assert _ran == [], "stray token must not forward to cmd_auto_run_live"



def test_cmd_chroot_build_no_subaction_defaults_to_live():
    """Bare `chroot build` (no live/installer) routes to cmd_build_chroot_live
    with no args — preserves today's autorun and the bare-`chroot build`
    operator UX."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_build_chroot_live = (
        lambda *a, **kw: _calls.append(('live', a, kw)))
    _sess.cmd_build_chroot_installer = (
        lambda *a, **kw: _calls.append(('installer', a, kw)))
    _sess.cmd_chroot('build')
    assert _calls == [('live', (), {})], _calls



def test_cmd_chroot_build_live_explicit_forwards_to_live():
    """`chroot build live [args]` routes to cmd_build_chroot_live with the
    remaining args."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_build_chroot_live = (
        lambda *a, **kw: _calls.append(('live', a, kw)))
    _sess.cmd_build_chroot_installer = (
        lambda *a, **kw: _calls.append(('installer', a, kw)))
    _sess.cmd_chroot('build', 'live', 'with_debug')
    assert _calls == [('live', ('with_debug',), {})], _calls



def test_cmd_chroot_build_installer_forwards_to_installer():
    """`chroot build installer [args]` routes to cmd_build_chroot_installer."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_build_chroot_live = (
        lambda *a, **kw: _calls.append(('live', a, kw)))
    _sess.cmd_build_chroot_installer = (
        lambda *a, **kw: _calls.append(('installer', a, kw)))
    _sess.cmd_chroot('build', 'installer', 'extra')
    assert _calls == [('installer', ('extra',), {})], _calls



def test_cmd_chroot_build_passthrough_args_to_live():
    """`chroot build with_debug` (no live/installer keyword) is treated as
    args to the live build, preserving the bare `chroot build with_debug`
    UX from before the split."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_build_chroot_live = (
        lambda *a, **kw: _calls.append(('live', a, kw)))
    _sess.cmd_build_chroot_installer = (
        lambda *a, **kw: _calls.append(('installer', a, kw)))
    _sess.cmd_chroot('build', 'with_debug')
    assert _calls == [('live', ('with_debug',), {})], _calls



def test_cmd_build_chroot_installer_bails_on_unmet_prereqs():
    """Phase 5: cmd_build_chroot_installer is no longer a stub — it runs
    the udeb-unpack pipeline.  But it must bail BEFORE any sudo prompt
    if the prereqs (dep_check_ready, source_build_ready, udeb_dep_tree)
    aren't met, so test invocations don't hang on Prompt input.

    This test pins the contract: no flags set → bails cleanly with None,
    no exception, no sudo prompt."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession, BuildFlags
    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.udeb_dep_tree = None
    # No flags set, no udeb tree → bails on the first prereq check.
    assert _sess.cmd_build_chroot_installer() is None
    # Setting dep_check_ready without source_build_ready bails on second check.
    _sess.flags.dep_check_ready = True
    assert _sess.cmd_build_chroot_installer() is None
    # Setting source_build_ready without udeb tree bails on third check.
    _sess.flags.source_build_ready = True
    assert _sess.cmd_build_chroot_installer() is None



def test_cmd_repo_dispatcher_drops_index_and_tunnel_hints():
    """`repo index` / `repo tunnel` were retired and their redirect hints
    removed — the dispatcher no longer special-cases either action; both
    fall through to the `_group_help` unknown-action listing.  The
    `cmd_index_repo` handler itself still exists for the auto-index paths
    (chroot build / mirror publish).  Pin via code inspection."""
    _body = _session_source()
    import re
    _m = re.search(
        r'def cmd_repo\(self.*?(?=\n    def )',
        _body, re.DOTALL,
    )
    assert _m, 'cmd_repo dispatcher not found'
    _fn = _m.group(0)
    assert "action == 'index'" not in _fn, (
        "the retired 'index' hint must be gone — not special-cased")
    assert "action == 'tunnel'" not in _fn, (
        "the retired 'tunnel' hint must be gone — not special-cased")
    assert 'return self.cmd_index_repo' not in _fn
    # cmd_index_repo itself must still exist (auto-index entry point).
    assert 'def cmd_index_repo(' in _body

    # And the `repo` command MUST be registered in the tui dispatch
    # table so the operator can reach `repo audit` / `repo repair`.
    assert "register_command('repo'" in _body, (
        "`repo` command not wired into tui dispatch"
    )



def test_superseded_binary_names_excludes_selected():
    """_superseded_binary_names returns names a SELECTED FORK Conflicts/Replaces,
    minus names that are themselves selected.  ONLY fork packages count: a
    rename fork's upstream binary is flagged, but a NON-fork's ordinary
    transitional Conflicts/Replaces (usrmerge Conflicts cryptsetup) is NOT a
    supersession — that over-broad scan wrongly flagged 82 production-sibling
    binaries (fixed 2026-06-08)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)

    # Fork registry the cache exposes (fork/source/ discovery).
    class _Cache:
        _fork_pkg_names = {'athena-setup-udeb'}
        _fork_src_names: set = set()
        _fork_udeb_names: set = set()
    _sess.cache = _Cache()

    class _Tree:
        selected_pkgs = {
            # FORK → its Conflicts/Replaces ARE supersessions.
            'athena-setup-udeb': {'Package': 'athena-setup-udeb',
                                  'Conflicts': 'apt-setup-udeb',
                                  'Replaces': 'apt-setup-udeb'},
            # NON-fork pool mutual-exclusion (both selected) — not superseded.
            'grub-efi-amd64': {'Package': 'grub-efi-amd64',
                               'Conflicts': 'grub-pc'},
            'grub-pc': {'Package': 'grub-pc'},          # also a selected pool extra
            # NON-fork ordinary transitional Conflicts — MUST be ignored.
            'usrmerge': {'Package': 'usrmerge',
                         'Conflicts': 'cryptsetup'},
        }

    _sess.dep_tree = _Tree()
    _sess.udeb_dep_tree = None
    _out = _sess._superseded_binary_names()
    assert 'apt-setup-udeb' in _out, "fork's superseded upstream binary"
    assert 'grub-pc' not in _out, "selected pool extra not superseded"
    assert 'cryptsetup' not in _out, (
        "a NON-fork's transitional Conflicts must NOT be treated as a "
        "supersession (the 82-false-orphan bug)")



def test_iso_installer_build_iso_installer_passes_pool_whitelist():
    """build.py:cmd_build_iso_installer must derive a pool whitelist
    of canonical selected_pkgs names minus live_exclusive_pkg_names,
    and pass it to build_installer_iso.  Pin the derivation so a
    future refactor doesn't silently revert to a blanket copy.

    Live-exclusive packages are intentionally excluded: with the
    post-2026-05-12 pkg.list audit, every package d-i apt-installs at
    install time is explicit in pkg.list, so live_exclusive only
    contains true live-only binaries that have no business shipping
    on an installer ISO."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _src = inspect.getsource(BuildSession.cmd_build_iso_installer)
    assert 'deb_whitelist=' in _src, (
        "cmd_build_iso_installer must pass deb_whitelist to build_installer_iso"
    )
    assert 'live_exclusive_pkg_names' in _src, (
        "live-exclusive must be subtracted from base_include AND pool"
    )
    assert "selected_pkgs[_name]['Package']" in _src, (
        "the whitelist must filter to canonical names (skip provides aliases)"
    )



def test_buildconfig_exposes_poollist_path():
    """`pool.list` is plumbed through BuildConfig the same way
    `pkg.list`/`live.list`/`installer.list` are; pin the attribute name
    so cmd_build_iso_installer's `self.config.poollist_path` keeps
    resolving."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    # BuildConfig parses argv at construct time; install a stable argv
    # that points at the working tree's config so the parser is happy.
    _saved_argv = sys.argv[:]
    sys.argv = ['build.py', '--working-dir', _ROOT]
    try:
        _cfg = utils.BuildConfig()
    finally:
        sys.argv = _saved_argv
    assert hasattr(_cfg, 'poollist_path'), \
        "BuildConfig.poollist_path missing"
    assert _cfg.poollist_path.endswith('config/pool.list'), \
        _cfg.poollist_path



def test_read_pkg_list_handles_pool_list_format():
    """`Build._read_pkg_list` is the shared parser for pkg/live/installer/
    pool lists.  Confirm pool.list parses to the expected name set
    (no surprises from the comment block)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _names = BuildSession._read_pkg_list(
        os.path.join(_ROOT, 'config', 'pool.list'),
        already_selected=set(),
    )
    assert 'grub-pc' in _names, _names
    assert 'grub-efi-amd64' in _names, _names
    assert 'open-vm-tools' in _names, _names
    assert 'open-vm-tools-desktop' in _names, _names
    # console-setup re-added 2026-05-18 after the audit-removal
    # regression — base-installer apt-installs it directly on /target,
    # not via the kbd chain.  keyboard-configuration + xkb-data remain
    # transitive via kbd → keyboard-configuration → xkb-data.
    # See test_pool_list_pins_target_only_packages.
    assert 'console-setup' in _names, _names
    assert 'keyboard-configuration' not in _names, _names
    assert 'xkb-data' not in _names, _names
    # No accidental comment lines bleeding through.
    for _n in _names:
        assert not _n.startswith('#'), _n



def test_buildconfig_chroot_paths_under_shared_buildroot_parent():
    """Phase 5 (revised 2026-05-11): the [Directories] Chroot value is
    a PARENT dir holding both child chroots — `<parent>/live` and
    `<parent>/installer`.  Pins the derivation so a future refactor
    doesn't accidentally flatten one of them into the parent
    (which would land the OTHER chroot inside the first one's content tree).

    Uses the project's real build.conf to avoid maintaining a parallel
    minimal fixture as BuildConfig's required-section set evolves."""
    import sys, tempfile, shutil
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import BuildConfig
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir, exist_ok=True)
        # Copy the project's actual build.conf — same shape BuildConfig
        # is exercised against in production.
        shutil.copy(os.path.join(_ROOT, 'config', 'build.conf'),
                    os.path.join(_cfg_dir, 'build.conf'))
        shutil.copy(os.path.join(_ROOT, 'config', 'distro.conf'),
                    os.path.join(_cfg_dir, 'distro.conf'))
        for _name in ('pkg.list', 'live.list', 'installer.list'):
            with open(os.path.join(_cfg_dir, _name), 'w') as f: f.write('')
        _saved_argv = sys.argv
        sys.argv = ['build.py',
                    '--working-dir',    _tmp,
                    '--config-file',    os.path.join(_cfg_dir, 'build.conf'),
                    '--pkg-list',       os.path.join(_cfg_dir, 'pkg.list'),
                    '--live-list',      os.path.join(_cfg_dir, 'live.list'),
                    '--installer-list', os.path.join(_cfg_dir, 'installer.list')]
        try:
            cfg = BuildConfig()
        finally:
            sys.argv = _saved_argv
        assert cfg.error_str == '', (
            f"BuildConfig fixture failed to load: {cfg.error_str}")
        # Both chroots share a parent (dir_buildroot) and live under it as siblings.
        assert hasattr(cfg, 'dir_buildroot')
        assert cfg.dir_chroot           == os.path.join(cfg.dir_buildroot, 'live')
        assert cfg.dir_chroot_installer == os.path.join(cfg.dir_buildroot, 'installer')
        # Neither chroot is INSIDE the other.
        assert not cfg.dir_chroot.startswith(cfg.dir_chroot_installer + os.sep)
        assert not cfg.dir_chroot_installer.startswith(cfg.dir_chroot + os.sep)



def test_buildconfig_creates_fork_source_dir():
    """FORK-01 Step 1: BuildConfig auto-creates fork/ and fork/source/ so
    operators (and tests) never have to `mkdir` them manually.  Mirrors the
    dir_patch + dir_patch_source pattern.  Pins the contract per the
    feedback_dir_ensure_on_config_load.md memory."""
    import sys, tempfile, shutil
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import BuildConfig
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir, exist_ok=True)
        shutil.copy(os.path.join(_ROOT, 'config', 'build.conf'),
                    os.path.join(_cfg_dir, 'build.conf'))
        shutil.copy(os.path.join(_ROOT, 'config', 'distro.conf'),
                    os.path.join(_cfg_dir, 'distro.conf'))
        for _name in ('pkg.list', 'live.list', 'installer.list'):
            with open(os.path.join(_cfg_dir, _name), 'w') as f: f.write('')
        _saved_argv = sys.argv
        sys.argv = ['build.py',
                    '--working-dir',    _tmp,
                    '--config-file',    os.path.join(_cfg_dir, 'build.conf'),
                    '--pkg-list',       os.path.join(_cfg_dir, 'pkg.list'),
                    '--live-list',      os.path.join(_cfg_dir, 'live.list'),
                    '--installer-list', os.path.join(_cfg_dir, 'installer.list')]
        try:
            cfg = BuildConfig()
        finally:
            sys.argv = _saved_argv
        assert cfg.is_valid, f"BuildConfig invalid: {cfg.error_str}"
        # Both the top-level and the source/ subdir must exist after load.
        assert os.path.isdir(cfg.dir_fork), (
            f"dir_fork {cfg.dir_fork} not auto-created")
        assert os.path.isdir(cfg.dir_fork_source), (
            f"dir_fork_source {cfg.dir_fork_source} not auto-created")
        assert os.access(cfg.dir_fork, os.W_OK)
        assert os.access(cfg.dir_fork_source, os.W_OK)
        # Path composition: dir_fork_source == <dir_fork>/source
        assert cfg.dir_fork_source == os.path.join(cfg.dir_fork, 'source')



def test_build_flags_carries_chroot_installer_ready_default_false():
    """Phase 5: a new flag bit was added — pin it default-False so a
    stale BuildFlags instance from before the field landed (or a future
    refactor that drops it) re-fails at this test boundary."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildFlags
    f = BuildFlags()
    assert hasattr(f, 'chroot_installer_ready')
    assert f.chroot_installer_ready is False
    # __str__ summary includes it (so `print state` surfaces it)
    assert 'chroot_installer' in str(f)
    # iso_disk_ready (COMP-09 disk end-state) — same default-False contract.
    assert hasattr(f, 'iso_disk_ready')
    assert f.iso_disk_ready is False
    assert 'iso_disk' in str(f)



def test_cmd_iso_build_requires_subaction():
    """`iso build` (no live/installer) prints usage and calls neither
    underlying iso build handler."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _called = []
    _sess.cmd_build_iso_live      = lambda *a, **kw: _called.append('live')
    _sess.cmd_build_iso_installer = lambda *a, **kw: _called.append('installer')
    _sess.cmd_iso('build')
    assert _called == [], (
        f"bare `iso build` must not invoke any handler, got {_called}")



# ─── COMP-02: repo index minimal/full + publish ssh/local ───────────────────

def test_repo_index_dispatch_falls_through_after_hint_removal():
    """`repo index` was retired and its redirect hint removed — the
    dispatcher no longer special-cases it, so it falls through to the
    group-help "Unknown repo action" listing without invoking either
    auto-index handler.  The handlers (cmd_index_repo,
    cmd_index_repo_minimal) survive as internal callables (chroot build +
    mirror publish auto-index)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_index_repo         = lambda *a, **kw: _calls.append(('full', a))
    _sess.cmd_index_repo_minimal = lambda *a, **kw: _calls.append(('minimal', a))

    _lines: 'list[str]' = []
    _orig = build.console.print
    build.console.print = lambda *a, **k: _lines.append(
        ' '.join(str(x) for x in a))
    try:
        _sess.cmd_repo('index', 'full')
        _sess.cmd_repo('index')
        _sess.cmd_repo('index', 'minimal')
    finally:
        build.console.print = _orig
    # No auto-index handler is invoked from the operator route
    assert _calls == [], _calls
    _joined = '\n'.join(_lines)
    # Falls through to the group help — no deprecation hint anymore
    assert 'no longer operator-visible' not in _joined, _joined
    assert "Unknown repo action: 'index'" in _joined, _joined
    # The handlers themselves still exist on the class (internal API)
    assert callable(getattr(BuildSession, 'cmd_index_repo', None))
    assert callable(getattr(BuildSession, 'cmd_index_repo_minimal', None))









def test_cmd_iso_build_live_forwards_to_cmd_build_iso_live():
    """`iso build live [args]` routes to cmd_build_iso_live(args), not
    the installer handler."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_build_iso_live = (
        lambda *a, **kw: _calls.append(('live', a, kw)))
    _sess.cmd_build_iso_installer = (
        lambda *a, **kw: _calls.append(('installer', a, kw)))
    _sess.cmd_iso('build', 'live', 'force')
    assert _calls == [('live', ('force',), {})], _calls



def test_cmd_iso_build_installer_forwards_to_cmd_build_iso_installer():
    """`iso build installer [args]` routes to cmd_build_iso_installer."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_build_iso_live = (
        lambda *a, **kw: _calls.append(('live', a, kw)))
    _sess.cmd_build_iso_installer = (
        lambda *a, **kw: _calls.append(('installer', a, kw)))
    _sess.cmd_iso('build', 'installer', 'extra')
    assert _calls == [('installer', ('extra',), {})], _calls



def test_cmd_iso_build_unknown_subaction_calls_neither_handler():
    """`iso build wat` falls through to help, no handler invoked."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _called = []
    _sess.cmd_build_iso_live = lambda *a, **kw: _called.append('live')
    _sess.cmd_build_iso_installer = lambda *a, **kw: _called.append('installer')
    _sess.cmd_iso('build', 'wat')
    assert _called == [], _called



# ─────────────────────────────────────────────────────────────────────────────
# `clean` dispatcher + idempotency on cmd_build_cache / cmd_parse_dependency
# ─────────────────────────────────────────────────────────────────────────────

def test_cmd_build_cache_skips_when_already_ready_no_force():
    """cmd_build_cache early-exits when cache_ready is True and
    an in-memory Cache is loaded.  Cache() must NOT be instantiated —
    that would do real network work.  `force` arg bypasses; covered in
    a follow-on test."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession, BuildFlags

    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.cache_ready = True
    _sess.cache = object()  # any non-None placeholder

    _ctor_calls = []
    _orig_Cache = build.Cache
    build.Cache = lambda *a, **kw: (_ctor_calls.append((a, kw)), object())[1]
    try:
        _sess.cmd_build_cache()
    finally:
        build.Cache = _orig_Cache
    assert _ctor_calls == [], (
        "cmd_build_cache should NOT instantiate Cache when already ready, "
        f"got {len(_ctor_calls)} call(s)")



def test_cmd_build_cache_runs_when_force_passed_even_if_ready():
    """`cache build force` bypasses the early-exit guard and
    re-runs the full cache build.  Verifies Cache() IS instantiated
    when force is in args."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from commands import cmd_cache
    import tui as _tui
    from build import BuildSession, BuildFlags

    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.cache_ready = True
    _sess.cache = object()
    # Stub config — only the bits cmd_build_cache reads on the early
    # path before Cache() is constructed.  Empty mirrors → the reachability
    # gate finds nothing down and proceeds to Cache().
    class _StubCfg:
        snapshot_enabled = False
        mirrors: list = []
        snapshot_baseurl = ''
    _sess.config = _StubCfg()

    # Spinner inside cmd_build_cache reads tui.tui_instance — must be
    # set or the constructor raises.  Earlier tests in the run may
    # have left None behind (e.g. test_console_raises_when_no_tui_anywhere
    # explicitly nulls it), so ordering on a clean CI host makes this
    # test fail; assert it's present for THIS test rather than rely on
    # accidental state from a sibling.
    class _StubTui:
        def __init__(self): self._next = 0
        def add_widget(self, _w):
            self._next += 1
            return self._next
        def del_widget(self, _id): pass
        def print(self, *_a, **_kw): pass
    _saved_tui = _tui.tui_instance
    _tui.tui_instance = _StubTui()

    _ctor_calls = []
    class _StubCache:
        def __init__(self, cfg):
            _ctor_calls.append(cfg)
            self.is_valid = False
            self.error_str = 'stub'
    _orig_Cache = cmd_cache.Cache
    cmd_cache.Cache = _StubCache
    try:
        _sess.cmd_build_cache('force')
    finally:
        cmd_cache.Cache = _orig_Cache
        _tui.tui_instance = _saved_tui
    assert len(_ctor_calls) == 1, (
        "cmd_build_cache with force MUST run Cache() even if cache_ready, "
        f"got {len(_ctor_calls)} call(s)")



def test_cmd_parse_dependency_skips_when_already_ready_no_force():
    """cmd_parse_dependency early-exits when dep_check_ready is
    True and an in-memory dep_tree exists.  DependencyTree() must NOT
    be instantiated."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    from build import BuildSession, BuildFlags

    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.cache_ready = True
    _sess.flags.dep_check_ready = True
    _sess.cache = object()
    _sess.dep_tree = object()

    _ctor_calls = []
    _orig_DT = dependencytree.DependencyTree
    dependencytree.DependencyTree = lambda *a, **kw: (
        _ctor_calls.append((a, kw)), object())[1]
    try:
        _sess.cmd_parse_dependency()
    finally:
        dependencytree.DependencyTree = _orig_DT
    assert _ctor_calls == [], (
        "cmd_parse_dependency should NOT instantiate DependencyTree when "
        f"already ready, got {len(_ctor_calls)} call(s)")



def test_cmd_parse_dependency_runs_when_force_passed_even_if_ready():
    """`dep parse force` bypasses the early-exit guard.
    Cannot drive the full resolve from a stub, but we can verify the
    early-exit guard is bypassed by checking Spinner() construction
    (which happens AFTER the guard but BEFORE any heavy work)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from commands import cmd_cache
    from build import BuildSession, BuildFlags

    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.cache_ready = True
    _sess.flags.dep_check_ready = True
    _sess.cache = object()
    _sess.dep_tree = object()

    _spinner_calls = []
    _orig_Spinner = cmd_cache.Spinner
    class _StubSpinner:
        def __init__(self, *a, **kw):
            _spinner_calls.append((a, kw))
            raise RuntimeError("stop here — guard was bypassed, that's all we wanted to check")
        def done(self): pass
    cmd_cache.Spinner = _StubSpinner
    try:
        try:
            _sess.cmd_parse_dependency('force')
        except RuntimeError as e:
            assert 'guard was bypassed' in str(e)
    finally:
        cmd_cache.Spinner = _orig_Spinner
    assert len(_spinner_calls) == 1, (
        "force should bypass the guard and reach Spinner construction, "
        f"got {len(_spinner_calls)} Spinner call(s)")



def test_wipe_dir_contents_returns_true_on_missing_dir():
    """_wipe_dir_contents on a path that doesn't exist is a no-op
    success — operator may run `clean X` before any `X` work has
    created the dir."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    with tempfile.TemporaryDirectory() as _tmp:
        _missing = os.path.join(_tmp, 'definitely-not-here')
        assert _sess._wipe_dir_contents(
            'test', _missing, sudo=False, skip_prompt=True) is True



def test_wipe_dir_contents_returns_true_on_empty_dir():
    """Empty dir → no work to do, returns True without prompting."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    with tempfile.TemporaryDirectory() as _tmp:
        # _tmp itself is empty; pass it as the target
        assert _sess._wipe_dir_contents(
            'test', _tmp, sudo=False, skip_prompt=True) is True



def test_wipe_dir_contents_actually_removes_files_and_subdirs():
    """skip_prompt=True + populated dir → entries removed, dir itself
    preserved (BuildConfig invariant)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    with tempfile.TemporaryDirectory() as _tmp:
        # Plant a file + a non-empty subdir.
        with open(os.path.join(_tmp, 'top.txt'), 'w') as f:
            f.write('hi')
        os.makedirs(os.path.join(_tmp, 'sub'))
        with open(os.path.join(_tmp, 'sub', 'inner.txt'), 'w') as f:
            f.write('ho')
        assert _sess._wipe_dir_contents(
            'test', _tmp, sudo=False, skip_prompt=True) is True
        # Dir survives but is empty.
        assert os.path.isdir(_tmp)
        assert os.listdir(_tmp) == []



def test_cmd_clean_source_resets_download_ready():
    """cmd_clean_source wipes dir_source and resets download_ready."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession, BuildFlags
    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.download_ready = True
    with tempfile.TemporaryDirectory() as _tmp:
        # Put a file so the dir is non-empty (exercises the actual wipe).
        with open(os.path.join(_tmp, 'a.tar.gz'), 'w') as f:
            f.write('x')
        class _Cfg:
            dir_source = _tmp
        _sess.config = _Cfg()
        _sess.cmd_clean_source('force')
    assert _sess.flags.download_ready is False



def test_cmd_clean_image_resets_iso_flags():
    """cmd_clean_image wipes dir_image and resets iso_live_ready,
    iso_installer_ready AND iso_disk_ready (single dir holds outputs
    from all three pipelines)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession, BuildFlags
    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.iso_live_ready = True
    _sess.flags.iso_installer_ready = True
    _sess.flags.iso_disk_ready = True
    with tempfile.TemporaryDirectory() as _tmp:
        class _Cfg:
            dir_image = _tmp
        _sess.config = _Cfg()
        _sess.cmd_clean_image('force')
    assert _sess.flags.iso_live_ready is False
    assert _sess.flags.iso_installer_ready is False
    assert _sess.flags.iso_disk_ready is False



def test_cmd_clean_repo_resets_source_build_ready_and_drops_counts():
    """cmd_clean_repo wipes dir_repo, resets source_build_ready, and
    drops last_source_build_counts (shown in the autorun summary)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession, BuildFlags
    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.source_build_ready = True
    _sess.last_source_build_counts = {'built': 12}
    with tempfile.TemporaryDirectory() as _tmp:
        class _Cfg:
            dir_repo = _tmp
        _sess.config = _Cfg()
        _sess.cmd_clean_repo('force')
    assert _sess.flags.source_build_ready is False
    assert _sess.last_source_build_counts is None



def test_cmd_container_purge_resets_flag_and_drops_session_ref():
    """cmd_container_purge with no docker state still resets the flag
    and drops self.container (idempotent contract: safe to run when
    no init has happened yet)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import docker as _docker  # confirm available before running test
    from build import BuildSession, BuildFlags

    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.build_container_ready = True
    _sess.container = object()
    class _Cfg:
        docker_server = ''
    _sess.config = _Cfg()

    # Stub the docker client so no real daemon is contacted.
    _client = _StubDockerClient(containers=[], images=[])
    _orig_from_env = _docker.from_env
    _docker.from_env = lambda: _client  # type: ignore[assignment,misc]
    try:
        _sess.cmd_container_purge('force')
    finally:
        _docker.from_env = _orig_from_env
    assert _sess.flags.build_container_ready is False
    assert _sess.container is None



def test_cmd_container_purge_removes_athena_containers_and_images():
    """When athenalinux:build-* containers + images exist, they are
    removed; non-athenalinux entries are ignored."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import docker as _docker
    from build import BuildSession, BuildFlags

    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.build_container_ready = True
    _sess.container = object()
    class _Cfg:
        docker_server = ''
    _sess.config = _Cfg()

    # Two athenalinux images + one foreign; three containers (two ours, one foreign).
    _img_athena_bw = _StubDockerImage('sha256:aaa', ['athenalinux:build-bookworm'])
    _img_athena_tr = _StubDockerImage('sha256:bbb', ['athenalinux:build-trixie'])
    _img_other     = _StubDockerImage('sha256:ccc', ['debian:bookworm-slim'])
    _c_athena_1 = _StubDockerContainer(_img_athena_bw)
    _c_athena_2 = _StubDockerContainer(_img_athena_tr)
    _c_other    = _StubDockerContainer(_img_other)
    # NOTE: client.images.list(name='athenalinux') filtering is server-side;
    # our stub returns whatever it has, so feed only the athenalinux ones.
    _client = _StubDockerClient(
        containers=[_c_athena_1, _c_athena_2, _c_other],
        images=[_img_athena_bw, _img_athena_tr],
    )
    _orig_from_env = _docker.from_env
    _docker.from_env = lambda: _client  # type: ignore[assignment,misc]
    try:
        _sess.cmd_container_purge('force')
    finally:
        _docker.from_env = _orig_from_env

    # Two athenalinux containers removed; foreign one untouched.
    assert _c_athena_1.removed is True
    assert _c_athena_2.removed is True
    assert _c_other.removed is False
    # Both athenalinux images removed.
    assert sorted(_client.images_removed) == ['sha256:aaa', 'sha256:bbb']
    # Flag + session ref reset.
    assert _sess.flags.build_container_ready is False
    assert _sess.container is None



def test_cmd_container_purge_handles_docker_connect_failure_gracefully():
    """If docker daemon is unreachable, cmd_container_purge prints an
    error and returns without raising — does NOT clobber the flag
    (caller can retry once daemon is up)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import docker as _docker
    from build import BuildSession, BuildFlags

    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.build_container_ready = True
    _sess.container = object()
    class _Cfg:
        docker_server = ''
    _sess.config = _Cfg()

    _orig_from_env = _docker.from_env
    def _raise(*a, **kw):
        raise _docker.errors.DockerException("simulated: daemon unreachable")
    _docker.from_env = _raise
    try:
        _sess.cmd_container_purge('force')  # must NOT raise
    finally:
        _docker.from_env = _orig_from_env
    # Connect failure leaves flag alone (operator can fix daemon + retry).
    assert _sess.flags.build_container_ready is True
    assert _sess.container is not None



def test_cmd_clean_dispatcher_unknown_action_calls_no_handler():
    """`clean wat` falls through to help, no handler invoked."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    _called = []
    for _name in ('cmd_cache_purge', 'cmd_clean_source', 'cmd_clean_repo',
                  'cmd_clean_buildroot', 'cmd_clean_image',
                  'cmd_clean_all'):
        setattr(_sess, _name, lambda *a, _n=_name, **kw: _called.append(_n))
    _sess.cmd_clean('wat')
    assert _called == [], (
        f"unknown clean action must not invoke any handler, got {_called}")



def test_cmd_build_iso_installer_bails_on_unmet_prereqs():
    """Phase 7: cmd_build_iso_installer is no longer a stub — it runs
    the mastering pipeline.  But it must bail BEFORE any sudo prompt if
    chroot_installer_ready is False, so test invocations don't hang on
    Prompt input.  Mirrors the prereq-bail test for
    cmd_build_chroot_installer."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession, BuildFlags
    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    # chroot_installer_ready is False by default → must bail cleanly.
    assert _sess.cmd_build_iso_installer() is None
    assert _sess.cmd_build_iso_installer('force') is None



def test_cache_purge_deletes_files_and_resets_flags():
    """`cache purge` deletes every regular file in dir_cache, drops the
    in-memory Cache + DependencyTree references, and resets cache_ready
    and dep_check_ready so downstream guards trip cleanly."""
    import sys, tempfile
    from unittest.mock import patch, MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession, BuildFlags

    with tempfile.TemporaryDirectory() as _tmp:
        # Sentinel files: two top-level files (should be deleted) plus a
        # subdirectory containing a file (should be left alone — purge
        # only touches top-level regular files).
        for _name in ('Packages.xz', 'snapshot.timestamp'):
            with open(os.path.join(_tmp, _name), 'wb') as _f:
                _f.write(b'x' * 1024)
        os.mkdir(os.path.join(_tmp, 'subdir'))
        with open(os.path.join(_tmp, 'subdir', 'keep.txt'), 'w') as _f:
            _f.write('keep')

        # Bypass __init__ — only need .config.dir_cache, .flags, the two
        # state attrs, and the dispatcher to forward.
        sess = BuildSession.__new__(BuildSession)
        class _Cfg: pass
        sess.config = _Cfg()
        sess.config.dir_cache = _tmp
        sess.flags = BuildFlags()
        sess.flags.cache_ready = True
        sess.flags.dep_check_ready = True
        sess.cache = object()        # sentinel — must be cleared
        sess.dep_tree = object()     # sentinel — must be cleared

        _prompt_inst = MagicMock()
        _prompt_inst.get_response.return_value = 'y'
        with patch('commands.cmd_cache.Prompt', return_value=_prompt_inst):
            sess.cmd_cache_purge()

        # Top-level files gone.
        assert not os.path.exists(os.path.join(_tmp, 'Packages.xz'))
        assert not os.path.exists(os.path.join(_tmp, 'snapshot.timestamp'))
        # Subdirectory and its contents preserved.
        assert os.path.isdir(os.path.join(_tmp, 'subdir'))
        assert os.path.isfile(os.path.join(_tmp, 'subdir', 'keep.txt'))
        # State reset.
        assert sess.cache is None
        assert sess.dep_tree is None
        assert sess.flags.cache_ready is False
        assert sess.flags.dep_check_ready is False



def test_cache_purge_cancelled_keeps_files_and_flags():
    """Operator says 'n' → no files deleted, flags untouched."""
    import sys, tempfile
    from unittest.mock import patch, MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession, BuildFlags

    with tempfile.TemporaryDirectory() as _tmp:
        with open(os.path.join(_tmp, 'Packages.xz'), 'wb') as _f:
            _f.write(b'x' * 1024)

        sess = BuildSession.__new__(BuildSession)
        class _Cfg: pass
        sess.config = _Cfg()
        sess.config.dir_cache = _tmp
        sess.flags = BuildFlags()
        sess.flags.cache_ready = True
        sess.cache = object()
        _sentinel_cache = sess.cache

        _prompt_inst = MagicMock()
        _prompt_inst.get_response.return_value = 'n'
        # Patch where the handler looks Prompt up (the cmd_cache mixin) —
        # patching build.Prompt only worked while an earlier test's stub
        # tui instance happened to be installed (order dependency).
        with patch('commands.cmd_cache.Prompt', return_value=_prompt_inst):
            sess.cmd_cache_purge()

        # File still present, flag untouched, cache reference intact.
        assert os.path.isfile(os.path.join(_tmp, 'Packages.xz'))
        assert sess.flags.cache_ready is True
        assert sess.cache is _sentinel_cache



def test_cache_purge_empty_dir_is_noop():
    """Empty cache dir → noop, no Prompt invoked, no flag changes."""
    import sys, tempfile
    from unittest.mock import patch, MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession, BuildFlags

    with tempfile.TemporaryDirectory() as _tmp:
        sess = BuildSession.__new__(BuildSession)
        class _Cfg: pass
        sess.config = _Cfg()
        sess.config.dir_cache = _tmp
        sess.flags = BuildFlags()
        sess.flags.cache_ready = True

        _prompt_inst = MagicMock()
        with patch('commands.cmd_cache.Prompt',
                   return_value=_prompt_inst) as mock_Prompt:
            sess.cmd_cache_purge()

        # Empty branch must not even construct the Prompt — confirm.
        mock_Prompt.assert_not_called()
        assert sess.flags.cache_ready is True



def test_build_py_threads_container_into_iso_callsites():
    """COMP-14: build.py's cmd_build_iso_live + cmd_build_iso_installer
    must pass self.container through to the ISO builders.  Pin via
    code inspection so the wiring can't silently drop."""
    _body = _session_source()
    # Live ISO: build_system.build_iso(container=self.container)
    assert 'build_system.build_iso(container=self.container)' in _body, (
        "cmd_build_iso_live must pass container=self.container to "
        "build_iso — REGRESSION to pre-COMP-14 (host-grub leak) if "
        "the kwarg is dropped"
    )
    # Installer ISO: requires both the call name AND the kwarg
    # somewhere — count appearances of `container=self.container` to
    # confirm both callsites are wired.
    _calls = _body.count('container=self.container')
    assert _calls >= 2, (
        f"expected >=2 `container=self.container` (one each for live + "
        f"installer ISO builds), found {_calls}.  REGRESSION to "
        f"pre-COMP-14 if either kwarg is dropped"
    )



def test_cmd_audit_nmu_residue_absorbed_into_cmd_audit():
    """P3 (2026-05-23): `repo audit_nmu` no longer exists as a
    standalone verb.  Its logic — audit_nmu_residue + reporting —
    lives in cmd_audit's _report_nmu_residue helper, run as the
    final section of every `repo audit` invocation.  Pin both:
    (a) the standalone verb is gone, (b) the absorbed helper is
    called from cmd_audit."""
    _body = _session_source()
    # (a) Standalone verb gone.
    assert 'def cmd_audit_nmu(' not in _body, (
        "cmd_audit_nmu must be removed — its logic was absorbed into "
        "cmd_audit's _report_nmu_residue (P3 2026-05-23)"
    )
    assert "if action == 'audit_nmu'" not in _body, (
        "'audit_nmu' must not be a cmd_repo action — absorbed into "
        "'repo audit'"
    )
    # (b) Absorbed helper exists + is called from cmd_audit.
    assert 'def _report_nmu_residue(' in _body, (
        "_report_nmu_residue helper must exist as the NMU-residue "
        "section of cmd_audit"
    )
    import re
    _m = re.search(
        r'def cmd_audit\(self.*?(?=\n    def \w)',
        _body, re.DOTALL,
    )
    assert _m, 'cmd_audit body not found'
    assert 'self._report_nmu_residue(' in _m.group(0), (
        "cmd_audit must call _report_nmu_residue as one of its sections"
    )



def test_cmd_strip_repo_registered_in_repo_dispatcher():
    """`repo repair strip` must route to cmd_strip_repo via the repair
    sub-dispatcher (post 2026-05-23 rename: was `repo strip`)."""
    _body = _session_source()
    import re
    # repo repair sub-dispatcher body
    _m = re.search(
        r'def cmd_repo_repair\(self.*?(?=\n    def \w)',
        _body, re.DOTALL,
    )
    assert _m, "cmd_repo_repair sub-dispatcher not found"
    _disp = _m.group(0)
    assert "'strip'" in _disp, (
        "strip must be advertised in cmd_repo_repair's table")
    assert re.search(
        r"if action == 'strip':\s*\n\s+return self\.cmd_strip_repo",
        _disp), "strip not dispatched in cmd_repo_repair"



def test_cmd_package_cleanup_registered_in_repo_dispatcher():
    """`repo repair cleanup` must route to cmd_package_cleanup via the
    repair sub-dispatcher (post 2026-05-23 rename: was `repo cleanup`).
    The command identifies obsolete .debs (orphan source or version
    drift) and ships in dry-run by default — `force` triggers actual
    delete after a YESNO prompt."""
    _body = _session_source()
    import re
    _m = re.search(
        r'def cmd_repo_repair\(self.*?(?=\n    def \w)',
        _body, re.DOTALL,
    )
    assert _m, "cmd_repo_repair sub-dispatcher not found"
    _disp = _m.group(0)
    assert "'cleanup'" in _disp, (
        "cleanup must be advertised in cmd_repo_repair's table"
    )
    assert re.search(
        r"if action == 'cleanup':\s*\n\s+return self\.cmd_package_cleanup",
        _disp), "cleanup not dispatched in cmd_repo_repair"
    # Body must define the underlying method.
    assert 'def cmd_package_cleanup(' in _body



def test_cmd_package_cleanup_reindexes_after_deletion():
    """After pruning .debs, cleanup regenerates the index (cmd_index_repo) so the
    on-disk Packages/Release no longer name deleted files — gated on an actual
    deletion."""
    import re
    _body = _session_source()
    _m = re.search(r'def cmd_package_cleanup\(self.*?(?=\n    def \w)',
                   _body, re.DOTALL)
    assert _m, "cmd_package_cleanup not found"
    _b = _m.group(0)
    assert 'self.cmd_index_repo()' in _b, (
        "cleanup must reindex (cmd_index_repo) after pruning")
    assert 'if _deleted > 0:' in _b, "reindex must be gated on an actual delete"



def test_cmd_package_cleanup_dry_run_default_force_flag_required():
    """`repo cleanup` without `force` must NOT delete any files —
    it's a dry-run by default.  Operator opt-in for the destructive
    path is double-gated: pass `force` flag AND answer 'y' to the
    YESNO prompt.

    Source-text inspection because exercising the full method end-to-
    end needs a fixture'd BuildSession + repo dir + control files."""
    _body = _session_source()
    import re
    # Find the method body.
    _m = re.search(
        r"\n    def cmd_package_cleanup\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m is not None, "cmd_package_cleanup body not found"
    _method = _m.group(0)
    # Dry-run banner appears BEFORE the YESNO prompt.
    assert 'DRY-RUN' in _method, (
        "cmd_package_cleanup must surface a DRY-RUN banner in the "
        "no-force path"
    )
    # Force gate present.
    assert "_force = 'force' in args" in _method, (
        "cmd_package_cleanup must read a `force` flag from args"
    )
    # YESNO prompt is gated on _force.
    assert 'if not _force:' in _method, (
        "no-force path must short-circuit before any deletion"
    )
    # The actual delete loop only runs after force AND the prompt.
    _delete_idx = _method.find('os.remove(_p)')
    _prompt_idx = _method.find('PROMPT_YESNO')
    assert _delete_idx != -1, "os.remove call missing — delete logic gone?"
    assert _prompt_idx != -1, "PROMPT_YESNO missing — delete must be guarded"
    assert _delete_idx > _prompt_idx, (
        "delete loop must follow the YESNO prompt (not precede it)"
    )



def test_cmd_package_cleanup_keeps_expected_files_drops_orphan_source():
    """The categoriser's contract: a file whose FILENAME is in
    src_pkg_files (predicted output of a selected source) is always
    kept.  A file whose Source field names a source NOT in
    selected_srcs is orphan → delete.  A file whose pkg name appears
    in src_pkg_files at a different filename is version drift → delete.
    A file whose source IS selected but whose pkg name does NOT appear
    in any src_pkg_files entry is a production sibling → keep
    (ships in /cdrom/pool but not selected for install).

    Anti-regression for the original epoch-trap implementation that
    compared Version fields raw — bsdutils Version 1:2.38.1-5 from
    util-linux source 2.38.1-5 would false-positive as drift.

    Categorisation now lives in the shared `_scan_stale_files` helper
    (refactored 2026-05-21 to also serve `repo audit`'s STALE
    section as a warn-only surface).  cmd_package_cleanup delegates
    to it and adds the dry-run/force/delete machinery."""
    _body = _session_source()
    import re
    # Helper holds the scan logic.
    _m_scan = re.search(
        r"\n    def _scan_stale_files\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m_scan is not None, "_scan_stale_files helper not found"
    _scan = _m_scan.group(0)
    # UPD-01: files are grouped by (subdir, name, pristine-base, arch) and the
    # HIGHEST version per EXPECTED base is kept (single-snapshot local) — so a
    # +asg<R>u<N>-stamped current artifact isn't mis-flagged as drift, while a
    # superseded lower version (e.g. the pristine predecessor) IS drift.
    assert '_expected_keys' in _scan and '_file_key' in _scan, (
        "_scan_stale_files must group by pristine-base key (UPD-01) so a "
        "stamped current artifact reconciles with its pristine prediction")
    assert 'apt_pkg.version_compare' in _scan, (
        "_scan_stale_files must compare versions to keep the highest per group")
    # Production-sibling preservation lives in the helper.
    assert 'production sibling' in _scan.lower(), (
        "_scan_stale_files must document/preserve production-sibling "
        "fall-through (source selected + pkg name not predicted = KEEP)"
    )
    # Cleanup delegates to the helper rather than re-implementing.
    _m_cleanup = re.search(
        r"\n    def cmd_package_cleanup\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m_cleanup is not None
    _cleanup = _m_cleanup.group(0)
    assert 'self._scan_stale_files()' in _cleanup, (
        "cmd_package_cleanup must call _scan_stale_files (not "
        "re-implement the scan inline — two copies will drift)"
    )
    # Audit cache invalidation after delete — repo state shifted.
    assert 'repo_audit.invalidate_cache' in _cleanup, (
        "audit cache must be invalidated after deletion"
    )



def test_scan_stale_files_covers_main_udeb_and_recovers_malformed():
    """STA-38: _scan_stale_files must walk `main-udeb`
    (main/debian-installer/) — our built udebs — not just _REPO_SUBDIRS.
    A superseded +asg<R>u<N> udeb there was invisible to BOTH cleanup and
    the chroot stale gate (the e2fsprogs-udeb / keyring-udeb drift that
    survived `repo repair cleanup force` 2026-06-13).  Also pins the
    recovered `malformed` bucket: an on-disk binary the index didn't emit
    (unscannable) is reported."""
    import sys, tempfile, os
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils, repo_audit
    from build import BuildSession

    assert 'main-udeb' in utils._STALE_SCAN_SUBDIRS, (
        "STA-38: main-udeb must be in the stale-scan canon")

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

        # Drop three udebs on disk in main/debian-installer/: current +
        # superseded e2fsprogs-udeb, plus one the index won't list.
        _di = cfg.dir_repo_main_udeb
        os.makedirs(_di, exist_ok=True)
        _cur = 'e2fsprogs-udeb_1.47.0-2+asg1u2_amd64.udeb'
        _old = 'e2fsprogs-udeb_1.47.0-2+asg1u1_amd64.udeb'
        _bad = 'corrupt-thing_1_amd64.udeb'   # not in index → malformed
        for _fn in (_cur, _old, _bad):
            open(os.path.join(_di, _fn), 'w').close()

        class _Tree:
            def __init__(self):
                self.selected_srcs = {'e2fsprogs': object()}
                self.src_pkg_files = {'e2fsprogs': [_cur]}  # current is expected

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = cfg
        _sess.dep_tree = _Tree()
        _sess.udeb_dep_tree = _Tree()
        _sess._superseded_binary_names = lambda: set()

        def _fake_iter(config, subdir, refresh=False):
            if subdir == 'main-udeb':
                for _v in ('+asg1u1', '+asg1u2'):
                    _fn = f'e2fsprogs-udeb_1.47.0-2{_v}_amd64.udeb'
                    yield (_fn, {
                        'Package': 'e2fsprogs-udeb', 'Source': 'e2fsprogs',
                        'Version': f'1.47.0-2{_v}',
                        'Filename': f'pool/{_fn}', 'Size': '100'})
            # corrupt-thing deliberately NOT emitted (unscannable)
            return

        with patch.object(repo_audit, 'iter_packages_all_versions',
                          side_effect=_fake_iter):
            (_orphan, _drift, _foreign, _malformed,
             _total) = _sess._scan_stale_files()

        # main-udeb WAS walked → the superseded +asg1u1 is drift.
        assert any('+asg1u1' in _fn for _sub, _fn, *_ in _drift), (
            f"superseded main-udeb +asg1u1 not flagged as drift: {_drift}")
        # current +asg1u2 is NOT drift.
        assert not any('+asg1u2' in _fn for _sub, _fn, *_ in _drift), _drift
        # The drift carries the 'main-udeb' label (so deletion resolves the dir).
        assert all(_sub == 'main-udeb' for _sub, _fn, *_ in _drift), _drift
        # corrupt-thing on disk but absent from the index → malformed.
        assert any('corrupt-thing' in _m for _m in _malformed), (
            f"unscannable on-disk udeb not reported malformed: {_malformed}")



def test_scan_stale_files_prunes_superseded_unselected_sibling():
    """STA-38 follow-up: a superseded lower version is drift even when the
    binary is NOT selected (a production sibling).  Single-snapshot local
    repo (UPD-01) keeps exactly one version per (name, pristine-base, arch);
    the previous classifier only deduped EXPECTED keys, so a superseded
    production sibling (e2fsprogs comerr-dev / ss-dev / fuse2fs +asg1u1
    after the +asg1u2 rebuild) accumulated forever and rode onto the
    installer ISO's /cdrom/pool.  The CURRENT (highest) sibling is still
    kept."""
    import sys, tempfile, os
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    from build import BuildSession

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
        _md = cfg.dir_repo_main
        os.makedirs(_md, exist_ok=True)
        _old = 'comerr-dev_2.1-1.47.0-2+asg1u1_amd64.deb'
        _cur = 'comerr-dev_2.1-1.47.0-2+asg1u2_amd64.deb'
        for _f in (_old, _cur):
            open(os.path.join(_md, _f), 'w').close()

        class _Tree:
            def __init__(self):
                # e2fsprogs source IS selected, but comerr-dev (a -dev
                # sibling) is NOT a predicted install target.
                self.selected_srcs = {'e2fsprogs': object()}
                self.src_pkg_files = {
                    'e2fsprogs': ['e2fsprogs_1.47.0-2+asg1u2_amd64.deb']}

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = cfg
        _sess.dep_tree = _Tree()
        _sess.udeb_dep_tree = _Tree()
        _sess._superseded_binary_names = lambda: set()

        def _fake_iter(config, subdir, refresh=False):
            if subdir == 'main':
                for _v in ('+asg1u1', '+asg1u2'):
                    _fn = f'comerr-dev_2.1-1.47.0-2{_v}_amd64.deb'
                    yield (_fn, {
                        'Package': 'comerr-dev', 'Source': 'e2fsprogs',
                        'Version': f'2.1-1.47.0-2{_v}',
                        'Filename': f'pool/{_fn}', 'Size': '100'})
            return

        with patch.object(repo_audit, 'iter_packages_all_versions',
                          side_effect=_fake_iter):
            (_orphan, _drift, _foreign, _malformed,
             _total) = _sess._scan_stale_files()

        _drift_fns = [_fn for _sub, _fn, *_ in _drift]
        assert _old in _drift_fns, (
            f"superseded unselected sibling not pruned: {_drift_fns}")
        assert _cur not in _drift_fns, (
            f"current sibling must be kept, not flagged: {_drift_fns}")
        # Current sibling is KEEP — not mis-flagged as orphan (source IS
        # selected) and not drift.
        assert not _orphan, (
            f"current production sibling wrongly flagged orphan: {_orphan}")



def test_scan_orphaned_sidecars_detects_and_cleanup_sweeps():
    """STA-54 follow-up: `.verified` sha-cache sidecars whose .deb/.udeb is
    gone (left by source-build output replacement, pre-STA-38 cleanups,
    lifecycle pruning) accumulate as harmless cruft.  `_scan_orphaned_
    sidecars` finds them (read-only, no dep tree) and `repo repair cleanup`
    sweeps them.  A sidecar whose binary still exists must be preserved."""
    import sys, tempfile, os
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

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
        _md = cfg.dir_repo_main
        os.makedirs(_md, exist_ok=True)
        # live binary + its sidecar (must be kept)
        open(os.path.join(_md, 'foo_1_amd64.deb'), 'w').close()
        open(os.path.join(_md, 'foo_1_amd64.deb.verified'), 'w').close()
        # orphan sidecars (binary gone)
        open(os.path.join(_md, 'gone_2_amd64.deb.verified'), 'w').close()
        open(os.path.join(_md, 'e2fsprogs_1.47.0-2+asg1u1_amd64.deb.verified'),
             'w').close()
        # a sidecar in the udeb component too
        _di = cfg.dir_repo_main_udeb
        os.makedirs(_di, exist_ok=True)
        open(os.path.join(_di, 'bar-udeb_3_amd64.udeb.verified'), 'w').close()

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = cfg
        _orphans = _sess._scan_orphaned_sidecars()
        _names = sorted(_f for _sub, _f in _orphans)
        assert _names == [
            'bar-udeb_3_amd64.udeb.verified',
            'e2fsprogs_1.47.0-2+asg1u1_amd64.deb.verified',
            'gone_2_amd64.deb.verified',
        ], _names
        # The live binary's sidecar is NOT flagged.
        assert not any('foo' in _f for _sub, _f in _orphans), _orphans

        # Simulate the cleanup sweep (the same os.remove the command runs).
        for _sub, _f in _orphans:
            os.remove(os.path.join(cfg.deb_dir_for(_sub), _f))
        assert _sess._scan_orphaned_sidecars() == [], "sweep incomplete"
        assert os.path.exists(os.path.join(_md, 'foo_1_amd64.deb.verified')), \
            "live sidecar wrongly removed"



def test_cmd_package_cleanup_sweeps_orphan_sidecars_source_pin():
    """STA-54 follow-up: cmd_package_cleanup must scan + delete orphaned
    sidecars (the systematic sweep), not only the per-deleted-binary drop."""
    _body = _session_source()
    import re
    _m = re.search(
        r"\n    def cmd_package_cleanup\b.*?(?=\n    def \w)", _body, re.DOTALL)
    assert _m, "cmd_package_cleanup not found"
    _fn = _m.group(0)
    assert 'self._scan_orphaned_sidecars()' in _fn, (
        "cleanup must scan for orphaned sidecars")
    assert 'orphan sidecar' in _fn.lower(), (
        "cleanup must report orphaned sidecars in its summary")



def test_cmd_package_cleanup_deletes_via_subdir_label_and_drops_sidecar():
    """STA-38: cleanup resolves the on-disk path from the SCANNED `_sub`
    label via deb_dir_for (not deb_dest_for_filename, which defaulted
    component='main' and mis-routed a non-free-firmware .deb to main/ →
    delete failure), and removes the orphaned `.verified` sidecar with
    each binary."""
    _body = _session_source()
    import re
    _m = re.search(
        r"\n    def cmd_package_cleanup\b.*?(?=\n    def \w)", _body, re.DOTALL)
    assert _m, "cmd_package_cleanup not found"
    _fn = _m.group(0)
    assert 'deb_dir_for(_sub)' in _fn, (
        "deletion must resolve the dir from the scanned label _sub via "
        "deb_dir_for, not re-derive from the filename")
    assert "deb_dest_for_filename(_f)" not in _fn, (
        "deletion must NOT use deb_dest_for_filename (defaults "
        "component='main' → mis-routes non-main binaries)")
    assert ".verified" in _fn, (
        "cleanup must drop the orphaned .verified sidecar alongside the binary")



def test_print_help_lists_every_registered_category():
    """`print help` mentions every category in CATEGORIES so adding a new
    handler doesn't silently disappear from the help screen."""
    import print_commands
    output = _capture_console_print(lambda: print_commands._print_help(None))
    for name in print_commands.CATEGORIES:
        assert name in output, f"`print help` did not mention {name!r}"



def test_print_dispatch_unknown_category_points_to_help():
    """An unknown category prints a clear pointer to `print help` rather
    than crashing or silently returning."""
    import print_commands
    output = _capture_console_print(
        lambda: print_commands.dispatch(None, 'nonexistent-category')
    )
    assert 'nonexistent-category' in output
    assert 'print help' in output



def test_print_dispatch_empty_category_shows_help():
    """No argument (= empty string) shows the help screen — saves the
    operator from having to discover `print help` separately."""
    import print_commands
    output = _capture_console_print(
        lambda: print_commands.dispatch(None, '')
    )
    for name in print_commands.CATEGORIES:
        assert name in output, f"empty arg should also show help with {name!r}"



def test_print_help_groups_categories_into_sections():
    """The help screen organises categories into named sections so 16+
    entries don't render as one wall of text."""
    import print_commands
    output = _capture_console_print(lambda: print_commands._print_help(None))
    for section in print_commands._HELP_GROUP_ORDER:
        assert f"{section}:" in output, f"help should have section {section!r}"



def test_print_dispatch_passes_extras_to_parametrized_handler():
    """`print pkg <name>` etc. — extras must reach the handler.  Verified
    via the help-text path of the handlers (returns 'Usage:' line when
    extras are missing, different content when they're present)."""
    import print_commands
    # No extras → handler prints 'Usage: print pkg <name>'
    output_no_arg = _capture_console_print(
        lambda: print_commands.dispatch(None, 'pkg')
    )
    assert 'Usage: print pkg' in output_no_arg



def test_fmt_dep_unconstrained():
    """An unconstrained dep tuple renders as the bare name."""
    from print_commands import _fmt_dep
    assert _fmt_dep(('libc6', '', '')) == 'libc6'



def test_fmt_dep_with_constraint():
    """A constrained dep tuple renders as `name (op ver)`."""
    from print_commands import _fmt_dep
    assert _fmt_dep(('libc6', '2.31', '>=')) == 'libc6 (>= 2.31)'
    assert _fmt_dep(('foo', '1.0', '<<')) == 'foo (<< 1.0)'



def test_fmt_dep_group_alternates_with_pipe():
    """An alt-dep group renders with ' | ' between alternatives."""
    from print_commands import _fmt_dep_group
    rendered = _fmt_dep_group([('a', '', ''), ('b', '2', '>=')])
    assert rendered == 'a | b (>= 2)'



def test_print_no_handler_crashes_on_uninitialized_session():
    """Every registered handler should either render something useful or
    print a `run <stage> first` guard message — never raise AttributeError
    or similar on a fresh BuildSession with cache=None / dep_tree=None."""
    import print_commands
    stub = _PrintSessionStub()
    for name, (handler, _group, _desc) in print_commands.CATEGORIES.items():
        try:
            _capture_console_print(lambda h=handler: h(stub))
        except Exception as e:
            raise AssertionError(
                f"handler for {name!r} crashed on uninitialised session: "
                f"{type(e).__name__}: {e}") from e



# ─────────────────────────────────────────────────────────────────────────────
# autorun summary (lives in print_commands.summary)
# ─────────────────────────────────────────────────────────────────────────────

def test_format_duration_seconds_only():
    """Under a minute: just `Ns`."""
    from print_commands import format_duration
    assert format_duration(0) == '0s'
    assert format_duration(7) == '7s'
    assert format_duration(59) == '59s'



def test_format_duration_minutes_seconds():
    """Under an hour: `Mm SSs` with zero-padded seconds."""
    from print_commands import format_duration
    assert format_duration(60) == '1m 00s'
    assert format_duration(125) == '2m 05s'
    assert format_duration(3599) == '59m 59s'



def test_format_duration_hours_minutes_seconds():
    """An hour or more: `Hh MMm SSs` with zero-padding on minutes + seconds."""
    from print_commands import format_duration
    assert format_duration(3600) == '1h 00m 00s'
    assert format_duration(7325) == '2h 02m 05s'
    assert format_duration(36000) == '10h 00m 00s'



def test_autorun_summary_success_includes_counts_and_iso_path():
    """Successful autorun summary (timing-driven path) surfaces wall time,
    all stage counts, and the predicted ISO path with build_iso hint."""
    import datetime as _dt
    import print_commands
    sess = _build_autorun_session_stub(all_done=True, source_build_done=True)
    timing = print_commands.AutorunTiming(
        started=_dt.datetime(2026, 5, 9, 12, 0, 0),
        finished=_dt.datetime(2026, 5, 9, 13, 5, 30),
        elapsed=3930,
        aborted_at=None,
    )
    output = _capture_console_print(
        lambda: print_commands.summary(sess, timing=timing)
    )
    assert 'SUCCESS' in output
    assert '2026-05-09 12:00:00' in output
    assert '2026-05-09 13:05:30' in output
    assert '1h 05m 30s' in output
    assert '32847 package names' in output
    assert '213 canonical packages' in output
    assert '31 source packages' in output
    assert '26 built'    in output
    assert '2 tunneled'  in output
    assert 'verified'    in output
    assert '/tmp/image/athena-0.1-amd64.iso' in output
    assert '/tmp/image/athena-installer-0.1-amd64.iso' in output
    # All-green rendering — both ISOs marked built.
    assert 'ISO live' in output
    assert 'ISO installer' in output



def test_autorun_summary_aborted_marks_stage_and_partial_state():
    """Aborted autorun summary identifies the stage that didn't complete
    and renders 'not built'/'not run' for stages downstream of the abort."""
    import datetime as _dt
    import print_commands
    sess = _build_autorun_session_stub(all_done=False, source_build_done=False)
    timing = print_commands.AutorunTiming(
        started=_dt.datetime(2026, 5, 9, 12, 0, 0),
        finished=_dt.datetime(2026, 5, 9, 12, 30, 0),
        elapsed=1800,
        aborted_at='source sync',
    )
    output = _capture_console_print(
        lambda: print_commands.summary(sess, timing=timing)
    )
    assert 'ABORTED' in output
    assert "'source sync'" in output
    assert '30m 00s' in output
    assert 'Source build   : not run' in output
    assert 'Chroot (live)  : not built' in output
    assert 'Chroot (inst)  : not built' in output



def test_print_state_renders_three_sections_with_all_flags():
    """`print state` groups stages into Shared / Live ISO target /
    Installer ISO target.  Pin section headers + every flag label so a
    future BuildFlags addition doesn't silently drop from the view."""
    import print_commands
    sess = _build_autorun_session_stub(all_done=True, source_build_done=True)
    output = _capture_console_print(
        lambda: print_commands._print_state(sess)
    )
    # Section headers.
    assert 'Shared:' in output
    assert 'Live ISO target:' in output
    assert 'Installer ISO target:' in output
    # Shared rows.
    assert 'cache_build' in output
    assert 'dep_parse' in output
    assert 'source_download' in output
    assert 'container_init' in output
    assert 'source_build' in output
    assert 'signing_key_verified' in output
    # Live rows.
    assert 'chroot_build_live' in output
    assert 'chroot_verify' in output
    assert 'iso_build_live' in output
    # Installer rows.
    assert 'chroot_build_installer' in output
    assert 'iso_build_installer' in output



def test_print_state_renders_unticked_when_flags_unset():
    """With every BuildFlag False, all rows show the `·` (unticked)
    glyph; no rows accidentally hard-coded to True."""
    import print_commands
    sess = _build_autorun_session_stub(all_done=False, source_build_done=False)
    # Force the two flags the stub does set to True back to False so we
    # exercise the pure-unticked path.
    sess.flags.cache_ready = False
    sess.flags.dep_check_ready = False
    output = _capture_console_print(
        lambda: print_commands._print_state(sess)
    )
    # No ticked rows.
    assert '[✓]' not in output, (
        f"Expected no ticked rows when every flag is False, got:\n{output}"
    )
    # All thirteen rows should be present as unticked (incl. the disk
    # surface's chroot_build_disk + iso_build_disk).
    assert output.count('[·]') == 13, (
        "Expected 13 unticked rows (one per BuildFlag), got "
        f"{output.count('[·]')}:\n{output}"
    )



def test_print_summary_without_timing_renders_state_snapshot():
    """Operator-invoked `print summary` (no timing) shows a state snapshot
    rather than SUCCESS/ABORTED — same per-stage rows, no wall clock."""
    import print_commands
    sess = _build_autorun_session_stub(all_done=True, source_build_done=True)
    output = _capture_console_print(
        lambda: print_commands.summary(sess, timing=None)
    )
    # State header, not SUCCESS/ABORTED
    assert 'Pipeline summary' in output
    assert 'SUCCESS' not in output
    assert 'ABORTED' not in output
    # Wall-clock rows must be absent
    assert 'Started' not in output
    assert 'Wall time' not in output
    # But per-stage rows present
    assert '32847 package names' in output
    assert '26 built' in output
    assert 'verified' in output



def test_print_summary_dispatch_through_handler():
    """`print summary` via the CATEGORIES dispatch reaches the no-timing
    branch (operator path)."""
    import print_commands
    sess = _build_autorun_session_stub(all_done=True, source_build_done=True)
    output = _capture_console_print(
        lambda: print_commands.dispatch(sess, 'summary')
    )
    assert 'Pipeline summary' in output
    assert 'SUCCESS' not in output



def test_canonical_names_filters_virtual_aliases_from_cohort():
    """REGRESSION pin (2026-05-21): cohort scopes must exclude virtual-
    alias names from selected_pkgs.keys().  The audit's
    audit_conflict_cohort scope-checks names against this set; including
    virtual aliases triggers false-positive conflicts on the canonical
    Debian fork-replaces-upstream idiom (`X Provides: Y, X Breaks: Y`
    means 'I take over Y's slot' — `Y` should NOT count as a separate
    cohort member).

    Concrete cases that triggered this fix:
      - fuse3 Provides: fuse + fuse3 Breaks: fuse
      - athena-tasksel-data Provides: tasksel-data
                            + Conflicts: tasksel-data
    Both flagged spurious conflicts before the canonical-only filter
    landed; both clear cleanly after."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    class _Pkg:
        def __init__(self, canonical):
            self._fields = {'Package': canonical}
        def __getitem__(self, k): return self._fields[k]

    _real_fuse3 = _Pkg('fuse3')
    _real_dirmngr = _Pkg('dirmngr')
    # selected_pkgs has both canonical names AND virtual aliases.
    # Mirrors parse_dependency:L494's Provides-walk behavior.
    class _Tree:
        selected_pkgs = {
            'fuse3':   _real_fuse3,
            'fuse':    _real_fuse3,    # virtual alias from fuse3's Provides
            'dirmngr': _real_dirmngr,
        }

    _canonical = BuildSession._canonical_names(_Tree)
    assert 'fuse3' in _canonical, "canonical name dropped"
    assert 'dirmngr' in _canonical
    assert 'fuse' not in _canonical, (
        "virtual alias 'fuse' (pointing to fuse3) leaked into cohort — "
        "would cause audit_conflict_cohort to false-positive on "
        "fuse3 Breaks: fuse"
    )



def test_cmd_init_container_gated_on_cache_ready():
    """REGRESSION pin (2026-05-21): cmd_init_container must refuse to
    run before cmd_build_cache.  BuildContainer is constructed with
    cache=self.cache; if cache is None, source-build's
    Source.build_depends(cache=None) returns raw groups without virtual-
    provider expansion.  In-container `apt-get install libcurl4-dev`
    (or any other virtual with multiple providers) then fails non-
    interactively with `Package 'X' has no installation candidate`,
    blowing up the source build cryptically.

    Catching the ordering at the command boundary keeps the failure
    actionable instead of silently breaking later builds."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    _m = re.search(r"\n    def cmd_init_container\b.*?\n    def \w",
                   _body, re.DOTALL)
    assert _m is not None, "cmd_init_container body not found"
    _body_text = _m.group(0)
    # Gate must read the flag and return early when False.
    assert 'self.flags.cache_ready' in _body_text, (
        "cmd_init_container no longer reads self.flags.cache_ready — "
        "the cache-build ordering gate regressed; in-container apt-installs "
        "for virtual Build-Depends will fail without diagnosis"
    )
    # The body must guard *before* mutating build_container_ready /
    # constructing the BuildContainer (otherwise we'd reset state then
    # bail, leaving inconsistent flags).
    _gate_idx = _body_text.find('self.flags.cache_ready')
    _construct_idx = _body_text.find('buildcontainer.BuildContainer(')
    assert _gate_idx < _construct_idx, (
        "cache_ready gate must precede BuildContainer construction "
        "(don't reset build_container_ready when bailing on the gate)"
    )



def test_buildconfig_argparse_exposes_live_and_installer_list_flags():
    """BuildConfig's argparse must accept --live-list and --installer-list,
    and the resulting BuildConfig instance must carry livelist_path and
    installerlist_path attributes pointing at sensible defaults."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import BuildConfig
    with tempfile.TemporaryDirectory() as _tmp:
        # Reuse the existing _stub_build_conf helper if available; otherwise
        # construct by hand the minimum BuildConfig needs to parse.
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir, exist_ok=True)
        # Minimal build.conf — borrows shape from the project's real one.
        with open(os.path.join(_cfg_dir, 'build.conf'), 'w') as f:
            f.write(
                "[Build]\nARCH=amd64\nDISTRIBUTION=Testdistro\nCODENAME=athena\nVERSION=0.0\n"
                "[Base]\nBASEURL=http://example/\nBASEID=debian\n"
                "RELEASE=athena\nBASEVERSION=1\n"
                "[Mirror.test]\n"
                "[Security]\nEnabled=false\nKeyring=\n"
                "[Snapshot]\nEnabled=false\nTimestamp=latest\n"
            )
        with open(os.path.join(_cfg_dir, 'pkg.list'), 'w') as f: f.write('')
        with open(os.path.join(_cfg_dir, 'live.list'), 'w') as f: f.write('')
        with open(os.path.join(_cfg_dir, 'installer.list'), 'w') as f: f.write('')
        _saved_argv = sys.argv
        sys.argv = ['build.py',
                    '--working-dir', _tmp,
                    '--config-file', os.path.join(_cfg_dir, 'build.conf'),
                    '--pkg-list',    os.path.join(_cfg_dir, 'pkg.list'),
                    '--live-list',   os.path.join(_cfg_dir, 'live.list'),
                    '--installer-list', os.path.join(_cfg_dir, 'installer.list')]
        try:
            cfg = BuildConfig()
        finally:
            sys.argv = _saved_argv
        assert cfg.livelist_path      == os.path.join(_cfg_dir, 'live.list')
        assert cfg.installerlist_path == os.path.join(_cfg_dir, 'installer.list')



def test_read_pkg_list_filters_comments_blanks_and_already_selected():
    """_read_pkg_list strips '#' lines, blanks, and any name already in
    the already_selected set (pkg.list closure)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    with tempfile.NamedTemporaryFile('w', suffix='.list', delete=False) as fh:
        fh.write(
            "# A comment\n"
            "\n"
            "live-boot\n"
            "  live-config  \n"          # leading/trailing whitespace
            "already-here\n"               # in already_selected → filtered
            "user-setup\n"
            "# another\n"
        )
        _path = fh.name
    try:
        out = BuildSession._read_pkg_list(_path, already_selected={'already-here'})
    finally:
        os.unlink(_path)
    assert out == ['live-boot', 'live-config', 'user-setup']



def test_read_pkg_list_missing_file_returns_empty():
    """A missing path → empty list, no exception (logged warning is fine)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    out = BuildSession._read_pkg_list('/nonexistent/path/to/list', set())
    assert out == []



def test_pass_iii_dedups_to_canonical_names_for_pkg_group_pkg_names():
    """Pass III in cmd_parse_dependency must collapse virtual aliases
    in selected_pkgs.keys() down to canonical Package: names when
    populating pkg_group_pkg_names.  selected_pkgs is keyed by both
    real and virtual names (the resolver registers virtuals as
    aliases pointing to their canonical providers); the per-group
    set must NOT contain virtuals because:
      - tasksel's task_avail() runs `apt-cache dumpavail` on each
        Key entry, and dumpavail only emits real Package: stanzas.
        A single virtual in the Key list silently hides the entire
        task from the menu.  Caught 2026-05-15: athena-development-tools
        had 10 virtuals (cpp, c++-compiler, git-core, …) and never
        appeared in tasksel.
      - install batches (chroot._compute_install_batches) operate
        on canonical names; passing virtuals through the filter
        is a no-op at best, confusing diagnostic output at worst.

    This test simulates the merge by directly invoking the dedup
    expression (cleaner than standing up a full Cache+DependencyTree
    fixture) and pins the canonical-only invariant."""
    # Simulated Pass III state: selected_pkgs has BOTH real names AND
    # virtual aliases, all pointing at the canonical Package object.
    class _StubPkg(dict):
        pass
    _gcc12 = _StubPkg({'Package': 'gcc-12'})
    _git = _StubPkg({'Package': 'git'})
    _libc6_dev = _StubPkg({'Package': 'libc6-dev'})
    selected_pkgs = {
        'gcc-12':       _gcc12,   # real
        'cpp':          _gcc12,   # virtual alias → gcc-12
        'c++-compiler': _gcc12,   # virtual alias → gcc-12
        'git':          _git,     # real
        'git-core':     _git,     # virtual alias → git
        'libc6-dev':    _libc6_dev,  # real
        'libc-dev':     _libc6_dev,  # virtual alias → libc6-dev
    }
    _pre_group_keys = set()  # nothing selected before this group
    _delta_keys = set(selected_pkgs.keys()) - _pre_group_keys

    # The dedup expression from build.py:781
    _canonical = {
        selected_pkgs[_n]['Package']
        for _n in _delta_keys
        if _n in selected_pkgs
    }
    assert _canonical == {'gcc-12', 'git', 'libc6-dev'}, (
        f"expected canonical-only set; got {_canonical}"
    )
    # No virtuals leak through
    for _v in ('cpp', 'c++-compiler', 'git-core', 'libc-dev'):
        assert _v not in _canonical, f"virtual {_v!r} leaked into group set"





def test_print_udebs_handles_no_udeb_tree_gracefully():
    """`print udebs` before parse_dependency runs (udeb_dep_tree=None)
    prints a "re-run dep parse" message instead of crashing on attr-access."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui as _tui
    from print_commands import _print_udebs

    captured = []
    class _Console:
        def print(self, msg, *_a, **_kw): captured.append(msg)
    _saved = _tui.console
    _tui.console = _Console()
    try:
        # Stub session that passes _require_dep_check (flag set, dep_tree
        # present) but has udeb_dep_tree=None.
        class _Flags: dep_check_ready = True
        class _Sess:
            flags = _Flags()
            dep_tree = object()
            udeb_dep_tree = None
        _print_udebs(_Sess())
    finally:
        _tui.console = _saved
    assert any('udeb' in m.lower() and ('re-run' in m.lower()
                                         or 'not built' in m.lower())
               for m in captured), captured



def test_print_udebs_lists_udeb_closure_when_tree_populated():
    """When udeb_dep_tree.selected_pkgs has entries, `print udebs` emits a
    one-line-per-udeb listing with version + source.  Uses a Version-like
    stub that rejects width-format-specs (matches real python-debian
    Version behaviour) so a future regression that drops the str() coerce
    re-fails at this test, not in production output."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui as _tui
    from print_commands import _print_udebs

    class _StubVersion:
        """Mimic debian.debian_support.Version: __str__ works, __format__
        with a non-empty spec raises TypeError (the production bug we fixed)."""
        def __init__(self, v): self._v = v
        def __str__(self): return self._v
        def __format__(self, spec):
            if spec:
                raise TypeError(
                    "unsupported format string passed to Version.__format__"
                )
            return self._v

    captured = []
    class _Console:
        def print(self, msg, *_a, **_kw): captured.append(msg)
    _saved = _tui.console
    _tui.console = _Console()
    try:
        class _Flags: dep_check_ready = True
        seed = _FakePkg('cdebconf-text-udeb', source='cdebconf',
                        filename='cdebconf-text-udeb_0.270_amd64.udeb')
        seed.version = _StubVersion('0.270')
        seed2 = _FakePkg('rootskel', source='rootskel',
                         filename='rootskel_1.136_all.udeb')
        seed2.version = _StubVersion('1.136')
        class _UdebTree:
            selected_pkgs = {'cdebconf-text-udeb': seed, 'rootskel': seed2}
            selected_srcs = {'cdebconf': object(), 'rootskel': object()}
        class _Sess:
            flags = _Flags()
            dep_tree = object()
            udeb_dep_tree = _UdebTree()
        _print_udebs(_Sess())
    finally:
        _tui.console = _saved
    joined = '\n'.join(captured)
    assert 'cdebconf-text-udeb' in joined
    assert 'rootskel' in joined
    assert '2 udeb(s)' in joined
    assert '2 source(s)' in joined
    assert '0.270' in joined
    assert '1.136' in joined



def test_print_extras_lists_recommended_packages():
    """`print extras` enumerates the entries with their source
    classification (extras-only vs mixed)."""
    import print_commands

    class _StubSrc:
        def __init__(self, pkgs): self.pkgs = pkgs

    class _Pkg:
        def __init__(self, name): self._n = name; self.version = '1.0'
        def __getitem__(self, k): return self._n if k == 'Package' else None
        def get(self, k, default=''): return default

    class _DT:
        selected_pkgs = {
            'firefox':         _Pkg('firefox'),
            'firefox-l10n-en': _Pkg('firefox-l10n-en'),
            'libnss3-tools':   _Pkg('libnss3-tools'),
        }
        selected_srcs = {
            'firefox': _StubSrc(['firefox_1.0_amd64.deb',
                                 'firefox-l10n-en_1.0_amd64.deb']),
            'libnss3': _StubSrc(['libnss3-tools_3.0_amd64.deb']),
        }
        extras_pkg_names = {'firefox-l10n-en', 'libnss3-tools'}
        extras_src_names = {'libnss3'}

    class _Sess:
        dep_tree = _DT()
        class flags: dep_check_ready = True
        config = None  # not touched by _print_extras

    output = _capture_console_print(
        lambda: print_commands._print_extras(_Sess())
    )
    assert 'firefox-l10n-en' in output
    assert 'libnss3-tools' in output
    assert 'extras-only' in output  # libnss3
    assert 'mixed source' in output  # firefox-l10n-en



def test_print_extras_handles_empty_extras_set():
    """When extras_pkg_names is empty, the view explains rather than
    rendering an empty list."""
    import print_commands

    class _DT:
        selected_pkgs = {}
        selected_srcs = {}
        extras_pkg_names = set()
        extras_src_names = set()

    class _Sess:
        dep_tree = _DT()
        class flags: dep_check_ready = True
        config = None

    output = _capture_console_print(
        lambda: print_commands._print_extras(_Sess())
    )
    assert 'IncludeRecommends' in output



# ─── source_build [profiles] override parsing ────────────────────

def test_source_build_args_no_args_defaults_to_pkg_subset():
    """Bare `source build` → subset='pkg', not 'live'.
    Locked decision — pkg is the user-choices layer, live is now an
    explicit additive on top."""
    from build import BuildSession
    err, force, subset, names, override = \
        BuildSession._parse_source_build_args(())
    assert err is None
    assert (force, subset, names, override) == (False, 'pkg', [], None)



def test_source_build_args_pkg_subset_explicit():
    """`source build pkg` is the explicit form of the bare default."""
    from build import BuildSession
    err, _f, subset, names, _o = \
        BuildSession._parse_source_build_args(('pkg',))
    assert err is None
    assert subset == 'pkg' and names == []



def test_source_build_args_live_subset_explicit():
    """`source build live` resolves to subset='live' (Phase 4 — now
    means live-exclusives only, not the full live closure)."""
    from build import BuildSession
    err, _f, subset, names, _o = \
        BuildSession._parse_source_build_args(('live',))
    assert err is None
    assert subset == 'live' and names == []



def test_source_build_args_installer_subset_recognised():
    """`source build installer` parses to subset='installer'."""
    from build import BuildSession
    err, _f, subset, names, _o = \
        BuildSession._parse_source_build_args(('installer',))
    assert err is None
    assert subset == 'installer' and names == []



def test_source_build_args_recommended_subset_recognised():
    """`source build recommended` parses to subset='recommended' (not the
    old `_recommended is True` boolean)."""
    from build import BuildSession
    err, _f, subset, names, _o = \
        BuildSession._parse_source_build_args(('recommended',))
    assert err is None
    assert subset == 'recommended' and names == []



def test_source_build_args_force_flag_anywhere():
    """`force` is detectable in any position, case-insensitive.  Bare
    `force` (no other args) defaults the subset to 'pkg' (Phase 4)."""
    from build import BuildSession
    for argv in (('force',), ('Force',), ('foo', 'FORCE'), ('force', 'foo')):
        err, force, _s, _n, _o = BuildSession._parse_source_build_args(argv)
        assert err is None and force is True, f"args={argv!r}"
    # Bare `force` defaults subset to 'pkg' since there are no names and
    # no other subset selector.
    _, _, subset, names, _ = BuildSession._parse_source_build_args(('force',))
    assert subset == 'pkg' and names == []



def test_source_build_args_subsets_mutually_exclusive():
    """Two subset selectors at once → parse error.  Phase 4: 'pkg' is a
    subset too, so pkg+anything is rejected the same way."""
    from build import BuildSession
    for argv in (('pkg', 'live'),
                 ('pkg', 'installer'),
                 ('live', 'installer'),
                 ('installer', 'recommended'),
                 ('live', 'recommended'),
                 ('all', 'pkg'),
                 ('all', 'recommended'),
                 ('pkg', 'live', 'installer', 'recommended', 'all')):
        err, *_ = BuildSession._parse_source_build_args(argv)
        assert err is not None, f"args={argv!r}"
        assert 'pick at most one' in err, f"args={argv!r}: {err!r}"



def test_obs02_build_history_ledger_append_and_read():
    """OBS-02: archive_build_record appends a full build record per line to
    log/build-history.jsonl (sibling of log/build/), and read_build_history
    projects journal + current build.json records to display summaries
    (done/failed → PASS/FAIL, built_version → intended_version fallback),
    oldest-first, skipping malformed records."""
    import utils as _u
    with tempfile.TemporaryDirectory() as _root:
        _log_build = os.path.join(_root, 'log', 'build')
        os.makedirs(_log_build)
        _u.archive_build_record(_log_build, {
            'package': 'glibc', 'phase': 'done', 'built_version': '2.36-9',
            'finished': '2026-06-16T10:00:00Z', 'elapsed_seconds': 420.1,
            'exit_code': 0})
        _u.archive_build_record(_log_build, {
            'package': 'gnome-shell', 'phase': 'failed',
            'intended_version': '43-1', 'finished': '2026-06-16T11:00:00Z',
            'exit_code': 1})
        _u.archive_build_record(_log_build, None)        # no-op, no line
        # ledger lives one level up from log/build/
        _ledger = os.path.join(_root, 'log', 'build-history.jsonl')
        assert os.path.isfile(_ledger)
        with open(_ledger, 'a') as _fh:
            _fh.write('{not json\n\n')                    # malformed + blank
        _rows = _u.read_build_history(_log_build)
        assert len(_rows) == 2, _rows
        assert [_r['status'] for _r in _rows] == ['PASS', 'FAIL']
        assert _rows[0]['version'] == '2.36-9'            # built_version
        assert _rows[1]['version'] == '43-1'             # intended_version fallback
        assert _rows[0]['package'] == 'glibc'

        # OBS-02 fold: read_build_history also surfaces existing <pkg>.build.json
        # records (builds that predate the ledger), deduped by (package, ts).
        import json as _json
        # a package NOT in the ledger → folded in
        with open(os.path.join(_log_build, 'foo.build.json'), 'w') as _fh:
            _json.dump({'package': 'foo', 'phase': 'done', 'built_version': '9-1',
                        'finished': '2026-06-16T09:00:00Z', 'elapsed_seconds': 5.0}, _fh)
        # a build.json matching the ledger glibc run (same package+ts) → NOT double-counted
        with open(os.path.join(_log_build, 'glibc.build.json'), 'w') as _fh:
            _json.dump({'package': 'glibc', 'phase': 'done', 'built_version': '2.36-9',
                        'finished': '2026-06-16T10:00:00Z', 'elapsed_seconds': 420.1}, _fh)
        # an interrupted record (no terminal phase/status) → skipped
        with open(os.path.join(_log_build, 'bar.build.json'), 'w') as _fh:
            _json.dump({'package': 'bar', 'phase': 'container_exited'}, _fh)
        _rows2 = _u.read_build_history(_log_build)
        _pkgs = [_r['package'] for _r in _rows2]
        assert _pkgs == ['foo', 'glibc', 'gnome-shell'], _pkgs   # chronological, deduped
        assert _pkgs.count('glibc') == 1                          # no double-count
        assert 'bar' not in _pkgs                                 # interrupted skipped



def test_refresh_patches_invalidates_record_when_patch_newer():
    """COMP-02 phase C: _refresh_patches must drop the build.json record
    when the patch CONTENT has changed since the last successful build.
    Without this, autorun's source-build step skips packages with
    `[SKIPPED] already built` even when the operator just modified a
    patch (caught 2026-05-13 with the base-installer keyring patch —
    autorun ran but the .udeb was the May-10 build, the patch never
    applied, install failed with 'No public key').

    Two-stage check: mtime gate + content hash.  This test exercises
    the real-change path: stored patch_set_hash on the record disagrees
    with the on-disk patch content → mtime gate trips → hash confirms
    divergence → record is removed."""
    import sys, tempfile, time
    from unittest.mock import MagicMock, patch as mock_patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    from package import Source
    import utils as _u

    with tempfile.TemporaryDirectory() as _root:
        _log_build = os.path.join(_root, 'log', 'build')
        _patch_dir = os.path.join(_root, 'patch', 'source', 'foo', '1.0')
        os.makedirs(_log_build)
        os.makedirs(_patch_dir)
        # Plant a build record with a STALE patch_set_hash.
        _rec = _u.new_build_record(
            package='foo', intended_version='1.0',
            patch_set_hash='deadbeef' * 8,
        )
        _rec.update({'phase': 'done', 'status': 'PASS'})
        _u.write_build_record(_log_build, _rec)
        _record = os.path.join(_log_build, 'foo' + _u.BUILD_RECORD_SUFFIX)
        # Write a patch with NEW content.
        _patch = os.path.join(_patch_dir, '9001-test.patch')
        time.sleep(0.01)
        with open(_patch, 'w') as fh:
            fh.write(
                'Description: t\nAuthor: t\nForwarded: no\n'
                'Last-Update: 2026-05-13\n'
                '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n')
        _now = time.time()
        os.utime(_record, (_now - 100, _now - 100))
        os.utime(_patch, (_now, _now))

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = MagicMock()
        _sess.config.dir_patch_source = os.path.join(_root, 'patch', 'source')
        _sess.config.dir_log = os.path.join(_root, 'log')
        _src = Source.__new__(Source)
        _src.package = 'foo'
        _src.version = '1.0'
        _src.patch_list = []
        _sess.dep_tree = MagicMock()
        _sess.dep_tree.selected_srcs = {'foo': _src}
        _sess.udeb_dep_tree = None

        with mock_patch('build.console'), \
             mock_patch('build.utils.check_dep3_header', return_value=[]):
            _sess._refresh_patches()
        assert not os.path.exists(_record), (
            f"_refresh_patches must invalidate stale record; still at {_record}"
        )
        assert _src.patch_list == ['9001-test.patch'], _src.patch_list



def test_refresh_patches_skips_invalidation_for_header_only_edit():
    """Two-stage invalidation: a patch whose MTIME is newer than the
    record but whose CONTENT matches the recorded patch_set_hash must
    NOT trigger a rebuild.  Covers the common case of editing only the
    DEP-3 header / commentary of an existing patch — diff hunks are
    byte-for-byte identical, no rebuild needed.  Caught when CONF-08
    annotations were added to three doc patches and the user noticed
    libyaml/protobuf/p7zip would otherwise rebuild despite no real
    change."""
    import sys, tempfile, time
    from unittest.mock import MagicMock, patch as mock_patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    from package import Source
    import utils as _u

    with tempfile.TemporaryDirectory() as _root:
        _log_build = os.path.join(_root, 'log', 'build')
        _patch_dir = os.path.join(_root, 'patch', 'source', 'foo', '1.0')
        os.makedirs(_log_build); os.makedirs(_patch_dir)
        _patch = os.path.join(_patch_dir, '9001-test.patch')
        _content = (
            'Description: t\nAuthor: t\nForwarded: no\nLast-Update: 2026-05-13\n'
            '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'
        )
        with open(_patch, 'w') as fh: fh.write(_content)
        # Baseline hash matches the current on-disk content.
        _rec = _u.new_build_record(
            package='foo', intended_version='1.0',
            patch_set_hash=_u.patch_set_hash(_patch_dir, ['9001-test.patch']),
        )
        _rec.update({'phase': 'done', 'status': 'PASS'})
        _u.write_build_record(_log_build, _rec)
        _record = os.path.join(_log_build, 'foo' + _u.BUILD_RECORD_SUFFIX)
        # Patch mtime > record mtime (header-only edit scenario).
        _now = time.time()
        os.utime(_record, (_now - 100, _now - 100))
        os.utime(_patch, (_now, _now))

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = MagicMock()
        _sess.config.dir_patch_source = os.path.join(_root, 'patch', 'source')
        _sess.config.dir_log = os.path.join(_root, 'log')
        _src = Source.__new__(Source)
        _src.package = 'foo'; _src.version = '1.0'; _src.patch_list = []
        _sess.dep_tree = MagicMock()
        _sess.dep_tree.selected_srcs = {'foo': _src}
        _sess.udeb_dep_tree = None

        with mock_patch('build.console'), \
             mock_patch('build.utils.check_dep3_header', return_value=[]):
            _sess._refresh_patches()
        assert os.path.exists(_record), (
            "header-only patch edit (same hash) must NOT invalidate record")
        # Record mtime should be touched past the patch mtime so future
        # patch_refresh runs don't keep re-entering the hash branch.
        assert os.path.getmtime(_record) >= os.path.getmtime(_patch), (
            "_refresh_patches must touch record mtime past patch mtime; "
            f"record={os.path.getmtime(_record)} patch={os.path.getmtime(_patch)}"
        )



def test_refresh_patches_invalidates_when_patches_removed():
    """Patch deletion: empty patch_list with a non-empty patch_set_hash
    on the build record (from a previous build that applied patches)
    → hash differs from current empty-set hash → drop the record.
    Comes for free with the content-hash schema; an mtime-only check
    could not detect this case (deletion doesn't bump any patch's
    mtime)."""
    import sys, tempfile
    from unittest.mock import MagicMock, patch as mock_patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    from package import Source
    import utils as _u

    with tempfile.TemporaryDirectory() as _root:
        _log_build = os.path.join(_root, 'log', 'build')
        # Patch dir does NOT exist (patches were removed).
        os.makedirs(_log_build)
        # Stale baseline hash reflecting the now-deleted patch set.
        _rec = _u.new_build_record(
            package='foo', intended_version='1.0',
            patch_set_hash='cafef00d' * 8,
        )
        _rec.update({'phase': 'done', 'status': 'PASS'})
        _u.write_build_record(_log_build, _rec)
        _record = os.path.join(_log_build, 'foo' + _u.BUILD_RECORD_SUFFIX)

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = MagicMock()
        _sess.config.dir_patch_source = os.path.join(_root, 'patch', 'source')
        _sess.config.dir_log = os.path.join(_root, 'log')
        _src = Source.__new__(Source)
        _src.package = 'foo'; _src.version = '1.0'; _src.patch_list = []
        _sess.dep_tree = MagicMock()
        _sess.dep_tree.selected_srcs = {'foo': _src}
        _sess.udeb_dep_tree = None

        with mock_patch('build.console'), \
             mock_patch('build.utils.check_dep3_header', return_value=[]):
            _sess._refresh_patches()
        assert not os.path.exists(_record), (
            "patch removal must invalidate the now-stale build record")



def test_refresh_patches_keeps_result_when_patch_older_than_result():
    """Inverse: if the .result is NEWER than all patches in the dir,
    the build is up-to-date and _refresh_patches must NOT invalidate
    the result (would force unnecessary rebuilds on every autorun)."""
    import sys, tempfile, time
    from unittest.mock import MagicMock, patch as mock_patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    from package import Source

    with tempfile.TemporaryDirectory() as _root:
        _log_build = os.path.join(_root, 'log', 'build')
        _patch_dir = os.path.join(_root, 'patch', 'source', 'foo', '1.0')
        os.makedirs(_log_build)
        os.makedirs(_patch_dir)
        _result = os.path.join(_log_build, 'foo.result')
        _patch = os.path.join(_patch_dir, '9001-test.patch')
        with open(_patch, 'w') as fh: fh.write(
            'Description: t\nAuthor: t\nForwarded: no\nLast-Update: 2026-05-13\n'
            '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'
        )
        with open(_result, 'w') as fh: fh.write('PASS\n')
        _now = time.time()
        os.utime(_patch, (_now - 100, _now - 100))
        os.utime(_result, (_now, _now))

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = MagicMock()
        _sess.config.dir_patch_source = os.path.join(_root, 'patch', 'source')
        _sess.config.dir_log = os.path.join(_root, 'log')
        _src = Source.__new__(Source)
        _src.package = 'foo'; _src.version = '1.0'; _src.patch_list = []
        _sess.dep_tree = MagicMock()
        _sess.dep_tree.selected_srcs = {'foo': _src}
        _sess.udeb_dep_tree = None

        with mock_patch('build.console'), \
             mock_patch('build.utils.check_dep3_header', return_value=[]):
            _sess._refresh_patches()
        assert os.path.exists(_result), (
            "result NEWER than all patches must NOT be invalidated"
        )



def test_autorun_installer_runs_source_build_then_source_build_installer():
    """Autorun installer pipeline shape.  Mirrors
    autorun live but the second source-build call uses 'installer' and
    the chroot step is cmd_build_chroot_installer."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    src = inspect.getsource(BuildSession.cmd_auto_run_installer)
    _i_pkg       = src.find("'source build'")
    _i_installer = src.find("'source build installer'")
    _i_chroot    = src.find("'chroot build installer'")
    assert _i_pkg       > 0, "_steps missing 'source build' stage"
    assert _i_installer > 0, "_steps missing 'source build installer' stage"
    assert _i_chroot    > 0, "_steps missing 'chroot build installer' stage"
    assert _i_pkg < _i_installer < _i_chroot, (
        f"Stage order wrong: source build @ {_i_pkg}, "
        f"source build installer @ {_i_installer}, "
        f"chroot build installer @ {_i_chroot}"
    )
    assert "cmd_source_build('installer')" in src
    assert "cmd_build_chroot_installer" in src
    # Gates on chroot_installer_ready (not chroot_verified — that's
    # live-only).  Pin the flag name so a future refactor doesn't
    # silently swap to the wrong one.
    assert "'chroot_installer_ready'" in src



def test_autorun_live_chains_iso_build_after_chroot():
    """`autorun live` ends with `iso build live` so the operator gets
    a bootable ISO, not just a verified chroot.  Pin the order
    (chroot before iso) and the gating flag (iso_live_ready) so a
    future refactor doesn't drop the iso step."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    src = inspect.getsource(BuildSession.cmd_auto_run_live)
    _i_chroot = src.find("'chroot build'")
    _i_iso    = src.find("'iso build live'")
    assert _i_iso > 0, "_steps missing the 'iso build live' stage label"
    assert _i_chroot < _i_iso, (
        f"iso build live must run AFTER chroot build (chroot @ {_i_chroot}, "
        f"iso @ {_i_iso})"
    )
    assert "cmd_build_iso_live" in src, (
        "autorun live must call cmd_build_iso_live"
    )
    assert "'iso_live_ready'" in src, (
        "autorun live must gate on iso_live_ready flag"
    )



def test_autorun_installer_chains_iso_build_after_chroot():
    """`autorun installer` ends with `iso build installer` so the
    operator gets a bootable installer ISO end-to-end.  Symmetric to
    test_autorun_live_chains_iso_build_after_chroot."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    src = inspect.getsource(BuildSession.cmd_auto_run_installer)
    _i_chroot = src.find("'chroot build installer'")
    _i_iso    = src.find("'iso build installer'")
    assert _i_iso > 0, "_steps missing the 'iso build installer' stage label"
    assert _i_chroot < _i_iso, (
        f"iso build installer must run AFTER chroot build installer "
        f"(chroot @ {_i_chroot}, iso @ {_i_iso})"
    )
    assert "cmd_build_iso_installer" in src, (
        "autorun installer must call cmd_build_iso_installer"
    )
    assert "'iso_installer_ready'" in src, (
        "autorun installer must gate on iso_installer_ready flag"
    )



def test_autorun_disk_builds_its_own_disk_chroot():
    """SURFACES-01: `autorun disk` builds the DISK surface's OWN chroot
    (`chroot build disk`, gated on chroot_disk_ready — decoupled from the
    live/GNOME chroot) then ends with `iso build disk` on iso_disk_ready."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    src = inspect.getsource(BuildSession.cmd_auto_run_disk)
    _i_chroot = src.find("'chroot build disk'")
    _i_iso    = src.find("'iso build disk'")
    assert _i_chroot > 0, "_steps missing 'chroot build disk' stage"
    assert _i_iso > 0,    "_steps missing 'iso build disk' terminal stage"
    assert _i_chroot < _i_iso, (
        f"stage order wrong: chroot disk @ {_i_chroot}, iso disk @ {_i_iso}")
    assert "cmd_build_chroot_disk" in src
    assert "'chroot_disk_ready'" in src
    assert "cmd_build_iso_disk" in src, "autorun disk must call cmd_build_iso_disk"
    assert "'iso_disk_ready'" in src, "autorun disk must gate on iso_disk_ready"
    # Must NOT terminate on the live ISO, nor build the LIVE chroot.
    assert "cmd_build_iso_live" not in src, (
        "autorun disk must end on the disk image, not the live ISO")
    assert "cmd_build_chroot_live" not in src, (
        "autorun disk must not build the live chroot (decoupled surface)")



def test_buildflags_carry_iso_ready_state():
    """iso_live_ready, iso_installer_ready and iso_disk_ready start False
    so a never-built artifact doesn't appear ready, and they're listed in
    __str__ so the status line surfaces them."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildFlags
    _flags = BuildFlags()
    assert _flags.iso_live_ready is False
    assert _flags.iso_installer_ready is False
    assert _flags.iso_disk_ready is False
    _s = str(_flags)
    assert 'iso_live' in _s, _s
    assert 'iso_installer' in _s, _s
    assert 'iso_disk' in _s, _s



def test_autorun_dispatcher_routes_bare_to_live_and_explicit_to_each():
    """cmd_auto_run is now a dispatcher: bare → live (preserves UX);
    'live' → live; 'installer' → installer; 'disk' → disk; anything
    else → help."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _calls = []
    _sess = BuildSession.__new__(BuildSession)
    _sess.cmd_auto_run_live      = lambda *a, **kw: _calls.append(('live', a))
    _sess.cmd_auto_run_installer = lambda *a, **kw: _calls.append(('installer', a))
    _sess.cmd_auto_run_disk      = lambda *a, **kw: _calls.append(('disk', a))

    # Bare → live
    _sess.cmd_auto_run()
    assert _calls == [('live', ())], _calls
    # Explicit 'live'
    _calls.clear()
    _sess.cmd_auto_run('live')
    assert _calls == [('live', ())], _calls
    # Explicit 'installer'
    _calls.clear()
    _sess.cmd_auto_run('installer')
    assert _calls == [('installer', ())], _calls
    # Explicit 'disk'
    _calls.clear()
    _sess.cmd_auto_run('disk')
    assert _calls == [('disk', ())], _calls
    # Unknown action → no handler invoked (falls through to _group_help)
    _calls.clear()
    _sess.cmd_auto_run('wat')
    assert _calls == [], (
        f"unknown autorun action must not invoke any handler, got {_calls}")



def test_autorun_live_runs_source_build_then_source_build_live():
    """Autorun live must invoke source build twice
    — once with no args (pkg subset, the new bare default) and once
    with 'live' — before chroot build.  Catches a regression where
    someone re-orders or drops the live extras step (which would
    silently produce a chroot missing live-boot/live-config and fail
    downstream).

    Phase 7 follow-up: source moved from cmd_auto_run (now a dispatcher)
    to cmd_auto_run_live — inspect the latter."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    src = inspect.getsource(BuildSession.cmd_auto_run_live)
    _i_pkg  = src.find("'source build'")
    _i_live = src.find("'source build live'")
    _i_chroot = src.find("'chroot build'")
    assert _i_pkg  > 0, "_steps missing the bare 'source build' stage label"
    assert _i_live > 0, "_steps missing the 'source build live' stage label"
    assert _i_chroot > 0, "_steps missing the 'chroot build' stage label"
    assert _i_pkg < _i_live < _i_chroot, (
        f"Stage order wrong: source build @ {_i_pkg}, "
        f"source build live @ {_i_live}, chroot build @ {_i_chroot}"
    )
    # The 'live' arm should be wired via cmd_source_build('live') — assert
    # the lambda + arg appear in proximity to the 'source build live' label.
    assert "cmd_source_build('live')" in src, (
        "autorun_live must call cmd_source_build('live') for the live arm"
    )



def test_source_build_args_subset_and_named_pkgs_mutually_exclusive():
    """`source build <subset> pkg1` is rejected for every subset word —
    operator must pick the subset OR specific names, not both.  Phase 4
    adds 'pkg' to the subset list too."""
    from build import BuildSession
    for _subset in ('pkg', 'live', 'installer', 'recommended', 'all'):
        err, *_ = BuildSession._parse_source_build_args((_subset, 'pkg1'))
        assert err is not None, _subset
        assert 'mutually exclusive' in err, f"{_subset}: {err!r}"



def test_source_build_args_named_pkgs_resolve_subset_to_empty():
    """When named packages are given, subset is '' (caller branches on
    `_names` first)."""
    from build import BuildSession
    err, _f, subset, names, _o = \
        BuildSession._parse_source_build_args(('foo', 'bar'))
    assert err is None
    assert subset == '' and names == ['foo', 'bar']



def test_source_build_args_all_subset_parses():
    """`source build all` resolves to subset='all', no names, no error.
    'all' is the union mode added 2026-05-20 to spare operators from
    running pkg + live + installer + recommended back-to-back."""
    from build import BuildSession
    err, _force, subset, names, _override = \
        BuildSession._parse_source_build_args(('all',))
    assert err is None, f"all should parse cleanly: {err!r}"
    assert subset == 'all', f"got subset={subset!r}"
    assert names == [], f"got names={names!r}"



def test_source_build_args_bracket_token_extracts_profiles():
    """`[nocheck]` parses to ['nocheck']; commas + whitespace tolerated."""
    from build import BuildSession
    err, _f, _s, names, override = \
        BuildSession._parse_source_build_args(('foo', '[nocheck]'))
    assert err is None
    assert names == ['foo']
    assert override == ['nocheck']

    _, _, _, _, override2 = \
        BuildSession._parse_source_build_args(('foo', '[nocheck, nodoc]'))
    assert override2 == ['nocheck', 'nodoc']



def test_source_build_args_empty_bracket_means_no_profiles():
    """`[]` parses to an empty list — most-permissive build, distinct from
    None (no override)."""
    from build import BuildSession
    err, _f, _s, _n, override = \
        BuildSession._parse_source_build_args(('foo', '[]'))
    assert err is None
    assert override == []
    # Distinct from "no override at all"
    assert override is not None



def test_source_build_args_multiple_bracket_tokens_rejected():
    """Two bracket-tokens is ambiguous — refuse with a usage hint."""
    from build import BuildSession
    err, *_ = BuildSession._parse_source_build_args(
        ('foo', '[nocheck]', '[nodoc]')
    )
    assert err is not None and 'only one [profiles] override' in err



def test_source_build_args_bracket_position_does_not_matter():
    """Bracket-token can appear before or after pkg names + flag-words."""
    from build import BuildSession
    for argv in (
        ('foo', '[nocheck]'),
        ('[nocheck]', 'foo'),
        ('force', '[nocheck]', 'foo'),
        ('foo', 'force', '[nocheck]'),
    ):
        err, _f, _s, names, override = \
            BuildSession._parse_source_build_args(argv)
        assert err is None, f"args={argv!r}"
        assert names == ['foo'], f"args={argv!r}"
        assert override == ['nocheck'], f"args={argv!r}"



def test_signing_key_verified_flag_default_false():
    """New BuildFlags must initialise signing_key_verified to False."""
    from build import BuildFlags
    flags = BuildFlags()
    assert flags.signing_key_verified is False



def test_ensure_signing_key_verified_true_when_key_exists():
    """Key present + verify roundtrip succeeds → flag set, returns True,
    no Prompt invoked.  Mocks signing.verify_key / get_key_info to keep
    the test fast (no real gpg call required for this branch)."""
    from unittest.mock import patch

    sess = _stub_session_for_signing_gate()
    with patch('signing.verify_key', return_value=(True, 'sign+verify OK')), \
         patch('signing.get_key_info', return_value={
             'fingerprint': 'AABBCC',
             'uid': 'Athena Build <athena@local>',
             'created': '1700000000',
             'expires': '0',
         }), \
         patch('build.Prompt') as mock_prompt:
        ok = sess._ensure_signing_key_verified()
    assert ok is True
    assert sess.flags.signing_key_verified is True
    # Prompt MUST NOT be invoked on the success path.
    mock_prompt.assert_not_called()



def test_ensure_signing_key_verified_false_on_user_decline():
    """No key + operator declines prompt → returns False, flag stays
    False, generate_key NOT called.  build_chroot will bail."""
    from unittest.mock import patch, MagicMock

    sess = _stub_session_for_signing_gate()
    _prompt_inst = MagicMock()
    _prompt_inst.get_response.return_value = 'n'
    with patch('signing.verify_key',
               return_value=(False, 'no signing key')), \
         patch('signing.generate_key') as mock_gen, \
         patch('build.Prompt', return_value=_prompt_inst):
        ok = sess._ensure_signing_key_verified()
    assert ok is False
    assert sess.flags.signing_key_verified is False
    mock_gen.assert_not_called()



def test_ensure_signing_key_verified_generates_then_verifies_on_accept():
    """No key + operator accepts → generate_key called → re-verify
    succeeds → returns True, flag set."""
    from unittest.mock import patch, MagicMock

    sess = _stub_session_for_signing_gate()
    _prompt_inst = MagicMock()
    _prompt_inst.get_response.return_value = 'y'
    # First verify_key call returns False (no key); after generate, the
    # second call returns True.  Use side_effect to script the sequence.
    with patch('signing.verify_key',
               side_effect=[(False, 'no signing key'),
                            (True, 'sign+verify OK')]) as mock_verify, \
         patch('signing.generate_key', return_value=True) as mock_gen, \
         patch('signing.get_key_info', return_value={
             'fingerprint': 'NEWFP', 'uid': 'Athena <a@b>',
             'created': '1', 'expires': '0',
         }), \
         patch('signing.signing_pubkey_path',
               return_value='/tmp/pub.gpg'), \
         patch('build.Prompt', return_value=_prompt_inst):
        ok = sess._ensure_signing_key_verified()
    assert ok is True
    assert sess.flags.signing_key_verified is True
    assert mock_verify.call_count == 2     # before-generate + after
    mock_gen.assert_called_once()



def test_ensure_signing_key_verified_false_when_generate_fails():
    """No key + operator accepts + generate fails → returns False, flag
    stays False, re-verify NOT called (early return on generate failure)."""
    from unittest.mock import patch, MagicMock

    sess = _stub_session_for_signing_gate()
    _prompt_inst = MagicMock()
    _prompt_inst.get_response.return_value = 'y'
    with patch('signing.verify_key',
               return_value=(False, 'no signing key')) as mock_verify, \
         patch('signing.generate_key', return_value=False) as mock_gen, \
         patch('build.Prompt', return_value=_prompt_inst):
        ok = sess._ensure_signing_key_verified()
    assert ok is False
    assert sess.flags.signing_key_verified is False
    # verify_key called once (the initial check); generate failed before
    # we'd re-verify.
    assert mock_verify.call_count == 1
    mock_gen.assert_called_once()



def test_cli_print_writes_to_stdout():
    """Cli.print writes to stdout, ignoring the color attribute."""
    cli, out, err, restore = _fresh_cli()
    try:
        cli.print('hello world')
        cli.print('with color', attribute=99)
    finally:
        captured_out = out.getvalue()
        captured_err = err.getvalue()
        restore()
    assert 'hello world' in captured_out
    assert 'with color' in captured_out
    assert captured_err == ''  # nothing on stderr from print()



def test_cli_severity_methods_write_to_stderr_with_tags():
    """INFO/WARNING/ERROR write to stderr with severity tags."""
    cli, out, err, restore = _fresh_cli()
    try:
        cli.INFO('info-msg')
        cli.WARNING('warn-msg')
        cli.ERROR('err-msg')
    finally:
        out_v = out.getvalue()
        err_v = err.getvalue()
        restore()
    assert '[INFO ]' in err_v and 'info-msg' in err_v
    assert '[WARN ]' in err_v and 'warn-msg' in err_v
    assert '[ERROR]' in err_v and 'err-msg' in err_v
    assert out_v == ''  # nothing leaked to stdout



def test_cli_registers_itself_as_tui_singleton():
    """Cli.__init__ sets tui.tui_instance — that's how the Console facade
    resolves to the CLI backend."""
    import tui
    cli, _o, _e, restore = _fresh_cli()
    try:
        assert tui.tui_instance is cli
    finally:
        restore()



def test_cli_register_command_dispatches_via_wait():
    """Registered handler is invoked when its name is typed at the prompt;
    args after the name are forwarded as positional args."""
    cli, out, err, restore = _fresh_cli()
    captured_args = []
    cli.register_command('echo', lambda *a: captured_args.append(a), 'echo args')
    try:
        # Drive the REPL with a scripted stdin.
        import io
        sys.stdin = io.StringIO('echo foo bar\nquit\n')
        cli.wait()
    finally:
        sys.stdin = sys.__stdin__
        restore()
    assert captured_args == [('foo', 'bar')]



def test_cli_unknown_command_does_not_crash_repl():
    """Typing an unknown command prints a hint (to stderr, so a `--cmd`
    run's stdout stays clean) and continues the REPL."""
    cli, out, err, restore = _fresh_cli()
    try:
        import io
        sys.stdin = io.StringIO('nonexistent\nquit\n')
        cli.wait()
    finally:
        err_v = err.getvalue()
        sys.stdin = sys.__stdin__
        restore()
    assert 'Unknown command' in err_v
    assert 'nonexistent' in err_v



def test_cli_repl_enables_readline_history():
    """The headless REPL turns on readline before its read loop so Up-arrow
    recalls the previous command — input() has no history until readline is
    imported.  The setup is best-effort (no-op for piped/non-TTY stdin)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import cli
    _wsrc = inspect.getsource(cli.Cli.wait)
    assert '_enable_line_editing()' in _wsrc, _wsrc
    _esrc = inspect.getsource(cli.Cli._enable_line_editing)
    assert 'import readline' in _esrc, _esrc
    # non-TTY stdin → early return, never raises, no readline side effects
    _c = object.__new__(cli.Cli)
    from unittest.mock import patch
    with patch('sys.stdin') as _stdin:
        _stdin.isatty.return_value = False
        _c._enable_line_editing()



def test_cli_handler_exception_does_not_kill_repl():
    """A handler raising mid-command logs the error to stderr and the REPL
    keeps going — same forgiving model as Tui.shell()."""
    cli, out, err, restore = _fresh_cli()
    cli.register_command('boom', lambda: 1 / 0, 'crash')
    survived = []
    cli.register_command('survived', lambda: survived.append(True), 'after-boom')
    try:
        import io
        sys.stdin = io.StringIO('boom\nsurvived\nquit\n')
        cli.wait()
    finally:
        err_v = err.getvalue()
        sys.stdin = sys.__stdin__
        restore()
    assert survived == [True], "REPL should have continued after the handler's exception"
    assert 'ZeroDivisionError' in err_v



def test_cli_help_lists_registered_commands():
    """`help` prints the registered commands plus the built-in quit/help."""
    cli, out, err, restore = _fresh_cli()
    cli.register_command('foo', lambda: None, 'do foo')
    cli.register_command('bar', lambda: None, 'do bar')
    try:
        import io
        sys.stdin = io.StringIO('help\nquit\n')
        cli.wait()
    finally:
        out_v = out.getvalue()
        sys.stdin = sys.__stdin__
        restore()
    assert 'foo' in out_v and 'do foo' in out_v
    assert 'bar' in out_v and 'do bar' in out_v
    assert 'help' in out_v
    assert 'quit' in out_v



def test_cli_eof_exits_repl_cleanly():
    """Ctrl+D (EOFError on input) exits the REPL with code 0."""
    cli, out, err, restore = _fresh_cli()
    try:
        import io
        sys.stdin = io.StringIO('')  # immediate EOF
        cli.wait()
    finally:
        sys.stdin = sys.__stdin__
        restore()
    assert cli._exit_code == 0



def test_cli_widget_methods_return_stable_ids_for_unknown_types():
    """add_widget returns a unique int per call; del_widget never raises.
    Bare objects (not ProgressBar/Spinner) pass through silently — only
    typed widgets get start/finish markers."""
    cli, out, _e, restore = _fresh_cli()
    try:
        wid1 = cli.add_widget(object())
        wid2 = cli.add_widget(object())
        assert wid1 != wid2
        cli.del_widget(wid1)
        cli.del_widget(wid2)
        # Double-delete must be a no-op
        cli.del_widget(wid1)
    finally:
        out_v = out.getvalue()
        restore()
    # Bare object() is neither a ProgressBar nor a Spinner — must not
    # leak any [start]/[done] markers to stdout.
    assert '[start]' not in out_v
    assert '[done' not in out_v



def test_cli_progress_bar_prints_start_and_finish_markers():
    """A ProgressBar's lifecycle in CLI mode emits two informational
    lines on stdout: a `[start]` marker on construction and a
    `[done: value/max]` marker on close().  Spinner is silent here —
    its own done() handler prints `… done` separately."""
    cli, out, _e, restore = _fresh_cli()
    try:
        from tui import ProgressBar
        bar = ProgressBar(label='SmokeTest', maxvalue=10)  # → add_widget
        for _ in range(10):
            bar.step()
        bar.close()  # → del_widget
    finally:
        out_v = out.getvalue()
        restore()
    assert 'SmokeTest [start]' in out_v
    assert 'SmokeTest [done: 10/10]' in out_v



def test_build_system_sh_checks_disk_image_tools():
    """The disk-image host-tool pre-flight lives in build-system.sh
    (matches the iso-tools gate pattern).  Pin the tool list so a
    refactor doesn't drop a load-bearing binary's check, and pin the
    gate behaviour: any miss → `exit 1` with the missing-package
    summary listed."""
    _sh = os.path.join(_ROOT, 'build-system.sh')
    with open(_sh) as fh:
        _body = fh.read()
    for _tool in ('rsync', 'qemu-img', 'mkfs.fat',
                  'losetup', 'sfdisk', 'mkfs.ext4',
                  'grub-install', 'blkid'):
        assert _tool in _body, (
            f"{_tool} no longer checked in build-system.sh disk-image "
            f"section — disk image build would fail mid-pipeline with "
            f"a less obvious error"
        )
    # Gate behaviour: DISK_TOOLS_OK flag + exit 1 on miss + the
    # packages listing in the failure message.
    assert 'DISK_TOOLS_OK' in _body, (
        "disk-image tools check must set a gate flag (DISK_TOOLS_OK), "
        "not just warn — missing tools should fail startup"
    )
    assert 'one or more disk image build tools missing' in _body, (
        "disk-image gate must surface the failure summary line so the "
        "operator sees the missing-packages list"
    )



def test_cmd_iso_dispatcher_routes_disk_action():
    """`iso build disk` must dispatch to cmd_build_iso_disk."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r"\n    def cmd_iso\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m, "cmd_iso dispatcher not found"
    _disp = _m.group(0)
    assert "'build disk'" in _disp, (
        "`build disk` must appear in cmd_iso's help table"
    )
    assert re.search(
        r"if _sub == 'disk':\s*\n\s+return self\.cmd_build_iso_disk",
        _disp,
    ), "disk subcommand not dispatched"



def test_cmd_build_iso_disk_gates_on_chroot_disk_ready_and_reads_size():
    """cmd_build_iso_disk (SURFACES-01): gates on chroot_disk_ready
    (the dedicated disk-surface chroot, NOT the live chroot_verified
    flag), force-mode re-verifies dir_chroot_disk with the live-boot
    check skipped, reads the configured default size, and calls
    disk_image.build_disk_image against dir_chroot_disk."""
    _body = _session_source()
    import re
    _m = re.search(
        r"\n    def cmd_build_iso_disk\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m, "cmd_build_iso_disk body not found"
    _method = _m.group(0)
    # Gate on the DISK surface flag, with force bypass.
    assert "self.flags.chroot_disk_ready" in _method, (
        "cmd_build_iso_disk must gate on chroot_disk_ready"
    )
    assert "_force" in _method
    # Force-mode verify runs against the disk chroot, live-boot skipped.
    assert "dir_chroot_disk" in _method, (
        "force verify + image build must target the disk-surface chroot"
    )
    assert "require_live_boot=False" in _method, (
        "disk-surface verify must skip the live-boot check"
    )
    # Must NOT refresh the LIVE surface's verified flag from a disk verify.
    assert "self.flags.chroot_verified = True" not in _method, (
        "disk verify must not set the live chroot_verified flag"
    )
    # Size resolution: default from config.
    assert "self.config.disk_image_size_gb" in _method, (
        "cmd_build_iso_disk must read the configured default size"
    )
    # Calls into disk_image.build_disk_image
    assert "disk_image.build_disk_image(" in _method



def test_progress_bar_show_rate_false_omits_rate_column():
    """The new show_rate kwarg drops the trailing {rate} column from
    the default fmt — build progress bars (source_build, chroot, iso,
    strip, cleanup) use this because per-pkg time variance is enormous
    and an avg pkg/s rate is misleading.  Pinned via __str__ output
    inspection."""
    cli, out, _e, restore = _fresh_cli()
    try:
        from tui import ProgressBar
        _bar_with    = ProgressBar(label='WithRate',    maxvalue=10)
        _bar_without = ProgressBar(label='WithoutRate', maxvalue=10, show_rate=False)
        for _ in range(5):
            _bar_with.step()
            _bar_without.step()
        _s_with    = str(_bar_with)
        _s_without = str(_bar_without)
    finally:
        restore()
    # With rate: trailing 'it/s' (or similar) substring present.
    assert 'it/s' in _s_with, _s_with
    # Without rate: no rate column anywhere.
    assert 'it/s' not in _s_without, _s_without
    # Both still show value/total.
    assert '5/10' in _s_with and '5/10' in _s_without



def test_progress_bar_label_width_pins_column_so_label_updates_dont_shift():
    """label_width pins the label column to N chars (ljust + truncate)
    so subsequent .label() calls with shorter strings keep the bar
    horizontally stable.  Anti-regression for source_build where the
    label cycles through package names of wildly different lengths
    (firefox-esr → ca-certificates → libc6) — without pinning the bar
    would dance left/right between updates."""
    cli, out, _e, restore = _fresh_cli()
    try:
        from tui import ProgressBar
        _bar = ProgressBar(label='Short', label_width=24, maxvalue=10, show_rate=False)
        _s_short = str(_bar)
        # Mid-iteration label swap to a longer + then shorter string.
        _bar.label('linux-image-amd64-modules')   # >24 chars — truncates
        _s_long  = str(_bar)
        _bar.label('libc6')                       # 5 chars — ljust-pads
        _s_pad   = str(_bar)
    finally:
        restore()
    # The "[" of the bar should sit at the same column in all three.
    # Find the first "[" position (after the leading spaces + label).
    _pos_short = _s_short.find('[')
    _pos_long  = _s_long.find('[')
    _pos_pad   = _s_pad.find('[')
    assert _pos_short == _pos_long == _pos_pad, (
        f"label column shifted between updates: short={_pos_short} "
        f"long={_pos_long} pad={_pos_pad}; bar must stay horizontally "
        f"stable"
    )



def test_tier3_coord_webapi_source_pins():
    """Pins for Tier-3 fixes #72/#88/#205 where the behavioural path (a TUI
    handler / gpg sign / an SSE generator) is disproportionate to drive."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.head as _h
    import webapi as _w
    from build import BuildSession
    # #88: stage to temp + atomic replace (prior manifest survives a failure)
    _hsrc = inspect.getsource(_h)
    assert ('os.replace(_path_tmp, _path)'.replace('os.', '_os.') in _hsrc
            and '_path_tmp' in _hsrc), '#88'
    # #205: a final drain inside the terminal block before the end event
    assert inspect.getsource(_w).count('while _sent < len(_out):') >= 2, '#205'
    # #72: the force snapshot-select path also reconciles the build.conf pin
    assert 'reconcile_snapshot_pin' in inspect.getsource(
        BuildSession._snapshot_select_force), '#72'



def test_build_patch_list_sorts_by_full_filename():
    """Regression (audit #23): build.py's patch_list must sort by the full
    filename (like buildcontainer), not x[:5] — a 5-char-prefix tie feeds an
    order-sensitive hash and flaps it, forcing needless rebuilds."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    assert 'key=lambda x: x[:5]' not in inspect.getsource(build), (
        "patch_list must sort by full filename, not a 5-char prefix")



def test_cli_quit_detection_keys_on_first_token():
    """Regression (audit #53): the REPL and one-shot loops must decide quit/exit
    on the FIRST token like _dispatch_one (which keys on parts[0]); testing the
    whole line drifted, so `quit x` was dispatched-as-quit but not broken."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import cli
    _src = inspect.getsource(cli)
    assert _src.count("(line.split()[:1] or [''])[0] in ('quit', 'exit')") >= 1
    assert _src.count("(_cmd.split()[:1] or [''])[0] in ('quit', 'exit')") >= 1
    assert "line.strip() in ('quit', 'exit')" not in _src



def test_cache_parse_build_mode_guards_unreadable_list():
    """Regression (audit #61): _cache_parse_build_mode must catch the OSError
    parse_build_pkg_list raises on an unreadable build_pkg.list and degrade to
    return False, not let it crash the parse."""
    import sys
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    import commands.cmd_cache as _cc

    _sess = BuildSession.__new__(BuildSession)
    _sess.dep_tree = object()
    _sess.cache = object()

    class _Cfg:
        build_pkg_list_path = '/nonexistent/build_pkg.list'
    _sess.config = _Cfg()
    with mock.patch.object(_cc, 'console'), \
         mock.patch.object(_cc.utils, 'parse_build_pkg_list',
                           side_effect=OSError('permission denied')):
        assert _sess._cache_parse_build_mode() is False   # must not raise



def test_cli_spinner_does_not_print_start_marker():
    """Spinner registration is silent — it prints its own `… done` line
    on done().  CLI mode must NOT add a duplicate start/finish marker
    for spinners (different widget shape, different lifecycle)."""
    cli, out, _e, restore = _fresh_cli()
    try:
        from tui import Spinner
        sp = Spinner('Spinning')
        sp.done()
    finally:
        out_v = out.getvalue()
        restore()
    assert '[start]' not in out_v
    assert '[done' not in out_v
    # Spinner's own done() handler did print the completion line.
    assert 'Spinning' in out_v and 'done' in out_v



def test_cli_console_mark_and_trim_to_are_no_ops():
    """`console_mark`/`console_trim_to` are no-ops in CLI mode — can't
    unprint stdout.  Method names must match the Tui surface (with the
    `console_` prefix) because the Console facade in tui.py calls them
    via `self._resolve().console_mark()` / `.console_trim_to()`.

    REGRESSION: first cut named these `mark`/`trim_to` (no prefix) and
    crashed at the parse_dependency multi-provider auto-pick path with
    AttributeError.  Drive through the Console facade here, NOT a
    direct method call, so the same kind of name-drift gets caught."""
    cli, _o, _e, restore = _fresh_cli()
    try:
        from tui import Console
        c = Console()
        m = c.mark()  # facade → cli.console_mark()
        assert isinstance(m, int)
        c.trim_to(m)  # facade → cli.console_trim_to(m)
        c.trim_to(99999)  # facade no-op on unknown mark
    finally:
        restore()



def test_cli_console_facade_exercise_full_surface():
    """Drive every Console facade method against a Cli instance to catch
    any future name-drift between Console._resolve() callers and the
    Cli/Tui method names they call."""
    cli, out, err, restore = _fresh_cli()
    try:
        from tui import Console
        c = Console()
        c.print('via facade')
        c.print('with attr', 99)
        c.info('info via facade')
        c.warning('warn via facade')
        c.error('err via facade')
        m = c.mark()
        c.trim_to(m)
    finally:
        out_v = out.getvalue()
        err_v = err.getvalue()
        restore()
    # print → stdout
    assert 'via facade' in out_v
    assert 'with attr' in out_v
    # info/warning/error → stderr (with severity tags)
    assert 'info via facade' in err_v
    assert 'warn via facade' in err_v
    assert 'err via facade' in err_v



def test_cli_prompt_reads_stdin():
    """prompt() reads a line from stdin and returns it (no masking)."""
    cli, _o, _e, restore = _fresh_cli()
    try:
        import io
        sys.stdin = io.StringIO('the answer\n')
        response = cli.prompt('Q: ', masked=False, keymode=False)
    finally:
        sys.stdin = sys.__stdin__
        restore()
    assert response == 'the answer'



def test_cli_keymode_prompt_reads_and_discards():
    """keymode (PROMPT_PAUSE) returns empty string regardless of input."""
    cli, _o, _e, restore = _fresh_cli()
    try:
        import io
        sys.stdin = io.StringIO('whatever\n')
        response = cli.prompt('Press enter: ', masked=False, keymode=True)
    finally:
        sys.stdin = sys.__stdin__
        restore()
    assert response == ''



def test_cli_logging_handlers_bound_to_cli_after_init():
    """After Cli.__init__ runs, the 'athena' logger has tab handlers
    bound that route through the Cli instance.  Verifies that the
    setup_logging contract works for both Tui and Cli backends."""
    import logging
    cli, _o, err, restore = _fresh_cli()
    try:
        logger = logging.getLogger('athena')
        logger.warning('hello-from-logger')
    finally:
        err_v = err.getvalue()
        restore()
    # _LogTabHandler routes WARNING through cli.WARNING which writes to stderr.
    assert 'hello-from-logger' in err_v



def test_ux09g_too_small_q_key_posts_shutdown():
    """The too-small overlay says "Press Q to exit" — a q/Q keystroke while
    state.too_small must actually post Shutdown (the only escape, since the
    command line is inert under the overlay).  When not too-small, q is an
    ordinary keystroke and posts no Shutdown."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import Shutdown

    def _drain(_d):
        _evs = []
        while not _d._events.empty():
            _evs.append(_d._events.get_nowait())
        return _evs

    d = Dispatcher(_v2_fake_renderer())
    d.state.too_small = True
    d._on_key('q')
    assert any(isinstance(e, Shutdown) for e in _drain(d))

    d2 = Dispatcher(_v2_fake_renderer())
    d2.state.too_small = False
    d2._on_key('q')
    assert not any(isinstance(e, Shutdown) for e in _drain(d2))



def test_status_lines_compact_snapshot():
    """print_commands.status_lines renders the compact status-tab snapshot:
    header + per-flag pipeline (✓/·) + artifact locations."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import print_commands

    class _Flags:
        cache_ready = True
        dep_check_ready = False
        download_ready = True
        build_container_ready = False
        source_build_ready = True
        signing_key_verified = False
        chroot_ready = False
        chroot_verified = False
        iso_live_ready = False
        iso_disk_ready = False
        chroot_installer_ready = False
        chroot_disk_ready = False
        iso_installer_ready = False

    class _Cfg:
        build_mode = 'distribution'
        build_distribution = 'Asgard'
        build_version = '1'
        build_codename = 'thor'
        snapshot_enabled = True
        snapshot_timestamp_config = '20260602T173733Z'
        dir_image = 'image'
        arch = 'amd64'

    class _Sess:
        config = _Cfg()
        flags = _Flags()
        cache = None
        dep_tree = None

    _rows = print_commands.status_lines(_Sess())
    _text = '\n'.join(_t for _t, _ in _rows)
    assert 'MODE: distribution' in _text
    assert '20260602T173733Z (pinned)' in _text
    assert '[✓] cache_build' in _text      # ✓ for a ready flag
    assert '[·] dep_parse' in _text         # · for a not-ready flag
    assert '[✓] source_build' in _text
    assert 'ISO live' in _text and 'Disk image' in _text
    # Predicted ISO name carries the snapshot tag, derived CHEAPLY from the
    # pin (no networked resolve) — see the no-network guard below.
    assert 'athena-1-20260602T173733Z-amd64.iso' in _text
    assert 'asgard-1-amd64.qcow2' in _text

    # Guard: the status render must NEVER call the networked
    # resolve_snapshot_timestamp (it runs at startup, even on an un-configured
    # box).  Patch it to explode; status_lines must still render the tag.
    import unittest.mock as _mock2
    import utils as _u
    with _mock2.patch.object(
            _u, 'resolve_snapshot_timestamp',
            side_effect=AssertionError('status_lines must not resolve')):
        _t3 = '\n'.join(_t for _t, _ in print_commands.status_lines(_Sess()))
    assert 'athena-1-20260602T173733Z-amd64.iso' in _t3

    # signing_key_verified is an in-memory flag (False here), but when the
    # key is PREPARED on disk the row shows ✓ (prepared), not a bare "·".
    import unittest.mock as _mock
    with _mock.patch.object(print_commands, '_signing_key_present',
                            return_value=True):
        _t2 = '\n'.join(_t for _t, _ in print_commands.status_lines(_Sess()))
    assert '[✓] signing_key  (prepared)' in _t2, _t2
    with _mock.patch.object(print_commands, '_signing_key_present',
                            return_value=False):
        _t3 = '\n'.join(_t for _t, _ in print_commands.status_lines(_Sess()))
    assert '[·] signing_key  (run `key generate`)' in _t3, _t3



def test_cmd_cache_info_prints_identity_and_relations():
    """`cache info <pkg>` looks up the cache and prints identity +
    relations; missing names report 'not found'."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from commands import cmd_cache
    from build import BuildSession

    class _Pkg(dict):
        def __init__(self):
            super().__init__()
            self.update({'Package': 'htop', 'Version': '3.2.2-1',
                         'Architecture': 'amd64', 'Section': 'utils',
                         'Priority': 'optional', 'Installed-Size': '512',
                         'Source': 'htop',
                         'Description': 'interactive process viewer\n more'})
            self.depends = [('libc6', '2.36', '>='), ('libncursesw6', '', '')]
            self.alt_depends = []
            self.pre_depends = []
            self.alt_pre_depends = []
            self.recommends = []
            self.suggests = []
            self.provides = []
            self.conflicts = []
            self.breaks = []
            self.replaces = []
            self.depended_by = []

    class _Cache:
        def get_packages(self, name, version=None, constraint=''):
            return [_Pkg()] if name == 'htop' else []

    prints = []
    class _RecConsole:
        def print(self, msg, attr=None): prints.append(msg)
        def INFO(self, m): pass
        def WARNING(self, m): pass
        def ERROR(self, m): pass

    # Patch the module-level console facade build.py uses directly —
    # deterministic regardless of the global tui_instance state left
    # by prior tests in the suite.
    saved_console = cmd_cache.console
    cmd_cache.console = _RecConsole()
    try:
        sess = BuildSession.__new__(BuildSession)
        sess.cache = _Cache()
        sess.dep_tree = None
        from types import SimpleNamespace
        sess.flags = SimpleNamespace(cache_ready=True)

        # Found package → identity + a Depends line.
        sess.cmd_cache_info('htop')
        blob = '\n'.join(prints)
        assert 'htop' in blob and '3.2.2-1' in blob, repr(prints)
        assert 'utils' in blob, repr(prints)              # Section
        assert 'libc6 (>= 2.36)' in blob, repr(prints)    # constrained dep
        assert 'libncursesw6' in blob, repr(prints)       # unconstrained dep

        # Missing package → 'not found'.
        prints.clear()
        sess.cmd_cache_info('nosuchpkg')
        assert any('not found' in p for p in prints)

        # No arg → usage.
        prints.clear()
        sess.cmd_cache_info()
        assert any('Usage' in p for p in prints)
    finally:
        cmd_cache.console = saved_console



def test_cmd_audit_registered_under_repo_dispatcher():
    """`repo audit` must be wired into cmd_repo's dispatch."""
    from build import BuildSession
    assert hasattr(BuildSession, 'cmd_audit'), (
        "BuildSession is missing cmd_audit")
    _body = _session_source()
    import re
    # `repo audit` routes to cmd_audit (an `external` sub-route may sit in
    # between, dispatching `repo audit external` to cmd_audit_external).
    assert re.search(
        r"if action == 'audit':.*?return self\.cmd_audit\(\*args\)",
        _body, re.DOTALL), "audit not dispatched in cmd_repo"
    assert "'audit'" in _body, (
        "audit must be advertised in cmd_repo's help table")



def test_cmd_audit_runs_three_checks():
    """`repo audit` must invoke ALL THREE primitives:
      - audit_dep_closure (hard dep gate over whole repo)
      - audit_conflict_cohort × 2 (live cohort + installer cohort)
    Hard-coded into the cmd body so a single command surfaces all three
    install-correctness checks."""
    _body = _session_source()
    import re
    _m = re.search(
        r'def cmd_audit\(self, \*args\):.*?(?=\n    def )',
        _body, re.DOTALL)
    assert _m, "cmd_audit not found"
    _method = _m.group(0)
    assert 'repo_audit.audit_dep_closure' in _method, (
        "cmd_audit must call repo_audit.audit_dep_closure for the dep gate")
    assert 'repo_audit.audit_conflict_cohort' in _method, (
        "cmd_audit must call repo_audit.audit_conflict_cohort "
        "(twice — once per cohort)")
    assert '_resolve_live_cohort' in _method, (
        "cmd_audit must resolve the live install cohort")
    assert '_resolve_installer_cohort' in _method, (
        "cmd_audit must resolve the installer ramdisk cohort")



def test_cmd_source_repair_dispatch_and_method_present():
    """source repair must be wired in cmd_source's dispatch table +
    have a matching cmd_source_repair method.  Sanity guard so
    `source repair` isn't a phantom command."""
    _body = _session_source()
    assert "'repair'" in _body, "repair not advertised in source help"
    assert 'def cmd_source_repair(' in _body, (
        "cmd_source_repair method missing or wrong name")
    import re
    assert re.search(
        r"if action == 'repair':\s*\n\s+return self\.cmd_source_repair",
        _body), "source repair not dispatched in cmd_source"



def test_cmd_source_repair_leaves_fail_result_untouched():
    """source repair must NOT overwrite a FAIL .result — that's an
    explicit operator/build decision that a previous attempt failed,
    not something repair should second-guess."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import build as _build_mod

    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)

        # Plant a FAIL .result for foo
        _result = os.path.join(_buildlog, 'foo.result')
        with open(_result, 'w') as fh:
            fh.write('FAIL\n')

        # Plant the .deb (so the binary check would succeed)
        _deb_name = 'foo_1.0_amd64.deb'
        with open(os.path.join(_repo, _deb_name), 'wb') as fh:
            fh.write(b'!<arch>\n')

        class _Src:


            pkgs = [_deb_name]


            files = {}


            version = '1.0'


            patch_list = []
        class _Container:
            buildlog_path = _buildlog
            @staticmethod
            def is_ar_file(_path): return True
            @staticmethod
            def verify_pkg_artifact(_path, _f):
                return (os.path.isfile(_path), 'ok' if os.path.isfile(_path) else 'missing')
        class _Cfg:
            dir_repo = _repo
            dir_log = os.path.join(_tmp, 'log')
            dir_source = os.path.join(_tmp, 'source')
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            @staticmethod
            def deb_dest_for_filename(_f, _comp="main"): return _repo
        class _Tree:
            selected_srcs = {'foo': _Src()}
            src_pkg_files = {'foo': list(_Src.pkgs)}
        class _Flags:
            cache_ready = True
            dep_check_ready = True
            build_container_ready = True

        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = _Tree
        _sess.udeb_dep_tree = None
        _sess.flags = _Flags
        _sess.container = _Container

        _sess.cmd_source_repair()

        # .result content must still be FAIL — repair MUST NOT overwrite
        with open(_result) as fh:
            assert fh.read().strip() == 'FAIL', (
                "repair overwrote an existing .result — must skip when "
                "the file is already present")



def test_cmd_source_repair_skips_when_binaries_missing():
    """source repair must NOT write .result when expected binaries
    are missing (those sources legitimately need rebuild)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import build as _build_mod

    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)
        # NO .deb planted — binary missing

        class _Src:


            pkgs = ['foo_1.0_amd64.deb']


            files = {}


            version = '1.0'


            patch_list = []
        class _Container:
            buildlog_path = _buildlog
            @staticmethod
            def is_ar_file(_path): return True
            @staticmethod
            def verify_pkg_artifact(_path, _f):
                return (os.path.isfile(_path), 'ok' if os.path.isfile(_path) else 'missing')
        class _Cfg:
            dir_repo = _repo
            dir_log = os.path.join(_tmp, 'log')
            dir_source = os.path.join(_tmp, 'source')
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            @staticmethod
            def deb_dest_for_filename(_f, _comp="main"): return _repo
        class _Tree:
            selected_srcs = {'foo': _Src()}
            src_pkg_files = {'foo': list(_Src.pkgs)}
        class _Flags:
            cache_ready = True
            dep_check_ready = True
            build_container_ready = True

        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = _Tree
        _sess.udeb_dep_tree = None
        _sess.flags = _Flags
        _sess.container = _Container

        _sess.cmd_source_repair()

        # No .result should have been written
        assert not os.path.exists(os.path.join(_buildlog, 'foo.result')), (
            "repair wrote .result for a source with missing binary — "
            "that would mask a legitimate rebuild")



def test_cmd_source_repair_clears_stale_pass_when_binaries_not_valid():
    """P2 (2026-05-23) semantics change: repair now CLEARS .result=PASS
    when the recorded PASS state has drifted from disk reality
    (binaries missing or not-ar).  Old behavior (pre-P2) was to
    preserve any existing .result; that was right under the
    repair-only-restores-missing model but wrong under the new
    repair-aligns-with-current-state model — a stale PASS lets the
    next `source build` falsely SKIP a package that genuinely needs
    rebuilding.

    Deep verify (per-binary content + dep resolution) remains the
    opt-in `source verify` / `repo audit` concern — repair uses the
    cheap shallow check (`is_ar_file`) to detect "binary went away
    or is corrupt"."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import build as _build_mod
    import utils as _u
    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)
        # Plant a done/PASS record (binaries claimed valid).
        _rec = _u.new_build_record(
            package='foo', intended_version='1.0', patch_set_hash='',
        )
        _rec.update({'phase': 'done', 'status': 'PASS'})
        _u.write_build_record(_buildlog, _rec)
        _record = os.path.join(_buildlog, 'foo.build.json')
        # Stub is_ar_file=False to mark binaries as corrupt/missing.
        class _Src:
            pkgs = ['foo_1.0_amd64.deb']
            files = {}
            version = '1.0'
            patch_list = []
        class _Container:
            buildlog_path = _buildlog
            @staticmethod
            def is_ar_file(_path): return False
            @staticmethod
            def verify_pkg_artifact(_path, _f):
                return (False, 'version-mismatch:X!=Y')
        class _Cfg:
            dir_repo = _repo
            dir_log = os.path.join(_tmp, 'log')
            dir_source = os.path.join(_tmp, 'source')
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            @staticmethod
            def deb_dest_for_filename(_f, _comp="main"): return _repo
        class _Tree:
            selected_srcs = {'foo': _Src()}
            src_pkg_files = {'foo': list(_Src.pkgs)}
        class _Flags:
            cache_ready = True
            dep_check_ready = True
            build_container_ready = True
        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = _Tree
        _sess.udeb_dep_tree = None
        _sess.flags = _Flags
        _sess.container = _Container

        _sess.cmd_source_repair()

        assert not os.path.exists(_record), (
            "stale PASS must be CLEARED by repair so next source build "
            "rebuilds rather than skipping; record still present"
        )



def test_cmd_source_repair_leaves_consistent_pass_alone():
    """When .result PASS AND binaries verify, repair must leave the
    .result untouched (no-op idempotency)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import build as _build_mod
    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)
        _result = os.path.join(_buildlog, 'foo.result')
        with open(_result, 'w') as fh:
            fh.write('PASS\n')
        # Plant the binary so _source_state sees state='ok' (the
        # repair-no-op path).  Pre-P2 the test relied on repair never
        # checking disk state when .result was present; that's the
        # exact lie P2's stale-PASS clearing removes — the test now
        # must set up a real consistent state.
        with open(os.path.join(_repo, 'foo_1.0_amd64.deb'), 'wb') as fh:
            fh.write(b'!<arch>\n')
        _orig_mtime = os.path.getmtime(_result)
        import time as _time
        _time.sleep(0.05)  # ensure any rewrite would change mtime

        class _Src:
            pkgs = ['foo_1.0_amd64.deb']
            files = {}
            version = '1.0'
            patch_list = []
        class _Container:
            buildlog_path = _buildlog
            @staticmethod
            def is_ar_file(_path): return True
            @staticmethod
            def verify_pkg_artifact(_path, _f): return (True, 'ok')
        class _Cfg:
            dir_repo = _repo
            dir_log = os.path.join(_tmp, 'log')
            dir_source = os.path.join(_tmp, 'source')
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            @staticmethod
            def deb_dest_for_filename(_f, _comp="main"): return _repo
        class _Tree:
            selected_srcs = {'foo': _Src()}
            src_pkg_files = {'foo': list(_Src.pkgs)}
        class _Flags:
            cache_ready = True
            dep_check_ready = True
            build_container_ready = True
        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = _Tree
        _sess.udeb_dep_tree = None
        _sess.flags = _Flags
        _sess.container = _Container

        _sess.cmd_source_repair()

        assert os.path.exists(_result), ".result was unexpectedly deleted"
        assert os.path.getmtime(_result) == _orig_mtime, (
            "consistent PASS .result was rewritten — should be no-op")



def test_cmd_source_repair_leaves_tunneled_marker_alone():
    """A .result containing TUNNELED is an explicit marker for "this
    binary was pulled, not built."  Repair must NOT re-verify the
    binaries (they may be in an upstream-from-cache form) and must
    NOT delete the marker."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import build as _build_mod
    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)
        _result = os.path.join(_buildlog, 'foo.result')
        with open(_result, 'w') as fh:
            fh.write('TUNNELED\n')

        class _Src:


            pkgs = ['foo_1.0_amd64.deb']


            files = {}


            version = '1.0'


            patch_list = []
        # verify_pkg_artifact would FAIL if called — but repair must
        # NOT call it for TUNNELED .result.
        class _Container:
            buildlog_path = _buildlog
            @staticmethod
            def is_ar_file(_path): return False
            @staticmethod
            def verify_pkg_artifact(_path, _f):
                return (False, 'should-not-be-called-for-tunneled')
        class _Cfg:
            dir_repo = _repo
            dir_log = os.path.join(_tmp, 'log')
            dir_source = os.path.join(_tmp, 'source')
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            @staticmethod
            def deb_dest_for_filename(_f, _comp="main"): return _repo
        class _Tree:
            selected_srcs = {'foo': _Src()}
            src_pkg_files = {'foo': list(_Src.pkgs)}
        class _Flags:
            cache_ready = True
            dep_check_ready = True
            build_container_ready = True
        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = _Tree
        _sess.udeb_dep_tree = None
        _sess.flags = _Flags
        _sess.container = _Container

        _sess.cmd_source_repair()

        assert os.path.exists(_result), "TUNNELED marker was deleted!"
        with open(_result) as fh:
            assert fh.read().strip() == 'TUNNELED'



def test_destructive_helpers_warn_in_docstring():
    """Helpers whose names DON'T scream 'destructive' but actually
    mutate filesystem state should carry a docstring warning marker
    (⚠️ or 'DESTRUCTIVE' or 'DELETES') so future callers can't miss it.

    Pinning the _refresh_patches case specifically since that's the
    one that bit us 2026-05-19.  If you add other ambiguously-named
    destructive helpers, extend this list."""
    _bp = open(os.path.join(_ROOT, 'scripts', 'build.py')).read()
    import re
    _m = re.search(
        r'def _refresh_patches\(self\)[^:]*:(.*?)(?=\n    def )',
        _bp, re.DOTALL,
    )
    assert _m, "_refresh_patches definition not found"
    _docstring_or_top = _m.group(1)[:1500]  # first ~30 lines
    assert any(_marker in _docstring_or_top
               for _marker in ('⚠', 'DESTRUCTIVE', 'DELETES')), (
        "_refresh_patches mutates state but its top-of-body comment "
        "carries no destructive-intent marker (⚠️/DESTRUCTIVE/DELETES). "
        "Future read-only callers will assume it's safe and recreate "
        "the 2026-05-19 over-counting bug.")



def test_print_wrapped_names_keeps_lines_under_wrap_width():
    """The compact failure formatter wraps `label: a, b, c, …` so the
    output stays readable when there are many names (e.g. 50+ stale
    sources).  Pins:
      - first line carries the label
      - no line exceeds the configured wrap width
      - every input name appears exactly once in the output
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build as _build_mod
    # Patch build.console.print (the module-level reference build.py
    # bound at import time) — patching tui.console.print won't catch
    # calls since build holds a separate reference.
    _captured: 'list[str]' = []
    _orig = _build_mod.console.print
    _build_mod.console.print = lambda *args, **kw: _captured.append(
        ' '.join(str(a) for a in args)
    )
    try:
        _names = [f'pkg-{i:02d}' for i in range(20)]
        _build_mod.BuildSession._print_wrapped_names(
            '  stale_pass (20)', _names, wrap=50,
        )
    finally:
        _build_mod.console.print = _orig

    _lines = [_l for _l in _captured if _l.strip()]
    assert _lines, f"no output produced, got: {_captured!r}"
    assert _lines[0].lstrip().startswith('stale_pass (20):'), _lines[0]
    _joined = ' '.join(_lines)
    for _n in _names:
        assert _joined.count(_n) == 1, (
            f"name {_n} appeared {_joined.count(_n)} times "
            f"(expected exactly 1)"
        )
    for _line in _lines:
        assert len(_line) <= 50, (
            f"line exceeds wrap=50: {len(_line)} chars: {_line!r}"
        )



def test_sta49_clean_all_wipes_disk_chroot_and_purge_nulls_udeb_tree():
    """STA-49: `cache purge` must null the udeb (installer) dep-tree — it's
    built off the same cache, so leaving it strands a tree pointing at
    deleted files.  `clean all` must wipe the disk chroot (root-owned), or
    buildroot/disk survives while the flag reset clears chroot_disk_ready,
    claiming it's gone (and the space isn't reclaimed)."""
    _body = _session_source()
    import re
    # cache purge drops ALL THREE in-memory trees built off the cache
    _purge = re.search(
        r'def cmd_cache_purge\(self.*?(?=\n    def \w)', _body, re.DOTALL)
    assert _purge, 'cmd_cache_purge body not found'
    _p = _purge.group(0)
    for _attr in ('self.cache = None', 'self.dep_tree = None',
                  'self.udeb_dep_tree = None'):
        assert _attr in _p, f'cmd_cache_purge must do `{_attr}` (STA-49)'
    # clean all wipes all THREE buildroot chroots (live + installer + disk)
    _all = re.search(
        r'def cmd_clean_all\(self.*?(?=\n    def \w)', _body, re.DOTALL)
    assert _all, 'cmd_clean_all body not found'
    _a = _all.group(0)
    for _expr in ('self.config.dir_chroot,',           # live (bare, trailing ,)
                  'self.config.dir_chroot_installer',
                  'self.config.dir_chroot_disk'):
        assert _expr in _a, f'clean all must wipe {_expr} (STA-49)'



def test_sta50_chroot_build_gates_on_repo_auto_index():
    """STA-50: both chroot-build entry points must GATE on
    `_ensure_repo_indexed_for_chroot()` — it returns False on auto-index
    failure, and a bare (unchecked) call falls straight through into a
    multi-minute chroot bring-up guaranteed to die later on the missing
    InRelease.  Pin the `if not …: return` shape in both methods."""
    _body = _session_source()
    import re
    for _entry in ('cmd_build_chroot_live', 'cmd_build_chroot_disk'):
        _m = re.search(
            rf'def {_entry}\(self.*?(?=\n    def \w)', _body, re.DOTALL)
        assert _m, f"{_entry} body not found"
        _fn = _m.group(0)
        assert 'if not self._ensure_repo_indexed_for_chroot():' in _fn, (
            f"{_entry} must GATE on _ensure_repo_indexed_for_chroot() "
            f"(return on False), not call it bare")
    # the helper still advertises the False-on-failure contract the gate relies on
    assert 'Returns True on success or skip; False on auto-index failure.' \
        in _body, "the _ensure_repo_indexed_for_chroot contract changed"



def test_mirror_audit_disk_vs_claims_folds_superseded_claims():
    """_mirror_audit_disk_vs_claims must fold marker claims (retracted/
    deprecated/obsolete) AND the published claims their *_seq back-refs
    supersede: a pruned obsolete/deprecated file is NOT missing_on_disk
    (17 false CRITICALs, 2026-06-11), and a RETAINED superseded file is
    NOT orphan_on_disk (append-only pool keeps the bytes)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession

    _by_builder = {'athena-primary': [
        # old version: original published claim + obsolescence marker
        {'claim_state': 'published', 'seq': 704,
         'filename': 'e2fsprogs_1.47.0-2+asg1u1_amd64.deb'},
        {'claim_state': 'obsolete', 'seq': 4844, 'obsoletes_seq': 704,
         'filename': 'e2fsprogs_1.47.0-2+asg1u1_amd64.deb'},
        # deprecated: original + deprecation marker
        {'claim_state': 'published', 'seq': 900,
         'filename': 'reportbug_12.0.0_all.deb'},
        {'claim_state': 'deprecated', 'seq': 4816, 'deprecates_seq': 900,
         'filename': 'reportbug_12.0.0_all.deb'},
        # live current-version claim
        {'claim_state': 'published', 'seq': 4831,
         'filename': 'e2fsprogs_1.47.0-2+asg1u2_amd64.deb'},
    ]}

    def _mk(pool: set):
        _sess = BuildSession.__new__(BuildSession)
        _sess._mirror_audit_pool_listing = lambda url, key: set(pool)
        return _sess

    _orig = build.console.print
    build.console.print = lambda *a, **k: None
    try:
        # Case 1: superseded files pruned remotely — only the live file
        # on disk.  No findings at all.
        _f = _mk({'e2fsprogs_1.47.0-2+asg1u2_amd64.deb'})._mirror_audit_disk_vs_claims(
            'm', {'url': 'ssh://x/y'}, _by_builder)
        assert _f == [], _f
        # Case 2: superseded files RETAINED remotely (append-only) —
        # not orphans, still no findings.
        _f = _mk({'e2fsprogs_1.47.0-2+asg1u2_amd64.deb',
                  'e2fsprogs_1.47.0-2+asg1u1_amd64.deb',
                  'reportbug_12.0.0_all.deb'})._mirror_audit_disk_vs_claims(
            'm', {'url': 'ssh://x/y'}, _by_builder)
        assert _f == [], _f
        # Case 3: the LIVE file missing → still a real CRITICAL.
        _f = _mk(set())._mirror_audit_disk_vs_claims(
            'm', {'url': 'ssh://x/y'}, _by_builder)
        assert len(_f) == 1 and _f[0][1] == 'missing_on_disk', _f
        assert '+asg1u2' in _f[0][2], _f
    finally:
        build.console.print = _orig



def test_mirror_publish_reindexes_stale_local_index():
    """mirror publish must re-index when any pool artifact is newer than
    the local InRelease, not only when InRelease is missing.  Publish
    pushes dists/ verbatim and coord-head PINS its sha, so a stale index
    publishes 'cleanly' and every downstream check passes while apt
    clients keep resolving superseded metadata — caught live 2026-06-11
    (Jun-9 index served broken e2fsprogs +asg1u1 Pre-Depends alongside a
    fixed +asg1u2 pool)."""
    import sys as _sys
    import tempfile
    import time as _time
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession

    class _Stop(Exception):
        pass

    with tempfile.TemporaryDirectory() as _tmp:
        _dist = os.path.join(
            _tmp, 'repo', 'dists', 'thor', 'main', 'binary-amd64')
        os.makedirs(_dist)
        _inrel = os.path.join(_tmp, 'repo', 'dists', 'thor', 'InRelease')
        _deb = os.path.join(_dist, 'foo_1.0_amd64.deb')
        open(_inrel, 'w').write('x')
        open(_deb, 'w').write('y')
        _now = _time.time()
        # fresh state: deb (and its dir) older than InRelease
        os.utime(_deb, (_now - 500, _now - 500))
        os.utime(_dist, (_now - 500, _now - 500))
        os.utime(_inrel, (_now - 100, _now - 100))

        class _Cfg:
            dir_repo = os.path.join(_tmp, 'repo')
            build_codename = 'thor'

        def _mk_session(_calls):
            _sess = BuildSession.__new__(BuildSession)
            _sess.config = _Cfg()
            _sess._coord_self_keys = lambda: ('bid', 'priv', 'pub')
            _sess.cmd_index_repo = (
                lambda: (_calls.append('index'), False)[1])
            def _stop():
                raise _Stop()
            _sess._snapshot_current = _stop
            return _sess

        _orig = build.console.print
        build.console.print = lambda *a, **k: None
        try:
            # CASE 1 — fresh index: no re-index, proceeds to the next
            # pipeline step (our _Stop sentinel).
            _calls: 'list[str]' = []
            try:
                _mk_session(_calls).cmd_mirror_publish()
                raise AssertionError("expected _Stop past the index block")
            except _Stop:
                pass
            assert _calls == [], _calls

            # CASE 2 — pool artifact newer than InRelease: re-index
            # fires (our stub returns False → publish aborts).
            os.utime(_deb, (_now + 100, _now + 100))
            _calls = []
            _r = _mk_session(_calls).cmd_mirror_publish()
            assert _calls == ['index'], _calls
            assert _r is False, _r

            # CASE 3 — deletion-only change (dir newer, no newer file):
            # re-index fires via the pool-dir mtime.
            os.utime(_deb, (_now - 500, _now - 500))
            os.utime(_dist, (_now + 100, _now + 100))
            _calls = []
            _r = _mk_session(_calls).cmd_mirror_publish()
            assert _calls == ['index'], _calls
            assert _r is False, _r
        finally:
            build.console.print = _orig



def test_preflight_repo_audit_blocks_on_stale_artifacts():
    """The chroot pre-flight repo audit includes the stale-file scan and
    GATES on it: a superseded .deb lingering in repo/ is silently
    consumable by the chroot installer (find_matching_artifact accepts
    any +asg-stamped variant — stale e2fsprogs +asg1u1 poisoned the disk
    image while the fixed +asg1u2 sat beside it, 2026-06-11).  Clean
    scan → proceeds without prompting; any version-drift artifact →
    prompts (answer n → abort)."""
    import sys as _sys
    import types as _types
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    import commands.cmd_audit as _ca
    import utils as _utils

    class _Flags:
        dep_check_ready = True

    class _Cfg:
        dir_log = '/nonexistent'
        dir_repo = '/nonexistent'

    _state = _types.SimpleNamespace(packages={'a': object()})
    _fake_repo_audit = _types.SimpleNamespace(
        scan_repo_state=lambda cfg: _state,
        audit_dep_closure=lambda st, consumer_set=None: ([], None),
        detect_dangling_asg_equals_pins=lambda st, consumer_set=None: [],
        audit_conflict_cohort=lambda st, cohort: [],
    )

    _prompted = []

    class _FakePrompt:
        def __init__(self, *a, **k):
            _prompted.append(a)

        def get_response(self):
            return 'n'

    _orig_ra, _ca.repo_audit = _ca.repo_audit, _fake_repo_audit
    _orig_pr, _ca.Prompt = _ca.Prompt, _FakePrompt
    _orig_vh = _utils.verify_output_hashes
    _utils.verify_output_hashes = (
        lambda *a, **k: {'mismatched': [], 'scanned': 0})
    _orig_print = build.console.print
    build.console.print = lambda *a, **k: None
    try:
        _drift_file = ('main', 'e2fsprogs_1.47.0-2+asg1u1_amd64.deb',
                       'e2fsprogs', 100)
        for _drift, _expect in (([], True), ([_drift_file], False)):
            _sess = BuildSession.__new__(BuildSession)
            _sess.flags = _Flags()
            _sess.config = _Cfg()
            _sess._resolve_install_corpus = lambda: None
            _sess._resolve_live_cohort = lambda: None
            _sess._resolve_installer_cohort = lambda: None
            _sess._scan_stale_files = (
                lambda _d=_drift: ([], list(_d), [], [], 1))
            _prompted.clear()
            _r = _sess._preflight_audit_repo()
            assert _r is _expect, (_drift, _r)
            # gate prompts ONLY when stale files exist
            assert bool(_prompted) == (not _expect), (_drift, _prompted)
    finally:
        _ca.repo_audit = _orig_ra
        _ca.Prompt = _orig_pr
        _utils.verify_output_hashes = _orig_vh
        build.console.print = _orig_print



def test_cmd_source_fork_disable_writes_marker_and_invalidates_state():
    """source fork <pkg> disabled — writes `.disabled` marker at
    fork/source/<pkg>/ AND clears cache_ready + dep_check_ready so the
    next pipeline run honours the change.  Pin the file + flag effects."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import build as _build_mod
    with tempfile.TemporaryDirectory() as _tmp:
        _fork_src = os.path.join(_tmp, 'fork', 'source')
        _pkg_dir = os.path.join(_fork_src, 'foo')
        os.makedirs(os.path.join(_pkg_dir, 'debian'), exist_ok=True)
        with open(os.path.join(_pkg_dir, 'debian', 'control'), 'w') as fh:
            fh.write('Source: foo\nPackage: foo\n')

        class _Cfg:
            dir_fork_source = _fork_src
        class _Flags:
            cache_ready = True
            dep_check_ready = True

        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.flags = _Flags
        _sess.cmd_source_fork('foo', 'disabled')

        _marker = os.path.join(_pkg_dir, '.disabled')
        assert os.path.isfile(_marker), (
            "`source fork foo disabled` must write .disabled marker"
        )
        assert _sess.flags.cache_ready is False, (
            "disable must invalidate cache_ready"
        )
        assert _sess.flags.dep_check_ready is False, (
            "disable must invalidate dep_check_ready"
        )



def test_cmd_source_fork_enable_removes_marker():
    """source fork <pkg> enabled — removes a previously-written
    `.disabled` marker.  Idempotent: re-enable on already-enabled
    fork is a no-op (no error)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import build as _build_mod
    with tempfile.TemporaryDirectory() as _tmp:
        _fork_src = os.path.join(_tmp, 'fork', 'source')
        _pkg_dir = os.path.join(_fork_src, 'foo')
        os.makedirs(os.path.join(_pkg_dir, 'debian'), exist_ok=True)
        with open(os.path.join(_pkg_dir, 'debian', 'control'), 'w') as fh:
            fh.write('Source: foo\nPackage: foo\n')
        _marker = os.path.join(_pkg_dir, '.disabled')
        with open(_marker, 'w') as fh:
            fh.write('disabled\n')

        class _Cfg:
            dir_fork_source = _fork_src
        class _Flags:
            cache_ready = True
            dep_check_ready = True

        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.flags = _Flags
        _sess.cmd_source_fork('foo', 'enabled')

        assert not os.path.exists(_marker), (
            "`source fork foo enabled` must remove .disabled marker"
        )
        # Re-enable: idempotent, no error.
        _sess.cmd_source_fork('foo', 'enabled')
        assert not os.path.exists(_marker)



def test_cmd_source_audit_classifies_rebuilds_by_subset():
    """source audit must group rebuild candidates by which `source build
    <mode>` covers them (pkg / installer / live / pool / recommended).
    Operator needs to know which command to run to address each bucket.
    (Was cmd_source_rescan's responsibility before P2 — folded into
    cmd_source_audit which is the read-only superset.)"""
    _body = _session_source()
    import re
    _m = re.search(
        r'def cmd_source_audit\(self, \*args\):.*?(?=\n    def )',
        _body, re.DOTALL,
    )
    assert _m, "cmd_source_audit not found"
    _method = _m.group(0)
    for _label in ('pkg', 'installer', 'live', 'pool', 'recommended'):
        assert f"'{_label}'" in _method, (
            f"audit must classify rebuild candidates as '{_label}' so "
            f"the operator knows which `source build` mode addresses them")
    for _attr in ('live_exclusive_src_names',
                  'installer_exclusive_src_names',
                  'extras_src_names'):
        assert _attr in _method, (
            f"audit must consult dep_tree.{_attr} to classify candidates")
    assert ("'source build'" in _method
            or 'source build installer' in _method), (
        "audit must print the command needed for each subset")



def test_cmd_source_audit_verbose_lists_tunneled_and_failed_names():
    """source audit verbose must drill into the terminal-state buckets
    (tunneled, fail, no_pkgs) — operator saw the counts in the
    non-verbose run and asked verbose to print the names too.
    Source-grep test pins the contract (the method does subprocess +
    dep-tree work that's too heavy to fixture into a unit-level run)."""
    _body = _session_source()
    import re
    _m = re.search(
        r'def cmd_source_audit\(self, \*args\):.*?(?=\n    def )',
        _body, re.DOTALL,
    )
    assert _m, "cmd_source_audit not found"
    _method = _m.group(0)
    # Informational tuple includes the three terminal buckets
    assert re.search(
        r"_informational\s*=\s*\(\s*'tunneled'\s*,\s*'fail'\s*,\s*'no_pkgs'\s*\)",
        _method,
    ), "verbose must drill into tunneled / fail / no_pkgs"
    # And the drill-down is gated on _verbose
    assert re.search(
        r"if _verbose:\s*\n\s+_informational\s*=", _method,
    ), "informational drill-down must be gated on the verbose flag"
    # Per-name annotation reuses _subset_for so the listing matches
    # the actionable-block format
    assert "_subset_for(_n)" in _method, (
        "verbose listings must annotate each name with its subset")



def test_iso_installer_call_site_passes_audit_flag():
    """build.py's installer-iso call site must pass
    audit_identity_scan from config so the [Audit] knob actually
    reaches iso_installer.  Source-level pin — otherwise the kwarg
    silently defaults to True and the operator-disabled audit still
    runs."""
    _body = _session_source()
    assert 'audit_identity_scan=self.config.audit_identity_scan' in _body, (
        'build.py must thread self.config.audit_identity_scan into '
        'iso_installer.build_installer_iso — otherwise [Audit] '
        'IdentityScan = false has no effect on iso build'
    )



def test_remote_container_init_wired():
    """`container remote init` builds a recipe-only (connect=False) container;
    remotebuild auto-inits it so it needs no local image."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as _f:
        _b = _f.read()
    assert 'def cmd_init_remote_container' in _b
    # two-level dispatch: `container remote init` → cmd_init_remote_container
    assert "action == 'remote'" in _b
    assert 'def _cmd_container_remote' in _b and 'cmd_init_remote_container()' in _b
    assert 'connect=False' in _b
    with open(os.path.join(_ROOT, 'scripts', 'commands', 'cmd_source.py')) as _f:
        assert 'self.cmd_init_remote_container()' in _f.read()



def test_container_two_level_command_surface_wired():
    """CONFIG-SPLIT Chunk 4: `container` dispatches local/remote groups, and
    the remote group routes add/list/delete/purge/test to handlers backed by
    the remote.conf helpers."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as _f:
        _b = _f.read()
    assert "action == 'local'" in _b and "action == 'remote'" in _b
    for _m in ('def _cmd_container_local', 'def _cmd_container_remote',
               'def cmd_container_local_test', 'def cmd_container_remote_add',
               'def cmd_container_remote_list', 'def cmd_container_remote_delete',
               'def cmd_container_remote_purge', 'def cmd_container_remote_test'):
        assert _m in _b, _m
    # the add/delete handlers go through the utils remote.conf helpers
    assert 'utils.add_remote(' in _b and 'utils.delete_remote(' in _b
    assert 'utils.list_remotes(' in _b



def test_container_init_remote_ensures_image():
    """`container init remote` ensures the remote image for the SELECTED
    snapshot (confirm / LAN-transfer / build), gated on RemoteBuildHost — not a
    passive local-only object."""
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as _f:
        _b = _f.read()
    _s = _b.index('def cmd_init_remote_container')
    _e = _b.index('\n    def ', _s + 1)
    _body = _b[_s:_e]
    assert 'ensure_remote_image(' in _body
    assert 'remote_build_host' in _body          # gated on a configured remote
    assert 'connect=False' in _body



def test_remotebuild_command_wired():
    """`source remotebuild` is dispatched, the handler + RemoteBuildHost config
    exist — the local `source build` path is separate."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as _f:
        _b = _f.read()
    assert "action == 'remotebuild'" in _b and 'cmd_source_remotebuild' in _b
    with open(os.path.join(_ROOT, 'scripts', 'commands', 'cmd_source.py')) as _f:
        _cs = _f.read()
    assert 'def cmd_source_remotebuild' in _cs
    # The per-package remote build body lives in _remotebuild_one_source (the
    # fan-out worker).  Its build STREAM (run_remote_agent — the REMOTE-API
    # transport) goes to the per-package log file, NOT the console tab (N
    # concurrent workers would interleave) — the call must use the file writer.
    _rb = _cs[_cs.index('def _remotebuild_one_source'):]
    _rb = _rb[:_rb.index('\n    def ', 1)]
    _after = _rb[_rb.index('run_remote_agent('):]
    assert 'log=_to_log' in _after[:400]
    assert 'log=console.print' not in _after     # build stream not to console
    assert 'buildlog_path' in _rb and '_to_log' in _rb   # → log/build/<pkg>
    # The per-remote SSH key + API token thread through to the agent transport.
    assert 'ssh_key=' in _after[:400] and 'token=' in _after[:400]
    with open(os.path.join(_ROOT, 'scripts', 'utils.py')) as _f:
        assert "'RemoteBuildHost'" in _f.read()



def test_copy_ssh_key_copies_with_0600_and_delete_removes_key():
    """utils.copy_ssh_key copies a key 0600 (False on missing src); `container
    remote delete` removes the copied config/<name>.key."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    with tempfile.TemporaryDirectory() as _tmp:
        _src = os.path.join(_tmp, 'id')
        with open(_src, 'w') as _f:
            _f.write('PRIVATE-KEY')
        _dst = os.path.join(_tmp, 'remote.key')
        assert utils.copy_ssh_key(_src, _dst) is True
        with open(_dst) as _f:
            assert _f.read() == 'PRIVATE-KEY'
        assert (os.stat(_dst).st_mode & 0o777) == 0o600
        assert utils.copy_ssh_key(os.path.join(_tmp, 'nope'), _dst) is False
    # delete handler removes the key file (source-level: os.remove of <name>.key)
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as _f:
        _b = _f.read()
    _del = _b[_b.index('def cmd_container_remote_delete'):]
    _del = _del[:_del.index('\n    def ', 1)]
    assert 'os.remove' in _del and '.key' in _del, (
        "delete must remove the copied config/<name>.key")



def test_container_remote_add_is_guided_with_probes():
    """`container remote add` copies the key into config/, runs the service +
    capacity probes, and persists via add_remote (the guided registration)."""
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as _f:
        _b = _f.read()
    _add = _b[_b.index('def cmd_container_remote_add'):]
    _add = _add[:_add.index('\n    def ', 1)]
    assert 'copy_ssh_key' in _add, "add must copy the key into config/"
    assert 'probe_remote_build_host' in _add, "add must run the service check"
    assert 'probe_ssh_auth' in _add, "add must validate ssh auth"
    assert 'add_remote' in _add, "add must persist to remote.conf"



def test_remotebuild_fanout_respects_slot_caps_and_requeues():
    """`_remotebuild_fanout` never runs more concurrent builds on a remote than
    its MaxParallelBuilds, builds every package, and re-queues a transport-failed
    package onto another remote."""
    import time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import remote_orchestrate as _ro
    from build import BuildSession

    class _Cfg:
        tunnel_packages: set = set()
        heavy_packages: set = set()
    class _Container:
        _image_tag = 'athenalinux:build-test'
    _sess = BuildSession.__new__(BuildSession)
    _sess.config = _Cfg()
    _sess.container = _Container()

    _lock = threading.Lock()
    _cur = {'A': 0, 'B': 0}
    _peak = {'A': 0, 'B': 0}
    _seen: 'list[str]' = []
    _transport_done = {'fired': False}

    def _fake_one(_src, _slot, _po, _force, register_proc=None,
                  bump_active=False, bump_release=None):
        _name = _slot['name']
        with _lock:
            _cur[_name] += 1
            _peak[_name] = max(_peak[_name], _cur[_name])
        time.sleep(0.04)
        with _lock:
            _cur[_name] -= 1
            _seen.append(_src.package)
        if _src.package == 'tfail' and not _transport_done['fired']:
            _transport_done['fired'] = True
            return ('transport', 0)
        return ('built', 0)
    _sess._remotebuild_one_source = _fake_one      # type: ignore[assignment]

    class _P:
        def __init__(_s, _n):
            _s.package = _n
    _pkgs = [_P(f'p{_i}') for _i in range(8)] + [_P('tfail')]
    _remotes = [
        {'name': 'A', 'host': 'ssh://u@a', 'ssh_key': '',
         'max_parallel_builds': 2, 'build_cpus': 0.0, 'build_memory': ''},
        {'name': 'B', 'host': 'ssh://u@b', 'ssh_key': '',
         'max_parallel_builds': 1, 'build_cpus': 0.0, 'build_memory': ''},
    ]
    _orig_ensure = _ro.ensure_remote_image
    _ro.ensure_remote_image = lambda *a, **k: 'present'
    try:
        _sess._remotebuild_fanout(_pkgs, _remotes, None, False)
    finally:
        _ro.ensure_remote_image = _orig_ensure
    # Never oversubscribe a remote beyond its MaxParallelBuilds.
    assert _peak['A'] <= 2, f"remote A oversubscribed: peak {_peak['A']}"
    assert _peak['B'] <= 1, f"remote B oversubscribed: peak {_peak['B']}"
    # Both remotes were actually used (work distributed, not all on one host).
    assert _peak['A'] >= 1 and _peak['B'] >= 1
    # All 9 packages built; tfail was attempted twice (transport → re-queue).
    assert _sess.last_source_build_counts['built'] == 9, (
        _sess.last_source_build_counts)
    assert _seen.count('tfail') == 2, "transport-failed pkg must be re-queued"



def test_virtual_buildlog_writes_predicted_and_filtered():
    """OBS-04 companion: `virtual build` writes `<pkg>.vbuildlog` with the
    PREDICTED artifact set + a FILTERED (declared-but-not-predicted) list,
    so it can be diffed against the real `<pkg>.buildlog`."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _tmp:
        os.makedirs(os.path.join(_tmp, 'build'))
        _sess = BuildSession.__new__(BuildSession)

        class _Cfg:
            dir_log = _tmp
            build_profiles = frozenset({'nodoc'})

        _sess.config = _Cfg()

        class _Src:
            version = '2.5.1-4'
            binary = ['attr', 'libattr1', 'libattr1-dev',
                      'attr-udeb', 'libattr1-udeb']

        # libattr1-dev intentionally absent from records → must land in FILTERED
        _records = [
            {'Filename': 'pool/main/a/attr/attr_2.5.1-4_amd64.deb'},
            {'Filename': 'libattr1_2.5.1-4_amd64.deb'},
            {'Filename': 'attr-udeb_2.5.1-4_amd64.udeb'},
            {'Filename': 'libattr1-udeb_2.5.1-4_amd64.udeb'},
        ]
        _sess._write_virtual_buildlog('attr', _Src(), _records, 'amd64', 1)
        _p = os.path.join(_tmp, 'build', 'attr.vbuildlog')
        assert os.path.isfile(_p), "vbuildlog not written"
        _txt = open(_p).read()
        assert 'VIRTUAL LOG: attr' in _txt
        assert 'PREDICTED ARTIFACTS (4)' in _txt
        assert 'attr_2.5.1-4_amd64.deb' in _txt          # basename, not pool path
        assert 'libattr1-udeb_2.5.1-4_amd64.udeb' in _txt
        assert 'FILTERED (declared but not predicted: 1)' in _txt
        assert 'libattr1-dev' in _txt



def test_comp03_phase4_build_one_source_skip_src_returns_skipped():
    """COMP-03 Phase 4 worker contract: _build_one_source returns
    ('skipped', 0) when the source is on cache.skip_src.  Behaviour
    test using a thin BuildSession stub."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    _sess = build.BuildSession.__new__(build.BuildSession)

    import shutil

    class _FakeCache:
        skip_src = {'skipped-pkg'}

    _tmp = tempfile.mkdtemp(prefix='skip-src-build-one-')

    class _FakeConfig:
        tunnel_packages = []
        dir_log = _tmp

    class _FakeContainer:
        # Present so the assert-non-None at the top of _build_one_source
        # passes; the skip-src early-return doesn't touch it otherwise.
        asg_ledger = None
    _sess.cache = _FakeCache()
    _sess.config = _FakeConfig()
    _sess.container = _FakeContainer()

    class _Src:
        package = 'skipped-pkg'
        version = '1.0'

    try:
        _result, _ = _sess._build_one_source(_Src(), False, False, None, None)
        assert _result == 'skipped', _result
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)



def test_comp03_phase4_build_one_source_tunneled_calls_do_tunnel():
    """COMP-03 Phase 4 worker contract: when src is on tunnel_packages,
    _build_one_source returns ('tunneled', 0) on successful download
    and ('failed', 0) on tunnel failure — NOT ('built', 0).  Mocked."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    import shutil
    _sess = build.BuildSession.__new__(build.BuildSession)

    class _FakeCache:
        skip_src = set()

    _tmp = tempfile.mkdtemp(prefix='tunneled-build-one-')

    class _FakeConfig:
        tunnel_packages = ['tunnel-me']
        dir_log = _tmp

    class _FakeContainer:
        asg_ledger = None

        def check_build(self, _src, _expected):
            return False  # not previously tunneled
    _sess.cache = _FakeCache()
    _sess.config = _FakeConfig()
    _sess.container = _FakeContainer()
    # Stub out _predicted_files_for_source and _tunnel_filenames_for_source.
    _sess._predicted_files_for_source = lambda _n: []
    _sess._tunnel_filenames_for_source = lambda _n: []
    _sess._do_tunnel = lambda _src: True  # tunnel succeeds

    class _Src:
        package = 'tunnel-me'
        version = '1.0'

    try:
        _result, _ = _sess._build_one_source(_Src(), False, False, None, None)
        assert _result == 'tunneled', _result

        _sess._do_tunnel = lambda _src: False  # tunnel fails
        _result, _ = _sess._build_one_source(_Src(), False, False, None, None)
        assert _result == 'failed', _result
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)



def test_build_one_source_tunneled_branch_uses_pristine_for_check_build():
    """Tunneled packages must be normalised post-download (strip + asg-
    stamp) so they land on disk at pristine / +asg-stamped names — the
    SAME names a from-source build would produce.  Therefore the skip
    gate in _build_one_source's tunnel branch passes the PRISTINE
    prediction (`_predicted_files_for_source`) to check_build, NOT the
    upstream-suffixed filenames.  Without that, a previously-tunneled
    pkg whose on-disk file is pristine-named would re-trigger _do_tunnel
    every run.
    """
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    import shutil
    _sess = build.BuildSession.__new__(build.BuildSession)

    _tmp = tempfile.mkdtemp(prefix='tunnel-pristine-')

    class _FakeCache:
        skip_src = set()

    class _FakeConfig:
        tunnel_packages = ['firefox-esr']
        dir_log = _tmp

    _check_calls: 'list[list[str]]' = []

    class _FakeContainer:
        asg_ledger = None

        def check_build(self, _src, _expected):
            _check_calls.append(list(_expected))
            return True  # pristine-named file already on disk → skip

    _sess.cache = _FakeCache()
    _sess.config = _FakeConfig()
    _sess.container = _FakeContainer()
    # Distinguishable lists so we can assert which one reached check_build.
    _sess._predicted_files_for_source = lambda _n: [
        'firefox-esr_140.10.2esr-1_amd64.deb']           # pristine
    _sess._tunnel_filenames_for_source = lambda _n: [
        'firefox-esr_140.10.2esr-1~deb12u1_amd64.deb']   # upstream
    _do_tunnel_calls: 'list[str]' = []
    _sess._do_tunnel = lambda _src: (
        _do_tunnel_calls.append(_src.package) or True)

    class _Src:
        package = 'firefox-esr'
        version = '140.10.2esr-1~deb12u1'

    try:
        _result, _ = _sess._build_one_source(
            _Src(), False, False, None, None)
        assert _result == 'skipped', f"expected 'skipped', got {_result!r}"
        assert _check_calls, "check_build was never called"
        assert _check_calls[0] == [
            'firefox-esr_140.10.2esr-1_amd64.deb'], (
            f"check_build got {_check_calls[0]!r} — should be PRISTINE "
            "(strip-NMU pristine name), not upstream-suffixed")
        assert not _do_tunnel_calls, (
            f"_do_tunnel was called {_do_tunnel_calls!r}; should have "
            "skipped because the pristine file is already on disk")
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)



def test_tunnel_filenames_full_set_arch_profile_filtered():
    """Option A: _tunnel_filenames_for_source returns the source's FULL
    declared binary set filtered by arch + active profiles (the same
    gates virtual uses) — resolving non-closure binaries from the full
    cache universe — not just the dep-closure subset.  arch-mismatched
    and profile-dropped binaries are excluded."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_pkg
    apt_pkg.init_system()
    from build import BuildSession

    class _Cfg:
        arch = 'amd64'
        build_profiles = frozenset({'nodoc'})
        dir_fork_source_repo = None

    class _Mirror:
        id = 'main'

    class _Src:
        package = 'fw'
        version = '1'                     # real Source always carries .version
        binary = ['fw-common', 'fw-amd64-only', 'fw-arm-only',
                  'fw-doc', 'fw-udeb']
        package_list = [
            'fw-common deb libs optional arch=all',
            'fw-amd64-only deb libs optional arch=amd64',
            'fw-arm-only deb libs optional arch=arm64',
            'fw-doc deb doc optional arch=all profile=!nodoc',
            'fw-udeb udeb debian-installer optional arch=all',
        ]
        files: dict = {}
        _mirror = _Mirror()

    class _Cache:
        source_hashtable = {'fw': [_Src()]}

        def get_packages(self, name):
            _m = {'fw-common': 'pool/f/fw-common_1_all.deb',
                  'fw-amd64-only': 'pool/f/fw-amd64-only_1_amd64.deb'}
            return ([{'Version': '1', 'Filename': _m[name]}]
                    if name in _m else [])

        def udeb_view(self):
            class _V:
                def get_packages(self, name):
                    return ([{'Version': '1',
                              'Filename': 'pool/f/fw-udeb_1_all.udeb'}]
                            if name == 'fw-udeb' else [])
            return _V()

    _sess = BuildSession.__new__(BuildSession)
    _sess.cache = _Cache()
    _sess.config = _Cfg()
    _sess.dep_tree = None
    _sess.udeb_dep_tree = None
    _fns = sorted(_sess._tunnel_filenames_for_source('fw'))
    # fw-arm-only → arch-filtered; fw-doc → nodoc profile-filtered
    assert _fns == ['fw-amd64-only_1_amd64.deb',
                    'fw-common_1_all.deb',
                    'fw-udeb_1_all.udeb'], _fns



def test_cmd_virtual_dispatch_routes_build_and_run():
    """`virtual build` and `virtual run` both reach cmd_virtual_build;
    bare `virtual` also delegates (default action)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_virtual_build = lambda *a, **k: _calls.append(('vb', a))  # type: ignore
    _sess._group_help = lambda *a, **k: 'help'                          # type: ignore
    _sess.cmd_virtual('build', 'all')
    _sess.cmd_virtual('run')
    _sess.cmd_virtual('')
    assert len(_calls) == 3
    assert _calls[0] == ('vb', ('all',))
    assert _calls[1] == ('vb', ())
    assert _calls[2] == ('vb', ())



def test_cmd_virtual_build_refuses_without_cache():
    """No cache → CRITICAL error + False return.  Operator must run
    `cache build` + `cache parse` first."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    _sess.cache = None
    _lines = []
    _orig = build.console.print
    build.console.print = lambda *a, **k: _lines.append(
        ' '.join(str(x) for x in a))
    try:
        _r = _sess.cmd_virtual_build()
    finally:
        build.console.print = _orig
    assert _r is False
    assert any('cache not parsed' in _ln for _ln in _lines)



def test_cmd_virtual_build_refuses_without_dep_tree():
    """Cache present but dep_tree=None → CRITICAL error + False."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    _sess.cache = object()       # truthy but no data
    _sess.dep_tree = None
    _lines = []
    _orig = build.console.print
    build.console.print = lambda *a, **k: _lines.append(
        ' '.join(str(x) for x in a))
    try:
        _r = _sess.cmd_virtual_build()
    finally:
        build.console.print = _orig
    assert _r is False
    assert any('dep_tree not populated' in _ln for _ln in _lines)



def test_tunnel_filenames_for_source_uses_upstream_not_stripped():
    """Tunneled = pristine Debian binary passthrough; the on-disk filename
    keeps its upstream ~debNuN suffix (matches the .deb's internal Version
    AND snapshot.debian.org's actual pool path).  Caught 2026-05-28 — the
    earlier code used _predicted_files_for_source (strip_nmu pristine) and
    either 404'd at snapshot or silently fetched a same-named unstable binary
    (wrong version, wrong contents)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)

    class _Tree:
        src_pkg_files = {
            'intel-microcode': ['intel-microcode_3.20251111.1_amd64.deb'],
        }
        selected_pkgs = {
            'intel-microcode': {
                'Filename': 'pool/non-free-firmware/i/intel-microcode/'
                            'intel-microcode_3.20251111.1~deb12u1_amd64.deb',
            },
        }
    _sess.dep_tree = _Tree()
    _sess.udeb_dep_tree = None
    _actual = _sess._tunnel_filenames_for_source('intel-microcode')
    assert _actual == [
        'intel-microcode_3.20251111.1~deb12u1_amd64.deb'], _actual



def test_tunnel_filenames_falls_back_when_binary_not_in_cache():
    """No cache entry for a predicted binary → fall back to the predicted
    pristine name (best-effort; caller surfaces any 404 on download)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)

    class _Tree:
        src_pkg_files = {'ghost-src': ['ghost-bin_1.0_amd64.deb']}
        selected_pkgs: dict = {}
    _sess.dep_tree = _Tree()
    _sess.udeb_dep_tree = None
    assert _sess._tunnel_filenames_for_source('ghost-src') == \
        ['ghost-bin_1.0_amd64.deb']



def test_local_cleanup_keeps_highest_prunes_superseded_and_flags_orphan():
    """UPD-01 single-snapshot prune (behavioral): when a pristine artifact and
    its freshly +asg-stamped successor coexist, the STAMPED (highest) one is
    kept and the pristine predecessor is drift; a non-selected source is
    orphan.  This is what `repo refresh`'s post-publish prune relies on."""
    import shutil as _sh
    if not (_sh.which('dpkg-deb') and _sh.which('dpkg-scanpackages')):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _tmp:
        # Disable security so BuildConfig.__init__ doesn't early-return on a
        # missing keyring (it would leave dir_repo_main unset); a [Mirror.*]
        # section is required.
        _mirror = """
    [Mirror.main]
    Suffix =
    Component = main
    """
        _body = (_BASE_CONF_BODY.format(mirror_block=_mirror).rstrip()
                 + "\n    [Security]\n    Disabled = true\n")
        _cfg_path = _write_test_config(_tmp, _body)
        cfg = _build_config_from(_tmp, _cfg_path)
        assert not getattr(cfg, 'error_str', ''), cfg.error_str
        _main = cfg.deb_dir_for('main')
        os.makedirs(_main, exist_ok=True)
        _build_minimal_deb(os.path.join(_main, 'openssl_3.0.15-1_amd64.deb'),
                           'openssl', '3.0.15-1')
        _build_minimal_deb(os.path.join(_main, 'openssl_3.0.15-1+asg1u1_amd64.deb'),
                           'openssl', '3.0.15-1+asg1u1')
        _build_minimal_deb(os.path.join(_main, 'ghost_1.0_amd64.deb'),
                           'ghost', '1.0')

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = cfg

        class _Tree:
            selected_srcs = {'openssl': object()}
            src_pkg_files = {'openssl': ['openssl_3.0.15-1_amd64.deb']}

        _sess.dep_tree = _Tree()
        _sess.udeb_dep_tree = None
        repo_audit.invalidate_cache(cfg.dir_repo)

        (_orphan, _drift, _foreign, _malformed,
         _total) = _sess._scan_stale_files()
        _drift_names = {fn for _s, fn, _src, _sz in _drift}
        _orphan_names = {fn for _s, fn, _src, _sz in _orphan}
        assert 'openssl_3.0.15-1_amd64.deb' in _drift_names, (
            f"pristine predecessor should be drift: {_drift_names}")
        assert 'openssl_3.0.15-1+asg1u1_amd64.deb' not in _drift_names, (
            "stamped current must be kept (highest), not drift")
        assert 'openssl_3.0.15-1+asg1u1_amd64.deb' not in _orphan_names
        assert 'ghost_1.0_amd64.deb' in _orphan_names, (
            f"non-selected source should be orphan: {_orphan_names}")



# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 step 6 — workload (change-detection) + Guard A preflight
# ─────────────────────────────────────────────────────────────────────────────


def test_needs_bump_build_predicts_transpose_filename():
    """_needs_bump_build under the content-order scheme: the expected filename
    is INTRINSIC — the binary's pristine base + the source's transposed update
    marker (uniform K across the source) + our patch level — exactly what the
    stamper writes.  The published ledger is NOT consulted; a stale older +asg
    on disk does not satisfy the current generation; a clean new base is never
    a target."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    from build import BuildSession
    import utils as _u
    with tempfile.TemporaryDirectory() as _tmp:
        _log = os.path.join(_tmp, 'log')
        _bl = os.path.join(_log, 'build')
        os.makedirs(_bl)
        _sess = BuildSession.__new__(BuildSession)

        class _Cfg:
            build_version = '1'
            dir_log = _log
            def deb_dest_for_filename(self, f, component='main'):
                return _tmp
        _sess.config = _Cfg()

        class _Src:
            def __init__(self, v):
                self.version = v

        def _plant(fn):
            open(os.path.join(_tmp, fn), 'w').close()

        # (1) clean new base (strip is a no-op) → never a bump-target
        _sess._predicted_files_for_source = lambda n: ['foo_1.3-1_amd64.deb']
        assert _sess._needs_bump_build('foo', _Src('1.3-1'), 1) is False

        # (2) security re-spin +deb1u1 → expected intrinsic +asg1u1; absent → target
        _sess._predicted_files_for_source = lambda n: ['foo_1.2-3_amd64.deb']
        _delta = _Src('1.2-3+deb1u1')        # strips to 1.2-3, K=1
        assert _sess._needs_bump_build('foo', _delta, 1) is True
        _plant('foo_1.2-3+asg1u1_amd64.deb')                 # plant the exact gen
        assert _sess._needs_bump_build('foo', _delta, 1) is False

        # (3) a NEWER upstream re-spin +deb1u2 (K=2) expects +asg1u2; the stale
        # on-disk u1 must NOT satisfy it (exact check, ledger-independent).
        _delta2 = _Src('1.2-3+deb1u2')
        assert _sess._needs_bump_build('foo', _delta2, 1) is True
        _plant('foo_1.2-3+asg1u2_amd64.deb')
        assert _sess._needs_bump_build('foo', _delta2, 1) is False

        # (4) uniform K across a source's binaries: both expect +asg1u1 (same
        # source suffix); one present, one missing → still a target.
        _sess._predicted_files_for_source = lambda n: [
            'foo_1.2-3_amd64.deb', 'foo-data_1.2-3_amd64.deb']
        assert _sess._needs_bump_build('foo', _delta, 1) is True
        _plant('foo-data_1.2-3+asg1u1_amd64.deb')
        assert _sess._needs_bump_build('foo', _delta, 1) is False

        # (5) a PATCHED re-spin: the build record's patch level makes the
        # expected filename +asg1u1+p1 — read from the prior record, not minted.
        _sess._predicted_files_for_source = lambda n: ['bar_1.2-3_amd64.deb']
        _rec = _u.new_build_record(package='bar',
                                   intended_version='1.2-3+deb1u1',
                                   patch_set_hash='deadbeef',
                                   started='2026-06-29T00:00:00Z')
        _rec['patch_bump_count'] = 1
        _rec.update({'phase': 'done', 'status': 'PASS'})
        _u.write_build_record(_bl, _rec)
        assert _sess._needs_bump_build('bar', _delta, 1) is True
        _plant('bar_1.2-3+asg1u1+p1_amd64.deb')
        assert _sess._needs_bump_build('bar', _delta, 1) is False



def test_needs_bump_build_shim_signed_uses_binary_own_version():
    """Regression (audit #6): _needs_bump_build must predict each binary's
    filename from the binary's OWN upstream version (like the normalizer and
    virtual_build), NOT source.pristine + the SOURCE's suffix.  For shim-signed
    the binary's trailing marker (~deb12u1, after the embedded +15.8-1) differs
    from the source suffix (+deb12u1), so the source-suffix reconstruction
    predicts +asg (wrong SIGN) and never matches the ~asg file the build
    actually writes — an infinite-rebuild risk.  With the binary's own version
    the predicted file is the ~asg one the normalizer produces, so a present
    file means no rebuild."""
    import sys
    import types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _tmp:
        _log = os.path.join(_tmp, 'log')
        os.makedirs(os.path.join(_log, 'build'))
        _sess = BuildSession.__new__(BuildSession)

        class _Cfg:
            build_version = '1'
            dir_log = _log

            def deb_dest_for_filename(self, f, component='main'):
                return _tmp
        _sess.config = _Cfg()

        class _BinPkg:
            def __init__(self, v):
                self.version = v
        # The binary's OWN upstream version carries a TRAILING ~deb12u1 (after
        # the embedded +15.8-1); the source's is a trailing +deb12u1.
        _sess.dep_tree = types.SimpleNamespace(selected_pkgs={
            'shim-signed': _BinPkg('1.44~1+deb12u1+15.8-1~deb12u1')})
        _sess.udeb_dep_tree = None
        # The predicted filename carries the binary's PRISTINE base.
        _sess._predicted_files_for_source = lambda n: [
            'shim-signed_1.44~1+deb12u1+15.8-1_amd64.deb']

        class _Src:
            version = '1.44~1+deb12u1'         # source's own (trailing +deb12u1)

        assert _sess._needs_bump_build('shim-signed', _Src(), 1) is True
        # Plant the ~asg file the normalizer actually writes (NOT +asg).
        open(os.path.join(
            _tmp, 'shim-signed_1.44~1+deb12u1+15.8-1~asg1u1_amd64.deb'),
            'w').close()
        # Present → no rebuild.  The buggy +asg predictor would look for the
        # wrong-signed name and still report True here.
        assert _sess._needs_bump_build('shim-signed', _Src(), 1) is False



def test_audit_state_reclassifies_security_respin_as_needs_bump():
    """`_audit_state` surfaces a same-base security/NMU re-spin as
    'needs_bump' when `_source_state` calls it 'ok' (lenient +asg presence)
    but `_needs_bump_build` says a fresh +asg bump is due AND the source is in
    the snapshot-delta workload — so the audit rebuild queue matches UPDATE-mode
    `source build`.  A bump-due re-spin OUTSIDE the workload must stay 'ok'
    (else every adopted ~debNuN false-flags — the 178 regression).  Hard states
    and release=None are left intact."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    _src = object()
    _wl = {'apache2'}                       # apache2 IS in the delta workload

    # 'ok' + bump due + IN workload → 'needs_bump'
    _sess._source_state = lambda p, s: 'ok'
    _sess._needs_bump_build = lambda p, s, rl: True
    assert _sess._audit_state('apache2', _src, 1, _wl) == 'needs_bump'

    # bump due but NOT in workload (adopted ~debNuN that didn't change) →
    # stays 'ok' — the 178-false-flag regression guard
    assert _sess._audit_state('glibc', _src, 1, _wl) == 'ok'

    # 'ok' but NO bump due → stays 'ok'
    _sess._needs_bump_build = lambda p, s, rl: False
    assert _sess._audit_state('apache2', _src, 1, _wl) == 'ok'

    # a hard state is NEVER reclassified, even if a bump would be due
    _sess._source_state = lambda p, s: 'needs_build'
    _sess._needs_bump_build = lambda p, s, rl: True
    assert _sess._audit_state('apache2', _src, 1, _wl) == 'needs_build'

    # release=None (non-integer VERSION) → bump check skipped, returns 'ok'
    _sess._source_state = lambda p, s: 'ok'
    _sess._needs_bump_build = lambda p, s, rl: True
    assert _sess._audit_state('apache2', _src, None, _wl) == 'ok'



def test_preflight_stamp_invariant_roundtrips_and_flags_bad_version():
    """Guard A: a clean prediction round-trips (no offenders); a non-integer
    [Build] VERSION is flagged BEFORE any build."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    _sess._predicted_files_for_source = lambda n: (
        ['openssl_3.0.16-1_amd64.deb'] if n == 'openssl' else [])

    class _Cfg:
        build_version = '1'

    _sess.config = _Cfg()
    assert _sess._preflight_stamp_invariant(['openssl']) == [], (
        "a valid prediction must round-trip through the asg stamp")

    class _CfgBad:
        build_version = '0.1'

    _sess.config = _CfgBad()
    _off = _sess._preflight_stamp_invariant(['openssl'])
    assert _off and _off[0][0] == '<config>', (
        "non-integer VERSION must be flagged before building")



def test_snapshot_base_subcommand_fully_removed():
    """MIRROR-01 Phase 1: `snapshot base` was removed without a compat
    redirect (we're still in beta — clean cut).  `cmd_snapshot('base')`
    falls through to the unknown-action help table.  The legacy
    `_cmd_snapshot_base` method is gone."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    assert not hasattr(_sess, '_cmd_snapshot_base'), (
        "_cmd_snapshot_base method must be removed in MIRROR-01 Phase 1")
    _lines = []
    _orig = build.console.print
    build.console.print = lambda *a, **k: _lines.append(
        ' '.join(str(x) for x in a))
    try:
        _sess.cmd_snapshot('base')
    finally:
        build.console.print = _orig
    _joined = '\n'.join(_lines)
    # _group_help signature: "Unknown <group> action: '<arg>'"
    assert "Unknown snapshot action: 'base'" in _joined, _joined
    # And the help table must NOT advertise 'base' anywhere as a subcommand
    assert ' base ' not in _joined.replace('base-', 'BASEHYPHEN_'), (
        "the snapshot help table must not advertise a 'base' subcommand")



def test_snapshot_select_interactive_sets_chosen_current():
    """The interactive picker lists the in-between snapshots and sets the
    chosen one as the new current (forward-only, cautioned)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from commands import cmd_snapshot
    import utils
    from build import BuildSession, BuildFlags
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _sess = BuildSession.__new__(BuildSession)
        _sess.flags = BuildFlags()     # _set_snapshot_pin invalidates cache flags

        class _Cfg:
            dir_config = _cfg_dir
            snapshot_enabled = True
            # reconcile_snapshot_pin (now called by _set_snapshot_pin) reads
            # these; an absent build.conf path makes it a graceful no-op here.
            snapshot_timestamp_config = '20260514T083402Z'
            config_path = os.path.join(_cfg_dir, 'build.conf')

        _sess.config = _Cfg()
        _sess._snapshot_current = lambda: '20260514T083402Z'
        _sess._snapshot_latest = lambda: '20260526T134919Z'
        utils._SNAPSHOT_TS_CACHE.clear()

        _answers = iter(['1', 'y'])     # pick #1, then confirm the caution

        class _FakePrompt:
            def __init__(self, _t, _m, options=None):
                pass

            def get_response(self):
                return next(_answers)

        # audit #73: the interactive picker now does a single
        # list_snapshots_and_latest GET → (latest, candidates).
        _sl = utils.list_snapshots_and_latest
        utils.list_snapshots_and_latest = lambda _c, _a: (
            '20260526T134919Z',
            ['20260518T000000Z', '20260526T134919Z'])
        _sp, _sc = cmd_snapshot.Prompt, build.console.print
        cmd_snapshot.Prompt = _FakePrompt
        build.console.print = lambda *a, **k: None
        try:
            _sess._snapshot_select_interactive()
        finally:
            utils.list_snapshots_and_latest = _sl
            cmd_snapshot.Prompt, build.console.print = _sp, _sc
        assert utils.read_snapshot_state(_sess.config)['current'] == \
            '20260518T000000Z', "picker must set the chosen ts as current"



def test_snapshot_select_syncs_build_conf_at_command_time():
    """`snapshot select` must reconcile build.conf [Snapshot] Timestamp to the
    new pin AT COMMAND TIME — not defer it to the next startup's reconcile
    (which surprises the operator with a build.conf rewrite on a later,
    unrelated run)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from commands import cmd_snapshot
    import utils
    from build import BuildSession, BuildFlags
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _conf = os.path.join(_cfg_dir, 'build.conf')
        with open(_conf, 'w') as _f:
            _f.write("[Snapshot]\nTimestamp = 20260602T173733Z\n"
                     "Enabled = true\n")
        _sess = BuildSession.__new__(BuildSession)
        _sess.flags = BuildFlags()

        class _Cfg:
            dir_config = _cfg_dir
            snapshot_enabled = True
            snapshot_timestamp_config = '20260602T173733Z'
            config_path = _conf

        _sess.config = _Cfg()
        _sess._snapshot_current = lambda: '20260602T173733Z'
        _sess._has_unpublished_local_builds = lambda: False

        class _Yes:
            def __init__(self, *a, **k):
                pass

            def get_response(self):
                return 'y'

        _sp, _sc = cmd_snapshot.Prompt, build.console.print
        cmd_snapshot.Prompt = _Yes
        build.console.print = lambda *a, **k: None
        try:
            _ok = _sess._set_snapshot_pin('20260620T203514Z')
        finally:
            cmd_snapshot.Prompt, build.console.print = _sp, _sc
        assert _ok is True
        assert utils.read_snapshot_state(_sess.config)['current'] == \
            '20260620T203514Z'
        # build.conf SYNCED now, not on the next run.
        with open(_conf) as _f:
            _body = _f.read()
        assert 'Timestamp = 20260620T203514Z' in _body, _body
        assert '20260602T173733Z' not in _body, \
            'stale TS must be gone from build.conf at command time'



def test_snapshot_select_warns_only_on_unpublished_local_builds():
    """The advance warning fires ONLY when we hold locally-BUILT, unpublished
    work (_has_unpublished_local_builds), not on a mere pin>floor difference —
    which a peer-PULLED delta (already on the mirror) would also trip under the
    old _update_build_pending guard."""
    import re
    _src = _session_source()
    _body = re.search(r'def _set_snapshot_pin\(self.*?(?=\n    def )',
                      _src, re.DOTALL).group(0)
    assert '_has_unpublished_local_builds()' in _body, "guard must use the helper"
    assert '_update_build_pending()' not in _body, (
        "the advance warning must NOT key off pin>floor (trips on pulled state)")
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildFlags, BuildSession
    from commands import cmd_snapshot
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _conf = os.path.join(_cfg_dir, 'build.conf')
        with open(_conf, 'w') as _f:
            _f.write("[Snapshot]\nTimestamp = 20260602T173733Z\nEnabled=true\n")

        class _Cfg:
            dir_config = _cfg_dir
            snapshot_enabled = True
            snapshot_timestamp_config = '20260602T173733Z'
            config_path = _conf

        for _flag in (True, False):
            _sess = BuildSession.__new__(BuildSession)
            _sess.flags = BuildFlags()
            _sess.config = _Cfg()
            _sess._snapshot_current = lambda: '20260602T173733Z'
            _sess._has_unpublished_local_builds = (lambda v=_flag: v)
            _lines: 'list[str]' = []

            class _No:
                def __init__(_s, *a, **k):
                    pass

                def get_response(_s):
                    return 'n'    # abort after the warning prints
            _sp, _sc = cmd_snapshot.Prompt, build.console.print
            cmd_snapshot.Prompt = _No
            build.console.print = lambda *a, _l=_lines, **k: _l.append(
                ' '.join(str(x) for x in a))
            try:
                _sess._set_snapshot_pin('20260620T203514Z')
            finally:
                cmd_snapshot.Prompt, build.console.print = _sp, _sc
            _joined = '\n'.join(_lines)
            if _flag:
                assert 'UNPUBLISHED' in _joined, _joined
            else:
                assert 'UNPUBLISHED' not in _joined, _joined



def test_snapshot_select_current_is_forward_only():
    """`snapshot select <older>` is REFUSED — current only moves forward.
    MIRROR-01 Phase 1: `_set_snapshot_pin` takes one positional arg (the new
    current); the legacy two-arg `(which, target)` shape was removed."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    import utils
    from build import BuildSession

    def _cap(fn):
        _lines = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            fn()
        finally:
            build.console.print = _orig
        return '\n'.join(_lines)

    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _sess = BuildSession.__new__(BuildSession)

        class _Cfg:
            dir_config = _cfg_dir

        _sess.config = _Cfg()
        _sess._snapshot_current = lambda: '20260514T083402Z'
        _out = _cap(lambda: _sess._set_snapshot_pin('20260101T000000Z'))
        assert 'REFUSED' in _out and 'forward' in _out, _out
        assert utils.read_snapshot_state(_sess.config) == {}, "no pin written"



def test_snapshot_select_force_accepts_backtrack():
    """`snapshot select force` prompts free-form and accepts an OLDER
    timestamp (bypassing forward-only).  Writes current + appends history."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from commands import cmd_snapshot
    import utils
    from build import BuildSession, BuildFlags

    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _sess = BuildSession.__new__(BuildSession)
        _sess.flags = BuildFlags()
        class _Cfg:
            dir_config = _cfg_dir
        _sess.config = _Cfg()
        # Pretend we're currently at June 2 and want to backtrack to May 14
        _sess._snapshot_current = lambda: '20260602T173733Z'
        _answers = iter(['20260514T083402Z', 'y'])  # ts, confirm
        class _FakePrompt:
            def __init__(self, _t, _m, options=None):
                pass
            def get_response(self):
                return next(_answers)
        _sp, _sc = cmd_snapshot.Prompt, build.console.print
        _orig_recon = utils.reconcile_snapshot_pin
        cmd_snapshot.Prompt = _FakePrompt
        build.console.print = lambda *a, **k: None
        # audit #72: the force path now mirrors the build.conf pin; the minimal
        # _Cfg here doesn't carry that state, so stub the reconcile.
        utils.reconcile_snapshot_pin = lambda cfg: None
        try:
            _sess.cmd_snapshot('select', 'force')
        finally:
            cmd_snapshot.Prompt, build.console.print = _sp, _sc
            utils.reconcile_snapshot_pin = _orig_recon
        _st = utils.read_snapshot_state(_sess.config)
        assert _st['current'] == '20260514T083402Z', _st
        _h = utils.read_snapshot_history(_sess.config)
        assert _h == ['20260514T083402Z'], _h
        # Cache flags invalidated so the operator must re-resolve.
        assert _sess.flags.cache_ready is False
        assert _sess.flags.dep_check_ready is False



def test_snapshot_select_force_cancels_on_empty_or_no():
    """Empty timestamp OR 'n' confirmation → no write."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from commands import cmd_snapshot
    import utils
    from build import BuildSession, BuildFlags

    def _run(answers):
        with tempfile.TemporaryDirectory() as _tmp:
            _cfg_dir = os.path.join(_tmp, 'config')
            os.makedirs(_cfg_dir)
            _sess = BuildSession.__new__(BuildSession)
            _sess.flags = BuildFlags()
            class _Cfg:
                dir_config = _cfg_dir
            _sess.config = _Cfg()
            _sess._snapshot_current = lambda: '20260602T173733Z'
            _it = iter(answers)
            class _FakePrompt:
                def __init__(self, _t, _m, options=None):
                    pass
                def get_response(self):
                    return next(_it)
            _sp, _sc = cmd_snapshot.Prompt, build.console.print
            cmd_snapshot.Prompt = _FakePrompt
            build.console.print = lambda *a, **k: None
            try:
                _sess.cmd_snapshot('select', 'force')
            finally:
                cmd_snapshot.Prompt, build.console.print = _sp, _sc
            return utils.read_snapshot_state(_sess.config)
    # Empty input cancels
    assert _run(['', 'y']) == {}
    # Operator says n to the y/n cancels
    assert _run(['20260514T083402Z', 'n']) == {}



def test_snapshot_select_force_rejects_malformed_timestamp():
    """force prompt validates YYYYMMDDTHHMMSSZ shape; garbage input → no write."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from commands import cmd_snapshot
    import utils
    from build import BuildSession, BuildFlags
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _sess = BuildSession.__new__(BuildSession)
        _sess.flags = BuildFlags()
        class _Cfg:
            dir_config = _cfg_dir
        _sess.config = _Cfg()
        _sess._snapshot_current = lambda: '20260602T173733Z'
        _answers = iter(['not-a-timestamp'])
        class _FakePrompt:
            def __init__(self, _t, _m, options=None):
                pass
            def get_response(self):
                return next(_answers)
        _sp, _sc = cmd_snapshot.Prompt, build.console.print
        cmd_snapshot.Prompt = _FakePrompt
        build.console.print = lambda *a, **k: None
        try:
            _sess.cmd_snapshot('select', 'force')
        finally:
            cmd_snapshot.Prompt, build.console.print = _sp, _sc
        assert utils.read_snapshot_state(_sess.config) == {}



def test_ensure_snapshot_pins_prompts_and_writes_when_unset():
    """A fresh system (no config/snapshot.state) prompts for current ONLY on
    cache build; accepting the default writes the pin AND appends history.

    MIRROR-01 Phase 1: archive-floor `base` was removed (moves to per-mirror
    state in Phase 4); only `current` is prompted."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from commands import cmd_snapshot
    import utils
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _sess = BuildSession.__new__(BuildSession)

        class _Cfg:
            dir_config = _cfg_dir
            dir_cache = _tmp
            snapshot_enabled = True
            snapshot_timestamp_config = '20260514T083402Z'

        _sess.config = _Cfg()
        utils._SNAPSHOT_TS_CACHE.clear()

        _answers = iter([''])      # accept default current; no base prompt

        class _FakePrompt:
            def __init__(self, _t, _m, options=None):
                pass

            def get_response(self):
                return next(_answers)

        _sp, _sc = cmd_snapshot.Prompt, build.console.print
        cmd_snapshot.Prompt = _FakePrompt
        build.console.print = lambda *a, **k: None
        try:
            assert _sess._ensure_snapshot_pins() is True
        finally:
            cmd_snapshot.Prompt, build.console.print = _sp, _sc
        _st = utils.read_snapshot_state(_sess.config)
        assert _st['current'] == '20260514T083402Z', _st
        assert 'base' not in _st, "MIRROR-01: base must NOT be written"
        # snapshot.history populated with the just-set ts
        _h = utils.read_snapshot_history(_sess.config)
        assert _h == ['20260514T083402Z'], _h
        # already pinned → no prompt, returns True (would StopIteration if it asked)
        assert _sess._ensure_snapshot_pins() is True



def test_ensure_snapshot_pins_aborts_when_no_selection():
    """If the operator never supplies a valid timestamp, cache build aborts
    (returns False) rather than silently building at an undefined snapshot."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from commands import cmd_snapshot
    import utils
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _sess = BuildSession.__new__(BuildSession)

        class _Cfg:
            dir_config = _cfg_dir
            dir_cache = _tmp
            snapshot_enabled = True
            snapshot_timestamp_config = 'latest'     # default resolves via query

        _sess.config = _Cfg()
        _sess._snapshot_latest = lambda: None        # 'latest' won't resolve
        utils._SNAPSHOT_TS_CACHE.clear()

        class _FakePrompt:
            def __init__(self, _t, _m, options=None):
                pass

            def get_response(self):
                return ''                            # always default → 'latest'

        _sp, _sc = cmd_snapshot.Prompt, build.console.print
        cmd_snapshot.Prompt = _FakePrompt
        build.console.print = lambda *a, **k: None
        try:
            assert _sess._ensure_snapshot_pins() is False
        finally:
            build.Prompt, build.console.print = _sp, _sc
        assert utils.read_snapshot_state(_sess.config) == {}, "no pins written"



def test_do_update_build_sets_source_build_ready_on_nothing_to_build():
    """`_do_update_build`'s "everything up-to-date — nothing to build"
    early-return must set `source_build_ready = True`.

    Pre-fix: cmd_source_build reset the flag to False on entry, then
    routed to _do_update_build for UPDATE mode.  When the workload was
    empty (nothing changed since published), _do_update_build returned
    without re-arming the flag.  Autorun then saw source_build_ready
    still False and aborted the next stage with "'source build
    installer' did not complete — aborting" — even though there was
    nothing wrong; the binaries were all up-to-date.

    Caught 2026-05-31 autorun installer.  Source-level check: scan
    the _do_update_build body for the up-to-date branch and verify
    the success flag gets set there."""
    import re
    _body = _session_source()
    _u = re.search(r'def _do_update_build\(self.*?(?=\n    def )',
                    _body, re.DOTALL)
    assert _u, '_do_update_build not found'
    _ub = _u.group(0)
    # Find the "nothing to build" branch + assert it sets the flag.
    _nothing_to_build = re.search(
        r'if not _delta_to_build and not _extra:.*?return',
        _ub, re.DOTALL,
    )
    assert _nothing_to_build, (
        "could not locate the 'nothing to build' branch in _do_update_build"
    )
    _branch = _nothing_to_build.group(0)
    assert 'self.flags.source_build_ready = True' in _branch, (
        "'nothing to build' is a success case but the early-return doesn't "
        "re-arm source_build_ready; autorun will abort the next stage.  "
        "Set the flag True before `return`."
    )



def test_workload_current_to_target_diffs_against_target_snapshot():
    """`snapshot workload`'s core: a source is in the workload iff its version
    AT THE TARGET snapshot is newer than the current pin's version."""
    import shutil as _sh
    if not _sh.which('dpkg'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    import repo_audit
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)

    class _Src:
        def __init__(self, v):
            self.version = v

    class _Tree:
        selected_srcs = {'openssl': _Src('3.0.15-1'), 'libc6': _Src('2.36-9')}

    _sess.dep_tree = _Tree()
    _sess.udeb_dep_tree = None
    _sess.config = object()
    _saved = repo_audit.fetch_source_versions_at
    try:
        repo_audit.fetch_source_versions_at = lambda _c, _ts: {
            'openssl': '3.0.16-1',     # advanced at target
            'libc6': '2.36-9',         # unchanged
        }
        _names, _err = _sess._workload_current_to_target('20260601T000000Z')
        assert _err is None and _names == ['openssl'], (_names, _err)
        # fetch failure → (None, error)
        repo_audit.fetch_source_versions_at = lambda _c, _ts: None
        _names, _err = _sess._workload_current_to_target('20260601T000000Z')
        assert _names is None and _err
    finally:
        repo_audit.fetch_source_versions_at = _saved



def test_workload_detects_debNuN_source_change_ignores_unchanged():
    """Detection is by FULL source version: a +debNuN security/point-release
    SOURCE upload (3.0.15-1 → 3.0.15-1+deb12u2) is caught; a source whose
    version is unchanged (the binNMU case — +bN never appears in Sources) is
    ignored."""
    import shutil as _sh
    if not _sh.which('dpkg'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    import repo_audit
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)

    class _Src:
        def __init__(self, v):
            self.version = v

    class _Tree:
        selected_srcs = {'openssl': _Src('3.0.15-1'), 'bind9': _Src('1.2-1')}

    _sess.dep_tree = _Tree()
    _sess.udeb_dep_tree = None
    _sess.config = object()
    _saved = repo_audit.fetch_source_versions_at
    try:
        repo_audit.fetch_source_versions_at = lambda _c, _ts: {
            'openssl': '3.0.15-1+deb12u2',   # security SOURCE upload → rebuild
            'bind9': '1.2-1',                # source unchanged (binNMU only) → skip
        }
        _names, _err = _sess._workload_current_to_target('20260601T000000Z')
        assert _err is None and _names == ['openssl'], (_names, _err)
    finally:
        repo_audit.fetch_source_versions_at = _saved



def test_workload_since_snapshot_diffs_published_to_current():
    """`repo refresh`'s rebuild set: a source whose CURRENT version is newer
    than at the PUBLISHED snapshot (or absent there) is rebuilt."""
    import shutil as _sh
    if not _sh.which('dpkg'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    import repo_audit
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)

    class _Src:
        def __init__(self, v):
            self.version = v

    class _Tree:
        # versions at the CURRENT pin
        selected_srcs = {
            'openssl': _Src('3.0.15-1+deb12u2'),   # advanced since published
            'libc6': _Src('2.36-9'),               # unchanged since published
            'newpkg': _Src('1.0-1'),               # absent at published → new
        }

    _sess.dep_tree = _Tree()
    _sess.udeb_dep_tree = None
    _sess.config = object()
    _saved = repo_audit.fetch_source_versions_at
    try:
        # versions AT THE PUBLISHED snapshot
        repo_audit.fetch_source_versions_at = lambda _c, _ts: {
            'openssl': '3.0.15-1',          # older at published → rebuild
            'libc6': '2.36-9',              # same → skip
        }
        _names, _err = _sess._workload_since_snapshot('20260514T083402Z')
        assert _err is None
        assert _names == ['newpkg', 'openssl'], _names   # advanced + new
    finally:
        repo_audit.fetch_source_versions_at = _saved



def test_workload_excludes_forks_from_snapshot_diff():
    """Forks are LOCAL (cache stamps `_mirror.id == 'fork'`) and never appear in
    any upstream snapshot's Sources, so they must NOT be flagged as changed by
    the snapshot-to-snapshot diff — otherwise every update build rebuilds them
    to an identical filename, which `_segregate_built_artifacts` then drops as a
    dup.  Regression for the 'our forks featured in the drift' bug."""
    import shutil as _sh
    if not _sh.which('dpkg'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    import repo_audit
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)

    class _Mirror:
        def __init__(self, _id):
            self.id = _id

    class _Src:
        def __init__(self, v, _mirror=None):
            self.version = v
            self._mirror = _mirror

    class _Tree:
        # current pin versions; the forks carry `_mirror.id == 'fork'`
        selected_srcs = {
            'openssl': _Src('3.0.16-1'),
            'athena-branding': _Src('1.2.0', _Mirror('fork')),
            'base-files': _Src('12.4+deb12u14+athena2', _Mirror('fork')),
        }

    _sess.dep_tree = _Tree()
    _sess.udeb_dep_tree = None
    _sess.config = object()
    _saved = repo_audit.fetch_source_versions_at
    try:
        # Forks are absent from upstream Sources (as they always are) — they must
        # be skipped, not flagged via the absent ("new since") branch.
        # current_to_target: a source is flagged when its version AT TARGET is
        # newer than the current pin.
        repo_audit.fetch_source_versions_at = lambda _c, _ts: {'openssl': '3.0.17-1'}
        _names, _err = _sess._workload_current_to_target('20260601T000000Z')
        assert _err is None and _names == ['openssl'], (_names, _err)
        # since_snapshot: flagged when the current pin is newer than at published.
        repo_audit.fetch_source_versions_at = lambda _c, _ts: {'openssl': '3.0.15-1'}
        _names, _err = _sess._workload_since_snapshot('20260514T083402Z')
        assert _err is None and _names == ['openssl'], (_names, _err)
    finally:
        repo_audit.fetch_source_versions_at = _saved




# ─────────────────────────────────────────────────────────────────────────────
# UPD-02 — index on the remote (dpkg-scanpackages on the VM)
# ─────────────────────────────────────────────────────────────────────────────

def test_index_minimal_stages_nested_subset():
    """cmd_index_repo_minimal stages the runtime subset in the SAME nested
    binary-<arch>/ layout as full (no flat pool/), excluding debug/source."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _tmp:
        _mirror = """
    [Mirror.main]
    Suffix =
    Component = main
    """
        _body = (_BASE_CONF_BODY.format(mirror_block=_mirror).rstrip()
                 + "\n    [Security]\n    Disabled = true\n")
        _cfg_path = _write_test_config(_tmp, _body)
        cfg = _build_config_from(_tmp, _cfg_path)
        _main = cfg.deb_dir_for('main')
        os.makedirs(_main, exist_ok=True)
        _build_minimal_deb(os.path.join(_main, 'openssl_3.0.15-1_amd64.deb'),
                           'openssl', '3.0.15-1')
        _build_minimal_deb(
            os.path.join(_main, 'openssl-dbgsym_3.0.15-1_amd64.deb'),
            'openssl-dbgsym', '3.0.15-1')

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = cfg
        assert _capture_console_print(
            lambda: _sess.cmd_index_repo_minimal()) is not None
        _rel = os.path.relpath(_main, cfg.dir_repo)
        _dst = os.path.join(cfg.dir_publish, _rel)
        # nested layout, runtime deb present, dbgsym excluded, NO flat pool/
        assert os.path.exists(os.path.join(_dst, 'openssl_3.0.15-1_amd64.deb'))
        assert not os.path.exists(
            os.path.join(_dst, 'openssl-dbgsym_3.0.15-1_amd64.deb'))
        assert not os.path.isdir(os.path.join(cfg.dir_publish, 'pool')), (
            "minimal must NOT use a flat pool/ — unified nested layout")




def test_ux04_buildflags_autosave_round_trip():
    """BuildFlags.load reads what autosave wrote; persisted-True flags
    survive a process boundary."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildFlags
    with tempfile.TemporaryDirectory() as _tmp:
        _path = os.path.join(_tmp, 'buildflags.json')
        _flags = BuildFlags(save_path=_path)
        _flags.download_ready = True
        _flags.source_build_ready = True
        # File should exist after the autosave
        assert os.path.isfile(_path)
        # Load fresh
        _restored = BuildFlags.load(_path)
        # Non-in-memory flags persisted
        assert _restored.download_ready is True
        assert _restored.source_build_ready is True



def test_ux04_buildflags_in_memory_only_reset_on_load():
    """_IN_MEMORY_ONLY flags (cache_ready, dep_check_ready,
    build_container_ready, signing_key_verified) are reset to False on
    load even if the JSON has them True — their backing in-memory state
    hasn't been re-established yet."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildFlags
    import json as _json
    with tempfile.TemporaryDirectory() as _tmp:
        _path = os.path.join(_tmp, 'buildflags.json')
        # Write a JSON with every flag True.
        with open(_path, 'w') as _fh:
            _json.dump({
                '_format_version': 1,
                'flags': dict.fromkeys(BuildFlags._FIELDS, True),
            }, _fh)
        _flags = BuildFlags.load(_path)
        # In-memory-only flags reset.
        for _f in BuildFlags._IN_MEMORY_ONLY:
            assert getattr(_flags, _f) is False, f"{_f} not reset"
        # Other flags preserved.
        _kept = set(BuildFlags._FIELDS) - BuildFlags._IN_MEMORY_ONLY
        for _f in _kept:
            assert getattr(_flags, _f) is True, f"{_f} not preserved"



def test_cve_command_registered():
    """`cve` command must be registered in main() alongside `sbom`
    so the operator can invoke it from the TUI."""
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    assert "register_command('cve'" in _body, (
        "main() must register the `cve` command — otherwise "
        "cmd_cve is unreachable from the TUI"
    )



def test_build_system_sh_grype_is_non_blocking():
    """build-system.sh checks for grype at startup but does NOT exit
    on its absence — grype is OPTIONAL.  Source-level pin: the check
    block must use the warn helper (non-fatal) and must NOT die or
    `exit 1`."""
    _bs = os.path.join(_ROOT, 'build-system.sh')
    with open(_bs) as fh:
        _body = fh.read()
    assert 'command -v grype' in _body, (
        'build-system.sh must check for grype on PATH'
    )
    import re
    # Match the grype-check block: from the `if [ -x "$(command -v grype`
    # line through the closing `fi`.
    _m = re.search(
        r'if \[ -x "\$\(command -v grype.*?\nfi',
        _body, re.DOTALL,
    )
    assert _m, 'grype check block not found in build-system.sh'
    _block = _m.group(0)
    assert 'exit 1' not in _block and 'die ' not in _block, (
        'grype check must be non-blocking (no exit/die on absence)'
    )
    assert 'warn "grype' in _block, (
        'grype-absent branch must use the warn (non-fatal) helper'
    )



def test_sbom_command_registered():
    """`sbom` command must be registered in main() so the operator
    can invoke it from the TUI.  Source-level pin."""
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    assert "register_command('sbom'" in _body, (
        "main() must register the `sbom` command — otherwise the "
        "operator has no way to invoke cmd_sbom from the TUI"
    )



def test_source_state_interrupted_when_record_is_non_terminal():
    """A build.json at phase=container_exited (process killed before
    phase=done) must classify as 'interrupted', not silently 'ok' or
    'needs_build'.  This is the crash-recovery signal."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod
    import utils as _u

    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)
        with open(os.path.join(_repo, 'foo_1.0_amd64.deb'), 'wb') as fh:
            fh.write(b'!<arch>\n')

        _rec = _u.new_build_record(
            package='foo', intended_version='1.0', patch_set_hash='',
            started='2026-06-02T14:00:00Z',
        )
        _rec.update({
            'phase': 'container_exited', 'exit_code': 0,
            'finished': '2026-06-02T14:01:00Z', 'elapsed_seconds': 60.0,
        })
        _u.write_build_record(_buildlog, _rec)

        class _Src:
            pkgs = ['foo_1.0_amd64.deb']
            files = {}
            version = '1.0'
            patch_list = []

        class _Cfg:
            dir_repo = _repo
            dir_log = os.path.join(_tmp, 'log')
            dir_source = os.path.join(_tmp, 'source')
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            @staticmethod
            def deb_dest_for_filename(_f, _comp="main"): return _repo

        class _Container:
            buildlog_path = _buildlog
            @staticmethod
            def is_ar_file(_p): return True

        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = MagicMock(src_pkg_files={'foo': list(_Src.pkgs)})
        _sess.udeb_dep_tree = None
        _sess.container = _Container
        # _predicted_files_for_source uses dep_tree.src_pkg_files
        _sess.flags = MagicMock(build_container_ready=True)

        _state = _sess._source_state('foo', _Src())
        assert _state == 'interrupted', (
            f"non-terminal phase must classify as 'interrupted', got {_state}")



def test_source_state_tunneled_record_with_missing_binaries_routes_to_stale_pass():
    """Regression: a build.json at phase=tunneled where the predicted
    pristine binary is NOT on disk must classify as 'stale_pass', not
    'tunneled'.  Audit-and-build asymmetry was the root cause of
    `source audit` giving a clean chit while `source build all`
    re-attempted the tunnel — audit's early-return on
    record_state=='tunneled' skipped the disk check that
    check_build/find_matching_artifact perform on the build side.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod
    import utils as _u

    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        _src_dir = os.path.join(_tmp, 'source')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)
        os.makedirs(_src_dir, exist_ok=True)
        # Deliberately DO NOT plant the pristine binary in _repo.

        _rec = _u.new_build_record(
            package='firefox-esr',
            intended_version='140.10.2esr-1~deb12u1',
            patch_set_hash='',
        )
        _rec.update({
            'phase':           'tunneled',
            'status':          'TUNNELED',
            'built_version':   '140.10.2esr-1',
            'finished':        _u._utc_now_iso(),
            'elapsed_seconds': 5.0,
            'outputs':         ['firefox-esr_140.10.2esr-1_amd64.deb'],
        })
        _u.write_build_record(_buildlog, _rec)

        class _Src:
            pkgs = ['firefox-esr_140.10.2esr-1_amd64.deb']
            files = {}
            version = '140.10.2esr-1~deb12u1'
            patch_list = []

        class _Cfg:
            dir_repo = _repo
            dir_log = os.path.join(_tmp, 'log')
            dir_source = _src_dir
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            @staticmethod
            def deb_dest_for_filename(_f, _comp="main"): return _repo

        class _Container:
            buildlog_path = _buildlog
            @staticmethod
            def is_ar_file(_p): return True

        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = MagicMock(src_pkg_files={
            'firefox-esr': list(_Src.pkgs)})
        _sess.udeb_dep_tree = None
        _sess.container = _Container
        _sess.flags = MagicMock(build_container_ready=True)

        _state = _sess._source_state('firefox-esr', _Src())
        assert _state == 'stale_pass', (
            f"tunneled record with no on-disk binary must be 'stale_pass' "
            f"(matching what check_build sees), got {_state!r}")



def test_source_state_tunneled_record_with_pristine_binary_returns_tunneled():
    """Mirror case: a build.json at phase=tunneled AND the predicted
    pristine binary IS on disk (the post-strip on-disk form) → audit
    correctly returns 'tunneled', not 'stale_pass'.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod
    import buildcontainer as _bc
    import utils as _u

    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        _src_dir = os.path.join(_tmp, 'source')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)
        os.makedirs(_src_dir, exist_ok=True)
        # Plant the pristine binary (what _do_tunnel lands after strip).
        with open(os.path.join(_repo, 'firefox-esr_140.10.2esr-1_amd64.deb'),
                  'wb') as _fh:
            _fh.write(b'!<arch>\n')

        _rec = _u.new_build_record(
            package='firefox-esr',
            intended_version='140.10.2esr-1~deb12u1',
            patch_set_hash='',
        )
        _rec.update({
            'phase':           'tunneled',
            'status':          'TUNNELED',
            'built_version':   '140.10.2esr-1',
            'finished':        _u._utc_now_iso(),
            'elapsed_seconds': 5.0,
            'outputs':         ['firefox-esr_140.10.2esr-1_amd64.deb'],
        })
        _u.write_build_record(_buildlog, _rec)

        class _Src:
            pkgs = ['firefox-esr_140.10.2esr-1_amd64.deb']
            files = {}
            version = '140.10.2esr-1~deb12u1'
            patch_list = []

        class _Cfg:
            dir_repo = _repo
            dir_log = os.path.join(_tmp, 'log')
            dir_source = _src_dir
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            @staticmethod
            def deb_dest_for_filename(_f, _comp="main"): return _repo

        class _Container:
            buildlog_path = _buildlog
            @staticmethod
            def is_ar_file(_p): return True

        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = MagicMock(src_pkg_files={
            'firefox-esr': list(_Src.pkgs)})
        _sess.udeb_dep_tree = None
        _sess.container = _Container
        _sess.flags = MagicMock(build_container_ready=True)

        # _source_state calls buildcontainer.BuildContainer.is_ar_file
        # directly (staticmethod, no instance) — that reads the file with
        # python-debian's DebFile, which our dummy ar-header doesn't
        # satisfy.  Patch for the duration of this test.
        _orig_is_ar = _bc.BuildContainer.is_ar_file
        _bc.BuildContainer.is_ar_file = staticmethod(lambda _p: True)
        try:
            _state = _sess._source_state('firefox-esr', _Src())
        finally:
            _bc.BuildContainer.is_ar_file = _orig_is_ar
        assert _state == 'tunneled', (
            f"tunneled record with pristine binary on disk should classify "
            f"as 'tunneled', got {_state!r}")



def test_shorten_origin_compacts_long_pool_url():
    """A long snapshot.debian.org pool URL collapses to
    `host/.../<last 5 parts>`; short URLs pass through."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from commands import cmd_tunnel as _b
    _long = (
        'https://snapshot.debian.org/archive/debian-security/'
        '20260602T173733Z/pool/updates/main/f/firefox-esr'
    )
    _out = _b._shorten_origin(_long, max_len=70)
    assert _out == (
        'snapshot.debian.org/.../pool/updates/main/f/firefox-esr'
    ), _out
    _short = 'https://example.com/pool/main/p/pkg'
    assert _b._shorten_origin(_short, max_len=70) == _short



def test_source_state_uses_origin_component_for_disk_lookup():
    """Regression: amd64-microcode lives in non-free-firmware/binary-arch/,
    not main/binary-arch/.  `_source_state` must derive the component
    from `src._mirror.component` (mirroring `check_build` at
    buildcontainer.py:1537) — otherwise audit looks under main and
    reports stale_pass for a perfectly-good tunneled non-free-firmware
    binary, while `source build all` (which DOES use the component)
    skips it correctly.  Symptom we saw 2026-06-04: tunnel succeeds,
    audit reports stale_pass, repair clears the record, re-tunnel,
    audit reports stale_pass again — infinite loop because audit was
    looking in the wrong dir the whole time.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod
    import buildcontainer as _bc
    import utils as _u

    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        _src_dir = os.path.join(_tmp, 'source')
        # Plant the binary under non-free-firmware/binary-amd64/, NOT main/.
        _nff_dir = os.path.join(_repo, 'non-free-firmware', 'binary-amd64')
        _main_dir = os.path.join(_repo, 'main', 'binary-amd64')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_nff_dir, exist_ok=True)
        os.makedirs(_main_dir, exist_ok=True)
        os.makedirs(_src_dir, exist_ok=True)
        with open(os.path.join(_nff_dir,
                                'amd64-microcode_3.20250311.1_amd64.deb'),
                  'wb') as _fh:
            _fh.write(b'!<arch>\n')

        _rec = _u.new_build_record(
            package='amd64-microcode',
            intended_version='3.20250311.1~deb12u1',
            patch_set_hash='',
        )
        _rec.update({
            'phase':           'tunneled',
            'status':          'TUNNELED',
            'built_version':   '3.20250311.1',
            'finished':        _u._utc_now_iso(),
            'elapsed_seconds': 5.0,
            'outputs':         ['amd64-microcode_3.20250311.1_amd64.deb'],
        })
        _u.write_build_record(_buildlog, _rec)

        class _Mirror:
            component = 'non-free-firmware'

        class _Src:
            pkgs = ['amd64-microcode_3.20250311.1_amd64.deb']
            files = {}
            version = '3.20250311.1~deb12u1'
            patch_list = []
            _mirror = _Mirror()

        # Static method on the config; routes to the right component dir.
        def _deb_dest(_f, _comp='main'):
            if _comp == 'non-free-firmware':
                return _nff_dir
            return _main_dir

        class _Cfg:
            dir_repo = _repo
            dir_log = os.path.join(_tmp, 'log')
            dir_source = _src_dir
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            deb_dest_for_filename = staticmethod(_deb_dest)

        class _Container:
            buildlog_path = _buildlog

        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = MagicMock(src_pkg_files={
            'amd64-microcode': list(_Src.pkgs)})
        _sess.udeb_dep_tree = None
        _sess.container = _Container
        _sess.flags = MagicMock(build_container_ready=True)

        _orig_is_ar = _bc.BuildContainer.is_ar_file
        _bc.BuildContainer.is_ar_file = staticmethod(lambda _p: True)
        try:
            _state = _sess._source_state('amd64-microcode', _Src())
        finally:
            _bc.BuildContainer.is_ar_file = _orig_is_ar

        assert _state == 'tunneled', (
            f"non-free-firmware tunneled package with pristine binary "
            f"at non-free-firmware/binary-arch/ must classify as "
            f"'tunneled' (audit's deb_dest_for_filename call must pass "
            f"the component, mirroring check_build); got {_state!r}")



def test_detect_build_audit_divergence_flags_missing_binary_with_no_fail_record():
    """Gate: when audit classifies a source as 'needs_build' (binary
    missing on disk) but the build record does NOT say 'fail' — i.e.
    no explicit declared failure — that's a divergence between the
    skip-gate (check_build) and the audit classifier (_source_state),
    and `_detect_build_audit_divergence` must surface it as a finding.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod

    _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
    _sess.cache = MagicMock(skip_src=set())
    _sess.config = MagicMock(dir_log='/tmp/divergence-test-irrelevant')
    # Stub _source_state to return 'needs_build' for foo, and
    # utils.read_build_record to return None (no record).
    _sess._source_state = lambda _n, _s: 'needs_build'  # type: ignore

    class _Pkg:
        package = 'foo'

    import utils as _u
    _orig_read = _u.read_build_record
    _u.read_build_record = lambda _bl, _n: None
    try:
        _findings = _sess._detect_build_audit_divergence([_Pkg()])
    finally:
        _u.read_build_record = _orig_read

    assert len(_findings) == 1, _findings
    assert _findings[0] == ('foo', 'needs_build', 'missing'), _findings[0]



def test_detect_build_audit_divergence_silent_when_record_says_fail():
    """A source the build declared as failed (record phase=failed) is
    EXPECTED to fail audit too — that's consistent, not a divergence.
    The gate must NOT flag it.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod
    import utils as _u

    _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
    _sess.cache = MagicMock(skip_src=set())
    _sess.config = MagicMock(dir_log='/tmp/divergence-test-irrelevant')
    _sess._source_state = lambda _n, _s: 'fail'  # type: ignore

    class _Pkg:
        package = 'foo'

    _orig_read = _u.read_build_record
    _u.read_build_record = lambda _bl, _n: {
        'phase':  'failed',
        'status': 'FAIL',
    }
    try:
        _findings = _sess._detect_build_audit_divergence([_Pkg()])
    finally:
        _u.read_build_record = _orig_read

    assert _findings == [], (
        f"record=fail must not be a divergence finding; got {_findings!r}")



def test_detect_build_audit_divergence_silent_on_clean_state():
    """When every source audits as 'ok' or 'tunneled', the gate
    returns an empty list — the happy path.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod

    _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
    _sess.cache = MagicMock(skip_src=set())
    _sess.config = MagicMock(dir_log='/tmp/divergence-test-irrelevant')
    _sess._source_state = lambda _n, _s: (   # type: ignore
        'ok' if _n == 'a' else 'tunneled')

    class _Pkg:
        def __init__(self, name): self.package = name

    _findings = _sess._detect_build_audit_divergence([_Pkg('a'), _Pkg('b')])
    assert _findings == [], _findings



def test_detect_build_audit_divergence_excludes_skip_src():
    """skip_src packages are the operator's declared "leave alone" set.
    Audit may flag them as needs_build (they're unbuilt by design); the
    divergence gate must NOT count that as a build/audit disagreement.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod

    _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
    _sess.cache = MagicMock(skip_src={'opt-out'})
    _sess.config = MagicMock(dir_log='/tmp/divergence-test-irrelevant')
    _sess._source_state = lambda _n, _s: 'needs_build'  # type: ignore

    class _Pkg:
        def __init__(self, name): self.package = name

    _findings = _sess._detect_build_audit_divergence([_Pkg('opt-out')])
    assert _findings == [], (
        f"skip_src package must be excluded; got {_findings!r}")



def test_detect_build_audit_divergence_flags_stale_pass_tunneled_record():
    """The exact bug class this gate exists for: build record says
    `phase=tunneled` but the on-disk file is missing/wrong-shape so
    audit classifies as 'stale_pass'.  Before the audit fix, audit
    would have said 'tunneled' and the divergence would have gone
    unnoticed; the gate is the safety net for any future regression.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod
    import utils as _u

    _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
    _sess.cache = MagicMock(skip_src=set())
    _sess.config = MagicMock(dir_log='/tmp/divergence-test-irrelevant')
    _sess._source_state = lambda _n, _s: 'stale_pass'  # type: ignore

    class _Pkg:
        package = 'firefox-esr'

    _orig_read = _u.read_build_record
    _u.read_build_record = lambda _bl, _n: {
        'phase':  'tunneled',
        'status': 'TUNNELED',
    }
    try:
        _findings = _sess._detect_build_audit_divergence([_Pkg()])
    finally:
        _u.read_build_record = _orig_read

    assert _findings == [('firefox-esr', 'stale_pass', 'tunneled')], _findings



def test_cmd_source_repair_clears_interrupted_record():
    """Repair on an interrupted record must clear all marker files so
    the next source build re-runs cleanly."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod
    import utils as _u

    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)
        with open(os.path.join(_repo, 'foo_1.0_amd64.deb'), 'wb') as fh:
            fh.write(b'!<arch>\n')

        _rec = _u.new_build_record(
            package='foo', intended_version='1.0', patch_set_hash='',
        )
        _rec['phase'] = 'segregated'
        _u.write_build_record(_buildlog, _rec)
        _record_path = os.path.join(_buildlog, 'foo.build.json')
        assert os.path.exists(_record_path)

        class _Src:
            pkgs = ['foo_1.0_amd64.deb']
            files = {}
            version = '1.0'
            patch_list = []

        class _Cfg:
            dir_repo = _repo
            dir_log = os.path.join(_tmp, 'log')
            dir_source = os.path.join(_tmp, 'source')
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            @staticmethod
            def deb_dest_for_filename(_f, _comp="main"): return _repo

        class _Container:
            buildlog_path = _buildlog
            @staticmethod
            def is_ar_file(_p): return True

        class _Flags:
            cache_ready = True
            dep_check_ready = True
            build_container_ready = True

        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = MagicMock(
            selected_srcs={'foo': _Src()},
            src_pkg_files={'foo': list(_Src.pkgs)},
        )
        _sess.udeb_dep_tree = None
        _sess.container = _Container
        _sess.flags = _Flags

        _sess.cmd_source_repair()

        assert not os.path.exists(_record_path), (
            "repair must clear an interrupted build.json record")



def test_print_build_times_aggregates_elapsed_across_records():
    """`print build-times` reads every <pkg>.build.json under log/build/,
    sorts by elapsed_seconds, and sums for the snapshot-pivot estimate."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import print_commands as _pc
    import utils as _u

    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        os.makedirs(_buildlog, exist_ok=True)
        for _pkg, _elapsed in (('libwmf', 156.0), ('libreoffice', 2820.0),
                                ('alsa-lib', 12.5)):
            _rec = _u.new_build_record(
                package=_pkg, intended_version='1.0', patch_set_hash='',
            )
            _rec.update({
                'phase': 'done', 'status': 'PASS', 'built_version': '1.0',
                'elapsed_seconds': _elapsed,
            })
            _u.write_build_record(_buildlog, _rec)

        class _Cfg:
            dir_log = os.path.join(_tmp, 'log')

        class _Sess:
            config = _Cfg()

        _records = _pc._iter_build_records(_Sess())
        _names = {r['package'] for r in _records}
        assert _names == {'libwmf', 'libreoffice', 'alsa-lib'}, _names
        _total = sum(r['elapsed_seconds'] for r in _records)
        assert int(_total) == 2988, f"total elapsed wrong: {_total}"



def test_print_build_times_skips_tampered_records():
    """A tampered build.json must not appear in the build-times view —
    the audit's tamper-detection cascades through this surface."""
    import sys, json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import print_commands as _pc
    import utils as _u

    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        os.makedirs(_buildlog, exist_ok=True)
        _u.write_build_record(_buildlog, _u.new_build_record(
            package='clean', intended_version='1.0', patch_set_hash='',
        ))
        # Tamper one record by post-write edit.
        _u.write_build_record(_buildlog, _u.new_build_record(
            package='dirty', intended_version='1.0', patch_set_hash='',
        ))
        _path = os.path.join(_buildlog, 'dirty.build.json')
        with open(_path) as _fh:
            _d = json.load(_fh)
        _d['intended_version'] = 'tampered'
        with open(_path, 'w') as _fh:
            json.dump(_d, _fh)

        class _Cfg:
            dir_log = os.path.join(_tmp, 'log')

        class _Sess:
            config = _Cfg()

        _names = {r['package'] for r in _pc._iter_build_records(_Sess())}
        assert _names == {'clean'}, (
            f"tampered record must be skipped; saw {_names}")



def test_release_iso_descriptors_finds_and_reports_missing():
    """_release_iso_descriptors discovers the current version+snapshot
    live/installer ISOs in image/ and reports which REQUIRED kinds are
    absent (the gate's input)."""
    import sys as _sys, tempfile
    from unittest.mock import patch
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    import utils

    with tempfile.TemporaryDirectory() as _img:
        class _Cfg:
            dir_image = _img
            build_version = '1'
            build_distribution = 'asgard'
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()

        _snap = '20260602T173733Z'
        with patch.object(utils, 'snapshot_iso_tag', return_value=_snap):
            # nothing present → both required kinds missing
            _found, _missing = _sess._release_iso_descriptors()
            assert _found == [] and sorted(_missing) == ['installer', 'live']
            # plant the two ISOs
            for _name in (f'athena-1-{_snap}-amd64.iso',
                          f'athena-installer-1-{_snap}-amd64.iso'):
                with open(os.path.join(_img, _name), 'wb') as _fh:
                    _fh.write(b'iso')
            _found, _missing = _sess._release_iso_descriptors()
            _kinds = {_d['kind'] for _d in _found}
            assert _kinds == {'live', 'installer'}, _kinds
            assert _missing == []
            assert all(_d['sha256'] and _d['size'] == 3 for _d in _found)
            # removing the installer → reported missing
            os.remove(os.path.join(
                _img, f'athena-installer-1-{_snap}-amd64.iso'))
            _found, _missing = _sess._release_iso_descriptors()
            assert _missing == ['installer'], _missing



def test_mirror_publish_release_gate_and_push_wired():
    """STA/CI-01 Stage 1 source pins: cmd_mirror_publish gates on the
    current-snapshot ISOs (bypassable via --no-iso) and pushes the static
    index + ISOs after a successful repo publish."""
    import inspect, sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _pub = inspect.getsource(BuildSession.cmd_mirror_publish)
    assert '_release_iso_descriptors()' in _pub, _pub
    assert "'--no-iso'" in _pub and '_missing_isos' in _pub, _pub
    assert '_push_release_assets(' in _pub, _pub
    _push = inspect.getsource(BuildSession._push_release_assets)
    assert 'release_index' in _push and 'releases.json' in _push, _push
    assert '/iso/' in _push, _push
    # ISOs immutable (no overwrite); index files overwrite
    assert 'overwrite=False' in _push and 'overwrite=True' in _push, _push



def test_mirror_recompute_base_returns_empty_when_no_claims():
    """Fresh mirror (empty claims dir / missing keyring) → empty
    string; caller preserves the seed-at-add-time value."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_cache = _td
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        assert _sess._mirror_recompute_base('nonexistent') == ''



def test_mirror_pull_write_build_records_built_pkg_records_pulled_from():
    """MIRROR-02 chunk 10: a non-tunneled claim pulled from a mirror
    yields a local build.json with phase=done + pulled_from set to
    {mirror_name, owner_builder}.  Lets source audit distinguish
    "we built it" from "we pulled it"."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _utils
    from build import BuildSession

    with tempfile.TemporaryDirectory() as _td:
        _log = os.path.join(_td, 'log')
        _buildlog = os.path.join(_log, 'build')
        os.makedirs(_buildlog)

        class _Cfg:
            dir_log = _log
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _claim = {
            'package':          'libnss3',
            'intended_version': '2:3.87.1-1+deb12u1',
            'built_version':    '2:3.87.1-1+deb12u1',
            'filename':         'libnss3_2:3.87.1-1+deb12u1_amd64.deb',
            'sha256':           'a' * 64,
            'snapshot':         'S',
            'builder':          'athena-team-b',  # owner
            'claim_state':      'published',
            # no republished_from → non-tunneled
        }
        _per_pkg = {'libnss3': [(_claim, 'athena-team-b')]}
        _sess._mirror_pull_write_build_records('primary', _per_pkg)
        _rec = _utils.read_build_record(_buildlog, 'libnss3')
        assert _rec is not None
        assert _rec['phase'] == 'done'
        assert _rec['status'] == 'PASS'
        assert _rec['built_version'] == '2:3.87.1-1+deb12u1'
        assert _rec['outputs'] == ['libnss3_2:3.87.1-1+deb12u1_amd64.deb']
        assert _rec['output_hashes'] == {
            'libnss3_2:3.87.1-1+deb12u1_amd64.deb': 'a' * 64,
        }
        assert _rec['republished_from'] == {}
        assert _rec['pulled_from'] == {
            'mirror_name':   'primary',
            'owner_builder': 'athena-team-b',
        }



def test_mirror_pull_write_build_records_tunneled_pkg_records_republished_from():
    """A tunneled claim (republished_from set on the wire) pulled from
    a mirror yields a local build.json with phase=tunneled +
    republished_from copied verbatim per filename.  pulled_from is ALSO
    set {mirror_name, owner_builder}: the tunnel was ADOPTED from the
    mirror, so generate_pending_claims must skip it (the peer never
    re-publishes a tunnel it pulled).  A self-tunnel carries no
    pulled_from and stays claimable."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _utils
    from build import BuildSession

    with tempfile.TemporaryDirectory() as _td:
        _log = os.path.join(_td, 'log')
        _buildlog = os.path.join(_log, 'build')
        os.makedirs(_buildlog)
        class _Cfg:
            dir_log = _log
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _claim = {
            'package':          'vlc',
            'intended_version': '3.0-1',
            'built_version':    '3.0-1',
            'filename':         'vlc_3.0-1_amd64.deb',
            'sha256':           'b' * 64,
            'snapshot':         'S',
            'builder':          'athena-team-c',
            'claim_state':      'published',
            'republished_from': {
                'url':             'http://deb.debian.org/.../vlc_3.0-1_amd64.deb',
                'upstream_sha256': 'b' * 64,
            },
        }
        _per_pkg = {'vlc': [(_claim, 'athena-team-c')]}
        _sess._mirror_pull_write_build_records('primary', _per_pkg)
        _rec = _utils.read_build_record(_buildlog, 'vlc')
        assert _rec is not None
        assert _rec['phase'] == 'tunneled'
        assert _rec['status'] == 'TUNNELED'
        # republished_from is per-file, keyed by filename
        assert _rec['republished_from'] == {
            'vlc_3.0-1_amd64.deb': {
                'url':             'http://deb.debian.org/.../vlc_3.0-1_amd64.deb',
                'upstream_sha256': 'b' * 64,
            },
        }
        # pulled_from IS set for an adopted tunnel — provenance of the
        # adoption (mirror + republisher) so the peer never re-claims it.
        assert _rec['pulled_from'] == {
            'mirror_name':   'primary',
            'owner_builder': 'athena-team-c',
        }



def test_mirror_pull_write_build_records_aggregates_multi_output_pkg():
    """Multiple .deb claims from the same source (e.g. libnss3 +
    libnss3-dev) collapse into ONE build.json record with both
    outputs listed."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _utils
    from build import BuildSession

    with tempfile.TemporaryDirectory() as _td:
        _log = os.path.join(_td, 'log')
        _buildlog = os.path.join(_log, 'build')
        os.makedirs(_buildlog)
        class _Cfg:
            dir_log = _log
        _sess = BuildSession.__new__(BuildSession)
        _sess.config = _Cfg()
        _c1 = {
            'package': 'nss', 'intended_version': '2:3.87-1',
            'built_version': '2:3.87-1',
            'filename': 'libnss3_2:3.87-1_amd64.deb',
            'sha256':   'a' * 64, 'snapshot': 'S',
            'builder':  'athena-team-b', 'claim_state': 'published',
        }
        _c2 = {
            'package': 'nss', 'intended_version': '2:3.87-1',
            'built_version': '2:3.87-1',
            'filename': 'libnss3-dev_2:3.87-1_amd64.deb',
            'sha256':   'd' * 64, 'snapshot': 'S',
            'builder':  'athena-team-b', 'claim_state': 'published',
        }
        _per_pkg = {'nss': [(_c1, 'athena-team-b'), (_c2, 'athena-team-b')]}
        _sess._mirror_pull_write_build_records('primary', _per_pkg)
        _rec = _utils.read_build_record(_buildlog, 'nss')
        assert _rec is not None
        assert sorted(_rec['outputs']) == [
            'libnss3-dev_2:3.87-1_amd64.deb',
            'libnss3_2:3.87-1_amd64.deb',
        ]
        assert _rec['output_count'] == 2



def test_mirror_pull_write_build_records_empty_input_is_no_op():
    """No claims to write → no error, no side-effect."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    class _Cfg:
        dir_log = '/nonexistent'  # never accessed when input is empty
    _sess = BuildSession.__new__(BuildSession)
    _sess.config = _Cfg()
    _sess._mirror_pull_write_build_records('primary', {})  # no raise



def test_revoke_builder_adds_to_revoked_preserving_head():
    """revoke_builder adds the id to the coord-head's revoked_builders and
    re-signs, preserving every other field; idempotent when already revoked;
    the decommission command refuses to revoke the LOCAL builder."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.publish as _publish
    from unittest.mock import patch

    _cfg = type('C', (), {'dir_coord': '/c',
                          'dir_coord_fetched': '/c/fetched'})()
    _head_dict = {
        'inrelease_sha256': 'a' * 64, 'snapshot': {'current': 'S'},
        'last_seqs': {'alice': 3}, 'neighbours': [],
        'config_sha256': 'c' * 64, 'closure_ledger_sha256': 'd' * 64,
    }
    _written: dict = {}

    def _write(_coord_dir, _head, _home):
        _written['head'] = _head
        return True

    def _run(_read_val):
        _written.clear()
        with patch('coord.publish._reconcile.publish_halt_reason',
                   return_value=None), \
             patch('coord.publish._transport.pull_remote_coord',
                   return_value=(True, '')), \
             patch('coord.publish._head.read_coord_head',
                   return_value=_read_val), \
             patch('coord.publish._head.write_coord_head', _write), \
             patch('coord.publish._transport.push_coord_head',
                   return_value=(True, '')):
            return _publish.revoke_builder(
                builder_id_to_revoke='bob', config=_cfg,
                remote_coord_spec='/c/remote', signing_homedir='/home',
                ssh_host=None)

    _ok, _msg = _run(dict(_head_dict))
    assert _ok, _msg
    assert 'bob' in _written['head']['revoked_builders']
    assert _written['head']['last_seqs'] == {'alice': 3}      # preserved
    assert _written['head']['snapshot'] == {'current': 'S'}    # preserved
    # content pins preserved — a revoke must not invalidate the canonical
    # config / closure ledger and force peers to fall back.
    assert _written['head']['config_sha256'] == 'c' * 64
    assert _written['head']['closure_ledger_sha256'] == 'd' * 64

    # already revoked → no-op True, no re-sign
    _already = dict(_head_dict, revoked_builders={'bob': 'T'})
    _ok2, _msg2 = _run(_already)
    assert _ok2 and 'already revoked' in _msg2
    assert 'head' not in _written

    # command refuses to revoke the LOCAL builder
    from build import BuildSession
    _s = BuildSession.__new__(BuildSession)
    _s.config = type('C', (), {})()
    with patch.object(BuildSession, '_coord_builder_id', return_value='me'):
        assert _s.cmd_mirror_builders_decommission('me') is False



def test_cmd_mirror_pull_no_mirrors_is_friendly():
    """`mirror pull` with no mirrors configured surfaces a friendly warning
    and exits without touching the network."""
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
            dir_coord_identity = os.path.join(_tmp, 'identity')
            dir_coord = os.path.join(_tmp, 'coord')
            build_codename = 'thor'
        os.makedirs(_Cfg.dir_coord_identity)
        os.makedirs(_Cfg.dir_coord)
        # Plant a builder id + pubkey so _coord_self_keys passes
        with open(os.path.join(_Cfg.dir_coord, 'BUILDER_ID'), 'w') as _fh:
            _fh.write('alice\n')
        with open(os.path.join(_Cfg.dir_coord_identity, 'alice.pem'), 'w') as _fh:
            _fh.write('')
        with open(os.path.join(_Cfg.dir_coord_identity, 'alice.pub'), 'w') as _fh:
            _fh.write('')
        _sess.config = _Cfg()
        _lines = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _ok = _sess.cmd_mirror_pull()
        finally:
            build.console.print = _orig
        assert _ok is False
        _joined = '\n'.join(_lines)
        assert 'no mirrors configured' in _joined, _joined



def test_mirror_audit_disk_vs_claims_flags_missing_and_orphan():
    """Phase 8 integrity sweep: cross-checks every non-retracted
    claim's filename against the actual on-disk pool listing.  For a
    file:// mirror, the helper walks the pool dir directly (no ssh).
    Three claims: A is on disk (ok), B is missing on disk (CRITICAL),
    C has no claim (orphan, WARNING).  D is retracted → not counted."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _td:
        _pool = os.path.join(_td, 'pool')
        os.makedirs(os.path.join(_pool, 'main'))
        # On-disk: A.deb (claimed + present), C.deb (orphan)
        for _f in ('a_1.0_amd64.deb', 'c_1.0_amd64.deb'):
            with open(os.path.join(_pool, 'main', _f), 'wb') as _fh:
                _fh.write(b'')
        _sess = BuildSession.__new__(BuildSession)
        class _Cfg:
            dir_config = _td
            dir_cache  = _td
            dir_coord  = _td
        _sess.config = _Cfg()
        _mirror_state = {
            'url': f'file://{_pool}',
            'ssh_key': '',
        }
        _by_builder = {
            'athena-test': [
                {'filename': 'a_1.0_amd64.deb',
                 'claim_state': 'published'},
                {'filename': 'b_1.0_amd64.deb',  # missing on disk
                 'claim_state': 'published'},
                {'filename': 'd_old.deb',
                 'claim_state': 'retracted', 'retracts_seq': 0},
            ],
        }
        _findings = _sess._mirror_audit_disk_vs_claims(
            'm', _mirror_state, _by_builder)
        _kinds = {(_s, _k) for _s, _k, _ in _findings}
        # B claimed but absent → CRITICAL missing_on_disk
        assert ('CRITICAL', 'missing_on_disk') in _kinds, _findings
        _msgs_missing = [_m for _s, _k, _m in _findings
                         if _k == 'missing_on_disk']
        assert any("'b_1.0_amd64.deb'" in _m for _m in _msgs_missing), _msgs_missing
        # C present but unclaimed → WARNING orphan_on_disk
        assert ('WARNING', 'orphan_on_disk') in _kinds, _findings
        _msgs_orphan = [_m for _s, _k, _m in _findings
                        if _k == 'orphan_on_disk']
        assert any("'c_1.0_amd64.deb'" in _m for _m in _msgs_orphan), _msgs_orphan
        # D retracted → must NOT count toward missing
        assert not any("'d_old.deb'" in _m for _, _, _m in _findings)
        # A both claimed and present → no finding mentions it
        assert not any("'a_1.0_amd64.deb'" in _m for _, _, _m in _findings)



def test_cmd_mirror_query_requires_pkg_arg():
    """Missing <pkg> arg → Usage hint, returns False."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    _lines = []
    _orig = build.console.print
    build.console.print = lambda *a, **k: _lines.append(
        ' '.join(str(x) for x in a))
    try:
        _ok = _sess.cmd_mirror_query()
    finally:
        build.console.print = _orig
    assert _ok is False
    assert 'Usage: mirror query' in '\n'.join(_lines)



def test_cmd_mirror_audit_no_mirrors_reports_warning():
    """`mirror audit` with no mirrors configured surfaces a friendly warning
    and exits without touching the network."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _sess = BuildSession.__new__(BuildSession)
        class _Cfg:
            dir_config = _cfg_dir
        _sess.config = _Cfg()
        _lines = []
        _orig = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _ok = _sess.cmd_mirror_audit()
        finally:
            build.console.print = _orig
        assert _ok is True
        assert 'no mirrors configured' in '\n'.join(_lines)



def test_scan_stale_files_buckets_foreign_keeps_native_sibling():
    """Phase-3 repo gate: a foreign cross-toolchain of a SELECTED source
    lands in the new `_foreign` bucket (the production-sibling branch was
    keeping it); the native cross sibling is KEPT."""
    import sys
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    from build import BuildSession
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
        _md = cfg.dir_repo_main
        os.makedirs(_md, exist_ok=True)
        _foreign = 'binutils-aarch64-linux-gnu_2.40-2_amd64.deb'
        _native = 'binutils-x86-64-linux-gnu_2.40-2_amd64.deb'
        for _f in (_foreign, _native):
            open(os.path.join(_md, _f), 'w').close()

        class _Tree:
            def __init__(self):
                # binutils source IS selected; predicted target is the
                # plain `binutils` binary.  Both crosses are siblings.
                self.selected_srcs = {'binutils': object()}
                self.src_pkg_files = {
                    'binutils': ['binutils_2.40-2_amd64.deb']}

        _sess = BuildSession.__new__(BuildSession)
        _sess.config = cfg
        _sess.dep_tree = _Tree()
        _sess.udeb_dep_tree = _Tree()
        _sess._superseded_binary_names = lambda: set()

        def _fake_iter(config, subdir, refresh=False):
            if subdir == 'main':
                for _fn in (_foreign, _native):
                    yield (_fn, {
                        'Package': _fn.split('_', 1)[0], 'Source': 'binutils',
                        'Version': '2.40-2',
                        'Filename': f'pool/{_fn}', 'Size': '100'})
            return

        with patch.object(repo_audit, 'iter_packages_all_versions',
                          side_effect=_fake_iter):
            (_orphan, _drift, _foreign_b, _malformed,
             _total) = _sess._scan_stale_files()

        _foreign_fns = [_fn for _sub, _fn, *_ in _foreign_b]
        _orphan_fns = [_fn for _sub, _fn, *_ in _orphan]
        _drift_fns = [_fn for _sub, _fn, *_ in _drift]
        assert _foreign in _foreign_fns, f"foreign not bucketed: {_foreign_fns}"
        assert _native not in _foreign_fns, f"native bucketed: {_foreign_fns}"
        assert _native not in _orphan_fns and _native not in _drift_fns, (
            f"native sibling must be KEPT: orphan={_orphan_fns} "
            f"drift={_drift_fns}")



def test_init_remote_builds_image_and_gates_localmirror():
    """container remote init builds the image ON the remote when absent, and
    stages the build mirror PER-REMOTE (each remote's own LocalMirror flag),
    gated on readiness.  No container-wide _localmirror_active for remote builds
    (per-remote applied at compose time)."""
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as _f:
        _b = _f.read()
    _m = _b[_b.index('def cmd_init_remote_container'):]
    _m = _m[:_m.index('\n    def ', 1)]
    assert 'build_remote_image' in _m, (
        "init must BUILD the image on the remote when neither side has it")
    assert '_stage_remote_localmirror_bars' in _m, "init populates the mirror"
    assert "if _r.get('local_mirror'):" in _m, (
        "localmirror staging is gated PER-REMOTE on that remote's flag")
    assert '_localmirror_active = False' in _m, (
        "no container-wide localmirror for remote builds (per-remote at compose)")
    assert 'build_container_ready = _all_ready' in _m, (
        "init is gated on image + mirror readiness")



def test_cli_command_gate_refuses_ungated_command():
    """Audit #54: command_gate refuses a non-allowlisted command with the
    'configure first' ERROR (returns False), while help still runs and
    quit short-circuits."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import cli as _climod
    _errs: list = []
    _ran: list = []
    _c = object.__new__(_climod.Cli)
    _c._cmds = {'foo': (lambda *a: _ran.append('foo'), 'test')}
    _c.command_gate = lambda _cmd: _cmd == 'configure'
    _c.ERROR = lambda _m: _errs.append(_m)
    _c._print_help = lambda: _ran.append('help')
    assert _c._dispatch_one('foo') is False        # gated → refused
    assert any('configure' in _e for _e in _errs)
    assert 'foo' not in _ran
    assert _c._dispatch_one('help') is True         # help not gated
    assert 'help' in _ran
    assert _c._dispatch_one('quit') is False         # quit short-circuits



def test_cmd_mirror_reclaim_forwards_no_iso_to_publish():
    """Audit #65: reclaim publishes with --no-iso so the ISO release-media
    gate can't block a repo/claims reclaim when current-snapshot install media
    is absent (the earlier test asserted the buggy args that omitted it)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import re as _re
    from commands import cmd_mirror
    _src = inspect.getsource(cmd_mirror)
    assert _re.search(
        r"cmd_mirror_publish\([^)]*'--no-iso'[^)]*reclaim_intents",
        _src, _re.DOTALL), "reclaim must forward --no-iso to cmd_mirror_publish"



def test_render_grype_summary_tolerates_null_artifact_and_missing_fix():
    """Audit #79: the extracted grype summary renderer handles a match with a
    null artifact and one with no fix versions without crashing, and reports
    the finding + severity counts."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from commands import cmd_supply_chain as _sc
    _printed: list = []
    _orig = _sc.console.print
    _sc.console.print = lambda *a, **k: _printed.append(a[0] if a else '')
    try:
        _self = object.__new__(_sc.SupplyChainCommandsMixin)
        _doc = {'matches': [
            {'vulnerability': {'id': 'CVE-1', 'severity': 'Critical'},
             'artifact': None},                           # null artifact
            {'vulnerability': {'id': 'CVE-2', 'severity': 'High', 'fix': {}},
             'artifact': {'name': 'foo', 'version': '1.0'}},   # no fix versions
        ]}
        _self._render_grype_summary(_doc, '/tmp/report.json')
    finally:
        _sc.console.print = _orig
    _joined = '\n'.join(str(_x) for _x in _printed)
    assert '2 finding(s)' in _joined, _joined
    assert 'Critical' in _joined and 'High' in _joined
    assert 'CVE-1' in _joined and 'CVE-2' in _joined
    assert 'fix: —' in _joined                            # missing fix rendered




def test_cmd_snapshot_workload_early_branches():
    """Audit #74: _cmd_snapshot_workload early branches - dep_check not ready,
    malformed target, and current==target short-circuit - print the right
    message and return without computing a workload."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import types as _t
    from commands import cmd_snapshot as _cs
    _printed = []
    _orig = _cs.console.print
    _cs.console.print = lambda *a, **k: _printed.append(str(a[0]) if a else '')
    try:
        _self = _t.SimpleNamespace(
            flags=_t.SimpleNamespace(dep_check_ready=False))
        _cs.SnapshotCommandsMixin._cmd_snapshot_workload(_self)
        assert any('cache build' in _m for _m in _printed), _printed
        _printed.clear()
        _self = _t.SimpleNamespace(
            flags=_t.SimpleNamespace(dep_check_ready=True),
            _snapshot_current=lambda: '20260101T000000Z')
        _cs.SnapshotCommandsMixin._cmd_snapshot_workload(_self, 'not-a-ts')
        assert any('YYYYMMDD' in _m or 'not a' in _m for _m in _printed), _printed
        _printed.clear()
        _self = _t.SimpleNamespace(
            flags=_t.SimpleNamespace(dep_check_ready=True),
            _snapshot_current=lambda: '20260101T000000Z')
        _cs.SnapshotCommandsMixin._cmd_snapshot_workload(
            _self, '20260101T000000Z')
        assert any('nothing would change' in _m for _m in _printed), _printed
    finally:
        _cs.console.print = _orig



def test_cache_build_mode_picks_debian_highest_version():
    """Audit #63: build-mode dep parse picks the Debian-HIGHER version across
    multiple cached versions of one package (1.10-1 > 1.9-1, not the string
    max which would wrongly pick 1.9-1)."""
    import types as _t
    from debian.debian_support import Version
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from commands import cmd_cache as _cc
    with tempfile.TemporaryDirectory() as _d:
        _bpl = os.path.join(_d, 'build_pkg.list')
        with open(_bpl, 'w') as _fh:
            _fh.write('foo\n')
        _pkg19 = {'Package': 'foo', 'Version': '1.9-1'}
        _pkg110 = {'Package': 'foo', 'Version': '1.10-1'}
        _cache = _t.SimpleNamespace(package_hashtable={
            'foo': {Version('1.9-1'): [_pkg19], Version('1.10-1'): [_pkg110]}})
        _dt = _t.SimpleNamespace(selected_pkgs={}, selected_srcs={},
                                 parse_sources=lambda: True)
        _self = _t.SimpleNamespace(
            config=_t.SimpleNamespace(build_pkg_list_path=_bpl),
            cache=_cache, dep_tree=_dt)
        assert _cc.CacheCommandsMixin._cache_parse_build_mode(_self) is True
        assert _self.dep_tree.selected_pkgs['foo'] is _pkg110



def test_do_tunnel_records_provenance_and_outputs():
    """Audit #81: _do_tunnel transposes each upstream .deb, records
    republished_from provenance keyed by the FINAL on-disk name, and writes the
    build-record outputs as the final on-disk filenames (behavioural coverage
    needs dpkg-deb + a pool; this pins the provenance/record contract)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    from commands import cmd_tunnel
    _src = inspect.getsource(cmd_tunnel.TunnelCommandsMixin._do_tunnel)
    assert 'transpose_deb' in _src                      # per-file transpose
    assert '_republished_from[_final_fn]' in _src       # keyed by FINAL name
    assert "'upstream_sha256'" in _src                  # upstream provenance
    assert 'outputs=_outputs_sorted' in _src            # record = on-disk names

TESTS = [
    test_init_remote_builds_image_and_gates_localmirror,
    test_startup_banner_runs_config_check,
    test_container_init_remote_ensures_image,
    test_container_two_level_command_surface_wired,
    test_remotebuild_command_wired,
    test_copy_ssh_key_copies_with_0600_and_delete_removes_key,
    test_container_remote_add_is_guided_with_probes,
    test_remotebuild_fanout_respects_slot_caps_and_requeues,
    test_remote_container_init_wired,
    test_cmd_run_setters_survive_local_conf_write_failure,
    test_print_state_shows_mode_header,
    test_cmd_auto_run_dispatch_routes_build_mode,
    test_cmd_auto_run_build_refuses_in_dist_mode,
    test_sta34_autorun_build_calls_source_build_bare_not_invalid_token,
    test_sta36_mirror_add_confirmation_declines_on_no,
    test_tunnel_transposes_and_needs_no_ledger,
    test_parse_source_build_args_recognises_indl_subset,
    test_cmd_source_build_indl_subset_rejected_in_dist_mode,
    test_source_audit_naturally_scopes_to_indl_in_build_mode,
    test_chroot_iso_builds_refuse_in_build_mode,
    test_iso_builds_gate_on_container_up_front,
    test_surface_builds_gate_on_dep_check_ready,
    test_refuse_in_build_mode_is_a_no_op_in_distribution,
    test_cache_parse_build_mode_resolves_named_pkgs_only,
    test_cache_parse_build_mode_warns_on_missing_pkg,
    test_cache_parse_build_mode_empty_indl_returns_false,
    test_sta25_cleanup_guards_live_published_claims,
    test_cleanup_publish_before_prune_gate_not_informational_cons14,
    test_sta37_build_chroot_gates_on_incomplete_set,
    test_ux05a_prompt_informational_kwarg_accepted,
    test_ux05a_auto_yes_short_circuits_informational_yesno,
    test_ux05a_auto_yes_does_not_skip_password_or_options,
    test_ux05b_atena_sudo_password_env_var_picked_up,
    test_ux05d_cli_print_emits_ansi_when_tty,
    test_ux08_signal_loss_fixes,
    test_ux08_cache_info_picks_highest_version,
    test_ux08_spinner_done_idempotent,
    test_ux05e_one_shot_dispatch_runs_each_in_order_and_exits,
    test_one_shot_queue_consumed_not_replayed_on_reentry,
    test_ux05e_one_shot_exit_code_nonzero_when_a_command_fails,
    test_ux05g_cmd_methods_reset_flags_on_entry,
    test_buildsession_constructible_with_stub_tui,
    test_group_dispatchers_forward_to_underlying_cmd_methods,
    test_ux09f_stray_token_dispatch_prints_usage,
    test_cache_purge_deletes_files_and_resets_flags,
    test_cache_purge_cancelled_keeps_files_and_flags,
    test_cache_purge_empty_dir_is_noop,
    test_cmd_iso_build_requires_subaction,
    test_repo_index_dispatch_falls_through_after_hint_removal,
    test_cmd_iso_build_live_forwards_to_cmd_build_iso_live,
    test_cmd_iso_build_installer_forwards_to_cmd_build_iso_installer,
    test_cmd_iso_build_unknown_subaction_calls_neither_handler,
    test_cmd_build_cache_skips_when_already_ready_no_force,
    test_cmd_build_cache_runs_when_force_passed_even_if_ready,
    test_cmd_parse_dependency_skips_when_already_ready_no_force,
    test_cmd_parse_dependency_runs_when_force_passed_even_if_ready,
    test_wipe_dir_contents_returns_true_on_missing_dir,
    test_wipe_dir_contents_returns_true_on_empty_dir,
    test_wipe_dir_contents_actually_removes_files_and_subdirs,
    test_cmd_clean_source_resets_download_ready,
    test_cmd_clean_image_resets_iso_flags,
    test_cmd_clean_repo_resets_source_build_ready_and_drops_counts,
    test_cmd_container_purge_resets_flag_and_drops_session_ref,
    test_cmd_container_purge_removes_athena_containers_and_images,
    test_cmd_container_purge_handles_docker_connect_failure_gracefully,
    test_cmd_clean_dispatcher_unknown_action_calls_no_handler,
    test_cmd_build_iso_installer_bails_on_unmet_prereqs,
    test_cmd_chroot_build_no_subaction_defaults_to_live,
    test_cmd_chroot_build_live_explicit_forwards_to_live,
    test_cmd_chroot_build_installer_forwards_to_installer,
    test_cmd_chroot_build_passthrough_args_to_live,
    test_cmd_build_chroot_installer_bails_on_unmet_prereqs,
    test_superseded_binary_names_excludes_selected,
    test_iso_installer_build_iso_installer_passes_pool_whitelist,
    test_buildconfig_exposes_poollist_path,
    test_read_pkg_list_handles_pool_list_format,
    test_buildconfig_chroot_paths_under_shared_buildroot_parent,
    test_build_flags_carries_chroot_installer_ready_default_false,
    test_cmd_audit_nmu_residue_absorbed_into_cmd_audit,
    test_cmd_strip_repo_registered_in_repo_dispatcher,
    test_cmd_package_cleanup_registered_in_repo_dispatcher,
    test_cmd_package_cleanup_reindexes_after_deletion,
    test_cmd_package_cleanup_dry_run_default_force_flag_required,
    test_cmd_package_cleanup_keeps_expected_files_drops_orphan_source,
    test_scan_stale_files_covers_main_udeb_and_recovers_malformed,
    test_scan_stale_files_prunes_superseded_unselected_sibling,
    test_scan_orphaned_sidecars_detects_and_cleanup_sweeps,
    test_cmd_package_cleanup_sweeps_orphan_sidecars_source_pin,
    test_cmd_package_cleanup_deletes_via_subdir_label_and_drops_sidecar,
    test_print_help_lists_every_registered_category,
    test_print_dispatch_unknown_category_points_to_help,
    test_print_dispatch_empty_category_shows_help,
    test_print_help_groups_categories_into_sections,
    test_print_dispatch_passes_extras_to_parametrized_handler,
    test_fmt_dep_unconstrained,
    test_fmt_dep_with_constraint,
    test_fmt_dep_group_alternates_with_pipe,
    test_print_no_handler_crashes_on_uninitialized_session,
    test_format_duration_seconds_only,
    test_format_duration_minutes_seconds,
    test_format_duration_hours_minutes_seconds,
    test_autorun_summary_success_includes_counts_and_iso_path,
    test_autorun_summary_aborted_marks_stage_and_partial_state,
    test_print_state_renders_three_sections_with_all_flags,
    test_print_state_renders_unticked_when_flags_unset,
    test_print_summary_without_timing_renders_state_snapshot,
    test_print_summary_dispatch_through_handler,
    test_canonical_names_filters_virtual_aliases_from_cohort,
    test_cmd_init_container_gated_on_cache_ready,
    test_buildconfig_argparse_exposes_live_and_installer_list_flags,
    test_read_pkg_list_filters_comments_blanks_and_already_selected,
    test_read_pkg_list_missing_file_returns_empty,
    test_pass_iii_dedups_to_canonical_names_for_pkg_group_pkg_names,
    test_print_udebs_handles_no_udeb_tree_gracefully,
    test_print_udebs_lists_udeb_closure_when_tree_populated,
    test_print_extras_lists_recommended_packages,
    test_print_extras_handles_empty_extras_set,
    test_source_build_args_no_args_defaults_to_pkg_subset,
    test_source_build_args_pkg_subset_explicit,
    test_source_build_args_live_subset_explicit,
    test_source_build_args_installer_subset_recognised,
    test_source_build_args_recommended_subset_recognised,
    test_source_build_args_force_flag_anywhere,
    test_source_build_args_subsets_mutually_exclusive,
    test_refresh_patches_invalidates_record_when_patch_newer,
    test_refresh_patches_keeps_result_when_patch_older_than_result,
    test_refresh_patches_skips_invalidation_for_header_only_edit,
    test_refresh_patches_invalidates_when_patches_removed,
    test_autorun_installer_runs_source_build_then_source_build_installer,
    test_autorun_live_chains_iso_build_after_chroot,
    test_autorun_installer_chains_iso_build_after_chroot,
    test_autorun_disk_builds_its_own_disk_chroot,
    test_buildflags_carry_iso_ready_state,
    test_autorun_dispatcher_routes_bare_to_live_and_explicit_to_each,
    test_autorun_live_runs_source_build_then_source_build_live,
    test_source_build_args_subset_and_named_pkgs_mutually_exclusive,
    test_source_build_args_named_pkgs_resolve_subset_to_empty,
    test_source_build_args_all_subset_parses,
    test_source_build_args_bracket_token_extracts_profiles,
    test_source_build_args_empty_bracket_means_no_profiles,
    test_source_build_args_multiple_bracket_tokens_rejected,
    test_source_build_args_bracket_position_does_not_matter,
    test_signing_key_verified_flag_default_false,
    test_ensure_signing_key_verified_true_when_key_exists,
    test_ensure_signing_key_verified_false_on_user_decline,
    test_ensure_signing_key_verified_generates_then_verifies_on_accept,
    test_ensure_signing_key_verified_false_when_generate_fails,
    test_cli_print_writes_to_stdout,
    test_cli_severity_methods_write_to_stderr_with_tags,
    test_cli_registers_itself_as_tui_singleton,
    test_cli_register_command_dispatches_via_wait,
    test_cli_unknown_command_does_not_crash_repl,
    test_cli_repl_enables_readline_history,
    test_cli_handler_exception_does_not_kill_repl,
    test_cli_help_lists_registered_commands,
    test_cli_eof_exits_repl_cleanly,
    test_cli_widget_methods_return_stable_ids_for_unknown_types,
    test_cli_progress_bar_prints_start_and_finish_markers,
    test_cli_spinner_does_not_print_start_marker,
    test_cli_console_mark_and_trim_to_are_no_ops,
    test_cli_console_facade_exercise_full_surface,
    test_cli_prompt_reads_stdin,
    test_cli_keymode_prompt_reads_and_discards,
    test_cli_logging_handlers_bound_to_cli_after_init,
    test_ux09g_too_small_q_key_posts_shutdown,
    test_status_lines_compact_snapshot,
    test_cmd_cache_info_prints_identity_and_relations,
    test_buildconfig_creates_fork_source_dir,
    test_cmd_audit_registered_under_repo_dispatcher,
    test_cmd_audit_runs_three_checks,
    test_print_wrapped_names_keeps_lines_under_wrap_width,
    test_sta50_chroot_build_gates_on_repo_auto_index,
    test_sta49_clean_all_wipes_disk_chroot_and_purge_nulls_udeb_tree,
    test_preflight_repo_audit_blocks_on_stale_artifacts,
    test_mirror_publish_reindexes_stale_local_index,
    test_mirror_audit_disk_vs_claims_folds_superseded_claims,
    test_cmd_source_fork_disable_writes_marker_and_invalidates_state,
    test_cmd_source_fork_enable_removes_marker,
    test_cmd_source_audit_classifies_rebuilds_by_subset,
    test_cmd_source_audit_verbose_lists_tunneled_and_failed_names,
    test_destructive_helpers_warn_in_docstring,
    test_cmd_source_repair_dispatch_and_method_present,
    test_cmd_source_repair_leaves_fail_result_untouched,
    test_cmd_source_repair_skips_when_binaries_missing,
    test_cmd_source_repair_clears_stale_pass_when_binaries_not_valid,
    test_cmd_source_repair_leaves_consistent_pass_alone,
    test_cmd_source_repair_leaves_tunneled_marker_alone,
    test_iso_installer_call_site_passes_audit_flag,
    test_obs02_build_history_ledger_append_and_read,
    test_virtual_buildlog_writes_predicted_and_filtered,
    test_comp03_phase4_build_one_source_skip_src_returns_skipped,
    test_comp03_phase4_build_one_source_tunneled_calls_do_tunnel,
    test_build_one_source_tunneled_branch_uses_pristine_for_check_build,
    test_tunnel_filenames_full_set_arch_profile_filtered,
    test_cmd_virtual_dispatch_routes_build_and_run,
    test_cmd_virtual_build_refuses_without_cache,
    test_cmd_virtual_build_refuses_without_dep_tree,
    test_tunnel_filenames_for_source_uses_upstream_not_stripped,
    test_tunnel_filenames_falls_back_when_binary_not_in_cache,
    test_local_cleanup_keeps_highest_prunes_superseded_and_flags_orphan,
    test_needs_bump_build_predicts_transpose_filename,
    test_needs_bump_build_shim_signed_uses_binary_own_version,
    test_audit_state_reclassifies_security_respin_as_needs_bump,
    test_preflight_stamp_invariant_roundtrips_and_flags_bad_version,
    test_snapshot_base_subcommand_fully_removed,
    test_snapshot_select_interactive_sets_chosen_current,
    test_snapshot_select_syncs_build_conf_at_command_time,
    test_snapshot_select_warns_only_on_unpublished_local_builds,
    test_snapshot_select_current_is_forward_only,
    test_snapshot_select_force_accepts_backtrack,
    test_snapshot_select_force_cancels_on_empty_or_no,
    test_snapshot_select_force_rejects_malformed_timestamp,
    test_ensure_snapshot_pins_prompts_and_writes_when_unset,
    test_ensure_snapshot_pins_aborts_when_no_selection,
    test_do_update_build_sets_source_build_ready_on_nothing_to_build,
    test_workload_current_to_target_diffs_against_target_snapshot,
    test_workload_detects_debNuN_source_change_ignores_unchanged,
    test_workload_since_snapshot_diffs_published_to_current,
    test_workload_excludes_forks_from_snapshot_diff,
    test_index_minimal_stages_nested_subset,
    test_ux04_buildflags_autosave_round_trip,
    test_ux04_buildflags_in_memory_only_reset_on_load,
    test_sbom_command_registered,
    test_cve_command_registered,
    test_build_system_sh_grype_is_non_blocking,
    test_source_state_interrupted_when_record_is_non_terminal,
    test_source_state_tunneled_record_with_missing_binaries_routes_to_stale_pass,
    test_source_state_tunneled_record_with_pristine_binary_returns_tunneled,
    test_shorten_origin_compacts_long_pool_url,
    test_source_state_uses_origin_component_for_disk_lookup,
    test_detect_build_audit_divergence_flags_missing_binary_with_no_fail_record,
    test_detect_build_audit_divergence_silent_when_record_says_fail,
    test_detect_build_audit_divergence_silent_on_clean_state,
    test_detect_build_audit_divergence_excludes_skip_src,
    test_detect_build_audit_divergence_flags_stale_pass_tunneled_record,
    test_cmd_source_repair_clears_interrupted_record,
    test_print_build_times_aggregates_elapsed_across_records,
    test_print_build_times_skips_tampered_records,
    test_release_iso_descriptors_finds_and_reports_missing,
    test_mirror_publish_release_gate_and_push_wired,
    test_mirror_recompute_base_returns_empty_when_no_claims,
    test_mirror_pull_write_build_records_built_pkg_records_pulled_from,
    test_mirror_pull_write_build_records_tunneled_pkg_records_republished_from,
    test_mirror_pull_write_build_records_aggregates_multi_output_pkg,
    test_mirror_pull_write_build_records_empty_input_is_no_op,
    test_revoke_builder_adds_to_revoked_preserving_head,
    test_cmd_mirror_pull_no_mirrors_is_friendly,
    test_mirror_audit_disk_vs_claims_flags_missing_and_orphan,
    test_cmd_mirror_query_requires_pkg_arg,
    test_cmd_mirror_audit_no_mirrors_reports_warning,
    test_cmd_repo_dispatcher_drops_index_and_tunnel_hints,
    test_build_py_threads_container_into_iso_callsites,
    test_build_system_sh_checks_disk_image_tools,
    test_cmd_iso_dispatcher_routes_disk_action,
    test_cmd_build_iso_disk_gates_on_chroot_disk_ready_and_reads_size,
    test_progress_bar_show_rate_false_omits_rate_column,
    test_progress_bar_label_width_pins_column_so_label_updates_dont_shift,
    test_tier3_coord_webapi_source_pins,
    test_build_patch_list_sorts_by_full_filename,
    test_cli_quit_detection_keys_on_first_token,
    test_cache_parse_build_mode_guards_unreadable_list,
    test_cli_command_gate_refuses_ungated_command,
    test_cmd_mirror_reclaim_forwards_no_iso_to_publish,
    test_render_grype_summary_tolerates_null_artifact_and_missing_fix,
    test_cmd_snapshot_workload_early_branches,
    test_cache_build_mode_picks_debian_highest_version,
    test_do_tunnel_records_provenance_and_outputs,
    test_scan_stale_files_buckets_foreign_keeps_native_sibling,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
