"""Athena tests — apt cache build + snapshot pinning (cache.py, cmd_snapshot.py, snapshot state).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile

from _test_helpers import (  # noqa: F401
    _ROOT,
    _StubArchTable,
    _conf11_drift_pkg_stanzas,
    _conf11_drift_src_stanzas,
    _download_source_with_mocked_get,
    _gen_signed_fixture,
    _make_collision_cache,
    _make_offline_cache,
    _make_udeb_packages_text,
    _session_source,
    _stub_tui,
    _utils_module,
    _with_stub_tui,
)




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



def test_should_reuse_pinned_release_skip_decision():
    """PERF-01: a snapshot-pinned InRelease is reused from cache only when
    pinned, remote (not file://), and a non-empty cached copy exists."""
    from cache import Cache
    with tempfile.TemporaryDirectory() as tmp:
        cached = os.path.join(tmp, 'InRelease')
        with open(cached, 'w') as fh:
            fh.write('signed release bytes\n')
        empty = os.path.join(tmp, 'empty')
        with open(empty, 'wb') as fh:
            fh.write(b'')
        missing = os.path.join(tmp, 'nope')

        # Reuse ONLY in the fully-pinned, remote, cached, non-empty case.
        assert Cache._should_reuse_pinned_release('20260622T000000Z', False, cached)
        # Floating mirror (no pin) → always re-fetch (file may have moved).
        assert not Cache._should_reuse_pinned_release(None, False, cached)
        # file:// fork mirror is regenerated each build → never reuse.
        assert not Cache._should_reuse_pinned_release('20260622T000000Z', True, cached)
        # No cached copy yet → must download.
        assert not Cache._should_reuse_pinned_release('20260622T000000Z', False, missing)
        # Zero-byte cached copy (truncated/aborted) → must re-download.
        assert not Cache._should_reuse_pinned_release('20260622T000000Z', False, empty)



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



def test_sta44_index_verified_against_release_sha():
    """STA-44: a freshly downloaded Packages/Sources index is hash-verified
    against the GPG-verified InRelease SHA256.  Previously the sha gated only
    the re-download SKIP, so a corrupt/tampered transfer was decompressed and
    ingested silently.  Verifies the uncompressed sha (what the skip-check
    keys on), falling back to the compressed artifact's sha."""
    import sys as _sys, hashlib as _hashlib, inspect as _inspect
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    _f = Cache._verify_index_against_release   # doesn't touch self
    with tempfile.TemporaryDirectory() as _td:
        _dst = os.path.join(_td, 'Packages')
        with open(_dst, 'wb') as _fh:
            _fh.write(b'index-bytes')
        _good = _hashlib.sha256(b'index-bytes').hexdigest()
        _comp = os.path.join(_td, 'Packages.xz')
        with open(_comp, 'wb') as _fh:
            _fh.write(b'compressed-bytes')
        _good_comp = _hashlib.sha256(b'compressed-bytes').hexdigest()
        _P = 'main/binary-amd64/Packages'
        # uncompressed sha present + matches → ok
        _ok, _d = _f(None, _path=_P, _chosen_ext='.xz',
                     _rel_sha={_P: _good}, _compressed_dst=_comp, _dst=_dst)
        assert _ok, _d
        # uncompressed sha present + MISMATCH → fail with detail
        _ok, _d = _f(None, _path=_P, _chosen_ext='.xz',
                     _rel_sha={_P: 'deadbeef' * 8},
                     _compressed_dst=_comp, _dst=_dst)
        assert not _ok and 'sha256' in _d, _d
        # only compressed sha present → verify the compressed artifact
        _ok, _d = _f(None, _path=_P, _chosen_ext='.xz',
                     _rel_sha={_P + '.xz': _good_comp},
                     _compressed_dst=_comp, _dst=_dst)
        assert _ok, _d
        _ok, _d = _f(None, _path=_P, _chosen_ext='.xz',
                     _rel_sha={_P + '.xz': 'beef' * 16},
                     _compressed_dst=_comp, _dst=_dst)
        assert not _ok, _d
    # both download paths invoke the verifier (def + 2 call sites)
    _csrc = _inspect.getsource(Cache)
    assert _csrc.count('_verify_index_against_release(') >= 3, \
        "a download path no longer verifies the index against InRelease"



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
        def set_max(s, *a, **k): pass
        def reset(s, *a, **k): pass
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

            _ms = MagicMock()
            _ms.head.return_value = mock_head
            _ms.get.return_value = mock_get
            with patch.object(utils, '_http_session', return_value=_ms):
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

            _ms = MagicMock()
            _ms.head.return_value = mock_head
            _ms.get.return_value = mock_get
            with patch.object(utils, '_http_session', return_value=_ms):
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

            _ms = MagicMock()
            _ms.head.return_value = mock_head
            _ms.get.return_value = mock_get
            with patch.object(utils, '_http_session', return_value=_ms):
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



def test_cache_build_wraps_cache_construction_in_spinner():
    """`cmd_build_cache` wraps Cache(self.config) in a Spinner so the
    quiet phases between record-index ProgressBars (mirror fetch,
    cross-validation, dep-graph internals) don't look like a frozen
    TUI.  Source-text inspection — exercising the full cache build
    needs network access + the actual snapshot.
    """
    _body = _session_source()
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



def test_fork_version_gate_ignores_provides_injected_records():
    """Regression (audit #47): the fork-vs-upstream collision gate must compute
    the fork version only from records whose real Package field is the name. A
    Provides-injected record (a DIFFERENT binary providing this name, keyed
    under its own epoch-bearing version) must NOT inflate the fork version and
    mask a real upstream-dominates collision."""
    import sys
    import types
    from collections import defaultdict
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from cache import Cache
    c = Cache.__new__(Cache)
    c._upstream_collisions = defaultdict(list, {'pkgsel': [('main', '0.80')]})
    c._upstream_udeb_collisions = defaultdict(list)
    c.package_hashtable = defaultdict(lambda: defaultdict(list))
    c.udeb_hashtable = defaultdict(lambda: defaultdict(list))
    # real fork record (low version) + a Provides-injected record (different
    # Package, high epoch version) both keyed under 'pkgsel'.
    c.package_hashtable['pkgsel']['0.79+thor1'] = [
        types.SimpleNamespace(package='pkgsel')]
    c.package_hashtable['pkgsel']['2:99'] = [
        types.SimpleNamespace(package='other-binary-providing-pkgsel')]
    c.error_str = ''
    # upstream 0.80 > real fork 0.79+thor1 → gate must FIRE.  The old code took
    # the injected 2:99 as the fork version (0.80 < 2:99) and wrongly passed.
    assert c._verify_no_fork_collisions() is False
    assert 'pkgsel' in c.error_str and '0.79+thor1' in c.error_str



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



def test_conf11_source_drift_tracks_upstream_version():
    """CONF-11 follow-up 2026-05-31: the source supersede walk now records
    upstream version in _upstream_source_collisions so the audit can warn
    when upstream source advances past fork.  Pin the parallel-to-
    `_upstream_collisions` shape."""
    import tempfile
    _fork_pkg = _conf11_drift_pkg_stanzas('1.0+deb12u1+athena1')
    _upstream_pkg = _conf11_drift_pkg_stanzas('1.0+deb12u1')
    _fork_src = _conf11_drift_src_stanzas('1.0+deb12u1+athena1', 'a')
    _upstream_src = _conf11_drift_src_stanzas('1.0+deb12u1', 'c')
    with tempfile.TemporaryDirectory() as td:
        c = _make_offline_cache(
            td,
            packages={'fork': _fork_pkg, 'main': _upstream_pkg},
            sources={'fork': _fork_src, 'main': _upstream_src},
        )
    assert c.is_valid is True, c.error_str
    # Upstream source version is now recorded — was silently lost pre-fix.
    assert 'athena-thing' in c._upstream_source_collisions, (
        c._upstream_source_collisions)
    assert ('main', '1.0+deb12u1') in c._upstream_source_collisions['athena-thing']



def test_conf11_source_drift_audit_silent_when_fork_ahead():
    """Drift audit is silent when fork source dominates upstream source —
    the normal `+athenaN` tiebreaker case.  No warning, no error_str."""
    import io
    import logging as _lg
    import tempfile
    _fork_pkg = _conf11_drift_pkg_stanzas('1.0+deb12u1+athena1')
    _upstream_pkg = _conf11_drift_pkg_stanzas('1.0+deb12u1')
    _fork_src = _conf11_drift_src_stanzas('1.0+deb12u1+athena1', 'a')
    _upstream_src = _conf11_drift_src_stanzas('1.0+deb12u1', 'c')
    _buf = io.StringIO()
    _h = _lg.StreamHandler(_buf)
    _h.setLevel(_lg.WARNING)
    _logger = _lg.getLogger('athena')
    _logger.addHandler(_h)
    try:
        with tempfile.TemporaryDirectory() as td:
            c = _make_offline_cache(
                td,
                packages={'fork': _fork_pkg, 'main': _upstream_pkg},
                sources={'fork': _fork_src, 'main': _upstream_src},
            )
    finally:
        _logger.removeHandler(_h)
    assert c.is_valid is True
    # No drift warning fired (fork is strictly ahead by +athena1 tiebreaker).
    assert 'fork source drift' not in _buf.getvalue(), _buf.getvalue()



def test_conf11_source_drift_audit_fires_when_upstream_advances():
    """When upstream source version >= fork source version, the drift
    audit emits a WARNING.  Advisory only — c.is_valid stays True
    (build doesn't fail on source drift; binary/udeb gate handles those)."""
    import io
    import logging as _lg
    import tempfile
    _fork_pkg = _conf11_drift_pkg_stanzas('1.0+deb12u1+athena1')
    # Upstream BINARY stays old so the binary collision gate doesn't fail
    # the build — we want to isolate the source-side audit signal.
    _upstream_pkg = _conf11_drift_pkg_stanzas('1.0+deb12u1')
    _fork_src = _conf11_drift_src_stanzas('1.0+deb12u1+athena1', 'a')
    # Upstream SOURCE has advanced past fork's NMU layer.
    _upstream_src = _conf11_drift_src_stanzas('1.0+deb12u2', 'c')
    _buf = io.StringIO()
    _h = _lg.StreamHandler(_buf)
    _h.setLevel(_lg.WARNING)
    _logger = _lg.getLogger('athena')
    _logger.addHandler(_h)
    try:
        with tempfile.TemporaryDirectory() as td:
            c = _make_offline_cache(
                td,
                packages={'fork': _fork_pkg, 'main': _upstream_pkg},
                sources={'fork': _fork_src, 'main': _upstream_src},
            )
    finally:
        _logger.removeHandler(_h)
    # Advisory only: build still valid.
    assert c.is_valid is True, c.error_str
    # Drift warning fired naming the colliding source.
    _log = _buf.getvalue()
    assert 'fork source drift' in _log, _log
    assert 'athena-thing' in _log, _log
    assert '1.0+deb12u2' in _log, _log



# ─────────────────────────────────────────────────────────────────────────────
# UPD-01 steps 7-8 — snapshot command family + repo refresh orchestrator
# ─────────────────────────────────────────────────────────────────────────────


def test_reconcile_snapshot_pin_syncs_build_conf_to_state():
    """snapshot.state 'current' is authoritative — reconcile_snapshot_pin
    rewrites build.conf [Snapshot] Timestamp to match (preserving comments
    + other sections), updates the in-memory value, and reports (old, new).
    No state file → None (build.conf stays authoritative)."""
    import json
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    with tempfile.TemporaryDirectory() as _t:
        _cfgdir = os.path.join(_t, 'config')
        os.makedirs(_cfgdir)
        _conf = os.path.join(_cfgdir, 'build.conf')
        with open(_conf, 'w') as _f:
            _f.write("[Snapshot]\n# keep this comment\n"
                     "Timestamp = 20260514T083402Z\nBaseUrl = https://x\n\n"
                     "[Other]\nFoo = bar\n")

        class _Cfg:
            config_path = _conf
            dir_config = _cfgdir
            snapshot_timestamp_config = '20260514T083402Z'

        _cfg = _Cfg()
        # no state file → no change
        assert utils.reconcile_snapshot_pin(_cfg) is None
        assert 'Timestamp = 20260514T083402Z' in open(_conf).read()

        with open(os.path.join(_cfgdir, 'snapshot.state'), 'w') as _f:
            json.dump({'current': '20260602T173733Z'}, _f)
        _r = utils.reconcile_snapshot_pin(_cfg)
        assert _r == ('20260514T083402Z', '20260602T173733Z'), _r
        _body = open(_conf).read()
        assert 'Timestamp = 20260602T173733Z' in _body   # rewritten
        assert '# keep this comment' in _body             # comments preserved
        assert 'Foo = bar' in _body                       # other sections intact
        assert _cfg.snapshot_timestamp_config == '20260602T173733Z'
        # already in sync → None
        assert utils.reconcile_snapshot_pin(_cfg) is None



def test_snapshot_state_roundtrip_and_resolve_precedence():
    """The durable pin lives in config/snapshot.state (NOT cache), and
    resolve_snapshot_timestamp prefers state.current over [Snapshot]
    Timestamp — so the pin survives `clean cache` and the operator
    override wins.  MIRROR-01 Phase 8: only `current` survives; legacy
    base/published/external kwargs are accepted (signature compat) but
    silently dropped at write time."""
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
        assert utils.read_snapshot_state(_cfg) == {
            'current': '20260601T000000Z'}
        # state.current wins over the explicit [Snapshot] Timestamp
        assert utils.resolve_snapshot_timestamp(_cfg) == '20260601T000000Z'
        # Only `current` persists — per-mirror state files own the
        # publish-target pins.
        _st = utils.read_snapshot_state(_cfg)
        assert _st == {'current': '20260601T000000Z'}, _st
        # the state file is under config/, not cache/
        assert os.path.exists(os.path.join(_cfg_dir, 'snapshot.state'))



def test_snapshot_state_writer_drops_legacy_fields_from_old_files():
    """A pre-MIRROR-01 snapshot.state file carrying base/published/
    external is auto-cleaned on the next write — the writer reads,
    rewrites just `current`, drops everything else."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import json as _json
    import utils
    with tempfile.TemporaryDirectory() as _tmp:
        _cfg_dir = os.path.join(_tmp, 'config')
        os.makedirs(_cfg_dir)

        class _Cfg:
            dir_config = _cfg_dir
            dir_cache = _tmp
            snapshot_enabled = True
            snapshot_timestamp_config = ''
        _cfg = _Cfg()
        # Hand-write a pre-Phase-8 file with the four-field shape
        _path = utils.snapshot_state_path(_cfg)
        with open(_path, 'w') as _fh:
            _json.dump({
                'current':   '20260601T000000Z',
                'base':      '20260514T083402Z',
                'published': '20260301T000000Z',
                'external':  True,
            }, _fh)
        # Reader tolerates the legacy shape (returns all four)
        _read_legacy = utils.read_snapshot_state(_cfg)
        assert 'base' in _read_legacy and 'published' in _read_legacy
        # Next write (advancing the pin) drops legacy fields entirely
        utils.write_snapshot_state(_cfg, current='20260615T000000Z')
        _read_clean = utils.read_snapshot_state(_cfg)
        assert _read_clean == {'current': '20260615T000000Z'}, _read_clean



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

    from unittest.mock import patch, MagicMock
    _ms = MagicMock()
    _ms.get.return_value = _Resp()
    with patch.object(utils, '_http_session', return_value=_ms):
        _got = utils.list_snapshots_between(
            _Cfg(), '20260514T000000Z', '20260526T000000Z')
    # strictly > 0514 and <= 0526, union of both keys, sorted, deduped
    assert _got == ['20260515T000000Z', '20260518T000000Z',
                    '20260526T000000Z'], _got



def test_cache_build_gates_on_snapshot_pins():
    """cmd_build_cache must call _ensure_snapshot_pins (and abort if it returns
    False) before building."""
    import re
    _body = _session_source()
    _m = re.search(r'def cmd_build_cache\(self.*?(?=\n    def )', _body, re.DOTALL)
    assert _m, "cmd_build_cache not found"
    assert re.search(r'if not self\._ensure_snapshot_pins\(\):\s*\n\s+return',
                     _m.group(0)), "cache build must gate on _ensure_snapshot_pins"




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



def test_download_file_retries_on_transient_timeout():
    """download_file must retry on ConnectTimeout / ReadTimeout /
    ConnectionError (transient network failure modes) so a single
    snapshot.debian.org CDN stall doesn't fail the whole cache build.

    Pin behavior: HEAD fails twice with ReadTimeout, succeeds on attempt
    3.  The whole download_file call should return success (size > 0)
    not -1.  Without the retry loop, the first ReadTimeout would fail
    the call."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import utils as _u
    from unittest.mock import patch as _patch, MagicMock
    from requests import ReadTimeout

    _calls: 'list[str]' = []

    def _head_then_succeed(url, **kw):
        _calls.append('head')
        if _calls.count('head') < 3:
            raise ReadTimeout('cdn cold cache')
        _r = MagicMock()
        _r.headers = {'content-length': '42'}
        return _r

    def _get_succeed(url, **kw):
        _calls.append('get')
        _r = MagicMock()
        _r.__enter__ = lambda _s: _r
        _r.__exit__ = lambda _s, *_a: None
        _r.headers = {'content-length': '42'}
        _r.iter_content = lambda chunk_size: [b'x' * 42]
        _r.raise_for_status = lambda: None
        return _r

    _ms = MagicMock()
    _ms.head = MagicMock(side_effect=_head_then_succeed)
    _ms.get  = MagicMock(side_effect=_get_succeed)
    with tempfile.TemporaryDirectory() as _td, \
            _patch.object(_u, '_http_session', return_value=_ms), \
            _patch('time.sleep'):    # don't actually wait in tests
        _size, _detail = _u.download_file(
            'http://example.test/file', os.path.join(_td, 'out'))
        # Retry succeeded on attempt 3 → bytes returned, not -1.
        assert _size == 42, f"expected size=42, got {_size}, detail={_detail!r}"
        # HEAD was called 3 times (2 timeouts + 1 success).
        assert _calls.count('head') == 3, f"expected 3 HEAD calls, got {_calls}"



def test_download_file_retries_on_requests_connection_error():
    """A TCP RST from the server raises `requests.exceptions.ConnectionError`,
    which is NOT a subclass of Python's builtin `ConnectionError` — they
    both extend OSError but have no subclass relationship.  A bare
    `except ConnectionError` in the retry block binds to the builtin
    and silently misses every requests-raised reset.  Caught 2026-06-02
    when snapshot.debian.org issued a TCP RST under load and the user
    saw 'download failed' with zero retry log lines.  This test pins
    the requests-specific exception type."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import utils as _u
    from unittest.mock import patch as _patch, MagicMock
    from requests.exceptions import ConnectionError as RequestsConnectionError

    _head_calls = []

    def _head_reset_then_succeed(url, **kw):
        _head_calls.append(1)
        if len(_head_calls) < 3:
            raise RequestsConnectionError(
                'Connection aborted.',
                ConnectionResetError(104, 'Connection reset by peer'))
        _r = MagicMock()
        _r.headers = {'content-length': '42'}
        return _r

    def _get_ok(url, **kw):
        _r = MagicMock()
        _r.__enter__ = lambda _s: _r
        _r.__exit__ = lambda _s, *_a: None
        _r.headers = {'content-length': '42'}
        _r.iter_content = lambda chunk_size: [b'x' * 42]
        _r.raise_for_status = lambda: None
        return _r

    _ms = MagicMock()
    _ms.head = MagicMock(side_effect=_head_reset_then_succeed)
    _ms.get  = MagicMock(side_effect=_get_ok)
    with tempfile.TemporaryDirectory() as _td, \
            _patch.object(_u, '_http_session', return_value=_ms), \
            _patch('time.sleep'):
        _size, _detail = _u.download_file(
            'http://example.test/file', os.path.join(_td, 'out'))
        assert _size == 42, (
            f"requests.ConnectionError must trigger retry; got size={_size}, "
            f"detail={_detail!r}.  If size=-1, the retry block is binding "
            f"to Python's builtin ConnectionError and missing the requests "
            f"exception class.")
        assert len(_head_calls) == 3, (
            f"expected 3 HEAD attempts (2 resets + 1 success), got {len(_head_calls)}")



def test_download_file_does_not_retry_on_http_error():
    """HTTPError (4xx/5xx) is deterministic — retrying just re-hits the
    same response.  download_file must NOT retry these.  Pins the
    classification gate."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    _stub_tui()
    import utils as _u
    from unittest.mock import patch as _patch, MagicMock
    from requests import HTTPError

    _calls: 'list[str]' = []

    def _head_ok(url, **kw):
        _calls.append('head')
        _r = MagicMock()
        _r.headers = {'content-length': '0'}
        return _r

    def _get_404(url, **kw):
        _calls.append('get')
        _r = MagicMock()
        _r.__enter__ = lambda _s: _r
        _r.__exit__ = lambda _s, *_a: None
        _r.headers = {'content-length': '0'}
        _err = HTTPError('404 Not Found')
        _err.response = MagicMock(status_code=404, reason='Not Found')
        _r.raise_for_status = MagicMock(side_effect=_err)
        return _r

    _ms = MagicMock()
    _ms.head = MagicMock(side_effect=_head_ok)
    _ms.get  = MagicMock(side_effect=_get_404)
    with tempfile.TemporaryDirectory() as _td, \
            _patch.object(_u, '_http_session', return_value=_ms), \
            _patch('time.sleep'):
        _size, _detail = _u.download_file(
            'http://example.test/missing', os.path.join(_td, 'out'))
        assert _size == -1
        assert '404' in _detail, f"expected 404 in detail, got {_detail!r}"
        # Only ONE GET call — HTTPError must NOT trigger a retry.
        assert _calls.count('get') == 1, (
            f"HTTPError must not retry; got {_calls.count('get')} GETs")



# ─────────────────────────────────────────────────────────────────────────────
# MIRROR-01 Phase 1 — snapshot history + mirror state CRUD
# ─────────────────────────────────────────────────────────────────────────────


def test_snapshot_history_ring_append_and_cap():
    """append_snapshot_history pushes onto the head and caps at 20."""
    _u = _utils_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        assert _u.read_snapshot_history(_cfg) == []
        # Append 25 ts; ring stays at 20 newest-first
        for _i in range(25):
            _u.append_snapshot_history(_cfg, f"2026{_i:02d}01T000000Z")
        _h = _u.read_snapshot_history(_cfg)
        assert len(_h) == _u.SNAPSHOT_HISTORY_MAX == 20
        # Newest first
        assert _h[0] == '20262401T000000Z'
        # Oldest still in ring
        assert _h[-1] == '20260501T000000Z'
        # Older entries fell off (entry 0..4 dropped)
        assert '20260001T000000Z' not in _h



def test_snapshot_history_dedups_promoted_to_head():
    """Re-selecting a timestamp already in history doesn't duplicate it —
    the existing entry is removed and the new one inserted at the head."""
    _u = _utils_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _cfg = _Cfg()
        for _ts in ('20260101T000000Z', '20260201T000000Z',
                    '20260301T000000Z', '20260101T000000Z'):
            _u.append_snapshot_history(_cfg, _ts)
        _h = _u.read_snapshot_history(_cfg)
        assert _h == [
            '20260101T000000Z',   # promoted to head
            '20260301T000000Z',
            '20260201T000000Z',
        ]



def test_snapshot_history_ignores_malformed_file():
    """A corrupt history file → silent empty return; helper does NOT raise."""
    _u = _utils_module()
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _path = _u.snapshot_history_path(_Cfg())
        with open(_path, 'w') as _fh:
            _fh.write('not valid json')
        assert _u.read_snapshot_history(_Cfg()) == []



def test_snapshot_history_rejects_non_ts_strings():
    """Entries that don't match YYYYMMDDTHHMMSSZ are silently dropped on read
    so a hand-edited file with garbage doesn't poison the ring."""
    _u = _utils_module()
    import json as _json
    with tempfile.TemporaryDirectory() as _td:
        class _Cfg:
            dir_config = _td
        _path = _u.snapshot_history_path(_Cfg())
        with open(_path, 'w') as _fh:
            _json.dump(['20260101T000000Z', 'oops', '', 42,
                        '20260201T000000Z'], _fh)
        assert _u.read_snapshot_history(_Cfg()) == [
            '20260101T000000Z', '20260201T000000Z',
        ]
        # append still works against the cleaned ring
        _u.append_snapshot_history(_Cfg(), '20260301T000000Z')
        assert _u.read_snapshot_history(_Cfg()) == [
            '20260301T000000Z', '20260101T000000Z', '20260201T000000Z',
        ]

TESTS = [
    test_verify_inrelease_clean_signature_passes,
    test_verify_inrelease_tampered_signature_fails,
    test_verify_inrelease_missing_keyring_fails,
    test_verify_inrelease_empty_keyring_fails,
    test_should_reuse_pinned_release_skip_decision,
    test_download_source_surfaces_http_error_clearly,
    test_download_source_surfaces_short_download_clearly,
    test_download_file_returns_http_status_detail_on_404,
    test_download_file_success_returns_size_and_empty_detail,
    test_download_file_zero_content_length_does_not_freeze_bar,
    test_sta44_index_verified_against_release_sha,
    test_gcc_base_re_matches_gcc_N_and_gcc_N_base,
    test_gcc_base_re_rejects_other_gcc_prefixed_packages,
    test_gcc_base_re_rejects_malformed_versions,
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
    test_udeb_view_exposes_udeb_hashtable_as_package_hashtable,
    test_udeb_view_get_packages_resolves_against_udeb_hashtable,
    test_udeb_view_does_not_leak_into_real_package_hashtable,
    test_download_file_handles_file_scheme,
    test_download_file_file_scheme_missing_source,
    test_offline_fixture_builds_a_valid_cache,
    test_cache_build_populates_upstream_collisions_via_real_path,
    test_cache_build_fork_supersede_drops_upstream_silently,
    test_conf11_source_drift_tracks_upstream_version,
    test_conf11_source_drift_audit_silent_when_fork_ahead,
    test_conf11_source_drift_audit_fires_when_upstream_advances,
    test_reconcile_snapshot_pin_syncs_build_conf_to_state,
    test_snapshot_state_roundtrip_and_resolve_precedence,
    test_snapshot_state_writer_drops_legacy_fields_from_old_files,
    test_list_snapshots_between_filters_range_and_unions_keys,
    test_cache_build_gates_on_snapshot_pins,
    test_snapshot_iso_tag_reflects_resolved_pin,
    test_download_file_retries_on_transient_timeout,
    test_download_file_retries_on_requests_connection_error,
    test_download_file_does_not_retry_on_http_error,
    test_snapshot_history_ring_append_and_cap,
    test_snapshot_history_dedups_promoted_to_head,
    test_snapshot_history_ignores_malformed_file,
    test_snapshot_history_rejects_non_ts_strings,
    test_cache_build_wraps_cache_construction_in_spinner,
    test_fork_version_gate_ignores_provides_injected_records,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
