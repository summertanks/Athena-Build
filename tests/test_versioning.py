"""Athena tests — version transpose / strip / bump mechanics (bump.py, bump_version.py, _version.py).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile

from _test_helpers import (  # noqa: F401
    _ROOT,
    _build_test_deb,
    _have_dpkg_deb,
)




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
            # `athena-build` is the generated toolchain-provenance marker
            # (always written, like .disk/snapshot); the *.md README is the one
            # that must NOT be copied from installer/disk/.
            assert sorted(os.listdir(_disk)) == [
                'athena-build', 'base_components', 'base_installable', 'info'
            ], (
                "README.md should NOT be copied to .disk/; "
                f"got {sorted(os.listdir(_disk))}"
            )
            # provenance marker carries the toolchain version
            import _version as _ver
            with open(os.path.join(_disk, 'athena-build')) as fh:
                assert fh.read().strip().startswith(_ver.base_version())
            # info content preserved verbatim
            with open(os.path.join(_disk, 'info')) as fh:
                assert fh.read() == 'Athena 0.1 amd64 INSTALLER\n'



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



# --- TRANSPOSE scheme primitives (transpose / strip_binNMU) ---------------

def test_transpose_trailing_plus_deb():
    """A trailing `+debNuK` becomes `+asg<R>uK`; epoch preserved."""
    from utils import transpose
    assert transpose('2.36-9+deb12u14', 'asg', 1) == '2.36-9+asg1u14'
    assert transpose('7:5.1.9-0+deb12u1', 'asg', 1) == '7:5.1.9-0+asg1u1'
    # a different prefix/release flow through verbatim
    assert transpose('1.2.3-4+deb12u3', 'val', 2) == '1.2.3-4+val2u3'



def test_transpose_trailing_tilde_deb_keeps_sign():
    """A trailing `~debNuK` keeps its `~` → `~asg<R>uK` (sorts below pristine)."""
    from utils import transpose
    assert transpose('2.4.67-1~deb12u2', 'asg', 1) == '2.4.67-1~asg1u2'
    assert transpose('1:9.18.49-1~deb12u1', 'asg', 1) == '1:9.18.49-1~asg1u1'



def test_transpose_embedded_deb_is_left_alone():
    """An EMBEDDED `+deb` (not the trailing token) is upstream identity — kept.
    shim ships `1.44~1+deb12u1+15.8-1~deb12u1`: only the trailing `~deb12u1`
    transposes; the mid-string `+deb12u1` stays."""
    from utils import transpose
    assert (transpose('1.44~1+deb12u1+15.8-1~deb12u1', 'asg', 1)
            == '1.44~1+deb12u1+15.8-1~asg1u1')
    # deb token followed by more version structure (cross-gcc) → no trailing
    # token → unchanged.
    assert transpose('12.2.0-14+deb12u1+25.2', 'asg', 1) == '12.2.0-14+deb12u1+25.2'



def test_transpose_no_token_unchanged():
    """No trailing redistribution token → version returns verbatim."""
    from utils import transpose
    assert transpose('0.7.4-20', 'asg', 1) == '0.7.4-20'
    assert transpose('2.10.4+nmu1', 'asg', 1) == '2.10.4+nmu1'        # +nmu kept
    assert transpose('2.10.4+nmu1+b1', 'asg', 1) == '2.10.4+nmu1+b1'  # trailing +b
    assert transpose('1.0-3.3', 'asg', 1) == '1.0-3.3'                # dotted rev
    assert transpose('0.7.0+really0.5.0-1', 'asg', 1) == '0.7.0+really0.5.0-1'



def test_transpose_floor_tail_tilde_preserved():
    """A constraint floor's trailing `~` (e.g. `>= 0.8.0-2+deb12u1~`) carries
    through so the transposed floor keeps the same ordering."""
    from utils import transpose
    assert transpose('0.8.0-2+deb12u1~', 'asg', 1) == '0.8.0-2+asg1u1~'



def test_strip_binNMU_splits_trailing_marker():
    """`+bN` / `~bpoN+M` split off; +deb / +nmu / dotted-rev are NOT touched."""
    from utils import strip_binNMU
    assert strip_binNMU('1.0-2+b1') == ('1.0-2', '+b1')
    assert strip_binNMU('1.0-2~bpo12+1') == ('1.0-2', '~bpo12+1')
    assert strip_binNMU('1.0-2+deb12u3+b1') == ('1.0-2+deb12u3', '+b1')
    assert strip_binNMU('1.0-2+deb12u3') == ('1.0-2+deb12u3', '')   # +deb kept
    assert strip_binNMU('2.10.4+nmu1') == ('2.10.4+nmu1', '')       # +nmu kept
    assert strip_binNMU('1.0-3.3') == ('1.0-3.3', '')              # dotted rev kept



def test_transposed_version_appends_p_and_b():
    """V = transpose(upstream) [+ +p<P>] [+ +b<bN>]."""
    from utils import transposed_version
    assert transposed_version('2.36-9+deb12u14', 'asg', 1) == '2.36-9+asg1u14'
    assert transposed_version('5.36.0-7+deb12u3', 'asg', 1, 1) == '5.36.0-7+asg1u3+p1'
    # patch / force on a pristine base anchor to u0 so they sort BELOW the
    # first upstream update (+p / +b would otherwise outrank the +asg namespace).
    assert transposed_version('5.2.15-2', 'asg', 1, 1) == '5.2.15-2+asg1u0+p1'
    assert transposed_version('1.0-2', 'asg', 1, force_bn=1) == '1.0-2+asg1u0+b1'
    # patch + force on an updated base: anchor already present (u3).
    assert transposed_version('1.0-2+deb12u3', 'asg', 1, 2, 1) == '1.0-2+asg1u3+p2+b1'



def test_compute_transposed_versions_per_binary_base_uniform_p():
    """Each binary transposes its OWN base (source-base != binary-base), with a
    uniform per-source P applied to all."""
    from utils import compute_transposed_versions
    # samba: samba-common at the source base, ldb binaries at their own base —
    # both carry +deb12u4 → both u4, with the same P.
    out = compute_transposed_versions({
        'samba-common': '2:4.17.12+dfsg-0+deb12u4',
        'libldb2': '2:2.6.2+samba4.17.12+dfsg-0+deb12u4',
    }, 'asg', 1, patch_level=1)
    assert out == {
        'samba-common': '2:4.17.12+dfsg-0+asg1u4+p1',
        'libldb2': '2:2.6.2+samba4.17.12+dfsg-0+asg1u4+p1',
    }



def test_transpose_control_text_version_deps_and_provenance():
    """Version + constraints transposed; pre-transpose upstream recorded."""
    from utils import transpose_control_text
    _ctrl = (
        'Package: apache2\n'
        'Version: 2.4.67-1~deb12u2\n'
        'Depends: libc6 (>= 2.36-9+deb12u14), perl (>= 5.36.0-7+deb12u3)\n'
        'Description: x\n'
    )
    _out, _n = transpose_control_text(_ctrl, 'asg', 1)
    assert 'Version: 2.4.67-1~asg1u2' in _out            # ~deb → ~asg
    assert 'X-Athena-Upstream-Version: 2.4.67-1~deb12u2' in _out
    assert '(>= 2.36-9+asg1u14)' in _out                 # cross-source floor transposed
    assert '(>= 5.36.0-7+asg1u3)' in _out
    assert _n >= 3



def test_transpose_control_text_strips_binnmu_from_constraint_bounds():
    """Constraint bounds referencing OTHER packages' Debian binNMU (+bN) /
    backport (~bpoN+M) versions must be STRIPPED (those versions don't exist
    in our +asg repo), while the +deb token is still transposed.  Full-universe
    audit 2026-07-09: without this, ~3.2k `-dev`→lib `(= X+bN)` pins fail to
    resolve.  Applies to ALL bound operators incl. `=` (the exact-pin case);
    the sibling `=` pins get re-stamped afterwards by transpose_deb, and
    cross-source `=` pins must land on the target's stripped repo version.
    GUARD: a bound already carrying OUR +asg force level is left untouched."""
    from utils import transpose_control_text
    _ctrl = (
        'Package: libc6-dev\n'
        'Version: 2.36-9+deb12u14\n'
        'Breaks: libasyncns-dev (<= 0.8-6+b2), foo (>= 1.0+deb12u3)\n'
        'Depends: bar (= 1.2.8-1+b1), baz (<< 3.0-1~bpo11+1)\n'
        'Conflicts: qux (<< 9.9-9+asg1u1+b1)\n'
        'Description: x\n'
    )
    _out, _n = transpose_control_text(_ctrl, 'asg', 1)
    assert '(<= 0.8-6)' in _out          # Debian binNMU stripped
    assert '(>= 1.0+asg1u3)' in _out     # +deb still transposed
    assert '(= 1.2.8-1)' in _out         # binNMU stripped from an '=' pin
    assert '(<< 3.0-1)' in _out          # ~bpo backport stripped
    # our own force-rebuild pin (+asg…+b1) is NOT stripped by the guard
    assert '(<< 9.9-9+asg1u1+b1)' in _out
    # the Version field keeps the transpose-only op (no binNMU here to strip)
    assert 'Version: 2.36-9+asg1u14' in _out
    # idempotent: a second pass changes nothing more
    _out2, _n2 = transpose_control_text(_out, 'asg', 1)
    assert _n2 == 0, (_n2, _out2)


def test_transpose_control_text_keep_binnmu_names_exempts_tunneled_targets():
    """keep_binnmu_names: a bound whose TARGET is a tunnelled binary keeps its
    Debian binNMU/backport layer — the tunnelled target ships that version
    VERBATIM (transpose_deb keeps a trailing +bN), so stripping the bound
    orphans it.  Full-universe oracle 2026-07-11: firmware-nonfree's frozen
    sibling `=` chain (firmware-linux → firmware-misc-nonfree) breaks under a
    simulated buildd +b1 without the exemption, resolves with it, and the
    exemption is a zero-difference no-op on the live universe.  Non-listed
    targets still strip; arch-qualified names match on the base name; a kept
    target's trailing +debNuK still transposes; idempotent."""
    from utils import transpose_control_text
    _keep = frozenset({'firmware-misc-nonfree', 'shim-signed-common'})
    _ctrl = (
        'Package: firmware-linux-nonfree\n'
        'Version: 20230210-5+b1\n'
        'Depends: firmware-misc-nonfree (= 20230210-5+b1), bar (= 1.2.8-1+b1)\n'
        'Recommends: shim-signed-common:amd64 (>= 1.40~bpo11+1), '
        'shim-signed-common (>= 1.44-1+deb12u1)\n'
        'Description: x\n'
    )
    _out, _n = transpose_control_text(_ctrl, 'asg', 1,
                                      keep_binnmu_names=_keep)
    # tunnelled sibling pin: +b1 KEPT (the target ships it verbatim)
    assert 'firmware-misc-nonfree (= 20230210-5+b1)' in _out
    # non-tunnelled '=' pin: still stripped (597a2ca semantics untouched)
    assert 'bar (= 1.2.8-1)' in _out
    # arch-qualified tunnelled target: backport layer kept, qualifier intact
    assert 'shim-signed-common:amd64 (>= 1.40~bpo11+1)' in _out
    # kept target with a TRAILING +deb token: still transposed
    assert 'shim-signed-common (>= 1.44-1+asg1u1)' in _out
    # own Version: trailing token is a binNMU → transpose no-op (tunnel shape)
    assert 'Version: 20230210-5+b1' in _out
    # idempotent
    _out2, _n2 = transpose_control_text(_out, 'asg', 1,
                                        keep_binnmu_names=_keep)
    assert _n2 == 0, (_n2, _out2)


