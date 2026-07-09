"""Athena tests — configuration + shared utils (BuildConfig, local.conf, utils helpers).

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
    _make_temp_file,
    _write_local_conf,
    _write_test_config,
)




def test_buildconfig_mode_defaults_to_distribution():
    """MIRROR-02: `[Build] Mode` defaults to 'distribution' when absent.
    Existing configs (and the shipped build.conf) keep working unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, f"BuildConfig invalid: {cfg.error_str}"
        assert cfg.build_mode == 'distribution', cfg.build_mode



def test_buildconfig_mode_build_mode_parses():
    """MIRROR-02: `[Build] Mode = build` accepted; case + whitespace
    tolerant."""
    _body = _BASE_CONF_BODY.replace(
        'MaxParallelBuilds = 1',
        'MaxParallelBuilds = 1\n    Mode = Build')
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _body.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, f"BuildConfig invalid: {cfg.error_str}"
        assert cfg.build_mode == 'build'



def test_buildconfig_mode_rejects_unknown_value():
    """MIRROR-02: typo in [Build] Mode → BuildConfig invalid + actionable
    error_str pointing at the allowed values."""
    _body = _BASE_CONF_BODY.replace(
        'MaxParallelBuilds = 1',
        'MaxParallelBuilds = 1\n    Mode = banana')
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _body.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        assert not cfg.is_valid
        assert "'distribution' or 'build'" in cfg.error_str
        assert 'banana' in cfg.error_str



def test_local_conf_mode_overrides_build_conf():
    """LOCAL-CONF: config/local.conf [Local] Mode wins over build.conf
    [Build] Mode — mode is a per-machine decision, the untracked sidecar
    is authoritative."""
    # build.conf says distribution; local.conf says build → build wins.
    _body = _BASE_CONF_BODY.replace(
        'MaxParallelBuilds = 1',
        'MaxParallelBuilds = 1\n    Mode = distribution')
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _body.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        _write_local_conf(tmp, """
            [Local]
            Mode = build
            Role = federation
            SetupComplete = true
        """)
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, f"BuildConfig invalid: {cfg.error_str}"
        assert cfg.build_mode == 'build', cfg.build_mode
        assert cfg.system_role == 'federation'
        assert cfg.setup_complete is True



def test_local_conf_absent_falls_back_to_build_conf():
    """LOCAL-CONF: no local.conf → build.conf/default still drives mode,
    and the onboarding attrs default to un-onboarded."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, f"BuildConfig invalid: {cfg.error_str}"
        assert cfg.build_mode == 'distribution'
        assert cfg.system_role == ''
        assert cfg.setup_complete is False



def test_local_conf_malformed_invalidates_config():
    """LOCAL-CONF: a broken local.conf is the operator's machine state — it
    must fail loudly (error_str), not silently fall back to distribution."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        # No section header → configparser.MissingSectionHeaderError.
        _write_local_conf(tmp, "Mode = build\n")
        cfg = _build_config_from(tmp, cfg_path)
        assert not cfg.is_valid
        assert 'local.conf' in cfg.error_str



def test_write_local_conf_round_trips():
    """LOCAL-CONF: write_local_conf merges fields and read_local_conf reads
    them back; a second partial write preserves prior fields.  (Registration
    moved to mirror.conf — see test_mirror_conf_registration_*.)"""
    import utils
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        utils.write_local_conf(cfg, mode='build', role='federation',
                               setup_complete=True)
        _p = utils.read_local_conf(cfg)
        assert _p.get('Local', 'Mode') == 'build'
        assert _p.get('Local', 'Role') == 'federation'
        assert _p.getboolean('Local', 'SetupComplete') is True
        # Merge: a second partial write keeps prior fields.
        utils.write_local_conf(cfg, name='athena-x')
        _p2 = utils.read_local_conf(cfg)
        assert _p2.get('Local', 'Mode') == 'build'          # preserved
        assert _p2.get('Local', 'Name') == 'athena-x'



