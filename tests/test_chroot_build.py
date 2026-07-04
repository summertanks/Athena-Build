"""Athena tests — chroot install (chroot.py, buildsystem.py).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile

from _test_helpers import (  # noqa: F401
    _ROOT,
    _StubConsole,
    _SurfPkg,
    _bare_buildsystem,
    _bare_buildsystem_with_deps,
    _session_source,
    _stub_chroot_mixin_for_apt_source,
    _stub_chroot_mixin_for_keyring_test,
)




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



def test_compute_install_batches_dpkg_dash_closure_scheduled_first():
    """The dpkg+dash dependency closure must be emitted in the earliest
    Kahn waves, ahead of other ready leaves.  Configure runs chrootless
    (host-side maintainer scripts) until dpkg+sh exist in the chroot; a
    postinst exec'ing its own shipped interpreter (python3.11-minimal)
    can only configure in-chroot, and its dependents' Pre-Depends then
    fail to unpack — the essential toolchain must land first so every
    later batch configures in-chroot (live 2026-06-11)."""
    bs = _bare_buildsystem_with_deps([
        ('aaa-leaf', [], []),            # alphabetically-first decoy leaf
        ('dash', [], []),
        ('dpkg', [], ['libzstd1']),
        ('libzstd1', [], []),
        ('app', [], ['dpkg']),
    ])
    batches = bs._compute_install_batches(libc_seed_set=set())
    # Core waves first: {dash, libzstd1} then {dpkg}; only afterwards
    # the decoy leaf + app.  Without the bias, wave 1 would have been
    # ['aaa-leaf', 'dash', 'libzstd1'].
    assert batches == [
        (['dash', 'libzstd1'], False),
        (['dpkg'], False),
        (['aaa-leaf', 'app'], False),
    ], batches



def test_build_chroot_retries_failed_unpacks_after_final_sweep_in_rounds():
    """A package whose unpack fails on a same-batch Pre-Depends must be
    retried AFTER the final configure sweep, in rounds, with a re-sweep
    once retries make progress.  The pre-dep may itself be a postinst
    only the in-chroot sweep can configure: early batches run chrootless
    (no dpkg/sh in the chroot yet), so a postinst exec'ing its own
    shipped binary resolves on the HOST and defers to the sweep —
    python3.11-minimal → python3-minimal → python3 chain, live
    2026-06-11.  A pre-sweep-only retry leaves the chain dead."""
    import types
    import chroot as chroot_module
    import tui as _tui

    # A's configure only succeeds in the sweep (chrootless-deferred
    # postinst stand-in); B pre-depends A, C pre-depends B.
    bs = _bare_buildsystem_with_deps(
        [('A', [], []), ('B', ['A'], []), ('C', ['B'], [])])
    bs._dir_chroot = '/nonexistent-chroot'
    bs._password = 'pw'
    _tui.console = _StubConsole()

    _calls = []
    bs._setup_chroot_env = lambda: None
    bs._init_dpkg_database = lambda: None
    bs._mount_chroot_fs = lambda: None
    bs._umount_chroot_fs = lambda: True
    bs._ensure_initramfs = lambda: None
    bs.post_install = lambda: None
    bs.generate_system_configs = lambda debug=False: None
    bs._compute_install_batches = (
        lambda libc_seed_set, install_set=None: [(['A', 'B', 'C'], True)])

    _full: set = set()       # truly-configured packages
    _unpacked: set = set()   # truly-unpacked packages
    _sweeps = []

    def _unpack(pkgs, quiet=False):
        _ok = {p for p in pkgs
               if (p == 'B' and 'A' in _full)
               or (p == 'C' and 'B' in _full)
               or p not in ('B', 'C')}
        _calls.append(('unpack', tuple(sorted(pkgs)), tuple(sorted(_ok))))
        _unpacked.update(_ok)
        return _ok

    def _configure(pkgs, force_deps=False):
        _ok = {p for p in pkgs
               if p in _unpacked and (p != 'A' or _sweeps)}
        _calls.append(('configure', tuple(sorted(pkgs))))
        _full.update(_ok)
        return _ok

    def _sweep(is_final=False):
        _calls.append(('sweep', is_final))
        _sweeps.append(is_final)
        _full.add('A')
        return {'A'}

    bs._unpack_packages = _unpack
    bs._configure_packages = _configure
    bs._configure_chroot = _sweep

    class _QProc:
        returncode = 0
        stdout = ''.join(
            f'{p} install ok installed\n'
            for p in ('libc6', 'libgcc-s1', 'libcrypt1', 'A', 'B', 'C'))
        stderr = ''
    _orig_subprocess = chroot_module.subprocess
    chroot_module.subprocess = types.SimpleNamespace(
        run=lambda *a, **k: _QProc())
    try:
        _ok = bs.build_chroot(install_set={'A', 'B', 'C'})
    finally:
        chroot_module.subprocess = _orig_subprocess

    assert _ok is True
    # Everything ends unpacked + configured: the chain converged.
    assert {'A', 'B', 'C'} <= _unpacked, _calls
    assert {'A', 'B', 'C'} <= _full, _calls
    # The retry rounds for B/C come strictly BETWEEN the two sweeps:
    # pre-sweep retries cannot succeed (A unconfigurable until the
    # sweep), and the re-sweep heals sweep-1 stragglers.
    _sweep_idx = [i for i, c in enumerate(_calls) if c[0] == 'sweep']
    assert len(_sweep_idx) == 2, _calls
    _b_round = next(i for i, c in enumerate(_calls)
                    if c[0] == 'unpack' and c[2] == ('B',))
    _c_round = next(i for i, c in enumerate(_calls)
                    if c[0] == 'unpack' and c[2] == ('C',))
    assert _sweep_idx[0] < _b_round < _c_round < _sweep_idx[1], _calls



def test_configure_chroot_final_pass_counts_stderr_failures():
    """The final `dpkg --configure -a` failure summary must (a) read dpkg's
    'error processing package' lines from STDERR — dpkg writes them there;
    the old stdout-only parse printed "0 package(s) failed" while
    gnome-menus was half-configured (caught live 2026-06-11) — and
    (b) extract the package NAME, not the line's last token, which is
    the '(--configure):' suffix."""
    import types
    import chroot as chroot_module
    import tui as _tui

    bs = _bare_buildsystem_with_deps([])
    bs._dir_chroot = '/nonexistent-chroot'
    bs._password = 'pw'
    bs._chroot_dpkg_available = lambda: True

    _printed = []

    class _RecConsole(_StubConsole):
        def print(s, m, *a, **k):
            _printed.append(str(m))
    _tui.console = _RecConsole()

    class _Proc:
        returncode = 1
        stdout = 'Setting up libfoo:amd64 (1.0-1) ...\n'
        stderr = (
            'dpkg: error processing package gnome-menus (--configure):\n'
            ' installed gnome-menus package post-installation script '
            'subprocess returned error exit status 127\n'
            'dpkg: error processing package python3:amd64 (--configure):\n'
            'Errors were encountered while processing:\n'
            ' gnome-menus\n'
        )

    _orig_subprocess = chroot_module.subprocess
    chroot_module.subprocess = types.SimpleNamespace(
        run=lambda *a, **k: _Proc())
    try:
        _configured = bs._configure_chroot()
    finally:
        chroot_module.subprocess = _orig_subprocess

    # Setting-up parse unaffected (arch suffix stripped).
    assert _configured == {'libfoo'}, _configured
    # Both stderr failures counted, names extracted, deduped + sorted.
    _msg = next((m for m in _printed if 'package(s) failed' in m), '')
    assert '2 package(s) failed' in _msg, _printed
    assert 'gnome-menus, python3' in _msg, _msg
    assert '(--configure)' not in _msg, _msg



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



def test_buildsystem_password_failure_reports_stderr():
    """Regression (audit #37): the sudo password-validation failures (__init__
    and for_iso) must surface _proc.stderr — where sudo writes 'Sorry, try
    again.' / 'not in the sudoers file' — not the empty stdout, matching the
    wipe handler.  Reading stdout printed a blank reason."""
    import inspect
    import re
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import buildsystem
    _msgs = re.findall(
        r'Incorrect password or user not in sudoers file: \{([^}]*)\}',
        inspect.getsource(buildsystem))
    assert _msgs, "password-validation error messages not found"
    for _m in _msgs:
        assert '_proc.stderr' in _m, (
            f"password-failure message must read _proc.stderr (sudo writes its "
            f"diagnostics there), got: {_m}")



def test_mat09_live_root_password_randomized_and_sudo_guaranteed():
    """MAT-09: the base chroot no longer ships the fixed `root`/`root` default
    credential — systemd-firstboot gets a per-build random password, and a
    `%sudo NOPASSWD` sudoers.d guarantees the live user (added to `sudo` by
    live-config) can still escalate (else a random root pw would lock it out)."""
    _src = open(os.path.join(_ROOT, 'scripts', 'chroot.py')).read()
    assert '--root-password=root' not in _src, "the fixed root/root must be gone"
    assert 'secrets.token_urlsafe' in _src, "root password must be randomized"
    assert '--root-password={_root_pw}' in _src, "firstboot must use the random pw"
    assert '/etc/sudoers.d/90-athena-nopasswd' in _src
    assert '%sudo ALL=(ALL:ALL) NOPASSWD:ALL' in _src



def test_install_signing_keyring_skips_when_no_key_generated():
    """No keyring file at signing.signing_pubkey_path → method returns
    silently with an INFO log; no subprocess invocations.  Operator may
    not have generated a key yet — the chroot build must keep going."""
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




def test_write_athena_apt_sources_noop_when_no_mirrors():
    """No mirrors registered → no apt source file (target relies on
    /cdrom/pool added at install time)."""
    inst, writes = _stub_chroot_mixin_for_apt_source(build_codename='thor')
    inst._write_athena_apt_sources()
    assert writes == []



def test_live_chroot_sources_list_is_self_contained():
    """Per project_self_contained_repo: the live chroot's
    /etc/apt/sources.list must NOT carry active `deb` lines pointing
    at upstream mirrors.  The header is a comment-only template;
    reference lines are commented; the network apt-source path goes
    through per-mirror sources.list.d/athena-<name>.list files written
    from the registered-mirror state.

    Caught 2026-06-01 live ISO test: every cfg.mirrors entry was
    being written as an active `deb` line → apt-update on the
    booted live system hit deb.debian.org.  Self-contained
    invariant violated.

    FORK-02 (2026-06-13): tightened further — the chroot
    sources.list must carry NO mirror-URL interpolation at all,
    not even commented `# deb {…}` reference lines.  On a
    Debian-upstream build those rendered as literal
    `deb http://deb.debian.org/debian …` strings (and disclosed
    the upstream codename) inside the shipped system file — found
    in the live ISO residue audit."""
    import inspect, re, sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import chroot
    _src = inspect.getsource(chroot._ChrootMixin.generate_system_configs)
    # No `deb` URL line composed for sources.list — neither an
    # interpolated `deb {…}` nor a literal `deb scheme://…`, active
    # OR commented (FORK-02).  Either re-introduces upstream URLs into
    # the shipped image.  `deb\s+` (whitespace required) deliberately
    # does NOT match the policy prose "never default to deb.debian.org".
    _deb_lines = re.findall(r"deb\s+(?:\{|[a-z]+://)\S*", _src)
    assert not _deb_lines, (
        f"generate_system_configs composes `deb` URL lines for "
        f"sources.list: {_deb_lines}.  Per FORK-02 the chroot "
        f"sources.list must carry no upstream `deb` lines at all "
        f"(active or commented); the network apt source goes through "
        f"sources.list.d/athena.list (see _write_athena_apt_source)."
    )
    # The header must call out the self-contained policy so a
    # future contributor doesn't reintroduce the bug.
    assert 'self-contained' in _src.lower(), (
        "sources.list header must mention the self-contained policy"
    )



def test_generate_system_configs_machine_id_stays_empty_for_first_boot():
    """Regression (audit #48): /etc/machine-id must ship EMPTY so each clone
    generates a unique id on first boot.  generate_system_configs must write it
    empty AND must NOT pass --setup-machine-id to systemd-firstboot, which would
    commit a concrete random id that disk_image.py rsyncs (-aHAX) verbatim into
    every image — so all clones would share one machine-id (DHCP collisions,
    journal/systemd identity bleed)."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import chroot
    _src = inspect.getsource(chroot._ChrootMixin.generate_system_configs)
    assert "_write_chroot_file('/etc/machine-id', '')" in _src, (
        "machine-id must be written empty for first-boot generation")
    # Match the quoted argv literal, not a prose mention in a comment.
    assert "'--setup-machine-id'" not in _src, (
        "systemd-firstboot must NOT pass --setup-machine-id — it overrides the "
        "empty /etc/machine-id with a fixed id shared across every clone")



def test_sta42_chroot_wipe_cannot_delete_host_dev_nodes():
    """STA-42: <chroot>/dev is `mount --bind /dev` — the HOST's /dev.  A
    crashed prior run can leave it (stacked) mounted, so the recursive chroot
    wipe must (1) unmount first, peeling ALL stacked layers, and (2) pass
    `-xdev` so `find -delete` never descends across a surviving mount into
    host /dev/{null,sda,console,…}.  Source pin (privileged path)."""
    import inspect, sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import chroot, buildsystem

    # _umount_chroot_fs peels every stacked layer (bounded loop on ismount)
    _um = inspect.getsource(chroot._ChrootMixin._umount_chroot_fs)
    assert 'if not os.path.ismount' in _um, \
        "_umount_chroot_fs must loop until the layer is gone (peel stacks)"
    assert ('for _' in _um or 'while' in _um) and 'umount' in _um, _um

    # the chroot wipe unmounts first AND uses -xdev on the find -delete
    _init = inspect.getsource(buildsystem.BuildSystem.__init__)
    _i = _init.find("'find', self._dir_chroot")
    assert _i != -1, "chroot wipe `find -delete` not found"
    _wipe = _init[_i:_i + 120]
    assert "'-xdev'" in _wipe and "'-delete'" in _wipe, _wipe
    _u = _init.find('self._umount_chroot_fs()')
    assert _u != -1 and _u < _i, \
        "wipe must call _umount_chroot_fs() BEFORE find -delete"



def test_sta41_write_chroot_file_ships_world_readable_mode():
    """STA-41: _write_chroot_file must set an explicit 0644 mode via `sudo
    install -m 644`, not `cp` — cp propagates the tempfile's 0600 mode when
    the destination doesn't pre-exist, shipping root-only /etc/machine-id,
    /etc/hostname, /etc/fstab, and apt sources into the live image."""
    import inspect, sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import chroot
    _src = inspect.getsource(chroot._ChrootMixin._write_chroot_file)
    assert "'install', '-m', '644'" in _src, _src
    assert "'cp', _tmp_path" not in _src, "still uses cp (propagates 0600)"



def test_chroot_build_wires_both_audit_gates_with_no_gate_bypass():
    """P5 (2026-05-23): both chroot build live + installer must
    pre-flight `source audit` AND `repo audit`, and both must honour
    a `no-gate` arg to bypass the gates.  Pin the structural shape
    of both methods."""
    _body = _session_source()
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



def test_compute_install_batches_install_set_param():
    """SURFACES-01: the surface closure (install_set) is the sole scoping
    authority — a member inside the set IS installed; one outside is not.
    None (unit-test convenience; every production caller passes a surface)
    leaves the graph unrestricted over the selected set."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import types
    from chroot import _ChrootMixin
    _pkgs = {
        'base-a':   _SurfPkg('base-a'),
        'gnome-b':  _SurfPkg('gnome-b', depends=['base-a']),
        'pool-c':   _SurfPkg('pool-c'),
    }
    _dt = types.SimpleNamespace(selected_pkgs=_pkgs)
    _mix = _ChrootMixin.__new__(_ChrootMixin)
    _mix._dependencytree = _dt
    # None: unrestricted — every canonical selected package batches
    _all = [p for batch, _f in
            _mix._compute_install_batches(set()) for p in batch]
    assert set(_all) == {'base-a', 'gnome-b', 'pool-c'}, _all
    # surface set: pool-c outside the closure stays out
    _surface = {'base-a', 'gnome-b'}
    _batches = _mix._compute_install_batches(set(), install_set=_surface)
    _flat = [p for batch, _f in _batches for p in batch]
    assert set(_flat) == {'base-a', 'gnome-b'}, _flat
    # dep order: base-a before gnome-b
    assert _flat.index('base-a') < _flat.index('gnome-b')
    # libc seed still subtracted under install_set
    _b2 = _mix._compute_install_batches({'base-a'}, install_set=_surface)
    assert [p for batch, _f in _b2 for p in batch] == ['gnome-b']

TESTS = [
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
    test_compute_install_batches_dpkg_dash_closure_scheduled_first,
    test_build_chroot_retries_failed_unpacks_after_final_sweep_in_rounds,
    test_configure_chroot_final_pass_counts_stderr_failures,
    test_buildsystem_password_readable_before_scrub,
    test_buildsystem_scrub_password_clears_field,
    test_buildsystem_password_property_raises_after_scrub,
    test_buildsystem_scrub_password_idempotent,
    test_buildsystem_password_failure_reports_stderr,
    test_mat09_live_root_password_randomized_and_sudo_guaranteed,
    test_install_signing_keyring_skips_when_no_key_generated,
    test_install_signing_keyring_invokes_cp_when_key_present,
    test_install_signing_keyring_warns_on_cp_failure,
    test_write_athena_apt_sources_noop_when_no_mirrors,
    test_live_chroot_sources_list_is_self_contained,
    test_generate_system_configs_machine_id_stays_empty_for_first_boot,
    test_chroot_build_wires_both_audit_gates_with_no_gate_bypass,
    test_sta42_chroot_wipe_cannot_delete_host_dev_nodes,
    test_sta41_write_chroot_file_ships_world_readable_mode,
    test_compute_install_batches_install_set_param,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
