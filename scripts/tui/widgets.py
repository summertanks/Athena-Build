"""Widgets — live overlays managed via the tui_instance singleton.

ProgressBar / Spinner resolve `tui_instance` at construction and call
`tui_instance.add_widget(self)` (legacy contract).  Both backends
implement `add_widget` and `del_widget`:

  - Cli (headless): prints `[start]` / `[done: N/M]` markers, stores
    the widget in a dict for lifetime accounting.
  - Tui (curses):   posts WidgetAdd / WidgetRemove events; the
    dispatcher renders the widget overlaid on the console tab and
    the WIDGET_IDLE_TIMEOUT cap drives animation at ~10 fps without
    per-step event posts.

`__str__` is the rendered representation called by the curses
Renderer.  `next_frame_at` is a Tui-side animation hint — the
dispatcher uses it to wake at the next desired frame.  Cli ignores it.
"""
from __future__ import annotations

import time
from math import floor
from typing import Any, Dict, Optional


def _instance() -> Any:
    """Resolve the active backend (Tui or Cli) at call time."""
    import tui as _pkg
    return _pkg.tui_instance


# ── ProgressBar ──────────────────────────────────────────────────────────
class ProgressBar:
    """Bar widget with a label, fill, value/total, and rate.

    Identical visual format to the legacy ProgressBar so operators
    don't see a behavior change.  `step()` mutates the internal
    counter; under the Tui backend the dispatcher polls at
    WIDGET_IDLE_TIMEOUT (~100 ms) and renders the current state.
    Under Cli, step is silent (no per-step output)."""

    RUNNING = 1
    PAUSED  = 2
    STOPPED = 3

    _DEFAULT_FMT_WITH_RATE = '  {label} [{bar}] {value}/{total}  {rate}'
    _DEFAULT_FMT_NO_RATE   = '  {label} [{bar}] {value}/{total}'
    _SCALE: Dict[str, float] = {'K': 1e3, 'M': 1e6, 'G': 1e9}

    def __init__(self, label: str, itr_label: str = 'it/s', bar_width: int = 40,
                 scale_factor: str = '', maxvalue: int = 100, fmt: str = '',
                 show_rate: bool = True, label_width: int = 0) -> None:
        self._label_width  = label_width if label_width > 0 else 20
        self._label        = label[:self._label_width].ljust(self._label_width)
        self._itr_label    = itr_label[:8]
        self._max          = max(1, maxvalue)
        self._value        = 0
        self._state        = self.RUNNING
        self._t0           = time.time_ns()
        self._scale_factor = scale_factor if scale_factor in ('', 'K', 'M', 'G') else ''
        self._bar_width    = max(10, min(60, bar_width))
        self._show_rate    = show_rate
        self._fmt = fmt or (self._DEFAULT_FMT_WITH_RATE if show_rate
                            else self._DEFAULT_FMT_NO_RATE)
        b = _instance()
        self._widget_id = b.add_widget(self) if b is not None else id(self)

    # ── Animation contract (read by the Tui dispatcher) ─────────────────
    def next_frame_at(self) -> Optional[float]:
        """ProgressBar doesn't have a fixed frame rate — the
        dispatcher's WIDGET_IDLE_TIMEOUT cap drives polling."""
        return None

    # ── Mutation ────────────────────────────────────────────────────────
    @property
    def value(self) -> int:
        return self._value

    def step(self, value: int = 1) -> None:
        if self._state != self.RUNNING:
            return
        self._value = min(self._value + value, self._max)
        if self._value >= self._max:
            self._state = self.STOPPED

    def label(self, message: str) -> None:
        self._label = message.strip()[:self._label_width].ljust(self._label_width)

    def pause(self) -> None:
        if self._state == self.RUNNING:
            self._state = self.PAUSED

    def resume(self) -> None:
        if self._state == self.PAUSED:
            self._state = self.RUNNING

    def set_max(self, value: int) -> None:
        # Never below the current progress: lowering max under _value would
        # make filled exceed the bar width and overflow the render.
        self._max = max(1, value, self._value)
        # step() auto-STOPs at the old max.  When the REAL max
        # arrives later (download_file grow-path, mirror-publish lazily-sized
        # bar seeded at maxvalue=1), un-freeze so the bar keeps animating
        # instead of sticking at 100% / 1-of-1.
        if self._state == self.STOPPED and self._value < self._max:
            self._state = self.RUNNING

    def reset(self) -> None:
        self._value = 0
        self._state = self.RUNNING
        self._t0    = time.time_ns()

    def close(self, persist: bool = False) -> None:
        """Stop the bar and remove it from the widget list.

        `persist=True` prints the final rendered line to the console
        tab so the operator sees it after the bar disappears.  Backends
        that don't render the unicode bar (the headless Cli) opt out via
        `renders_widgets = False` and rely on their own [done] marker."""
        self._state = self.STOPPED
        b = _instance()
        if persist and b is not None and getattr(b, 'renders_widgets', True):
            b.print(str(self))
        if b is not None:
            b.del_widget(self._widget_id)

    # ── Render ──────────────────────────────────────────────────────────
    def __str__(self) -> str:
        pct    = self._value / self._max
        filled = floor(pct * self._bar_width)
        bar    = '█' * filled + '░' * (self._bar_width - filled)
        elapsed = max(1, time.time_ns() - self._t0)
        rate_hz = (self._value / elapsed) * 1e9
        sf = self._scale_factor
        if sf == '':
            if   rate_hz >= 1e9: sf = 'G'
            elif rate_hz >= 1e6: sf = 'M'
            elif rate_hz >= 1e3: sf = 'K'
        rate_disp = rate_hz / self._SCALE.get(sf, 1.0)
        rate_str  = f'{rate_disp:.2f}{sf}{self._itr_label}'
        pct_str   = f'{pct * 100:5.1f}%'
        if   self._max >= 1.15e9: val_sf = 'G'
        elif self._max >= 1.15e6: val_sf = 'M'
        elif self._max >= 1.15e3: val_sf = 'K'
        else:                     val_sf = ''
        val_div   = self._SCALE.get(val_sf, 1.0)
        value_str = f'{self._value / val_div:.2f}{val_sf}' if val_sf else str(self._value)
        total_str = f'{self._max   / val_div:.2f}{val_sf}' if val_sf else str(self._max)
        return self._fmt.format(
            label=self._label, bar=bar,
            value=value_str, total=total_str,
            rate=rate_str, pct=pct_str,
        )


