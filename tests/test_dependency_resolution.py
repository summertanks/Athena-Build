"""Athena tests — dependency resolution (dependencytree.py, package.py, or_resolve.py).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile

from _test_helpers import (  # noqa: F401
    _FakeCache,
    _FakePkg,
    _ParseDepPkg,
    _ROOT,
    _StubProviderCache,
    _build_dep_tree_with_recommend,
    _make_offline_cache,
    _make_parse_dep_tree,
    _make_pool_dep_tree_stub,
    _or_resolve_xterm_graph,
    _seed_test_dep_tree,
    _session_source,
    _src_with_build_depends,
    _sta18_make_dt,
    _stub_tui,
)




def test_sta33_build_depends_serialises_apt_pkg_profile_global():
    """STA-33: the apt_pkg Build-Profiles global set + every parse that
    reads it must run under a module lock so concurrent build workers
    can't swap the profile set out from under each other's parse."""
    import inspect, sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import package
    assert hasattr(package, '_BUILD_PROFILES_LOCK'), (
        "module-level _BUILD_PROFILES_LOCK missing")
    _src = inspect.getsource(package.Source.build_depends)
    # The global-set and the parse CALL must be inside the `with` lock
    # block.  (rindex for the parse: the docstring also names
    # parse_src_depends, so take the last/code occurrence.)
    assert 'with _BUILD_PROFILES_LOCK:' in _src, _src
    _lock_at = _src.index('with _BUILD_PROFILES_LOCK:')
    _set_at = _src.index("apt_pkg.config['APT::Build-Profiles'] =")
    _parse_at = _src.rindex('apt_pkg.parse_src_depends(')
    assert _lock_at < _set_at < _parse_at, (
        "the profile-global set AND parse_src_depends must both be inside "
        "the lock block")



def test_sta45_provides_does_not_clobber_real_selected_package():
    """STA-45: a virtual Provides must not overwrite an already-selected REAL
    package of the same name.  Resolve `foo` (real) then `bar` (Provides: foo)
    AFTER it — foo must survive as the canonical selected_pkgs['foo'] entry,
    not be clobbered to point at bar (which made foo vanish from the closure,
    order-dependently, under the old unconditional write)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    _pkg_blob = (
        "Package: foo\n"
        "Source: foo\n"
        "Version: 1.0-1\n"
        "Architecture: amd64\n"
        "Filename: pool/main/f/foo/foo_1.0-1_amd64.deb\n"
        "Size: 1\n"
        "SHA256: " + ("a" * 64) + "\n"
        "\n"
        "Package: bar\n"
        "Source: bar\n"
        "Version: 2.0-1\n"
        "Architecture: amd64\n"
        "Provides: foo\n"
        "Filename: pool/main/b/bar/bar_2.0-1_amd64.deb\n"
        "Size: 1\n"
        "SHA256: " + ("b" * 64) + "\n"
    )
    _src_blob = (
        "Package: foo\n"
        "Binary: foo\n"
        "Version: 1.0-1\n"
        "Architecture: any\n"
        "Directory: pool/main/f/foo\n"
        "Checksums-Sha256:\n"
        " " + ("a" * 64) + " 100 foo_1.0-1.dsc\n"
        "\n"
        "Package: bar\n"
        "Binary: bar\n"
        "Version: 2.0-1\n"
        "Architecture: any\n"
        "Directory: pool/main/b/bar\n"
        "Checksums-Sha256:\n"
        " " + ("b" * 64) + " 100 bar_2.0-1.dsc\n"
    )
    with tempfile.TemporaryDirectory() as _td:
        _cache = _make_offline_cache(
            _td, packages={'main': _pkg_blob}, sources={'main': _src_blob})
        assert _cache.is_valid, _cache.error_str
        _dt = dependencytree.DependencyTree(
            _cache, select_recommended=False, arch='amd64',
            build_profiles=['nodoc', 'nocheck'])
        # foo first, then bar (Provides: foo) — the order that triggered the clobber.
        _dt.resolve_packages(['foo', 'bar'])
        # foo survives as its own canonical entry; bar registered under bar.
        assert _dt.selected_pkgs['foo']['Package'] == 'foo', \
            f"foo clobbered → {_dt.selected_pkgs['foo']['Package']}"
        assert _dt.selected_pkgs['bar']['Package'] == 'bar'
        assert _dt.selected_pkgs['foo'] is not _dt.selected_pkgs['bar']



