"""Interactive package-set selector TUI.

Lets the operator toggle packages in `config/pkg.list` AND `config/pool.list`
(and add new ones from the apt cache) without hand-editing the files.  The
pool.list tier appears as the reserved `(pool)` group at the end of the list —
toggle/add/drop work identically, and save routes pool entries to pool.list
(comment-preserving) and the rest to pkg.list.  Runs in a dedicated `select`
tab; the dispatcher routes keystrokes to this controller's `handle_key` while
that tab is active (F-keys still switch tabs, so the operator can always
leave).

Key bindings (while the `select` tab is active):
    ↑ / ↓        move the row cursor
    PgUp / PgDn  page the cursor
    Space        toggle the package on the cursor row
    a            add a package (input prompt; tab-completes cache names)
    d            deselect the package on the cursor row
    [ / ]        previous / next group
    s            save → write config/pkg.list + pool.list (stay in selector)
    W            save & apply → write, re-parse, preview impact, accept/cancel
    q            quit the selector (prompts apply/discard if unsaved edits)

Each row shows: `[*]` / `[ ]`, name, installed size, direct-dep
count, transitive-closure footprint (approximate — first-provider
BFS over Depends/Pre-Depends, computed lazily in the background),
and the one-line apt description.

The model is the editable mirror of pkg.list: an ordered list of
(group, name, selected) entries.  Save preserves group order, the
`## Description:` comments, and within-group ordering; unselected
entries are dropped and newly-added ones are appended to their group.
"""
from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional, Tuple

import tui
import utils


# Row kinds in the flattened display list.
_HEADER = 'header'
_PKG    = 'pkg'

# Reserved pseudo-group key for the pool.list tier.  pool.list is a FLAT
# file (no INI sections), but we surface it inside the same selector as one
# more group so the operator toggles/adds/drops pool-only packages with the
# identical UI.  On save it routes to config/pool.list (not pkg.list).  The
# parenthesised name is RESERVED: a real pkg.list section literally written
# `[(pool)]` would parse to this same key, so _load_model detects that clash
# and warns rather than silently conflating the two.
POOL_GROUP = '(pool)'


class _Row:
    """One display row — a group header or a package entry."""
    __slots__ = ('kind', 'group', 'name')

    def __init__(self, kind: str, group: str, name: str = '') -> None:
        self.kind  = kind
        self.group = group
        self.name  = name


