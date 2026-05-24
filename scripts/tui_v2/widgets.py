"""Widgets — live overlays rendered on the console tab.

Widget contract (duck-typed; no formal Protocol needed):
    __str__(self) -> str
        Return the rendered string.  Called by the Renderer.

    next_frame_at(self) -> Optional[float]
        Wall-clock monotonic time at which this widget wants its next
        redraw, or None when the widget doesn't animate on its own
        (e.g. ProgressBar only updates on step()).

Widgets register with the dispatcher via WidgetAdd events.  When state
changes that should be visible (step, label update), they post a
WidgetTick to wake the dispatcher; the next render picks it up.

ProgressBar.step() and Spinner stay independent of the renderer — they
hold a reference to the dispatcher and post events.  The renderer
never imports widgets; it just calls str(w) on whatever's in the list.
"""
from __future__ import annotations

import time
from math import floor
from typing import Any, Dict, Optional


# ── Dispatcher reference (set by tui.py at construction) ─────────────────
_dispatcher: Optional[Any] = None


def set_dispatcher(d: Any) -> None:
    """Wire the singleton dispatcher reference.  Called once by Tui."""
    global _dispatcher
    _dispatcher = d


def _post(event: Any) -> None:
    """Post helper that no-ops when no dispatcher is bound (test mode)."""
    if _dispatcher is not None:
        _dispatcher.post(event)


# ── ProgressBar ──────────────────────────────────────────────────────────
class ProgressBar:
    """Bar widget with a label, fill, value/total, and rate.

    Identical visual format to the legacy Tui ProgressBar so operators
    don't see a behavior change.  `step()` posts a WidgetTick to
    schedule a redraw; otherwise the bar is quiescent (next_frame_at
    returns None — no per-frame animation cost when idle)."""

    RUNNING = 1
    PAUSED  = 2
    STOPPED = 3

    _DEFAULT_FMT_WITH_RATE = '  {label} [{bar}] {value}/{total}  {rate}'
    _DEFAULT_FMT_NO_RATE   = '  {label} [{bar}] {value}/{total}'
    _SCALE: Dict[str, float] = {'K': 1e3, 'M': 1e6, 'G': 1e9}

    def __init__(self, label: str, itr_label: str = 'it/s', bar_width: int = 40,
                 scale_factor: str = '', maxvalue: int = 100, fmt: str = '',
                 show_rate: bool = True, label_width: int = 0) -> None:
        # Lazy-import to avoid cycle: tui_v2/__init__.py imports facade,
        # which would import widgets at top level.
        from .events import WidgetAdd
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
        _post(WidgetAdd(self))

    # ── Animation contract ──────────────────────────────────────────────
    def next_frame_at(self) -> Optional[float]:
        """ProgressBar doesn't animate on its own — it advances only
        when step() is called.  Returning None means the dispatcher
        sleeps until the next event (typically step()'s WidgetTick)."""
        return None

    # ── Mutation ────────────────────────────────────────────────────────
    @property
    def value(self) -> int:
        return self._value

    def step(self, value: int = 1) -> None:
        """Advance by `value` units.  Posts a WidgetTick to redraw."""
        if self._state != self.RUNNING:
            return
        self._value = min(self._value + value, self._max)
        if self._value >= self._max:
            self._state = self.STOPPED
        from .events import WidgetTick
        _post(WidgetTick())

    def label(self, message: str) -> None:
        self._label = message.strip()[:self._label_width].ljust(self._label_width)
        from .events import WidgetTick
        _post(WidgetTick())

    def pause(self) -> None:
        if self._state == self.RUNNING:
            self._state = self.PAUSED

    def resume(self) -> None:
        if self._state == self.PAUSED:
            self._state = self.RUNNING

    def set_max(self, value: int) -> None:
        self._max = max(1, value)

    def reset(self) -> None:
        self._value = 0
        self._state = self.RUNNING
        self._t0    = time.time_ns()

    def close(self, persist: bool = False) -> None:
        """Stop the bar and remove it from the widget list.

        `persist=True` posts the final rendered line to the console
        tab so the operator sees it after the bar disappears."""
        from .events import PrintEvent, WidgetRemove
        self._state = self.STOPPED
        if persist:
            _post(PrintEvent(str(self)))
        _post(WidgetRemove(id(self)))

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

    Unlike ProgressBar, Spinner DOES animate on its own — next_frame_at
    returns the time of the next frame so the dispatcher wakes for it.
    10 fps is the visible-perception sweet spot."""

    _FRAMES = ['⣾', '⣽', '⣻', '⢿', '⡿', '⣟', '⣯', '⣷']
    _FRAME_INTERVAL = 0.1     # 100 ms / 10 fps

    def __init__(self, message: str) -> None:
        from .events import WidgetAdd
        self._message = message[:70]
        self._frame   = 0
        self._last_t  = time.monotonic()
        self._done    = False
        _post(WidgetAdd(self))

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
        from .events import PrintEvent, WidgetRemove
        self._done = True
        _post(PrintEvent(f'  ✓  {self._message} done'))
        _post(WidgetRemove(id(self)))
