"""Athena tests — TUI + web API (tui/, webapi/).

Split from the original single-file suite.  Run the whole suite
via `python3 tests/test_module.py`, or just this part directly.
Register new tests in the TESTS list at the bottom of THIS file
(the registration guard enforces it)."""
import os
import sys
import tempfile
import threading

from _test_helpers import (  # noqa: F401
    _ROOT,
    _v2_fake_renderer,
)




def test_progress_rate_counts_only_completed_records():
    """Regression (audit #213): the throughput rate window must count only
    COMPLETED records — in-flight / failed mtimes inflated
    completed_in_window and rate_per_hour."""
    import sys
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    from webapi import readers
    with tempfile.TemporaryDirectory() as _tmp:
        for _pkg, _ph in (('a', 'done'), ('b', 'entry')):   # entry=in-flight
            utils.write_build_record(_tmp, utils.new_build_record(
                package=_pkg, intended_version='1', patch_set_hash=''))
            utils.update_build_record(_tmp, _pkg, phase=_ph)
        _doc = readers.progress(_tmp, window_s=3600)
        assert _doc['completed_in_window'] == 1, _doc       # only 'a' (done)



def test_progress_bar_set_max_clamps_to_current_value():
    """Regression (audit #196): set_max must not drop _max below the current
    progress (_value) — that would make `filled` exceed the bar width and
    overflow the render."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.widgets import ProgressBar
    _pb = ProgressBar('test', maxvalue=100)
    _pb._value = 80
    _pb.set_max(10)                  # below current progress
    assert _pb._max >= 80, _pb._max



def test_tier3_tui_auth_source_pins():
    """Pins for Tier-3 fixes #194 (SystemExit code) and #207 (auth retry poll)
    where the behavioural path (the dispatch loop / a write race) is
    disproportionate to exercise directly."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui.tui as _t
    from webapi import auth as _a
    # #194: a non-int, non-None exit code maps to 1 (failure), not 0.
    assert '0 if _code is None else 1' in inspect.getsource(_t), '#194'
    # #207: bounded retry poll for the winner's key, not a single re-read.
    assert 'for _ in range(50)' in inspect.getsource(_a), '#207'



def test_api_backend_prunes_completed_jobs():
    """Regression (audit #210): the --api backend must cap retained completed
    jobs — the queue worker added a Job per submit and never evicted any, so a
    long-lived server accumulated Job records unboundedly.  Running/queued jobs
    are never pruned."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from webapi.jobs import ApiBackend, Job
    _b = ApiBackend.__new__(ApiBackend)
    _b._jobs = {}
    _b._jobs_lock = threading.Lock()
    _cap = ApiBackend._MAX_COMPLETED_JOBS
    for _i in range(_cap + 50):
        _j = Job(f'cmd{_i}')
        _j.state = 'done'
        _j.finished = float(_i)
        _b._jobs[_j.id] = _j
    _running = Job('live')
    _running.state = 'running'
    _b._jobs[_running.id] = _running
    _b._prune_completed_jobs()
    _done = [_j for _j in _b._jobs.values() if _j.state == 'done']
    assert len(_done) == _cap, len(_done)
    assert _running.id in _b._jobs, "running job must never be pruned"
    assert all(_j.finished >= 50 for _j in _done), "evicted the wrong (newest)"



def test_dispatcher_handles_cancelled_future():
    """Regression (audit #186): console_mark / request_prompt must catch
    CancelledError (the UI loop cancels pending Futures on shutdown), not let
    it propagate."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui.dispatcher as _d
    _src = inspect.getsource(_d)
    assert _src.count('except CancelledError') >= 2, (
        "both console_mark and request_prompt must handle a cancelled Future")



def test_safe_addstr_right_margin_multibyte_safe():
    """The right-margin insert in _safe_addstr must use insstr, not
    insch: insch takes a single-byte chtype, so a non-ASCII final
    character raised OverflowError and froze the renderer (2026-07-18,
    identity-scan findings ending in localized debconf text).  Pin the
    call and the OverflowError backstop in the except clause."""
    import inspect
    import tui.render as _r
    _src = inspect.getsource(_r._safe_addstr)
    assert '.insch(' not in _src, \
        'insch cannot take multibyte chars — use insstr'
    assert '.insstr(' in _src, 'right-margin insert must use insstr'
    assert 'OverflowError' in _src, \
        'OverflowError must be caught as a render backstop'


def test_render_anchors_widgets_to_bottom_band():
    """Regression (audit #191): when content doesn't fill the pane, widgets
    must be anchored to the bottom band, not floated under the last line."""
    import inspect
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui.render as _r
    assert 'max(row, max_y - len(widget_strs))' in inspect.getsource(_r), (
        "widget row must be padded to the bottom band")



def test_read_artifact_truncated_uses_byte_counts():
    """Regression (audit #214): read_artifact's tail 'truncated' flag compared
    the byte size to a CHARACTER-length sum; a multibyte line made sum(chars) <
    bytes and falsely reported truncated even when every line was returned."""
    import sys
    import tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from webapi import readers
    with tempfile.TemporaryDirectory() as _tmp:
        # '→' is 3 bytes / 1 char — the exact multibyte trap.
        with open(os.path.join(_tmp, 'foo.buildlog'), 'w',
                  encoding='utf-8') as _fh:
            _fh.write('a→b\nc\n')
        _doc = readers.read_artifact(_tmp, 'foo', 'buildlog', tail=10)
        assert _doc['found'] is True
        assert _doc['lines'] == ['a→b', 'c'], _doc
        assert _doc['truncated'] is False, (
            "all lines returned but 'truncated' true — byte/char mismatch")



def test_progress_reads_buildlogs_via_context_manager():
    """Regression (audit #215): progress() must read the buildlog/vbuildlog via
    a `with open(...)` context manager, not a bare open(...).read() that leaks
    the handle to GC."""
    import inspect
    import re
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from webapi import readers
    assert not re.search(r'open\([^)]*\)\.read\(\)',
                         inspect.getsource(readers)), (
        "readers must not call .read() on a bare open() (leaks the handle); "
        "use `with open(...) as fh: fh.read()`")



def test_v2_wrap_helpers_round_trip():
    """tui.wrap mirrors the legacy P7 helpers — same contract."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui import wrap
    assert wrap.wrap_line('hello', 80) == ['hello']
    assert wrap.wrap_line('a' * 11, 10) == ['a' * 10, 'a']
    assert wrap.wrap_line('', 80) == ['']
    assert wrap.entry_display_rows('a' * 81, 80) == 2
    buf = [('short', 0), ('a' * 100, 0), ('', 0)]
    assert wrap.total_display_rows(buf, 80) == 1 + 2 + 1



def test_dispatcher_tick_reads_key_and_drains_events():
    """The UI loop's _tick reads a key via the Renderer (sole curses owner —
    no input-pump thread) AND drains queued worker events in one iteration."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import PrintEvent

    _keys = ['Z']

    class _R:
        def __init__(self):
            self.renders = 0
        def render(self, state):
            self.renders += 1
        def width(self):
            return 80
        def content_rows(self):
            return 20
        def begin_input(self):
            pass
        def read_key(self, ms):
            return _keys.pop(0) if _keys else None
        def drain_keys(self):
            return []

    d = Dispatcher(_R())
    _seen: list = []
    d._on_key = lambda k: _seen.append(k)      # spy — gate-independent
    d.post(PrintEvent('hello world'))
    d._tick()
    assert _seen == ['Z'], _seen                # key was read + dispatched
    con = d.state.tabs['console']
    assert any('hello world' in _t for _t, _ in con.buffer), con.buffer