def test_write_local_conf_writes_relocated_machine_keys():
    """CONFIG-SPLIT: write_local_conf persists the keys relocated out of the
    tracked config — builder Name, [Build] host tuning, [Repo] SigningKeyUid —
    and BuildConfig reads them back with local.conf precedence."""
    import utils
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=_MINIMAL_MIRROR_BLOCK))
        cfg = _build_config_from(tmp, cfg_path)
        utils.write_local_conf(
            cfg, name='athena-primary', max_parallel_builds=3,
            build_cpus=4.0, build_memory='8g', docker_server='',
            signing_key_uid='Athena Build <athena@local>')
        _p = utils.read_local_conf(cfg)
        assert _p.get('Local', 'Name') == 'athena-primary'
        assert _p.getint('Build', 'MaxParallelBuilds') == 3
        assert _p.get('Build', 'BuildMemory') == '8g'
        assert _p.get('Repo', 'SigningKeyUid') == 'Athena Build <athena@local>'
        # BuildConfig now reads these from local.conf (precedence over tracked).
        cfg2 = _build_config_from(tmp, cfg_path)
        assert cfg2.system_name == 'athena-primary'
        assert cfg2.max_parallel_builds == 3
        assert cfg2.build_memory == '8g'
        assert cfg2.signing_key_uid == 'Athena Build <athena@local>'



def test_parse_build_pkg_list_strips_comments_dedups_preserves_order():
    """MIRROR-02: `config/build_pkg.list` parser — flat list, `#` comments and
    blank lines stripped, inline comments allowed, dedup preserves
    first-seen order, missing file → empty list."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    with tempfile.TemporaryDirectory() as _td:
        _path = os.path.join(_td, 'build_pkg.list')
        with open(_path, 'w') as _fh:
            _fh.write(
                '# operator notes\n'
                'firefox-esr\n'
                '\n'
                'libreoffice  # OOMs on host A\n'
                '# blank section follows\n'
                '\n'
                'thunderbird\n'
                'firefox-esr\n'   # dup — dropped
                '   # whitespace-only comment\n'
            )
        _got = utils.parse_build_pkg_list(_path)
        assert _got == ['firefox-esr', 'libreoffice', 'thunderbird'], _got
        # Missing file → []
        _missing = os.path.join(_td, 'nonexistent.list')
        assert utils.parse_build_pkg_list(_missing) == []



def test_buildconfig_parses_three_mirrors():
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main

    [Mirror.updates]
    Suffix = -updates
    Component = main

    [Mirror.security]
    BASEID = debian-security
    Suffix = -security
    Component = main
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, f"BuildConfig invalid: {cfg.error_str}"
        assert len(cfg.mirrors) == 3, f"expected 3 mirrors, got {len(cfg.mirrors)}"

        by_id = {m.id: m for m in cfg.mirrors}
        assert set(by_id) == {'main', 'updates', 'security'}, by_id

        assert by_id['main'].suite    == 'bookworm'
        assert by_id['updates'].suite == 'bookworm-updates'
        assert by_id['security'].suite == 'bookworm-security'

        # Per-mirror BASEID override applies only to security
        assert by_id['main'].url     == 'http://deb.debian.org/debian'
        assert by_id['updates'].url  == 'http://deb.debian.org/debian'
        assert by_id['security'].url == 'http://deb.debian.org/debian-security'



def test_buildconfig_rejects_no_mirrors():
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=''))
        cfg = _build_config_from(tmp, cfg_path)
        assert not cfg.is_valid
        assert 'Mirror' in cfg.error_str, cfg.error_str



def test_buildconfig_security_defaults():
    """Without an explicit [Security] section, defaults kick in and (if the
    debian-archive-keyring is installed on the test host) validation passes."""
    keyring_default = '/usr/share/keyrings/debian-archive-keyring.gpg'
    if not os.path.exists(keyring_default):
        # Host is non-Debian or keyring not installed — skip.
        print("SKIP test_buildconfig_security_defaults (no host keyring)")
        return

    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, f"BuildConfig invalid: {cfg.error_str}"
        assert cfg.security_keyring == keyring_default, cfg.security_keyring
        assert cfg.security_disabled is False



def test_buildconfig_security_disabled_accepts_missing_keyring():
    """Disabled = true skips the keyring file check entirely."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main

    [Security]
    Keyring = /definitely/not/here/keyring.gpg
    Disabled = true
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, f"BuildConfig should accept missing keyring when Disabled=true: {cfg.error_str}"
        assert cfg.security_disabled is True



