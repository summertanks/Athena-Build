#!/usr/bin/env python3
"""Shared fixtures, stubs and helpers for the Athena test suite.

Every tests/test_<part>.py imports from here.  The suite runner also
lives here (run_tests) so any part can execute standalone:
    python3 tests/test_module.py          # whole suite
    python3 tests/test_<part>.py          # one part

No real Docker, no real sudo, no real network — anything that touches
those is mocked at the boundary.
"""
import os
import sys
import tempfile
import textwrap

# Allow running from project root: `python3 tests/test_module.py`
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))


def _session_source():
    """Concatenated source of the BuildSession surface — build.py plus every
    command mixin under commands/.  Source-scan tests use this so they survive
    the god-class split: a handler/route asserted-present may now live in any
    mixin module, while the dispatcher (register_command) stays in build.py."""
    import glob
    _parts = [os.path.join(_ROOT, 'scripts', 'build.py')]
    # Only the real command implementations — NOT base.py (type-only stub
    # declarations like `def cmd_audit(self, *args): ...`, which would let a
    # body-extraction regex match the stub) nor the package __init__.
    _parts += [_p for _p in sorted(
        glob.glob(os.path.join(_ROOT, 'scripts', 'commands', '*.py')))
        if os.path.basename(_p) not in ('base.py', '__init__.py')]
    _txt = ''
    for _p in _parts:
        with open(_p) as _fh:
            _txt += _fh.read()
    # Sentinel terminator: many source-scan tests extract a method body with
    # `... .*?(?=\n    def )`.  The last real method in the last concatenated
    # mixin has no following `    def ` to anchor that lookahead — append a
    # harmless one so those regexes still terminate at EOF.
    _txt += '\n\n    def _source_scan_eof_sentinel(self):\n        pass\n'
    return _txt


def _write_test_config(tmpdir: str, body: str) -> str:
    """Helper: write a synthetic build.conf + empty pkg.list under tmpdir."""
    cfg_dir = os.path.join(tmpdir, 'config')
    os.makedirs(cfg_dir)
    path = os.path.join(cfg_dir, 'build.conf')
    with open(path, 'w') as fh:
        fh.write(textwrap.dedent(body))
    with open(os.path.join(cfg_dir, 'pkg.list'), 'w') as fh:
        fh.write('')
    return path


def _build_config_from(tmpdir: str, cfg_path: str):
    """Helper: instantiate BuildConfig with patched argv."""
    from utils import BuildConfig
    saved = sys.argv
    sys.argv = ['test', '--working-dir', tmpdir, '--config-file', cfg_path,
                '--pkg-list', os.path.join(tmpdir, 'config', 'pkg.list')]
    try:
        return BuildConfig()
    finally:
        sys.argv = saved


_BASE_CONF_BODY = """
    [Build]
    ARCH = amd64
    DISTRIBUTION = "Testdistro"
    CODENAME = "test"
    VERSION = "1"
    CHANNEL = "stable"
    MaxParallelBuilds = 1

    [Base]
    BASEURL = http://deb.debian.org
    BASEID = debian
    RELEASE = bookworm
    BASEVERSION = 12.0
    {mirror_block}
    [Directories]
    Log = log
    Cache = cache
    Temp = tmp
    Source = source
    Build = build
    Repo = repo
    LocalMirror = localmirror
    Config = config
    Patch = patch
    Fork = fork
    Image = image
    Chroot = buildroot
    Gnupg = gnupg

    [Source]
    SkipTest =
    BuildProfiles = nodoc, nocheck
    Tunneled =
    DistroSuffix = thor1
    IncludeBuildClosure = false
    """


_MINIMAL_MIRROR_BLOCK = """
    [Mirror.main]
    Suffix =
    Component = main
    """


def _write_local_conf(tmp: str, body: str) -> None:
    """Drop a config/local.conf alongside the synthetic build.conf."""
    with open(os.path.join(tmp, 'config', 'local.conf'), 'w') as fh:
        fh.write(textwrap.dedent(body))


class _FakeOnbSession:
    """Minimal session for onboarding tests: a config with a tmp config_path
    (so write_local_conf lands in the tmp dir) + the federation methods the
    wizard calls, recording invocations."""
    def __init__(self, tmp, *, self_keys=('bid-1', 'priv', 'pub'),
                 cmd_mirror_ret=True, cmd_key_ret=True):
        class _Cfg:
            pass
        self.config = _Cfg()
        self.config.config_path = os.path.join(tmp, 'build.conf')
        self.config.dir_config = tmp        # ssh key copy + local.conf land here
        self.config.build_mode = 'distribution'
        self.config.setup_complete = False
        self._self_keys = self_keys
        self._cmd_mirror_ret = cmd_mirror_ret
        self._cmd_key_ret = cmd_key_ret
        self._prepared = True               # _mirror_is_prepared result
        self.mirror_calls: 'list' = []
        self.key_calls: 'list' = []
        self.snap_called = False

    def _coord_self_keys(self):
        return self._self_keys

    def _coord_builder_id(self):
        return self._self_keys[0] if self._self_keys else None

    def _mirror_is_prepared(self, url):
        return (self._prepared, 'ok' if self._prepared else 'no marker')

    def cmd_mirror(self, *args):
        self.mirror_calls.append(args)
        return self._cmd_mirror_ret

    def cmd_key(self, *args):
        self.key_calls.append(args)
        return self._cmd_key_ret

    def _ensure_snapshot_pins(self):
        self.snap_called = True


import contextlib as _contextlib


@_contextlib.contextmanager
def _onboarding_patched(answers, *, verify_ok):
    """Patch onboarding.Prompt (scripted answers), signing.verify_key, and
    console.print for the duration of a wizard run; restore after."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import onboarding
    _seq = list(answers)
    _printed: 'list[str]' = []

    class _P:
        def __init__(self, *a, **k):
            pass

        def get_response(self):
            return _seq.pop(0) if _seq else ''

    _saved = (onboarding.Prompt, onboarding.signing.verify_key,
              onboarding.console.print)
    onboarding.Prompt = _P
    onboarding.signing.verify_key = lambda cfg: (verify_ok, 'stub')
    onboarding.console.print = lambda *a, **k: _printed.append(
        str(a[0]) if a else '')
    try:
        yield _printed
    finally:
        (onboarding.Prompt, onboarding.signing.verify_key,
         onboarding.console.print) = _saved


@_contextlib.contextmanager
def _federation_validators_patched(*, prepared=True, probes_ok=True,
                                   gpg_ok=True):
    """Patch the federation-flow validators (mirror probes, GPG install) so the
    happy-path orchestration can be exercised without a live mirror."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import onboarding
    import mirror as _m
    _saved = (_m.probe_dns_and_tcp, _m.probe_ssh_auth,
              _m.probe_remote_writable, onboarding._validate_and_install_gpg)
    _r = (probes_ok, 'ok')
    _m.probe_dns_and_tcp = lambda *a, **k: _r
    _m.probe_ssh_auth = lambda *a, **k: _r
    _m.probe_remote_writable = lambda *a, **k: _r
    onboarding._validate_and_install_gpg = lambda *a, **k: gpg_ok
    try:
        yield
    finally:
        (_m.probe_dns_and_tcp, _m.probe_ssh_auth,
         _m.probe_remote_writable, onboarding._validate_and_install_gpg) = _saved


# ─────────────────────────────────────────────────────────────────────────────
# Source.build_depends — virtual-package expansion
# ─────────────────────────────────────────────────────────────────────────────
#
# Multi-provider virtual build-deps (libcurl4-dev, libsdl-dev, awk, …)
# can't be resolved by `apt-get install` non-interactively — apt refuses
# to disambiguate from the CLI.  Source.build_depends(cache=…) rewrites
# such single-element groups in-place to an alternatives chain so the
# BuildContainer's `||`-fallback apt-install loop can succeed.


class _StubProviderCache:
    """Minimal cache shape — only `.get_packages(name)` is exercised by
    `Source._expand_virtual_alternatives`.  Returns dict-like records
    whose 'Package' key is the canonical name (matching the
    `_pkg['Package']` access in production code)."""

    def __init__(self, table):
        # name → list of {'Package': canonical_name} dicts
        self._table = table

    def get_packages(self, name):
        return list(self._table.get(name, []))


def _src_with_build_depends(raw_build_depends):
    """Build a minimally-valid Source object with the given Build-Depends."""
    import package
    stanza = (
        "Package: dummy\n"
        "Version: 1.0\n"
        "Architecture: any\n"
        "Directory: pool/main/d/dummy\n"
        "Files:\n a 0 dummy_1.0.dsc\n"
        f"Build-Depends: {raw_build_depends}\n"
    )
    return package.Source(stanza)


# ─────────────────────────────────────────────────────────────────────────────
# _compute_install_batches single-pass topo sort
# ─────────────────────────────────────────────────────────────────────────────
#
# Tests use lightweight stand-in objects so we can exercise the graph logic
# without a real DependencyTree / apt cache / dpkg subprocess.

class _StubDepTree:
    """Mimics the surface of dependencytree.DependencyTree that
    _resolve_pre_depends / _resolve_depends / _compute_install_batches
    touch.  Each pkg_spec is (name, pre_depends, depends); the stub
    bypasses Provides / alt-deps machinery (covered by other tests).
    """
    def __init__(self, pkg_specs):
        class _Pkg:
            def __init__(self, name, pre, dep):
                # _resolve_pre_depends / _resolve_depends iterate
                # pkg.pre_depends / pkg.depends as lists of (name,…) tuples,
                # then look the name up in selected_pkgs and read .['Package'].
                self.pre_depends     = [(n, '', '') for n in pre]
                self.alt_pre_depends = []
                self.depends         = [(n, '', '') for n in dep]
                self.alt_depends     = []
                self._fields = {'Package': name}
            def __getitem__(self, k):
                return self._fields[k]
            def get(self, k, default=''):
                return self._fields.get(k, default)
        self.selected_pkgs = {
            spec[0]: _Pkg(*spec) for spec in pkg_specs
        }


class _StubConsole:
    def print(s, m, *a, **k): pass
    def info(s, m): pass
    def warning(s, m): pass
    def error(s, m): pass
    def mark(s): return 0
    def trim_to(s, *a): pass


def _bare_buildsystem_with_deps(pkg_specs):
    """Build a BuildSystem instance wired only enough to run the dep-graph
    methods.  Bypasses __init__ (no sudo, no chroot dirs, no Prompt).

    Always replaces tui.console with a no-op stub: the default
    tui.console is a Console facade whose methods raise
    "No Tui instance — create Tui before using Console" — has the right
    surface but the wrong behaviour for tests.  Replacing it is cheap
    and makes the cycle-batch warning path testable.
    """
    import buildsystem
    import tui as _tui
    _tui.console = _StubConsole()
    bs = buildsystem.BuildSystem.__new__(buildsystem.BuildSystem)
    bs._dependencytree = _StubDepTree(pkg_specs)
    # STA-53(c): __init__ (bypassed here) seeds the per-instance chroot env;
    # provide a default so methods that pass env=self._chroot_env don't
    # AttributeError under bare construction.
    bs._chroot_env = dict(os.environ)
    return bs


# ─────────────────────────────────────────────────────────────────────────────
# InRelease GPG verification
# ─────────────────────────────────────────────────────────────────────────────
#
# These tests generate a throwaway PGP keypair in a temp dir, clear-sign a
# tiny payload with it, then verify against an exported keyring file.  No
# host state required — works on any machine with `gpg` on PATH.

