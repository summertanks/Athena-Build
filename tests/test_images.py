"""Athena tests — image surfaces (iso.py, disk_image.py, cmd_build.py).

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
    _session_source,
    _surf_tree,
)




def test_mat03_live_chroot_identity_scanned_before_squashfs():
    """MAT-03: the live chroot root is identity-scanned BEFORE mksquashfs —
    the squashfs is opaque to every later audit, so residue in the booted
    root would ship undetected.  Pins: the scan call precedes the
    mksquashfs invocation, a failure aborts the build (return False), the
    [Audit] IdentityScan gate applies (loud skip when disabled), and the
    allowlist carves out the legally-retained upstream attribution paths."""
    import re
    _p = os.path.join(_ROOT, 'scripts', 'iso.py')
    with open(_p) as _fh:
        _s = _fh.read()
    _scan = _s.find('_audit_staged_iso(self._dir_chroot')
    _squash = _s.find("'mksquashfs', self._dir_chroot")
    assert _scan != -1, 'chroot identity scan missing from build_iso'
    assert _squash != -1, 'mksquashfs invocation not found'
    assert _scan < _squash, 'scan must run BEFORE the expensive mksquashfs'
    assert re.search(r'audit_identity_scan.*\n.*_audit_staged_iso', _s), \
        'scan must be gated by [Audit] IdentityScan'
    assert 'IdentityScan = false' in _s, 'disabled gate must warn loudly'
    # The chroot scan is a POSITIVE surface manifest, not a full-root
    # walk — a full walk drowned in 8,311 factual upstream mentions
    # (2026-07-18 first run).
    assert 'include_globs=BOOTED_IDENTITY_SURFACES' in _s, \
        'chroot scan must be scoped to the booted-root surface manifest'
    _al = os.path.join(_ROOT, 'audit', 'identity-allowlist')
    with open(_al) as _fh:
        _a = _fh.read()
    for _glob in ('usr/share/doc/*', 'usr/share/common-licenses/*',
                  'usr/share/man/*', 'etc/dpkg/origins/*',
                  'usr/share/desktop-directories/Debian.directory'):
        assert _glob in _a, f'allowlist must carve out {_glob}'


def test_arch_profile_stage15_personality_and_host_matrix():
    """COMP-04 stage 1.5 (B2/B9): the execution-model fields.  i386 is
    the only personality-wrapped arch (linux32, Debian buildd
    convention) and the only one executable on a wider host (amd64);
    amd64/arm64 run only on themselves.  assert_host_compatible must
    refuse a non-native target loudly and honor the
    ATHENA_ALLOW_FOREIGN_ARCH=1 analysis escape."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import arch_profile as ap
    assert ap.profile('amd64').linux_personality == ''
    assert ap.profile('i386').linux_personality == 'linux32'
    assert ap.profile('arm64').linux_personality == ''
    assert ap.profile('amd64').host_arches == ('amd64',)
    assert ap.profile('i386').host_arches == ('amd64', 'i386')
    assert ap.profile('arm64').host_arches == ('arm64',)
    # matrix behavior, host pinned to amd64 via the cache
    _saved = ap._HOST_ARCH
    try:
        ap._HOST_ARCH = 'amd64'
        ap.assert_host_compatible('amd64')
        ap.assert_host_compatible('i386')     # native 32-bit personality
        try:
            ap.assert_host_compatible('arm64')
            raise AssertionError('arm64-on-amd64 must be refused')
        except RuntimeError as _e:
            assert 'Exec format error' in str(_e)
        os.environ['ATHENA_ALLOW_FOREIGN_ARCH'] = '1'
        try:
            ap.assert_host_compatible('arm64')   # warning, no raise
        finally:
            del os.environ['ATHENA_ALLOW_FOREIGN_ARCH']
        ap._HOST_ARCH = 'arm64'
        ap.assert_host_compatible('arm64')
        try:
            ap.assert_host_compatible('i386')
            raise AssertionError('i386-on-arm64 must be refused')
        except RuntimeError:
            pass
    finally:
        ap._HOST_ARCH = _saved
    # B9 is wired at the config arch read, not merely available
    with open(os.path.join(_ROOT, 'scripts', 'utils.py')) as _fh:
        _u = _fh.read()
    assert 'assert_host_compatible(self.arch)' in _u, \
        'BuildConfig must run the B9 host-arch preflight'


