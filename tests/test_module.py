#!/usr/bin/env python3
"""Athena Linux build-system tests.

Single test file, append new test_<what>() functions as features land.
Run from project root:
    python3 tests/test_module.py
Exits 0 on success, 1 on any failure.

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


# ─────────────────────────────────────────────────────────────────────────────
# v0.2 step 1 — Mirror class + multi-mirror BuildConfig parsing
# ─────────────────────────────────────────────────────────────────────────────

def test_mirror_url_composition():
    from utils import Mirror
    m = Mirror(mirror_id='main', baseurl='http://deb.debian.org/',
               baseid='/debian/', release='bookworm', suffix='',
               component='main', arch='amd64')
    assert m.url == 'http://deb.debian.org/debian', m.url
    assert m.suite == 'bookworm', m.suite
    assert m.dist_url == 'http://deb.debian.org/debian/dists/bookworm/', m.dist_url
    assert m.packages_path == 'main/binary-amd64/Packages', m.packages_path
    assert m.sources_path == 'main/source/Sources', m.sources_path


def test_mirror_suite_with_suffix():
    from utils import Mirror
    m = Mirror(mirror_id='security', baseurl='http://deb.debian.org',
               baseid='debian-security', release='bookworm', suffix='-security',
               component='main', arch='amd64')
    assert m.suite == 'bookworm-security', m.suite
    assert m.url == 'http://deb.debian.org/debian-security', m.url
    assert m.dist_url == 'http://deb.debian.org/debian-security/dists/bookworm-security/', m.dist_url


def test_mirror_repr_does_not_crash():
    from utils import Mirror
    m = Mirror('updates', 'http://x', 'y', 'z', '-updates', 'main', 'amd64')
    repr(m)  # raises if broken


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
    NAME = "Test"
    CODENAME = "test"
    VERSION = "0.1"
    CHANNEL = "stable"
    MaxParallelBuilds = 1
    CONTAINER_RELEASE = bookworm

    [Base]
    BASEURL = http://deb.debian.org
    BASEID = debian
    RELEASE = bookworm
    BASEVERSION = 12.0
    {mirror_block}
    [Directories]
    Log = log
    Download = download
    Cache = cache
    Temp = tmp
    Source = source
    Build = build
    Repo = repo
    Config = config
    Patch = patch
    Image = image
    Chroot = buildroot
    Gnupg = gnupg

    [Source]
    SkipTest =
    BuildProfiles = nodoc, nocheck
    Tunneled =
    """


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


def test_package_and_source_have_mirror_field():
    """_mirror is declared on the class, not set via getattr."""
    import package
    pkg = package.Package('Package: x\nVersion: 1\nArchitecture: amd64\n')
    src = package.Source('Package: x\nVersion: 1\nDirectory: pool/main/x\n'
                         'Files:\n a 0 b\n')
    assert hasattr(pkg, '_mirror'), "Package._mirror not declared"
    assert hasattr(src, '_mirror'), "Source._mirror not declared"
    assert pkg._mirror is None
    assert src._mirror is None


def test_source_parses_security_stanza_without_files_field():
    """bookworm-security source stanzas drop the legacy MD5 'Files:' field
    and ship only 'Checksums-Sha256:'.  The Source parser must accept this
    and build self.files from the sha256 entries."""
    import package
    stanza = (
        "Package: openssh\n"
        "Version: 1:9.2p1-2+deb12u9\n"
        "Architecture: any all\n"
        "Directory: pool/updates/main/o/openssh\n"
        "Checksums-Sha256:\n"
        " d0fa1ecc55cdfc7d82db05d9cedc52ec96d0641c2cd2b283446df5d73e09534e 3327 openssh_9.2p1-2+deb12u9.dsc\n"
        " 3f66dbf1655fb45f50e1c56da62ab01218c228807b21338d634ebcdf9d71cf46 1852380 openssh_9.2p1.orig.tar.gz\n"
    )
    src = package.Source(stanza)
    assert src.isvalid, f"Source invalid: {src._err_str}"
    assert src.package == 'openssh'
    assert 'openssh_9.2p1-2+deb12u9.dsc' in src.files
    dsc = src.files['openssh_9.2p1-2+deb12u9.dsc']
    assert dsc['sha256'] == 'd0fa1ecc55cdfc7d82db05d9cedc52ec96d0641c2cd2b283446df5d73e09534e'
    assert dsc['size'] == 3327
    assert dsc['md5'] == ''  # no Files: → no MD5 available
    assert dsc['path'] == 'pool/updates/main/o/openssh/openssh_9.2p1-2+deb12u9.dsc'


def test_source_parses_main_stanza_with_both_files_and_sha256():
    """bookworm main still ships both Files: (MD5) and Checksums-Sha256:.
    The parser must populate both md5 and sha256."""
    import package
    stanza = (
        "Package: hello\n"
        "Version: 2.10-3\n"
        "Architecture: any\n"
        "Directory: pool/main/h/hello\n"
        "Files:\n"
        " aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 100 hello_2.10-3.dsc\n"
        "Checksums-Sha256:\n"
        " bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 100 hello_2.10-3.dsc\n"
    )
    src = package.Source(stanza)
    assert src.isvalid, f"Source invalid: {src._err_str}"
    f = src.files['hello_2.10-3.dsc']
    assert f['md5']    == 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    assert f['sha256'] == 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
    assert f['size']   == 100


# ─────────────────────────────────────────────────────────────────────────────
# ARCH-12 — _compute_install_batches single-pass topo sort
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
        # EXTRAS-01: chroot install filter reads dep_tree.extras_pkg_names;
        # default empty so existing tests behave as before.
        self.extras_pkg_names: set = set()
        self.extras_src_names: set = set()


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
    return bs


def test_compute_install_batches_linear_chain():
    """A→B→C produces one batch per node, in topo order (deepest first).
    All batches acyclic so needs_force=False everywhere."""
    bs = _bare_buildsystem_with_deps([
        ('A', [], ['B']),       # A depends on B
        ('B', [], ['C']),       # B depends on C
        ('C', [], []),          # C is the leaf
    ])
    batches = bs._compute_install_batches(libc_seed_set=set())
    assert batches == [(['C'], False), (['B'], False), (['A'], False)], batches


def test_compute_install_batches_independent_packages_share_a_batch():
    """A, B, C with no edges between them collapse into a single batch."""
    bs = _bare_buildsystem_with_deps([
        ('A', [], []),
        ('B', [], []),
        ('C', [], []),
    ])
    batches = bs._compute_install_batches(libc_seed_set=set())
    # Within-batch order is deterministic-sorted by the implementation.
    assert batches == [(['A', 'B', 'C'], False)], batches


def test_compute_install_batches_fan_out():
    """Common base + multiple dependents: base alone, then dependents in
    one parallel batch — proves Kahn collapses independent leaves."""
    bs = _bare_buildsystem_with_deps([
        ('base', [], []),
        ('a',    [], ['base']),
        ('b',    [], ['base']),
        ('c',    [], ['base']),
    ])
    batches = bs._compute_install_batches(libc_seed_set=set())
    assert batches == [(['base'], False), (['a', 'b', 'c'], False)], batches