def _gen_signed_fixture(tmpdir: str):
    """Create a temp keypair, return (signed_path, keyring_path, gen_home).

    gen_home is the gnupg homedir used to generate the key.  The caller
    should pass a *different* gnupg homedir to verify_inrelease() so we
    are testing the keyring-import path, not just the same-home case.
    """
    import gnupg

    gen_home = os.path.join(tmpdir, 'gen-home')
    os.makedirs(gen_home, mode=0o700)
    gpg = gnupg.GPG(gnupghome=gen_home)  # type: ignore[attr-defined]
    key_input = gpg.gen_key_input(
        name_real='Athena Test Signer',
        name_email='test@athena.local',
        passphrase='',
        no_protection=True,
        key_type='RSA',
        key_length=2048,
        expire_date='1d',
    )
    key = gpg.gen_key(key_input)
    assert key.fingerprint, f"gen_key failed: {key.stderr if hasattr(key, 'stderr') else key}"

    # NOTE: python-gnupg silently drops the first line of stdin to gpg
    # (a known library quirk).  Lead with a blank line so the signed
    # body actually contains the headers we expect to tamper with.
    payload = (
        "\n"
        "Origin: AthenaTest\n"
        "Suite: athena\n"
        "Codename: athena\n"
        "Date: Thu, 07 May 2026 12:00:00 UTC\n"
        "SHA256:\n"
        " 0000000000000000000000000000000000000000000000000000000000000000 "
        "1 main/binary-amd64/Packages\n"
    )
    signed = gpg.sign(payload, keyid=key.fingerprint, clearsign=True, passphrase='')
    signed_path = os.path.join(tmpdir, 'InRelease')
    with open(signed_path, 'w') as fh:
        fh.write(str(signed))

    keyring_path = os.path.join(tmpdir, 'keyring.gpg')
    pubkey = gpg.export_keys(key.fingerprint)
    assert pubkey, "export_keys returned empty"
    with open(keyring_path, 'w') as fh:
        fh.write(pubkey)

    return signed_path, keyring_path, gen_home


# ─────────────────────────────────────────────────────────────────────────────
# BuildSystem sudo-password lifetime
# ─────────────────────────────────────────────────────────────────────────────

def _bare_buildsystem(password: str = 'secret'):
    """Construct a BuildSystem with just the password attributes set —
    bypasses __init__ so tests don't need real config / sudo / chroot dirs."""
    import buildsystem
    bs = buildsystem.BuildSystem.__new__(buildsystem.BuildSystem)
    bs._password = password
    bs._password_scrubbed = False
    return bs


# ─────────────────────────────────────────────────────────────────────────────
# download_source surfaces HTTP / short-download errors clearly
# ─────────────────────────────────────────────────────────────────────────────

def _download_source_with_mocked_get(mock_resp_factory, expected_size: int = 100,
                                     sha256_hex: str = 'a' * 64):
    """Run utils.download_source against a single-file fake dep tree, with
    requests.get patched to return whatever mock_resp_factory() yields.

    Returns the list of console messages (print/info/warning/error) emitted
    during the call, so callers can assert on the wording without any live
    network or real Mirror infrastructure.
    """
    from unittest.mock import patch, MagicMock
    import utils
    from utils import Mirror

    captured: list = []
    class _Cap:
        def print(s, m, *a, **k): captured.append(m)
        def info(s, m):           captured.append(m)
        def warning(s, m):        captured.append(m)
        def error(s, m):          captured.append(m)

    class _Bar:
        def __init__(s, *a, **k): pass
        def step(s, *a, **k): pass
        def label(s, *a, **k): pass
        def set_max(s, *a, **k): pass
        def reset(s, *a, **k): pass
        def close(s, *a, **k): pass

    mirror = Mirror('main', 'http://x.test', 'debian', 'bookworm', '', 'main', 'amd64')

    class _Src:
        _mirror = mirror
        files = {
            'pkg_1.0.dsc': {
                'sha256': sha256_hex,
                'size':   expected_size,
                'md5':    '',
                'path':   'pool/main/p/pkg/pkg_1.0.dsc',
            }
        }

    class _Dt:
        selected_srcs = {'pkg': _Src()}
        download_size = expected_size

    saved_console = utils.tui.console
    saved_bar     = utils.ProgressBar
    utils.tui.console = _Cap()        # type: ignore[assignment]
    utils.ProgressBar = _Bar          # type: ignore[assignment,misc]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _ms = MagicMock()
            _ms.get.return_value = mock_resp_factory()
            with patch.object(utils, '_http_session', return_value=_ms):
                utils.download_source(_Dt(), tmp)
    finally:
        utils.tui.console = saved_console
        utils.ProgressBar = saved_bar  # type: ignore[misc]

    return captured


def _make_fake_popen(calls_log: list):
    """Build a side_effect for `subprocess.Popen` that records the argv
    and returns a fake process whose stdout streams a single stub
    `Package: ...` stanza (enough to keep _scan_packages_with_progress
    happy without an actual dpkg-scanpackages run).

    Used by the post-2026-05-22 apt_repo + repo_audit tests that pin
    the new Popen-based scanner pipeline.
    """
    from unittest.mock import MagicMock
    import io
    def _fake(argv, *_a, **_kw):
        calls_log.append(tuple(argv) if isinstance(argv, (list, tuple)) else (argv,))
        _proc = MagicMock()
        # stdout: stub Packages stanza ending with a Package: line so
        # the bar steps at least once.
        _proc.stdout = io.StringIO("Package: stub\nVersion: 1.0\n\n")
        _proc.stderr = io.StringIO("")
        _proc.stdin  = MagicMock()
        _proc.wait.return_value = 0
        return _proc
    return _fake


def _wrap_with_install_handler(inner_fake):
    """Wrap an apt_repo `subprocess.run` fake to ALSO handle the
    `sudo -S install -m 644 <src> <dst>` calls that repo_audit's
    _scan_packages_with_progress emits to atomically place the
    scanner tempfile.  Both fakes share the same subprocess module
    object (apt_repo.subprocess IS repo_audit.subprocess IS the
    stdlib `subprocess`), so chaining via one wrapper keeps both
    branches exercised without one patch overriding the other."""
    from unittest.mock import MagicMock
    import shutil as _shutil
    def _fake(argv, *a, **kw):
        if (isinstance(argv, (list, tuple)) and len(argv) >= 7
                and argv[:3] == ['sudo', '-S', 'install']):
            _src, _dst = argv[5], argv[6]
            try:
                _shutil.move(_src, _dst)
            except OSError:
                pass
            _r = MagicMock()
            _r.returncode = 0
            _r.stderr = ''
            return _r
        return inner_fake(argv, *a, **kw)
    return _fake


def _make_pool_dep_tree_stub():
    """Shared fixture for pool.list dep-tree tests.

    Builds a DependencyTree without invoking __init__ (which needs a real
    Cache) — replicates only the fields the assertions touch.  Two
    conflicting Package stubs (`grub-pc` + `grub-efi-amd64`) and a
    minimal Cache so add_lookahead can resolve them without prompting.
    """
    import sys
    from collections import defaultdict
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree

    class _Pkg:
        def __init__(self, name, ver, conflicts=None, breaks=None):
            self._fields = {'Package': name, 'Version': ver}
            self.package = name
            self.version = ver
            self._provides = []
            self.conflicts = conflicts or []
            self.depends = []
            self.pre_depends = []
            self.recommends = []
            self.alt_depends = []
            self.alt_pre_depends = []
            self.breaks = breaks or []
            self.constraints_satisfied = True
        def __getitem__(self, k): return self._fields[k]
        def get_provides(self): return list(self._provides)
        def add_constraint(self, v, o): pass

    grub_pc  = _Pkg('grub-pc',         '2.06-13+deb12u1',
                    conflicts=[[('grub-efi-amd64', '', '')]])
    grub_efi = _Pkg('grub-efi-amd64',  '2.06-13+deb12u1',
                    conflicts=[[('grub-pc',        '', '')]])

    class _Cache:
        def __init__(self):
            self.package_hashtable = {
                'grub-pc':        {grub_pc.version:  [grub_pc]},
                'grub-efi-amd64': {grub_efi.version: [grub_efi]},
            }
            self.skip_src = []
        def get_packages(self, name, ver=None, op=''):
            result = []
            for _vlist in self.package_hashtable.get(name, {}).values():
                result.extend(_vlist)
            return result

    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt._DependencyTree__cache = _Cache()
    dt._DependencyTree__lookahead = defaultdict(dict)
    dt._DependencyTree__recommended = False
    dt.selected_pkgs = {}
    dt.selected_srcs = {}
    dt.extras_pkg_names = set()
    dt.live_exclusive_pkg_names = set()
    dt.installer_exclusive_pkg_names = set()
    dt.pool_extras_pkg_names = set()
    dt.pool_extras_src_names = set()
    dt._auto_pick_highest_when_ambiguous = False
    dt.arch = 'amd64'
    dt.build_profiles = frozenset()
    return dt, grub_pc, grub_efi, dependencytree


# ─────────────────────────────────────────────────────────────────────────────
# COMP-12 Phase F — installer_smoke harness pattern parser
# ─────────────────────────────────────────────────────────────────────────────


def _smoke_import():
    """Import tests/installer_smoke/known_bad_patterns.py without
    polluting sys.path globally."""
    import importlib.util
    _path = os.path.join(_ROOT, 'tests', 'installer_smoke',
                         'known_bad_patterns.py')
    assert os.path.isfile(_path), f"missing {_path}"
    _spec = importlib.util.spec_from_file_location(
        'installer_smoke_known_bad_patterns', _path,
    )
    assert _spec is not None and _spec.loader is not None
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    return _mod


def _build_sample_for_pattern(re_src: str) -> str:
    """Produce a line that should match `re_src`.  For patterns with
    regex metachars (literal-dot escapes, .* wildcards, escaped
    asterisks), construct a sample by hand-substituting escaped
    metachars to their literal form so the synthesized line matches.
    """
    _specials = {
        r'WARNING \*\*:.*main-menu':
            'May 22 12:34:56 something WARNING **: foo main-menu bar',
    }
    if re_src in _specials:
        return _specials[re_src]
    # Generic case: turn `\.` into `.`, `\*` into `*`, `.*` into
    # `_anything_`, then prepend a syslog-ish timestamp prefix.
    _literal = (
        re_src
        .replace(r'.*', '_anything_')
        .replace(r'\.', '.')
        .replace(r'\*', '*')
    )
    return f'May 22 12:34:56 stub: {_literal}'


def _fake_sudo_write_run(cmd, *_a, **_k):
    """Mock for installer_chroot._sudo_write's two subprocess.run calls:
      1. `sudo -S -v` — refreshes sudo timestamp, consumes password line.
      2. `sudo tee <path>` — writes raw stdin content to path.
    Tests expecting password-less stdin to tee depend on this split."""
    class _R:
        returncode = 0
        stderr = ''
    if cmd[:3] == ['sudo', '-S', '-v']:
        # Refresh — no side effect beyond returning 0.
        return _R()
    if cmd[:2] == ['sudo', 'tee'] and len(cmd) >= 3:
        # Tee — stdin is raw content (NOT prefixed with password — that's
        # the whole point of the fix).
        with open(cmd[2], 'w') as fh:
            fh.write(_k.get('input', ''))
        return _R()
    raise AssertionError(f"unexpected subprocess.run call: {cmd}")


class _StubDockerImage:
    def __init__(self, image_id, tags, short_id='img1234'):
        self.id = image_id
        self.tags = tags
        self.short_id = short_id


class _StubDockerContainer:
    def __init__(self, image, short_id='c1234'):
        self.image = image
        self.short_id = short_id
        self.removed = False
    def remove(self, force=False):
        self.removed = True


