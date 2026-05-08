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
        # STA-09
        test_download_file_returns_http_status_detail_on_404,
        test_download_file_success_returns_size_and_empty_detail,
        # STA-03
        test_shipped_build_conf_has_snapshot_enabled,
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