def test_compute_install_batches_pre_depends_and_depends_unioned():
    """Pre-Depends and Depends edges both contribute to ordering."""
    bs = _bare_buildsystem_with_deps([
        ('A', ['B'], []),       # A pre-depends on B
        ('B', [], ['C']),       # B depends on C
        ('C', [], []),
    ])
    batches = bs._compute_install_batches(libc_seed_set=set())
    assert batches == [(['C'], False), (['B'], False), (['A'], False)], batches


def test_compute_install_batches_libc_seed_breaks_cycle():
    """The libc seed pattern: libc6 ↔ gcc-12-base — both are pre-installed
    (in libc_seed_set) so neither appears in the output and edges into
    them from other packages are dropped."""
    bs = _bare_buildsystem_with_deps([
        ('libc6',       ['gcc-12-base'], []),
        ('gcc-12-base', ['libc6'],       []),
        ('bash',        ['libc6'],       []),
    ])
    batches = bs._compute_install_batches(
        libc_seed_set={'libc6', 'gcc-12-base'}
    )
    # bash's only edge points into the seed → ready in batch 1, no force.
    # libc6 / gcc-12-base never appear (seed handled separately by caller).
    assert batches == [(['bash'], False)], batches


def test_compute_install_batches_self_dep_is_ignored():
    """Self-edges (A → A via Provides loop) must not block A's own batch."""
    bs = _bare_buildsystem_with_deps([
        ('A', [], ['A']),       # self-edge
    ])
    batches = bs._compute_install_batches(libc_seed_set=set())
    assert batches == [(['A'], False)], batches


def test_compute_install_batches_cycle_emitted_as_forced_batch():
    """Two packages with mutual Depends and no Pre-Depends within the cycle
    collapse into a single forced batch — Pre-Depends sub-splitting yields
    one sub-group containing both."""
    bs = _bare_buildsystem_with_deps([
        ('X', [], ['Y']),
        ('Y', [], ['X']),
    ])
    batches = bs._compute_install_batches(libc_seed_set=set())
    assert batches == [(['X', 'Y'], True)], batches


def test_compute_install_batches_cycle_with_pre_depends_chain_splits():
    """When a Depends-cycle batch also contains an internal Pre-Depends chain,
    the cycle is split into sub-batches by Pre-Depends order so dpkg's strict
    Pre-Depends contract holds within the cycle.  Real-world parallel on
    bookworm: the systemd ↔ systemd-sysv ↔ init Pre-Depends chain inside the
    libdevmapper/grub Depends-SCC."""
    bs = _bare_buildsystem_with_deps([
        ('A', [],         ['B']),    # Depends cycle A ↔ B
        ('B', [],         ['A']),
        ('C', ['A', 'B'], []),       # Pre-Depends on cycle members → must come after
    ])
    batches = bs._compute_install_batches(libc_seed_set=set())
    # All three are in the cycle (A↔B closes it; C joins via Pre-Depends).
    # Pre-Depends sub-split: A,B in sub-batch 1 (no in-cycle Pre-Depends);
    # C in sub-batch 2 (Pre-Depends on A and B).
    assert batches == [
        (['A', 'B'], True),
        (['C'], True),
    ], batches


def test_compute_install_batches_acyclic_then_cycle():
    """Acyclic prefix is emitted normally, cycle is split by Pre-Depends
    (here a single sub-group since the cycle has no internal Pre-Depends)."""
    bs = _bare_buildsystem_with_deps([
        ('leaf', [], []),
        ('top',  [], ['leaf']),
        ('X',    [], ['Y']),
        ('Y',    [], ['X']),
    ])
    batches = bs._compute_install_batches(libc_seed_set=set())
    assert batches == [
        (['leaf'], False),
        (['top'], False),
        (['X', 'Y'], True),
    ], batches


def test_compute_install_batches_external_deps_filtered():
    """A Depends entry that names a package not in selected (presumed
    satisfied by chroot base state, e.g. 'awk' on a host already
    providing one) must not block ordering."""
    bs = _bare_buildsystem_with_deps([
        ('A', [], ['some-package-not-in-graph']),
        ('B', [], []),
    ])
    batches = bs._compute_install_batches(libc_seed_set=set())
    # External dep dropped → A and B both ready in batch 1, no force.
    assert batches == [(['A', 'B'], False)], batches


# ─────────────────────────────────────────────────────────────────────────────
# STA-01 / SEC-01 — InRelease GPG verification
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
    gpg = gnupg.GPG(gnupghome=gen_home)
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


def test_verify_inrelease_clean_signature_passes():
    """A correctly clear-signed InRelease verifies against its keyring."""
    from utils import verify_inrelease, _GPG_VERIFIER_CACHE
    with tempfile.TemporaryDirectory() as tmp:
        signed, keyring, _ = _gen_signed_fixture(tmp)
        verify_home = os.path.join(tmp, 'verify-home')
        os.makedirs(verify_home, mode=0o700)
        # Per-test isolation — the module-level verifier cache must not
        # carry stale GPG instances from earlier tests.
        _GPG_VERIFIER_CACHE.clear()

        ok, detail = verify_inrelease(signed, keyring, verify_home)
        assert ok, f"expected verify ok, got: {detail}"
        assert 'Athena Test Signer' in detail or 'test@athena.local' in detail, detail


def test_verify_inrelease_tampered_signature_fails():
    """Flipping a byte in the signed body breaks the signature."""
    from utils import verify_inrelease, _GPG_VERIFIER_CACHE
    with tempfile.TemporaryDirectory() as tmp:
        signed, keyring, _ = _gen_signed_fixture(tmp)
        verify_home = os.path.join(tmp, 'verify-home')
        os.makedirs(verify_home, mode=0o700)
        _GPG_VERIFIER_CACHE.clear()

        # Replace 'AthenaTest' with 'AthenaXEST' inside the signed body.
        # Byte-flip inside the signature block can be hidden by gpg's
        # error recovery; modifying covered text guarantees BADSIG.
        with open(signed, 'r') as fh:
            content = fh.read()
        with open(signed, 'w') as fh:
            fh.write(content.replace('AthenaTest', 'AthenaXEST'))

        ok, detail = verify_inrelease(signed, keyring, verify_home)
        assert not ok, f"expected verify failure on tampered file, got ok: {detail}"


def test_verify_inrelease_missing_keyring_fails():
    """A keyring path that does not exist is reported, not silently swallowed."""
    from utils import verify_inrelease
    with tempfile.TemporaryDirectory() as tmp:
        signed = os.path.join(tmp, 'InRelease')
        with open(signed, 'w') as fh:
            fh.write('placeholder\n')
        verify_home = os.path.join(tmp, 'verify-home')
        os.makedirs(verify_home, mode=0o700)

        ok, detail = verify_inrelease(signed, '/nonexistent/keyring.gpg', verify_home)
        assert not ok, "expected failure when keyring missing"
        assert 'keyring missing' in detail, detail


def test_verify_inrelease_empty_keyring_fails():
    """A keyring file with no keys is rejected with a clear message."""
    from utils import verify_inrelease, _GPG_VERIFIER_CACHE
    with tempfile.TemporaryDirectory() as tmp:
        signed = os.path.join(tmp, 'InRelease')
        with open(signed, 'w') as fh:
            fh.write('placeholder\n')
        empty = os.path.join(tmp, 'empty.gpg')
        with open(empty, 'wb') as fh:
            fh.write(b'')
        verify_home = os.path.join(tmp, 'verify-home')
        os.makedirs(verify_home, mode=0o700)
        _GPG_VERIFIER_CACHE.clear()

        ok, detail = verify_inrelease(signed, empty, verify_home)
        assert not ok, "expected failure on empty keyring"
        assert 'no keys imported' in detail, detail


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
# STA-07 — BuildSystem sudo-password lifetime
# ─────────────────────────────────────────────────────────────────────────────