class _StubDockerClient:
    """Minimal Docker client surface the purge code touches."""
    def __init__(self, containers=None, images=None):
        self._containers = containers or []
        self._images = images or []
        self.images_removed = []
        # Mirror docker SDK shape: client.containers.list(), client.images.list().
        self.containers = type('CMgr', (), {
            'list': lambda _self, **kw: list(self._containers),
        })()
        self.containers.list = lambda **kw: list(self._containers)
        self.images = type('IMgr', (), {})()
        self.images.list = lambda **kw: list(self._images)
        self.images.remove = lambda image_id, force=False: self.images_removed.append(image_id)
    def ping(self):
        return True


def _build_test_deb(_tmp, pkg, version, fname):
    """Build a minimal .deb with the given Package/Version under _tmp; return
    its path.  Shared by the transpose coverage tests."""
    import subprocess
    _work = os.path.join(_tmp, 'src_' + fname)
    os.makedirs(os.path.join(_work, 'DEBIAN'))
    os.makedirs(os.path.join(_work, 'usr', 'lib'))
    with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as _fh:
        _fh.write(f'Package: {pkg}\nVersion: {version}\n'
                  'Architecture: amd64\nMaintainer: T <t@l>\n'
                  'Description: transpose coverage\n')
    with open(os.path.join(_work, 'usr', 'lib', 'p'), 'w') as _fh:
        _fh.write('x\n')
    _p = os.path.join(_tmp, fname)
    subprocess.run(['dpkg-deb', '--root-owner-group', '-b', _work, _p],
                   check=True, capture_output=True)
    return _p


