"""Pure char-based line wrap helpers (port of P7 from tui.py).

Split out into its own module so:
  1. The renderer (render.py) and state (state.py) can both use them
     without circular imports.
  2. Tests can exercise them without any TUI / curses init.

Character-based wrap is deliberate: log output has wrapped tokens
(URLs, hex digests, stack frames) that read better at fixed columns
than at word boundaries.
"""
from __future__ import annotations

from typing import List, Tuple


def wrap_line(text: str, width: int) -> List[str]:
    """Split *text* into chunks of at most *width* chars.

    Empty input returns `['']` so blank entries still contribute one
    display row (caller line counts stay consistent).  Width <= 0 is a
    fallback for tests / pre-init contexts — returns the input
    unchanged in a single-element list.
    """
    if width <= 0 or not text:
        return [text]
    if len(text) <= width:
        return [text]
    return [text[i:i + width] for i in range(0, len(text), width)]


def entry_display_rows(text: str, width: int) -> int:
    """Number of display rows a single buffer entry occupies at the
    given width.  Always >= 1 so blank entries take a row."""
    if width <= 0 or not text:
        return 1
    return max(1, -(-len(text) // width))   # ceil-div


def total_display_rows(buffer: List[Tuple[str, int]], width: int) -> int:
    """Sum of wrapped display rows over every entry in *buffer*."""
    return sum(entry_display_rows(t, width) for t, _ in buffer)