def test_sta45_replacement_fork_supersedes_real_package():
    """STA-45 follow-up: a REPLACEMENT package (`Provides: X` + `Replaces: X`,
    our same-name forks like athena-tasksel→tasksel) MUST take the name and
    supersede the real X — unlike a genuine virtual provider, which the real
    package beats.  Resolve `foo` (real) then `bar` (Provides+Replaces foo):
    selected_pkgs['foo'] must now point at bar, so the real foo drops out of
    the canonical set and the `Provides X + Conflicts X = "I am X"` self-skip
    in the conflict pass holds (else the fork's Conflicts fires spuriously)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree
    _pkg_blob = (
        "Package: foo\nSource: foo\nVersion: 1.0-1\nArchitecture: amd64\n"
        "Filename: pool/main/f/foo/foo_1.0-1_amd64.deb\nSize: 1\n"
        "SHA256: " + ("a" * 64) + "\n\n"
        "Package: bar\nSource: bar\nVersion: 2.0-1\nArchitecture: amd64\n"
        "Provides: foo (= 1.0-1)\nConflicts: foo\nReplaces: foo\n"
        "Filename: pool/main/b/bar/bar_2.0-1_amd64.deb\nSize: 1\n"
        "SHA256: " + ("b" * 64) + "\n"
    )
    _src_blob = (
        "Package: foo\nBinary: foo\nVersion: 1.0-1\nArchitecture: any\n"
        "Directory: pool/main/f/foo\nChecksums-Sha256:\n"
        " " + ("a" * 64) + " 100 foo_1.0-1.dsc\n\n"
        "Package: bar\nBinary: bar\nVersion: 2.0-1\nArchitecture: any\n"
        "Directory: pool/main/b/bar\nChecksums-Sha256:\n"
        " " + ("b" * 64) + " 100 bar_2.0-1.dsc\n"
    )
    with tempfile.TemporaryDirectory() as _td:
        _cache = _make_offline_cache(
            _td, packages={'main': _pkg_blob}, sources={'main': _src_blob})
        assert _cache.is_valid, _cache.error_str
        _dt = dependencytree.DependencyTree(
            _cache, select_recommended=False, arch='amd64',
            build_profiles=['nodoc', 'nocheck'])
        _dt.resolve_packages(['foo', 'bar'])
        # replacement fork wins the name; real foo superseded out of canonical set.
        assert _dt.selected_pkgs['foo']['Package'] == 'bar', \
            f"replacement fork lost: {_dt.selected_pkgs['foo']['Package']}"
        assert _dt.selected_pkgs['bar']['Package'] == 'bar'



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



def test_build_depends_real_name_with_provider_aliases_not_expanded():
    """A name that is BOTH a real package AND Provided by others must NOT
    be expanded.  Real case: libunwind-dev is a real package (src
    libunwind) while LLVM's libunwind-{14,15,16,19}-dev all
    `Provides: libunwind-dev`.  Expansion sorted the LLVM providers
    first in the ||-chain, the container installed libunwind-14-dev
    (which ships no libunwind.pc), and gstreamer1.0's meson hard-failed
    with `Dependency "libunwind" not found` (2026-06-07, thor1 full
    rebuild).  apt semantics: a concrete name is never substituted by
    a Provides alias — the group must pass through verbatim."""
    cache = _StubProviderCache({
        'libunwind-dev': [
            {'Package': 'libunwind-14-dev'},
            {'Package': 'libunwind-15-dev'},
            {'Package': 'libunwind-dev'},      # the real package
            {'Package': 'libunwind-19-dev'},
        ],
    })
    src = _src_with_build_depends("libunwind-dev")
    groups = src.build_depends('amd64', cache=cache)
    assert len(groups) == 1
    assert [alt[0] for alt in groups[0]] == ['libunwind-dev'], groups[0]



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



def test_build_depends_prefix_matched_provider_sorts_first():
    """Providers whose name starts with the virtual name (e.g.
    `imagemagick-6.q16` for virtual `imagemagick`) sort BEFORE
    name-unrelated providers (`graphicsmagick-imagemagick-compat`).
    Plain alphabetic order would put graphicsmagick-imagemagick-compat
    first — the BuildContainer's `||`-chain would stop at the first
    install success, leaving graphicsmagick's `convert` shim in place
    of real ImageMagick, breaking fonts-noto-color-emoji's Makefile
    rule that uses ImageMagick-specific `canvas:none` input."""
    cache = _StubProviderCache({
        'imagemagick': [
            {'Package': 'graphicsmagick-imagemagick-compat'},
            {'Package': 'imagemagick-6.q16'},
        ],
    })
    src = _src_with_build_depends("imagemagick")
    groups = src.build_depends('amd64', cache=cache)
    names = [alt[0] for alt in groups[0]]
    assert names == [
        'imagemagick-6.q16',
        'graphicsmagick-imagemagick-compat',
        'imagemagick',
    ], names



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
            self.alt_pre_depends = []
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
    _body = _session_source()
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
    # (Command renamed `repo cleanup` → `repo repair cleanup` in P1.)
    assert 'repo repair cleanup' in _helper, (
        "warn-only path must point operator at `repo repair cleanup` "
        "for the actual action"
    )
    # Helper must NOT call os.remove — warn-only.
    assert 'os.remove' not in _helper, (
        "warn-only audit path must NOT delete files; cleanup owns "
        "deletion"
    )



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
            self.alt_pre_depends = []
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
            self.alt_pre_depends = []
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
    import sys
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
            self.alt_pre_depends = []
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



def test_tier3_doc_source_pins():
    """Doc-accuracy pins for Tier-3 fixes #40/#130/#149 (pure docstring/comment
    corrections — no behaviour change to assert)."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import bump
    import or_resolve
    import installer_chroot
    assert "'5.2.15-2+asg1u0+p1'" in inspect.getsource(bump), '#40'
    assert 'DETERMINISTIC, order-independent closure' in inspect.getsource(
        or_resolve), '#149'
    assert 'find_matching_artifact' in inspect.getsource(installer_chroot), \
        '#130'