def _have_dpkg_deb():
    import subprocess
    try:
        subprocess.run(['dpkg-deb', '--version'], check=True,
                       capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _make_synthetic_deb(path, pkg_name, version, arch='amd64'):
    """Build a minimal valid .deb at the given path for round-trip tests.
    Returns the file path.  Requires dpkg-deb on PATH (Debian-derived
    hosts always have it; CI on Ubuntu does too)."""
    import subprocess, tempfile
    with tempfile.TemporaryDirectory() as _work:
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        os.makedirs(os.path.join(_work, 'usr', 'share', 'doc', pkg_name))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(
                f'Package: {pkg_name}\n'
                f'Version: {version}\n'
                f'Architecture: {arch}\n'
                f'Maintainer: Test <test@local>\n'
                f'Description: synthetic test package\n'
                f' Used by tests/test_module.py for rebump round-trips.\n'
            )
        with open(os.path.join(_work, 'usr', 'share', 'doc', pkg_name, 'README'), 'w') as fh:
            fh.write('synthetic data file — verifies data.tar survives repack\n')
        subprocess.run(
            ['dpkg-deb', '--root-owner-group', '-b', _work, path],
            check=True, capture_output=True,
        )
    return path


# ─────────────────────────────────────────────────────────────────────────────
# get_sha256 — sidecar (size, mtime_ns) cache
# ─────────────────────────────────────────────────────────────────────────────
#
# Caching wraps `_compute_sha256`.  Tests assert cache hits/misses by
# monkey-patching `_compute_sha256` to count invocations.


def _make_temp_file(contents: bytes = b'hello world'):
    """Helper — write `contents` to a tempfile, return its path."""
    import tempfile
    _fd, _path = tempfile.mkstemp(prefix='athena-sha-test-')
    os.write(_fd, contents)
    os.close(_fd)
    return _path


# ─────────────────────────────────────────────────────────────────────────────
# print_commands — dispatch + help screen
# ─────────────────────────────────────────────────────────────────────────────

def _capture_console_print(callable_):
    """Run callable_ with tui.console.print monkeypatched to capture lines.
    Returns the captured output as one newline-joined string."""
    import tui
    captured = []
    _orig = tui.console.print
    tui.console.print = lambda *args, **kwargs: captured.append(
        ' '.join(str(a) for a in args)
    )
    try:
        callable_()
    finally:
        tui.console.print = _orig
    return '\n'.join(captured)


class _PrintSessionStub:
    """Minimal BuildSession-shaped stub for invoking print handlers safely.
    cache/dep_tree are None, flags are all-False — handlers should hit
    their `run X first` guards and print a friendly message rather than
    AttributeError on .required / .selected_pkgs / etc."""
    def __init__(self):
        # Empty BuildConfig-shaped stub — just enough attrs that the
        # handlers that don't need cache/dep_tree (config, mirrors, paths,
        # snapshot, state, stats, tunneled) can render something.
        class _Cfg:
            arch = 'amd64'
            release = 'bookworm'
            baseid = 'debian'
            baseversion = '12'
            build_distribution = 'Athena'
            build_base_id = 'athena'
            build_codename = 'athena'
            build_version = '0.1'
            mirrors: list = []
            snapshot_enabled = False
            snapshot_timestamp_config = ''
            tunnel_packages: list = []
            include_recommends = False
            signing_key_uid = 'Athena Build <athena@local>'
            working_dir = '/tmp/build'
            dir_cache = '/tmp/build/cache'
            dir_image = '/tmp/build/image'
            dir_log = '/tmp/build/log'
            dir_gnupg = '/tmp/build/gnupg'
            config_path = '/tmp/build/build.conf'
            pkglist_path = '/tmp/build/pkg.list'
        self.config = _Cfg()
        self.cache = None
        self.dep_tree = None

        class _Flags:
            cache_ready             = False
            dep_check_ready         = False
            download_ready          = False
            build_container_ready   = False
            source_build_ready      = False
            signing_key_verified    = False
            chroot_ready            = False
            chroot_verified         = False
            chroot_installer_ready  = False
            chroot_disk_ready = False
            iso_live_ready          = False
            iso_installer_ready     = False
            iso_disk_ready          = False
        self.flags = _Flags()


def _build_autorun_session_stub(*, all_done: bool, source_build_done: bool):
    """Construct a minimal BuildSession-shaped stub for exercising the
    autorun summary helper without standing up TUI / config / cache."""
    from build import BuildSession

    class _Cfg:
        arch = 'amd64'
        build_version = '0.1'
        build_distribution = 'Athena'
        dir_image = '/tmp/image'

    class _Flags:
        cache_ready             = True
        dep_check_ready         = True
        download_ready          = source_build_done or all_done
        build_container_ready   = source_build_done or all_done
        source_build_ready      = source_build_done or all_done
        signing_key_verified    = all_done
        chroot_ready            = all_done
        chroot_verified         = all_done
        # 2026-05-14: summary view now renders live + installer chroot
        # rows + iso target rows independently.  Default to all_done so
        # an "all green" run renders both targets as built.
        chroot_installer_ready  = all_done
        iso_live_ready          = all_done
        iso_installer_ready     = all_done
        iso_disk_ready          = all_done

    class _Pkg:
        def __init__(self, name): self._name = name
        def __getitem__(self, k):
            return self._name if k == 'Package' else None

    class _Cache:
        package_hashtable = {f'pkg{i}': None for i in range(32847)}

    class _DT:
        selected_pkgs = {f'pkg{i}': _Pkg(f'pkg{i}') for i in range(213)}
        selected_srcs = {f'src{i}': None for i in range(31)}

    sess = object.__new__(BuildSession)
    sess.config = _Cfg()        # type: ignore[assignment]
    sess.tui = None
    sess.cache = _Cache()       # type: ignore[assignment]
    sess.dep_tree = _DT()       # type: ignore[assignment]
    sess.container = None
    sess.flags = _Flags()       # type: ignore[assignment]
    sess.last_source_build_counts = (
        {'built': 26, 'tunneled': 2, 'failed': 0, 'skipped': 3, 'total': 31}
        if source_build_done else None
    )
    return sess


# ─────────────────────────────────────────────────────────────────────────────
# pull recommends into selected_pkgs as available-not-installed
# ─────────────────────────────────────────────────────────────────────────────

class _FakePkg:
    """Minimal Package surface for tests.  Carries the fields
    DependencyTree.pull_recommends_extras, parse_dependency, and
    derive_extras_src_names actually read: ['Package'], .recommends,
    .source, .get('Filename'), .depends, .pre_depends, .alt_depends,
    .get_provides(), .version, .add_constraint, .depends_on, .depended_by."""
    def __init__(self, name, source, filename=None, recommends=None,
                 depends=None, version='1.0'):
        self._fields = {'Package': name, 'Filename': filename or ''}
        self.source = source
        self.package = name
        from debian.debian_support import Version
        try:
            self.version = Version(version)
        except Exception:
            self.version = version
        # parse_depends shape: each entry is a tuple (name, ver, op).
        self.recommends = [(r, '', '') for r in (recommends or [])]
        self.depends = [(d, '', '') for d in (depends or [])]
        self.pre_depends = []
        self.alt_depends = []
        self.alt_pre_depends = []
        self.depends_on = []
        self.depended_by = []

    def __getitem__(self, k): return self._fields[k]
    def __contains__(self, k): return k in self._fields
    def get(self, k, default=''): return self._fields.get(k, default)
    def get_provides(self): return []
    def add_constraint(self, *args, **kwargs): pass


class _FakeCache:
    """Minimal Cache surface — package_hashtable + skip_src + get_packages.
    Mirrors the real shape: Dict[name, Dict[version, List[Package]]] — the
    inner list carries the per-mirror records.  Versions are simple strings
    so max() works lexically (sufficient for these tests)."""
    def __init__(self, pkgs_by_name, skip_src=()):
        # pkgs_by_name: {name: [_FakePkg, ...]}
        self.package_hashtable = {
            name: {f"v{i}": [p] for i, p in enumerate(pkgs)}
            for name, pkgs in pkgs_by_name.items()
        }
        self.skip_src = list(skip_src)

    def get_packages(self, name, version=None, constraint=''):
        """Mirror Cache.get_packages: return the Package candidates matching
        `name` (optionally version-filtered).  For test simplicity we ignore
        the version filter — recommends in our fixtures are unversioned, so
        callers don't trip on this."""
        _bucket = self.package_hashtable.get(name) or {}
        _out = []
        for _ver_pkgs in _bucket.values():
            _out.extend(_ver_pkgs)
        return _out


def _build_dep_tree_with_recommend(*, recommend_source='libnss3',
                                   skip_src=()):
    """Construct a DependencyTree containing one selected pkg `firefox` from
    source `firefox` that recommends `libnss3-tools` from `recommend_source`.
    Used by several tests."""
    import dependencytree
    seed = _FakePkg('firefox', source='firefox',
                    filename='pool/main/f/firefox/firefox_1.0_amd64.deb',
                    recommends=['libnss3-tools'])
    rec = _FakePkg('libnss3-tools', source=recommend_source,
                   filename=f'pool/main/n/{recommend_source}/'
                            f'libnss3-tools_3.0_amd64.deb',
                   recommends=[])
    cache = _FakeCache(
        {'firefox': [seed], 'libnss3-tools': [rec]},
        skip_src=skip_src,
    )
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    # Bypass __init__ (avoids needing the full Cache constructor) — set the
    # attributes pull_recommends_extras + parse_dependency +
    # derive_extras_src_names actually use.
    _seed_test_dep_tree(dt, cache, {'firefox': seed})
    return dt, seed, rec


def _seed_test_dep_tree(dt, cache, selected_pkgs):
    """Populate a __new__-constructed DependencyTree with the minimum
    attribute set parse_dependency / pull_recommends_extras need.

    Centralised here because Bug 1's fix wired pull_recommends_extras
    through parse_dependency, which touches more state (lookahead,
    constraints set, etc.) than the old direct-insert path."""
    from collections import defaultdict
    dt._DependencyTree__cache = cache
    dt._DependencyTree__lookahead = defaultdict(dict)
    dt._DependencyTree__recommended = False
    dt._auto_pick_highest_when_ambiguous = False
    dt.selected_pkgs = dict(selected_pkgs)
    dt.selected_srcs = {}
    dt.src_pkg_files = {}
    dt.extras_pkg_names = set()
    dt.extras_src_names = set()
    dt.live_exclusive_pkg_names = set()
    dt.installer_exclusive_pkg_names = set()
    dt.pool_extras_pkg_names = set()
    dt.pkg_group_extras_pkg_names = set()


# ─────────────────────────────────────────────────────────────────────────────
# STA-18 — Depends-arm aliasing: alias entries must use Provides clause
# version (not provider's own Version field) when checking constraints
# ─────────────────────────────────────────────────────────────────────────────

def _sta18_make_dt():
    """Shared stub DependencyTree + Package factory for STA-18 tests.

    Mirrors the libgcc1 shape: a provider whose own Version: lacks the
    epoch its Provides: clause declares.  Concrete bookworm case:
    libgcc-s1 (Version: 12.2.0-14+deb12u1) declares
    `Provides: libgcc1 (= 1:12.2.0-14+deb12u1)` — epoch 1 in the
    Provides clause, epoch 0 in self.version.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    from debian.debian_support import Version

    class _Pkg:
        """Surface needed by _version_for_constraint_target + the
        validate_selection alt-dep loop: __getitem__ for 'Package',
        .version, .explicit_provides_version(name).  No real Provides
        parsing — fixture hands the helper a name→version map."""
        def __init__(self, name, version, *, provides=()):
            self._fields = {'Package': name}
            self.version = Version(version)
            # provides shape: ((name, version_str_or_None), ...)
            self._provides_map = dict(provides)
            self.breaks = []
            self.conflicts = []
            self.alt_depends = []
            self.alt_pre_depends = []
            self.recommends = []
            self.constraints_satisfied = True
        def __getitem__(self, k): return self._fields[k]
        def explicit_provides_version(self, name):
            _v = self._provides_map.get(name, '__MISSING__')
            if _v == '__MISSING__' or _v is None:
                return None
            return Version(_v)

    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt.selected_pkgs = {}
    dt.pool_extras_pkg_names = set()
    return dt, _Pkg


def _make_collision_cache(deb_drops=None, udeb_drops=None,
                          pkg_versions=None, udeb_versions=None):
    """Construct a bare Cache with just the state _verify_no_fork_collisions
    inspects.  No mirrors, no downloads — strictly a unit-test scaffold
    for the gate function.

    Args (all optional):
      deb_drops:     dict pkg_name -> list[(mirror_id, upstream_version)]
      udeb_drops:    same shape, for udeb namespace
      pkg_versions:  dict pkg_name -> [fork_version_string, ...]
      udeb_versions: same shape, for udeb namespace
    """
    import sys
    import types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    from collections import defaultdict
    c = Cache.__new__(Cache)
    c._upstream_collisions      = defaultdict(list, deb_drops or {})
    c._upstream_udeb_collisions = defaultdict(list, udeb_drops or {})
    c.package_hashtable = defaultdict(lambda: defaultdict(list))
    c.udeb_hashtable    = defaultdict(lambda: defaultdict(list))
    # Real records carry a .package field == the standalone name; the fork
    # gate now filters on it (audit #47), so the scaffold must too.
    for _n, _vers in (pkg_versions or {}).items():
        for _v in _vers:
            c.package_hashtable[_n][_v] = [types.SimpleNamespace(package=_n)]
    for _n, _vers in (udeb_versions or {}).items():
        for _v in _vers:
            c.udeb_hashtable[_n][_v] = [types.SimpleNamespace(package=_n)]
    c.error_str = ''
    return c


# Helper: build a synthetic udeb Packages stanza string (multi-record).
def _make_udeb_packages_text():
    """Return a Debian-format Packages-file content with three udeb records:
    one Priority: required, one Priority: important, one Priority: optional.
    Mirrors what the real bookworm d-i index publishes (sparse priorities)."""
    return (
        "Package: base-installer\n"
        "Source: base-installer\n"
        "Version: 1.197\n"
        "Architecture: amd64\n"
        "Maintainer: Debian Install Team <debian-boot@lists.debian.org>\n"
        "Installed-Size: 100\n"
        "Filename: pool/main/b/base-installer/base-installer_1.197_amd64.udeb\n"
        "MD5sum: 0123456789abcdef0123456789abcdef\n"
        "SHA256: " + "0" * 64 + "\n"
        "Size: 12345\n"
        "Section: debian-installer\n"
        "Priority: required\n"
        "Description: Base system installer\n"
        "\n"
        "Package: kmod-udeb\n"
        "Source: kmod\n"
        "Version: 30+20221128-1\n"
        "Architecture: amd64\n"
        "Maintainer: Debian kmod Team <pkg-kmod-devel@lists.alioth.debian.org>\n"
        "Installed-Size: 200\n"
        "Filename: pool/main/k/kmod/kmod-udeb_30+20221128-1_amd64.udeb\n"
        "MD5sum: fedcba9876543210fedcba9876543210\n"
        "SHA256: " + "1" * 64 + "\n"
        "Size: 23456\n"
        "Section: debian-installer\n"
        "Priority: important\n"
        "Description: Kernel module loader (udeb)\n"
        "\n"
        "Package: cdebconf-text-udeb\n"
        "Source: cdebconf\n"
        "Version: 0.270\n"
        "Architecture: amd64\n"
        "Maintainer: Debian Install Team <debian-boot@lists.debian.org>\n"
        "Installed-Size: 96\n"
        "Depends: cdebconf-udeb, libc6-udeb (>= 2.36), libreadline8-udeb\n"
        "Filename: pool/main/c/cdebconf/cdebconf-text-udeb_0.270_amd64.udeb\n"
        "MD5sum: aaaabbbbccccddddeeeeffffaaaabbbb\n"
        "SHA256: " + "2" * 64 + "\n"
        "Size: 24032\n"
        "Section: debian-installer\n"
        "Priority: optional\n"
        "Description: Plain text frontend for cdebconf\n"
    )


class _StubArchTable:
    """Mimic DpkgArchTable.matches_architecture — return True (compatible)
    for the host arch, False for known incompatible, None for unknown."""
    def matches_architecture(self, pkg_arch, host_arch):
        if pkg_arch == 'all':
            return True
        return pkg_arch == host_arch


class _StubTuiForProgressBar:
    """Minimum Tui surface ProgressBar needs: add_widget/del_widget/print.
    Used by _ingest_udeb_indices tests because the helper instantiates a
    real ProgressBar internally (matches the regular Packages pass)."""
    def __init__(self):
        self._next_id = 0
        self._widgets = {}
    def add_widget(self, w):
        wid = self._next_id
        self._next_id += 1
        self._widgets[wid] = w
        return wid
    def del_widget(self, wid):
        self._widgets.pop(wid, None)
    def print(self, *_a, **_kw): pass


def _with_stub_tui(fn):
    """Decorator: run a test with a stub Tui registered as the singleton.
    Restores the prior tui_instance on exit so other tests aren't disturbed."""
    def _wrapped(*args, **kwargs):
        import sys
        sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
        import tui as _tui
        _saved = _tui.tui_instance
        _tui.tui_instance = _StubTuiForProgressBar()
        try:
            return fn(*args, **kwargs)
        finally:
            _tui.tui_instance = _saved
    _wrapped.__name__ = fn.__name__
    _wrapped.__doc__ = fn.__doc__
    return _wrapped


# ─── CONF-02 phase 3: signing key gate at top of build_chroot ─────────────

def _stub_session_for_signing_gate():
    """Construct a BuildSession via object.__new__ (bypassing __init__)
    with just the attrs `_ensure_signing_key_verified` touches.  Saves
    + restores tui.tui_instance so the Prompt() construction inside the
    helper can resolve the singleton."""
    import build

    class _Cfg:
        signing_key_uid = 'Athena Build <athena@local>'
        dir_gnupg = '/tmp/athena-test-gnupg-not-real'

    sess = object.__new__(build.BuildSession)
    sess.config = _Cfg()
    sess.flags = build.BuildFlags()
    sess.tui = None

    return sess


# ─── CONF-02 phase 3: install signing keyring into chroot ─────────────────

def _stub_chroot_mixin_for_keyring_test(*, dir_gnupg, dir_chroot):
    """Construct a _ChrootMixin instance wired only enough to exercise
    _install_signing_keyring.  Bypass __init__ — set the four attrs the
    method touches: _config (with dir_gnupg), _dir_chroot, _password."""
    import chroot
    class _Cfg:
        pass
    cfg = _Cfg()
    cfg.dir_gnupg = dir_gnupg
    inst = chroot._ChrootMixin.__new__(chroot._ChrootMixin)
    inst._config = cfg
    inst._dir_chroot = dir_chroot
    inst._password = 'test-password-not-real'
    return inst


# ─── CONF-02: network apt source (sources.list.d/athena.list) ────────────────

def _stub_chroot_mixin_for_apt_source(*, build_codename='thor',
                                       dir_config=None):
    """_ChrootMixin wired only enough to exercise _write_athena_apt_sources:
    a _config carrying build_codename (and dir_config so the MIRROR-01
    mirror lookup can find state files), and a recording stub in place
    of _write_chroot_file."""
    import chroot
    class _Cfg:
        pass
    cfg = _Cfg()
    cfg.build_codename = build_codename
    # MIRROR-01 Phase 5: mirror.list_mirrors walks config.dir_config for
    # mirror.<name>.state files.  Default to a fresh tmpdir so tests that
    # opt out of mirrors see an empty mirror set.
    cfg.dir_config = dir_config or tempfile.mkdtemp(prefix='_chroot_cfg_')
    inst = chroot._ChrootMixin.__new__(chroot._ChrootMixin)
    inst._config = cfg
    inst._dir_chroot = '/tmp/fake-chroot'
    inst._password = 'test-password-not-real'
    _writes = []
    inst._write_chroot_file = lambda path, content: _writes.append((path, content))
    return inst, _writes


# ─── UX-05 Path B: headless CLI backend (scripts/cli.py) ────────────────────

def _fresh_cli():
    """Construct a Cli, capturing stdout/stderr.  Returns (cli, stdout_buf,
    stderr_buf, restore) — call restore() after the test to put the
    streams + tui_instance back."""
    import io
    import tui
    from cli import Cli

    _orig_tui_instance = tui.tui_instance
    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    cli = Cli()  # registers itself as tui.tui_instance + binds logging

    def restore():
        sys.stdout = _orig_stdout
        sys.stderr = _orig_stderr
        tui.tui_instance = _orig_tui_instance

    return cli, sys.stdout, sys.stderr, restore


# ─────────────────────────────────────────────────────────────────────────────
# tui (event-dispatcher TUI; was tui_v2 during the side-by-side phase)
# ─────────────────────────────────────────────────────────────────────────────

def _v2_fake_renderer():
    """Stub Renderer for Dispatcher tests — counts renders, records last
    state snapshot.  Width/content_rows fixed at 80×20 so tests have
    predictable wrap arithmetic."""
    class _R:
        def __init__(self):
            self.renders = 0
            self.last = None
        def render(self, state):
            self.renders += 1
            self.last = state
        def width(self): return 80
        def content_rows(self): return 20
    return _R()


# ─────────────────────────────────────────────────────────────────────────────
# COMP-06 — package-set selector
# ─────────────────────────────────────────────────────────────────────────────

def _select_controller(tmp_pkglist_body: str):
    """Build a SelectPackages against a tmp pkg.list + a fake cache +
    a recording fake Tui.  Returns (controller, fake_tui, path)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import select_packages

    d = tempfile.mkdtemp()
    path = os.path.join(d, 'pkg.list')
    with open(path, 'w') as f:
        f.write(tmp_pkglist_body)

    class _FakePkg(dict):
        def __init__(self, size, desc):
            super().__init__()
            self['Installed-Size'] = str(size)
            self['Description'] = desc
            self.depends = []
            self.pre_depends = []

    class _FakeCache:
        def get_packages(self, name, version=None, constraint=''):
            # Every queried name resolves to a 100 KB stub.
            return [_FakePkg(100, f'desc for {name}')]

    class _FakeTui:
        COLOR_HIGHLIGHT = 5
        COLOR_INFO = 7
        def __init__(self):
            self.buffers = {}
            self.added = []
            self.removed = []
            self.activated = []
            self.key_handler = None
            self.prints = []
            self.cleared = False
        def add_tab(self, name): self.added.append(name)
        def remove_tab(self, name): self.removed.append(name)
        def activate_tab(self, name): self.activated.append(name)
        def set_tab_buffer(self, name, rows): self.buffers[name] = rows
        def set_tab_key_handler(self, name, fn): self.key_handler = fn
        def clear_tab_key_handler(self): self.cleared = True
        def viewport_rows(self): return 20
        def attr_reverse(self): return 1 << 18
        def attr_color(self, idx): return idx << 8
        def print(self, msg, attr=None): self.prints.append(msg)
        def prompt(self, message, masked=False, keymode=False): return ''

    from types import SimpleNamespace
    cfg = SimpleNamespace(pkglist_path=path, poollist_path=path + ".pool")
    ctl = select_packages.SelectPackages(cfg, _FakeCache(), _FakeTui())
    return ctl, ctl._tui, path


# ─────────────────────────────────────────────────────────────────────────────
# FORK-01 Step 2 — fork mirror generation + cache integration
# ─────────────────────────────────────────────────────────────────────────────

def _setup_fork_test_tmpdir(tmp: str, with_pkg: bool = True) -> object:
    """Build a minimal pseudo-BuildConfig pointing at a tmpdir-scoped fork tree.
    Optionally populates fork/source/athena-installer-data/ with a valid
    debian/ layout so generate_fork_mirror has real input to chew on."""
    from types import SimpleNamespace
    bc = SimpleNamespace()
    bc.working_dir = tmp
    bc.dir_fork = os.path.join(tmp, 'fork')
    bc.dir_fork_source = os.path.join(bc.dir_fork, 'source')
    bc.dir_fork_source_repo = os.path.join(bc.dir_fork_source, 'repo')
    # Added for fork-mirror invalidation: _wipe_fork_pkg_outputs reaches
    # into dir_source, dir_repo, dir_log/build for derived artifacts.
    bc.dir_source = os.path.join(tmp, 'source')
    bc.dir_repo   = os.path.join(tmp, 'repo')
    # CONF-01 Stage D: unified apt-repo layout — paths nested under
    # dists/<codename>/.  The fork tests that plant artifacts in
    # repo target dir_repo_main (post-Stage D the binary-amd64/
    # leaf); the rest (doc/dbgsym/tests) created for completeness so
    # fork_mirror's all_deb_dirs() walk has all expected paths.
    bc.dir_repo_main        = os.path.join(bc.dir_repo, 'dists', 'thor', 'main', 'binary-amd64')
    bc.dir_repo_main_udeb   = os.path.join(bc.dir_repo, 'dists', 'thor', 'main', 'debian-installer', 'binary-amd64')
    bc.dir_repo_main_source = os.path.join(bc.dir_repo, 'dists', 'thor', 'main', 'source')
    bc.dir_repo_doc         = os.path.join(bc.dir_repo, 'dists', 'thor', 'doc', 'binary-amd64')
    bc.dir_repo_dbgsym      = os.path.join(bc.dir_repo, 'dists', 'thor-debug', 'main', 'binary-amd64')
    bc.dir_repo_tests       = os.path.join(bc.dir_repo, 'dists', 'thor', 'tests', 'binary-amd64')
    # Stub the helper fork_mirror uses to walk all .deb dirs.
    bc.all_deb_dirs = lambda: [
        bc.dir_repo_main, bc.dir_repo_main_udeb, bc.dir_repo_doc,
        bc.dir_repo_dbgsym, bc.dir_repo_tests,
    ]
    bc.dir_log    = os.path.join(tmp, 'log')
    bc.build_codename = 'thor'
    bc.arch = 'amd64'
    os.makedirs(bc.dir_fork, exist_ok=True)
    os.makedirs(bc.dir_fork_source, exist_ok=True)
    os.makedirs(bc.dir_fork_source_repo, exist_ok=True)
    os.makedirs(bc.dir_source, exist_ok=True)
    os.makedirs(bc.dir_repo,   exist_ok=True)
    for _sub in (bc.dir_repo_main, bc.dir_repo_main_udeb,
                 bc.dir_repo_main_source, bc.dir_repo_doc,
                 bc.dir_repo_dbgsym, bc.dir_repo_tests):
        os.makedirs(_sub, exist_ok=True)
    os.makedirs(os.path.join(bc.dir_log, 'build'), exist_ok=True)
    # CONF-10: write a permissive identity-allowlist so the fork_mirror
    # gate doesn't fire on test fixtures (which contain debian/copyright
    # boilerplate referencing debian.org).  Tests here exercise
    # fork_mirror behavior, not the identity audit itself — that lives
    # in dedicated test_identity_scan_* tests.
    os.makedirs(os.path.join(tmp, 'audit'), exist_ok=True)
    with open(os.path.join(tmp, 'audit', 'identity-allowlist'), 'w') as fh:
        fh.write('*\t*\ttest fixture — absorbs every token\n')
    if with_pkg:
        _pkg_dir = os.path.join(bc.dir_fork_source, 'athena-installer-data')
        os.makedirs(os.path.join(_pkg_dir, 'debian'), exist_ok=True)
        with open(os.path.join(_pkg_dir, 'debian', 'changelog'), 'w') as fh:
            fh.write('athena-installer-data (1.0.0) thor; urgency=low\n\n'
                     '  * test fixture\n\n'
                     ' -- Test <test@local>  Sat, 16 May 2026 12:00:00 +0000\n')
        with open(os.path.join(_pkg_dir, 'debian', 'control'), 'w') as fh:
            fh.write(textwrap.dedent("""\
                Source: athena-installer-data
                Section: debian-installer
                Priority: optional
                Maintainer: Test <test@local>
                Build-Depends: debhelper-compat (= 13)
                Standards-Version: 4.6.0

                Package: athena-installer-data
                Package-Type: udeb
                Section: debian-installer
                Architecture: all
                Description: test udeb
                 Long description body for the test udeb.
                """))
        with open(os.path.join(_pkg_dir, 'debian', 'rules'), 'w') as fh:
            fh.write('#!/usr/bin/make -f\n%:\n\tdh $@\n')
        os.chmod(os.path.join(_pkg_dir, 'debian', 'rules'), 0o755)
        with open(os.path.join(_pkg_dir, 'debian', 'copyright'), 'w') as fh:
            fh.write('Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n')
    return bc


def _stub_tui():
    """Inject minimal stubs so fork_mirror et al can call tui.console.print
    AND construct ProgressBar/Spinner without a real Tui."""
    import tui as _tui
    class _Console:
        def __init__(self): self.out = []
        def print(self, *a, **kw): self.out.append(' '.join(str(x) for x in a))
    class _FakeTui:
        def add_widget(self, w): return 0
        def del_widget(self, wid): pass
        def print(self, *a, **kw): pass
        # logger handlers route Tui.ERROR/WARNING/INFO/DEBUG; stub them.
        def ERROR(self, msg): pass
        def WARNING(self, msg): pass
        def INFO(self, msg): pass
        def DEBUG(self, msg): pass
    _tui.console = _Console()
    _tui.tui_instance = _FakeTui()
    return _tui.console


# ─── CONF-11: fork-tree internal completeness audit ─────────────────────────

def _conf11_make_fork_pkg(root: str, makefile: str = '', rules: str = '',
                          extra_files: 'list[str] | None' = None,
                          binaries: 'list[str] | None' = None) -> str:
    """Build a fake fork/source/<pkg>/ tree for audit testing.
    Returns the pkg_dir.  `extra_files` are touched relative to pkg_dir.
    `binaries` lists the Package: stanzas to write to debian/control."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _pkg_dir = os.path.join(root, 'testpkg')
    os.makedirs(os.path.join(_pkg_dir, 'debian'), exist_ok=True)
    _ctrl_lines = ['Source: testpkg', 'Maintainer: x', '']
    for _b in (binaries or ['testpkg']):
        _ctrl_lines += [f'Package: {_b}', 'Architecture: all',
                        'Description: x', '']
    with open(os.path.join(_pkg_dir, 'debian', 'control'), 'w') as _fh:
        _fh.write('\n'.join(_ctrl_lines))
    if makefile:
        with open(os.path.join(_pkg_dir, 'Makefile'), 'w') as _fh:
            _fh.write(makefile)
    if rules:
        with open(os.path.join(_pkg_dir, 'debian', 'rules'), 'w') as _fh:
            _fh.write(rules)
    for _rel in (extra_files or []):
        _abs = os.path.join(_pkg_dir, _rel)
        os.makedirs(os.path.dirname(_abs), exist_ok=True)
        with open(_abs, 'w') as _fh:
            _fh.write('content\n')
    return _pkg_dir


def _plant_adopted_fork(bc, pkg, fn, *, pulled_from):
    """Helper: place an adopted-fork build record (+/- pulled_from) and the
    binary on disk where deb_dest_for_filename expects it; remove any
    persisted tree-hash (fresh-peer condition).  Returns (dst_path, hash_file)."""
    import utils as _u
    _hash_file = os.path.join(bc.dir_fork_source_repo, pkg + '.tree-hash')
    if os.path.exists(_hash_file):
        os.remove(_hash_file)
    _dst = bc.dir_repo_main          # in all_deb_dirs(); where the wipe looks
    os.makedirs(_dst, exist_ok=True)
    with open(os.path.join(_dst, fn), 'w') as fh:
        fh.write('adopted bytes')
    _rec = _u.new_build_record(package=pkg, intended_version='1.0.0',
                               patch_set_hash='')
    _rec.update({'phase': 'done', 'outputs': [fn], 'component': 'main'})
    if pulled_from:
        _rec['pulled_from'] = {'mirror_name': 'test-mirror',
                               'owner_builder': 'origin'}
    _u.write_build_record(os.path.join(bc.dir_log, 'build'), _rec)
    return os.path.join(_dst, fn), _hash_file


# ─────────────────────────────────────────────────────────────────────────────
# repo reload — dep-hash classification + sidecar plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _make_minimal_pkg(tmp_root: str, source: str, version: str,
                      binaries: list, depends_main: str = '') -> str:
    """Create a minimal fork pkg dir at tmp_root/<source>/ with changelog
    + control.  Returns the pkg dir.  `binaries` is a list of Package:
    stanza names (first one inherits Depends from `depends_main`)."""
    _pkg_dir = os.path.join(tmp_root, source)
    os.makedirs(os.path.join(_pkg_dir, 'debian'), exist_ok=True)
    with open(os.path.join(_pkg_dir, 'debian', 'changelog'), 'w') as fh:
        fh.write(f'{source} ({version}) thor; urgency=low\n\n'
                 '  * fixture\n\n'
                 ' -- Test <t@local>  Sat, 16 May 2026 12:00:00 +0000\n')
    _control = f'Source: {source}\nMaintainer: Test <t@local>\n\n'
    for _i, _bin in enumerate(binaries):
        _control += f'Package: {_bin}\nArchitecture: all\n'
        if _i == 0 and depends_main:
            _control += f'Depends: {depends_main}\n'
        _control += 'Description: x\n .\n\n'
    with open(os.path.join(_pkg_dir, 'debian', 'control'), 'w') as fh:
        fh.write(_control)
    return _pkg_dir