def test_source_package_version_predictor():
    """MAT-02 D2: the published source's version == its binaries' version
    minus any force-+bN layer (the source never moves on a forced rebuild)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import source_package_version
    assert source_package_version('2.36-9+deb12u14', 'asg', 1) \
        == '2.36-9+asg1u14'
    assert source_package_version('5.2.15-2', 'asg', 1) == '5.2.15-2'
    assert source_package_version('5.2.15-2', 'asg', 1, 1) \
        == '5.2.15-2+asg1u0+p1'
    assert source_package_version('1:2.39.5-0+deb12u3', 'asg', 1, 2) \
        == '1:2.39.5-0+asg1u3+p2'


def test_transpose_ceiling_dot_tail_transposes():
    """Oracle-2 class 2: a punctuation-only tail after the debNuK ordinal
    (`.0` apt-ceiling, `.1~`) must not block the transpose — erlang's
    `<< X+deb12u1.0` / mosquitto's `<< X+deb12u1.1~` ceilings otherwise
    stay Debian-flavoured and wrongly ADMIT our +asg versions (asg < deb),
    allowing version skew upstream forbids."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import transpose
    assert transpose('1:25.2.3+dfsg-1+deb12u1.0', 'asg', 1) \
        == '1:25.2.3+dfsg-1+asg1u1.0'
    assert transpose('2.0.11-1.2+deb12u1.1~', 'asg', 1) \
        == '2.0.11-1.2+asg1u1.1~'
    # plain forms unchanged in behaviour
    assert transpose('2.36-9+deb12u14', 'asg', 1) == '2.36-9+asg1u14'
    assert transpose('0.8.0-2+deb12u1~', 'asg', 1) == '0.8.0-2+asg1u1~'
    # dot-tail on a NON-deb token: untouched
    assert transpose('1.0-2+b1.0', 'asg', 1) == '1.0-2+b1.0'


def test_transpose_constraint_strips_legacy_binnmu_bound():
    """Oracle-2 class 1: the legacy no-'+' binNMU form in a CONSTRAINT bound
    (gnome-contacts' `libfolks-dev (>= 0.15.5-2b)`) is stripped so the floor
    lands on the version our repo actually ships; the Version field is never
    touched by the legacy strip (bounds-only rule)."""
    from utils import transpose_control_text
    _ctrl = (
        'Package: gnome-contacts\n'
        'Version: 43.1-2b\n'                     # pathological, NOT stripped
        'Depends: libfolks-dev (>= 0.15.5-2b), other (>= 1.0-2b1)\n'
        'Description: x\n'
    )
    _out, _n = transpose_control_text(_ctrl, 'asg', 1)
    assert '(>= 0.15.5-2)' in _out
    assert 'other (>= 1.0-2)' in _out
    assert 'Version: 43.1-2b' in _out            # Version field untouched
    _out2, _n2 = transpose_control_text(_out, 'asg', 1)
    assert _n2 == 0, (_n2, _out2)


def test_transpose_deb_source_annotation_cases():
    """MAT-02 D3: the binary's Source: reference must point at the PUBLISHED
    source package's version.
      (a) tunnel: explicit source_version stamps the UPSTREAM source version
          (tunnelled sources are republished verbatim);
      (b) cross-base annotation: an upstream `Source: n (v+debNuK)` is
          transposed (+ uniform +pP);
      (c) forced rebuild: +bN is binary-only — the stamp is sans-+bN;
      (d) a re-normalise pass over (b)'s output (P=0 — the only repeated
          shape in production; transpose_deb is never re-invoked with P>0
          on its own output) is a no-op: the already-stamped annotation is
          kept verbatim, not double-suffixed."""
    import subprocess
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import transpose_deb
    if not _have_dpkg_deb():
        print("SKIP test_transpose_deb_source_annotation_cases (no dpkg-deb)")
        return

    def _mk(_tmp, tag, ctrl, fname):
        _work = os.path.join(_tmp, f'src-{tag}')
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as _fh:
            _fh.write(ctrl)
        _p = os.path.join(_tmp, fname)
        subprocess.run(['dpkg-deb', '--root-owner-group', '-b', _work, _p],
                       check=True, capture_output=True)
        return _p

    def _field(path, field):
        return subprocess.run(
            ['dpkg-deb', '-f', path, field],
            check=True, capture_output=True, text=True).stdout.strip()

    with tempfile.TemporaryDirectory() as _tmp:
        # (a) tunnel stamp: version transposes ~deb→~asg, Source pins upstream
        _a = _mk(_tmp, 'a',
                 'Package: shim-signed\nVersion: 1.44~1+deb12u1\n'
                 'Architecture: amd64\nMaintainer: T <t@l>\nDescription: x\n',
                 'shim-signed_1.44~1+deb12u1_amd64.deb')
        _ra = transpose_deb(_a, 'asg', 1, source_version='1.44~1+deb12u1')
        assert _ra['status'] == 'rewritten', _ra
        assert _field(_ra['new_path'], 'Source') \
            == 'shim-signed (1.44~1+deb12u1)'
        assert _field(_ra['new_path'], 'Version') == '1.44~1+asg1u1'
        # (b) cross-base annotation transposed + patch level
        _b = _mk(_tmp, 'b',
                 'Package: comerr-dev\nVersion: 2.1-1.47.0-2+deb12u1\n'
                 'Source: e2fsprogs (1.47.0-2+deb12u1)\n'
                 'Architecture: amd64\nMaintainer: T <t@l>\nDescription: x\n',
                 'comerr-dev_2.1-1.47.0-2+deb12u1_amd64.deb')
        _rb = transpose_deb(_b, 'asg', 1, patch_level=1)
        assert _field(_rb['new_path'], 'Source') \
            == 'e2fsprogs (1.47.0-2+asg1u1+p1)'
        assert _field(_rb['new_path'], 'Version') == '2.1-1.47.0-2+asg1u1+p1'
        # (d) re-normalise pass (P=0) over (b)'s output: no-op
        _rb2 = transpose_deb(_rb['new_path'], 'asg', 1)
        assert _rb2['status'] == 'unchanged', _rb2
        # (c) force: binary gets +bN, Source stamp is sans-+bN
        _c = _mk(_tmp, 'c',
                 'Package: relinked\nVersion: 1.0-2\n'
                 'Architecture: amd64\nMaintainer: T <t@l>\nDescription: x\n',
                 'relinked_1.0-2_amd64.deb')
        _rc = transpose_deb(_c, 'asg', 1, force_bn=1)
        assert _field(_rc['new_path'], 'Version') == '1.0-2+asg1u0+b1'
        assert _field(_rc['new_path'], 'Source') == 'relinked (1.0-2)'