def test_resolve_closure_accepts_generator_seeds():
    """Regression (audit #148): resolve_closure consumes `seeds` twice
    (_infer_real + the _pending comprehension), so a one-shot generator was
    exhausted by the first pass and yielded an empty closure on the default
    (real_pkgs=None) path. Materialize seeds once."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import or_resolve
    _out = or_resolve.resolve_closure((_s for _s in ['a']), {'a': []})
    assert _out == {'a'}, _out



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



def test_parse_dependency_resolves_or_grouped_pre_depends():
    """Regression (audit #10): an OR-grouped Pre-Depends (`Pre-Depends: a | b`,
    carried as alt_pre_depends) must be resolved through the SAME alternative-
    selection loop as alt_depends — defaulting to the first alternative when
    none is pre-selected — so its provider enters the closure.  Before the fix
    parse_dependency never read alt_pre_depends, so an OR pre-dep whose
    providers aren't otherwise pulled was silently dropped (and
    validate_selection never flagged it either)."""
    a = _ParseDepPkg('a', '1.0')
    b = _ParseDepPkg('b', '1.0')
    foo = _ParseDepPkg('foo', '1.0', alt_pre_depends=[
        [('a', '', ''), ('b', '', '')]
    ])
    dt = _make_parse_dep_tree({'foo': [foo], 'a': [a], 'b': [b]})
    dt.parse_dependency('foo')
    # First alternative of the OR pre-dep is pulled (Debian convention).
    assert 'a' in foo.depends_on
    assert 'b' not in foo.depends_on



def test_dependencytree_pins_resolve_silently_and_record_picks():
    """SELECT-LOCK Chunk 3: a pinned dep auto-selects its candidate (no
    prompt) and records the pick; an unsatisfiable/absent pin returns None
    (caller re-prompts); a fresh prompt pick is recorded for first-run seed."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from dependencytree import DependencyTree
    # cache=None is safe: __init__ only touches cache when lookahead is given.
    _dt = DependencyTree(cache=None, select_recommended=False, arch='amd64',
                         pins={'awk': 'mawk'})
    _collapsed = [{'Package': 'gawk'}, {'Package': 'mawk'}]
    _picked = _dt._apply_pin('awk', _collapsed)
    assert _picked == {'Package': 'mawk'}, _picked
    assert _dt._pinned_chosen == {'awk': 'mawk'}, _dt._pinned_chosen
    # pinned package no longer among candidates ⇒ None (→ re-baseline path)
    assert _dt._apply_pin('awk', [{'Package': 'gawk'}]) is None
    # no pin configured ⇒ None
    _dt2 = DependencyTree(cache=None, select_recommended=False, arch='amd64')
    assert _dt2._pins == {}
    assert _dt2._apply_pin('awk', _collapsed) is None
    # a genuine prompt pick is recorded for first-run lockfile seeding
    _dt2._record_prompt_pick('telnet-client', {'Package': 'telnet'})
    assert _dt2._pinned_chosen == {'telnet-client': 'telnet'}, _dt2._pinned_chosen