def test_dispatcher_cancel_pending_futures_unblocks_callers():
    """On shutdown the loop must cancel the active AND any still-queued
    prompt / console_mark Futures, so a main-thread caller racing in can't
    deadlock waiting for a resolution that will never come."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import PromptRequest, ConsoleMark
    from concurrent.futures import Future

    d = Dispatcher(_v2_fake_renderer())
    _f1: Future = Future()
    _f2: Future = Future()
    d.post(PromptRequest('> ', 'line', _f1))
    d.post(ConsoleMark(_f2))
    d._cancel_pending_futures()
    assert _f1.cancelled(), 'queued prompt future must be cancelled'
    assert _f2.cancelled(), 'queued console_mark future must be cancelled'



def test_dispatcher_request_prompt_is_quit_aware():
    """request_prompt must not block forever if shutdown raced in: with
    state.quit set and nobody resolving the Future, it raises promptly."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher

    d = Dispatcher(_v2_fake_renderer())
    d.state.quit = True                        # simulate post-shutdown
    _raised = False
    try:
        d.request_prompt('> ')                 # no UI loop to resolve it
    except RuntimeError:
        _raised = True
    assert _raised, 'request_prompt must raise when the dispatcher has stopped'



def test_dispatcher_sample_status_sets_status_text_inline():
    """Resource meters are sampled INLINE on the UI thread (no psutil daemon
    → no finalization SIGSEGV).  _sample_status sets state.status_text."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher

    d = Dispatcher(_v2_fake_renderer())
    d._prime_status()
    d._sample_status()
    assert 'CPU' in d.state.status_text, d.state.status_text
    assert 'MEM' in d.state.status_text, d.state.status_text



def test_tui_single_ui_thread_topology():
    """Lock the redesigned topology: NO input-pump / status / shell daemon
    threads; the command REPL runs on the MAIN thread in wait(); exactly one
    curses-owning UI thread is started in run()."""
    import re
    _p = os.path.join(_ROOT, 'scripts', 'tui', 'tui.py')
    with open(_p) as _fh:
        _body = _fh.read()
    assert 'tui-status' not in _body, 'status daemon thread must be gone'
    assert 'tui-shell' not in _body, 'shell daemon thread must be gone'
    assert 'start_input_pump' not in _body, 'input pump must be gone'
    assert not os.path.exists(
        os.path.join(_ROOT, 'scripts', 'tui', 'input_pump.py')), \
        'input_pump.py must be deleted'
    _m = re.search(r'def wait\(self\).*?(?=\n    def )', _body, re.DOTALL)
    assert _m and 'self._shell()' in _m.group(0), \
        'wait() must run the command REPL on the main thread'
    assert "name='tui-dispatch'" in _body, 'the single UI thread'



def test_v2_state_append_and_scroll():
    """State.tabs[name].append handles scroll_offset in display-row units."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.state import State

    s = State()
    assert 'console' in s.tabs and 'log' in s.tabs
    assert s.active_tab_name() == 'console'

    # At-bottom: scroll stays 0 after append.
    s.tabs['console'].append([('a', 0), ('b', 0)], width=80)
    assert s.tabs['console'].scroll_offset == 0

    # Scrolled away: scroll_offset bumps by display rows (1 row each at w=80).
    s.tabs['console'].scroll_offset = 5
    s.tabs['console'].append([('c', 0), ('d', 0)], width=80)
    assert s.tabs['console'].scroll_offset == 7

    # Long line wraps to 3 rows at w=10; offset increments by 3.
    s.tabs['log'].scroll_offset = 1
    s.tabs['log'].append([('x' * 25, 0)], width=10)
    assert s.tabs['log'].scroll_offset == 1 + 3



def test_tui_page_rows_subtracts_widgets_on_console_only():
    """The console tab overlays live widgets on its bottom rows, so the
    scroll page (and clamp viewport) there must be shorter by len(widgets)
    — otherwise the oldest len(widgets) lines are unreachable during a build.
    Non-console tabs carry no widgets and keep the full height."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher

    d = Dispatcher(_v2_fake_renderer())     # content_rows() == 20
    assert d.state.active_tab_name() == 'console'

    # No widgets → full height.
    assert d._page_rows() == 20

    # Three live widgets on the console tab → viewport shrinks by 3.
    d.state.add_widget(object())
    d.state.add_widget(object())
    d.state.add_widget(object())
    assert d._page_rows() == 17

    # On a non-console tab the widgets don't overlay, so full height stands.
    d.state.activate('log')
    assert d._page_rows() == 20

    # Never collapses below 1 even if widgets exceed the height.
    d.state.activate('console')
    for _ in range(40):
        d.state.add_widget(object())
    assert d._page_rows() == 1



def test_v2_cmdline_edit_and_history():
    """CmdLine matches legacy _Commands semantics (cursor edit + history)."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.state import CmdLine

    c = CmdLine()
    for ch in 'hello':
        c.insert(ch)
    assert c.text == 'hello' and c.cursor == 5
    c.move_left(); c.move_left()
    c.insert('X')
    assert c.text == 'helXlo' and c.cursor == 4
    assert c.backspace() is True
    assert c.text == 'hello' and c.cursor == 3

    # History walk.
    c.push_history('first')
    c.push_history('second')
    c.set_text('draft')
    assert c.history_prev() is True and c.text == 'second'
    assert c.history_prev() is True and c.text == 'first'
    assert c.history_prev() is False   # at oldest
    assert c.history_next() is True and c.text == 'second'
    assert c.history_next() is True
    assert c.text == 'draft', 'past-newest Down restores draft'