def test_transpose_source_relations_build_fields_and_substvars():
    """MAT-02 D4: the SOURCE field set — Build-Depends*/Build-Conflicts* are
    transposed alongside the runtime fields in a multi-stanza source
    debian/control; substvars pass through; unrelated fields untouched;
    idempotent.  (The binary transpose path's field set is unchanged.)"""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from bump import transpose_source_relations
    _ctrl = (
        'Source: demo\n'
        'Build-Depends: debhelper-compat (= 13),\n'
        ' libfoo-dev (>= 1.2-3+deb12u1) [amd64] <!nocheck>,\n'
        ' libbar-dev (= 2.0-1+b3)\n'
        'Build-Conflicts: oldtool (<< 5.5+deb12u2)\n'
        'Build-Depends-Indep: docgen (>= 0.15.5-2b)\n'
        '\n'
        'Package: demo-bin\n'
        'Architecture: any\n'
        'Depends: ${shlibs:Depends}, demo-data (= ${source:Version}),\n'
        ' other (>= 1.0-1+deb12u1)\n'
        'Description: source-relations coverage\n'
    )
    _out, _n = transpose_source_relations(_ctrl, 'asg', 1)
    assert 'libfoo-dev (>= 1.2-3+asg1u1) [amd64] <!nocheck>' in _out
    assert 'libbar-dev (= 2.0-1)' in _out            # +b3 stripped
    assert 'oldtool (<< 5.5+asg1u2)' in _out         # Build-Conflicts too
    assert 'docgen (>= 0.15.5-2)' in _out            # legacy Nb stripped
    assert 'debhelper-compat (= 13)' in _out         # no layer → unchanged
    assert '${shlibs:Depends}' in _out               # substvars untouched
    assert 'demo-data (= ${source:Version})' in _out
    assert 'other (>= 1.0-1+asg1u1)' in _out         # binary stanza Depends
    _out2, _n2 = transpose_source_relations(_out, 'asg', 1)
    assert _n2 == 0, (_n2, _out2)


def _make_native_source(tmp, name, version, build_depends=''):
    """Fixture: a minimal 3.0 (native) source package; returns its .dsc."""
    import subprocess
    _tree = os.path.join(tmp, f'{name}-tree')
    os.makedirs(os.path.join(_tree, 'debian', 'source'))
    with open(os.path.join(_tree, 'debian', 'source', 'format'), 'w') as _f:
        _f.write('3.0 (native)\n')
    _bd = f'Build-Depends: {build_depends}\n' if build_depends else ''
    with open(os.path.join(_tree, 'debian', 'control'), 'w') as _f:
        _f.write(f'Source: {name}\nMaintainer: T <t@l>\n{_bd}\n'
                 f'Package: {name}\nArchitecture: all\n'
                 'Description: emit fixture\n fixture body\n')
    with open(os.path.join(_tree, 'debian', 'changelog'), 'w') as _f:
        _f.write(f'{name} ({version}) bookworm; urgency=medium\n\n'
                 '  * Fixture entry.\n\n'
                 ' -- Up Stream <up@stream>  Sun, 06 Jul 2026 00:00:00 +0000\n')
    subprocess.run(['dpkg-source', '-b', _tree], cwd=tmp,
                   check=True, capture_output=True)
    _noepoch = version.split(':', 1)[-1]
    return os.path.join(tmp, f'{name}_{_noepoch}.dsc')


def test_source_emit_classify_verbatim_and_reemit():
    """MAT-02 D1/D2 end-to-end on the emit engine:
      - pristine source → verbatim: bytes in out_dir identical to input
      - pristine source with a Debian-layer Build-Depends literal → DEMOTED
        to reemit (D1 demotion rule)
      - +debNuK source → reemit at the transposed version with a synthesized
        top changelog entry, transposed relations, and DETERMINISTIC output
        (two independent emits byte-identical)
      - append-only: a same-name different-bytes collision in out_dir raises"""
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import source_emit as SE
    if not _have_dpkg_deb():
        print("SKIP test_source_emit_classify_verbatim_and_reemit (no dpkg)")
        return
    with tempfile.TemporaryDirectory() as _tmp:
        # verbatim
        _dsc = _make_native_source(_tmp, 'pristine', '1.0')
        _out1 = os.path.join(_tmp, 'out1')
        _r = SE.emit_source(_dsc, _out1)
        assert _r['status'] == 'verbatim', _r
        assert _r['version'] == '1.0'
        for _f, _sha in _r['files'].items():
            assert SE._sha256(os.path.join(_tmp, _f)) == _sha, _f
        # demotion: pristine version, Debian-layer literal in Build-Depends
        _dsc2 = _make_native_source(_tmp, 'demoted', '2.0',
                                    'libx-dev (>= 1.2-3+deb12u1)')
        _r2 = SE.emit_source(_dsc2, _out1)
        assert _r2['status'] == 'emitted', _r2
        assert _r2['version'] == '2.0'
        # transposed reemit, deterministic
        _dsc3 = _make_native_source(_tmp, 'moved', '3.0+deb12u2',
                                    'liby-dev (= 4.0-1+b2)')
        _outa = os.path.join(_tmp, 'outa')
        _outb = os.path.join(_tmp, 'outb')
        _ra = SE.emit_source(_dsc3, _outa)
        _rb = SE.emit_source(_dsc3, _outb)
        assert _ra['status'] == 'emitted' and _ra['version'] == '3.0+asg1u2'
        assert 'moved_3.0+asg1u2.dsc' in _ra['files'], _ra['files']
        assert _ra['files'] == _rb['files'], 'non-deterministic emit'
        _newdsc = os.path.join(_outa, 'moved_3.0+asg1u2.dsc')
        _txt = open(_newdsc).read()
        assert 'liby-dev (= 4.0-1)' in _txt          # build-dep transposed
        assert 'Version: 3.0+asg1u2' in _txt
        # append-only conflict
        _victim = os.path.join(_outa, 'moved_3.0+asg1u2.dsc')
        with open(_victim, 'a') as _f:
            _f.write('# corrupted\n')
        try:
            SE.emit_source(_dsc3, _outa)
            raise AssertionError('same-name different-bytes must raise')
        except SE.SourceEmitError as _e:
            assert 'append-only' in str(_e)