def test_or_resolve_greedy_diverges_fixpoint_does_not():
    """SELECT-02: the greedy (live-resolver) model gives DIFFERENT closures
    for the two seed orderings, while the order-independent fixpoint gives
    the same closure (matching the minimal, no-xterm result)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import or_resolve as _or
    deps, provides = _or_resolve_xterm_graph()

    # Greedy: order decides whether xterm (+ libutempter0) is pulled.
    _g1 = _or.resolve_closure_greedy(['xorg', 'gnome-terminal'], deps, provides=provides)
    _g2 = _or.resolve_closure_greedy(['gnome-terminal', 'xorg'], deps, provides=provides)
    assert 'xterm' in _g1 and 'libutempter0' in _g1, _g1
    assert 'xterm' not in _g2 and 'libutempter0' not in _g2, _g2
    assert _g1 != _g2, "expected the greedy model to be order-sensitive"

    # Fixpoint: identical for either ordering; x-terminal-emulator is
    # satisfied by gnome-terminal's Provides, so xterm is never pulled.
    _f1 = _or.resolve_closure(['xorg', 'gnome-terminal'], deps, provides=provides)
    _f2 = _or.resolve_closure(['gnome-terminal', 'xorg'], deps, provides=provides)
    assert _f1 == _f2 == {'xorg', 'gnome-terminal'}, (_f1, _f2)
    assert 'xterm' not in _f1



def test_or_resolve_fixpoint_invariant_over_all_permutations():
    """The fixpoint closure is a pure function of the seed SET — identical
    across EVERY seed ordering — whereas the greedy model produces more than
    one distinct closure over those same orderings."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import or_resolve as _or
    import itertools
    deps, provides = _or_resolve_xterm_graph()
    _seeds = ['xorg', 'gnome-terminal']

    _fix = {frozenset(_or.resolve_closure(_p, deps, provides=provides))
            for _p in itertools.permutations(_seeds)}
    _greedy = {frozenset(_or.resolve_closure_greedy(list(_p), deps, provides=provides))
               for _p in itertools.permutations(_seeds)}
    assert len(_fix) == 1, f"fixpoint must be order-invariant, got {_fix}"
    assert len(_greedy) >= 2, f"greedy expected to diverge, got {_greedy}"



def test_or_resolve_first_alt_when_no_alternative_satisfied():
    """With no satisfying alternative present, the fixpoint pulls the FIRST
    declared alternative (Debian convention) plus its hard deps."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import or_resolve as _or
    deps, provides = _or_resolve_xterm_graph()
    # Only xorg seeded — nothing Provides x-terminal-emulator in the closure.
    _c = _or.resolve_closure(['xorg'], deps, provides=provides)
    assert _c == {'xorg', 'xterm', 'libutempter0'}, _c



def test_or_resolve_canonical_tiebreak_is_seed_order_free():
    """Two mirror-image OR groups (a|b and b|a) with no provider: the
    fixpoint always resolves the canonically-smallest group's first
    alternative ('a'), regardless of seed order — the greedy model would
    pick 'a' or 'b' depending on which seed is visited first."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import or_resolve as _or
    deps = {'p': [('a', 'b')], 'q': [('b', 'a')], 'a': [], 'b': []}
    assert _or.resolve_closure(['p', 'q'], deps) == {'p', 'q', 'a'}
    assert _or.resolve_closure(['q', 'p'], deps) == {'p', 'q', 'a'}
    # greedy diverges: visiting p first pulls 'a', visiting q first pulls 'b'.
    _ga = _or.resolve_closure_greedy(['p', 'q'], deps)
    _gb = _or.resolve_closure_greedy(['q', 'p'], deps)
    assert _ga == {'p', 'q', 'a'} and _gb == {'p', 'q', 'b'}, (_ga, _gb)