def test_v2_dispatcher_key_events_gated_without_prompt():
    """Command-line editing is GATED on a pending prompt: while a command runs
    (no prompt accepting input) keystrokes are IGNORED, so the busy state is
    unambiguous and stray typing isn't captured.  (With a prompt active, keys
    edit the line + Enter submits — see test_v2_dispatcher_prompt_future_round_trip.)
    """
    import sys, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import KeyEvent, Shutdown

    d = Dispatcher(_v2_fake_renderer())
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    time.sleep(0.02)
    # No pending prompt (command-running state) → keystrokes dropped.
    for ch in 'abc':
        d.post(KeyEvent(ch))
    d.post(KeyEvent('KEY_LEFT'))
    d.post(KeyEvent('X'))
    time.sleep(0.05)
    assert d.state.cmd.text == '', d.state.cmd.text
    d.post(Shutdown(0))
    t.join(timeout=1)



def test_v2_dispatcher_status_bar_includes_network():
    """The footer status bar now carries network throughput (NET:↑/↓)
    alongside CPU/MEM/DISK.  StatusEvent gained up/down (bytes/s) with
    back-compat defaults; _fmt_rate renders compact human rates."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import StatusEvent

    # rate formatter scales B/K/M/G
    assert Dispatcher._fmt_rate(0) == '0'
    assert Dispatcher._fmt_rate(900) == '900'
    assert Dispatcher._fmt_rate(2 * 1024 * 1024).endswith('M')
    assert Dispatcher._fmt_rate(3 * 1024 ** 3).endswith('G')

    # back-compat: 3-arg StatusEvent still constructs (up/down default 0)
    _e0 = StatusEvent(10.0, 20.0, 30.0)
    assert _e0.up == 0.0 and _e0.down == 0.0

    d = Dispatcher(_v2_fake_renderer())
    d._on_status(StatusEvent(12.0, 34.0, 56.0,
                             up=2 * 1024 * 1024, down=512 * 1024))
    _txt = d.state.status_text
    assert 'CPU:12%' in _txt and 'MEM:34%' in _txt and 'DISK:56%' in _txt, _txt
    assert 'NET:' in _txt and '↑2.0M' in _txt and '↓512.0K' in _txt, _txt



def test_v2_dispatcher_prompt_future_round_trip():
    """request_prompt blocks caller, dispatcher fulfills via Enter."""
    import sys, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import KeyEvent, Shutdown

    d = Dispatcher(_v2_fake_renderer())
    threading.Thread(target=d.run, daemon=True).start()
    time.sleep(0.02)

    result_holder = {}
    def caller():
        result_holder['ans'] = d.request_prompt('Enter: ')
    threading.Thread(target=caller, daemon=True).start()
    time.sleep(0.05)

    # Now the dispatcher is in prompt mode; route keys to the prompt.
    for ch in 'yes':
        d.post(KeyEvent(ch))
    d.post(KeyEvent('\n'))
    time.sleep(0.1)
    assert result_holder.get('ans') == 'yes', result_holder
    d.post(Shutdown(0))



def test_v2_dispatcher_tab_switch_by_index():
    """F-key activates nth tab; out-of-range silently no-ops."""
    import sys, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import KeyEvent, Shutdown

    d = Dispatcher(_v2_fake_renderer())
    threading.Thread(target=d.run, daemon=True).start()
    time.sleep(0.02)
    d.post(KeyEvent('KEY_F(2)'))   # log (index 1)
    time.sleep(0.05)
    assert d.state.active_tab_name() == 'log'
    d.post(KeyEvent('KEY_F(99)'))  # out of range — no change
    time.sleep(0.05)
    assert d.state.active_tab_name() == 'log'
    d.post(Shutdown(0))



def test_v2_dispatcher_progressbar_widget_lifecycle():
    """ProgressBar add/remove flow via the duck-typed tui_instance
    contract: __init__ → tui_instance.add_widget(self) → dispatcher
    sees WidgetAdd; close() → del_widget → WidgetRemove."""
    import sys, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui as _pkg
    from tui import widgets
    from tui.dispatcher import Dispatcher
    from tui.events import Shutdown, WidgetAdd, WidgetRemove

    d = Dispatcher(_v2_fake_renderer())

    # Bridge: stand-in for the real Tui — exposes the duck-typed
    # surface widgets call (add_widget / del_widget / print) and
    # forwards into the dispatcher under test.
    class _Bridge:
        def add_widget(self, w):
            d.post(WidgetAdd(w))
            return id(w)
        def del_widget(self, wid):
            d.post(WidgetRemove(wid))
        def print(self, msg, attr=None):
            pass

    _saved = _pkg.tui_instance
    _pkg.tui_instance = _Bridge()
    try:
        threading.Thread(target=d.run, daemon=True).start()
        time.sleep(0.02)

        bar = widgets.ProgressBar('Test', maxvalue=100)
        time.sleep(0.05)
        assert bar in d.state.widgets
        bar.step(50)
        time.sleep(0.05)
        assert bar.value == 50
        bar.close()
        time.sleep(0.05)
        assert bar not in d.state.widgets
        d.post(Shutdown(0))
    finally:
        _pkg.tui_instance = _saved



def test_v2_dispatcher_adaptive_idle_no_widgets():
    """No widgets and no events -> dispatcher blocks on get(timeout=1s)
    rather than busy-spinning.  We can't measure timeout directly, but
    we can verify no events get processed during a 200ms idle window."""
    import sys, time
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import Shutdown

    r = _v2_fake_renderer()
    d = Dispatcher(r)
    threading.Thread(target=d.run, daemon=True).start()
    time.sleep(0.02)
    renders_before = r.renders
    time.sleep(0.2)
    # With IDLE_TIMEOUT=1.0 and no widgets, at most one extra render
    # (the initial one) should have happened in 200ms.
    assert r.renders - renders_before <= 1, (
        f'expected <=1 render during 200ms idle; got {r.renders - renders_before}'
    )
    d.post(Shutdown(0))



def test_v2_logging_bridge_consolidates_to_log_tab_with_stage_prefix():
    """Three-tab UX: every log record lands in the single 'log' tab, with
    the originating stage preserved as a bracketed prefix on the message."""
    import sys, time, logging as _logging
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import Shutdown
    from tui.logging_bridge import setup_logging

    d = Dispatcher(_v2_fake_renderer())
    threading.Thread(target=d.run, daemon=True).start()
    time.sleep(0.02)
    setup_logging(d)

    _logging.getLogger('athena').info('plain athena')
    _logging.getLogger('athena.cache').warning('cache stage')
    _logging.getLogger('athena.iso').error('iso stage')
    time.sleep(0.1)

    log_buf = [t for t, _ in d.state.tabs['log'].buffer]
    # all three in the single log tab, stages prefixed
    assert any('plain athena' in line for line in log_buf), log_buf
    assert any('[cache] cache stage' in line for line in log_buf), log_buf
    assert any('[iso] iso stage' in line for line in log_buf), log_buf
    # bare athena gets no prefix
    assert not any('[athena]' in line for line in log_buf), log_buf
    d.post(Shutdown(0))



def test_render_failure_one_shot_capture_writes_post_mortem():
    """When the renderer raises, the dispatcher's _safe_render captures
    the first failure to disk with state context — replaces the old
    blanket `except: pass` that hid renderer regressions silently.

    Regression for the 2026-05-31 log-tab-removal incident: render
    started failing after `repo audit`, no further output painted,
    operator saw no error.  Couldn't pin the mechanism without ground
    truth; this capture makes the next occurrence diagnosable."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui import dispatcher as _disp
    from tui.state import State

    # Renderer that always raises — simulates the silent-render bug.
    class _Boom:
        def render(self, state):
            raise RuntimeError('simulated curses error: bad window')
        def width(self): return 80
        def content_rows(self): return 20

    with tempfile.TemporaryDirectory() as td:
        _path = os.path.join(td, 'render-failure.log')
        # Override the capture target + reset one-shot guard.
        _disp.RENDER_FAILURE_PATH = _path
        _disp._reset_render_failure_state()
        try:
            d = _disp.Dispatcher(_Boom(), State())
            # First dirty render — should capture.
            d.state.dirty = True
            d._safe_render()
            assert _disp.render_failure_count() == 1
            assert os.path.isfile(_path), 'first failure should write the file'
            body = open(_path).read()
            assert 'TUI render failure' in body
            assert 'RuntimeError' in body
            assert 'simulated curses error' in body
            assert 'active_tab:' in body
            assert 'console' in body                # one of the default tabs
            assert 'traceback:' in body

            # Second dirty render — counter bumps, file NOT rewritten.
            _first_mtime = os.path.getmtime(_path)
            d.state.dirty = True
            d._safe_render()
            assert _disp.render_failure_count() == 2
            assert os.path.getmtime(_path) == _first_mtime, (
                'second failure must not rewrite the file (one-shot)'
            )
        finally:
            _disp.RENDER_FAILURE_PATH = None
            _disp._reset_render_failure_state()