def test_source_emit_environment_mode_helpers():
    """MAT-02 D4b: os-release ID parsing + the transpose-mode decision —
    debian container → transpose applies; the native distribution → it must
    not (callers refuse loudly on mismatch)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import source_emit as SE
    _deb = 'PRETTY_NAME="Debian GNU/Linux 12"\nID=debian\n'
    _asg = 'PRETTY_NAME="Asgard 1 (thor)"\nID=asgard\nID_LIKE=debian\n'
    assert SE.environment_id(_deb) == 'debian'
    assert SE.environment_id(_asg) == 'asgard'
    assert SE.environment_id('no id here') == ''
    assert SE.transpose_applies('debian', 'asgard') is True
    assert SE.transpose_applies('asgard', 'asgard') is False
    assert SE.transpose_applies('asgard', 'Asgard') is False


def test_source_emit_backfill_helpers():
    """MAT-02 stage 3 helpers: patch_level_from_version recovers the uniform
    +pP from a shipped version; find_dsc resolves across search dirs and
    picks the highest version; apply_emit_to_record folds results under
    SEPARATE keys (never outputs/output_hashes — those are .deb-routed)."""
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import source_emit as SE
    assert SE.patch_level_from_version('2.36-9+asg1u14+p3') == 3
    assert SE.patch_level_from_version('5.2.15-2+asg1u0+p1') == 1
    assert SE.patch_level_from_version('1.0-2') == 0
    assert SE.patch_level_from_version('') == 0
    with tempfile.TemporaryDirectory() as _tmp:
        _a = os.path.join(_tmp, 'a')
        _b = os.path.join(_tmp, 'b')
        os.makedirs(_a)
        os.makedirs(_b)
        for _f in ('demo_1.0-1.dsc',):
            open(os.path.join(_a, _f), 'w').write('x')
        for _f in ('demo_1.0-2.dsc', 'other_9.9.dsc'):
            open(os.path.join(_b, _f), 'w').write('x')
        _hit = SE.find_dsc('demo', [_a, _b])
        assert _hit and _hit.endswith('demo_1.0-2.dsc'), _hit
        assert SE.find_dsc('missing', [_a, _b]) is None
    _rec = {'outputs': ['x.deb'], 'output_hashes': {'x.deb': 'aa'}}
    _res = {'status': 'emitted', 'class': 'reemit',
            'files': {'demo_1.0-2+asg1u1.dsc': 'bb',
                      'demo_1.0.orig.tar.gz': 'cc'}}
    SE.apply_emit_to_record(_rec, _res)
    assert _rec['outputs'] == ['x.deb']              # .deb keys untouched
    assert _rec['output_hashes'] == {'x.deb': 'aa'}
    assert _rec['source_outputs'] == ['demo_1.0-2+asg1u1.dsc',
                                      'demo_1.0.orig.tar.gz']
    assert _rec['source_output_hashes']['demo_1.0-2+asg1u1.dsc'] == 'bb'
    assert _rec['source_emit_class'] == 'reemit'


def test_tunneled_binary_names_resolves_from_cache():
    """utils.tunneled_binary_names maps every [Source] Tunneled source to its
    binary names via cache.source_hashtable (the keep_binnmu_names feed for
    the transpose call sites); empty-safe on a missing cache/config and on
    sources the cache doesn't know."""
    import types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    _src = types.SimpleNamespace(
        binary=['firmware-misc-nonfree', 'firmware-linux'])
    _cache = types.SimpleNamespace(
        source_hashtable={'firmware-nonfree': [_src]})
    _cfg = types.SimpleNamespace(tunnel_packages=['firmware-nonfree', 'ghost'])
    assert _u.tunneled_binary_names(_cfg, _cache) == frozenset(
        {'firmware-misc-nonfree', 'firmware-linux'})
    assert _u.tunneled_binary_names(_cfg, None) == frozenset()
    assert _u.tunneled_binary_names(None, _cache) == frozenset()


def test_transpose_control_text_no_token_is_noop_no_provenance():
    """A pristine / +nmu version is unchanged and gets no X-upstream field."""
    from utils import transpose_control_text
    _out, _n = transpose_control_text(
        'Package: cupt\nVersion: 2.10.4+nmu1\nDescription: x\n', 'asg', 1)
    assert 'Version: 2.10.4+nmu1' in _out
    assert 'X-Athena-Upstream-Version:' not in _out
    assert _n == 0



def test_transpose_scheme_boundary_table_and_ordering():
    """Immutable-scheme regression: pin the worked-example outputs AND the
    resulting dpkg ordering chain.  Self-contained (no cache/repo dependency) so
    it guards the frozen behaviour on every run.  A change that breaks any row
    or any inequality re-orders already-shipped releases — that is the failure
    this test exists to catch."""
    import shutil as _sh
    if not _sh.which('dpkg'):
        return
    from utils import transposed_version as _T
    # (upstream, patched? -> patch_level, expected) from docs/versioning-mechanics.md.
    _table = [
        ('0.7.4-20',            0, '0.7.4-20'),                 # pristine, faithful
        ('1.2.3-4+deb12u3',     0, '1.2.3-4+asg1u3'),           # faithful update
        ('1.2.3-4+deb12u4',     0, '1.2.3-4+asg1u4'),           # later update
        ('1.2.3-4+deb12u2',     0, '1.2.3-4+asg1u2'),           # lower (downgrade target)
        ('2.4.67-1~deb12u2',    0, '2.4.67-1~asg1u2'),          # ~deb -> ~asg (below pristine)
        ('7:5.1.9-0+deb12u1',   0, '7:5.1.9-0+asg1u1'),         # epoch preserved
        ('1.2.3-4',             1, '1.2.3-4+asg1u0+p1'),        # patch on pristine (anchored)
        ('1.2.3-4+deb12u3',     1, '1.2.3-4+asg1u3+p1'),        # patch on an update
        # embedded update marker kept; only the trailing one translates
        ('1.44~1+deb12u1+15.8-1~deb12u1', 0, '1.44~1+deb12u1+15.8-1~asg1u1'),
    ]
    for _up, _p, _exp in _table:
        assert _T(_up, 'asg', 1, _p) == _exp, (_up, _p, _T(_up, 'asg', 1, _p))

    import subprocess

    def _lt(_a, _b):
        return subprocess.run(['dpkg', '--compare-versions', _a, 'lt', _b]).returncode == 0

    # ~asg sits BELOW its own pristine base (the chosen ~deb policy).
    assert _lt('2.4.67-1~asg1u2', '2.4.67-1')
    # The +asg update chain, including patch levels, is strictly increasing.
    _asg_chain = [
        '1.2.3-4',
        '1.2.3-4+asg1u0+p1',
        '1.2.3-4+asg1u2',
        '1.2.3-4+asg1u3',
        '1.2.3-4+asg1u3+p1',
        '1.2.3-4+asg1u4',
    ]
    # pairwise walk — the offset slice is one shorter by design
    for _lo, _hi in zip(_asg_chain, _asg_chain[1:], strict=False):
        assert _lt(_lo, _hi), f"ordering broken: {_lo} !< {_hi}"
    # A forced rebuild on a pristine base is anchored too (sorts below u1).
    assert _T('1.0-2', 'asg', 1, 0, 1) == '1.0-2+asg1u0+b1'
    assert _lt('1.0-2+asg1u0+b1', '1.0-2+asg1u1')



def test_decide_patch_bump_count_branches():
    """Every branch of the patch-level (P) decision against the prior record:
    first build, version change (reset), patch-set change (++), patch removed
    (->0), idempotent rebuild (reuse), and defensive defaults."""
    from utils import decide_patch_bump_count as D, EMPTY_PATCH_SET_HASH as E
    _H, _H2 = 'deadbeef', 'cafef00d'
    # First build (no prior): patched -> 1, unpatched -> 0.
    assert D(None, '1.0-1', _H) == 1
    assert D(None, '1.0-1', E) == 0
    _prior = {'intended_version': '1.0-1', 'patch_set_hash': _H,
              'patch_bump_count': 3}
    # Source version changed -> reset (1 if patched, else 0).
    assert D(_prior, '1.0-2', _H) == 1
    assert D(_prior, '1.0-2', E) == 0
    # Same version, our patch CHANGED -> advance.
    assert D(_prior, '1.0-1', _H2) == 4
    # Same version, patch REMOVED -> back to faithful 0.
    assert D(_prior, '1.0-1', E) == 0
    # Same version, same patch -> reuse (idempotent rebuild).
    assert D(_prior, '1.0-1', _H) == 3
    # Defensive: missing counter defaults to 0 then advances; non-int tolerated;
    # an empty-dict prior is treated as no-prior.
    assert D({'intended_version': '1.0-1', 'patch_set_hash': _H}, '1.0-1', _H2) == 1
    assert D({'intended_version': '1.0-1', 'patch_set_hash': _H,
              'patch_bump_count': 'x'}, '1.0-1', _H2) == 1
    assert D({}, '1.0-1', _H) == 1



