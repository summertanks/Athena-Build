"""Athena tests — local repo + audits + supply chain (apt_repo.py, repo_audit.py, cmd_repo.py, cmd_audit.py, signing.py, sbom.py, release_index.py).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile

from _test_helpers import (  # noqa: F401
    _BASE_CONF_BODY,
    _ROOT,
    _build_config_from,
    _build_minimal_deb,
    _make_buildcontainer_stub,
    _make_fake_popen,
    _sbom_test_buildconfig,
    _sbom_test_src,
    _session_source,
    _stub_signed_manifest_gpg,
    _wrap_with_install_handler,
    _write_test_config,
)




def test_signing_import_key_round_trip_real_gpg():
    """INTEGRATION: generate a key, export the pubkey, import it into a fresh
    homedir via signing.import_key; missing file → clean error."""
    import shutil
    import subprocess
    import tempfile
    if shutil.which('gpg') is None:
        return
    import signing
    from signing import generate_key, signing_pubkey_path
    with tempfile.TemporaryDirectory() as tmp:
        class _Cfg:
            dir_gnupg = os.path.join(tmp, 'gnupg')
            signing_key_uid = 'Athena Test <test@athena.local>'
        assert generate_key(_Cfg(), _key_length=2048) is True
        _pub = signing_pubkey_path(_Cfg())
        _dest = os.path.join(tmp, 'home2')
        _ok, _det = signing.import_key(_dest, _pub)
        assert _ok, _det
        _r = subprocess.run(['gpg', '--homedir', _dest, '--list-keys'],
                            capture_output=True, text=True)
        assert 'Athena Test' in _r.stdout, _r.stdout
        # Missing file → clean failure.
        _ok2, _det2 = signing.import_key(_dest, os.path.join(tmp, 'nope'))
        assert _ok2 is False and 'not found' in _det2



def test_signing_export_public_keyring_from_existing_key():
    """export_public_keyring re-creates the public keyring from the key already
    in the signing homedir — the federation case (key imported, keyring never
    exported) — without regenerating the key."""
    import shutil
    import subprocess
    import tempfile
    if shutil.which('gpg') is None:
        return
    from signing import (export_public_keyring, generate_key,
                         signing_pubkey_path)
    with tempfile.TemporaryDirectory() as tmp:
        class _Cfg:
            dir_gnupg = os.path.join(tmp, 'gnupg')
            signing_key_uid = 'Athena Test <test@athena.local>'
        assert generate_key(_Cfg(), _key_length=2048) is True
        _pub = signing_pubkey_path(_Cfg())
        os.remove(_pub)                      # simulate the federation gap
        assert not os.path.isfile(_pub)
        _ok, _det = export_public_keyring(_Cfg())   # re-export from the key
        assert _ok, _det
        assert os.path.isfile(_pub) and os.path.getsize(_pub) > 0
        _r = subprocess.run(
            ['gpg', '--no-default-keyring', '--keyring', _pub, '--list-keys'],
            capture_output=True, text=True)
        assert 'Athena Test' in _r.stdout, _r.stdout



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
        # SEC-07: the per-subdir Release writer installs a user-owned
        # tempfile via `sudo install -m 644 <tmp> <dst>` — the password
        # never shares stdin with file content.  Mirror the copy so the
        # Release actually lands for downstream existence checks.
        elif cmd[0] == 'install' and len(cmd) >= 5:
            try:
                with open(cmd[3]) as _s, open(cmd[4], 'w') as _d:
                    _d.write(_s.read())
            except OSError:
                pass
        _r = MagicMock()
        _r.returncode = 0
        _r.stderr = ''
        return _r

    def _fake_subprocess_run(cmd, *a, **kw):
        _calls.append(tuple(cmd))
        # apt-ftparchive release: real apt-ftparchive writes to stdout;
        # the helper redirects via stdout=<file handle> kwarg.  Mirror
        # by writing a stub Release to that handle so size > 0 check passes.
        if (cmd[:2] == ['sudo', '-S'] and len(cmd) > 2 and cmd[2] in (
              'apt-ftparchive', 'dpkg-scanpackages', 'dpkg-scansources')):
            # STA-40: apt-ftparchive + dpkg-scan* now run argv-form with the
            # output going to the stdout=<handle> kwarg.
            _stdout = kw.get('stdout')
            if _stdout is not None and hasattr(_stdout, 'write'):
                _stdout.write(b'Package: stub\nVersion: 1.0\n\n'
                              if 'scan' in cmd[2] else
                              b'Suite: stub\nCodename: stub\n')
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



def test_write_subdir_release_never_mixes_password_with_content():
    """SEC-07 regression pin: _write_subdir_release must deliver the
    file content via a user-owned tempfile + `sudo install -m 644`,
    NEVER on the same stdin as the sudo password.  The previous
    `sudo -S bash -c "cat > path"` + `input=password+'\\n'+content`
    pattern wrote the password as line 1 of every per-component
    Release whenever the sudo credential was already cached (`sudo -S`
    consumes the stdin line only when it actually authenticates) —
    found leaked in published artifacts 2026-06-12.

    Drives the REAL _sudo helper with subprocess.run mocked, so the
    pin covers the full stdin composition, not just the argv shape."""
    import sys, tempfile
    from unittest.mock import patch, MagicMock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo

    _password = 'hunter2-secret'
    _inputs = []

    def _fake_run(argv, *a, **kw):
        _inputs.append((tuple(argv), kw.get('input', '')))
        if (isinstance(argv, (list, tuple)) and len(argv) >= 7
                and argv[:3] == ['sudo', '-S', 'install']):
            with open(argv[5]) as _s, open(argv[6], 'w') as _d:
                _d.write(_s.read())
        _r = MagicMock()
        _r.returncode = 0
        _r.stderr = ''
        return _r

    with tempfile.TemporaryDirectory() as _dir:
        with patch.object(apt_repo.subprocess, 'run', side_effect=_fake_run):
            _ok = apt_repo._write_subdir_release(
                _dir, 'thor', 'thor', 'main', 'amd64', _password)
        assert _ok is True
        _release = os.path.join(_dir, 'Release')
        assert os.path.isfile(_release), "Release file not written"
        with open(_release) as _fh:
            _text = _fh.read()
        assert _text.startswith('Origin: '), (
            f"Release must start with a deb822 field, got first line "
            f"{_text.splitlines()[0]!r}"
        )
        assert _password not in _text, (
            "sudo password leaked into the Release file content"
        )
    # Every subprocess stdin must be the bare password line (sudo -S
    # consumption) — file content must never ride the same stdin.
    for _argv, _input in _inputs:
        assert _input == _password + '\n', (
            f"stdin for {_argv[:4]} carries more than the password: "
            f"{_input!r}"
        )
        assert 'bash' not in _argv, (
            f"shell wrapper crept back into the Release writer: {_argv}"
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
        # SEC-07: per-subdir Release lands via `install -m 644 <tmp> <dst>`.
        elif cmd[0] == 'install' and len(cmd) >= 5:
            try:
                with open(cmd[3]) as _s, open(cmd[4], 'w') as _d:
                    _d.write(_s.read())
            except OSError:
                pass
        _r = type('R', (), {})()
        _r.returncode = 0
        _r.stderr = ''
        return _r

    def _fake_subprocess_run(cmd, *_a, **kw):
        _calls.append(tuple(cmd))
        if (cmd[:2] == ['sudo', '-S'] and len(cmd) > 2 and cmd[2] in (
                'apt-ftparchive', 'dpkg-scanpackages', 'dpkg-scansources')):
            # STA-40: apt-ftparchive + dpkg-scan* now run argv-form with the
            # output going to the stdout=<handle> kwarg.
            _stdout = kw.get('stdout')
            if _stdout is not None and hasattr(_stdout, 'write'):
                _stdout.write(b'Package: stub\nVersion: 1.0\n\n'
                              if 'scan' in cmd[2] else
                              b'Suite: stub\nCodename: stub\n')
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
        # SEC-07: per-subdir Release lands via `install -m 644 <tmp> <dst>`.
        elif cmd[0] == 'install' and len(cmd) >= 5:
            try:
                with open(cmd[3]) as _s, open(cmd[4], 'w') as _d:
                    _d.write(_s.read())
            except OSError:
                pass
        _r = type('R', (), {})()
        _r.returncode = 0
        _r.stderr = ''
        return _r

    def _fake_subprocess_run(cmd, *_a, **kw):
        _calls.append(tuple(cmd))
        if (cmd[:2] == ['sudo', '-S'] and len(cmd) > 2 and cmd[2] in (
                'apt-ftparchive', 'dpkg-scanpackages', 'dpkg-scansources')):
            # STA-40: apt-ftparchive + dpkg-scan* run argv-form, output via
            # the stdout=<handle> kwarg.
            _stdout = kw.get('stdout')
            if _stdout is not None and hasattr(_stdout, 'write'):
                _stdout.write(b'Package: stub\nVersion: 1.0\n\n'
                              if 'scan' in cmd[2] else b'Components: stub\n')
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



def test_apt_repo_generate_repo_indexes_udeb_scan_allows_empty():
    """Regression (audit #18, apt_repo.py:307-311): the udeb (debian-installer)
    Packages scan in generate_repo_indexes must pass allow_empty=True, matching
    the sibling generate_apt_repo.  The debian-installer/binary-<arch> dir is
    created unconditionally at BuildConfig init, but udebs only come from the
    installer pipeline — so a repo with .debs but no udebs has a present-but-
    empty dir.  Without allow_empty, dpkg-scanpackages' zero-entry output is
    treated as a failure (_run_dpkg_scan, allow_empty=False branch) and
    _scan_packages_to → generate_repo_indexes → cmd_index_repo aborts the whole
    publish."""
    import inspect
    import re
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo
    _src = inspect.getsource(apt_repo.generate_repo_indexes)
    # The udeb scan call passes udeb=True; the fix appends allow_empty=True
    # immediately after it (matching generate_apt_repo).  Keying on the
    # adjacency avoids brittle balanced-paren matching across the inner
    # os.path.join(...) argument.
    assert 'udeb=True' in _src, (
        'udeb _scan_packages_to call not found in generate_repo_indexes')
    assert re.search(r'udeb=True,\s*allow_empty=True', _src), (
        "generate_repo_indexes udeb scan must pass allow_empty=True (parity "
        "with generate_apt_repo) — a present-but-empty debian-installer/ dir "
        "is valid and must not abort the publish")
    # And confirm the sibling it mirrors still sets it, so the parity claim
    # this test pins can't silently rot.
    _sib = inspect.getsource(apt_repo.generate_apt_repo)
    assert re.search(r'udeb=True,\s*allow_empty=True', _sib), (
        "generate_apt_repo (the parity reference) no longer sets "
        "allow_empty=True on its udeb scan")


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



def test_sta40_no_shell_interpolation_in_sudo_sites():
    """STA-40: the live `sudo bash -c f"…"` sites are argv-form now —
    _run_dpkg_scan (argv + stdout handle, stderr CAPTURED not 2>/dev/null'd),
    sfdisk + the cpio initrd pipeline (askpass + argv).  utils.sudo_askpass_env
    supplies the password via a 0700 helper so the command's stdin stays free."""
    import sys as _sys, subprocess as _sp, inspect as _inspect
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils, apt_repo, disk_image, iso_installer

    # sudo_askpass_env yields a usable askpass helper; cleaned up after
    with utils.sudo_askpass_env('s3cr3t! $x`') as _env:
        _ap = _env['SUDO_ASKPASS']
        assert os.path.isfile(_ap)
        assert (os.stat(_ap).st_mode & 0o777) == 0o700
        # the helper echoes the password VERBATIM (metachars survive — that's
        # the whole point vs interpolating into a shell)
        _out = _sp.run([_ap], env=_env, capture_output=True, text=True).stdout
        assert _out == 's3cr3t! $x`', repr(_out)
    assert not os.path.exists(_ap), "askpass helper not cleaned up"

    # live sites are de-shelled (no `bash -c`)
    _scan = _inspect.getsource(apt_repo._run_dpkg_scan)
    assert "'bash', '-c'" not in _scan, _scan
    # stderr CAPTURED (PIPE), no longer redirected to /dev/null in a shell
    assert 'stderr=subprocess.PIPE' in _scan, "_run_dpkg_scan must capture stderr"
    assert "'2>/dev/null'" not in _scan and '2>/dev/null > ' not in _scan, \
        "stderr is captured now, not discarded via a shell redirect"
    _di = _inspect.getsource(disk_image.build_disk_image)
    assert "'bash', '-c'" not in _di and 'sudo_askpass_env' in _di, _di
    _initrd = _inspect.getsource(iso_installer._build_initrd)
    assert "'bash', '-c'" not in _initrd and 'sudo_askpass_env' in _initrd, \
        "cpio initrd pipeline still shells out"



def test_sta48_cve_report_path_never_overwrites_input_sbom():
    """STA-48: the cve report path must never equal the input SBOM.  The old
    `sbom_path.replace('.cdx.json', '.cve.json')` was a no-op on any name not
    ending `.cdx.json` (e.g. sbom.json), so the grype report clobbered the
    operator's SBOM via open('w')."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from commands.cmd_supply_chain import SupplyChainCommandsMixin
    _f = SupplyChainCommandsMixin._cve_report_path
    # canonical .cdx.json double-suffix strip (unchanged behaviour)
    assert _f('/x/athena-1-snap-amd64.cdx.json') == \
        '/x/athena-1-snap-amd64.cve.json'
    # the regression cases — NOT a no-op, distinct from the input
    assert _f('/tmp/sbom.json') == '/tmp/sbom.cve.json'
    assert _f('/tmp/sbom') == '/tmp/sbom.cve.json'
    assert _f('/a/b.cve.json') == '/a/b.cve.cve.json'
    # invariant: the report path never collides with the input SBOM
    for _p in ('/x/foo.cdx.json', '/tmp/sbom.json', '/tmp/sbom',
               '/a/b.cve.json', 'relative.json'):
        assert os.path.abspath(_f(_p)) != os.path.abspath(_p), _p



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



def test_signing_verify_key_uses_on_disk_uid_not_config_peer_onboarding():
    """INTEGRATION regression (audit #8, signing.py:327-345): verify_key must
    resolve the UID of the key ACTUALLY on disk (actual_signing_uid), not
    config.signing_key_uid.  A federation peer imports the origin's tier-1 key
    whose UID differs from the peer's machine-local default; the old code
    filtered get_key_info + --local-user by the configured UID, so the
    freshly-imported key was falsely reported unusable and onboarding aborted.
    Generate a key under one UID, point config at a DIFFERENT UID, assert
    verify_key still succeeds.  Skipped silently if gpg is absent."""
    import shutil
    import tempfile
    if shutil.which('gpg') is None:
        return
    from signing import generate_key, verify_key
    with tempfile.TemporaryDirectory() as tmp:
        _home = os.path.join(tmp, 'gnupg')

        class _GenCfg:                       # the origin: generates the key
            dir_gnupg = _home
            signing_key_uid = 'Origin Federation <origin@example.org>'
        assert generate_key(_GenCfg(), _key_length=2048) is True

        class _PeerCfg:                      # the peer: same homedir (imported
            dir_gnupg = _home                # key), but a stale/default UID it
            signing_key_uid = 'Athena Build <athena@local>'   # never updated
        ok, msg = verify_key(_PeerCfg())
        assert ok, msg                       # must NOT fail on UID mismatch



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



def test_detect_dangling_asg_equals_pins_classifies_cross_source_pin():
    """The `=`-pin detector flags ONLY the actionable subclass: a bare
    pristine exact pin `T (= V)` whose repo target is `V+asg<R>u<N>`.
    A pin that already carries +asg (a restamped sibling) is satisfied and
    a pristine pin satisfied by a pristine target are both left alone."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from repo_audit import detect_dangling_asg_equals_pins, RepoState

    _state = RepoState(
        packages={
            # consumer A: cross-source pin stripped to pristine; target is
            # stamped → DANGLES → must be flagged.
            'aconsumer': {'Package': 'aconsumer', 'Version': '1.0-1',
                          'Depends': 'libfoo (= 2.0-1)'},
            'libfoo':    {'Package': 'libfoo', 'Version': '2.0-1+asg1u3'},
            # consumer B: sibling pin already restamped to +asg → satisfied,
            # NOT flagged.
            'bconsumer': {'Package': 'bconsumer', 'Version': '3.0-1+asg1u2',
                          'Pre-Depends': 'libbar (= 3.0-1+asg1u2)'},
            'libbar':    {'Package': 'libbar', 'Version': '3.0-1+asg1u2'},
            # consumer C: pristine pin satisfied by a pristine target →
            # NOT flagged (no asg involved).
            'cconsumer': {'Package': 'cconsumer', 'Version': '4.0-1',
                          'Depends': 'libbaz (= 5.0-1)'},
            'libbaz':    {'Package': 'libbaz', 'Version': '5.0-1'},
        },
        provides_index={},
        packages_file='',
        repo_mtime=0.0,
    )
    _findings = detect_dangling_asg_equals_pins(_state)
    assert len(_findings) == 1, f"expected exactly 1 finding, got {_findings}"
    _consumer, _field, _target, _pinned, _avail, _remedy = _findings[0]
    assert _consumer == 'aconsumer'
    assert _field == 'Depends'
    assert _target == 'libfoo'
    assert _pinned == '2.0-1'
    assert _avail == '2.0-1+asg1u3'
    assert '>= 2.0-1' in _remedy and '2.0-1+asg1u3' in _remedy



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



def test_repo_index_prints_post_run_summary():
    """`cmd_index_repo` calls _print_repo_index_summary after a
    successful index run.  Summary surfaces per-suite Release sig
    state + per-component file counts + total payload — quick at-a-
    glance confirmation the index landed completely.
    """
    _body = _session_source()
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
        # STA-40: argv form — the scanner's stdout is the kw['stdout'] handle
        # (binary).  Write the stub stanzas there so the move + checks see
        # content.  Anything else (the `sudo install` follow-up) returns rc=0.
        if (isinstance(argv, (list, tuple))
                and any('dpkg-scan' in str(_x) for _x in argv)):
            _stdout = kw.get('stdout')
            if _stdout is not None and hasattr(_stdout, 'write'):
                _stdout.write(_stub_packages.encode())
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



def test_format_gpg_time_survives_overflow_epoch():
    """Regression (audit #181): a huge epoch (int out of time_t range) raises
    OverflowError, not ValueError/OSError — format_gpg_time must degrade to the
    raw string, not propagate."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from signing import format_gpg_time
    _big = '9' * 30
    assert format_gpg_time(_big) == _big          # must not raise



def test_cmd_repo_cleanup_report_lists_all_scanned_components():
    """Regression (audit #68): the cleanup summary's scanned-component list must
    track utils._STALE_SCAN_SUBDIRS (it hardcoded {main,doc,dbgsym,tests},
    omitting main-udeb that _scan_stale_files actually scans)."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import commands.cmd_repo as _cr
    _src = inspect.getsource(_cr)
    assert '{main,doc,dbgsym,tests}' not in _src, (
        "scanned-component report hardcodes a list that drifts from "
        "_STALE_SCAN_SUBDIRS (missing main-udeb)")
    assert '_STALE_SCAN_SUBDIRS' in _src, (
        "report must derive the component list from _STALE_SCAN_SUBDIRS")



def test_repo_dispatcher_advertises_merged_package_actions():
    """After the package→repo merge, the cmd_repo dispatcher exposes the
    consolidated actions (audit, repair).  audit_nmu was absorbed into
    'audit' itself; strip/cleanup moved under 'repair'.  index/tunnel were
    retired (auto-index / `source tunnel`) and their redirect hints
    removed, so they're no longer dispatched here."""
    _body = _session_source()
    import re
    # Body of cmd_repo.
    _m = re.search(
        r"\n    def cmd_repo\b.*?(?=\n    def \w)",
        _body, re.DOTALL,
    )
    assert _m, "cmd_repo dispatcher not found"
    _disp = _m.group(0)
    for _action in ('audit', 'repair'):
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



def test_highest_stanza_per_pkg_arch_keeps_filename_and_dedups():
    """highest_stanza_per_pkg_arch retains filename/sha/size, picks the
    HIGHEST version per package, and keys by the DIRECTORY arch (so an
    Architecture: all udeb keys under its binary-<arch> dir, matching
    audit_packages_chain's index)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    # one binary-amd64 Packages: two versions of liba + an Architecture:all row
    _amd64 = (
        "Package: liba\nVersion: 1.0\nArchitecture: amd64\n"
        "Filename: pool/main/liba_1.0_amd64.deb\n"
        f"SHA256: {'a' * 64}\nSize: 100\n\n"
        "Package: liba\nVersion: 2.0\nArchitecture: amd64\n"
        "Filename: pool/main/liba_2.0_amd64.deb\n"
        f"SHA256: {'b' * 64}\nSize: 200\n\n"
        "Package: libdata\nVersion: 1.5\nArchitecture: all\n"
        "Filename: pool/main/libdata_1.5_all.deb\n"
        f"SHA256: {'d' * 64}\nSize: 50\n\n"
    )
    _m = repo_audit.highest_stanza_per_pkg_arch(_amd64, 'main', 'amd64')
    # newest liba wins
    assert _m['liba|amd64']['version'] == '2.0'
    assert _m['liba|amd64']['filename'] == 'liba_2.0_amd64.deb'
    assert _m['liba|amd64']['sha256'] == 'b' * 64
    assert _m['liba|amd64']['size'] == 200
    assert _m['liba|amd64']['component'] == 'main'
    # Architecture:all keyed under the DIR arch (amd64), not 'all'
    assert _m['libdata|amd64']['filename'] == 'libdata_1.5_all.deb'
    assert set(_m) == {'liba|amd64', 'libdata|amd64'}
    # merge a second dir (arm64) into the accumulator → distinct key
    _arm64 = ("Package: liba\nVersion: 1.0\nArchitecture: arm64\n"
              "Filename: pool/main/liba_1.0_arm64.deb\n"
              f"SHA256: {'c' * 64}\nSize: 150\n\n")
    repo_audit.highest_stanza_per_pkg_arch(_arm64, 'main', 'arm64', into=_m)
    assert _m['liba|arm64']['filename'] == 'liba_1.0_arm64.deb'
    assert set(_m) == {'liba|amd64', 'libdata|amd64', 'liba|arm64'}



def test_published_ledger_entries_returns_full_set_arch_filtered():
    """published_ledger_entries walks dists/<codename>/**/binary-*/Packages,
    derives component from path, and returns the FULL published set (latest
    per pkg|arch) — NOT closure-limited (so out-of-closure -dev/-udeb are
    INCLUDED) — with foreign cross-toolchains the ONLY exclusion (arch filter).
    """
    import sys
    import types
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    with tempfile.TemporaryDirectory() as _td:
        _root = os.path.join(_td, 'dists', 'thor')
        _main = os.path.join(_root, 'main', 'binary-amd64')
        _udeb = os.path.join(_root, 'main', 'debian-installer', 'binary-amd64')
        os.makedirs(_main)
        os.makedirs(_udeb)
        with open(os.path.join(_main, 'Packages'), 'w') as _f:
            _f.write(
                "Package: liba\nVersion: 1.0\nArchitecture: amd64\n"
                "Filename: pool/main/liba_1.0_amd64.deb\n"
                f"SHA256: {'a' * 64}\nSize: 100\n\n"
                # out-of-closure -dev: MUST be included now (full set)
                "Package: libextra-dev\nVersion: 1.0\nArchitecture: amd64\n"
                "Filename: pool/main/libextra-dev_1.0_amd64.deb\n"
                f"SHA256: {'f' * 64}\nSize: 10\n\n"
                # foreign cross-toolchain: MUST be excluded (arch filter)
                "Package: binutils-aarch64-linux-gnu\nVersion: 2.40\n"
                "Architecture: amd64\n"
                "Filename: pool/main/binutils-aarch64-linux-gnu_2.40"
                "_amd64.deb\n"
                f"SHA256: {'c' * 64}\nSize: 200\n\n")
        with open(os.path.join(_udeb, 'Packages'), 'w') as _f:
            _f.write(
                "Package: cdrom-detect\nVersion: 1.5\nArchitecture: amd64\n"
                "Filename: pool/main/cdrom-detect_1.5_amd64.udeb\n"
                f"SHA256: {'b' * 64}\nSize: 50\n\n")
        _cfg = types.SimpleNamespace(
            dir_repo=_td, build_codename='thor', arch='amd64')
        _entries = repo_audit.published_ledger_entries(_cfg)
        # FULL set: closure AND out-of-closure -dev/-udeb all present
        assert 'liba|amd64' in _entries
        assert 'libextra-dev|amd64' in _entries, \
            "out-of-closure -dev must be INCLUDED (full set, not closure)"
        assert 'cdrom-detect|amd64' in _entries
        assert _entries['cdrom-detect|amd64']['component'] == \
            'main/debian-installer'
        # arch filter: foreign cross-toolchain excluded
        assert 'binutils-aarch64-linux-gnu|amd64' not in _entries, \
            "foreign-arch cross-toolchain must be excluded"



def test_repo_state_from_packages_text_resolves_depends():
    """repo_state_from_packages_text builds a RepoState carrying Depends so
    audit_dep_closure can re-resolve the published closure: a satisfied dep
    yields no unresolved; a missing dep is reported."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    _ok_text = (
        "Package: liba\nVersion: 1.0\nArchitecture: amd64\n"
        "Filename: pool/main/liba_1.0_amd64.deb\nDepends: libb\n\n"
        "Package: libb\nVersion: 1.0\nArchitecture: amd64\n"
        "Filename: pool/main/libb_1.0_amd64.deb\n\n"
    )
    _state = repo_audit.repo_state_from_packages_text(_ok_text)
    assert set(_state.packages) == {'liba', 'libb'}
    _unresolved, _weak = repo_audit.audit_dep_closure(
        _state, consumer_set=frozenset({'liba'}))
    assert _unresolved == [], _unresolved
    # drop libb → liba's hard dep is now unsatisfiable
    _bad_text = (
        "Package: liba\nVersion: 1.0\nArchitecture: amd64\n"
        "Filename: pool/main/liba_1.0_amd64.deb\nDepends: libb\n\n"
    )
    _state2 = repo_audit.repo_state_from_packages_text(_bad_text)
    _unresolved2, _ = repo_audit.audit_dep_closure(
        _state2, consumer_set=frozenset({'liba'}))
    assert any(_t[0] == 'liba' for _t in _unresolved2), _unresolved2



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
    from commands.cmd_audit import _dedupe_bidirectional_conflicts
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
    build.py: the RepoState dataclass, plus the callables
    scan_repo_state / audit_dep_closure / audit_conflict_cohort /
    audit_nmu_residue / invalidate_cache."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    for _name in (
        'RepoState',
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
    _body = _session_source()
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
            "If this passes, the cache-resolution path is doing "
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



def test_sta43_durable_state_writes_atomic_and_preserve_mode():
    """STA-43: snapshot.state / build.conf / SBOM route through
    `_atomic_write_bytes` (temp + fsync + os.replace) so a crash mid-write
    can't truncate them, and an operator-set mode (e.g. group-writable
    0o664) survives the rewrite rather than being forced to 0o644."""
    import sys as _sys, json as _json, inspect as _inspect
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils, sbom

    # _existing_mode: real mode when present, default when absent
    with tempfile.TemporaryDirectory() as _td:
        assert utils._existing_mode(
            os.path.join(_td, 'nope'), default=0o600) == 0o600
        _f = os.path.join(_td, 'f')
        with open(_f, 'w') as _fh:
            _fh.write('x')
        os.chmod(_f, 0o664)
        assert utils._existing_mode(_f) == 0o664
        # _atomic_write_bytes honours mode + leaves no temp behind
        _g = os.path.join(_td, 'g')
        utils._atomic_write_bytes(_g, b'data', mode=0o664)
        assert open(_g).read() == 'data'
        assert (os.stat(_g).st_mode & 0o777) == 0o664
        assert not [n for n in os.listdir(_td) if n.endswith('.tmp')], \
            os.listdir(_td)

    # write_snapshot_state: valid JSON written, operator mode preserved
    with tempfile.TemporaryDirectory() as _td:
        _cfgdir = os.path.join(_td, 'config')
        os.makedirs(_cfgdir)
        _statef = os.path.join(_cfgdir, 'snapshot.state')
        with open(_statef, 'w') as _fh:
            _fh.write('{"current": "OLD"}\n')
        os.chmod(_statef, 0o664)
        _cfg = type('C', (), {'dir_config': _cfgdir})()
        utils.write_snapshot_state(_cfg, current='20260101T000000Z')
        assert _json.load(open(_statef))['current'] == '20260101T000000Z'
        assert (os.stat(_statef).st_mode & 0o777) == 0o664, \
            "write_snapshot_state must preserve the operator's mode"

    # reconcile_snapshot_pin: build.conf rewritten in place, mode preserved
    with tempfile.TemporaryDirectory() as _td:
        _cfgdir = os.path.join(_td, 'config')
        os.makedirs(_cfgdir)
        with open(os.path.join(_cfgdir, 'snapshot.state'), 'w') as _fh:
            _json.dump({'current': 'NEWTS'}, _fh)
        _confp = os.path.join(_td, 'build.conf')
        with open(_confp, 'w') as _fh:
            _fh.write("[Snapshot]\n    Timestamp = OLDTS\n")
        os.chmod(_confp, 0o664)
        _c = type('C', (), {
            'dir_config': _cfgdir, 'config_path': _confp,
            'snapshot_timestamp_config': 'OLDTS'})()
        assert utils.reconcile_snapshot_pin(_c) == ('OLDTS', 'NEWTS')
        assert 'Timestamp = NEWTS' in open(_confp).read()
        assert (os.stat(_confp).st_mode & 0o777) == 0o664, \
            "build.conf rewrite must preserve mode (was 0o664)"
        assert _c.snapshot_timestamp_config == 'NEWTS'

    # source pins: all three writers route through the atomic helper
    assert "open(_path, 'w')" not in _inspect.getsource(
        utils.write_snapshot_state)
    assert '_atomic_write_bytes' in _inspect.getsource(
        utils.write_snapshot_state)
    assert '_atomic_write_bytes' in _inspect.getsource(
        utils.reconcile_snapshot_pin)
    assert '_atomic_write_bytes' in _inspect.getsource(sbom.generate_cdx)



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



def test_audit_identity_scan_default_true():
    """[Audit] IdentityScan defaults to True when the section is absent —
    production builds keep the gate on without operator action."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    with tempfile.TemporaryDirectory() as tmp:
        mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
"""
        cfg_path = _write_test_config(
            tmp, _BASE_CONF_BODY.format(mirror_block=mirror_block),
        )
        bc = _build_config_from(tmp, cfg_path)
    assert bc.is_valid, bc.error_str
    assert bc.audit_identity_scan is True



def test_audit_identity_scan_explicit_false_parses():
    """An explicit `[Audit] IdentityScan = false` flips the flag."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    mirror_block = """
    [Mirror.main]
    Suffix =
    Component = main
"""
    body = _BASE_CONF_BODY.format(mirror_block=mirror_block) + (
        "\n    [Audit]\n    IdentityScan = false\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = _write_test_config(tmp, body)
        bc = _build_config_from(tmp, cfg_path)
    assert bc.is_valid, bc.error_str
    assert bc.audit_identity_scan is False



def test_provenance_stamped_into_iso_and_repo_metadata():
    """The toolchain version is stamped into the two shipped surfaces — the ISO
    (.disk/athena-build marker) and the repo (per-component Release
    X-Athena-Build-Version).  Source-pins (the value paths need sudo / a full
    ISO stage to exercise)."""
    import inspect
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import apt_repo
    import iso_installer
    _disk = inspect.getsource(iso_installer._stage_disk_info)
    assert "'athena-build'" in _disk and '_version.get_version()' in _disk, _disk
    _rel = inspect.getsource(apt_repo._write_subdir_release)
    assert 'X-Athena-Build-Version' in _rel and '_version.get_version()' in _rel, \
        _rel



def test_fetch_source_versions_cached_on_disk():
    """fetch_source_versions_at caches the floor Sources index on disk keyed by
    the (immutable) snapshot timestamp — re-downloading it every run was the
    audit-startup delay."""
    from unittest import mock
    import json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg = mock.Mock(dir_cache=_tmp, mirrors=[])
        # a pre-seeded cache file is served WITHOUT touching the network
        with open(os.path.join(_tmp, 'source-versions-20260101T0Z.json'),
                  'w') as _f:
            json.dump({'foo': '1.0'}, _f)
        with mock.patch.object(repo_audit.utils, '_http_session') as _hs:
            _r = repo_audit.fetch_source_versions_at(_cfg, '20260101T0Z')
        assert _r == {'foo': '1.0'}
        _hs.assert_not_called()                 # served from disk
        # a fresh timestamp (no mirrors) → empty result, written to cache
        _r2 = repo_audit.fetch_source_versions_at(_cfg, '20260202T0Z')
        assert _r2 == {}
        assert os.path.isfile(
            os.path.join(_tmp, 'source-versions-20260202T0Z.json'))



def test_published_ledger_memoised():
    """published_ledger memoises on the manifest's (path, mtime, size) so
    `source audit`'s repeated calls don't re-read/re-verify/re-parse it — the
    audit-startup delay."""
    from unittest import mock
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    repo_audit._PUBLISHED_LEDGER_CACHE.clear()
    try:
        with mock.patch('repo_audit.os.stat',
                        return_value=mock.Mock(st_mtime_ns=111, st_size=222)), \
                mock.patch.object(repo_audit, 'local_manifest_path',
                                  return_value='/m'), \
                mock.patch.object(repo_audit, 'read_published_manifest',
                                  return_value='Package: foo\nVersion: 1\n\n'
                                  ) as _rd, \
                mock.patch.object(repo_audit, 'parse_packages_to_ledger',
                                  return_value={'foo': ['1']}) as _pp:
            _r1 = repo_audit.published_ledger(mock.Mock())
            _r2 = repo_audit.published_ledger(mock.Mock())
        assert _r1 == {'foo': ['1']}
        assert _r2 is _r1                       # same cached object
        assert _rd.call_count == 1              # read ONCE despite two calls
        assert _pp.call_count == 1
    finally:
        repo_audit._PUBLISHED_LEDGER_CACHE.clear()



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



# ─────────────────────────────────────────────────────────────────────────────
# merge_packages_indexes — multi-version union (append-only, local wins)
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



def test_local_published_packages_text_concatenates_component_indices():
    """local_published_packages_text() aggregates every binary-*/Packages
    under repo/dists/<codename>/ (the text written to published.manifest as
    the +asg bump authority on publish)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    with tempfile.TemporaryDirectory() as _tmp:
        _repo = os.path.join(_tmp, 'repo')

        class _Cfg:
            dir_repo = _repo
            build_codename = 'thor'

        for _comp, _pkg in (('main', 'openssl'), ('non-free-firmware', 'amd64-microcode')):
            _d = os.path.join(_repo, 'dists', 'thor', _comp, 'binary-amd64')
            os.makedirs(_d)
            with open(os.path.join(_d, 'Packages'), 'w') as _f:
                _f.write(f'Package: {_pkg}\nVersion: 1.0+asg1u3\n\n')
        # a non-binary dir with no Packages must be ignored
        os.makedirs(os.path.join(_repo, 'dists', 'thor', 'main', 'source'))
        _txt = repo_audit.local_published_packages_text(_Cfg())
        assert 'Package: openssl' in _txt
        assert 'Package: amd64-microcode' in _txt
        assert '+asg1u3' in _txt



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
        # STA-40: argv form — the scanner writes to the kw['stdout'] handle.
        # Model an EMPTY scan: return rc=0 without writing (leave 0 bytes),
        # so the real code's open(_tmp,'wb') stays empty.
        if isinstance(argv, list) and any('dpkg-scan' in str(_a) for _a in argv):
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




def test_manifest_vs_remote_discrepancies():
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import repo_audit
    _local = {'openssl': ['3.0.15-1', '3.0.15-1+asg1u1'], 'libc6': ['2.36-9']}
    _remote = {'openssl': ['3.0.15-1'], 'curl': ['8.0-1']}
    _ol, _orr = repo_audit.manifest_vs_remote_discrepancies(_local, _remote)
    assert _ol == {'openssl=3.0.15-1+asg1u1', 'libc6=2.36-9'}, _ol
    assert _orr == {'curl=8.0-1'}, _orr



def test_sbom_emits_valid_cyclonedx_skeleton():
    """generate_cdx produces a CycloneDX 1.5 JSON with all required
    top-level fields + one component per source."""
    import sys, tempfile, json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import sbom
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as tmp:
        bc = _sbom_test_buildconfig(tmp)
        _dt = SimpleNamespace()
        _dt.selected_srcs = {
            'base-files':    _sbom_test_src('base-files',    '12.4',
                                             'sha-bf'),
            'openssl':       _sbom_test_src('openssl',       '3.0.15-1',
                                             'sha-ssl'),
        }
        _out = os.path.join(tmp, 'sbom.cdx.json')
        _path = sbom.generate_cdx(bc, _dt, out_path=_out)
        assert _path == _out
        with open(_out) as fh:
            doc = json.load(fh)
    assert doc['bomFormat']    == 'CycloneDX'
    assert doc['specVersion']  == '1.5'
    assert doc['version']      == 1
    assert doc['serialNumber'].startswith('urn:uuid:')
    assert doc['metadata']['component']['type']    == 'operating-system'
    assert doc['metadata']['component']['name']    == 'Asgard'
    assert doc['metadata']['component']['version'] == '1'
    _props = {p['name']: p['value']
              for p in doc['metadata']['component']['properties']}
    assert _props['athena:codename']  == 'thor'
    assert _props['athena:arch']      == 'amd64'
    assert _props['athena:base-id']   == 'asgard'
    assert len(doc['components']) == 2
    _by_name = {c['name']: c for c in doc['components']}
    assert _by_name['base-files']['version'] == '12.4'
    assert _by_name['base-files']['purl']    == 'pkg:deb/asgard/base-files@12.4'
    assert _by_name['base-files']['hashes']  == [
        {'alg': 'SHA-256', 'content': 'sha-bf'}
    ]



def test_sbom_components_sorted_by_name():
    """Components emit in lexical name order — deterministic across
    runs so diffs between consecutive SBOMs reflect actual source
    changes, not dict iteration order."""
    import sys, tempfile, json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import sbom
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as tmp:
        bc = _sbom_test_buildconfig(tmp)
        _dt = SimpleNamespace()
        # Dict insertion order intentionally NOT alphabetical.
        _dt.selected_srcs = {
            'zlib':       _sbom_test_src('zlib',       '1.2.13'),
            'apt':        _sbom_test_src('apt',        '2.6.1'),
            'libc6':      _sbom_test_src('libc6',      '2.36-9'),
        }
        _out = os.path.join(tmp, 'sbom.cdx.json')
        sbom.generate_cdx(bc, _dt, out_path=_out)
        with open(_out) as fh:
            doc = json.load(fh)
    _names = [c['name'] for c in doc['components']]
    assert _names == ['apt', 'libc6', 'zlib'], _names



def test_sbom_dedupes_udeb_only_sources_into_union():
    """A source present in BOTH dep_tree.selected_srcs and
    udeb_dep_tree.selected_srcs appears ONCE in the SBOM; sources
    unique to the udeb tree are added."""
    import sys, tempfile, json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import sbom
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as tmp:
        bc = _sbom_test_buildconfig(tmp)
        _dt = SimpleNamespace()
        _dt.selected_srcs = {
            'glibc':      _sbom_test_src('glibc',      '2.36-9'),
            'gcc-12':     _sbom_test_src('gcc-12',     '12.2.0-14'),
        }
        _udt = SimpleNamespace()
        _udt.selected_srcs = {
            'glibc':      _sbom_test_src('glibc',      '2.36-9'),  # shared
            'busybox':    _sbom_test_src('busybox',    '1.35.0'),  # udeb-only
        }
        _out = os.path.join(tmp, 'sbom.cdx.json')
        sbom.generate_cdx(bc, _dt, udeb_dep_tree=_udt, out_path=_out)
        with open(_out) as fh:
            doc = json.load(fh)
    _names = sorted(c['name'] for c in doc['components'])
    assert _names == ['busybox', 'gcc-12', 'glibc']



def test_sbom_patch_set_hash_zero_when_no_patches():
    """Pristine source (no patch/source/<name>/<ver>/ dir) gets
    athena:patch-count=0 and a deterministic empty-input sha256 in
    athena:patch-set-hash."""
    import sys, tempfile, json, hashlib
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import sbom
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as tmp:
        bc = _sbom_test_buildconfig(tmp)
        _dt = SimpleNamespace()
        _dt.selected_srcs = {
            'openssl': _sbom_test_src('openssl', '3.0.15-1'),
        }
        _out = os.path.join(tmp, 'sbom.cdx.json')
        sbom.generate_cdx(bc, _dt, out_path=_out)
        with open(_out) as fh:
            doc = json.load(fh)
    _props = {p['name']: p['value']
              for p in doc['components'][0]['properties']}
    assert _props['athena:patch-count']    == '0'
    # Empty patch list → patch_set_hash returns sha256 of "" — fixed.
    assert _props['athena:patch-set-hash'] == hashlib.sha256(b'').hexdigest()



def test_sbom_patch_set_hash_nonempty_when_patches_present():
    """A source with patches in patch/source/<name>/<ver>/ gets a
    real patch-set hash + patch-files property listing the names."""
    import sys, tempfile, json, hashlib
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import sbom
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as tmp:
        bc = _sbom_test_buildconfig(tmp)
        _pdir = os.path.join(bc.dir_patch_source, 'libfoo', '1.0')
        os.makedirs(_pdir)
        with open(os.path.join(_pdir, '9001-something.patch'), 'w') as fh:
            fh.write('--- a\n+++ b\n@@ -0 +1 @@\n+x\n')
        with open(os.path.join(_pdir, '9002-another.patch'), 'w') as fh:
            fh.write('--- a\n+++ b\n@@ -0 +1 @@\n+y\n')
        _dt = SimpleNamespace()
        _dt.selected_srcs = {
            'libfoo': _sbom_test_src('libfoo', '1.0'),
        }
        _out = os.path.join(tmp, 'sbom.cdx.json')
        sbom.generate_cdx(bc, _dt, out_path=_out)
        with open(_out) as fh:
            doc = json.load(fh)
    _props = {p['name']: p['value']
              for p in doc['components'][0]['properties']}
    assert _props['athena:patch-count'] == '2'
    assert _props['athena:patch-set-hash'] != hashlib.sha256(b'').hexdigest()
    assert _props['athena:patch-files']  == (
        '9001-something.patch, 9002-another.patch'
    )



def test_sbom_purl_format():
    """PURL conforms to the package-url deb-type convention:
    pkg:deb/<vendor>/<name>@<version>.  Vendor = build_base_id."""
    import sys, tempfile, json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import sbom
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as tmp:
        bc = _sbom_test_buildconfig(tmp)
        _dt = SimpleNamespace()
        _dt.selected_srcs = {
            'tasksel': _sbom_test_src('tasksel', '3.73+athena1'),
        }
        _out = os.path.join(tmp, 'sbom.cdx.json')
        sbom.generate_cdx(bc, _dt, out_path=_out)
        with open(_out) as fh:
            doc = json.load(fh)
    assert doc['components'][0]['purl'] == (
        'pkg:deb/asgard/tasksel@3.73+athena1'
    )



def test_sbom_empty_out_path_no_ops():
    """generate_cdx('', ...) returns '' and writes nothing.  Caller
    chooses the location; an empty path is a programming error
    surfaced via the empty return."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import sbom
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as tmp:
        bc = _sbom_test_buildconfig(tmp)
        _dt = SimpleNamespace()
        _dt.selected_srcs = {}
        _path = sbom.generate_cdx(bc, _dt, out_path='')
    assert _path == ''



def test_cve_skips_when_grype_absent():
    """cmd_cve must `shutil.which` grype and bail with an install hint
    when absent — grype is an OPTIONAL prerequisite (per build-system.sh)
    so the command should be a friendly no-op rather than a stack trace.
    Source-level pin since exercising the full method requires a
    BuildSession + Config + dir_image fixture."""
    _body = _session_source()
    import re
    _m = re.search(
        r'def cmd_cve\(self.*?(?=\n    def )', _body, re.DOTALL,
    )
    assert _m, 'cmd_cve definition not found'
    _ub = _m.group(0)
    assert "shutil.which('grype')" in _ub or "_shutil.which('grype')" in _ub, (
        "cmd_cve must check for grype on PATH before invoking"
    )
    assert 'install' in _ub.lower(), (
        "cmd_cve must surface install instructions when grype is absent"
    )



def test_release_index_manifest_and_html():
    """The release-index generator: releases.json carries the apt deb-line
    + resolved ISO URLs; index.html renders the same data; both derive from
    one manifest so they can't disagree."""
    import sys as _sys, json as _json
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import release_index

    _m = release_index.build_release_manifest(
        distribution='Asgard', version='1', snapshot='20260602T173733Z',
        codename='thor', component='main', arch='amd64',
        public_url='http://mirror.example/asgard/',   # trailing slash trimmed
        signed_by_keyring='/usr/share/keyrings/athena-archive-keyring.gpg',
        isos=[{'kind': 'installer',
               'file': 'athena-installer-1-20260602T173733Z-amd64.iso',
               'size': 1082880000, 'sha256': 'a' * 64,
               'built_at': '2026-06-13T08:27:00Z'}],
        generated_at='2026-06-13T18:00:00Z')
    assert _m['apt']['deb_line'] == (
        'deb [signed-by=/usr/share/keyrings/athena-archive-keyring.gpg] '
        'http://mirror.example/asgard thor main')
    assert _m['isos'][0]['url'] == (
        'http://mirror.example/asgard/iso/'
        'athena-installer-1-20260602T173733Z-amd64.iso')
    _html, _jsons = release_index.render_release_files(_m)
    # JSON round-trips and matches the manifest
    assert _json.loads(_jsons) == _m
    # HTML carries the ISO link + the deb line
    assert 'athena-installer-1-20260602T173733Z-amd64.iso' in _html
    assert 'http://mirror.example/asgard/iso/' in _html
    assert 'releases.json' in _html        # link to the machine manifest
    # deterministic JSON (sorted keys) so consecutive publishes diff clean
    assert release_index.render_releases_json(_m) == _jsons



def test_release_index_html_empty_iso_branch():
    """Audit #158: render_index_html with no ISOs shows the 'No ISO images
    published' placeholder and emits no <table>; a non-empty manifest does
    render a table."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import release_index
    _empty = release_index.render_index_html({
        'distribution': 'Asgard', 'version': '1', 'snapshot': 'S',
        'apt': {'deb_line': 'deb [signed] file:/// thor main'}, 'isos': []})
    assert 'No ISO images published' in _empty
    assert '<table>' not in _empty
    _full = release_index.render_index_html({
        'distribution': 'Asgard', 'version': '1', 'isos': [
            {'kind': 'live', 'url': 'u', 'file': 'thor.iso', 'size': 10,
             'sha256': 'a' * 64}]})
    assert '<table>' in _full and 'thor.iso' in _full



def test_generate_repo_indexes_udeb_scan_allows_empty():
    """Audit #19/#18: the udeb (debian-installer) component scan is invoked
    with allow_empty=True, so a present-but-empty debian-installer/binary-<arch>
    dir (a repo with .debs but no udebs) doesn't abort the whole publish.
    (Full behavioural coverage needs sudo + dpkg-scanpackages + gpg, so this
    pins the fix at the source + signature level.)"""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import apt_repo
    _src = inspect.getsource(apt_repo.generate_repo_indexes)
    assert 'allow_empty=True' in _src, "udeb scan must pass allow_empty=True"
    assert 'allow_empty' in inspect.signature(
        apt_repo._scan_packages_to).parameters
    # the empty-udeb comment is anchored to the debian-installer path
    assert 'debian-installer' in _src



def test_repo_audit_nmu_residue_clean_on_anchored_asg():
    """Audit #169: audit_nmu_residue does NOT flag our anchored +asg<R>uK+bN /
    +asg<R>uK+pP+bN built versions (nor a sibling '(= ...+asg...+b1)' pin) as
    residue — those +pP/+bN are legitimate transpose layers — while an
    un-anchored upstream +debNuN version IS flagged."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import types
    import repo_audit
    _state = types.SimpleNamespace(packages={
        'foo': {'Version': '1.2.3-4+asg1u0+b1',
                'Depends': 'bar (= 1.2.3-4+asg1u3+b1)'},   # anchored → clean
        'sig': {'Version': '1.2.3-4+asg1u3+p1+b1'},        # anchored → clean
        'baz': {'Version': '0.8-10+deb12u1'},              # residue → flagged
    })
    _findings = repo_audit.audit_nmu_residue(_state)
    _flagged = {_f[0] for _f in _findings}
    assert 'foo' not in _flagged, _findings
    assert 'sig' not in _flagged, _findings
    assert 'baz' in _flagged, _findings

TESTS = [
    test_fetch_source_versions_cached_on_disk,
    test_published_ledger_memoised,
    test_signing_import_key_round_trip_real_gpg,
    test_signing_export_public_keyring_from_existing_key,
    test_deb_dest_for_filename_routes_by_component,
    test_deb_excluded_from_minimal,
    test_generate_apt_repo_tolerates_empty_udeb_component,
    test_iso_installer_count_records_zero_one_many,
    test_iso_installer_generate_apt_repo_invokes_correct_pipeline,
    test_write_subdir_release_never_mixes_password_with_content,
    test_iso_installer_sign_release_files_runs_both_gpg_invocations,
    test_iso_installer_sign_release_files_errors_when_release_missing,
    test_iso_installer_sign_release_files_errors_when_homedir_missing,
    test_iso_installer_export_pubkey_to_staging_copies_to_disk_archive_key,
    test_iso_installer_export_pubkey_to_staging_errors_when_pubkey_missing,
    test_iso_installer_export_pubkey_to_staging_errors_when_disk_dir_missing,
    test_classify_repo_subdir_routes_dev_to_main,
    test_audit_nmu_residue_skips_tunneled_sources,
    test_audit_nmu_residue_detects_layered_versions,
    test_sta40_no_shell_interpolation_in_sudo_sites,
    test_sta48_cve_report_path_never_overwrites_input_sbom,
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
    test_signing_verify_key_uses_on_disk_uid_not_config_peer_onboarding,
    test_audit_dep_closure_resolves_against_whole_repo,
    test_detect_dangling_asg_equals_pins_classifies_cross_source_pin,
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
    test_sta43_durable_state_writes_atomic_and_preserve_mode,
    test_verify_pkg_artifact_repo_state_overrides_cache_resolution,
    test_verify_pkg_artifact_repo_state_still_fails_when_actually_unsatisfied,
    test_signing_get_key_info_returns_none_when_gpg_missing,
    test_signing_generate_key_returns_false_when_gpg_missing,
    test_signing_generate_key_returns_false_when_export_step_fails,
    test_audit_identity_scan_default_true,
    test_audit_identity_scan_explicit_false_parses,
    test_provenance_stamped_into_iso_and_repo_metadata,
    test_parse_packages_to_ledger_multiversion_epoch_stripped,
    test_published_base_versions,
    test_group_by_pristine_base,
    test_merge_remote_index_preserves_old_versions_multiversion,
    test_merge_packages_indexes_dedup_local_wins,
    test_generate_top_release_subprocess_text_mode_consistency,
    test_generate_top_release_avoids_self_reference_via_tempfile,
    test_published_manifest_roundtrip_and_ledger,
    test_local_published_packages_text_concatenates_component_indices,
    test_write_signed_manifest_fails_closed_when_signing_missing,
    test_read_signed_manifest_refuses_when_signing_setup_fails,
    test_write_signed_manifest_scrubs_unsigned_file_on_sign_failure,
    test_sta22_shared_dpkg_scan_helper_exists_and_is_consumed,
    test_sta22_run_dpkg_scan_writes_via_tempfile,
    test_sta22_run_dpkg_scan_honours_allow_empty,
    test_manifest_vs_remote_discrepancies,
    test_sbom_emits_valid_cyclonedx_skeleton,
    test_sbom_components_sorted_by_name,
    test_sbom_dedupes_udeb_only_sources_into_union,
    test_sbom_patch_set_hash_zero_when_no_patches,
    test_sbom_patch_set_hash_nonempty_when_patches_present,
    test_sbom_purl_format,
    test_sbom_empty_out_path_no_ops,
    test_cve_skips_when_grype_absent,
    test_release_index_manifest_and_html,
    test_apt_repo_generate_repo_indexes_walks_all_suites_and_components,
    test_apt_repo_generate_repo_indexes_skips_when_binary_dir_missing,
    test_apt_repo_generate_repo_indexes_skips_empty_component_but_indexes_others,
    test_apt_repo_generate_repo_indexes_udeb_scan_allows_empty,
    test_stage_d_no_old_repo_subdir_paths_in_production_code,
    test_stage_d_buildconfig_paths_use_new_nested_layout,
    test_audit_dep_closure_invokes_progress_cb_per_pkg,
    test_highest_stanza_per_pkg_arch_keeps_filename_and_dedups,
    test_published_ledger_entries_returns_full_set_arch_filtered,
    test_repo_state_from_packages_text_resolves_depends,
    test_audit_conflict_cohort_invokes_progress_cb_per_pkg,
    test_audit_nmu_residue_invokes_progress_cb_per_pkg,
    test_repo_index_prints_post_run_summary,
    test_scan_packages_with_progress_writes_output_via_subprocess_run,
    test_format_gpg_time_survives_overflow_epoch,
    test_cmd_repo_cleanup_report_lists_all_scanned_components,
    test_repo_dispatcher_advertises_merged_package_actions,
    test_release_index_html_empty_iso_branch,
    test_generate_repo_indexes_udeb_scan_allows_empty,
    test_repo_audit_nmu_residue_clean_on_anchored_asg,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