def _bare_buildsystem(password: str = 'secret'):
    """Construct a BuildSystem with just the password attributes set —
    bypasses __init__ so tests don't need real config / sudo / chroot dirs."""
    import buildsystem
    bs = buildsystem.BuildSystem.__new__(buildsystem.BuildSystem)
    bs._password = password
    bs._password_scrubbed = False
    return bs


def test_buildsystem_password_readable_before_scrub():
    bs = _bare_buildsystem('hunter2')
    assert bs.password == 'hunter2'


def test_buildsystem_scrub_password_clears_field():
    bs = _bare_buildsystem('hunter2')
    bs.scrub_password()
    assert bs._password == ''
    assert bs._password_scrubbed is True


def test_buildsystem_password_property_raises_after_scrub():
    """The .password property is the surface area cmd_build_chroot uses;
    a stale read after scrub must fail loudly so a missed cleanup in a
    handler cannot smuggle an empty password into a sudo subprocess
    call (which would silently fail authentication and corrupt the
    user-visible error message)."""
    bs = _bare_buildsystem('hunter2')
    bs.scrub_password()
    raised = False
    try:
        _ = bs.password
    except RuntimeError as e:
        raised = True
        assert 'scrub' in str(e).lower(), str(e)
    assert raised, "expected RuntimeError on .password after scrub"


def test_buildsystem_scrub_password_idempotent():
    """Calling scrub_password twice must not raise — finally blocks paired
    with try blocks that themselves call scrub on an early-return need
    this to be safe."""
    bs = _bare_buildsystem('hunter2')
    bs.scrub_password()
    bs.scrub_password()  # should not raise


# ─────────────────────────────────────────────────────────────────────────────
# STA-04 — download_source surfaces HTTP / short-download errors clearly
# ─────────────────────────────────────────────────────────────────────────────

def _download_source_with_mocked_get(mock_resp_factory, expected_size: int = 100,
                                     sha256_hex: str = 'a' * 64):
    """Run utils.download_source against a single-file fake dep tree, with
    requests.get patched to return whatever mock_resp_factory() yields.

    Returns the list of console messages (print/info/warning/error) emitted
    during the call, so callers can assert on the wording without any live
    network or real Mirror infrastructure.
    """
    from unittest.mock import patch
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
    utils.tui.console = _Cap()
    utils.ProgressBar = _Bar
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(utils.requests, 'get', return_value=mock_resp_factory()):
                utils.download_source(_Dt(), tmp)
    finally:
        utils.tui.console = saved_console
        utils.ProgressBar = saved_bar

    return captured


def test_download_source_surfaces_http_error_clearly():
    """Non-200 GET → HTTPError → handler logs 'HTTP failure'.  Pre-fix this
    surfaced as the misleading 'Hash mismatch' downstream after the GET
    silently no-op'd on a 404."""
    from unittest.mock import MagicMock
    from requests import HTTPError

    def mock_resp():
        m = MagicMock()
        m.__enter__.return_value = m
        m.raise_for_status.side_effect = HTTPError('404 Not Found')
        return m

    msgs = ' | '.join(_download_source_with_mocked_get(mock_resp))
    assert 'HTTP failure' in msgs, msgs
    assert 'Hash mismatch' not in msgs, msgs


def test_download_source_surfaces_short_download_clearly():
    """A truncated 200 (file shorter than the expected size from Sources)
    surfaces as 'short download' with a precise byte count, not the
    cryptic 'hash mismatch' that hid the real symptom pre-fix."""
    from unittest.mock import MagicMock

    def mock_resp():
        m = MagicMock()
        m.__enter__.return_value = m
        m.raise_for_status.return_value = None
        # Yield 50 bytes — half of the expected 100.
        m.iter_content.return_value = [b'x' * 50]
        return m

    msgs = ' | '.join(_download_source_with_mocked_get(mock_resp))
    assert 'short download' in msgs, msgs
    assert '50' in msgs and '100' in msgs, msgs
    assert 'Hash mismatch' not in msgs, msgs


# ─────────────────────────────────────────────────────────────────────────────
# SEC-02 — DOCKER_SERVER guard refuses unsafe network-reachable daemons
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


# ─────────────────────────────────────────────────────────────────────────────
# CONF-04 — BuildOptions / BuildProfiles are separate config keys
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


# ─────────────────────────────────────────────────────────────────────────────
# CONF-05 — DEP-3 header check
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


# ─────────────────────────────────────────────────────────────────────────────
# ARCH-01 — BuildSession encapsulates pipeline state; cmd_* handlers are
#           methods bound to it (no module-level globals).
# ─────────────────────────────────────────────────────────────────────────────

def test_buildsession_constructible_with_stub_tui():
    """BuildSession ctor takes (config, tui_inst); flags init clean,
    state pointers start as None.  No singleton, no TUI subsystem,
    no apt_pkg required — exactly the unit-test entry point the prior
    module-globals layout was blocking."""
    import sys, tempfile, textwrap
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
                          'cmd_source_download', 'cmd_init_container',
                          'cmd_source_build', 'cmd_tunnel_package',
                          'cmd_build_chroot', 'cmd_build_iso',
                          'cmd_verify_chroot', 'cmd_auto_run',
                          'cmd_print'):
                _fn = getattr(session, _name)
                assert callable(_fn), f"{_name} not callable"
                assert _fn.__self__ is session, f"{_name} not bound to this session"
    finally:
        _tui.tui_instance = saved


# ─────────────────────────────────────────────────────────────────────────────
# ARCH-03 — TUI primitives accept Tui explicitly (no singleton required)
# ─────────────────────────────────────────────────────────────────────────────

def test_console_with_explicit_tui_does_not_touch_singleton():
    """Constructing Console(tui=stub) routes calls to the stub, not to
    whatever is in tui_instance — so tests can isolate the TUI without
    monkey-patching module state."""
    import tui as _tui

    captured = []
    class _StubTui:
        def print(self, m, attr=None): captured.append(('print', m))
        def ERROR(self, m): captured.append(('error', m))
        def INFO(self, m):  captured.append(('info', m))
        def WARNING(self, m): captured.append(('warning', m))
        def console_mark(self): return 42
        def console_trim_to(self, n): captured.append(('trim_to', n))

    saved = _tui.tui_instance
    _tui.tui_instance = None        # force singleton path to fail
    try:
        c = _tui.Console(tui=_StubTui())
        c.print('hello', None)
        c.error('uh-oh')
        c.info('fyi')
        c.warning('careful')
        assert c.mark() == 42
        c.trim_to(7)
    finally:
        _tui.tui_instance = saved

    kinds = [k for k, _ in captured]
    assert kinds == ['print', 'error', 'info', 'warning', 'trim_to'], kinds