def test_render_failure_capture_no_op_on_success():
    """A renderer that succeeds doesn't write the post-mortem file."""
    import sys, tempfile
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui import dispatcher as _disp
    from tui.state import State

    class _OK:
        def render(self, state): pass
        def width(self): return 80
        def content_rows(self): return 20

    with tempfile.TemporaryDirectory() as td:
        _path = os.path.join(td, 'render-failure.log')
        _disp.RENDER_FAILURE_PATH = _path
        _disp._reset_render_failure_state()
        try:
            d = _disp.Dispatcher(_OK(), State())
            d.state.dirty = True
            d._safe_render()
            assert _disp.render_failure_count() == 0
            assert not os.path.exists(_path)
        finally:
            _disp.RENDER_FAILURE_PATH = None
            _disp._reset_render_failure_state()



def test_logging_bridge_splits_multiline_records():
    """A single log record with embedded \\n must produce one buffer
    entry per line, not one mega-entry that wrap_line slices arbitrarily
    across line boundaries.  Regression for the 2026-05-31 chroot-tab
    truncation: _dpkg_unpack dumped full stdout (~1500 lines) as one
    record; the renderer's char-based wrap_line then chopped the
    concatenated text into 70-char slices that crossed the real line
    boundaries, presenting unreadable right-tails to the operator."""
    import sys, time, logging as _logging
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui.dispatcher import Dispatcher
    from tui.events import Shutdown
    from tui.logging_bridge import setup_logging

    d = Dispatcher(_v2_fake_renderer())
    threading.Thread(target=d.run, daemon=True).start()
    time.sleep(0.02)
    setup_logging(d)

    # Simulate the _dpkg_unpack pattern — one record, three logical lines.
    _logging.getLogger('athena.chroot').info(
        'Selecting previously unselected package libc6-udeb.\n'
        'Preparing to unpack .../libc6-udeb_2.36-9_amd64.udeb ...\n'
        'Unpacking libc6-udeb (2.36-9) ...'
    )
    time.sleep(0.1)

    chroot_buf = [t for t, _ in d.state.tabs['log'].buffer]
    # Each \n-separated line should be its own buffer entry — and no
    # entry should contain a literal \n character.
    assert any(line == '[chroot] Selecting previously unselected package libc6-udeb.'
               for line in chroot_buf), chroot_buf
    assert any(line.startswith('Preparing to unpack ') for line in chroot_buf), chroot_buf
    assert any(line == 'Unpacking libc6-udeb (2.36-9) ...'
               for line in chroot_buf), chroot_buf
    assert not any('\n' in line for line in chroot_buf), (
        'no buffer entry should contain a literal \\n'
    )
    d.post(Shutdown(0))