class SelectPackages:
    """Controller for the package-selector tab.

    Owns the editable model + cursor + scroll + a lazy metadata cache.
    Renders by replacing the `select` tab's buffer wholesale (via
    `tui_instance.set_tab_buffer`) every time state changes."""

    TAB = 'select'

    def __init__(self, config, cache, tui_inst) -> None:
        self._config = config
        self._cache  = cache
        self._tui    = tui_inst
        self._path   = config.pkglist_path
        self._poolpath = config.poollist_path

        # Editable model: group → ordered list of [name, selected].
        # Selection defaults True for every name already in pkg.list.
        # The reserved POOL_GROUP holds config/pool.list entries.
        self._groups: Dict[str, List[List]] = {}
        self._meta:   Dict[str, Dict[str, str]] = {}
        self._load_model()

        # View state.
        self._cursor = 0      # index into the flattened row list
        self._scroll = 0      # top visible row index
        self._unsaved = False

        # Inline input mode — the selector owns ALL its keystrokes via
        # the dispatcher's key interceptor and NEVER calls
        # request_prompt (doing so would cancel the shell's idle prompt
        # and kill the shell thread, since the dispatcher has a single
        # pending-prompt slot).  Instead, 'add' and 'quit-confirm'
        # render an input line inside the select tab and capture keys
        # here.  None = navigation mode; 'add' = typing a pkg name;
        # 'quit' = y/n confirm.
        self._input_mode: Optional[str] = None
        self._input_buffer = ''
        self._add_group = ''   # group the 'add' input targets

        # Lazy per-package metadata cache:
        #   name -> {'size': int_kb, 'deps': int, 'desc': str,
        #            'closure': Optional[int_kb]}   (None = not yet computed)
        self._pkgmeta: Dict[str, Dict] = {}
        self._closure_inflight: set = set()
        self._closure_lock = threading.Lock()

        # Save&quit handoff.  cmd_cache_select blocks on wait_for_done() (shell
        # thread) while this selector runs on the dispatcher thread; an exit
        # path sets _intent + _done, so the shell thread resumes and runs the
        # parse/accept transaction in normal console context.  Intents:
        #   'apply'   — save & quit: re-parse, preview impact, accept/cancel
        #   'discard' — quit, revert all edits (transactional rollback)
        #   'quit'    — quit, leave disk as-is (no pending in-memory edits)
        self._done = threading.Event()
        self._intent = 'quit'

    # ─── Model load / build ──────────────────────────────────────────────
    def _load_model(self) -> None:
        groups = utils.parse_pkg_list_groups(self._path)
        self._meta = utils.parse_pkg_list_group_meta(self._path)
        for gname, names in groups.items():
            self._groups[gname] = [[n, True] for n in names]
        # Append the pool.list tier as the reserved POOL_GROUP (flat file →
        # one synthetic group, rendered last).  A real pkg.list section
        # literally named [(pool)] parses to this same key and would be
        # silently overwritten here — detect and warn rather than lose it.
        if POOL_GROUP in self._groups:
            utils.logger.warning(
                f"pkg.list contains a [{POOL_GROUP}] section that collides "
                f"with the reserved pool tier; the pkg.list group is "
                f"superseded by config/pool.list")
        self._groups[POOL_GROUP] = [[n, True] for n in _read_flat(self._poolpath)]
        self._meta[POOL_GROUP] = {
            'description': 'config/pool.list — ships in /cdrom/pool, '
                           'never installed in a chroot'}

    def _rows(self) -> List[_Row]:
        """Flatten the model into display rows (header + entries)."""
        rows: List[_Row] = []
        for gname, entries in self._groups.items():
            rows.append(_Row(_HEADER, gname))
            for name, _sel in entries:
                rows.append(_Row(_PKG, gname, name))
        return rows

    def _entry(self, group: str, name: str) -> Optional[List]:
        for e in self._groups.get(group, []):
            if e[0] == name:
                return e
        return None

    # ─── Metadata ────────────────────────────────────────────────────────
    def _meta_for(self, name: str) -> Dict:
        """Direct metadata (size / dep-count / description) from cache.
        Cheap; computed once and memoised."""
        m = self._pkgmeta.get(name)
        if m is not None:
            return m
        size = 0
        deps = 0
        desc = ''
        pkgs = self._cache.get_packages(name) if self._cache else []
        if pkgs:
            p = pkgs[0]
            try:
                size = int(p.get('Installed-Size', '0') or 0)
            except (ValueError, TypeError):
                size = 0
            deps = len(getattr(p, 'depends', [])) + len(getattr(p, 'pre_depends', []))
            desc = (p.get('Description', '') or '').split('\n')[0].strip()
        m = {'size': size, 'deps': deps, 'desc': desc, 'closure': None}
        self._pkgmeta[name] = m
        return m

    def _approx_closure(self, name: str) -> Tuple[int, int]:
        """First-provider BFS over Depends + Pre-Depends.  Returns
        (total_installed_kb, package_count).  Approximate — picks the
        first cache candidate per name, ignores alternatives/conflicts
        — but cheap and prompt-free, good enough for a footprint glance."""
        seen: set = set()
        stack = [name]
        total = 0
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            pkgs = self._cache.get_packages(n) if self._cache else []
            if not pkgs:
                continue
            p = pkgs[0]
            try:
                total += int(p.get('Installed-Size', '0') or 0)
            except (ValueError, TypeError):
                pass
            for dep in list(getattr(p, 'depends', [])) + list(getattr(p, 'pre_depends', [])):
                dep_name = dep[0] if isinstance(dep, (tuple, list)) else str(dep)
                if dep_name not in seen:
                    stack.append(dep_name)
        return total, len(seen)

    def _ensure_closure(self, name: str) -> None:
        """Kick off a background closure computation for `name` if not
        already cached or in flight.  Re-renders when done."""
        m = self._meta_for(name)
        if m['closure'] is not None:
            return
        with self._closure_lock:
            if name in self._closure_inflight:
                return
            self._closure_inflight.add(name)

        def _work() -> None:
            try:
                total, _count = self._approx_closure(name)
                self._meta_for(name)['closure'] = total
            finally:
                with self._closure_lock:
                    self._closure_inflight.discard(name)
            self._render()   # posts SetTabBuffer — thread-safe

        threading.Thread(target=_work, daemon=True,
                         name=f'closure-{name}').start()

    # ─── Lifecycle ───────────────────────────────────────────────────────
    def activate(self) -> None:
        self._tui.add_tab(self.TAB)
        self._tui.activate_tab(self.TAB)
        self._tui.set_tab_key_handler(self.TAB, self.handle_key)
        self._render()

    def _teardown(self) -> None:
        self._tui.clear_tab_key_handler()
        self._tui.remove_tab(self.TAB)

    def wait_for_done(self) -> str:
        """Block the calling (shell) thread until the operator exits the
        selector; return the exit intent ('apply' / 'discard' / 'quit')."""
        self._done.wait()
        return self._intent

    def _finish(self, intent: str) -> None:
        """Single exit point — tear the tab down and release the shell
        thread with the operator's intent."""
        self._intent = intent
        self._teardown()
        self._done.set()

    # ─── Rendering ───────────────────────────────────────────────────────
    def _render(self) -> None:
        rows = self._rows()
        if not rows:
            self._tui.set_tab_buffer(self.TAB, [('  (no packages)', 0)])
            return

        self._cursor = max(0, min(self._cursor, len(rows) - 1))
        height = max(3, self._tui.viewport_rows() - 2)   # leave room for help line

        # Keep cursor inside [scroll, scroll+height).
        if self._cursor < self._scroll:
            self._scroll = self._cursor
        elif self._cursor >= self._scroll + height:
            self._scroll = self._cursor - height + 1
        self._scroll = max(0, min(self._scroll, max(0, len(rows) - height)))

        rev   = self._tui.attr_reverse()
        hdr_a = self._tui.attr_color(tui.COLOR_HIGHLIGHT)
        info_a = self._tui.attr_color(tui.COLOR_INFO)

        out: List[Tuple[str, int]] = []
        # Title + counts.
        total_sel = sum(1 for es in self._groups.values() for _n, s in es if s)
        total_all = sum(len(es) for es in self._groups.values())
        out.append((f'  Package selector — {total_sel}/{total_all} selected'
                    f'   (pkg.list + pool.list)', info_a))

        visible = rows[self._scroll:self._scroll + height]
        for i, row in enumerate(visible):
            idx = self._scroll + i
            is_cursor = (idx == self._cursor)
            if row.kind == _HEADER:
                desc = self._meta.get(row.group, {}).get('description', '')
                txt = f' [{row.group}]' + (f'  — {desc}' if desc else '')
                out.append((txt[:200], hdr_a if not is_cursor else rev))
            else:
                out.append((self._format_pkg_row(row, is_cursor),
                            rev if is_cursor else 0))
                if is_cursor:
                    self._ensure_closure(row.name)

        # Bottom line: inline input prompt when active, else the help.
        if self._input_mode == 'add':
            group = self._add_group or '?'
            out.append((f'  add to [{group}]: {self._input_buffer}_', rev))
        elif self._input_mode == 'quit':
            out.append(('  Unsaved changes —  y = apply (save + re-parse + '
                        'accept)   n/Esc = discard (revert all)', rev))
        else:
            out.append(('  ↑↓ move  SPACE toggle  a add  d drop  [ ] group  '
                        's save  W save&apply  q quit', info_a))
        self._tui.set_tab_buffer(self.TAB, out)

    def _format_pkg_row(self, row: _Row, is_cursor: bool) -> str:
        entry = self._entry(row.group, row.name)
        sel = entry[1] if entry else False
        mark = '[*]' if sel else '[ ]'
        m = self._meta_for(row.name)
        size_kb = m['size']
        size_s = self._fmt_size(size_kb)
        deps = m['deps']
        clo = m['closure']
        clo_s = self._fmt_size(clo) if clo is not None else '…'
        # Layout (80 cols):
        #   [*] name(<=22)  size(>=6)  (clo, Nd)(<=14)  desc(rest)
        name = row.name[:22].ljust(22)
        meta = f'{size_s:>7} ({clo_s}, {deps}d)'
        desc = m['desc']
        line = f'   {mark} {name} {meta}  {desc}'
        return line[:200]

    @staticmethod
    def _fmt_size(kb: Optional[int]) -> str:
        if kb is None:
            return '…'
        if kb < 1024:
            return f'{kb}K'
        mb = kb / 1024.0
        if mb < 1024:
            return f'{mb:.1f}M'
        return f'{mb / 1024.0:.1f}G'

    # ─── Key handling (runs on the dispatcher thread) ────────────────────
    def handle_key(self, key: str) -> bool:
        """Return True if the key was consumed by the selector.

        The selector owns EVERY keystroke except F-keys (so tab-switch
        still works) and KEY_RESIZE (so the renderer reflows).  This
        keeps stray keys out of the shell's idle command line and means
        the selector never needs the dispatcher prompt — input modes
        ('add', 'quit') are handled inline below."""
        # Always let the dispatcher handle resize + F-key tab switches.
        if key == 'KEY_RESIZE':
            return False
        if key.startswith('KEY_F(') and key.endswith(')'):
            return False

        # ── Inline input mode (add / quit-confirm) ──────────────────────
        if self._input_mode is not None:
            self._handle_input_key(key)
            return True

        rows = self._rows()
        if not rows:
            if key in ('q', 'Q'):
                self._finish('quit')
            return True

        if key == 'KEY_UP':
            self._move_cursor(-1, rows)
        elif key == 'KEY_DOWN':
            self._move_cursor(1, rows)
        elif key == 'KEY_PPAGE':
            self._move_cursor(-(max(1, self._tui.viewport_rows() - 3)), rows)
        elif key == 'KEY_NPAGE':
            self._move_cursor(max(1, self._tui.viewport_rows() - 3), rows)
        elif key == ' ':
            self._toggle_current(rows)
        elif key in ('d', 'D'):
            self._set_current(rows, False)
        elif key == ']':
            self._jump_group(rows, +1)
        elif key == '[':
            self._jump_group(rows, -1)
        elif key in ('a', 'A'):
            self._begin_add(rows)
        elif key in ('s', 'S'):
            self._save()
        elif key in ('W',):
            self._save_and_quit()
        elif key in ('q', 'Q'):
            self._quit()
        # Any other key is swallowed (kept out of the shell cmdline).
        return True

    def _handle_input_key(self, key: str) -> None:
        """Keystroke handling while an inline input line is active."""
        if self._input_mode == 'quit':
            # y/n confirm — single keystroke.
            ch = key.lower()
            if ch == 'y':                 # apply — save + run the transaction
                self._input_mode = None
                self._save()
                self._finish('apply')
            elif ch in ('n', '\x1b'):     # n or Esc → discard (revert all)
                self._input_mode = None
                self._finish('discard')
            # any other key: ignore, keep waiting.
            return

        # 'add' mode — line editor.
        if key in ('\n', '\r'):
            name = self._input_buffer.strip()
            self._input_mode = None
            self._input_buffer = ''
            if name:
                self._commit_add(name)
            else:
                self._render()
            return
        if key == '\x1b':          # Esc → cancel add
            self._input_mode = None
            self._input_buffer = ''
            self._render()
            return
        if key in ('KEY_BACKSPACE', '\x7f', '\x08'):
            self._input_buffer = self._input_buffer[:-1]
            self._render()
            return
        if len(key) == 1 and key.isprintable():
            self._input_buffer += key
            self._render()

    def _move_cursor(self, delta: int, rows: List[_Row]) -> None:
        new = max(0, min(self._cursor + delta, len(rows) - 1))
        # Skip landing exactly on the title/help (they're not in rows);
        # rows only contains headers + pkgs, so any index is valid.
        self._cursor = new
        self._render()

    def _current_pkg_row(self, rows: List[_Row]) -> Optional[_Row]:
        if 0 <= self._cursor < len(rows):
            r = rows[self._cursor]
            if r.kind == _PKG:
                return r
        return None

    def _toggle_current(self, rows: List[_Row]) -> None:
        r = self._current_pkg_row(rows)
        if r is None:
            return
        e = self._entry(r.group, r.name)
        if e is not None:
            e[1] = not e[1]
            self._unsaved = True
            self._render()

    def _set_current(self, rows: List[_Row], value: bool) -> None:
        r = self._current_pkg_row(rows)
        if r is None:
            return
        e = self._entry(r.group, r.name)
        if e is not None and e[1] != value:
            e[1] = value
            self._unsaved = True
            self._render()

    def _jump_group(self, rows: List[_Row], direction: int) -> None:
        gnames = list(self._groups.keys())
        if not gnames:
            return
        cur_group = rows[self._cursor].group if self._cursor < len(rows) else gnames[0]
        try:
            gi = gnames.index(cur_group)
        except ValueError:
            gi = 0
        gi = (gi + direction) % len(gnames)
        target = gnames[gi]
        for idx, r in enumerate(rows):
            if r.kind == _HEADER and r.group == target:
                self._cursor = idx
                break
        self._render()

    def _begin_add(self, rows: List[_Row]) -> None:
        """Enter inline 'add' input mode for the current group."""
        self._add_group = (rows[self._cursor].group if self._cursor < len(rows)
                           else next(iter(self._groups), 'base'))
        self._input_mode = 'add'
        self._input_buffer = ''
        self._render()

    def _commit_add(self, name: str) -> None:
        group = self._add_group or next(iter(self._groups), 'base')
        # Reject names absent from the cache — there's no source to
        # build them, so adding would only fail the build later.
        if self._cache and not self._cache.get_packages(name):
            self._tui.print(f'  select: "{name}" not in cache — not added '
                            '(no source to build it)')
            self._render()
            return
        if self._entry(group, name) is None:
            self._groups.setdefault(group, []).append([name, True])
            self._unsaved = True
        self._render()

    def _save(self) -> None:
        # Route the reserved POOL_GROUP to pool.list; everything else is
        # pkg.list.  Both writers do a minimal, comment-preserving diff.
        _pkg_groups = {g: e for g, e in self._groups.items() if g != POOL_GROUP}
        _pkg_meta = {g: m for g, m in self._meta.items() if g != POOL_GROUP}
        write_pkg_list(self._path, _pkg_groups, _pkg_meta)
        _saved = self._path
        if POOL_GROUP in self._groups:
            _pool_sel = [n for n, s in self._groups[POOL_GROUP] if s]
            # Don't CREATE an empty pool.list: skip the write when nothing is
            # selected AND the file doesn't already exist (a spurious empty
            # file the cache discard-path would orphan).  Still write to empty
            # an existing pool.list when the operator clears all pool packages.
            if _pool_sel or os.path.isfile(self._poolpath):
                write_flat_list(self._poolpath, _pool_sel)
                _saved = f'{self._path} + {self._poolpath}'
        self._unsaved = False
        self._tui.print(f'  select: saved {_saved}')
        self._render()

    def _save_and_quit(self) -> None:
        """Save the lists then hand off to the apply transaction (re-parse +
        preview + accept/cancel, run by cmd_cache_select on the shell thread)."""
        self._save()
        self._finish('apply')

    def _quit(self) -> None:
        if not self._unsaved:
            self._finish('quit')
            return
        # Unsaved edits — inline confirm: apply (parse) or discard (revert).
        self._input_mode = 'quit'
        self._render()


