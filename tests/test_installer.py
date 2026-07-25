"""Athena tests — installer chroot / ISO + branding (installer_chroot.py, iso_installer.py, tasksel_desc.py, identity_scan.py).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile

from _test_helpers import (  # noqa: F401
    _FakePkg,
    _ROOT,
    _build_sample_for_pattern,
    _fake_sudo_write_run,
    _session_source,
    _smoke_import,
    _stub_tui,
)




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



def test_iso_installer_kernel_pkg_regex_matches_real_kernels_only():
    """REGRESSION (2026-05-11): the iso_installer kernel finder must
    match real kernel packages (linux-image-<ABI>-amd64_*.deb) and
    skip meta/flavor variants — meta packages are empty + have no
    /boot/vmlinuz, so extracting them produces an unusable kernel
    candidate.  Caught when iso build installer picked
    linux-image-rt-amd64 (an empty preempt-rt meta) and failed."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import arch_profile
    _KERNEL_PKG_RE = arch_profile.profile('amd64').kernel_pkg_re
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
    _body = _session_source()
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



def test_audit_chroot_hooks_strip_allowlisted():
    """A hook running `apt-install <unpooled>` is auto-stripped when its
    path is in installer/strip-hooks-allowlist.  CONF-10 S2 — replaces
    the rotting hardcoded _targets list with a build-time audit."""
    import sys, tempfile
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _audit_and_strip_chroot_hooks

    def _fake_sudo(cmd, _pw):
        class _R:
            returncode = 0; stderr = ''; stdout = ''
        if cmd[0] == 'rm' and cmd[1] == '-f':
            try:
                os.unlink(cmd[2])
            except FileNotFoundError:
                pass
        return _R()

    with tempfile.TemporaryDirectory() as _root:
        _chroot = os.path.join(_root, 'chroot')
        _inst   = os.path.join(_root, 'installer')
        os.makedirs(_inst)
        # Plant two upstream-style Debian-residue hooks + one firmware
        # hook that only references pooled packages.
        _hooks = {
            'usr/lib/pre-pkgsel.d/20install-hwpackages':
                '#!/bin/sh\napt-install discover\n',
            'usr/lib/pre-pkgsel.d/50save-logs':
                '#!/bin/sh\napt-install installation-report\n',
            'usr/lib/pre-pkgsel.d/50install-firmware':
                '#!/bin/sh\napt-install firmware-misc-nonfree || true\n',
        }
        for rel, body in _hooks.items():
            _abs = os.path.join(_chroot, rel)
            os.makedirs(os.path.dirname(_abs), exist_ok=True)
            with open(_abs, 'w') as fh:
                fh.write(body)
        # Allowlist the two Debian-residue ones.
        with open(os.path.join(_inst, 'strip-hooks-allowlist'), 'w') as fh:
            fh.write(
                'usr/lib/pre-pkgsel.d/20install-hwpackages\tdiscover not in pool\n'
                'usr/lib/pre-pkgsel.d/50save-logs\tinstallation-report not in pool\n'
            )
        # firmware-misc-nonfree IS in pool — 50install-firmware audit
        # passes; no action.
        pool = {'firmware-misc-nonfree', 'base-files', 'bash'}
        with patch('installer_chroot._sudo', side_effect=_fake_sudo):
            ok = _audit_and_strip_chroot_hooks(_chroot, pool, _inst, 'pw')
        assert ok is True
        # The two allowlisted residue hooks are gone.
        for rel in ('usr/lib/pre-pkgsel.d/20install-hwpackages',
                    'usr/lib/pre-pkgsel.d/50save-logs'):
            assert not os.path.exists(os.path.join(_chroot, rel)), (
                f'{rel} should have been stripped'
            )
        # The firmware hook stays (its apt-install target IS in pool).
        assert os.path.exists(os.path.join(
            _chroot, 'usr/lib/pre-pkgsel.d/50install-firmware'))



def test_audit_chroot_hooks_no_op_when_no_hooks():
    """Empty hook trees / missing chroot subtrees → audit no-ops.
    Replaces the old _strip_debian_residue_hooks_idempotent_on_missing
    test; new audit walks subtrees and skips when they don't exist."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _audit_and_strip_chroot_hooks
    with tempfile.TemporaryDirectory() as _root:
        _chroot = os.path.join(_root, 'chroot'); os.makedirs(_chroot)
        _inst   = os.path.join(_root, 'installer'); os.makedirs(_inst)
        ok = _audit_and_strip_chroot_hooks(
            _chroot, {'bash'}, _inst, 'pw'
        )
    assert ok is True



def test_audit_chroot_hooks_called_in_build_flow():
    """Pin the call-site in build_installer_chroot so a future refactor
    can't silently drop the audit step."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    from installer_chroot import build_installer_chroot
    _src = inspect.getsource(build_installer_chroot)
    assert '_audit_and_strip_chroot_hooks(' in _src, (
        "_audit_and_strip_chroot_hooks call missing from "
        "build_installer_chroot — installer residue won't be audited"
    )