# ── Spinner ──────────────────────────────────────────────────────────────
class Spinner:
    """Animated spinner — cycles a glyph every 100 ms.

    Unlike ProgressBar, Spinner DOES animate on its own —
    `next_frame_at` returns the time of the next frame so the Tui
    dispatcher wakes for it.  10 fps is the visible-perception sweet
    spot."""

    _FRAMES = ['⣾', '⣽', '⣻', '⢿', '⡿', '⣟', '⣯', '⣷']
    _FRAME_INTERVAL = 0.1     # 100 ms / 10 fps

    def __init__(self, message: str) -> None:
        self._message = message[:70]
        self._frame   = 0
        self._last_t  = time.monotonic()
        self._done    = False
        b = _instance()
        self._widget_id = b.add_widget(self) if b is not None else id(self)

    def next_frame_at(self) -> Optional[float]:
        if self._done:
            return None
        return self._last_t + self._FRAME_INTERVAL

    def __str__(self) -> str:
        now = time.monotonic()
        if now - self._last_t >= self._FRAME_INTERVAL:
            self._frame = (self._frame + 1) % len(self._FRAMES)
            self._last_t = now
        return f'  {self._FRAMES[self._frame]}  {self._message}'

    def done(self) -> None:
        # idempotent — a Spinner that spans several passes (e.g.
        # cmd_parse_dependency's "Parsing Dependencies") gets done() at the
        # end of one pass AND again on a later SELECT-LOCK return; without
        # this guard the "✓ … done" line prints twice.
        if self._done:
            return
        self._done = True
        b = _instance()
        if b is not None:
            b.print(f'  ✓  {self._message} done')
            b.del_widget(self._widget_id)
