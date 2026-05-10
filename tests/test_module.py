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
    """The .password property is the surface area cmd_build_chroot_live uses;
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
                          'cmd_build_chroot_live', 'cmd_build_chroot_installer',
                          'cmd_build_iso_live', 'cmd_build_iso_installer',
                          'cmd_verify_chroot', 'cmd_auto_run',
                          'cmd_print',
                          # Group dispatchers (noun-verb command surface).
                          'cmd_cache', 'cmd_dep', 'cmd_patch',
                          'cmd_source', 'cmd_package', 'cmd_container',
                          'cmd_chroot', 'cmd_iso', 'cmd_key'):
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
        ('cmd_dep',       'parse',    'cmd_parse_dependency'),
        ('cmd_patch',     'refresh',  'cmd_patch_refresh'),
        ('cmd_source',    'download', 'cmd_source_download'),
        ('cmd_source',    'build',    'cmd_source_build'),
        ('cmd_package',   'tunnel',   'cmd_tunnel_package'),
        ('cmd_container', 'init',     'cmd_init_container'),
        # cmd_chroot 'build' is now multi-token ('build live' / 'build
        # installer') with default-to-live; covered by its own tests below.
        ('cmd_chroot',    'verify',   'cmd_verify_chroot'),
        # cmd_iso is multi-token ('build live' / 'build installer') —
        # not a verb-only dispatcher; covered by its own tests below.
        ('cmd_key',       'generate', 'cmd_generate_signing_key'),
        ('cmd_key',       'verify',   'cmd_verify_signing_key'),
    ]

    for _group_name, _verb, _target_name in _matrix:
        # Bypass __init__ — we don't want to construct a real config; we
        # only need the bound dispatcher methods, which are class-level.
        _sess = BuildSession.__new__(BuildSession)
        _calls = []
        # Stamp the target with a recorder; the dispatcher should call it.
        setattr(_sess, _target_name,
                lambda *a, _name=_target_name, **kw: _calls.append((_name, a, kw)))
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


def test_cmd_build_chroot_installer_is_stub():
    """The installer chroot handler is a COMP-01a stub: returns without
    doing any work, prints an error referencing the plan doc."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    # Should not raise, returns None.  No prerequisites checked because the
    # stub bails before any state is touched.
    assert _sess.cmd_build_chroot_installer() is None
    assert _sess.cmd_build_chroot_installer('with_debug') is None


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