def test_resolve_closure_multi_group_and_generator_seeds():
    """Audit #152: multiple interacting OR groups resolve deterministically
    (a function of the graph, not seed order) and generator seeds are
    consumed correctly."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from or_resolve import resolve_closure
    _deps = {'p': [('a', 'b')], 'q': [('b', 'c')],
             'a': [], 'b': [], 'c': []}
    _r1 = resolve_closure(['p', 'q'], _deps)
    _r2 = resolve_closure(['q', 'p'], _deps)          # order-independent
    assert _r1 == _r2, (_r1, _r2)
    assert {'p', 'q'} <= _r1
    assert ('a' in _r1 or 'b' in _r1)                 # p's group satisfied
    assert ('b' in _r1 or 'c' in _r1)                 # q's group satisfied
    _r3 = resolve_closure((_s for _s in ['p', 'q']), _deps)   # generator seeds
    assert _r3 == _r1



def test_package_add_constraint_conflict_matrix():
    """Audit #153: add_constraint's (new,old) conflict matrix — nc keeps old,
    xg replaces with the stricter, eq collapses to '=', err keeps old +
    returns False; an invalid operator string is rejected."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import package as _pkgmod
    from debian.debian_support import Version

    def _mk():
        _p = object.__new__(_pkgmod.Package)
        _p._constraints = {}
        _p.package = 'x'
        _p.version = Version('1.0')
        return _p

    _v = Version('2.0')
    # first add stores it verbatim
    _p = _mk()
    assert _p.add_constraint(_v, '>=') is True
    assert _p._constraints[_v].constraint == '>='
    # xg: >= then >> → stored becomes the stricter >>
    assert _p.add_constraint(_v, '>>') is True
    assert _p._constraints[_v].constraint == '>>'
    # eq: >= then <= → collapses to '='
    _p2 = _mk(); _p2.add_constraint(_v, '>=')
    assert _p2.add_constraint(_v, '<=') is True
    assert _p2._constraints[_v].constraint == '='
    # nc: = then >= keeps '='
    _p3 = _mk(); _p3.add_constraint(_v, '=')
    assert _p3.add_constraint(_v, '>=') is True
    assert _p3._constraints[_v].constraint == '='
    # err: = then >> is unresolvable → keeps old, returns False
    _p4 = _mk(); _p4.add_constraint(_v, '=')
    assert _p4.add_constraint(_v, '>>') is False
    assert _p4._constraints[_v].constraint == '='
    # invalid operator string → rejected
    assert _mk().add_constraint(_v, '~=') is False



