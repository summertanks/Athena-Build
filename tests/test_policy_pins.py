"""Athena tests — repo-wide policy enforcers and cross-cutting source pins.

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys

from _test_helpers import (  # noqa: F401
    _ROOT,
)




def test_tier3_misc_source_pins():
    """Regression pins for a cluster of low-reachability Tier-3 fixes (audit
    #21/#55/#75/#78/#95/#133/#159) where a behavioral harness is disproportion-
    ate; each asserts the specific corrected code is present."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import arch_filter
    import remote_agent
    import iso_installer
    import coord.publish as _pub
    import commands.cmd_audit as _ca
    import commands.cmd_source as _cs
    import commands.cmd_supply_chain as _sc
    # #21: per-arch failure degrades only that arch, not the whole triplet map
    assert 'except subprocess.CalledProcessError' in inspect.getsource(
        arch_filter), '#21'
    # #159: next-offset is frm + bytes read, not the stale size
    assert 'frm + len(_data)' in inspect.getsource(remote_agent), '#159'
    # #133: fork-superseded exclusion applies to the pool selection
    # (the whitelist-less blanket-copy path it originally guarded was
    # removed — deb_whitelist is required now)
    _isrc = inspect.getsource(iso_installer)
    assert '_exclude = exclude_names or set()' in _isrc, '#133'
    # #95: drift requires the binary to actually be present in the pool
    assert '_latest is not None and _fn != _latest' in inspect.getsource(
        _pub), '#95'
    # #55/#56: stale-files row colour matches the gate, which now counts
    # malformed by folding it into _n_stale (audit #56 aligned the audit row
    # with _preflight_audit_repo), so ok=(_n_stale == 0) still ambers a
    # malformed-only repo.
    _casrc = inspect.getsource(_ca)
    assert 'len(_drift) + len(_malformed)' in _casrc, '#55/#56'
    assert 'ok=(_n_stale == 0))' in _casrc, '#55/#56'
    # #75: the .disabled marker hint is an f-string (real pkg name)
    assert 'f\'run `source fork {pkg} enabled` to \'' in inspect.getsource(
        _cs), '#75'
    # #78: the component-count set comprehension uses the `or {}` idiom
    assert "(_m.get('artifact', {}) or {}).get('name', '')" in inspect.getsource(
        _sc), '#78'



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



def test_all_datetime_emission_is_utc():
    """Federation spans machines/timezones — every emitted timestamp must be
    UTC.  No naive `datetime.now()` / `datetime.utcnow()` in scripts/; both
    must be `datetime.now(timezone.utc)`."""
    import glob
    import re
    _bad = []
    for _f in glob.glob(os.path.join(_ROOT, 'scripts', '**', '*.py'),
                        recursive=True):
        with open(_f) as _fh:
            for _i, _line in enumerate(_fh, 1):
                if re.search(r'\bdatetime\.now\(\)', _line) or \
                        re.search(r'\butcnow\(\)', _line):
                    _bad.append(f"{os.path.relpath(_f, _ROOT)}:{_i}: {_line.strip()}")
    assert not _bad, (
        "naive datetime — use datetime.now(datetime.timezone.utc):\n"
        + "\n".join(_bad))



def test_every_outbound_request_uses_http_session():
    """Two invariants in one greps:

    1) No bare `requests.{get,head,post,put}(...)` call sites in
       scripts/ — those bypass our module-level Session and so lose
       HTTP keep-alive (~300-500ms TLS handshake per request that the
       Session amortizes across a single TCP connection).

    2) Future call sites that DO use _http_session() inherit the
       User-Agent from the Session's headers — so the per-call
       headers= argument becomes optional.

    Catches a new call site that forgets to route through the Session
    at CI time, not after the next 30-minute source sync stalls."""
    import re as _re
    _root = os.path.join(_ROOT, 'scripts')
    _offenders: 'list[str]' = []
    _bare = _re.compile(r'\brequests\.(get|head|post|put)\s*\(')
    for _dir, _, _files in os.walk(_root):
        for _name in _files:
            if not _name.endswith('.py'):
                continue
            _path = os.path.join(_dir, _name)
            with open(_path) as _fh:
                _content = _fh.read()
            for _m in _bare.finditer(_content):
                _line_no = _content[:_m.start()].count('\n') + 1
                _rel = os.path.relpath(_path, _ROOT)
                _offenders.append(f"{_rel}:L{_line_no}")
    assert not _offenders, (
        "bare requests.{get,head,post,put}(...) call(s) outside the "
        "Session — these bypass keep-alive and force a fresh TLS "
        "handshake per request:\n  " + "\n  ".join(_offenders))



# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def test_every_defined_test_is_registered():
    """Policy enforcer: every `def test_*` in every part file must appear
    in THAT file's TESTS list, every part must be collected by the
    test_module.py aggregator, and no test name may exist twice across
    parts.  58 tests once sat defined-but-never-registered (so they
    never ran) until a 2026-06-12 sweep — 11 of them had silently
    rotted against renamed code in the meantime.  Registration is
    manual by convention; this pin makes forgetting it a test failure
    instead of a silent coverage hole."""
    import glob as _glob
    import re as _re
    _dir = os.path.dirname(os.path.abspath(__file__))
    _parts = sorted(_glob.glob(os.path.join(_dir, 'test_*.py')))
    _parts = [_p for _p in _parts
              if os.path.basename(_p) != 'test_module.py']
    assert _parts, "no part files found next to the guard"
    _agg_src = open(os.path.join(_dir, 'test_module.py')).read()
    _seen: dict = {}
    for _p in _parts:
        _base = os.path.basename(_p)
        _src = open(_p).read()
        _defined = set(_re.findall(r'^def (test_\w+)\(', _src, _re.M))
        _m = _re.search(r'^TESTS = \[\n(.*?)^\]', _src, _re.M | _re.S)
        assert _m, f"{_base}: no TESTS registration list"
        _registered = _re.findall(r'^\s+(test_\w+),\s*$', _m.group(1), _re.M)
        _missing = sorted(_defined - set(_registered))
        assert not _missing, (
            f"{_base}: {len(_missing)} test(s) defined but never "
            f"registered in TESTS: {_missing[:10]}")
        _ghosts = sorted(set(_registered) - _defined)
        assert not _ghosts, (
            f"{_base}: registered but not defined: {_ghosts}")
        for _t in _registered:
            assert _t not in _seen, (
                f"duplicate test name {_t} in {_base} and {_seen[_t]} — "
                "the aggregator would run both under one name")
            _seen[_t] = _base
        _mod = _base[:-3]
        assert f'import {_mod}' in _agg_src, (
            f"{_base} is not imported by test_module.py")
        assert f'*{_mod}.TESTS,' in _agg_src, (
            f"{_base}'s TESTS are not collected by test_module.py")



def test_all_event_types_are_dispatcher_handled():
    """Audit #188: every type in events.ALL_EVENT_TYPES must appear in the
    dispatcher's isinstance handler chain (giving the list a real consumer),
    and each entry is a frozen dataclass event — so a newly-added event can't
    be silently unhandled."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import inspect
    import re as _re
    import dataclasses
    from tui import events as _ev
    from tui import dispatcher as _dp
    _handled = set(_re.findall(
        r'isinstance\(\s*\w+\s*,\s*(\w+)\)', inspect.getsource(_dp)))
    _listed = {_t.__name__ for _t in _ev.ALL_EVENT_TYPES}
    assert _listed, "ALL_EVENT_TYPES must not be empty"
    assert not (_listed - _handled), (
        f"ALL_EVENT_TYPES entries with no dispatcher isinstance handler: "
        f"{_listed - _handled}")
    for _t in _ev.ALL_EVENT_TYPES:
        assert dataclasses.is_dataclass(_t), _t

TESTS = [
    test_no_stale_progressbar_methods_in_build_py,
    test_readonly_named_commands_have_no_destructive_calls,
    test_all_datetime_emission_is_utc,
    test_every_outbound_request_uses_http_session,
    test_every_defined_test_is_registered,
    test_tier3_misc_source_pins,
    test_all_event_types_are_dispatcher_handled,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
