"""/ — identity-leakage scanner.

Walks shipped / operator-facing fork content + (when called from the
ISO stage) the staged ISO root, greps each text file for residue
tokens that betray our Debian heritage in places the operator would
see, then filters against `audit/identity-allowlist`.  Findings
report path:line + the matching token.

Per project memory `project_filter_debian_specific_installer_hooks`
and `project_strip_debian_identity_telemetry_from_shipped_distro` —
Athena ships as Athena.  This scanner replaces today's hardcoded
`installer_chroot._strip_debian_residue_hooks` _targets list with
a durable mechanism that surfaces NEW residue (added by upstream
rebase, fork edits, new udebs) without requiring a code patch.

Allow-list format (`audit/identity-allowlist`):

  # comments and blank lines ignored
  <path-glob>\\t<token-name>\\t<reason>

  <path-glob>:   fnmatch-style glob, matched against the scanner's
                 relative path (relative to scan root).
  <token-name>:  key from IDENTITY_TOKENS, or '*' for any token.
  <reason>:      free-text rationale (printed when an allowlist
                 entry suppresses a finding).

Example:
  fork/source/*/debian/copyright\\t*\\tlegal attribution (must keep)
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import shlex
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger('athena.cache')


# Token name → compiled regex.  Word boundaries on the ones whose
# substring is real English (discover → discoverable, popcon → … well
# popcon is rarely in normal prose, but be safe).
IDENTITY_TOKENS = {
    'Debian':              re.compile(r'\bDebian\b'),
    'debian.org':          re.compile(r'debian\.org'),
    'discover':            re.compile(r'\bdiscover\b'),
    'installation-report': re.compile(r'\binstallation-report\b'),
    'reportbug':           re.compile(r'\breportbug\b'),
    'popularity-contest':  re.compile(r'\bpopularity-contest\b'),
    'popcon':              re.compile(r'\bpopcon\b'),
    'apt-listchanges':     re.compile(r'\bapt-listchanges\b'),
    'bug-buddy':           re.compile(r'\bbug-buddy\b'),
}


# File / subtree patterns the scanner skips entirely.  Two reasons to
# skip: (1) binary content where grep is meaningless, (2) trees we
# don't own and don't ship (.git, upstream patches, .pc series, build
# artifacts).
_SKIP_GLOBS = (
    '*/.git/*', '.git/*',
    '*/.pc/*',  '.pc/*',
    '*/debian/patches/*',
    '*.po',  '*.pot',          # translations echo source strings
    '*.gz',  '*.xz',  '*.bz2', '*.zip', '*.tar',
    '*.deb', '*.udeb', '*.dsc',
    '*.png', '*.jpg', '*.jpeg', '*.gif', '*.ico', '*.svg',
    '*.pdf',
)


def load_allowlist(path: str) -> List[Tuple[str, str, str]]:
    """Parse `path` into [(path_glob, token_name, reason)] tuples.

    Missing file → empty list (caller treats audit as "all findings
    are violations").  Malformed lines (wrong column count) are
    silently skipped — strict-fail on this would punish operator
    edits mid-flight."""
    rules: List[Tuple[str, str, str]] = []
    if not os.path.isfile(path):
        return rules
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            for raw in fh:
                ln = raw.rstrip('\n')
                if not ln or ln.lstrip().startswith('#'):
                    continue
                parts = ln.split('\t')
                if len(parts) != 3:
                    continue
                rules.append((parts[0].strip(), parts[1].strip(),
                              parts[2].strip()))
    except OSError as e:
        logger.warning(f"identity_scan: cannot read allowlist {path}: {e}")
    return rules


def _allowed(rel_path: str, token: str,
             allowlist: List[Tuple[str, str, str]]) -> Optional[str]:
    """Return the reason if (rel_path, token) is allowlisted; else None."""
    for glob, allowed_token, reason in allowlist:
        if not fnmatch.fnmatch(rel_path, glob):
            continue
        if allowed_token != '*' and allowed_token != token:
            continue
        return reason
    return None


def _should_skip(rel_path: str) -> bool:
    """True for binary-ish / upstream-owned / translation files."""
    return any(fnmatch.fnmatch(rel_path, pat) for pat in _SKIP_GLOBS)


def _is_binary_file(abs_path: str, probe_bytes: int = 4096) -> bool:
    """Best-effort binary-vs-text classifier: read up to `probe_bytes`
    bytes and treat the file as binary if it contains a NUL byte.

    Catches files the SKIP_GLOBS list doesn't anticipate by extension:
    `boot/vmlinuz` (Linux kernel image, no extension), `*.efi`
    bootloader stubs, signed kernel modules, etc.  Without this guard
    the audit greps the kernel's embedded build-id strings (e.g.
    `debian-kernel@lists.debian.org`, `Debian Secure Boot CA`) and
    treats them as identity leakage — caught 2026-05-31 staged ISO
    audit on `boot/vmlinuz`.

    Read errors → treat as binary (caller's `try` already discards,
    same fail-open behaviour)."""
    try:
        with open(abs_path, 'rb') as fh:
            chunk = fh.read(probe_bytes)
    except OSError:
        return True
    return b'\x00' in chunk


def audit_identity(root: str,
                   allowlist_path: Optional[str] = None
                   ) -> List[Dict[str, object]]:
    """Walk `root`, grep each text file for IDENTITY_TOKENS.

    Returns a list of findings — each is a dict with keys
    `path` (relative to root), `line_no` (1-based), `line` (stripped),
    `token` (name from IDENTITY_TOKENS).  Allowlisted hits are NOT
    in the result.  Caller decides whether to fail/warn/just log.

    Empty root or missing root → empty list.  Files that error on
    read (permission, decode) → skipped silently."""
    findings: List[Dict[str, object]] = []
    if not os.path.isdir(root):
        return findings

    allowlist = load_allowlist(allowlist_path) if allowlist_path else []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune common skip-dirs early so we don't recurse into them.
        dirnames[:] = [d for d in dirnames if d not in ('.git', '.pc')]
        for fn in filenames:
            abs_path = os.path.join(dirpath, fn)
            rel_path = os.path.relpath(abs_path, root)
            if _should_skip(rel_path):
                continue
            if _is_binary_file(abs_path):
                continue
            try:
                with open(abs_path, 'r', encoding='utf-8',
                          errors='replace') as fh:
                    for i, line in enumerate(fh, 1):
                        for token, pat in IDENTITY_TOKENS.items():
                            if not pat.search(line):
                                continue
                            if _allowed(rel_path, token, allowlist):
                                continue
                            findings.append({
                                'path':    rel_path,
                                'line_no': i,
                                'line':    line.rstrip()[:200],
                                'token':   token,
                            })
            except OSError:
                # errors='replace' on the open above means decoding never
                # raises UnicodeDecodeError; only open/read OSError reaches here.
                continue
    return findings


# ─────────────────────────────────────────────────────────────────────────────
# S2 — Chroot apt-install audit (replaces installer_chroot._strip_debian_
# residue_hooks' hardcoded _targets list).
#
# pre-pkgsel.d and finish-install.d hooks shipped by upstream udebs
# call `apt-install X` against the target system's apt during d-i's
# pre-packageselection phase.  When X is not in our pool, the call
# fails noisily — sometimes fatally, sometimes (with `|| true`) just
# generating error log spam.
#
# This audit walks every hook script, extracts the apt-install pkg
# arguments, and cross-references against the pool the installer
# ships.  Outcomes:
#   • all pkgs in pool                          → no action
#   • unpooled pkgs, hook listed in allowlist   → STRIP (rm -f)
#   • unpooled pkgs, hook NOT allowlisted, soft (|| true)  → WARN
#   • unpooled pkgs, hook NOT allowlisted, hard apt-install → FAIL
# ─────────────────────────────────────────────────────────────────────────────


_HOOK_SUBTREES = (
    'usr/lib/pre-pkgsel.d',
    'usr/lib/finish-install.d',
    'lib/finish-install.d',
)


def _parse_apt_install_line(line: str) -> Optional[Tuple[List[str], bool]]:
    """Extract pkg names from one line if it invokes apt-install.

    Returns (pkg_names, soft_failure) or None.
    `soft_failure=True` when the tail is `|| true` / `|| :` (intentional
    best-effort apt-install).

    Handles common shapes:
        apt-install foo
        apt-install foo bar baz
        apt-install foo || true
        apt-install foo || :
        apt-install --no-install-recommends foo
        $logoutput apt-install foo                  (apt-setup generator wrapper)
    Skips `$VAR` arguments — variable expansion is not auditable here."""
    stripped = line.lstrip()
    if stripped.startswith('#'):
        return None
    if 'apt-install' not in stripped:
        return None
    # Strip trailing inline-comment.  Conservative: only treat ' #' as a
    # comment marker so URLs containing # in the body don't get clipped.
    head = stripped.split(' #', 1)[0]
    # The 'apt-install' gate above tested the whole line; if the token
    # survived only inside the stripped ' #' comment (e.g.
    # `echo done # call apt-install foo later`), the actual command has no
    # invocation to audit.  Re-check the comment-stripped command — without
    # this, the split below returns a 1-element list and [1] raises
    # IndexError, aborting the whole hook audit.
    if 'apt-install' not in head:
        return None
    # Take whatever follows the `apt-install` token.  Multiple occurrences
    # of the literal in one line (rare) would be malformed; take the first.
    after = head.split('apt-install', 1)[1]

    soft_failure = False
    # `|| true` / `|| :` makes the apt-install best-effort.  Recognise the
    # tail then drop everything from `||` onward so we don't try to parse
    # the suffix as another apt-install argument.
    if '||' in after:
        head_part, tail_part = after.split('||', 1)
        tail_part = tail_part.strip()
        if tail_part.startswith(('true', ':')):
            soft_failure = True
        after = head_part
    # Command terminators only (&& and ;) — DON'T pre-split on
    # > / < / | here.  Those collide with file-descriptor redirects
    # like `brltty 1>&2` (caught 2026-05-31: a `>` pre-split made
    # the parser report ['brltty', '1'] from upstream 07brltty).
    # The per-token walk below handles redirects + pipes cleanly via
    # shlex tokenisation.
    for sep in ('&&', ';'):
        if sep in after:
            after = after.split(sep, 1)[0]

    try:
        tokens = shlex.split(after.strip())
    except ValueError:
        return None

    # Shell redirect operators that may appear AFTER the pkg list.
    # Numbered variants (`1>`, `2>&1`, `1>&2`, `&>file`) match the regex
    # below.  Once we hit one of these, everything past it is shell
    # plumbing, not an apt-install argument.
    _redirect_token_literals = {'|', '>', '<', '>>', '<<', '&>', '|&'}
    _redirect_token_re = re.compile(r'^\d*[<>]&?\d*$')

    pkgs: List[str] = []
    for _t in tokens:
        if not _t:
            continue
        if _t in _redirect_token_literals or _redirect_token_re.match(_t):
            break
        if _t.startswith('-'):
            continue
        if _t.startswith('$'):
            continue
        pkgs.append(_t)
    return (pkgs, soft_failure) if pkgs else None


def _normalise_hook_path(abs_path: str, chroot_root: str) -> str:
    """abs_path → chroot-relative posix-style path (forward slashes)."""
    rel = os.path.relpath(abs_path, chroot_root)
    return rel.replace(os.sep, '/')


def _load_strip_allowlist(path: str) -> List[Tuple[str, str]]:
    """Parse `installer/strip-hooks-allowlist` into [(path_glob, reason)].

    Simpler than identity-allowlist (no token column): hook either
    matches a glob and is auto-stripped, or it doesn't and any
    unpooled apt-install causes a failure / warning.
    """
    rules: List[Tuple[str, str]] = []
    if not path or not os.path.isfile(path):
        return rules
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            for raw in fh:
                ln = raw.rstrip('\n')
                if not ln or ln.lstrip().startswith('#'):
                    continue
                parts = ln.split('\t')
                if len(parts) < 2:
                    continue
                rules.append((parts[0].strip(), parts[1].strip()))
    except OSError as e:
        logger.warning(
            f"identity_scan: cannot read strip-hooks allowlist {path}: {e}"
        )
    return rules


def _hook_is_allowlisted(rel_path: str,
                         rules: List[Tuple[str, str]]) -> Optional[str]:
    for glob, reason in rules:
        if fnmatch.fnmatch(rel_path, glob):
            return reason
    return None


def audit_chroot_hooks(chroot_root: str,
                       pool_pkg_names: Set[str],
                       allowlist_path: Optional[str] = None
                       ) -> List[Dict[str, object]]:
    """Walk hook subtrees under chroot_root and audit every apt-install
    against pool_pkg_names.

    Returns findings list — each dict has:
      path          : chroot-relative hook path
      line_no       : 1-based line of the apt-install call
      missing_pkgs  : list of named pkgs absent from pool_pkg_names
      soft_failure  : True if line was `apt-install … || true|:` form
      action        : 'strip' | 'warn' | 'fail'
      reason        : (only for 'strip') allowlist rationale

    Empty pool_pkg_names short-circuits to no findings — callers
    without a built pool yet (early test fixtures) get a no-op."""
    findings: List[Dict[str, object]] = []
    if not pool_pkg_names:
        return findings
    if not os.path.isdir(chroot_root):
        return findings
    allowlist = _load_strip_allowlist(allowlist_path) if allowlist_path else []

    for subtree in _HOOK_SUBTREES:
        root = os.path.join(chroot_root, subtree)
        if not os.path.isdir(root):
            continue
        for fn in sorted(os.listdir(root)):
            abs_path = os.path.join(root, fn)
            if not os.path.isfile(abs_path):
                continue
            rel_path = _normalise_hook_path(abs_path, chroot_root)
            try:
                with open(abs_path, 'r', encoding='utf-8',
                          errors='replace') as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                parsed = _parse_apt_install_line(line)
                if parsed is None:
                    continue
                pkgs, soft = parsed
                missing = [p for p in pkgs if p not in pool_pkg_names]
                if not missing:
                    continue
                allow_reason = _hook_is_allowlisted(rel_path, allowlist)
                if allow_reason is not None:
                    findings.append({
                        'path': rel_path, 'line_no': i,
                        'missing_pkgs': missing,
                        'soft_failure': soft,
                        'action': 'strip',
                        'reason': allow_reason,
                    })
                else:
                    findings.append({
                        'path': rel_path, 'line_no': i,
                        'missing_pkgs': missing,
                        'soft_failure': soft,
                        'action': 'warn' if soft else 'fail',
                    })
    return findings