def test_buildcontainer_stage15_arch_tag_label_and_personality():
    """COMP-04 stage 1.5 (B1/B2) source pins: the docker image tag
    carries the arch (BS1/BS3 share host + snapshot pin — an
    unqualified tag reuses the wrong rootfs), the athena.arch label is
    stamped at build AND asserted on reuse, and both containers.run
    sites route through the personality-wrapping _container_command."""
    _p = os.path.join(_ROOT, 'scripts', 'buildcontainer.py')
    with open(_p) as _fh:
        _s = _fh.read()
    import re
    _m = re.search(r'self\._image_tag\s*=\s*\(([^)]*)\)', _s)
    assert _m and '{self.arch}' in _m.group(1), \
        'image tag must be arch-qualified'
    assert _s.count("'athena.arch'") >= 2, \
        'athena.arch label must be stamped at build and read on reuse'
    assert 'stored_arch != self.arch' in _s, \
        'reuse must rebuild on arch-label mismatch'
    assert '_container_command(cmd_str)' in _s and \
        _s.count('command=self._container_command(cmd_str)') == 2, \
        'both run sites must use the personality wrapper'
    assert '"setarch", _personality, "--"' in _s
    # behavioral: the wrapper itself, on a bare instance
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildcontainer as _bcm
    _bc = object.__new__(_bcm.BuildContainer)
    _bc.arch = 'i386'
    assert _bcm.BuildContainer._container_command(_bc, 'echo hi') == \
        ['setarch', 'linux32', '--', '/bin/bash', '-c', 'echo hi']
    _bc.arch = 'amd64'
    assert _bcm.BuildContainer._container_command(_bc, 'echo hi') == \
        ['/bin/bash', '-c', 'echo hi']


