"""State — the single-owner data model.

The dispatcher (dispatcher.py) is the sole mutator.  The renderer
(render.py) is read-only.  Producer threads enqueue Events; they never
touch State.

No curses, no threading, no logging imports — keeps this layer pure
and trivially testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import wrap


# ── Severity constants (mirror legacy Tui.SEVERITY_*) ────────────────────
SEVERITY_INFO    = 3
SEVERITY_WARNING = 2
SEVERITY_ERROR   = 1

# ── Tab sizing default ──────────────────────────────────────────────────
MAX_BUFFER_LINES = 10000

# ── Default tabs (insertion order = F1..Fn mapping) ──────────────────────
DEFAULT_TABS: Tuple[str, ...] = (
    'console', 'log', 'cache', 'build', 'chroot', 'iso',
)


@dataclass
class TabState:
    """Per-tab buffer + scroll position.

    `buffer` is a list of `(text, curses_attr)` tuples — one logical
    line per entry.  Render-time wrap (see render.py) expands long
    lines into multiple display rows; the buffer itself never grows
    for wrap.

    `scroll_offset` is in DISPLAY-ROW units (rows above the bottom of
    the visible viewport).  0 = sticky at-bottom; positive = user has
    scrolled up.  Anchored on display rows (not logical lines) so
    terminal resize re-wraps without losing the reading position.

    `selected` is True for the single active tab.  Exactly one tab in a
    State has selected=True at any time (enforced by dispatcher's
    TabActivate handler)."""
    buffer: List[Tuple[str, int]] = field(default_factory=list)
    scroll_offset: int = 0
    selected: bool = False

    def append(self, lines: List[Tuple[str, int]], width: int) -> None:
        """Add entries to the buffer.

        If the user is scrolled away (scroll_offset > 0), bump the
        offset by the new entries' display-row count so the visible
        anchor stays put on the same content.  If overflow exceeds
        MAX_BUFFER_LINES, drop oldest entries from the front and
        decrement scroll_offset by their display rows (clamped at 0
        if the user was viewing rows that just got evicted)."""
        if not lines:
            return
        self.buffer.extend(lines)
        if self.scroll_offset > 0:
            self.scroll_offset += sum(
                wrap.entry_display_rows(t, width) for t, _ in lines
            )
        if len(self.buffer) > MAX_BUFFER_LINES:
            drop_n = len(self.buffer) - MAX_BUFFER_LINES
            if self.scroll_offset > 0:
                dropped_rows = sum(
                    wrap.entry_display_rows(t, width)
                    for t, _ in self.buffer[:drop_n]
                )
                self.scroll_offset = max(0, self.scroll_offset - dropped_rows)
            del self.buffer[:drop_n]

    def clear(self) -> None:
        self.buffer.clear()
        self.scroll_offset = 0

    def trim_to(self, mark: int, width: int) -> None:
        """Truncate buffer to `mark` entries; clamp scroll_offset."""
        self.buffer = self.buffer[:mark]
        self.scroll_offset = min(
            self.scroll_offset,
            max(0, wrap.total_display_rows(self.buffer, width) - 1),
        )

    def scroll_by(self, delta: int, width: int, content_rows: int) -> None:
        """Adjust scroll_offset by *delta* display rows, clamped to
        [0, max_off] where max_off keeps the topmost wrapped row
        visible at the top of the viewport."""
        max_off = max(0, wrap.total_display_rows(self.buffer, width) - content_rows)
        self.scroll_offset = max(0, min(max_off, self.scroll_offset + delta))


@dataclass
class CmdLine:
    """In-progress command-line input state.

    `text` is the line being edited.  `cursor` is the edit position
    (0..len(text)).  `history` is the submitted-command log; `hist_idx`
    walks history (None = editing a fresh line); `hist_draft` stashes
    the in-progress line when the user enters history navigation so
    pressing Down past newest restores it.

    `masked` is True during PROMPT_PASSWORD so the renderer paints '*'
    instead of the chars."""
    text: str = ''
    cursor: int = 0
    history: List[str] = field(default_factory=list)
    hist_idx: Optional[int] = None
    hist_draft: str = ''
    masked: bool = False

    # ── Editing ──────────────────────────────────────────────────────────
    def insert(self, ch: str) -> None:
        self.text = self.text[:self.cursor] + ch + self.text[self.cursor:]
        self.cursor += len(ch)

    def backspace(self) -> bool:
        if self.cursor <= 0:
            return False
        self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
        self.cursor -= 1
        return True

    def move_left(self) -> bool:
        if self.cursor > 0:
            self.cursor -= 1
            return True
        return False

    def move_right(self) -> bool:
        if self.cursor < len(self.text):
            self.cursor += 1
            return True
        return False

    def set_text(self, s: str) -> None:
        self.text = s
        self.cursor = len(s)

    def reset(self) -> None:
        self.text = ''
        self.cursor = 0
        self.hist_idx = None
        self.hist_draft = ''

    # ── History ──────────────────────────────────────────────────────────
    def push_history(self, line: str) -> None:
        line = line.strip()
        if line:
            self.history.append(line)
        self.hist_idx = None
        self.hist_draft = ''

    def history_prev(self) -> bool:
        """↑ — walk one step toward older.  First Up stashes the draft."""
        if not self.history:
            return False
        if self.hist_idx is None:
            self.hist_draft = self.text
            self.hist_idx = len(self.history) - 1
            self.set_text(self.history[self.hist_idx])
            return True
        if self.hist_idx > 0:
            self.hist_idx -= 1
            self.set_text(self.history[self.hist_idx])
            return True
        return False

    def history_next(self) -> bool:
        """↓ — walk one step toward newer.  Past newest, restore draft."""
        if self.hist_idx is None:
            return False
        if self.hist_idx < len(self.history) - 1:
            self.hist_idx += 1
            self.set_text(self.history[self.hist_idx])
            return True
        self.hist_idx = None
        self.set_text(self.hist_draft)
        self.hist_draft = ''
        return True


@dataclass
class State:
    """Top-level dispatcher-owned state.

    Composed of: per-tab buffers, command-line input, live widgets,
    status snapshot, prompt string, dirty flag.  Methods on State are
    pure data mutations — the dispatcher is the only thing that calls
    them.

    `dirty` is set by mutating methods; cleared by the dispatcher after
    a successful render.  Watch a single boolean instead of plumbing
    explicit "needs-redraw" returns out of every handler."""
    tabs: Dict[str, TabState] = field(default_factory=dict)
    cmd: CmdLine = field(default_factory=CmdLine)
    widgets: List[object] = field(default_factory=list)
    cmd_prompt: str = '> '
    banner: str = 'Athena Build'
    status_text: str = ''
    dirty: bool = True
    too_small: bool = False
    quit: bool = False
    exit_code: int = 0

    def __post_init__(self) -> None:
        if not self.tabs:
            for name in DEFAULT_TABS:
                self.tabs[name] = TabState(selected=(name == 'console'))

    # ── Tab management ───────────────────────────────────────────────────
    def active_tab_name(self) -> str:
        for name, t in self.tabs.items():
            if t.selected:
                return name
        # Recover: select first
        first = next(iter(self.tabs))
        self.tabs[first].selected = True
        return first

    def active_tab(self) -> TabState:
        return self.tabs[self.active_tab_name()]

    def activate(self, name: str) -> bool:
        if name not in self.tabs:
            return False
        for t in self.tabs.values():
            t.selected = False
        self.tabs[name].selected = True
        self.dirty = True
        return True

    def activate_by_index(self, idx: int) -> bool:
        keys = list(self.tabs)
        if 0 <= idx < len(keys):
            return self.activate(keys[idx])
        return False

    def add_tab(self, name: str) -> bool:
        name = name.strip()
        if not name or name in self.tabs:
            return False
        self.tabs[name] = TabState()
        self.dirty = True
        return True

    # ── Widget management ────────────────────────────────────────────────
    def add_widget(self, w: object) -> int:
        self.widgets.append(w)
        self.dirty = True
        return id(w)

    def remove_widget(self, wid: int) -> None:
        before = len(self.widgets)
        self.widgets = [w for w in self.widgets if id(w) != wid]
        if len(self.widgets) != before:
            self.dirty = True

    # ── Animation deadline ───────────────────────────────────────────────
    def next_animation_deadline(self, now: float) -> Optional[float]:
        """Seconds from `now` until the nearest widget wants a frame.

        Returns None when no widget needs animation — caller can block
        on the event queue indefinitely (resize / new events still
        wake it via curses KEY_RESIZE and producer posts)."""
        deadlines: List[float] = []
        for w in self.widgets:
            if hasattr(w, 'next_frame_at'):
                d = w.next_frame_at()
                if d is not None:
                    deadlines.append(d)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)
