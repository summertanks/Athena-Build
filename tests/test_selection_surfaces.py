"""Athena tests — package selection + surfaces (selection_lock.py, select_packages.py, surfaces.py, arch_filter.py, build_closure.py, dep_drift.py).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile

from _test_helpers import (  # noqa: F401
    _ROOT,
    _SurfPkg,
    _select_controller,
    _selection_lock_cfg,
    _sl_tree,
    _surf_tree,
)





def test_select_latest_kernel_version_aware_and_paired():
    """STA-31: kernel selection is version-aware (apt_pkg ABI compare),
    NOT lexicographic, and the initrd is paired to the chosen kernel by
    its exact suffix.  A plain sorted()[-1] picks 6.1.0-9 over 6.1.0-47
    ('9' > '4') and 6.9 over 6.10 — both wrong."""
    import tempfile
    import utils

    # latest_kernel_name: the two classic lexicographic traps
    assert utils.latest_kernel_name([
        'vmlinuz-6.1.0-9-amd64', 'vmlinuz-6.1.0-47-amd64',
    ]) == 'vmlinuz-6.1.0-47-amd64'
    assert utils.latest_kernel_name([
        'vmlinuz-6.9.0-1-amd64', 'vmlinuz-6.10.0-1-amd64',
    ]) == 'vmlinuz-6.10.0-1-amd64'
    # works on linux-image .deb filenames (ABI, not the deb-version)
    assert utils.latest_kernel_name([
        'linux-image-6.1.0-9-amd64_6.1.174-1_amd64.deb',
        'linux-image-6.1.0-47-amd64_6.1.174-1_amd64.deb',
    ]).startswith('linux-image-6.1.0-47-amd64')
    assert utils.kernel_abi_of(
        'linux-image-6.1.0-47-amd64_6.1.174-1+asg1u2_amd64.deb'
    ) == '6.1.0-47-amd64'
    assert utils.latest_kernel_name([]) is None

    with tempfile.TemporaryDirectory() as _d:
        _boot = os.path.join(_d, 'boot')
        os.makedirs(_boot)
        for _f in ('vmlinuz-6.1.0-9-amd64', 'vmlinuz-6.1.0-47-amd64',
                   'initrd.img-6.1.0-9-amd64', 'initrd.img-6.1.0-47-amd64'):
            open(os.path.join(_boot, _f), 'w').close()
        # highest ABI chosen, initrd PAIRED to it (not an independent sort)
        assert utils.select_latest_kernel(_boot) == (
            'vmlinuz-6.1.0-47-amd64', 'initrd.img-6.1.0-47-amd64')
        # remove the matching initrd → hard failure (None), never a mismatch
        os.remove(os.path.join(_boot, 'initrd.img-6.1.0-47-amd64'))
        assert utils.select_latest_kernel(_boot) is None
        # no vmlinuz at all → None
        _empty = os.path.join(_d, 'empty')
        os.makedirs(_empty)
        assert utils.select_latest_kernel(_empty) is None



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



def test_d5_arch_qualifier_semantics_and_reader_views():
    """COMP-04 D5: [arch] seed qualifiers.  dpkg restriction semantics
    (positive any-match, all-negated none-match, mixing forbidden,
    wildcards via dpkg's arch table); parse_pkg_list_groups returns the
    VERBATIM view at arch=None (editor round-trip) and the
    filtered+stripped view for a concrete arch; flat readers filter the
    same way; a seed line's [arch] suffix never parses as a section
    header."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import arch_profile as ap
    import utils
    assert ap.entry_applies('a [amd64]', 'amd64') == (True, 'a')
    assert ap.entry_applies('a [amd64]', 'i386') == (False, 'a')
    assert ap.entry_applies('b [amd64 i386]', 'i386') == (True, 'b')
    assert ap.entry_applies('c', 'arm64') == (True, 'c')
    assert ap.entry_applies('d [!arm64]', 'arm64') == (False, 'd')
    assert ap.entry_applies('d [!arm64]', 'i386') == (True, 'd')
    assert ap.entry_applies('e [linux-any]', 'arm64') == (True, 'e')
    for _bad in ('x [amd64 !i386]', 'x []'):
        try:
            ap.split_qualifier(_bad)
            raise AssertionError(f'{_bad!r} must raise')
        except ValueError:
            pass
    _text = (
        '[base]\n'
        'linux-image-amd64 [amd64]\n'
        'linux-image-686-pae [i386]\n'
        'shared-entry\n'
        '[desktop]\n'
        'grub-efi-arm64-bin [arm64]\n')
    _lines = _text.splitlines()
    _raw = utils.parse_pkg_list_groups('t.list', lines=_lines)
    assert _raw['base'] == ['linux-image-amd64 [amd64]',
                            'linux-image-686-pae [i386]', 'shared-entry'], \
        'arch=None must be the verbatim editor view'
    _amd = utils.parse_pkg_list_groups('t.list', lines=_lines, arch='amd64')
    assert _amd['base'] == ['linux-image-amd64', 'shared-entry']
    assert _amd['desktop'] == []
    _i386 = utils.parse_pkg_list_groups('t.list', lines=_lines, arch='i386')
    assert _i386['base'] == ['linux-image-686-pae', 'shared-entry']
    _arm = utils.parse_pkg_list_groups('t.list', lines=_lines, arch='arm64')
    assert _arm['desktop'] == ['grub-efi-arm64-bin']
    # flat readers: surfaces.read_flat_roots + selection_lock._read_flat_seeds
    import surfaces
    import selection_lock
    with tempfile.TemporaryDirectory() as _td:
        _p = os.path.join(_td, 'flat.list')
        with open(_p, 'w') as _fh:
            _fh.write('# comment\nplain\nx-only [amd64 i386]\narm-only [arm64]\n')
        assert surfaces.read_flat_roots(_p, arch='arm64') == \
            ['plain', 'arm-only']
        assert surfaces.read_flat_roots(_p, arch='amd64') == \
            ['plain', 'x-only']
        assert selection_lock._read_flat_seeds(_p, 'i386') == \
            ['plain', 'x-only']
        # arch=None keeps verbatim (round-trip callers)
        assert selection_lock._read_flat_seeds(_p) == \
            ['plain', 'x-only [amd64 i386]', 'arm-only [arm64]']


def test_d5_shipped_lists_amd64_view_and_wiring():
    """The shipped config lists carry qualifiers now: the amd64 view
    must still include its kernel/grub entries and EXCLUDE the foreign
    seeds; the Tunneled parser and the closure-feeding call sites must
    route through the arch filter (source pins)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    import arch_profile as ap
    _pkg = os.path.join(_ROOT, 'config', 'pkg.list')
    _amd = utils.parse_pkg_list_groups(_pkg, arch='amd64')['base']
    _i386 = utils.parse_pkg_list_groups(_pkg, arch='i386')['base']
    _arm = utils.parse_pkg_list_groups(_pkg, arch='arm64')['base']
    assert 'linux-image-amd64' in _amd and 'grub-pc-bin' in _amd
    assert 'linux-image-686-pae' not in _amd
    assert 'linux-image-686-pae' in _i386 and 'grub-efi-ia32-bin' in _i386
    assert 'linux-image-arm64' in _arm and 'grub-pc-bin' not in _arm
    # Tunneled: build.conf entries are qualified; amd64 view drops the
    # foreign signing chains
    _tun = [t.strip() for t in open(
        os.path.join(_ROOT, 'config', 'build.conf')).read().split(
        'Tunneled =', 1)[1].splitlines()[0].split(',')]
    _amd_tun = ap.filter_seed_lines(_tun, 'amd64')
    assert 'fwupd-amd64-signed' in _amd_tun
    assert 'fwupd-i386-signed' not in _amd_tun
    assert 'shim-signed' in _amd_tun
    assert ap.filter_seed_lines(_tun, 'arm64') == \
        ['firmware-nonfree', 'fwupd-arm64-signed', 'ffmpegthumbnailer']
    # wiring pins
    with open(os.path.join(_ROOT, 'scripts', 'utils.py')) as _fh:
        _u = _fh.read()
    assert 'filter_seed_lines(' in _u, 'Tunneled parser must arch-filter'
    with open(os.path.join(_ROOT, 'scripts', 'commands',
                           'cmd_build.py')) as _fh:
        _cb = _fh.read()
    assert _cb.count('arch=self.config.arch') >= 7, \
        'cmd_build closure-seed readers must pass arch'
    with open(os.path.join(_ROOT, 'scripts', 'selection_lock.py')) as _fh:
        _sl = _fh.read()
    assert "arch=_arch" in _sl and 'seeds_raw' in _sl
    assert 'name-render fallback' in _sl, \
        'restore fallback must warn (qualifiers not reconstructible)'
    with open(os.path.join(_ROOT, 'scripts', 'select_packages.py')) as _fh:
        _sp = _fh.read()
    assert 'arch=None DELIBERATELY' in _sp, \
        'editor must stay on the verbatim view'


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



def test_sta24_dep_target_names_extracts_all_fields_names_only():
    """STA-24: _dep_target_names returns the dep-target NAMES across all
    four fields, ignoring version constraints (so an NMU-strip rewriting a
    constraint version is NOT seen as a removal)."""
    import dep_drift

    class _P:
        depends = [('libc6', '2.34', '>='), ('libfoo', '', '')]
        pre_depends = [('dpkg', '', '')]
        alt_depends = [[('a', '', ''), ('b', '1', '=')]]
        alt_pre_depends = [[('init', '', '')]]
    assert dep_drift._dep_target_names(_P()) == {
        'libc6', 'libfoo', 'dpkg', 'a', 'b', 'init'}
    # version-only change → same name set (no false "removal")
    class _Q:
        depends = [('libc6', '2.36', '>=')]   # constraint bumped
        pre_depends = []
        alt_depends = []
        alt_pre_depends = []
    class _R:
        depends = [('libc6', '', '')]          # constraint stripped
        pre_depends = []
        alt_depends = []
        alt_pre_depends = []
    assert (dep_drift._dep_target_names(_Q())
            == dep_drift._dep_target_names(_R()) == {'libc6'})



def test_sta24_check_dep_drift_warns_on_lost_selected_dep():
    """STA-24: a built .deb that DROPPED a dep on a SELECTED package (vs
    the upstream cache record) must raise a prominent WARNING — the
    e2fsprogs/libext2fs2 class that the minimal closure silently follows
    into a boot loop.  A constraint-version-only drift must NOT warn."""
    import sys as _sys, subprocess as _subprocess
    from unittest.mock import patch
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dep_drift, utils, tui

    class _CachePkg:
        def __init__(self, depends):
            self._fields = {
                'Package': 'e2fsprogs', 'Version': '1.47.0-2',
                'Filename': 'pool/e2fsprogs_1.47.0-2_amd64.deb'}
            self.version = '1.47.0-2'
            self.pre_depends = []
            self.alt_pre_depends = []
            self.alt_depends = []
            self.depends = list(depends)

        def __getitem__(self, k): return self._fields[k]
        def __setitem__(self, k, v): self._fields[k] = v
        def get(self, k, d=''): return self._fields.get(k, d)

    class _DT:
        def __init__(self, _cache):
            self.canonical_pkgs = {'e2fsprogs': _cache}
            # libgcc-s1 is the real package that `Provides: libgcc1` — the
            # generic signal STA-24 uses to clear the transitional rename.
            class _Prov:
                def __init__(self, provides):
                    self.provides = provides
            self.selected_pkgs = {
                'e2fsprogs': _cache,
                'libext2fs2': object(), 'libc6': object(),
                'libgcc1': object(),
                'libgcc-s1': _Prov([[('libgcc1', '', '')]]),
                'libunwind8': object()}
            self.extras_pkg_names: set = set()

    def _run(_cache, _disk_depends_line):
        _disk_ctrl = (f"Package: e2fsprogs\nVersion: 1.47.0-2\n"
                      f"Architecture: amd64\n{_disk_depends_line}")
        _proc = _subprocess.CompletedProcess([], 0, _disk_ctrl, '')

        class _Mixin(dep_drift._DepDriftMixin):
            def __init__(self):
                self._dependencytree = _DT(_cache)
                self._dir_repo_main = '/repo/main'
                self.normalize_repo_filename = lambda f: f

            def _verify_dep_resolution(self):  # not under test here
                pass

        _printed: 'list[str]' = []
        with patch.object(utils, 'find_matching_artifact',
                          return_value='/repo/main/e2fsprogs.deb'), \
             patch.object(dep_drift.subprocess, 'run', return_value=_proc), \
             patch.object(tui.console, 'print',
                          side_effect=lambda *a, **k: _printed.append(
                              ' '.join(str(x) for x in a))):
            _Mixin()._check_dep_drift()
        return '\n'.join(_printed)

    # LOST a selected dep (libext2fs2) → WARN
    _cache = _CachePkg([('libext2fs2', '', ''), ('libc6', '', '')])
    _out = _run(_cache, "Depends: libc6")
    assert 'dep-loss' in _out.lower(), _out
    assert 'e2fsprogs' in _out and 'libext2fs2' in _out, _out

    # constraint-version-only change (same names) → NO warn
    _cache2 = _CachePkg([('libext2fs2', '1.47.0-2', '='),
                         ('libc6', '', '')])
    _out2 = _run(_cache2, "Depends: libext2fs2 (>= 1.43.9), libc6")
    assert 'dep-loss' not in _out2.lower(), _out2

    # transitional rename, GENERIC: cache says libgcc1, the built .deb now
    # depends on libgcc-s1 which `Provides: libgcc1` → apt still satisfies
    # the edge → NOT a loss.  No package name is special-cased; suppression
    # falls straight out of the new dep's Provides.
    _cache3 = _CachePkg([('libgcc1', '', ''), ('libc6', '', '')])
    _out3 = _run(_cache3, "Depends: libgcc-s1 (>= 3.0), libc6")
    assert 'dep-loss' not in _out3.lower(), _out3

    # genuinely dropped a selected dep whose name nothing in the new deps
    # Provides (xorg→libunwind8 class) → still WARN; Provides-suppression
    # must not mask it
    _cache4 = _CachePkg([('libunwind8', '', ''), ('libc6', '', '')])
    _out4 = _run(_cache4, "Depends: libc6")
    assert 'dep-loss' in _out4.lower() and 'libunwind8' in _out4, _out4



def test_read_selection_state_distinguishes_transient_io_error():
    """Regression (audit #179): a transient READ error (the file is present but
    unreadable — EIO/EACCES/NFS) must return STATUS_IOERROR, not
    STATUS_MALFORMED.  MALFORMED falsely flags tamper and tells the operator to
    wipe + re-baseline a healthy lockfile.  Both still fail closed."""
    import sys
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
            dir_log = _td
        # Make selection.state a DIRECTORY → open() raises IsADirectoryError
        # (an OSError that is NOT FileNotFoundError).
        os.makedirs(_sl.selection_state_path(_Cfg()))
        _lock, _status = _sl.read_selection_state(_Cfg())
        assert _status == _sl.STATUS_IOERROR, _status
        assert _lock is None
        # classify still treats it as a hard stop (fail closed).
        assert _sl.classify(_status, None, {})[0] == _sl.ACTION_HARDSTOP



def test_select_packages_detects_reserved_pool_group_clash():
    """Regression (audit #176): a real pkg.list [(pool)] section collides with
    the reserved POOL_GROUP; _load_model must detect and warn, not silently
    overwrite (the old comment wrongly claimed it couldn't collide)."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import select_packages
    assert 'if POOL_GROUP in self._groups' in inspect.getsource(
        select_packages), "the reserved-pool-group clash must be detected"



def test_selection_lock_classify_returns_independent_empty_sets():
    """Regression (audit #180): classify's empty added/removed results must be
    independent dicts with independent sets — dict(_empty) shallow-copied and
    SHARED the inner set() objects across every return, so a caller mutating
    one result corrupted later ones."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    _, _add1, _rem1 = _sl.classify(_sl.STATUS_MISSING, {}, {})
    assert _add1['bins'] is not _rem1['bins'], "added/removed share a set"
    _add1['bins'].add('poison')
    _, _add2, _rem2 = _sl.classify(_sl.STATUS_MISSING, {}, {})
    assert _add2['bins'] == set(), "shared mutable set leaked across calls"
    assert _rem2['srcs'] == set()



def test_dep_drift_hard_dep_violation_omits_empty_constraint():
    """Regression (audit #111): a hard pre_depends/depends violation for an
    UNVERSIONED dep must render the name alone, not 'name ( )' — mirror the
    alt-dep path's conditional."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import dep_drift
    _src = inspect.getsource(dep_drift)
    assert '({_dep[2]} {_dep[1]}) — {_why}' not in _src, (
        "hard-dep violation unconditionally formats a constraint, yielding "
        "'name ( )' for unversioned deps")



def test_select_loads_groups_all_selected():
    """Model loads every pkg.list entry as selected=True, groups in
    declaration order."""
    import select_packages
    ctl, _t, _p = _select_controller(
        '[base]\n## Description: core\nbash\ncoreutils\n\n[devel]\ngit\nvim\n')
    # pkg.list groups in declaration order, plus the reserved pool tier.
    assert list(ctl._groups.keys()) == ['base', 'devel', select_packages.POOL_GROUP]
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

    cfg = SimpleNamespace(pkglist_path=path, poollist_path=path + ".pool")
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
    assert list(ctl._groups.keys()) == ['base', select_packages.POOL_GROUP]
    # mirror _save: pkg.list gets only the non-pool groups
    _pkg_only = {g: e for g, e in ctl._groups.items()
                 if g != select_packages.POOL_GROUP}
    select_packages.write_pkg_list(path, _pkg_only, ctl._meta)
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
    cfg = SimpleNamespace(pkglist_path=path, poollist_path=path + ".pool")

    class _T:
        COLOR_HIGHLIGHT = 5
        COLOR_INFO = 7
    ctl = select_packages.SelectPackages(cfg, _Cache(), _T())
    total, count = ctl._approx_closure('a')
    assert count == 3          # a, b, c
    assert total == 300        # 3 × 100 KB



def test_install_expand_or_group_resolution_is_deterministic():
    """Regression (audit #27): _install_expand must resolve OR-groups
    deterministically.  The frontier was seeded from a set, whose iteration
    order over str keys is PYTHONHASHSEED-randomized; the OR-group
    satisfied-check short-circuits on whatever is already in the closure, so an
    unsorted frontier resolves a genuine OR-group (here P:`A|B`, Q:`B|A`) to a
    different provider run-to-run — drifting the closure (and the mirror
    download manifest).  Run the same closure under several PYTHONHASHSEED
    values in fresh interpreters; the result must be identical."""
    import sys
    import subprocess
    _script = (
        'import sys; sys.path.insert(0, %r)\n'
        'import build_closure as bc\n'
        'bi = {"P": {"Depends": "A | B"}, "Q": {"Depends": "B | A"},'
        ' "A": {}, "B": {}}\n'
        'print(",".join(sorted(bc._install_expand(["P", "Q"], bi, {}))))\n'
    ) % os.path.join(_ROOT, 'scripts')
    _results = set()
    for _seed in ('0', '1', '2', '3', '4', '5', '6', '7'):
        _env = dict(os.environ, PYTHONHASHSEED=_seed)
        _out = subprocess.run([sys.executable, '-c', _script], env=_env,
                              capture_output=True, text=True, check=True)
        _results.add(_out.stdout.strip())
    assert _results == {'B,P,Q'}, (
        f"non-deterministic OR-group closure across hash seeds: {_results}")



def test_selection_lock_roundtrip_signs_and_verifies():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    with tempfile.TemporaryDirectory() as _root:
        _cfg = _selection_lock_cfg(_root)
        _state = {
            'arch': 'amd64', 'snapshot': '20260602T173733Z',
            'flags': {'IncludeRecommends': True, 'IncludeBuildClosure': False},
            'seeds': {'pkg': {'base': ['a', 'b']}, 'pool': ['c']},
            'closure': {'bins': {'a': ['base']}, 'srcs': {'asrc': ['base']}},
            'pins': {'awk': 'mawk'},
        }
        _sl.write_selection_state(_cfg, _state)
        _back, _status = _sl.read_selection_state(_cfg)
        assert _status == _sl.STATUS_OK, _status
        assert _back is not None
        assert _back['arch'] == 'amd64'
        assert _back['pins'] == {'awk': 'mawk'}
        assert _back['schema_version'] == _sl.SELECTION_STATE_SCHEMA_VERSION
        # write must not mutate the caller's dict
        assert 'sig' not in _state and 'schema_version' not in _state



def test_federation_state_adopts_owner_payload_and_local_seeds():
    """FEDERATION PROPAGATION: federation_state takes the owner's
    pins/closure/flags/snapshot from the canonical-config payload and the
    seeds from the peer's just-written local lists; re-signed with the peer's
    OWN key it reads back STATUS_OK (federation trust was the GPG head sha)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    with tempfile.TemporaryDirectory() as _root:
        _cfg = _selection_lock_cfg(_root)
        _cfg.pkglist_path = os.path.join(_root, 'pkg.list')
        _cfg.livelist_path = os.path.join(_root, 'live.list')
        _cfg.installerlist_path = os.path.join(_root, 'installer.list')
        _cfg.poollist_path = os.path.join(_root, 'pool.list')
        _cfg.arch = 'amd64'
        _cfg.snapshot_timestamp_config = ''
        _cfg.include_recommends = False
        with open(_cfg.pkglist_path, 'w') as _f:
            _f.write('[base]\nfirefox\n')        # peer's verbatim local list
        for _p in (_cfg.livelist_path, _cfg.installerlist_path,
                   _cfg.poollist_path):
            with open(_p, 'w') as _f:
                _f.write('')
        _payload = {'pins': {'x-terminal-emulator': 'xterm'},
                    'closure': {'bins': {'firefox': ['base']}, 'srcs': {}},
                    'flags': {'IncludeRecommends': True, 'IncludeBuildClosure': False},
                    'arch': 'amd64', 'snapshot': '20260602T173733Z'}
        _state = _sl.federation_state(_cfg, _payload)
        _sl.write_selection_state(_cfg, _state)         # peer-signed
        _back, _status = _sl.read_selection_state(_cfg)
        assert _status == _sl.STATUS_OK, _status
        assert _back['pins'] == {'x-terminal-emulator': 'xterm'}    # owner's
        assert _back['closure'] == {'bins': {'firefox': ['base']}, 'srcs': {}}
        assert _back['snapshot'] == '20260602T173733Z'             # owner's
        assert _back['flags']['IncludeRecommends'] is True         # owner's
        assert _back['seeds']['pkg'] == {'base': ['firefox']}      # peer's local



def test_selection_lock_missing_is_distinct_from_tamper():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    with tempfile.TemporaryDirectory() as _root:
        _cfg = _selection_lock_cfg(_root)
        _back, _status = _sl.read_selection_state(_cfg)
        assert _back is None and _status == _sl.STATUS_MISSING, _status



def test_selection_lock_tamper_detected_as_badsig():
    import json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    with tempfile.TemporaryDirectory() as _root:
        _cfg = _selection_lock_cfg(_root)
        _sl.write_selection_state(_cfg, {'arch': 'amd64', 'closure': {}})
        _path = _sl.selection_state_path(_cfg)
        _doc = json.loads(open(_path).read())
        _doc['arch'] = 'arm64'                       # mutate a signed field
        open(_path, 'w').write(json.dumps(_doc, sort_keys=True, indent=2))
        _back, _status = _sl.read_selection_state(_cfg)
        assert _back is None and _status == _sl.STATUS_BADSIG, _status



def test_selection_lock_malformed_json_is_malformed():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    with tempfile.TemporaryDirectory() as _root:
        _cfg = _selection_lock_cfg(_root)
        open(_sl.selection_state_path(_cfg), 'w').write('{not json')
        _back, _status = _sl.read_selection_state(_cfg)
        assert _back is None and _status == _sl.STATUS_MALFORMED, _status
        # a valid-JSON non-dict is also malformed
        open(_sl.selection_state_path(_cfg), 'w').write('[1, 2, 3]')
        assert _sl.read_selection_state(_cfg)[1] == _sl.STATUS_MALFORMED



def test_selection_lock_preserves_unknown_future_keys():
    """Forward-compat: a key this version doesn't know (e.g. a future
    IncludeBuildClosure closure section) survives a read and re-verifies."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    with tempfile.TemporaryDirectory() as _root:
        _cfg = _selection_lock_cfg(_root)
        _sl.write_selection_state(
            _cfg, {'arch': 'amd64', 'future_section': {'x': 1}})
        _back, _status = _sl.read_selection_state(_cfg)
        assert _status == _sl.STATUS_OK, _status
        assert _back['future_section'] == {'x': 1}



def test_selection_lock_build_closure_canonical_and_tiers():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    # 'libfoo0' is a real pkg; 'foo-virtual' is a Provides alias of libfoo0.
    _deb = _sl_tree(
        {'base-files': 'base-files', 'grub-pc': 'grub-pc',
         'foo-virtual': 'libfoo0', 'libfoo0': 'libfoo0'},
        {'base-files', 'grub2'},
        pool=['grub-pc'], groups={'gnome': ['libfoo0']})
    _udeb = _sl_tree({'anna': 'anna'}, {'anna', 'debootstrap'})
    _clo = _sl.build_closure(_deb, _udeb, None)
    # virtual alias dropped
    assert 'foo-virtual' not in _clo['bins'], _clo['bins']
    assert set(_clo['bins']) == {'base-files', 'grub-pc', 'libfoo0', 'anna'}
    assert _clo['bins']['base-files'] == ['base']
    assert _clo['bins']['grub-pc'] == ['pool']
    assert _clo['bins']['libfoo0'] == ['group:gnome']
    assert 'udeb' in _clo['bins']['anna']
    # udeb sources unioned in (the installer-only sources)
    assert {'base-files', 'grub2', 'anna', 'debootstrap'} == set(_clo['srcs'])
    assert _clo['srcs']['debootstrap'] == ['udeb']



def test_selection_lock_diff_closure_add_remove_and_tier_only():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    _old = {'bins': {'a': ['base'], 'b': ['base']}, 'srcs': {'asrc': ['deb']}}
    # add-only
    _new_add = {'bins': {'a': ['base'], 'b': ['base'], 'c': ['base']},
                'srcs': {'asrc': ['deb']}}
    _added, _removed = _sl.diff_closure(_old, _new_add)
    assert _added['bins'] == {'c'} and not _removed['bins'], (_added, _removed)
    # remove-only (the dangerous case)
    _new_rm = {'bins': {'a': ['base']}, 'srcs': {}}
    _added, _removed = _sl.diff_closure(_old, _new_rm)
    assert _removed['bins'] == {'b'} and _removed['srcs'] == {'asrc'}, _removed
    assert not _added['bins'], _added
    # tier-only change ⇒ NO add/remove (names identical, only tags differ)
    _new_tier = {'bins': {'a': ['pool'], 'b': ['base']}, 'srcs': {'asrc': ['deb']}}
    _added, _removed = _sl.diff_closure(_old, _new_tier)
    assert not _added['bins'] and not _removed['bins'], (_added, _removed)



def test_selection_lock_classify_asymmetric_guard():
    """SELECT-LOCK Chunk 4 policy: missing→bootstrap, badsig/malformed→
    hardstop, removal→block, additions-only/unchanged→refresh."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    _lock = {'closure': {'bins': {'a': ['base'], 'b': ['base']},
                         'srcs': {'asrc': ['deb']}}}
    # missing → bootstrap
    _act, _, _ = _sl.classify(_sl.STATUS_MISSING, None, {'bins': {}, 'srcs': {}})
    assert _act == _sl.ACTION_BOOTSTRAP, _act
    # tamper → hardstop
    for _s in (_sl.STATUS_BADSIG, _sl.STATUS_MALFORMED):
        _act, _, _ = _sl.classify(_s, _lock, _lock['closure'])
        assert _act == _sl.ACTION_HARDSTOP, (_s, _act)
    # removal → block (b + asrc dropped)
    _shrunk = {'bins': {'a': ['base']}, 'srcs': {}}
    _act, _added, _removed = _sl.classify(_sl.STATUS_OK, _lock, _shrunk)
    assert _act == _sl.ACTION_BLOCK, _act
    assert _removed['bins'] == {'b'} and _removed['srcs'] == {'asrc'}, _removed
    # additions-only → refresh
    _grown = {'bins': {'a': ['base'], 'b': ['base'], 'c': ['base']},
              'srcs': {'asrc': ['deb']}}
    _act, _added, _removed = _sl.classify(_sl.STATUS_OK, _lock, _grown)
    assert _act == _sl.ACTION_REFRESH and _added['bins'] == {'c'}, (_act, _added)
    # unchanged → refresh (no add/remove)
    _act, _added, _removed = _sl.classify(_sl.STATUS_OK, _lock, _lock['closure'])
    assert _act == _sl.ACTION_REFRESH
    assert not _added['bins'] and not _removed['bins']



def test_selection_lock_assemble_state_shape_and_pin_union():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    import types
    _cfg = types.SimpleNamespace(
        arch='amd64', snapshot_timestamp_config='20260602T173733Z',
        include_recommends=True, pkglist_path='', livelist_path='',
        installerlist_path='', poollist_path='')
    _deb = _sl_tree({'a': 'a'}, {'asrc'})
    _deb._pinned_chosen = {'awk': 'mawk'}
    _udeb = _sl_tree({'anna': 'anna'}, {'anna'})
    _udeb._pinned_chosen = {'telnet-client': 'telnet'}
    _state = _sl.assemble_state(_deb, _udeb, _cfg)
    assert _state['arch'] == 'amd64'
    assert _state['flags']['IncludeRecommends'] is True
    assert _state['flags']['IncludeBuildClosure'] is False
    # pins union across both trees
    assert _state['pins'] == {'awk': 'mawk', 'telnet-client': 'telnet'}, _state['pins']
    assert 'a' in _state['closure']['bins'] and 'anna' in _state['closure']['bins']
    assert set(_state['seeds']) == {'pkg', 'pkg_meta', 'live', 'installer', 'pool'}



def test_selection_lock_restore_roundtrips_lists_with_meta():
    """SELECT-LOCK Chunk 5: restore regenerates the four list files from the
    lockfile seeds; pkg.list round-trips through the parsers INCLUDING the
    `## Description:` tasksel metadata, and flat lists match seed order."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    import utils as _u
    import types
    with tempfile.TemporaryDirectory() as _root:
        _cfgdir = os.path.join(_root, 'config')
        os.makedirs(_cfgdir)
        _cfg = types.SimpleNamespace(
            pkglist_path=os.path.join(_cfgdir, 'pkg.list'),
            livelist_path=os.path.join(_cfgdir, 'live.list'),
            installerlist_path=os.path.join(_cfgdir, 'installer.list'),
            poollist_path=os.path.join(_cfgdir, 'pool.list'))
        _lock = {'seeds': {
            'pkg': {'base': ['a', 'b'], 'desktop': ['x']},
            'pkg_meta': {'desktop': {'description': 'Desktop environment'}},
            'live': ['live-boot'], 'installer': ['anna'],
            'pool': ['grub-pc', 'grub-efi-amd64']}}
        _written = _sl.restore_list_files(_cfg, _lock)
        assert set(_written) == {'pkg', 'live', 'installer', 'pool'}, _written
        # pkg.list round-trips groups + names
        _groups = _u.parse_pkg_list_groups(_cfg.pkglist_path)
        assert _groups == {'base': ['a', 'b'], 'desktop': ['x']}, _groups
        # and the tasksel description survives
        _meta = _u.parse_pkg_list_group_meta(_cfg.pkglist_path)
        assert _meta.get('desktop', {}).get('description') == 'Desktop environment', _meta
        # flat lists preserve order
        assert open(_cfg.poollist_path).read().split() == ['grub-pc', 'grub-efi-amd64']
        assert open(_cfg.livelist_path).read().strip() == 'live-boot'



def test_selection_lock_restore_seeds_raw_byte_faithful():
    """SELECT-01: seeds_raw round-trips the four list files BYTE-FOR-BYTE on
    restore — operator comments survive and pkg-group order is preserved
    (the parsed-and-re-rendered form alphabetises groups + strips comments)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    import utils as _u
    import types
    with tempfile.TemporaryDirectory() as _root:
        _cfgdir = os.path.join(_root, 'config')
        os.makedirs(_cfgdir)
        _cfg = types.SimpleNamespace(
            pkglist_path=os.path.join(_cfgdir, 'pkg.list'),
            livelist_path=os.path.join(_cfgdir, 'live.list'),
            installerlist_path=os.path.join(_cfgdir, 'installer.list'),
            poollist_path=os.path.join(_cfgdir, 'pool.list'))
        # pkg.list: groups in NON-alphabetical order, with comments + a
        # tasksel description — exactly what the name-render would mangle.
        _pkg_text = (
            "# operator notes: desktop must resolve before base\n"
            "[desktop]\n"
            "## Description: Desktop environment\n"
            "xterm\n"
            "\n"
            "[base]\n"
            "coreutils\n"
            "dpkg\n"
        )
        _pool_text = (
            "# pool.list — shipped in /cdrom/pool, never installed\n"
            "# both grub flavours intentionally co-exist here\n"
            "grub-pc\n"
            "grub-efi-amd64\n"
        )
        _u._atomic_write_bytes(_cfg.pkglist_path, _pkg_text.encode('utf-8'))
        _u._atomic_write_bytes(_cfg.poollist_path, _pool_text.encode('utf-8'))
        _u._atomic_write_bytes(_cfg.livelist_path, b"live-boot\n")
        _u._atomic_write_bytes(_cfg.installerlist_path, b"anna\n")

        # Capture exactly what assemble_state would stash.
        _lock = {
            'seeds': _sl.seeds_from_config(_cfg),
            'seeds_raw': _sl.seeds_raw_from_config(_cfg),
        }
        assert set(_lock['seeds_raw']) == {'pkg', 'live', 'installer', 'pool'}
        assert _lock['seeds_raw']['pkg'] == _pkg_text

        # Blow the files away and restore from the lock.
        for _p in (_cfg.pkglist_path, _cfg.poollist_path,
                   _cfg.livelist_path, _cfg.installerlist_path):
            os.remove(_p)
        _sl.restore_list_files(_cfg, _lock)

        # Byte-for-byte: comments + group order preserved.
        assert open(_cfg.pkglist_path).read() == _pkg_text
        assert open(_cfg.poollist_path).read() == _pool_text
        # group ORDER (desktop before base) survived — the render would invert it
        assert (open(_cfg.pkglist_path).read().index('[desktop]')
                < open(_cfg.pkglist_path).read().index('[base]'))

        # Legacy lockfile (no seeds_raw) falls back to the name-render: it still
        # round-trips the GROUPS but loses comments and alphabetises order.
        _legacy = {'seeds': _lock['seeds']}
        for _p in (_cfg.pkglist_path, _cfg.poollist_path):
            os.remove(_p)
        _sl.restore_list_files(_cfg, _legacy)
        _rendered = open(_cfg.pkglist_path).read()
        assert '# operator notes' not in _rendered          # comments gone
        assert _u.parse_pkg_list_groups(_cfg.pkglist_path) == {
            'desktop': ['xterm'], 'base': ['coreutils', 'dpkg']}



def test_selection_lock_closure_matches_lock_statuses():
    """SELECT-LOCK Chunk 9: closure_matches_lock distinguishes match / delta /
    nolock / badlock so ISO + audits can refuse a stale RAM tree."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import selection_lock as _sl
    with tempfile.TemporaryDirectory() as _root:
        _cfg = _selection_lock_cfg(_root)
        _cfg.arch = 'amd64'
        _cfg.snapshot_timestamp_config = ''
        _cfg.include_recommends = True
        _cfg.pkglist_path = _cfg.livelist_path = ''
        _cfg.installerlist_path = _cfg.poollist_path = ''
        _deb = _sl_tree({'a': 'a', 'b': 'b'}, {'asrc'})
        _udeb = None
        # nolock — no lockfile yet
        _st, _, _ = _sl.closure_matches_lock(_deb, _udeb, _cfg)
        assert _st == _sl.COHERENCE_NOLOCK, _st
        # write the lockfile from this exact closure → match
        _sl.write_selection_state(_cfg, _sl.assemble_state(_deb, _udeb, _cfg))
        _st, _, _ = _sl.closure_matches_lock(_deb, _udeb, _cfg)
        assert _st == _sl.COHERENCE_MATCH, _st
        # resolve a SHRUNK tree (drop b) → delta
        _deb2 = _sl_tree({'a': 'a'}, {'asrc'})
        _st, _add, _rem = _sl.closure_matches_lock(_deb2, _udeb, _cfg)
        assert _st == _sl.COHERENCE_DELTA and _rem['bins'] == {'b'}, (_st, _rem)
        # corrupt the lockfile → badlock
        _p = _sl.selection_state_path(_cfg)
        open(_p, 'w').write('{not json')
        _st, _, _ = _sl.closure_matches_lock(_deb, _udeb, _cfg)
        assert _st == _sl.COHERENCE_BADLOCK, _st



def test_select_packages_pool_tier_load_save_and_comments():
    """cache select extension: pool.list loads as the reserved (pool) group;
    save routes pool entries to pool.list (comment-preserving) and the rest to
    pkg.list, untouched."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import select_packages as _sp
    import utils as _u
    import types

    class _T:   # minimal tui stub for _save → _render
        def print(self, *a): pass
        def set_tab_buffer(self, *a): pass
        def viewport_rows(self): return 24
        def attr_reverse(self): return 0
        def attr_color(self, c): return 0

    with tempfile.TemporaryDirectory() as _root:
        _cfgdir = os.path.join(_root, 'config'); os.makedirs(_cfgdir)
        _pkg = os.path.join(_cfgdir, 'pkg.list')
        _pool = os.path.join(_cfgdir, 'pool.list')
        open(_pkg, 'w').write('[base]\n## Description: Base\na\nb\n[desktop]\nx\n')
        open(_pool, 'w').write('# header comment\ngrub-pc\nreportbug\n# trailing\n')
        _cfg = types.SimpleNamespace(pkglist_path=_pkg, poollist_path=_pool)
        _sel = _sp.SelectPackages(_cfg, None, _T())
        # pool.list loaded as the reserved group
        assert _sp.POOL_GROUP in _sel._groups, list(_sel._groups)
        assert [n for n, s in _sel._groups[_sp.POOL_GROUP]] == ['grub-pc', 'reportbug']
        # drop reportbug from the pool tier, then save
        for _e in _sel._groups[_sp.POOL_GROUP]:
            if _e[0] == 'reportbug':
                _e[1] = False
        _sel._save()
        # pool.list: reportbug gone, comments + grub-pc preserved
        _pool_txt = open(_pool).read()
        assert '# header comment' in _pool_txt and '# trailing' in _pool_txt
        assert 'grub-pc' in _pool_txt
        assert 'reportbug' not in _pool_txt.replace('# ', '')  # not as a pkg line
        # pkg.list untouched (groups + description intact)
        assert _u.parse_pkg_list_groups(_pkg) == {'base': ['a', 'b'], 'desktop': ['x']}
        assert _u.parse_pkg_list_group_meta(_pkg).get('base', {}).get('description') == 'Base'



def test_select_packages_save_and_quit_apply_intent():
    """save & apply (W): writes the lists then signals 'apply' so the shell
    thread runs the parse/accept transaction; teardown happens before the
    shell thread is released."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import select_packages as _sp
    import types

    class _T:
        def __init__(self): self.cleared = False; self.removed = []
        def print(self, *a): pass
        def set_tab_buffer(self, *a): pass
        def viewport_rows(self): return 24
        def attr_reverse(self): return 0
        def attr_color(self, c): return 0
        def add_tab(self, n): pass
        def activate_tab(self, n): pass
        def set_tab_key_handler(self, n, f): pass
        def clear_tab_key_handler(self): self.cleared = True
        def remove_tab(self, n): self.removed.append(n)

    with tempfile.TemporaryDirectory() as _root:
        _cfgdir = os.path.join(_root, 'config'); os.makedirs(_cfgdir)
        _pkg = os.path.join(_cfgdir, 'pkg.list')
        _pool = os.path.join(_cfgdir, 'pool.list')
        open(_pkg, 'w').write('[base]\na\n')
        open(_pool, 'w').write('# keep\ngrub-pc\nreportbug\n')
        _cfg = types.SimpleNamespace(pkglist_path=_pkg, poollist_path=_pool)
        _t = _T()
        _sel = _sp.SelectPackages(_cfg, None, _t)
        # drop reportbug from pool, then save & apply
        for _e in _sel._groups[_sp.POOL_GROUP]:
            if _e[0] == 'reportbug':
                _e[1] = False
        _sel._save_and_quit()                 # writes lists + _finish('apply')
        assert _sel.wait_for_done() == 'apply'
        assert _t.cleared and _t.removed == ['select']   # torn down
        _pool_txt = open(_pool).read()
        assert '# keep' in _pool_txt and 'grub-pc' in _pool_txt
        assert 'reportbug' not in _pool_txt.replace('# ', '')   # dropped



def test_select_packages_discard_intent_reverts_nothing_written_by_finish():
    """quit + discard signals 'discard' (the caller reverts the files); a clean
    quit with no edits signals 'quit'."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import select_packages as _sp
    import types

    class _T:
        def print(self, *a): pass
        def set_tab_buffer(self, *a): pass
        def viewport_rows(self): return 24
        def attr_reverse(self): return 0
        def attr_color(self, c): return 0
        def clear_tab_key_handler(self): pass
        def remove_tab(self, n): pass

    with tempfile.TemporaryDirectory() as _root:
        _pkg = os.path.join(_root, 'pkg.list'); open(_pkg, 'w').write('[base]\na\n')
        _pool = os.path.join(_root, 'pool.list'); open(_pool, 'w').write('grub-pc\n')
        _cfg = types.SimpleNamespace(pkglist_path=_pkg, poollist_path=_pool)
        # clean quit (no edits) → 'quit'
        _s1 = _sp.SelectPackages(_cfg, None, _T())
        _s1._quit()
        assert _s1.wait_for_done() == 'quit'
        # edit then discard → 'discard'
        _s2 = _sp.SelectPackages(_cfg, None, _T())
        _s2._groups['base'][0][1] = False     # toggle off → unsaved
        _s2._unsaved = True
        _s2._quit()                            # → input_mode 'quit'
        assert _s2._input_mode == 'quit'
        _s2._handle_input_key('n')             # discard
        assert _s2.wait_for_done() == 'discard'



def test_select_packages_write_flat_list_minimal_diff():
    """write_flat_list keeps comments + still-selected lines in place, drops
    unselected, appends new at EOF."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import select_packages as _sp
    with tempfile.TemporaryDirectory() as _root:
        _p = os.path.join(_root, 'pool.list')
        open(_p, 'w').write('# c1\nkeep-me\ndrop-me\n# c2\nalso-keep\n')
        _sp.write_flat_list(_p, ['keep-me', 'also-keep', 'brand-new'])
        _lines = open(_p).read().splitlines()
        assert '# c1' in _lines and '# c2' in _lines        # comments preserved
        assert 'drop-me' not in _lines                       # unselected dropped
        assert _lines.index('keep-me') < _lines.index('# c2')  # order preserved
        assert _lines[-1] == 'brand-new'                     # new appended at EOF



def test_surface_closure_credit_trap_and_or_groups():
    """The credit-trap pin: a dep shared by two groups is reachable from
    EITHER group's seeds via the closure (never lost to delta crediting).
    OR groups follow the first SELECTED alternative; virtual seeds
    canonicalize."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import surfaces as _sf
    _shared = _SurfPkg('libshared')
    _pkgs = {
        'devtool':  _SurfPkg('devtool', depends=['libshared']),
        'gnome-app': _SurfPkg('gnome-app', depends=['libshared'],
                              alt_depends=[['xterm', 'x-terminal-emulator']]),
        'libshared': _shared,
        # xterm NOT selected; the provider gnome-terminal IS, via virtual
        'x-terminal-emulator': _SurfPkg('gnome-terminal'),
        'gnome-terminal': _SurfPkg('gnome-terminal'),
        # virtual alias for seed canonicalization
        'editor': _SurfPkg('nano'),
        'nano': _SurfPkg('nano'),
    }
    _dt = _surf_tree(_pkgs)
    # closure from gnome-app alone MUST include the shared dep (even though
    # a delta-credit model could have credited it to devtool's group)
    _c = _sf.surface_closure(_dt, ['gnome-app'])
    assert 'libshared' in _c, _c
    # OR group: xterm absent → first SELECTED alt = x-terminal-emulator →
    # canonicalized to gnome-terminal
    assert 'gnome-terminal' in _c and 'xterm' not in _c, _c
    # virtual seed canonicalizes
    assert _sf.surface_closure(_dt, ['editor']) == {'nano'}
    # absent seed silently skipped
    assert _sf.surface_closure(_dt, ['ghost']) == set()



def test_surface_closure_extras_fixpoint():
    """extras=True pulls only extras RECOMMENDED BY closure members, plus
    their hard deps, to fixpoint; unrecommended extras stay out."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import surfaces as _sf
    _pkgs = {
        'app':      _SurfPkg('app', recommends=['codec']),
        'codec':    _SurfPkg('codec', depends=['libcodec']),
        'libcodec': _SurfPkg('libcodec', recommends=['codec-extra']),
        'codec-extra': _SurfPkg('codec-extra'),
        'lonely-extra': _SurfPkg('lonely-extra'),
    }
    _dt = _surf_tree(_pkgs, extras=['codec', 'codec-extra', 'lonely-extra'])
    # hard closure only — no extras
    assert _sf.surface_closure(_dt, ['app']) == {'app'}
    # with extras: app→(rec)codec→(dep)libcodec→(rec)codec-extra (fixpoint);
    # lonely-extra recommended by nobody in the surface stays out
    _c = _sf.surface_closure(_dt, ['app'], include_recommends_extras=True)
    assert _c == {'app', 'codec', 'libcodec', 'codec-extra'}, _c



def test_surfaces_group_seed_names_and_flat_roots():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import surfaces as _sf
    with tempfile.TemporaryDirectory() as _root:
        _pkg = os.path.join(_root, 'pkg.list')
        open(_pkg, 'w').write('[base]\na\nb\n[gnome-desktop]\nc\n')
        assert _sf.group_seed_names(_pkg, ['base'], arch='amd64') == {'a', 'b'}
        assert _sf.group_seed_names(_pkg, ['base', 'gnome-desktop'],
                                    arch='amd64') == \
            {'a', 'b', 'c'}
        assert _sf.group_seed_names(_pkg, ['nope'], arch='amd64') == set()
        _flat = os.path.join(_root, 'installer-defaults.list')
        open(_flat, 'w').write('# d-i roots\ngrub-pc\n\nintel-microcode\n')
        assert _sf.read_flat_roots(_flat, arch='amd64') == ['grub-pc', 'intel-microcode']
        assert _sf.read_flat_roots(_flat + '.missing', arch='amd64') == []



def test_buildconfig_surface_group_knobs():
    """[Live]/[Disk] Groups parse comma/space lists; absent sections default
    to {'base'}; installer_defaults_path points under config/."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    _cfg = _u.BuildConfig()
    assert isinstance(_cfg.live_groups, set) and _cfg.live_groups
    assert isinstance(_cfg.disk_groups, set) and _cfg.disk_groups
    assert 'base' in _cfg.disk_groups
    assert _cfg.installer_defaults_path.endswith(
        'config/installer-defaults.list')
    # the authored defaults file parses + carries the d-i roots
    import surfaces as _sf
    _roots = _sf.read_flat_roots(_cfg.installer_defaults_path, arch='amd64')
    for _must in ('grub-pc', 'grub-efi-amd64', 'console-setup',
                  'intel-microcode', 'open-vm-tools'):
        assert _must in _roots, f'{_must} missing from installer-defaults'
    # every root is also a pool.list build/publish root (sync invariant)
    _pool = set(_sf.read_flat_roots(_cfg.poollist_path, arch='amd64'))
    _missing = [_r for _r in _roots if _r not in _pool]
    assert not _missing, f'installer-defaults not in pool.list: {_missing}'



# ── ARCH-FILTER: assured foreign-target cross-toolchain gate ──────────

def test_arch_filter_native_and_dev_kept():
    """The native cross-package (x86-64-linux-gnu → build arch) and plain
    toolchain / -dev binaries are NOT foreign — must be kept."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import arch_filter as _af
    for _n in ('binutils-x86-64-linux-gnu',
               'binutils-x86-64-linux-gnu-dbg',
               'gcc-12', 'cpp-12', 'libc6-dev'):
        assert _af.is_foreign_target_binary(_n, 'amd64') is False, _n



def test_arch_filter_foreign_dropped():
    """Cross-toolchains targeting a non-build arch — incl. the kfreebsd /
    hurd families and the armhf longest-match — are foreign."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import arch_filter as _af
    for _n in ('binutils-aarch64-linux-gnu',
               'binutils-x86-64-kfreebsd-gnu-dbg',   # the Issue-2 case
               'gcc-12-arm-linux-gnueabihf',          # armhf via longest-match
               'binutils-i686-gnu',                   # hurd-i386 (cpu alias)
               'g++-multilib-i686-linux-gnu',
               'libc6-arm64-cross'):                  # -cross bare-arch form
        assert _af.is_foreign_target_binary(_n, 'amd64') is True, _n



def test_arch_filter_no_false_positives():
    """The historical substring traps (arm/arc) and multilib runtimes
    (native amd64 binaries with a foreign-ABI name) must be KEPT — a false
    positive here would break closure."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import arch_filter as _af
    for _n in ('apparmor', 'build-essential', 'libaom-doc', 'libabsl-dev',
               'acl-udeb', 'zbar-tools', 'libc6-i386', 'libc6-x32',
               'lib32gcc-s1', 'libx32gcc-s1'):
        assert _af.is_foreign_target_binary(_n, 'amd64') is False, _n



def test_arch_filter_failsafe_keeps_on_uncertainty():
    """Empty dpkg map (dpkg absent) or an unparseable name → KEEP; the
    detector never raises and never drops on uncertainty."""
    import sys
    from unittest.mock import patch
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import arch_filter as _af
    with patch.object(_af, '_maps', return_value=({}, set())):
        assert _af.is_foreign_target_binary(
            'binutils-aarch64-linux-gnu', 'amd64') is False
    assert _af.is_foreign_target_binary('', 'amd64') is False
    assert _af.is_foreign_target_binary('totally-made-up-pkg', 'amd64') is False



def test_build_closure_compute_returns_all_and_unsatisfiable():
    """compute_build_closure returns ONLY the full closure ('all', with the
    build-essential/dpkg-dev toolchain base folded in) and the dropped
    'unsatisfiable' Build-Depends groups — tier segregation lives solely in
    classify_tiers (CONS-16)."""
    import build_closure as bc
    bin_index = {
        'build-essential': {'Depends': 'gcc, make'},
        'gcc': {'Depends': 'libc6-dev'}, 'make': {}, 'libc6-dev': {},
        'dpkg-dev': {}, 'cmake': {'Depends': 'libc6-dev'},
        'debhelper': {'Depends': 'dpkg-dev'},
        'libfoo-dev': {'Depends': 'libfoo1'}, 'libfoo1': {},
    }
    r = bc.compute_build_closure(
        ['foo'], {'foo': 'debhelper, cmake, libfoo-dev'}, bin_index, {})
    # full closure: direct build-deps + their install closure + toolchain base
    assert {'build-essential', 'dpkg-dev', 'gcc', 'make', 'libc6-dev',
            'debhelper', 'cmake', 'libfoo-dev', 'libfoo1'} <= r['all']
    assert r['unsatisfiable'] == []
    # tiers are no longer computed here — classify_tiers owns that
    assert set(r.keys()) == {'all', 'unsatisfiable'}
    # an unresolvable Build-Depends group is reported (toolchain base still in)
    r2 = bc.compute_build_closure(
        ['bar'], {'bar': 'no-such-pkg'}, bin_index, {})
    assert {'build-essential', 'dpkg-dev'} <= r2['all']
    assert any('no-such-pkg' in _g for (_s, _g) in r2['unsatisfiable'])



def test_build_closure_classify_tiers_uses_adjacency():
    """classify_tiers: reachability over a forward-dep adjacency assigns
    toolchain (from build-essential) vs language (from cmake) vs leaf."""
    import build_closure as bc
    members = {'build-essential', 'gcc', 'cmake', 'cmake-data', 'libbar-dev'}
    adjacency = {
        'build-essential': ['gcc'], 'gcc': [],
        'cmake': ['cmake-data'], 'cmake-data': [],
        'libbar-dev': [],
    }
    t = bc.classify_tiers(members, adjacency)
    assert t['toolchain'] == {'build-essential', 'gcc'}
    assert t['language'] == {'cmake', 'cmake-data'}
    assert t['leaf'] == {'libbar-dev'}



def test_arch_filter_degrades_to_keep_when_arch_table_missing():
    """Audit #22: with the triplet map populated but _arch_table() None (dpkg
    tables unreadable), is_foreign_target_binary degrades to KEEP (False)
    rather than dropping a foreign-looking cross-toolchain binary."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import arch_filter as _af
    _orig = _af._arch_table
    _af._arch_table = lambda: None
    try:
        assert _af.is_foreign_target_binary(
            'binutils-aarch64-linux-gnu', 'amd64') is False
    finally:
        _af._arch_table = _orig



def test_compute_build_closure_provides_resolution_path():
    """Audit #29: a build-dep that is a pure VIRTUAL name resolves via
    provides_index to its provider (which lands in the closure), and a
    transitive Depends is satisfied by a provider pulled in the same way."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build_closure as bc
    _bin = {
        'realprovider':  {'Depends': 'virtdep2', 'Pre-Depends': ''},
        'realprovider2': {'Depends': '', 'Pre-Depends': ''},
        'build-essential': {'Depends': '', 'Pre-Depends': ''},
        'dpkg-dev':        {'Depends': '', 'Pre-Depends': ''},
    }
    _prov = {'virtdep': ['realprovider'], 'virtdep2': ['realprovider2']}
    _r = bc.compute_build_closure(['s'], {'s': 'virtdep'}, _bin, _prov)
    _all = _r['all']
    assert 'realprovider' in _all      # direct virtual build-dep → provider
    assert 'realprovider2' in _all     # transitive Depends → provider
    assert not _r['unsatisfiable']



def test_classify_tiers_transit_through_non_member_and_overlap():
    """Audit #30: classify_tiers reaches a member THROUGH a non-member
    intermediary, and a member reachable from BOTH seed sets lands in
    toolchain (toolchain wins over language)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build_closure as bc
    _members = {'M1', 'M2'}
    _adj = {
        'TC':   ['mid'],          # non-member intermediary
        'mid':  ['M1'],           # reach M1 only through it
        'LANG': ['M1', 'M2'],     # M1 reachable from lang too; M2 lang-only
    }
    _tiers = bc.classify_tiers(
        _members, _adj,
        toolchain_seeds=frozenset({'TC'}),
        language_seeds=frozenset({'LANG'}))
    assert 'M1' in _tiers['toolchain']    # toolchain wins the overlap
    assert 'M1' not in _tiers['language']
    assert 'M2' in _tiers['language']
    assert not _tiers['leaf']

TESTS = [
    test_build_closure_compute_returns_all_and_unsatisfiable,
    test_build_closure_classify_tiers_uses_adjacency,
    test_select_latest_kernel_version_aware_and_paired,
    test_parse_pkg_list_groups_flat_file_becomes_implicit_base,
    test_parse_pkg_list_groups_ini_style_multi_section,
    test_parse_pkg_list_groups_rejects_seed_before_first_section,
    test_parse_pkg_list_groups_empty_section_name_raises,
    test_parse_pkg_list_group_meta_extracts_descriptions,
    test_parse_pkg_list_group_meta_flat_file_returns_base_only,
    test_d5_arch_qualifier_semantics_and_reader_views,
    test_d5_shipped_lists_amd64_view_and_wiring,
    test_pkg_list_base_includes_athena_tasksel,
    test_verify_dep_resolution_skips_extras,
    test_verify_dep_resolution_still_catches_real_violations,
    test_sta24_dep_target_names_extracts_all_fields_names_only,
    test_sta24_check_dep_drift_warns_on_lost_selected_dep,
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
    test_selection_lock_roundtrip_signs_and_verifies,
    test_federation_state_adopts_owner_payload_and_local_seeds,
    test_selection_lock_missing_is_distinct_from_tamper,
    test_selection_lock_tamper_detected_as_badsig,
    test_selection_lock_malformed_json_is_malformed,
    test_selection_lock_preserves_unknown_future_keys,
    test_selection_lock_build_closure_canonical_and_tiers,
    test_selection_lock_diff_closure_add_remove_and_tier_only,
    test_selection_lock_classify_asymmetric_guard,
    test_selection_lock_assemble_state_shape_and_pin_union,
    test_selection_lock_restore_roundtrips_lists_with_meta,
    test_selection_lock_restore_seeds_raw_byte_faithful,
    test_selection_lock_closure_matches_lock_statuses,
    test_select_packages_pool_tier_load_save_and_comments,
    test_select_packages_write_flat_list_minimal_diff,
    test_select_packages_save_and_quit_apply_intent,
    test_select_packages_discard_intent_reverts_nothing_written_by_finish,
    test_surface_closure_credit_trap_and_or_groups,
    test_surface_closure_extras_fixpoint,
    test_surfaces_group_seed_names_and_flat_roots,
    test_buildconfig_surface_group_knobs,
    test_install_expand_or_group_resolution_is_deterministic,
    test_read_selection_state_distinguishes_transient_io_error,
    test_select_packages_detects_reserved_pool_group_clash,
    test_selection_lock_classify_returns_independent_empty_sets,
    test_dep_drift_hard_dep_violation_omits_empty_constraint,
    test_arch_filter_native_and_dev_kept,
    test_arch_filter_foreign_dropped,
    test_arch_filter_no_false_positives,
    test_arch_filter_degrades_to_keep_when_arch_table_missing,
    test_compute_build_closure_provides_resolution_path,
    test_classify_tiers_transit_through_non_member_and_overlap,
    test_arch_filter_failsafe_keeps_on_uncertainty,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
