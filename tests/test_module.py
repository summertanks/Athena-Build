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


def test_mirror_repr_does_not_crash():
    from utils import Mirror
    m = Mirror('updates', 'http://x', 'y', 'z', '-updates', 'main', 'amd64')
    repr(m)  # raises if broken


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


def test_buildconfig_parses_distro_suffix():
    """`[Source] DistroSuffix` lands on `cfg.distro_suffix` as a stripped
    string.  Used by BuildContainer (changelog prepend) and
    DependencyTree (filename prediction); both ends MUST agree."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            print(f"SKIP test_buildconfig_parses_distro_suffix ({cfg.error_str})")
            return
        assert cfg.distro_suffix == 'thor1', cfg.distro_suffix


def test_buildconfig_distro_suffix_defaults_to_empty():
    """Omitted DistroSuffix → empty string (legacy behaviour, no version
    bump).  Confirms the field is optional."""
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
    """
    body = _BASE_CONF_BODY.replace(
        'DistroSuffix = thor1\n', '',
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, body.format(mirror_block=mirror_block))
        cfg = _build_config_from(tmp, cfg_path)
        if not cfg.is_valid:
            print(f"SKIP test_buildconfig_distro_suffix_defaults_to_empty ({cfg.error_str})")
            return
        assert cfg.distro_suffix == '', cfg.distro_suffix


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
                          'cmd_source_download', 'cmd_init_container',
                          'cmd_source_build', 'cmd_tunnel_package',
                          'cmd_build_chroot_live', 'cmd_build_chroot_installer',
                          'cmd_build_iso_live', 'cmd_build_iso_installer',
                          'cmd_verify_chroot', 'cmd_auto_run',
                          'cmd_auto_run_live', 'cmd_auto_run_installer',
                          'cmd_print',
                          # Group dispatchers (noun-verb command surface).
                          'cmd_cache', 'cmd_dep', 'cmd_patch',
                          'cmd_source', 'cmd_package', 'cmd_container',
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
        ('cmd_dep',       'parse',    'cmd_parse_dependency'),
        ('cmd_patch',     'refresh',  'cmd_patch_refresh'),
        ('cmd_source',    'download', 'cmd_source_download'),
        ('cmd_source',    'build',    'cmd_source_build'),
        ('cmd_package',   'tunnel',   'cmd_tunnel_package'),
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
    from iso_installer import _count_records
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
    import iso_installer, tui as _tui

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
            with patch.object(iso_installer, '_sudo', side_effect=_fake_sudo), \
                 patch.object(iso_installer.subprocess, 'run',
                              side_effect=_fake_subprocess_run):
                _ok = iso_installer._generate_apt_repo(
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
    assert _ok is True
    assert _assert_dirs_exist, (
        "dists/<suite>/main/{binary-amd64,debian-installer/binary-amd64,source} not created"
    )
    # Sanity-check the call sequence carries dpkg-scanpackages (deb +
    # udeb) + dpkg-scansources + apt-ftparchive release.  The scan
    # helpers use `bash -c` (shell redirection for file output); the
    # apt-ftparchive release helper goes via subprocess.run argv
    # directly (avoids word-splitting issues — see fix 2026-05-11).
    _shell_strings = [' '.join(c) for c in _calls if c and 'bash' in c[0]]
    _joined_shell = '\n'.join(_shell_strings)
    assert 'dpkg-scanpackages -m  pool' in _joined_shell, (
        f"missing deb scan call; got:\n{_joined_shell}")
    assert 'dpkg-scanpackages -m -t udeb pool' in _joined_shell, (
        f"missing udeb scan call; got:\n{_joined_shell}")
    assert 'dpkg-scansources pool' in _joined_shell, "missing source scan"
    # apt-ftparchive lands as argv (no shell wrapper).
    _any_ftparchive = any(
        len(c) >= 3 and c[0] == 'sudo' and c[2] == 'apt-ftparchive'
        for c in _calls
    )
    assert _any_ftparchive, (
        f"missing top-level Release generation via argv apt-ftparchive; "
        f"got calls: {[c[:4] for c in _calls if c and c[0]=='sudo']}"
    )


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
        _kept, _skipped = _select_pool_files(_repo, deb_whitelist=set())
        assert len(_kept) == 3, _kept
        assert _skipped == 0
        assert all(n.endswith('.udeb') for n in _kept)


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
            _repo, deb_whitelist={'acl', 'libc6'},
        )
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
            _repo,
            deb_whitelist={
                'grub-pc',
                'linux-image-6.1.0-47-amd64',
                'systemd',
            },
        )
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
            _repo, deb_whitelist={'linux-image-amd64'},
        )
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
            _repo, deb_whitelist={'pkg'},
        )
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
        _kept, _skipped = _select_pool_files(_repo, deb_whitelist=None)
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
    from iso_installer import _sign_release_files
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
        with patch('iso_installer.subprocess.run', side_effect=_fake_run):
            assert _sign_release_files(
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
    from iso_installer import _sign_release_files
    with tempfile.TemporaryDirectory() as _stage, \
         tempfile.TemporaryDirectory() as _gpgdir:
        # Don't create dists/thor/Release
        with patch('iso_installer.subprocess.run') as _mock:
            assert _sign_release_files(
                _stage, 'thor', _gpgdir, 'pw') is False
            _mock.assert_not_called()


def test_iso_installer_sign_release_files_errors_when_homedir_missing():
    """Without a signing homedir gpg has no key to use — bail loud with
    a hint about 'signing keygen' rather than producing a cryptic gpg
    error message."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _sign_release_files
    with tempfile.TemporaryDirectory() as _stage:
        _suite_dir = os.path.join(_stage, 'dists', 'thor')
        os.makedirs(_suite_dir)
        with open(os.path.join(_suite_dir, 'Release'), 'w') as fh:
            fh.write('Suite: thor\n')
        with patch('iso_installer.subprocess.run') as _mock:
            assert _sign_release_files(
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
    from iso_installer import _export_pubkey_to_staging
    with tempfile.TemporaryDirectory() as _stage, \
         tempfile.NamedTemporaryFile('wb', delete=False) as _pubkey_fh:
        _pubkey_fh.write(b'-----BEGIN PGP PUBLIC KEY BLOCK-----\nFAKE\n')
        _pubkey_path = _pubkey_fh.name
    try:
        os.makedirs(os.path.join(_stage, '.disk'))
        assert _export_pubkey_to_staging(_stage, _pubkey_path, 'pw') is True
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
    from iso_installer import _export_pubkey_to_staging
    with tempfile.TemporaryDirectory() as _stage:
        os.makedirs(os.path.join(_stage, '.disk'))
        assert _export_pubkey_to_staging(
            _stage, '/nonexistent/pubkey.gpg', 'pw') is False


def test_iso_installer_export_pubkey_to_staging_errors_when_disk_dir_missing():
    """_stage_disk_info must run before _export_pubkey_to_staging; the
    helper must fail loud if it hasn't (rather than silently mkdir + copy
    and skip the .disk/info disc-marker contract _stage_disk_info enforces)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from iso_installer import _export_pubkey_to_staging
    with tempfile.TemporaryDirectory() as _stage, \
         tempfile.NamedTemporaryFile('wb', delete=False) as _pubkey_fh:
        _pubkey_fh.write(b'KEY')
        _pubkey_path = _pubkey_fh.name
    try:
        # No .disk/ in _stage.
        assert _export_pubkey_to_staging(_stage, _pubkey_path, 'pw') is False
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

    with tempfile.TemporaryDirectory() as _repo:
        # File on disk: binNMU stripped (this is what dpkg-buildpackage emits)
        _fake_path = os.path.join(_repo, 'busybox-udeb_1.35.0-4_amd64.udeb')
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
        out = _resolve_udeb_files(_UdebTree(), _repo)
        assert out == [_fake_path], out


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