def test_per_module_logger_names_pin_routing():
    """Each scripts/*.py module's bare module-level `logger` must point
    at the right logger name so its records carry the right `[stage]`
    prefix (via _stage_prefix) in the single shared 'log' tab — all
    records land in 'log' (see _tab_for_logger), so this pins the logger
    NAME, not a per-tab route.  Pinned to prevent silent regression."""
    import re
    expected = {
        # cache stage — cache parse / dep walk / fork mirror / dep drift
        'cache':            'athena.cache',
        'dependencytree':   'athena.cache',
        'dep_drift':        'athena.cache',
        'fork_mirror':      'athena.cache',
        'package':          'athena.cache',
        # build stage — orchestrator + per-source build container
        'build':            'athena.build',
        'buildcontainer':   'athena.build',
        # chroot stage — live + installer chroot bring-up
        'chroot':           'athena.chroot',
        'installer_chroot': 'athena.chroot',
        # iso stage — squashfs / grub / qcow2
        'iso':              'athena.iso',
        'iso_installer':    'athena.iso',
        'disk_image':       'athena.iso',
        # utils: file download / sha256 / snapshot resolution — runs
        # at cache time; route to cache tab not the bare fallback.
        'utils':            'athena.cache',
        # cross-stage / generic — bare 'athena' → build tab (fallback)
        'signing':          'athena',
        'apt_repo':         'athena',
        'repo_audit':       'athena',
        'cli':              'athena',
    }

    def _tab(name):
        if name.startswith('athena.'):
            return name[len('athena.'):].split('.', 1)[0]
        return 'build'

    pat = re.compile(
        r"^logger\s*=\s*logging\.getLogger\(\s*'(athena(?:\.[a-z_]+)?)'\s*\)",
        re.MULTILINE,
    )
    scripts_dir = os.path.join(_ROOT, 'scripts')
    for mod, expected_name in expected.items():
        path = os.path.join(scripts_dir, f'{mod}.py')
        assert os.path.isfile(path), f'expected module missing: {path}'
        with open(path) as f:
            body = f.read()
        m = pat.search(body)
        assert m, (
            f'{mod}.py: no module-level '
            f"`logger = logging.getLogger('athena…')` found"
        )
        actual = m.group(1)
        assert actual == expected_name, (
            f'{mod}.py: expected logger {expected_name!r} '
            f'(tab {_tab(expected_name)!r}); got {actual!r} '
            f'(tab {_tab(actual)!r})'
        )



def test_api01_key_lifecycle():
    """API-01: api key autogenerates 0600, persists across loads, and
    verification is fail-closed (empty/absent key NEVER verifies)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from webapi import auth
    with tempfile.TemporaryDirectory() as _tmp:
        _p = os.path.join(_tmp, 'api.key')
        _k1 = auth.ensure_api_key(_p)
        assert _k1 and len(_k1) >= 32
        assert (os.stat(_p).st_mode & 0o777) == 0o600, oct(os.stat(_p).st_mode)
        assert auth.ensure_api_key(_p) == _k1          # stable across calls
        assert auth.load_api_key(_p) == _k1
        assert auth.verify_key(_k1, _k1)
        assert not auth.verify_key('wrong', _k1)
        assert not auth.verify_key('', _k1)
        assert not auth.verify_key(_k1, '')            # fail-closed
        assert not auth.verify_key('', '')



def test_api01_readers_index_get_and_tamper():
    """API-01 readers: paginated index + phase filter; get_build reports
    found/verified honestly — a tampered record is found=True
    verified=False, never silently absent."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    from webapi import readers
    with tempfile.TemporaryDirectory() as _tmp:
        for _pkg, _ph, _st in (('acme1', 'done', 'PASS'),
                               ('acme2', 'failed', 'FAIL')):
            utils.write_build_record(_tmp, utils.new_build_record(
                package=_pkg, intended_version='1.0-1', patch_set_hash=''))
            utils.update_build_record(_tmp, _pkg, phase=_ph, status=_st)
        _idx = readers.list_builds(_tmp)
        assert _idx['total'] == 2
        assert {i['package'] for i in _idx['items']} == {'acme1', 'acme2'}
        _done = readers.list_builds(_tmp, phase='done')
        assert _done['total'] == 1 and _done['items'][0]['package'] == 'acme1'
        _page = readers.list_builds(_tmp, limit=1, offset=1)
        assert _page['total'] == 2 and len(_page['items']) == 1
        _doc = readers.get_build(_tmp, 'acme1')
        assert _doc['found'] and _doc['verified']
        assert _doc['record']['phase'] == 'done'
        # tamper → found but NOT verified
        _path = os.path.join(_tmp, f"acme2{utils.BUILD_RECORD_SUFFIX}")
        _raw = open(_path).read().replace('"FAIL"', '"PASS"')
        with open(_path, 'w') as _fh:
            _fh.write(_raw)
        _doc = readers.get_build(_tmp, 'acme2')
        assert _doc['found'] and not _doc['verified'], _doc
        assert readers.get_build(_tmp, 'absent')['found'] is False
        # traversal-shaped names are rejected before any fs access
        assert not readers.valid_package_name('../etc/passwd')
        assert not readers.valid_package_name('Foo')
        assert not readers.valid_package_name('')
        assert readers.valid_package_name('libzstd')