def test_strip_build_version_rejects_malformed_filename():
    """Filenames not in `name_version_arch.ext` shape raise ValueError."""
    from utils import strip_build_version
    for bad in ('not-a-deb.deb', 'one_two.deb', 'a_b_c_d_amd64.deb'):
        try:
            strip_build_version(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")



def test_parse_deb_filename_splits_raw_components():
    """ARCH-19 shared splitter → (name, raw-version, arch, ext).  The version
    stays RAW (epoch encoded %3a) and any directory prefix is preserved so the
    callers that reconstruct a filename/pool path round-trip byte-exactly."""
    from utils import parse_deb_filename
    assert parse_deb_filename('foo_1.0-2_amd64.deb') == ('foo', '1.0-2', 'amd64', '.deb')
    assert parse_deb_filename('bar_1%3a2.3-4_arm64.udeb') == (
        'bar', '1%3a2.3-4', 'arm64', '.udeb')          # epoch NOT decoded
    assert parse_deb_filename('pool/main/f/foo/foo_1-2_amd64.deb') == (
        'pool/main/f/foo/foo', '1-2', 'amd64', '.deb')  # path prefix kept



def test_parse_deb_filename_returns_none_on_mismatch():
    """None on a non-.deb/.udeb or a wrong part-count — each caller maps the
    miss to its own convention (skip / raise / status dict / sentinel)."""
    from utils import parse_deb_filename
    assert parse_deb_filename('Packages.gz') is None
    assert parse_deb_filename('foo_1_amd64') is None        # no .deb/.udeb ext
    assert parse_deb_filename('not-a-deb.deb') is None      # 1 part
    assert parse_deb_filename('one_two.deb') is None        # 2 parts
    assert parse_deb_filename('a_b_c_d_amd64.deb') is None  # 5 parts



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



def test_repack_deb_clamps_to_source_date_epoch_for_reproducibility():
    """Regression (2026-06): the strip/restamp repack MUST clamp the ar
    member + data.tar root timestamps to SOURCE_DATE_EPOCH, not build time.

    Without it, `dpkg-deb -b` stamped wrapper timestamps with the current
    time, so every NMU/asg rebuild produced different bytes than the
    published mirror even though the package content was identical — a mass
    hash-divergence that blocks a clean re-publish.  Assert (a) same epoch →
    identical bytes (reproducible) and (b) the epoch actually drives the
    output (different epoch → different bytes)."""
    import hashlib
    import os
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import bump

    def _sha(p):
        return hashlib.sha256(open(p, 'rb').read()).hexdigest()

    with tempfile.TemporaryDirectory() as _w:
        _tree = os.path.join(_w, 'pkg')
        os.makedirs(os.path.join(_tree, 'DEBIAN'))
        os.makedirs(os.path.join(_tree, 'usr', 'share', 'foo'))
        with open(os.path.join(_tree, 'usr', 'share', 'foo', 'data'), 'w') as _f:
            _f.write('x' * 256)
        with open(os.path.join(_tree, 'DEBIAN', 'control'), 'w') as _f:
            _f.write('Package: foo\nVersion: 1.0-1\nArchitecture: amd64\n'
                     'Maintainer: t <t@t>\nDescription: t\n')
        _epoch = 1673995855          # 2023-01-17
        _a1 = os.path.join(_w, 'a1.deb'); bump._repack_deb(_tree, _a1, _epoch)
        _a2 = os.path.join(_w, 'a2.deb'); bump._repack_deb(_tree, _a2, _epoch)
        _b = os.path.join(_w, 'b.deb'); bump._repack_deb(_tree, _b, _epoch + 10**6)
        assert _sha(_a1) == _sha(_a2), (
            "repack not reproducible: same SOURCE_DATE_EPOCH gave different bytes")
        assert _sha(_a1) != _sha(_b), (
            "SOURCE_DATE_EPOCH not honoured: wrapper timestamp ignores the epoch")



def test_bump_is_canonical_home_for_version_logic():
    """The version/bump primitives live in `bump`; `utils` only re-exports
    them.  Assert identity so a future accidental redefinition in utils
    (a divergent fork of the logic) is caught immediately."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import bump
    import utils
    for _name in ('strip_nmu_suffix', 'strip_nmu_from_deb', 'transpose',
                  'transposed_version', 'transpose_deb', 'pristine_base',
                  'decide_patch_bump_count', 'normalize_repo_filename',
                  'find_matching_artifact', 'parse_asg_suffix'):
        assert getattr(utils, _name) is getattr(bump, _name), (
            f"utils.{_name} is not bump.{_name} — version logic has forked "
            f"out of its single reviewable home (scripts/bump.py)")
    # bump must not depend on utils (keeps the import graph acyclic)
    import inspect
    _src = inspect.getsource(bump)
    assert 'import utils' not in _src and 'from utils' not in _src, (
        "bump.py must not import utils — dependency is one-way utils -> bump")



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



def test_strip_nmu_inserts_x_athena_upstream_version_when_stripped():
    """When a Version field carries an NMU suffix that strip rewrites,
    `X-Athena-Upstream-Version:` is inserted right after the (new)
    Version line carrying the original pre-strip version.  Preserves
    CVE-tracking provenance — see docs/cve-tracking.md."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_from_control_text
    _ctrl = (
        'Package: libfoo\n'
        'Version: 1.0-2+deb12u3\n'
        'Architecture: amd64\n'
    )
    _out, _n = strip_nmu_from_control_text(_ctrl)
    assert _n >= 1
    assert 'Version: 1.0-2\n' in _out
    assert (
        'X-Athena-Upstream-Version: 1.0-2+deb12u3\n' in _out
    ), f'X-field not inserted after strip: {_out!r}'
    # Field must sit DIRECTLY after the Version line, not elsewhere.
    _lines = _out.splitlines()
    _v_idx = next(i for i, ln in enumerate(_lines)
                  if ln.startswith('Version: '))
    assert _lines[_v_idx + 1].startswith('X-Athena-Upstream-Version: '), (
        f'X-field not adjacent to Version line: {_lines}'
    )



def test_strip_nmu_omits_x_field_for_pristine_version():
    """A control text whose Version is already pristine doesn't get
    the X-field — pristine sources need no record (Version IS the
    upstream version).  Idempotent: re-stripping a pristine .deb is a
    no-op, no provenance pollution."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_from_control_text
    _ctrl = (
        'Package: libfoo\n'
        'Version: 1.0-2\n'
        'Architecture: amd64\n'
    )
    _out, _n = strip_nmu_from_control_text(_ctrl)
    assert _n == 0
    assert 'X-Athena-Upstream-Version:' not in _out



def test_strip_nmu_x_field_idempotent_on_already_stamped():
    """A control text that ALREADY has `X-Athena-Upstream-Version:`
    (e.g. a re-strip of a previously-stripped .deb) doesn't double-
    insert the field."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import strip_nmu_from_control_text
    _ctrl = (
        'Package: libfoo\n'
        'Version: 1.0-2\n'
        'X-Athena-Upstream-Version: 1.0-2+deb12u3\n'
        'Architecture: amd64\n'
    )
    _out, _ = strip_nmu_from_control_text(_ctrl)
    # Exactly one X-field after the round-trip.
    assert _out.count('X-Athena-Upstream-Version:') == 1



def test_strip_nmu_pair_rewrite_collapses_sibling_idiom():
    """The upstream `X (>> V), X (<< V-.)` "any debrev of upstream V"
    idiom is collapsed to `X (= our_version)` at strip time.  After
    strip-NMU, our siblings ship at `V-0` which apt treats as equal to
    bare V (Policy §5.6.12), so the >> half of the original fails.
    The rewrite locks the constraint to our own atomic-source-build
    version, matching the invariant our pipeline actually preserves.

    See docs/versioning-mechanics.md (Appendix B) for the full
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



def test_transpose_deb_round_trip():
    """End-to-end transpose+stamp: a built .deb with a +deb Version, a patched
    source (P=1), a cross-source floor and a same-source sibling pin →
    transpose to +asg, append +p1, restamp the sibling to the exact final,
    transpose the cross-source floor, preserve epoch."""
    import subprocess, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import transpose_deb
    try:
        subprocess.run(['dpkg-deb', '--version'],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("SKIP test_transpose_deb_round_trip (no dpkg-deb)")
        return
    with tempfile.TemporaryDirectory() as _tmp:
        _work = os.path.join(_tmp, 'src')
        os.makedirs(os.path.join(_work, 'DEBIAN'))
        os.makedirs(os.path.join(_work, 'usr', 'lib'))
        with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(
                'Package: git\n'
                'Version: 1:2.39.5-0+deb12u3\n'                 # epoch + trailing +deb
                'Architecture: amd64\n'
                'Maintainer: T <t@l>\n'
                # sibling pin (same source, at this binary's version) +
                # cross-source floor.
                'Depends: git-man (= 1:2.39.5-0+deb12u3), libc6 (>= 2.36-9+deb12u14)\n'
                'Description: transpose round-trip\n'
            )
        with open(os.path.join(_work, 'usr', 'lib', 'p'), 'w') as fh:
            fh.write('x\n')
        _orig = os.path.join(_tmp, 'git_2.39.5-0+deb12u3_amd64.deb')  # epoch dropped in fn
        subprocess.run(
            ['dpkg-deb', '--root-owner-group', '-b', _work, _orig],
            check=True, capture_output=True,
        )

        _r = transpose_deb(_orig, 'asg', 1, patch_level=1)
        assert _r['status'] == 'rewritten', _r
        assert _r['version'] == '1:2.39.5-0+asg1u3+p1', _r
        assert (os.path.basename(_r['new_path'])
                == 'git_2.39.5-0+asg1u3+p1_amd64.deb'), _r['new_path']
        _ver = subprocess.run(
            ['dpkg-deb', '-f', _r['new_path'], 'Version'],
            check=True, capture_output=True, text=True).stdout.strip()
        assert _ver == '1:2.39.5-0+asg1u3+p1', f"epoch/version? {_ver!r}"
        _deps = subprocess.run(
            ['dpkg-deb', '-f', _r['new_path'], 'Depends'],
            check=True, capture_output=True, text=True).stdout.strip()
        # sibling pin restamped to the EXACT final version (incl. +p1)
        assert 'git-man (= 1:2.39.5-0+asg1u3+p1)' in _deps, _deps
        # cross-source floor transposed (no +p — not a sibling)
        assert 'libc6 (>= 2.36-9+asg1u14)' in _deps, _deps
        assert '+deb12u' not in _deps, f"deb residue: {_deps!r}"
        # provenance recorded
        _xup = subprocess.run(
            ['dpkg-deb', '-f', _r['new_path'], 'X-Athena-Upstream-Version'],
            check=True, capture_output=True, text=True).stdout.strip()
        assert _xup == '1:2.39.5-0+deb12u3', _xup



def test_transpose_deb_tunnel_keeps_binnmu_and_rewrites_trailing_deb():
    """Coverage gap (audit #15): the TUNNEL call shape of transpose_deb (no
    patch_level / force_bn / sibling_names — the firmware/microcode path).
      (a) a tunnelled binNMU `X+debNuK+bN`: the trailing token is a binNMU, so
          the transpose is a NO-OP on the EMBEDDED +deb and the frozen +bN
          inter-binary pin is KEPT (status 'unchanged').
      (b) a tunnel whose trailing token IS a `+debNuK` rewrites to `+asg<R>uK`.
    A trailing-token-regex regression would silently corrupt a frozen +bN pin
    on a real production path; this is the highest-value of the three gaps."""
    import subprocess
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import transpose_deb
    if not _have_dpkg_deb():
        print("SKIP test_transpose_deb_tunnel_keeps_binnmu (no dpkg-deb)")
        return
    with tempfile.TemporaryDirectory() as _tmp:
        # (a) tunnelled binNMU — unchanged, +b1 AND embedded +deb12u3 retained.
        _a = _build_test_deb(_tmp, 'shim-signed', '1.0-2+deb12u3+b1',
                             'shim-signed_1.0-2+deb12u3+b1_amd64.deb')
        _ra = transpose_deb(_a, 'asg', 1)               # tunnel shape
        assert _ra['status'] == 'unchanged', _ra
        _va = subprocess.run(['dpkg-deb', '-f', _ra['new_path'], 'Version'],
                             check=True, capture_output=True,
                             text=True).stdout.strip()
        assert _va == '1.0-2+deb12u3+b1', _va
        assert (os.path.basename(_ra['new_path'])
                == 'shim-signed_1.0-2+deb12u3+b1_amd64.deb'), _ra['new_path']
        # (b) trailing +debNuK rewrites to +asg<R>uK.
        _b = _build_test_deb(_tmp, 'shim-signed', '1.0-2+deb12u3',
                             'shim-signed_1.0-2+deb12u3_amd64.deb')
        _rb = transpose_deb(_b, 'asg', 1)
        assert _rb['status'] == 'rewritten', _rb
        assert _rb['version'] == '1.0-2+asg1u3', _rb
        assert (os.path.basename(_rb['new_path'])
                == 'shim-signed_1.0-2+asg1u3_amd64.deb'), _rb['new_path']



def test_transpose_deb_tunnel_keep_binnmu_preserves_frozen_sibling_pin():
    """Tunnel call shape + keep_binnmu_names: a tunnelled deb whose frozen
    sibling `=` pin carries the buildd +b1 keeps the pin VERBATIM — both
    sides ship +b1, so the deb is byte-stable ('unchanged').  Without the
    keep set the constraint strip rewrites the pin to a version the sibling
    never ships (the transpose_deb docstring's frozen-pin rationale defeated
    by 597a2ca's strip) — pinned in both directions so neither regresses."""
    import subprocess
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from utils import transpose_deb
    if not _have_dpkg_deb():
        print("SKIP test_transpose_deb_tunnel_keep_binnmu_pin (no dpkg-deb)")
        return
    _pin = 'firmware-misc-nonfree (= 20230210-5+b1)'
    with tempfile.TemporaryDirectory() as _tmp:
        for _sub, _keep, _want_status, _want_dep in (
                ('keep', frozenset({'firmware-misc-nonfree'}), 'unchanged',
                 _pin),
                ('strip', None, 'rewritten',
                 'firmware-misc-nonfree (= 20230210-5)')):
            _work = os.path.join(_tmp, f'src-{_sub}')
            os.makedirs(os.path.join(_work, 'DEBIAN'))
            with open(os.path.join(_work, 'DEBIAN', 'control'), 'w') as _fh:
                _fh.write('Package: firmware-linux\n'
                          'Version: 20230210-5+b1\n'
                          'Architecture: all\nMaintainer: T <t@l>\n'
                          f'Depends: {_pin}\n'
                          'Description: frozen sibling pin\n')
            _p = os.path.join(_tmp, f'{_sub}-firmware-linux_20230210-5+b1_all.deb')
            subprocess.run(['dpkg-deb', '--root-owner-group', '-b', _work, _p],
                           check=True, capture_output=True)
            _r = transpose_deb(_p, 'asg', 1, keep_binnmu_names=_keep)
            assert _r['status'] == _want_status, (_sub, _r)
            _deps = subprocess.run(
                ['dpkg-deb', '-f', _r['new_path'], 'Depends'],
                check=True, capture_output=True, text=True).stdout.strip()
            assert _want_dep in _deps, (_sub, _deps)



def test_transpose_fork_athena_version_is_noop():
    """Coverage gap (audit #16): a fork version (Model A — changelog-verbatim,
    ending +athenaN with an EMBEDDED +deb before it) must pass through transpose
    unchanged: no trailing +debNuK, so no rewrite, and a faithful (was_patched=
    False) build adds no +asg / +asg1u0+p1.  Pinned at the pure-transpose level
    AND end-to-end through transpose_deb (the per-binary transform
    _normalize_built_artifacts runs)."""
    import subprocess
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    # (a) pure transpose: the embedded +deb followed by +athenaN is left alone.
    assert utils.transpose('12.4+deb12u14+athena1', 'asg', 1) == \
        '12.4+deb12u14+athena1'
    assert utils.transpose('12.4+athena2', 'asg', 1) == '12.4+athena2'
    # (b) end-to-end on a fork .deb (faithful build): filename + control Version
    # stay +athena1 — no +asg, no +asg1u0+p1.
    if not _have_dpkg_deb():
        print("SKIP test_transpose_fork_athena_version_is_noop (no dpkg-deb)")
        return
    with tempfile.TemporaryDirectory() as _tmp:
        _f = _build_test_deb(
            _tmp, 'athena-base-files', '12.4+deb12u14+athena1',
            'athena-base-files_12.4+deb12u14+athena1_amd64.deb')
        _r = utils.transpose_deb(_f, 'asg', 1)          # patch_level=0 (faithful)
        assert _r['status'] == 'unchanged', _r
        _v = subprocess.run(['dpkg-deb', '-f', _r['new_path'], 'Version'],
                            check=True, capture_output=True,
                            text=True).stdout.strip()
        assert _v == '12.4+deb12u14+athena1', _v
        assert (os.path.basename(_r['new_path'])
                == 'athena-base-files_12.4+deb12u14+athena1_amd64.deb'), \
            _r['new_path']



def test_restamp_sibling_pins_force_bn_branch():
    """Coverage gap (audit #17): the force-binNMU branch of _restamp_sibling_pins
    (only the +p1 patch tail was covered).  A force build (force_bn set) restamps
    every same-source sibling `=` pin (already transposed) with the +bN tail —
    anchored to +asg<R>u0 on a pristine base — while a non-sibling pin is left
    untouched.  Note: bn_bump_count is only ever 0 at runtime today, so the
    force path is shipped-but-inert; this pins the wiring."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import bump
    _content = ('Depends: libX (= 1.0-2+asg1u3), libY (= 1.0-2), '
                'other (= 1.0-2+asg1u3)\n')
    # force_bn=1, patch_level=0
    _out = bump._restamp_sibling_pins(
        _content, {'libX', 'libY'}, 0, 1, 'asg', 1)
    assert 'libX (= 1.0-2+asg1u3+b1)' in _out, _out     # asg marker → +b1
    assert 'libY (= 1.0-2+asg1u0+b1)' in _out, _out     # pristine → u0 anchor
    assert 'other (= 1.0-2+asg1u3)' in _out, _out       # non-sibling untouched
    # combined patch + force: +pP then +bN, in that order.
    _out2 = bump._restamp_sibling_pins(
        'Depends: libX (= 1.0-2+asg1u3)\n', {'libX'}, 2, 1, 'asg', 1)
    assert 'libX (= 1.0-2+asg1u3+p2+b1)' in _out2, _out2
    # no patch + no force = no-op (returns content unchanged).
    assert bump._restamp_sibling_pins(
        _content, {'libX'}, 0, None, 'asg', 1) == _content



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
    # The transpose scheme also ships patch (+pP) / force (+bN) tails and the
    # below-pristine ~asg form — all must reduce to the same base, or every
    # patched/fork package mis-groups against its pristine line.
    assert pristine_base('3.0.15-1+asg1u0+p1') == '3.0.15-1'    # patch on pristine
    assert pristine_base('3.0.15-1+asg1u3+p1') == '3.0.15-1'
    assert pristine_base('3.0.15-1+asg1u3+p1+b1') == '3.0.15-1'
    assert pristine_base('3.0.15-1+asg1u0+b1') == '3.0.15-1'    # force on pristine
    assert pristine_base('2.4.67-1~asg1u2+p1') == '2.4.67-1'    # ~asg form
    from utils import parse_asg_suffix
    assert parse_asg_suffix('3.0.15-1+asg1u3+p1') == (1, 3)     # tail ignored
    assert parse_asg_suffix('2.4.67-1~asg1u2') == (1, 2)
    assert parse_asg_suffix('3.0.15-1+deb12u2') is None



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

    # COMPOUND version (backport-of-an-update): the predictor strips to bare
    # pristine but transpose() keeps the EMBEDDED +debN, rewriting only the
    # trailing ~debM.  match must reconcile the two via pristine base — else a
    # present, correctly-transposed binary reads as missing (libhttp-daemon-
    # perl 6.16-1+deb13u1~deb12u1 -> 6.16-1+deb13u1~asg1u1 audited stale_pass,
    # 2026-07-09).
    _cp = 'libhttp-daemon-perl_6.16-1_all.deb'
    assert match_pristine_base(
        _cp, 'libhttp-daemon-perl_6.16-1+deb13u1~asg1u1_all.deb')
    # embedded +deb that survives strip_nmu on BOTH sides still matches
    _shim = 'shim-signed_1.44~1+deb12u1+15.8-1_amd64.deb'
    assert match_pristine_base(
        _shim, 'shim-signed_1.44~1+deb12u1+15.8-1~asg1u1_amd64.deb')
    # but a different pristine base still rejects even with an asg layer
    assert not match_pristine_base(
        _cp, 'libhttp-daemon-perl_6.17-1+deb13u1~asg1u1_all.deb')



def test_version_module_resolution():
    """_version is the single source of truth for the TOOLCHAIN version.
    base_version() is the stable SemVer base (no git suffix); get_version() is
    provenance-exact (carries +g<sha> until a v* tag exists)."""
    import re as _re
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import _version
    # base is a plain SemVer X.Y.Z (don't pin a literal — it bumps on release)
    assert _re.fullmatch(r'\d+\.\d+\.\d+', _version.base_version()), \
        _version.base_version()
    _v = _version.get_version()
    assert isinstance(_v, str) and _v
    # full version starts from the SemVer base (exactly it when on a tag, or
    # <base>+g<sha> / <base>-N-g<sha> between/after tags)
    assert _v.startswith(_version.base_version()) or \
        _re.match(r'\d+\.\d+\.\d+', _v), _v
    assert _version.version_line().startswith('athena-build '), \
        _version.version_line()
    _vb = _version.version_line(verbose=True)
    assert 'athena-build' in _vb and 'python' in _vb, _vb



def test_version_base_matches_pyproject():
    """pyproject [project].version and _version._BASE_VERSION must stay in
    lockstep — bump_version.py rewrites both; this guards against drift."""
    import re as _re
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import _version
    with open(os.path.join(_ROOT, 'pyproject.toml')) as _fh:
        _txt = _fh.read()
    _m = _re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', _txt)
    assert _m, "no [project].version found in pyproject.toml"
    assert _m.group(1) == _version._BASE_VERSION, \
        (f"pyproject {_m.group(1)!r} != _version._BASE_VERSION "
         f"{_version._BASE_VERSION!r} — keep them in lockstep")



def test_version_git_describe_matches_only_version_tags():
    """The repo carries descriptive savepoint tags (working-branding-*) that a
    bare `git describe` would emit AS the version; _from_git MUST restrict to
    v[0-9]* tags.  Source-pin so the guard can't be silently dropped."""
    import inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import _version
    _src = inspect.getsource(_version._from_git)
    assert '--match' in _src and 'v[0-9]*' in _src, _src



def test_version_command_registered_and_user_agent_derived():
    """`version` is a real dispatcher noun, allowed before configure, has a
    handler, and the HTTP User-Agent derives from the SAME single source — no
    hardcoded '0.1' literal that could drift."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import _version
    import onboarding
    import utils
    assert 'version' in onboarding.ALLOW_BEFORE_CONFIGURE
    assert utils._HTTP_HEADERS['User-Agent'] == \
        f'athena-build/{_version.base_version()}', utils._HTTP_HEADERS
    with open(os.path.join(_ROOT, 'scripts', 'build.py')) as _fh:
        assert "register_command('version'" in _fh.read()
    with open(os.path.join(_ROOT, 'scripts', 'commands', 'cmd_run.py')) as _fh:
        assert 'def cmd_version' in _fh.read()



def test_bump_version_next_computation():
    """bump_version._next_version implements SemVer part-bumps + explicit set,
    and refuses non-SemVer input."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import bump_version
    assert bump_version._next_version('0.1.0', 'major') == '1.0.0'
    assert bump_version._next_version('0.1.0', 'minor') == '0.2.0'
    assert bump_version._next_version('0.1.0', 'patch') == '0.1.1'
    assert bump_version._next_version('1.4.9', 'patch') == '1.4.10'
    assert bump_version._next_version('0.1.0', '2.3.4') == '2.3.4'
    for _bad in ('1.2', 'xyz', 'v0.1.0'):
        try:
            bump_version._next_version('0.1.0', _bad)
            raise AssertionError(f"expected SystemExit for bump {_bad!r}")
        except SystemExit:
            pass
    # a non-SemVer *current* version is refused too
    try:
        bump_version._next_version('0.1.0-14-gdead', 'patch')
        raise AssertionError("expected SystemExit for non-SemVer current")
    except SystemExit:
        pass



def test_bump_version_rewrite_patterns_match_real_files():
    """The bump rewrites pyproject [project].version and _version._BASE_VERSION
    in lockstep; both regexes must match EXACTLY ONCE in the real files, or a
    reformat would make a release silently no-op."""
    import re as _re
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with open(os.path.join(_ROOT, 'pyproject.toml')) as _fh:
        _, _n1 = _re.subn(r'(?m)^(version\s*=\s*")[^"]+(")', r'\g<1>X\g<2>',
                          _fh.read(), count=1)
    assert _n1 == 1, "pyproject version-rewrite pattern did not match once"
    with open(os.path.join(_ROOT, 'scripts', '_version.py')) as _fh:
        _, _n2 = _re.subn(r'(_BASE_VERSION\s*=\s*")[^"]+(")', r'\g<1>X\g<2>',
                          _fh.read(), count=1)
    assert _n2 == 1, "_version._BASE_VERSION rewrite pattern did not match once"



def test_bump_version_freeze_stamp_excludes_gitignored_buildstamp():
    """Regression (audit #9): --freeze-stamp must NOT stage the gitignored
    scripts/_buildstamp.py for the release commit.  `git add` on an ignored
    path exits 1, which aborted the release mid-way (pyproject + _version.py
    rewritten, stamp written, but no commit and no tag).  Drive main() with
    git/file ops stubbed and assert the recorded `git add` excludes the stamp
    while still writing it to disk."""
    import sys
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import bump_version
    _calls = []

    def _fake_git(*args):
        _calls.append(args)
        return ''                       # status --porcelain -> '' (clean tree)

    with mock.patch.object(bump_version, '_git', side_effect=_fake_git), \
         mock.patch.object(bump_version, '_git_ok', return_value=False), \
         mock.patch.object(bump_version, '_current_version',
                           return_value='1.2.3'), \
         mock.patch.object(bump_version, '_rewrite'), \
         mock.patch.object(bump_version, '_write_buildstamp') as _ws:
        _rc = bump_version.main(['patch', '--freeze-stamp', '--no-tag'])

    assert _rc == 0
    assert _ws.called, "freeze-stamp must still write the stamp to disk"
    _add = next(c for c in _calls if c and c[0] == 'add')
    assert bump_version._BUILDSTAMP_PY not in _add, (
        "gitignored _buildstamp.py must not be staged for the release commit "
        f"(git add on it exits 1 and aborts the release); add args: {_add}")
    assert bump_version._PYPROJECT in _add and bump_version._VERSION_PY in _add



def test_build_record_carries_toolchain_version():
    """new_build_record stamps the producing Athena-Build version (provenance);
    additive + informational, so it must not perturb the existing fields."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import _version
    import utils as _u
    _rec = _u.new_build_record(
        package='foo', intended_version='1.0', patch_set_hash='ab' * 32)
    assert _rec['athena_build_version'] == _version.get_version()
    assert _rec['athena_build_version'].startswith(_version.base_version())
    # existing load-bearing fields still present + unchanged
    assert _rec['schema_version'] == _u.BUILD_RECORD_SCHEMA_VERSION
    assert _rec['package'] == 'foo' and _rec['phase'] == 'entry'



def test_get_version_is_cached():
    """get_version() caches — provenance stampers call it per build record / per
    Release without re-shelling to git each time."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import _version
    _a = _version.get_version()
    _b = _version.get_version()
    assert _a == _b and _a
    assert _version._CACHED_VERSION == _a



def test_http_session_is_module_level_singleton():
    """Two _http_session() calls in the same process must return the
    SAME Session instance — that's what gives us HTTP keep-alive
    across calls.  If a future refactor changes this to construct
    a new Session per call, snapshot.debian.org sync goes from
    fast back to ~15min of TLS handshake overhead."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import _version as _ver
    import utils as _u
    _a = _u._http_session()
    _b = _u._http_session()
    assert _a is _b, "expected same Session instance, got distinct objects"
    # Session must carry our User-Agent so per-call headers aren't required.
    # Derived from _version.base_version() (the single source) — not a hardcoded
    # literal that would silently drift from the real UA on a version bump.
    assert _a.headers.get('User-Agent') == f'athena-build/{_ver.base_version()}'



def test_bump_version_freeze_stamp_commits_without_gitignored_stamp():
    """Audit #42: bump_version --freeze-stamp rewrites pyproject + _version.py,
    writes the gitignored _buildstamp.py to disk, and COMMITS the release
    (the stamp is excluded from the commit set, so a gitignored `git add`
    can't abort the release mid-way — the #9 regression)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import bump_version as _bv
    import subprocess as _sp
    with tempfile.TemporaryDirectory() as _d:
        _scripts = os.path.join(_d, 'scripts')
        os.makedirs(_scripts)
        with open(os.path.join(_d, 'pyproject.toml'), 'w') as _fh:
            _fh.write('[project]\nversion = "0.1.0"\n')
        with open(os.path.join(_scripts, '_version.py'), 'w') as _fh:
            _fh.write('_BASE_VERSION = "0.1.0"\n')
        with open(os.path.join(_d, '.gitignore'), 'w') as _fh:
            _fh.write('scripts/_buildstamp.py\n')

        def _g(*_a):
            _sp.run(['git', '-C', _d, *_a], check=True, capture_output=True)
        _g('init', '-q')
        _g('config', 'user.email', 't@example.com')
        _g('config', 'user.name', 'Test')
        _g('add', '-A')
        _g('commit', '-q', '-m', 'init')

        _saved = (_bv._ROOT, _bv._PYPROJECT, _bv._VERSION_PY, _bv._BUILDSTAMP_PY)
        _bv._ROOT = _d
        _bv._PYPROJECT = os.path.join(_d, 'pyproject.toml')
        _bv._VERSION_PY = os.path.join(_scripts, '_version.py')
        _bv._BUILDSTAMP_PY = os.path.join(_scripts, '_buildstamp.py')
        try:
            _rc = _bv.main(['patch', '--freeze-stamp', '--no-tag'])
        finally:
            (_bv._ROOT, _bv._PYPROJECT, _bv._VERSION_PY,
             _bv._BUILDSTAMP_PY) = _saved
        assert _rc == 0
        _log = _sp.run(['git', '-C', _d, 'log', '--oneline'],
                       capture_output=True, text=True).stdout
        assert 'release: v0.1.1' in _log, _log
        # the stamp was written but is NOT tracked (gitignored)
        assert os.path.isfile(os.path.join(_scripts, '_buildstamp.py'))
        _tracked = _sp.run(['git', '-C', _d, 'ls-files'],
                           capture_output=True, text=True).stdout
        assert '_buildstamp.py' not in _tracked, _tracked
        with open(os.path.join(_d, 'pyproject.toml')) as _fh:
            assert 'version = "0.1.1"' in _fh.read()

TESTS = [
    test_iso_installer_stage_disk_info_copies_files_skipping_readme,
    test_strip_build_version_strips_trailing_binNMU,
    test_strip_build_version_preserves_point_release_suffix,
    test_strip_build_version_strips_binNMU_after_point_release,
    test_strip_build_version_leaves_embedded_binNMU_alone,
    test_strip_build_version_handles_udeb_extension,
    test_strip_build_version_no_change_when_no_binNMU,
    test_transpose_trailing_plus_deb,
    test_transpose_trailing_tilde_deb_keeps_sign,
    test_transpose_embedded_deb_is_left_alone,
    test_transpose_no_token_unchanged,
    test_transpose_floor_tail_tilde_preserved,
    test_strip_binNMU_splits_trailing_marker,
    test_transposed_version_appends_p_and_b,
    test_compute_transposed_versions_per_binary_base_uniform_p,
    test_transpose_control_text_version_deps_and_provenance,
    test_transpose_control_text_strips_binnmu_from_constraint_bounds,
    test_transpose_control_text_keep_binnmu_names_exempts_tunneled_targets,
    test_source_package_version_predictor,
    test_transpose_ceiling_dot_tail_transposes,
    test_transpose_constraint_strips_legacy_binnmu_bound,
    test_transpose_deb_source_annotation_cases,
    test_transpose_source_relations_build_fields_and_substvars,
    test_source_emit_classify_verbatim_and_reemit,
    test_source_emit_environment_mode_helpers,
    test_source_emit_backfill_helpers,
    test_tunneled_binary_names_resolves_from_cache,
    test_transpose_control_text_no_token_is_noop_no_provenance,
    test_transpose_scheme_boundary_table_and_ordering,
    test_decide_patch_bump_count_branches,
    test_strip_build_version_rejects_malformed_filename,
    test_parse_deb_filename_splits_raw_components,
    test_parse_deb_filename_returns_none_on_mismatch,
    test_repack_deb_clamps_to_source_date_epoch_for_reproducibility,
    test_bump_is_canonical_home_for_version_logic,
    test_strip_nmu_suffix_strips_known_patterns,
    test_strip_nmu_suffix_idempotent,
    test_strip_nmu_from_control_text_walks_relation_fields,
    test_strip_nmu_inserts_x_athena_upstream_version_when_stripped,
    test_strip_nmu_omits_x_field_for_pristine_version,
    test_strip_nmu_x_field_idempotent_on_already_stamped,
    test_strip_nmu_pair_rewrite_collapses_sibling_idiom,
    test_strip_nmu_pair_rewrite_only_when_pair_matches,
    test_strip_nmu_pair_rewrite_idempotent,
    test_strip_nmu_pair_rewrite_scoped_to_depends_pre_depends,
    test_strip_nmu_from_deb_round_trip,
    test_strip_nmu_from_deb_idempotent,
    test_transpose_deb_round_trip,
    test_transpose_deb_tunnel_keeps_binnmu_and_rewrites_trailing_deb,
    test_transpose_deb_tunnel_keep_binnmu_preserves_frozen_sibling_pin,
    test_transpose_fork_athena_version_is_noop,
    test_restamp_sibling_pins_force_bn_branch,
    test_version_no_epoch_strips_epoch_from_debian_version,
    test_version_no_epoch_no_change_when_no_epoch,
    test_version_no_epoch_accepts_string_input,
    test_version_no_epoch_handles_multidigit_epoch,
    test_version_no_epoch_only_strips_first_colon,
    test_nmu_regex_does_not_eat_asg_suffix,
    test_pristine_base_strips_both_layers,
    test_asg_suffix_sorts_above_pristine_and_across_release,
    test_asg_filename_maps_pristine_to_stamped,
    test_match_pristine_base_reconciles_stamped_artifact,
    test_version_module_resolution,
    test_version_base_matches_pyproject,
    test_version_git_describe_matches_only_version_tags,
    test_version_command_registered_and_user_agent_derived,
    test_bump_version_next_computation,
    test_bump_version_rewrite_patterns_match_real_files,
    test_bump_version_freeze_stamp_excludes_gitignored_buildstamp,
    test_build_record_carries_toolchain_version,
    test_get_version_is_cached,
    test_http_session_is_module_level_singleton,
    test_bump_version_freeze_stamp_commits_without_gitignored_stamp,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
