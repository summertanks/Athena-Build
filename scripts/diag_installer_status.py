#!/usr/bin/env python3
"""COMP-02 phase B diagnostic: find malformed stanzas in the installer
chroot's dpkg status file.

main-menu's libdebian-installer parser ("parser_rfc822") segfaults on
stanzas it can't parse — error "Iek! Don't find end of field".  This
script reproduces what that parser checks for, runs it across every
stanza in /var/lib/dpkg/status, and reports the first stanza that would
trip it.

Run from the build host AFTER `chroot build installer` has populated
buildroot/installer/var/lib/dpkg/status.  No deps beyond stdlib.
"""

import os
import re
import sys
from typing import Dict, List, Tuple


# Stock-seed udebs added by COMP-02 phase B (config/installer.list).
# Excludes virtual-package names (fat-modules etc.) — those resolve to
# kernel-ABI-specific real packages whose names we don't know until
# build time.
NEW_SEEDS = frozenset({
    # base
    'anna', 'archdetect', 'cdebconf-udeb', 'di-utils', 'di-utils-reboot',
    'di-utils-shell', 'libdebconfclient0-udeb', 'libdebian-installer4-udeb',
    'lowmemcheck', 'rescue-check', 'env-preseed', 'pciutils-udeb',
    'screen-udeb', 'wget-udeb', 'ca-certificates-udeb', 'kmod-udeb',
    'haveged-udeb', 'wireless-regdb-udeb', 'udev-udeb',
    # cdrom/common
    'localechooser', 'load-cdrom', 'cdrom-checker', 'bogl-bterm-udeb',
    'di-utils-terminfo', 'cdebconf-newt-terminal', 'cdebconf-text-udeb',
    'brltty-udeb', 'initrd-preseed', 'file-preseed', 'nano-udeb',
    'media-retriever', 'save-logs', 'libfribidi0-udeb',
    # cdrom/amd64.cfg (non-modular)
    'console-setup-pc-ekmap', 'console-setup-udeb', 'kbd-udeb',
})


FIELD_HEADER_RE = re.compile(r'^([A-Za-z][A-Za-z0-9\-]*)[ \t]*:[ \t]*(.*)$')


def parse_stanzas(content: str) -> List[Tuple[int, Dict[str, str], List[str]]]:
    """Parse dpkg-status content into (line_start, fields, raw_lines) tuples.

    Mirrors libdebian-installer parser/rfc822.c behaviour:
      - blank line separates stanzas
      - field header: `Name: value` (case-insensitive)
      - continuation: line begins with ' ' or '\\t' — append to previous
        field's value
      - any other line is MALFORMED.
    """
    _stanzas: List[Tuple[int, Dict[str, str], List[str]]] = []
    _cur_fields: Dict[str, str] = {}
    _cur_lines: List[str] = []
    _cur_start = 1
    _last_field: str = ''
    for _lineno, _raw in enumerate(content.split('\n'), 1):
        if _raw == '':
            if _cur_fields:
                _stanzas.append((_cur_start, _cur_fields, _cur_lines))
            _cur_fields = {}
            _cur_lines = []
            _cur_start = _lineno + 1
            _last_field = ''
            continue
        _cur_lines.append(_raw)
        if _raw[0] in (' ', '\t'):
            if _last_field:
                _cur_fields[_last_field] += '\n' + _raw
            else:
                _cur_fields['__MALFORMED__'] = (
                    f'continuation at line {_lineno} with no preceding field'
                )
            continue
        _m = FIELD_HEADER_RE.match(_raw)
        if _m:
            _last_field = _m.group(1)
            _cur_fields[_last_field] = _m.group(2)
        else:
            _cur_fields.setdefault('__MALFORMED__', '')
            _cur_fields['__MALFORMED__'] += (
                f'\nbad line {_lineno}: {_raw!r}'
            )
    if _cur_fields:
        _stanzas.append((_cur_start, _cur_fields, _cur_lines))
    return _stanzas


def audit_stanza(
    fields: Dict[str, str], lineno: int, raw_lines: List[str]
) -> List[str]:
    """Return a list of issues with this stanza that could trip
    libdebian-installer's parser."""
    _issues: List[str] = []
    if '__MALFORMED__' in fields:
        _issues.append(f"PARSE: {fields['__MALFORMED__']}")
    if 'Package' not in fields:
        _issues.append("MISSING: Package field")
    if 'Description' not in fields:
        _issues.append("MISSING: Description field (main-menu may NULL-deref)")
    # Walk raw bytes for control / 8-bit chars in any field
    for _field, _value in fields.items():
        if _field == '__MALFORMED__':
            continue
        for _i, _c in enumerate(_value):
            _o = ord(_c)
            if _o > 127:
                _issues.append(
                    f"NON-ASCII: field {_field!r} char {_c!r} (0x{_o:x}) "
                    f"at pos {_i}"
                )
                break
            if _o < 32 and _c not in '\n\t':
                _issues.append(
                    f"CTRL-BYTE: field {_field!r} char 0x{_o:x} at pos {_i}"
                )
                break
    # Continuation lines with TAB prefix (some parsers stricter on this)
    for _r in raw_lines:
        if _r.startswith('\t'):
            _issues.append(f"TAB-CONT: stanza has tab-prefix continuation line")
            break
    # Unterminated field: a field header (`Name:`) with NO value AND no
    # continuation line following — libdebian-installer trips on this.
    for _f, _v in fields.items():
        if _f == '__MALFORMED__':
            continue
        if _v == '':
            _issues.append(f"EMPTY-VALUE: field {_f!r} has empty value")
    return _issues


def main(path: str) -> int:
    if not os.path.isfile(path):
        print(f"ERROR: {path} not found")
        return 2
    # File may be root-owned; try plain read first, fall back to sudo.
    try:
        with open(path, 'r', errors='replace') as fh:
            _content = fh.read()
    except PermissionError:
        import subprocess
        _r = subprocess.run(['sudo', 'cat', path], capture_output=True, text=True)
        if _r.returncode != 0:
            print(f"ERROR: cannot read {path}: {_r.stderr}")
            return 2
        _content = _r.stdout

    _stanzas = parse_stanzas(_content)
    print(f"Parsed {len(_stanzas)} stanzas from {path}")
    print(f"  ({len(NEW_SEEDS)} package names tracked as 'newly-seeded by phase B')")
    print()

    _any_issue = False
    _new_seen = 0
    for _line_start, _fields, _raw in _stanzas:
        _pkg = _fields.get('Package', '<no-package>')
        _is_new = _pkg in NEW_SEEDS
        if _is_new:
            _new_seen += 1
        _issues = audit_stanza(_fields, _line_start, _raw)
        if _issues:
            _any_issue = True
            _tag = '[NEW]' if _is_new else '     '
            print(f"{_tag} L{_line_start}  Package: {_pkg}")
            for _issue in _issues:
                print(f"        ! {_issue}")
            print(f"        fields: {sorted(_fields.keys())}")
            print()

    print(f"Summary: {_new_seen}/{len(NEW_SEEDS)} new-seed stanzas observed")
    if not _any_issue:
        print("No issues found by static checks.")
        print()
        print("That doesn't fully clear the file — libdebian-installer's")
        print("parser has quirks beyond what we check here.  Try running")
        print("the actual parser:")
        print()
        print("  in-target chroot, run:")
        print("    sh -c 'cat /var/lib/dpkg/status | head -N | "
              "anna-install --dry-run'")
        return 0
    return 1


if __name__ == '__main__':
    _default = (
        '/home/harkirat/Athena/Athena-Build/buildroot/installer/'
        'var/lib/dpkg/status'
    )
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else _default))