def test_api01_http_endpoints_auth_and_payloads():
    """API-01 endpoints via TestClient: 401 without/with-wrong key, 200
    payloads for state/builds/build, 404 absent, 400 invalid name.
    Skips cleanly when python3-fastapi (or httpx) is not installed."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    try:
        from fastapi.testclient import TestClient  # noqa: F401
    except Exception:
        print('  SKIP — python3-fastapi/httpx not installed')
        return
    import json as _json
    import utils
    import webapi
    with tempfile.TemporaryDirectory() as _tmp:
        _bl = os.path.join(_tmp, 'build'); os.makedirs(_bl)
        _flags = os.path.join(_tmp, 'buildflags.json')
        with open(_flags, 'w') as _fh:
            _json.dump({'_format_version': 1,
                        'flags': {'cache_ready': True}}, _fh)
        utils.write_build_record(_bl, utils.new_build_record(
            package='acme1', intended_version='1.0-1', patch_set_hash=''))
        utils.update_build_record(_bl, 'acme1', phase='done', status='PASS')
        _keyp = os.path.join(_tmp, 'api.key')
        _app = webapi.create_app(
            buildlog_dir=_bl, flags_path=_flags, api_key_path=_keyp)
        from webapi import auth
        _key = auth.load_api_key(_keyp)
        _c = TestClient(_app)
        assert _c.get('/api/v1/state').status_code == 401
        assert _c.get('/api/v1/state',
                      headers={'X-Api-Key': 'nope'}).status_code == 401
        _h = {'X-Api-Key': _key}
        _r = _c.get('/api/v1/state', headers=_h)
        assert _r.status_code == 200, _r.text
        assert _r.json()['flags']['cache_ready'] is True
        assert _r.json()['record_phases'] == {'done': 1}
        _r = _c.get('/api/v1/builds', headers=_h)
        assert _r.status_code == 200 and _r.json()['total'] == 1
        _r = _c.get('/api/v1/builds/acme1', headers=_h)
        assert _r.status_code == 200 and _r.json()['verified'] is True
        assert _c.get('/api/v1/builds/absent', headers=_h).status_code == 404
        assert _c.get('/api/v1/builds/Bad_Name', headers=_h).status_code == 400



def test_api01_artifact_tail_windowing_and_guards():
    """API-01 chunk 2: read_artifact serves the three sidecar kinds;
    tail returns exactly the last N lines without full reads; oversize
    full reads are refused; kind allowlist + name validation hold."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from webapi import readers
    with tempfile.TemporaryDirectory() as _tmp:
        with open(os.path.join(_tmp, 'acme.buildlog'), 'w') as _fh:
            _fh.write("=== BUILD LOG: acme ===\nline2\n")
        with open(os.path.join(_tmp, 'acme'), 'w') as _fh:   # container log
            _fh.write(''.join(f"l{i}\n" for i in range(1000)))
        _doc = readers.read_artifact(_tmp, 'acme', 'buildlog')
        assert _doc['found'] and 'BUILD LOG: acme' in _doc['text']
        _doc = readers.read_artifact(_tmp, 'acme', 'log', tail=3)
        assert _doc['found'] and _doc['lines'] == ['l997', 'l998', 'l999']
        assert _doc['truncated'] is True and _doc['size'] > 0
        _doc = readers.read_artifact(_tmp, 'acme', 'log', tail=5000)
        assert len(_doc['lines']) == 1000          # asks-for-more → all
        # oversize full read refused (cap), tail still allowed
        readers_cap = readers._FULL_READ_CAP
        try:
            readers._FULL_READ_CAP = 10
            _doc = readers.read_artifact(_tmp, 'acme', 'log')
            assert _doc['found'] and _doc['truncated'] and 'error' in _doc
        finally:
            readers._FULL_READ_CAP = readers_cap
        assert readers.read_artifact(_tmp, 'acme', 'sneaky')['found'] is False
        assert readers.read_artifact(_tmp, '../x', 'buildlog')['found'] is False
        assert readers.read_artifact(_tmp, 'ghost', 'buildlog')['found'] is False



def test_api01_progress_aggregate():
    """API-01 chunk 3: progress() ports the rebuild monitoring loop —
    phases, rate over window, ETA vs total, in-flight log liveness,
    failed list, and FILTERED-cross-referenced delta residue."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import utils
    from webapi import readers
    with tempfile.TemporaryDirectory() as _tmp:
        for _pkg, _ph in (('done1', 'done'), ('fail1', 'failed'),
                          ('fly1', 'entry')):
            utils.write_build_record(_tmp, utils.new_build_record(
                package=_pkg, intended_version='1.0-1', patch_set_hash=''))
            utils.update_build_record(_tmp, _pkg, phase=_ph)
        with open(os.path.join(_tmp, 'fly1'), 'w') as _fh:
            _fh.write('building...\n')               # in-flight container log
        # delta pair: done1 missing one binary; vbuildlog explains 'doc1'
        # but NOT 'mystery1' → only mystery1 in residue
        with open(os.path.join(_tmp, 'done1.buildlog'), 'w') as _fh:
            _fh.write("--- DELTA ---\n"
                      "  emitted-not-declared: (none)\n"
                      "  declared-not-emitted: doc1, mystery1\n")
        with open(os.path.join(_tmp, 'done1.vbuildlog'), 'w') as _fh:
            _fh.write("--- FILTERED (declared but not predicted: 1) ---\n"
                      "  doc1\n")
        _doc = readers.progress(_tmp, total=10, window_s=3600)
        assert _doc['records'] == 3 and _doc['remaining'] == 7
        assert _doc['phases'] == {'done': 1, 'failed': 1, 'entry': 1}
        assert _doc['failed'] == ['fail1']
        # audit #213: rate counts only COMPLETED records (success), not the
        # failed or in-flight ones — only done1 here.
        assert _doc['completed_in_window'] == 1
        assert _doc['rate_per_hour'] == 1.0
        assert _doc['eta_hours'] is not None
        assert _doc['in_flight'][0]['package'] == 'fly1'
        assert _doc['in_flight'][0]['log_bytes'] == len('building...\n')
        assert _doc['deltas'] == [
            {'package': 'done1', 'missing': ['mystery1'], 'extra': []}]



def test_api01_jobs_backend_dispatch_capture_and_prompt():
    """API-01 chunk 4: ApiBackend runs queued commands through the SAME
    Cli dispatcher, captures console output per job, fails jobs on
    handler errors, and converts prompts into fail-fast errors (no
    interactive input over HTTP)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import tui
    from webapi.jobs import ApiBackend
    _prev = tui.tui_instance
    try:
        _b = ApiBackend()              # registers as tui.tui_instance
        # capture is asserted via the backend's own print seam — the
        # global tui.console facade gets swapped by other tests'
        # _stub_tui() and is deliberately not relied on here
        _b.register_command(
            'hello', lambda *a: _b.print('hi ' + ' '.join(a)),
            'test')
        _b.register_command('boom', lambda: 1 / 0, 'test')
        _b.register_command('ask', lambda: _b.prompt('password?'), 'test')
        assert _b.known_command('hello')      # known_command takes the verb
        assert not _b.known_command('nosuch')
        _j1 = _b.submit('hello a b')
        _j2 = _b.submit('boom')
        _j3 = _b.submit('ask')
        _b.shutdown()
        _b.wait()                      # drains the three jobs, then exits
        # output may carry logging-handler echo lines ('[INFO ] …')
        # alongside the console line — assert membership, not equality
        assert _j1.state == 'done' and 'hi a b' in _j1.output, _j1.as_dict()
        assert _j2.state == 'error'
        assert any('ZeroDivisionError' in _l for _l in _j2.output), _j2.output
        assert _j3.state == 'error'
        # audit #209: PromptRequired is re-raised through _dispatch_one and
        # delivered as the structured prompt_required error, not a generic
        # ERROR output line.
        assert _j3.error and _j3.error.startswith('prompt_required:'), \
            _j3.as_dict()
        assert _j1.as_dict()['elapsed'] is not None
        _ids = [j['id'] for j in _b.list_jobs()]
        assert _ids == [_j1.id, _j2.id, _j3.id]   # submission order
    finally:
        tui.tui_instance = _prev