def _closure_ledger_entry(filename, sha, size, version, pkg, arch='amd64',
                          component='main'):
    return {'filename': filename, 'sha256': sha, 'size': size,
            'version': version, 'component': component, 'package': pkg,
            'arch': arch}


def _build_minimal_deb(path: str, package: str, version: str,
                        architecture: str = 'amd64',
                        depends: str = '') -> None:
    """Write a minimal .deb at `path` with the given control fields.

    Uses python-debian's debfile is NOT writable; we shell out to
    dpkg-deb -b on a synth tree.  Skipped (raises) if dpkg-deb isn't
    available in the test environment.
    """
    import subprocess
    _tmp = path + '.tree'
    os.makedirs(os.path.join(_tmp, 'DEBIAN'), exist_ok=True)
    _ctrl = (
        f'Package: {package}\n'
        f'Version: {version}\n'
        f'Architecture: {architecture}\n'
        'Maintainer: Test <t@local>\n'
        'Description: synth fixture\n'
        ' .\n'
    )
    if depends:
        _ctrl += f'Depends: {depends}\n'
    with open(os.path.join(_tmp, 'DEBIAN', 'control'), 'w') as fh:
        fh.write(_ctrl)
    # dpkg-deb refuses to build if mode bits are loose
    os.chmod(os.path.join(_tmp, 'DEBIAN'), 0o755)
    _r = subprocess.run(
        ['dpkg-deb', '--build', '--root-owner-group', _tmp, path],
        capture_output=True, text=True,
    )
    if _r.returncode != 0:
        raise RuntimeError(f"dpkg-deb failed: {_r.stderr}")