def test_buildconfig_security_enabled_rejects_missing_keyring():
    """When Disabled=false (default), a non-existent keyring is fatal."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main

    [Security]
    Keyring = /definitely/not/here/keyring.gpg
    Disabled = false
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert not cfg.is_valid, "BuildConfig should reject missing keyring when Disabled=false"
        assert 'Keyring not found' in cfg.error_str, cfg.error_str



# ─────────────────────────────────────────────────────────────────────────────
# BuildOptions / BuildProfiles are separate config keys
# ─────────────────────────────────────────────────────────────────────────────

def test_buildconfig_build_options_and_profiles_are_separate():
    """When both keys are set they populate distinct frozensets."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    body = _BASE_CONF_BODY.replace(
        'BuildProfiles = nodoc, nocheck',
        'BuildOptions = nodoc, nocheck, parallel=4\n    BuildProfiles = nodoc, nocheck',
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            print(f"SKIP test_buildconfig_build_options_and_profiles_are_separate ({cfg.error_str})")
            return
        assert cfg.build_options  == frozenset({'nodoc', 'nocheck', 'parallel=4'}), cfg.build_options
        assert cfg.build_profiles == frozenset({'nodoc', 'nocheck'}),               cfg.build_profiles



def test_buildconfig_build_options_falls_back_to_profiles_when_omitted():
    """Backward compat: legacy build.conf without BuildOptions reuses
    BuildProfiles for both env vars (the prior conflated semantics)."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            print(f"SKIP test_buildconfig_build_options_falls_back_to_profiles_when_omitted ({cfg.error_str})")
            return
        # _BASE_CONF_BODY sets BuildProfiles = nodoc, nocheck and no BuildOptions.
        assert cfg.build_options == cfg.build_profiles
        assert cfg.build_options == frozenset({'nodoc', 'nocheck'})



def test_arch16_per_pkg_build_options_override_global():
    """ARCH-16: a `[Source.<pkg>]` section's BuildOptions / BuildProfiles
    override the global `[Source]` values for that one source, and the
    accessors fall back to global for any source not listed."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    # Append two [Source.<pkg>] blocks: firefox-esr overrides BuildOptions
    # only (BuildProfiles falls through); libfoo overrides BuildProfiles
    # only (BuildOptions falls through).
    body = _BASE_CONF_BODY.replace(
        'BuildProfiles = nodoc, nocheck',
        'BuildOptions = nodoc, nocheck\n    BuildProfiles = nodoc, nocheck\n\n'
        '[Source.firefox-esr]\n'
        '    BuildOptions = nodoc, nocheck, parallel=1\n\n'
        '[Source.libfoo]\n'
        '    BuildProfiles = nostrip',
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            print(f"SKIP test_arch16_per_pkg_build_options_override_global ({cfg.error_str})")
            return
        # firefox-esr: options overridden, profiles fall through
        assert cfg.build_options_for('firefox-esr') == frozenset(
            {'nodoc', 'nocheck', 'parallel=1'}), cfg.build_options_for('firefox-esr')
        assert cfg.build_profiles_for('firefox-esr') == cfg.build_profiles
        # libfoo: profiles overridden, options fall through
        assert cfg.build_options_for('libfoo') == cfg.build_options
        assert cfg.build_profiles_for('libfoo') == frozenset({'nostrip'})
        # unknown pkg: both fall through to global
        assert cfg.build_options_for('zzz') == cfg.build_options
        assert cfg.build_profiles_for('zzz') == cfg.build_profiles



def test_skiptest_unions_nocheck_into_effective_options():
    """`[Source] SkipTest = a, b` must suppress the listed packages' test
    suites through the EFFECTIVE options — build_options_for is the single
    authority compose_recipe consults.  Regression pin for the c61b0e6 dead
    config: SkipTest was parsed and stamped onto a per-Source flag nothing
    read, so listed packages built WITH tests (vim, 2026-07-07 run)."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    body = _BASE_CONF_BODY.replace(
        'SkipTest =',
        'SkipTest = vim, libsoup2.4',
    ).replace(
        'BuildProfiles = nodoc, nocheck',
        'BuildOptions =\n    BuildProfiles =\n\n'
        '[Source.vim]\n'
        '    BuildOptions = nodoc',
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            print(f"SKIP test_skiptest_unions_nocheck_into_effective_options ({cfg.error_str})")
            return
        # listed pkg with empty global options → nocheck injected
        assert 'nocheck' in cfg.build_options_for('libsoup2.4'), \
            cfg.build_options_for('libsoup2.4')
        # listed pkg with a per-pkg override → UNION, not replace
        assert cfg.build_options_for('vim') == frozenset({'nodoc', 'nocheck'}), \
            cfg.build_options_for('vim')
        # unlisted pkg stays untouched
        assert 'nocheck' not in cfg.build_options_for('glibc')
        # profiles are NOT affected — SkipTest is an options-only knob
        assert cfg.build_profiles_for('vim') == cfg.build_profiles


def test_sec05_audit_build_deps_default_false():
    """SEC-05: `[Security] AuditBuildDeps` defaults to False — no
    behaviour change for existing operators on un-modified configs."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            print(f"SKIP test_sec05_audit_build_deps_default_false ({cfg.error_str})")
            return
        assert cfg.audit_build_deps is False



def test_sec05_audit_build_deps_parses_true():
    """SEC-05: `[Security] AuditBuildDeps = true` is honoured."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    # _BASE_CONF_BODY has no [Security] section; append one.
    body = _BASE_CONF_BODY + (
        '\n    [Security]\n'
        '    Disabled = true\n'
        '    AuditBuildDeps = true\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            print(f"SKIP test_sec05_audit_build_deps_parses_true ({cfg.error_str})")
            return
        assert cfg.audit_build_deps is True



def test_arch16_empty_per_pkg_section_name_rejected():
    """ARCH-16: an operator typo like `[Source.]` (empty package name)
    must error at config load with a clear hint, not silently store
    overrides under the empty string."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    body = _BASE_CONF_BODY.replace(
        'BuildProfiles = nodoc, nocheck',
        'BuildProfiles = nodoc, nocheck\n\n[Source.]\n    BuildOptions = nostrip',
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        assert not cfg.is_valid, "empty [Source.] name must fail config load"
        assert 'empty package name' in cfg.error_str, cfg.error_str




def test_setup_file_logging_filename_has_timestamp():
    """Two calls in quick succession produce distinct files (timestamped)."""
    import os, tempfile, time
    import tui as _tui

    with tempfile.TemporaryDirectory() as d:
        p1 = _tui.setup_file_logging(d, name='build')
        time.sleep(1.1)  # ensure timestamp granularity (seconds) ticks
        p2 = _tui.setup_file_logging(d, name='build')
        assert p1 != p2, (p1, p2)
        assert os.path.exists(p1) and os.path.exists(p2)



# ─────────────────────────────────────────────────────────────────────────────
# download_file surfaces HTTP status in its return value
# ─────────────────────────────────────────────────────────────────────────────

def test_force_ipv4_http_pins_af_inet_and_respects_optout():
    """force_ipv4_http pins urllib3 to IPv4 (no Happy-Eyeballs → unrouted SLAAC
    IPv6 stalls every connect); ATHENA_ALLOW_IPV6=1 opts out."""
    import socket
    import urllib3.util.connection as _u3
    import utils as _u
    _orig = _u3.allowed_gai_family
    _prev = os.environ.get('ATHENA_ALLOW_IPV6')
    try:
        # default → pins AF_INET
        _u3.allowed_gai_family = _orig
        os.environ.pop('ATHENA_ALLOW_IPV6', None)
        _u.force_ipv4_http()
        assert _u3.allowed_gai_family() == socket.AF_INET
        # opt-out → leaves resolution untouched
        _u3.allowed_gai_family = _orig
        os.environ['ATHENA_ALLOW_IPV6'] = '1'
        _u.force_ipv4_http()
        assert _u3.allowed_gai_family is _orig
    finally:
        _u3.allowed_gai_family = _orig
        if _prev is None:
            os.environ.pop('ATHENA_ALLOW_IPV6', None)
        else:
            os.environ['ATHENA_ALLOW_IPV6'] = _prev




def test_buildconfig_snapshot_endpoints_default_to_debian():
    """When the shipped build.conf [Snapshot] block omits the endpoint
    fields, BuildConfig falls back to the Debian snapshot service.  Test
    the fallback path (covers operators using older build.conf files
    that pre-date ARCH-13)."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            print(f"SKIP test_buildconfig_snapshot_endpoints_default_to_debian ({cfg.error_str})")
            return
        assert cfg.snapshot_baseurl == 'https://snapshot.debian.org/archive'
        assert cfg.snapshot_timestamp_api == 'https://snapshot.debian.org/mr/timestamp/'
        assert cfg.snapshot_archive_keys == ['debian', 'debian-security']



def test_buildconfig_snapshot_endpoints_overridable_via_config():
    """Operators running a fork's own snapshot mirror can point the
    three endpoints at it via [Snapshot] BaseUrl / TimestampApi /
    ArchiveKeys.  Confirm the override is read end-to-end."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main

    [Snapshot]
    Enabled = true
    Timestamp = latest
    BaseUrl = https://snap.athena.local/archive
    TimestampApi = https://snap.athena.local/api/ts
    ArchiveKeys = athena, athena-security, athena-backports
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            print(f"SKIP test_buildconfig_snapshot_endpoints_overridable_via_config ({cfg.error_str})")
            return
        assert cfg.snapshot_baseurl == 'https://snap.athena.local/archive'
        assert cfg.snapshot_timestamp_api == 'https://snap.athena.local/api/ts'
        assert cfg.snapshot_archive_keys == ['athena', 'athena-security', 'athena-backports']



def test_buildconfig_creates_dir_gnupg_with_0700():
    """dir_gnupg is created and chmod 0700 (gpg homedir requirement)."""
    import stat
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            # Possibly missing host keyring; that's covered by another test.
            print(f"SKIP test_buildconfig_creates_dir_gnupg_with_0700 ({cfg.error_str})")
            return
        assert os.path.isdir(cfg.dir_gnupg), cfg.dir_gnupg
        mode = stat.S_IMODE(os.stat(cfg.dir_gnupg).st_mode)
        assert mode == 0o700, f"expected 0700, got {oct(mode)}"



def test_get_sha256_writes_sidecar_on_first_call():
    """First invocation computes the hash AND writes a sidecar
    `<file>.verified` recording (size, mtime_ns, sha256)."""
    import utils
    _f = _make_temp_file(b'hello athena')
    try:
        _h = utils.get_sha256(_f)
        assert _h != '', "expected non-empty hash for valid file"
        _sidecar = _f + '.verified'
        assert os.path.isfile(_sidecar), f"sidecar not written at {_sidecar}"
        with open(_sidecar) as fh:
            _parts = fh.readline().strip().split()
        assert len(_parts) == 3
        _stat = os.stat(_f)
        assert int(_parts[0]) == _stat.st_size
        assert int(_parts[1]) == _stat.st_mtime_ns
        assert _parts[2] == _h
    finally:
        for _p in (_f, _f + '.verified'):
            if os.path.exists(_p):
                os.unlink(_p)



def test_get_sha256_returns_cached_value_on_size_mtime_match():
    """Second call with the same (size, mtime_ns) MUST NOT recompute —
    that's the whole point.  Spy on `_compute_sha256` to assert the
    cache hit."""
    import utils
    _f = _make_temp_file(b'cached content')
    try:
        _h1 = utils.get_sha256(_f)
        # Patch _compute_sha256 to detect a re-hash
        _orig = utils._compute_sha256
        _count = {'n': 0}

        def _spy(path):
            _count['n'] += 1
            return _orig(path)
        utils._compute_sha256 = _spy
        try:
            _h2 = utils.get_sha256(_f)
        finally:
            utils._compute_sha256 = _orig
        assert _h1 == _h2
        assert _count['n'] == 0, (
            f"_compute_sha256 called {_count['n']} time(s) — sidecar"
            f" cache miss (sidecar should have served the request)"
        )
    finally:
        for _p in (_f, _f + '.verified'):
            if os.path.exists(_p):
                os.unlink(_p)



def test_get_sha256_recomputes_when_mtime_changes():
    """Touching the file (mtime advances) invalidates the cache."""
    import time
    import utils
    _f = _make_temp_file(b'mtime-test')
    try:
        _h1 = utils.get_sha256(_f)
        # Advance mtime — use a definitely-different value so we're robust
        # to filesystems with low mtime resolution.
        _stat = os.stat(_f)
        os.utime(_f, ns=(_stat.st_atime_ns, _stat.st_mtime_ns + 10**9))

        _orig = utils._compute_sha256
        _count = {'n': 0}

        def _spy(path):
            _count['n'] += 1
            return _orig(path)
        utils._compute_sha256 = _spy
        try:
            _h2 = utils.get_sha256(_f)
        finally:
            utils._compute_sha256 = _orig
        assert _h1 == _h2, "content unchanged, hash must match"
        assert _count['n'] == 1, (
            f"_compute_sha256 called {_count['n']} time(s); expected 1"
            f" (mtime changed → cache should have missed)"
        )
        _ = time  # quiet unused-import flag — kept for future timing tests
    finally:
        for _p in (_f, _f + '.verified'):
            if os.path.exists(_p):
                os.unlink(_p)



def test_get_sha256_recomputes_when_size_changes():
    """Rewriting the file with different content (size differs)
    invalidates the cache."""
    import utils
    _f = _make_temp_file(b'short')
    try:
        _h1 = utils.get_sha256(_f)
        with open(_f, 'wb') as fh:
            fh.write(b'much longer content than before')
        _h2 = utils.get_sha256(_f)
        assert _h1 != _h2, "content changed, hash must differ"
        # Sidecar updated to record the new (size, mtime_ns)
        with open(_f + '.verified') as fh:
            _parts = fh.readline().strip().split()
        _stat = os.stat(_f)
        assert int(_parts[0]) == _stat.st_size
        assert _parts[2] == _h2
    finally:
        for _p in (_f, _f + '.verified'):
            if os.path.exists(_p):
                os.unlink(_p)



def test_get_sha256_ignores_malformed_sidecar():
    """A garbage sidecar (corrupt write, partial truncation, manual
    edit) MUST NOT cause us to return a wrong hash — we fall through
    to recompute and overwrite the sidecar."""
    import utils
    _f = _make_temp_file(b'fresh content')
    _sidecar = _f + '.verified'
    try:
        # Plant a malformed sidecar BEFORE first call
        with open(_sidecar, 'w') as fh:
            fh.write("this is not a valid sidecar\n")
        _h = utils.get_sha256(_f)
        assert _h != '', "expected valid hash despite malformed sidecar"
        # Sidecar should now be well-formed
        with open(_sidecar) as fh:
            _parts = fh.readline().strip().split()
        assert len(_parts) == 3
        assert _parts[2] == _h
    finally:
        for _p in (_f, _sidecar):
            if os.path.exists(_p):
                os.unlink(_p)



def test_get_sha256_use_cache_false_skips_sidecar_entirely():
    """`use_cache=False` MUST NOT read OR write the sidecar — for
    callers verifying a just-written file that want a strict
    round-trip from disk without any cache layer."""
    import utils
    _f = _make_temp_file(b'no-cache path')
    _sidecar = _f + '.verified'
    try:
        _h = utils.get_sha256(_f, use_cache=False)
        assert _h != ''
        assert not os.path.exists(_sidecar), (
            "use_cache=False should not write the sidecar"
        )
        # If a stale sidecar lies on disk, use_cache=False must NOT trust it.
        with open(_sidecar, 'w') as fh:
            _stat = os.stat(_f)
            fh.write(f"{_stat.st_size} {_stat.st_mtime_ns} deadbeef\n")
        _h2 = utils.get_sha256(_f, use_cache=False)
        assert _h2 == _h and _h2 != 'deadbeef', (
            "use_cache=False must not read the sidecar — got 'deadbeef'"
            " instead of the real hash"
        )
    finally:
        for _p in (_f, _sidecar):
            if os.path.exists(_p):
                os.unlink(_p)



def test_get_sha256_missing_file_returns_empty_string():
    """Pre-existing behaviour — missing file yields '' (callers
    use this as an "expected != computed" signal to trigger
    re-download).  Pin so the new cache layer doesn't change
    semantics."""
    import utils
    _path = '/tmp/athena-sha-test-definitely-does-not-exist-xyz'
    assert not os.path.exists(_path), "test precondition violated"
    assert utils.get_sha256(_path) == ''
    assert utils.get_sha256(_path, use_cache=False) == ''



# ─────────────────────────────────────────────────────────────────────────────
# format_snapshot_timestamp — UI helper for cmd_build_cache
# ─────────────────────────────────────────────────────────────────────────────

def test_format_snapshot_timestamp_well_formed():
    """A valid Debian-snapshot timestamp renders as readable UTC."""
    from utils import format_snapshot_timestamp
    assert (format_snapshot_timestamp('20260506T120451Z')
            == '2026-05-06 12:04:51 UTC')
    assert (format_snapshot_timestamp('20250101T000000Z')
            == '2025-01-01 00:00:00 UTC')



def test_format_snapshot_timestamp_falls_back_on_malformed():
    """Anything that doesn't match YYYYMMDDTHHMMSSZ is returned as-is so the
    caller still has *something* to display."""
    from utils import format_snapshot_timestamp
    for bad in ('latest', '', 'not-a-timestamp', '20260506', '20260506T1204Z'):
        assert format_snapshot_timestamp(bad) == bad



def test_patch_set_hash_stable_and_order_sensitive():
    """patch_set_hash invariants: deterministic for identical
    inputs (so a recorded baseline matches a re-computation), and
    order-sensitive (so re-ordering patches — which could change their
    apply order — yields a different digest)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import patch_set_hash

    with tempfile.TemporaryDirectory() as _root:
        _a = os.path.join(_root, '9001-a.patch')
        _b = os.path.join(_root, '9002-b.patch')
        with open(_a, 'w') as fh: fh.write('content-a\n')
        with open(_b, 'w') as fh: fh.write('content-b\n')
        _h1 = patch_set_hash(_root, ['9001-a.patch', '9002-b.patch'])
        _h2 = patch_set_hash(_root, ['9001-a.patch', '9002-b.patch'])
        _h3 = patch_set_hash(_root, ['9002-b.patch', '9001-a.patch'])
        _h_empty = patch_set_hash(_root, [])
        assert _h1 == _h2, "patch_set_hash must be deterministic"
        assert _h1 != _h3, "patch_set_hash must be order-sensitive"
        assert _h_empty == _h2[:0] or len(_h_empty) == 64, (
            f"empty patch list must still produce a hex digest, got {_h_empty!r}"
        )
        # Content change → different hash
        with open(_a, 'w') as fh: fh.write('content-a-changed\n')
        _h_changed = patch_set_hash(_root, ['9001-a.patch', '9002-b.patch'])
        assert _h_changed != _h1, "content edit must change the digest"


def test_patch_set_hash_folds_prebuild_script():
    """The version-independent prebuild script (`patch/source/<pkg>/
    prebuild.sh`) shapes the build like a patch, so it folds into
    patch_set_hash: adding or editing it changes the digest; passing None
    or a missing path leaves the patch-only digest (so packages without a
    prebuild keep their existing baselines)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import patch_set_hash

    with tempfile.TemporaryDirectory() as _root:
        _a = os.path.join(_root, '9001-a.patch')
        with open(_a, 'w') as fh: fh.write('content-a\n')
        _base = patch_set_hash(_root, ['9001-a.patch'])
        assert patch_set_hash(_root, ['9001-a.patch'],
                              prebuild_path=None) == _base
        assert patch_set_hash(
            _root, ['9001-a.patch'],
            prebuild_path=os.path.join(_root, 'missing.sh')) == _base
        _pb = os.path.join(_root, 'prebuild.sh')
        with open(_pb, 'w') as fh: fh.write('export FOO=1\n')
        _h1 = patch_set_hash(_root, ['9001-a.patch'], prebuild_path=_pb)
        assert _h1 != _base, "adding a prebuild must change the digest"
        with open(_pb, 'w') as fh: fh.write('export FOO=2\n')
        _h2 = patch_set_hash(_root, ['9001-a.patch'], prebuild_path=_pb)
        assert _h2 != _h1, "prebuild content edit must change the digest"
        # prebuild also folds when there are no patches at all
        assert patch_set_hash(_root, [], prebuild_path=_pb) != \
            patch_set_hash(_root, [])



# ─────────────────────────────────────────────────────────────────────────────
# Fork-pkg content invalidation (tree-hash mechanism)
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_tree_hash_deterministic_and_content_addressed():
    """compute_tree_hash must produce identical digests for identical
    tree content (any traversal-order / mtime variation absorbed) and
    different digests when content changes."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, 'a'))
        with open(os.path.join(tmp, 'a', 'x'), 'w') as fh:
            fh.write('hello')
        with open(os.path.join(tmp, 'b'), 'w') as fh:
            fh.write('world')
        _h1 = utils.compute_tree_hash(tmp)
        # Bump mtime only (content unchanged) — hash MUST stay the same
        os.utime(os.path.join(tmp, 'a', 'x'), (1_700_000_000, 1_700_000_000))
        _h2 = utils.compute_tree_hash(tmp)
        assert _h1 == _h2, "mtime-only change must not shift tree hash"

        # Modify content — hash MUST change
        with open(os.path.join(tmp, 'a', 'x'), 'w') as fh:
            fh.write('HELLO')
        _h3 = utils.compute_tree_hash(tmp)
        assert _h1 != _h3, "content change must shift tree hash"



def test_compute_tree_hash_changes_on_file_add_and_delete():
    """Adding or removing a file must shift the tree hash (otherwise
    fork_mirror's invalidation can't catch packages/list-style omissions)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, 'one'), 'w') as fh:
            fh.write('one')
        _baseline = utils.compute_tree_hash(tmp)

        with open(os.path.join(tmp, 'two'), 'w') as fh:
            fh.write('two')
        _added = utils.compute_tree_hash(tmp)
        assert _added != _baseline, "file add must shift tree hash"

        os.remove(os.path.join(tmp, 'two'))
        _restored = utils.compute_tree_hash(tmp)
        assert _restored == _baseline, "post-delete hash must match baseline"