def test_api01_config_redaction_and_command_routes():
    """API-01 chunk 4: config endpoint redacts secret-bearing keys;
    command routes validate verbs, queue jobs, and report results.
    HTTP part skips without fastapi; redaction reader always runs."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from webapi import readers
    with tempfile.TemporaryDirectory() as _tmp:
        _conf = os.path.join(_tmp, 'build.conf')
        with open(_conf, 'w') as _fh:
            _fh.write("[Repo]\nSigningKeyUid = athena\n"
                      "PublishSshKey = config/repo.key\n"
                      "ARCH = amd64\nMyPassword = hunter2\n")
        _doc = readers.read_config_redacted(_conf)
        assert 'hunter2' not in _doc['text']
        assert 'config/repo.key' not in _doc['text']
        assert 'PublishSshKey = [REDACTED]' in _doc['text']
        assert 'ARCH = amd64' in _doc['text']          # non-secret intact
        try:
            from fastapi.testclient import TestClient
        except Exception:
            print('  SKIP http part — python3-fastapi/httpx not installed')
            return
        import tui
        import webapi
        from webapi.jobs import ApiBackend
        _prev = tui.tui_instance
        try:
            _b = ApiBackend()
            _b.register_command(
                'hello', lambda *a: _b.print('hi'), 'test')
            _bl = os.path.join(_tmp, 'build'); os.makedirs(_bl)
            _app = webapi.create_app(
                buildlog_dir=_bl,
                flags_path=os.path.join(_tmp, 'f.json'),
                api_key_path=os.path.join(_tmp, 'api.key'),
                conf_path=_conf, backend=_b)
            from webapi import auth
            _h = {'X-Api-Key': auth.load_api_key(
                os.path.join(_tmp, 'api.key'))}
            _c = TestClient(_app)
            _r = _c.get('/api/v1/config', headers=_h)
            assert _r.status_code == 200 and 'hunter2' not in _r.text
            assert _c.post('/api/v1/command', headers=_h,
                           json={'cmd': 'nosuch x'}).status_code == 400
            assert _c.post('/api/v1/command', headers=_h,
                           json={'cmd': 'quit'}).status_code == 400
            _r = _c.post('/api/v1/command', headers=_h,
                         json={'cmd': 'hello'})
            assert _r.status_code == 202, _r.text
            _jid = _r.json()['job_id']
            _b.shutdown(); _b.wait()                  # drain queue
            _r = _c.get(f'/api/v1/jobs/{_jid}', headers=_h)
            assert _r.status_code == 200
            assert _r.json()['state'] == 'done'
            assert 'hi' in _r.json()['output']
            assert _c.get('/api/v1/jobs/nope',
                          headers=_h).status_code == 404
        finally:
            tui.tui_instance = _prev



def test_tui_state_tabstate_eviction_and_cmdline_history():
    """Audit #192: TabState.append evicts oldest entries past MAX_BUFFER_LINES
    and decrements scroll_offset by the evicted display rows (clamped at 0);
    CmdLine Up/Up/Down/Down stashes the in-progress draft on first Up and
    restores it past the newest entry."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    from tui import state as _state
    _ts = _state.TabState()
    _ts.buffer = [('x', 0)] * 5           # already over the (patched) cap
    _ts.scroll_offset = 1                  # user scrolled up
    _orig = _state.MAX_BUFFER_LINES
    _state.MAX_BUFFER_LINES = 3
    try:
        _ts.append([('x', 0)], width=80)   # len→6, drop 3 oldest
    finally:
        _state.MAX_BUFFER_LINES = _orig
    assert len(_ts.buffer) == 3
    assert _ts.scroll_offset == 0          # decremented past 0 → clamped
    _cl = _state.CmdLine()
    _cl.history = ['cmd1', 'cmd2']
    _cl.set_text('draft')
    assert _cl.history_prev()              # first Up stashes the draft
    assert _cl.text == 'cmd2' and _cl.hist_draft == 'draft'
    assert _cl.history_prev() and _cl.text == 'cmd1'
    assert _cl.history_next() and _cl.text == 'cmd2'
    assert _cl.history_next() and _cl.text == 'draft' and _cl.hist_idx is None