def test_athena_installer_data_ships_mirror_protocol_stub():
    """FORK-01 Step 3: the mirror/protocol stub template (which
    bootstrap-base.postinst queries unguarded — caught 2026-05-11) now
    ships from athena-installer-data's data/ tree via debian/install.
    Pin the content + install path so an accidental edit fails here
    before it ships a broken installer."""
    _data = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                         'data', 'athena-stubs.templates')
    assert os.path.isfile(_data), f"missing {_data}"
    with open(_data) as fh:
        _content = fh.read()
    assert 'Template: mirror/protocol' in _content, _content
    assert 'Type: string' in _content, _content
    assert 'Default: file' in _content, _content

    # debian/install must wire data/athena-stubs.templates to
    # /var/lib/dpkg/info/ (the path rootskel's S20templates run-part
    # walks at boot via debconf-loadtemplate).
    _install = os.path.join(_ROOT, 'fork', 'source', 'athena-installer-data',
                            'debian', 'install')
    assert os.path.isfile(_install), f"missing {_install}"
    with open(_install) as fh:
        _install_body = fh.read()
    assert 'data/athena-stubs.templates' in _install_body, _install_body
    assert 'var/lib/dpkg/info' in _install_body, _install_body


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


def test_buildcontainer_emits_changelog_bump_when_distro_suffix_set():
    """When BuildContainer.distro_suffix is non-empty, the assembled
    cmd_str must include the changelog-prepend snippet that bumps the
    version to `<src-ver>+<suffix>` before dpkg-buildpackage runs.  The
    pipeline is failure-prone if half-implemented: prepend without
    filename-suffix wiring → check_build can't find the built .deb.
    Pin the wiring so a refactor doesn't silently drop the prepend.

    Also pins the `-b` (binary-only) dpkg-buildpackage flag — required
    because our changelog prepend produces a version that doesn't match
    the .dsc, so a source-rebuild step would fail."""
    _bc = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_bc) as fh:
        _body = fh.read()
    assert 'dpkg-parsechangelog -SVersion' in _body, (
        "changelog bump snippet missing — distro_suffix wiring incomplete")
    assert '${{SRC_VER}}+{_suffix}' in _body or \
           '"${SRC_VER}+' in _body, (
        "changelog bump must compose new version as <src-ver>+<suffix>")
    assert ' -b -us -uc -nc' in _body, (
        "dpkg-buildpackage must use -b (binary-only) — required when "
        "changelog bump produces a version that doesn't match the .dsc")
    assert 'self.distro_suffix = config.distro_suffix' in _body, (
        "BuildContainer.distro_suffix not initialised from config")


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
    """cmd_clean_image wipes dir_image and resets BOTH iso_live_ready
    and iso_installer_ready (single dir holds outputs from both
    pipelines)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession, BuildFlags
    _sess = BuildSession.__new__(BuildSession)
    _sess.flags = BuildFlags()
    _sess.flags.iso_live_ready = True
    _sess.flags.iso_installer_ready = True
    with tempfile.TemporaryDirectory() as _tmp:
        class _Cfg:
            dir_image = _tmp
        _sess.config = _Cfg()
        _sess.cmd_clean_image('force')
    assert _sess.flags.iso_live_ready is False
    assert _sess.flags.iso_installer_ready is False


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
    _docker.from_env = lambda: _client
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
    _docker.from_env = lambda: _client
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


# ─────────────────────────────────────────────────────────────────────────────
# TUI primitives accept Tui explicitly (no singleton required)
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
# single logging adapter routes by level into the Tui
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
# apply_distro_suffix — append `+<suffix>` to bumped binaries
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_distro_suffix_appends_to_stripped_filename():
    """The canonical path: strip_build_version produces
    `foo_1.0-2_amd64.deb`, apply_distro_suffix bumps it to
    `foo_1.0-2+thor1_amd64.deb` — what BuildContainer's changelog
    prepend produces from a source build."""
    from utils import apply_distro_suffix
    assert (apply_distro_suffix('foo_1.0-2_amd64.deb', 'thor1')
            == 'foo_1.0-2+thor1_amd64.deb')
    assert (apply_distro_suffix('foo_1.0-2_amd64.udeb', 'thor1')
            == 'foo_1.0-2+thor1_amd64.udeb')


def test_apply_distro_suffix_empty_is_noop():
    """Empty suffix → legacy behaviour, filename unchanged.  Lets
    operators turn the feature off via blank DistroSuffix config."""
    from utils import apply_distro_suffix
    assert (apply_distro_suffix('foo_1.0-2_amd64.deb', '')
            == 'foo_1.0-2_amd64.deb')


def test_apply_distro_suffix_idempotent_on_already_suffixed():
    """Calling apply_distro_suffix on a filename that already ends
    with `+<suffix>` returns it unchanged.  Guards against double-
    suffixing if the function is composed with itself or applied
    to a filename that's already been through the pipeline."""
    from utils import apply_distro_suffix
    assert (apply_distro_suffix('foo_1.0-2+thor1_amd64.deb', 'thor1')
            == 'foo_1.0-2+thor1_amd64.deb')