def _read_flat(path: str) -> List[str]:
    """Ordered seed names from a flat list file (one per line, `#` comments
    and blanks ignored).  Missing/unreadable → empty."""
    out: List[str] = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith('#'):
                    out.append(s)
    except OSError:
        return []
    return out


def write_flat_list(path: str, selected: List[str]) -> None:
    """Serialise a flat list (pool.list) back to `path` — MINIMAL DIFF,
    mirroring write_pkg_list's flat branch: re-read the original, keep every
    comment/blank line and every still-selected package in place, drop
    unselected packages, append newly-added selected packages at EOF.  Atomic.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            orig = f.read().splitlines()
    except OSError:
        orig = []
    _sel_set = set(selected)
    _seen: set = set()
    out: List[str] = []
    for line in orig:
        s = line.strip()
        if not s or s.startswith('#'):
            out.append(line)
        elif s in _sel_set:
            out.append(line)
            _seen.add(s)
        # else: unselected package → drop
    for n in selected:
        if n not in _seen:
            out.append(n)
    body = '\n'.join(out).rstrip('\n') + '\n'
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(body)
    os.replace(tmp, path)


def write_pkg_list(path: str, groups: Dict[str, List[List]],
                   meta: Dict[str, Dict[str, str]]) -> None:
    """Serialise the edited model back to `path` — MINIMAL DIFF.

    Re-reads the original file and edits it line-by-line rather than
    regenerating from the model, so ALL comments (the file header,
    inline `#` notes, blank lines) and original ordering are preserved.
    Only two kinds of change are applied:

      - A package line that is now UNSELECTED is dropped.
      - A newly-ADDED selected package is appended at the end of its
        group block (just before the next `[group]` header, or EOF).

    Groups present in the model but absent from the file are appended
    whole (header + `## Description:` + selected names).  Atomic write
    (temp + os.replace).

    Falls back to a from-scratch flat emit only when the file doesn't
    exist yet (first-ever save of a flat model)."""
    import re

    try:
        with open(path, 'r', encoding='utf-8') as f:
            orig = f.read().splitlines()
    except OSError:
        orig = []

    section_re = re.compile(r'^\s*\[([^\]]*)\]\s*$')
    has_sections = any(section_re.match(line) for line in orig)

    # Selected-name set per group (membership test) + ordered list (append).
    sel_set = {g: {n for n, s in entries if s} for g, entries in groups.items()}
    sel_ord = {g: [n for n, s in entries if s] for g, entries in groups.items()}

    out: List[str] = []

    if not has_sections:
        # Flat file (or brand-new).  Preserve comments/blanks; keep
        # selected base pkgs in original order; append new ones.
        seen: set = set()
        base_set = sel_set.get('base', set())
        for line in orig:
            s = line.strip()
            if not s or s.startswith('#'):
                out.append(line)
            elif s in base_set:
                out.append(line)
                seen.add(s)
            # else: unselected package → drop
        for n in sel_ord.get('base', []):
            if n not in seen:
                out.append(n)
    else:
        # INI file.  Walk lines, tracking the current group; drop
        # unselected pkg lines; flush newly-added pkgs at each group
        # boundary (and at EOF).
        cur: Optional[str] = None
        seen_per: Dict[str, set] = {}

        def _flush_new(group: Optional[str]) -> None:
            if group is None:
                return
            already = seen_per.setdefault(group, set())
            for n in sel_ord.get(group, []):
                if n not in already:
                    out.append(n)
                    already.add(n)

        for line in orig:
            m = section_re.match(line)
            if m:
                _flush_new(cur)        # finish the group we're leaving
                cur = m.group(1).strip()
                seen_per.setdefault(cur, set())
                out.append(line)
                continue
            s = line.strip()
            if not s or s.startswith('#'):
                out.append(line)
                continue
            if cur is not None and s in sel_set.get(cur, set()):
                out.append(line)
                seen_per[cur].add(s)
            # else: unselected package → drop
        _flush_new(cur)                # last group at EOF

        # Groups in the model that never appeared in the file → append.
        for g in groups:
            if g not in seen_per:
                out.append(f'[{g}]')
                desc = meta.get(g, {}).get('description')
                if desc:
                    out.append(f'## Description: {desc}')
                for n in sel_ord.get(g, []):
                    out.append(n)

    body = '\n'.join(out).rstrip('\n') + '\n'
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(body)
    os.replace(tmp, path)