def test_webapi_readers_flags_repo_phase_mirror():
    """Audit #216: read_flags / repo_summary / phase_counts / mirror_state
    across happy + empty-dir paths (previously undirect-tested)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import json as _json
    from webapi import readers as _rd
    with tempfile.TemporaryDirectory() as _d:
        # read_flags: happy, malformed, missing
        _fp = os.path.join(_d, 'flags.json')
        with open(_fp, 'w') as _fh:
            _json.dump({'flags': {'cache_ready': True}}, _fh)
        assert _rd.read_flags(_fp) == {'flags': {'cache_ready': True}}
        with open(_fp, 'w') as _fh:
            _fh.write('{not json')
        assert _rd.read_flags(_fp) == {'flags': {}}
        assert _rd.read_flags(os.path.join(_d, 'nope.json')) == {'flags': {}}

        # repo_summary: a .deb under dists, plus an empty repo
        _bindir = os.path.join(_d, 'repo', 'dists', 'thor', 'main',
                               'binary-amd64')
        os.makedirs(_bindir)
        with open(os.path.join(_bindir, 'foo_1.0_amd64.deb'), 'wb') as _fh:
            _fh.write(b'0123456789')
        _rs = _rd.repo_summary(os.path.join(_d, 'repo'))
        assert _rs['total_files'] == 1 and _rs['total_bytes'] == 10
        assert _rd.repo_summary(os.path.join(_d, 'empty'))['total_files'] == 0

        # phase_counts: two records + one non-record + empty dir
        _blog = os.path.join(_d, 'buildlog')
        os.makedirs(_blog)
        for _n, _ph in (('a', 'done'), ('b', 'done'), ('c', 'failed')):
            with open(os.path.join(_blog, f'{_n}.build.json'), 'w') as _fh:
                _json.dump({'phase': _ph}, _fh)
        with open(os.path.join(_blog, 'notarecord.txt'), 'w') as _fh:
            _fh.write('x')
        _pc = _rd.phase_counts(_blog)
        assert _pc == {'done': 2, 'failed': 1}, _pc
        assert _rd.phase_counts(os.path.join(_d, 'noblog')) == {}

        # mirror_state: empty dirs → sane defaults; coord-head.json present
        _ms = _rd.mirror_state(os.path.join(_d, 'cfg'), os.path.join(_d, 'crd'))
        assert _ms['mirrors'] == {} and _ms['coord_head'] is None
        _crd = os.path.join(_d, 'coord')
        os.makedirs(_crd)
        with open(os.path.join(_crd, 'coord-head.json'), 'w') as _fh:
            _json.dump({'head_time': 'T'}, _fh)
        _ms2 = _rd.mirror_state(os.path.join(_d, 'cfg'), _crd)
        assert _ms2['coord_head'] == {'head_time': 'T'}



def test_webapi_sse_stream_emits_all_lines_then_end():
    """Audit #206: the SSE /jobs/{id}/stream endpoint streams every output
    line of a finished job then a trailing `event: end`.  Skips when fastapi
    isn't installed (as the other webapi HTTP tests do)."""
    sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    try:
        from fastapi.testclient import TestClient
    except Exception:
        print('  SKIP #206 — python3-fastapi/httpx not installed')
        return
    import tui
    import webapi
    from webapi.jobs import ApiBackend
    from webapi import auth
    _prev = tui.tui_instance
    with tempfile.TemporaryDirectory() as _tmp:
        try:
            _b = ApiBackend()
            _b.register_command(
                'emit', lambda *a: (_b.print('line1'), _b.print('line2')),
                'test')
            _conf = os.path.join(_tmp, 'build.conf')
            with open(_conf, 'w') as _fh:
                _fh.write('[Repo]\nARCH = amd64\n')
            _bl = os.path.join(_tmp, 'build')
            os.makedirs(_bl)
            _app = webapi.create_app(
                buildlog_dir=_bl, flags_path=os.path.join(_tmp, 'f.json'),
                api_key_path=os.path.join(_tmp, 'api.key'),
                conf_path=_conf, backend=_b)
            _h = {'X-Api-Key': auth.load_api_key(
                os.path.join(_tmp, 'api.key'))}
            _job = _b.submit('emit')
            _b.shutdown()
            _b.wait()                       # run the job to terminal state
            assert _job.state == 'done', _job.as_dict()
            _c = TestClient(_app)
            _r = _c.get(f'/api/v1/jobs/{_job.id}/stream', headers=_h)
            assert _r.status_code == 200
            assert 'line1' in _r.text and 'line2' in _r.text
            assert 'event: end' in _r.text
        finally:
            tui.tui_instance = _prev

TESTS = [
    test_v2_wrap_helpers_round_trip,
    test_v2_state_append_and_scroll,
    test_tui_page_rows_subtracts_widgets_on_console_only,
    test_dispatcher_tick_reads_key_and_drains_events,
    test_dispatcher_cancel_pending_futures_unblocks_callers,
    test_dispatcher_request_prompt_is_quit_aware,
    test_dispatcher_sample_status_sets_status_text_inline,
    test_tui_single_ui_thread_topology,
    test_v2_cmdline_edit_and_history,
    test_v2_dispatcher_key_events_gated_without_prompt,
    test_v2_dispatcher_status_bar_includes_network,
    test_v2_dispatcher_prompt_future_round_trip,
    test_v2_dispatcher_tab_switch_by_index,
    test_v2_dispatcher_progressbar_widget_lifecycle,
    test_v2_dispatcher_adaptive_idle_no_widgets,
    test_v2_logging_bridge_consolidates_to_log_tab_with_stage_prefix,
    test_api01_key_lifecycle,
    test_api01_readers_index_get_and_tamper,
    test_api01_http_endpoints_auth_and_payloads,
    test_api01_artifact_tail_windowing_and_guards,
    test_api01_progress_aggregate,
    test_api01_jobs_backend_dispatch_capture_and_prompt,
    test_api01_config_redaction_and_command_routes,
    test_per_module_logger_names_pin_routing,
    test_logging_bridge_splits_multiline_records,
    test_render_failure_one_shot_capture_writes_post_mortem,
    test_render_failure_capture_no_op_on_success,
    test_progress_rate_counts_only_completed_records,
    test_progress_bar_set_max_clamps_to_current_value,
    test_tier3_tui_auth_source_pins,
    test_api_backend_prunes_completed_jobs,
    test_dispatcher_handles_cancelled_future,
    test_safe_addstr_right_margin_multibyte_safe,
    test_render_anchors_widgets_to_bottom_band,
    test_read_artifact_truncated_uses_byte_counts,
    test_progress_reads_buildlogs_via_context_manager,
    test_tui_state_tabstate_eviction_and_cmdline_history,
    test_webapi_readers_flags_repo_phase_mirror,
    test_webapi_sse_stream_emits_all_lines_then_end,
]


if __name__ == '__main__':
    from _test_helpers import run_tests
    raise SystemExit(run_tests(TESTS))