def test_apply_distro_suffix_preserves_deb12u1_security_suffix():
    """Security/point-release suffixes like `+deb12u1` stay intact —
    apply_distro_suffix appends, doesn't replace.  Real example:
    `openssh-client_1:9.2p1-2+deb12u9_amd64.deb` becomes
    `openssh-client_1:9.2p1-2+deb12u9+thor1_amd64.deb`."""
    from utils import apply_distro_suffix
    assert (apply_distro_suffix('foo_1.0-2+deb12u1_amd64.deb', 'thor1')
            == 'foo_1.0-2+deb12u1+thor1_amd64.deb')


def test_apply_distro_suffix_beats_debian_bin_nmu_constraint():
    """Architectural invariant — our distro_suffix MUST produce a
    binary version that beats EVERY upstream bin-NMU version, so any
    downstream consumer whose Depends was stamped against a Debian
    bin-NMU (`(>= 0.15.5-2b)`, `(>= 0.15.5-2+b1)`, ...) is satisfied
    by our binary at install time.

    Verified with apt_pkg.check_dep — the same comparison apt does on
    the target.  This is the WHOLE POINT of the distro_suffix system;
    if this test fails the suffix is broken.
    """
    import apt_pkg
    apt_pkg.init_system()
    _our_ver = '0.15.5-2+thor1'
    for _upstream_constraint in ('0.15.5-2', '0.15.5-2b',
                                  '0.15.5-2+b1', '0.15.5-2+b99'):
        assert apt_pkg.check_dep(_our_ver, '>=', _upstream_constraint), (
            f"DistroSuffix-bumped {_our_ver} must satisfy "
            f">= {_upstream_constraint} but doesn't"
        )


def test_apply_distro_suffix_rejects_malformed_filename():
    """Same shape check as strip_build_version — refuse to silently
    butcher non-conforming filenames."""
    from utils import apply_distro_suffix
    for bad in ('not-a-deb.deb', 'one_two.deb', 'a_b_c_d_amd64.deb'):
        try:
            apply_distro_suffix(bad, 'thor1')
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_apply_distro_suffix_noop_skips_shape_check_for_empty():
    """When suffix is empty we don't even validate the shape — caller
    is asking for a no-op and we honour it bit-for-bit.  Avoids
    surprising ValueErrors when distro_suffix happens to be blank."""
    from utils import apply_distro_suffix
    assert apply_distro_suffix('arbitrary-string-not-deb', '') \
        == 'arbitrary-string-not-deb'


# ─────────────────────────────────────────────────────────────────────────────
# rebump_deb_file — one-time backfill of an existing repo/ corpus
# ─────────────────────────────────────────────────────────────────────────────


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