def test_console_singleton_fallback_when_tui_omitted():
    """Console() with no arg keeps the legacy behaviour: resolve through
    the module-level tui_instance at call time."""
    import tui as _tui

    captured = []
    class _Sentinel:
        def print(self, m, attr=None): captured.append(m)
        def ERROR(self, m): pass
        def INFO(self, m): pass
        def WARNING(self, m): pass

    saved = _tui.tui_instance
    _tui.tui_instance = _Sentinel()
    try:
        c = _tui.Console()           # no explicit tui → singleton fallback
        c.print('routed-via-singleton')
    finally:
        _tui.tui_instance = saved

    assert captured == ['routed-via-singleton'], captured


def test_console_raises_when_no_tui_anywhere():
    """No explicit tui AND no singleton → RuntimeError on use."""
    import tui as _tui
    saved = _tui.tui_instance
    _tui.tui_instance = None
    try:
        c = _tui.Console()
        raised = False
        try:
            c.print('should not be sent')
        except RuntimeError as e:
            raised = True
            assert 'No Tui instance' in str(e), str(e)
        assert raised
    finally:
        _tui.tui_instance = saved


# ─────────────────────────────────────────────────────────────────────────────
# ARCH-07 — single logging adapter routes by level into the Tui
# ─────────────────────────────────────────────────────────────────────────────

def _logger_test_with_stub_tui():
    """Set up _StubTui as tui_instance and configure logger handlers.
    Returns (captured_list, restore_callable)."""
    import tui as _tui

    captured = []
    class _StubTui:
        def print(self, m, attr=None): captured.append(('print', m, attr))
        def ERROR(self, m): captured.append(('error', m))
        def WARNING(self, m): captured.append(('warning', m))
        def INFO(self, m): captured.append(('info', m))

    saved = _tui.tui_instance
    _tui.tui_instance = _StubTui()
    _tui.setup_logging()  # binds handlers via tui_instance fallback

    def restore():
        _tui.tui_instance = saved
        _tui.setup_logging(saved)  # rebind to original (or None)
    return captured, restore


def test_logger_info_routes_to_log_tab():
    """logging.getLogger('athena').info(...) → Tui.INFO (log tab)."""
    import logging as _logging
    import tui as _tui

    captured, restore = _logger_test_with_stub_tui()
    try:
        _logging.getLogger(_tui.LOGGER_NAME).info('hi from logger')
    finally:
        restore()

    assert ('info', 'hi from logger') in captured, captured
    # Must NOT also reach the console tab
    assert not any(k == 'print' for k, *_ in captured), captured


def test_logger_warning_routes_to_log_tab():
    import logging as _logging
    import tui as _tui

    captured, restore = _logger_test_with_stub_tui()
    try:
        _logging.getLogger(_tui.LOGGER_NAME).warning('mirror lag')
    finally:
        restore()

    assert ('warning', 'mirror lag') in captured, captured


def test_logger_error_routes_to_log_tab():
    import logging as _logging
    import tui as _tui

    captured, restore = _logger_test_with_stub_tui()
    try:
        _logging.getLogger(_tui.LOGGER_NAME).error('GPG verify failed')
    finally:
        restore()

    assert ('error', 'GPG verify failed') in captured, captured


def test_logger_display_level_routes_to_console_tab():
    """logger.log(DISPLAY, ...) → Tui.print (console tab); NOT the log tab."""
    import logging as _logging
    import tui as _tui

    captured, restore = _logger_test_with_stub_tui()
    try:
        _logging.getLogger(_tui.LOGGER_NAME).log(_tui.DISPLAY, 'building cache')
    finally:
        restore()

    # Reached console tab
    assert any(entry[0] == 'print' and entry[1] == 'building cache'
               for entry in captured), captured
    # Did NOT also leak to the log tab
    assert not any(k in ('info', 'warning', 'error') for k, *_ in captured), captured


def test_logger_display_propagates_attribute_extra():
    """The optional 'attribute' extra reaches Tui.print as its colour arg."""
    import logging as _logging
    import tui as _tui

    captured, restore = _logger_test_with_stub_tui()
    try:
        log = _logging.getLogger(_tui.LOGGER_NAME)
        log.log(_tui.DISPLAY, 'green text', extra={'attribute': _tui.COLOR_HIGHLIGHT})
    finally:
        restore()

    found = [e for e in captured if e[0] == 'print']
    assert len(found) == 1, captured
    assert found[0] == ('print', 'green text', _tui.COLOR_HIGHLIGHT), found


def test_logger_debug_is_dropped_below_handler_threshold():
    """logger.debug(...) is suppressed: _LogTabHandler is INFO-gated."""
    import logging as _logging
    import tui as _tui

    captured, restore = _logger_test_with_stub_tui()
    try:
        _logging.getLogger(_tui.LOGGER_NAME).debug('chatter')
    finally:
        restore()

    assert captured == [], captured


def test_setup_logging_is_idempotent():
    """Calling setup_logging() twice does not duplicate handlers."""
    import logging as _logging
    import tui as _tui

    captured, restore = _logger_test_with_stub_tui()
    try:
        _tui.setup_logging()  # second call
        _logging.getLogger(_tui.LOGGER_NAME).info('once')
    finally:
        restore()

    info_hits = [e for e in captured if e[0] == 'info' and e[1] == 'once']
    assert len(info_hits) == 1, captured


def test_setup_file_logging_writes_records_to_timestamped_file():
    """setup_file_logging(dir) attaches a FileHandler that captures DEBUG
    and above into a build-<timestamp>.log; the path returned must exist
    and contain records emitted after the call."""
    import logging as _logging
    import os, tempfile, glob
    import tui as _tui

    saved_handlers = list(_logging.getLogger(_tui.LOGGER_NAME).handlers)
    saved_tui = _tui.tui_instance
    _tui.tui_instance = None  # tab handlers no-op
    _tui.setup_logging()      # clean slate

    with tempfile.TemporaryDirectory() as d:
        try:
            path = _tui.setup_file_logging(d, name='build')
            assert path.endswith('.log'), path
            assert os.path.dirname(path) == os.path.abspath(d)

            log = _logging.getLogger(_tui.LOGGER_NAME)
            log.debug('chroot transcript line')
            log.info('cache loaded')
            log.warning('mirror lag')
            log.error('GPG verify failed')

            # Flush handler buffers before reading.
            for h in log.handlers:
                h.flush()

            with open(path, 'r') as fh:
                content = fh.read()

            assert 'chroot transcript line' in content, content
            assert 'cache loaded' in content, content
            assert 'mirror lag' in content, content
            assert 'GPG verify failed' in content, content
            assert '[DEBUG' in content and '[INFO' in content, content
        finally:
            log = _logging.getLogger(_tui.LOGGER_NAME)
            for h in list(log.handlers):
                log.removeHandler(h)
                try: h.close()
                except Exception: pass
            for h in saved_handlers:
                log.addHandler(h)
            _tui.tui_instance = saved_tui