def _make_buildcontainer_stub(cache=None, buildlog='/tmp/log', repo='/tmp/repo'):
    """Construct a BuildContainer without running __init__ (which needs
    docker etc).  Returns an instance with just the attributes
    verify_pkg_artifact / check_build read."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer
    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc.buildlog_path = buildlog
    _bc.repo_path = repo
    _bc.cache = cache
    return _bc


def _advertised_and_dispatched(method):
    """(advertised, dispatched) sets for a command-umbrella method: advertised
    = the first word of each help-table dict key; dispatched = every string
    `action` is compared against (== or `in (...)`).  AST-based, so it's robust
    to formatting."""
    import ast
    import inspect
    import textwrap
    _tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    _adv, _disp = set(), set()
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.Dict):
            for _k in _node.keys:
                if isinstance(_k, ast.Constant) and isinstance(_k.value, str):
                    _parts = _k.value.split()
                    if _parts and not _parts[0].startswith('<'):
                        _adv.add(_parts[0])
        if (isinstance(_node, ast.Compare)
                and isinstance(_node.left, ast.Name)
                and _node.left.id == 'action'):
            # ast.Compare guarantees len(ops) == len(comparators)
            for _op, _comp in zip(_node.ops, _node.comparators, strict=True):
                if isinstance(_op, ast.Eq) and isinstance(_comp, ast.Constant):
                    if isinstance(_comp.value, str):
                        _disp.add(_comp.value)
                elif isinstance(_op, ast.In) and isinstance(
                        _comp, (ast.Tuple, ast.List, ast.Set)):
                    for _e in _comp.elts:
                        if isinstance(_e, ast.Constant) and isinstance(
                                _e.value, str):
                            _disp.add(_e.value)
    return _adv, _disp


# ─────────────────────────────────────────────────────────────────────────────
# TEST-05: offline Cache fixture
#
# Drives Cache.__build_cache against on-disk Packages/Sources blobs in a
# tempdir, bypassing __init__'s mirror downloads + GPG verification.  Used
# by TEST-02 (parse_dependency) and TEST-10 (collision-gate integration).
#
# Two helpers:
#   _make_offline_mirror(_id, baseurl='file:///tmp/x', ...) — bare Mirror
#     instance; default file:// scheme so with_snapshot() is a no-op.
#   _make_offline_cache(tmpdir, packages={'main': <text>, ...},
#                       sources={...}, udeb_packages={...}) — full Cache
#     with hashtables populated.  No real network.
# ─────────────────────────────────────────────────────────────────────────────

def _make_offline_mirror(_id: str = 'main',
                         baseurl: str = 'file:///tmp/offline',
                         baseid: str = 'debian',
                         release: str = 'bookworm',
                         suffix: str = '',
                         component: str = 'main',
                         arch: str = 'amd64'):
    from utils import Mirror
    return Mirror(_id, baseurl, baseid, release, suffix, component, arch)


def _make_offline_cache(tmpdir: str,
                        packages=None,
                        sources=None,
                        udeb_packages=None,
                        arch: str = 'amd64',
                        fork_id: str = 'fork'):
    """Construct a real Cache against on-disk Packages/Sources blobs.

    Each input dict is keyed by mirror id and maps to the file content.
    Mirror order in the Cache is the iteration order of `packages`; the
    fork-supersede check looks for mirror_id == 'fork' (case-sensitive).

    Returns the populated Cache instance with .is_valid==True; caller
    can inspect package_hashtable, udeb_hashtable, etc."""
    import os
    from collections import defaultdict
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    from debian.debian_support import DpkgArchTable

    packages      = packages      or {}
    sources       = sources       or {}
    udeb_packages = udeb_packages or {}

    c = Cache.__new__(Cache)
    c._config_valid = False
    c.error_str = ''
    c._arch_table = DpkgArchTable.load_arch_table()
    c.cache_dir = tmpdir
    c._security_keyring = ''
    c._security_work_dir = tmpdir
    c._security_disabled = True
    c.snapshot_ts = None
    c.mirrors = [
        _make_offline_mirror(_id, baseurl=f'file://{tmpdir}/{_id}')
        for _id in packages
    ]
    c._compression_openers = []
    c.required = []
    c.important = []
    c.udeb_required = []
    c.udeb_important = []
    c.skip_src = []
    c.package_hashtable = defaultdict(lambda: defaultdict(list))
    c.source_hashtable  = defaultdict(list)
    c.udeb_hashtable    = defaultdict(lambda: defaultdict(list))
    c._fork_pkg_names  = set()
    c._fork_udeb_names = set()
    c._fork_src_names  = set()
    c._upstream_collisions      = defaultdict(list)
    c._upstream_udeb_collisions = defaultdict(list)
    c._upstream_source_collisions = defaultdict(list)

    # Write Packages + Sources blobs and stamp mirror_cache_files /
    # mirror_udeb_cache_files (these are what __build_cache /
    # _ingest_udeb_indices read).
    c.mirror_cache_files = {}
    c.mirror_udeb_cache_files = {}
    for _id, _body in packages.items():
        _mdir = os.path.join(tmpdir, _id)
        os.makedirs(_mdir, exist_ok=True)
        _ppath = os.path.join(_mdir, 'Packages')
        _spath = os.path.join(_mdir, 'Sources')
        with open(_ppath, 'w') as fh: fh.write(_body)
        with open(_spath, 'w') as fh: fh.write(sources.get(_id, ''))
        c.mirror_cache_files[_id] = {'Packages': _ppath, 'Sources': _spath}
        if _id in udeb_packages:
            _upath = os.path.join(_mdir, 'Packages-udeb')
            with open(_upath, 'w') as fh: fh.write(udeb_packages[_id])
            c.mirror_udeb_cache_files[_id] = _upath

    # Run the real bound method.  Name-mangled because __build_cache is
    # double-underscored.  We exercise the same path Cache.__init__ does
    # so any future refactor of __build_cache is covered by these tests.
    _ok = c._Cache__build_cache(arch)  # type: ignore[attr-defined]
    if udeb_packages:
        c._ingest_udeb_indices(arch)
    c._config_valid = _ok
    return c


# ─────────────────────────────────────────────────────────────────────────────
# TEST-02: DependencyTree.parse_dependency
#
# Existing coverage: lookahead reuse (sudo case), auto-pick across names
# (deb vs udeb), real-name-prefers-virtual (anacron case).
# Gaps filled below: empty-name guard, already-selected unsatisfied-version
# warning, alt-deps first-already-selected path, alt-deps fallback to first,
# recommends inclusion, cycle protection, Provides version satisfies
# constraint (Policy §7.5).
# ─────────────────────────────────────────────────────────────────────────────


def _make_parse_dep_tree(cache_pkgs: dict, *, select_recommended: bool = False,
                         auto_pick_highest: bool = False):
    """Construct a bare DependencyTree wired to a fake cache shaped from
    a {pkg_name: [_FakePkg, ...]} dict.  Returns the tree."""
    import sys
    from collections import defaultdict
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree

    class _Cache:
        def __init__(self, pkgs):
            self._pkgs = pkgs
            self.skip_src = []
        def get_packages(self, name, ver=None, op=''):
            return list(self._pkgs.get(name, []))

    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt._DependencyTree__cache = _Cache(cache_pkgs)         # type: ignore[attr-defined]
    dt._DependencyTree__lookahead = defaultdict(dict)      # type: ignore[attr-defined]
    dt._DependencyTree__recommended = select_recommended   # type: ignore[attr-defined]
    dt.selected_pkgs = {}
    dt.selected_srcs = {}
    dt.extras_pkg_names = set()
    dt.live_exclusive_pkg_names = set()
    dt.installer_exclusive_pkg_names = set()
    dt._auto_pick_highest_when_ambiguous = auto_pick_highest
    dt.arch = 'amd64'
    dt.build_profiles = frozenset()
    return dt


class _ParseDepPkg:
    """Minimal Package surface that mimics the attributes parse_dependency
    reads.  Distinct from the module-level _FakePkg (used by the
    pull_recommends / installer_chroot tests) — different shape, kept
    separate to avoid namespace collisions on shared keyword args."""
    def __init__(self, name, ver='1.0', *, depends=None, pre_depends=None,
                 recommends=None, alt_depends=None, alt_pre_depends=None,
                 provides=None):
        self._fields = {'Package': name, 'Version': ver}
        self.package = name
        self.version = ver
        self.depends      = list(depends      or [])
        self.pre_depends  = list(pre_depends  or [])
        self.recommends   = list(recommends   or [])
        self.alt_depends  = list(alt_depends  or [])
        self.alt_pre_depends = list(alt_pre_depends or [])
        self.conflicts    = []
        self.breaks       = []
        self.replaces     = []
        self._provides    = list(provides     or [])
        self.depends_on   = []
        self.depended_by  = []
    def __getitem__(self, k): return self._fields[k]
    def get_provides(self): return list(self._provides)
    def add_constraint(self, v, o): pass


def _conf11_drift_pkg_stanzas(version: str) -> str:
    """Build a Packages stanza for athena-thing at the given version."""
    return (
        "Package: athena-thing\n"
        f"Version: {version}\n"
        "Architecture: all\n"
        f"Filename: pool/main/a/athena-thing/athena-thing_{version}_all.deb\n"
        "Size: 100\n"
        "SHA256: " + ("a" * 64) + "\n"
    )


def _conf11_drift_src_stanzas(version: str, sha_seed: str = 'a') -> str:
    """Build a Sources stanza for athena-thing at the given version."""
    return (
        "Package: athena-thing\n"
        "Binary: athena-thing\n"
        f"Version: {version}\n"
        "Architecture: any\n"
        "Directory: pool/main/a/athena-thing\n"
        "Checksums-Sha256:\n"
        " " + (sha_seed * 64) + f" 100 athena-thing_{version}.dsc\n"
    )


def _remote_agent_spawn(bundle, build_cmd, *, hang=30, token='secret'):
    """Start scripts/remote_agent.py as a detached subprocess against a
    --build-cmd (no docker needed); return (proc, port)."""
    import subprocess as _sub
    import time
    _tf = os.path.join(bundle, 'token')
    with open(_tf, 'w') as _fh:
        _fh.write(token)
    _p = _sub.Popen(
        [sys.executable, os.path.join(_ROOT, 'scripts', 'remote_agent.py'),
         '--bundle', bundle, '--token-file', _tf, '--port', '0',
         '--hang-secs', str(hang), '--build-cmd', build_cmd])
    _portf = os.path.join(bundle, 'agent.port')
    for _ in range(200):
        if os.path.exists(_portf):
            _v = open(_portf).read().strip()
            if _v:
                return _p, int(_v)
        time.sleep(0.05)
    _p.kill()
    raise AssertionError('remote_agent did not publish its port')


def _remote_agent_req(port, path, method='GET', token='secret'):
    """One token-authed request to the agent; returns (status, body, headers)."""
    import urllib.request as _u
    _req = _u.Request(f'http://127.0.0.1:{port}{path}', method=method)
    if token is not None:
        _req.add_header('X-Athena-Token', token)
    with _u.urlopen(_req, timeout=5) as _r:
        return _r.status, _r.read(), dict(_r.headers)


def _remote_agent_wait_terminal(port, tries=200):
    import json as _j
    import time
    _st = {}
    for _ in range(tries):
        _st = _j.loads(_remote_agent_req(port, '/status')[1])
        if _st['phase'] in ('done', 'failed', 'timeout', 'aborted'):
            return _st
        time.sleep(0.1)
    return _st


def _run_remote_agent_fakes(out_dir, *, phase_seq):
    """Build (subprocess-fake, agent-request-fake, calls-record) for driving
    run_remote_agent without a real remote.  `phase_seq` is the sequence of
    /status phases the agent reports."""
    import types as _t
    _calls = {'run': [], 'popen': [], 'status': 0, 'logs': 0}

    class _FakeProc:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def _run(argv, **k):
        _calls['run'].append(list(argv))
        _joined = ' '.join(argv)
        if 'agent.port' in _joined:
            return _t.SimpleNamespace(returncode=0, stdout='54321\n', stderr='')
        if 'out/*.deb' in _joined:
            open(os.path.join(out_dir, 'adduser_1_amd64.deb'), 'w').close()
        return _t.SimpleNamespace(returncode=0, stdout='', stderr='')

    def _popen(argv, **k):
        _calls['popen'].append(list(argv))
        return _FakeProc()

    _fake_sp = _t.SimpleNamespace(
        run=_run, Popen=_popen, DEVNULL=-3, PIPE=-1, STDOUT=-2,
        SubprocessError=Exception)

    def _req(port, path, token, method='GET', timeout=10):
        if path == '/status':
            _i = min(_calls['status'], len(phase_seq) - 1)
            _calls['status'] += 1
            _phase = phase_seq[_i]
            _outs = '["adduser_1_amd64.deb"]' if _phase == 'done' else '[]'
            _code = '0' if _phase == 'done' else 'null'
            return (200, (f'{{"phase":"{_phase}","outputs":{_outs},'
                          f'"exit_code":{_code}}}').encode(), {})
        if path.startswith('/logs'):
            _calls['logs'] += 1
            if _calls['logs'] == 1:
                return (200, b'building adduser...\n', {'X-Log-Next': '20'})
            return (200, b'', {'X-Log-Next': '20'})
        return (200, b'{}', {})        # /abort /shutdown

    return _fake_sp, _req, _calls


# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 decomposition — local manifest, external on/off, update-mode plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _stub_signed_manifest_gpg(repo_audit_module, signing_home_dir):
    """Test helper: monkey-patch repo_audit's subprocess.run so gpg
    --detach-sign succeeds (writing a stub .sig) and gpg --verify returns 0.
    Returns the original subprocess.run so the test can restore it."""
    import subprocess as _real_sp
    _orig_run = repo_audit_module.subprocess.run

    def _fake_run(argv, **kw):
        if isinstance(argv, list) and argv and argv[0] == 'gpg':
            if '--detach-sign' in argv:
                _out = argv[argv.index('-o') + 1]
                with open(_out, 'w') as _fh:
                    _fh.write('-----BEGIN PGP SIGNATURE-----\nstub\n')
                return _real_sp.CompletedProcess(argv, 0, b'', b'')
            if '--verify' in argv:
                return _real_sp.CompletedProcess(argv, 0, b'', b'')
        return _orig_run(argv, **kw)

    repo_audit_module.subprocess.run = _fake_run
    os.makedirs(signing_home_dir, exist_ok=True)
    return _orig_run


def _sbom_test_buildconfig(tmp: str) -> object:
    """Minimal BuildConfig-shaped namespace for sbom tests — only the
    attributes generate_cdx reads.  No patches dir, no snapshot, no
    container; tests that exercise those paths set them up explicitly."""
    from types import SimpleNamespace
    bc = SimpleNamespace()
    bc.build_distribution = 'Asgard'
    bc.build_version      = '1'
    bc.build_codename     = 'thor'
    bc.build_base_id      = 'asgard'
    bc.arch               = 'amd64'
    bc.dir_patch_source   = os.path.join(tmp, 'patch', 'source')
    bc.snapshot_baseurl   = ''
    # snapshot_iso_tag depends on a real BuildConfig; tests below
    # avoid that by computing out_path themselves.
    return bc


def _sbom_test_src(name: str, version: str, dsc_sha: str = ''):
    """Minimal Source-shaped object: package + version + files-dict
    (with a .dsc entry carrying the optional sha256)."""
    from types import SimpleNamespace
    s = SimpleNamespace()
    s.package = name
    s.version = version
    s.files = {
        f'{name}_{version}.dsc': {'sha256': dsc_sha, 'size': 0,
                                   'md5': '', 'path': ''},
    }
    return s


# ─── OBS-01: canonical signed build record ──────────────────────────────────


def _utils_module():
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    return _u


def _build_session_for_setget(_tmp):
    """Minimal BuildSession stub with the bits cmd_set / cmd_get touch."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from unittest.mock import MagicMock
    import build as _build_mod

    _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)

    class _Cfg:
        build_mode = 'distribution'
        include_recommends = False
        build_version = '0.1'
        build_codename = 'thor'
        build_distribution = 'Asgard'
        arch = 'amd64'
        snapshot_enabled = True
        snapshot_timestamp_config = '20260602T173733Z'
        max_parallel_builds = 3
        # _set_mode persists to config/local.conf alongside build.conf;
        # point config_path into the tmp dir so the write lands there.
        config_path = os.path.join(_tmp, 'build.conf')
        dir_config = _tmp                       # mirror.conf / local.conf live here
        dir_log = os.path.join(_tmp, 'log')     # HMAC key dir
        # Registered federation peer — the default context so `set mode build`
        # passes the Phase-3 gate (first-system / unregistered refusals have
        # their own dedicated tests).
        system_role = 'federation'
    _sess.config = _Cfg()
    _sess.flags = MagicMock(dep_check_ready=True)
    import mirror as _mreg
    _mreg.set_registration(_sess.config, 'origin', 'bid-1')
    return _sess, _build_mod


