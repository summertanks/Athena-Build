"""Generate the tasksel `.desc` from the selection groups.

The installer's software-selection menu was a STATIC `.desc` compiled into
the athena-tasksel fork (tasks/* → makedesc.pl), manually mirrored from
pkg.list groups (test-enforced) — a menu change needed a fork rebuild + new
ISO.  This module derives the `.desc` from the SIGNED LOCKFILE's groups at
ISO-mastering time instead; the ISO stages it under `/.disk/` and our
athena-pkgsel pre-pkgsel.d hook copies it onto /target before tasksel
renders the menu.  One source of truth, no fork rebuild for menu edits;
the fork's static desc remains as a fallback.

cdebconf is FRAGILE (memory: tasksel-desc-keep-minimal): a task is
silently dropped from the dialog when its Section isn't a known value or
its Description carries non-ASCII, commas, em-dashes, or parentheses.
Every emitted field goes through `_sanitize`.
"""

from typing import Dict, List, Optional

_BANNED = set(',()[]{}—–…')


def _sanitize(text: str) -> str:
    """ASCII-only, no commas/dashes-beyond-hyphen/parens, collapsed
    whitespace — the shape cdebconf renders reliably."""
    _out: List[str] = []
    for _ch in text:
        if ord(_ch) > 126 or _ch in _BANNED:
            _out.append(' ')
        else:
            _out.append(_ch)
    return ' '.join(''.join(_out).split())


def _title(group: str) -> str:
    """Default short description from the group name."""
    return _sanitize(group.replace('-', ' ').title())


def generate_desc(groups: 'Dict[str, list]',
                  meta: 'Optional[Dict[str, dict]]' = None) -> str:
    """makedesc-shaped task stanzas for every non-[base] group.

    `groups` maps group → SEED name list (the tasksel Key vocabulary —
    use the lockfile's ``seeds.pkg``, NOT the credited per-group deltas).
    `meta` maps group → {'description': ...} (pkg.list ``## Description:``).
    [base] is skipped — it is debootstrapped, never a task.
    """
    _meta = meta or {}
    _stanzas: List[str] = []
    for _group, _seeds in groups.items():
        if _group == 'base' or not _seeds:
            continue
        _desc = _sanitize(
            (_meta.get(_group, {}) or {}).get('description', '')
        ) or _title(_group)
        _lines = [
            f'Task: {_sanitize(_group)}',
            'Section: user',
            'Relevance: 5',
            f'Description: {_desc}',
            f' Install the {_sanitize(_group)} package set.',
            'Key: ',
        ]
        _lines.extend(f' {_sanitize(_s)}' for _s in _seeds)
        _stanzas.append('\n'.join(_lines))
    return ('\n\n'.join(_stanzas) + '\n') if _stanzas else ''