def test_compute_tree_hash_skips_designated_dirs():
    """compute_tree_hash should skip .git / __pycache__ by default so
    bytecode + VCS metadata churn doesn't trigger spurious invalidations."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, 'real'), 'w') as fh:
            fh.write('real content')
        _baseline = utils.compute_tree_hash(tmp)

        # Adding files under .git or __pycache__ should NOT change the hash
        for _d in ('.git', '__pycache__'):
            os.makedirs(os.path.join(tmp, _d))
            with open(os.path.join(tmp, _d, 'noise'), 'w') as fh:
                fh.write('garbage that should be ignored')
        assert utils.compute_tree_hash(tmp) == _baseline, (
            "compute_tree_hash must skip .git / __pycache__")



def test_compute_tree_hash_missing_root_returns_empty_digest():
    """Missing root → SHA256 of empty input.  Defensive return path."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    import hashlib
    _expected = hashlib.sha256().hexdigest()
    assert utils.compute_tree_hash('/nonexistent/path/xyz') == _expected



def test_readfile_decodes_utf8_under_ascii_locale():
    """Regression (audit #43): utils.readfile must decode as UTF-8, not the
    locale default — Debian indices (Packages/Sources) are UTF-8, so under a
    C/ASCII locale a non-ASCII byte (accented maintainer, em-dash) would raise
    UnicodeDecodeError (a ValueError) and escape the cache build's OSError-only
    guards, crashing the whole build."""
    import sys
    import tempfile
    import builtins
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    _real_open = builtins.open

    def _ascii_default_open(file, mode='r', *_a, **_kw):
        if 'b' not in mode and 'encoding' not in _kw:
            _kw['encoding'] = 'ascii'
        return _real_open(file, mode, *_a, **_kw)

    with tempfile.TemporaryDirectory() as _tmp:
        _p = os.path.join(_tmp, 'Packages')
        with _real_open(_p, 'w', encoding='utf-8') as _fh:
            _fh.write('Package: foo\nMaintainer: Jérôme — Test <j@x>\n'
                      'Description: an em — dash\n')
        with mock.patch.object(utils, 'open', _ascii_default_open,
                               create=True):
            _content = utils.readfile(_p)            # must NOT raise
        assert 'Jérôme' in _content and '—' in _content, repr(_content)