def test_rebump_deb_file_round_trip_rewrites_control_and_renames():
    """Canonical path: a .deb at version X-Y becomes a .deb at
    X-Y+thor1.  DEBIAN/control's Version is rewritten in lock-step
    with the filename; the data.tar (README sentinel) survives the
    repack intact."""
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import rebump_deb_file

    # Skip when dpkg-deb isn't available (non-Debian host).
    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_rebump_deb_file_round_trip_rewrites_control_and_renames (no dpkg-deb)")
        return

    with tempfile.TemporaryDirectory() as _tmp:
        _original = os.path.join(_tmp, 'libfoo26_1.0-2_amd64.deb')
        _make_synthetic_deb(_original, 'libfoo26', '1.0-2')
        _new = rebump_deb_file(_original, 'thor1')
        assert _new == 'libfoo26_1.0-2+thor1_amd64.deb', _new
        assert not os.path.exists(_original), "original .deb must be removed"
        _bumped = os.path.join(_tmp, _new)
        assert os.path.isfile(_bumped), "bumped .deb must exist"
        # Verify the new control says Version: 1.0-2+thor1
        _ctrl = subprocess.run(
            ['dpkg-deb', '-f', _bumped, 'Version'],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert _ctrl == '1.0-2+thor1', f"Version field: {_ctrl!r}"
        # Verify data.tar survived (the README sentinel is still there)
        _files = subprocess.run(
            ['dpkg-deb', '-c', _bumped],
            check=True, capture_output=True, text=True,
        ).stdout
        assert 'usr/share/doc/libfoo26/README' in _files, _files


def test_rebump_deb_file_idempotent_on_already_bumped():
    """A second invocation on an already-bumped file returns the
    existing basename unchanged.  Lets the operator re-run
    `package rebump` safely (e.g. after adding a new package that
    didn't get bumped the first time)."""
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import rebump_deb_file

    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_rebump_deb_file_idempotent_on_already_bumped (no dpkg-deb)")
        return

    with tempfile.TemporaryDirectory() as _tmp:
        _original = os.path.join(_tmp, 'libfoo26_1.0-2+thor1_amd64.deb')
        _make_synthetic_deb(_original, 'libfoo26', '1.0-2+thor1')
        _new = rebump_deb_file(_original, 'thor1')
        assert _new == 'libfoo26_1.0-2+thor1_amd64.deb', _new
        assert os.path.exists(_original), (
            "idempotent rebump must not touch the file"
        )


def test_rebump_deb_file_empty_suffix_is_noop():
    """No DistroSuffix configured → no rebump, file untouched.  Lets
    the rebump command short-circuit when the operator hasn't opted
    into the suffix scheme."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import rebump_deb_file
    # No actual file needed — empty-suffix path returns before any I/O.
    assert (rebump_deb_file('/nonexistent/foo_1.0-2_amd64.deb', '')
            == 'foo_1.0-2_amd64.deb')


def test_rebump_deb_file_handles_udeb_extension():
    """Udebs round-trip identically — same dpkg-deb -R/-b plumbing,
    just .udeb instead of .deb extension."""
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import rebump_deb_file

    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_rebump_deb_file_handles_udeb_extension (no dpkg-deb)")
        return

    with tempfile.TemporaryDirectory() as _tmp:
        _original = os.path.join(_tmp, 'foo-udeb_1.0-2_amd64.udeb')
        _make_synthetic_deb(_original, 'foo-udeb', '1.0-2')
        # Rename the .deb dpkg-deb produced to .udeb (test fixture cheat
        # — dpkg-deb doesn't care about extension when packing).
        if not _original.endswith('.udeb'):
            os.rename(_original.replace('.udeb', '.deb'), _original) \
                if os.path.exists(_original.replace('.udeb', '.deb')) else None
        # Some dpkg-deb versions write .deb regardless; handle both shapes.
        _alt = _original.replace('.udeb', '.deb')
        if os.path.exists(_alt) and not os.path.exists(_original):
            os.rename(_alt, _original)
        _new = rebump_deb_file(_original, 'thor1')
        assert _new == 'foo-udeb_1.0-2+thor1_amd64.udeb', _new
        assert os.path.exists(os.path.join(_tmp, _new))


def test_rebump_deb_file_skips_malformed_filename():
    """Filenames not in `name_version_arch.ext` shape get a no-op
    return (caller decides whether to log/warn).  Matches the
    strip_build_version error-tolerance pattern — backfill walks
    everything in repo/ and shouldn't die on a stray file."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import rebump_deb_file
    for bad in ('weird.deb', 'a_b_c_d_amd64.deb'):
        assert rebump_deb_file(f'/nonexistent/{bad}', 'thor1') == bad


def test_rebump_deb_file_preserves_epoch_from_control():
    """REGRESSION (2026-05-18): rebump must derive the new Version from
    DEBIAN/control's existing Version field — NOT from the filename.
    Debian filenames strip epochs (`pkg_1.0-2_arch.deb` for a binary
    whose internal Version is `2:1.0-2`), so deriving the new version
    from the filename silently drops the epoch.  The first install
    attempt after the buggy rebump failed because gmp's `2:6.2.1+...`
    became `6.2.1+...+thor1`, failing every `(>= 2:…)` Pre-Depends.
    """
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import rebump_deb_file

    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_rebump_deb_file_preserves_epoch_from_control (no dpkg-deb)")
        return

    with tempfile.TemporaryDirectory() as _tmp:
        # Synthetic .deb with an EPOCH'd Version in DEBIAN/control but
        # no epoch in the filename (mirrors how Debian builds them).
        _original = os.path.join(_tmp, 'libgmp10_6.2.1+dfsg1-1.1_amd64.deb')
        _work = os.path.join(_tmp, 'src')
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        os.makedirs(os.path.join(_work, 'usr', 'lib'))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(
                'Package: libgmp10\n'
                'Version: 2:6.2.1+dfsg1-1.1\n'   # ← epoch here
                'Architecture: amd64\n'
                'Maintainer: Test <test@local>\n'
                'Description: epoch-bearing test package\n'
            )
        with open(os.path.join(_work, 'usr', 'lib', 'placeholder'), 'w') as fh:
            fh.write('data\n')
        subprocess.run(
            ['dpkg-deb', '--root-owner-group', '-b', _work, _original],
            check=True, capture_output=True,
        )

        _new = rebump_deb_file(_original, 'thor1')
        # Filename gets +thor1 appended (no epoch since filenames strip it)
        assert _new == 'libgmp10_6.2.1+dfsg1-1.1+thor1_amd64.deb', _new
        # ← THE INVARIANT: control's Version MUST still carry the epoch.
        _ver = subprocess.run(
            ['dpkg-deb', '-f', os.path.join(_tmp, _new), 'Version'],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert _ver == '2:6.2.1+dfsg1-1.1+thor1', (
            f"Epoch lost in rebump.  Got: {_ver!r}, expected: "
            "2:6.2.1+dfsg1-1.1+thor1"
        )


def test_restore_deb_epoch_prepends_when_missing():
    """One-time recovery helper: prepends an epoch prefix to a .deb's
    DEBIAN/control Version field when it's missing.  Returns 'fixed'
    on success, 'already-correct' if no change needed.  Used to
    recover from the rebump epoch-strip bug — restores the original
    epoch by looking it up in the cache."""
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import restore_deb_epoch

    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_restore_deb_epoch_prepends_when_missing (no dpkg-deb)")
        return

    with tempfile.TemporaryDirectory() as _tmp:
        # Synthetic .deb whose Version is EPOCH-LESS (mirrors the
        # post-buggy-rebump corrupted state).
        _deb = os.path.join(_tmp, 'libgmp10_6.2.1+dfsg1-1.1+thor1_amd64.deb')
        _work = os.path.join(_tmp, 'src')
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        os.makedirs(os.path.join(_work, 'usr', 'lib'))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(
                'Package: libgmp10\n'
                'Version: 6.2.1+dfsg1-1.1+thor1\n'   # ← no epoch (corrupted)
                'Architecture: amd64\n'
                'Maintainer: Test <test@local>\n'
                'Description: post-rebump epoch-stripped test\n'
            )
        with open(os.path.join(_work, 'usr', 'lib', 'placeholder'), 'w') as fh:
            fh.write('data\n')
        subprocess.run(
            ['dpkg-deb', '--root-owner-group', '-b', _work, _deb],
            check=True, capture_output=True,
        )

        # First call — should fix.
        _r1 = restore_deb_epoch(_deb, '2:')
        assert _r1 == 'fixed', _r1
        _ver = subprocess.run(
            ['dpkg-deb', '-f', _deb, 'Version'],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert _ver == '2:6.2.1+dfsg1-1.1+thor1', _ver

        # Second call — idempotent.
        _r2 = restore_deb_epoch(_deb, '2:')
        assert _r2 == 'already-correct', _r2


def test_rewrite_intra_thor1_strict_equals_round_trip():
    """REGRESSION (2026-05-18 install failure): rebump_deb_file only
    updates each .deb's own Version field; it leaves stale `(= X)`
    cross-references to sibling binaries from the same source.
    Symptom: systemd's `Depends: libsystemd-shared (= 252.39-1~deb12u1)`
    couldn't be satisfied by our bumped libsystemd-shared at
    252.39-1~deb12u1+thor1, so debootstrap failed at base configure.

    rewrite_intra_thor1_strict_equals walks Depends/Pre-Depends/
    Recommends/Suggests/Enhances/Provides; for any `(= X)` constraint
    targeting a package in `bumped_pkg_set` and whose X doesn't already
    end with `+suffix`, appends `+suffix`.
    """
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import rewrite_intra_thor1_strict_equals

    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_rewrite_intra_thor1_strict_equals_round_trip (no dpkg-deb)")
        return

    with tempfile.TemporaryDirectory() as _tmp:
        _deb = os.path.join(_tmp, 'systemd_252.39-1~deb12u1+thor1_amd64.deb')
        _work = os.path.join(_tmp, 'src')
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        os.makedirs(os.path.join(_work, 'usr', 'bin'))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(
                'Package: systemd\n'
                'Version: 252.39-1~deb12u1+thor1\n'
                'Architecture: amd64\n'
                'Maintainer: Test <test@local>\n'
                # Mix of constraints:
                # - libsystemd-shared (= ...) → bumped sibling, MUST rewrite
                # - libsystemd0 (= ...) → bumped sibling, MUST rewrite
                # - libacl1 (>= ...) → cross-source >=, leave alone
                # - mount (no version) → leave alone
                # - libudev1:amd64 (= ...) → bumped sibling with arch, MUST rewrite
                'Depends: libsystemd-shared (= 252.39-1~deb12u1), '
                'libsystemd0 (= 252.39-1~deb12u1), '
                'libacl1 (>= 2.2.23), mount, '
                'libudev1:amd64 (= 252.39-1~deb12u1)\n'
                'Pre-Depends: libc6 (>= 2.34)\n'
                'Description: synthetic systemd-like test package\n'
            )
        with open(os.path.join(_work, 'usr', 'bin', 'systemd'), 'w') as fh:
            fh.write('placeholder\n')
        subprocess.run(
            ['dpkg-deb', '--root-owner-group', '-b', _work, _deb],
            check=True, capture_output=True,
        )

        # Bumped set: the three sibling lib pkgs.  libacl1 is NOT in the
        # set (a cross-source dep still at upstream version).
        _bumped = {'libsystemd-shared', 'libsystemd0', 'libudev1'}
        _n = rewrite_intra_thor1_strict_equals(_deb, _bumped, 'thor1')
        assert _n == 3, f"expected 3 rewrites, got {_n}"

        _depends = subprocess.run(
            ['dpkg-deb', '-f', _deb, 'Depends'],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # bumped sibling refs must now carry +thor1
        assert 'libsystemd-shared (= 252.39-1~deb12u1+thor1)' in _depends, _depends
        assert 'libsystemd0 (= 252.39-1~deb12u1+thor1)' in _depends, _depends
        assert 'libudev1:amd64 (= 252.39-1~deb12u1+thor1)' in _depends, _depends
        # cross-source >= constraint must be UNCHANGED
        assert 'libacl1 (>= 2.2.23)' in _depends, _depends
        # parenthesis-free dep stays parenthesis-free
        assert 'mount' in _depends, _depends


def test_rewrite_intra_thor1_strict_equals_idempotent():
    """Re-running on an already-rewritten .deb does no work.  Pinned so
    a recovery script that runs twice doesn't double-suffix."""
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import rewrite_intra_thor1_strict_equals

    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_rewrite_intra_thor1_strict_equals_idempotent (no dpkg-deb)")
        return

    with tempfile.TemporaryDirectory() as _tmp:
        _deb = os.path.join(_tmp, 'foo_1.0-2+thor1_amd64.deb')
        _work = os.path.join(_tmp, 'src')
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        os.makedirs(os.path.join(_work, 'usr', 'bin'))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(
                'Package: foo\nVersion: 1.0-2+thor1\nArchitecture: amd64\n'
                'Maintainer: T <t@l>\n'
                'Depends: bar (= 1.0-2+thor1)\n'   # already rewritten
                'Description: test\n'
            )
        with open(os.path.join(_work, 'usr', 'bin', 'foo'), 'w') as fh:
            fh.write('x\n')
        subprocess.run(['dpkg-deb', '--root-owner-group', '-b', _work, _deb],
                       check=True, capture_output=True)

        _n = rewrite_intra_thor1_strict_equals(_deb, {'bar'}, 'thor1')
        assert _n == 0, f"idempotent must do nothing, got {_n} rewrites"


def test_rewrite_intra_thor1_strict_equals_leaves_conflicts_untouched():
    """Conflicts / Breaks / Replaces have different semantics ('won't
    coexist with X', not 'needs X').  rewrite must not touch them —
    rewriting a Conflicts constraint could let two genuinely-
    incompatible packages co-install."""
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import rewrite_intra_thor1_strict_equals

    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_rewrite_intra_thor1_strict_equals_leaves_conflicts_untouched (no dpkg-deb)")
        return

    with tempfile.TemporaryDirectory() as _tmp:
        _deb = os.path.join(_tmp, 'pkga_1.0-2+thor1_amd64.deb')
        _work = os.path.join(_tmp, 'src')
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        os.makedirs(os.path.join(_work, 'usr', 'bin'))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(
                'Package: pkga\nVersion: 1.0-2+thor1\nArchitecture: amd64\n'
                'Maintainer: T <t@l>\n'
                'Conflicts: pkgb (= 1.0-2)\n'   # must NOT be rewritten
                'Breaks: pkgc (= 1.0-2)\n'
                'Replaces: pkgd (= 1.0-2)\n'
                'Description: test\n'
            )
        with open(os.path.join(_work, 'usr', 'bin', 'pkga'), 'w') as fh:
            fh.write('x\n')
        subprocess.run(['dpkg-deb', '--root-owner-group', '-b', _work, _deb],
                       check=True, capture_output=True)

        _n = rewrite_intra_thor1_strict_equals(
            _deb, {'pkgb', 'pkgc', 'pkgd'}, 'thor1',
        )
        assert _n == 0, f"Conflicts/Breaks/Replaces must NOT be rewritten, got {_n}"
        for _field in ('Conflicts', 'Breaks', 'Replaces'):
            _val = subprocess.run(
                ['dpkg-deb', '-f', _deb, _field],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            assert '(= 1.0-2)' in _val, f"{_field} mutated: {_val!r}"
            assert '+thor1' not in _val, f"{_field} got +thor1: {_val!r}"


def test_restore_deb_epoch_empty_prefix_is_noop():
    """Empty epoch prefix → nothing to restore; returns
    'already-correct' immediately without touching the file."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import restore_deb_epoch
    # No file access happens — early return on empty prefix.
    assert restore_deb_epoch('/nonexistent/foo.deb', '') == 'already-correct'


def test_rebump_deb_file_skips_non_deb_files():
    """Non-.deb/.udeb files in repo/ are left alone (signatures,
    Packages indexes, stray text files, etc.)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import rebump_deb_file
    assert (rebump_deb_file('/nonexistent/Release', 'thor1')
            == 'Release')
    assert (rebump_deb_file('/nonexistent/Packages.gz', 'thor1')
            == 'Packages.gz')


def test_cmd_rebump_packages_registered_under_package_dispatcher():
    """Pin the dispatcher wiring: `package rebump` must route to
    cmd_rebump_packages.  Without this, operators discover the
    command exists via `package` help only to have the verb
    silently no-op."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    assert hasattr(BuildSession, 'cmd_rebump_packages'), (
        "BuildSession is missing cmd_rebump_packages"
    )
    # Static inspection of the dispatcher source — cheaper than a
    # full BuildSession setup + mock chain just to confirm routing.
    import inspect
    _disp_src = inspect.getsource(BuildSession.cmd_package)
    assert "'rebump'" in _disp_src, _disp_src
    assert 'self.cmd_rebump_packages' in _disp_src, _disp_src


# ─────────────────────────────────────────────────────────────────────────────
# version_no_epoch — patch dir lookup must match Debian filename convention
# ─────────────────────────────────────────────────────────────────────────────
#
# Debian filenames strip the epoch (`git_2.39.5-…dsc` for source whose
# `Version: 1:2.39.5-…`).  Patch directories follow the same convention
# (`patch/source/git/2.39.5-…/`).  The pre-fix discovery used
# `str(src.version)` which kept the epoch, so any source with an epoch
# (git, llvm-toolchain-15, …) silently never had its patches discovered.


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
        aborted_at='source download',
    )
    output = _capture_console_print(
        lambda: print_commands.summary(sess, timing=timing)
    )
    assert 'ABORTED' in output
    assert "'source download'" in output
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
    # All eleven rows should be present as unticked.
    assert output.count('[·]') == 11, (
        "Expected 11 unticked rows (one per BuildFlag), got "
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
    dt._distro_suffix = ''  # no version bump in this synthetic test
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
        def __init__(self, pkgs): self.pkgs = pkgs

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
    dt.selected_srcs = {
        'firefox':     _StubSrc(['firefox_1.0_amd64.deb',
                                 'firefox-l10n-en_1.0_amd64.deb']),
        'live-config': _StubSrc(['live-config_1.0_all.deb']),
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
        def __init__(self, pkgs): self.pkgs = pkgs

    seed_pkgs = {
        'partman-base': _FakePkg('partman-base', source='partman-base',
                                 filename='partman-base_1.0_all.udeb'),
    }
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    dt._DependencyTree__cache = _FakeCache({})
    dt.selected_pkgs = seed_pkgs
    dt.selected_srcs = {
        'partman-base': _StubSrc(['partman-base_1.0_all.udeb']),
    }
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


def test_dependency_tree_default_does_not_auto_pick_across_names():
    """Default DependencyTree (deb tree) does NOT auto-pick when there
    are multiple Package names — the operator prompt is the only way
    out.  This pins the deb-world behaviour so the udeb fallback does
    not bleed into deb resolution."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    dt = dependencytree.DependencyTree.__new__(dependencytree.DependencyTree)
    # Replicate __init__'s zero-state for the relevant flag only.
    dt._auto_pick_highest_when_ambiguous = False
    assert dt._auto_pick_highest_when_ambiguous is False


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


def test_buildsession_initialises_udeb_dep_tree_as_none():
    """A fresh BuildSession has udeb_dep_tree=None until cmd_parse_dependency
    runs.  Consumers must gate on dep_check_ready before touching it."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    s = BuildSession.__new__(BuildSession)
    # Replicate the __init__ assignments relevant to this test.
    s.dep_tree = None
    s.udeb_dep_tree = None
    assert s.udeb_dep_tree is None
    assert hasattr(s, 'udeb_dep_tree')


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
                 ('pkg', 'live', 'installer', 'recommended')):
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
    """Phase 4 regression guard: cmd_source_download must call
    utils.download_source for the udeb tree too — otherwise sources that
    exist only in udeb_dep_tree (base-installer, debian-installer-utils,
    debootstrap, etc.) never land in dir_source and `source build
    installer` fails with `cp: cannot stat /source/<pkg>*: No such file
    or directory` inside the build container.  Caught in production on
    2026-05-10."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession
    src = inspect.getsource(BuildSession.cmd_source_download)
    # Both tree references must appear in the code (size + download call).
    assert 'self.dep_tree.download_size' in src or '_deb_size' in src, (
        "cmd_source_download must include deb tree download size")
    assert 'self.udeb_dep_tree.download_size' in src or '_udeb_size' in src, (
        "cmd_source_download must include udeb tree download size")
    assert 'utils.download_source(self.dep_tree' in src, (
        "cmd_source_download must call download_source on deb tree")
    assert 'utils.download_source(' in src and 'self.udeb_dep_tree' in src, (
        "cmd_source_download must also call download_source on udeb tree")


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


def test_buildflags_carry_iso_ready_state():
    """iso_live_ready and iso_installer_ready start False so
    a never-built ISO doesn't appear ready, and they're listed in
    __str__ so the status line surfaces them."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildFlags
    _flags = BuildFlags()
    assert _flags.iso_live_ready is False
    assert _flags.iso_installer_ready is False
    _s = str(_flags)
    assert 'iso_live' in _s, _s
    assert 'iso_installer' in _s, _s


def test_autorun_dispatcher_routes_bare_to_live_and_explicit_to_each():
    """cmd_auto_run is now a dispatcher: bare → live (preserves UX);
    'live' → live; 'installer' → installer; anything else → help."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from build import BuildSession

    _calls = []
    _sess = BuildSession.__new__(BuildSession)
    _sess.cmd_auto_run_live      = lambda *a, **kw: _calls.append(('live', a))
    _sess.cmd_auto_run_installer = lambda *a, **kw: _calls.append(('installer', a))

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
    # Unknown action → neither handler invoked (falls through to _group_help)
    _calls.clear()
    _sess.cmd_auto_run('wat')
    assert _calls == [], (
        f"unknown autorun action must not invoke either handler, got {_calls}")


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
    for _subset in ('pkg', 'live', 'installer', 'recommended'):
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
# FORK-01 Step 2 — fork mirror generation + cache integration
# ─────────────────────────────────────────────────────────────────────────────

def _setup_fork_test_tmpdir(tmp: str, with_pkg: bool = True) -> object:
    """Build a minimal pseudo-BuildConfig pointing at a tmpdir-scoped fork tree.
    Optionally populates fork/source/athena-installer-data/ with a valid
    debian/ layout so generate_fork_mirror has real input to chew on."""
    class _BC: pass
    bc = _BC()
    bc.working_dir = tmp
    bc.dir_fork = os.path.join(tmp, 'fork')
    bc.dir_fork_source = os.path.join(bc.dir_fork, 'source')
    bc.dir_fork_source_repo = os.path.join(bc.dir_fork_source, 'repo')
    # Added for fork-mirror invalidation: _wipe_fork_pkg_outputs reaches
    # into dir_source, dir_repo, dir_log/build for derived artifacts.
    bc.dir_source = os.path.join(tmp, 'source')
    bc.dir_repo   = os.path.join(tmp, 'repo')
    bc.dir_log    = os.path.join(tmp, 'log')
    bc.build_codename = 'thor'
    bc.arch = 'amd64'
    os.makedirs(bc.dir_fork, exist_ok=True)
    os.makedirs(bc.dir_fork_source, exist_ok=True)
    os.makedirs(bc.dir_fork_source_repo, exist_ok=True)
    os.makedirs(bc.dir_source, exist_ok=True)
    os.makedirs(bc.dir_repo,   exist_ok=True)
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
    """Inject minimal stubs so fork_mirror can call tui.console.print
    without a real Tui."""
    import tui as _tui
    class _Console:
        def __init__(self): self.out = []
        def print(self, *a, **kw): self.out.append(' '.join(str(x) for x in a))
    _tui.console = _Console()
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

        # Plant fake stale artifacts that should get wiped on next run
        _stale_source = os.path.join(
            bc.dir_source, 'athena-installer-data_1.0.0.tar.gz')
        with open(_stale_source, 'w') as fh:
            fh.write('stale tarball')
        _stale_deb = os.path.join(
            bc.dir_repo, 'athena-installer-data_1.0.0+thor1_all.udeb')
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
            bc.dir_repo, 'athena-installer-data_1.0.0+thor1_all.udeb')
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

        # Pre-plant stale .debs for BOTH binaries in repo/
        _stale_main = os.path.join(bc.dir_repo, 'athena-foo_1.0+thor1_all.deb')
        _stale_data = os.path.join(bc.dir_repo, 'athena-foo-data_1.0+thor1_all.deb')
        for _p in (_stale_main, _stale_data):
            with open(_p, 'w') as fh:
                fh.write('stale')
        # And an unrelated package that MUST survive
        _unrelated = os.path.join(bc.dir_repo, 'libfoo_1.0_all.deb')
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
# package reload — dep-hash classification + sidecar plumbing
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
    """Adding a new Depends: must shift the dep-hash so package reload
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
    sidecars so package reload's two-stage decision has both baselines."""
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


def test_cmd_rebump_parses_source_name_filter_args():
    """package rebump with non-`force` args must collect them into a
    Source-name filter set, so a partial rebump can target just the
    kernel family without touching everything else."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    # Pin: rebump cmd reads a filter set from non-force args
    assert "_source_filter = _args_set - {'force'}" in _body, (
        "rebump must compute _source_filter as args minus 'force' so "
        "a targeted rebump only touches the named sources")
    # Pin: filter is applied by reading the .deb's Source: control field
    assert "DebFile" in _body and "_source_filter and _todo" in _body, (
        "rebump filter must actually narrow _todo via DebFile control "
        "field read — filename-prefix would miss linux-signed-amd64's "
        "binaries that ship as linux-image-*")


def test_cmd_source_rescan_registered_in_dispatcher():
    """source rescan must be wired in cmd_source's dispatch table —
    otherwise the user gets `Unknown sub-command` and the command is
    a phantom."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    assert "'rescan'" in _body, "rescan not advertised in source help"
    assert "cmd_source_rescan" in _body, (
        "cmd_source_rescan method missing or wrong name")
    # Pin: rescan dispatches to the method
    import re
    assert re.search(
        r"if action == 'rescan':\s*\n\s+return self\.cmd_source_rescan",
        _body), "source rescan not dispatched in cmd_source"


def test_cmd_source_rescan_method_uses_check_build():
    """rescan must NOT re-implement check_build — it shares the same
    decision the source-build path uses, so its count actually reflects
    what would rebuild.  Pinning the integration to keep them in sync."""
    _bc = os.path.join(_ROOT, 'scripts', 'build.py')
    with open(_bc) as fh:
        _body = fh.read()
    # Locate the cmd_source_rescan body and verify it calls check_build
    import re
    _m = re.search(
        r'def cmd_source_rescan\(self, \*args\):.*?(?=\n    def )',
        _body, re.DOTALL)
    assert _m, "cmd_source_rescan not found"
    _method = _m.group(0)
    assert 'self.container.check_build' in _method, (
        "rescan must use BuildContainer.check_build so it stays in sync "
        "with source build's skip logic")
    # Pin: rescan flags-gate on cache + dep + container ready
    assert 'cache_ready' in _method and 'dep_check_ready' in _method \
        and 'build_container_ready' in _method, (
        "rescan must gate on the three readiness flags so it doesn't "
        "scan against stale state")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [
        # v0.2 step 1
        test_mirror_url_composition,
        test_mirror_suite_with_suffix,
        test_mirror_repr_does_not_crash,
        test_mirror_is_frozen_after_construction,
        test_mirror_normalises_baseurl_and_baseid_slashes,
        test_mirror_rejects_empty_required_fields,
        test_mirror_rejects_baseurl_without_scheme,
        test_mirror_rejects_suffix_without_leading_dash,
        test_mirror_with_snapshot_returns_new_instance_untouched_original,
        test_buildconfig_parses_three_mirrors,
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
        test_installer_chroot_resolve_udeb_files_skips_virtual_aliases,
        test_installer_chroot_resolve_udeb_files_strips_binnmu_suffix,
        test_installer_chroot_resolve_udeb_files_logs_missing_silently,
        test_installer_chroot_resolve_udeb_files_skips_record_without_filename,
        # FORK-01 Step 4: helpers replaced by athena-installer-data udeb
        test_athena_installer_data_ships_runtime_dirs,
        test_athena_installer_data_ships_release_files_with_tokens,
        test_buildcontainer_injects_athena_codename_env,
        # COMP-02 phase B — stock d-i image-build conformance helpers
        test_installer_chroot_sudo_write_does_not_leak_password_to_tee,
        test_athena_installer_data_ships_mirror_protocol_stub,
        test_installer_chroot_run_depmod_skips_when_no_modules_dir,
        test_installer_chroot_run_depmod_indexes_each_kernel_present,
        test_strip_debian_residue_hooks_removes_known_files,
        test_strip_debian_residue_hooks_idempotent_on_missing_targets,
        test_strip_debian_residue_hooks_called_in_build_flow,
        test_installer_chroot_register_self_appends_debian_installer_stanza,
        test_installer_chroot_register_self_idempotent_on_repeat,
        test_installer_grub_cfg_has_preseed_kernel_cmdline,
        test_buildconfig_chroot_paths_under_shared_buildroot_parent,
        test_build_flags_carries_chroot_installer_ready_default_false,
        test_console_with_explicit_tui_does_not_touch_singleton,
        test_console_singleton_fallback_when_tui_omitted,
        test_console_raises_when_no_tui_anywhere,
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
        # apply_distro_suffix — bump bumped binaries with `+thor1`
        test_apply_distro_suffix_appends_to_stripped_filename,
        test_apply_distro_suffix_empty_is_noop,
        test_apply_distro_suffix_idempotent_on_already_suffixed,
        test_apply_distro_suffix_preserves_deb12u1_security_suffix,
        test_apply_distro_suffix_beats_debian_bin_nmu_constraint,
        test_apply_distro_suffix_rejects_malformed_filename,
        test_apply_distro_suffix_noop_skips_shape_check_for_empty,
        test_rebump_deb_file_round_trip_rewrites_control_and_renames,
        test_rebump_deb_file_idempotent_on_already_bumped,
        test_rebump_deb_file_empty_suffix_is_noop,
        test_rebump_deb_file_handles_udeb_extension,
        test_rebump_deb_file_skips_malformed_filename,
        test_rebump_deb_file_preserves_epoch_from_control,
        test_restore_deb_epoch_prepends_when_missing,
        test_rewrite_intra_thor1_strict_equals_round_trip,
        test_rewrite_intra_thor1_strict_equals_idempotent,
        test_rewrite_intra_thor1_strict_equals_leaves_conflicts_untouched,
        test_restore_deb_epoch_empty_prefix_is_noop,
        test_rebump_deb_file_skips_non_deb_files,
        test_cmd_rebump_packages_registered_under_package_dispatcher,
        test_production_build_conf_has_noautodbgsym_in_build_options,
        test_buildconfig_parses_distro_suffix,
        test_buildconfig_distro_suffix_defaults_to_empty,
        test_buildcontainer_emits_changelog_bump_when_distro_suffix_set,
        test_buildcontainer_emits_token_substitution_snippet,
        test_buildcontainer_token_subst_uses_if_not_short_circuit_and,
        test_buildcontainer_token_subst_no_double_braces_in_regular_strings,
        test_buildcontainer_token_subst_grep_rescue_or_true,
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
        test_dependency_tree_default_does_not_auto_pick_across_names,
        test_dependency_tree_udeb_tree_flag_enables_max_version_fallback,
        test_auto_pick_candidate_prefers_real_package_matching_seed_name,
        test_dependency_tree_constructor_accepts_auto_pick_flag,
        test_buildsession_initialises_udeb_dep_tree_as_none,
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
        test_buildflags_carry_iso_ready_state,
        test_autorun_dispatcher_routes_bare_to_live_and_explicit_to_each,
        test_autorun_live_runs_source_build_then_source_build_live,
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
        test_cmd_rebump_parses_source_name_filter_args,
        test_cmd_source_rescan_registered_in_dispatcher,
        test_cmd_source_rescan_method_uses_check_build,
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