# ─────────────────────────────────────────────────────────────────────────────
# COORD-01 — multi-builder coord modules
# ─────────────────────────────────────────────────────────────────────────────


def _coord_modules():
    """Lazy import of coord submodules.  Returns (schema, identity,
    store, policy, head, reconcile, transport, publish).  Tests skip
    cleanly if openssl is unavailable (Ed25519-backed identity)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.schema as _s
    import coord.identity as _i
    import coord.store as _st
    import coord.policy as _p
    import coord.head as _h
    import coord.reconcile as _r
    import coord.transport as _t
    import coord.publish as _pub
    return _s, _i, _st, _p, _h, _r, _t, _pub


def _openssl_available() -> bool:
    """True iff openssl Ed25519 is functional on this host.  Cached
    after first call so test runs don't spawn N subprocesses."""
    import shutil
    import subprocess
    if shutil.which('openssl') is None:
        return False
    _r = subprocess.run(
        ['openssl', 'list', '-public-key-algorithms'],
        capture_output=True, text=True)
    return _r.returncode == 0 and 'ED25519' in (_r.stdout or '').upper()


def _new_pending_claim(builder: str, package: str, filename: str,
                       version: str, sha: str = 'f' * 64) -> dict:
    """Compact builder for filter_pending_by_ownership tests."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.schema as _schema
    return _schema.new_claim(
        builder=builder, seq=0, package=package,
        intended_version=version, built_version=version,
        filename=filename, sha256=sha, size=1,
        snapshot='S', built_at='T',
        claim_state=_schema.CLAIM_STATE_PENDING,
    )


def _write_clearsigned_inrelease(path: str, sha256_block: 'list[tuple[str, int, str]]') -> None:
    """Build a minimal clearsigned-format InRelease at `path` carrying
    the given SHA256 block.  Not actually GPG-signed (audit doesn't
    re-verify; it hashes raw bytes), but the deb822 parser accepts
    the clearsigned wrapper either way."""
    _body = [
        "Architectures: amd64",
        "Codename: thor",
        "Components: main",
        "Date: Fri, 05 Jun 2026 00:00:00 +0000",
        "Suite: thor",
        "SHA256:",
    ]
    for _sha, _size, _name in sha256_block:
        _body.append(f" {_sha} {_size:>9} {_name}")
    _content = "\n".join([
        "-----BEGIN PGP SIGNED MESSAGE-----",
        "Hash: SHA512",
        "",
        *_body,
        "",
        "-----BEGIN PGP SIGNATURE-----",
        "iQfake==",
        "-----END PGP SIGNATURE-----",
        "",
    ])
    with open(path, 'w') as _fh:
        _fh.write(_content)


def _mirror_module():
    """Lazy import of scripts/mirror.py."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror
    return mirror


def _cmd_mirror_add_session(tmp_dir, codename='thor', distribution='Asgard',
                             base_pin='20260602T000000Z'):
    """Build a minimal BuildSession + monkey-patch console.print so
    cmd_mirror_add tests can capture output without curses.  Returns
    (sess, restore_fn).  `restore_fn()` puts console.print back."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build as _build
    _sess = _build.BuildSession.__new__(_build.BuildSession)
    _cfg_dir = os.path.join(tmp_dir, 'config')
    _cache_dir = os.path.join(tmp_dir, 'cache')
    _log_dir = os.path.join(tmp_dir, 'log')
    os.makedirs(_cfg_dir, exist_ok=True)
    os.makedirs(_cache_dir, exist_ok=True)
    os.makedirs(_log_dir, exist_ok=True)

    class _Cfg:
        dir_config = _cfg_dir
        dir_cache = _cache_dir
        dir_log = _log_dir
        dir_gnupg = os.path.join(tmp_dir, 'gnupg')
        dir_coord = os.path.join(tmp_dir, 'coord')
        dir_repo = os.path.join(tmp_dir, 'repo')
        build_codename = codename
        build_distribution = distribution
        build_base_id = distribution.lower()
    _sess.config = _Cfg()
    _sess._snapshot_current = lambda: base_pin     # type: ignore[method-assign]
    # Stub the reconcile-neighbours fan-out so tests don't reach for
    # network primitives; record the call for assertion.
    _sess._reconcile_called = 0
    def _stub_reconcile(*_a, **_kw):
        _sess._reconcile_called += 1
        return True
    _sess.cmd_mirror_reconcile_neighbours = _stub_reconcile  # type: ignore[method-assign]
    # Stage 3: the prepared-mirror gate probes mirror-info.json over HTTP.
    # Default the stub to "prepared" so the existing add scenarios keep
    # testing their own behaviour; the gate itself has a dedicated test.
    _sess._mirror_is_prepared = lambda _url: (  # type: ignore[method-assign]
        True, '2026-06-13T00:00:00Z')

    _sess.lines: 'list[str]' = []
    _orig = _build.console.print
    _build.console.print = lambda *a, **k: _sess.lines.append(
        ' '.join(str(x) for x in a))
    return _sess, lambda: setattr(_build.console, 'print', _orig)


def _patch_mirror_add_primitives(*, sidecar_head_return,
                                  discover_peers_return=None):
    """unittest.mock.patch context-manager bundle that stubs every
    network/sign primitive cmd_mirror_add reaches for.  Yields when
    every patch is active."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import mirror as _mirror
    import signing as _signing
    from unittest.mock import patch
    from contextlib import ExitStack
    _stack = ExitStack()
    _stack.enter_context(patch.object(
        _signing, 'verify_key',
        return_value=(True, 'mock-key-verified')))
    _stack.enter_context(patch.object(
        _signing, 'signing_home',
        return_value='/fake/gpg'))
    _stack.enter_context(patch.object(
        _mirror, 'probe_dns_and_tcp',
        return_value=(True, 'mock-reachable')))
    _stack.enter_context(patch.object(
        _mirror, 'probe_ssh_auth',
        return_value=(True, 'mock-ssh-auth-ok')))
    _stack.enter_context(patch.object(
        _mirror, 'probe_remote_writable',
        return_value=(True, 'mock-writable-ok')))
    _stack.enter_context(patch.object(
        _mirror, 'probe_http_inrelease',
        return_value=(True, 'mock-http-ok')))
    _stack.enter_context(patch.object(
        _mirror, 'probe_sidecar_head',
        return_value=sidecar_head_return))
    if discover_peers_return is not None:
        _stack.enter_context(patch.object(
            _mirror, 'discover_federation_peers',
            return_value=discover_peers_return))
    return _stack