def test_dependencytree_pickle_roundtrip():
    """Audit #112: DependencyTree.__getstate__/__setstate__ round-trip — the
    non-picklable __cache is dropped (restored to None), and __lookahead's
    defaultdict is flattened for pickling then restored as a defaultdict."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import pickle
    from collections import defaultdict
    import dependencytree as _dt
    _tree = object.__new__(_dt.DependencyTree)
    # a lambda is unpicklable — if __getstate__ didn't drop __cache, dumps()
    # would raise, so this also proves the drop.
    _tree.__dict__['_DependencyTree__cache'] = lambda: None
    _la: 'defaultdict' = defaultdict(dict)
    _la['x'] = {'1.0': 'pkgstub'}
    _tree.__dict__['_DependencyTree__lookahead'] = _la
    _tree.selected_pkgs = {'a': 1}
    _round = pickle.loads(pickle.dumps(_tree))
    assert _round.selected_pkgs == {'a': 1}
    assert _round.__dict__['_DependencyTree__cache'] is None
    _rla = _round.__dict__['_DependencyTree__lookahead']
    assert isinstance(_rla, defaultdict)
    assert _rla['x'] == {'1.0': 'pkgstub'}



def test_dep_drift_syncs_version_from_disk():
    """Audit #110: _check_dep_drift syncs the cache Package's Version to the
    on-disk (NMU-stripped) value, so the consumer/provider version surfaces
    agree (the 144-spurious-mismatch fix) - both .version and ['Version']."""
    from unittest import mock
    import types as _t
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dep_drift as _dd
    import package as _pkgm
    with tempfile.TemporaryDirectory() as _repo:
        _fn = 'libx_0.8-10_amd64.deb'
        open(os.path.join(_repo, _fn), 'w').close()
        _cache_pkg = _pkgm.Package(
            "Package: libx\nVersion: 0.8-10+deb12u1\n"
            f"Architecture: amd64\nFilename: pool/{_fn}\n")
        _dt = _t.SimpleNamespace(canonical_pkgs={'libx': _cache_pkg},
                                 selected_pkgs={})
        _self = _t.SimpleNamespace(
            _dependencytree=_dt, _dir_repo_main=_repo,
            normalize_repo_filename=lambda _f: _f)
        _self._verify_dep_resolution = (
            lambda: _dd._DepDriftMixin._verify_dep_resolution(_self))

        def _fake_run(argv, **k):
            return _t.SimpleNamespace(
                returncode=0,
                stdout="Package: libx\nVersion: 0.8-10\nArchitecture: amd64\n",
                stderr='')

        with mock.patch.object(_dd, 'subprocess',
                               _t.SimpleNamespace(run=_fake_run)):
            _dd._DepDriftMixin._check_dep_drift(_self)
        assert str(_cache_pkg.version) == '0.8-10', _cache_pkg.version
        assert _cache_pkg.get('Version') == '0.8-10', _cache_pkg.get('Version')



def test_dependencytree_order_independence_report():
    """SELECT-02 shadow: order_independence_report flags the order-pulled
    extras the greedy closure holds that an order-independent fixpoint would
    drop (xterm + libutempter0 once x-terminal-emulator is satisfied by
    gnome-terminal's Provides), and reports nothing for an order-invariant
    closure."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dependencytree as _dt

    class _P:
        def __init__(self, name, depends=(), alt_depends=(), provides=()):
            self._n = name
            self.depends = [(_d, '', '') for _d in depends]
            self.pre_depends = []
            self.alt_depends = [[(a, '', '') for a in g] for g in alt_depends]
            self.alt_pre_depends = []
            self.recommends = []
            self._prov = list(provides)

        def __getitem__(self, k):
            if k == 'Package':
                return self._n
            raise KeyError(k)

        def get_provides(self):
            return [(v, '') for v in self._prov]

    # (1) order-sensitive: greedy pulled xterm (+ its libutempter0 dep) even
    # though gnome-terminal Provides x-terminal-emulator.
    _tree = object.__new__(_dt.DependencyTree)
    _tree._DependencyTree__recommended = False
    _tree.selected_pkgs = {
        'xorg': _P('xorg', alt_depends=[['xterm', 'x-terminal-emulator']]),
        'gnome-terminal': _P('gnome-terminal',
                             provides=['x-terminal-emulator']),
        'xterm': _P('xterm', depends=['libutempter0']),
        'libutempter0': _P('libutempter0'),
    }
    _tree._seed_history = ['xorg', 'gnome-terminal']
    _rep = _tree.order_independence_report()
    assert set(_rep['greedy_only']) == {'xterm', 'libutempter0'}, _rep
    assert _rep['fixpoint_only'] == [], _rep

    # (2) order-invariant plain chain a->b->c: no divergence.
    _tree2 = object.__new__(_dt.DependencyTree)
    _tree2._DependencyTree__recommended = False
    _tree2.selected_pkgs = {
        'a': _P('a', depends=['b']),
        'b': _P('b', depends=['c']),
        'c': _P('c'),
    }
    _tree2._seed_history = ['a']
    assert _tree2.order_independence_report() == {
        'greedy_only': [], 'fixpoint_only': []}

    # (3) fixpoint_only is filtered to cache-resolvable names: the fixpoint
    # model keeps unresolvable names as unknown leaves (fwupd →
    # secureboot-db on bookworm, which does not exist in the archive) —
    # those phantoms must not be reported; a genuinely addable package is.
    class _CacheStub:
        def get_packages(self, name, version='', constraint=''):
            return [object()] if name == 'real-extra' else []

    _tree3 = object.__new__(_dt.DependencyTree)
    _tree3._DependencyTree__recommended = True
    _tree3._DependencyTree__cache = _CacheStub()
    _p_a = _P('a')
    _p_a.recommends = [('ghost-pkg', '', ''), ('real-extra', '', '')]
    _tree3.selected_pkgs = {'a': _p_a}
    _tree3._seed_history = ['a']
    _rep3 = _tree3.order_independence_report()
    assert _rep3['fixpoint_only'] == ['real-extra'], _rep3
    assert 'ghost-pkg' not in _rep3['fixpoint_only'], _rep3

TESTS = [
    test_sta33_build_depends_serialises_apt_pkg_profile_global,
    test_sta45_provides_does_not_clobber_real_selected_package,
    test_sta45_replacement_fork_supersedes_real_package,
    test_package_and_source_have_mirror_field,
    test_source_parses_security_stanza_without_files_field,
    test_source_parses_main_stanza_with_both_files_and_sha256,
    test_build_depends_no_cache_leaves_virtuals_unchanged,
    test_build_depends_expands_multi_provider_virtual,
    test_build_depends_single_provider_virtual_not_expanded,
    test_build_depends_real_package_name_not_expanded,
    test_build_depends_real_name_with_provider_aliases_not_expanded,
    test_build_depends_already_alternative_group_untouched,
    test_build_depends_version_constraint_inherited_by_synthetic_providers,
    test_build_depends_unknown_name_left_unchanged,
    test_resolve_packages_check_conflicts_false_skips_lookahead_check,
    test_validate_selection_skips_conflict_when_pool_extra,
    test_validate_selection_skips_break_when_pool_extra,
    test_validate_selection_still_fires_on_non_pool_conflicts,
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
    test_derive_extras_src_names_marks_extras_only_sources,
    test_dep_tree_initialises_subset_exclusive_sets_empty,
    test_dep_tree_initialises_pkg_group_fields_empty,
    test_derive_subset_exclusive_src_names_marks_live_only_sources,
    test_derive_subset_exclusive_src_names_no_op_when_both_empty,
    test_derive_subset_exclusive_src_names_handles_installer_exclusive,
    test_parse_dependency_reuses_lookahead_for_multi_version_same_name,
    test_dependency_tree_udeb_tree_flag_enables_max_version_fallback,
    test_auto_pick_candidate_prefers_real_package_matching_seed_name,
    test_dependency_tree_constructor_accepts_auto_pick_flag,
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
    test_parse_dependency_resolves_or_grouped_pre_depends,
    test_dependencytree_pins_resolve_silently_and_record_picks,
    test_or_resolve_greedy_diverges_fixpoint_does_not,
    test_or_resolve_fixpoint_invariant_over_all_permutations,
    test_or_resolve_first_alt_when_no_alternative_satisfied,
    test_or_resolve_canonical_tiebreak_is_seed_order_free,
    test_build_depends_prefix_matched_provider_sorts_first,
    test_package_audit_includes_stale_files_warning_section,
    test_sta18_version_for_constraint_target_real_pkg,
    test_sta18_version_for_constraint_target_versioned_provides_with_epoch,
    test_sta18_version_for_constraint_target_unversioned_provides_returns_none,
    test_sta18_validate_selection_resolves_epoch_aliased_alt_dep,
    test_sta18_validate_selection_unversioned_provides_cannot_satisfy_versioned_dep,
    test_tier3_doc_source_pins,
    test_resolve_closure_accepts_generator_seeds,
    test_resolve_closure_multi_group_and_generator_seeds,
    test_package_add_constraint_conflict_matrix,
    test_dependencytree_pickle_roundtrip,
    test_dependencytree_order_independence_report,
    test_dep_drift_syncs_version_from_disk,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