def test_audit_chroot_hooks_fail_on_hard_unpooled():
    """An `apt-install X` (no `|| true`) targeting an unpooled, non-
    allowlisted package fails the build."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _audit_and_strip_chroot_hooks
    with tempfile.TemporaryDirectory() as _root:
        _chroot = os.path.join(_root, 'chroot')
        _inst   = os.path.join(_root, 'installer'); os.makedirs(_inst)
        _hook = os.path.join(_chroot, 'usr/lib/pre-pkgsel.d/30unknown')
        os.makedirs(os.path.dirname(_hook), exist_ok=True)
        with open(_hook, 'w') as fh:
            fh.write('#!/bin/sh\napt-install mystery-pkg\n')
        # Empty allowlist file; mystery-pkg not in pool → hard fail.
        with open(os.path.join(_inst, 'strip-hooks-allowlist'), 'w') as fh:
            fh.write('# empty\n')
        ok = _audit_and_strip_chroot_hooks(_chroot, {'base-files'}, _inst, 'pw')
    assert ok is False



def test_audit_chroot_hooks_warn_on_soft_unpooled():
    """`apt-install X || true` targeting an unpooled pkg is soft —
    surfaces as WARN, build still passes."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _audit_and_strip_chroot_hooks
    with tempfile.TemporaryDirectory() as _root:
        _chroot = os.path.join(_root, 'chroot')
        _inst   = os.path.join(_root, 'installer'); os.makedirs(_inst)
        _hook = os.path.join(_chroot, 'usr/lib/pre-pkgsel.d/40best-effort')
        os.makedirs(os.path.dirname(_hook), exist_ok=True)
        with open(_hook, 'w') as fh:
            fh.write('#!/bin/sh\napt-install optional-pkg || true\n')
        with open(os.path.join(_inst, 'strip-hooks-allowlist'), 'w') as fh:
            fh.write('# empty\n')
        ok = _audit_and_strip_chroot_hooks(_chroot, {'base-files'}, _inst, 'pw')
    assert ok is True   # soft failure ≠ hard failure