def test_setup_logging_preserves_filehandler_added_before_it():
    """Regression: build.py main() opens the FileHandler via
    setup_file_logging() *before* Tui(banner) constructs the Tui (which
    calls setup_logging() again).  setup_logging must not nuke the
    FileHandler — otherwise the per-run log file ends up empty."""
    import logging as _logging
    import os, tempfile
    import tui as _tui

    saved_handlers = list(_logging.getLogger(_tui.LOGGER_NAME).handlers)
    saved_tui = _tui.tui_instance
    _tui.tui_instance = None

    with tempfile.TemporaryDirectory() as d:
        try:
            _tui.setup_logging()
            path = _tui.setup_file_logging(d, name='build')
            # Now simulate Tui.__init__ re-calling setup_logging — the
            # FileHandler must survive.
            _tui.setup_logging()

            log = _logging.getLogger(_tui.LOGGER_NAME)
            file_handlers = [h for h in log.handlers
                             if isinstance(h, _logging.FileHandler)]
            assert len(file_handlers) == 1, log.handlers

            log.info('still alive after re-setup')
            for h in log.handlers:
                h.flush()
            assert os.path.exists(path)
            with open(path) as fh:
                content = fh.read()
            assert 'still alive after re-setup' in content, content
        finally:
            log = _logging.getLogger(_tui.LOGGER_NAME)
            for h in list(log.handlers):
                log.removeHandler(h)
                try: h.close()
                except Exception: pass
            for h in saved_handlers:
                log.addHandler(h)
            _tui.tui_instance = saved_tui


def test_logger_warning_does_not_leak_to_console_tab():
    """Regression: _ConsoleTabHandler must filter strictly on
    levelno == DISPLAY.  A bare `level=DISPLAY` floor would let
    WARNING (30) and ERROR (40) pass through and double-print into
    the console tab on top of the log tab routing."""
    import logging as _logging
    import tui as _tui

    captured, restore = _logger_test_with_stub_tui()
    try:
        log = _logging.getLogger(_tui.LOGGER_NAME)
        log.warning('mirror lag')
        log.error('GPG verify failed')
    finally:
        restore()

    # Must NOT reach the console tab via Tui.print
    leaked = [e for e in captured if e[0] == 'print']
    assert leaked == [], f'logger.warning/error leaked to console tab: {leaked}'
    # But should reach the log tab
    assert any(e[0] == 'warning' for e in captured), captured
    assert any(e[0] == 'error'   for e in captured), captured


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
# STA-09 — download_file surfaces HTTP status in its return value
# ─────────────────────────────────────────────────────────────────────────────

def test_download_file_returns_http_status_detail_on_404():
    """A non-200 GET → HTTPError → download_file returns (-1, 'HTTP 404 Not Found')
    so callers can include the actual status in their error_str instead of
    the legacy generic 'download failed' message."""
    from unittest.mock import patch, MagicMock
    from requests import HTTPError
    import utils

    class _Cap:
        def print(s, m, *a, **k): pass
        def info(s, m): pass
        def warning(s, m): pass
        def error(s, m): pass
    class _Bar:
        def __init__(s, *a, **k): pass
        def step(s, *a, **k): pass
        def label(s, *a, **k): pass
        def close(s, *a, **k): pass

    saved_console = utils.tui.console
    saved_bar     = utils.tui.ProgressBar
    utils.tui.console = _Cap()
    utils.tui.ProgressBar = _Bar
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mock_head = MagicMock()
            mock_head.headers = {'content-length': '0'}

            mock_get = MagicMock()
            mock_get.__enter__.return_value = mock_get
            _resp = MagicMock()
            _resp.status_code = 404
            _resp.reason = 'Not Found'
            mock_get.raise_for_status.side_effect = HTTPError('404 Client Error', response=_resp)

            with patch.object(utils.requests, 'head', return_value=mock_head), \
                 patch.object(utils.requests, 'get', return_value=mock_get):
                size, detail = utils.download_file('http://x.test/missing', os.path.join(tmp, 'out'))

            assert size == -1, size
            assert 'HTTP 404' in detail, detail
            assert 'Not Found' in detail, detail
    finally:
        utils.tui.console = saved_console
        utils.tui.ProgressBar = saved_bar


def test_download_file_success_returns_size_and_empty_detail():
    """Happy path: (size, '') so callers can keep using the size for their
    accounting and treat empty detail as 'no error to surface'."""
    from unittest.mock import patch, MagicMock
    import utils

    class _Cap:
        def print(s, m, *a, **k): pass
        def info(s, m): pass
        def warning(s, m): pass
        def error(s, m): pass
    class _Bar:
        def __init__(s, *a, **k): pass
        def step(s, *a, **k): pass
        def label(s, *a, **k): pass
        def close(s, *a, **k): pass

    saved_console = utils.tui.console
    saved_bar     = utils.tui.ProgressBar
    utils.tui.console = _Cap()
    utils.tui.ProgressBar = _Bar
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mock_head = MagicMock()
            mock_head.headers = {'content-length': '11'}

            mock_get = MagicMock()
            mock_get.__enter__.return_value = mock_get
            mock_get.raise_for_status.return_value = None
            mock_get.iter_content.return_value = [b'hello world']

            with patch.object(utils.requests, 'head', return_value=mock_head), \
                 patch.object(utils.requests, 'get', return_value=mock_get):
                size, detail = utils.download_file('http://x.test/ok', os.path.join(tmp, 'out'))

            assert size == 11, size
            assert detail == '', repr(detail)
    finally:
        utils.tui.console = saved_console
        utils.tui.ProgressBar = saved_bar


def test_shipped_build_conf_has_snapshot_enabled():
    """STA-03: the shipped config/build.conf must default to snapshot pinning
    enabled, so cache and live mirror cannot drift between cache build and
    source build.  Lock-in test — fails if anyone flips Enabled back to false."""
    import configparser
    p = configparser.ConfigParser()
    cfg_path = os.path.join(_ROOT, 'config', 'build.conf')
    assert os.path.isfile(cfg_path), f"shipped build.conf missing at {cfg_path}"
    p.read(cfg_path)
    assert p.has_section('Snapshot'), "shipped build.conf is missing [Snapshot]"
    assert p.getboolean('Snapshot', 'Enabled') is True, (
        "STA-03 regression: shipped build.conf must default Snapshot.Enabled = true"
    )


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


# ─────────────────────────────────────────────────────────────────────────────
# STA-15 / TEST-04 — strip_build_version edge cases
# ─────────────────────────────────────────────────────────────────────────────

def test_strip_build_version_strips_trailing_binNMU():
    """`+bN` at the end of the version field is removed."""
    from utils import strip_build_version
    assert strip_build_version('foo_1.0-2+b1_amd64.deb') == 'foo_1.0-2_amd64.deb'
    assert strip_build_version('foo_1.0-2+b15_amd64.deb') == 'foo_1.0-2_amd64.deb'


def test_strip_build_version_preserves_point_release_suffix():
    """`+debNuM` (Debian point-release) must not be stripped."""
    from utils import strip_build_version
    assert (strip_build_version('foo_1.0-2+deb12u1_amd64.deb')
            == 'foo_1.0-2+deb12u1_amd64.deb')
    assert (strip_build_version('foo_1.0-2+deb11u3_amd64.deb')
            == 'foo_1.0-2+deb11u3_amd64.deb')


def test_strip_build_version_strips_binNMU_after_point_release():
    """Mixed suffix `+debNuM+bK` — only the trailing `+bK` is stripped."""
    from utils import strip_build_version
    assert (strip_build_version('foo_1.0-2+deb12u1+b3_amd64.deb')
            == 'foo_1.0-2+deb12u1_amd64.deb')


def test_strip_build_version_leaves_embedded_binNMU_alone():
    """`+bN` not at the end of the version field is part of upstream — keep it."""
    from utils import strip_build_version
    # +b1 is in the middle of the version, not a buildd suffix
    assert (strip_build_version('foo_1.0+b1-2_amd64.deb')
            == 'foo_1.0+b1-2_amd64.deb')