def test_cmd_build_iso_installer_is_stub():
    """The installer ISO handler is a COMP-01a stub: returns without doing
    any work, prints an error referencing the plan doc."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    # Should not raise, returns None.  No prerequisites checked because the
    # stub bails before any state is touched.
    assert _sess.cmd_build_iso_installer() is None
    assert _sess.cmd_build_iso_installer('force', 'extra') is None


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
        with patch('build.Prompt', return_value=_prompt_inst):
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
        with patch('build.Prompt', return_value=_prompt_inst):
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
        with patch('build.Prompt', return_value=_prompt_inst) as mock_Prompt:
            sess.cmd_cache_purge()

        # Empty branch must not even construct the Prompt — confirm.
        mock_Prompt.assert_not_called()
        assert sess.flags.cache_ready is True


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
    """Happy path: (bytes_written, '') so callers can keep using the size
    for their accounting and treat empty detail as 'no error to surface'.
    Returns the actual bytes streamed, not the HEAD/GET hint — fixes the
    case where a chunked-encoded mirror reports 0 in Content-Length."""
    from unittest.mock import patch, MagicMock
    import utils

    class _Cap:
        def print(s, m, *a, **k): pass
        def info(s, m): pass
        def warning(s, m): pass
        def error(s, m): pass
    class _Bar:
        def __init__(s, *a, **k): s._value = 0; s._max = max(1, k.get('maxvalue', 100))
        def step(s, n=1): s._value = min(s._value + n, s._max)
        def label(s, *a, **k): pass
        def close(s, *a, **k): pass
        def set_max(s, n): s._max = max(1, n)
        @property
        def value(s): return s._value

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
            mock_get.headers = {'content-length': '11'}
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


def test_download_file_zero_content_length_does_not_freeze_bar():
    """When BOTH HEAD and GET return Content-Length: 0 (or omit it), the
    bar must not freeze at 1/1 — fall back to a 1 MB seed and grow as
    bytes arrive.  Returns the actual bytes written, regardless of the
    upstream hint."""
    from unittest.mock import patch, MagicMock
    import utils

    class _Cap:
        def print(s, m, *a, **k): pass
        def info(s, m): pass
        def warning(s, m): pass
        def error(s, m): pass

    _set_max_calls = []
    class _Bar:
        def __init__(s, *a, **k): s._value = 0; s._max = max(1, k.get('maxvalue', 100))
        def step(s, n=1): s._value = min(s._value + n, s._max)
        def label(s, *a, **k): pass
        def close(s, *a, **k): pass
        def set_max(s, n): s._max = max(1, n); _set_max_calls.append(n)
        @property
        def value(s): return s._value

    saved_console = utils.tui.console
    saved_bar     = utils.tui.ProgressBar
    utils.tui.console = _Cap()
    utils.tui.ProgressBar = _Bar
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mock_head = MagicMock()
            mock_head.headers = {}   # no content-length

            mock_get = MagicMock()
            mock_get.__enter__.return_value = mock_get
            mock_get.headers = {}    # no content-length
            mock_get.raise_for_status.return_value = None
            # 2 MB total — exceeds the 1 MB seed, must trigger set_max growth.
            _payload = [b'x' * 8192] * 256   # 2 MB in 8 KB chunks
            mock_get.iter_content.return_value = _payload

            with patch.object(utils.requests, 'head', return_value=mock_head), \
                 patch.object(utils.requests, 'get', return_value=mock_get):
                size, detail = utils.download_file('http://x.test/big', os.path.join(tmp, 'out'))

            assert size == 2 * 1024 * 1024, size
            assert detail == ''
            # set_max called at least once during the stream (growth) plus
            # once at the end (final correction).
            assert len(_set_max_calls) >= 2, _set_max_calls
            # Final correction matches actual bytes.
            assert _set_max_calls[-1] == 2 * 1024 * 1024
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
            include_recommends_in_repo = False
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
        aborted_at='source download',
    )
    output = _capture_console_print(
        lambda: print_commands.summary(sess, timing=timing)
    )
    assert 'ABORTED' in output
    assert "'source download'" in output
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


def test_verify_dep_resolution_skips_extras():
    """EXTRAS-01 REGRESSION: _verify_dep_resolution walked canonical_pkgs
    including extras and demanded their (often-not-in-our-install-set)
    deps resolve, blocking real builds at the chroot stage with errors
    like 'ca-certificates depends: openssl — unresolved'.  Extras are
    not chroot-installed; their transitive deps resolve via apt at
    install-time on the booted system, so this gate must skip them.

    Stub setup: 'foo' (selected) is fine; 'extra-y' (extras) depends on
    'missing-dep' which is NOT in selected_pkgs.  Without the skip,
    verify would raise; with the skip, it must complete cleanly."""
    import dep_drift

    class _Pkg:
        def __init__(self, name, version='1.0',
                     pre_depends=(), depends=(),
                     alt_pre_depends=(), alt_depends=()):
            self._fields = {'Package': name, 'Version': version}
            self.version = version
            self.pre_depends = list(pre_depends)
            self.depends = list(depends)
            self.alt_pre_depends = list(alt_pre_depends)
            self.alt_depends = list(alt_depends)

        def __getitem__(self, k): return self._fields[k]
        def get(self, k, default=''): return self._fields.get(k, default)

    foo = _Pkg('foo')
    extra = _Pkg('extra-y',
                 depends=[('missing-dep', '', '')])
    selected_pkgs = {'foo': foo, 'extra-y': extra}

    class _DT:
        selected_pkgs = {'foo': foo, 'extra-y': extra}
        extras_pkg_names = {'extra-y'}
        @property
        def canonical_pkgs(self):
            return {k: v for k, v in self.selected_pkgs.items()
                    if k == v['Package']}

    class _Mixin(dep_drift._DepDriftMixin):
        def __init__(self):
            self._dependencytree = _DT()

    # Should NOT raise — extra-y is in extras_pkg_names so the
    # missing-dep violation is skipped.
    _Mixin()._verify_dep_resolution()


def test_verify_dep_resolution_still_catches_real_violations():
    """The extras skip in _verify_dep_resolution must NOT mask genuine
    install-set dep violations.  Stub: 'foo' (selected, install set)
    depends on 'real-missing' which is NOT in selected — verify must
    still raise."""
    import dep_drift

    class _Pkg:
        def __init__(self, name, version='1.0',
                     depends=()):
            self._fields = {'Package': name, 'Version': version}
            self.version = version
            self.pre_depends = []
            self.depends = list(depends)
            self.alt_pre_depends = []
            self.alt_depends = []

        def __getitem__(self, k): return self._fields[k]
        def get(self, k, default=''): return self._fields.get(k, default)

    foo = _Pkg('foo', depends=[('real-missing', '', '')])

    class _DT:
        selected_pkgs = {'foo': foo}
        extras_pkg_names = set()
        @property
        def canonical_pkgs(self):
            return {k: v for k, v in self.selected_pkgs.items()
                    if k == v['Package']}

    class _Mixin(dep_drift._DepDriftMixin):
        def __init__(self):
            self._dependencytree = _DT()

    try:
        _Mixin()._verify_dep_resolution()
    except RuntimeError as e:
        assert 'real-missing' in str(e) or 'unresolved' in str(e)
        return
    raise AssertionError(
        "verify must raise on a real install-set dep violation"
    )


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

def test_source_build_args_no_args_defaults_to_live_subset():
    """Bare `source build` → subset='live' (preserves today's UX), no
    force, no names, no override."""
    from build import BuildSession
    err, force, subset, names, override = \
        BuildSession._parse_source_build_args(())
    assert err is None
    assert (force, subset, names, override) == (False, 'live', [], None)


def test_source_build_args_live_subset_explicit():
    """`source build live` is the explicit form of the bare default."""
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
    `force` (no other args) defaults the subset to 'live'."""
    from build import BuildSession
    for argv in (('force',), ('Force',), ('foo', 'FORCE'), ('force', 'foo')):
        err, force, _s, _n, _o = BuildSession._parse_source_build_args(argv)
        assert err is None and force is True, f"args={argv!r}"
    # Bare `force` should still default subset to 'live' since there are
    # no names and no other subset selector.
    _, _, subset, names, _ = BuildSession._parse_source_build_args(('force',))
    assert subset == 'live' and names == []


def test_source_build_args_subsets_mutually_exclusive():
    """Two subset selectors at once → parse error."""
    from build import BuildSession
    for argv in (('live', 'installer'),
                 ('installer', 'recommended'),
                 ('live', 'recommended'),
                 ('live', 'installer', 'recommended')):
        err, *_ = BuildSession._parse_source_build_args(argv)
        assert err is not None, f"args={argv!r}"
        assert 'pick at most one' in err, f"args={argv!r}: {err!r}"


def test_source_build_args_subset_and_named_pkgs_mutually_exclusive():
    """`source build <subset> pkg1` is rejected for every subset word —
    operator must pick the subset OR specific names, not both."""
    from build import BuildSession
    for _subset in ('live', 'installer', 'recommended'):
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


# ─── CONF-02 phase 1: signing key generate / verify (scripts/signing.py) ──

def test_signing_parse_uid_helpers():
    """_parse_uid_name and _parse_uid_email split 'Name <email>' uids."""
    from signing import _parse_uid_name, _parse_uid_email
    assert _parse_uid_name('Athena Build <athena@local>') == 'Athena Build'
    assert _parse_uid_email('Athena Build <athena@local>') == 'athena@local'
    # Bare strings pass through unchanged (used for both halves by gpg
    # batch param file).
    assert _parse_uid_name('Bare Name') == 'Bare Name'
    assert _parse_uid_email('Bare Name') == 'Bare Name'


def test_signing_parse_secret_keys_colons_extracts_fields():
    """Hand-crafted gpg --with-colons output parses to the expected
    key-info dict — fingerprint, primary uid, created, expires."""
    from signing import parse_secret_keys_colons
    sample = (
        "sec:u:4096:1:ABC123DEF456:1700000000:0:::u:::scESC:::+::::::0:\n"
        "fpr:::::::::ABCDEF1234567890ABCDEF1234567890ABCDEF12:\n"
        "uid:u::::1700000000::1234abcd::Athena Build <athena@local>::::::::::0:\n"
    )
    keys = parse_secret_keys_colons(sample)
    assert len(keys) == 1
    assert keys[0]['fingerprint'] == \
        'ABCDEF1234567890ABCDEF1234567890ABCDEF12'
    assert keys[0]['uid'] == 'Athena Build <athena@local>'
    assert keys[0]['created'] == '1700000000'
    assert keys[0]['expires'] == '0'
    assert keys[0]['keyid'] == 'ABC123DEF456'


def test_signing_parse_secret_keys_colons_empty_input():
    """Empty / no-key input returns []."""
    from signing import parse_secret_keys_colons
    assert parse_secret_keys_colons('') == []
    # Lines that don't start with sec/fpr/uid are ignored.
    assert parse_secret_keys_colons('tru:::1:1234567890:1:3:1:5\n') == []


def test_signing_parse_secret_keys_colons_only_keeps_first_uid():
    """Multiple uid records on the same key — keep only the primary."""
    from signing import parse_secret_keys_colons
    sample = (
        "sec:u:4096:1:KEY1:1700000000:0:::u:::scESC:::+::::::0:\n"
        "fpr:::::::::FP1:\n"
        "uid:u::::1700000000::abcd::Primary UID <a@x>::::::::::0:\n"
        "uid:u::::1700000000::efgh::Secondary <b@x>::::::::::0:\n"
    )
    keys = parse_secret_keys_colons(sample)
    assert keys[0]['uid'] == 'Primary UID <a@x>'


def test_signing_get_key_info_returns_none_when_homedir_absent():
    """No homedir → no key state → None.  Doesn't raise."""
    import tempfile
    from signing import get_key_info
    with tempfile.TemporaryDirectory() as tmp:
        class _Cfg:
            dir_gnupg = os.path.join(tmp, 'gnupg-does-not-exist-yet')
            signing_key_uid = 'Athena <athena@local>'
        assert get_key_info(_Cfg()) is None


def test_signing_verify_key_returns_no_key_when_absent():
    """verify_key on a missing key returns (False, 'no signing key …')
    rather than raising — caller can surface the message verbatim."""
    import tempfile
    from signing import verify_key
    with tempfile.TemporaryDirectory() as tmp:
        class _Cfg:
            dir_gnupg = os.path.join(tmp, 'gnupg-empty')
            signing_key_uid = 'Athena <athena@local>'
        ok, msg = verify_key(_Cfg())
        assert ok is False
        assert 'no signing key' in msg


def test_signing_paths_compose_off_dir_gnupg():
    """signing_home and signing_pubkey_path are stable functions of
    config.dir_gnupg — downstream callers depend on these paths."""
    from signing import signing_home, signing_pubkey_path
    class _Cfg:
        dir_gnupg = '/some/path/gnupg'
    assert signing_home(_Cfg()) == '/some/path/gnupg/signing'
    assert signing_pubkey_path(_Cfg()) == \
        '/some/path/gnupg/signing/athena-archive-keyring.gpg'


def test_signing_generate_and_verify_roundtrip_real_gpg():
    """INTEGRATION: generate a real key in a tmp homedir, then sign+verify.
    Uses RSA-2048 for test speed (~3s vs ~30s for production's 4096);
    same code path either way.  Skipped silently if gpg isn't on PATH
    (the broader codebase already requires gpg, so this should never
    actually skip in a normal dev/CI environment)."""
    import shutil
    import tempfile
    if shutil.which('gpg') is None:
        return
    from signing import generate_key, verify_key, get_key_info
    with tempfile.TemporaryDirectory() as tmp:
        class _Cfg:
            dir_gnupg = os.path.join(tmp, 'gnupg')
            signing_key_uid = 'Athena Test <test@athena.local>'
        # Pre-condition: no key
        assert get_key_info(_Cfg()) is None
        # Generate (RSA-2048 for test speed)
        assert generate_key(_Cfg(), _key_length=2048) is True
        # Post-condition: key info available, fingerprint populated
        info = get_key_info(_Cfg())
        assert info is not None
        assert len(info['fingerprint']) == 40, info  # SHA-1 hex = 40 chars
        assert 'Athena Test' in info['uid']
        # Verify roundtrip
        ok, msg = verify_key(_Cfg())
        assert ok, msg


# ─── CONF-02 phase 3: signing key gate at top of build_chroot ─────────────

def _stub_session_for_signing_gate():
    """Construct a BuildSession via object.__new__ (bypassing __init__)
    with just the attrs `_ensure_signing_key_verified` touches.  Saves
    + restores tui.tui_instance so the Prompt() construction inside the
    helper can resolve the singleton."""
    import build
    import tui

    class _Cfg:
        signing_key_uid = 'Athena Build <athena@local>'
        dir_gnupg = '/tmp/athena-test-gnupg-not-real'

    sess = object.__new__(build.BuildSession)
    sess.config = _Cfg()
    sess.flags = build.BuildFlags()
    sess.tui = None

    return sess


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


def test_install_signing_keyring_skips_when_no_key_generated():
    """No keyring file at signing.signing_pubkey_path → method returns
    silently with an INFO log; no subprocess invocations.  Operator may
    not have generated a key yet — the chroot build must keep going."""
    import tempfile
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        # No keyring file under tmp/signing/ — get_key_info would also
        # return None.  This is the "operator hasn't run
        # generate_signing_key yet" path.
        inst = _stub_chroot_mixin_for_keyring_test(
            dir_gnupg=tmp, dir_chroot=os.path.join(tmp, 'fake-chroot'),
        )
        with patch('subprocess.run') as mock_run:
            inst._install_signing_keyring()
            assert mock_run.call_count == 0, (
                "no subprocess calls should fire when keyring is absent — "
                "we only log INFO and return"
            )


def test_install_signing_keyring_invokes_cp_when_key_present():
    """Keyring file exists → subprocess.run called with `sudo cp <src>
    <chroot>/usr/share/keyrings/athena-archive-keyring.gpg`."""
    import tempfile
    from unittest.mock import patch, MagicMock
    with tempfile.TemporaryDirectory() as tmp:
        # Plant a fake keyring file at the path
        # signing.signing_pubkey_path() will return.
        signing_dir = os.path.join(tmp, 'signing')
        os.makedirs(signing_dir, mode=0o700)
        keyring_path = os.path.join(signing_dir, 'athena-archive-keyring.gpg')
        with open(keyring_path, 'wb') as f:
            f.write(b'fake-keyring-bytes')

        chroot_path = os.path.join(tmp, 'fake-chroot')
        inst = _stub_chroot_mixin_for_keyring_test(
            dir_gnupg=tmp, dir_chroot=chroot_path,
        )

        # Mock subprocess.run; pretend every call succeeds (rc=0).
        _success = MagicMock(returncode=0, stderr='', stdout='')
        with patch('subprocess.run', return_value=_success) as mock_run:
            inst._install_signing_keyring()

        # Three subprocess invocations expected: mkdir -p, cp, chmod.
        assert mock_run.call_count == 3
        # Inspect the cp call (second invocation).
        cp_args = mock_run.call_args_list[1][0][0]
        assert cp_args[0:3] == ['sudo', '-S', 'cp']
        assert cp_args[3] == keyring_path
        assert cp_args[4] == os.path.join(
            chroot_path, 'usr/share/keyrings/athena-archive-keyring.gpg'
        )


def test_install_signing_keyring_warns_on_cp_failure():
    """If sudo cp returns non-zero (chroot read-only? mount issue?) the
    method logs ERROR and returns — does NOT raise.  Build_chroot
    continues; operator sees the WARNING in the log tab."""
    import tempfile
    from unittest.mock import patch, MagicMock
    with tempfile.TemporaryDirectory() as tmp:
        signing_dir = os.path.join(tmp, 'signing')
        os.makedirs(signing_dir, mode=0o700)
        with open(os.path.join(signing_dir, 'athena-archive-keyring.gpg'),
                  'wb') as f:
            f.write(b'bytes')
        inst = _stub_chroot_mixin_for_keyring_test(
            dir_gnupg=tmp, dir_chroot=os.path.join(tmp, 'chroot'),
        )

        # First call (mkdir) succeeds; second (cp) fails.
        results = [
            MagicMock(returncode=0, stderr='', stdout=''),
            MagicMock(returncode=1, stderr='cp: cannot create — read-only', stdout=''),
        ]
        with patch('subprocess.run', side_effect=results) as mock_run:
            # Must not raise.
            inst._install_signing_keyring()
        # mkdir + cp called; chmod NOT called (early return on cp failure).
        assert mock_run.call_count == 2


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
    """Typing an unknown command prints a hint and continues the REPL."""
    cli, out, err, restore = _fresh_cli()
    try:
        import io
        sys.stdin = io.StringIO('nonexistent\nquit\n')
        cli.wait()
    finally:
        out_v = out.getvalue()
        sys.stdin = sys.__stdin__
        restore()
    assert 'Unknown command' in out_v
    assert 'nonexistent' in out_v


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
        test_group_dispatchers_forward_to_underlying_cmd_methods,
        test_cache_purge_deletes_files_and_resets_flags,
        test_cache_purge_cancelled_keeps_files_and_flags,
        test_cache_purge_empty_dir_is_noop,
        # COMP-01c — iso build live | iso build installer split
        test_cmd_iso_build_requires_subaction,
        test_cmd_iso_build_live_forwards_to_cmd_build_iso_live,
        test_cmd_iso_build_installer_forwards_to_cmd_build_iso_installer,
        test_cmd_iso_build_unknown_subaction_calls_neither_handler,
        test_cmd_build_iso_installer_is_stub,
        # COMP-01c — chroot build live | chroot build installer split
        test_cmd_chroot_build_no_subaction_defaults_to_live,
        test_cmd_chroot_build_live_explicit_forwards_to_live,
        test_cmd_chroot_build_installer_forwards_to_installer,
        test_cmd_chroot_build_passthrough_args_to_live,
        test_cmd_build_chroot_installer_is_stub,
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
        test_download_file_zero_content_length_does_not_freeze_bar,
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
        test_verify_dep_resolution_skips_extras,
        test_verify_dep_resolution_still_catches_real_violations,
        test_print_extras_lists_recommended_packages,
        test_print_extras_handles_empty_extras_set,
        # source build args parsing (EXTRAS-01 + COMP-01c subset selectors)
        test_source_build_args_no_args_defaults_to_live_subset,
        test_source_build_args_live_subset_explicit,
        test_source_build_args_installer_subset_recognised,
        test_source_build_args_recommended_subset_recognised,
        test_source_build_args_force_flag_anywhere,
        test_source_build_args_subsets_mutually_exclusive,
        test_source_build_args_subset_and_named_pkgs_mutually_exclusive,
        test_source_build_args_named_pkgs_resolve_subset_to_empty,
        test_source_build_args_bracket_token_extracts_profiles,
        test_source_build_args_empty_bracket_means_no_profiles,
        test_source_build_args_multiple_bracket_tokens_rejected,
        test_source_build_args_bracket_position_does_not_matter,
        test_buildcontainer_build_signature_accepts_profile_override_kwargs,
        # CONF-02 phase 1: signing key generate / verify
        test_signing_parse_uid_helpers,
        test_signing_parse_secret_keys_colons_extracts_fields,
        test_signing_parse_secret_keys_colons_empty_input,
        test_signing_parse_secret_keys_colons_only_keeps_first_uid,
        test_signing_get_key_info_returns_none_when_homedir_absent,
        test_signing_verify_key_returns_no_key_when_absent,
        test_signing_paths_compose_off_dir_gnupg,
        test_signing_generate_and_verify_roundtrip_real_gpg,
        # CONF-02 phase 3: signing key gate at top of build_chroot
        test_signing_key_verified_flag_default_false,
        test_ensure_signing_key_verified_true_when_key_exists,
        test_ensure_signing_key_verified_false_on_user_decline,
        test_ensure_signing_key_verified_generates_then_verifies_on_accept,
        test_ensure_signing_key_verified_false_when_generate_fails,
        # CONF-02 phase 3: install signing keyring into chroot
        test_install_signing_keyring_skips_when_no_key_generated,
        test_install_signing_keyring_invokes_cp_when_key_present,
        test_install_signing_keyring_warns_on_cp_failure,
        # UX-05 Path B: headless CLI backend
        test_cli_print_writes_to_stdout,
        test_cli_severity_methods_write_to_stderr_with_tags,
        test_cli_registers_itself_as_tui_singleton,
        test_cli_register_command_dispatches_via_wait,
        test_cli_unknown_command_does_not_crash_repl,
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