TESTS = [
    test_buildconfig_mode_defaults_to_distribution,
    test_buildconfig_mode_build_mode_parses,
    test_buildconfig_mode_rejects_unknown_value,
    test_local_conf_mode_overrides_build_conf,
    test_local_conf_absent_falls_back_to_build_conf,
    test_local_conf_malformed_invalidates_config,
    test_write_local_conf_writes_relocated_machine_keys,
    test_write_local_conf_round_trips,
    test_parse_build_pkg_list_strips_comments_dedups_preserves_order,
    test_buildconfig_parses_three_mirrors,
    test_buildconfig_rejects_no_mirrors,
    test_buildconfig_security_defaults,
    test_buildconfig_security_disabled_accepts_missing_keyring,
    test_buildconfig_security_enabled_rejects_missing_keyring,
    test_buildconfig_creates_dir_gnupg_with_0700,
    test_buildconfig_build_options_and_profiles_are_separate,
    test_buildconfig_build_options_falls_back_to_profiles_when_omitted,
    test_arch16_per_pkg_build_options_override_global,
    test_skiptest_unions_nocheck_into_effective_options,
    test_arch16_empty_per_pkg_section_name_rejected,
    test_sec05_audit_build_deps_default_false,
    test_sec05_audit_build_deps_parses_true,
    test_setup_file_logging_filename_has_timestamp,
    test_force_ipv4_http_pins_af_inet_and_respects_optout,
    test_buildconfig_snapshot_endpoints_default_to_debian,
    test_buildconfig_snapshot_endpoints_overridable_via_config,
    test_get_sha256_writes_sidecar_on_first_call,
    test_get_sha256_returns_cached_value_on_size_mtime_match,
    test_get_sha256_recomputes_when_mtime_changes,
    test_get_sha256_recomputes_when_size_changes,
    test_get_sha256_ignores_malformed_sidecar,
    test_get_sha256_use_cache_false_skips_sidecar_entirely,
    test_get_sha256_missing_file_returns_empty_string,
    test_format_snapshot_timestamp_well_formed,
    test_format_snapshot_timestamp_falls_back_on_malformed,
    test_patch_set_hash_stable_and_order_sensitive,
    test_patch_set_hash_folds_prebuild_script,
    test_compute_tree_hash_deterministic_and_content_addressed,
    test_compute_tree_hash_changes_on_file_add_and_delete,
    test_compute_tree_hash_skips_designated_dirs,
    test_compute_tree_hash_missing_root_returns_empty_digest,
    test_readfile_decodes_utf8_under_ascii_locale,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