def test_strip_build_version_handles_udeb_extension():
    """`.udeb` (debian-installer) extensions work the same way."""
    from utils import strip_build_version
    assert (strip_build_version('foo_1.0-2+b1_amd64.udeb')
            == 'foo_1.0-2_amd64.udeb')


def test_strip_build_version_no_change_when_no_binNMU():
    """Version without `+bN` round-trips unchanged."""
    from utils import strip_build_version
    assert (strip_build_version('foo_1.0-2_amd64.deb')
            == 'foo_1.0-2_amd64.deb')


def test_strip_build_version_rejects_malformed_filename():
    """Filenames not in `name_version_arch.ext` shape raise ValueError."""
    from utils import strip_build_version
    for bad in ('not-a-deb.deb', 'one_two.deb', 'a_b_c_d_amd64.deb'):
        try:
            strip_build_version(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


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
            build_codename = 'athena'
            build_version = '0.1'
            mirrors: list = []
            snapshot_enabled = False
            snapshot_timestamp_config = ''
            tunnel_packages: list = []
            working_dir = '/tmp/build'
            dir_cache = '/tmp/build/cache'
            dir_image = '/tmp/build/image'
            dir_log = '/tmp/build/log'
            config_path = '/tmp/build/build.conf'
            pkglist_path = '/tmp/build/pkg.list'
        self.config = _Cfg()
        self.cache = None
        self.dep_tree = None

        class _Flags:
            cache_ready = False
            dep_check_ready = False
            download_ready = False
            build_container_ready = False
            source_build_ready = False
            chroot_ready = False
            chroot_verified = False
        self.flags = _Flags()


def test_print_no_handler_crashes_on_uninitialized_session():
    """Every registered handler should either render something useful or
    print a `run <stage> first` guard message — never raise AttributeError
    or similar on a fresh BuildSession with cache=None / dep_tree=None."""
    import print_commands
    stub = _PrintSessionStub()
    for name, (handler, _group, _desc) in print_commands.CATEGORIES.items():
        try:
            _capture_console_print(lambda: handler(stub))
        except Exception as e:
            raise AssertionError(
                f"handler for {name!r} crashed on uninitialised session: "
                f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# UX-03 — autorun summary (lives in print_commands.summary)
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


def _build_autorun_session_stub(*, all_done: bool, source_build_done: bool):
    """Construct a minimal BuildSession-shaped stub for exercising the
    autorun summary helper without standing up TUI / config / cache."""
    from build import BuildSession

    class _Cfg:
        arch = 'amd64'
        build_version = '0.1'
        dir_image = '/tmp/image'

    class _Flags:
        cache_ready           = True
        dep_check_ready       = True
        download_ready        = source_build_done or all_done
        build_container_ready = source_build_done or all_done
        source_build_ready    = source_build_done or all_done
        chroot_ready          = all_done
        chroot_verified       = all_done

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
    sess.config = _Cfg()
    sess.tui = None
    sess.cache = _Cache()
    sess.dep_tree = _DT()
    sess.container = None
    sess.flags = _Flags()
    sess.last_source_build_counts = (
        {'built': 26, 'tunneled': 2, 'failed': 0, 'skipped': 3, 'total': 31}
        if source_build_done else None
    )
    return sess


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
    # All-green next-step hint should be visible
    assert 'Ready' in output and 'build_iso' in output


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
        aborted_at='source_download',
    )
    output = _capture_console_print(
        lambda: print_commands.summary(sess, timing=timing)
    )
    assert 'ABORTED' in output
    assert "'source_download'" in output
    assert '30m 00s' in output
    assert 'Source build   : not run' in output
    assert 'Chroot         : not built' in output


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


# ─────────────────────────────────────────────────────────────────────────────
# STA-12 — Cache._GCC_BASE_RE pattern
# ─────────────────────────────────────────────────────────────────────────────

def test_gcc_base_re_matches_gcc_N_and_gcc_N_base():
    """The pattern matches the two canonical names and captures the major."""
    from cache import _GCC_BASE_RE
    for name, major in (('gcc-12', '12'), ('gcc-13-base', '13'),
                        ('gcc-9', '9'), ('gcc-15-base', '15')):
        m = _GCC_BASE_RE.fullmatch(name)
        assert m is not None, f"{name!r} should match"
        assert m.group(1) == major, f"{name!r} → {m.group(1)} (want {major})"


def test_gcc_base_re_rejects_other_gcc_prefixed_packages():
    """The pattern leaves cross-compilers, multilib variants, g++, etc. alone."""
    from cache import _GCC_BASE_RE
    for name in ('gcc-mingw-w64', 'gcc-12-multilib', 'gcc-12-cross',
                 'g++-12', 'gcc', 'gcc-doc', 'gccgo-12', 'libgcc-12-dev'):
        assert _GCC_BASE_RE.fullmatch(name) is None, \
            f"{name!r} should NOT match (would clobber unrelated package)"


def test_gcc_base_re_rejects_malformed_versions():
    """Non-digit versions and trailing junk are not matched."""
    from cache import _GCC_BASE_RE
    for name in ('gcc-snapshot', 'gcc-12.1', 'gcc-12-base-extra',
                 'gcc-12base', 'gcc--base'):
        assert _GCC_BASE_RE.fullmatch(name) is None, f"{name!r} should NOT match"


# ─────────────────────────────────────────────────────────────────────────────
# EXTRAS-01 — pull recommends into selected_pkgs as available-not-installed
# ─────────────────────────────────────────────────────────────────────────────

class _FakePkg:
    """Minimal Package surface for EXTRAS-01 tests.  Carries the fields
    DependencyTree.pull_recommends_extras and derive_extras_src_names
    actually read: ['Package'], .recommends, .source, .get('Filename')."""
    def __init__(self, name, source, filename=None, recommends=None):
        self._fields = {'Package': name, 'Filename': filename or ''}
        self.source = source
        # parse_depends shape: each entry is a tuple (name, ver, op).
        self.recommends = [(r, '', '') for r in (recommends or [])]

    def __getitem__(self, k): return self._fields[k]
    def get(self, k, default=''): return self._fields.get(k, default)


class _FakeCache:
    """Minimal Cache surface — package_hashtable + skip_src.  Mirrors the
    real shape:  Dict[name, Dict[version, List[Package]]]  — the inner
    list carries the per-mirror records.  Versions are simple strings so
    max() works lexically (sufficient for these tests)."""
    def __init__(self, pkgs_by_name, skip_src=()):
        # pkgs_by_name: {name: [_FakePkg, ...]}
        self.package_hashtable = {
            name: {f"v{i}": [p] for i, p in enumerate(pkgs)}
            for name, pkgs in pkgs_by_name.items()
        }
        self.skip_src = list(skip_src)


def _build_dep_tree_with_recommend(*, recommend_source='libnss3',
                                   skip_src=()):
    """Construct a DependencyTree containing one selected pkg `firefox` from
    source `firefox` that recommends `libnss3-tools` from `recommend_source`.
    Used by several EXTRAS-01 tests."""
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
    # attributes pull_recommends_extras + derive_extras_src_names actually use.
    dt._DependencyTree__cache = cache
    dt.selected_pkgs = {'firefox': seed}
    dt.selected_srcs = {}
    dt.extras_pkg_names = set()
    dt.extras_src_names = set()
    return dt, seed, rec


def test_pull_recommends_extras_pulls_single_name_recommends():
    """A recommend whose source is NOT already in selected_srcs gets added
    to selected_pkgs and tracked in extras_pkg_names."""
    dt, _seed, _rec = _build_dep_tree_with_recommend()
    added = dt.pull_recommends_extras()
    assert added == 1
    assert 'libnss3-tools' in dt.selected_pkgs
    assert 'libnss3-tools' in dt.extras_pkg_names
    assert 'firefox' not in dt.extras_pkg_names  # seed isn't an extra


def test_pull_recommends_extras_skips_when_source_in_skip_src():
    """Recommends whose source is on cache.skip_src are skipped with a WARN
    (don't promise something we can't build/tunnel)."""
    dt, _seed, _rec = _build_dep_tree_with_recommend(skip_src=['libnss3'])
    added = dt.pull_recommends_extras()
    assert added == 0
    assert 'libnss3-tools' not in dt.selected_pkgs
    assert dt.extras_pkg_names == set()


def test_pull_recommends_extras_drops_alt_groups():
    """OR-grouped recommends ('foo | bar') are silently dropped today by
    package.py:201 (`if len(g) == 1`).  Document the gap with a test
    asserting the current (drop) behaviour so any future widening is
    conscious."""
    import dependencytree
    # Seed has an empty .recommends list — alt-groups don't make it into
    # the parsed list at all.  This test pins that today's behaviour is
    # "no alt-recommends in the data model".
    seed = _FakePkg('firefox', source='firefox',
                    filename='firefox_1.0_amd64.deb',
                    recommends=[])  # parsed list ignores OR-groups
    cache = _FakeCache({'firefox': [seed]})
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt._DependencyTree__cache = cache
    dt.selected_pkgs = {'firefox': seed}
    dt.selected_srcs = {}
    dt.extras_pkg_names = set()
    dt.extras_src_names = set()
    added = dt.pull_recommends_extras()
    assert added == 0  # nothing pulled from an alt-recommend


def test_pull_recommends_extras_handles_multi_mirror_version_buckets():
    """REGRESSION: package_hashtable[name][version] is a List[Package]
    (per-mirror), not a single Package.  pull_recommends_extras must index
    into the inner list, not call .source on it directly.  The first run
    against bookworm hit AttributeError on every recommend because the
    method was treating the bucket as a Package."""
    import dependencytree
    seed = _FakePkg('firefox', source='firefox',
                    filename='firefox_1.0_amd64.deb',
                    recommends=['libnss3-tools'])
    rec = _FakePkg('libnss3-tools', source='libnss3',
                   filename='libnss3-tools_3.0_amd64.deb',
                   recommends=[])
    # Construct a hashtable matching the real production shape:
    # the inner value is a LIST of packages (per-mirror), even when
    # only one mirror ships this name+version.
    cache = _FakeCache({'firefox': [seed], 'libnss3-tools': [rec]})
    # Verify the test fixture itself exercises the multi-mirror shape:
    _bucket = cache.package_hashtable['libnss3-tools']['v0']
    assert isinstance(_bucket, list), \
        "test fixture must mirror the real Dict[name,Dict[ver,List[Pkg]]] shape"
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt._DependencyTree__cache = cache
    dt.selected_pkgs = {'firefox': seed}
    dt.selected_srcs = {}
    dt.extras_pkg_names = set()
    dt.extras_src_names = set()
    added = dt.pull_recommends_extras()
    assert added == 1
    assert 'libnss3-tools' in dt.extras_pkg_names


def test_pull_recommends_extras_skips_already_in_selected_pkgs():
    """A recommend that is already in selected_pkgs (covered by the
    required/important/manual closure) is NOT re-added or marked as extras."""
    dt, _seed, _rec = _build_dep_tree_with_recommend()
    # Pre-populate selected_pkgs with the recommend — simulate it being in
    # the install closure already.
    dt.selected_pkgs['libnss3-tools'] = _rec
    added = dt.pull_recommends_extras()
    assert added == 0
    assert 'libnss3-tools' not in dt.extras_pkg_names


def test_derive_extras_src_names_marks_extras_only_sources():
    """A source whose every binary is in extras_pkg_names is in
    extras_src_names.  A mixed source (some selected + some extras) is NOT."""
    import dependencytree

    class _StubSrc:
        def __init__(self, pkgs): self.pkgs = pkgs

    # firefox source: produces firefox.deb (selected) AND firefox-l10n-en.deb
    # (extras).  Mixed → NOT in extras_src_names.
    # libnss3 source: produces only libnss3-tools.deb (extras).  Extras-only.
    seed_pkgs = {
        'firefox':         _FakePkg('firefox',         source='firefox',
                                    filename='firefox_1.0_amd64.deb'),
        'firefox-l10n-en': _FakePkg('firefox-l10n-en', source='firefox',
                                    filename='firefox-l10n-en_1.0_amd64.deb'),
        'libnss3-tools':   _FakePkg('libnss3-tools',   source='libnss3',
                                    filename='libnss3-tools_3.0_amd64.deb'),
    }
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt._DependencyTree__cache = _FakeCache({})
    dt.selected_pkgs = seed_pkgs
    dt.selected_srcs = {
        'firefox': _StubSrc(['firefox_1.0_amd64.deb',
                             'firefox-l10n-en_1.0_amd64.deb']),
        'libnss3': _StubSrc(['libnss3-tools_3.0_amd64.deb']),
    }
    dt.extras_pkg_names = {'firefox-l10n-en', 'libnss3-tools'}
    dt.extras_src_names = set()
    n = dt.derive_extras_src_names()
    assert n == 1
    assert dt.extras_src_names == {'libnss3'}
    assert 'firefox' not in dt.extras_src_names  # mixed — NOT extras-only


def test_compute_install_batches_excludes_extras_pkg_names():
    """EXTRAS-01: chroot install path skips packages in
    dependencytree.extras_pkg_names so they never enter a batch."""
    bs = _bare_buildsystem_with_deps([
        ('foo', [], []),
        ('bar', [], ['foo']),       # bar depends on foo
        ('extra-y', [], ['foo']),   # an extra that also depends on foo
    ])
    bs._dependencytree.extras_pkg_names = {'extra-y'}
    batches = bs._compute_install_batches(libc_seed_set=set())
    _all_named = {p for batch_pkgs, _force in batches for p in batch_pkgs}
    assert 'foo' in _all_named
    assert 'bar' in _all_named
    assert 'extra-y' not in _all_named, \
        "EXTRAS-01: extras must be filtered out of install batches"


def test_print_extras_lists_recommended_packages():
    """`print extras` enumerates the EXTRAS-01 entries with their source
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
    assert 'IncludeRecommendsInRepo' in output


# ─── EXTRAS-01: source_build [profiles] override parsing ────────────────────

def test_source_build_args_no_args_is_default_mode():
    """Bare `source_build` → no flags, no names, no override."""
    from build import BuildSession
    err, force, rec, names, override = \
        BuildSession._parse_source_build_args(())
    assert err is None
    assert (force, rec, names, override) == (False, False, [], None)


def test_source_build_args_force_flag_anywhere():
    """`force` is detectable in any position, case-insensitive."""
    from build import BuildSession
    for argv in (('force',), ('Force',), ('foo', 'FORCE'), ('force', 'foo')):
        err, force, _r, _n, _o = BuildSession._parse_source_build_args(argv)
        assert err is None and force is True, f"args={argv!r}"


def test_source_build_args_recommended_and_named_pkgs_mutually_exclusive():
    """`recommended pkg1` is rejected — operator must pick one mode."""
    from build import BuildSession
    err, *_ = BuildSession._parse_source_build_args(('recommended', 'pkg1'))
    assert err is not None and 'mutually exclusive' in err


def test_source_build_args_bracket_token_extracts_profiles():
    """`[nocheck]` parses to ['nocheck']; commas + whitespace tolerated."""
    from build import BuildSession
    err, _f, _r, names, override = \
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
    err, _f, _r, _n, override = \
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
        err, _f, _r, names, override = \
            BuildSession._parse_source_build_args(argv)
        assert err is None, f"args={argv!r}"
        assert names == ['foo'], f"args={argv!r}"
        assert override == ['nocheck'], f"args={argv!r}"


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


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [
        # v0.2 step 1
        test_mirror_url_composition,
        test_mirror_suite_with_suffix,
        test_mirror_repr_does_not_crash,
        test_buildconfig_parses_three_mirrors,
        test_buildconfig_rejects_no_mirrors,
        test_package_and_source_have_mirror_field,
        test_source_parses_security_stanza_without_files_field,
        test_source_parses_main_stanza_with_both_files_and_sha256,
        # STA-01 / SEC-01
        test_verify_inrelease_clean_signature_passes,
        test_verify_inrelease_tampered_signature_fails,
        test_verify_inrelease_missing_keyring_fails,
        test_verify_inrelease_empty_keyring_fails,
        test_buildconfig_security_defaults,
        test_buildconfig_security_disabled_accepts_missing_keyring,
        test_buildconfig_security_enabled_rejects_missing_keyring,
        test_buildconfig_creates_dir_gnupg_with_0700,
        # ARCH-12
        test_compute_install_batches_linear_chain,
        test_compute_install_batches_independent_packages_share_a_batch,
        test_compute_install_batches_fan_out,
        test_compute_install_batches_pre_depends_and_depends_unioned,
        test_compute_install_batches_libc_seed_breaks_cycle,
        test_compute_install_batches_self_dep_is_ignored,
        test_compute_install_batches_cycle_emitted_as_forced_batch,
        test_compute_install_batches_cycle_with_pre_depends_chain_splits,
        test_compute_install_batches_acyclic_then_cycle,
        test_compute_install_batches_external_deps_filtered,
        # STA-07
        test_buildsystem_password_readable_before_scrub,
        test_buildsystem_scrub_password_clears_field,
        test_buildsystem_password_property_raises_after_scrub,
        test_buildsystem_scrub_password_idempotent,
        # STA-04
        test_download_source_surfaces_http_error_clearly,
        test_download_source_surfaces_short_download_clearly,
        # SEC-02
        test_docker_server_guard_accepts_safe_targets,
        test_docker_server_guard_refuses_unsafe_targets,
        # CONF-04
        test_buildconfig_build_options_and_profiles_are_separate,
        test_buildconfig_build_options_falls_back_to_profiles_when_omitted,
        # CONF-05
        test_check_dep3_header_clean_patch_returns_empty,
        test_check_dep3_header_missing_origin_returns_field,
        test_check_dep3_header_subject_satisfies_description,
        # ARCH-01
        test_buildsession_constructible_with_stub_tui,
        # ARCH-03
        test_console_with_explicit_tui_does_not_touch_singleton,
        test_console_singleton_fallback_when_tui_omitted,
        test_console_raises_when_no_tui_anywhere,
        # ARCH-07
        test_logger_info_routes_to_log_tab,
        test_logger_warning_routes_to_log_tab,
        test_logger_error_routes_to_log_tab,
        test_logger_display_level_routes_to_console_tab,
        test_logger_display_propagates_attribute_extra,
        test_logger_debug_is_dropped_below_handler_threshold,
        test_setup_logging_is_idempotent,
        test_setup_file_logging_writes_records_to_timestamped_file,
        test_setup_logging_preserves_filehandler_added_before_it,
        test_logger_warning_does_not_leak_to_console_tab,
        test_setup_file_logging_filename_has_timestamp,
        # STA-09
        test_download_file_returns_http_status_detail_on_404,
        test_download_file_success_returns_size_and_empty_detail,
        # STA-03
        test_shipped_build_conf_has_snapshot_enabled,
        # STA-15 / TEST-04
        test_strip_build_version_strips_trailing_binNMU,
        test_strip_build_version_preserves_point_release_suffix,
        test_strip_build_version_strips_binNMU_after_point_release,
        test_strip_build_version_leaves_embedded_binNMU_alone,
        test_strip_build_version_handles_udeb_extension,
        test_strip_build_version_no_change_when_no_binNMU,
        test_strip_build_version_rejects_malformed_filename,
        # STA-12
        test_gcc_base_re_matches_gcc_N_and_gcc_N_base,
        test_gcc_base_re_rejects_other_gcc_prefixed_packages,
        test_gcc_base_re_rejects_malformed_versions,
        # snapshot UI helper
        test_format_snapshot_timestamp_well_formed,
        test_format_snapshot_timestamp_falls_back_on_malformed,
        # print_commands
        test_print_help_lists_every_registered_category,
        test_print_dispatch_unknown_category_points_to_help,
        test_print_dispatch_empty_category_shows_help,
        test_print_help_groups_categories_into_sections,
        test_print_dispatch_passes_extras_to_parametrized_handler,
        test_fmt_dep_unconstrained,
        test_fmt_dep_with_constraint,
        test_fmt_dep_group_alternates_with_pipe,
        test_print_no_handler_crashes_on_uninitialized_session,
        # UX-03 — autorun summary (in print_commands)
        test_format_duration_seconds_only,
        test_format_duration_minutes_seconds,
        test_format_duration_hours_minutes_seconds,
        test_autorun_summary_success_includes_counts_and_iso_path,
        test_autorun_summary_aborted_marks_stage_and_partial_state,
        test_print_summary_without_timing_renders_state_snapshot,
        test_print_summary_dispatch_through_handler,
        # EXTRAS-01
        test_pull_recommends_extras_pulls_single_name_recommends,
        test_pull_recommends_extras_skips_when_source_in_skip_src,
        test_pull_recommends_extras_drops_alt_groups,
        test_pull_recommends_extras_handles_multi_mirror_version_buckets,
        test_pull_recommends_extras_skips_already_in_selected_pkgs,
        test_derive_extras_src_names_marks_extras_only_sources,
        test_compute_install_batches_excludes_extras_pkg_names,
        test_print_extras_lists_recommended_packages,
        test_print_extras_handles_empty_extras_set,
        # EXTRAS-01: source_build [profiles] override parsing
        test_source_build_args_no_args_is_default_mode,
        test_source_build_args_force_flag_anywhere,
        test_source_build_args_recommended_and_named_pkgs_mutually_exclusive,
        test_source_build_args_bracket_token_extracts_profiles,
        test_source_build_args_empty_bracket_means_no_profiles,
        test_source_build_args_multiple_bracket_tokens_rejected,
        test_source_build_args_bracket_position_does_not_matter,
        test_buildcontainer_build_signature_accepts_profile_override_kwargs,
    ]
    failures = 0
    for t in tests:
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


if __name__ == '__main__':
    sys.exit(main())
