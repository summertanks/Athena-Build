"""COMP-06 — interactive package-set selector TUI.

Lets the operator toggle packages in `config/pkg.list` (and add new
ones from the apt cache) without hand-editing the file.  Runs in a
dedicated `select` tab; the dispatcher routes keystrokes to this
controller's `handle_key` while that tab is active (F-keys still
switch tabs, so the operator can always leave).

Key bindings (while the `select` tab is active):
    ↑ / ↓        move the row cursor
    PgUp / PgDn  page the cursor
    Space        toggle the package on the cursor row
    a            add a package (input prompt; tab-completes cache names)
    d            deselect the package on the cursor row
    [ / ]        previous / next group
    s            save → write config/pkg.list
    q            quit the selector (prompts if there are unsaved edits)

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

        # Editable model: group → ordered list of [name, selected].
        # Selection defaults True for every name already in pkg.list.
        self._groups: Dict[str, List[List]] = {}
        self._meta:   Dict[str, Dict[str, str]] = {}
        self._load_model()

        # View state.
        self._cursor = 0      # index into the flattened row list
        self._scroll = 0      # top visible row index
        self._unsaved = False

        # Lazy per-package metadata cache:
        #   name -> {'size': int_kb, 'deps': int, 'desc': str,
        #            'closure': Optional[int_kb]}   (None = not yet computed)
        self._pkgmeta: Dict[str, Dict] = {}
        self._closure_inflight: set = set()
        self._closure_lock = threading.Lock()

    # ─── Model load / build ──────────────────────────────────────────────
    def _load_model(self) -> None:
        groups = utils.parse_pkg_list_groups(self._path)
        self._meta = utils.parse_pkg_list_group_meta(self._path)
        for gname, names in groups.items():
            self._groups[gname] = [[n, True] for n in names]

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
                    f'   (config/pkg.list)', info_a))

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

        # Help line at the bottom.
        out.append(('  ↑↓ move  SPACE toggle  a add  d drop  [ ] group  '
                    's save  q quit', info_a))
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
        """Return True if the key was consumed by the selector."""
        rows = self._rows()
        if not rows:
            if key in ('q', 'Q'):
                self._teardown()
                return True
            return False

        if key == 'KEY_UP':
            self._move_cursor(-1, rows)
            return True
        if key == 'KEY_DOWN':
            self._move_cursor(1, rows)
            return True
        if key == 'KEY_PPAGE':
            self._move_cursor(-(max(1, self._tui.viewport_rows() - 3)), rows)
            return True
        if key == 'KEY_NPAGE':
            self._move_cursor(max(1, self._tui.viewport_rows() - 3), rows)
            return True
        if key == ' ':
            self._toggle_current(rows)
            return True
        if key in ('d', 'D'):
            self._set_current(rows, False)
            return True
        if key == ']':
            self._jump_group(rows, +1)
            return True
        if key == '[':
            self._jump_group(rows, -1)
            return True
        if key in ('a', 'A'):
            self._add_package(rows)
            return True
        if key in ('s', 'S'):
            self._save()
            return True
        if key in ('q', 'Q'):
            self._quit()
            return True
        # F-keys / other → fall through so tab-switch still works.
        return False

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

    def _add_package(self, rows: List[_Row]) -> None:
        """Prompt for a package name; add it to the current group.

        Runs the blocking prompt on a worker thread so the dispatcher
        loop (which called us) isn't re-entered — request_prompt would
        deadlock if called on the dispatcher thread."""
        cur_group = (rows[self._cursor].group if self._cursor < len(rows)
                     else next(iter(self._groups), 'base'))

        def _work() -> None:
            name = self._tui.prompt(f'Add package to [{cur_group}]: ').strip()
            if not name:
                return
            if self._cache and not self._cache.get_packages(name):
                self._tui.print(f'  select: "{name}" not in cache — added anyway')
            if self._entry(cur_group, name) is None:
                self._groups[cur_group].append([name, True])
                self._unsaved = True
            self._render()

        threading.Thread(target=_work, daemon=True, name='select-add').start()

    def _save(self) -> None:
        write_pkg_list(self._path, self._groups, self._meta)
        self._unsaved = False
        self._tui.print(f'  select: saved {self._path}')
        self._render()

    def _quit(self) -> None:
        if not self._unsaved:
            self._teardown()
            return

        def _work() -> None:
            resp = self._tui.prompt('Unsaved changes — save before quit? [y/n]: ')
            if resp.strip().lower() in ('y', 'yes'):
                self._save()
            self._teardown()

        threading.Thread(target=_work, daemon=True, name='select-quit').start()


def write_pkg_list(path: str, groups: Dict[str, List[List]],
                   meta: Dict[str, Dict[str, str]]) -> None:
    """Serialise the edited model back to `path`.

    Preserves group order, `## Description:` comments, and within-group
    ordering.  Drops unselected entries.  Atomic (temp + rename).

    A single-group `base`-only model with no description is written
    flat (no `[base]` header) to keep round-trips clean for legacy
    flat pkg.list files."""
    lines: List[str] = []
    gnames = list(groups.keys())
    flat = (gnames == ['base'] and not meta.get('base', {}).get('description'))

    if flat:
        for name, sel in groups['base']:
            if sel:
                lines.append(name)
    else:
        for gname in gnames:
            lines.append(f'[{gname}]')
            desc = meta.get(gname, {}).get('description')
            if desc:
                lines.append(f'## Description: {desc}')
            for name, sel in groups[gname]:
                if sel:
                    lines.append(name)
            lines.append('')   # blank line between groups
        while lines and lines[-1] == '':
            lines.pop()

    body = '\n'.join(lines) + '\n'
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(body)
    os.replace(tmp, path)