def test_audit_chroot_hooks_skips_when_no_pool():
    """pool_pkg_names=None or empty → audit short-circuits (legacy
    callers without a built dep tree get a no-op)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from installer_chroot import _audit_and_strip_chroot_hooks
    with tempfile.TemporaryDirectory() as _root:
        _chroot = os.path.join(_root, 'chroot')
        _inst   = os.path.join(_root, 'installer'); os.makedirs(_inst)
        # Plant a hook that would otherwise fail.
        _hook = os.path.join(_chroot, 'usr/lib/pre-pkgsel.d/99bad')
        os.makedirs(os.path.dirname(_hook), exist_ok=True)
        with open(_hook, 'w') as fh:
            fh.write('#!/bin/sh\napt-install never-in-pool\n')
        # None pool → skip; True returned.
        ok = _audit_and_strip_chroot_hooks(_chroot, None, _inst, 'pw')
    assert ok is True



def test_parse_apt_install_line_variants():
    """The parser handles the common forms found in real hooks."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from identity_scan import _parse_apt_install_line
    assert _parse_apt_install_line('apt-install foo\n') == (['foo'], False)
    assert _parse_apt_install_line('	apt-install foo bar\n') == (['foo', 'bar'], False)
    assert _parse_apt_install_line('apt-install foo || true\n') == (['foo'], True)
    assert _parse_apt_install_line('apt-install foo || :\n') == (['foo'], True)
    # $logoutput wrapper (apt-setup generators)
    assert _parse_apt_install_line('$logoutput apt-install ca-certificates\n') == (
        ['ca-certificates'], False
    )
    # Comment lines and non-matches return None.
    assert _parse_apt_install_line('# apt-install foo\n') is None
    assert _parse_apt_install_line('echo apt-get install foo\n') is None
    # --no-install-recommends and similar flags skipped.
    assert _parse_apt_install_line(
        'apt-install --no-install-recommends foo\n'
    ) == (['foo'], False)
    # Variable expansion args skipped (not auditable).
    assert _parse_apt_install_line('apt-install $PKG\n') is None
    # Shell redirects must not contaminate the pkg list.  Caught
    # 2026-05-31: upstream 07brltty's `apt-install brltty 1>&2` was
    # mis-parsed as ['brltty', '1'] because the pre-shlex split on `>`
    # grabbed the file descriptor number.  New parser shlex-tokenises
    # and breaks at the first redirect-or-pipe token.
    assert _parse_apt_install_line('    if apt-install brltty 1>&2; then\n') == (
        ['brltty'], False
    )
    assert _parse_apt_install_line('apt-install foo > /dev/null\n') == (
        ['foo'], False
    )
    assert _parse_apt_install_line('apt-install foo 2>&1\n') == (
        ['foo'], False
    )
    assert _parse_apt_install_line('apt-install foo | grep bar\n') == (
        ['foo'], False
    )
    assert _parse_apt_install_line('apt-install foo >> file.log\n') == (
        ['foo'], False
    )
    # Regression (audit #12): 'apt-install' appearing ONLY inside a trailing
    # ' #' inline comment on a non-comment command line must return None, not
    # raise IndexError (the whole-line gate passes but the comment-stripped
    # head no longer contains the token).  Without the guard this aborted the
    # entire chroot-hook audit.
    assert _parse_apt_install_line(
        'echo done # call apt-install foo later\n') is None
    assert _parse_apt_install_line(
        'mkdir /x   # apt-install bar baz\n') is None
    # And a real invocation WITH a trailing comment still parses.
    assert _parse_apt_install_line('apt-install foo # see note\n') == (
        ['foo'], False
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
    """SURFACES-01 rework of the FORK-01 5b invariant: the AUTHORITATIVE
    menu is now GENERATED from the lockfile groups at ISO mastering
    (tasksel_desc.generate_desc) and installed by the athena-pkgsel
    pre-pkgsel.d hook — so the fork's static tasks/* are a FALLBACK,
    no longer required to mirror pkg.list exactly.

    Relaxed invariant (still load-bearing): every EXISTING fork task
    file's Key set must be a SUBSET of its pkg.list group's seeds —
    a Key referencing a package we don't build would make tasksel
    silently hide the task when the fallback desc is in play (the
    2026-05-18 failure class).  A pkg.list group WITHOUT a fork task
    file is fine now (the generated desc covers it); fork Keys the
    group dropped are NOT fine (unbuilt reference).

    [base] is exempt — debootstrapped, never a task.
    """
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import parse_pkg_list_groups
    _pkglist = os.path.join(_ROOT, 'config', 'pkg.list')
    _tasks_dir = os.path.join(_ROOT, 'fork', 'source',
                              'athena-tasksel', 'tasks')
    _groups = parse_pkg_list_groups(_pkglist)
    for _group, _seeds in _groups.items():
        if _group == 'base':
            continue
        _task_file = os.path.join(_tasks_dir, _group)
        if not os.path.isfile(_task_file):
            continue   # generated desc covers groups without a fallback file
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
        _orphans = sorted(set(_key_seeds) - set(_seeds))
        assert not _orphans, (
            f"fallback tasks/{_group} Key entries not in pkg.list "
            f"[{_group}] (would reference unbuilt packages → tasksel "
            f"silently hides the task): {_orphans}"
        )



def test_athena_tasksel_no_test_new_install_skip():
    """`Test-new-install: mark skip` tells tasksel to hide the task
    from the new-install dialog — Debian uses this for `standard`
    because Priority: standard already auto-installs those pkgs.

    We DON'T have the Priority-driven path: standard ships via an
    explicit Key: list (see project_pkg_list_groups_mirror_tasksel_keys
    memory).  So if `mark skip` is present, the operator never sees
    the task and the Key: pkgs only land via base_include — meaning
    operator can't toggle the group off and might not realize it's
    being installed.

    Caught 2026-05-31 install: operator reported 5/6 non-base tasks
    visible in the installer, `standard` missing.  Root cause was
    `Test-new-install: mark skip` left over from the upstream
    template that seeded the file."""
    import glob
    _tasks_dir = os.path.join(_ROOT, 'fork', 'source',
                              'athena-tasksel', 'tasks')
    for _path in sorted(glob.glob(os.path.join(_tasks_dir, '*'))):
        if not os.path.isfile(_path) or os.path.basename(_path) == 'README':
            continue
        with open(_path) as fh:
            for _i, _line in enumerate(fh, 1):
                _stripped = _line.strip()
                # Skip blank + comments.
                if not _stripped or _stripped.startswith('#'):
                    continue
                if _stripped.startswith('Test-new-install:'):
                    assert 'skip' not in _stripped.lower(), (
                        f"{_path}:{_i} has `{_stripped}` — would hide the "
                        f"task from the installer.  Athena tasks ship via "
                        f"explicit Key: list; remove this line."
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



def test_athena_tasksel_fork_ships_curated_task_set():
    """FORK-01 Step 5b + 2026-06-02 expansion: the fork's tasks/ dir
    contains exactly the curated set of tasks Asgard chooses to
    surface in the d-i task selector.

    Original 2026-05-17 set (6): standard, ssh-server, laptop,
    desktop, gnome-desktop, development-tools.  These mirror
    config/pkg.list groups and drive the install-time package
    selection — sync enforced by
    test_athena_tasksel_task_keys_mirror_pkg_list_groups.

    2026-06-02 addition (7): asgard-office, asgard-pim,
    asgard-multimedia, asgard-gnome-extras, asgard-accessibility,
    asgard-network-services, asgard-printing-extras.
    These are operator-facing subcategory tasks whose Key: list is
    a single metapackage name (the asgard-<group> metapackage in
    pool.list).  Each metapackage Depends on asgard-gnome-desktop
    so selecting any of these tasks pulls the GNOME desktop base
    automatically via apt's resolver.  These tasks do NOT have a
    matching pkg.list group — they're pool-only.

    Both sets are pinned here so accidental task additions still
    surface as a test failure, but deliberate additions are
    explicitly recorded.  asgard-games was drafted in the initial
    8-task push but removed 2026-06-02 — operator decision not to
    ship a games / BitTorrent subcategory."""
    _tasks_dir = os.path.join(_ROOT, 'fork', 'source', 'athena-tasksel',
                              'tasks')
    files = {f for f in os.listdir(_tasks_dir)
             if os.path.isfile(os.path.join(_tasks_dir, f))
             and not f.startswith('.')
             and f != 'README'}
    expected = {
        # FORK-01 Step 5b — pkg.list-group mirror tasks
        'standard', 'ssh-server', 'laptop', 'desktop',
        'gnome-desktop', 'development-tools',
        # 2026-06-02 — operator-facing subcategory tasks; Key list
        # points at the asgard-<group> metapackage; metapackage
        # Depends pulls asgard-gnome-desktop transitively
        'asgard-office', 'asgard-pim', 'asgard-multimedia',
        'asgard-gnome-extras', 'asgard-accessibility',
        'asgard-network-services', 'asgard-printing-extras',
    }
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



def test_iso_installer_uses_spinner_for_initrd_and_grub_mkrescue():
    """Installer ISO build wraps the cpio|gzip initrd pack and the
    container grub-mkrescue in Spinners.  Both take 30-60s each
    silently on a typical installer chroot."""
    _path = os.path.join(_ROOT, 'scripts', 'iso_installer.py')
    with open(_path) as fh:
        _body = fh.read()
    import re
    _initrd = re.search(
        r"tui\.Spinner\([^)]*initrd[^)]*\).*?sudo_askpass_env",
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



def test_diag_audit_stanza_empty_value_only_required_fields():
    """Regression (audit #116): EMPTY-VALUE must flag only fields that REQUIRE a
    value (Package/Version/Description), not every empty field — an optional
    field a stanza legitimately leaves blank shouldn't be a finding."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import diag_installer_status as _d
    # empty OPTIONAL field → no EMPTY-VALUE finding
    _iss = _d.audit_stanza(
        {'Package': 'foo', 'Version': '1', 'Description': 'x', 'Homepage': ''},
        ['Package: foo'])
    assert not any('EMPTY-VALUE' in _i for _i in _iss), _iss
    # empty REQUIRED field → flagged
    _iss2 = _d.audit_stanza(
        {'Package': '', 'Version': '1', 'Description': 'x'}, ['Package:'])
    assert any('EMPTY-VALUE' in _i and 'Package' in _i for _i in _iss2), _iss2



def test_tasksel_sanitize_drops_control_chars():
    """Regression (audit #184): _sanitize must banish non-printable ASCII
    (tab/newline/NUL/control, ord < 32), not just ord > 126, before cdebconf
    renders it."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tasksel_desc
    _out = tasksel_desc._sanitize('a\tb\x07c\nd')
    assert '\t' not in _out and '\x07' not in _out and '\n' not in _out, _out
    assert _out == 'a b c d', _out



def test_diag_installer_status_reads_latin1():
    """Regression (audit #114): the status file must be read latin-1 (1:1
    byte->codepoint) so the non-ASCII scan reports the true byte value, not
    U+FFFD from a utf-8/errors='replace' read."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import diag_installer_status
    assert "encoding='latin-1'" in inspect.getsource(diag_installer_status), (
        "status file read must decode latin-1 for true byte values")



def test_diag_installer_status_reports_folded_line():
    """Regression (audit #117): the non-ASCII position must be interpretable —
    report the folded line number, since the offset is into the continuation-
    joined value, not a source column."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import diag_installer_status
    assert 'folded line' in inspect.getsource(diag_installer_status), (
        "non-ASCII diagnostic must report the folded line, not a bare offset")



# ─────────────────────────────────────────────────────────────────────────────
# CONF-10 / AUDIT-01 — identity-residue scanner
# ─────────────────────────────────────────────────────────────────────────────

def test_identity_scan_finds_debian_token_in_template():
    """audit_identity finds a Debian-prose hit in a debconf templates body."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from identity_scan import audit_identity
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, 'fork-pkg', 'debian'))
        with open(os.path.join(td, 'fork-pkg', 'debian',
                               'fork-pkg.templates'), 'w') as fh:
            fh.write(
                'Template: example/some\nType: text\n'
                '_Description: Use the Debian-style mirror?\n'
            )
        findings = audit_identity(td)
    assert len(findings) == 1, findings
    assert findings[0]['token'] == 'Debian'
    assert 'fork-pkg' in findings[0]['path']



def test_identity_scan_allowlist_absorbs_finding():
    """A matching allowlist entry suppresses the finding."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from identity_scan import audit_identity
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, 'p', 'debian'))
        with open(os.path.join(td, 'p', 'debian', 'changelog'), 'w') as fh:
            fh.write('p (1.0) thor; urgency=low\n  * Forked from Debian.\n')
        _allow = os.path.join(td, 'identity-allowlist')
        with open(_allow, 'w') as fh:
            fh.write('*/debian/changelog\t*\tlegal retention\n')
        findings = audit_identity(td, _allow)
    assert findings == [], findings



def test_identity_scan_skips_binary_globs():
    """Binary file types (.deb, .png, etc) are not greped."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from identity_scan import audit_identity
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, 'pkg.deb'), 'w') as fh:
            fh.write('Debian inside binary archive\n')
        with open(os.path.join(td, 'translations.po'), 'w') as fh:
            fh.write('msgstr "Debian"\n')
        findings = audit_identity(td)
    assert findings == [], (
        f'binary / .po file should be skipped; got {findings}'
    )



def test_identity_scan_skips_extensionless_binary_via_nul_probe():
    """Files without a known binary extension (e.g. `boot/vmlinuz`) are
    skipped via NUL-byte detection.  Caught 2026-05-31 staged ISO
    audit: the kernel image's embedded build-id strings
    (`debian-kernel@lists.debian.org`, `Debian Secure Boot CA`) were
    grep'd as identity leakage because `vmlinuz` has no extension."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from identity_scan import audit_identity
    with tempfile.TemporaryDirectory() as td:
        # Simulate a kernel binary: NUL bytes interspersed with
        # printable text that the audit's regex would otherwise match.
        os.makedirs(os.path.join(td, 'boot'))
        with open(os.path.join(td, 'boot', 'vmlinuz'), 'wb') as fh:
            fh.write(b'GCC: (Debian 12.2.0-14) 12.2.0\x00\x00\x00')
            fh.write(b'debian-kernel@lists.debian.org\x00')
            fh.write(b'Debian Secure Boot CA\x00\xff\xfe\x00')
        # And a sibling text file the audit MUST still scan.
        with open(os.path.join(td, 'boot', 'config.txt'), 'w') as fh:
            fh.write('reference to Debian package\n')
        findings = audit_identity(td)
    # vmlinuz skipped → only the text file's hit remains.
    _paths = sorted(f['path'] for f in findings)
    assert _paths == [os.path.join('boot', 'config.txt')], (
        f"vmlinuz should be skipped via NUL probe; got {_paths}"
    )



def test_identity_scan_word_boundary_excludes_discoverable():
    """The `discover` token uses \\b — `discoverable` in license text
    should NOT match."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from identity_scan import audit_identity
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, 'cc0.txt'), 'w') as fh:
            fh.write('the present or absence of errors, '
                     'whether or not discoverable, all to\n')
        findings = audit_identity(td)
    # No `\bdiscover\b` match; also no Debian/debian.org.
    assert findings == [], findings



def test_identity_scan_allowlist_wildcard_token():
    """Allowlist '*' for token-name absorbs every token at the path."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from identity_scan import audit_identity
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, 'multi.txt'), 'w') as fh:
            fh.write(
                'See https://bugs.debian.org/x for details.\n'
                'Original by Debian Project.\n'
                'Uses popularity-contest data.\n'
            )
        _allow = os.path.join(td, 'allow')
        with open(_allow, 'w') as fh:
            fh.write('multi.txt\t*\tfixture\n')
        findings = audit_identity(td, _allow)
    assert findings == [], findings



def test_identity_scan_specific_token_does_not_absorb_others():
    """An allowlist entry naming `debian.org` lets that token through
    but still surfaces `Debian` hits on the same file."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from identity_scan import audit_identity
    with tempfile.TemporaryDirectory() as td_root:
        # Allowlist OUTSIDE the scan root — otherwise the scanner would
        # walk the allowlist file itself and surface its own "debian.org"
        # token reference.
        td = os.path.join(td_root, 'scan')
        os.makedirs(td)
        with open(os.path.join(td, 'mix.txt'), 'w') as fh:
            fh.write(
                'See https://debian.org/path  -- intentional\n'
                'Originally Debian-derived.            -- leakage\n'
            )
        _allow = os.path.join(td_root, 'allow')
        with open(_allow, 'w') as fh:
            fh.write('mix.txt\tdebian.org\tdoc URL OK\n')
        findings = audit_identity(td, _allow)
    assert len(findings) == 1, findings
    assert findings[0]['token'] == 'Debian'



def test_audit_staged_iso_passes_clean_tree():
    """A staged tree with no Debian tokens passes (returns True)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from iso_installer import _audit_staged_iso
    with tempfile.TemporaryDirectory() as td:
        _staging = os.path.join(td, 'buildroot', 'image', 'staging')
        _dir_image = os.path.dirname(_staging)
        os.makedirs(_staging)
        os.makedirs(os.path.join(td, 'audit'))
        with open(os.path.join(td, 'audit', 'identity-allowlist'), 'w') as fh:
            fh.write('# empty allowlist\n')
        # Plant clean content (Asgard, not Debian).
        os.makedirs(os.path.join(_staging, '.disk'))
        with open(os.path.join(_staging, '.disk', 'info'), 'w') as fh:
            fh.write('Asgard 0.1 "thor" - amd64 INSTALLER\n')
        assert _audit_staged_iso(_staging, _dir_image) is True



def test_audit_staged_iso_fails_on_debian_leak():
    """An unsubstituted Debian token in staged content aborts the build."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from iso_installer import _audit_staged_iso
    with tempfile.TemporaryDirectory() as td:
        _staging = os.path.join(td, 'buildroot', 'image', 'staging')
        _dir_image = os.path.dirname(_staging)
        os.makedirs(_staging)
        os.makedirs(os.path.join(td, 'audit'))
        with open(os.path.join(td, 'audit', 'identity-allowlist'), 'w') as fh:
            fh.write('# empty — every token is a violation\n')
        os.makedirs(os.path.join(_staging, '.disk'))
        with open(os.path.join(_staging, '.disk', 'info'), 'w') as fh:
            fh.write('Debian 12.4 "bookworm" - amd64 INSTALLER\n')
        assert _audit_staged_iso(_staging, _dir_image) is False



def test_audit_staged_iso_skips_when_no_allowlist():
    """No allowlist anywhere up the tree → audit logs a warning and
    returns True (degraded — doesn't break iso build for test fixtures
    that haven't laid down the audit/ dir)."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from iso_installer import _audit_staged_iso
    with tempfile.TemporaryDirectory() as td:
        _staging = os.path.join(td, 'staging'); os.makedirs(_staging)
        with open(os.path.join(_staging, 'info'), 'w') as fh:
            fh.write('Debian leak that would normally trip the gate\n')
        assert _audit_staged_iso(_staging, td) is True



def test_audit_staged_iso_skips_binary_pool():
    """Pool .deb files in the staging tree are skipped by the scanner's
    binary-glob filter — wouldn't trip on the Debian-name strings
    inside a binary archive."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    from iso_installer import _audit_staged_iso
    with tempfile.TemporaryDirectory() as td:
        _staging = os.path.join(td, 'buildroot', 'image', 'staging')
        _dir_image = os.path.dirname(_staging)
        os.makedirs(_staging)
        os.makedirs(os.path.join(td, 'audit'))
        with open(os.path.join(td, 'audit', 'identity-allowlist'), 'w') as fh:
            fh.write('# empty\n')
        # Plant a fake .deb in pool/
        _pool = os.path.join(_staging, 'pool', 'main', 'b')
        os.makedirs(_pool)
        with open(os.path.join(_pool, 'base-files_12.4_amd64.deb'), 'wb') as fh:
            fh.write(b'!<arch>\nDebian inside binary archive bytes\n')
        # And a clean .disk/info
        os.makedirs(os.path.join(_staging, '.disk'))
        with open(os.path.join(_staging, '.disk', 'info'), 'w') as fh:
            fh.write('Asgard 0.1 (thor)\n')
        assert _audit_staged_iso(_staging, _dir_image) is True



def test_iso_installer_skips_staged_iso_audit_when_disabled():
    """build_installer_iso threads audit_identity_scan through to
    _audit_staged_iso; when False, the audit is skipped + a WARN
    logged."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import iso_installer
    _src = inspect.getsource(iso_installer.build_installer_iso)
    assert 'audit_identity_scan' in _src, (
        'build_installer_iso must accept the audit_identity_scan kwarg'
    )
    assert 'if audit_identity_scan' in _src, (
        'audit gate must be conditional on the flag'
    )



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



def test_installer_smoke_workflow_contract():
    """CI-01 stage 2: the installer-smoke workflow exists and wires the
    harness — scheduled + manual triggers, resolves the ISO from the
    mirror's releases.json (or a dispatch input), and gates on
    `run.py --quick` for both BIOS and EFI.  String pins (no PyYAML dep —
    the CI test job doesn't install it)."""
    _wf_path = os.path.join(
        _ROOT, '.github', 'workflows', 'installer-smoke.yml')
    assert os.path.isfile(_wf_path), "installer-smoke.yml missing"
    with open(_wf_path) as _fh:
        _wf = _fh.read()
    # triggers: nightly schedule + manual dispatch
    assert 'schedule:' in _wf and 'cron:' in _wf, _wf
    assert 'workflow_dispatch:' in _wf, _wf
    # ISO source: the mirror's static manifest, or a dispatch URL
    assert 'releases.json' in _wf, _wf
    assert 'ASGARD_MIRROR_URL' in _wf and 'iso_url' in _wf, _wf
    # sha-verify the download
    assert 'sha256sum -c' in _wf, _wf
    # runs the harness, quick mode, both firmware modes
    assert 'tests/installer_smoke/run.py' in _wf, _wf
    assert '--quick' in _wf, _wf
    assert '--mode bios' in _wf and '--mode efi' in _wf, _wf
    # no continue-on-error escape hatch on the gate (the run.py exit code
    # must fail the job)
    assert 'continue-on-error' not in _wf, _wf



def test_tasksel_desc_generator_shape_and_sanitization():
    """SURFACES-01 Chunk 6: makedesc-shaped stanzas; cdebconf-fragile
    characters (non-ASCII, commas, parens, em-dashes) sanitized; [base]
    skipped; Keys == the group seed lists."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tasksel_desc as _td
    _groups = {
        'base': ['a'],                       # skipped — debootstrapped
        'gnome-desktop': ['gnome-shell', 'gdm3'],
        'ssh-server': ['openssh-server'],
        'empty-group': [],                   # skipped — no seeds
    }
    _meta = {'gnome-desktop': {
        'description': 'GNOME désktop — env, (shell, apps)'}}
    _out = _td.generate_desc(_groups, _meta)
    assert 'Task: base' not in _out and 'empty-group' not in _out
    assert 'Task: gnome-desktop' in _out and 'Task: ssh-server' in _out
    # stanza shape
    _stanza = _out[_out.index('Task: gnome-desktop'):]
    _stanza = _stanza.split('\n\n')[0]
    assert 'Section: user' in _stanza
    assert 'Key: ' in _stanza
    assert '\n gnome-shell' in _stanza and '\n gdm3' in _stanza
    # sanitization: ASCII-only, no commas/parens/em-dash survive
    _desc_line = [l for l in _stanza.split('\n')
                  if l.startswith('Description:')][0]
    assert _desc_line.isascii(), _desc_line
    for _bad in (',', '(', ')', '—'):
        assert _bad not in _desc_line, _desc_line
    assert 'GNOME d sktop' in _desc_line  # é dropped, words preserved
    # fallback title when no meta
    assert 'Description: Ssh Server' in _out
    # whole output is ASCII (encodable for the staging write)
    _out.encode('ascii')



def test_athena_pkgsel_pre_pkgsel_hook_installs_generated_desc():
    """SURFACES-01 Chunk 7: the 05athena-tasks pre-pkgsel.d hook exists,
    is executable, runs BEFORE the existing hooks (05 < 10), copies the
    ISO-staged desc onto /target, and the dh install file ships the dir
    under the binary package name."""
    _fork = os.path.join(_ROOT, 'fork', 'source', 'athena-pkgsel')
    _hook = os.path.join(_fork, 'pre-pkgsel.d', '05athena-tasks')
    assert os.path.isfile(_hook), _hook
    assert os.access(_hook, os.X_OK), "hook must be executable"
    _src = open(_hook).read()
    assert _src.startswith('#!/bin/sh')
    assert '/cdrom/.disk/athena-tasks.desc' in _src      # ISO staging path
    assert '/target/usr/share/tasksel/descs/athena-tasks.desc' in _src
    assert '[ -e /cdrom/.disk/athena-tasks.desc ]' in _src  # fallback-safe
    # dh install file uses the BINARY package name (dh-helper-files memory)
    _install = os.path.join(_fork, 'debian', 'athena-pkgsel.install')
    assert os.path.isfile(_install)
    assert 'pre-pkgsel.d usr/lib' in open(_install).read()
    # ordering: 05 sorts before the existing 10laptop-detect
    _hooks = sorted(os.listdir(os.path.join(_fork, 'pre-pkgsel.d')))
    assert _hooks[0] == '05athena-tasks', _hooks



def test_tasksel_desc_meta_none_and_empty_seeds():
    """Audit #185: a group whose meta value is None (or is absent from meta)
    falls back to the title without raising; a group with empty seeds is
    skipped entirely."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tasksel_desc
    _out = tasksel_desc.generate_desc(
        {'gnome-desktop': ['gnome-core'], 'empty-grp': []},
        {'gnome-desktop': None})          # None meta → title fallback, no raise
    assert 'Task: gnome-desktop' in _out
    assert 'Description: Gnome Desktop' in _out   # _title fallback
    assert 'empty-grp' not in _out               # empty seeds skipped
    # meta entirely None is tolerated too
    _out2 = tasksel_desc.generate_desc({'web': ['nginx']}, None)
    assert 'Task: web' in _out2 and 'Description: Web' in _out2



def test_diag_parse_stanzas_covers_all_branches():
    """Audit #118: parse_stanzas handles blank-line separation, folded
    continuation, a continuation with no preceding field (malformed), a bad
    non-header line (malformed), and a trailing stanza with no final newline."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import diag_installer_status as _d
    _content = (
        "Package: foo\n"
        "Description: a\n"
        " continued\n"
        "\n"
        "Package: bar\n"
        "Status: install ok installed")   # trailing stanza, no final newline
    _st = _d.parse_stanzas(_content)
    assert len(_st) == 2, _st
    assert _st[0][1]['Package'] == 'foo'
    assert _st[0][1]['Description'] == 'a\n continued'
    assert _st[1][1]['Package'] == 'bar'
    assert '__MALFORMED__' not in _st[1][1]
    # continuation with no preceding field → malformed
    _bad = _d.parse_stanzas(" orphan\nPackage: x")
    assert '__MALFORMED__' in _bad[0][1]
    # a non-header, non-continuation line → malformed
    _bad2 = _d.parse_stanzas("Package: x\nnot-a-header-line")
    assert '__MALFORMED__' in _bad2[0][1]



def test_select_pool_files_excludes_on_legacy_whitelist_none_path():
    """Audit #135: exclude_names applies on the legacy deb_whitelist=None
    blanket-copy path too — a superseded udeb (apt-setup-udeb) is dropped,
    siblings kept."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import iso_installer
    with tempfile.TemporaryDirectory() as _d:
        for _f in ('apt-setup-udeb_1.0_all.udeb',
                   'anna_1.0_amd64.udeb',
                   'busybox-udeb_1.0_amd64.udeb'):
            with open(os.path.join(_d, _f), 'w') as _fh:
                _fh.write('x')
        _kept, _skipped = iso_installer._select_pool_files(
            [_d], deb_whitelist=None, exclude_names={'apt-setup-udeb'})
        _names = {_n for _, _n in _kept}
        assert 'apt-setup-udeb_1.0_all.udeb' not in _names
        assert 'anna_1.0_amd64.udeb' in _names
        assert 'busybox-udeb_1.0_amd64.udeb' in _names

TESTS = [
    test_arch17_top_offender_modules_fully_annotated,
    test_installer_chroot_dpkg_unpack_carries_required_force_flags,
    test_iso_installer_kernel_pkg_regex_matches_real_kernels_only,
    test_iso_installer_stage_grub_cfg_errors_when_data_layer_missing,
    test_iso_installer_stage_grub_cfg_copies_when_present,
    test_iso_installer_stage_disk_info_errors_when_dir_missing,
    test_iso_installer_stage_disk_info_substitutes_codename_and_version,
    test_iso_installer_stage_disk_info_safe_substitute_leaves_unknown_vars,
    test_iso_installer_stage_disk_info_errors_when_only_readme,
    test_iso_installer_stage_base_include_writes_one_name_per_line,
    test_iso_installer_stage_base_include_creates_disk_dir_if_missing,
    test_iso_installer_stage_base_include_noop_on_empty_or_none,
    test_iso_installer_parse_deb_filename_handles_normal_filenames,
    test_iso_installer_select_pool_files_includes_udebs_unconditionally,
    test_iso_installer_select_pool_files_excludes_superseded,
    test_iso_installer_select_pool_files_drops_dbgsym_unconditionally,
    test_iso_installer_select_pool_files_filters_by_whitelist,
    test_iso_installer_select_pool_files_keeps_highest_version_per_name,
    test_iso_installer_select_pool_files_uses_debian_version_order,
    test_iso_installer_base_include_and_pool_filter_agree,
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
    test_athena_installer_data_ships_runtime_dirs,
    test_athena_installer_data_ships_release_files_with_tokens,
    test_installer_chroot_sudo_write_does_not_leak_password_to_tee,
    test_installer_chroot_run_depmod_skips_when_no_modules_dir,
    test_installer_chroot_run_depmod_indexes_each_kernel_present,
    test_installer_chroot_generator_guard_passes_when_both_present,
    test_installer_chroot_generator_guard_fails_without_cdrom,
    test_installer_chroot_generator_guard_fails_without_mirror,
    test_athena_cdrom_setup_does_not_provide_mirror_setup,
    test_audit_chroot_hooks_strip_allowlisted,
    test_audit_chroot_hooks_no_op_when_no_hooks,
    test_audit_chroot_hooks_called_in_build_flow,
    test_audit_chroot_hooks_fail_on_hard_unpooled,
    test_audit_chroot_hooks_warn_on_soft_unpooled,
    test_audit_chroot_hooks_skips_when_no_pool,
    test_parse_apt_install_line_variants,
    test_installer_chroot_register_self_appends_debian_installer_stanza,
    test_installer_chroot_register_self_idempotent_on_repeat,
    test_installer_grub_cfg_has_preseed_kernel_cmdline,
    test_find_kernel_prefers_expected_kernel_pkg_match,
    test_find_kernel_falls_back_to_highest_when_no_match,
    test_athena_tasksel_fork_ignores_debian_tasks_only_env,
    test_athena_tasksel_control_provides_conflicts_replaces_tasksel,
    test_athena_tasksel_depends_on_athena_tasksel_data_directly,
    test_athena_tasksel_task_keys_mirror_pkg_list_groups,
    test_athena_tasksel_no_test_new_install_skip,
    test_athena_pkgsel_no_popcon_pre_pkgsel_hook,
    test_athena_pkgsel_fork_postinst_drops_debian_tasks_only_prefix,
    test_athena_pkgsel_control_provides_conflicts_replaces_pkgsel,
    test_athena_pkgsel_dh_helper_files_use_binary_name,
    test_pkgsel_patch_dir_deleted,
    test_stage_group_manifests_writes_one_file_per_group,
    test_stage_group_manifests_empty_groups_is_noop,
    test_athena_tasksel_data_binary_stanza_in_fork_control,
    test_athena_tasksel_fork_ships_curated_task_set,
    test_athena_tasksel_standard_task_uses_curated_key_list,
    test_iso_installer_synthetic_tasksel_data_retired,
    test_installer_list_includes_athena_pkgsel,
    test_overlay_map_does_not_contain_pre_pkgsel_hook,
    test_installer_pkgsel_dir_does_not_exist,
    test_identity_scan_finds_debian_token_in_template,
    test_identity_scan_allowlist_absorbs_finding,
    test_identity_scan_skips_binary_globs,
    test_identity_scan_skips_extensionless_binary_via_nul_probe,
    test_identity_scan_word_boundary_excludes_discoverable,
    test_identity_scan_allowlist_wildcard_token,
    test_identity_scan_specific_token_does_not_absorb_others,
    test_iso_installer_skips_staged_iso_audit_when_disabled,
    test_audit_staged_iso_passes_clean_tree,
    test_audit_staged_iso_fails_on_debian_leak,
    test_audit_staged_iso_skips_when_no_allowlist,
    test_audit_staged_iso_skips_binary_pool,
    test_tasksel_desc_generator_shape_and_sanitization,
    test_athena_pkgsel_pre_pkgsel_hook_installs_generated_desc,
    test_stage_disk_info_writes_snapshot_marker_and_substitutes,
    test_installer_smoke_workflow_contract,
    test_iso_installer_stage_grub_cfg_copies_background_when_present,
    test_iso_installer_stage_grub_cfg_tolerates_missing_background,
    test_installer_grub_cfg_wires_background_image,
    test_installer_smoke_scan_log_returns_empty_on_clean_log,
    test_installer_smoke_scan_log_catches_each_fatal_pattern,
    test_installer_smoke_scan_log_distinguishes_warn_from_fatal,
    test_installer_smoke_scan_log_handles_missing_file,
    test_installer_smoke_known_bad_extends_with_extra_patterns,
    test_installer_smoke_run_module_has_required_modes,
    test_athena_installer_data_ships_templates_override,
    test_athena_installer_data_ships_value_overrides,
    test_athena_installer_data_branding_hook_applies_both_mechanisms,
    test_athena_installer_data_no_broken_palette_mechanism,
    test_athena_installer_data_no_branding_patches_in_repo,
    test_athena_branding_ships_target_grub_background,
    test_iso_installer_run_grub_mkrescue_routes_through_container,
    test_iso_installer_uses_spinner_for_initrd_and_grub_mkrescue,
    test_diag_audit_stanza_empty_value_only_required_fields,
    test_tasksel_sanitize_drops_control_chars,
    test_diag_installer_status_reads_latin1,
    test_diag_installer_status_reports_folded_line,
    test_tasksel_desc_meta_none_and_empty_seeds,
    test_diag_parse_stanzas_covers_all_branches,
    test_select_pool_files_excludes_on_legacy_whitelist_none_path,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