def test_arch_profile_d2_table_and_kernel_regexes():
    """COMP-04 D2: arch_profile is the single authority for per-arch
    facts.  Pins the table rows the image builders depend on, the
    kernel regex semantics (real ABI kernels match; metas, -rt-/-cloud-
    flavors and foreign flavors do not), and that an unknown arch is a
    loud ValueError rather than a silent amd64 fallback."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import arch_profile as ap
    assert ap.supported_arches() == ('amd64', 'arm64', 'i386')
    # kernel flavor != dpkg arch (the G1 gap this module closes)
    assert ap.profile('i386').kernel_flavor == '686-pae'
    assert ap.profile('amd64').kernel_meta == 'linux-image-amd64'
    assert ap.profile('arm64').kernel_meta == 'linux-image-arm64'
    # arm64 boot model: EFI-only, no BIOS, no microcode, no grub-pc
    _a64 = ap.profile('arm64')
    assert not _a64.bios_boot
    assert _a64.grub_bins == ('grub-efi-arm64-bin',)
    assert _a64.microcode == ()
    assert _a64.efi_removable_name == 'BOOTAA64.EFI'
    assert _a64.grub_efi_target == 'arm64-efi'
    # x86 profiles keep the hybrid BIOS+EFI model
    for _x in ('amd64', 'i386'):
        assert ap.profile(_x).bios_boot
        assert 'grub-pc-bin' in ap.profile(_x).grub_bins
    # kernel-deb regex semantics, per arch
    _cases = {
        'amd64': ('linux-image-6.1.0-47-amd64_6.1.170-3_amd64.deb',
                  'linux-image-amd64_6.1.170-3_amd64.deb'),
        'i386':  ('linux-image-6.1.0-47-686-pae_6.1.170-3_i386.deb',
                  'linux-image-686-pae_6.1.170-3_i386.deb'),
        'arm64': ('linux-image-6.1.0-47-arm64_6.1.170-3_arm64.deb',
                  'linux-image-arm64_6.1.170-3_arm64.deb'),
    }
    for _arch, (_real, _meta) in _cases.items():
        _p = ap.profile(_arch)
        assert _p.kernel_pkg_re.match(_real), f'{_arch}: real must match'
        assert not _p.kernel_pkg_re.match(_meta), f'{_arch}: meta must not'
        assert not _p.kernel_pkg_re.match(
            'linux-image-6.1.0-47-rt-amd64_6.1.170-3_amd64.deb')
    # foreign flavor never crosses profiles
    assert not ap.profile('i386').kernel_pkg_re.match(_cases['amd64'][0])
    # installed-name regex agrees with the deb regex
    assert ap.profile('amd64').kernel_name_re.match('linux-image-6.1.0-50-amd64')
    assert not ap.profile('amd64').kernel_name_re.match('linux-image-amd64')
    # loud failure on unknown arch
    try:
        ap.profile('riscv64')
        raise AssertionError('unknown arch must raise')
    except ValueError as _e:
        assert 'riscv64' in str(_e)


def test_comp04_stage1_no_live_amd64_literals():
    """COMP-04 stage 1: the six literal clusters are dead — artifact
    names, Release Architectures, kernel regexes and the container grub
    toolset all derive from config.arch / arch_profile.  Pins the
    absence of the load-bearing literal FORMS (docstrings/comments may
    still mention amd64 as an example)."""
    _checks = {
        'scripts/apt_repo.py':      ["_ARCH = 'amd64'",
                                     'Architectures=amd64'],
        'scripts/iso.py':           ['-amd64.iso"'],
        'scripts/commands/cmd_build.py':  ['-amd64.iso"', '-amd64$'],
        'scripts/commands/cmd_mirror.py': ['-amd64.iso"', '-amd64.qcow2"',
                                           "arch='amd64'"],
        'scripts/print_commands.py': ['-amd64.iso"'],
        'scripts/buildcontainer.py': ['grub-pc-bin grub-efi-amd64-bin'],
        'scripts/remote_localmirror.py': ['Architectures: amd64 all'],
        'scripts/mirror.py':        ["= ('amd64',)"],
        'scripts/iso_installer.py': ["-amd64_'", "'linux-image-*-amd64*.deb'"],
    }
    for _rel, _needles in _checks.items():
        with open(os.path.join(_ROOT, _rel)) as _fh:
            _src = _fh.read()
        for _n in _needles:
            assert _n not in _src, f'{_rel}: live literal {_n!r} resurfaced'


def test_identity_scan_include_globs_positive_manifest():
    """audit_identity(include_globs=...) scans ONLY matching rel-paths —
    the MAT-03 booted-root scan depends on out-of-manifest files being
    out of scope entirely (not merely allowlisted).  Also pins that the
    manifest covers os-release and grub fragments and that `*` crosses
    `/` (fnmatch semantics: etc/dpkg/origins/* matches nested names)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from identity_scan import audit_identity, BOOTED_IDENTITY_SURFACES
    with tempfile.TemporaryDirectory() as _td:
        os.makedirs(os.path.join(_td, 'etc'))
        os.makedirs(os.path.join(_td, 'usr/bin'))
        with open(os.path.join(_td, 'etc', 'os-release'), 'w') as _fh:
            _fh.write('PRETTY_NAME="Debian GNU/Linux 12"\n')
        with open(os.path.join(_td, 'usr/bin', 'tzselect'), 'w') as _fh:
            _fh.write('# report bugs to the Debian maintainers\n')
        _fs = audit_identity(_td, None,
                             include_globs=BOOTED_IDENTITY_SURFACES)
        _paths = {_f['path'] for _f in _fs}
        assert 'etc/os-release' in _paths, 'in-manifest leak must flag'
        assert 'usr/bin/tzselect' not in _paths, \
            'out-of-manifest file must be out of scope'
    for _surf in ('etc/os-release', 'usr/lib/os-release',
                  'etc/default/grub.d/*', 'etc/grub.d/*',
                  'usr/share/desktop-directories/*'):
        assert _surf in BOOTED_IDENTITY_SURFACES, \
            f'{_surf} missing from booted-root manifest'


def test_verify_chroot_disk_surface_skips_live_boot():
    """SURFACES-01: the disk chroot ships without live-boot by design,
    so _verify_chroot(require_live_boot=False) reports the live-boot
    check as SKIP (not FAIL) and the surface verifies clean.  The first
    disk chroot build failed 7/8 on this (2026-06-11)."""
    import sys as _sys
    import types as _types
    import tempfile
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import build
    from build import BuildSession
    import commands.cmd_build as _cb

    with tempfile.TemporaryDirectory() as _td:
        os.makedirs(os.path.join(_td, 'boot'))
        open(os.path.join(_td, 'boot', 'vmlinuz-6.1.0-1'), 'w').close()
        open(os.path.join(_td, 'boot', 'initrd.img-6.1.0-1'), 'w').close()
        os.makedirs(os.path.join(_td, 'etc'))
        with open(os.path.join(_td, 'etc', 'os-release'), 'w') as _f:
            _f.write('PRETTY_NAME="Asgard Linux 1 (thor)"\n')

        def _fake_run(cmd, **k):
            _p = _types.SimpleNamespace(returncode=0, stdout='', stderr='')
            if '--get-selections' in cmd:
                _p.stdout = 'pkg\tinstall\n'
            elif 'bash' in cmd or 'systemctl' in cmd:
                _p.stdout = 'version 1.0\n'
            return _p

        _orig_sp = _cb.subprocess
        _cb.subprocess = _types.SimpleNamespace(run=_fake_run)
        _sess = BuildSession.__new__(BuildSession)
        _lines: 'list[str]' = []
        _orig_print = build.console.print
        build.console.print = lambda *a, **k: _lines.append(
            ' '.join(str(x) for x in a))
        try:
            _passed, _failed = _sess._verify_chroot(
                'pw', _td, require_live_boot=False)
        finally:
            _cb.subprocess = _orig_sp
            build.console.print = _orig_print

        _joined = '\n'.join(_lines)
        assert _failed == 0, (_passed, _failed, _joined)
        assert '[SKIP]' in _joined and 'live-boot' in _joined, _joined
        assert 'blocked' not in _joined, _joined



def test_write_iso_md5sum_manifest_mat08():
    """MAT-08: write_iso_md5sum_manifest emits a SORTED Debian-style md5sum.txt
    (excluding itself) via `sudo -A find … -exec md5sum` → `sudo -A tee`, with
    the password supplied through SUDO_ASKPASS so tee's stdin is the manifest
    ONLY (no password/content mixing)."""
    import types as _types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    with tempfile.TemporaryDirectory() as _stage:
        _calls = []
        _tee = {}

        def _fake_run(argv, **kw):
            _calls.append(argv)
            if 'find' in argv:                # the hashing pass (unsorted)
                return _types.SimpleNamespace(
                    returncode=0, stderr='',
                    stdout=("bbb  ./live/filesystem.squashfs\n"
                            "aaa  ./boot/vmlinuz\n"))
            if 'tee' in argv:
                _tee['data'] = kw.get('input')
                _tee['askpass'] = 'SUDO_ASKPASS' in (kw.get('env') or {})
                return _types.SimpleNamespace(returncode=0, stdout='', stderr='')
            return _types.SimpleNamespace(returncode=0, stdout='', stderr='')

        _orig = utils.subprocess
        utils.subprocess = _types.SimpleNamespace(run=_fake_run, DEVNULL=-3)
        try:
            _ok = utils.write_iso_md5sum_manifest(_stage, 'pw')
        finally:
            utils.subprocess = _orig
    assert _ok is True
    _find = next(a for a in _calls if 'find' in a)
    assert '-A' in _find and 'md5sum' in _find and 'md5sum.txt' in _find, _find
    # tee receives the SORTED manifest (and only that), password via askpass.
    assert _tee['data'] == ("aaa  ./boot/vmlinuz\n"
                            "bbb  ./live/filesystem.squashfs\n"), _tee['data']
    assert _tee['askpass']



def test_iso_builders_write_md5sum_manifest_mat08():
    """MAT-08 wiring: both ISO builders write md5sum.txt BEFORE grub-mkrescue,
    and the live ISO offers a `live-media-check` boot entry."""
    _iso = open(os.path.join(_ROOT, 'scripts', 'iso.py')).read()
    _inst = open(os.path.join(_ROOT, 'scripts', 'iso_installer.py')).read()
    assert 'write_iso_md5sum_manifest' in _iso, "live ISO must write the manifest"
    assert 'write_iso_md5sum_manifest' in _inst, "installer must write the manifest"
    assert (_iso.index('write_iso_md5sum_manifest')
            < _iso.index('container.run_grub_mkrescue(')), \
        "manifest must be written before grub-mkrescue (live)"
    assert (_inst.index('write_iso_md5sum_manifest')
            < _inst.index('if not _run_grub_mkrescue(')), \
        "manifest must be written before grub-mkrescue (installer)"
    assert 'live-media-check' in _iso, "live ISO must offer a Check-media entry"



def test_mat09_disk_image_locks_root_and_adds_sudo_user():
    """MAT-09: the disk image LOCKS root and adds a cloud-style passwordless-sudo
    user (in the sudo group) with a per-build random console password — so the
    qcow2 ships no default credential and stays loginnable.  The user + ext4
    label DERIVE from the distribution id, not a hardcoded 'asgard'."""
    _src = open(os.path.join(_ROOT, 'scripts', 'disk_image.py')).read()
    assert "'passwd', '-l', 'root'" in _src, "disk image must lock root"
    assert "'useradd'" in _src and "'-G', 'sudo'" in _src, "user in sudo group"
    assert 'chpasswd' in _src and 'secrets.token_urlsafe' in _src, \
        "default user needs a random console password"
    # distro-derived, not a hardcoded distro name
    assert '_distro_account(' in _src and 'dist_id' in _src, \
        "default user must derive from the distribution id"
    assert "'asgard'" not in _src, \
        "disk image must not hardcode the 'asgard' account/label"



def test_distro_account_derives_valid_username():
    """disk_image._distro_account: a valid Linux username from the distro id —
    sanitises invalid chars, requires a letter/underscore start (else falls
    back to 'admin'), and caps to 32 chars."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import disk_image
    assert disk_image._distro_account('Asgard') == 'asgard'
    assert disk_image._distro_account('valhalla') == 'valhalla'
    assert disk_image._distro_account('My Distro!') == 'mydistro'   # stripped
    assert disk_image._distro_account('123') == 'admin'             # digit start
    assert disk_image._distro_account('-x') == 'admin'              # dash start
    assert disk_image._distro_account('') == 'admin'                # empty
    assert len(disk_image._distro_account('a' * 50)) == 32          # capped



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



def test_hk06_disk_image_e2fsck_clears_orphan_list():
    """HK-06: the disk build runs `e2fsck -f -p` on the root partition after
    the root umount and before the loop detach, so the shipped qcow2 is
    pristine (first boot otherwise logs e2fsck cleaning a benign ext4
    orphan-inode list).  Source pin (the privileged disk path can't run in
    CI)."""
    import inspect, sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import disk_image
    _src = inspect.getsource(disk_image.build_disk_image)
    _i = _src.find("'e2fsck'")
    assert _i != -1, "no e2fsck pass in the disk image build"
    _call = _src[_i:_i + 70]
    assert "'-f'" in _call and "'-p'" in _call and '_root_part' in _call, _call
    # ordering: AFTER the root umount, BEFORE the loop detach (happy path —
    # first occurrence of each precedes the finally-block copies)
    _u = _src.find("umount', _mnt")
    _d = _src.find("losetup', '-d', _loop_dev")
    assert _u != -1 and _d != -1 and _u < _i < _d, (_u, _i, _d)



def test_disk_image_regenerates_initramfs_for_fsck_tools():
    """STA-26: after writing the real fstab (ext4 root + vfat ESP, step 7),
    the disk build re-runs `update-initramfs -u` inside the chroot so
    initramfs-tools' fsck hook copies fsck.ext4/fsck.vfat.  The kernel's
    build-time update-initramfs ran against the virtual-only chroot fstab and
    copied none.  Source pin (the privileged disk path can't run in CI)."""
    import sys, inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import disk_image
    _src = inspect.getsource(disk_image.build_disk_image)
    _i = _src.find("'update-initramfs'")
    assert _i != -1, "no update-initramfs call in the disk image build"
    # runs as -u inside the chroot (env PATH + chroot _mnt)
    _call = _src[_i - 80:_i + 80]
    assert "'-u'" in _call and 'chroot' in _call, _call
    # MUST run AFTER the fstab write — the fsck hook keys on fstab fstypes
    _fstab_i = _src.find("'fstab'")
    assert _fstab_i != -1 and _fstab_i < _i, \
        "update-initramfs must run after the fstab write"
    # non-fatal: warns, never aborts the image build
    assert 'image initramfs may lack' in _src, _src



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
    and failed.  Source-text pin: the BIOS install branch must be gated
    on the `_bios = _has_bios_modules(_mnt)` result (computed once, audit
    #122)."""
    _path = os.path.join(_ROOT, 'scripts', 'disk_image.py')
    with open(_path) as fh:
        _body = fh.read()
    import re
    # _has_bios_modules is computed once into _bios, and the i386-pc
    # invocation must live inside the `if _bios:` block.
    _m = re.search(
        r"_bios = _has_bios_modules\(_mnt\).*?if _bios:.*?--target=i386-pc",
        _body, re.DOTALL,
    )
    assert _m, (
        "BIOS grub-install must be gated on _bios (= _has_bios_modules(_mnt)) "
        "— running it unconditionally on a chroot without i386-pc modules "
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


def test_sta52_live_iso_wipes_staging_and_wildcards_exclude_fallback():
    """STA-52: the live ISO build must wipe its staging tree between runs
    (grub-mkrescue images the whole tree, so a since-removed file ships
    forever), and the mksquashfs `-e dir/*` PermissionError fallback must
    pass `-wildcards` or mksquashfs treats the glob literally and silently
    ships the unlistable dir's contents.  Source pin (full build needs
    sudo + the real pipeline)."""
    import inspect, sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import iso
    _src = inspect.getsource(iso._IsoMixin.build_iso)
    # staging is wiped (sudo rm -rf) before recreation
    import re
    assert re.search(r"rm['\"],\s*['\"]-rf['\"],\s*_staging", _src), \
        "live staging tree is not wiped before reuse (STA-52)"
    # the glob fallback enables -wildcards
    assert '_used_glob' in _src and "'-wildcards'" in _src, _src
    assert "os.path.join(_dir_path, '*')" in _src, _src



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



def test_disk_image_removes_raw_intermediate_on_failure():
    """Regression (audit #120): build_disk_image must unlink the sparse _raw
    intermediate in its finally — _convert_to_qcow2 only removes it on success,
    so a failure path leaked GBs of disk."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import disk_image
    assert 'os.unlink(_raw)' in inspect.getsource(disk_image.build_disk_image), (
        "build_disk_image's finally must remove the _raw intermediate")



def test_disk_image_e2fsck_handles_exit_code_three():
    """Regression (audit #121): e2fsck exit status is a bitmask; rc=3 (=1|2,
    corrected+reboot) must be treated as cleaned, not fall through silently."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import disk_image
    assert "1 <= _fsck.returncode < 4" in inspect.getsource(disk_image), (
        "e2fsck cleaned-case must cover rc 1..3, not just (1, 2)")



def test_verify_chroot_get_selections_checks_returncode():
    """Regression (audit #59): _verify_chroot's 'All packages fully installed'
    check must consider the dpkg returncode — a failed `dpkg --get-selections`
    yields empty stdout (→ _incomplete == []) and would otherwise falsely
    PASS."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import commands.cmd_build as _cb
    assert 'dpkg --get-selections failed' in inspect.getsource(_cb), (
        "the 'fully installed' check must surface a dpkg returncode failure "
        "distinctly, else a failed get-selections falsely PASSes")



def test_iso_filenames_carry_snapshot_tag():
    """Both the live (iso.py) and installer (build.py) ISO filenames embed the
    snapshot tag so base vs stepped builds are distinguishable on disk."""
    import re
    _iso = os.path.join(_ROOT, 'scripts', 'iso.py')
    with open(_iso) as fh:
        _ib = fh.read()
    assert 'utils.snapshot_iso_tag(cfg)' in _ib
    assert 'athena-{_version}-{_snap}-{_arch}.iso' in _ib
    _bb = _session_source()
    assert 'utils.snapshot_iso_tag(self.config)' in _bb
    assert 'athena-installer-{_version}-{_snap}-{_arch}.iso' in _bb
    # snapshot threaded into the installer ISO build
    assert re.search(r'snapshot=_snap', _bb), "snapshot not passed to build_installer_iso"



def test_disk_surface_plumbing():
    """SURFACES-01 Chunk 4: dir_chroot_disk exists + is distinct;
    chroot_disk_ready is a real flag field; BuildSystem accepts a
    dir_chroot override; iso disk gates on the disk chroot."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils as _u
    import inspect
    _cfg = _u.BuildConfig()
    assert _cfg.dir_chroot_disk.endswith('buildroot/disk')
    assert _cfg.dir_chroot_disk != _cfg.dir_chroot
    assert os.path.isdir(_cfg.dir_chroot_disk)
    from build import BuildFlags
    assert 'chroot_disk_ready' in BuildFlags._FIELDS
    import buildsystem
    _sig = inspect.signature(buildsystem.BuildSystem.__init__)
    assert 'dir_chroot' in _sig.parameters
    # iso disk command sources the DISK chroot, not the live one
    import commands.cmd_build as _cb
    _src = inspect.getsource(_cb)
    _disk_fn = _src[_src.index('def cmd_build_iso_disk'):]
    _disk_fn = _disk_fn[:_disk_fn.index('def cmd_', 10)]
    assert 'dir_chroot=self.config.dir_chroot_disk' in _disk_fn
    assert 'chroot_disk_ready' in _disk_fn



def test_iso_pool_manifest_excludes_unreachable_pool_roots():
    """SURFACES-01 Chunk 5: the pool manifest = closure(base ∪ task groups
    ∪ installer-defaults).  A pool-rooted package reachable by NOTHING on
    the ISO (asgard-meta/residue style) is excluded; a d-i root
    (grub/microcode style) is included with its deps."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import surfaces as _sf
    _pkgs = {
        'base-a':    _SurfPkg('base-a'),
        'task-b':    _SurfPkg('task-b', depends=['libshared']),
        'libshared': _SurfPkg('libshared'),
        'grub-x':    _SurfPkg('grub-x', depends=['grub-common-x']),
        'grub-common-x': _SurfPkg('grub-common-x'),
        'residue':   _SurfPkg('residue', depends=['residue-lib']),
        'residue-lib': _SurfPkg('residue-lib'),
    }
    _dt = _surf_tree(_pkgs)
    # manifest roots: base group + task group + d-i defaults — NOT residue
    _manifest = _sf.surface_closure(
        _dt, {'base-a', 'task-b', 'grub-x'},
        include_recommends_extras=True)
    assert 'grub-x' in _manifest and 'grub-common-x' in _manifest
    assert 'libshared' in _manifest
    assert 'residue' not in _manifest and 'residue-lib' not in _manifest
    # the wiring composes manifest seeds from groups + _di_roots + req/imp
    import inspect
    import commands.cmd_build as _cb
    _src = inspect.getsource(_cb)
    _blk = _src[_src.index('manifest-driven pool'):]
    _blk = _blk[:2500]
    assert '_manifest_seeds.update(_di_roots)' in _blk
    assert 'self.config.installerlist_path' in _blk   # efibootmgr-class roots
    assert 'surface_closure' in _blk



def test_iso_installer_accepts_tasks_desc_text():
    """build_installer_iso takes tasks_desc_text and the staging writer
    targets .disk/athena-tasks.desc; cmd_build generates from the signed
    lockfile with pkg.list fallback."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import iso_installer as _ii
    _sig = inspect.signature(_ii.build_installer_iso)
    assert 'tasks_desc_text' in _sig.parameters
    _src = inspect.getsource(_ii.build_installer_iso)
    assert "athena-tasks.desc" in _src
    import commands.cmd_build as _cb
    _cbsrc = inspect.getsource(_cb)
    assert 'tasks_desc_text=self._generate_tasks_desc()' in _cbsrc
    _gen = _cbsrc[_cbsrc.index('def _generate_tasks_desc'):]
    _gen = _gen[:_gen.index('def cmd_', 10)]
    assert 'read_selection_state' in _gen        # lockfile is the source
    assert 'parse_pkg_list_groups' in _gen       # fallback path

TESTS = [
    test_mat03_live_chroot_identity_scanned_before_squashfs,
    test_arch_profile_stage15_personality_and_host_matrix,
    test_buildcontainer_stage15_arch_tag_label_and_personality,
    test_arch_profile_d2_table_and_kernel_regexes,
    test_comp04_stage1_no_live_amd64_literals,
    test_identity_scan_include_globs_positive_manifest,
    test_verify_chroot_disk_surface_skips_live_boot,
    test_write_iso_md5sum_manifest_mat08,
    test_iso_builders_write_md5sum_manifest_mat08,
    test_mat09_disk_image_locks_root_and_adds_sudo_user,
    test_distro_account_derives_valid_username,
    test_disk_surface_plumbing,
    test_iso_pool_manifest_excludes_unreachable_pool_roots,
    test_iso_installer_accepts_tasks_desc_text,
    test_iso_filenames_carry_snapshot_tag,
    test_iso_py_build_iso_routes_through_container,
    test_disk_image_module_exposes_build_disk_image_signature,
    test_disk_image_regenerates_initramfs_for_fsck_tools,
    test_hk06_disk_image_e2fsck_clears_orphan_list,
    test_disk_image_has_bios_modules_probes_chroot_dir,
    test_disk_image_grub_install_uses_absolute_path_and_env_path,
    test_disk_image_writes_minimal_grub_cfg_unconditionally,
    test_disk_image_efi_only_fallback_when_no_bios_modules,
    test_disk_image_minimal_grub_cfg_writes_uuid_kernel_line,
    test_disk_image_minimal_grub_cfg_fails_clean_when_no_kernel,
    test_disk_image_sfdisk_script_lays_out_three_partitions,
    test_sta52_live_iso_wipes_staging_and_wildcards_exclude_fallback,
    test_iso_build_uses_spinner_for_mksquashfs_and_grub_mkrescue,
    test_disk_image_removes_raw_intermediate_on_failure,
    test_disk_image_e2fsck_handles_exit_code_three,
    test_verify_chroot_get_selections_checks_returncode,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
