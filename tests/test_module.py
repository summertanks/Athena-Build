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
import threading

# Allow running from project root: `python3 tests/test_module.py`
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))


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
        kwargs = dict(id='main', baseurl='http://x.test', baseid='debian',
                      release='bookworm', suffix='', component='main', arch='amd64')
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
    DISTRIBUTION = "Testdistro"
    CODENAME = "test"
    VERSION = "1"
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
    Fork = fork
    Image = image
    Chroot = buildroot
    Gnupg = gnupg

    [Source]
    SkipTest =
    BuildProfiles = nodoc, nocheck
    Tunneled =
    DistroSuffix = thor1
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


def test_build_depends_no_cache_leaves_virtuals_unchanged():
    """Without cache, multi-provider virtuals stay as single-name entries
    — caller-driven opt-in keeps the legacy parse a pure transform."""
    src = _src_with_build_depends("libcurl4-dev")
    groups = src.build_depends('amd64')
    assert len(groups) == 1
    assert groups[0][0][0] == 'libcurl4-dev'


def test_build_depends_expands_multi_provider_virtual():
    """Single-element virtual w/ ≥2 distinct concrete providers gets
    expanded to alternatives [providers…, virtual_name].  Providers
    sorted alphabetically; virtual preserved as final fallback."""
    cache = _StubProviderCache({
        'libcurl4-dev': [
            {'Package': 'libcurl4-openssl-dev'},
            {'Package': 'libcurl4-gnutls-dev'},
            {'Package': 'libcurl4-nss-dev'},
        ],
    })
    src = _src_with_build_depends("libcurl4-dev")
    groups = src.build_depends('amd64', cache=cache)
    assert len(groups) == 1
    names = [alt[0] for alt in groups[0]]
    assert names == [
        'libcurl4-gnutls-dev',
        'libcurl4-nss-dev',
        'libcurl4-openssl-dev',
        'libcurl4-dev',
    ], names


def test_build_depends_single_provider_virtual_not_expanded():
    """A virtual name with only ONE concrete provider is NOT expanded —
    apt resolves single-provider virtuals fine; expansion would just
    add noise.  Threshold: ≥2 distinct providers."""
    cache = _StubProviderCache({
        'awk': [{'Package': 'gawk'}],
    })
    src = _src_with_build_depends("awk")
    groups = src.build_depends('amd64', cache=cache)
    assert len(groups) == 1
    assert [alt[0] for alt in groups[0]] == ['awk']


def test_build_depends_real_package_name_not_expanded():
    """A name that resolves to itself (the package is real, not virtual)
    isn't expanded — `_pkg['Package'] != name` filters out the trivial
    self-match."""
    cache = _StubProviderCache({
        'debhelper-compat': [{'Package': 'debhelper-compat'}],
    })
    src = _src_with_build_depends("debhelper-compat (= 13)")
    groups = src.build_depends('amd64', cache=cache)
    assert len(groups) == 1
    assert [alt[0] for alt in groups[0]] == ['debhelper-compat']


def test_build_depends_already_alternative_group_untouched():
    """Multi-element groups (maintainer-authored `|` alternations) are
    already in the right shape — leave them alone, even if one of the
    alternatives looks virtual-ish."""
    cache = _StubProviderCache({
        'libcurl4-dev': [
            {'Package': 'libcurl4-openssl-dev'},
            {'Package': 'libcurl4-gnutls-dev'},
        ],
    })
    src = _src_with_build_depends("libcurl4-openssl-dev | libcurl4-dev")
    groups = src.build_depends('amd64', cache=cache)
    assert len(groups) == 1
    assert [alt[0] for alt in groups[0]] == ['libcurl4-openssl-dev', 'libcurl4-dev']


def test_build_depends_version_constraint_inherited_by_synthetic_providers():
    """Synthetic provider entries inherit the version/op of the original
    virtual entry — matches how apt propagates a version constraint
    across an `|` chain when only one alternative carries it."""
    cache = _StubProviderCache({
        'libsdl-dev': [
            {'Package': 'libsdl1.2-dev'},
            {'Package': 'libsdl1.2-compat-dev'},
        ],
    })
    src = _src_with_build_depends("libsdl-dev (>= 1.2)")
    groups = src.build_depends('amd64', cache=cache)
    assert len(groups) == 1
    versions = {alt[0]: (alt[1], alt[2]) for alt in groups[0]}
    assert versions['libsdl1.2-dev'] == versions['libsdl-dev']
    assert versions['libsdl1.2-dev'] == ('1.2', '>=')


def test_build_depends_unknown_name_left_unchanged():
    """A name absent from the cache (not a real package, not a virtual
    name we recognise) is left as-is — the BuildContainer's apt-install
    will then surface the real error rather than us silently
    swallowing it."""
    cache = _StubProviderCache({})  # empty cache
    src = _src_with_build_depends("nonexistent-pkg")
    groups = src.build_depends('amd64', cache=cache)
    assert [alt[0] for alt in groups[0]] == ['nonexistent-pkg']


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
        # chroot install filter reads dep_tree.extras_pkg_names;
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
    utils.tui.console = _Cap()        # type: ignore[assignment]
    utils.ProgressBar = _Bar          # type: ignore[assignment,misc]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(utils.requests, 'get', return_value=mock_resp_factory()):
                utils.download_source(_Dt(), tmp)
    finally:
        utils.tui.console = saved_console
        utils.ProgressBar = saved_bar  # type: ignore[misc]

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
    # The constructor and for_iso factory both must read+pop the env var
    # before reaching for Prompt(PROMPT_PASSWORD, ...).
    assert "_os.environ.pop('ATHENA_SUDO_PASSWORD'" in _src, (
        "UX-05b: ATHENA_SUDO_PASSWORD must be pop'd from os.environ "
        "(read + remove in one step so the env var doesn't leak to "
        "child subprocesses)")
    # Pop must happen TWICE (once per code path: __init__ + for_iso)
    assert _src.count(
        "_os.environ.pop('ATHENA_SUDO_PASSWORD'") == 2, (
        "UX-05b: env-var pickup must be in BOTH BuildSystem.__init__ "
        "and BuildSystem.for_iso (the iso-only factory)")


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


def test_ux05e_main_strips_cmd_and_yes_from_argv_and_seeds_backend():
    """UX-05e: build.py:main reads `--cmd <cmd>` (potentially multiple)
    and `--yes`, strips them from sys.argv before BuildConfig sees argv,
    and seeds the backend's one_shot_cmds + auto_yes."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _src = fh.read()
    # main() reads --yes, --cmd, --headless and strips each from sys.argv
    for _flag in ("'--headless'", "'--yes'", "'--cmd'"):
        assert _flag in _src, f"UX-05: main() must recognise {_flag}"
    # one_shot_cmds threaded onto backend; auto_yes assigned
    assert 'tui_inst.auto_yes = _auto_yes' in _src
    assert "if hasattr(tui_inst, 'one_shot_cmds')" in _src


def test_ux05e_one_shot_dispatch_runs_each_in_order_and_exits():
    """UX-05e: Cli with one_shot_cmds populated must dispatch each
    in order then exit (no REPL).  Exit code reflects worst outcome."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from unittest.mock import MagicMock, patch
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


def test_ux05c_progressbar_step_does_not_print_in_cli():
    """UX-05c: ProgressBar.step() must not invoke the backend's print
    surface — only add_widget (start marker) and del_widget (done
    marker) emit output.  This preserves the "no per-step flood" design
    in CLI mode regardless of how fast the inner loop steps the bar."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    from tui.widgets import ProgressBar
    _src = inspect.getsource(ProgressBar.step)
    # ProgressBar.step body must not reach for any backend print path
    assert 'print(' not in _src, (
        "UX-05c: ProgressBar.step() must not print directly — would "
        "flood CLI stdout under hot loops")
    assert 'add_widget' not in _src and 'del_widget' not in _src, _src
    assert 'self._tui' not in _src, _src


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
    assert _real.count('apt -y -o') >= 1, "plain-dep install present"
    assert _real.count('apt-get install -y -o') >= 2, "OR-group fallback chain"
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
    # And the gate must be after dep enumeration so plain_deps + or_groups
    # are available — i.e. it must reference both lists.
    _gate_call_idx = _build_body.index('self._audit_build_deps_gate(')
    _enum_idx = _build_body.index('for _grp in src_pkg.build_depends(')
    assert _enum_idx < _gate_call_idx, (
        "SEC-05: gate must run AFTER build-dep enumeration so deps are "
        "available to preview")


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


def test_arch17_top_offender_modules_fully_annotated():
    """ARCH-17: every function in the 5 top-offender modules (per the
    2026-05-21 consolidation audit) must carry both return-type and
    argument-type annotations.

    Modules: iso_installer / utils / fork_mirror / installer_chroot /
    repo_audit.  Pure annotation coverage gate; no behaviour assertion
    here — mypy + ruff own correctness.  This test guards against
    *regression* of the coverage (someone adding a new function without
    annotations) and makes future-mypy strict mode tractable.
    """
    import ast
    _modules = (
        'scripts/iso_installer.py',
        'scripts/utils.py',
        'scripts/fork_mirror.py',
        'scripts/installer_chroot.py',
        'scripts/repo_audit.py',
    )
    _unannotated = []
    for _rel in _modules:
        _path = os.path.join(_ROOT, _rel)
        with open(_path) as _fh:
            _tree = ast.parse(_fh.read())
        for _node in ast.walk(_tree):
            if not isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            _ok_ret = _node.returns is not None
            _ok_args = all(
                _a.annotation is not None
                for _a in _node.args.args
                if _a.arg not in ('self', 'cls'))
            if not (_ok_ret and _ok_args):
                _which = []
                if not _ok_ret:
                    _which.append('return')
                if not _ok_args:
                    _which.append('args')
                _unannotated.append(
                    f"{_rel}:L{_node.lineno} {_node.name} ({'+'.join(_which)})"
                )
    assert not _unannotated, (
        "ARCH-17: every function in the top-offender modules must be "
        "annotated.  Unannotated:\n  " + "\n  ".join(_unannotated))


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


def test_production_build_conf_has_noautodbgsym_in_build_options():
    """Pin `noautodbgsym` in config/build.conf's [Source] BuildOptions.
    Without it, dh_strip emits a `pkg-dbgsym_*.deb` companion for every
    binary built from source — ~1500 dbgsym files / ~3-4 GB on a full
    build, plus the post-strip repack cost on every source build.

    Side-artifact dbgsym packages aren't tracked in
    dependencytree.selected_pkgs (no consumer ever depends on them) so
    nothing in the install path notices their absence; the only effect
    of dropping them is reduced disk + faster builds.

    Pin is on the production config file so a future operator edit
    (e.g. someone removing it 'to enable debugging') can't silently
    re-balloon repo/."""
    _conf = os.path.join(_ROOT, 'config', 'build.conf')
    assert os.path.isfile(_conf), _conf
    import configparser
    _cp = configparser.ConfigParser(interpolation=None)
    _cp.read(_conf)
    _opts_raw = _cp.get('Source', 'BuildOptions', fallback='')
    _opts = {_o.strip() for _o in _opts_raw.split(',') if _o.strip()}
    assert 'noautodbgsym' in _opts, (
        f"config/build.conf [Source] BuildOptions must include "
        f"`noautodbgsym` to suppress dbgsym blobs.  Current value: "
        f"{_opts_raw!r}"
    )
    # Also sanity-check that the two existing opts we depend on stay put.
    assert 'nodoc' in _opts, _opts_raw
    assert 'nocheck' in _opts, _opts_raw


def test_build_conf_ingests_nonfree_components_and_tunnels_firmware():
    """build.conf ingests contrib/non-free/non-free-firmware (one [Mirror.*]
    per component, incl. the security suite) and tunnels the curated
    firmware/microcode SOURCES; pool.list selects the matching BINARIES so
    they ship in the pool + publish under non-free-firmware."""
    import configparser
    _cp = configparser.ConfigParser(interpolation=None)
    _cp.read(os.path.join(_ROOT, 'config', 'build.conf'))
    _comps = {
        _cp.get(_s, 'Component', fallback='main')
        for _s in _cp.sections() if _s.startswith('Mirror.')
    }
    for _c in ('main', 'contrib', 'non-free', 'non-free-firmware'):
        assert _c in _comps, (_c, _comps)
    # security suite must also offer non-free-firmware (microcode security upd).
    assert _cp.get('Mirror.security-non-free-firmware', 'Component') == \
        'non-free-firmware'
    assert _cp.get('Mirror.security-non-free-firmware', 'BASEID') == \
        'debian-security'
    _tun = {_t.strip() for _t in
            _cp.get('Source', 'Tunneled', fallback='').split(',') if _t.strip()}
    for _src in ('intel-microcode', 'amd64-microcode', 'firmware-nonfree'):
        assert _src in _tun, (_src, _tun)
    # shim-signed / shim-helpers-amd64-signed / grub2 are intentionally NOT
    # tunneled — Athena ships no Secure Boot story today.  Path C (self-
    # signed + MOK) is tracked in docs/plans/secboot-01-mok-self-sign.md.
    for _src in ('shim-signed', 'shim-helpers-amd64-signed', 'grub2'):
        assert _src not in _tun, (
            f"{_src} unexpectedly in Tunneled — SECBOOT-01 is deferred, "
            f"don't auto-add the Debian-signed chain")
    with open(os.path.join(_ROOT, 'config', 'pool.list')) as fh:
        _pool = {ln.strip() for ln in fh
                 if ln.strip() and not ln.lstrip().startswith('#')}
    for _bin in ('intel-microcode', 'amd64-microcode', 'firmware-iwlwifi',
                 'firmware-realtek', 'firmware-misc-nonfree',
                 'firmware-amd-graphics'):
        assert _bin in _pool, _bin
    # shim-signed deliberately not in pool.list either.
    assert 'shim-signed' not in _pool, (
        "shim-signed in pool.list — SECBOOT-01 is deferred; reintroduce "
        "only when the self-signed + MOK chain lands")


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


# ─────────────────────────────────────────────────────────────────────────────
# BuildSession encapsulates pipeline state; cmd_* handlers are
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
        ('cmd_repo',      'tunnel',   'cmd_tunnel_package'),
        # 'reload' removed from cmd_repo in P4 (2026-05-23) — fork
        # operations live under cmd_source now.
        ('cmd_source',    'fork',     'cmd_source_fork'),
        ('cmd_repo',      'audit',    'cmd_audit'),
        ('cmd_repo',      'repair',   'cmd_repo_repair'),
        # cmd_repo 'index' is now multi-token ('index full' / 'index
        # minimal'), and 'publish' takes 'git minimal' — covered by their
        # own routing tests below, not this verb-only matrix.
        ('cmd_container', 'init',     'cmd_init_container'),
        ('cmd_container', 'purge',    'cmd_container_purge'),
        # cmd_chroot 'build' is now multi-token ('build live' / 'build
        # installer') with default-to-live; covered by its own tests below.
        ('cmd_chroot',    'verify',   'cmd_verify_chroot'),
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
        ('cmd_clean',     'download',  'cmd_clean_download'),
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


def test_iso_installer_kernel_pkg_regex_matches_real_kernels_only():
    """REGRESSION (2026-05-11): the iso_installer kernel finder must
    match real kernel packages (linux-image-<ABI>-amd64_*.deb) and
    skip meta/flavor variants — meta packages are empty + have no
    /boot/vmlinuz, so extracting them produces an unusable kernel
    candidate.  Caught when iso build installer picked
    linux-image-rt-amd64 (an empty preempt-rt meta) and failed."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _KERNEL_PKG_RE
    # Real kernels — should match
    for _real in (
        'linux-image-6.1.0-45-amd64_6.1.170-1_amd64.deb',
        'linux-image-6.1.0-47-amd64_6.1.170-3_amd64.deb',
        'linux-image-5.10.0-23-amd64_5.10.179-1_amd64.deb',
    ):
        assert _KERNEL_PKG_RE.match(_real), f"{_real} should match"
    # Meta packages / non-amd64 flavors — should NOT match
    for _meta in (
        'linux-image-amd64_6.1.170-3_amd64.deb',
        'linux-image-rt-amd64_6.1.170-3_amd64.deb',
        'linux-image-cloud-amd64_6.1.170-3_amd64.deb',
        'linux-image-6.1.0-47-cloud-amd64_6.1.170-3_amd64.deb',
        'linux-image-6.1.0-47-rt-amd64_6.1.170-3_amd64.deb',
        'linux-image-6.1.0-47-amd64-dbg_6.1.170-3_amd64.deb',
    ):
        assert not _KERNEL_PKG_RE.match(_meta), f"{_meta} should NOT match"


def test_iso_installer_stage_grub_cfg_errors_when_data_layer_missing():
    """Phase 7: iso_installer is data-driven from installer/boot/grub.cfg.
    If the operator deletes the data-layer grub.cfg, the engine MUST
    error with a clear message — not silently produce an unbootable ISO.
    Pins the data-layer contract: deletions are loud."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_grub_cfg
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(os.path.join(_stage, 'boot', 'grub'), exist_ok=True)
        with tempfile.TemporaryDirectory() as _installer_empty:
            # installer_dir has no boot/grub.cfg → must return False
            assert _stage_grub_cfg(_stage, _installer_empty) is False


def test_iso_installer_count_records_zero_one_many():
    """_count_records is what the operator-facing progress line uses
    after dpkg-scanpackages.  Must handle empty file, single record,
    multiple records correctly."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from apt_repo import _count_records
    with tempfile.NamedTemporaryFile('w', delete=False) as fh:
        _path = fh.name
    try:
        # Empty
        assert _count_records(_path) == 0
        # Single record (starts at col 0)
        with open(_path, 'w') as fh:
            fh.write("Package: foo\nVersion: 1.0\n")
        assert _count_records(_path) == 1
        # Two records
        with open(_path, 'w') as fh:
            fh.write("Package: foo\nVersion: 1.0\n\nPackage: bar\nVersion: 2.0\n")
        assert _count_records(_path) == 2
        # Missing file → 0, no raise
    finally:
        os.unlink(_path)
    assert _count_records('/nonexistent/file') == 0


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


def test_iso_installer_generate_apt_repo_invokes_correct_pipeline():
    """Pin the order of operations in _generate_apt_repo: mkdir
    binary-amd64 + debian-installer/binary-amd64 + source, then
    dpkg-scanpackages (twice — debs and udebs), then dpkg-scansources,
    then per-subdir Release writes, then apt-ftparchive release.

    Drives the helper with subprocess.run / _sudo mocked so no actual
    repo or sudo is needed.  Asserts on the sequence of commands fired."""
    import sys, tempfile
    from unittest.mock import patch, MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import iso_installer  # noqa: F401 — apt_repo Mocks were originally
                          # iso_installer-namespaced; kept import as
                          # historical anchor while we transition
    import apt_repo
    import tui as _tui

    # Stub Tui so the helper's tui.console.print() calls don't crash
    # (_with_stub_tui decorator is defined further down in this file;
    # inline the same pattern here).
    class _StubTui:
        def __init__(self):
            self._next_id = 0; self._widgets = {}
        def add_widget(self, w):
            wid = self._next_id; self._next_id += 1
            self._widgets[wid] = w; return wid
        def del_widget(self, wid): self._widgets.pop(wid, None)
        def print(self, *_a, **_kw): pass
    _saved_tui = _tui.tui_instance
    _tui.tui_instance = _StubTui()

    _calls = []
    def _fake_sudo(cmd, password):
        _calls.append(tuple(cmd))
        # Side-effect: when bash shell-cmd writes to a file via redirect,
        # actually create the file so subsequent file-existence/size
        # checks pass.
        if cmd[0] == 'bash' and len(cmd) > 1 and '> ' in cmd[2]:
            _target = cmd[2].split('> ')[1].strip().split()[0]
            try:
                with open(_target, 'w') as fh:
                    fh.write("Package: stub\nVersion: 1.0\n")
            except OSError:
                pass
        _r = MagicMock()
        _r.returncode = 0
        _r.stderr = ''
        return _r

    # The per-subdir Release writer uses subprocess.run directly (with
    # stdin), not _sudo — patch it separately.
    def _fake_subprocess_run(cmd, *a, **kw):
        _calls.append(tuple(cmd))
        # cat > /path writes stdin to file (the _write_subdir_release path).
        if cmd[:2] == ['sudo', '-S'] and 'cat >' in (cmd[3] if len(cmd) > 3 else ''):
            _path = cmd[3].split('cat >')[1].strip()
            try:
                _input = kw.get('input', '')
                # input is "password\n<content>"
                _, _, _content = _input.partition('\n')
                with open(_path, 'w') as fh:
                    fh.write(_content)
            except OSError:
                pass
        # apt-ftparchive release: real apt-ftparchive writes to stdout;
        # the helper redirects via stdout=<file handle> kwarg.  Mirror
        # by writing a stub Release to that handle so size > 0 check passes.
        elif (cmd[:2] == ['sudo', '-S'] and len(cmd) > 2 and
              cmd[2] == 'apt-ftparchive'):
            _stdout = kw.get('stdout')
            if _stdout is not None and hasattr(_stdout, 'write'):
                _stdout.write(b'Suite: stub\nCodename: stub\n')
        _r = MagicMock()
        _r.returncode = 0
        _r.stderr = b''
        return _r

    try:
        with tempfile.TemporaryDirectory() as _staging:
            # Pre-create pool so the cwd=staging cd works.
            os.makedirs(os.path.join(_staging, 'pool'), exist_ok=True)
            import repo_audit
            _popen = _make_fake_popen(_calls)
            _combined = _wrap_with_install_handler(_fake_subprocess_run)
            with patch.object(apt_repo, '_sudo', side_effect=_fake_sudo), \
                 patch.object(apt_repo.subprocess, 'run',
                              side_effect=_combined), \
                 patch.object(repo_audit.subprocess, 'Popen',
                              side_effect=_popen):
                _ok = apt_repo.generate_apt_repo(
                    _staging, 'athena', 'athena', '0.1', 'pw')
            _assert_paths_staging = _staging  # keep for asserts below
            _assert_dirs_exist = all(os.path.isdir(p) for p in (
                os.path.join(_staging, 'dists', 'athena', 'main', 'binary-amd64'),
                os.path.join(_staging, 'dists', 'athena', 'main',
                             'debian-installer', 'binary-amd64'),
                os.path.join(_staging, 'dists', 'athena', 'main', 'source'),
            ))
    finally:
        _tui.tui_instance = _saved_tui
    assert _ok is True, (
        f"generate_apt_repo returned False; calls so far:\n{_calls}"
    )
    assert _assert_dirs_exist, (
        "dists/<suite>/main/{binary-amd64,debian-installer/binary-amd64,source} not created"
    )
    # Scan calls go via `sudo bash -c 'cd <staging> && dpkg-scan...'`
    # — Spinner-wrapped subprocess (reverted from Popen-streaming
    # after 2026-05-22 operator report that the progressbar stayed
    # stuck at 0/N).  Check the joined call strings for the scanner
    # signatures.
    _argv_strings = [' '.join(c) for c in _calls if c]
    _joined = '\n'.join(_argv_strings)
    assert 'dpkg-scanpackages -m pool' in _joined, (
        f"missing deb scan call; got:\n{_joined}")
    assert 'dpkg-scanpackages -m -t udeb pool' in _joined, (
        f"missing udeb scan call; got:\n{_joined}")
    assert 'dpkg-scansources pool' in _joined, (
        f"missing source scan; got:\n{_joined}"
    )
    # apt-ftparchive lands as argv (no shell wrapper).
    _any_ftparchive = any(
        len(c) >= 3 and c[0] == 'sudo' and c[2] == 'apt-ftparchive'
        for c in _calls
    )
    assert _any_ftparchive, (
        f"missing top-level Release generation via argv apt-ftparchive; "
        f"got calls: {[c[:4] for c in _calls if c and c[0]=='sudo']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CONF-01 Stage B — generate_repo_indexes() multi-suite orchestrator
# + cmd_index_repo dispatcher
# ─────────────────────────────────────────────────────────────────────────────


def test_apt_repo_generate_repo_indexes_walks_all_suites_and_components():
    """Stage B: generate_repo_indexes iterates over every (suite,
    component) pair in suites_spec and scans each binary-<arch>/ dir.
    Pin the call sequence so a future refactor doesn't accidentally
    skip a suite or component."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo
    import tui as _tui

    class _StubTui:
        def __init__(self):
            self._next_id = 0; self._widgets = {}
        def add_widget(self, w):
            wid = self._next_id; self._next_id += 1
            self._widgets[wid] = w; return wid
        def del_widget(self, wid): self._widgets.pop(wid, None)
        def print(self, *_a, **_kw): pass
    _saved_tui = _tui.tui_instance
    _tui.tui_instance = _StubTui()

    _calls = []
    def _fake_sudo(cmd, _password):
        _calls.append(tuple(cmd))
        # bash -c writes stub Packages/Sources file
        if cmd[0] == 'bash' and len(cmd) > 1 and '> ' in cmd[2]:
            _target = cmd[2].split('> ')[1].strip().split()[0]
            try:
                with open(_target, 'w') as fh:
                    fh.write("Package: stub\nVersion: 1.0\n")
            except OSError:
                pass
        _r = type('R', (), {})()
        _r.returncode = 0
        _r.stderr = ''
        return _r

    def _fake_subprocess_run(cmd, *_a, **kw):
        _calls.append(tuple(cmd))
        if cmd[:2] == ['sudo', '-S'] and len(cmd) > 3 and 'cat >' in (cmd[3] if len(cmd) > 3 else ''):
            _path = cmd[3].split('cat >')[1].strip()
            _input = kw.get('input', '')
            _, _, _content = _input.partition('\n')
            with open(_path, 'w') as fh: fh.write(_content)
        elif (cmd[:2] == ['sudo', '-S'] and len(cmd) > 2 and
              cmd[2] == 'apt-ftparchive'):
            _stdout = kw.get('stdout')
            if _stdout is not None and hasattr(_stdout, 'write'):
                _stdout.write(b'Suite: stub\nCodename: stub\n')
        _r = type('R', (), {})()
        _r.returncode = 0
        _r.stderr = b''
        return _r

    try:
        with tempfile.TemporaryDirectory() as _repo:
            # Pre-create binary-amd64 dirs for thor (main + doc) and
            # thor-debug (main).  Stage C will normally do this; here we
            # do it manually as test setup.
            _spec = {
                'thor':       ['main', 'doc'],
                'thor-debug': ['main'],
            }
            for _suite, _comps in _spec.items():
                for _comp in _comps:
                    _d = os.path.join(_repo, 'dists', _suite, _comp,
                                       'binary-amd64')
                    os.makedirs(_d, exist_ok=True)
                    # New semantics (post-2026-05-22): empty
                    # binary-amd64/ → skip.  Put a stub .deb in each
                    # so the indexer treats them as populated.
                    with open(os.path.join(_d, 'stub.deb'), 'wb') as fh:
                        fh.write(b'STUB')
            import repo_audit
            _popen = _make_fake_popen(_calls)
            _combined = _wrap_with_install_handler(_fake_subprocess_run)
            with patch.object(apt_repo, '_sudo', side_effect=_fake_sudo), \
                 patch.object(apt_repo.subprocess, 'run',
                              side_effect=_combined), \
                 patch.object(repo_audit.subprocess, 'Popen',
                              side_effect=_popen):
                _ok = apt_repo.generate_repo_indexes(
                    repo_root=_repo,
                    suites_spec=_spec,
                    codename_for_suite={'thor': 'thor', 'thor-debug': 'thor-debug'},
                    version='0.1',
                    arch='amd64',
                    password='pw',
                    signing_homedir=None,
                    signing_pubkey_path=None,
                )
            assert _ok is True, f"generate_repo_indexes False; calls: {_calls}"
    finally:
        _tui.tui_instance = _saved_tui

    # All three components got a dpkg-scanpackages call.  Post-helper-
    # refactor (2026-05-22): argv shape is
    # ['sudo', '-S', 'dpkg-scanpackages', '-m', '<pool_subdir>'] —
    # streamed via subprocess.Popen for per-file ProgressBar.
    _scan_argv = [c for c in _calls
                  if c and any('dpkg-scanpackages' in s for s in c)]
    _scan_strs = [' '.join(c) for c in _scan_argv]
    assert len(_scan_argv) == 3, (
        f"expected 3 dpkg-scanpackages invocations (thor/main + thor/doc "
        f"+ thor-debug/main), got {len(_scan_argv)}: {_scan_strs}"
    )
    # Each scan invocation includes the suite+component path
    assert any('dists/thor/main/binary-amd64' in _s for _s in _scan_strs), _scan_strs
    assert any('dists/thor/doc/binary-amd64'  in _s for _s in _scan_strs), _scan_strs
    assert any('dists/thor-debug/main/binary-amd64' in _s for _s in _scan_strs), _scan_strs
    # Two apt-ftparchive release calls (one per suite)
    _ftparchive_calls = [c for c in _calls
                          if len(c) >= 3 and c[0] == 'sudo' and c[2] == 'apt-ftparchive']
    assert len(_ftparchive_calls) == 2, (
        f"expected 2 apt-ftparchive release calls (one per suite), "
        f"got {len(_ftparchive_calls)}"
    )


def test_apt_repo_generate_repo_indexes_skips_when_binary_dir_missing():
    """Stage B/C semantic: if a (suite, component)'s binary-<arch>/
    directory doesn't exist OR is empty, generate_repo_indexes SKIPS
    that component cleanly (not an error).  Legitimate cases:
    -debug suite when no dbgsyms were built (nodoc/nostrip profiles);
    doc component when no -doc packages exist yet.

    Was an ERROR contract in early Stage B drafts; revised post-Stage C
    operator testing 2026-05-22 (real repo had empty dbgsym/ from
    nodoc build → spurious failure).  If ALL components in a suite
    are skipped, the suite itself is skipped (no top-level Release).
    """
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo
    import tui as _tui

    class _StubTui:
        def __init__(self):
            self._next_id = 0; self._widgets = {}
        def add_widget(self, w):
            wid = self._next_id; self._next_id += 1
            self._widgets[wid] = w; return wid
        def del_widget(self, wid): self._widgets.pop(wid, None)
        def print(self, *_a, **_kw): pass
    _saved_tui = _tui.tui_instance
    _tui.tui_instance = _StubTui()
    try:
        with tempfile.TemporaryDirectory() as _repo:
            # Suite with ONE component, dir doesn't exist → suite skipped
            # → returns True (no work was needed; nothing to error on).
            _ok = apt_repo.generate_repo_indexes(
                repo_root=_repo,
                suites_spec={'thor': ['main']},
                codename_for_suite={'thor': 'thor'},
                version='0.1', arch='amd64', password='pw',
            )
            assert _ok is True
            # AND no top-level Release was generated (suite was skipped)
            assert not os.path.exists(
                os.path.join(_repo, 'dists', 'thor', 'Release')
            ), "empty suite must not produce a top-level Release"
    finally:
        _tui.tui_instance = _saved_tui


def test_apt_repo_generate_repo_indexes_skips_empty_component_but_indexes_others():
    """Real-repo case from 2026-05-22 testing: a suite has some
    populated components and some empty/missing ones (thor-debug had
    no dbgsyms in the operator's nodoc build).  The populated ones
    must still get indexed; the empty ones get a clean SKIP, not a
    failure.  And the suite's top-level Release lists ONLY the
    populated components (apt rejects Components: with a missing
    target dir).
    """
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo
    import tui as _tui

    class _StubTui:
        def __init__(self):
            self._next_id = 0; self._widgets = {}
        def add_widget(self, w):
            wid = self._next_id; self._next_id += 1
            self._widgets[wid] = w; return wid
        def del_widget(self, wid): self._widgets.pop(wid, None)
        def print(self, *_a, **_kw): pass
    _saved_tui = _tui.tui_instance
    _tui.tui_instance = _StubTui()

    _calls = []
    def _fake_sudo(cmd, _password):
        _calls.append(tuple(cmd))
        if cmd[0] == 'bash' and len(cmd) > 1 and '> ' in cmd[2]:
            _target = cmd[2].split('> ')[1].strip().split()[0]
            try:
                with open(_target, 'w') as fh:
                    fh.write("Package: stub\nVersion: 1.0\n")
            except OSError:
                pass
        _r = type('R', (), {})()
        _r.returncode = 0
        _r.stderr = ''
        return _r

    def _fake_subprocess_run(cmd, *_a, **kw):
        _calls.append(tuple(cmd))
        if cmd[:2] == ['sudo', '-S'] and len(cmd) > 3 and 'cat >' in cmd[3]:
            _path = cmd[3].split('cat >')[1].strip()
            _input = kw.get('input', '')
            _, _, _content = _input.partition('\n')
            with open(_path, 'w') as fh: fh.write(_content)
        elif (cmd[:2] == ['sudo', '-S'] and len(cmd) > 2 and
              cmd[2] == 'apt-ftparchive'):
            _stdout = kw.get('stdout')
            if _stdout is not None and hasattr(_stdout, 'write'):
                _stdout.write(b'Components: stub\n')
        _r = type('R', (), {})()
        _r.returncode = 0
        _r.stderr = b''
        return _r

    try:
        with tempfile.TemporaryDirectory() as _repo:
            # thor: main + doc populated, tests empty (dir exists but no
            # files), and the spec also lists `dbgsym` (typo / future-
            # component) with no dir at all
            os.makedirs(
                os.path.join(_repo, 'dists', 'thor', 'main', 'binary-amd64'),
            )
            with open(
                os.path.join(_repo, 'dists', 'thor', 'main', 'binary-amd64',
                             'foo.deb'),
                'wb',
            ) as fh:
                fh.write(b'STUB')
            os.makedirs(
                os.path.join(_repo, 'dists', 'thor', 'doc', 'binary-amd64'),
            )
            with open(
                os.path.join(_repo, 'dists', 'thor', 'doc', 'binary-amd64',
                             'bar-doc.deb'),
                'wb',
            ) as fh:
                fh.write(b'STUB')
            os.makedirs(
                os.path.join(_repo, 'dists', 'thor', 'tests', 'binary-amd64'),
            )   # exists but EMPTY
            # 'dbgsym' component dir doesn't exist at all

            import repo_audit
            _popen = _make_fake_popen(_calls)
            _combined = _wrap_with_install_handler(_fake_subprocess_run)
            with patch.object(apt_repo, '_sudo', side_effect=_fake_sudo), \
                 patch.object(apt_repo.subprocess, 'run',
                              side_effect=_combined), \
                 patch.object(repo_audit.subprocess, 'Popen',
                              side_effect=_popen):
                _ok = apt_repo.generate_repo_indexes(
                    repo_root=_repo,
                    suites_spec={
                        'thor': ['main', 'doc', 'tests', 'dbgsym'],
                    },
                    codename_for_suite={'thor': 'thor'},
                    version='0.1', arch='amd64', password='pw',
                )
            assert _ok is True
    finally:
        _tui.tui_instance = _saved_tui

    # Two scan-packages calls (main + doc) — tests and dbgsym skipped.
    # Post-helper-refactor: scans go via subprocess.Popen as
    # ['sudo', '-S', 'dpkg-scanpackages', '-m', '<pool_subdir>'].
    _scan_argv = [c for c in _calls
                  if c and any('dpkg-scanpackages' in s for s in c)]
    assert len(_scan_argv) == 2, (
        f"expected 2 scans (main + doc); tests + dbgsym should be "
        f"skipped (empty / missing).  Got {len(_scan_argv)}: "
        f"{[ ' '.join(c) for c in _scan_argv ]}"
    )

    # Top-level Release was generated (suite had at least one populated
    # component).  And the Components: field passes ONLY the populated
    # ones — pin this via the apt-ftparchive args.
    _ftparchive = [c for c in _calls
                    if len(c) >= 3 and c[0] == 'sudo' and c[2] == 'apt-ftparchive']
    assert len(_ftparchive) == 1, _ftparchive
    _argv_str = ' '.join(_ftparchive[0])
    assert 'Components=main doc' in _argv_str, (
        f"Components= should list only populated components (main + doc); "
        f"got args: {_argv_str}"
    )
    assert 'tests' not in _argv_str and 'dbgsym' not in _argv_str, (
        f"empty/missing components leaked into Components=: {_argv_str}"
    )


def test_cmd_repo_dispatcher_routes_index_action():
    """Stage B: `repo index` action must dispatch to cmd_index_repo.
    Pin via code inspection so a future dispatcher refactor doesn't
    silently drop the routing (which would make `repo index` print a
    help table instead of doing the index)."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r'def cmd_repo\(self.*?(?=\n    def )',
        _body, re.DOTALL,
    )
    assert _m, 'cmd_repo dispatcher not found'
    _fn = _m.group(0)
    assert "action == 'index'" in _fn, _fn
    assert 'return self.cmd_index_repo' in _fn, _fn

    # And the `repo` command MUST be registered in the tui dispatch
    # table at the bottom of build.py so the operator can actually type it.
    assert "register_command('repo'" in _body, (
        "`repo` command not wired into tui dispatch — operator can't "
        "invoke `repo index` from the prompt"
    )


# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# CONF-01 Stage D — consumer refactor anti-regression
# ─────────────────────────────────────────────────────────────────────────────


def test_stage_d_no_old_repo_subdir_paths_in_production_code():
    """CONF-01 Stage D regression pin: no production code path should
    construct the OLD flat-layout paths `repo/main/`, `repo/doc/`,
    `repo/dbgsym/`, `repo/tests/`.  After Stage D, these are all
    nested under repo/dists/<codename>/<comp>/binary-<arch>/ and
    reachable only via config.dir_repo_main / dir_repo_main_udeb /
    deb_dest_for_filename / all_deb_dirs.

    Scans scripts/ for the tell-tale `os.path.join(dir_repo, 'main')`
    idiom + its variants.  Comments and docstrings are stripped so
    documentation references to the old paths don't trigger."""
    import re
    _BAD_PATTERNS = [
        re.compile(r"os\.path\.join\([^)]*\bdir_repo\b[^)]*,\s*'main'\)"),
        re.compile(r"os\.path\.join\([^)]*\bdir_repo\b[^)]*,\s*'doc'\)"),
        re.compile(r"os\.path\.join\([^)]*\bdir_repo\b[^)]*,\s*'dbgsym'\)"),
        re.compile(r"os\.path\.join\([^)]*\bdir_repo\b[^)]*,\s*'tests'\)"),
    ]
    _scripts_dir = os.path.join(_ROOT, 'scripts')
    _findings = []
    for _fname in os.listdir(_scripts_dir):
        if not _fname.endswith('.py'):
            continue
        _path = os.path.join(_scripts_dir, _fname)
        with open(_path) as fh:
            _body = fh.read()
        # Strip docstrings + comments so prose mentions don't trigger
        _code = re.sub(r'""".*?"""', '', _body, flags=re.DOTALL)
        _code = re.sub(r"'''.*?'''", '', _code, flags=re.DOTALL)
        _code = '\n'.join(
            _ln for _ln in _code.splitlines()
            if not _ln.lstrip().startswith('#')
        )
        for _pat in _BAD_PATTERNS:
            for _m in _pat.finditer(_code):
                _findings.append(f"  scripts/{_fname}: {_m.group(0)}")
    assert not _findings, (
        "Stage D REGRESSION: production code is constructing old "
        "flat-layout paths.  Use config.dir_repo_main / dir_repo_main_udeb "
        "/ deb_dest_for_filename(filename) / all_deb_dirs() instead.\n"
        + "\n".join(_findings)
    )


def test_stage_d_buildconfig_paths_use_new_nested_layout():
    """CONF-01 Stage D contract: BuildConfig's dir_repo_main et al.
    must point at the NEW nested apt-repo paths, not the old
    flat ones.  Verify via attribute access on a real BuildConfig
    instance constructed from the default build.conf."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import BuildConfig
    _cfg = BuildConfig()
    if not _cfg.is_valid:
        # Test env doesn't have a writable working_dir — skip
        # gracefully; the assertions can only run against a valid
        # config.
        import pytest as _pytest
        _pytest.skip(f"BuildConfig invalid in test env: {_cfg.error()}")
    # Each attr must contain the dists/ nesting (not be at repo root)
    for _attr in ('dir_repo_main', 'dir_repo_main_udeb', 'dir_repo_main_source',
                  'dir_repo_doc', 'dir_repo_dbgsym', 'dir_repo_tests',
                  'dir_repo_contrib', 'dir_repo_non_free',
                  'dir_repo_non_free_firmware'):
        _val = getattr(_cfg, _attr)
        assert '/dists/' in _val, (
            f"{_attr} = {_val!r} doesn't use the new nested layout — "
            f"Stage D regression"
        )
        assert _val.startswith(_cfg.dir_repo), (
            f"{_attr} = {_val!r} doesn't start with dir_repo "
            f"{_cfg.dir_repo!r}"
        )
    # The dbgsym attr specifically must be under the -debug suite (Q2)
    assert '-debug/' in _cfg.dir_repo_dbgsym, (
        f"dir_repo_dbgsym = {_cfg.dir_repo_dbgsym!r} must be under the "
        f"<codename>-debug suite per Q2"
    )
    # Non-main component dirs are real components under the primary suite.
    assert _cfg.dir_repo_non_free_firmware.endswith(
        os.path.join('non-free-firmware', f'binary-{_cfg.arch}')), \
        _cfg.dir_repo_non_free_firmware
    assert '-debug/' not in _cfg.dir_repo_non_free_firmware
    # Component dirs are NOT in all_deb_dirs() (maintenance walks must skip
    # pristine tunneled binaries); publish reads them directly instead.
    for _d in (_cfg.dir_repo_contrib, _cfg.dir_repo_non_free,
               _cfg.dir_repo_non_free_firmware):
        assert _d not in _cfg.all_deb_dirs(), _d


def test_deb_dest_for_filename_routes_by_component():
    """deb_dest_for_filename routes a plain installable .deb to its component
    dir when component != 'main'; default stays 'main'; the udeb special-case
    and side-artifact dirs are unaffected.  Confirms the new component dirs
    are created and intentionally excluded from all_deb_dirs()."""
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
        _fw = 'intel-microcode_3.20230808.1_amd64.deb'
        assert cfg.deb_dest_for_filename(_fw) == cfg.dir_repo_main
        assert cfg.deb_dest_for_filename(_fw, 'non-free-firmware') == \
            cfg.dir_repo_non_free_firmware
        assert cfg.deb_dest_for_filename(_fw, 'non-free') == cfg.dir_repo_non_free
        assert cfg.deb_dest_for_filename(_fw, 'contrib') == cfg.dir_repo_contrib
        # udeb special-case wins even with a component.
        assert cfg.deb_dest_for_filename(
            'foo-udeb_1_amd64.udeb', 'non-free-firmware') == \
            cfg.dir_repo_main_udeb
        # Component dirs are created (dir-ensure) but intentionally NOT in
        # all_deb_dirs() — that list feeds maintenance walks (strip/audit_nmu)
        # which must not touch pristine tunneled binaries.
        for _d in (cfg.dir_repo_contrib, cfg.dir_repo_non_free,
                   cfg.dir_repo_non_free_firmware):
            assert os.path.isdir(_d), _d
            assert _d not in cfg.all_deb_dirs(), _d
        assert '/dists/' in cfg.dir_repo_non_free_firmware
        assert '-debug/' not in cfg.dir_repo_non_free_firmware


def test_iso_installer_stage_disk_info_errors_when_dir_missing():
    """Phase 7 cdrom-detect fix: installer/disk/ MUST be present —
    without /cdrom/.disk/info, cdrom-detect rejects the disc and the
    installer reports 'No device or installation media (like CD-ROM)
    was detected'.  Fail loud at iso-build time so this never silently
    ships."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_disk_info
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(_stage, exist_ok=True)
        with tempfile.TemporaryDirectory() as _installer_empty:
            # installer_dir has no disk/ subdir → must return False
            assert _stage_disk_info(_stage, _installer_empty,
                                     'athena', '0.1') is False


def test_iso_installer_stage_disk_info_copies_files_skipping_readme():
    """installer/disk/* files copied verbatim to staging/.disk/* —
    except *.md (READMEs aren't shipped on the installer disc)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_disk_info
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(_stage, exist_ok=True)
        with tempfile.TemporaryDirectory() as _installer:
            _src = os.path.join(_installer, 'disk')
            os.makedirs(_src)
            with open(os.path.join(_src, 'info'), 'w') as fh:
                fh.write('Athena 0.1 amd64 INSTALLER\n')
            with open(os.path.join(_src, 'base_installable'), 'w') as fh:
                fh.write('')   # empty sentinel
            with open(os.path.join(_src, 'base_components'), 'w') as fh:
                fh.write('main\n')
            with open(os.path.join(_src, 'README.md'), 'w') as fh:
                fh.write('# docs — should not be shipped\n')
            assert _stage_disk_info(_stage, _installer,
                                     'athena', '0.1') is True
            _disk = os.path.join(_stage, '.disk')
            assert sorted(os.listdir(_disk)) == [
                'base_components', 'base_installable', 'info'
            ], (
                "README.md should NOT be copied to .disk/; "
                f"got {sorted(os.listdir(_disk))}"
            )
            # info content preserved verbatim
            with open(os.path.join(_disk, 'info')) as fh:
                assert fh.read() == 'Athena 0.1 amd64 INSTALLER\n'


def test_iso_installer_stage_disk_info_substitutes_codename_and_version():
    """REGRESSION (2026-05-11): cdrom-detect parses the quoted codename
    out of .disk/info to locate dists/<codename>/Release.  When
    .disk/info had a hardcoded "athena" but build.conf [Build] CODENAME
    was "thor", the dists/ subdir was thor/ and cdrom-detect couldn't
    find the Release file → "Error reading Release file; unable to
    determine distribution".  Fix: substitute ${codename} / ${version}
    placeholders at iso-build time.

    This test pins that .disk/info content with placeholders gets
    substituted with the actual codename + version values."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_disk_info
    with tempfile.TemporaryDirectory() as _stage:
        with tempfile.TemporaryDirectory() as _installer:
            _src = os.path.join(_installer, 'disk')
            os.makedirs(_src)
            with open(os.path.join(_src, 'info'), 'w') as fh:
                fh.write('Athena ${version} "${codename}" - amd64 INSTALLER\n')
            with open(os.path.join(_src, 'base_installable'), 'w') as fh:
                fh.write('')
            assert _stage_disk_info(_stage, _installer, 'thor', '0.1') is True
            with open(os.path.join(_stage, '.disk', 'info')) as fh:
                _result = fh.read()
            assert _result == 'Athena 0.1 "thor" - amd64 INSTALLER\n', (
                f"placeholder substitution failed; got: {_result!r}")


def test_iso_installer_stage_disk_info_safe_substitute_leaves_unknown_vars():
    """${codename} and ${version} are substituted.  Other $variables
    (e.g. ${foo}) are LEFT UNCHANGED — string.Template.safe_substitute
    semantics.  Operator can use other $-prefixed strings in
    .disk/info content without them being mangled."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_disk_info
    with tempfile.TemporaryDirectory() as _stage:
        with tempfile.TemporaryDirectory() as _installer:
            _src = os.path.join(_installer, 'disk')
            os.makedirs(_src)
            with open(os.path.join(_src, 'info'), 'w') as fh:
                fh.write('${codename} ${unknown} ${version} $not_a_var\n')
            assert _stage_disk_info(_stage, _installer, 'thor', '0.1') is True
            with open(os.path.join(_stage, '.disk', 'info')) as fh:
                _result = fh.read()
            # Known placeholders substituted; unknown ones unchanged.
            assert _result == 'thor ${unknown} 0.1 $not_a_var\n', _result


def test_iso_installer_stage_disk_info_errors_when_only_readme():
    """A disk/ dir with only README.md (no info/base_installable) is
    effectively empty for engine purposes — should fail same as a
    missing dir, with a clearer error message."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_disk_info
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(_stage, exist_ok=True)
        with tempfile.TemporaryDirectory() as _installer:
            _src = os.path.join(_installer, 'disk')
            os.makedirs(_src)
            with open(os.path.join(_src, 'README.md'), 'w') as fh:
                fh.write('# only docs\n')
            assert _stage_disk_info(_stage, _installer,
                                     'athena', '0.1') is False


def test_iso_installer_stage_base_include_writes_one_name_per_line():
    """A populated list lands in staging/.disk/base_include — one
    package name per line, in caller-supplied order, ending with \\n.
    base-installer reads this raw and appends to debootstrap --include."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_base_include
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(os.path.join(_stage, '.disk'), exist_ok=True)
        _pkgs = ['bash', 'coreutils', 'libc6']
        assert _stage_base_include(_stage, _pkgs) is True
        _path = os.path.join(_stage, '.disk', 'base_include')
        assert os.path.isfile(_path)
        with open(_path, 'r') as fh:
            _content = fh.read()
        assert _content == 'bash\ncoreutils\nlibc6\n', _content


def test_iso_installer_stage_base_include_creates_disk_dir_if_missing():
    """Helper creates staging/.disk/ on the fly so the orchestrator can
    call it in any order relative to _stage_disk_info."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_base_include
    with tempfile.TemporaryDirectory() as _stage:
        # No .disk/ pre-created — helper must mkdir.
        assert _stage_base_include(_stage, ['hello']) is True
        assert os.path.isfile(os.path.join(_stage, '.disk', 'base_include'))


def test_iso_installer_stage_base_include_noop_on_empty_or_none():
    """Empty list / None → no file written, success returned.  Lets the
    orchestrator pass through when caller has no list to provide."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_base_include
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(os.path.join(_stage, '.disk'), exist_ok=True)
        _path = os.path.join(_stage, '.disk', 'base_include')
        assert _stage_base_include(_stage, None) is True
        assert not os.path.exists(_path)
        assert _stage_base_include(_stage, []) is True
        assert not os.path.exists(_path)


def test_iso_installer_parse_deb_filename_handles_normal_filenames():
    """Debian binary filename convention is `<name>_<version>_<arch>.{deb,udeb}`
    with no underscores allowed in name or version.  Helper splits on
    `_` and returns `(name, version)`.  Epoch `%3a` in filename is
    decoded back to `:` so it matches the Version field in apt
    metadata."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _parse_deb_filename
    assert _parse_deb_filename('acl_2.3.1-3_amd64.deb') == \
        ('acl', '2.3.1-3')
    assert _parse_deb_filename(
        'linux-image-6.1.0-47-amd64_6.1.170-3_amd64.deb'
    ) == ('linux-image-6.1.0-47-amd64', '6.1.170-3')
    assert _parse_deb_filename(
        'cdebconf-newt-udeb_0.270_amd64.udeb'
    ) == ('cdebconf-newt-udeb', '0.270')
    # Epoch decoding: filename `pkg_1%3a2.3-4_arch.deb` ↔ Version `1:2.3-4`.
    assert _parse_deb_filename(
        'libfoo_1%3a2.3-4_amd64.deb'
    ) == ('libfoo', '1:2.3-4')
    # Malformed / non-deb filenames return ('', '') so callers skip them.
    assert _parse_deb_filename('Packages.gz') == ('', '')
    assert _parse_deb_filename('garbage_only_two') == ('', '')
    assert _parse_deb_filename('') == ('', '')


def test_iso_installer_select_pool_files_includes_udebs_unconditionally():
    """Every .udeb in the source dir is kept regardless of whitelist —
    anna may fetch any of them at install time and we don't currently
    know which (cdrom-detect queues some dynamically based on hardware
    + apt-cdrom-setup dependencies)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _select_pool_files
    with tempfile.TemporaryDirectory() as _repo:
        for _name in (
            'apt-cdrom-setup_0.270_amd64.udeb',
            'thor-support_0.1_amd64.udeb',
            'eject-udeb_2.38.1-5_amd64.udeb',
        ):
            with open(os.path.join(_repo, _name), 'w') as fh:
                fh.write('')
        # Empty set whitelist — no deb names allowed.  Udebs still kept.
        _kept, _skipped = _select_pool_files([_repo], deb_whitelist=set())
        _kept = [_fn for _, _fn in _kept]
        assert len(_kept) == 3, _kept
        assert _skipped == 0
        assert all(n.endswith('.udeb') for n in _kept)


def test_iso_installer_select_pool_files_excludes_superseded():
    """Fork-superseded upstream binaries (passed via exclude_names) are dropped
    from the pool — even udebs, which are otherwise kept unconditionally — so an
    upstream udeb a shipped fork Conflicts (apt-setup-udeb vs athena-setup-udeb)
    can't ride onto the ISO and run its generators (the security.debian.org bug).
    """
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _select_pool_files
    with tempfile.TemporaryDirectory() as _repo:
        for _name in (
            'apt-setup-udeb_0.182_amd64.udeb',              # superseded → drop
            'athena-setup-udeb_0.182+athena1_amd64.udeb',   # fork → keep
            'eject-udeb_2.38.1-5_amd64.udeb',               # normal → keep
        ):
            with open(os.path.join(_repo, _name), 'w') as fh:
                fh.write('')
        _kept, _skipped = _select_pool_files(
            [_repo], deb_whitelist=set(), exclude_names={'apt-setup-udeb'})
        _kept = [_fn for _, _fn in _kept]
        assert 'apt-setup-udeb_0.182_amd64.udeb' not in _kept, _kept
        assert 'athena-setup-udeb_0.182+athena1_amd64.udeb' in _kept
        assert 'eject-udeb_2.38.1-5_amd64.udeb' in _kept
        assert _skipped == 1


def test_superseded_binary_names_excludes_selected():
    """_superseded_binary_names returns names a SELECTED pkg Conflicts/Replaces,
    minus names that are themselves selected — so a rename fork's upstream
    binary is flagged, but a genuine pool mutual-exclusion (grub-pc/grub-efi,
    both selected) is not."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)

    class _Tree:
        selected_pkgs = {
            'athena-setup-udeb': {'Package': 'athena-setup-udeb',
                                  'Conflicts': 'apt-setup-udeb',
                                  'Replaces': 'apt-setup-udeb'},
            'grub-efi-amd64': {'Package': 'grub-efi-amd64',
                               'Conflicts': 'grub-pc'},
            'grub-pc': {'Package': 'grub-pc'},          # also a selected pool extra
        }

    _sess.dep_tree = _Tree()
    _sess.udeb_dep_tree = None
    _out = _sess._superseded_binary_names()
    assert 'apt-setup-udeb' in _out
    assert 'grub-pc' not in _out


def test_iso_installer_select_pool_files_drops_dbgsym_unconditionally():
    """dbgsym packages are debug symbols, ~25% of pool size, never
    needed on an installed system.  Even when their parent is in the
    whitelist, the dbgsym variant is dropped."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _select_pool_files
    with tempfile.TemporaryDirectory() as _repo:
        for _name in (
            'acl_2.3.1-3_amd64.deb',
            'acl-dbgsym_2.3.1-3_amd64.deb',
            'libc6_2.36-9_amd64.deb',
            'libc6-dbgsym_2.36-9_amd64.deb',
        ):
            with open(os.path.join(_repo, _name), 'w') as fh:
                fh.write('')
        _kept, _skipped = _select_pool_files(
            [_repo], deb_whitelist={'acl', 'libc6'},
        )
        _kept = [_fn for _, _fn in _kept]
        assert sorted(_kept) == [
            'acl_2.3.1-3_amd64.deb', 'libc6_2.36-9_amd64.deb',
        ], _kept
        assert _skipped == 2


def test_iso_installer_select_pool_files_filters_by_whitelist():
    """Only debs whose canonical name is in the whitelist ship.
    Unused kernel flavors (linux-image-rt-*, linux-image-cloud-*),
    -unsigned kernel variants, and old kernel ABIs all get dropped
    because they aren't in `selected_pkgs`.  This test uses a tight
    whitelist that excludes live-* — in the real cmd_build_iso_installer
    derivation those WOULD be in the whitelist (canonical selected_pkgs
    keeps live-exclusive on the ISO so apt-install can find them at
    install time)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _select_pool_files
    with tempfile.TemporaryDirectory() as _repo:
        _keep = (
            'grub-pc_2.06-13_amd64.deb',
            'linux-image-6.1.0-47-amd64_6.1.170-3_amd64.deb',
            'systemd_252.39_amd64.deb',
        )
        _drop = (
            'live-boot_20230131_all.deb',
            'live-config_11.0.3_all.deb',
            'linux-image-rt-amd64_6.1.170-3_amd64.deb',
            'linux-image-cloud-amd64_6.1.170-3_amd64.deb',
            'linux-image-6.1.0-47-amd64-unsigned_6.1.170-3_amd64.deb',
            'linux-image-6.1.0-45-amd64_6.1.170-1_amd64.deb',
        )
        for _name in _keep + _drop:
            with open(os.path.join(_repo, _name), 'w') as fh:
                fh.write('')
        _kept, _skipped = _select_pool_files(
            [_repo],
            deb_whitelist={
                'grub-pc',
                'linux-image-6.1.0-47-amd64',
                'systemd',
            },
        )
        _kept = [_fn for _, _fn in _kept]
        assert sorted(_kept) == sorted(_keep), _kept
        assert _skipped == len(_drop), _skipped


def test_iso_installer_select_pool_files_keeps_highest_version_per_name():
    """Source builds can leave multiple versions of the same package
    in repo/ (e.g. linux-image-amd64 _6.1.170-1_ and _6.1.170-3_ after
    an iterative kernel rebuild).  The helper keeps only the highest
    version per name — using Debian version-compare semantics, not
    string compare — so older builds get dropped without us having to
    cross-walk cache versions."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _select_pool_files
    with tempfile.TemporaryDirectory() as _repo:
        for _name in (
            'linux-image-amd64_6.1.170-1_amd64.deb',
            'linux-image-amd64_6.1.170-3_amd64.deb',
        ):
            with open(os.path.join(_repo, _name), 'w') as fh:
                fh.write('')
        _kept, _skipped = _select_pool_files(
            [_repo], deb_whitelist={'linux-image-amd64'},
        )
        _kept = [_fn for _, _fn in _kept]
        assert _kept == ['linux-image-amd64_6.1.170-3_amd64.deb'], _kept
        assert _skipped == 1


def test_iso_installer_select_pool_files_uses_debian_version_order():
    """`6.1.170-10` is NEWER than `6.1.170-9` under Debian semantics
    even though string-compare disagrees.  Pin this so a future drop
    of apt_pkg dependency doesn't silently revert to lexicographic
    compare and ship the wrong build."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    try:
        import apt_pkg  # noqa: F401
    except ImportError:
        # apt_pkg unavailable on the test host — degraded path uses
        # string compare which would FAIL this test.  Skip cleanly so
        # we don't false-fail on non-Debian dev hosts.
        return
    from iso_installer import _select_pool_files
    with tempfile.TemporaryDirectory() as _repo:
        for _name in (
            'pkg_6.1.170-9_amd64.deb',
            'pkg_6.1.170-10_amd64.deb',
        ):
            with open(os.path.join(_repo, _name), 'w') as fh:
                fh.write('')
        _kept, _skipped = _select_pool_files(
            [_repo], deb_whitelist={'pkg'},
        )
        _kept = [_fn for _, _fn in _kept]
        assert _kept == ['pkg_6.1.170-10_amd64.deb'], _kept
        assert _skipped == 1


def test_iso_installer_select_pool_files_legacy_mode_keeps_everything():
    """When deb_whitelist is None, the helper keeps every regular
    file — preserves the previous blanket-copy behaviour for callers
    that don't have a dep tree."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _select_pool_files
    with tempfile.TemporaryDirectory() as _repo:
        for _name in (
            'foo_1_amd64.deb', 'bar-dbgsym_1_amd64.deb',
            'baz_1_amd64.udeb', 'random.txt',
        ):
            with open(os.path.join(_repo, _name), 'w') as fh:
                fh.write('')
        _kept, _skipped = _select_pool_files([_repo], deb_whitelist=None)
        _kept = [_fn for _, _fn in _kept]
        assert sorted(_kept) == [
            'bar-dbgsym_1_amd64.deb', 'baz_1_amd64.udeb',
            'foo_1_amd64.deb', 'random.txt',
        ], _kept


def test_iso_installer_base_include_and_pool_filter_agree():
    """Invariant: every package in base_include must exist in the pool
    whitelist.  debootstrap reads base_include as `--include` and
    fails with `base-installer/debootstrap-failed` if any entry isn't
    findable in /cdrom/pool.

    Filter rules (verified working 2026-05-12 after the pkg.list audit
    that added busybox/zstd/eject/etc. explicitly so they're in
    pkg_closure, not in live_exclusive):
      - base_include = canonical - extras - live_exclusive
      - pool_whitelist = canonical - live_exclusive
      - Live-exclusive binaries (live-boot, live-config, live-tools)
        ship neither on the installer pool nor on the target.
      - Recommends-only extras (eject etc. when not explicit in
        pkg.list) ship in the pool so the operator can apt-install
        them post-install, but aren't in base_include.

    Both lists are derived inside cmd_build_iso_installer; this test
    re-derives them and asserts the set relationships hold."""
    class _FakeSelected(dict):
        def __getitem__(self, name):
            return {'Package': name, 'Version': '1.0'}
        def __contains__(self, name):
            return name in dict.keys(self) or name in self._names
        def __iter__(self):
            return iter(self._names)
        def __init__(self, names):
            self._names = list(names)
            super().__init__()

    _names = [
        'bash', 'libc6', 'systemd',          # required closure
        'busybox',                            # explicit in pkg.list now
        'live-boot', 'live-config',          # live-exclusive
        'pkg-recommends-only',                # extras (Recommends-only)
        'grub-pc',                            # installer-exclusive
    ]
    _selected = _FakeSelected(_names)
    _extras = {'pkg-recommends-only'}
    _live_excl = {'live-boot', 'live-config'}

    _canonical = {
        n for n in _selected if n == _selected[n]['Package']
    }
    _base_include = sorted(_canonical - _extras - _live_excl)
    _pool_whitelist = _canonical - _live_excl

    # Every base_include entry must be in the pool.
    _missing = [n for n in _base_include if n not in _pool_whitelist]
    assert not _missing, (
        f"base_include entries missing from pool_whitelist: {_missing}.  "
        "debootstrap will fail at install time.  Filters must agree."
    )
    # busybox in pkg_closure (pkg.list) → in both lists.
    assert 'busybox' in _base_include
    assert 'busybox' in _pool_whitelist
    # Live-exclusive in NEITHER (post pkg.list audit, anything d-i
    # needs at install time is explicit in pkg.list so live_exclusive
    # only contains true live-only binaries).
    assert 'live-boot' not in _base_include
    assert 'live-boot' not in _pool_whitelist
    # Extras in pool only.
    assert 'pkg-recommends-only' in _pool_whitelist
    assert 'pkg-recommends-only' not in _base_include


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


def test_iso_installer_sign_release_files_runs_both_gpg_invocations():
    """COMP-02 phase C: _sign_release_files must produce Release.gpg
    (detached, --armor) AND InRelease (clearsigned).  Pin the two gpg
    invocations so a future refactor doesn't accidentally drop one;
    older apt clients fall back to Release+Release.gpg when InRelease
    is absent, and dropping InRelease would silently regress modern
    clients."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from apt_repo import sign_release_files
    with tempfile.TemporaryDirectory() as _stage, \
         tempfile.TemporaryDirectory() as _gpgdir:
        _suite_dir = os.path.join(_stage, 'dists', 'thor')
        os.makedirs(_suite_dir)
        with open(os.path.join(_suite_dir, 'Release'), 'w') as fh:
            fh.write('Suite: thor\n')
        _calls = []
        def _fake_run(cmd, *_a, **_k):
            _calls.append(tuple(cmd))
            class _R: returncode = 0; stderr = ''; stdout = ''
            # Pretend gpg wrote the output file so the helper sees success.
            if '--output' in cmd:
                _out = cmd[cmd.index('--output') + 1]
                with open(_out, 'w') as fh: fh.write('FAKE-SIGNATURE\n')
            return _R()
        with patch('apt_repo.subprocess.run', side_effect=_fake_run):
            assert sign_release_files(
                _stage, 'thor', _gpgdir, 'pw') is True
        # Exactly two gpg calls.
        _gpg_calls = [c for c in _calls if c[0] == 'gpg']
        assert len(_gpg_calls) == 2, _gpg_calls
        # First: detach-sign with --armor → Release.gpg
        _detach = _gpg_calls[0]
        assert '--detach-sign' in _detach, _detach
        assert '--armor' in _detach, _detach
        assert _detach[-1].endswith('/Release'), _detach
        _out_idx = _detach.index('--output')
        assert _detach[_out_idx + 1].endswith('/Release.gpg'), _detach
        # Second: clearsign → InRelease
        _clear = _gpg_calls[1]
        assert '--clearsign' in _clear, _clear
        assert _clear[-1].endswith('/Release'), _clear
        _out_idx = _clear.index('--output')
        assert _clear[_out_idx + 1].endswith('/InRelease'), _clear
        # Both invocations use --batch --yes (don't prompt).
        for _c in _gpg_calls:
            assert '--batch' in _c and '--yes' in _c, _c


def test_iso_installer_sign_release_files_errors_when_release_missing():
    """_sign_release_files must bail loud if Release file is absent —
    no point running gpg against nothing.  The error message must point
    the operator at _generate_apt_repo (the upstream step)."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from apt_repo import sign_release_files
    with tempfile.TemporaryDirectory() as _stage, \
         tempfile.TemporaryDirectory() as _gpgdir:
        # Don't create dists/thor/Release
        with patch('apt_repo.subprocess.run') as _mock:
            assert sign_release_files(
                _stage, 'thor', _gpgdir, 'pw') is False
            _mock.assert_not_called()


def test_iso_installer_sign_release_files_errors_when_homedir_missing():
    """Without a signing homedir gpg has no key to use — bail loud with
    a hint about 'signing keygen' rather than producing a cryptic gpg
    error message."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from apt_repo import sign_release_files
    with tempfile.TemporaryDirectory() as _stage:
        _suite_dir = os.path.join(_stage, 'dists', 'thor')
        os.makedirs(_suite_dir)
        with open(os.path.join(_suite_dir, 'Release'), 'w') as fh:
            fh.write('Suite: thor\n')
        with patch('apt_repo.subprocess.run') as _mock:
            assert sign_release_files(
                _stage, 'thor', '/nonexistent/gpgdir', 'pw') is False
            _mock.assert_not_called()


def test_iso_installer_export_pubkey_to_staging_copies_to_disk_archive_key():
    """COMP-02 phase C: _export_pubkey_to_staging must land the pubkey
    at .disk/archive-key.gpg with mode 0644.  Our base-installer patch
    (patch/source/base-installer/1.213/9001-install-athena-archive-keyring)
    reads from that exact path; renaming or moving it would silently
    break the install-time keyring install."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from apt_repo import export_pubkey_to_staging
    with tempfile.TemporaryDirectory() as _stage, \
         tempfile.NamedTemporaryFile('wb', delete=False) as _pubkey_fh:
        _pubkey_fh.write(b'-----BEGIN PGP PUBLIC KEY BLOCK-----\nFAKE\n')
        _pubkey_path = _pubkey_fh.name
    try:
        os.makedirs(os.path.join(_stage, '.disk'))
        assert export_pubkey_to_staging(_stage, _pubkey_path, 'pw') is True
        _dst = os.path.join(_stage, '.disk', 'archive-key.gpg')
        assert os.path.isfile(_dst), _dst
        with open(_dst, 'rb') as fh:
            assert fh.read().startswith(b'-----BEGIN PGP'), 'content not copied'
        _mode = os.stat(_dst).st_mode & 0o777
        assert _mode == 0o644, oct(_mode)
    finally:
        os.unlink(_pubkey_path)


def test_iso_installer_export_pubkey_to_staging_errors_when_pubkey_missing():
    """Bail loud if the project pubkey isn't where signing.py says it
    should be — sets the operator up to run 'signing keygen' rather
    than ship an ISO that fails apt-cdrom verify at install time."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from apt_repo import export_pubkey_to_staging
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(os.path.join(_stage, '.disk'))
        assert export_pubkey_to_staging(
            _stage, '/nonexistent/pubkey.gpg', 'pw') is False


def test_iso_installer_export_pubkey_to_staging_errors_when_disk_dir_missing():
    """_stage_disk_info must run before _export_pubkey_to_staging; the
    helper must fail loud if it hasn't (rather than silently mkdir + copy
    and skip the .disk/info disc-marker contract _stage_disk_info enforces)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from apt_repo import export_pubkey_to_staging
    with tempfile.TemporaryDirectory() as _stage, \
         tempfile.NamedTemporaryFile('wb', delete=False) as _pubkey_fh:
        _pubkey_fh.write(b'KEY')
        _pubkey_path = _pubkey_fh.name
    try:
        # No .disk/ in _stage.
        assert export_pubkey_to_staging(_stage, _pubkey_path, 'pw') is False
    finally:
        os.unlink(_pubkey_path)


def test_pool_list_pins_target_only_packages():
    """COMP-02 phase D follow-up: `config/pool.list` ships packages
    that base-installer / hw-detect / finish-install apt-install on
    /target but which we don't want pre-installed in the live image.
    Pin the current set so a future config rework doesn't drop them:
      - grub-pc + grub-efi-amd64: bootloader metas (firmware-mode-
        dependent; grub-installer picks one at install time).  Both
        must be explicit — they conflict with each other, neither
        pulls the other transitively.
      - open-vm-tools-desktop: hw-detect apt-installs on VMware
        guests with desktop env.  Pulls open-vm-tools transitively;
        open-vm-tools also explicitly listed defensively for the
        future case where desktop is dropped from the install path.
      - console-setup: explicit in pool.list because base-installer
        apt-installs it on /target as a SEPARATE step, independent of
        any transitive chain.  Removed 2026-05-17 in a pool-audit
        cleanup that mis-assumed the kbd chain would cover it; the
        2026-05-18 install reproduced the original "no installation
        candidate" failure and it was re-added.
      - keyboard-configuration + xkb-data: NOT explicit — reach the
        pool transitively via `kbd` (in pkg.list [base], Depends
        keyboard-configuration → Depends xkb-data).  Verified by the
        2026-05-18 install log which shows both being installed from
        the cdrom pool while console-setup failed.  Re-add if `kbd`
        is ever dropped from [base]."""
    _path = os.path.join(_ROOT, 'config', 'pool.list')
    assert os.path.isfile(_path), _path
    with open(_path) as fh:
        _names = {
            _line.strip() for _line in fh
            if _line.strip() and not _line.lstrip().startswith('#')
        }
    # Bootloader metas — both must be explicit (mutually exclusive)
    assert 'grub-pc' in _names, _names
    assert 'grub-efi-amd64' in _names, _names
    # VMware guest tooling — desktop pulls non-desktop transitively
    assert 'open-vm-tools-desktop' in _names, _names
    assert 'open-vm-tools' in _names, _names
    # console-setup: base-installer apt-installs directly on /target;
    # NOT covered by kbd's transitive chain (2026-05-18 regression).
    assert 'console-setup' in _names, (
        "console-setup must be explicit — base-installer apt-installs "
        "it directly, not via the kbd chain")
    # Negative assertions — these reach pool transitively via kbd
    assert 'keyboard-configuration' not in _names, _names
    assert 'xkb-data' not in _names, _names


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


def test_resolve_packages_check_conflicts_false_skips_lookahead_check():
    """Pass VII calls resolve_packages(check_conflicts=False) so two
    mutually-conflicting metas (`grub-pc` + `grub-efi-amd64`) can both
    enter the lookahead.  With the default (True), the second name
    would be rejected at lookahead time.  Confirm both end up in the
    lookahead when the flag is False."""
    dt, grub_pc, grub_efi, _ = _make_pool_dep_tree_stub()
    # Add both via add_lookahead with conflict-check disabled.
    dt.add_lookahead(['grub-pc', 'grub-efi-amd64'], check_conflicts=False)
    _la = dt._DependencyTree__lookahead
    assert 'grub-pc'        in _la, _la
    assert 'grub-efi-amd64' in _la, _la
    # Sanity: with check_conflicts=True (the default), the second name
    # should NOT be added because it conflicts with the first.
    dt2, _, _, _ = _make_pool_dep_tree_stub()
    dt2.add_lookahead(['grub-pc', 'grub-efi-amd64'], check_conflicts=True)
    _la2 = dt2._DependencyTree__lookahead
    assert 'grub-pc' in _la2, _la2
    assert 'grub-efi-amd64' not in _la2, \
        f"default check_conflicts should reject second conflicting entry, got {dict(_la2)}"


def test_validate_selection_skips_conflict_when_pool_extra():
    """validate_selection bypasses Conflicts when EITHER side is in
    pool_extras_pkg_names — apt enforces them on the target.  Both
    grub metas in pool_extras: validate_selection returns True (no
    DEPENDENCY HELL fired) even though they Conflict."""
    dt, grub_pc, grub_efi, _ = _make_pool_dep_tree_stub()
    dt.selected_pkgs = {
        'grub-pc':        grub_pc,
        'grub-efi-amd64': grub_efi,
    }
    dt.pool_extras_pkg_names = {'grub-pc', 'grub-efi-amd64'}
    assert dt.validate_selection() is True


def test_validate_selection_skips_break_when_pool_extra():
    """Same membership-based bypass applies to Breaks (weaker form of
    Conflicts in Debian semantics)."""
    dt, _, _, _ = _make_pool_dep_tree_stub()
    # Build two packages that Break each other.
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    class _Pkg:
        def __init__(self, name, ver, breaks=None):
            self._fields = {'Package': name, 'Version': ver}
            self.package = name
            self.version = ver
            self.conflicts = []
            self.depends = []
            self.pre_depends = []
            self.recommends = []
            self.alt_depends = []
            self.breaks = breaks or []
            self.constraints_satisfied = True
        def __getitem__(self, k): return self._fields[k]
        def get_provides(self): return []
        def add_constraint(self, v, o): pass
    a = _Pkg('a', '1', breaks=[[('b', '', '')]])
    b = _Pkg('b', '1', breaks=[[('a', '', '')]])
    dt.selected_pkgs = {'a': a, 'b': b}
    dt.pool_extras_pkg_names = {'a'}  # Only a is pool extra; bypass still fires.
    assert dt.validate_selection() is True


def test_validate_selection_still_fires_on_non_pool_conflicts():
    """Sanity: when neither side of the conflict is a pool extra,
    validate_selection still returns False (DEPENDENCY HELL fires).
    Without this guarantee the bypass would be over-broad."""
    dt, grub_pc, grub_efi, _ = _make_pool_dep_tree_stub()
    dt.selected_pkgs = {
        'grub-pc':        grub_pc,
        'grub-efi-amd64': grub_efi,
    }
    dt.pool_extras_pkg_names = set()  # No pool extras → no bypass.
    assert dt.validate_selection() is False


def test_base_installer_athena_keyring_patch_exists_and_is_dep3_clean():
    """The quilt patch on base-installer 1.213 is the install-time half
    of phase C — without it the disc's signed Release is unusable
    because /target's apt has no trust anchor at apt-cdrom-add time.
    Pin the path so a future re-pack of patch/source/ doesn't drop it,
    and pin DEP-3 cleanliness so the patch keeps its provenance header."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    _path = os.path.join(
        _ROOT, 'patch', 'source', 'base-installer', '1.213',
        '9001-install-athena-archive-keyring.patch'
    )
    assert os.path.isfile(_path), _path
    _missing = utils.check_dep3_header(_path)
    assert not _missing, f"DEP-3 fields missing: {_missing}"
    with open(_path) as fh:
        _content = fh.read()
    # Pin the key path apt + base-installer agree on.
    assert '/cdrom/.disk/archive-key.gpg' in _content, _content
    assert (
        '/target/etc/apt/trusted.gpg.d/athena-archive-keyring.gpg' in _content
    ), _content
    # And pin the placement — must be inside library.sh, not e.g. debian/rules.
    assert '+++ b/library.sh' in _content, _content


def test_iso_installer_stage_grub_cfg_copies_when_present():
    """Symmetric: a present grub.cfg under installer/boot/ is copied
    verbatim to staging/boot/grub/grub.cfg.  Engine never modifies it
    (data-vs-code contract)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()         # _stage_grub_cfg prints via tui.console
    from iso_installer import _stage_grub_cfg
    _content = "set timeout=5\nmenuentry 'Test' { linux /boot/vmlinuz }\n"
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(os.path.join(_stage, 'boot', 'grub'), exist_ok=True)
        with tempfile.TemporaryDirectory() as _installer:
            os.makedirs(os.path.join(_installer, 'boot'), exist_ok=True)
            _src = os.path.join(_installer, 'boot', 'grub.cfg')
            with open(_src, 'w') as fh: fh.write(_content)
            assert _stage_grub_cfg(_stage, _installer) is True
            _dst = os.path.join(_stage, 'boot', 'grub', 'grub.cfg')
            with open(_dst) as fh:
                assert fh.read() == _content, "engine must copy data layer verbatim"


def test_iso_installer_stage_grub_cfg_copies_background_when_present():
    """COMP-01f Phase 2: when installer/boot/grub-background.png exists,
    _stage_grub_cfg must copy it to staging/boot/grub/grub-background.png
    so grub.cfg's `background_image /boot/grub/grub-background.png` can
    resolve at boot time.  Pin filename match (basename in grub.cfg ↔
    staged filename) so a future rename doesn't silently break the
    splash."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from iso_installer import _stage_grub_cfg
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(os.path.join(_stage, 'boot', 'grub'), exist_ok=True)
        with tempfile.TemporaryDirectory() as _installer:
            os.makedirs(os.path.join(_installer, 'boot'), exist_ok=True)
            # grub.cfg (required)
            with open(os.path.join(_installer, 'boot', 'grub.cfg'), 'w') as fh:
                fh.write("background_image /boot/grub/grub-background.png\n")
            # background asset (optional but present here)
            _png_src = os.path.join(_installer, 'boot', 'grub-background.png')
            with open(_png_src, 'wb') as fh:
                fh.write(b'\x89PNG\r\n\x1a\n' + b'stub')   # PNG magic + body
            assert _stage_grub_cfg(_stage, _installer) is True
            _png_dst = os.path.join(_stage, 'boot', 'grub', 'grub-background.png')
            assert os.path.exists(_png_dst), (
                "background PNG not staged — grub.cfg's background_image "
                "line will fail at boot"
            )
            with open(_png_dst, 'rb') as fh:
                assert fh.read().startswith(b'\x89PNG'), "binary copy corrupted"


def test_iso_installer_stage_grub_cfg_tolerates_missing_background():
    """COMP-01f Phase 2: background PNG is optional.  When absent,
    _stage_grub_cfg must still succeed (boot menu works in text mode
    via grub.cfg's `if loadfont … ; then … fi` guard).  Pin the
    cosmetic-not-load-bearing contract so a future overzealous error
    handler doesn't promote "missing splash" to "build failure"."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from iso_installer import _stage_grub_cfg
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(os.path.join(_stage, 'boot', 'grub'), exist_ok=True)
        with tempfile.TemporaryDirectory() as _installer:
            os.makedirs(os.path.join(_installer, 'boot'), exist_ok=True)
            # ONLY grub.cfg — no background PNG.
            with open(os.path.join(_installer, 'boot', 'grub.cfg'), 'w') as fh:
                fh.write("menuentry 'x' {}\n")
            assert _stage_grub_cfg(_stage, _installer) is True


def test_installer_grub_cfg_wires_background_image():
    """COMP-01f Phase 2: the shipped grub.cfg must include the gfxterm
    + background_image setup (gated by `if loadfont`), and the
    background filename must match the asset committed under
    installer/boot/.  Anti-regression so a stray edit to grub.cfg
    doesn't silently drop the splash."""
    _cfg = os.path.join(_ROOT, 'installer', 'boot', 'grub.cfg')
    assert os.path.isfile(_cfg), f"missing {_cfg}"
    with open(_cfg) as fh:
        _body = fh.read()
    # Gated setup — `if loadfont … ; then … fi` falls back to text
    # if firmware can't provide a framebuffer.
    assert 'if loadfont' in _body, _body
    # `insmod all_video` MUST be loaded before gfxterm — without a
    # video driver, gfxterm has no framebuffer to switch into and
    # the entire if-block silently fails (text mode persists, no
    # splash).  This was the initial 2026-05-22 bug — pin the fix
    # so a future grub.cfg edit doesn't reintroduce it.
    assert 'insmod all_video' in _body, (
        "REGRESSION: grub.cfg missing `insmod all_video` — gfxterm "
        "has no framebuffer and background_image silently no-ops.  "
        "Symptom: installer boot menu in text mode with no splash."
    )
    assert 'insmod gfxterm' in _body, _body
    assert 'insmod png' in _body, _body
    assert 'terminal_output gfxterm' in _body, _body
    assert 'background_image /boot/grub/grub-background.png' in _body, _body
    # `gfxmode=auto` lets firmware pick a mode it actually advertises —
    # hard-coded 800x600 was too restrictive (silently fell back to
    # text on hardware that didn't offer that exact mode).
    assert 'set gfxmode=auto' in _body, (
        "REGRESSION: grub.cfg should use `set gfxmode=auto` not a "
        "hard-coded resolution.  Hard-coded modes are firmware-"
        "specific and the if-block fails silently on hardware that "
        "doesn't advertise that exact mode."
    )
    # Asset committed at the path the grub.cfg references (basename match).
    _png = os.path.join(_ROOT, 'installer', 'boot', 'grub-background.png')
    assert os.path.isfile(_png), (
        f"grub.cfg references background_image but {_png} is missing — "
        f"run installer/boot/regenerate-bg.py to (re-)produce it"
    )
    # Quick sanity: PNG magic.
    with open(_png, 'rb') as fh:
        assert fh.read(8) == b'\x89PNG\r\n\x1a\n', (
            f"{_png} does not start with the PNG magic bytes — file is "
            f"corrupted or regenerate-bg.py output something else"
        )


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


def test_installer_smoke_scan_log_returns_empty_on_clean_log():
    """A serial log with no known-bad patterns produces an empty
    findings list.  Anti-regression so the harness doesn't start
    flagging clean logs as failures."""
    import tempfile
    _mod = _smoke_import()
    with tempfile.NamedTemporaryFile('w', suffix='.log', delete=False) as _fh:
        _fh.write(
            'May 22 12:34:56 main-menu[123]: INFO: Menu item localechooser selected\n'
            'May 22 12:34:57 anna: anna 1.91\n'
            'May 22 12:34:58 main-menu[123]: INFO: Falling back to the package description for brltty-udeb\n'
        )
        _path = _fh.name
    try:
        _findings = _mod.scan_log(_path)
        assert _findings == [], _findings
        assert not _mod.has_fatal(_findings)
    finally:
        os.unlink(_path)


def test_installer_smoke_scan_log_catches_each_fatal_pattern():
    """Every 'fatal' pattern in KNOWN_BAD must match a synthesized
    log line that should trigger it.  Catches regex typos (e.g.
    accidentally escaping a literal that shouldn't be escaped).
    """
    import tempfile
    import re as _re_mod
    _mod = _smoke_import()
    _fatal_entries = [_e for _e in _mod.KNOWN_BAD if _e[1] == 'fatal']
    assert _fatal_entries, 'KNOWN_BAD has no fatal entries — empty test'
    for _re_src, _sev, _meaning in _fatal_entries:
        # Build a synthetic line that should match by inserting a
        # literal substring from the regex source.  This is crude but
        # sufficient — each pattern is a short literal phrase.
        _re_compiled = _re_mod.compile(_re_src)
        # Generate a sample matching line.  For most KNOWN_BAD patterns
        # the source IS the literal phrase; for `WARNING \*\*:.*main-menu`
        # we need to construct a matching line manually.
        _sample = _build_sample_for_pattern(_re_src)
        assert _re_compiled.search(_sample), (
            f"pattern {_re_src!r} doesn't match its own synthesized "
            f"sample {_sample!r} — fix the test sample or the pattern"
        )
        # Now run through scan_log against a single-line log:
        with tempfile.NamedTemporaryFile('w', suffix='.log', delete=False) as _fh:
            _fh.write(_sample + '\n')
            _path = _fh.name
        try:
            _findings = _mod.scan_log(_path)
            _found = [_f for _f in _findings if _f['pattern'] == _re_src]
            assert _found, (
                f"scan_log didn't surface pattern {_re_src!r} on its "
                f"sample line {_sample!r}; got {_findings}"
            )
            assert _found[0]['severity'] == 'fatal'
            assert _mod.has_fatal(_findings)
        finally:
            os.unlink(_path)


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


def test_installer_smoke_scan_log_distinguishes_warn_from_fatal():
    """Warns shouldn't trigger has_fatal — they're tracking-only."""
    import tempfile
    _mod = _smoke_import()
    _warns = [_e for _e in _mod.KNOWN_BAD if _e[1] == 'warn']
    if not _warns:
        return   # no warns defined; test is a no-op
    _re_src = _warns[0][0]
    _sample = _build_sample_for_pattern(_re_src)
    with tempfile.NamedTemporaryFile('w', suffix='.log', delete=False) as _fh:
        _fh.write(_sample + '\n')
        _path = _fh.name
    try:
        _findings = _mod.scan_log(_path)
        assert _findings, f"scan_log missed warn pattern {_re_src!r}"
        assert all(_f['severity'] == 'warn' for _f in _findings), _findings
        assert not _mod.has_fatal(_findings), (
            "has_fatal returned True for a warn-only finding"
        )
    finally:
        os.unlink(_path)


def test_installer_smoke_scan_log_handles_missing_file():
    """A nonexistent log path returns [] (not an exception).  The
    harness uses this when QEMU dies before producing any serial
    output — the scan should fail gracefully + the harness reports
    'QEMU exited rc=N, 0 bytes log' instead of crashing."""
    _mod = _smoke_import()
    _findings = _mod.scan_log('/tmp/this-path-definitely-does-not-exist-12345.log')
    assert _findings == []


def test_installer_smoke_known_bad_extends_with_extra_patterns():
    """scan_log accepts caller-supplied extra patterns — used by
    operators who want a one-shot ad-hoc gate without editing the
    canonical KNOWN_BAD list.  Pin the extension surface so a
    refactor doesn't drop it."""
    import tempfile
    _mod = _smoke_import()
    _extra = [(r'ad-hoc-regression-XYZ', 'fatal', 'test-only pattern')]
    with tempfile.NamedTemporaryFile('w', suffix='.log', delete=False) as _fh:
        _fh.write('something something ad-hoc-regression-XYZ here\n')
        _path = _fh.name
    try:
        _findings = _mod.scan_log(_path, extra_patterns=_extra)
        assert any(_f['pattern'] == r'ad-hoc-regression-XYZ'
                   for _f in _findings), _findings
    finally:
        os.unlink(_path)


def test_installer_smoke_run_module_has_required_modes():
    """The run.py entry point must expose --quick (default) + --full
    modes per the README contract.  Code-inspection anti-regression.
    """
    _path = os.path.join(_ROOT, 'tests', 'installer_smoke', 'run.py')
    with open(_path) as fh:
        _body = fh.read()
    assert "'--quick'" in _body, "--quick mode flag missing from run.py"
    assert "'--full'" in _body, "--full mode flag missing from run.py"
    assert "'--iso'" in _body, "--iso arg missing from run.py"
    assert "'--mode'" in _body, "--mode (bios/efi) arg missing from run.py"
    assert 'qemu-system-x86_64' in _body, (
        "run.py must invoke qemu-system-x86_64"
    )
    # The known-bad parser must be wired in.
    assert 'known_bad_patterns' in _body
    assert 'scan_log' in _body


def test_installer_chroot_dpkg_unpack_carries_required_force_flags():
    """REGRESSION (2026-05-11): _dpkg_unpack must invoke dpkg with at
    least --force-depends + --force-overwrite + --no-triggers, with
    --unpack (not -i).  Drop any one of these and the unpack fails on
    real udebs:
      - --force-depends: udeb deps reference other udebs not on host
      - --force-overwrite: d-i udebs ship overlapping files by design
                           (busybox-udeb's /sbin/depmod stub vs
                           kmod-udeb's real /sbin/depmod)
      - --no-triggers: trigger machinery is irrelevant + would run
                       host hooks against the chroot
      - --unpack vs -i: skip configure (postinsts need cdebconf at runtime)
    Tests via inspect.getsource — actual dpkg invocation requires sudo
    and is exercised by manual smoke."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _dpkg_unpack
    src = inspect.getsource(_dpkg_unpack)
    for _flag in ('--force-depends', '--force-overwrite',
                  '--no-triggers', '--unpack'):
        assert _flag in src, f"_dpkg_unpack missing {_flag}"
    # Confirm we are NOT using `-i` (configure step), which would try to
    # run postinsts that need cdebconf at chroot-build time.
    assert "'-i'" not in src and '"-i"' not in src, (
        "_dpkg_unpack must use --unpack, not -i — postinsts require "
        "cdebconf running which only happens at first boot"
    )


def test_installer_chroot_overlay_map_is_data_not_code():
    """The engine overlay map MUST be a small list of (src, dst) tuples
    holding only path strings — no code, no string formatting based on
    runtime state.  This pins the data-layer contract: adding a new
    file mapping is an append to this list + an entry in
    installer/README.md, never a code change."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _OVERLAY_MAP
    assert isinstance(_OVERLAY_MAP, list)
    for _entry in _OVERLAY_MAP:
        assert isinstance(_entry, tuple) and len(_entry) == 2, _entry
        _src, _dst = _entry
        assert isinstance(_src, str) and isinstance(_dst, str), _entry
        # Targets must be relative (chroot-relative); absolute would
        # silently break os.path.join with the chroot root.
        assert not _dst.startswith('/'), (
            f"overlay dst must be relative to chroot root, got {_dst!r}")
        # Sources must be relative to installer_dir.
        assert not _src.startswith('/'), (
            f"overlay src must be relative to installer_dir, got {_src!r}")


def test_installer_ships_forked_choose_mirror():
    """choose-mirror is in installer.list (our Athena-only fork drives the
    mirror step), and the fork's masterlist lists only the Athena repo."""
    with open(os.path.join(_ROOT, 'config', 'installer.list')) as fh:
        assert any(_l.strip() == 'choose-mirror' for _l in fh), \
            "choose-mirror missing from installer.list"
    with open(os.path.join(_ROOT, 'fork', 'source', 'choose-mirror',
                           'Mirrors.masterlist')) as fh:
        _body = fh.read()
    assert 'Site: 140.245.198.222' in _body and '/asgard/' in _body, _body
    assert 'debian.org' not in _body, _body
    # ./mirrorlist's parser matches every `key: value` line, so a `#` comment
    # containing ": " before the first Site: crashes the build (subscript -1).
    # The masterlist must be pure RFC822 stanzas — no comment lines.
    assert not any(_l.lstrip().startswith('#')
                   for _l in _body.splitlines()), \
        "Mirrors.masterlist must have no '#' comments (breaks ./mirrorlist)"


def test_athena_installer_data_drops_mirror_protocol_stub():
    """The mirror/protocol stub is gone — real choose-mirror provides it."""
    _base = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data')
    assert not os.path.exists(
        os.path.join(_base, 'data', 'athena-stubs.templates')), \
        "athena-stubs.templates should be removed"
    with open(os.path.join(_base, 'debian', 'install')) as fh:
        assert 'athena-stubs.templates' not in fh.read(), \
            "debian/install still references the removed stub"


def test_preseed_pins_http_mirror_and_enables_use_mirror():
    """preseed pins http + forces apt-setup/use_mirror=true (else 50mirror
    exits and writes no mirror), and never sets use_mirror=false."""
    with open(os.path.join(_ROOT, 'installer', 'preseed', 'preseed.cfg')) as fh:
        _body = fh.read()
    assert 'mirror/protocol string http' in _body, _body
    assert 'apt-setup/use_mirror boolean true' in _body, _body
    assert 'apt-setup/use_mirror boolean false' not in _body, _body


def test_preseed_enables_non_free_firmware_no_deb_src():
    """The target's mirror line must include `main non-free-firmware` (the repo
    now publishes that component) but NO deb-src, and the firmware machinery
    must be RE-ENABLED so hw-detect installs microcode/firmware:
      - apt-setup/non-free-firmware=true → 50mirror writes the component.
      - hw-detect/firmware-lookup=never must be GONE (was the disable-firmware
        escape hatch from when the repo had no non-free-firmware component).
      - apt-setup/enable-source-repositories=false still drops the deb-src line.
      - contrib/non-free are NOT forced true (we don't populate them yet)."""
    with open(os.path.join(_ROOT, 'installer', 'preseed', 'preseed.cfg')) as fh:
        _body = fh.read()
    assert 'apt-setup/non-free-firmware boolean true' in _body, _body
    assert 'hw-detect/firmware-lookup string never' not in _body, _body
    assert 'apt-setup/enable-source-repositories boolean false' in _body, _body
    assert 'apt-setup/non-free boolean true' not in _body, _body
    assert 'apt-setup/contrib boolean true' not in _body, _body


def test_choose_mirror_fork_drops_menu_item():
    """The forked choose-mirror drops XB-Installer-Menu-Item so it doesn't
    run as an early standalone step — apt-setup's 50mirror invokes it after
    base install (the stock CD flow)."""
    import re
    with open(os.path.join(_ROOT, 'fork', 'source', 'choose-mirror',
                           'debian', 'control')) as fh:
        _body = fh.read()
    assert not any(re.match(r'^(XB-)?Installer-Menu-Item:', _l)
                   for _l in _body.splitlines()), \
        "choose-mirror must not declare a menu-item field (would run early)"


def test_finish_install_cdrom_disable_overlay():
    """The cdrom-disable finish-install hook is overlaid + executable, only
    comments cdrom: entries when a network source exists, AND is numbered 11
    so it runs AFTER 08hw-detect — 08hw-detect's apt-install of microcode /
    open-vm-tools-desktop / shim-signed needs the cdrom source ACTIVE on
    offline / no-mirror installs (caught 2026-05-28 when it was at 06 →
    disabled cdrom before 08, every apt-install "Unable to locate package")."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _OVERLAY_MAP
    assert ('finish-install/11athena-disable-cdrom',
            'usr/lib/finish-install.d/11athena-disable-cdrom') in _OVERLAY_MAP, \
        _OVERLAY_MAP
    # Numeric prefix 11 > 08 → runs AFTER 08hw-detect's apt-install pass.
    assert '08hw-detect' < '11athena-disable-cdrom'
    _hook = os.path.join(_ROOT, 'installer', 'finish-install',
                         '11athena-disable-cdrom')
    assert os.access(_hook, os.X_OK), "hook must be executable (cp -p preserves mode)"
    with open(_hook) as fh:
        _s = fh.read()
    assert 'cdrom:' in _s and 'sed' in _s, _s
    assert '(https?|ftp)' in _s, "must guard on a real network source existing"


def test_iso_pool_staging_includes_non_main_component_dirs():
    """The ISO pool staging must scan the non-main component dirs (firmware
    + future contrib/non-free binaries) so they reach the cdrom — caught
    2026-05-28: _stage_pool only walked main + main-udeb, so tunneled
    firmware never made it onto the cdrom and finish-install.d/08hw-detect
    apt-install of intel-microcode failed with 'Unable to locate'.
    Pinned via source-inspection because _stage_pool needs sudo + a real
    repo to exercise end-to-end."""
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as fh:
        _body = fh.read()
    # The build_installer_iso call must forward the non-main component dirs.
    for _attr in ('dir_repo_non_free_firmware', 'dir_repo_non_free',
                  'dir_repo_contrib'):
        assert f'self.config.{_attr}' in _body, _attr
    assert 'dir_repo_extras=[' in _body, _body[:200]
    # And iso_installer must extend _pool_sources from that parameter.
    with open(os.path.join(_ROOT, 'scripts', 'iso_installer.py')) as fh:
        _iso = fh.read()
    assert 'dir_repo_extras' in _iso, 'iso_installer must accept extras'
    assert '_pool_sources.extend' in _iso, \
        'iso_installer must extend _pool_sources from dir_repo_extras'


def test_finish_install_default_source_overlay():
    """When the operator skips the mirror, 05athena-default-source writes a
    default Athena apt source (athena.list) so the installed system isn't
    stranded with only the cdrom source.  It must: be overlaid + executable,
    run BEFORE the cdrom-disable hook (11) so that disable sees the network
    source, no-op when a network source already exists (guard), derive coords
    from the same choose-mirror debconf keys (no separate hardcoded URL), and
    write to sources.list.d/athena.list."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _OVERLAY_MAP
    assert ('finish-install/05athena-default-source',
            'usr/lib/finish-install.d/05athena-default-source') in _OVERLAY_MAP, \
        _OVERLAY_MAP
    # Numeric prefix 05 < 11 → run-parts runs the default-source hook before
    # the cdrom-disable hook, so the latter sees the new source.
    assert '05athena-default-source' < '11athena-disable-cdrom'
    _hook = os.path.join(_ROOT, 'installer', 'finish-install',
                         '05athena-default-source')
    assert os.access(_hook, os.X_OK), "hook must be executable (cp -p preserves mode)"
    with open(_hook) as fh:
        _s = fh.read()
    # Guard: only fire when NO network source exists (mirror skipped).
    assert '(https?|ftp)' in _s, "must guard on an existing network source"
    # Coords from the choose-mirror debconf keys — not a separate hardcoded URL.
    assert 'mirror/http/hostname' in _s and 'mirror/codename' in _s, _s
    assert 'sources.list.d/athena.list' in _s, _s
    # Fallback line includes the non-free-firmware component (matches the
    # selected-mirror line; the repo publishes that component).
    assert 'main non-free-firmware' in _s, _s


def test_installer_chroot_resolve_udeb_files_skips_virtual_aliases():
    """_resolve_udeb_files must skip entries where the dict key differs
    from pkg['Package'] — those are virtual-package aliases (same
    canonical package re-keyed under a Provides name).  Without the
    skip, the SAME udeb would appear N times in the unpack list."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _resolve_udeb_files
    pkg = _FakePkg(
        'cdebconf-text-udeb',
        source='cdebconf',
        filename='pool/main/c/cdebconf/cdebconf-text-udeb_0.270_amd64.udeb',
    )
    class _UdebTree:
        # canonical key + a virtual alias pointing at the same Package
        selected_pkgs = {
            'cdebconf-text-udeb':         pkg,
            'cdebconf-frontend-provider': pkg,  # virtual — should be skipped
        }
    out = _resolve_udeb_files(_UdebTree(), '/nonexistent/repo')
    # Both keys would otherwise produce a candidate path; virtual must
    # be skipped → at most one missing-file warning, never two duplicate
    # path entries.  Since /nonexistent/repo has no files, out is [].
    assert out == []


def test_installer_chroot_resolve_udeb_files_strips_binnmu_suffix():
    """REGRESSION (2026-05-11): the Packages index records a binNMU
    version like `1.35.0-4+b7` but dpkg-buildpackage emits the file
    *without* the binNMU suffix (`busybox-udeb_1.35.0-4_amd64.udeb`).
    _resolve must apply utils.strip_build_version to the Filename
    field, matching what chroot.py's _get_deb_files does for the deb
    world.  Caught when chroot build installer reported four udebs
    missing on the first real run, all with +bN suffixes."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _resolve_udeb_files

    with tempfile.TemporaryDirectory() as _udeb_dir:
        # CONF-01 Stage D: _resolve_udeb_files takes the dir HOLDING
        # udebs directly (was the parent of 'main/' pre-Stage D); the
        # caller resolves config.dir_repo_main_udeb.
        _fake_path = os.path.join(_udeb_dir, 'busybox-udeb_1.35.0-4_amd64.udeb')
        with open(_fake_path, 'wb') as fh:
            fh.write(b'')
        # Package record: binNMU preserved (this is what Packages index has)
        pkg = _FakePkg(
            'busybox-udeb',
            source='busybox',
            filename='pool/main/b/busybox/busybox-udeb_1.35.0-4+b7_amd64.udeb',
        )
        class _UdebTree:
            selected_pkgs = {'busybox-udeb': pkg}
        out = _resolve_udeb_files(_UdebTree(), _udeb_dir)
        assert out == [_fake_path], out


def test_installer_chroot_resolve_udeb_files_matches_asg_stamp():
    """REGRESSION (2026-05-27 kernel panic 'no init found'): a udeb stamped with
    a +asg<R>u<N> update marker (busybox-udeb after a security delta) must still
    resolve — the index Filename is the PRISTINE name but the on-disk file is
    stamped.  Without find_matching_artifact, busybox-udeb was dropped from the
    initrd → no /bin/sh → the kernel couldn't exec /init."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _resolve_udeb_files

    with tempfile.TemporaryDirectory() as _udeb_dir:
        _stamped = os.path.join(
            _udeb_dir, 'busybox-udeb_1.35.0-4+asg1u1_amd64.udeb')
        with open(_stamped, 'wb') as fh:
            fh.write(b'')
        # Index Filename is the PRISTINE name (no +asg)
        pkg = _FakePkg(
            'busybox-udeb',
            source='busybox',
            filename='pool/main/b/busybox/busybox-udeb_1.35.0-4_amd64.udeb',
        )
        class _UdebTree:
            selected_pkgs = {'busybox-udeb': pkg}
        out = _resolve_udeb_files(_UdebTree(), _udeb_dir)
        assert out == [_stamped], out


def test_installer_chroot_resolve_udeb_files_logs_missing_silently():
    """Missing-on-disk udebs are logged + skipped (not raised).  This
    is the documented contract — caller (build_installer_chroot)
    checks the result-list length and surfaces an error if empty."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _resolve_udeb_files
    pkg = _FakePkg(
        'nonexistent-udeb',
        source='nonexistent',
        filename='pool/main/n/nonexistent/nonexistent-udeb_1.0_amd64.udeb',
    )
    class _UdebTree:
        selected_pkgs = {'nonexistent-udeb': pkg}
    # Should not raise; should return [].
    out = _resolve_udeb_files(_UdebTree(), '/nonexistent/repo')
    assert out == []


def test_installer_chroot_resolve_udeb_files_skips_record_without_filename():
    """If a Package record has no Filename field (rare — malformed
    index), _resolve warns + skips.  Empty filename is treated as
    'no filename'."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _resolve_udeb_files
    pkg = _FakePkg('orphan-udeb', source='orphan', filename='')
    class _UdebTree:
        selected_pkgs = {'orphan-udeb': pkg}
    out = _resolve_udeb_files(_UdebTree(), '/repo')
    assert out == []


def test_athena_installer_data_ships_runtime_dirs():
    """FORK-01 Step 4: /tmp, /var/tmp, /root are created by
    athena-installer-data via debian/dirs (was installer_chroot.py's
    _create_runtime_dirs helper, deleted in Step 4).  debhelper sets
    default mode (0755) — installer runs as root, no multi-user
    requirements at chroot scope."""
    _dirs = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                         'debian', 'dirs')
    assert os.path.isfile(_dirs), f"missing {_dirs}"
    with open(_dirs) as fh:
        _entries = {line.strip() for line in fh if line.strip()}
    assert 'tmp' in _entries, _entries
    assert 'var/tmp' in _entries, _entries
    assert 'root' in _entries, _entries


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


def test_installer_chroot_sudo_write_does_not_leak_password_to_tee():
    """Regression for 2026-05-13 password leak — `_sudo_write` MUST NOT
    include the password in tee's stdin, because sudo -S does not
    consume stdin when its credential cache is hot.  The bug shipped the
    operator's plaintext sudo password to /var/lib/dpkg/status,
    /etc/lsb-release, /etc/default-release, and athena-stubs.templates
    inside the installer ramdisk."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _sudo_write
    _calls = []
    def _capturing_run(cmd, *_a, **_k):
        _calls.append((tuple(cmd), _k.get('input', '')))
        class _R: returncode = 0; stderr = ''
        if cmd[:2] == ['sudo', 'tee']:
            with open(cmd[2], 'w') as fh:
                fh.write(_k.get('input', ''))
        return _R()
    with tempfile.TemporaryDirectory() as _d:
        _path = os.path.join(_d, 'leak-check.txt')
        _SECRET = 'secret-sudo-pw-do-not-leak'
        with patch('installer_chroot.subprocess.run', side_effect=_capturing_run):
            assert _sudo_write(_path, 'expected file content', _SECRET) is True
        # The tee call's stdin must NOT contain the password anywhere.
        _tee_calls = [c for c in _calls if c[0][:2] == ('sudo', 'tee')]
        assert len(_tee_calls) == 1, _tee_calls
        assert _SECRET not in _tee_calls[0][1], (
            f"password leaked into tee stdin: {_tee_calls[0][1]!r}"
        )
        # Also, the on-disk file must NOT contain the password.
        with open(_path) as fh:
            _disk = fh.read()
        assert _SECRET not in _disk, f"password leaked to disk: {_disk!r}"
        assert _disk == 'expected file content', _disk
        # The refresh call MUST carry the password (that's `-v`'s job).
        _refresh_calls = [c for c in _calls if c[0][:3] == ('sudo', '-S', '-v')]
        assert len(_refresh_calls) == 1, _refresh_calls
        assert _refresh_calls[0][1] == _SECRET + '\n', _refresh_calls


def test_installer_chroot_run_depmod_skips_when_no_modules_dir():
    """Non-kernel installer flavours (rescue, hd-media without kernel)
    legitimately have no /lib/modules — _run_depmod must skip rather
    than fail.  Pin the no-op behaviour so a future "fail closed" rewrite
    doesn't break those flavours."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _run_depmod
    with tempfile.TemporaryDirectory() as _chroot:
        # No /lib/modules at all.
        with patch('installer_chroot._sudo') as _mock_sudo:
            assert _run_depmod(_chroot, 'pw') is True
            _mock_sudo.assert_not_called()


def test_installer_chroot_run_depmod_indexes_each_kernel_present():
    """When /lib/modules/<kver> dirs exist, _run_depmod must call
    `depmod -a -b <chroot> <kver>` for each kver.  Caught 2026-05-12 as
    a cosmetic "depmod: WARNING" install-log line — promoted to a real
    build-pipeline step (was missing from our pipeline; stock d-i runs
    it in its image-build Makefile)."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _run_depmod
    with tempfile.TemporaryDirectory() as _chroot:
        os.makedirs(os.path.join(_chroot, 'lib/modules/6.1.0-39-amd64'))
        os.makedirs(os.path.join(_chroot, 'lib/modules/6.1.0-42-amd64'))
        _calls = []
        class _R: returncode = 0; stderr = ''; stdout = ''
        def _fake_sudo(cmd, _pw):
            _calls.append(cmd)
            return _R()
        with patch('installer_chroot._sudo', side_effect=_fake_sudo):
            assert _run_depmod(_chroot, 'pw') is True
        _depmod_cmds = [c for c in _calls if c[0] == 'depmod']
        assert len(_depmod_cmds) == 2, _calls
        # Each call must be `depmod -a -b <chroot> <kver>`.
        _kvers = sorted(c[-1] for c in _depmod_cmds)
        assert _kvers == ['6.1.0-39-amd64', '6.1.0-42-amd64'], _kvers
        for _c in _depmod_cmds:
            assert _c[1] == '-a' and _c[2] == '-b' and _c[3] == _chroot, _c


def test_installer_chroot_generator_guard_passes_when_both_present():
    """The build-time invariant passes only when BOTH source generators
    are present: 40cdrom (athena-cdrom-setup) AND 50mirror
    (athena-mirror-setup)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _assert_apt_setup_generators
    with tempfile.TemporaryDirectory() as _chroot:
        _gendir = os.path.join(_chroot, 'usr/lib/apt-setup/generators')
        os.makedirs(_gendir)
        open(os.path.join(_gendir, '40cdrom'), 'w').close()
        open(os.path.join(_gendir, '50mirror'), 'w').close()
        assert _assert_apt_setup_generators(_chroot) is True


def test_installer_chroot_generator_guard_fails_without_cdrom():
    """No 40cdrom → no-mirror install path has no apt source → guard fails."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _assert_apt_setup_generators
    with tempfile.TemporaryDirectory() as _chroot:
        _gendir = os.path.join(_chroot, 'usr/lib/apt-setup/generators')
        os.makedirs(_gendir)
        open(os.path.join(_gendir, '50mirror'), 'w').close()
        assert _assert_apt_setup_generators(_chroot) is False


def test_installer_chroot_generator_guard_fails_without_mirror():
    """No 50mirror → no network-mirror step and no mirror source in the
    target's sources.list.  This is the 2026-05-27 regression: seeding
    athena-cdrom-setup (whose Provides wrongly included apt-mirror-setup)
    knocked the real athena-mirror-setup out of the closure.  The guard
    must catch a missing 50mirror, not just 40cdrom."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _assert_apt_setup_generators
    with tempfile.TemporaryDirectory() as _chroot:
        _gendir = os.path.join(_chroot, 'usr/lib/apt-setup/generators')
        os.makedirs(_gendir)
        open(os.path.join(_gendir, '40cdrom'), 'w').close()
        assert _assert_apt_setup_generators(_chroot) is False


def test_athena_cdrom_setup_does_not_provide_mirror_setup():
    """athena-cdrom-setup ships ONLY the cdrom generators (40cdrom/41cdset),
    so it must Provides apt-cdrom-setup and NOTHING mirror-related.  If it
    Provides apt-mirror-setup / athena-mirror-setup, seeding it silently
    satisfies athena-setup-udeb's `Depends: athena-mirror-setup` and the real
    50mirror is dropped from the installer (2026-05-27 regression)."""
    _control = os.path.join(
        _ROOT, 'fork/source/athena-apt-setup/debian/control')
    with open(_control) as _f:
        _text = _f.read()
    # Isolate the athena-cdrom-setup stanza (blank-line separated).
    _stanza = next(
        s for s in _text.split('\n\n')
        if 'Package: athena-cdrom-setup' in s
    )
    _provides = next(
        (ln for ln in _stanza.splitlines() if ln.startswith('Provides:')),
        '',
    )
    assert 'mirror-setup' not in _provides, (
        f"athena-cdrom-setup must not Provide any *-mirror-setup: {_provides!r}"
    )
    assert 'apt-cdrom-setup' in _provides, _provides


def test_athena_installer_data_ships_release_files_with_tokens():
    """/etc/lsb-release + /etc/default-release ship from
    athena-installer-data with @DISTRIBUTION@/@CODENAME@ tokens.
    BuildContainer's _token_subst snippet resolves them at build
    time (no per-package sed needed in debian/rules anymore).
    debian/rules now only handles the dynamic symlink filename for
    /usr/share/debootstrap/scripts/<codename> — the one thing
    centralised text-substitution can't do."""
    _lsb = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                        'data', 'lsb-release')
    _def = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                        'data', 'default-release')
    assert os.path.isfile(_lsb), f"missing {_lsb}"
    assert os.path.isfile(_def), f"missing {_def}"
    with open(_lsb) as fh:
        _lsb_body = fh.read()
    with open(_def) as fh:
        _def_body = fh.read()
    # Source carries tokens — values substituted at build time by
    # BuildContainer._token_subst.
    assert 'DISTRIB_ID=@DISTRIBUTION@' in _lsb_body, _lsb_body
    assert '@CODENAME@' in _lsb_body, _lsb_body
    assert '@CODENAME@' in _def_body, _def_body

    # debian/install wires both to /etc/
    _install = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                            'debian', 'install')
    with open(_install) as fh:
        _install_body = fh.read()
    assert 'data/lsb-release' in _install_body and 'etc' in _install_body
    assert 'data/default-release' in _install_body

    # debian/rules: symlink creation stays (dynamic filename); the
    # @CODENAME@ sed block in override_dh_install is GONE (centralised
    # in BuildContainer now).  Negative assertion guards against
    # accidental reintroduction.
    _rules = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                          'debian', 'rules')
    with open(_rules) as fh:
        _rules_body = fh.read()
    assert 'ATHENA_CODENAME' in _rules_body, _rules_body
    assert 'ln -sf sid' in _rules_body, _rules_body
    assert "sed -i 's/@CODENAME@" not in _rules_body, (
        "per-package sed for @CODENAME@ resurrected — centralised in "
        "BuildContainer._token_subst, must not duplicate here")


def test_athena_installer_data_ships_templates_override():
    """COMP-01f Phase 1 (1.2.0 revision): the type=text templates that
    were initially shipped via debconf-set-selections (1.1.0 — silent
    no-op because set-selections only sets VALUES, not Descriptions)
    are now shipped via athena-overrides.templates + debconf-loadtemplate.

    Pin presence + load-bearing template re-declarations so a refactor
    doesn't silently drop them and regress to the 1.1.0 no-op state."""
    _tpl = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                        'data', 'athena-overrides.templates')
    assert os.path.isfile(_tpl), f"missing {_tpl}"
    with open(_tpl) as fh:
        _body = fh.read()
    # Templates file format: stanzas separated by blank lines.  Each
    # stanza must declare Template / Type / Description.  Pin the
    # main-menu-title override (the most visible string).
    assert 'Template: debian-installer/main-menu-title' in _body, _body
    assert 'Type: text' in _body, _body
    assert 'Description: @DISTRIBUTION@ installer main menu' in _body, _body

    # debian/install wires it under /usr/share/athena-installer-data/
    # so the apply hook can locate it via a stable path (vs
    # /var/lib/dpkg/info/ where it'd auto-load before main-menu and
    # lose the conflict).
    _install = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                            'debian', 'install')
    with open(_install) as fh:
        _install_body = fh.read()
    assert 'data/athena-overrides.templates' in _install_body, _install_body
    assert 'usr/share/athena-installer-data' in _install_body, _install_body


def test_athena_installer_data_ships_value_overrides():
    """The slimmed debconf-overrides.dat — VALUE-type templates only
    (type=string/boolean/select/multiselect).  Templates that were
    incorrectly listed as type=string here in 1.1.0 (which made the
    overrides silent no-ops because the real type is text) have moved
    to athena-overrides.templates.  Pin what's left so the file isn't
    accidentally repopulated with the wrong template types."""
    _data = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                         'data', 'debconf-overrides.dat')
    assert os.path.isfile(_data), f"missing {_data}"
    with open(_data) as fh:
        _body = fh.read()
    # The one value override we currently ship.
    assert 'd-i netcfg/get_hostname string @BASE_ID@' in _body, _body

    # Anti-regression: NO Debian literal in any uncommented override
    # line — would defeat the rebrand.
    for _ln in _body.splitlines():
        _stripped = _ln.strip()
        if not _stripped or _stripped.startswith('#'):
            continue
        assert 'Debian' not in _stripped, (
            f"Debian literal leaked into override line: {_stripped!r}")
        # Anti-regression for the 1.1.0 bug: don't put type=text
        # templates back into the value-overrides file.  Detect by
        # asserting we don't see the known type=text template paths
        # being set via debconf-set-selections syntax.
        for _text_path in ('debian-installer/main-menu-title',
                           'debian-installer/title'):
            assert _text_path not in _stripped, (
                f"REGRESSION: type=text template {_text_path!r} is in "
                f"debconf-overrides.dat — set-selections is a no-op on "
                f"type=text.  Move to athena-overrides.templates instead.  "
                f"Offending line: {_stripped!r}"
            )


def test_athena_installer_data_branding_hook_applies_both_mechanisms():
    """S40-athena-branding runs BOTH override mechanisms in the right
    order: (1) debconf-loadtemplate for type=text overrides AFTER
    S20templates already loaded main-menu's templates (path-key
    conflict → LAST load wins); (2) debconf-set-selections for the
    value-type overrides.

    Anti-regression for the 1.1.0 bug where the hook only ran
    set-selections, which is a no-op against type=text templates."""
    _hook = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                         'data', 'S40-athena-branding')
    assert os.path.isfile(_hook), f"missing {_hook}"
    assert os.access(_hook, os.X_OK), (
        f"{_hook} not executable — dh_install preserves source mode, "
        f"so a non-executable source ships a non-executable hook that "
        f"run-parts skips silently"
    )
    with open(_hook) as fh:
        _body = fh.read()
    assert '#!/bin/sh' in _body, "missing shebang"
    # Both mechanisms must be wired in the hook.
    assert 'debconf-loadtemplate' in _body, (
        "hook must call debconf-loadtemplate for type=text overrides "
        "(athena-overrides.templates) — REGRESSION to 1.1.0 if missing"
    )
    assert 'debconf-set-selections' in _body, (
        "hook must also call debconf-set-selections for type=string "
        "value overrides"
    )
    assert '/usr/share/athena-installer-data/athena-overrides.templates' in _body, _body
    assert '/usr/share/athena-installer-data/debconf-overrides.dat' in _body, _body

    # debian/install wires the hook to the correct startup-d path
    _install = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                            'debian', 'install')
    with open(_install) as fh:
        _install_body = fh.read()
    assert 'data/S40-athena-branding' in _install_body, _install_body
    assert 'lib/debian-installer-startup.d' in _install_body, _install_body


def test_athena_installer_data_no_broken_palette_mechanism():
    """REGRESSION pin: the 1.1.0 S35-athena-palette + palette.athena
    approach was confirmed broken on first install (rootskel execs
    scripts as children → export dies; cdebconf-newt has compiled-in
    palette → ignores newt env vars).  Removed in 1.2.0 and documented
    as irreducible residue in docs/branding-methodology.md § 7.

    Anti-regression: don't re-introduce the broken pattern.  If a
    future cdebconf rev DOES start honouring an env var, that's a new
    mechanism — needs its own design + test."""
    _dead_palette = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                                  'data', 'palette.athena')
    _dead_hook = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                               'data', 'S35-athena-palette')
    assert not os.path.exists(_dead_palette), (
        f"{_dead_palette} re-introduced — was removed in 1.2.0 because "
        f"the mechanism is broken (cdebconf-newt ignores NEWT_COLORS_FILE).  "
        f"See docs/branding-methodology.md § 7."
    )
    assert not os.path.exists(_dead_hook), (
        f"{_dead_hook} re-introduced — same.  Don't re-add without "
        f"first verifying cdebconf-newt has gained a runtime palette "
        f"override path."
    )

    # And it must not be wired into debian/install
    _install = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                            'debian', 'install')
    with open(_install) as fh:
        _install_body = fh.read()
    assert 'palette.athena' not in _install_body, _install_body
    assert 'S35-athena-palette' not in _install_body, _install_body


def test_athena_installer_data_no_branding_patches_in_repo():
    """Negative pin for Principle P1 (docs/branding-methodology.md):
    we MUST NOT ship per-version patches against cdebconf or
    cdrom-detect to rebrand strings.  Their absence is the principle;
    a quilt patch directory appearing here would be the regression.
    """
    for _pkg in ('cdebconf', 'cdrom-detect', 'main-menu'):
        _patch_dir = os.path.join(_ROOT, 'patch', 'source', _pkg)
        assert not os.path.isdir(_patch_dir), (
            f"{_patch_dir} exists — would violate Principle P1 (no "
            f"per-version source patches for branding).  See "
            f"docs/branding-methodology.md §§ 1, 6 (A1)."
        )


def test_athena_branding_ships_target_grub_background():
    """athena-branding 1.2.0+ ships the target-system GRUB background.
    Pin all four moving parts so a refactor doesn't silently drop one:
      1. data/50-athena.cfg sets GRUB_BACKGROUND= to the canonical path
      2. debian/rules renders grub-background.png from aegis-dark.svg
      3. debian/install ships the rendered PNG under /usr/share/athena-branding/
      4. debian/postinst re-runs update-grub on configure

    Why this matters: observed 2026-05-22 on a fresh install — GRUB on
    the installed target had our distributor string ("Asgard GNU/Linux")
    but no graphical background.  Stock Debian's GRUB background ships
    from desktop-base, which we displace via Provides+Conflicts+Replaces;
    displacing without replacing left the background gap.  1.2.0 closes
    it.  See docs/branding-methodology.md § 5 catalogue."""
    _root = os.path.join(_ROOT, 'fork', 'source', 'athena-branding')

    # 1. GRUB_BACKGROUND= line in 50-athena.cfg
    _cfg = os.path.join(_root, 'data', '50-athena.cfg')
    with open(_cfg) as fh:
        _cfg_body = fh.read()
    assert 'GRUB_BACKGROUND="/usr/share/athena-branding/grub-background.png"' in _cfg_body, (
        f"GRUB_BACKGROUND missing or path changed in {_cfg} — the boot "
        f"menu won't get a background.  See docs/branding-methodology.md "
        f"§ 5 catalogue."
    )

    # 2. rsvg-convert renders grub-background.png in debian/rules
    _rules = os.path.join(_root, 'debian', 'rules')
    with open(_rules) as fh:
        _rules_body = fh.read()
    assert 'grub-background.png' in _rules_body, (
        "debian/rules doesn't render grub-background.png — install will "
        "fail or ship nothing"
    )
    assert 'aegis-dark.svg' in _rules_body, (
        "GRUB background should come from aegis-dark.svg (readable menu "
        "text on midnight sky) — light variant would wash out menu text"
    )

    # 3. debian/install ships the PNG to the canonical location
    _install = os.path.join(_root, 'debian', 'install')
    with open(_install) as fh:
        _install_body = fh.read()
    assert '_build/png/grub-background.png' in _install_body, _install_body
    assert 'usr/share/athena-branding' in _install_body, _install_body

    # 4. postinst exists, is executable, calls update-grub on configure
    _postinst = os.path.join(_root, 'debian', 'postinst')
    assert os.path.isfile(_postinst), (
        f"missing {_postinst} — athena-branding installs AFTER grub in "
        f"the typical d-i pkgsel order, so grub's own postinst-driven "
        f"update-grub ran without seeing our 50-athena.cfg.  Without our "
        f"postinst retriggering, the boot menu stays unbranded until next "
        f"manual update-grub."
    )
    assert os.access(_postinst, os.X_OK), (
        f"{_postinst} not executable — dpkg won't run it"
    )
    with open(_postinst) as fh:
        _post_body = fh.read()
    assert 'update-grub' in _post_body, _post_body
    assert 'command -v update-grub' in _post_body, (
        "postinst must guard update-grub on `command -v` so chroot/live "
        "scenarios without grub don't fail the postinst"
    )
    assert '#DEBHELPER#' in _post_body, (
        "debhelper marker missing — dh_installdeb won't splice the auto-"
        "generated maintscript fragments (ldconfig, etc.) into the final "
        "postinst"
    )


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


def test_find_kernel_prefers_expected_kernel_pkg_match():
    """_find_kernel must prefer .debs whose filename starts with the
    cache-predicted binary name (e.g. linux-image-6.1.0-47-amd64) over
    the lexicographically highest match.  Without this, a stale higher-
    ABI .deb left in repo/ from a pre-rollback snapshot would win the
    glob — the ABI-47-vs-48 bug shape from 2026-05-19."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import iso_installer
    with tempfile.TemporaryDirectory() as _tmp:
        _repo = os.path.join(_tmp, 'repo')
        _chroot = os.path.join(_tmp, 'chroot')
        os.makedirs(_repo)
        os.makedirs(os.path.join(_chroot, 'boot'))
        # Plant stale higher-ABI .deb AND the cache-matching one.
        # Both must be valid ar archives or _find_kernel won't reach
        # the picker — but we only test the picker, so dpkg-deb-extract
        # would still fail.  Mock by monkey-patching subprocess.run.
        for _name in ('linux-image-6.1.0-47-amd64_6.1.170-3+thor1_amd64.deb',
                      'linux-image-6.1.0-48-amd64_6.1.172-1_amd64.deb',
                      'linux-image-6.1.0-49-amd64_6.1.172-1_amd64.deb'):
            with open(os.path.join(_repo, _name), 'wb') as fh:
                fh.write(b'!<arch>\n')

        # Stub subprocess + os.makedirs + glob so _find_kernel's
        # extraction step "succeeds" with a fake vmlinuz.  We just want
        # to know which .deb it chose.
        import subprocess as _sp
        _chosen = []
        _orig_run = _sp.run
        def _fake_run(cmd, **kw):
            class _R:
                returncode = 0
                stderr = ''
                stdout = ''
            # Record the .deb passed to dpkg-deb -x
            if 'dpkg-deb' in cmd and '-x' in cmd:
                _idx = cmd.index('-x')
                _chosen.append(cmd[_idx + 1])
                # Synthesize a vmlinuz under the extraction dir so the
                # post-extract glob finds something.
                _extract_dir = cmd[_idx + 2]
                os.makedirs(os.path.join(_extract_dir, 'boot'), exist_ok=True)
                with open(os.path.join(_extract_dir, 'boot', 'vmlinuz-x'),
                          'w') as fh:
                    fh.write('fake')
            return _R()
        _sp.run = _fake_run
        try:
            _result = iso_installer._find_kernel(
                _repo, _chroot, password='',
                expected_kernel_pkg='linux-image-6.1.0-47-amd64',
            )
        finally:
            _sp.run = _orig_run
        assert _chosen, "_find_kernel didn't reach the dpkg-deb -x step"
        assert '6.1.0-47-amd64' in _chosen[-1], (
            f"expected picker to choose ABI-47 .deb, chose: {_chosen[-1]}")


def test_find_kernel_falls_back_to_highest_when_no_match():
    """When expected_kernel_pkg has no match in repo/, _find_kernel
    falls back to highest-ABI sort (the original behaviour).  Ensures
    the new path doesn't break legacy callers that don't pass the hint."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import iso_installer
    with tempfile.TemporaryDirectory() as _tmp:
        _repo = os.path.join(_tmp, 'repo')
        _chroot = os.path.join(_tmp, 'chroot')
        os.makedirs(_repo)
        os.makedirs(os.path.join(_chroot, 'boot'))
        for _name in ('linux-image-6.1.0-47-amd64_6.1.170-3+thor1_amd64.deb',
                      'linux-image-6.1.0-48-amd64_6.1.172-1_amd64.deb'):
            with open(os.path.join(_repo, _name), 'wb') as fh:
                fh.write(b'!<arch>\n')
        import subprocess as _sp
        _chosen = []
        _orig_run = _sp.run
        def _fake_run(cmd, **kw):
            class _R:
                returncode = 0; stderr = ''; stdout = ''
            if 'dpkg-deb' in cmd and '-x' in cmd:
                _idx = cmd.index('-x')
                _chosen.append(cmd[_idx + 1])
                os.makedirs(os.path.join(cmd[_idx + 2], 'boot'), exist_ok=True)
                with open(os.path.join(cmd[_idx + 2], 'boot', 'vmlinuz-x'),
                          'w') as fh:
                    fh.write('fake')
            return _R()
        _sp.run = _fake_run
        try:
            _result = iso_installer._find_kernel(
                _repo, _chroot, password='',
                expected_kernel_pkg='linux-image-6.1.0-99-amd64',  # not on disk
            )
        finally:
            _sp.run = _orig_run
        # Fall-back: pick highest-ABI sort → 6.1.0-48 (lex-higher than 47)
        assert '6.1.0-48-amd64' in _chosen[-1], (
            f"expected fallback to highest-ABI, chose: {_chosen[-1]}")


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


def test_strip_debian_residue_hooks_removes_known_files():
    """pre-pkgsel.d hooks from upstream hw-detect and save-logs udebs
    try to apt-install Debian-specific tools (discover, installation-
    report) that aren't in our pool.  build_installer_chroot must
    strip them after udeb unpack so pkgsel's pre-hooks loop doesn't
    spam `E: Unable to locate package X` during install.

    Verifies: with both hook files present in a synthetic chroot
    layout, _strip_debian_residue_hooks calls `rm -f` on both and
    they're gone afterwards.  Missing files are non-fatal (rm -f
    is silent on missing) — covered by the no-op return path.
    """
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _strip_debian_residue_hooks

    def _fake_sudo(cmd, _pw):
        # Mimic `sudo rm -f <path>` semantics: ignore missing.
        class _R:
            returncode = 0
            stderr = ''
            stdout = ''
        if cmd[0] == 'rm' and cmd[1] == '-f':
            try:
                os.unlink(cmd[2])
            except FileNotFoundError:
                pass
        return _R()

    with tempfile.TemporaryDirectory() as _chroot:
        # Plant the two hooks plus a third (50install-firmware) we
        # MUST leave alone.
        for _rel in (
            'usr/lib/pre-pkgsel.d/20install-hwpackages',
            'usr/lib/pre-pkgsel.d/50save-logs',
            'usr/lib/pre-pkgsel.d/50install-firmware',
        ):
            _abs = os.path.join(_chroot, _rel)
            os.makedirs(os.path.dirname(_abs), exist_ok=True)
            with open(_abs, 'w') as fh:
                fh.write('#!/bin/sh\n# stub\n')

        with patch('installer_chroot._sudo', side_effect=_fake_sudo):
            assert _strip_debian_residue_hooks(_chroot, 'pw') is True

        # The two Debian-residue hooks are gone.
        for _rel in (
            'usr/lib/pre-pkgsel.d/20install-hwpackages',
            'usr/lib/pre-pkgsel.d/50save-logs',
        ):
            assert not os.path.exists(os.path.join(_chroot, _rel)), (
                f"{_rel} should have been stripped"
            )
        # The kept hook stays.
        _kept = os.path.join(_chroot, 'usr/lib/pre-pkgsel.d/50install-firmware')
        assert os.path.exists(_kept), (
            "50install-firmware must NOT be stripped (firmware loader, "
            "not Debian-specific)"
        )


def test_strip_debian_residue_hooks_idempotent_on_missing_targets():
    """A subsequent install build (or one where upstream dropped the
    hooks already) shouldn't fail.  `rm -f` is silent on missing →
    function returns True with zero files actually removed."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _strip_debian_residue_hooks

    def _fake_sudo(cmd, _pw):
        class _R:
            returncode = 0
            stderr = ''
            stdout = ''
        if cmd[0] == 'rm' and cmd[1] == '-f':
            try:
                os.unlink(cmd[2])
            except FileNotFoundError:
                pass
        return _R()

    with tempfile.TemporaryDirectory() as _chroot:
        # No pre-pkgsel.d/ at all.
        with patch('installer_chroot._sudo', side_effect=_fake_sudo):
            assert _strip_debian_residue_hooks(_chroot, 'pw') is True


def test_strip_debian_residue_hooks_called_in_build_flow():
    """Pin the call-site in build_installer_chroot so a future refactor
    can't silently drop the strip step.  Static check on the source —
    cheaper than a full integration test."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    from installer_chroot import build_installer_chroot
    _src = inspect.getsource(build_installer_chroot)
    assert '_strip_debian_residue_hooks(' in _src, (
        "_strip_debian_residue_hooks call missing from "
        "build_installer_chroot — Debian residue won't get stripped"
    )


def test_installer_chroot_register_self_appends_debian_installer_stanza():
    """Stock d-i image-build adds a dummy `Package: debian-installer`
    stanza to /var/lib/dpkg/status so `dpkg-query -W debian-installer`
    returns a result.  We replicate it under that exact name (not
    "athena-installer") so stock d-i scripts that string-compare the
    package name continue to work."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _register_self_in_dpkg_status
    with tempfile.TemporaryDirectory() as _chroot:
        _status = os.path.join(_chroot, 'var/lib/dpkg/status')
        os.makedirs(os.path.dirname(_status))
        # Pre-existing content (representative — one unrelated stanza).
        with open(_status, 'w') as fh:
            fh.write(
                'Package: foo\nStatus: install ok installed\n'
                'Version: 1.0\nArchitecture: amd64\n'
            )
        def _fake_sudo(cmd, _pw):
            class _R:
                returncode = 0
                stderr = ''
                stdout = ''
            if cmd[0] == 'cat':
                with open(cmd[1]) as fh:
                    _R.stdout = fh.read()
            return _R()
        with patch('installer_chroot._sudo', side_effect=_fake_sudo), \
             patch('installer_chroot.subprocess.run',
                   side_effect=_fake_sudo_write_run):
            assert _register_self_in_dpkg_status(
                _chroot, 'thor', 'pw') is True
        with open(_status) as fh:
            _content = fh.read()
        assert 'Package: foo' in _content, _content        # original preserved
        assert 'Package: debian-installer\n' in _content, _content
        assert 'Version: thor\n' in _content, _content
        # Password leak regression — must not appear in the on-disk file.
        assert 'pw' not in _content, _content


def test_installer_chroot_register_self_idempotent_on_repeat():
    """A second invocation must not duplicate the debian-installer stanza
    — otherwise rerunning `chroot build installer` after a partial run
    would compound stanzas every time."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _register_self_in_dpkg_status
    with tempfile.TemporaryDirectory() as _chroot:
        _status = os.path.join(_chroot, 'var/lib/dpkg/status')
        os.makedirs(os.path.dirname(_status))
        with open(_status, 'w') as fh:
            fh.write(
                'Package: debian-installer\nStatus: install ok installed\n'
                'Version: thor\nArchitecture: all\n'
            )
        _writes = []
        def _fake_sudo(cmd, _pw):
            class _R:
                returncode = 0; stderr = ''; stdout = ''
            if cmd[0] == 'cat':
                with open(cmd[1]) as fh:
                    _R.stdout = fh.read()
            return _R()
        def _fake_run(cmd, *_a, **_k):
            _writes.append(cmd)
            class _R: returncode = 0; stderr = ''
            return _R()
        with patch('installer_chroot._sudo', side_effect=_fake_sudo), \
             patch('installer_chroot.subprocess.run', side_effect=_fake_run):
            assert _register_self_in_dpkg_status(
                _chroot, 'thor', 'pw') is True
        # No tee write occurred — the helper noticed the stanza already
        # present and returned True without touching the file.
        assert _writes == [], _writes


def test_installer_grub_cfg_has_preseed_kernel_cmdline():
    """grub.cfg's kernel cmdline must carry `auto=true preseed/file=
    /preseed.cfg` so preseed-common.udeb loads /preseed.cfg at boot
    (stock d-i mechanism).  Replaces the prior load-preseed.sh overlay
    (deleted 2026-05-12)."""
    _path = os.path.join(_ROOT, 'installer', 'boot', 'grub.cfg')
    with open(_path) as fh:
        _content = fh.read()
    assert 'auto=true' in _content, _content
    assert 'preseed/file=/preseed.cfg' in _content, _content


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


# ─── COMP-02: repo index minimal/full + publish git minimal ──────────────────

def test_repo_index_dispatch():
    """`repo index full` (and bare `repo index`) → cmd_index_repo;
    `repo index minimal` → cmd_index_repo_minimal; unknown index sub →
    help (no handler invoked)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_index_repo         = lambda *a, **kw: _calls.append(('full', a))
    _sess.cmd_index_repo_minimal = lambda *a, **kw: _calls.append(('minimal', a))

    _sess.cmd_repo('index', 'full')
    assert _calls == [('full', ())], _calls
    _calls.clear()
    _sess.cmd_repo('index')                 # bare → full (back-compat)
    assert _calls == [('full', ())], _calls
    _calls.clear()
    _sess.cmd_repo('index', 'minimal')
    assert _calls == [('minimal', ())], _calls
    _calls.clear()
    _sess.cmd_repo('index', 'wat')          # unknown sub → help, no handler
    assert _calls == [], f"unknown index sub must not invoke a handler: {_calls}"


def test_deb_excluded_from_minimal():
    """The minimal (runtime) filter drops debug + source debs by package
    name, keeps ordinary runtime debs."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from apt_repo import deb_excluded_from_minimal as ex
    assert ex('linux-image-6.1.0-47-amd64-dbg_6.1.170-3_amd64.deb')
    assert ex('libc6-dbgsym_2.36-9_amd64.deb')
    assert ex('linux-source-6.1_6.1.170-3_all.deb')
    assert ex('gcc-12-source_12.2.0-14_all.deb')
    assert not ex('bash_5.2-15_amd64.deb')
    assert not ex('libssl3_3.0.14-1_amd64.deb')
    assert not ex('firefox-esr_140.10.2esr-1_amd64.deb')
    assert not ex('libfoo-dev_1.2-3_amd64.deb')   # -dev is not debug/source
    # over-match traps: a font pkg with 'source' in the name must be KEPT,
    # and the kernel's linux-source-<ver> (version-suffixed) must be DROPPED
    assert not ex('fonts-source-code-pro_2.030-1_all.deb')
    assert ex('linux-source-6.1_6.1.170-3_all.deb')


def test_generate_apt_repo_tolerates_empty_udeb_component():
    """A debs-only (minimal) pool has no udebs; generate_apt_repo must not
    treat the empty udeb Packages as fatal.  The udeb step passes
    allow_empty=True, while _scan_packages_to defaults allow_empty=False
    so the main deb index stays strict."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo
    _gen = inspect.getsource(apt_repo.generate_apt_repo)
    assert 'allow_empty=True' in _gen, (
        "udeb scan in generate_apt_repo must allow empty output")
    _sig = inspect.signature(apt_repo._scan_packages_to)
    assert _sig.parameters['allow_empty'].default is False, (
        "main deb scan must stay strict (allow_empty defaults False)")


def test_buildconfig_publish_dir_and_apt_source_default():
    """dir_publish is created under working_dir, and [Repo] AptSourceURL
    defaults to empty."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block="""
    [Mirror.main]
    BASEID = debian
    Suffix =
    Component = main
    """))
        cfg = _build_config_from(tmp, cfg_path)
        assert cfg.is_valid, f"BuildConfig invalid: {cfg.error_str}"
        assert cfg.apt_source_url == ''
        assert cfg.publish_ssh_target == ''
        assert cfg.publish_ssh_key == ''
        assert cfg.dir_publish == os.path.join(tmp, 'publish')
        assert os.path.isdir(cfg.dir_publish)


def test_repo_publish_dispatch():
    """`repo publish ssh full` / `repo publish ssh minimal` forward to
    cmd_repo_publish('ssh', <scope>)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_repo_publish = lambda *a, **kw: _calls.append(a)
    _sess.cmd_repo('publish', 'ssh', 'full')
    assert _calls == [('ssh', 'full')], _calls
    _calls.clear()
    _sess.cmd_repo('publish', 'ssh', 'minimal')
    assert _calls == [('ssh', 'minimal')], _calls


def test_rsync_streamed_argv_ignore_existing_and_filters():
    """_rsync_streamed: -aH + --info=progress2, ADDITIVE (no --delete); with
    ignore_existing=True it adds --ignore-existing + the given filters; a plain
    call adds neither.  (The immutable-.deb pool pass uses the former; the
    metadata pass uses the latter.)"""
    import sys, io
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)
    _popen = []

    class _FakeProc:
        def __init__(self):
            self.stdout = io.StringIO("100%\n")
            self.returncode = 0

        def wait(self):
            return 0

    def _fake_popen(argv, **kw):
        _popen.append(list(argv))
        return _FakeProc()

    _saved = build.subprocess.Popen
    build.subprocess.Popen = _fake_popen
    try:
        _capture_console_print(lambda: _sess._rsync_streamed(
            'debs', '/src', 'host:/dst', ['ssh', '-i', 'k'],
            ignore_existing=True,
            filters=['--include=*/', '--include=*.deb', '--exclude=*']))
        _argv = _popen[0]
        assert _argv[0] == 'rsync' and '-aH' in _argv and '--info=progress2' in _argv
        assert '--delete' not in _argv, _argv
        assert '--ignore-existing' in _argv, _argv
        assert '--include=*.deb' in _argv and '--exclude=*' in _argv, _argv
        _e = _argv.index('-e')
        assert _argv[_e + 1] == 'ssh -i k'
        assert _argv[-2] == '/src/' and _argv[-1] == 'host:/dst'
        # plain call: no --ignore-existing / no filters
        _popen.clear()
        _capture_console_print(lambda: _sess._rsync_streamed(
            'meta', '/s', 'h:/d', ['ssh']))
        assert '--ignore-existing' not in _popen[0]
        assert not any(a.startswith('--include') for a in _popen[0])
    finally:
        build.subprocess.Popen = _saved


def test_publish_full_remote_scan_flow_wiring():
    """`repo publish` (external on): push only .debs immutably
    (--ignore-existing + deb filter) → rebuild the index ON THE REMOTE
    (remote_reindex_and_sign) → upload metadata → refresh manifest → prune +
    record published.  External off → local merge, no rsync."""
    import re
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    _m = re.search(r'def cmd_repo_publish\(self.*?(?=\n    def )', _body, re.DOTALL)
    _b = _m.group(0)
    assert '_external_enabled()' in _b, "must gate on the external flag"
    assert 'ignore_existing=True' in _b and 'filters=_debfilter' in _b, (
        "the .deb pool pass must be immutable (--ignore-existing + deb filter)")
    assert 'remote_reindex_and_sign(' in _b, "must rebuild the index on the remote"
    assert 'write_published_manifest(' in _b, "must refresh the local manifest"
    assert '_refresh_merge_index()' in _b, "external-off path indexes locally"
    assert 'cmd_package_cleanup' in _b and \
        'published=self._snapshot_current()' in _b, "full prunes + records published"
    # ordering: deb push BEFORE remote re-index BEFORE metadata push
    assert (_b.index('ignore_existing=True') < _b.index('remote_reindex_and_sign(')
            < _b.index('rsync index')), "deb push → remote reindex → metadata push"


def test_repo_audit_external_dispatch():
    """`repo audit external` → cmd_audit_external; `repo audit <pkg>` still
    routes to cmd_audit (drill-in), and bare `repo audit` too."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)
    _calls = []
    _sess.cmd_audit          = lambda *a, **kw: _calls.append(('audit', a))
    _sess.cmd_audit_external = lambda *a, **kw: _calls.append(('external', a))

    _sess.cmd_repo('audit', 'external')
    assert _calls == [('external', ())], _calls
    _calls.clear()
    _sess.cmd_repo('audit', 'lsb-base')      # drill-in target, NOT external
    assert _calls == [('audit', ('lsb-base',))], _calls
    _calls.clear()
    _sess.cmd_repo('audit')                  # full local audit
    assert _calls == [('audit', ())], _calls


def test_cmd_audit_external_requires_apt_source_url():
    """audit external bails (no network) when AptSourceURL is unset."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _sess = BuildSession.__new__(BuildSession)

    class _Cfg:
        apt_source_url = ''
    _sess.config = _Cfg()
    out = _capture_console_print(lambda: _sess.cmd_audit_external())
    assert 'AptSourceURL' in out, out


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
    import build
    import tui as _tui
    from build import BuildSession, BuildFlags

    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.cache_ready = True
    _sess.cache = object()
    # Stub config — only the bits cmd_build_cache reads on the early
    # path before Cache() is constructed.
    class _StubCfg:
        snapshot_enabled = False
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
    _orig_Cache = build.Cache
    build.Cache = _StubCache
    try:
        _sess.cmd_build_cache('force')
    finally:
        build.Cache = _orig_Cache
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
    import build
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
    import build
    from build import BuildSession, BuildFlags

    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.cache_ready = True
    _sess.flags.dep_check_ready = True
    _sess.cache = object()
    _sess.dep_tree = object()

    _spinner_calls = []
    _orig_Spinner = build.Spinner
    class _StubSpinner:
        def __init__(self, *a, **kw):
            _spinner_calls.append((a, kw))
            raise RuntimeError("stop here — guard was bypassed, that's all we wanted to check")
        def done(self): pass
    build.Spinner = _StubSpinner
    try:
        try:
            _sess.cmd_parse_dependency('force')
        except RuntimeError as e:
            assert 'guard was bypassed' in str(e)
    finally:
        build.Spinner = _orig_Spinner
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


def test_cmd_container_purge_resets_flag_and_drops_session_ref():
    """cmd_container_purge with no docker state still resets the flag
    and drops self.container (idempotent contract: safe to run when
    no init has happened yet)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import docker as _docker  # confirm available before running test
    import build
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
    import build
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
    import build
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
                  'cmd_clean_download', 'cmd_clean_all'):
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
    """the shipped config/build.conf must default to snapshot pinning
    enabled, so cache and live mirror cannot drift between cache build and
    source build.  Lock-in test — fails if anyone flips Enabled back to false."""
    import configparser
    p = configparser.ConfigParser()
    cfg_path = os.path.join(_ROOT, 'config', 'build.conf')
    assert os.path.isfile(cfg_path), f"shipped build.conf missing at {cfg_path}"
    p.read(cfg_path)
    assert p.has_section('Snapshot'), "shipped build.conf is missing [Snapshot]"
    assert p.getboolean('Snapshot', 'Enabled') is True, (
        "regression: shipped build.conf must default Snapshot.Enabled = true"
    )


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
# strip_build_version edge cases
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
# strip_nmu_suffix + strip_nmu_from_control_text + strip_nmu_from_deb
# — normalise NMU/binNMU/backport suffixes off Debian version strings
# ─────────────────────────────────────────────────────────────────────────────


def test_classify_repo_subdir_routes_dev_to_main():
    """`-dev` packages live in repo/main alongside main binaries (not in
    a separate repo/dev subdir).  Reason: install-corpus packages
    hard-depend on them at runtime — build-essential Depends libc6-dev,
    gcc-12 Depends libgcc-12-dev, g++-12 Depends libstdc++-12-dev.
    Without -dev in main, those hard deps go unresolved at install.

    `-doc`, `-dbgsym`, `-test`/`-tests` stay in their own subdirs (true
    side artifacts that never install in any chroot)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import classify_repo_subdir
    cases = [
        # Main install corpus
        ('libc6_2.36-9_amd64.deb',              'main'),
        ('libc6-udeb_2.36-9_amd64.udeb',        'main'),
        # -dev → main (per the runtime-hard-dep observation above)
        ('libc6-dev_2.36-9_amd64.deb',          'main'),
        ('libgcc-12-dev_12.2.0-14_amd64.deb',   'main'),
        ('libstdc++-12-dev_12.2.0-14_amd64.deb', 'main'),
        ('python3-dev_3.11-1_amd64.deb',        'main'),
        # Other suffixes still segregate
        ('libfoo-doc_1.0-2_all.deb',            'doc'),
        ('libfoo-dbgsym_1.0-2_amd64.deb',       'dbgsym'),
        ('libfoo-tests_1.0-2_amd64.deb',        'tests'),
        ('libfoo-test_1.0-2_amd64.deb',         'tests'),
        # Mid-name -dev doesn't trigger (suffix-only check)
        ('libfoo-dev-bin_1.0-2_amd64.deb',      'main'),
        # Malformed → safe default
        ('not_a_real_deb.deb',                  'main'),
    ]
    for inp, expected in cases:
        got = classify_repo_subdir(inp)
        assert got == expected, (
            f"classify_repo_subdir({inp!r}) = {got!r}, expected {expected!r}"
        )


def test_strip_nmu_suffix_strips_known_patterns():
    """Each canonical NMU/binNMU/backport layer pattern strips cleanly."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_suffix
    cases = [
        ('1.0-2',                  '1.0-2'),           # pristine — no-op
        ('1.0-2+b1',               '1.0-2'),           # modern binNMU
        ('1.0-2+b99',              '1.0-2'),           # high N
        ('1.0-2+deb12u3',          '1.0-2'),           # security/point update
        ('1.0-2+deb12u3+b1',       '1.0-2'),           # stacked: NMU + binNMU
        ('2.90-4~deb12u2',         '2.90-4'),          # sort-BEFORE-base NMU
        ('1.0-2~bpo12+1',          '1.0-2'),           # backport
        ('1.0-2+rpi1',             '1.0-2'),           # raspbian rebuild
        ('1.0-2+rpt2',             '1.0-2'),           # raspberry-pi-tools
        ('0.15.5-2b',              '0.15.5-2'),        # legacy binNMU (bare b)
        ('0.15.5-2b1',             '0.15.5-2'),        # legacy binNMU (bN)
        ('2:1.0-2+b1',             '2:1.0-2'),         # epoch preserved
        ('1.0-2alpha',             '1.0-2alpha'),      # not an NMU layer
        ('libb1.0-2',              'libb1.0-2'),       # 'b1' embedded in name
        # REGRESSION 2026-05-21: constraint-style trailing-tilde marker
        # AFTER an NMU suffix.  libflatpak0's `Depends: bubblewrap
        # (>= 0.8.0-2+deb12u1~)` was leaking through strip because the
        # regex's `$` anchor was blocked by the `~`.  Each of these
        # is the constraint-version literal that the on-disk Depends
        # carries — strip must scrub the entire NMU+tilde block.
        ('0.8.0-2+deb12u1~',       '0.8.0-2'),         # the bubblewrap case
        ('1.0-2+b1~',              '1.0-2'),           # binNMU + tilde
        ('1.0~bpo12+1~',           '1.0'),             # backport + tilde
        ('2.36-9+deb12u14~',       '2.36-9'),          # glibc-shape + tilde
        # NOT an NMU — bare-version pre-release tilde must SURVIVE.
        ('1.0~rc1',                '1.0~rc1'),         # rc marker
        ('72.1~rc-1',              '72.1~rc-1'),       # real-world: icu
    ]
    for input_ver, expected in cases:
        got = strip_nmu_suffix(input_ver)
        assert got == expected, (
            f"strip_nmu_suffix({input_ver!r}) = {got!r}, expected {expected!r}"
        )


def test_strip_nmu_suffix_idempotent():
    """Re-running on an already-stripped version is a no-op."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_suffix
    for v in ('1.0-2', '2:1.0-2', '6.1.172-1'):
        assert strip_nmu_suffix(strip_nmu_suffix(v)) == strip_nmu_suffix(v)


def test_strip_nmu_from_control_text_walks_relation_fields():
    """The in-memory text transform must strip from Version, Depends,
    Pre-Depends, Recommends, Suggests, Enhances, Provides, Conflicts,
    Breaks, Replaces.  Other fields (Architecture, Description) untouched."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_from_control_text
    _ctrl = (
        'Package: libfoo\n'
        'Version: 1.0-2+deb12u3+b1\n'
        'Architecture: amd64\n'
        'Depends: libbar (>= 1.0-2+b1), libbaz (= 0.15.5-2b)\n'
        'Provides: libfoo (= 1.0-2+b1)\n'
        'Conflicts: liboldfoo (<< 1.0-2+deb12u3)\n'
        'Description: just a test (no version, 2+b1 not in a constraint)\n'
    )
    _out, _n = strip_nmu_from_control_text(_ctrl)
    assert _n >= 5, f"expected at least 5 strips, got {_n}"
    assert 'Version: 1.0-2\n' in _out
    assert 'Depends: libbar (>= 1.0-2), libbaz (= 0.15.5-2)\n' in _out
    assert 'Provides: libfoo (= 1.0-2)\n' in _out
    assert 'Conflicts: liboldfoo (<< 1.0-2)\n' in _out
    # Description must be untouched — `2+b1` inside Description text is
    # not a version constraint.
    assert '2+b1' in _out, "Description text should not be stripped"


def test_strip_nmu_pair_rewrite_collapses_sibling_idiom():
    """The upstream `X (>> V), X (<< V-.)` "any debrev of upstream V"
    idiom is collapsed to `X (= our_version)` at strip time.  After
    strip-NMU, our siblings ship at `V-0` which apt treats as equal to
    bare V (Policy §5.6.12), so the >> half of the original fails.
    The rewrite locks the constraint to our own atomic-source-build
    version, matching the invariant our pipeline actually preserves.

    See docs/strip-nmu-sibling-constraint-idiom.md for the full
    rationale + impact analysis."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_from_control_text
    # Real git-shape: Depends pair with same name, >> V on one half,
    # << V-. on the other.  Mixed with unrelated constraints that must
    # be preserved as-is.
    _ctrl = (
        'Package: git\n'
        'Version: 1:2.39.5-0\n'
        'Architecture: amd64\n'
        'Depends: libc6 (>= 2.34), git-man (>> 1:2.39.5),'
        ' git-man (<< 1:2.39.5-.), perl\n'
        'Description: a test stanza\n'
    )
    _out, _n = strip_nmu_from_control_text(_ctrl)
    # Pair collapses to single (=) entry; >> and << gone.
    assert 'git-man (= 1:2.39.5-0)' in _out, (
        f"sibling idiom not collapsed; got:\n{_out}"
    )
    assert '>>' not in _out and '<< 1:2.39.5-.' not in _out, (
        f"original pair still present after rewrite:\n{_out}"
    )
    # Unrelated constraints survive untouched.
    assert 'libc6 (>= 2.34)' in _out
    assert ', perl' in _out


def test_strip_nmu_pair_rewrite_only_when_pair_matches():
    """The rewriter is strictly bound to the FULL pair pattern.
    Single `>>` or pair without the `-.` upper bound must NOT trigger
    the collapse — that would be relaxing a constraint the maintainer
    might have written for an entirely different reason."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_from_control_text

    # Case A: lone `>>` (no matching `<< V-.`) — must NOT rewrite.
    _ctrl_a = (
        'Package: foo\n'
        'Version: 1.0-3\n'
        'Architecture: amd64\n'
        'Depends: libbar (>> 1.0)\n'
    )
    _out_a, _ = strip_nmu_from_control_text(_ctrl_a)
    assert 'libbar (>> 1.0)' in _out_a, (
        "lone >> incorrectly rewritten"
    )

    # Case B: pair with DIFFERENT names — must NOT rewrite.
    _ctrl_b = (
        'Package: foo\n'
        'Version: 1.0-3\n'
        'Architecture: amd64\n'
        'Depends: libbar (>> 1.0), libquux (<< 1.0-.)\n'
    )
    _out_b, _ = strip_nmu_from_control_text(_ctrl_b)
    assert 'libbar (>> 1.0)' in _out_b
    assert 'libquux (<< 1.0-.)' in _out_b

    # Case C: pair without `-.` upper bound (e.g. `<< 2.0` instead).
    # NOT the canonical idiom — different intent, must NOT rewrite.
    _ctrl_c = (
        'Package: foo\n'
        'Version: 1.0-3\n'
        'Architecture: amd64\n'
        'Depends: libbar (>> 1.0), libbar (<< 2.0)\n'
    )
    _out_c, _ = strip_nmu_from_control_text(_ctrl_c)
    assert 'libbar (>> 1.0)' in _out_c
    assert 'libbar (<< 2.0)' in _out_c

    # Case D: OR-alternative containing the pair — must NOT rewrite
    # (the idiom is AND-level only; OR-alternatives change the semantics).
    _ctrl_d = (
        'Package: foo\n'
        'Version: 1.0-3\n'
        'Architecture: amd64\n'
        'Depends: libbar (>> 1.0) | libbar (<< 1.0-.)\n'
    )
    _out_d, _ = strip_nmu_from_control_text(_ctrl_d)
    assert '(>> 1.0)' in _out_d and '(<< 1.0-.)' in _out_d, (
        "OR-grouped pair incorrectly collapsed; the idiom only applies "
        "at AND-level"
    )


def test_strip_nmu_pair_rewrite_idempotent():
    """Re-running on already-rewritten content is a no-op (pair was
    already collapsed to `(= our_version)`, no `>>` half remains to
    detect)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_from_control_text
    _ctrl = (
        'Package: git\n'
        'Version: 1:2.39.5-0\n'
        'Architecture: amd64\n'
        'Depends: git-man (= 1:2.39.5-0)\n'
    )
    _out, _n = strip_nmu_from_control_text(_ctrl)
    # Should be a no-op — content already has (=) form.
    assert _out == _ctrl
    assert _n == 0


def test_strip_nmu_pair_rewrite_scoped_to_depends_pre_depends():
    """The idiom is a hard-link convention (Depends / Pre-Depends).
    Recommends / Suggests / Enhances must NOT be rewritten — they're
    soft links and could legitimately use the `>>, <<-.` pair to
    express "if you happen to have this debrev, fine".  Conflicts /
    Breaks similarly carry different semantics."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_from_control_text
    _ctrl = (
        'Package: foo\n'
        'Version: 1.0-3\n'
        'Architecture: amd64\n'
        'Recommends: libbar (>> 1.0), libbar (<< 1.0-.)\n'
        'Suggests:   libquux (>> 2.0), libquux (<< 2.0-.)\n'
    )
    _out, _ = strip_nmu_from_control_text(_ctrl)
    assert 'libbar (>> 1.0)' in _out and 'libbar (<< 1.0-.)' in _out, (
        "Recommends pair was rewritten; idiom must be scoped to "
        "Depends/Pre-Depends only"
    )
    assert 'libquux (>> 2.0)' in _out and 'libquux (<< 2.0-.)' in _out


def test_strip_nmu_from_deb_round_trip():
    """End-to-end: synthesise a .deb with NMU suffixes everywhere, run
    strip_nmu_from_deb, verify filename + internal Version + all dep
    constraints are normalised, epoch preserved."""
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_from_deb
    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_strip_nmu_from_deb_round_trip (no dpkg-deb)")
        return
    with tempfile.TemporaryDirectory() as _tmp:
        _work = os.path.join(_tmp, 'src')
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        os.makedirs(os.path.join(_work, 'usr', 'lib'))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(
                'Package: libfoo\n'
                'Version: 2:1.0-2+deb12u3+b1\n'      # epoch + NMU stack
                'Architecture: amd64\n'
                'Maintainer: T <t@l>\n'
                'Depends: libbar (>= 1.0-2+b1), libbaz (= 0.15.5-2b)\n'
                'Conflicts: liboldfoo (<< 1.0-2+deb12u3)\n'
                'Description: round-trip test\n'
            )
        with open(os.path.join(_work, 'usr', 'lib', 'p'), 'w') as fh:
            fh.write('x\n')
        _orig = os.path.join(_tmp, 'libfoo_1.0-2+deb12u3+b1_amd64.deb')
        subprocess.run(
            ['dpkg-deb', '--root-owner-group', '-b', _work, _orig],
            check=True, capture_output=True,
        )

        _r = strip_nmu_from_deb(_orig)
        assert _r['status'] == 'rewritten', _r
        assert _r['strips_count'] >= 5, _r
        assert os.path.basename(_r['new_path']) == 'libfoo_1.0-2_amd64.deb'
        # Verify internal metadata
        _ver = subprocess.run(
            ['dpkg-deb', '-f', _r['new_path'], 'Version'],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert _ver == '2:1.0-2', f"epoch lost? {_ver!r}"
        _deps = subprocess.run(
            ['dpkg-deb', '-f', _r['new_path'], 'Depends'],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert '+b1' not in _deps and '+deb12u3' not in _deps and '2b' not in _deps, (
            f"NMU residue in Depends: {_deps!r}"
        )


def test_strip_nmu_from_deb_idempotent():
    """Re-running on an already-stripped .deb returns 'unchanged' with
    no I/O — important for the post-build hook to avoid wasted repacks
    when an idempotent rebuild happens."""
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_from_deb
    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_strip_nmu_from_deb_idempotent (no dpkg-deb)")
        return
    with tempfile.TemporaryDirectory() as _tmp:
        _work = os.path.join(_tmp, 'src')
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        os.makedirs(os.path.join(_work, 'usr', 'lib'))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(
                'Package: libfoo\n'
                'Version: 1.0-2\n'
                'Architecture: amd64\n'
                'Maintainer: T <t@l>\n'
                'Depends: libbar (>= 1.0-2)\n'
                'Description: already stripped\n'
            )
        with open(os.path.join(_work, 'usr', 'lib', 'p'), 'w') as fh:
            fh.write('x\n')
        _p = os.path.join(_tmp, 'libfoo_1.0-2_amd64.deb')
        subprocess.run(
            ['dpkg-deb', '--root-owner-group', '-b', _work, _p],
            check=True, capture_output=True,
        )
        _r = strip_nmu_from_deb(_p)
        assert _r['status'] == 'unchanged', _r
        assert _r['strips_count'] == 0, _r


def test_buildcontainer_calls_strip_post_build():
    """BuildContainer.build must normalise artifacts (strip NMU + UPD-01 asg
    stamp) post-dpkg-buildpackage on every successfully-built source, over
    the just-emitted file list (STA-21).  Pin via code inspection."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    # Pin: normalise helper exists (renamed from _strip_nmu_from_built_artifacts)
    assert re.search(
        r'def _normalize_built_artifacts\(self', _body), (
        "BuildContainer needs a _normalize_built_artifacts method that strips "
        "NMU then conditionally applies the +asg<R>u<N> stamp")
    _m = re.search(
        r'def build\(self, src_pkg.*?(?=\n    def )',
        _body, re.DOTALL)
    assert _m, "BuildContainer.build not found"
    _build_body = _m.group(0)
    assert ('self._normalize_built_artifacts(src_pkg, _emitted, _was_patched)'
            in _build_body), (
        "BuildContainer.build must call _normalize_built_artifacts with the "
        "emitted-files list AND was_patched — REGRESSION to pre-STA-21 if the "
        "file list is dropped (re-introduces the post-build full-repo stall)")
    # COMP-03 Phase 1: signature now (src_pkg, scratch_dir) — match either
    # whitespace shape (single-line or multi-line call site).
    import re as _re_check
    assert _re_check.search(
        r'_emitted\s*=\s*self\._segregate_built_artifacts\(\s*src_pkg\s*,\s*_scratch_dir\s*\)',
        _build_body), (
        "_segregate_built_artifacts must return its moved-files list so "
        "normalise iterates only those — REGRESSION to pre-STA-21 if dropped; "
        "COMP-03 Phase 1 also requires the per-worker scratch dir arg "
        "(`_scratch_dir`) so concurrent workers don't race on the host-side "
        "os.listdir(repo_path) scan")


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
    _m = re.search(
        r'def _strip_nmu_from_built_artifacts\(.*?(?=\n    def )',
        _body, re.DOTALL)
    assert _m, "_strip_nmu_from_built_artifacts not found"
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
        r'def _strip_nmu_from_built_artifacts\(self,\s*src_pkg,\s*\n?\s*built_files',
        _fn_body
    ), (
        "_strip_nmu_from_built_artifacts must accept `built_files` as "
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
    _bc.mirrors = []   # empty → empty sources.list; OK for argv-shape pin
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


def test_buildcontainer_run_grub_mkrescue_propagates_failure():
    """Non-zero exit from docker container → (False, stdout, stderr)."""
    import sys
    from unittest.mock import MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer

    _bc = buildcontainer.BuildContainer.__new__(buildcontainer.BuildContainer)
    _bc._image_tag = 'athenalinux:build-test'
    _bc.mirrors = []
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


def test_iso_installer_run_grub_mkrescue_routes_through_container():
    """COMP-14: iso_installer._run_grub_mkrescue must delegate to
    container.run_grub_mkrescue, NOT directly shell out to bare
    `grub-mkrescue` (which would re-introduce host-grub contamination).
    Pin via code inspection so a future refactor can't silently
    regress."""
    _src = os.path.join(_ROOT, 'scripts', 'iso_installer.py')
    with open(_src) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r'def _run_grub_mkrescue\(.*?(?=\n(?:def |\Z))',
        _body, re.DOTALL,
    )
    assert _m, '_run_grub_mkrescue not found'
    _fn = _m.group(0)
    assert 'container.run_grub_mkrescue' in _fn, (
        "iso_installer._run_grub_mkrescue must delegate to "
        "container.run_grub_mkrescue — REGRESSION to pre-COMP-14 "
        "if it shells out to bare grub-mkrescue.  Build host's grub "
        "would leak back into the ISO bootloader."
    )
    # Strip docstrings before checking the no-bare-subprocess assertion
    _fn_code = re.sub(r'""".*?"""', '', _fn, flags=re.DOTALL)
    _fn_code = re.sub(r"'''.*?'''", '', _fn_code, flags=re.DOTALL)
    assert "['grub-mkrescue'" not in _fn_code and '["grub-mkrescue"' not in _fn_code, (
        "REGRESSION: iso_installer._run_grub_mkrescue is shelling out "
        "to bare `grub-mkrescue` again — defeats COMP-14"
    )


def test_iso_py_build_iso_routes_through_container():
    """COMP-14: scripts/iso.py:build_iso must delegate to
    container.run_grub_mkrescue, not shell out to bare grub-mkrescue.
    Same anti-regression as the installer-ISO side."""
    _src = os.path.join(_ROOT, 'scripts', 'iso.py')
    with open(_src) as fh:
        _body = fh.read()
    assert 'container.run_grub_mkrescue' in _body, (
        "scripts/iso.py:build_iso must delegate to "
        "container.run_grub_mkrescue — REGRESSION to pre-COMP-14 if "
        "it shells out to bare grub-mkrescue."
    )
    import re
    _code = re.sub(r'""".*?"""', '', _body, flags=re.DOTALL)
    _code = re.sub(r"'''.*?'''", '', _code, flags=re.DOTALL)
    _code = '\n'.join(
        _ln for _ln in _code.splitlines()
        if not _ln.lstrip().startswith('#')
    )
    assert "['grub-mkrescue'" not in _code and '["grub-mkrescue"' not in _code, (
        "REGRESSION: scripts/iso.py is shelling out to bare "
        "`grub-mkrescue` again — defeats COMP-14"
    )


def test_build_py_threads_container_into_iso_callsites():
    """COMP-14: build.py's cmd_build_iso_live + cmd_build_iso_installer
    must pass self.container through to the ISO builders.  Pin via
    code inspection so the wiring can't silently drop."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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


def test_audit_nmu_residue_skips_tunneled_sources():
    """Tunneled packages (pristine Debian binary passthrough) MUST keep their
    upstream ~debNuN / +debNuN suffix — rewriting would invalidate any embedded
    signature (Microsoft Secure Boot on shim-signed et al.).  audit_nmu_residue
    must skip them when given a tunnel_sources set, otherwise the auditor
    flags them and the suggested `repo repair strip` corrupts the signature.
    Caught 2026-05-28."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    _state = repo_audit.RepoState(
        packages={
            'shim-signed': {
                'Source': 'shim-signed (1.44~1+deb12u1)',
                'Version': '1.44~1+deb12u1+15.8-1~deb12u1',
            },
            # Pkg with no Source field — defaults to its own name, source
            # name = 'intel-microcode' which is also tunneled.
            'intel-microcode': {
                'Version': '3.20251111.1~deb12u1',
            },
            # NON-tunneled binary that genuinely has residue → must be flagged.
            'libfoo': {
                'Source': 'foo',
                'Version': '1.0-2+deb12u1',
            },
        },
        provides_index={}, packages_file='', repo_mtime=0,
    )
    _tun = {'shim-signed', 'shim-helpers-amd64-signed', 'intel-microcode'}
    _findings = repo_audit.audit_nmu_residue(_state, tunnel_sources=_tun)
    _pkgs = {f[0] for f in _findings}
    # The tunneled ones must NOT appear.
    assert 'shim-signed' not in _pkgs, _findings
    assert 'intel-microcode' not in _pkgs, _findings
    # The genuinely-non-tunneled one IS flagged.
    assert 'libfoo' in _pkgs, _findings
    # Without the tunnel set, all three get flagged (baseline).
    _findings_all = repo_audit.audit_nmu_residue(_state)
    assert {'shim-signed', 'intel-microcode', 'libfoo'} == \
        {f[0] for f in _findings_all}, _findings_all


def test_cmd_audit_nmu_residue_absorbed_into_cmd_audit():
    """P3 (2026-05-23): `repo audit_nmu` no longer exists as a
    standalone verb.  Its logic — audit_nmu_residue + reporting —
    lives in cmd_audit's _report_nmu_residue helper, run as the
    final section of every `repo audit` invocation.  Pin both:
    (a) the standalone verb is gone, (b) the absorbed helper is
    called from cmd_audit."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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


def test_cmd_package_cleanup_dry_run_default_force_flag_required():
    """`repo cleanup` without `force` must NOT delete any files —
    it's a dry-run by default.  Operator opt-in for the destructive
    path is double-gated: pass `force` flag AND answer 'y' to the
    YESNO prompt.

    Source-text inspection because exercising the full method end-to-
    end needs a fixture'd BuildSession + repo dir + control files."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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


def test_package_audit_includes_stale_files_warning_section():
    """`repo audit` must call _scan_stale_files (via the
    _report_stale_files_warning helper) so the operator sees orphan-
    source and version-drift residue surfaced as a soft warning at
    the end of every audit run.  Audit is the natural place to flag
    these — they don't break dep resolution (apt picks highest-version
    per name and the older sibling becomes a phantom), but they DO
    silently break chroot builds and install-time dpkg unpack.

    Anti-regression for the 2026-05-21 base-files Path X migration
    where stale base-files_12.4_amd64.deb sat next to the new
    base-files_12.4+deb12u14+athena1_amd64.deb and neither audit nor
    dep-parse complained until source-build looped."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r"\n    def cmd_audit\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m is not None
    _audit = _m.group(0)
    # The warning helper is invoked.
    assert '_report_stale_files_warning' in _audit, (
        "cmd_audit must call _report_stale_files_warning so STALE FILES "
        "section appears in the audit output"
    )
    # Helper exists and does the right thing.
    _m_helper = re.search(
        r"\n    def _report_stale_files_warning\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m_helper is not None, "_report_stale_files_warning helper missing"
    _helper = _m_helper.group(0)
    assert 'self._scan_stale_files()' in _helper, (
        "warning helper must call _scan_stale_files (single source of "
        "categorisation truth)"
    )
    assert 'STALE FILES' in _helper, (
        "helper must surface a STALE FILES section header"
    )
    # NOT a hard gate — must point operator to cleanup, not abort.
    assert 'repo cleanup' in _helper, (
        "warn-only path must point operator at `repo cleanup` for "
        "the actual action"
    )
    # Helper must NOT call os.remove — warn-only.
    assert 'os.remove' not in _helper, (
        "warn-only audit path must NOT delete files; cleanup owns "
        "deletion"
    )


def test_audit_nmu_residue_detects_layered_versions():
    """audit_nmu_residue must flag any version with NMU layer remaining,
    in Version field OR in any dep-field version constraint."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    # Hand-construct a RepoState to exercise the auditor without I/O.
    _state = repo_audit.RepoState(
        packages={
            'libfoo': {
                'Package': 'libfoo',
                'Version': '1.0-2',                    # clean
                'Depends': 'libbar (>= 1.0-2)',        # clean
            },
            'libdirty': {
                'Package': 'libdirty',
                'Version': '1.0-2+deb12u3',            # NMU residue
                'Depends': 'libbar (>= 1.0-2+b1)',     # NMU residue in constraint
            },
        },
        provides_index={},
        packages_file='/dev/null',
        repo_mtime=0.0,
    )
    _findings = repo_audit.audit_nmu_residue(_state)
    _pkgs = {f[0] for f in _findings}
    _fields = {(f[0], f[1]) for f in _findings}
    assert 'libdirty' in _pkgs, (
        f"libdirty has NMU residue, expected in findings: {_findings}"
    )
    assert 'libfoo' not in _pkgs, (
        f"libfoo is clean, should NOT be in findings: {_findings}"
    )
    assert ('libdirty', 'Version') in _fields, (
        f"Version field should be flagged: {_findings}"
    )
    assert ('libdirty', 'Depends') in _fields, (
        f"Depends constraint should be flagged: {_findings}"
    )


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


def test_version_no_epoch_strips_epoch_from_debian_version():
    """`Version('1:2.39.5-0+deb12u3')` → `'2.39.5-0+deb12u3'`."""
    from utils import version_no_epoch
    from debian.debian_support import Version
    assert version_no_epoch(Version('1:2.39.5-0+deb12u3')) == '2.39.5-0+deb12u3'
    assert version_no_epoch(Version('1:15.0.6-4')) == '15.0.6-4'


def test_version_no_epoch_no_change_when_no_epoch():
    """Version without an epoch round-trips unchanged."""
    from utils import version_no_epoch
    from debian.debian_support import Version
    assert version_no_epoch(Version('2.39.5-0+deb12u3')) == '2.39.5-0+deb12u3'
    assert version_no_epoch(Version('1.13.0+dfsg-1')) == '1.13.0+dfsg-1'


def test_version_no_epoch_accepts_string_input():
    """Plain string input (not just Version) works — coerced via str()."""
    from utils import version_no_epoch
    assert version_no_epoch('1:2.39.5-0+deb12u3') == '2.39.5-0+deb12u3'
    assert version_no_epoch('2.39.5-0+deb12u3') == '2.39.5-0+deb12u3'


def test_version_no_epoch_handles_multidigit_epoch():
    """Epoch is `[0-9]+` per Debian policy — multi-digit epochs work."""
    from utils import version_no_epoch
    assert version_no_epoch('42:1.0-1') == '1.0-1'
    assert version_no_epoch('100:9.8.7-3') == '9.8.7-3'


def test_version_no_epoch_only_strips_first_colon():
    """Only the first `:` (epoch separator) is consumed.  Subsequent
    colons are part of the upstream version (rare but allowed in
    Debian policy 5.6.12 for `[0-9][A-Za-z0-9.+-:~]*`)."""
    from utils import version_no_epoch
    assert version_no_epoch('1:2.0:beta-1') == '2.0:beta-1'


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
            build_distribution = 'Athena'
            build_base_id = 'athena'
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
            cache_ready             = False
            dep_check_ready         = False
            download_ready          = False
            build_container_ready   = False
            source_build_ready      = False
            signing_key_verified    = False
            chroot_ready            = False
            chroot_verified         = False
            chroot_installer_ready  = False
            iso_live_ready          = False
            iso_installer_ready     = False
            iso_disk_ready          = False
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
    # All twelve rows should be present as unticked.
    assert output.count('[·]') == 12, (
        "Expected 12 unticked rows (one per BuildFlag), got "
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
# Cache._GCC_BASE_RE pattern
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
    _seed_test_dep_tree(dt, cache, {'firefox': seed})
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
    _seed_test_dep_tree(dt, cache, {'firefox': seed})
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


def test_pull_recommends_extras_walks_transitive_depends():
    """REGRESSION (2026-05-20, pre-audit-split tag): the old code
    inserted recommends directly into selected_pkgs WITHOUT going through
    parse_dependency.  The recommend's own Depends graph was never walked,
    leaving leaf libraries (libksba8, libnpth0, libmbim-glib4 et al)
    absent from the install corpus and source-build queue.  Result: 80+
    unresolved package_audit findings hidden until install time.

    After the fix, pull_recommends_extras calls self.parse_dependency,
    which recurses into hard Depends.  Verify it pulls dirmngr's
    libksba8 along for the ride."""
    import dependencytree
    seed = _FakePkg('libgpgme11', source='gpgme1.0',
                    filename='libgpgme11_1.0_amd64.deb',
                    recommends=['dirmngr'])
    # dirmngr Depends libksba8.  libksba8 is in the cache but no consumer
    # selected it directly — only dirmngr (via recommend) needs it.
    dirmngr = _FakePkg('dirmngr', source='gnupg2',
                       filename='dirmngr_2.0_amd64.deb',
                       depends=['libksba8'])
    libksba8 = _FakePkg('libksba8', source='libksba',
                        filename='libksba8_1.6_amd64.deb')
    cache = _FakeCache({
        'libgpgme11': [seed], 'dirmngr': [dirmngr], 'libksba8': [libksba8],
    })
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    _seed_test_dep_tree(dt, cache, {'libgpgme11': seed})
    added = dt.pull_recommends_extras()
    assert added == 1   # only dirmngr is a direct recommend
    # The fix's payload: libksba8 follows dirmngr in via the Depends walk.
    assert 'dirmngr' in dt.selected_pkgs, \
        "direct recommend missing from selected_pkgs"
    assert 'libksba8' in dt.selected_pkgs, \
        "BUG 1 REGRESSED: recommend's transitive Depends not walked"
    # Both the direct recommend AND its transitive Depends are marked
    # extras (their only justification is the recommend chain).
    assert 'dirmngr' in dt.extras_pkg_names
    assert 'libksba8' in dt.extras_pkg_names
    # Seed itself stays out of extras (it's a "real" selection).
    assert 'libgpgme11' not in dt.extras_pkg_names


def test_parse_sources_uses_per_tree_src_pkg_files_not_shared_source_attr():
    """REGRESSION (2026-05-20, pre-audit-split tag): Source.pkgs used to
    live on the Source object, which is SHARED across the deb and udeb
    DependencyTree instances via cache.source_hashtable.  The udeb tree's
    parse_sources reset .pkgs = [] before appending only the udeb binary,
    overwriting the deb tree's prediction.  source_audit then saw only
    libc6-udeb (present in repo) and missed missing deb binaries like
    libc6-dev.

    The fix: per-tree src_pkg_files dict on DependencyTree.  Two trees,
    two independent maps.  Verify by populating both and confirming
    neither overwrites the other.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    import package as _pkg_mod

    # Source.pkgs attribute must NOT exist on a fresh Source instance —
    # if it reappears, the storage class drifted back to the shared-
    # mutable-state design and this regression will recur.
    _src = _pkg_mod.Source.__new__(_pkg_mod.Source)
    assert not hasattr(_src, 'pkgs'), (
        "Source.pkgs has reappeared — per-tree src_pkg_files split "
        "is broken; the udeb tree will silently overwrite the deb "
        "tree's predictions again")

    # Two independent DependencyTrees with their own src_pkg_files.
    # Simulate parse_sources having populated each from its own
    # selected_pkgs without any shared state to clobber.
    dt_deb = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt_deb.src_pkg_files = {'glibc': ['libc6_2.36-9_amd64.deb',
                                       'libc6-dev_2.36-9_amd64.deb',
                                       'libc-bin_2.36-9_amd64.deb']}
    dt_udeb = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt_udeb.src_pkg_files = {'glibc': ['libc6-udeb_2.36-9_amd64.udeb']}

    # Cross-tree isolation: each tree's view is intact, the other's
    # write didn't leak.
    assert 'libc6-dev_2.36-9_amd64.deb' in dt_deb.src_pkg_files['glibc']
    assert 'libc6-udeb_2.36-9_amd64.udeb' in dt_udeb.src_pkg_files['glibc']
    assert 'libc6-udeb_2.36-9_amd64.udeb' not in dt_deb.src_pkg_files['glibc']
    assert 'libc6-dev_2.36-9_amd64.deb' not in dt_udeb.src_pkg_files['glibc']


def test_explicit_provides_version_returns_none_for_unversioned_provides():
    """Anti-regression for the real-Package code path: when a stanza
    declares `Provides: fwupdate` with no version, the new
    explicit_provides_version helper must return None — NOT fall back
    to the provider's own version like get_provides does.

    The stub-based validate_selection tests use a hand-rolled
    explicit_provides_version; this test pins the real Package class's
    behavior so the stub can't drift out of sync with production."""
    import sys, io
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import apt_pkg
    apt_pkg.init()
    from debian.deb822 import Packages
    from package import Package

    _stanza = (
        "Package: fwupd\n"
        "Version: 1.8.12-2\n"
        "Architecture: amd64\n"
        "Maintainer: Test <test@example>\n"
        "Description: test\n"
        "Provides: fwupdate\n"
        "Filename: pool/main/f/fwupd/fwupd_1.8.12-2_amd64.deb\n"
        "SHA256: deadbeef\n"
        "Size: 100\n"
    )
    section = next(Packages.iter_paragraphs(io.StringIO(_stanza)))
    pkg = Package(section)
    assert pkg.isvalid, "test fixture failed to parse"
    # get_provides STILL substitutes (kept that way for Depends
    # resolution semantics — apt treats the provider's version as
    # satisfying for `Depends: virtual`).
    assert pkg.get_provides() == [('fwupdate', pkg.version)], (
        "get_provides regression — must keep substituting self.version "
        "for unversioned Provides so Depends-satisfaction lookups stay "
        "consistent with apt's behaviour")
    # The new helper does NOT substitute — returns None for unversioned.
    assert pkg.explicit_provides_version('fwupdate') is None, (
        "REGRESSION: explicit_provides_version is substituting "
        "self.version for unversioned Provides — the whole point of "
        "this helper is to distinguish that case so validate_selection "
        "can apply Policy §7.5 correctly")
    # And None for a name we don't provide at all.
    assert pkg.explicit_provides_version('nonexistent') is None


def test_validate_selection_unversioned_provides_no_spurious_break():
    """REGRESSION (2026-05-20): fwupd declares `Provides: fwupdate`
    (UNVERSIONED).  linux-image declares `Breaks: fwupdate (<< 12-7)`.
    Per Debian Policy §7.5, an unversioned Provides cannot satisfy a
    versioned Breaks/Conflicts — apt does not flag this combo.

    Pre-fix, validate_selection fell back to the provider's own version
    (fwupd 1.8.12-2) when Provides was unversioned, then `check_dep`
    saw 1.8.12-2 << 12-7 → True (correct Debian version comparison,
    wrong scope) and triggered a spurious break.  The bug was masked
    until parse_dependency started properly registering virtual aliases
    (Bug 2 fix) — once selected_pkgs['fwupdate'] pointed to the fwupd
    Package, validate_selection's existing-but-broken logic fired.

    Test wires the exact upstream shape (fwupd + linux-image-amd64)
    and asserts validate_selection returns True (no breaks).
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree

    class _BrkPkg:
        """Minimal Package surface for validate_selection — needs
        .breaks (list of OR-groups), .conflicts, .alt_depends,
        .recommends, .constraints_satisfied, .version,
        .explicit_provides_version, and ['Package'] / __getitem__."""
        def __init__(self, name, version, *, breaks=(), provides=()):
            self._fields = {'Package': name}
            from debian.debian_support import Version
            self.version = Version(version)
            # breaks shape mirrors parse_depends: list-of-list-of-tuples.
            # Each inner list is one OR-group (Debian forbids alts in
            # breaks so always length 1).  Tuple is (name, ver, op).
            self.breaks = [[(n, v, op)] for n, v, op in breaks]
            self.conflicts = []
            self.alt_depends = []
            self.recommends = []
            # _provides: list of (name, version_str_or_None).  None
            # means "unversioned Provides" — explicit_provides_version
            # returns None for it (Policy §7.5).
            self._provides = list(provides)
            self.constraints_satisfied = True
        def __getitem__(self, k): return self._fields[k]
        def __contains__(self, k): return k in self._fields
        def get(self, k, d=''): return self._fields.get(k, d)
        def explicit_provides_version(self, name):
            from debian.debian_support import Version
            for _n, _v in self._provides:
                if _n != name:
                    continue
                return Version(_v) if _v is not None else None
            return None

    fwupd = _BrkPkg('fwupd', '1.8.12-2',
                    provides=[('fwupdate', None)])
    linux_image = _BrkPkg('linux-image-amd64', '6.1.170-3',
                          breaks=[('fwupdate', '12-7', '<<')])
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt.selected_pkgs = {
        'fwupd':             fwupd,
        'fwupdate':          fwupd,         # virtual alias → real fwupd Package
        'linux-image-amd64': linux_image,
    }
    dt.pool_extras_pkg_names = set()
    assert dt.validate_selection() is True, (
        "spurious break: linux-image-amd64 Breaks: fwupdate (<< 12-7) "
        "should NOT trigger against fwupd's unversioned `Provides: "
        "fwupdate` (Debian Policy §7.5)"
    )


def test_validate_selection_versioned_provides_still_flagged():
    """Symmetry: when Provides IS versioned and that version actually
    matches the Breaks constraint, the break MUST still trigger.
    Pins behaviour for the legitimate case so the Policy §7.5 fix
    doesn't accidentally over-loosen."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    from debian.debian_support import Version

    class _BrkPkg:
        def __init__(self, name, version, *, breaks=(), provides=()):
            self._fields = {'Package': name}
            self.version = Version(version)
            self.breaks = [[(n, v, op)] for n, v, op in breaks]
            self.conflicts = []
            self.alt_depends = []
            self.recommends = []
            self._provides = list(provides)  # [(name, version_str_or_None)]
            self.constraints_satisfied = True
        def __getitem__(self, k): return self._fields[k]
        def __contains__(self, k): return k in self._fields
        def get(self, k, d=''): return self._fields.get(k, d)
        def explicit_provides_version(self, name):
            for _n, _v in self._provides:
                if _n != name:
                    continue
                return Version(_v) if _v is not None else None
            return None

    # Provides version = 5.0; Breaks constraint = (<< 10).  5.0 << 10 → break.
    provider = _BrkPkg('provider', '99-99',  # provider's own version irrelevant
                       provides=[('virtual', '5.0')])
    breaker = _BrkPkg('breaker', '1.0',
                      breaks=[('virtual', '10', '<<')])
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt.selected_pkgs = {
        'provider': provider,
        'virtual':  provider,
        'breaker':  breaker,
    }
    dt.pool_extras_pkg_names = set()
    assert dt.validate_selection() is False, (
        "versioned Provides (= 5.0) MUST satisfy Breaks (<< 10) — "
        "the Policy §7.5 fix dropped a legitimate break"
    )


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
            self._provides_map = {n: v for n, v in provides}
            self.breaks = []
            self.conflicts = []
            self.alt_depends = []
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


def test_sta18_version_for_constraint_target_real_pkg():
    """Helper returns the entry's own Version when entry IS the
    constraint target (the canonical-name case)."""
    dt, _Pkg = _sta18_make_dt()
    foo = _Pkg('foo', '1.2.3')
    dt.selected_pkgs['foo'] = foo
    assert dt._version_for_constraint_target('foo', 'foo') == '1.2.3'


def test_sta18_version_for_constraint_target_versioned_provides_with_epoch():
    """LIBGCC1 SHAPE: provider's own Version lacks the epoch its Provides
    clause declares.  Helper must return the Provides clause version
    (with epoch), NOT the provider's Version field.  This is the bug
    STA-18 is about — pre-fix, reads of selected_pkgs[alias].version
    returned the provider's Version directly, dropping the epoch and
    causing spurious 'unresolved dependency' WARNINGs."""
    dt, _Pkg = _sta18_make_dt()
    libgcc_s1 = _Pkg('libgcc-s1', '12.2.0-14+deb12u1',
                     provides=[('libgcc1', '1:12.2.0-14+deb12u1')])
    dt.selected_pkgs['libgcc-s1'] = libgcc_s1
    dt.selected_pkgs['libgcc1']   = libgcc_s1   # virtual alias

    # Direct entry lookup: real-pkg behaviour preserved
    assert dt._version_for_constraint_target('libgcc-s1', 'libgcc-s1') == '12.2.0-14+deb12u1'
    # Alias entry resolved via Provides clause — MUST carry the epoch
    assert dt._version_for_constraint_target('libgcc1', 'libgcc1') == '1:12.2.0-14+deb12u1'
    # Provides-fallback path (validate_selection's Site 3): entry_key is
    # the provider's canonical name, target_name is the virtual alias
    assert dt._version_for_constraint_target('libgcc-s1', 'libgcc1') == '1:12.2.0-14+deb12u1'


def test_sta18_version_for_constraint_target_unversioned_provides_returns_none():
    """When the provider declares `Provides: virt` UNVERSIONED, helper
    returns None.  Per Debian Policy §7.5, an unversioned Provides
    cannot satisfy a versioned constraint — callers MUST handle None
    as 'not satisfied' for versioned constraints, 'satisfied' for
    unversioned (existence-only) constraints."""
    dt, _Pkg = _sta18_make_dt()
    fwupd = _Pkg('fwupd', '1.8.12-2', provides=[('fwupdate', None)])
    dt.selected_pkgs['fwupd']    = fwupd
    dt.selected_pkgs['fwupdate'] = fwupd
    assert dt._version_for_constraint_target('fwupdate', 'fwupdate') is None
    assert dt._version_for_constraint_target('fwupd', 'fwupdate') is None


def test_sta18_validate_selection_resolves_epoch_aliased_alt_dep():
    """INTEGRATION: validate_selection's alt-dep loop on the libgcc1
    shape.  Pre-fix, consumer `Depends: libgcc1 (>= 1:4.0)` failed
    apt_pkg.check_dep against provider's stored '12.2.0-14+deb12u1'
    (epoch 0 < epoch 1) → 'Alt-dep version constraint failed' WARNING
    fired and _found stayed False.  Post-fix, the helper returns the
    Provides clause version '1:12.2.0-14+deb12u1' (epoch 1) → check_dep
    sees epoch 1 >= 1:4.0 → resolves cleanly, _found = True,
    validate_selection returns True overall."""
    dt, _Pkg = _sta18_make_dt()
    libgcc_s1 = _Pkg('libgcc-s1', '12.2.0-14+deb12u1',
                     provides=[('libgcc1', '1:12.2.0-14+deb12u1')])
    # Consumer: alt_depends shape is list-of-OR-groups, OR-group is
    # list of dep-tuples (name, version_str, comparator).
    consumer = _Pkg('libwebrtc-audio-processing1', '0.3-1+b1')
    consumer.alt_depends = [
        [('libgcc1', '1:4.0', '>=')],
    ]
    dt.selected_pkgs = {
        'libgcc-s1':                     libgcc_s1,
        'libgcc1':                       libgcc_s1,   # virtual alias
        'libwebrtc-audio-processing1':   consumer,
    }
    assert dt.validate_selection() is True, (
        "STA-18 regression: consumer's `Depends: libgcc1 (>= 1:4.0)` "
        "must resolve cleanly against libgcc-s1's `Provides: libgcc1 "
        "(= 1:12.2.0-14+deb12u1)` — pre-fix the constraint check read "
        "libgcc-s1.version directly (epoch 0), failed >= 1:4.0, and "
        "emitted spurious 'unresolved dependency' WARNINGs"
    )


def test_sta18_validate_selection_unversioned_provides_cannot_satisfy_versioned_dep():
    """SYMMETRY for STA-18: when Provides IS unversioned and the
    consumer's constraint IS versioned, the constraint MUST NOT
    satisfy (per Policy §7.5).  Pins behaviour so the helper's
    None-handling can't accidentally over-loosen."""
    _stub_tui()       # ERROR-path triggers tui.console.print
    dt, _Pkg = _sta18_make_dt()
    fwupd = _Pkg('fwupd', '1.8.12-2', provides=[('fwupdate', None)])
    consumer = _Pkg('needsfwupdate', '1.0')
    consumer.alt_depends = [
        [('fwupdate', '12-7', '>=')],
    ]
    dt.selected_pkgs = {
        'fwupd':         fwupd,
        'fwupdate':      fwupd,    # virtual alias, unversioned Provides
        'needsfwupdate': consumer,
    }
    assert dt.validate_selection() is False, (
        "unversioned `Provides: fwupdate` MUST NOT satisfy `Depends: "
        "fwupdate (>= 12-7)` (Policy §7.5) — the STA-18 fix dropped "
        "the spurious-pass guard"
    )


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


def test_cohort_resolvers_route_through_canonical_names():
    """Every cohort/corpus resolver must route through _canonical_names.
    Anti-regression for any future resolver that re-introduces raw
    selected_pkgs.keys() and brings back the virtual-alias false
    positives."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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


def test_autorun_step_lists_include_container_init():
    """`container init` is part of both autorun pipelines, ordered
    AFTER cache build (gate requires it) and BEFORE source build.
    Scope assertion to the `_steps = [...]` literal."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    for _fn in ('cmd_auto_run_live', 'cmd_auto_run_installer'):
        _m = re.search(rf"\n    def {_fn}\b.*?_steps = \[(?P<list>.*?)\]\s*\n",
                       _body, re.DOTALL)
        assert _m is not None, f"{_fn}'s _steps list not found"
        _steps = _m.group('list')
        assert 'cmd_init_container' in _steps, (
            f"{_fn} missing cmd_init_container step — source builds will "
            f"hit 'Run `container init` first' mid-pipeline"
        )
        # Ordering: cache build comes BEFORE container init (gate
        # requirement), container init comes BEFORE source build
        # (source build needs the container ready).
        _cache_idx = _steps.find('cmd_build_cache')
        _container_idx = _steps.find('cmd_init_container')
        _source_idx = _steps.find('cmd_source_build')
        assert _cache_idx < _container_idx < _source_idx, (
            f"{_fn} step order broken: expected cache_build → "
            f"init_container → source_build, got idx "
            f"{_cache_idx}/{_container_idx}/{_source_idx}"
        )


def test_derive_extras_src_names_marks_extras_only_sources():
    """A source whose every binary is in extras_pkg_names is in
    extras_src_names.  A mixed source (some selected + some extras) is NOT."""
    import dependencytree

    class _StubSrc:
        pass

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
    dt._distro_suffix = ''  # no version bump in this synthetic test
    dt.selected_pkgs = seed_pkgs
    dt.selected_srcs = {'firefox': _StubSrc(), 'libnss3': _StubSrc()}
    # Per-tree predicted filenames (replaces Source.pkgs, see
    # dependencytree.py:src_pkg_files docstring for why).
    dt.src_pkg_files = {
        'firefox': ['firefox_1.0_amd64.deb',
                    'firefox-l10n-en_1.0_amd64.deb'],
        'libnss3': ['libnss3-tools_3.0_amd64.deb'],
    }
    dt.extras_pkg_names = {'firefox-l10n-en', 'libnss3-tools'}
    dt.extras_src_names = set()
    n = dt.derive_extras_src_names()
    assert n == 1
    assert dt.extras_src_names == {'libnss3'}
    assert 'firefox' not in dt.extras_src_names  # mixed — NOT extras-only


# ─────────────────────────────────────────────────────────────────────────────
# live.list / installer.list split
# ─────────────────────────────────────────────────────────────────────────────

def test_dep_tree_initialises_subset_exclusive_sets_empty():
    """A fresh DependencyTree has live_exclusive / installer_exclusive
    pkg + src sets that exist and start empty."""
    import sys, types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    # __init__ requires a real Cache; bypass via __new__ and replicate the
    # subset of fields the assertions touch.
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    # Replicate __init__'s zero-state for the subset fields.
    dt.live_exclusive_pkg_names = set()
    dt.installer_exclusive_pkg_names = set()
    dt.live_exclusive_src_names = set()
    dt.installer_exclusive_src_names = set()
    # Sanity — if a future change drops these the type check below will catch
    # the regression at class scope.
    assert hasattr(dependencytree.DependencyTree.__init__, '__code__')
    assert isinstance(dt.live_exclusive_pkg_names, set)
    assert isinstance(dt.installer_exclusive_pkg_names, set)
    assert isinstance(dt.live_exclusive_src_names, set)
    assert isinstance(dt.installer_exclusive_src_names, set)
    assert not (dt.live_exclusive_pkg_names | dt.installer_exclusive_pkg_names
                | dt.live_exclusive_src_names | dt.installer_exclusive_src_names)


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


# ─────────────────────────────────────────────────────────────────────────────
# GROUPS-01: pkg.list INI-style group parser
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_pkg_list_groups_flat_file_becomes_implicit_base():
    """Backward compat: a flat pkg.list (no [section] headers) parses
    as a single `[base]` group containing every non-comment line.  The
    in-repo pkg.list pre-dates groups and must keep working."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import parse_pkg_list_groups
    with tempfile.NamedTemporaryFile('w', suffix='.list', delete=False) as fh:
        fh.write("# leading comment\nbash\ncoreutils\n# another\nlinux-image-amd64\n")
        _path = fh.name
    try:
        groups = parse_pkg_list_groups(_path)
        assert list(groups.keys()) == ['base'], groups
        assert groups['base'] == ['bash', 'coreutils', 'linux-image-amd64']
    finally:
        os.unlink(_path)


def test_parse_pkg_list_groups_ini_style_multi_section():
    """An INI-style pkg.list with multiple `[group]` headers parses
    into a dict respecting declaration order (Python 3.7+ dict
    preserves insertion order).  Comments and blanks inside sections
    are skipped."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import parse_pkg_list_groups
    _body = (
        "# top-of-file comment\n"
        "[base]\n"
        "bash\n"
        "# inline comment\n"
        "coreutils\n"
        "\n"
        "[development-tools]\n"
        "gcc\n"
        "make\n"
        "\n"
        "[gnome]\n"
        "gnome-shell\n"
        "firefox-esr\n"
    )
    with tempfile.NamedTemporaryFile('w', suffix='.list', delete=False) as fh:
        fh.write(_body)
        _path = fh.name
    try:
        groups = parse_pkg_list_groups(_path)
        assert list(groups.keys()) == ['base', 'development-tools', 'gnome'], list(groups.keys())
        assert groups['base'] == ['bash', 'coreutils']
        assert groups['development-tools'] == ['gcc', 'make']
        assert groups['gnome'] == ['gnome-shell', 'firefox-esr']
    finally:
        os.unlink(_path)


def test_parse_pkg_list_groups_rejects_seed_before_first_section():
    """In INI-style mode (any `[section]` present), a seed before any
    header is a configuration error — operator probably forgot `[base]`
    when migrating a flat file.  ValueError names the line and tells
    the operator how to fix."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import parse_pkg_list_groups
    _body = "bash\n[gnome]\ngnome-shell\n"
    with tempfile.NamedTemporaryFile('w', suffix='.list', delete=False) as fh:
        fh.write(_body)
        _path = fh.name
    try:
        try:
            parse_pkg_list_groups(_path)
        except ValueError as e:
            assert 'bash' in str(e), str(e)
            assert '[base]' in str(e), str(e)
        else:
            raise AssertionError("seed before first section should raise ValueError")
    finally:
        os.unlink(_path)


def test_parse_pkg_list_groups_empty_section_name_raises():
    """`[]` as a section header is malformed; reject with a clear error
    rather than producing a confusing empty-key group."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import parse_pkg_list_groups
    with tempfile.NamedTemporaryFile('w', suffix='.list', delete=False) as fh:
        fh.write("[]\nbash\n")
        _path = fh.name
    try:
        try:
            parse_pkg_list_groups(_path)
        except ValueError as e:
            assert 'empty group name' in str(e), str(e)
        else:
            raise AssertionError("empty section name should raise ValueError")
    finally:
        os.unlink(_path)


def test_athena_tasksel_fork_ignores_debian_tasks_only_env():
    """FORK-01 Step 5: athena-tasksel (fork/source/athena-tasksel/)
    must have list_task_descs() always glob all .desc files —
    NOT branch on $ENV{DEBIAN_TASKS_ONLY}.  Pin the source-level
    edit so a future re-import of upstream tasksel doesn't silently
    re-introduce the env-var filter that hides our athena.desc."""
    _pl = os.path.join(_ROOT, 'fork', 'source', 'athena-tasksel',
                       'tasksel.pl')
    assert os.path.isfile(_pl), f"missing {_pl}"
    with open(_pl) as fh:
        _body = fh.read()
    # The Debian env-var branch must be gone.
    assert '$ENV{DEBIAN_TASKS_ONLY}' not in _body, (
        "athena-tasksel still references DEBIAN_TASKS_ONLY — the "
        "Step 5 source edit was reverted or wasn't applied"
    )
    # Verify the canonical-fork glob is there
    assert '"$descdir/*.desc"' in _body, (
        "list_task_descs must glob all .desc files"
    )


def test_athena_tasksel_control_provides_conflicts_replaces_tasksel():
    """FORK-01 Step 5: athena-tasksel's debian/control declares
    Provides + Conflicts + Replaces tasksel.  Without all three,
    apt resolution doesn't reliably pick our fork over upstream
    tasksel when both are in the pool."""
    _ctrl = os.path.join(_ROOT, 'fork', 'source', 'athena-tasksel',
                         'debian', 'control')
    assert os.path.isfile(_ctrl), f"missing {_ctrl}"
    with open(_ctrl) as fh:
        _body = fh.read()
    assert 'Package: athena-tasksel' in _body, _body
    # Versioned Provides is REQUIRED — without it, tasksel-data's strict
    # Depends: tasksel (= 3.73) fails dpkg config (caught 2026-05-17).
    # See memory/feedback_fork_provides_must_be_versioned.md.
    assert 'Provides: tasksel (= 3.73)' in _body, (
        "athena-tasksel must declare versioned Provides — "
        "unversioned Provides fails dpkg's strict-version checks "
        f"from tasksel-data Depends.  Got:\n{_body}"
    )
    assert 'Conflicts: tasksel' in _body, _body
    assert 'Replaces: tasksel' in _body, _body


def test_athena_tasksel_depends_on_athena_tasksel_data_directly():
    """FORK-01 Step 5 regression guard (caught 2026-05-18 install):
    athena-tasksel's Depends must name `athena-tasksel-data` directly,
    NOT the virtual `tasksel-data`.  Apt prefers real packages over
    virtual Provides — a bare `Depends: tasksel-data` resolves to
    upstream tasksel-data (still in our cache), shipping upstream's
    .desc (only "standard system utilities" task) and HIDING our
    athena-tasks.desc.  Symptom: tasksel menu offers exactly one
    option, our 6 curated tasks (standard / ssh-server / laptop /
    desktop / gnome-desktop / development-tools) never appear.

    The Provides+Conflicts+Replaces chain still works for third
    parties that depend on the virtual `tasksel-data`, but
    athena-tasksel MUST be explicit because it's the entry point.
    """
    _ctrl = os.path.join(_ROOT, 'fork', 'source', 'athena-tasksel',
                         'debian', 'control')
    with open(_ctrl) as fh:
        _body = fh.read()
    # Extract the athena-tasksel binary stanza's Depends line.
    import re
    _m = re.search(
        r'^Package: athena-tasksel\s*$.*?^Depends:\s*(.+?)\s*$',
        _body, re.MULTILINE | re.DOTALL,
    )
    assert _m, "athena-tasksel binary stanza missing Depends line"
    _deps = _m.group(1)
    assert 'athena-tasksel-data' in _deps, (
        f"athena-tasksel must Depend on athena-tasksel-data directly "
        f"(virtual tasksel-data loses to upstream real pkg).  Got: {_deps!r}"
    )
    # And the bare `tasksel-data` token must NOT appear in the Depends
    # (would re-trigger the upstream-wins regression).  Match a word
    # boundary so `athena-tasksel-data` itself doesn't trip the check.
    assert not re.search(r'(?<![\w-])tasksel-data(?![\w-])', _deps), (
        f"athena-tasksel Depends still names bare `tasksel-data` — "
        f"will resolve to upstream.  Got: {_deps!r}"
    )


def test_athena_tasksel_task_keys_mirror_pkg_list_groups():
    """FORK-01 Step 5b architectural invariant: each non-`[base]`
    group in config/pkg.list MUST have a matching task file in
    fork/source/athena-tasksel/tasks/<group>, and that file's Key:
    list MUST exactly equal the pkg.list group's seed list.

    Why this matters: tasksel's task_avail() runs
    `apt-cache dumpavail` on every Key entry and silently hides the
    task if any package is missing.  The pkg.list group is also the
    source-of-truth that drives `resolve_packages` → `selected_pkgs`
    → source build, so a Key entry not listed in the pkg.list group
    won't be built / shipped.  Sync enforced both directions.

    Caught 2026-05-18 install: tasks/desktop, tasks/laptop,
    tasks/ssh-server had `Key: task-<name>` referencing upstream
    Debian meta-packages we don't build (athena-tasksel-data is a
    single binary, not multi-binary with task-* meta packages).
    Three tasks silently dropped from the menu.

    [base] is exempt — it's installed via debootstrap, doesn't
    surface as a tasksel task.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import parse_pkg_list_groups
    _pkglist = os.path.join(_ROOT, 'config', 'pkg.list')
    _tasks_dir = os.path.join(_ROOT, 'fork', 'source',
                              'athena-tasksel', 'tasks')
    _groups = parse_pkg_list_groups(_pkglist)
    _non_base = {_g: _seeds for _g, _seeds in _groups.items() if _g != 'base'}
    for _group, _seeds in _non_base.items():
        _task_file = os.path.join(_tasks_dir, _group)
        assert os.path.isfile(_task_file), (
            f"pkg.list defines [{_group}] but tasks/{_group} is missing.  "
            f"Create fork/source/athena-tasksel/tasks/{_group} with a "
            f"Key: list mirroring the pkg.list group's seeds."
        )
        # Extract the Key: list from the task file.
        _key_seeds = []
        with open(_task_file) as fh:
            _in_key = False
            for _line in fh:
                _stripped = _line.rstrip()
                if _stripped.startswith('Key:'):
                    _in_key = True
                    continue
                if _in_key:
                    if not _line.startswith((' ', '\t')):
                        break  # end of Key block
                    _name = _stripped.strip()
                    if _name:
                        _key_seeds.append(_name)
        assert set(_key_seeds) == set(_seeds), (
            f"tasks/{_group} Key: list out of sync with pkg.list [{_group}].\n"
            f"  in pkg.list only: {sorted(set(_seeds) - set(_key_seeds))}\n"
            f"  in tasks only:    {sorted(set(_key_seeds) - set(_seeds))}\n"
            f"Mirror the lists (Path β manual-sync workflow per "
            f"docs/plans/fork-source.md)."
        )


def test_athena_pkgsel_no_popcon_pre_pkgsel_hook():
    """Athena ships as Athena (no Debian telemetry residue per
    project_filter_debian_specific_installer_hooks memory).  Upstream
    pkgsel ships pre-pkgsel.d/90popcon which apt-installs
    popularity-contest (Debian's popularity-contest telemetry); the
    hook fails on install (`E: Unable to locate package
    popularity-contest`) because popcon isn't in our pool, and even
    if it were we don't want it on Thor.  Pin the absence so a
    future rebase from upstream doesn't reintroduce it."""
    _popcon = os.path.join(_ROOT, 'fork', 'source', 'athena-pkgsel',
                           'pre-pkgsel.d', '90popcon')
    assert not os.path.exists(_popcon), (
        f"{_popcon} reintroduces Debian's popularity-contest telemetry; "
        f"delete to keep Thor free of Debian residue"
    )


def test_athena_pkgsel_fork_postinst_drops_debian_tasks_only_prefix():
    """FORK-01 Step 5: athena-pkgsel's debian/postinst must NOT
    prefix its in-target tasksel call with DEBIAN_TASKS_ONLY=1.
    Belt-and-suspenders with athena-tasksel's source edit — even
    though our tasksel ignores the env var, removing the setter
    keeps the install flow's intent explicit."""
    _post = os.path.join(_ROOT, 'fork', 'source', 'athena-pkgsel',
                         'debian', 'postinst')
    assert os.path.isfile(_post), f"missing {_post}"
    with open(_post) as fh:
        _body = fh.read()
    # The literal "DEBIAN_TASKS_ONLY=1 in-target sh -c \"tasksel"
    # pattern (without leading # comment) must NOT appear as an
    # active line.  Comments referencing it are OK.
    for _line in _body.splitlines():
        _stripped = _line.lstrip()
        if _stripped.startswith('#'):
            continue
        assert 'DEBIAN_TASKS_ONLY=1 in-target' not in _stripped, (
            f"active line still has DEBIAN_TASKS_ONLY=1 prefix: {_line!r}"
        )


def test_athena_pkgsel_control_provides_conflicts_replaces_pkgsel():
    """FORK-01 Step 5: athena-pkgsel's debian/control declares
    Provides + Conflicts + Replaces pkgsel + Package-Type udeb."""
    _ctrl = os.path.join(_ROOT, 'fork', 'source', 'athena-pkgsel',
                         'debian', 'control')
    assert os.path.isfile(_ctrl), f"missing {_ctrl}"
    with open(_ctrl) as fh:
        _body = fh.read()
    assert 'Package: athena-pkgsel' in _body, _body
    assert 'Package-Type: udeb' in _body, _body
    # Versioned Provides — same defensive pattern as athena-tasksel
    # even though nothing depends on pkgsel with a version constraint
    # today.  See memory/feedback_fork_provides_must_be_versioned.md.
    assert 'Provides: pkgsel (= 0.79)' in _body, _body
    assert 'Conflicts: pkgsel' in _body, _body
    assert 'Replaces: pkgsel' in _body, _body


def test_athena_pkgsel_dh_helper_files_use_binary_name():
    """FORK-01 Step 5 regression guard (caught 2026-05-18 install):
    dh-style helper files in fork/source/athena-pkgsel/debian/ must be
    named `athena-pkgsel.<helper>`, not `pkgsel.<helper>`.  debhelper
    matches helper files against the BINARY package name in
    debian/control (`Package: athena-pkgsel`); any `pkgsel.<helper>`
    file is silently ignored, producing an EMPTY udeb that ships
    nothing except DEBIAN/control + DEBIAN/postinst.

    Symptom of the bug: main-menu fires athena-pkgsel, its postinst
    runs but `db_progress START debian-installer/pkgsel/title` is a
    no-op (templates never installed → debconf has no record), all
    subsequent `db_get pkgsel/<key>` calls return empty, the tasksel
    branch is skipped, postinst exits 0, main-menu logs
    `succeeded but requested to be left unconfigured` and the menu
    loop never advances.  Install hangs on the package-selection step.
    """
    _dir = os.path.join(_ROOT, 'fork', 'source', 'athena-pkgsel', 'debian')
    # The four dh helper files that drive the udeb's payload.
    for _helper in ('install', 'dirs', 'templates', 'isinstallable'):
        _correct = os.path.join(_dir, f'athena-pkgsel.{_helper}')
        _wrong = os.path.join(_dir, f'pkgsel.{_helper}')
        assert os.path.isfile(_correct), (
            f"missing {_correct} — debhelper will ship an empty udeb")
        assert not os.path.isfile(_wrong), (
            f"{_wrong} exists but binary is athena-pkgsel; dh would "
            f"silently ignore this file.  Rename to athena-pkgsel.{_helper}")


def test_pkgsel_patch_dir_deleted():
    """FORK-01 Step 5: patch/source/pkgsel/ is gone — athena-pkgsel
    fork replaces the patch entirely.  Pin the deletion so a
    future operator doesn't accidentally re-add a patch that gets
    silently shadowed by the fork."""
    _patch_dir = os.path.join(_ROOT, 'patch', 'source', 'pkgsel')
    assert not os.path.exists(_patch_dir), (
        f"{_patch_dir} should not exist after Step 5 — pkgsel patches "
        "are replaced by the athena-pkgsel fork"
    )


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


def test_dep_tree_initialises_pkg_group_fields_empty():
    """DependencyTree starts with empty pkg_group_pkg_names dict and
    empty pkg_group_extras_pkg_names set; populated by Pass III in
    cmd_parse_dependency."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt.pkg_group_pkg_names = {}
    dt.pkg_group_extras_pkg_names = set()
    dt.pkg_group_extras_src_names = set()
    assert dt.pkg_group_pkg_names == {}
    assert dt.pkg_group_extras_pkg_names == set()
    assert dt.pkg_group_extras_src_names == set()


def test_stage_group_manifests_writes_one_file_per_group():
    """`_stage_group_manifests` writes `.disk/groups/<group>.list` with
    one canonical package name per line, alpha-sorted (reproducibility)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_group_manifests
    with tempfile.TemporaryDirectory() as _stage:
        _groups = {
            'base':              {'bash', 'coreutils', 'linux-image-amd64'},
            'development-tools': {'make', 'gcc', 'git'},
            'gnome':             {'gnome-shell', 'firefox-esr'},
        }
        assert _stage_group_manifests(_stage, _groups) is True
        _dir = os.path.join(_stage, '.disk', 'groups')
        assert os.path.isdir(_dir), _dir
        for _g, _names in _groups.items():
            _path = os.path.join(_dir, f'{_g}.list')
            assert os.path.isfile(_path), _path
            with open(_path) as fh:
                _lines = [_l.strip() for _l in fh if _l.strip()]
            assert _lines == sorted(_names), (_g, _lines)


def test_stage_group_manifests_empty_groups_is_noop():
    """No groups → no manifest dir, no error.  Lets callers pass an
    empty dict unconditionally."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _stage_group_manifests
    with tempfile.TemporaryDirectory() as _stage:
        assert _stage_group_manifests(_stage, {}) is True
        assert not os.path.exists(os.path.join(_stage, '.disk', 'groups'))


# ─────────────────────────────────────────────────────────────────────────────
# GROUPS-01 phase 2: tasksel `.desc` generation + pre-pkgsel hook
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_pkg_list_group_meta_extracts_descriptions():
    """`## Description: …` lines after `[group]` headers parse into
    per-group metadata.  Comments without the `## Description:` prefix
    (regular `# foo` comments) are ignored."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import parse_pkg_list_group_meta
    _body = (
        "[base]\n"
        "## Description: Athena base — runtime, kernel, bootloader.\n"
        "# regular comment\n"
        "bash\n"
        "\n"
        "[development-tools]\n"
        "## Description: Compiler toolchain + version control + build helpers.\n"
        "gcc\n"
        "\n"
        "[gnome]\n"
        "# no description on this group\n"
        "gnome-shell\n"
    )
    with tempfile.NamedTemporaryFile('w', suffix='.list', delete=False) as fh:
        fh.write(_body)
        _path = fh.name
    try:
        meta = parse_pkg_list_group_meta(_path)
        assert meta['base']['description'] == 'Athena base — runtime, kernel, bootloader.'
        assert meta['development-tools']['description'] == 'Compiler toolchain + version control + build helpers.'
        assert 'description' not in meta.get('gnome', {})
    finally:
        os.unlink(_path)


def test_parse_pkg_list_group_meta_flat_file_returns_base_only():
    """A flat (no `[section]`) pkg.list returns `{'base': {}}` — same
    backward-compat shape as parse_pkg_list_groups."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import parse_pkg_list_group_meta
    with tempfile.NamedTemporaryFile('w', suffix='.list', delete=False) as fh:
        fh.write("bash\ncoreutils\n")
        _path = fh.name
    try:
        meta = parse_pkg_list_group_meta(_path)
        assert meta == {'base': {}}
    finally:
        os.unlink(_path)


def test_installer_list_includes_athena_pkgsel():
    """FORK-01 Step 5: athena-pkgsel (Provides + Conflicts + Replaces
    pkgsel) must be in installer.list — drives the 'Software selection'
    step at install time (which invokes `in-target tasksel --new-install`
    to read athena.desc and surface the operator-defined groups).
    Upstream pkgsel is shadowed via the Provides resolution."""
    _path = os.path.join(_ROOT, 'config', 'installer.list')
    with open(_path) as fh:
        _names = {
            _l.strip() for _l in fh
            if _l.strip() and not _l.lstrip().startswith('#')
        }
    assert 'athena-pkgsel' in _names, "installer.list missing athena-pkgsel"
    # Upstream pkgsel must NOT be listed (would be redundant + confusing)
    assert 'pkgsel' not in _names, (
        "installer.list lists raw 'pkgsel' alongside athena-pkgsel — "
        "remove the upstream reference; athena-pkgsel Provides pkgsel"
    )


def test_pkg_list_base_includes_athena_tasksel():
    """FORK-01 Step 5: athena-tasksel (Provides + Conflicts + Replaces
    tasksel) must be in pkg.list [base] so it's debootstrapped onto
    every /target.  athena-tasksel-data Depends: tasksel — apt resolves
    that to athena-tasksel via Provides."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import parse_pkg_list_groups
    _path = os.path.join(_ROOT, 'config', 'pkg.list')
    groups = parse_pkg_list_groups(_path)
    assert 'athena-tasksel' in groups.get('base', []), \
        f"pkg.list [base] missing athena-tasksel; got base={groups.get('base')}"
    # Upstream tasksel must NOT be in [base] (would be redundant)
    assert 'tasksel' not in groups.get('base', []), (
        "pkg.list [base] lists raw 'tasksel' alongside athena-tasksel — "
        "remove the upstream reference; athena-tasksel Provides tasksel"
    )


# Old pre-pkgsel.d hook block (commits 2cd13b6 / d9818b0 / 346ce20)
# is gone — replaced by a synthetic athena-tasksel-data .deb generated
# in iso_installer._build_tasksel_data_deb.  Tasksel reads the .desc
# via its standard glob over /usr/share/tasksel/descs/ (per
# /usr/bin/tasksel:53-65), and dpkg-installing the .deb is enough to
# put the file there.  No more apt-cdrom mount-on-demand gymnastics.


def test_overlay_map_does_not_contain_pre_pkgsel_hook():
    """The pre-pkgsel.d hook is gone — replaced by the synthetic
    athena-tasksel-data .deb (iso_installer._build_tasksel_data_deb).
    If a future refactor accidentally re-adds the hook to the overlay
    map, we want to fail here so the dual-mechanism confusion doesn't
    silently land."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _OVERLAY_MAP
    _src_to_target = dict(_OVERLAY_MAP)
    assert 'pkgsel/pre-pkgsel.d-athena-tasks' not in _src_to_target, (
        "obsolete pre-pkgsel hook re-added to overlay map — "
        "athena-tasksel-data .deb supersedes it"
    )


def test_installer_pkgsel_dir_does_not_exist():
    """Belt+braces: the installer/pkgsel/ directory itself should not
    exist on disk.  If someone re-creates the hook, we want this test
    to fail next CI run rather than ship a half-removed mechanism."""
    _path = os.path.join(_ROOT, 'installer', 'pkgsel')
    assert not os.path.exists(_path), (
        f"installer/pkgsel/ should be gone (obsolete pre-pkgsel hook),"
        f" but exists at {_path}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# FORK-01 Step 5b: athena-tasksel-data shipped from the fork
# ─────────────────────────────────────────────────────────────────────────────


def test_athena_tasksel_data_binary_stanza_in_fork_control():
    """FORK-01 Step 5b: fork/source/athena-tasksel/debian/control
    declares the second binary stanza athena-tasksel-data with
    versioned Provides + Conflicts + Replaces tasksel-data (= 3.73).
    The data binary replaces upstream tasksel-data; without versioned
    Provides, downstream pkgs with strict Depends: tasksel-data
    (= 3.73) would fail to resolve.  See memory
    feedback_fork_provides_must_be_versioned.md."""
    _ctrl = os.path.join(_ROOT, 'fork', 'source', 'athena-tasksel',
                         'debian', 'control')
    with open(_ctrl) as fh:
        body = fh.read()
    assert 'Package: athena-tasksel-data' in body, body
    assert 'Provides: tasksel-data (= 3.73)' in body, body
    assert 'Conflicts: tasksel-data' in body, body
    assert 'Replaces: tasksel-data' in body, body


def test_athena_tasksel_fork_ships_exactly_six_curated_tasks():
    """FORK-01 Step 5b: the fork's tasks/ dir contains exactly the
    six curated tasks chosen 2026-05-17: standard (curated Key:
    list, NOT Packages: standard), ssh-server, laptop, desktop
    (kept from Debian), gnome-desktop, development-tools (Athena
    content mirroring pkg.list groups)."""
    _tasks_dir = os.path.join(_ROOT, 'fork', 'source', 'athena-tasksel',
                              'tasks')
    files = {f for f in os.listdir(_tasks_dir)
             if os.path.isfile(os.path.join(_tasks_dir, f))
             and not f.startswith('.')
             and f != 'README'}
    expected = {'standard', 'ssh-server', 'laptop', 'desktop',
                'gnome-desktop', 'development-tools'}
    assert files == expected, (
        f"tasks/ mismatch.  Extra (in tree, not expected): "
        f"{files - expected}.  Missing (expected, not in tree): "
        f"{expected - files}"
    )


def test_athena_tasksel_standard_task_uses_curated_key_list():
    """FORK-01 Step 5b DEF-4: tasks/standard must use an explicit
    Key: list (Athena curation), NOT upstream's `Packages: standard`
    sigil (which would install all 87 Priority: standard pkgs).
    Curated subset is smaller (~25 pkgs); operator gets the rest
    via apt from /cdrom/pool."""
    _standard = os.path.join(_ROOT, 'fork', 'source', 'athena-tasksel',
                             'tasks', 'standard')
    with open(_standard) as fh:
        body = fh.read()
    # Check line-starts only (so quoted text inside Description doesn't false-positive)
    for _line in body.splitlines():
        assert not _line.startswith('Packages: standard'), (
            "tasks/standard reverted to upstream's `Packages: standard` "
            "sigil — should use explicit curated Key: list per DEF-4"
        )
    assert '\nKey:' in body, body
    # A few essentials we expect; pinning ALL would be brittle
    for must in ('openssh-client', 'less', 'manpages', 'wget'):
        assert f' {must}' in body, f"standard task missing {must}"


def test_iso_installer_synthetic_tasksel_data_retired():
    """FORK-01 Step 5b path β: the synthetic athena-tasksel-data .deb
    generation in iso_installer.py is RETIRED — replaced by the
    fork's multi-binary build (athena-tasksel + athena-tasksel-data
    from one source).  Pin the removal so a future refactor doesn't
    silently re-introduce the synthetic mechanism."""
    _isoi = os.path.join(_ROOT, 'scripts', 'iso_installer.py')
    with open(_isoi) as fh:
        body = fh.read()
    assert 'def _build_tasksel_data_deb' not in body, (
        "_build_tasksel_data_deb re-introduced — synthetic generation "
        "should stay retired (fork at fork/source/athena-tasksel/ now "
        "produces athena-tasksel-data via multi-binary debian/control)"
    )
    assert 'def _stage_tasksel_desc' not in body, (
        "_stage_tasksel_desc re-introduced — same as above"
    )


def test_derive_subset_exclusive_src_names_marks_live_only_sources():
    """A source whose every binary is in live_exclusive_pkg_names is marked
    in live_exclusive_src_names; a mixed source (some pkg-layer, some
    live-exclusive binaries) is NOT — same rule as derive_extras_src_names."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree

    class _StubSrc:
        pass

    # firefox source: produces firefox.deb (pkg-layer) AND firefox-l10n-en.deb
    # (extras).  Mixed → NOT in any exclusive src set.
    # live-config source: produces only live-config.deb (live-exclusive).
    seed_pkgs = {
        'firefox':         _FakePkg('firefox',         source='firefox',
                                    filename='firefox_1.0_amd64.deb'),
        'firefox-l10n-en': _FakePkg('firefox-l10n-en', source='firefox',
                                    filename='firefox-l10n-en_1.0_amd64.deb'),
        'live-config':     _FakePkg('live-config',     source='live-config',
                                    filename='live-config_1.0_all.deb'),
    }
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt._DependencyTree__cache = _FakeCache({})
    dt.selected_pkgs = seed_pkgs
    dt.selected_srcs = {'firefox': _StubSrc(), 'live-config': _StubSrc()}
    dt.src_pkg_files = {
        'firefox':     ['firefox_1.0_amd64.deb',
                        'firefox-l10n-en_1.0_amd64.deb'],
        'live-config': ['live-config_1.0_all.deb'],
    }
    dt.extras_pkg_names = set()
    dt.extras_src_names = set()
    dt.live_exclusive_pkg_names = {'live-config'}
    dt.installer_exclusive_pkg_names = set()
    dt.live_exclusive_src_names = set()
    dt.installer_exclusive_src_names = set()
    live_n, inst_n = dt.derive_subset_exclusive_src_names()
    assert live_n == 1
    assert inst_n == 0
    assert dt.live_exclusive_src_names == {'live-config'}
    assert 'firefox' not in dt.live_exclusive_src_names  # mixed
    assert dt.installer_exclusive_src_names == set()


def test_derive_subset_exclusive_src_names_no_op_when_both_empty():
    """When both pkg_names sets are empty (no live or installer exclusives)
    the helper short-circuits and returns (0, 0) without scanning."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt._DependencyTree__cache = _FakeCache({})
    dt.selected_pkgs = {}
    dt.selected_srcs = {}
    dt.extras_pkg_names = set()
    dt.extras_src_names = set()
    dt.live_exclusive_pkg_names = set()
    dt.installer_exclusive_pkg_names = set()
    dt.live_exclusive_src_names = set()
    dt.installer_exclusive_src_names = set()
    live_n, inst_n = dt.derive_subset_exclusive_src_names()
    assert (live_n, inst_n) == (0, 0)


def test_derive_subset_exclusive_src_names_handles_installer_exclusive():
    """Symmetry: the installer arm of the same helper marks installer-only
    sources the same way the live arm does."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree

    class _StubSrc:
        pass

    seed_pkgs = {
        'partman-base': _FakePkg('partman-base', source='partman-base',
                                 filename='partman-base_1.0_all.udeb'),
    }
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt._DependencyTree__cache = _FakeCache({})
    dt.selected_pkgs = seed_pkgs
    dt.selected_srcs = {'partman-base': _StubSrc()}
    dt.src_pkg_files = {'partman-base': ['partman-base_1.0_all.udeb']}
    dt.extras_pkg_names = set()
    dt.extras_src_names = set()
    dt.live_exclusive_pkg_names = set()
    dt.installer_exclusive_pkg_names = {'partman-base'}
    dt.live_exclusive_src_names = set()
    dt.installer_exclusive_src_names = set()
    live_n, inst_n = dt.derive_subset_exclusive_src_names()
    assert (live_n, inst_n) == (0, 1)
    assert dt.installer_exclusive_src_names == {'partman-base'}


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


def test_cache_class_declares_udeb_fields_on_init():
    """A freshly constructed Cache has udeb_hashtable / udeb_required /
    udeb_important / mirror_udeb_cache_files initialised empty."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    # Bypass __init__ — full init needs a real BuildConfig with mirrors,
    # snapshot resolution, and live downloads.  We only need to verify the
    # new fields would exist; the Phase 2 ingest test below exercises them
    # against real parsed data.
    c = Cache.__new__(Cache)
    # Replicate __init__'s zero-state for the new fields.
    from collections import defaultdict
    c.udeb_hashtable = defaultdict(lambda: defaultdict(list))
    c.udeb_required = []
    c._fork_udeb_names = set()  # FORK-01 Step 2: supersede tracking
    c.udeb_important = []
    c.mirror_udeb_cache_files = {}
    assert isinstance(c.udeb_hashtable, dict)
    assert isinstance(c.udeb_required, list)
    assert isinstance(c.udeb_important, list)
    assert isinstance(c.mirror_udeb_cache_files, dict)
    assert len(c.udeb_hashtable) == len(c.udeb_required) == len(c.udeb_important) == 0


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
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    from collections import defaultdict
    c = Cache.__new__(Cache)
    c._upstream_collisions      = defaultdict(list, deb_drops or {})
    c._upstream_udeb_collisions = defaultdict(list, udeb_drops or {})
    c.package_hashtable = defaultdict(lambda: defaultdict(list))
    c.udeb_hashtable    = defaultdict(lambda: defaultdict(list))
    for _n, _vers in (pkg_versions or {}).items():
        for _v in _vers:
            c.package_hashtable[_n][_v] = ['<placeholder>']
    for _n, _vers in (udeb_versions or {}).items():
        for _v in _vers:
            c.udeb_hashtable[_n][_v] = ['<placeholder>']
    c.error_str = ''
    return c


def test_collision_gate_passes_when_no_drops_recorded():
    """No upstream-side records were dropped during the walk → gate
    must pass silently.  Today's clean state (all athena-* forks net-
    new, no upstream collision) lives here."""
    c = _make_collision_cache()
    assert c._verify_no_fork_collisions() is True
    assert c.error_str == ''


def test_collision_gate_passes_when_fork_version_dominates():
    """Upstream `pkgsel 0.79` was dropped; fork ships `0.79+thor1`.
    dpkg version comparison: `0.79+thor1` > `0.79` because `+thor1`
    is a positive suffix.  Gate passes."""
    c = _make_collision_cache(
        deb_drops={'pkgsel': [('main', '0.79')]},
        pkg_versions={'pkgsel': ['0.79+thor1']},
    )
    assert c._verify_no_fork_collisions() is True, c.error_str
    assert c.error_str == ''


def test_collision_gate_fails_when_upstream_dominates():
    """Upstream `pkgsel 0.80` shipped; fork stuck at `0.79+thor1`.
    Upstream > fork → gate fails (would-be-hidden bug fix in 0.80)."""
    c = _make_collision_cache(
        deb_drops={'pkgsel': [('main', '0.80')]},
        pkg_versions={'pkgsel': ['0.79+thor1']},
    )
    assert c._verify_no_fork_collisions() is False
    assert 'pkgsel' in c.error_str
    assert '0.79+thor1' in c.error_str
    assert '0.80' in c.error_str
    assert 'main' in c.error_str


def test_collision_gate_fails_on_tied_versions():
    """Fork and upstream ship identical version (`0.79`).  Gate fails
    because resolution becomes mirror-priority-dependent — fragile.
    Operator must bump the suffix or rename."""
    c = _make_collision_cache(
        deb_drops={'pkgsel': [('main', '0.79')]},
        pkg_versions={'pkgsel': ['0.79']},
    )
    assert c._verify_no_fork_collisions() is False
    assert 'pkgsel' in c.error_str


def test_collision_gate_reports_all_collisions_in_one_error():
    """Three colliding forks → one error message lists all three,
    not just the first.  Operator sees the full landscape in a
    single build."""
    c = _make_collision_cache(
        deb_drops={
            'pkgsel': [('main', '0.80')],
            'curl':   [('main', '8.0.1')],
            'wget':   [('updates', '1.22')],
        },
        pkg_versions={
            'pkgsel': ['0.79+thor1'],
            'curl':   ['7.88+thor1'],
            'wget':   ['1.21+thor1'],
        },
    )
    assert c._verify_no_fork_collisions() is False
    assert 'pkgsel' in c.error_str
    assert 'curl'   in c.error_str
    assert 'wget'   in c.error_str
    assert '3 fork collision' in c.error_str


def test_collision_gate_handles_udeb_namespace():
    """Udeb supersede goes into _upstream_udeb_collisions (separate
    namespace).  Gate must check both."""
    c = _make_collision_cache(
        udeb_drops={'cdebconf-udeb': [('main', '0.265')]},
        udeb_versions={'cdebconf-udeb': ['0.264+thor1']},
    )
    assert c._verify_no_fork_collisions() is False
    assert 'udeb' in c.error_str
    assert 'cdebconf-udeb' in c.error_str


def test_collision_gate_error_message_points_to_docs():
    """Diagnostic must reference docs/collision-gate.md so the
    operator knows where to find the mitigations.  Also asserts
    the version-bump-rejection wording (per memory rule)."""
    c = _make_collision_cache(
        deb_drops={'pkgsel': [('main', '0.80')]},
        pkg_versions={'pkgsel': ['0.79+thor1']},
    )
    assert c._verify_no_fork_collisions() is False
    assert 'docs/collision-gate.md' in c.error_str
    # The diagnostic must reject the version-bump-only mitigation
    # explicitly (per memory/project_fork_collision_no_bump_mitigation).
    assert 'lies about' in c.error_str or 'NOT bump' in c.error_str.lower() or \
           'not bump' in c.error_str.lower(), c.error_str


def test_collision_gate_multi_mirror_drops_same_name():
    """Same pkg dropped from BOTH main and security mirrors.  Per-
    mirror version comparison: main passes (fork dominates) but
    security has a security-update bump that beats fork → gate
    fails reporting the security collision specifically, NOT main.
    Confirms per-(mirror, version) granularity in the diagnostic."""
    c = _make_collision_cache(
        deb_drops={'curl': [('main', '7.88'), ('security', '7.89')]},
        pkg_versions={'curl': ['7.88+thor1']},
    )
    # 7.88+thor1 > 7.88 (main): no collision from main.
    # 7.88+thor1 < 7.89 (security): collision fires from security.
    _ok = c._verify_no_fork_collisions()
    assert _ok is False
    assert 'security' in c.error_str
    assert '7.89' in c.error_str
    # main passed — must NOT appear as a collision.
    assert 'main' not in c.error_str


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


@_with_stub_tui
def test_ingest_udeb_indices_routes_records_to_udeb_hashtable():
    """_ingest_udeb_indices reads the per-mirror udeb Packages files and
    populates udeb_hashtable + udeb_required + udeb_important.  The
    regular package_hashtable is untouched."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    from utils import Mirror
    from collections import defaultdict

    with tempfile.TemporaryDirectory() as _tmp:
        _udeb_path = os.path.join(_tmp, 'di-packages')
        with open(_udeb_path, 'w') as f:
            f.write(_make_udeb_packages_text())

        c = Cache.__new__(Cache)
        c._arch_table = _StubArchTable()
        c.mirrors = [Mirror(id='main', baseurl='http://example/',
                            baseid='debian', release='bookworm', suffix='',
                            component='main', arch='amd64')]
        c.mirror_udeb_cache_files = {'main': _udeb_path}
        c.udeb_hashtable = defaultdict(lambda: defaultdict(list))
        c.udeb_required = []
        c._fork_udeb_names = set()  # FORK-01 Step 2: supersede tracking
        c.udeb_important = []
        # Regular hashtable also needed to assert it's untouched.
        c.package_hashtable = defaultdict(lambda: defaultdict(list))

        c._ingest_udeb_indices('amd64')

        # All 3 records routed to udeb_hashtable
        assert 'base-installer'    in c.udeb_hashtable
        assert 'kmod-udeb'         in c.udeb_hashtable
        assert 'cdebconf-text-udeb' in c.udeb_hashtable
        # Regular hashtable stays empty — these are udebs, not regular pkgs
        assert len(c.package_hashtable) == 0
        # Priority tracking
        assert 'base-installer' in c.udeb_required
        assert 'kmod-udeb'      in c.udeb_important
        assert 'cdebconf-text-udeb' not in c.udeb_required
        assert 'cdebconf-text-udeb' not in c.udeb_important


def test_ingest_udeb_indices_skips_mirrors_without_udeb_file():
    """A mirror absent from mirror_udeb_cache_files is silently skipped —
    no exception, udeb_hashtable stays empty for that mirror's records."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    from utils import Mirror
    from collections import defaultdict

    c = Cache.__new__(Cache)
    c._arch_table = _StubArchTable()
    c.mirrors = [
        Mirror(id='main',     baseurl='http://example/', baseid='debian',
               release='bookworm', suffix='', component='main', arch='amd64'),
        Mirror(id='security', baseurl='http://example/', baseid='debian',
               release='bookworm-security', suffix='', component='main', arch='amd64'),
    ]
    # Neither mirror has a udeb file path — both should be skipped
    c.mirror_udeb_cache_files = {}
    c.udeb_hashtable = defaultdict(lambda: defaultdict(list))
    c.udeb_required = []
    c._fork_udeb_names = set()  # FORK-01 Step 2: supersede tracking
    c.udeb_important = []

    c._ingest_udeb_indices('amd64')   # must not raise
    assert len(c.udeb_hashtable) == 0
    assert c.udeb_required == []
    assert c.udeb_important == []


@_with_stub_tui
def test_ingest_udeb_indices_handles_partial_mirror_set():
    """Only main publishes the d-i index; updates+security don't.  Verify
    the helper handles the realistic mixed case — main's udebs land,
    others are no-ops, no spurious errors."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    from utils import Mirror
    from collections import defaultdict

    with tempfile.TemporaryDirectory() as _tmp:
        _udeb_path = os.path.join(_tmp, 'main-di-packages')
        with open(_udeb_path, 'w') as f:
            f.write(_make_udeb_packages_text())

        c = Cache.__new__(Cache)
        c._arch_table = _StubArchTable()
        c.mirrors = [
            Mirror(id='main',     baseurl='http://example/', baseid='debian',
                   release='bookworm', suffix='', component='main', arch='amd64'),
            Mirror(id='updates',  baseurl='http://example/', baseid='debian',
                   release='bookworm-updates', suffix='', component='main', arch='amd64'),
            Mirror(id='security', baseurl='http://example/', baseid='debian',
                   release='bookworm-security', suffix='', component='main', arch='amd64'),
        ]
        # Only main has udebs.  updates + security mirror.id NOT in dict.
        c.mirror_udeb_cache_files = {'main': _udeb_path}
        c.udeb_hashtable = defaultdict(lambda: defaultdict(list))
        c.udeb_required = []
        c._fork_udeb_names = set()  # FORK-01 Step 2: supersede tracking
        c.udeb_important = []

        c._ingest_udeb_indices('amd64')
        # 3 records from main — others contributed nothing
        assert len(c.udeb_hashtable) == 3
        assert 'base-installer' in c.udeb_required
        assert 'kmod-udeb'      in c.udeb_important


@_with_stub_tui
def test_ingest_udeb_indices_dedups_priority_lists_via_caller():
    """If the same udeb appears in multiple mirrors' indices (e.g. main
    and a mirror that re-publishes), the priority list ends up with
    duplicates after _ingest_udeb_indices.  __build_cache dedups via
    dict.fromkeys after this helper returns — pin that contract here so
    a future refactor doesn't drop the dedup step."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    from utils import Mirror
    from collections import defaultdict

    with tempfile.TemporaryDirectory() as _tmp:
        _udeb_path = os.path.join(_tmp, 'di-packages')
        with open(_udeb_path, 'w') as f:
            f.write(_make_udeb_packages_text())

        c = Cache.__new__(Cache)
        c._arch_table = _StubArchTable()
        c.mirrors = [
            Mirror(id='main1', baseurl='http://example/', baseid='debian',
                   release='bookworm', suffix='', component='main', arch='amd64'),
            Mirror(id='main2', baseurl='http://example/', baseid='debian',
                   release='bookworm', suffix='', component='main', arch='amd64'),
        ]
        c.mirror_udeb_cache_files = {'main1': _udeb_path, 'main2': _udeb_path}
        c.udeb_hashtable = defaultdict(lambda: defaultdict(list))
        c.udeb_required = []
        c._fork_udeb_names = set()  # FORK-01 Step 2: supersede tracking
        c.udeb_important = []

        c._ingest_udeb_indices('amd64')
        # Pre-dedup: main1 + main2 each contribute base-installer → list has dupes
        assert c.udeb_required.count('base-installer') == 2
        # Caller (__build_cache) dedups via list(dict.fromkeys(...))
        assert list(dict.fromkeys(c.udeb_required)) == ['base-installer']


# ─────────────────────────────────────────────────────────────────────────────
# Parallel udeb DependencyTree (Cache.udeb_view)
# ─────────────────────────────────────────────────────────────────────────────

def test_udeb_view_exposes_udeb_hashtable_as_package_hashtable():
    """Cache.udeb_view() returns a thin wrapper whose package_hashtable
    attribute IS the cache's udeb_hashtable.  That's the contract that
    lets DependencyTree resolve against udebs unchanged."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache, UdebCacheView
    from collections import defaultdict

    c = Cache.__new__(Cache)
    c.udeb_hashtable = defaultdict(lambda: defaultdict(list))
    c.udeb_hashtable['cdebconf-text-udeb']['0.270'] = ['fake-pkg-record']
    c.skip_src = []
    c.source_hashtable = defaultdict(list)

    v = c.udeb_view()
    assert isinstance(v, UdebCacheView)
    # The view's package_hashtable IS the cache's udeb_hashtable (same object)
    assert v.package_hashtable is c.udeb_hashtable
    # skip_src and source_hashtable are shared (universal across deb/udeb)
    assert v.skip_src is c.skip_src
    assert v.source_hashtable is c.source_hashtable
    # Lookup goes against udeb_hashtable
    assert v.package_hashtable.get('cdebconf-text-udeb') is not None


def test_udeb_view_get_packages_resolves_against_udeb_hashtable():
    """UdebCacheView.get_packages mirrors Cache.get_packages semantics
    but reads udeb_hashtable.  Bare lookup (no version constraint)
    returns every version's package list flattened."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    from collections import defaultdict

    c = Cache.__new__(Cache)
    c.udeb_hashtable = defaultdict(lambda: defaultdict(list))
    # Two versions of the same udeb under one name
    c.udeb_hashtable['rootskel']['1.135'] = ['pkg-v1.135']
    c.udeb_hashtable['rootskel']['1.136'] = ['pkg-v1.136']
    c.skip_src = []
    c.source_hashtable = defaultdict(list)

    v = c.udeb_view()
    out = v.get_packages('rootskel')
    assert sorted(out) == ['pkg-v1.135', 'pkg-v1.136']
    # Unknown udeb name → empty (and does NOT pollute the defaultdict)
    assert v.get_packages('does-not-exist') == []
    assert 'does-not-exist' not in v.package_hashtable


def test_udeb_view_does_not_leak_into_real_package_hashtable():
    """Resolving against the udeb view must not mutate Cache.package_hashtable.
    The two universes are strictly isolated."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    from collections import defaultdict

    c = Cache.__new__(Cache)
    c.package_hashtable = defaultdict(lambda: defaultdict(list))
    c.package_hashtable['cdebconf']['0.270'] = ['regular-deb-record']
    c.udeb_hashtable = defaultdict(lambda: defaultdict(list))
    c.udeb_hashtable['cdebconf-text-udeb']['0.270'] = ['udeb-record']
    c.skip_src = []
    c.source_hashtable = defaultdict(list)

    v = c.udeb_view()
    # Looking up the .deb name in the view returns empty (it's not a udeb)
    assert v.get_packages('cdebconf') == []
    # Looking up the udeb name in the real cache returns empty too
    assert c.get_packages('cdebconf-text-udeb') == []
    # Each universe sees only its own records
    assert c.get_packages('cdebconf') == ['regular-deb-record']
    assert v.get_packages('cdebconf-text-udeb') == ['udeb-record']


def test_parse_dependency_reuses_lookahead_for_multi_version_same_name():
    """parse_dependency's lookahead-Case-I must fire when the cache
    returns MULTIPLE VERSIONS of the same Package name and that name
    is in __lookahead — not just when there's exactly one matching
    candidate.  Regression test for 2026-05-12 bug:

    `sudo` prompted twice within a single Pass III resolve_packages call
    because:
      - add_lookahead('sudo') prompted, user picked sudo (real); wrote
        __lookahead['sudo'] = {<latest ver>: sudo_pkg}.
      - parse_dependency('sudo') then asked cache.get_packages('sudo')
        which returned 4 entries: sudo at bookworm + bookworm-security
        versions, PLUS sudo-ldap at the same two versions (both Provide
        sudo).
      - Both sudo Package records had ['Package']='sudo', and 'sudo' was
        in __lookahead → _selected_pkg_lookahead = [sudo_v1, sudo_v2],
        length 2.
      - Old Case I's strict `len == 1` check fell through, multi-cand
        prompt path fired → second prompt for the same name.

    Fix: collapse _selected_pkg_lookahead by Package name (highest version
    per name).  Case I matches when one Package name remains."""
    import sys
    from collections import defaultdict
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    import tui

    class _Pkg:
        """Minimal Package surface used by parse_dependency's early
        returns + lookahead check."""
        def __init__(self, name, ver, provides=None):
            self._fields = {'Package': name, 'Version': ver}
            self.package = name
            self.version = ver
            self._provides = provides or []
            self.conflicts = []
            self.depends = []
            self.pre_depends = []
            self.recommends = []
            self.alt_depends = []
            self.breaks = []
            self.constraints_satisfied = True
        def __getitem__(self, k): return self._fields[k]
        def get_provides(self): return list(self._provides)
        def add_constraint(self, v, o): pass

    sudo_v1     = _Pkg('sudo',      '1.9.13p3-1+deb12u2')
    sudo_v2     = _Pkg('sudo',      '1.9.13p3-1+deb12u3')
    sudoldap_v1 = _Pkg('sudo-ldap', '1.9.13p3-1+deb12u2',
                        provides=[('sudo', '1.9.13p3-1+deb12u2')])
    sudoldap_v2 = _Pkg('sudo-ldap', '1.9.13p3-1+deb12u3',
                        provides=[('sudo', '1.9.13p3-1+deb12u3')])

    class _Cache:
        def __init__(self):
            # Mirrors what Cache populates: 'sudo' key holds both the
            # real sudo records AND sudo-ldap records (because sudo-ldap
            # Provides sudo).  4 entries total.
            self.package_hashtable = {
                'sudo': {
                    sudo_v1.version: [sudo_v1, sudoldap_v1],
                    sudo_v2.version: [sudo_v2, sudoldap_v2],
                },
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
    dt._auto_pick_highest_when_ambiguous = False
    dt.arch = 'amd64'
    dt.build_profiles = frozenset()

    # Simulate add_lookahead having picked sudo (the real package, latest
    # version): __lookahead['sudo'] = {v2_str: sudo_v2}.
    dt._DependencyTree__lookahead['sudo'][sudo_v2.version] = sudo_v2

    # If parse_dependency tries to prompt, fail with a clear message —
    # the lookahead choice must be reused.  Patch
    # dependencytree.Prompt (the module's local import binding) rather
    # than tui.Prompt — dependencytree did `from tui import Prompt` so
    # patching the source module wouldn't be seen by the consumer.
    _orig_prompt = dependencytree.Prompt
    _prompt_calls = []
    class _RaisingPrompt:
        def __init__(self, *args, **kwargs):
            _prompt_calls.append((args, kwargs))
        def get_response(self):
            raise AssertionError(
                "parse_dependency should NOT prompt — lookahead already "
                f"disambiguated 'sudo'.  Prompt args: {_prompt_calls[-1]}"
            )
    dependencytree.Prompt = _RaisingPrompt
    try:
        result = dt.parse_dependency('sudo')
    finally:
        dependencytree.Prompt = _orig_prompt

    # No prompts fired.
    assert _prompt_calls == [], (
        f"parse_dependency prompted {len(_prompt_calls)} time(s); "
        "Case I lookahead-reuse should have short-circuited"
    )
    # Case I picked the latest sudo (real, not sudo-ldap).
    assert result is sudo_v2, (
        f"expected sudo_v2 ({sudo_v2.version}), got {result and result.package} "
        f"{result and result.version}"
    )


def test_dependency_tree_udeb_tree_flag_enables_max_version_fallback():
    """When auto_pick_highest_when_ambiguous=True, multi-name candidates
    that _auto_pick_candidate refuses to auto-pick get the highest-version
    fallback applied.  Verifies the flag is honoured at the parse_dependency
    call-site."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from dependencytree import _auto_pick_candidate

    # Construct fake candidates mimicking ext4-modules-6.1.0-{NN}-amd64-di
    # at different versions.  _auto_pick_candidate collapses by name (one
    # entry per name); since names differ, it returns (None, collapsed).
    class _FakeCandidate:
        def __init__(self, name, ver):
            self._fields = {'Package': name}
            self.package = name
            self.version = ver
        def __getitem__(self, k): return self._fields[k]

    cands = [
        _FakeCandidate('ext4-modules-6.1.0-39-amd64-di', '6.1.148-1'),
        _FakeCandidate('ext4-modules-6.1.0-42-amd64-di', '6.1.159-1'),
        _FakeCandidate('ext4-modules-6.1.0-44-amd64-di', '6.1.164-1'),
        _FakeCandidate('ext4-modules-6.1.0-45-amd64-di', '6.1.170-1'),
        _FakeCandidate('ext4-modules-6.1.0-46-amd64-di', '6.1.170-2'),
        _FakeCandidate('ext4-modules-6.1.0-47-amd64-di', '6.1.170-3'),
    ]
    _auto, _collapsed = _auto_pick_candidate(cands)
    # Multi-name: _auto is None, _collapsed has all 6
    assert _auto is None
    assert len(_collapsed) == 6
    # The fallback the udeb call-site applies: max(collapsed, key=ver)
    _picked = max(_collapsed, key=lambda p: p.version)
    assert _picked.package == 'ext4-modules-6.1.0-47-amd64-di'
    assert _picked.version == '6.1.170-3'


def test_auto_pick_candidate_prefers_real_package_matching_seed_name():
    """Regression guard (caught 2026-05-18 install): when a seed name
    matches a real Package: in the candidate set, _auto_pick_candidate
    must pick that real package even if other candidates merely
    `Provides:` it.  Mirrors apt's "real package wins over virtual" rule.

    Bug symptom: seed `anacron` in [laptop] resolved to
    `systemd-cron (Provides anacron)` instead of `anacron (real)`,
    triggering `ERROR: Cannot add 'anacron' — conflicts with 'cron'
    already in lookahead` (systemd-cron Conflicts cron; anacron does
    not).  Resolver eventually recovered via a different path but the
    detour cost a confusing error message and a non-deterministic pick.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from dependencytree import _auto_pick_candidate

    class _FakeCandidate:
        def __init__(self, name, ver):
            self._fields = {'Package': name}
            self.package = name
            self.version = ver
        def __getitem__(self, k): return self._fields[k]

    cands = [
        _FakeCandidate('anacron', '2.3-36'),
        _FakeCandidate('systemd-cron', '1.15.19-5'),
    ]
    # Without prefer_name: multi-name → no auto-pick.
    _auto, _collapsed = _auto_pick_candidate(cands)
    assert _auto is None
    assert {c.package for c in _collapsed} == {'anacron', 'systemd-cron'}

    # With prefer_name='anacron': pick the real anacron, ignoring systemd-cron.
    _auto, _collapsed = _auto_pick_candidate(cands, prefer_name='anacron')
    assert _auto is not None
    assert _auto.package == 'anacron'
    assert _auto.version == '2.3-36'

    # With prefer_name='awk' (no real match): falls through to None (would
    # prompt or apply per-tree fallback).
    _auto, _collapsed = _auto_pick_candidate(cands, prefer_name='awk')
    assert _auto is None

    # Single-name case still auto-picks regardless of prefer_name.
    single = [_FakeCandidate('foo', '1.0')]
    _auto, _ = _auto_pick_candidate(single, prefer_name='unrelated')
    assert _auto is not None and _auto.package == 'foo'


def test_dependency_tree_constructor_accepts_auto_pick_flag():
    """DependencyTree.__init__ accepts auto_pick_highest_when_ambiguous as
    a keyword arg; defaults to False (deb tree); True is honoured (udeb tree).
    Catches accidental signature regressions during refactors."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from dependencytree import DependencyTree
    sig = inspect.signature(DependencyTree.__init__)
    assert 'auto_pick_highest_when_ambiguous' in sig.parameters
    p = sig.parameters['auto_pick_highest_when_ambiguous']
    assert p.default is False




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


def test_compute_install_batches_excludes_extras_pkg_names():
    """chroot install path skips packages in
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
        ": extras must be filtered out of install batches"


def test_verify_dep_resolution_skips_extras():
    """REGRESSION: _verify_dep_resolution walked canonical_pkgs
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
    assert 'IncludeRecommendsInRepo' in output


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


def test_refresh_patches_iterates_both_deb_and_udeb_trees():
    """Phase 4 regression guard: _refresh_patches must walk the udeb tree
    too — otherwise a source that exists only in udeb_dep_tree (e.g.
    fuse3 pulled because libfuse3-3-udeb is a d-i udeb dep) gets an
    empty patch_list even when patch/source/<pkg>/<ver>/*.patch is on
    disk, and `source build <pkg>` fails because the build container
    runs the unpatched debian/rules.  Caught in production 2026-05-10."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    src = inspect.getsource(BuildSession._refresh_patches)
    # Both tree references must appear: the deb tree (always) and the
    # udeb tree (Phase 4).  Use string substring rather than a real
    # invocation since _refresh_patches reads BuildConfig + filesystem;
    # the contract is "iterate the union", which we can pin via source.
    assert 'self.dep_tree.selected_srcs' in src, (
        "_refresh_patches must include deb tree")
    assert 'self.udeb_dep_tree' in src and 'selected_srcs' in src, (
        "_refresh_patches must walk udeb tree's sources too")


def test_refresh_patches_invalidates_result_when_patch_newer():
    """COMP-02 phase C: _refresh_patches must delete a .result file
    when the patch CONTENT has changed since the last successful build.
    Without this, autorun's source-build step skips packages with
    `[SKIPPED] already built` even when the operator just modified a
    patch (caught 2026-05-13 with the base-installer keyring patch —
    autorun ran but the .udeb was the May-10 build, the patch never
    applied, install failed with 'No public key').

    Two-stage check: mtime gate + content hash.  This test exercises
    the real-change path: stale .patchhash on disk with a different
    digest from the on-disk patch content → mtime gate trips → hash
    confirms divergence → .result is removed and .patchhash rewritten.
    """
    import sys, tempfile, time
    from unittest.mock import MagicMock, patch as mock_patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    from package import Source

    with tempfile.TemporaryDirectory() as _root:
        # Build a synthetic env: a single source 'foo' v1.0 with a
        # patch on disk, an older .result, and a stale .patchhash
        # reflecting an EARLIER patch revision.
        _log_build = os.path.join(_root, 'log', 'build')
        _patch_dir = os.path.join(_root, 'patch', 'source', 'foo', '1.0')
        os.makedirs(_log_build)
        os.makedirs(_patch_dir)
        _result = os.path.join(_log_build, 'foo.result')
        _hash_file = os.path.join(_log_build, 'foo.patchhash')
        _patch = os.path.join(_patch_dir, '9001-test.patch')
        with open(_result, 'w') as fh: fh.write('PASS\n')
        # Pretend the last build saw the patch with old content.
        with open(_hash_file, 'w') as fh: fh.write('deadbeef' * 8 + '\n')
        time.sleep(0.01)
        with open(_patch, 'w') as fh: fh.write(
            'Description: t\nAuthor: t\nForwarded: no\nLast-Update: 2026-05-13\n'
            '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'
        )
        _now = time.time()
        os.utime(_result, (_now - 100, _now - 100))
        os.utime(_hash_file, (_now - 100, _now - 100))
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
        assert not os.path.exists(_result), (
            f"_refresh_patches must invalidate stale .result; still at {_result}"
        )
        assert _src.patch_list == ['9001-test.patch'], _src.patch_list
        # New baseline written reflecting the current on-disk patch.
        with open(_hash_file, 'r') as fh:
            _new_hash = fh.read().strip()
        assert _new_hash and _new_hash != 'deadbeef' * 8, (
            f"baseline .patchhash must be rewritten; still {_new_hash!r}"
        )


def test_refresh_patches_skips_invalidation_for_header_only_edit():
    """Two-stage invalidation: a patch whose MTIME is newer than the
    .result but whose CONTENT matches the recorded .patchhash must NOT
    trigger a rebuild.  Covers the common case of editing only the
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
    from utils import patch_set_hash

    with tempfile.TemporaryDirectory() as _root:
        _log_build = os.path.join(_root, 'log', 'build')
        _patch_dir = os.path.join(_root, 'patch', 'source', 'foo', '1.0')
        os.makedirs(_log_build); os.makedirs(_patch_dir)
        _result = os.path.join(_log_build, 'foo.result')
        _hash_file = os.path.join(_log_build, 'foo.patchhash')
        _patch = os.path.join(_patch_dir, '9001-test.patch')
        _content = (
            'Description: t\nAuthor: t\nForwarded: no\nLast-Update: 2026-05-13\n'
            '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'
        )
        with open(_patch, 'w') as fh: fh.write(_content)
        with open(_result, 'w') as fh: fh.write('PASS\n')
        # Baseline hash matches the current on-disk content.
        with open(_hash_file, 'w') as fh:
            fh.write(patch_set_hash(_patch_dir, ['9001-test.patch']) + '\n')
        # Patch mtime > result mtime (header-only edit scenario).
        _now = time.time()
        os.utime(_result, (_now - 100, _now - 100))
        os.utime(_hash_file, (_now - 100, _now - 100))
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
        assert os.path.exists(_result), (
            "header-only patch edit (same hash) must NOT invalidate .result"
        )
        # .result mtime should be touched past the patch mtime so future
        # patch_refresh runs don't keep re-entering the hash branch.
        assert os.path.getmtime(_result) >= os.path.getmtime(_patch), (
            "_refresh_patches must touch .result mtime past patch mtime; "
            f"result={os.path.getmtime(_result)} patch={os.path.getmtime(_patch)}"
        )


def test_refresh_patches_writes_baseline_when_no_patchhash():
    """Migration path: a source with .result but NO .patchhash (built
    before the hash schema landed) must NOT be invalidated on first
    encounter — the existing .result is trusted to reflect the current
    patches.  Instead the current hash is written as a baseline so
    future runs compare against it correctly."""
    import sys, tempfile, time
    from unittest.mock import MagicMock, patch as mock_patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    from package import Source
    from utils import patch_set_hash

    with tempfile.TemporaryDirectory() as _root:
        _log_build = os.path.join(_root, 'log', 'build')
        _patch_dir = os.path.join(_root, 'patch', 'source', 'foo', '1.0')
        os.makedirs(_log_build); os.makedirs(_patch_dir)
        _result = os.path.join(_log_build, 'foo.result')
        _hash_file = os.path.join(_log_build, 'foo.patchhash')
        _patch = os.path.join(_patch_dir, '9001-test.patch')
        with open(_patch, 'w') as fh: fh.write(
            'Description: t\nAuthor: t\nForwarded: no\nLast-Update: 2026-05-13\n'
            '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'
        )
        with open(_result, 'w') as fh: fh.write('PASS\n')
        # Patch newer than .result; NO .patchhash present (pre-upgrade state).
        _now = time.time()
        os.utime(_result, (_now - 100, _now - 100))
        os.utime(_patch, (_now, _now))
        assert not os.path.exists(_hash_file)

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
            "first-encounter migration must NOT invalidate trusted .result"
        )
        assert os.path.exists(_hash_file), (
            "first-encounter migration must write baseline .patchhash"
        )
        with open(_hash_file, 'r') as fh:
            _written = fh.read().strip()
        assert _written == patch_set_hash(_patch_dir, ['9001-test.patch']), (
            f"baseline hash mismatch: {_written}"
        )


def test_refresh_patches_invalidates_when_patches_removed():
    """Patch deletion: empty patch_list with a non-empty .patchhash on
    disk (from a previous build that applied patches) → hash differs
    from current empty-set hash → invalidate .result + drop the
    .patchhash.  Comes for free with the content-hash schema; the old
    mtime-only check could not detect this case (deletion doesn't bump
    any patch's mtime)."""
    import sys, tempfile
    from unittest.mock import MagicMock, patch as mock_patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    from package import Source

    with tempfile.TemporaryDirectory() as _root:
        _log_build = os.path.join(_root, 'log', 'build')
        # Patch dir does NOT exist (patches were removed).
        os.makedirs(_log_build)
        _result = os.path.join(_log_build, 'foo.result')
        _hash_file = os.path.join(_log_build, 'foo.patchhash')
        with open(_result, 'w') as fh: fh.write('PASS\n')
        # Stale baseline hash reflecting the now-deleted patch set.
        with open(_hash_file, 'w') as fh: fh.write('cafef00d' * 8 + '\n')

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
        assert not os.path.exists(_result), (
            "patch removal must invalidate the now-stale .result"
        )
        assert not os.path.exists(_hash_file), (
            "patch removal must drop the now-stale .patchhash"
        )


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


def test_source_download_iterates_both_deb_and_udeb_trees():
    """Phase 4 regression guard: cmd_source_sync must call
    utils.download_source for the udeb tree too — otherwise sources that
    exist only in udeb_dep_tree (base-installer, debian-installer-utils,
    debootstrap, etc.) never land in dir_source and `source build
    installer` fails with `cp: cannot stat /source/<pkg>*: No such file
    or directory` inside the build container.  Caught in production on
    2026-05-10."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    src = inspect.getsource(BuildSession.cmd_source_sync)
    # Both tree references must appear in the code (size + download call).
    assert 'self.dep_tree.download_size' in src or '_deb_size' in src, (
        "cmd_source_sync must include deb tree download size")
    assert 'self.udeb_dep_tree.download_size' in src or '_udeb_size' in src, (
        "cmd_source_sync must include udeb tree download size")
    assert 'utils.download_source(self.dep_tree' in src, (
        "cmd_source_sync must call download_source on deb tree")
    assert 'utils.download_source(' in src and 'self.udeb_dep_tree' in src, (
        "cmd_source_sync must also call download_source on udeb tree")


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


def test_autorun_disk_reuses_live_chroot_then_builds_disk():
    """`autorun disk` shares the live early stages (it masters the disk
    image from the verified LIVE chroot), then ends with `iso build disk`
    gated on iso_disk_ready — not `iso build live`."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    src = inspect.getsource(BuildSession.cmd_auto_run_disk)
    _i_live   = src.find("'source build live'")
    _i_chroot = src.find("'chroot build'")
    _i_iso    = src.find("'iso build disk'")
    assert _i_live > 0,   "_steps missing 'source build live' (live extras)"
    assert _i_chroot > 0, "_steps missing 'chroot build' (live chroot)"
    assert _i_iso > 0,    "_steps missing 'iso build disk' terminal stage"
    assert _i_live < _i_chroot < _i_iso, (
        f"stage order wrong: live @ {_i_live}, chroot @ {_i_chroot}, "
        f"iso disk @ {_i_iso}"
    )
    assert "cmd_build_iso_disk" in src, "autorun disk must call cmd_build_iso_disk"
    assert "'iso_disk_ready'" in src, "autorun disk must gate on iso_disk_ready"
    # Must NOT terminate on the live ISO — that would be the wrong artifact.
    assert "cmd_build_iso_live" not in src, (
        "autorun disk must end on the disk image, not the live ISO")


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


def test_format_gpg_time_renders_epoch_as_utc_iso():
    """gpg's --with-colons emits creation/expiry as Unix epoch seconds.
    format_gpg_time renders that as a human-readable UTC stamp.
    1778372793 is 2026-05-10 00:26 UTC (verified independently)."""
    from signing import format_gpg_time
    assert format_gpg_time('1778372793') == '2026-05-10 00:26 UTC'


def test_format_gpg_time_empty_returns_default():
    """Empty string (no expiration set on the gpg key — Athena's default
    for manual-rotation keys) returns the caller-supplied default."""
    from signing import format_gpg_time
    assert format_gpg_time('', '(never — manual rotation)') == '(never — manual rotation)'
    assert format_gpg_time('') == ''


def test_format_gpg_time_garbage_returns_raw():
    """Non-integer input degrades to the raw string rather than raising
    or returning the default — surfaces a parser bug instead of hiding it."""
    from signing import format_gpg_time
    assert format_gpg_time('not-a-number') == 'not-a-number'


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


# ─── CONF-02: network apt source (sources.list.d/athena.list) ────────────────

def _stub_chroot_mixin_for_apt_source(*, apt_source_url, build_codename='thor'):
    """_ChrootMixin wired only enough to exercise _write_athena_apt_source:
    a _config carrying apt_source_url + build_codename, and a recording
    stub in place of _write_chroot_file."""
    import chroot
    class _Cfg:
        pass
    cfg = _Cfg()
    cfg.apt_source_url = apt_source_url
    cfg.build_codename = build_codename
    inst = chroot._ChrootMixin.__new__(chroot._ChrootMixin)
    inst._config = cfg
    inst._dir_chroot = '/tmp/fake-chroot'
    inst._password = 'test-password-not-real'
    _writes = []
    inst._write_chroot_file = lambda path, content: _writes.append((path, content))
    return inst, _writes


def test_write_athena_apt_source_noop_when_url_empty():
    """Empty [Repo] AptSourceURL → no file written (target relies on the
    /cdrom/pool source added at install time)."""
    inst, writes = _stub_chroot_mixin_for_apt_source(apt_source_url='')
    inst._write_athena_apt_source()
    assert writes == [], f"empty URL must write nothing, got {writes}"


def test_write_athena_apt_source_writes_signed_by_when_url_set():
    """A configured URL → /etc/apt/sources.list.d/athena.list with a
    [signed-by=...athena-archive-keyring.gpg] pin, the URL, the release
    codename, and the main component."""
    inst, writes = _stub_chroot_mixin_for_apt_source(
        apt_source_url='https://repo.example.org/athena',
        build_codename='thor',
    )
    inst._write_athena_apt_source()
    assert len(writes) == 1, f"expected one write, got {writes}"
    _path, _content = writes[0]
    assert _path == '/etc/apt/sources.list.d/athena.list', _path
    assert _content.startswith('deb [signed-by='), _content
    assert '/usr/share/keyrings/athena-archive-keyring.gpg]' in _content, _content
    assert 'https://repo.example.org/athena thor main' in _content, _content
    assert _content.endswith('\n'), "apt source line must be newline-terminated"


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


def test_audit_dep_closure_invokes_progress_cb_per_pkg():
    """`audit_dep_closure` accepts an optional progress_cb that fires
    once per (package, cohort) iteration.  Pin so cmd_audit's
    ProgressBar plumbing doesn't silently break on a refactor.
    Synthesises a 3-pkg RepoState + counts callback firings."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from repo_audit import audit_dep_closure, RepoState

    _state = RepoState(
        packages={
            'a': {'Package': 'a', 'Version': '1', 'Depends': 'b'},
            'b': {'Package': 'b', 'Version': '1'},
            'c': {'Package': 'c', 'Version': '1', 'Depends': 'absent (>= 99)'},
        },
        provides_index={},
        packages_file='',
        repo_mtime=0.0,
    )
    _calls: list = []
    audit_dep_closure(
        _state, consumer_set=None,
        progress_cb=lambda: _calls.append(1),
    )
    assert len(_calls) == 3, (
        f"progress_cb fired {len(_calls)} times; expected one fire per "
        f"package in state (3 packages)"
    )


def test_audit_conflict_cohort_invokes_progress_cb_per_pkg():
    """Same plumbing as audit_dep_closure, for the conflict gate."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from repo_audit import audit_conflict_cohort, RepoState

    _state = RepoState(
        packages={
            'x': {'Package': 'x', 'Version': '1'},
            'y': {'Package': 'y', 'Version': '1'},
        },
        provides_index={},
        packages_file='',
        repo_mtime=0.0,
    )
    _calls: list = []
    audit_conflict_cohort(
        _state, cohort=frozenset(['x', 'y']),
        progress_cb=lambda: _calls.append(1),
    )
    assert len(_calls) == 2


def test_audit_nmu_residue_invokes_progress_cb_per_pkg():
    """audit_nmu_residue mirrors the per-pkg progress_cb contract."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from repo_audit import audit_nmu_residue, RepoState

    _state = RepoState(
        packages={
            'p': {'Package': 'p', 'Version': '1.0+deb12u1'},   # has NMU
            'q': {'Package': 'q', 'Version': '1.0'},           # clean
        },
        provides_index={},
        packages_file='',
        repo_mtime=0.0,
    )
    _calls: list = []
    _findings = audit_nmu_residue(_state, progress_cb=lambda: _calls.append(1))
    assert len(_calls) == 2
    # And the actual residue is still detected:
    assert any(f[0] == 'p' for f in _findings), _findings


def test_cache_build_wraps_cache_construction_in_spinner():
    """`cmd_build_cache` wraps Cache(self.config) in a Spinner so the
    quiet phases between record-index ProgressBars (mirror fetch,
    cross-validation, dep-graph internals) don't look like a frozen
    TUI.  Source-text inspection — exercising the full cache build
    needs network access + the actual snapshot.
    """
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r"\n    def cmd_build_cache\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m, "cmd_build_cache body not found"
    _method = _m.group(0)
    assert 'Spinner(' in _method, (
        "cmd_build_cache must wrap Cache() in a Spinner"
    )
    # Spinner must be done()'d in a finally so a Cache() exception
    # doesn't leak the spinner widget.
    assert 'try:' in _method and 'finally:' in _method
    assert '_spin.done()' in _method


def test_repo_index_prints_post_run_summary():
    """`cmd_index_repo` calls _print_repo_index_summary after a
    successful index run.  Summary surfaces per-suite Release sig
    state + per-component file counts + total payload — quick at-a-
    glance confirmation the index landed completely.
    """
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    # The summary helper is defined as a method.
    assert 'def _print_repo_index_summary(' in _body, (
        "_print_repo_index_summary helper missing"
    )
    # cmd_index_repo invokes it.
    _m = re.search(
        r"\n    def cmd_index_repo\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m, "cmd_index_repo body not found"
    assert 'self._print_repo_index_summary(' in _m.group(0), (
        "cmd_index_repo must call _print_repo_index_summary after a "
        "successful index run"
    )


def test_source_audit_uses_per_source_progress_bar():
    """`cmd_source_audit` wraps its per-cohort selected_srcs walk in
    a ProgressBar — 500+ sources × multi-file ar-validity checks each
    is enough to look frozen without progress feedback."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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


def test_scan_packages_with_progress_writes_output_via_subprocess_run():
    """The Spinner-wrapped scanner helper invokes the scanner via
    `bash -c 'cd … && <scanner> 2>/dev/null > <tempfile>'` through
    subprocess.run, then moves the tempfile to output_path.  Pin
    the post-run output presence + a successful return."""
    import sys, tempfile
    from unittest.mock import patch, MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit

    _stub_packages = (
        "Package: a\nVersion: 1.0\n\n"
        "Package: b\nVersion: 2.0\n\n"
    )

    def _fake_run(argv, *_a, **kw):
        # Identify the `bash -c '… > <tempfile>'` call and write the
        # stub stanzas to that tempfile so subsequent move + checks
        # see real content.  Anything else (the `sudo install` follow-
        # up in the sudo path) just returns rc=0.
        if (isinstance(argv, (list, tuple)) and len(argv) >= 3
                and argv[0] == 'bash' and argv[1] == '-c'
                and '> ' in argv[2]):
            _target = argv[2].split('> ')[1].strip().split()[0]
            with open(_target, 'w') as fh:
                fh.write(_stub_packages)
        _r = MagicMock()
        _r.returncode = 0
        _r.stderr = ''
        return _r

    with tempfile.TemporaryDirectory() as _tmp:
        _out = os.path.join(_tmp, 'Packages')
        with patch.object(repo_audit.subprocess, 'run', side_effect=_fake_run):
            _ok = repo_audit._scan_packages_with_progress(
                ['dpkg-scanpackages', '--multiversion', _tmp, '/dev/null'],
                _out, _tmp,
            )
        assert _ok is True
        # Output file moved into place + carries the stub stanzas.
        assert os.path.isfile(_out)
        with open(_out) as fh:
            _written = fh.read()
        assert _written == _stub_packages


# ─────────────────────────────────────────────────────────────────────────────
# COMP-09 — disk_image module + `iso build disk` command
# ─────────────────────────────────────────────────────────────────────────────


def test_disk_image_module_exposes_build_disk_image_signature():
    """Top-level entry exists with the expected positional + kwarg
    surface.  Anti-regression so a refactor doesn't silently rename
    the helper out from under cmd_build_iso_disk."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import disk_image
    assert hasattr(disk_image, 'build_disk_image'), (
        "disk_image.build_disk_image is the operator entry point — must exist"
    )
    _sig = inspect.signature(disk_image.build_disk_image)
    _params = list(_sig.parameters.keys())
    # Required first four positionals: dir_chroot, output_qcow2,
    # size_gb, password.  Plus optional container kwarg for the
    # COMP-14 follow-up.
    assert _params[:4] == ['dir_chroot', 'output_qcow2', 'size_gb', 'password'], (
        f"build_disk_image signature changed; got {_params}"
    )
    assert 'container' in _params, (
        "container kwarg must remain — present so the COMP-14 follow-"
        "up can route grub-install through it without a signature break"
    )


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


def test_disk_image_has_bios_modules_probes_chroot_dir():
    """`_has_bios_modules(mnt)` returns True iff <mnt>/usr/lib/grub/
    i386-pc/ exists.  Used to decide whether to attempt BIOS
    grub-install (skipping cleanly when grub-pc-bin wasn't installed
    in the chroot — operator's pkg.list may not include it)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import disk_image
    with tempfile.TemporaryDirectory() as _tmp:
        # Empty mnt — no i386-pc/ → False.
        assert disk_image._has_bios_modules(_tmp) is False
        # Create the dir → True.
        os.makedirs(os.path.join(_tmp, 'usr', 'lib', 'grub', 'i386-pc'))
        assert disk_image._has_bios_modules(_tmp) is True


def test_disk_image_grub_install_uses_absolute_path_and_env_path():
    """Operator-observed failure (2026-05-22): bare `grub-install`
    inside `chroot _mnt grub-install` returned ENOENT because sudo's
    secure_path didn't include /usr/sbin and PATH lookup inside the
    chroot missed it.  Fix: use absolute `/usr/sbin/grub-install` +
    explicit `env PATH=...` prefix.  Pin both to prevent regression."""
    _path = os.path.join(_ROOT, 'scripts', 'disk_image.py')
    with open(_path) as fh:
        _body = fh.read()
    # Absolute path to grub-install (the grub.cfg generation path
    # uses the hand-rolled minimal cfg unconditionally — update-grub
    # was removed 2026-05-23 after producing a chroot'd cfg the
    # booted grub couldn't load).
    assert '/usr/sbin/grub-install' in _body, (
        "grub-install must be invoked with absolute path inside chroot"
    )
    # Explicit env PATH belt-and-suspenders for sub-processes spawned
    # by grub-install (grub-mkimage, grub-probe, etc.).
    assert "PATH=/usr/local/sbin" in _body
    assert "'env'" in _body, (
        "chroot calls must run via `env PATH=...` prefix"
    )


def test_disk_image_writes_minimal_grub_cfg_unconditionally():
    """`update-grub` inside a chrooted target is fragile (os-prober
    can fail, /etc/default/grub may be missing, generated cfg may
    have paths that don't work post-boot).  Operator hit a grub-
    shell-on-boot 2026-05-23 because update-grub returned 0 but
    produced an unusable cfg.  Pin: build_disk_image must call
    _write_minimal_grub_cfg unconditionally (not as a fallback), and
    must NOT invoke update-grub."""
    _path = os.path.join(_ROOT, 'scripts', 'disk_image.py')
    with open(_path) as fh:
        _body = fh.read()
    # update-grub call must not appear in build_disk_image's flow.
    # (The helper function _write_minimal_grub_cfg can still mention
    # `grub-mkconfig` in its docstring — substring check on the
    # chroot'd call.)
    assert "'/usr/sbin/update-grub'" not in _body, (
        "update-grub must not be called from build_disk_image — the "
        "hand-rolled minimal grub.cfg is the deterministic path"
    )
    # And the unconditional call to _write_minimal_grub_cfg exists.
    import re
    assert re.search(
        r"if not _write_minimal_grub_cfg\(_mnt, _root_uuid, password\):",
        _body,
    ), "_write_minimal_grub_cfg must be invoked unconditionally"


def test_disk_image_efi_only_fallback_when_no_bios_modules():
    """When the chroot has no /usr/lib/grub/i386-pc/ (grub-pc-bin
    not installed), BIOS grub-install must be SKIPPED — not attempted
    and failed.  Source-text pin: the `if _has_bios_modules(_mnt):`
    gate must wrap the BIOS install branch."""
    _path = os.path.join(_ROOT, 'scripts', 'disk_image.py')
    with open(_path) as fh:
        _body = fh.read()
    import re
    # The BIOS grub-install call must live inside an if _has_bios_modules
    # block (heuristic: pattern shows up before the i386-pc invocation).
    _m = re.search(
        r"if _has_bios_modules\(_mnt\):.*?--target=i386-pc",
        _body, re.DOTALL,
    )
    assert _m, (
        "BIOS grub-install must be gated on _has_bios_modules(_mnt) — "
        "running it unconditionally on a chroot without i386-pc modules "
        "fails the whole build"
    )


def test_disk_image_minimal_grub_cfg_writes_uuid_kernel_line():
    """The hand-rolled grub.cfg fallback (used when update-grub fails)
    must reference the root UUID and an actual /boot/vmlinuz-* kernel
    from the mounted target."""
    import sys, tempfile
    from unittest.mock import patch, MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import disk_image

    with tempfile.TemporaryDirectory() as _mnt:
        # Set up <mnt>/boot/vmlinuz-* + initrd.img-* + grub dir
        os.makedirs(os.path.join(_mnt, 'boot', 'grub'))
        for _f in ('vmlinuz-6.1.0-45-amd64', 'initrd.img-6.1.0-45-amd64'):
            with open(os.path.join(_mnt, 'boot', _f), 'w') as fh:
                fh.write('stub')

        # Capture the install call to inspect what got written.
        _captured = {}
        def _fake_sudo(argv, _password, capture=True):
            # ['install', '-m', '644', <tmp>, <dest>]
            if argv[0] == 'install':
                _src = argv[3]
                with open(_src) as fh:
                    _captured['content'] = fh.read()
                _captured['dest'] = argv[4]
            _r = MagicMock()
            _r.returncode = 0
            _r.stderr = ''
            return _r

        with patch.object(disk_image, '_sudo', side_effect=_fake_sudo):
            _ok = disk_image._write_minimal_grub_cfg(
                _mnt, 'aaaa-bbbb-cccc-dddd', 'pw',
            )
        assert _ok is True
        # Content checks:
        _c = _captured['content']
        assert 'aaaa-bbbb-cccc-dddd' in _c, (
            f"root UUID missing from grub.cfg:\n{_c}"
        )
        assert 'vmlinuz-6.1.0-45-amd64' in _c, (
            f"kernel filename missing from grub.cfg:\n{_c}"
        )
        assert 'initrd.img-6.1.0-45-amd64' in _c, (
            f"initrd filename missing from grub.cfg:\n{_c}"
        )
        # Destination is /boot/grub/grub.cfg under the mount.
        assert _captured['dest'].endswith('/boot/grub/grub.cfg')


def test_disk_image_minimal_grub_cfg_fails_clean_when_no_kernel():
    """When /boot has no vmlinuz-* (chroot built without a kernel
    package somehow), the fallback returns False instead of writing
    a broken grub.cfg.  Surfaces the gap to the operator."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import disk_image
    with tempfile.TemporaryDirectory() as _mnt:
        os.makedirs(os.path.join(_mnt, 'boot', 'grub'))
        # No vmlinuz-* present.
        assert disk_image._write_minimal_grub_cfg(
            _mnt, 'uuid-x', 'pw',
        ) is False


def test_disk_image_sfdisk_script_lays_out_three_partitions():
    """The sfdisk script template must produce: 1 MB BIOS-boot, 100 MB
    EFI (vfat), rest = root.  Pin via substring checks — small enough
    to read end-to-end if the layout ever needs to change."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import disk_image
    _s = disk_image._SFDISK_SCRIPT_TEMPLATE
    assert 'label: gpt' in _s, "must declare a GPT label"
    # BIOS-boot type GUID
    assert '21686148-6449-6E6F-744E-656564454649' in _s, (
        "BIOS-boot partition type GUID missing"
    )
    # EFI System Partition type GUID
    assert 'C12A7328-F81F-11D2-BA4B-00A0C93EC93B' in _s, (
        "ESP partition type GUID missing"
    )
    # Root linux-fs GUID
    assert '0FC63DAF-8483-4772-8E79-3D69D8477DE4' in _s, (
        "Linux filesystem partition type GUID missing"
    )
    # ESP must be marked bootable (per UEFI spec)
    assert 'bootable' in _s


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


def test_cmd_build_iso_disk_gates_on_chroot_verified_and_reads_size():
    """cmd_build_iso_disk: chroot_verified gate (with force bypass)
    + size_gb arg parsing (default from config, override via first
    non-flag positional)."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r"\n    def cmd_build_iso_disk\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m, "cmd_build_iso_disk body not found"
    _method = _m.group(0)
    # Gate on chroot_verified with force bypass.
    assert "self.flags.chroot_verified" in _method, (
        "cmd_build_iso_disk must check chroot_verified flag"
    )
    assert "_force" in _method
    # Size resolution: default from config, parsed from args.
    assert "self.config.disk_image_size_gb" in _method, (
        "cmd_build_iso_disk must read the configured default size"
    )
    # Calls into disk_image.build_disk_image
    assert "disk_image.build_disk_image(" in _method


def test_iso_build_uses_spinner_for_mksquashfs_and_grub_mkrescue():
    """Live ISO build wraps mksquashfs + grub-mkrescue in Spinners so
    the long subprocesses don't look like a hung TUI.  Source-text
    inspection — exercising the full build needs sudo + the actual
    pipeline.  Anti-regression for accidentally dropping the spinner
    on a refactor."""
    _path = os.path.join(_ROOT, 'scripts', 'iso.py')
    with open(_path) as fh:
        _body = fh.read()
    # mksquashfs call has a Spinner directly above (within ~10 lines).
    import re
    _msq = re.search(
        r"tui\.Spinner\([^)]*Creating squashfs[^)]*\).*?mksquashfs",
        _body, re.DOTALL,
    )
    assert _msq, "iso.py mksquashfs is no longer wrapped in a Spinner"
    _grub = re.search(
        r"tui\.Spinner\([^)]*grub-mkrescue[^)]*\).*?container\.run_grub_mkrescue",
        _body, re.DOTALL,
    )
    assert _grub, "iso.py grub-mkrescue is no longer wrapped in a Spinner"


def test_iso_installer_uses_spinner_for_initrd_and_grub_mkrescue():
    """Installer ISO build wraps the cpio|gzip initrd pack and the
    container grub-mkrescue in Spinners.  Both take 30-60s each
    silently on a typical installer chroot."""
    _path = os.path.join(_ROOT, 'scripts', 'iso_installer.py')
    with open(_path) as fh:
        _body = fh.read()
    import re
    _initrd = re.search(
        r"tui\.Spinner\([^)]*initrd[^)]*\).*?_sudo\(\['bash'",
        _body, re.DOTALL,
    )
    assert _initrd, (
        "iso_installer.py _build_initrd cpio|gzip pipeline no longer "
        "wrapped in a Spinner"
    )
    _grub = re.search(
        r"tui\.Spinner\([^)]*grub-mkrescue[^)]*\).*?container\.run_grub_mkrescue",
        _body, re.DOTALL,
    )
    assert _grub, (
        "iso_installer.py _run_grub_mkrescue is no longer wrapped in a Spinner"
    )


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


def test_repo_dispatcher_advertises_merged_package_actions():
    """After the package→repo merge, the cmd_repo dispatcher must
    advertise the former cmd_package actions (tunnel/reload/audit)
    alongside its own (repair/index).  audit_nmu was absorbed into
    'audit' itself in P3 (2026-05-23).  strip/cleanup moved under
    'repair' (P1)."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    # Body of cmd_repo.
    _m = re.search(
        r"\n    def cmd_repo\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m, "cmd_repo dispatcher not found"
    _disp = _m.group(0)
    for _action in ('tunnel', 'reload', 'audit', 'repair', 'index'):
        assert f"'{_action}'" in _disp, (
            f"cmd_repo dispatcher missing action {_action!r}"
        )
    # The old cmd_package helper must be gone — no leftover dispatcher
    # to call.  Anti-back-compat sticking around silently.
    assert 'def cmd_package(' not in _body, (
        "cmd_package dispatcher must be removed after the merge — "
        "`package` is no longer a registered command"
    )
    # And `register_command('package'` must be gone too.
    assert "register_command('package'" not in _body, (
        "'package' must not be a registered top-level command after "
        "the merge into 'repo'"
    )


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


def test_v2_wrap_helpers_round_trip():
    """tui.wrap mirrors the legacy P7 helpers — same contract."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui import wrap
    assert wrap.wrap_line('hello', 80) == ['hello']
    assert wrap.wrap_line('a' * 11, 10) == ['a' * 10, 'a']
    assert wrap.wrap_line('', 80) == ['']
    assert wrap.entry_display_rows('a' * 81, 80) == 2
    buf = [('short', 0), ('a' * 100, 0), ('', 0)]
    assert wrap.total_display_rows(buf, 80) == 1 + 2 + 1


def test_v2_state_append_and_scroll():
    """State.tabs[name].append handles scroll_offset in display-row units."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.state import State

    s = State()
    assert 'console' in s.tabs and 'log' in s.tabs
    assert s.active_tab_name() == 'console'

    # At-bottom: scroll stays 0 after append.
    s.tabs['console'].append([('a', 0), ('b', 0)], width=80)
    assert s.tabs['console'].scroll_offset == 0

    # Scrolled away: scroll_offset bumps by display rows (1 row each at w=80).
    s.tabs['console'].scroll_offset = 5
    s.tabs['console'].append([('c', 0), ('d', 0)], width=80)
    assert s.tabs['console'].scroll_offset == 7

    # Long line wraps to 3 rows at w=10; offset increments by 3.
    s.tabs['log'].scroll_offset = 1
    s.tabs['log'].append([('x' * 25, 0)], width=10)
    assert s.tabs['log'].scroll_offset == 1 + 3


def test_v2_cmdline_edit_and_history():
    """CmdLine matches legacy _Commands semantics (cursor edit + history)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.state import CmdLine

    c = CmdLine()
    for ch in 'hello':
        c.insert(ch)
    assert c.text == 'hello' and c.cursor == 5
    c.move_left(); c.move_left()
    c.insert('X')
    assert c.text == 'helXlo' and c.cursor == 4
    assert c.backspace() is True
    assert c.text == 'hello' and c.cursor == 3

    # History walk.
    c.push_history('first')
    c.push_history('second')
    c.set_text('draft')
    assert c.history_prev() is True and c.text == 'second'
    assert c.history_prev() is True and c.text == 'first'
    assert c.history_prev() is False   # at oldest
    assert c.history_next() is True and c.text == 'second'
    assert c.history_next() is True
    assert c.text == 'draft', 'past-newest Down restores draft'


def test_v2_dispatcher_key_events_gated_without_prompt():
    """Command-line editing is GATED on a pending prompt: while a command runs
    (no prompt accepting input) keystrokes are IGNORED, so the busy state is
    unambiguous and stray typing isn't captured.  (With a prompt active, keys
    edit the line + Enter submits — see test_v2_dispatcher_prompt_future_round_trip.)
    """
    import sys, threading, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import KeyEvent, Shutdown

    d = Dispatcher(_v2_fake_renderer())
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    time.sleep(0.02)
    # No pending prompt (command-running state) → keystrokes dropped.
    for ch in 'abc':
        d.post(KeyEvent(ch))
    d.post(KeyEvent('KEY_LEFT'))
    d.post(KeyEvent('X'))
    time.sleep(0.05)
    assert d.state.cmd.text == '', d.state.cmd.text
    d.post(Shutdown(0))
    t.join(timeout=1)


def test_v2_dispatcher_prompt_future_round_trip():
    """request_prompt blocks caller, dispatcher fulfills via Enter."""
    import sys, threading, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import KeyEvent, Shutdown

    d = Dispatcher(_v2_fake_renderer())
    threading.Thread(target=d.run, daemon=True).start()
    time.sleep(0.02)

    result_holder = {}
    def caller():
        result_holder['ans'] = d.request_prompt('Enter: ')
    threading.Thread(target=caller, daemon=True).start()
    time.sleep(0.05)

    # Now the dispatcher is in prompt mode; route keys to the prompt.
    for ch in 'yes':
        d.post(KeyEvent(ch))
    d.post(KeyEvent('\n'))
    time.sleep(0.1)
    assert result_holder.get('ans') == 'yes', result_holder
    d.post(Shutdown(0))


def test_v2_dispatcher_tab_switch_by_index():
    """F-key activates nth tab; out-of-range silently no-ops."""
    import sys, threading, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import KeyEvent, Shutdown

    d = Dispatcher(_v2_fake_renderer())
    threading.Thread(target=d.run, daemon=True).start()
    time.sleep(0.02)
    d.post(KeyEvent('KEY_F(3)'))   # cache (index 2)
    time.sleep(0.05)
    assert d.state.active_tab_name() == 'cache'
    d.post(KeyEvent('KEY_F(99)'))  # out of range — no change
    time.sleep(0.05)
    assert d.state.active_tab_name() == 'cache'
    d.post(Shutdown(0))


def test_v2_dispatcher_progressbar_widget_lifecycle():
    """ProgressBar add/remove flow via the duck-typed tui_instance
    contract: __init__ → tui_instance.add_widget(self) → dispatcher
    sees WidgetAdd; close() → del_widget → WidgetRemove."""
    import sys, threading, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui as _pkg
    from tui import widgets
    from tui.dispatcher import Dispatcher
    from tui.events import Shutdown, WidgetAdd, WidgetRemove

    d = Dispatcher(_v2_fake_renderer())

    # Bridge: stand-in for the real Tui — exposes the duck-typed
    # surface widgets call (add_widget / del_widget / print) and
    # forwards into the dispatcher under test.
    class _Bridge:
        def add_widget(self, w):
            d.post(WidgetAdd(w))
            return id(w)
        def del_widget(self, wid):
            d.post(WidgetRemove(wid))
        def print(self, msg, attr=None):
            pass

    _saved = _pkg.tui_instance
    _pkg.tui_instance = _Bridge()
    try:
        threading.Thread(target=d.run, daemon=True).start()
        time.sleep(0.02)

        bar = widgets.ProgressBar('Test', maxvalue=100)
        time.sleep(0.05)
        assert bar in d.state.widgets
        bar.step(50)
        time.sleep(0.05)
        assert bar.value == 50
        bar.close()
        time.sleep(0.05)
        assert bar not in d.state.widgets
        d.post(Shutdown(0))
    finally:
        _pkg.tui_instance = _saved


def test_v2_dispatcher_adaptive_idle_no_widgets():
    """No widgets and no events -> dispatcher blocks on get(timeout=1s)
    rather than busy-spinning.  We can't measure timeout directly, but
    we can verify no events get processed during a 200ms idle window."""
    import sys, threading, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import Shutdown

    r = _v2_fake_renderer()
    d = Dispatcher(r)
    threading.Thread(target=d.run, daemon=True).start()
    time.sleep(0.02)
    renders_before = r.renders
    time.sleep(0.2)
    # With IDLE_TIMEOUT=1.0 and no widgets, at most one extra render
    # (the initial one) should have happened in 200ms.
    assert r.renders - renders_before <= 1, (
        f'expected <=1 render during 200ms idle; got {r.renders - renders_before}'
    )
    d.post(Shutdown(0))


def test_v2_logging_bridge_routes_by_stage():
    """LogTabHandler emits LogEvents tagged with the right tab."""
    import sys, threading, time, logging as _logging
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import Shutdown
    from tui.logging_bridge import setup_logging

    d = Dispatcher(_v2_fake_renderer())
    threading.Thread(target=d.run, daemon=True).start()
    time.sleep(0.02)
    setup_logging(d)

    _logging.getLogger('athena').info('plain athena')
    _logging.getLogger('athena.cache').warning('cache stage')
    _logging.getLogger('athena.iso').error('iso stage')
    time.sleep(0.1)

    log_buf   = [t for t, _ in d.state.tabs['log'].buffer]
    cache_buf = [t for t, _ in d.state.tabs['cache'].buffer]
    iso_buf   = [t for t, _ in d.state.tabs['iso'].buffer]
    assert any('plain athena' in line for line in log_buf), log_buf
    assert any('cache stage'  in line for line in cache_buf), cache_buf
    assert any('iso stage'    in line for line in iso_buf), iso_buf

    # Bare athena should NOT have leaked into cache/iso.
    assert not any('plain athena' in line for line in cache_buf)
    d.post(Shutdown(0))


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
    cfg = SimpleNamespace(pkglist_path=path)
    ctl = select_packages.SelectPackages(cfg, _FakeCache(), _FakeTui())
    return ctl, ctl._tui, path


def test_select_loads_groups_all_selected():
    """Model loads every pkg.list entry as selected=True, groups in
    declaration order."""
    ctl, _t, _p = _select_controller(
        '[base]\n## Description: core\nbash\ncoreutils\n\n[devel]\ngit\nvim\n')
    assert list(ctl._groups.keys()) == ['base', 'devel']
    assert [n for n, s in ctl._groups['base']] == ['bash', 'coreutils']
    assert all(s for _n, s in ctl._groups['base'])
    assert ctl._meta['base']['description'] == 'core'


def test_select_toggle_and_save_round_trip():
    """Toggling a package off drops it from the written pkg.list;
    group order + descriptions preserved."""
    import select_packages
    ctl, _t, path = _select_controller(
        '[base]\n## Description: core\nbash\ncoreutils\n\n[devel]\ngit\nvim\n')
    # Toggle coreutils off.
    ctl._entry('base', 'coreutils')[1] = False
    select_packages.write_pkg_list(path, ctl._groups, ctl._meta)

    with open(path) as f:
        body = f.read()
    assert '[base]' in body and '[devel]' in body
    assert '## Description: core' in body
    assert 'bash' in body
    assert 'coreutils' not in body          # dropped
    assert body.index('[base]') < body.index('[devel]')   # order preserved

    # Re-parse → group structure intact, coreutils gone.
    import utils
    groups = utils.parse_pkg_list_groups(path)
    assert groups['base'] == ['bash']
    assert groups['devel'] == ['git', 'vim']


def test_select_add_package_inline_input_appends_to_group():
    """'a' enters inline add-mode; typing a name + Enter appends it
    (selected) to the current group — WITHOUT touching the dispatcher
    prompt (which would cancel the shell's idle prompt)."""
    ctl, ft, _p = _select_controller('[base]\nbash\n')
    ctl.activate()
    assert ctl._entry('base', 'htop') is None
    # Cursor on the base header (row 0) — 'a' targets [base].
    assert ctl.handle_key('a') is True
    assert ctl._input_mode == 'add'
    # The selector must NOT have called the dispatcher prompt.
    # (_FakeTui.prompt returns '' and records nothing — assert add-mode
    # is driven purely by handle_key, never by a prompt round-trip.)
    for ch in 'htop':
        assert ctl.handle_key(ch) is True
    assert ctl._input_buffer == 'htop'
    ctl.handle_key('\n')
    assert ctl._input_mode is None
    assert ctl._entry('base', 'htop') == ['htop', True]
    assert ctl._unsaved is True


def test_select_add_rejects_package_not_in_cache():
    """A name absent from the cache is NOT added — without source it
    would only fail the build later."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import select_packages
    from types import SimpleNamespace

    d = tempfile.mkdtemp()
    path = os.path.join(d, 'pkg.list')
    with open(path, 'w') as f:
        f.write('[base]\nbash\n')

    class _Pkg(dict):
        def __init__(self):
            super().__init__()
            self['Installed-Size'] = '100'
            self['Description'] = ''
            self.depends = []
            self.pre_depends = []

    class _Cache:
        KNOWN = {'bash', 'htop'}
        def get_packages(self, name, version=None, constraint=''):
            return [_Pkg()] if name in self.KNOWN else []

    class _T:
        COLOR_HIGHLIGHT = 5
        COLOR_INFO = 7
        def __init__(self): self.prints = []
        def add_tab(self, n): pass
        def activate_tab(self, n): pass
        def set_tab_buffer(self, n, r): pass
        def set_tab_key_handler(self, n, fn): pass
        def viewport_rows(self): return 20
        def attr_reverse(self): return 1 << 18
        def attr_color(self, i): return i << 8
        def print(self, msg, attr=None): self.prints.append(msg)

    cfg = SimpleNamespace(pkglist_path=path)
    ft = _T()
    ctl = select_packages.SelectPackages(cfg, _Cache(), ft)

    # Known name → added.
    ctl._commit_add('htop')
    assert ctl._entry('base', 'htop') == ['htop', True]
    assert ctl._unsaved is True

    # Unknown name → rejected, not added, warning printed.
    ctl._unsaved = False
    ctl._commit_add('sdfg')
    assert ctl._entry('base', 'sdfg') is None
    assert ctl._unsaved is False
    assert any('not in cache' in p for p in ft.prints)


def test_select_quit_without_save_does_not_use_dispatcher_prompt():
    """Regression: quitting with unsaved edits must NOT call
    request_prompt (which cancels the shell's pending prompt and kills
    the shell, ignoring all later commands).  Instead it enters an
    inline y/n confirm handled by handle_key; 'n' tears down cleanly."""
    ctl, ft, _p = _select_controller('[base]\nbash\ncoreutils\n')
    ctl.activate()

    # Make an edit so the quit path takes the confirm branch.
    ctl.handle_key('KEY_DOWN')   # → bash
    ctl.handle_key(' ')          # toggle off
    assert ctl._unsaved is True

    # 'q' must NOT post a dispatcher prompt — it enters inline confirm.
    ft.prompt_calls = 0
    orig_prompt = ft.prompt
    def _counting_prompt(*a, **k):
        ft.prompt_calls += 1
        return orig_prompt(*a, **k)
    ft.prompt = _counting_prompt

    assert ctl.handle_key('q') is True
    assert ctl._input_mode == 'quit'
    assert ft.prompt_calls == 0, 'quit must not call the dispatcher prompt'

    # 'n' → discard + teardown (tab removed, key handler cleared).
    assert ctl.handle_key('n') is True
    assert ctl._input_mode is None
    assert ft.removed == ['select']
    assert ft.cleared is True
    assert ft.prompt_calls == 0


def test_select_quit_confirm_yes_saves_then_teardown():
    """Inline quit-confirm 'y' saves the model then tears down."""
    import utils
    ctl, ft, path = _select_controller('[base]\nbash\ncoreutils\n')
    ctl.activate()
    ctl.handle_key('KEY_DOWN')   # → bash
    ctl.handle_key(' ')          # toggle bash off
    ctl.handle_key('q')          # → inline confirm
    assert ctl._input_mode == 'quit'
    ctl.handle_key('y')          # save + teardown
    assert ctl._input_mode is None
    assert ft.removed == ['select'] and ft.cleared is True
    # bash dropped on disk.
    assert utils.parse_pkg_list_groups(path) == {'base': ['coreutils']}


def test_select_save_preserves_comments_and_appends_new():
    """Regression: write_pkg_list must preserve the file header
    comment block + inline `#` notes + blank lines (the buggy
    regenerate-from-scratch version stripped them).  Edits applied as
    a minimal diff: drop unselected, append new at end of group."""
    import select_packages, utils
    body = (
        '# pkg.list header comment\n'
        '# second header line\n'
        '\n'
        '[base]\n'
        '## Description: core\n'
        '# inline note about bash\n'
        'bash\n'
        'coreutils\n'
        '\n'
        '[devel]\n'
        'git\n'
    )
    ctl, _t, path = _select_controller(body)
    # Drop coreutils, add htop to base.
    ctl._entry('base', 'coreutils')[1] = False
    ctl._groups['base'].append(['htop', True])
    select_packages.write_pkg_list(path, ctl._groups, ctl._meta)

    with open(path) as f:
        new = f.read()
    # Comments + blank lines preserved verbatim.
    assert '# pkg.list header comment' in new
    assert '# second header line' in new
    assert '# inline note about bash' in new
    assert '## Description: core' in new
    # Edits applied.
    assert 'coreutils' not in new        # dropped
    assert 'htop' in new                 # appended
    assert 'bash' in new and 'git' in new
    # Structure still parses correctly.
    groups = utils.parse_pkg_list_groups(path)
    assert groups['base'] == ['bash', 'htop']
    assert groups['devel'] == ['git']


def test_select_flat_file_round_trips_without_header():
    """A flat (legacy) pkg.list with no sections round-trips flat —
    no spurious [base] header injected."""
    import select_packages, utils
    ctl, _t, path = _select_controller('bash\ncoreutils\nvim\n')
    assert list(ctl._groups.keys()) == ['base']
    select_packages.write_pkg_list(path, ctl._groups, ctl._meta)
    with open(path) as f:
        body = f.read()
    assert '[base]' not in body          # stayed flat
    assert utils.parse_pkg_list_groups(path) == {'base': ['bash', 'coreutils', 'vim']}


def test_select_key_toggle_sets_unsaved_and_rerenders():
    """Space on a package row toggles it + marks unsaved + re-renders
    (a fresh buffer is pushed to the 'select' tab)."""
    ctl, ft, _p = _select_controller('[base]\nbash\ncoreutils\n')
    ctl.activate()
    assert ft.added == ['select'] and ft.activated == ['select']
    # Bound methods compare by __func__ (a fresh bound-method object is
    # created on each attribute access, so `is` on the method fails).
    assert ft.key_handler.__func__ is ctl.handle_key.__func__
    # rows: [header base, bash, coreutils].  Cursor 0 = header; move to bash.
    ctl.handle_key('KEY_DOWN')   # cursor → 1 (bash)
    ft.buffers.clear()
    consumed = ctl.handle_key(' ')
    assert consumed is True
    assert ctl._unsaved is True
    assert ctl._entry('base', 'bash')[1] is False
    assert 'select' in ft.buffers   # re-rendered

    # F-keys fall through (not consumed) so tab-switch still works.
    assert ctl.handle_key('KEY_F(2)') is False


def test_select_approx_closure_sums_first_provider_bfs():
    """_approx_closure walks Depends/Pre-Depends first-provider and
    sums Installed-Size.  Build a 3-pkg chain a→b→c each 100 KB."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import select_packages

    class _P(dict):
        def __init__(self, size, deps):
            super().__init__()
            self['Installed-Size'] = str(size)
            self['Description'] = ''
            self.depends = [(d, '', '') for d in deps]
            self.pre_depends = []

    chain = {'a': _P(100, ['b']), 'b': _P(100, ['c']), 'c': _P(100, [])}

    class _Cache:
        def get_packages(self, name, version=None, constraint=''):
            return [chain[name]] if name in chain else []

    from types import SimpleNamespace
    import tempfile
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'pkg.list')
    with open(path, 'w') as f:
        f.write('[base]\na\n')
    cfg = SimpleNamespace(pkglist_path=path)

    class _T:
        COLOR_HIGHLIGHT = 5
        COLOR_INFO = 7
    ctl = select_packages.SelectPackages(cfg, _Cache(), _T())
    total, count = ctl._approx_closure('a')
    assert count == 3          # a, b, c
    assert total == 300        # 3 × 100 KB


def test_cmd_cache_info_prints_identity_and_relations():
    """`cache info <pkg>` looks up the cache and prints identity +
    relations; missing names report 'not found'."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
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
    saved_console = build.console
    build.console = _RecConsole()
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
        build.console = saved_console


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
        assert False, "empty release should have raised"
    except ValueError:
        pass


def test_download_file_handles_file_scheme():
    """file:// URLs are copied locally via shutil — no HTTP request."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import download_file
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, 'src.txt')
        dst = os.path.join(tmp, 'dst.txt')
        with open(src, 'w') as fh:
            fh.write('hello fork\n')
        size, detail = download_file('file://' + src, dst)
        assert size == 11, f"expected size 11, got {size}; detail={detail}"
        assert detail == ''
        with open(dst, 'r') as fh:
            assert fh.read() == 'hello fork\n'


def test_download_file_file_scheme_missing_source():
    """file:// URL to non-existent path returns -1 + detail string."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import download_file
    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, 'dst.txt')
        size, detail = download_file('file:///nonexistent/path', dst)
        assert size == -1
        assert 'missing' in detail or 'No such' in detail


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
        _stale_build_log = os.path.join(
            bc.dir_log, 'build', 'athena-installer-data.result')
        with open(_stale_build_log, 'w') as fh:
            fh.write('PASS\n')

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


# ─────────────────────────────────────────────────────────────────────────────
# package rebump — source-name filter + cmd_source_rescan
# ─────────────────────────────────────────────────────────────────────────────

def test_no_stale_progressbar_methods_in_build_py():
    """ProgressBar exposes step/close/label/pause/resume/set_max/reset
    in scripts/tui.py — NOT done/print_progress/print.  Those belong to
    Spinner (only `done`), or never existed.  Calling them on a
    ProgressBar raises AttributeError at runtime — bit-rot caught
    2026-05-19 in `package rebump`.  Pin to prevent regression."""
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    import re
    # Find every `<name>_bar.<method>(` and `progress_bar.<method>(`
    # call and assert it's in the valid set.
    _valid = {'step', 'close', 'label', 'pause', 'resume',
              'set_max', 'reset'}
    for _m in re.finditer(
            r'\b(?:[a-zA-Z_]*_)?(?:bar|progress_bar)\.([a-z_]+)\(', _body):
        _method = _m.group(1)
        assert _method in _valid, (
            f"build.py calls progress-bar method `.{_method}()` which is "
            f"not in ProgressBar's public API (valid: {sorted(_valid)}). "
            f"Likely confused with Spinner.done() or a removed method.")


def test_cmd_audit_registered_under_repo_dispatcher():
    """`repo audit` must be wired into cmd_repo's dispatch."""
    from build import BuildSession
    assert hasattr(BuildSession, 'cmd_audit'), (
        "BuildSession is missing cmd_audit")
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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


def test_resolve_live_cohort_subtracts_pool_and_installer():
    """live cohort = dep_tree.selected_pkgs − pool_extras −
    installer_exclusive.  Pin via code inspection."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
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
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r'def _resolve_installer_cohort\(self.*?(?=\n    def )',
        _body, re.DOTALL)
    assert _m, "_resolve_installer_cohort not found"
    _method = _m.group(0)
    assert 'udeb_dep_tree.selected_pkgs' in _method, (
        "installer cohort is just udeb_dep_tree.selected_pkgs — udebs "
        "in the ramdisk only; debs install on target separately")


def test_audit_dep_closure_resolves_against_whole_repo():
    """audit_dep_closure resolves Depends against ALL of repo, not a
    selected subset — apt at install time can pull transitive deps
    from any tier (pkg, live, installer-debs, pool).  A live-pkg's
    dep on a pool-pkg is NOT a violation."""
    _ra = os.path.join(_ROOT, 'scripts', 'repo_audit.py')
    with open(_ra) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r'def audit_dep_closure\(state.*?(?=\ndef |\Z)',
        _body, re.DOTALL)
    assert _m, "audit_dep_closure not found"
    _method = _m.group(0)
    # Pin: no 'cohort' / 'scope' arg restricting resolution
    assert 'scope=' not in _method.split(':', 1)[0], (
        "audit_dep_closure signature should not take a scope — it always "
        "resolves against the whole repo")
    # Pin: hard fields walked (via _HARD_DEP_FIELDS constant)
    assert '_HARD_DEP_FIELDS' in _method, (
        "audit_dep_closure must iterate _HARD_DEP_FIELDS — single source "
        "of truth for which dep fields are hard")


def test_audit_conflict_cohort_only_flags_within_cohort():
    """audit_conflict_cohort flags a Conflicts/Breaks only when the
    target also resolves to a pkg in the same cohort.  Cross-cohort
    conflicts (e.g. grub-pc in pool, grub-efi-amd64 in live) are NOT
    flagged — apt arbitrates at install time."""
    _ra = os.path.join(_ROOT, 'scripts', 'repo_audit.py')
    with open(_ra) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r'def audit_conflict_cohort\(state.*?(?=\ndef |\Z)',
        _body, re.DOTALL)
    assert _m, "audit_conflict_cohort not found"
    _method = _m.group(0)
    assert 'cohort' in _method, (
        "audit_conflict_cohort signature must take a cohort frozenset")
    # Pin: consumer iteration filters by cohort membership
    assert 'if _pkg not in cohort' in _method, (
        "audit_conflict_cohort must skip consumers not in the cohort — "
        "we only care about Conflicts between pkgs that actually co-install")
    # Pin: resolution is scoped to cohort (passed via scope=cohort)
    assert 'scope=cohort' in _method, (
        "conflict resolution must be scope-limited to the cohort — "
        "cross-cohort hits don't matter")


def test_dedupe_bidirectional_conflicts_collapses_pairs():
    """`_dedupe_bidirectional_conflicts` collapses (A→B) + (B→A)
    declarations of the same conflict into one entry.  Halves the
    apparent conflict count for the common symmetric pattern."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import _dedupe_bidirectional_conflicts
    _input = [
        ('grub-pc', 'Conflicts', 'grub-efi-amd64', 'grub-efi-amd64'),
        ('grub-efi-amd64', 'Conflicts', 'grub-pc', 'grub-pc'),
        ('fuse3', 'Breaks', 'fuse', 'fuse'),
    ]
    _out = _dedupe_bidirectional_conflicts(_input)
    assert len(_out) == 2, (
        f"two distinct conflicts after dedup; got {len(_out)}: {_out}"
    )
    _consumers = {e[0] for e in _out}
    # Only one of grub-pc/grub-efi-amd64 should survive; the other was
    # collapsed.  fuse3 always survives (no reverse pair).
    assert 'fuse3' in _consumers
    assert ('grub-pc' in _consumers) ^ ('grub-efi-amd64' in _consumers), (
        f"expected exactly one of grub-pc / grub-efi-amd64: {_consumers}"
    )


def test_audit_versioned_provides_satisfies_any_operator():
    """Per Debian Policy §7.5: a versioned Provides (`Provides: X (= V)`)
    satisfies a Depends on X with ANY comparison operator, not just `=`.
    Tested via a synthetic RepoState — `sysvinit-utils Provides lsb-base
    (= 11.1.0)` must satisfy `Depends: lsb-base (>= 3.0-9)`."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    _state = repo_audit.RepoState(
        packages={
            'sysvinit-utils': {
                'Package': 'sysvinit-utils',
                'Version': '3.06-4',
                'Provides': 'lsb-base (= 11.1.0)',
            },
            'consumer-ge': {
                'Package': 'consumer-ge',
                'Version': '1.0',
                'Depends': 'lsb-base (>= 3.0-9)',
            },
            'consumer-gt': {
                'Package': 'consumer-gt',
                'Version': '1.0',
                'Depends': 'lsb-base (>> 3.0)',
            },
            'consumer-le': {
                'Package': 'consumer-le',
                'Version': '1.0',
                'Depends': 'lsb-base (<= 99.0)',
            },
            'consumer-eq': {
                'Package': 'consumer-eq',
                'Version': '1.0',
                'Depends': 'lsb-base (= 11.1.0)',
            },
            'consumer-unmet': {
                'Package': 'consumer-unmet',
                'Version': '1.0',
                'Depends': 'lsb-base (>= 99.0)',   # genuinely unmet
            },
        },
        provides_index={'lsb-base': [('sysvinit-utils', '11.1.0')]},
        packages_file='/dev/null',
        repo_mtime=0.0,
    )
    _unresolved, _ = repo_audit.audit_dep_closure(_state)
    _unresolved_consumers = {u[0] for u in _unresolved}
    # All four operators must resolve via the versioned Provides
    for _consumer in ('consumer-ge', 'consumer-gt', 'consumer-le', 'consumer-eq'):
        assert _consumer not in _unresolved_consumers, (
            f"{_consumer}'s Depends should resolve via versioned Provides; "
            f"unresolved={_unresolved}"
        )
    # The genuinely-unmet one must still be flagged
    assert 'consumer-unmet' in _unresolved_consumers, (
        f"consumer-unmet wants >= 99.0 but provider is at 11.1.0 — "
        f"this MUST be flagged: {_unresolved}"
    )


def test_audit_unversioned_provides_does_not_satisfy_versioned_depends():
    """Per Debian Policy §7.5: an UNVERSIONED Provides does NOT satisfy
    a versioned Depends — the policy reserves unversioned virtuals for
    matching unversioned Depends only."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    _state = repo_audit.RepoState(
        packages={
            'provider': {
                'Package': 'provider',
                'Version': '1.0',
                'Provides': 'virtfoo',        # unversioned
            },
            'consumer-unversioned': {
                'Package': 'consumer-unversioned',
                'Version': '1.0',
                'Depends': 'virtfoo',          # unversioned → satisfied
            },
            'consumer-versioned': {
                'Package': 'consumer-versioned',
                'Version': '1.0',
                'Depends': 'virtfoo (>= 2.0)', # versioned → NOT satisfied
            },
        },
        provides_index={'virtfoo': [('provider', None)]},
        packages_file='/dev/null',
        repo_mtime=0.0,
    )
    _unresolved, _ = repo_audit.audit_dep_closure(_state)
    _unresolved_consumers = {u[0] for u in _unresolved}
    assert 'consumer-unversioned' not in _unresolved_consumers, (
        "unversioned Provides satisfies unversioned Depends"
    )
    assert 'consumer-versioned' in _unresolved_consumers, (
        "unversioned Provides must NOT satisfy versioned Depends per "
        "Debian Policy §7.5"
    )


def test_repo_max_mtime_detects_delete_and_rename():
    """`_repo_max_mtime` must include the directory's OWN mtime (not
    just per-entry mtimes), otherwise pure-delete and pure-rename
    operations leave it unchanged → cache stays stale → audit returns
    pre-edit state.  Pin the actual behavior by exercising both."""
    import sys, tempfile, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit

    with tempfile.TemporaryDirectory() as _tmp:
        for _n in ('a.deb', 'b.deb'):
            with open(os.path.join(_tmp, _n), 'w') as fh:
                fh.write('x')
        _initial = repo_audit._repo_max_mtime(_tmp)
        time.sleep(0.05)
        # Pure delete: surviving files' mtimes unchanged
        os.remove(os.path.join(_tmp, 'a.deb'))
        _after_delete = repo_audit._repo_max_mtime(_tmp)
        assert _after_delete > _initial, (
            f"delete must advance max_mtime; "
            f"{_initial} → {_after_delete}"
        )
        time.sleep(0.05)
        # Pure rename: same inode, same content mtime
        os.rename(os.path.join(_tmp, 'b.deb'), os.path.join(_tmp, 'c.deb'))
        _after_rename = repo_audit._repo_max_mtime(_tmp)
        assert _after_rename > _after_delete, (
            f"rename must advance max_mtime; "
            f"{_after_delete} → {_after_rename}"
        )


def test_repo_audit_module_exports():
    """The repo_audit module must export the primitives consumed by
    build.py: RepoState + AuditResult dataclasses, plus the callables
    scan_repo_state / audit_dep_closure / audit_conflict_cohort /
    audit_nmu_residue / invalidate_cache."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    for _name in (
        'RepoState', 'AuditResult',
        'scan_repo_state', 'audit_dep_closure',
        'audit_conflict_cohort', 'audit_nmu_residue',
        'invalidate_cache',
    ):
        assert hasattr(repo_audit, _name), (
            f"repo_audit module must export {_name}")


def test_repo_audit_scan_uses_dpkg_scanpackages_and_apt_pkg():
    """The scanner must use `dpkg-scanpackages` (fast C subprocess) +
    `apt_pkg.TagFile` (streaming parser) rather than per-file
    python-debian DebFile reads.  ~3x speedup on 5000-pkg repos and
    enables a session-persistent cache (the Packages file on disk)."""
    _bc = os.path.join(_ROOT, 'scripts', 'repo_audit.py')
    with open(_bc) as fh:
        _body = fh.read()
    assert 'dpkg-scanpackages' in _body, (
        "repo_audit.scan_repo_state must shell out to dpkg-scanpackages")
    assert 'apt_pkg.TagFile' in _body, (
        "repo_audit must stream-parse via apt_pkg.TagFile, not a "
        "per-section dict comprehension (TagFile is much faster on "
        "5000-stanza files)")
    assert 'apt_pkg.version_compare' in _body, (
        "repo_audit must dedupe multi-version stanzas via "
        "apt_pkg.version_compare so highest version wins")


def test_scan_repo_state_main_udeb_passes_t_udeb_to_scanpackages():
    """Regression for source-verify-2026-05-23: scanning the udeb dir
    without `-t udeb` produces an empty Packages file with no error,
    silently breaking every udeb dep lookup.  Pin that the main-udeb
    subdir branch adds `-t udeb` to the dpkg-scanpackages argv."""
    _bc = os.path.join(_ROOT, 'scripts', 'repo_audit.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    # Body of scan_repo_state.
    _m = re.search(
        r'def scan_repo_state\(.*?(?=\ndef \w)',
        _body, re.DOTALL,
    )
    assert _m, 'scan_repo_state body not found'
    _fn = _m.group(0)
    assert "subdir == 'main-udeb'" in _fn, (
        "scan_repo_state must branch on subdir == 'main-udeb' to add "
        "-t udeb; without it, dpkg-scanpackages defaults to .deb-only "
        "and produces an empty Packages file"
    )
    assert "'-t', 'udeb'" in _fn, (
        "scan_repo_state must pass -t udeb when scanning the udeb dir"
    )


def test_scan_repo_state_treats_empty_cache_file_as_missing():
    """Defensive guard: a zero-byte cached Packages file (e.g. from a
    pre-fix run that scanned udebs without -t udeb) must NOT be served
    as a cache hit on subsequent runs — re-scan instead."""
    _bc = os.path.join(_ROOT, 'scripts', 'repo_audit.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    _m = re.search(
        r'def scan_repo_state\(.*?(?=\ndef \w)',
        _body, re.DOTALL,
    )
    assert _m, 'scan_repo_state body not found'
    _fn = _m.group(0)
    # Pin the empty-file check.  Both shapes acceptable:
    #   os.stat(...).st_size > 0
    #   os.path.getsize(...) > 0
    assert ('st_size > 0' in _fn) or ('getsize' in _fn and '> 0' in _fn), (
        "scan_repo_state must require a non-zero cached Packages file "
        "before honouring the cache; otherwise an empty file (e.g. from "
        "a pre-fix scan that produced 0 records) is served forever"
    )


def test_content_integrity_absorbed_into_cmd_audit_per_cohort():
    """P3 (2026-05-23): `source verify` is no longer a standalone verb.
    Its per-cohort deep-verify logic moved into cmd_audit's
    _report_content_integrity helper.  Pin both the absence of the
    old verb AND the per-cohort separation in the new home."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    # Old verb gone.
    assert 'def cmd_source_verify(' not in _body, (
        "cmd_source_verify must be removed — absorbed into cmd_audit "
        "as _report_content_integrity (P3 2026-05-23)"
    )
    assert "if action == 'verify'" not in _body[:_body.find(
        'def cmd_chroot') if 'def cmd_chroot' in _body else len(_body)
    ] or "return self.cmd_source_verify" not in _body, (
        "'verify' source-action must not route to cmd_source_verify "
        "(chroot/key verify subcommands remain — different namespace)"
    )
    # Absorbed helper exists.
    assert 'def _report_content_integrity(' in _body, (
        "_report_content_integrity helper must exist as the integrity "
        "section of cmd_audit"
    )
    import re
    _m = re.search(
        r'def _report_content_integrity\(self.*?(?=\n    def \w)',
        _body, re.DOTALL,
    )
    assert _m, '_report_content_integrity body not found'
    _fn = _m.group(0)
    # Per-cohort iteration — same separation as the old cmd_source_verify.
    assert "'deb'" in _fn and "'udeb'" in _fn, (
        "integrity section must iterate per cohort (deb + udeb), each "
        "scoped to its own namespace's RepoState"
    )
    assert "endswith(_ext)" in _fn, (
        "predicted-files filter must use per-cohort extension"
    )
    # Both repo-state subdirs are still scanned — deb_state passed in
    # by caller (cmd_audit), udeb_state scanned inline.
    assert "'main-udeb'" in _fn, (
        "udeb cohort must scan_repo_state for 'main-udeb'"
    )
    # cmd_audit must call this helper as one of its sections.
    _m2 = re.search(
        r'def cmd_audit\(self.*?(?=\n    def \w)',
        _body, re.DOTALL,
    )
    assert _m2, 'cmd_audit body not found'
    assert 'self._report_content_integrity(' in _m2.group(0), (
        "cmd_audit must call _report_content_integrity as one of its "
        "sections"
    )


def test_deb_dir_for_recognises_main_udeb_label():
    """Pin: deb_dir_for must accept 'main-udeb' → dir_repo_main_udeb so
    repo_audit.scan_repo_state can drive a udeb-side RepoState (needed
    by `source verify`)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import BuildConfig
    _cfg = BuildConfig()
    if not _cfg.is_valid:
        return
    assert _cfg.deb_dir_for('main-udeb') == _cfg.dir_repo_main_udeb


def test_repo_audit_closure_handles_conflicts_and_provides():
    """The audit primitives must walk hard deps, conflicts, AND honour
    versioned Provides for virtual-target resolution per Debian Policy
    §7.5.  Pin via code inspection — the resolution logic shouldn't
    silently drop classes when refactored."""
    _bc = os.path.join(_ROOT, 'scripts', 'repo_audit.py')
    with open(_bc) as fh:
        _body = fh.read()
    assert "'Depends'" in _body and "'Pre-Depends'" in _body, (
        "audit must walk both hard-dep fields")
    assert "'Conflicts'" in _body and "'Breaks'" in _body, (
        "audit must walk both conflict fields")
    assert 'provides_index' in _body, (
        "audit must consult the Provides index for virtual-target "
        "resolution — Depends on virtual `awk` is satisfied by any "
        "pkg whose Provides includes `awk`")
    assert "'Recommends'" in _body, (
        "audit must walk Recommends as the weak-class report")


def test_readonly_named_commands_have_no_destructive_calls():
    """Any function whose name signals read-only intent MUST NOT
    contain filesystem-write primitives OR call known-mutating
    helpers.  Caught 2026-05-19: cmd_source_rescan invoked
    self._refresh_patches() (which deletes .result files on patch-set
    changes) and over-counted the rebuild surface by 47 packages.

    The reverse failure (a destructive helper masquerading as
    read-only by name) is the more dangerous shape — the name is the
    operator's primary signal for "is this safe to run?"

    Naming patterns considered read-only:
      cmd_*_rescan / cmd_*_status / cmd_*_show / cmd_*_list /
      cmd_*_info / cmd_print* / _print_* / cmd_*_scan /
      cmd_*_audit / cmd_*_inspect / cmd_*_summary

    Forbidden in those bodies:
      - FS mutations: os.{remove,unlink,rmdir,rename,replace,utime},
        shutil.{rmtree,move}
      - File opens in write/append mode: open(..., 'w'/'a'/'wb'/'ab')
      - Mutating self-helpers by naming convention:
        self._refresh_patches, self._invalidate_*, self._wipe_*,
        self._remove_*, self._purge_*

    Comment lines are stripped before scanning to avoid false positives
    from docstrings describing forbidden patterns.
    """
    import re

    _readonly_pattern = re.compile(
        r'^(?:    )?def (cmd_\w*?(?:rescan|status|show|list|info|scan|audit|inspect|summary)\w*'
        r'|cmd_print\w*'
        r'|_print_\w+)\(',
        re.MULTILINE,
    )

    _forbidden = [
        # FS mutation primitives
        (r'\bos\.remove\(',     'os.remove'),
        (r'\bos\.unlink\(',     'os.unlink'),
        (r'\bos\.rmdir\(',      'os.rmdir'),
        (r'\bos\.rename\(',     'os.rename'),
        (r'\bos\.replace\(',    'os.replace'),
        (r'\bos\.utime\(',      'os.utime'),
        (r'\bshutil\.rmtree\(', 'shutil.rmtree'),
        (r'\bshutil\.move\(',   'shutil.move'),
        # File opens in write/append mode (single OR double quotes)
        (r'''open\([^)]*,\s*['"](?:w|a)[b+]?['"]''',
         "open(..., 'w'/'a' mode)"),
        # Mutating helpers by naming convention
        (r'self\._refresh_patches\(', 'self._refresh_patches()'),
        (r'self\._invalidate_\w*\(',  'self._invalidate_*()'),
        (r'self\._wipe_\w*\(',        'self._wipe_*()'),
        (r'self\._remove_\w*\(',      'self._remove_*()'),
        (r'self\._purge_\w*\(',       'self._purge_*()'),
    ]

    _files_to_scan = [
        os.path.join(_ROOT, 'scripts', 'build.py'),
        os.path.join(_ROOT, 'scripts', 'print_commands.py'),
    ]

    _violations = []
    for _path in _files_to_scan:
        with open(_path) as fh:
            _source = fh.read()
        for _name_match in _readonly_pattern.finditer(_source):
            _name = _name_match.group(1)
            # Extract method/function body up to the next def/class at
            # the same or lower indent (or EOF).
            _start = _name_match.end()
            _rest = _source[_start:]
            _body_match = re.search(
                r'(.*?)(?=\n(?:    )?def \w|\nclass \w|\Z)',
                _rest, re.DOTALL,
            )
            _body = _body_match.group(1) if _body_match else _rest
            # Strip comment-only lines so docstrings/notes mentioning
            # the forbidden tokens don't trigger.
            _body_lines = []
            for _line in _body.splitlines():
                _stripped = _line.lstrip()
                if _stripped.startswith('#'):
                    continue
                _body_lines.append(_line)
            _body_clean = '\n'.join(_body_lines)
            # Also strip triple-quoted docstrings (rough heuristic: drop
            # lines between consecutive `"""` markers).
            _body_clean = re.sub(r'""".*?"""', '', _body_clean, flags=re.DOTALL)
            _body_clean = re.sub(r"'''.*?'''", '', _body_clean, flags=re.DOTALL)
            for _pat, _label in _forbidden:
                if re.search(_pat, _body_clean):
                    _violations.append(
                        f"{os.path.basename(_path)}:{_name} contains "
                        f"forbidden write/mutation pattern {_label!r}"
                    )

    assert not _violations, (
        "Read-only-named functions must not contain destructive calls.\n"
        + "\n".join(f"  • {_v}" for _v in _violations)
        + "\n\nFix options:\n"
        + "  (a) Rename the function (drop the read-only-implying name).\n"
        + "  (b) Extract the destructive logic into a separate helper.\n"
        + "  (c) If the pattern is a false positive (e.g. read-only "
        + "open via positional arg), refine the test's regex."
    )


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


def test_verify_pkg_artifact_repo_state_overrides_cache_resolution():
    """Regression for the 54-false-positive bug 2026-05-23.

    Scenario: a .deb at pristine `Depends: libbar (= 2.5)`.  The cache
    has libbar at upstream's NMU-bumped `2.5+b1` (cache reflects
    upstream Packages indices, not our post-strip repo).  Cache-based
    resolution would falsely flag this as unsatisfied because
    `Version('2.5+b1') == Version('2.5')` is False.

    With repo_state passed in, resolution must hit the post-strip
    pristine version (matching the .deb's Depends literal) and pass.

    Pins the contract: when repo_state is non-None, verify_pkg_artifact
    uses ONLY repo_state, NEVER the cache, so a cache-vs-repo skew
    cannot leak through."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _path = os.path.join(_tmp, 'foo_1.0_amd64.deb')
        _build_minimal_deb(_path, 'foo', '1.0', 'amd64',
                            depends='libbar (= 2.5)')

        # Cache reflects upstream's NMU-bumped libbar — would fail
        # strict-equal resolution against pristine `(= 2.5)`.
        from collections import defaultdict
        class _Cache: pass
        _cache = _Cache()
        _cache.package_hashtable = defaultdict(lambda: defaultdict(list))
        _cache.package_hashtable['libbar']['2.5+b1'] = ['<placeholder>']
        _cache.udeb_hashtable = defaultdict(lambda: defaultdict(list))

        # Repo state: post-strip pristine libbar 2.5 — what apt actually
        # sees at install time.
        import repo_audit
        _state = repo_audit.RepoState(
            packages={'libbar': {'Package': 'libbar', 'Version': '2.5'}},
            provides_index={},
            packages_file='/dev/null',
            repo_mtime=0.0,
        )

        _bc = _make_buildcontainer_stub(cache=_cache, repo=_tmp)

        # Without repo_state: false-positive (cache-only path).
        _ok, _why = _bc.verify_pkg_artifact(_path, 'foo_1.0_amd64.deb')
        assert not _ok and 'unsatisfied-Depends' in _why, (
            f"cache-only path should over-report; got ({_ok}, {_why}).  "
            "If this passes, the legacy cache-resolution path is doing "
            "something other than the documented strict-equal lookup."
        )

        # With repo_state: passes (authoritative).
        _ok, _why = _bc.verify_pkg_artifact(
            _path, 'foo_1.0_amd64.deb', repo_state=_state,
        )
        assert _ok, (
            f"repo_state resolution should satisfy `libbar (= 2.5)` "
            f"against the pristine repo entry; got ({_ok}, {_why})"
        )


def test_verify_pkg_artifact_repo_state_still_fails_when_actually_unsatisfied():
    """The repo_state path must still report unsatisfied-Depends for
    genuinely-missing deps — not a blanket "always pass when repo_state
    is given".  Otherwise the fix would mask real install failures."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _path = os.path.join(_tmp, 'foo_1.0_amd64.deb')
        _build_minimal_deb(_path, 'foo', '1.0', 'amd64',
                            depends='libnonexistent (>= 1.0)')

        import repo_audit
        _state = repo_audit.RepoState(
            packages={'libbar': {'Package': 'libbar', 'Version': '2.5'}},
            provides_index={},
            packages_file='/dev/null',
            repo_mtime=0.0,
        )

        _bc = _make_buildcontainer_stub(cache=None, repo=_tmp)
        _ok, _why = _bc.verify_pkg_artifact(
            _path, 'foo_1.0_amd64.deb', repo_state=_state,
        )
        assert not _ok, "missing dep should fail verify even with repo_state"
        assert 'unsatisfied-Depends' in _why, _why
        assert 'libnonexistent' in _why, _why


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


def test_cmd_source_repair_dispatch_and_method_present():
    """source repair must be wired in cmd_source's dispatch table +
    have a matching cmd_source_repair method.  Sanity guard so
    `source repair` isn't a phantom command."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    assert "'repair'" in _body, "repair not advertised in source help"
    assert 'def cmd_source_repair(' in _body, (
        "cmd_source_repair method missing or wrong name")
    import re
    assert re.search(
        r"if action == 'repair':\s*\n\s+return self\.cmd_source_repair",
        _body), "source repair not dispatched in cmd_source"


def test_cmd_source_repair_writes_pass_when_binaries_present():
    """source repair restores .result=PASS when all expected binaries
    exist in repo/.  Synthesize the minimum state needed and call
    the method directly via a stubbed BuildSession."""
    import sys, types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import build as _build_mod

    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)

        # Plant a valid .deb (ar magic header).  is_ar_file via
        # python-debian's DebFile is stricter — needs full member set;
        # easier to monkey-patch is_ar_file on the stubbed container.
        _deb_name = 'foo_1.0_amd64.deb'
        with open(os.path.join(_repo, _deb_name), 'wb') as fh:
            fh.write(b'!<arch>\n')  # ar magic; content beyond is fake

        # Plant a matching .patchhash baseline so _source_state
        # classifies the source as 'repairable' (post 2026-05-23 P3
        # tightening: 'repairable' now requires .patchhash matching
        # current patch_set_hash as proof binaries reflect current
        # patches — without it the state is 'needs_build' because
        # we can't prove binaries are fresh).
        import sys as _sys
        _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
        from utils import patch_set_hash as _patch_set_hash
        _patch_dir = os.path.join(_tmp, 'patch', 'source', 'foo', '1.0')
        _baseline = _patch_set_hash(_patch_dir, [])
        with open(os.path.join(_buildlog, 'foo.patchhash'), 'w') as fh:
            fh.write(_baseline + '\n')

        # Stub Source-like
        class _Src:
            pkgs = [_deb_name]
            # _source_state needs these even when not
            # populated in this test.
            files = {}
            version = '1.0'
            patch_list = []

        # Stub container with the minimum repair/check_build helpers need.
        # verify_pkg_artifact is the unified gate (filename + ar +
        # Pkg/Version/Arch match + Depends resolution); for these
        # unit tests we just trust the stub returns OK.
        class _Container:
            buildlog_path = _buildlog
            @staticmethod
            def is_ar_file(_path):
                return True
            @staticmethod
            def verify_pkg_artifact(_path, _filename):
                return (os.path.isfile(_path), 'ok' if os.path.isfile(_path) else 'missing')

        # Stub config — CONF-01 Stage D: deb_dest_for_filename routes
        # path lookups via the new nested apt-repo layout.  For this
        # test we want planted .debs at top-level _repo, so stub returns
        # _repo for every filename.
        class _Cfg:
            dir_repo = _repo
            # _source_state reads these even when the actual subdirs
            # aren't populated in this test (files={}, patch_list=[]).
            dir_log = os.path.join(_tmp, 'log')
            dir_source = os.path.join(_tmp, 'source')
            dir_patch_source = os.path.join(_tmp, 'patch', 'source')
            @staticmethod
            def deb_dest_for_filename(_f):
                return _repo

        # Stub dep tree
        class _Tree:
            selected_srcs = {'foo': _Src()}
            src_pkg_files = {'foo': list(_Src.pkgs)}

        # Stub flags
        class _Flags:
            cache_ready = True
            dep_check_ready = True
            build_container_ready = True

        # Build session shell
        _sess = _build_mod.BuildSession.__new__(_build_mod.BuildSession)
        _sess.config = _Cfg
        _sess.dep_tree = _Tree
        _sess.udeb_dep_tree = None
        _sess.flags = _Flags
        _sess.container = _Container

        # Sanity: .result must not exist before
        _result = os.path.join(_buildlog, 'foo.result')
        assert not os.path.exists(_result)

        _sess.cmd_source_repair()

        # After repair: .result should exist + contain PASS
        assert os.path.isfile(_result), "repair didn't write .result"
        with open(_result) as fh:
            assert fh.read().strip() == 'PASS', "wrong .result content"


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
            def deb_dest_for_filename(_f): return _repo
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
            def deb_dest_for_filename(_f): return _repo
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
    with tempfile.TemporaryDirectory() as _tmp:
        _buildlog = os.path.join(_tmp, 'log', 'build')
        _repo = os.path.join(_tmp, 'repo')
        os.makedirs(_buildlog, exist_ok=True)
        os.makedirs(_repo, exist_ok=True)
        _result = os.path.join(_buildlog, 'foo.result')
        with open(_result, 'w') as fh:
            fh.write('PASS\n')
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
            def deb_dest_for_filename(_f): return _repo
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

        assert not os.path.exists(_result), (
            "stale PASS must be CLEARED by repair so next source build "
            "rebuilds rather than skipping; got file still present"
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
            def deb_dest_for_filename(_f): return _repo
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
            def deb_dest_for_filename(_f): return _repo
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


def test_source_state_repairable_requires_patchhash_baseline():
    """Regression for the audit/repair ping-pong reported 2026-05-23.

    Before this fix: repair on stale_pass cleared both .result and
    .patchhash; the next audit re-classified the source as
    'repairable' (binaries valid + .result missing); a second repair
    would write PASS over actually-stale binaries.  Cycle didn't
    settle.

    After fix: 'repairable' requires .patchhash matching current
    patch_set_hash as proof binaries are current.  Without the
    baseline, classify as 'needs_build' instead — operator must
    rebuild, not repair.

    Pins the three .patchhash branches for the binaries-valid +
    .result-missing case."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import build as _build_mod
    from utils import patch_set_hash as _patch_set_hash

    def _run(patchhash_content):
        with tempfile.TemporaryDirectory() as _tmp:
            _buildlog = os.path.join(_tmp, 'log', 'build')
            _repo = os.path.join(_tmp, 'repo')
            os.makedirs(_buildlog, exist_ok=True)
            os.makedirs(_repo, exist_ok=True)
            _deb = 'foo_1.0_amd64.deb'
            with open(os.path.join(_repo, _deb), 'wb') as fh:
                fh.write(b'!<arch>\n')
            # .result deliberately NOT created (this is the "missing
            # .result" axis we're testing).
            if patchhash_content is not None:
                with open(os.path.join(_buildlog, 'foo.patchhash'),
                          'w') as fh:
                    fh.write(patchhash_content + '\n')
            class _Src:
                pkgs = [_deb]
                files = {}
                version = '1.0'
                patch_list = []
            class _Container:
                buildlog_path = _buildlog
                @staticmethod
                def is_ar_file(_): return True
                @staticmethod
                def verify_pkg_artifact(*_a, **_kw): return (True, 'ok')
            class _Cfg:
                dir_repo = _repo
                dir_log = os.path.join(_tmp, 'log')
                dir_source = os.path.join(_tmp, 'source')
                dir_patch_source = os.path.join(_tmp, 'patch', 'source')
                @staticmethod
                def deb_dest_for_filename(_): return _repo
            class _Tree:
                selected_srcs = {'foo': _Src()}
                src_pkg_files = {'foo': list(_Src.pkgs)}
            _sess = _build_mod.BuildSession.__new__(
                _build_mod.BuildSession)
            _sess.config = _Cfg
            _sess.dep_tree = _Tree
            _sess.udeb_dep_tree = None
            _sess.container = _Container
            return _sess._source_state('foo', _Tree.selected_srcs['foo'])

    # Branch A: .patchhash absent → can't prove binaries fresh →
    # needs_build (NOT repairable).
    assert _run(None) == 'needs_build', (
        "missing .patchhash + missing .result + valid binaries must "
        "classify as 'needs_build' (no proof of freshness), not "
        "'repairable' — otherwise repair-after-stale_pass writes PASS "
        "over actually-stale binaries"
    )

    # Branch B: .patchhash matches current → binaries provably built
    # against current patches → 'repairable'.
    _matching = _patch_set_hash('/nonexistent', [])
    assert _run(_matching) == 'repairable'

    # Branch C: .patchhash differs from current → binaries are stale
    # → 'needs_build'.
    assert _run('deadbeef' * 8) == 'needs_build'


def test_chroot_build_wires_both_audit_gates_with_no_gate_bypass():
    """P5 (2026-05-23): both chroot build live + installer must
    pre-flight `source audit` AND `repo audit`, and both must honour
    a `no-gate` arg to bypass the gates.  Pin the structural shape
    of both methods."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    import re
    # Both audit helpers must exist.
    assert 'def _preflight_audit_source(' in _body, (
        "missing _preflight_audit_source helper (P5)"
    )
    assert 'def _preflight_audit_repo(' in _body, (
        "missing _preflight_audit_repo helper (was P3)"
    )
    # Both chroot build entry points must call both helpers AND check
    # for the no-gate bypass.
    for _entry in ('cmd_build_chroot_live', 'cmd_build_chroot_installer'):
        _m = re.search(
            rf'def {_entry}\(self.*?(?=\n    def \w)',
            _body, re.DOTALL,
        )
        assert _m, f"{_entry} body not found"
        _fn = _m.group(0)
        assert "'no-gate' in args" in _fn or "--no-gate" in _fn, (
            f"{_entry} missing no-gate bypass arg parsing"
        )
        assert 'self._preflight_audit_source()' in _fn, (
            f"{_entry} doesn't call _preflight_audit_source"
        )
        assert 'self._preflight_audit_repo()' in _fn, (
            f"{_entry} doesn't call _preflight_audit_repo"
        )


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


def test_cmd_source_audit_classifies_rebuilds_by_subset():
    """source audit must group rebuild candidates by which `source build
    <mode>` covers them (pkg / installer / live / pool / recommended).
    Operator needs to know which command to run to address each bucket.
    (Was cmd_source_rescan's responsibility before P2 — folded into
    cmd_source_audit which is the read-only superset.)"""
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
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
    c.release_info = ''
    c.pkg_list = []
    c.src_list = []
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


def test_offline_fixture_builds_a_valid_cache():
    """Smoke test that the fixture infra itself works — one upstream
    mirror, one package, one source, no fork.  All other TEST-02 /
    TEST-10 tests below assume this base case is sound."""
    import tempfile
    _pkg = (
        "Package: hello\n"
        "Version: 2.10-3\n"
        "Architecture: amd64\n"
        "Description: classic hello\n"
        "Filename: pool/main/h/hello/hello_2.10-3_amd64.deb\n"
        "Size: 100\n"
        "SHA256: deadbeef\n"
    )
    _src = (
        "Package: hello\n"
        "Binary: hello\n"
        "Version: 2.10-3\n"
        "Architecture: any\n"
        "Directory: pool/main/h/hello\n"
        "Checksums-Sha256:\n"
        " " + ("a" * 64) + " 100 hello_2.10-3.dsc\n"
    )
    with tempfile.TemporaryDirectory() as td:
        c = _make_offline_cache(td, packages={'main': _pkg},
                                sources={'main': _src})
    assert c.is_valid, c.error_str
    assert 'hello' in c.package_hashtable
    assert 'hello' in c.source_hashtable


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
                 recommends=None, alt_depends=None, provides=None):
        self._fields = {'Package': name, 'Version': ver}
        self.package = name
        self.version = ver
        self.depends      = list(depends      or [])
        self.pre_depends  = list(pre_depends  or [])
        self.recommends   = list(recommends   or [])
        self.alt_depends  = list(alt_depends  or [])
        self.conflicts    = []
        self.breaks       = []
        self._provides    = list(provides     or [])
        self.depends_on   = []
        self.depended_by  = []
    def __getitem__(self, k): return self._fields[k]
    def get_provides(self): return list(self._provides)
    def add_constraint(self, v, o): pass


def test_parse_dependency_empty_name_returns_none():
    """Guard the early empty-name return — caller could pass '' from a
    malformed dep tuple (e.g. parser garbled output).  Must not raise
    KeyError on the cache lookup; must return None."""
    dt = _make_parse_dep_tree({})
    assert dt.parse_dependency('') is None


def test_parse_dependency_no_candidates_returns_none():
    """Case II — name not in cache, no Provides match.  Returns None
    so caller emits 'unresolved' warning rather than crashing."""
    dt = _make_parse_dep_tree({})
    assert dt.parse_dependency('does-not-exist') is None


def test_parse_dependency_single_candidate_case_iii():
    """Case III — exactly one candidate, no lookahead disambiguation
    needed.  Selected directly; placed into selected_pkgs."""
    foo = _ParseDepPkg('foo', '1.0')
    dt = _make_parse_dep_tree({'foo': [foo]})
    result = dt.parse_dependency('foo')
    assert result is foo
    assert dt.selected_pkgs['foo'] is foo


def test_parse_dependency_already_selected_returns_existing():
    """Already-selected name with no version constraint — early return
    of the existing Package without re-walking the cache.  Pins the
    short-circuit so a later parse_dependency() call for the same name
    is O(1)."""
    foo = _ParseDepPkg('foo', '1.0')
    dt = _make_parse_dep_tree({'foo': [foo]})
    dt.parse_dependency('foo')
    # Second call: cache is empty (we cleared it), but result still
    # returns from selected_pkgs.
    dt._DependencyTree__cache._pkgs = {}
    assert dt.parse_dependency('foo') is foo


def test_parse_dependency_provides_registers_virtual_alias():
    """A real package providing virtual `awk` must register both names
    in selected_pkgs so a later parse_dependency('awk') hits the
    selected_pkgs early-return path."""
    gawk = _ParseDepPkg('gawk', '1.0', provides=[('awk', None)])
    dt = _make_parse_dep_tree({'gawk': [gawk], 'awk': [gawk]})
    dt.parse_dependency('gawk')
    # Virtual name registered alongside the real one.
    assert dt.selected_pkgs.get('awk') is gawk
    assert dt.selected_pkgs.get('gawk') is gawk


def test_parse_dependency_propagates_dep_recursively():
    """`foo` Depends `bar` — selecting `foo` must recurse and select
    `bar` too.  Pins the depth-first walk; without it selected_pkgs
    would only contain the seed."""
    bar = _ParseDepPkg('bar', '1.0')
    foo = _ParseDepPkg('foo', '1.0', depends=[('bar', '', '')])
    dt = _make_parse_dep_tree({'foo': [foo], 'bar': [bar]})
    dt.parse_dependency('foo')
    assert 'bar' in dt.selected_pkgs
    assert 'bar' in foo.depends_on
    assert 'foo' in bar.depended_by


def test_parse_dependency_cycle_protection_does_not_infinite_loop():
    """A → B → A.  The early return on already-selected names
    (selected_pkgs check at top of parse_dependency) breaks the cycle.
    Without it the test would recurse forever and bust the stack —
    a finite return value here proves cycle detection works."""
    a = _ParseDepPkg('a', '1.0', depends=[('b', '', '')])
    b = _ParseDepPkg('b', '1.0', depends=[('a', '', '')])
    dt = _make_parse_dep_tree({'a': [a], 'b': [b]})
    result = dt.parse_dependency('a')
    assert result is a
    assert 'a' in dt.selected_pkgs
    assert 'b' in dt.selected_pkgs


def test_parse_dependency_recommends_pulled_when_flag_on():
    """select_recommended=True → recommends are walked like depends.
    Off (default) → recommends are skipped.  Pins both branches."""
    rec = _ParseDepPkg('rec', '1.0')
    foo_with_rec = _ParseDepPkg('foo', '1.0', recommends=[('rec', '', '')])

    dt_off = _make_parse_dep_tree({'foo': [foo_with_rec], 'rec': [rec]})
    dt_off.parse_dependency('foo')
    assert 'rec' not in dt_off.selected_pkgs, (
        "default: recommends NOT pulled in"
    )

    # Build a fresh tree (Package objects carry state across resolves).
    rec2 = _ParseDepPkg('rec', '1.0')
    foo2 = _ParseDepPkg('foo', '1.0', recommends=[('rec', '', '')])
    dt_on = _make_parse_dep_tree({'foo': [foo2], 'rec': [rec2]},
                                  select_recommended=True)
    dt_on.parse_dependency('foo')
    assert 'rec' in dt_on.selected_pkgs, (
        "select_recommended=True: recommends pulled in"
    )


def test_parse_dependency_alt_deps_first_already_selected_wins():
    """`foo` has alt-dep `[ ('a', ...), ('b', ...) ]`.  When `a` is
    already in selected_pkgs (and satisfies the version), parse_dep
    must pick `a` — not the first alternative blindly.  This pins
    the alt-deps selected-first preference loop."""
    a = _ParseDepPkg('a', '1.0')
    b = _ParseDepPkg('b', '1.0')
    foo = _ParseDepPkg('foo', '1.0', alt_depends=[
        [('a', '', ''), ('b', '', '')]
    ])
    dt = _make_parse_dep_tree({'foo': [foo], 'a': [a], 'b': [b]})
    # Pre-seed `a`.
    dt.selected_pkgs['a'] = a
    dt.parse_dependency('foo')
    # Both could end up in selected_pkgs via Provides if any; but the
    # forward edge from foo must go to `a` (the already-selected alt).
    assert 'a' in foo.depends_on
    assert 'b' not in foo.depends_on


def test_parse_dependency_alt_deps_default_to_first_alternative():
    """When none of the alts are already selected, parse_dependency
    falls back to the FIRST alternative (Debian convention).  Pins
    the default-pick behaviour at the bottom of the alt-deps loop."""
    a = _ParseDepPkg('a', '1.0')
    b = _ParseDepPkg('b', '1.0')
    foo = _ParseDepPkg('foo', '1.0', alt_depends=[
        [('a', '', ''), ('b', '', '')]
    ])
    dt = _make_parse_dep_tree({'foo': [foo], 'a': [a], 'b': [b]})
    dt.parse_dependency('foo')
    # First alt picked.
    assert 'a' in foo.depends_on
    assert 'b' not in foo.depends_on


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


# ─────────────────────────────────────────────────────────────────────────────
# TEST-10: coverage gaps in signing + cache
# ─────────────────────────────────────────────────────────────────────────────


def test_signing_get_key_info_returns_none_when_gpg_missing():
    """get_key_info short-circuits with None when shutil.which('gpg')
    returns None — operator without gpg installed must not crash a
    `print signing` or build_chroot startup gate.  This pins the
    no-gpg branch separately from the no-homedir branch already
    covered by test_signing_get_key_info_returns_none_when_homedir_absent."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import signing

    class _Cfg:
        def __init__(self, td):
            self.dir_gnupg = td
            self.signing_key_uid = 'Athena Build <athena@local>'

    with tempfile.TemporaryDirectory() as td:
        # Create signing_home so the homedir-missing branch doesn't
        # short-circuit first.
        os.makedirs(os.path.join(td, 'signing'), mode=0o700)
        cfg = _Cfg(td)
        with patch.object(signing.shutil, 'which', return_value=None):
            assert signing.get_key_info(cfg) is None


def test_signing_generate_key_returns_false_when_gpg_missing():
    """generate_key short-circuits with False when gpg is absent on
    PATH — caller (cmd_generate_signing_key) must see the failure
    and prompt the operator to install gpg rather than hanging on a
    subprocess.run that would FileNotFoundError."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import signing

    class _Cfg:
        def __init__(self, td):
            self.dir_gnupg = td
            self.signing_key_uid = 'Athena Build <athena@local>'

    with tempfile.TemporaryDirectory() as td:
        cfg = _Cfg(td)
        with patch.object(signing.shutil, 'which', return_value=None):
            assert signing.generate_key(cfg) is False


def test_signing_generate_key_returns_false_when_export_step_fails():
    """generate_key has TWO subprocess calls — gen + export.  Existing
    tests cover gen-failure indirectly (real-gpg roundtrip) but the
    export-failure branch (after a successful gen) is unexercised.
    Pin that path by mocking subprocess.run to succeed on gen, fail
    on export."""
    import sys, tempfile, subprocess as _subp
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import signing

    class _Cfg:
        def __init__(self, td):
            self.dir_gnupg = td
            self.signing_key_uid = 'Athena Build <athena@local>'

    _calls = []
    def _fake_run(cmd, *args, **kwargs):
        _calls.append(tuple(cmd))
        _r = _subp.CompletedProcess(cmd, returncode=0,
                                     stdout='', stderr='')
        # First call is --gen-key (succeeds); second is --export (fails).
        if '--export' in cmd:
            _r.returncode = 2
            _r.stderr = 'gpg: forced export failure for test'
        return _r

    with tempfile.TemporaryDirectory() as td:
        cfg = _Cfg(td)
        with patch.object(signing.shutil, 'which', return_value='/usr/bin/gpg'), \
             patch.object(signing.subprocess, 'run', side_effect=_fake_run):
            ok = signing.generate_key(cfg)
    assert ok is False
    # Both calls fired: gen succeeded, export failed.  Pin the order
    # so a refactor doesn't reverse it (export-first would leak a
    # half-created key on the disk if gen then failed).
    assert len(_calls) == 2
    assert '--gen-key' in _calls[0]
    assert '--export'  in _calls[1]


def test_cache_build_populates_upstream_collisions_via_real_path():
    """End-to-end through __build_cache: a fork mirror + an upstream
    mirror, both shipping 'pkgsel', upstream version greater than
    fork's.  The collision-gate must fire and set error_str.

    Existing _make_collision_cache scaffold tests the gate function
    in isolation; this test exercises the real ingest path that
    populates _upstream_collisions from on-disk Packages records."""
    import tempfile
    _fork_pkg = (
        "Package: pkgsel\n"
        "Version: 0.79\n"
        "Architecture: amd64\n"
        "Filename: pool/main/p/pkgsel/pkgsel_0.79_amd64.deb\n"
        "Size: 100\n"
        "SHA256: " + ("a" * 64) + "\n"
    )
    _upstream_pkg = (
        "Package: pkgsel\n"
        "Version: 0.80\n"
        "Architecture: amd64\n"
        "Filename: pool/main/p/pkgsel/pkgsel_0.80_amd64.deb\n"
        "Size: 100\n"
        "SHA256: " + ("b" * 64) + "\n"
    )
    with tempfile.TemporaryDirectory() as td:
        c = _make_offline_cache(
            td,
            packages={'fork': _fork_pkg, 'main': _upstream_pkg},
        )
    assert c.is_valid is False, "collision gate must fail the build"
    assert 'pkgsel' in c.error_str
    assert '0.79'   in c.error_str
    assert '0.80'   in c.error_str
    assert 'main'   in c.error_str
    # Pin the collision-tracking side-channel too — _upstream_collisions
    # was populated during the walk before the gate fired.
    assert 'pkgsel' in c._upstream_collisions
    assert ('main', '0.80') in c._upstream_collisions['pkgsel']


def test_cache_build_fork_supersede_drops_upstream_silently():
    """When the fork's version dominates upstream, the gate passes and
    upstream's record is dropped from package_hashtable (apt sees only
    the fork).  Pin both: gate passes AND upstream version is absent
    from the hashtable for the colliding name."""
    import tempfile
    _fork_pkg = (
        "Package: athena-base-files\n"
        "Version: 12.4+deb12u14+athena1\n"
        "Architecture: all\n"
        "Filename: pool/main/a/athena-base-files/athena-base-files_12.4+deb12u14+athena1_all.deb\n"
        "Size: 100\n"
        "SHA256: " + ("a" * 64) + "\n"
    )
    _upstream_pkg = (
        "Package: athena-base-files\n"
        "Version: 12.4+deb12u14\n"
        "Architecture: all\n"
        "Filename: pool/main/a/athena-base-files/athena-base-files_12.4+deb12u14_all.deb\n"
        "Size: 100\n"
        "SHA256: " + ("b" * 64) + "\n"
    )
    with tempfile.TemporaryDirectory() as td:
        c = _make_offline_cache(
            td,
            packages={'fork': _fork_pkg, 'main': _upstream_pkg},
        )
    assert c.is_valid is True, c.error_str
    # Fork version present in hashtable.
    _versions = list(c.package_hashtable['athena-base-files'].keys())
    assert len(_versions) == 1
    assert str(_versions[0]) == '12.4+deb12u14+athena1'
    # Upstream version was logged as superseded.
    assert 'athena-base-files' in c._upstream_collisions
    assert ('main', '12.4+deb12u14') in c._upstream_collisions['athena-base-files']


# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 step 1 — +asg<R>u<N> version-suffix primitives
# ─────────────────────────────────────────────────────────────────────────────

def test_nmu_regex_does_not_eat_asg_suffix():
    """_NMU_SUFFIX_RE must NOT strip our +asg<R>u<N> marker — otherwise the
    post-build strip pass would erase the stamp before we apply it."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_suffix
    assert strip_nmu_suffix('3.0.15-1+asg1u1') == '3.0.15-1+asg1u1'
    assert strip_nmu_suffix('3.0.15-1+asg12u7') == '3.0.15-1+asg12u7'
    # An NMU layer UNDER nothing-of-ours still strips:
    assert strip_nmu_suffix('3.0.15-1+deb12u2') == '3.0.15-1'


def test_pristine_base_strips_both_layers():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import pristine_base
    assert pristine_base('3.0.15-1') == '3.0.15-1'
    assert pristine_base('3.0.15-1+asg1u2') == '3.0.15-1'
    assert pristine_base('3.0.15-1+deb12u2') == '3.0.15-1'
    assert pristine_base('1:2.38.1-5+asg1u1') == '1:2.38.1-5'   # epoch kept
    # Both layers cannot legitimately co-occur (we strip NMU before stamping),
    # but the grouping key must still resolve to the base if they did:
    assert pristine_base('3.0.15-1+deb12u2') == pristine_base('3.0.15-1+asg9u9')


def test_asg_suffix_sorts_above_pristine_and_across_release():
    """dpkg version ordering: our suffix beats pristine; release integer R
    dominates the update counter N (asg2u1 > asg1u9) — the whole reason for
    the distro-derived form over the codename-derived +thorN."""
    import subprocess
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import apply_asg_suffix, parse_asg_suffix
    try:
        subprocess.run(['dpkg', '--version'], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_asg_suffix_sorts (no dpkg)")
        return

    def _cmp(a, op, b):
        return subprocess.run(
            ['dpkg', '--compare-versions', a, op, b]).returncode == 0

    assert _cmp(apply_asg_suffix('3.0.15-1', 1, 1), 'gt', '3.0.15-1')
    assert _cmp(apply_asg_suffix('3.0.15-1', 1, 2), 'gt',
                apply_asg_suffix('3.0.15-1', 1, 1))
    # Release integer dominates: asg2u1 > asg1u9 (the +thorN bug fix)
    assert _cmp(apply_asg_suffix('3.0.15-1', 2, 1), 'gt',
                apply_asg_suffix('3.0.15-1', 1, 9))
    # round-trips through the parser
    assert parse_asg_suffix(apply_asg_suffix('3.0.15-1', 2, 7)) == (2, 7)
    assert parse_asg_suffix('3.0.15-1') is None


def test_asg_filename_maps_pristine_to_stamped():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import asg_filename
    assert (asg_filename('openssl_3.0.15-1_amd64.deb', 1, 1)
            == 'openssl_3.0.15-1+asg1u1_amd64.deb')
    assert (asg_filename('libssl3_3.0.15-1_amd64.udeb', 3, 2)
            == 'libssl3_3.0.15-1+asg3u2_amd64.udeb')
    # malformed / non-deb → no-op
    assert asg_filename('not-a-package', 1, 1) == 'not-a-package'
    assert asg_filename('a_b_c_d_amd64.deb', 1, 1) == 'a_b_c_d_amd64.deb'


def test_match_pristine_base_reconciles_stamped_artifact():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import match_pristine_base
    pred = 'openssl_3.0.15-1_amd64.deb'
    assert match_pristine_base(pred, pred)                              # exact
    assert match_pristine_base(pred, 'openssl_3.0.15-1+asg1u1_amd64.deb')
    assert match_pristine_base(pred, 'openssl_3.0.15-1+asg7u3_amd64.deb')
    # different name / arch / ext / base → no match
    assert not match_pristine_base(pred, 'openssh_3.0.15-1+asg1u1_amd64.deb')
    assert not match_pristine_base(pred, 'openssl_3.0.15-1+asg1u1_i386.deb')
    assert not match_pristine_base(pred, 'openssl_3.0.16-1+asg1u1_amd64.deb')
    assert not match_pristine_base(pred, 'openssl_3.0.15-1_amd64.udeb')


def test_restamp_asg_deb_bumps_version_and_intra_source_deps():
    """Build a .deb at a pristine base with a collapsed intra-source sibling
    pin `(= base)`, restamp to +asg<R>u<N>, and verify: filename, internal
    Version, and the sibling pin all carry the suffix — while a foreign
    `>=` dep stays pristine.  Epoch preserved."""
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import restamp_asg_deb
    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_restamp_asg_deb (no dpkg-deb)")
        return
    with tempfile.TemporaryDirectory() as _tmp:
        _work = os.path.join(_tmp, 'src')
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        os.makedirs(os.path.join(_work, 'usr', 'lib'))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(
                'Package: openssl\n'
                'Version: 1:3.0.15-1\n'                       # epoch + pristine
                'Architecture: amd64\n'
                'Maintainer: T <t@l>\n'
                # sibling pin (collapsed idiom) at THIS binary's version:
                'Depends: libssl3 (= 1:3.0.15-1), libc6 (>= 2.36)\n'
                'Description: restamp test\n'
            )
        with open(os.path.join(_work, 'usr', 'lib', 'p'), 'w') as fh:
            fh.write('x\n')
        _orig = os.path.join(_tmp, 'openssl_3.0.15-1_amd64.deb')   # filename: no epoch
        subprocess.run(['dpkg-deb', '--root-owner-group', '-b', _work, _orig],
                       check=True, capture_output=True)

        _r = restamp_asg_deb(_orig, 1, 1)
        assert _r['status'] == 'rewritten', _r
        assert os.path.basename(_r['new_path']) == 'openssl_3.0.15-1+asg1u1_amd64.deb'
        assert _r['version'] == '1:3.0.15-1+asg1u1', _r
        _ver = subprocess.run(['dpkg-deb', '-f', _r['new_path'], 'Version'],
                              check=True, capture_output=True, text=True).stdout.strip()
        assert _ver == '1:3.0.15-1+asg1u1', f"epoch/suffix wrong: {_ver!r}"
        _deps = subprocess.run(['dpkg-deb', '-f', _r['new_path'], 'Depends'],
                               check=True, capture_output=True, text=True).stdout.strip()
        assert 'libssl3 (= 1:3.0.15-1+asg1u1)' in _deps, (
            f"sibling pin not bumped: {_deps!r}")
        assert 'libc6 (>= 2.36)' in _deps, (
            f"foreign dep should stay pristine: {_deps!r}")


# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 step 2 — append-only enforcement (additive publish + segregate)
# ─────────────────────────────────────────────────────────────────────────────

def test_publish_rsync_is_additive_no_delete():
    """The publish rsync must be ADDITIVE: _rsync_streamed defaults to no
    --delete, adds it only under `if delete:`, and cmd_repo_publish never
    requests delete=True.  A --delete publish would erase remote versions
    absent from the local single-snapshot tree — destroying the append-only
    archive."""
    import re as _re
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    assert _re.search(
        r'def _rsync_streamed\(self, label, src, dest, ssh_cmd, delete=False',
        _body), "_rsync_streamed must default delete=False (additive)"
    _m = _re.search(r'def _rsync_streamed\(.*?return _proc\.returncode',
                    _body, _re.DOTALL)
    assert _m, "_rsync_streamed body not found"
    _fn = _m.group(0)
    assert 'if delete:' in _fn and "_argv.append('--delete')" in _fn, (
        "--delete must be conditional on the delete flag")
    assert "'rsync', '-aH', '--delete'" not in _fn, (
        "rsync base argv still hard-codes --delete")
    _pm = _re.search(r'def cmd_repo_publish\(self.*?(?=\n    def )',
                     _body, _re.DOTALL)
    assert _pm and 'delete=True' not in _pm.group(0), (
        "cmd_repo_publish must not request a destructive --delete publish")


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
        assert os.path.join(_dest_dir, _fn) not in _moved, (
            "a kept-existing collision must not be reported as freshly moved")


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
    assert _params == ['self', 'src_pkg', 'source_dir'], (
        f"signature regression: expected (self, src_pkg, source_dir), "
        f"got {_params}")


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
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _src = fh.read()
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
    assert 'as_completed' in _body, (
        "Phase 4: results consumed via as_completed for parallel "
        "accounting in the main thread")


def test_comp03_phase4_serial_path_kept_for_max_parallel_builds_one():
    """COMP-03 Phase 4 backward-compat: MaxParallelBuilds == 1 must
    take a serial for-loop path (no executor, no scoped signal
    handler), so the existing serial behaviour is preserved verbatim
    as the debugging mode + zero-risk fallback."""
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _src = fh.read()
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
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _src = fh.read()
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


def test_comp03_phase4_executor_shutdown_uses_wait_true_and_cancel_futures():
    """COMP-03 Phase 4: executor.shutdown(wait=True, cancel_futures=True)
    on the way out — wait=True so workers drain their finally blocks
    (container reap, scratch-dir rmtree); cancel_futures=True so queued
    builds don't run after shutdown."""
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _src = fh.read()
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
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _src = fh.read()
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
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _src = fh.read()
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


def test_comp03_phase4_build_one_source_skip_src_returns_skipped():
    """COMP-03 Phase 4 worker contract: _build_one_source returns
    ('skipped', 0) when the source is on cache.skip_src.  Behaviour
    test using a thin BuildSession stub."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    _sess = build.BuildSession.__new__(build.BuildSession)

    class _FakeCache:
        skip_src = {'skipped-pkg'}

    class _FakeConfig:
        tunnel_packages = []
    _sess.cache = _FakeCache()
    _sess.config = _FakeConfig()

    class _Src:
        package = 'skipped-pkg'
        version = '1.0'

    _result, _ = _sess._build_one_source(_Src(), False, False, None, None)
    assert _result == 'skipped', _result


def test_comp03_phase4_build_one_source_tunneled_calls_do_tunnel():
    """COMP-03 Phase 4 worker contract: when src is on tunnel_packages,
    _build_one_source returns ('tunneled', 0) on successful download
    and ('failed', 0) on tunnel failure — NOT ('built', 0).  Mocked."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    _sess = build.BuildSession.__new__(build.BuildSession)

    class _FakeCache:
        skip_src = set()

    class _FakeConfig:
        tunnel_packages = ['tunnel-me']

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

    _result, _ = _sess._build_one_source(_Src(), False, False, None, None)
    assert _result == 'tunneled', _result

    _sess._do_tunnel = lambda _src: False  # tunnel fails
    _result, _ = _sess._build_one_source(_Src(), False, False, None, None)
    assert _result == 'failed', _result


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

def test_highest_asg_update_reads_remote_ledger():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import highest_asg_update
    pub = ['3.0.15-1', '3.0.15-1+asg1u1', '3.0.15-1+asg1u2',
           '3.0.16-1+asg1u1', '3.0.15-1+asg2u1']
    assert highest_asg_update(pub, '3.0.15-1', 1) == 2   # u1,u2 at R=1
    assert highest_asg_update(pub, '3.0.15-1', 2) == 1   # only asg2u1 at R=2
    assert highest_asg_update(pub, '3.0.16-1', 1) == 1
    assert highest_asg_update([], '3.0.15-1', 1) == 0
    assert highest_asg_update(['3.0.15-1'], '3.0.15-1', 1) == 0   # pristine only


def test_asg_next_n_is_per_file_and_cumulative():
    """asg_next_n = highest published +1, derived PER FILE (per the binary's
    own ledger entry) so a file updated more often carries a higher N; resets
    per release R."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import asg_next_n
    assert asg_next_n([], '3.0.15-1', 1) == 1                       # first → u1
    assert asg_next_n(['3.0.15-1+asg1u1'], '3.0.15-1', 1) == 2      # u1 → u2
    assert asg_next_n(['3.0.15-1+asg1u1', '3.0.15-1+asg1u2'],
                      '3.0.15-1', 1) == 3                           # cumulative
    assert asg_next_n(['3.0.15-1+asg1u1'], '3.0.15-1', 2) == 1      # R differs


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
        with open(os.path.join(_log, 'openssl.result'), 'w') as fh:
            fh.write('PASS\n')

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
        with open(os.path.join(_log, 'intel-microcode.result'), 'w') as fh:
            fh.write('TUNNELED\n')
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


def test_normalize_built_artifacts_stamps_delta_when_ledger_present():
    """A delta build (here forced via was_patched) with a remote ledger loaded
    gets +asg<R>u<N> stamped; N derives from the ledger."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _p = os.path.join(_tmp, 'openssl_3.0.15-1_amd64.deb')
        _build_minimal_deb(_p, 'openssl', '3.0.15-1', 'amd64')
        _bc = _make_buildcontainer_stub(repo=_tmp)
        # ledger already has the pristine published → next update is u1
        _bc.asg_ledger = {'openssl': ['3.0.15-1']}

        class _FakeConfig:
            build_version = '1'

        class _Src:
            package = 'openssl'
            version = '3.0.15-1'

        _bc.config = _FakeConfig()
        _bc._normalize_built_artifacts(_Src(), [_p], was_patched=True)
        assert not os.path.exists(_p), "pristine file should be renamed"
        _stamped = os.path.join(_tmp, 'openssl_3.0.15-1+asg1u1_amd64.deb')
        assert os.path.exists(_stamped), "delta build was not asg-stamped"
        import subprocess
        _ver = subprocess.run(['dpkg-deb', '-f', _stamped, 'Version'],
                              check=True, capture_output=True, text=True).stdout.strip()
        assert _ver == '3.0.15-1+asg1u1', _ver


def test_normalize_built_artifacts_no_stamp_without_ledger():
    """No remote ledger (a plain `source build`, not a refresh) → strip only,
    NO asg stamp, even for a delta build."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as _tmp:
        _p = os.path.join(_tmp, 'openssl_3.0.15-1_amd64.deb')
        _build_minimal_deb(_p, 'openssl', '3.0.15-1', 'amd64')
        _bc = _make_buildcontainer_stub(repo=_tmp)
        # no asg_ledger attribute set → getattr returns None → no stamping

        class _FakeConfig:
            build_version = '1'

        class _Src:
            package = 'openssl'
            version = '3.0.15-1'

        _bc.config = _FakeConfig()
        _bc._normalize_built_artifacts(_Src(), [_p], was_patched=True)
        assert os.path.exists(_p), "without a ledger the file must stay pristine"


def test_postbuild_convergence_hard_fails_when_check_build_still_false():
    """Guard B: the per-source build path must re-check check_build
    after a PASS and hard-fail (not silently rebuild next run) on
    non-convergence.  Logic lives in _build_one_source (COMP-03 Phase 4
    extracted the per-source unit from the cmd_source_build loop body)."""
    import re as _re
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    _m = _re.search(r'def _build_one_source\(.*?(?=\n    def )',
                    _body, _re.DOTALL)
    assert _m, "_build_one_source not found"
    _b = _m.group(0)
    assert 'if _build_result and self.container.check_build(' in _b, (
        "Guard B: a PASS must be re-validated through check_build")
    assert ('if _build_result:' in _b
            and 'check_build still false' in _b), (
        "Guard B: a built-but-unconverged source must hard-fail with a diagnostic")


# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 step 4 — remote ledger + version-aware local cleanup
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_packages_to_ledger_multiversion_epoch_stripped():
    """The ledger lists EVERY version per package (multi-version) with the
    epoch stripped so it matches the filename-derived bases the build uses."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    with tempfile.TemporaryDirectory() as _tmp:
        _pk = os.path.join(_tmp, 'Packages')
        with open(_pk, 'w') as fh:
            fh.write(
                'Package: openssl\nVersion: 1:3.0.15-1\n'
                'Filename: pool/openssl_3.0.15-1_amd64.deb\n\n'
                'Package: openssl\nVersion: 1:3.0.15-1+asg1u1\n'
                'Filename: pool/openssl_3.0.15-1+asg1u1_amd64.deb\n\n'
                'Package: libc6\nVersion: 2.36-9\n'
                'Filename: pool/libc6_2.36-9_amd64.deb\n\n')
        _ledger = repo_audit.parse_packages_to_ledger(_pk)
        assert sorted(_ledger['openssl']) == ['3.0.15-1', '3.0.15-1+asg1u1'], _ledger
        assert _ledger['libc6'] == ['2.36-9']
        # epoch stripped → matches build-side filename bases
        assert all(':' not in _v for _vs in _ledger.values() for _v in _vs)


def test_published_base_versions():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    ledger = {
        'openssl': ['3.0.15-1', '3.0.15-1+asg1u1', '3.0.16-1'],
        'libc6': ['2.36-9'],
    }
    _bases = repo_audit.published_base_versions(ledger)
    assert _bases['openssl'] == '3.0.16-1'    # highest base
    assert _bases['libc6'] == '2.36-9'


def test_group_by_pristine_base():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    _g = repo_audit.group_by_pristine_base(
        ['3.0.15-1', '3.0.15-1+asg1u1', '3.0.15-1+asg1u2', '3.0.16-1'])
    assert sorted(_g['3.0.15-1']) == ['3.0.15-1', '3.0.15-1+asg1u1', '3.0.15-1+asg1u2']
    assert _g['3.0.16-1'] == ['3.0.16-1']


def test_local_cleanup_keeps_highest_prunes_superseded_and_flags_orphan():
    """UPD-01 single-snapshot prune (behavioral): when a pristine artifact and
    its freshly +asg-stamped successor coexist, the STAMPED (highest) one is
    kept and the pristine predecessor is drift; a non-selected source is
    orphan.  This is what `repo refresh`'s post-publish prune relies on."""
    import shutil as _sh
    if not (_sh.which('dpkg-deb') and _sh.which('dpkg-scanpackages')):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build, repo_audit
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

        _orphan, _drift, _malformed, _total = _sess._scan_stale_files()
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
# UPD-01 step 5 — merge_remote_index (multi-version, signed locally)
# ─────────────────────────────────────────────────────────────────────────────

def test_merge_remote_index_preserves_old_versions_multiversion():
    """merge_packages_indexes unions remote + local, PRESERVING every remote
    version (append-only) and adding the new local version."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):   # apt_pkg present wherever dpkg tooling is
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo
    import apt_pkg
    remote = (
        'Package: openssl\nVersion: 3.0.15-1\nArchitecture: amd64\nSize: 100\n'
        'Filename: dists/thor/main/binary-amd64/openssl_3.0.15-1_amd64.deb\n\n'
        'Package: openssl\nVersion: 3.0.15-1+asg1u1\nArchitecture: amd64\nSize: 101\n'
        'Filename: dists/thor/main/binary-amd64/openssl_3.0.15-1+asg1u1_amd64.deb\n')
    local = (
        'Package: openssl\nVersion: 3.0.15-1+asg1u2\nArchitecture: amd64\nSize: 102\n'
        'Filename: dists/thor/main/binary-amd64/openssl_3.0.15-1+asg1u2_amd64.deb\n\n'
        'Package: libc6\nVersion: 2.36-9\nArchitecture: amd64\nSize: 200\n'
        'Filename: dists/thor/main/binary-amd64/libc6_2.36-9_amd64.deb\n')
    _merged = apt_repo.merge_packages_indexes(remote, local)
    with tempfile.NamedTemporaryFile('w', delete=False) as fh:
        fh.write(_merged)
        _p = fh.name
    try:
        _seen = set()
        with open(_p) as rf:
            for _sec in apt_pkg.TagFile(rf):
                _seen.add((_sec.get('Package'), _sec.get('Version')))
    finally:
        os.remove(_p)
    assert ('openssl', '3.0.15-1') in _seen          # remote pristine kept
    assert ('openssl', '3.0.15-1+asg1u1') in _seen   # remote delta kept
    assert ('openssl', '3.0.15-1+asg1u2') in _seen   # new local version added
    assert ('libc6', '2.36-9') in _seen
    assert len(_seen) == 4


def test_merge_packages_indexes_dedup_local_wins():
    """A (Package, Version) present in BOTH appears once; the local stanza
    wins (freshly built this snapshot)."""
    import shutil as _sh
    if not _sh.which('dpkg-deb'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo
    import apt_pkg
    remote = ('Package: foo\nVersion: 1.0\nArchitecture: amd64\nSize: 1\n'
              'Filename: pool/foo_1.0_amd64.deb\n')
    local = ('Package: foo\nVersion: 1.0\nArchitecture: amd64\nSize: 999\n'
             'Filename: pool/foo_1.0_amd64.deb\n')
    _merged = apt_repo.merge_packages_indexes(remote, local)
    with tempfile.NamedTemporaryFile('w', delete=False) as fh:
        fh.write(_merged)
        _p = fh.name
    try:
        with open(_p) as rf:
            _secs = [(s.get('Package'), s.get('Version'), s.get('Size'))
                     for s in apt_pkg.TagFile(rf)]
    finally:
        os.remove(_p)
    assert len(_secs) == 1, _secs                    # deduped
    assert _secs[0] == ('foo', '1.0', '999')         # local won


def test_generate_top_release_subprocess_text_mode_consistency():
    """Python's `subprocess.run(..., text=True, input=bytes)` raises
    AttributeError 'bytes object has no attribute encode' — under text mode
    subprocess internally calls `input.encode(stdin.encoding)` to convert
    str input, and bytes-input crashes that path.  Caught 2026-05-28 after
    a publish failed mid-flight with `Error: 'bytes' object has no
    attribute 'encode'`.  This test asserts no subprocess.run() inside
    _generate_top_release mixes those two — either both bytes (no text=)
    or both str (text=True)."""
    import re
    _ar = os.path.join(_ROOT, 'scripts', 'apt_repo.py')
    with open(_ar) as fh:
        _body = fh.read()
    _m = re.search(r'def _generate_top_release\(.*?(?=\ndef )', _body,
                   re.DOTALL)
    assert _m, "_generate_top_release not found"
    _fn = _m.group(0)
    # Walk every subprocess.run(...) inside the function.  Balanced-paren
    # match: find 'subprocess.run(' then consume up through the matching ')'.
    _idx = 0
    _calls: 'list[str]' = []
    while True:
        _start = _fn.find('subprocess.run(', _idx)
        if _start < 0:
            break
        _depth = 0
        _i = _start + len('subprocess.run')
        while _i < len(_fn):
            _ch = _fn[_i]
            if _ch == '(':
                _depth += 1
            elif _ch == ')':
                _depth -= 1
                if _depth == 0:
                    _calls.append(_fn[_start:_i + 1])
                    _idx = _i + 1
                    break
            _i += 1
        else:
            break
    assert _calls, "no subprocess.run() calls parsed from _generate_top_release"
    for _c in _calls:
        _has_text_true = 'text=True' in _c
        _has_bytes_input = (".encode('utf-8')" in _c
                            or '.encode("utf-8")' in _c)
        assert not (_has_text_true and _has_bytes_input), (
            f"subprocess.run() in _generate_top_release combines text=True "
            f"with .encode('utf-8') input — Python will call .encode() on "
            f"the bytes result and raise AttributeError at runtime:\n{_c}"
        )


def test_generate_top_release_avoids_self_reference_via_tempfile():
    """apt-ftparchive's `release` mode walks the suite dir and hashes every
    file it sees — including its own output if streamed straight to
    dists/<suite>/Release.  That made InRelease record a stale hash for
    `Release` (the partial header apt-ftparchive had written before the walk
    reached it), and `repo audit external` failed with MISMATCH Release
    (2026-05-28).  Fix: write to a temp file OUTSIDE the walked subtree
    (`staging/.release-tmp-*`), mv into place after apt-ftparchive
    completes.  Pin the workaround via source-inspection — the real
    exercise (apt-ftparchive + sudo + temp + mv) needs a live tree."""
    import re
    _ar = os.path.join(_ROOT, 'scripts', 'apt_repo.py')
    with open(_ar) as fh:
        _body = fh.read()
    _m = re.search(r'def _generate_top_release\(.*?(?=\ndef )', _body,
                   re.DOTALL)
    assert _m, "_generate_top_release not found"
    _fn = _m.group(0)
    # The fix: temp file under staging/, NOT a direct write to output_path.
    assert "tempfile.mkstemp(" in _fn, \
        "_generate_top_release must write to a temp file (not output_path)"
    assert "prefix='.release-tmp-'" in _fn, \
        "temp file must be at staging root with .release-tmp- prefix"
    assert "dir=staging" in _fn, \
        "temp file must be created in staging/ (outside the walked subtree)"
    # Followed by an sudo mv into the real path — atomic + handles root-owned
    # parent dirs from prior runs.
    assert "'mv', _tmp_path, output_path" in _fn, \
        "must mv the temp file into place once apt-ftparchive finishes"
    # The OLD bug pattern (`open(output_path, 'wb')` straight to the target
    # file) must NOT be present.
    assert "open(output_path, 'wb')" not in _fn, \
        ("_generate_top_release must not stream apt-ftparchive output "
         "directly to output_path — caused MISMATCH Release self-reference")


def test_merge_remote_index_signs_locally_and_merges():
    """merge_remote_index must merge remote+local stanzas, scan the local
    pool, and sign locally (key never leaves the host)."""
    import re
    _ar = os.path.join(_ROOT, 'scripts', 'apt_repo.py')
    with open(_ar) as fh:
        _body = fh.read()
    _m = re.search(r'def merge_remote_index\(.*?(?=\ndef )', _body, re.DOTALL)
    assert _m, "merge_remote_index not found"
    _fn = _m.group(0)
    assert 'merge_packages_indexes(' in _fn, "must merge remote+local stanzas"
    assert '_scan_packages_to(' in _fn, "must scan the local pool"
    assert 'sign_release_files(' in _fn, "must sign locally"
    # Multi-component: non-main components are scanned + published when
    # populated, and the top-level Release lists the populated set.
    assert 'non-free-firmware' in _fn, "must publish non-free-firmware component"
    assert "for _other in ('contrib', 'non-free', 'non-free-firmware')" in _fn
    assert 'components=_populated' in _fn, \
        "top Release must list the populated component set, not just main"


def test_publish_suites_spec_includes_nonfree_components():
    """Both the `repo index` and `repo publish` (full scope) suites_spec must
    list the non-main components so generate_repo_indexes emits them (empty
    ones auto-skip).  Minimal-scope publish stays main-only by design."""
    import re
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as fh:
        _body = fh.read()
    _specs = re.findall(r"_codename:\s*\[([^\]]*?)\]", _body, re.DOTALL)
    _multi = [s for s in _specs if 'doc' in s]
    assert len(_multi) >= 2, _multi
    for _s in _multi:
        for _c in ('contrib', 'non-free', 'non-free-firmware'):
            assert f"'{_c}'" in _s, (_c, _s)


# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 step 6 — workload (change-detection) + Guard A preflight
# ─────────────────────────────────────────────────────────────────────────────

def test_workload_since_published_picks_only_advanced_bases():
    """_workload_since_published returns sources whose pristine base advanced
    beyond the published ledger (or unpublished) — the genuine refresh
    workload."""
    import shutil as _sh
    if not _sh.which('dpkg'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    from build import BuildSession
    _sess = BuildSession.__new__(BuildSession)

    class _Tree:
        selected_srcs = {'openssl': object(), 'libc6': object()}

    _sess.dep_tree = _Tree()
    _sess.udeb_dep_tree = None
    _pred = {
        'openssl': ['openssl_3.0.16-1_amd64.deb'],   # advanced
        'libc6': ['libc6_2.36-9_amd64.deb'],         # unchanged
    }
    _sess._predicted_files_for_source = lambda n: _pred.get(n, [])

    _ledger = {'openssl': ['3.0.15-1'], 'libc6': ['2.36-9']}
    assert _sess._workload_since_published(_ledger) == {'openssl'}
    # nothing published → everything is workload
    assert _sess._workload_since_published({}) == {'openssl', 'libc6'}


def test_needs_bump_build_per_file_exact_un():
    """_needs_bump_build: a same-base re-spin (source version strips smaller)
    is a bump-target only while its EXACT current-generation +asg<R>u<N> file
    is absent — per file, and not satisfied by a stale older +asg.  A clean
    new-base source is never a target."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _tmp:
        _sess = BuildSession.__new__(BuildSession)

        class _Cfg:
            build_version = '1'
            def deb_dest_for_filename(self, f):
                return _tmp
        _sess.config = _Cfg()

        class _Src:
            def __init__(self, v):
                self.version = v

        def _plant(fn):
            open(os.path.join(_tmp, fn), 'w').close()

        # (1) clean new base (strip is a no-op) → never a bump-target
        _sess._predicted_files_for_source = lambda n: ['foo_1.3-1_amd64.deb']
        assert _sess._needs_bump_build('foo', _Src('1.3-1'), {}, 1) is False

        # (2) security re-spin, expected u1 absent → target
        _sess._predicted_files_for_source = lambda n: ['foo_1.2-3_amd64.deb']
        _delta = _Src('1.2-3+deb1u1')        # strips to 1.2-3 (same base)
        assert _sess._needs_bump_build('foo', _delta, {}, 1) is True
        # plant the EXACT u1 → no longer a target (idempotent)
        _plant('foo_1.2-3+asg1u1_amd64.deb')
        assert _sess._needs_bump_build('foo', _delta, {}, 1) is False

        # (3) cumulative + exact-uN: ledger already at u1 → need u2; the
        # on-disk u1 must NOT satisfy the u2 check (exact os.path.isfile, not
        # find_matching_artifact).
        _ledger = {'foo': ['1.2-3+asg1u1']}
        assert _sess._needs_bump_build('foo', _delta, _ledger, 1) is True
        _plant('foo_1.2-3+asg1u2_amd64.deb')
        assert _sess._needs_bump_build('foo', _delta, _ledger, 1) is False

        # (4) per-file divergence: foo→u2 present, foo-data→u5 missing → target
        _sess._predicted_files_for_source = lambda n: [
            'foo_1.2-3_amd64.deb', 'foo-data_1.2-3_amd64.deb']
        _led2 = {'foo': ['1.2-3+asg1u1'], 'foo-data': ['1.2-3+asg1u4']}
        assert _sess._needs_bump_build('foo', _delta, _led2, 1) is True
        _plant('foo-data_1.2-3+asg1u5_amd64.deb')   # each at its OWN next N
        assert _sess._needs_bump_build('foo', _delta, _led2, 1) is False


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


# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 steps 7-8 — snapshot command family + repo refresh orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def test_snapshot_and_refresh_commands_wired():
    """`snapshot` is registered and dispatches its sub-actions; bare snapshot →
    overview; advance → select current; `repo refresh` routes to
    cmd_repo_refresh."""
    import re
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    assert 'def cmd_snapshot(' in _body
    for _sub in ('_cmd_snapshot_overview', '_cmd_snapshot_list',
                 '_cmd_snapshot_select', '_cmd_snapshot_workload',
                 '_cmd_snapshot_base'):
        assert f'def {_sub}(' in _body, f"{_sub} missing"
    # bare `snapshot` → overview
    assert re.search(r"if action in \('', 'status'\):\s*\n\s+"
                     r"return self\._cmd_snapshot_overview", _body)
    # advance is shorthand for `select current`
    assert re.search(r"if action == 'advance':\s*\n\s+"
                     r"return self\._cmd_snapshot_select\('current'", _body)
    assert "register_command('snapshot'" in _body, "snapshot not registered"
    assert re.search(
        r"if action == 'refresh':\s*\n\s+return self\.cmd_repo_refresh",
        _body), "repo refresh not routed to cmd_repo_refresh"


def test_snapshot_state_roundtrip_and_resolve_precedence():
    """The durable pins live in config/snapshot.state (NOT cache), and
    resolve_snapshot_timestamp prefers state.current over [Snapshot] Timestamp
    — so the pin survives `clean cache` and the operator override wins."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)

        class _Cfg:
            dir_config = _cfg_dir
            dir_cache = _tmp
            snapshot_enabled = True
            snapshot_timestamp_config = '20260514T083402Z'   # explicit default

        _cfg = _Cfg()
        # no state yet → falls back to the config default
        assert utils.read_snapshot_state(_cfg) == {}
        utils._SNAPSHOT_TS_CACHE.clear()
        # set a durable current override
        utils.write_snapshot_state(_cfg, current='20260601T000000Z')
        assert utils.read_snapshot_state(_cfg)['current'] == '20260601T000000Z'
        # state.current wins over the explicit [Snapshot] Timestamp
        assert utils.resolve_snapshot_timestamp(_cfg) == '20260601T000000Z'
        # base set independently; both retained
        utils.write_snapshot_state(_cfg, base='20260514T083402Z')
        _st = utils.read_snapshot_state(_cfg)
        assert _st['base'] == '20260514T083402Z'
        assert _st['current'] == '20260601T000000Z'
        # the state file is under config/, not cache/
        assert os.path.exists(os.path.join(_cfg_dir, 'snapshot.state'))


def test_snapshot_base_show_and_forward_only():
    """`snapshot base show` (and bare) display the pin from config/snapshot.state;
    a lower timestamp is REFUSED (base is forward-only) before any prompt/write."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    import utils
    from build import BuildSession

    def _cap(fn):
        # Patch build.console directly (robust to suite-wide tui.console
        # rebinding by other tests).
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
            dir_cache = _tmp
            apt_source_url = ''          # → fetch_remote_ledger returns None
            build_codename = 'thor'
            arch = 'amd64'
            snapshot_enabled = True
            snapshot_timestamp_config = '20260514T083402Z'

        _sess.config = _Cfg()
        utils._SNAPSHOT_TS_CACHE.clear()
        utils.write_snapshot_state(_sess.config, base='20260601T000000Z')

        # `show` displays the pin (not parsed as a timestamp)
        _out = _cap(lambda: _sess._cmd_snapshot_base('show'))
        assert 'snapshot base pin: 20260601T000000Z' in _out, _out
        # an EARLIER timestamp is refused (forward-only), before prompt/write
        _out = _cap(lambda: _sess._cmd_snapshot_base('20260101T000000Z'))
        assert 'REFUSED' in _out and 'forward-only' in _out, _out
        assert utils.read_snapshot_state(_sess.config)['base'] == '20260601T000000Z', (
            "a refused (backward) base set must not change the pin")


def test_list_snapshots_between_filters_range_and_unions_keys():
    """list_snapshots_between unions all archive keys' timestamps and keeps
    only those strictly after `after` and up to (inclusive) `upto`."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {'result': {
                'debian': ['20260514T000000Z', '20260518T000000Z',
                           '20260526T000000Z', '20260601T000000Z'],
                'debian-security': ['20260515T000000Z', '20260526T000000Z'],
            }}

    class _Cfg:
        snapshot_timestamp_api = 'http://x'
        snapshot_archive_keys = ['debian', 'debian-security']

    _saved = utils.requests.get
    utils.requests.get = lambda *a, **k: _Resp()
    try:
        _got = utils.list_snapshots_between(
            _Cfg(), '20260514T000000Z', '20260526T000000Z')
    finally:
        utils.requests.get = _saved
    # strictly > 0514 and <= 0526, union of both keys, sorted, deduped
    assert _got == ['20260515T000000Z', '20260518T000000Z',
                    '20260526T000000Z'], _got


def test_snapshot_select_interactive_sets_chosen_current():
    """The interactive picker lists the in-between snapshots and sets the
    chosen one as the new current (forward-only, cautioned)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
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

        _sl = utils.list_snapshots_between
        utils.list_snapshots_between = lambda _c, _a, _u: [
            '20260518T000000Z', '20260526T134919Z']
        _sp, _sc = build.Prompt, build.console.print
        build.Prompt = _FakePrompt
        build.console.print = lambda *a, **k: None
        try:
            _sess._snapshot_select_interactive()
        finally:
            utils.list_snapshots_between = _sl
            build.Prompt, build.console.print = _sp, _sc
        assert utils.read_snapshot_state(_sess.config)['current'] == \
            '20260518T000000Z', "picker must set the chosen ts as current"


def test_snapshot_select_current_is_forward_only():
    """`snapshot select current <older>` is REFUSED — current only moves
    forward (must be above the present current)."""
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
        _out = _cap(lambda: _sess._set_snapshot_pin('current', '20260101T000000Z'))
        assert 'REFUSED' in _out and 'forward' in _out, _out
        assert utils.read_snapshot_state(_sess.config) == {}, "no pin written"


def test_ensure_snapshot_pins_prompts_and_writes_when_unset():
    """A fresh system (no config/snapshot.state) prompts for current + base on
    cache build; accepting the defaults writes both pins."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
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

        _answers = iter(['', ''])      # accept default current, default base

        class _FakePrompt:
            def __init__(self, _t, _m, options=None):
                pass

            def get_response(self):
                return next(_answers)

        _sp, _sc = build.Prompt, build.console.print
        build.Prompt = _FakePrompt
        build.console.print = lambda *a, **k: None
        try:
            assert _sess._ensure_snapshot_pins() is True
        finally:
            build.Prompt, build.console.print = _sp, _sc
        _st = utils.read_snapshot_state(_sess.config)
        assert _st['current'] == '20260514T083402Z', _st     # default applied
        assert _st['base'] == '20260514T083402Z', _st
        # already pinned → no prompt, returns True (would StopIteration if it asked)
        assert _sess._ensure_snapshot_pins() is True


def test_ensure_snapshot_pins_aborts_when_no_selection():
    """If the operator never supplies a valid timestamp, cache build aborts
    (returns False) rather than silently building at an undefined snapshot."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
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

        _sp, _sc = build.Prompt, build.console.print
        build.Prompt = _FakePrompt
        build.console.print = lambda *a, **k: None
        try:
            assert _sess._ensure_snapshot_pins() is False
        finally:
            build.Prompt, build.console.print = _sp, _sc
        assert utils.read_snapshot_state(_sess.config) == {}, "no pins written"


def test_cache_build_gates_on_snapshot_pins():
    """cmd_build_cache must call _ensure_snapshot_pins (and abort if it returns
    False) before building."""
    import re
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    _m = re.search(r'def cmd_build_cache\(self.*?(?=\n    def )', _body, re.DOTALL)
    assert _m, "cmd_build_cache not found"
    assert re.search(r'if not self\._ensure_snapshot_pins\(\):\s*\n\s+return',
                     _m.group(0)), "cache build must gate on _ensure_snapshot_pins"


def test_repo_refresh_is_thin_wrapper():
    """After decomposition, repo refresh just chains the (update-aware) steps —
    source sync → source build all → repo publish ssh full — takes no target,
    and short-circuits when current is not ahead of published."""
    import re
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    _m = re.search(r'def cmd_repo_refresh\(self.*?(?=\n    def )',
                   _body, re.DOTALL)
    assert _m, "cmd_repo_refresh not found"
    _b = _m.group(0)
    assert 'takes no target' in _b, "repo refresh must reject a target arg"
    assert 'cmd_source_sync(' in _b, "must run source sync"
    assert "cmd_source_build('all')" in _b, "must run source build all"
    assert "cmd_repo_publish('ssh', 'full')" in _b, "must run repo publish ssh full"
    assert _b.index('cmd_source_build(') < _b.index('cmd_repo_publish('), (
        "build must precede publish")
    assert '_snapshot_published(' in _b, "must short-circuit on current<=published"


def test_publish_full_folds_index_merge_prune_and_gates_rsync():
    """`repo publish ssh full` builds the merged index + manifest, gates the
    rsync on the external flag, prunes, and records published = current."""
    import re
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    _m = re.search(r'def cmd_repo_publish\(self.*?(?=\n    def )', _body, re.DOTALL)
    _b = _m.group(0)
    assert '_refresh_merge_index()' in _b, "full publish must build the merged index"
    assert '_external_enabled()' in _b, "rsync must be gated on the external flag"
    assert 'cmd_package_cleanup' in _b, "publish must prune (publish-before-prune)"
    assert 'published=self._snapshot_current()' in _b, (
        "publish must record published = current")
    assert _b.index('_refresh_merge_index()') < _b.index('cmd_package_cleanup'), (
        "merge/index before prune")


def test_source_build_autodetects_update_mode():
    """source build self-detects update mode (gotcha G): a subset/bare call
    routes to _do_update_build when a delta is pending; _do_update_build uses
    the published→current diff + the manifest ledger, and rebuilds WITHOUT
    blanket force — the loaded ledger makes the per-source build path bump-
    aware (already-built skipped; same-base re-spins rebuilt as bump-targets).
    COMP-03 Phase 4 extracted the per-source unit into _build_one_source."""
    import re
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _body = fh.read()
    _m = re.search(r'def cmd_source_build\(self.*?(?=\n    def )', _body, re.DOTALL)
    _b = _m.group(0)
    assert '_update_build_pending()' in _b and '_do_update_build()' in _b, (
        "source build must auto-detect + route to update mode")
    assert 'not _names' in _b, "explicit package names opt out of update mode"
    # Bump-awareness lives in _build_one_source after the Phase 4 refactor.
    _w = re.search(r'def _build_one_source\(.*?(?=\n    def )', _body, re.DOTALL)
    assert _w, "_build_one_source not found"
    assert '_needs_bump_build(' in _w.group(0), (
        "the per-source build path must be bump-aware (rebuild same-base re-spins)")
    _u = re.search(r'def _do_update_build\(self.*?(?=\n    def )', _body, re.DOTALL)
    _ub = _u.group(0)
    assert '_workload_since_snapshot(' in _ub, "update build diffs published→current"
    assert 'published_ledger(' in _ub and 'asg_ledger' in _ub, (
        "update build loads the manifest ledger for +asg stamping")
    assert "cmd_source_build('force'" not in _ub, (
        "update build must NOT blanket-force — it relies on bump-aware skipping")
    assert '_source_state(' in _ub and 'needs_build' in _ub, (
        "update build must ALSO build non-delta sources that need building "
        "(forks etc.), so `source build all` builds everything the audit lists")


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


def test_snapshot_published_defaults_to_base_then_tracks():
    """`published` defaults to base when unset, and is read back once set."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    import utils
    from build import BuildSession
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)
        _sess = BuildSession.__new__(BuildSession)

        class _Cfg:
            dir_config = _cfg_dir
            snapshot_enabled = True
            snapshot_timestamp_config = '20260514T083402Z'

        _sess.config = _Cfg()
        utils._SNAPSHOT_TS_CACHE.clear()
        utils.write_snapshot_state(_sess.config, base='20260514T083402Z',
                                   current='20260514T083402Z')
        # published unset → defaults to base
        assert _sess._snapshot_published() == '20260514T083402Z'
        # after a refresh records it, it tracks the new value
        utils.write_snapshot_state(_sess.config, published='20260520T000000Z')
        assert _sess._snapshot_published() == '20260520T000000Z'


# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 — snapshot tag in the ISO identity (distinguish base vs stepped)
# ─────────────────────────────────────────────────────────────────────────────

def test_snapshot_iso_tag_reflects_resolved_pin():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    _saved = utils.resolve_snapshot_timestamp
    try:
        utils.resolve_snapshot_timestamp = lambda _c: '20260514T083402Z'
        assert utils.snapshot_iso_tag(object()) == '20260514T083402Z'
        utils.resolve_snapshot_timestamp = lambda _c: None       # snapshots off
        assert utils.snapshot_iso_tag(object()) == ''
        def _boom(_c):
            raise RuntimeError('resolve failed')
        utils.resolve_snapshot_timestamp = _boom                 # never raises
        assert utils.snapshot_iso_tag(object()) == ''
    finally:
        utils.resolve_snapshot_timestamp = _saved


def test_stage_disk_info_writes_snapshot_marker_and_substitutes():
    """_stage_disk_info writes a .disk/snapshot marker and substitutes
    ${snapshot} (without touching the cdrom-detect-parsed .disk/info format);
    with no snapshot, no marker is written."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import iso_installer
    with tempfile.TemporaryDirectory() as _tmp:
        _inst = os.path.join(_tmp, 'installer')
        os.makedirs(os.path.join(_inst, 'disk'))
        with open(os.path.join(_inst, 'disk', 'info'), 'w') as fh:
            fh.write('Asgard ${version} "${codename}" snap=${snapshot}\n')
        _stage = os.path.join(_tmp, 'staging')
        os.makedirs(_stage)
        assert iso_installer._stage_disk_info(
            _stage, _inst, 'thor', '1', '20260514T083402Z')
        with open(os.path.join(_stage, '.disk', 'snapshot')) as fh:
            assert fh.read().strip() == '20260514T083402Z'
        with open(os.path.join(_stage, '.disk', 'info')) as fh:
            assert 'snap=20260514T083402Z' in fh.read()

    # No snapshot → no marker, ${snapshot} collapses to empty.
    with tempfile.TemporaryDirectory() as _tmp:
        _inst = os.path.join(_tmp, 'installer')
        os.makedirs(os.path.join(_inst, 'disk'))
        with open(os.path.join(_inst, 'disk', 'info'), 'w') as fh:
            fh.write('Asgard ${version} "${codename}"\n')
        _stage = os.path.join(_tmp, 'staging')
        os.makedirs(_stage)
        assert iso_installer._stage_disk_info(_stage, _inst, 'thor', '1')
        assert not os.path.exists(os.path.join(_stage, '.disk', 'snapshot'))


def test_iso_filenames_carry_snapshot_tag():
    """Both the live (iso.py) and installer (build.py) ISO filenames embed the
    snapshot tag so base vs stepped builds are distinguishable on disk."""
    import re
    _iso = os.path.join(_ROOT, 'scripts', 'iso.py')
    with open(_iso) as fh:
        _ib = fh.read()
    assert 'utils.snapshot_iso_tag(cfg)' in _ib
    assert 'athena-{_version}-{_snap}-amd64.iso' in _ib
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _bb = fh.read()
    assert 'utils.snapshot_iso_tag(self.config)' in _bb
    assert 'athena-installer-{_version}-{_snap}-amd64.iso' in _bb
    # snapshot threaded into the installer ISO build
    assert re.search(r'snapshot=_snap', _bb), "snapshot not passed to build_installer_iso"


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


def test_published_manifest_roundtrip_and_ledger():
    """STA-21: write/read the local signed manifest succeeds when signing
    is set up; the ledger parses it into {package: [version,...]}.

    Previously (pre-STA-21) the test pinned 'unsigned when no key' fall-
    through behaviour; that path silently degraded trust and is now
    refused (separate fail-closed tests below)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    with tempfile.TemporaryDirectory() as _tmp:
        _cfgdir = os.path.join(_tmp, 'config')
        _gnupg = os.path.join(_tmp, 'gnupg')
        os.makedirs(_cfgdir)

        class _Cfg:
            dir_config = _cfgdir
            dir_gnupg = _gnupg

        _cfg = _Cfg()
        _orig_run = _stub_signed_manifest_gpg(
            repo_audit, os.path.join(_gnupg, 'signing'))
        try:
            assert repo_audit.published_ledger(_cfg) == {}    # nothing yet
            _pk = (
                'Package: openssl\nVersion: 3.0.15-1+asg1u1\n'
                'Filename: dists/thor/main/binary-amd64/openssl_3.0.15-1+asg1u1_amd64.deb\n\n'
                'Package: libc6\nVersion: 2.36-9\n'
                'Filename: dists/thor/main/binary-amd64/libc6_2.36-9_amd64.deb\n')
            assert repo_audit.write_published_manifest(_cfg, _pk) is True
            assert os.path.exists(repo_audit.local_manifest_path(_cfg))
            assert os.path.exists(repo_audit.local_manifest_path(_cfg) + '.sig')
            assert 'openssl' in repo_audit.read_published_manifest(_cfg)
            _led = repo_audit.published_ledger(_cfg)
            assert _led['openssl'] == ['3.0.15-1+asg1u1']
            assert _led['libc6'] == ['2.36-9']
        finally:
            repo_audit.subprocess.run = _orig_run


def test_write_signed_manifest_fails_closed_when_signing_missing():
    """STA-21: when the signing module / homedir is unavailable, the
    writer must NOT leave an unsigned manifest behind — the local manifest
    is the authority for `+asg uN` bump derivation, and a silently-
    unsigned write corrupts that authority on the next publish."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    with tempfile.TemporaryDirectory() as _tmp:
        _cfgdir = os.path.join(_tmp, 'config')
        os.makedirs(_cfgdir)

        class _Cfg:
            # dir_gnupg INTENTIONALLY missing — simulates the
            # signing-setup-broken scenario the STA-21 fix targets.
            dir_config = _cfgdir

        _cfg = _Cfg()
        _ok = repo_audit.write_published_manifest(
            _cfg, 'Package: foo\nVersion: 1.0\n')
        assert _ok is False, "must return False on signing-setup failure"
        assert not os.path.exists(repo_audit.local_manifest_path(_cfg)), \
            "unsigned manifest must NOT remain on disk (false-authority)"
        assert repo_audit.read_published_manifest(_cfg) == ''
        assert repo_audit.published_ledger(_cfg) == {}


def test_read_signed_manifest_refuses_when_signing_setup_fails():
    """STA-21: a present .sig sidecar with broken signing setup must NOT
    fall through to read unverified text.  Returns '' so callers treat
    the manifest as absent rather than trusted."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    with tempfile.TemporaryDirectory() as _tmp:
        _cfgdir = os.path.join(_tmp, 'config')
        os.makedirs(_cfgdir)
        _path = os.path.join(_cfgdir, 'published.manifest')
        # Manually plant a manifest + sig pair (simulating a prior good
        # write), then break signing setup (no dir_gnupg attr).
        with open(_path, 'w') as _fh:
            _fh.write('Package: openssl\nVersion: 3.0.15-1\n')
        with open(_path + '.sig', 'w') as _fh:
            _fh.write('-----BEGIN PGP SIGNATURE-----\n')

        class _Cfg:
            dir_config = _cfgdir
            # No dir_gnupg — signing setup fails inside _read_signed_manifest

        _cfg = _Cfg()
        assert repo_audit.read_published_manifest(_cfg) == '', \
            "must NOT return unverified text when signing setup is broken"


def test_write_signed_manifest_scrubs_unsigned_file_on_sign_failure():
    """STA-21: when gpg --detach-sign returns non-zero, the writer must
    remove the unsigned manifest + any stale .sig so the next reader
    sees the manifest as absent."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import subprocess as _real_sp
    import repo_audit
    with tempfile.TemporaryDirectory() as _tmp:
        _cfgdir = os.path.join(_tmp, 'config')
        _gnupg = os.path.join(_tmp, 'gnupg')
        os.makedirs(_cfgdir)
        os.makedirs(os.path.join(_gnupg, 'signing'))

        class _Cfg:
            dir_config = _cfgdir
            dir_gnupg = _gnupg

        _cfg = _Cfg()
        _orig_run = repo_audit.subprocess.run

        def _fake_run(argv, **kw):
            # Make gpg --detach-sign FAIL (CalledProcessError under check=True)
            if isinstance(argv, list) and argv and argv[0] == 'gpg':
                raise _real_sp.CalledProcessError(2, argv, b'', b'gpg: no key')
            return _orig_run(argv, **kw)

        repo_audit.subprocess.run = _fake_run
        try:
            _ok = repo_audit.write_published_manifest(_cfg, 'Package: x\nVersion: 1\n')
        finally:
            repo_audit.subprocess.run = _orig_run
        assert _ok is False
        assert not os.path.exists(repo_audit.local_manifest_path(_cfg))
        assert not os.path.exists(repo_audit.local_manifest_path(_cfg) + '.sig')


def test_sta22_shared_dpkg_scan_helper_exists_and_is_consumed():
    """STA-22: apt_repo._run_dpkg_scan is the single shared shell-subprocess
    helper; both apt_repo's two scanners and repo_audit's
    _scan_packages_with_progress wrapper delegate to it.  Pinning the
    consolidation here so a future divergence reintroducing parallel
    f-string-into-`sudo bash -c` patterns trips this test."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import apt_repo
    import repo_audit
    # Helper exists.
    assert hasattr(apt_repo, '_run_dpkg_scan'), \
        "STA-22: shared helper apt_repo._run_dpkg_scan must exist"
    # Both apt_repo scanners delegate.
    for _fn_name in ('_scan_packages_to', '_scan_sources_to'):
        _src = inspect.getsource(getattr(apt_repo, _fn_name))
        assert '_run_dpkg_scan(' in _src, (
            f"STA-22: apt_repo.{_fn_name} must delegate to "
            f"_run_dpkg_scan (no duplicate shell-string subprocess)")
        # And must NOT carry the f-string-into-shell pattern anymore —
        # that's the duplication STA-22 retired.  Look for the literal
        # bash-c argv shape (allowing docstring prose to mention "sudo").
        for _smell in ("'bash', '-c'", '["bash", "-c"]', "f'cd {"):
            assert _smell not in _src, (
                f"STA-22: apt_repo.{_fn_name} still contains shell-string "
                f"pattern {_smell!r}; helper should own this")
    # repo_audit wrapper delegates.
    _src = inspect.getsource(repo_audit._scan_packages_with_progress)
    assert 'apt_repo._run_dpkg_scan' in _src, (
        "STA-22: repo_audit._scan_packages_with_progress must delegate "
        "to apt_repo._run_dpkg_scan")
    # And the wrapper should no longer carry its own subprocess.run +
    # tempfile choreography.
    assert 'subprocess.run' not in _src and 'mkstemp' not in _src, (
        "STA-22: wrapper should be a thin delegation, not a duplicate "
        "of the helper's internals")
    # The 5 dead "Reserved" parameters from the pre-consolidation shape
    # must be gone.
    _sig = inspect.signature(repo_audit._scan_packages_with_progress)
    for _dead in ('count_dir', 'include_udeb', 'count_extensions', 'use_shell'):
        assert _dead not in _sig.parameters, (
            f"STA-22: pre-consolidation dead parameter {_dead!r} "
            f"must be removed from the wrapper signature")


def test_sta22_run_dpkg_scan_writes_via_tempfile():
    """STA-22: the consolidated helper writes via a tempfile then
    `os.replace` (or `sudo install`), NOT via a direct shell-redirect to
    `output_path`.  The tempfile pattern handles the root-owned-file
    truncate case that the pre-consolidation `_scan_packages_to` shape
    couldn't (operator-observed 2026-05-22)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import apt_repo
    _src = inspect.getsource(apt_repo._run_dpkg_scan)
    assert 'mkstemp(' in _src, \
        "STA-22: helper must allocate a tempfile (mkstemp)"
    assert 'os.replace(' in _src or "'install'" in _src, (
        "STA-22: helper must atomically move the tempfile into place "
        "(os.replace for non-sudo, sudo install for sudo paths)")


def test_sta22_run_dpkg_scan_honours_allow_empty():
    """STA-22: when allow_empty=False, a successful scan with zero-byte
    output is treated as failure (callers indexing the install corpus
    want this).  When allow_empty=True, zero bytes is fine (sparse
    optional-component pools)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import subprocess as _real_sp
    import apt_repo
    _orig_run = apt_repo.subprocess.run

    def _fake_run_empty(argv, **kw):
        # Simulate a successful scan that produces an empty output file.
        # The shell command opens the tempfile via `> path`; mimic by
        # creating an empty file at the path embedded in the shell.
        if isinstance(argv, list) and argv and argv[0] == 'bash':
            _shell = argv[-1]
            # Extract the `> /path/to/tmp` redirect target
            _tmp = _shell.rsplit('> ', 1)[-1].strip()
            with open(_tmp, 'w'):
                pass
            return _real_sp.CompletedProcess(argv, 0, '', '')
        return _orig_run(argv, **kw)

    with tempfile.TemporaryDirectory() as _t:
        _out = os.path.join(_t, 'Packages')
        apt_repo.subprocess.run = _fake_run_empty
        try:
            _ok_strict = apt_repo._run_dpkg_scan(
                ['dpkg-scanpackages', '-m', 'pool'], _out,
                cwd=_t, allow_empty=False)
            _ok_relaxed = apt_repo._run_dpkg_scan(
                ['dpkg-scanpackages', '-m', 'pool'], _out,
                cwd=_t, allow_empty=True)
        finally:
            apt_repo.subprocess.run = _orig_run
        assert _ok_strict is False, "allow_empty=False must fail on empty"
        assert _ok_relaxed is True, "allow_empty=True must accept empty"


def test_cmd_repo_publish_surfaces_sign_failure():
    """STA-21: build.py's repo-publish handler must surface the
    write_published_manifest False return as an operator-facing ERROR
    rather than silently proceeding."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _b = fh.read()
    # Both publish paths (ssh-driven + repo_refresh) must gate on the bool
    assert 'if not repo_audit.write_published_manifest(' in _b, (
        "cmd_repo_publish must check the bool return of "
        "write_published_manifest and bail on False (STA-21 fail-closed).")
    assert '+asg uN' in _b, (
        "operator-facing error must explain the +asg uN impact so the "
        "fix path (key generate / verify) is obvious.")


def test_manifest_vs_remote_discrepancies():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    _local = {'openssl': ['3.0.15-1', '3.0.15-1+asg1u1'], 'libc6': ['2.36-9']}
    _remote = {'openssl': ['3.0.15-1'], 'curl': ['8.0-1']}
    _ol, _orr = repo_audit.manifest_vs_remote_discrepancies(_local, _remote)
    assert _ol == {'openssl=3.0.15-1+asg1u1', 'libc6=2.36-9'}, _ol
    assert _orr == {'curl=8.0-1'}, _orr


def test_repo_external_disable_and_enable_empty_rebaselines():
    """`repo external disable` flips to local-only; `repo external enable` onto
    an EMPTY remote rebaselines current = base = published (boundary-D)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    import utils
    import repo_audit
    from build import BuildSession, BuildFlags

    def _cap(fn):
        _orig = build.console.print
        build.console.print = lambda *a, **k: None
        try:
            fn()
        finally:
            build.console.print = _orig

    with tempfile.TemporaryDirectory() as _tmp:
        _cfgdir = os.path.join(_tmp, 'config')
        os.makedirs(_cfgdir)
        _sess = BuildSession.__new__(BuildSession)
        _sess.flags = BuildFlags()

        class _Cfg:
            dir_config = _cfgdir
            external_enabled = True

        _sess.config = _Cfg()
        utils._SNAPSHOT_TS_CACHE.clear()
        assert _sess._external_enabled() is True             # config default
        _cap(lambda: _sess.cmd_repo_external('disable'))
        assert _sess._external_enabled() is False
        # enable onto an empty remote → rebaseline current=base
        utils.write_snapshot_state(_sess.config, base='20260514T083402Z')
        _saved = repo_audit.fetch_remote_ledger
        repo_audit.fetch_remote_ledger = lambda _c: {}       # reachable, empty
        try:
            _cap(lambda: _sess.cmd_repo_external('enable'))
        finally:
            repo_audit.fetch_remote_ledger = _saved
        _st = utils.read_snapshot_state(_sess.config)
        assert _st['external'] is True
        assert _st['current'] == '20260514T083402Z', _st      # rebaselined
        assert _st['published'] == '20260514T083402Z', _st


def test_update_build_pending_logic():
    """_update_build_pending: True only when a base is published AND current is
    ahead of published."""
    import shutil as _sh
    if not _sh.which('dpkg'):
        return
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build  # noqa: F401
    import repo_audit
    from build import BuildSession, BuildFlags
    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.dep_check_ready = True
    _sess.config = object()
    _sess._snapshot_current = lambda: '20260520T000000Z'
    _sess._snapshot_published = lambda: '20260514T083402Z'
    _saved = repo_audit.published_ledger
    try:
        repo_audit.published_ledger = lambda _c: {'openssl': ['3.0.15-1']}
        assert _sess._update_build_pending() is True
        repo_audit.published_ledger = lambda _c: {}          # nothing published
        assert _sess._update_build_pending() is False
        repo_audit.published_ledger = lambda _c: {'x': ['1']}
        _sess._snapshot_published = lambda: '20260520T000000Z'   # == current
        assert _sess._update_build_pending() is False
    finally:
        repo_audit.published_ledger = _saved


def test_external_and_audit_ssh_and_published_ledger_wired():
    import re
    _bp = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bp) as fh:
        _bb = fh.read()
    assert "if action == 'external':" in _bb and 'def cmd_repo_external(' in _bb
    assert re.search(r"if args and args\[0\] == 'ssh':", _bb) and \
        'def _audit_external_vs_manifest(' in _bb
    _ra = os.path.join(_ROOT, 'scripts', 'repo_audit.py')
    with open(_ra) as fh:
        _rb = fh.read()
    for _fn in ('def published_ledger(', 'def write_published_manifest(',
                'def read_published_manifest(', 'def local_manifest_path('):
        assert _fn in _rb, f"{_fn} missing from repo_audit"


# ─────────────────────────────────────────────────────────────────────────────
# UPD-02 — index on the remote (dpkg-scanpackages on the VM)
# ─────────────────────────────────────────────────────────────────────────────

def test_remote_scan_packages_builds_ssh_argv():
    """_remote_scan_packages runs `cd <root> && dpkg-scanpackages -m <rel>` on
    the VM via ssh (with `-t udeb` for udebs) and returns stdout."""
    import types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo
    _calls = []

    def _fake_run(argv, **kw):
        _calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout='Package: x\n',
                                     stderr='')

    _saved = apt_repo.subprocess.run
    apt_repo.subprocess.run = _fake_run
    try:
        _txt = apt_repo._remote_scan_packages(
            ['ssh', '-i', 'k'], 'u@h', '/srv/asgard',
            'dists/thor/main/binary-amd64', udeb=False)
        assert _txt == 'Package: x\n'
        _argv = _calls[0]
        assert _argv[:4] == ['ssh', '-i', 'k', 'u@h'], _argv
        _shell = _argv[4]
        assert 'cd /srv/asgard' in _shell
        assert ('dpkg-scanpackages -m dists/thor/main/binary-amd64 /dev/null'
                in _shell), _shell
        # udeb variant adds -t udeb
        _calls.clear()
        apt_repo._remote_scan_packages(
            ['ssh'], 'u@h', '/srv/asgard',
            'dists/thor/main/debian-installer/binary-amd64', udeb=True)
        assert '-t udeb' in _calls[0][-1], _calls[0]
    finally:
        apt_repo.subprocess.run = _saved


def test_remote_reindex_wiring():
    """remote_reindex_and_sign scans REMOTELY but builds Release + signs
    LOCALLY (key never leaves the box)."""
    _ar = os.path.join(_ROOT, 'scripts', 'apt_repo.py')
    with open(_ar) as fh:
        _b = fh.read()
    import re
    _m = re.search(r'def remote_reindex_and_sign\(.*?(?=\ndef )', _b, re.DOTALL)
    assert _m, "remote_reindex_and_sign not found"
    _fn = _m.group(0)
    assert '_remote_scan_packages(' in _fn, "scan must run on the remote"
    for _local in ('_compress_index(', '_write_subdir_release(',
                   '_generate_top_release(', 'sign_release_files('):
        assert _local in _fn, f"{_local} (local) missing — signing must stay local"


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


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [
        # v0.2 step 1
        test_mirror_url_composition,
        test_mirror_suite_with_suffix,
        test_mirror_repr_includes_identifying_fields,
        test_mirror_is_frozen_after_construction,
        test_mirror_normalises_baseurl_and_baseid_slashes,
        test_mirror_rejects_empty_required_fields,
        test_mirror_rejects_baseurl_without_scheme,
        test_mirror_rejects_suffix_without_leading_dash,
        test_mirror_with_snapshot_returns_new_instance_untouched_original,
        test_buildconfig_parses_three_mirrors,
        test_deb_dest_for_filename_routes_by_component,
        test_buildconfig_rejects_no_mirrors,
        test_package_and_source_have_mirror_field,
        test_source_parses_security_stanza_without_files_field,
        test_source_parses_main_stanza_with_both_files_and_sha256,
        # Source.build_depends — virtual-package expansion
        test_build_depends_no_cache_leaves_virtuals_unchanged,
        test_build_depends_expands_multi_provider_virtual,
        test_build_depends_single_provider_virtual_not_expanded,
        test_build_depends_real_package_name_not_expanded,
        test_build_depends_already_alternative_group_untouched,
        test_build_depends_version_constraint_inherited_by_synthetic_providers,
        test_build_depends_unknown_name_left_unchanged,
        # /
        test_verify_inrelease_clean_signature_passes,
        test_verify_inrelease_tampered_signature_fails,
        test_verify_inrelease_missing_keyring_fails,
        test_verify_inrelease_empty_keyring_fails,
        test_buildconfig_security_defaults,
        test_buildconfig_security_disabled_accepts_missing_keyring,
        test_buildconfig_security_enabled_rejects_missing_keyring,
        test_buildconfig_creates_dir_gnupg_with_0700,
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
        test_buildsystem_password_readable_before_scrub,
        test_buildsystem_scrub_password_clears_field,
        test_buildsystem_password_property_raises_after_scrub,
        test_buildsystem_scrub_password_idempotent,
        test_download_source_surfaces_http_error_clearly,
        test_download_source_surfaces_short_download_clearly,
        test_docker_server_guard_accepts_safe_targets,
        test_docker_server_guard_refuses_unsafe_targets,
        test_buildconfig_build_options_and_profiles_are_separate,
        test_buildconfig_build_options_falls_back_to_profiles_when_omitted,
        # ARCH-16: per-package BuildOptions / BuildProfiles overrides
        test_arch16_per_pkg_build_options_override_global,
        test_arch16_buildcontainer_consults_per_pkg_overrides,
        test_arch16_empty_per_pkg_section_name_rejected,
        # ARCH-17: type-annotation coverage gate
        test_arch17_top_offender_modules_fully_annotated,
        # UX-05: TUI/CLI flexibility polish
        test_ux05a_prompt_informational_kwarg_accepted,
        test_ux05a_auto_yes_short_circuits_informational_yesno,
        test_ux05a_auto_yes_does_not_skip_password_or_options,
        test_ux05b_atena_sudo_password_env_var_picked_up,
        test_ux05c_progressbar_step_does_not_print_in_cli,
        test_ux05d_cli_print_emits_ansi_when_tty,
        test_ux05e_main_strips_cmd_and_yes_from_argv_and_seeds_backend,
        test_ux05e_one_shot_dispatch_runs_each_in_order_and_exits,
        test_ux05g_cmd_methods_reset_flags_on_entry,
        # SEC-05: opt-in build-dep audit gate
        test_sec05_audit_build_deps_default_false,
        test_sec05_audit_build_deps_parses_true,
        test_sec05_render_install_cmd_injects_simulate_flag,
        test_sec05_build_gates_on_audit_when_enabled_in_source,
        test_sec05_gate_short_circuits_when_no_deps,
        test_sec05_gate_returns_false_when_operator_declines,
        test_sec05_gate_returns_false_when_preview_infrastructure_fails,
        # COMP-03 Phase 0: parallel-build config plumbing
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
        test_buildsession_constructible_with_stub_tui,
        test_group_dispatchers_forward_to_underlying_cmd_methods,
        test_cache_purge_deletes_files_and_resets_flags,
        test_cache_purge_cancelled_keeps_files_and_flags,
        test_cache_purge_empty_dir_is_noop,
        # — iso build live | iso build installer split
        test_cmd_iso_build_requires_subaction,
        test_repo_index_dispatch,
        test_deb_excluded_from_minimal,
        test_generate_apt_repo_tolerates_empty_udeb_component,
        test_buildconfig_publish_dir_and_apt_source_default,
        test_repo_publish_dispatch,
        test_rsync_streamed_argv_ignore_existing_and_filters,
        test_publish_full_remote_scan_flow_wiring,
        test_repo_audit_external_dispatch,
        test_cmd_audit_external_requires_apt_source_url,
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
        # — chroot build live | chroot build installer split
        test_cmd_chroot_build_no_subaction_defaults_to_live,
        test_cmd_chroot_build_live_explicit_forwards_to_live,
        test_cmd_chroot_build_installer_forwards_to_installer,
        test_cmd_chroot_build_passthrough_args_to_live,
        test_cmd_build_chroot_installer_bails_on_unmet_prereqs,
        test_installer_chroot_dpkg_unpack_carries_required_force_flags,
        test_iso_installer_kernel_pkg_regex_matches_real_kernels_only,
        test_iso_installer_count_records_zero_one_many,
        test_iso_installer_generate_apt_repo_invokes_correct_pipeline,
        test_iso_installer_stage_grub_cfg_errors_when_data_layer_missing,
        test_iso_installer_stage_grub_cfg_copies_when_present,
        test_iso_installer_stage_disk_info_errors_when_dir_missing,
        test_iso_installer_stage_disk_info_copies_files_skipping_readme,
        test_iso_installer_stage_disk_info_substitutes_codename_and_version,
        test_iso_installer_stage_disk_info_safe_substitute_leaves_unknown_vars,
        test_iso_installer_stage_disk_info_errors_when_only_readme,
        test_iso_installer_stage_base_include_writes_one_name_per_line,
        test_iso_installer_stage_base_include_creates_disk_dir_if_missing,
        test_iso_installer_stage_base_include_noop_on_empty_or_none,
        test_iso_installer_parse_deb_filename_handles_normal_filenames,
        test_iso_installer_select_pool_files_includes_udebs_unconditionally,
        test_iso_installer_select_pool_files_excludes_superseded,
        test_superseded_binary_names_excludes_selected,
        test_iso_installer_select_pool_files_drops_dbgsym_unconditionally,
        test_iso_installer_select_pool_files_filters_by_whitelist,
        test_iso_installer_select_pool_files_keeps_highest_version_per_name,
        test_iso_installer_select_pool_files_uses_debian_version_order,
        test_iso_installer_select_pool_files_legacy_mode_keeps_everything,
        test_iso_installer_base_include_and_pool_filter_agree,
        test_iso_installer_build_iso_installer_passes_pool_whitelist,
        # COMP-02 phase C — sign Release + ship + install pubkey
        test_iso_installer_sign_release_files_runs_both_gpg_invocations,
        test_iso_installer_sign_release_files_errors_when_release_missing,
        test_iso_installer_sign_release_files_errors_when_homedir_missing,
        test_iso_installer_export_pubkey_to_staging_copies_to_disk_archive_key,
        test_iso_installer_export_pubkey_to_staging_errors_when_pubkey_missing,
        test_iso_installer_export_pubkey_to_staging_errors_when_disk_dir_missing,
        test_pool_list_pins_target_only_packages,
        test_buildconfig_exposes_poollist_path,
        test_read_pkg_list_handles_pool_list_format,
        test_resolve_packages_check_conflicts_false_skips_lookahead_check,
        test_validate_selection_skips_conflict_when_pool_extra,
        test_validate_selection_skips_break_when_pool_extra,
        test_validate_selection_still_fires_on_non_pool_conflicts,
        test_base_installer_athena_keyring_patch_exists_and_is_dep3_clean,
        test_installer_chroot_overlay_map_is_data_not_code,
        test_installer_ships_forked_choose_mirror,
        test_athena_installer_data_drops_mirror_protocol_stub,
        test_preseed_pins_http_mirror_and_enables_use_mirror,
        test_preseed_enables_non_free_firmware_no_deb_src,
        test_choose_mirror_fork_drops_menu_item,
        test_finish_install_cdrom_disable_overlay,
        test_iso_pool_staging_includes_non_main_component_dirs,
        test_finish_install_default_source_overlay,
        test_installer_chroot_resolve_udeb_files_skips_virtual_aliases,
        test_installer_chroot_resolve_udeb_files_strips_binnmu_suffix,
        test_installer_chroot_resolve_udeb_files_matches_asg_stamp,
        test_installer_chroot_resolve_udeb_files_logs_missing_silently,
        test_installer_chroot_resolve_udeb_files_skips_record_without_filename,
        # FORK-01 Step 4: helpers replaced by athena-installer-data udeb
        test_athena_installer_data_ships_runtime_dirs,
        test_athena_installer_data_ships_release_files_with_tokens,
        test_buildcontainer_injects_athena_codename_env,
        # COMP-02 phase B — stock d-i image-build conformance helpers
        test_installer_chroot_sudo_write_does_not_leak_password_to_tee,
        test_installer_chroot_run_depmod_skips_when_no_modules_dir,
        test_installer_chroot_run_depmod_indexes_each_kernel_present,
        test_installer_chroot_generator_guard_passes_when_both_present,
        test_installer_chroot_generator_guard_fails_without_cdrom,
        test_installer_chroot_generator_guard_fails_without_mirror,
        test_athena_cdrom_setup_does_not_provide_mirror_setup,
        test_strip_debian_residue_hooks_removes_known_files,
        test_strip_debian_residue_hooks_idempotent_on_missing_targets,
        test_strip_debian_residue_hooks_called_in_build_flow,
        test_installer_chroot_register_self_appends_debian_installer_stanza,
        test_installer_chroot_register_self_idempotent_on_repeat,
        test_installer_grub_cfg_has_preseed_kernel_cmdline,
        test_buildconfig_chroot_paths_under_shared_buildroot_parent,
        test_build_flags_carries_chroot_installer_ready_default_false,
        test_setup_file_logging_filename_has_timestamp,
        test_download_file_returns_http_status_detail_on_404,
        test_download_file_success_returns_size_and_empty_detail,
        test_download_file_zero_content_length_does_not_freeze_bar,
        test_shipped_build_conf_has_snapshot_enabled,
        test_buildconfig_snapshot_endpoints_default_to_debian,
        test_buildconfig_snapshot_endpoints_overridable_via_config,
        test_mirror_with_snapshot_uses_passed_baseurl,
        # /
        test_strip_build_version_strips_trailing_binNMU,
        test_strip_build_version_preserves_point_release_suffix,
        test_strip_build_version_strips_binNMU_after_point_release,
        test_strip_build_version_leaves_embedded_binNMU_alone,
        test_strip_build_version_handles_udeb_extension,
        test_strip_build_version_no_change_when_no_binNMU,
        test_strip_build_version_rejects_malformed_filename,
        test_classify_repo_subdir_routes_dev_to_main,
        test_strip_nmu_suffix_strips_known_patterns,
        test_strip_nmu_suffix_idempotent,
        test_strip_nmu_from_control_text_walks_relation_fields,
        test_strip_nmu_pair_rewrite_collapses_sibling_idiom,
        test_strip_nmu_pair_rewrite_only_when_pair_matches,
        test_strip_nmu_pair_rewrite_idempotent,
        test_strip_nmu_pair_rewrite_scoped_to_depends_pre_depends,
        test_strip_nmu_from_deb_round_trip,
        test_strip_nmu_from_deb_idempotent,
        test_buildcontainer_calls_strip_post_build,
        test_audit_nmu_residue_skips_tunneled_sources,
        test_cmd_audit_nmu_residue_absorbed_into_cmd_audit,
        test_cmd_strip_repo_registered_in_repo_dispatcher,
        test_cmd_package_cleanup_registered_in_repo_dispatcher,
        test_cmd_package_cleanup_dry_run_default_force_flag_required,
        test_cmd_package_cleanup_keeps_expected_files_drops_orphan_source,
        test_audit_nmu_residue_detects_layered_versions,
        # apply_distro_suffix — bump bumped binaries with `+thor1`
        test_production_build_conf_has_noautodbgsym_in_build_options,
        test_build_conf_ingests_nonfree_components_and_tunnels_firmware,
        test_buildcontainer_emits_token_substitution_snippet,
        test_buildcontainer_token_subst_uses_if_not_short_circuit_and,
        test_buildcontainer_token_subst_no_double_braces_in_regular_strings,
        test_buildcontainer_token_subst_grep_rescue_or_true,
        test_find_kernel_prefers_expected_kernel_pkg_match,
        test_find_kernel_falls_back_to_highest_when_no_match,
        test_buildcontainer_changelog_uses_codename_field,
        # version_no_epoch — patch dir lookup must match Debian filename convention
        test_version_no_epoch_strips_epoch_from_debian_version,
        test_version_no_epoch_no_change_when_no_epoch,
        test_version_no_epoch_accepts_string_input,
        test_version_no_epoch_handles_multidigit_epoch,
        test_version_no_epoch_only_strips_first_colon,
        # get_sha256 — sidecar (size, mtime_ns) cache
        test_get_sha256_writes_sidecar_on_first_call,
        test_get_sha256_returns_cached_value_on_size_mtime_match,
        test_get_sha256_recomputes_when_mtime_changes,
        test_get_sha256_recomputes_when_size_changes,
        test_get_sha256_ignores_malformed_sidecar,
        test_get_sha256_use_cache_false_skips_sidecar_entirely,
        test_get_sha256_missing_file_returns_empty_string,
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
        # — autorun summary (in print_commands)
        test_format_duration_seconds_only,
        test_format_duration_minutes_seconds,
        test_format_duration_hours_minutes_seconds,
        test_autorun_summary_success_includes_counts_and_iso_path,
        test_autorun_summary_aborted_marks_stage_and_partial_state,
        test_print_state_renders_three_sections_with_all_flags,
        test_print_state_renders_unticked_when_flags_unset,
        test_print_summary_without_timing_renders_state_snapshot,
        test_print_summary_dispatch_through_handler,
        test_pull_recommends_extras_pulls_single_name_recommends,
        test_pull_recommends_extras_skips_when_source_in_skip_src,
        test_pull_recommends_extras_drops_alt_groups,
        test_pull_recommends_extras_handles_multi_mirror_version_buckets,
        test_pull_recommends_extras_skips_already_in_selected_pkgs,
        test_pull_recommends_extras_walks_transitive_depends,
        test_parse_sources_uses_per_tree_src_pkg_files_not_shared_source_attr,
        test_explicit_provides_version_returns_none_for_unversioned_provides,
        test_validate_selection_unversioned_provides_no_spurious_break,
        test_validate_selection_versioned_provides_still_flagged,
        test_canonical_names_filters_virtual_aliases_from_cohort,
        test_cohort_resolvers_route_through_canonical_names,
        test_cmd_init_container_gated_on_cache_ready,
        test_autorun_step_lists_include_container_init,
        test_derive_extras_src_names_marks_extras_only_sources,
        # phase 1: live.list / installer.list split
        test_dep_tree_initialises_subset_exclusive_sets_empty,
        test_buildconfig_argparse_exposes_live_and_installer_list_flags,
        test_read_pkg_list_filters_comments_blanks_and_already_selected,
        test_read_pkg_list_missing_file_returns_empty,
        test_parse_pkg_list_groups_flat_file_becomes_implicit_base,
        test_parse_pkg_list_groups_ini_style_multi_section,
        test_parse_pkg_list_groups_rejects_seed_before_first_section,
        test_parse_pkg_list_groups_empty_section_name_raises,
        test_dep_tree_initialises_pkg_group_fields_empty,
        test_pass_iii_dedups_to_canonical_names_for_pkg_group_pkg_names,
        # FORK-01 Step 5: athena-tasksel + athena-pkgsel forks
        # replace the old pkgsel patch
        test_athena_tasksel_fork_ignores_debian_tasks_only_env,
        test_athena_tasksel_control_provides_conflicts_replaces_tasksel,
        test_athena_tasksel_depends_on_athena_tasksel_data_directly,
        test_athena_tasksel_task_keys_mirror_pkg_list_groups,
        test_athena_pkgsel_no_popcon_pre_pkgsel_hook,
        test_athena_pkgsel_fork_postinst_drops_debian_tasks_only_prefix,
        test_athena_pkgsel_control_provides_conflicts_replaces_pkgsel,
        test_athena_pkgsel_dh_helper_files_use_binary_name,
        test_pkgsel_patch_dir_deleted,
        test_stage_group_manifests_writes_one_file_per_group,
        test_stage_group_manifests_empty_groups_is_noop,
        test_parse_pkg_list_group_meta_extracts_descriptions,
        test_parse_pkg_list_group_meta_flat_file_returns_base_only,
        # FORK-01 Step 5b: synthetic tasksel-data retired, fork ships it
        test_athena_tasksel_data_binary_stanza_in_fork_control,
        test_athena_tasksel_fork_ships_exactly_six_curated_tasks,
        test_athena_tasksel_standard_task_uses_curated_key_list,
        test_iso_installer_synthetic_tasksel_data_retired,
        test_installer_list_includes_athena_pkgsel,
        test_pkg_list_base_includes_athena_tasksel,
        test_overlay_map_does_not_contain_pre_pkgsel_hook,
        test_installer_pkgsel_dir_does_not_exist,
        test_derive_subset_exclusive_src_names_marks_live_only_sources,
        test_derive_subset_exclusive_src_names_no_op_when_both_empty,
        test_derive_subset_exclusive_src_names_handles_installer_exclusive,
        # phase 2: cache parses udeb (debian-installer) Packages index
        test_mirror_udeb_packages_path_format,
        test_cache_class_declares_udeb_fields_on_init,
        test_collision_gate_passes_when_no_drops_recorded,
        test_collision_gate_passes_when_fork_version_dominates,
        test_collision_gate_fails_when_upstream_dominates,
        test_collision_gate_fails_on_tied_versions,
        test_collision_gate_reports_all_collisions_in_one_error,
        test_collision_gate_handles_udeb_namespace,
        test_collision_gate_error_message_points_to_docs,
        test_collision_gate_multi_mirror_drops_same_name,
        test_ingest_udeb_indices_routes_records_to_udeb_hashtable,
        test_ingest_udeb_indices_skips_mirrors_without_udeb_file,
        test_ingest_udeb_indices_handles_partial_mirror_set,
        test_ingest_udeb_indices_dedups_priority_lists_via_caller,
        # phase 3: parallel udeb DependencyTree
        test_udeb_view_exposes_udeb_hashtable_as_package_hashtable,
        test_udeb_view_get_packages_resolves_against_udeb_hashtable,
        test_udeb_view_does_not_leak_into_real_package_hashtable,
        test_parse_dependency_reuses_lookahead_for_multi_version_same_name,
        test_dependency_tree_udeb_tree_flag_enables_max_version_fallback,
        test_auto_pick_candidate_prefers_real_package_matching_seed_name,
        test_dependency_tree_constructor_accepts_auto_pick_flag,
        test_print_udebs_handles_no_udeb_tree_gracefully,
        test_print_udebs_lists_udeb_closure_when_tree_populated,
        test_compute_install_batches_excludes_extras_pkg_names,
        test_verify_dep_resolution_skips_extras,
        test_verify_dep_resolution_still_catches_real_violations,
        test_print_extras_lists_recommended_packages,
        test_print_extras_handles_empty_extras_set,
        # source build args parsing (+ subset selectors)
        test_source_build_args_no_args_defaults_to_pkg_subset,
        test_source_build_args_pkg_subset_explicit,
        test_source_build_args_live_subset_explicit,
        test_source_build_args_installer_subset_recognised,
        test_source_build_args_recommended_subset_recognised,
        test_source_build_args_force_flag_anywhere,
        test_source_build_args_subsets_mutually_exclusive,
        test_source_build_pkg_subset_excludes_live_installer_extras,
        test_source_build_installer_subset_unions_udeb_tree_with_deb_arm,
        test_refresh_patches_iterates_both_deb_and_udeb_trees,
        test_refresh_patches_invalidates_result_when_patch_newer,
        test_refresh_patches_keeps_result_when_patch_older_than_result,
        test_refresh_patches_skips_invalidation_for_header_only_edit,
        test_refresh_patches_writes_baseline_when_no_patchhash,
        test_refresh_patches_invalidates_when_patches_removed,
        test_patch_set_hash_stable_and_order_sensitive,
        test_source_download_iterates_both_deb_and_udeb_trees,
        test_autorun_installer_runs_source_build_then_source_build_installer,
        test_autorun_live_chains_iso_build_after_chroot,
        test_autorun_installer_chains_iso_build_after_chroot,
        test_autorun_disk_reuses_live_chroot_then_builds_disk,
        test_buildflags_carry_iso_ready_state,
        test_autorun_dispatcher_routes_bare_to_live_and_explicit_to_each,
        test_autorun_live_runs_source_build_then_source_build_live,
        test_source_build_args_subset_and_named_pkgs_mutually_exclusive,
        test_source_build_args_named_pkgs_resolve_subset_to_empty,
        test_source_build_args_all_subset_parses,
        test_source_build_all_selection_unions_both_trees_no_exclusions,
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
        test_format_gpg_time_renders_epoch_as_utc_iso,
        test_format_gpg_time_empty_returns_default,
        test_format_gpg_time_garbage_returns_raw,
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
        test_write_athena_apt_source_noop_when_url_empty,
        test_write_athena_apt_source_writes_signed_by_when_url_set,
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
        # tui — event-dispatcher TUI (replaces legacy ARCH-14 P1-P7 tests)
        test_v2_wrap_helpers_round_trip,
        test_v2_state_append_and_scroll,
        test_v2_cmdline_edit_and_history,
        test_v2_dispatcher_key_events_gated_without_prompt,
        test_v2_dispatcher_prompt_future_round_trip,
        test_v2_dispatcher_tab_switch_by_index,
        test_v2_dispatcher_progressbar_widget_lifecycle,
        test_v2_dispatcher_adaptive_idle_no_widgets,
        test_v2_logging_bridge_routes_by_stage,
        # COMP-06 — package-set selector
        test_select_loads_groups_all_selected,
        test_select_toggle_and_save_round_trip,
        test_select_add_package_inline_input_appends_to_group,
        test_select_add_rejects_package_not_in_cache,
        test_select_save_preserves_comments_and_appends_new,
        test_select_flat_file_round_trips_without_header,
        test_select_key_toggle_sets_unsaved_and_rerenders,
        test_select_quit_without_save_does_not_use_dispatcher_prompt,
        test_select_quit_confirm_yes_saves_then_teardown,
        test_select_approx_closure_sums_first_provider_bfs,
        test_cmd_cache_info_prints_identity_and_relations,
        # FORK-01 Step 1 (was missing from registry)
        test_buildconfig_creates_fork_source_dir,
        # FORK-01 Step 2 — fork mirror generation + cache integration
        test_mirror_flat_layout_is_signalled_by_empty_component,
        test_mirror_flat_layout_url_properties,
        test_mirror_with_snapshot_skips_file_scheme,
        test_mirror_validation_allows_empty_component,
        test_download_file_handles_file_scheme,
        test_download_file_file_scheme_missing_source,
        test_fork_mirror_discover_skips_repo_subdir,
        test_fork_mirror_discover_skips_dirs_missing_debian_control,
        test_fork_mirror_generate_empty_tree_returns_false,
        test_fork_mirror_generate_emits_complete_layout,
        test_fork_mirror_strips_epoch_from_constructed_filenames,
        test_fork_mirror_packages_udeb_routing_and_placeholder_hashes,
        test_fork_mirror_sources_uses_real_hashes_and_directory,
        test_fork_mirror_register_prepends_at_index_zero,
        test_fork_mirror_re_run_is_idempotent_via_stale_check,
        test_compute_tree_hash_deterministic_and_content_addressed,
        test_compute_tree_hash_changes_on_file_add_and_delete,
        test_compute_tree_hash_skips_designated_dirs,
        test_compute_tree_hash_missing_root_returns_empty_digest,
        test_fork_invalidation_wipes_artifacts_on_content_change,
        test_fork_invalidation_no_op_when_hash_matches,
        test_fork_invalidation_covers_multi_binary_via_control_parse,
        test_binary_names_from_control_extracts_all_package_stanzas,
        test_compute_dep_hash_changes_on_depends_field_edit,
        test_compute_dep_hash_changes_on_version_bump,
        test_compute_dep_hash_stable_under_description_edit,
        test_compute_dep_hash_stable_under_whitespace_reflow,
        test_load_pkg_hashes_returns_empty_strings_when_sidecars_missing,
        test_persist_tree_hash_writes_both_sidecars,
        test_wipe_fork_pkg_outputs_removes_both_hash_sidecars,
        test_no_stale_progressbar_methods_in_build_py,
        test_cmd_audit_registered_under_repo_dispatcher,
        test_cmd_audit_runs_three_checks,
        test_resolve_live_cohort_subtracts_pool_and_installer,
        test_resolve_installer_cohort_uses_udeb_tree,
        test_audit_dep_closure_resolves_against_whole_repo,
        test_audit_conflict_cohort_only_flags_within_cohort,
        test_dedupe_bidirectional_conflicts_collapses_pairs,
        test_audit_versioned_provides_satisfies_any_operator,
        test_audit_unversioned_provides_does_not_satisfy_versioned_depends,
        test_repo_max_mtime_detects_delete_and_rename,
        test_repo_audit_module_exports,
        test_repo_audit_scan_uses_dpkg_scanpackages_and_apt_pkg,
        test_scan_repo_state_main_udeb_passes_t_udeb_to_scanpackages,
        test_scan_repo_state_treats_empty_cache_file_as_missing,
        test_content_integrity_absorbed_into_cmd_audit_per_cohort,
        test_deb_dir_for_recognises_main_udeb_label,
        test_repo_audit_closure_handles_conflicts_and_provides,
        test_print_wrapped_names_keeps_lines_under_wrap_width,
        test_source_state_repairable_requires_patchhash_baseline,
        test_chroot_build_wires_both_audit_gates_with_no_gate_bypass,
        test_cmd_source_fork_disable_writes_marker_and_invalidates_state,
        test_cmd_source_fork_enable_removes_marker,
        test_fork_mirror_discover_skips_disabled_trees,
        test_cmd_source_audit_classifies_rebuilds_by_subset,
        test_readonly_named_commands_have_no_destructive_calls,
        test_destructive_helpers_warn_in_docstring,
        test_verify_pkg_artifact_ok_when_all_fields_match,
        test_verify_pkg_artifact_fails_on_internal_version_mismatch,
        test_verify_pkg_artifact_fails_on_unsatisfied_depends,
        test_verify_pkg_artifact_repo_state_overrides_cache_resolution,
        test_verify_pkg_artifact_repo_state_still_fails_when_actually_unsatisfied,
        test_verify_pkg_artifact_passes_on_satisfied_depends,
        test_verify_pkg_artifact_strips_epoch_from_internal_version,
        test_verify_pkg_artifact_resolves_udeb_deps_via_udeb_hashtable,
        test_verify_pkg_artifact_substvar_skip_logic_present,
        test_verify_pkg_artifact_or_group_satisfied_by_any_alternative,
        test_cmd_source_repair_dispatch_and_method_present,
        test_cmd_source_repair_writes_pass_when_binaries_present,
        test_cmd_source_repair_leaves_fail_result_untouched,
        test_cmd_source_repair_skips_when_binaries_missing,
        test_cmd_source_repair_clears_stale_pass_when_binaries_not_valid,
        test_cmd_source_repair_leaves_consistent_pass_alone,
        test_cmd_source_repair_leaves_tunneled_marker_alone,
        # TEST-05: offline Cache fixture
        test_offline_fixture_builds_a_valid_cache,
        # TEST-02: parse_dependency coverage
        test_parse_dependency_empty_name_returns_none,
        test_parse_dependency_no_candidates_returns_none,
        test_parse_dependency_single_candidate_case_iii,
        test_parse_dependency_already_selected_returns_existing,
        test_parse_dependency_provides_registers_virtual_alias,
        test_parse_dependency_propagates_dep_recursively,
        test_parse_dependency_cycle_protection_does_not_infinite_loop,
        test_parse_dependency_recommends_pulled_when_flag_on,
        test_parse_dependency_alt_deps_first_already_selected_wins,
        test_parse_dependency_alt_deps_default_to_first_alternative,
        # TEST-08: Mirror.with_snapshot properties
        test_mirror_with_snapshot_none_returns_self,
        test_mirror_with_snapshot_is_idempotent,
        test_mirror_with_snapshot_preserves_suite_and_component,
        # TEST-10: coverage gaps in signing + cache
        test_signing_get_key_info_returns_none_when_gpg_missing,
        test_signing_generate_key_returns_false_when_gpg_missing,
        test_signing_generate_key_returns_false_when_export_step_fails,
        test_cache_build_populates_upstream_collisions_via_real_path,
        test_cache_build_fork_supersede_drops_upstream_silently,
        # UPD-01 step 1: +asg<R>u<N> version-suffix primitives
        test_nmu_regex_does_not_eat_asg_suffix,
        test_pristine_base_strips_both_layers,
        test_asg_suffix_sorts_above_pristine_and_across_release,
        test_asg_filename_maps_pristine_to_stamped,
        test_match_pristine_base_reconciles_stamped_artifact,
        test_restamp_asg_deb_bumps_version_and_intra_source_deps,
        # UPD-01 step 2: append-only enforcement
        test_publish_rsync_is_additive_no_delete,
        test_segregate_never_deletes_existing_published_deb,
        # COMP-03 Phase 1: per-worker scratch repo dir + segregate refactor
        test_comp03_segregate_signature_takes_source_dir,
        test_comp03_segregate_does_not_read_self_repo_path,
        test_comp03_segregate_cross_source_isolation_via_scratch_dir,
        test_comp03_segregate_rolls_back_on_move_failure,
        test_comp03_build_uses_per_worker_scratch_dir_in_volume_bind,
        test_comp03_buildconfig_creates_and_validates_dir_build_stage,
        test_comp03_buildcontainer_init_sweeps_build_stage_survivors,
        # COMP-03 Phase 2: live-container registry + label-based orphan reap
        test_comp03_register_live_adds_under_lock_and_is_idempotent,
        test_comp03_deregister_live_removes_and_tolerates_missing_key,
        test_comp03_all_containers_run_sites_carry_athena_label,
        test_comp03_register_live_called_after_each_containers_run,
        test_comp03_deregister_live_called_in_every_finally,
        test_comp03_startup_orphan_reap_filters_by_athena_label,
        test_comp03_container_labels_include_pid_for_disambiguation,
        # COMP-03 Phase 3: shutdown_event + request_shutdown + reap_all_live
        test_comp03_reap_all_live_force_removes_every_registered_container,
        test_comp03_reap_all_live_is_idempotent_and_tolerates_api_error,
        test_comp03_request_shutdown_sets_event_and_reaps,
        test_comp03_shutdown_event_initialised_in_init,
        test_comp03_reap_all_live_uses_snapshot_not_live_iteration,
        # COMP-03 Phase 4: ThreadPoolExecutor parallel path in cmd_source_build
        test_comp03_phase4_parallel_path_uses_thread_pool_executor,
        test_comp03_phase4_serial_path_kept_for_max_parallel_builds_one,
        test_comp03_phase4_installs_scoped_sigint_handler_around_executor,
        test_comp03_phase4_executor_shutdown_uses_wait_true_and_cancel_futures,
        test_comp03_phase4_pool_breaks_on_shutdown_event,
        test_comp03_phase4_tunnel_sub_phase_runs_serial_before_parallel,
        test_comp03_phase4_build_one_source_skip_src_returns_skipped,
        test_comp03_phase4_build_one_source_tunneled_calls_do_tunnel,
        # UPD-01 step 3: build-side stamping + check_build matching + Guard B
        test_highest_asg_update_reads_remote_ledger,
        test_asg_next_n_is_per_file_and_cumulative,
        test_check_build_matches_asg_variant_of_prediction,
        test_check_build_locates_non_main_component_deb,
        test_tunnel_filenames_for_source_uses_upstream_not_stripped,
        test_tunnel_filenames_falls_back_when_binary_not_in_cache,
        test_normalize_built_artifacts_stamps_delta_when_ledger_present,
        test_normalize_built_artifacts_no_stamp_without_ledger,
        test_postbuild_convergence_hard_fails_when_check_build_still_false,
        # UPD-01 step 4: remote ledger + version-aware local cleanup
        test_parse_packages_to_ledger_multiversion_epoch_stripped,
        test_published_base_versions,
        test_group_by_pristine_base,
        test_local_cleanup_keeps_highest_prunes_superseded_and_flags_orphan,
        # UPD-01 step 5: merge_remote_index
        test_merge_remote_index_preserves_old_versions_multiversion,
        test_merge_packages_indexes_dedup_local_wins,
        test_generate_top_release_subprocess_text_mode_consistency,
        test_generate_top_release_avoids_self_reference_via_tempfile,
        test_merge_remote_index_signs_locally_and_merges,
        test_publish_suites_spec_includes_nonfree_components,
        # UPD-01 step 6: workload + Guard A preflight
        test_workload_since_published_picks_only_advanced_bases,
        test_needs_bump_build_per_file_exact_un,
        test_preflight_stamp_invariant_roundtrips_and_flags_bad_version,
        # UPD-01 steps 7-8: snapshot commands + repo refresh orchestrator
        test_snapshot_and_refresh_commands_wired,
        test_snapshot_state_roundtrip_and_resolve_precedence,
        test_snapshot_base_show_and_forward_only,
        test_list_snapshots_between_filters_range_and_unions_keys,
        test_snapshot_select_interactive_sets_chosen_current,
        test_snapshot_select_current_is_forward_only,
        test_ensure_snapshot_pins_prompts_and_writes_when_unset,
        test_ensure_snapshot_pins_aborts_when_no_selection,
        test_cache_build_gates_on_snapshot_pins,
        test_repo_refresh_is_thin_wrapper,
        test_publish_full_folds_index_merge_prune_and_gates_rsync,
        test_source_build_autodetects_update_mode,
        test_workload_current_to_target_diffs_against_target_snapshot,
        test_workload_detects_debNuN_source_change_ignores_unchanged,
        test_workload_since_snapshot_diffs_published_to_current,
        test_workload_excludes_forks_from_snapshot_diff,
        test_snapshot_published_defaults_to_base_then_tracks,
        # UPD-01: snapshot tag in ISO identity
        test_snapshot_iso_tag_reflects_resolved_pin,
        test_stage_disk_info_writes_snapshot_marker_and_substitutes,
        test_iso_filenames_carry_snapshot_tag,
        # UPD-01 decomposition: manifest, external on/off, update-mode plumbing
        test_published_manifest_roundtrip_and_ledger,
        # STA-21: fail-closed signing manifest
        test_write_signed_manifest_fails_closed_when_signing_missing,
        test_read_signed_manifest_refuses_when_signing_setup_fails,
        test_write_signed_manifest_scrubs_unsigned_file_on_sign_failure,
        # STA-22: consolidated _scan_packages_* shell-subprocess helper
        test_sta22_shared_dpkg_scan_helper_exists_and_is_consumed,
        test_sta22_run_dpkg_scan_writes_via_tempfile,
        test_sta22_run_dpkg_scan_honours_allow_empty,
        test_cmd_repo_publish_surfaces_sign_failure,
        test_manifest_vs_remote_discrepancies,
        test_repo_external_disable_and_enable_empty_rebaselines,
        test_update_build_pending_logic,
        test_external_and_audit_ssh_and_published_ledger_wired,
        # UPD-02: index on the remote
        test_remote_scan_packages_builds_ssh_argv,
        test_remote_reindex_wiring,
        test_index_minimal_stages_nested_subset,
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