# ─────────────────────────────────────────────────────────────────────────────
# MIRROR-01 Phase 2 — coord-head neighbours + federation gate + reconcile
# ─────────────────────────────────────────────────────────────────────────────


def _coord_schema_v2_modules():
    """Lazy import of coord modules for Phase 2 tests."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import coord.schema as _s
    import coord.reconcile as _r
    return _s, _r


def _sta30_publish_cfg(_td):
    """Minimal config for a remote_publish drive (STA-30 a/b tests)."""
    import os as _os

    class _Cfg:
        dir_coord = _td
        dir_coord_fetched = _os.path.join(_td, 'fetched')
        dir_coord_claims = _os.path.join(_td, 'claims')
        dir_log = _td
        dir_repo = _td
        dir_gnupg = _td
    _os.makedirs(_Cfg.dir_coord_fetched, exist_ok=True)
    _os.makedirs(_Cfg.dir_coord_claims, exist_ok=True)
    _os.makedirs(_os.path.join(_Cfg.dir_coord_fetched, 'claims'),
                 exist_ok=True)
    return _Cfg


def _mirror_pull_harness(_td, _claims, _head, _contents):
    """Shared scaffold for cmd_mirror_pull tests: a fresh-peer pool, mocked
    coord transport returning `_head` + `_claims`, and a pull_single_file that
    writes `_contents[basename]`.  Returns (_sess, _pulled, _lines, run, undo).
    Caller patches read_verified_closure_ledger separately if needed."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    import mirror as _mirror_mod
    import signing as _signing_mod
    import coord.head as _head_mod
    import coord.identity as _id_mod
    import coord.store as _store_mod
    import coord.transport as _transport_mod

    _pool = os.path.join(_td, 'pool')
    os.makedirs(_pool, exist_ok=True)
    _pulled: 'list[str]' = []

    def _fake_pull_single(*, remote_spec, local_path, ssh_key=None):
        _bn = os.path.basename(local_path)
        _pulled.append(_bn)
        with open(local_path, 'wb') as _fh:
            _fh.write(_contents.get(_bn, b'?'))
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
        (_head_mod, 'read_coord_head', lambda fetched, home: _head),
        (_id_mod, 'load_keyring', lambda d: {}),
        (_store_mod, 'read_all_claims', lambda d, k, r: _claims),
        (_transport_mod, 'pull_remote_coord', lambda **k: (True, '')),
        (_transport_mod, 'pull_single_file', _fake_pull_single),
    ]
    _saved = [(_m, _a, getattr(_m, _a)) for _m, _a, _ in _patches]
    _sess = BuildSession.__new__(BuildSession)
    _sess.config = _Cfg()
    _sess._coord_self_keys = lambda: ('us', 'priv', 'pub')
    _sess._snapshot_current = lambda: 'TS12'
    _sess._apply_canonical_config = lambda f, h: None
    _recorded: 'list' = []
    _sess._mirror_pull_write_build_records = (
        lambda n, d: _recorded.append(d))
    _lines: 'list[str]' = []
    _orig_print = build.console.print

    def _run():
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        for _m, _a, _f2 in _patches:
            setattr(_m, _a, _f2)
        return _sess.cmd_mirror_pull('m1')

    def _undo():
        for _m, _a, _orig in _saved:
            setattr(_m, _a, _orig)
        build.console.print = _orig_print

    return _sess, _pulled, _lines, _recorded, _run, _undo


# ─────────────────────────────────────────────────────────────────────────────
# SELECT-LOCK — signed selection lockfile (Chunk 1)
# ─────────────────────────────────────────────────────────────────────────────

def _selection_lock_cfg(root):
    """Minimal config stub for selection_lock — needs dir_config + dir_log,
    and log/build/ (the HMAC key lives there, shared with build records)."""
    import types
    _cfg_dir = os.path.join(root, 'config')
    _log_dir = os.path.join(root, 'log')
    os.makedirs(_cfg_dir, exist_ok=True)
    os.makedirs(os.path.join(_log_dir, 'build'), exist_ok=True)
    return types.SimpleNamespace(dir_config=_cfg_dir, dir_log=_log_dir)


def _sl_tree(pkgs, srcs, **tiers):
    """Synthetic dep-tree stub for selection_lock closure tests.
    `pkgs`: {name: real_name} (canonical iff name==real_name)."""
    import types
    return types.SimpleNamespace(
        selected_pkgs={_n: {'Package': _r} for _n, _r in pkgs.items()},
        selected_srcs=dict.fromkeys(srcs),
        extras_pkg_names=set(tiers.get('extras', ())),
        live_exclusive_pkg_names=set(tiers.get('live', ())),
        installer_exclusive_pkg_names=set(tiers.get('installer', ())),
        pool_extras_pkg_names=set(tiers.get('pool', ())),
        pkg_group_pkg_names={_g: set(_m) for _g, _m in tiers.get('groups', {}).items()},
    )


def _or_resolve_xterm_graph():
    """The documented SELECT-02 repro as a synthetic graph: xorg Depends
    `xterm | x-terminal-emulator`; gnome-terminal Provides
    x-terminal-emulator; xterm drags in libutempter0."""
    deps = {
        'xorg': [('xterm', 'x-terminal-emulator')],
        'xterm': ['libutempter0'],
        'gnome-terminal': [],
        'libutempter0': [],
    }
    provides = {'gnome-terminal': ['x-terminal-emulator']}
    return deps, provides


# ─────────────────────────────────────────────────────────────────────────────
# SURFACES-01 — per-surface composition
# ─────────────────────────────────────────────────────────────────────────────

class _SurfPkg:
    """Package stub for surfaces tests: mapping access for ['Package'] +
    dep attribute lists."""
    def __init__(self, name, depends=(), pre_depends=(), alt_depends=(),
                 recommends=()):
        self._name = name
        self.depends = [(d, '', '') for d in depends]
        self.pre_depends = [(d, '', '') for d in pre_depends]
        self.alt_depends = [[(a, '', '') for a in grp] for grp in alt_depends]
        self.alt_pre_depends = []
        self.recommends = [(r, '', '') for r in recommends]
    def __getitem__(self, key):
        assert key == 'Package'
        return self._name


def _surf_tree(pkgs, extras=()):
    """dep-tree stub: selected_pkgs {name_or_virtual: _SurfPkg} + extras."""
    import types
    return types.SimpleNamespace(selected_pkgs=pkgs,
                                 extras_pkg_names=set(extras))


def _lm_mock_cache_and_tree():
    """Shared fixture for local_mirror tests: a tiny package universe + one
    source whose Build-Depends seed a resolvable closure."""
    class _Mir:
        url = 'https://snapshot.debian.org/archive/debian/20260621T135952Z'

    class _Pkg(dict):
        def __init__(s, d):
            super().__init__(d)
            s._mirror = _Mir()

        def dump(s):
            # mimic python-debian Deb822.dump() — used by plan(include_index)
            return ''.join(f"{_k}: {_v}\n" for _k, _v in s.items())

    def _P(deps, fn, sz):
        return _Pkg({'Package': os.path.basename(fn).split('_')[0],
                     'Depends': deps, 'Pre-Depends': '', 'Filename': fn,
                     'Size': str(sz), 'SHA256': '', 'Provides': ''})

    class _Cache:
        package_hashtable = {
            'build-essential': {'1': [_P('gcc, make',
                'pool/main/b/be/build-essential_1_amd64.deb', 100)]},
            'gcc':       {'1': [_P('libc6-dev', 'pool/main/g/gcc/gcc_1_amd64.deb', 200)]},
            'make':      {'1': [_P('', 'pool/main/m/make/make_1_amd64.deb', 50)]},
            'libc6-dev': {'1': [_P('', 'pool/main/g/glibc/libc6-dev_1_amd64.deb', 300)]},
            'dpkg-dev':  {'1': [_P('', 'pool/main/d/dpkg/dpkg-dev_1_amd64.deb', 80)]},
            'debhelper': {'1': [_P('dpkg-dev', 'pool/main/d/dh/debhelper_1_amd64.deb', 60)]},
            'libfoo-dev': {'1': [_P('', 'pool/main/f/foo/libfoo-dev_1_amd64.deb', 40)]},
        }

    class _Src(dict):
        pass

    class _Dt:
        selected_srcs = {'foo': _Src({'Build-Depends': 'debhelper, libfoo-dev',
                                      'Build-Depends-Indep': '',
                                      'Build-Depends-Arch': ''})}
    return _Cache(), _Dt()

def _detach_bridge_log_handlers():
    """Remove any tui logging-bridge handlers left attached by a prior
    test.  A test that binds the headless Cli (registers tui_instance +
    binds logging) leaves the bridge on the logger tree, so every LATER
    test that deliberately drives an error path echoes alarming
    `[ERROR] [<stage>] …` lines between PASSes — noise that reads as a
    failure to anyone without context.  Only the bridge's own handler
    classes are stripped: handlers tests attach themselves (log-capture
    asserts) are untouched, and the bridge's unit tests run before this
    sweeps."""
    try:
        from tui import logging_bridge as _lb
        _classes = tuple(
            _c for _c in (getattr(_lb, '_LogTabHandler', None),
                          getattr(_lb, '_ConsoleTabHandler', None))
            if _c is not None)
    except Exception:
        return
    if not _classes:
        return
    import logging as _lg
    _loggers = [_lg.getLogger()] + [
        _l for _l in _lg.Logger.manager.loggerDict.values()
        if isinstance(_l, _lg.Logger)]
    for _logger in _loggers:
        for _h in list(_logger.handlers):
            if isinstance(_h, _classes):
                _logger.removeHandler(_h)


def run_tests(tests) -> int:
    """Run a TESTS list; print PASS/FAIL per test and the summary line.
    Returns a process exit code (0 all passed).

    Quiet by default: bridge log handlers a test leaves behind are
    detached between tests, so deliberate error-path coverage doesn't
    interleave `[ERROR]`/`[WARN]` chatter with the PASS lines.  Set
    ATHENA_TEST_VERBOSE=1 to keep the code-under-test log output
    (useful when debugging a failure)."""
    _verbose = bool(os.environ.get('ATHENA_TEST_VERBOSE'))
    failures = 0
    for t in tests:
        if not _verbose:
            _detach_bridge_log_handlers()
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 0 if failures == 0 else 1

