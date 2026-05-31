"""CONF-10 / AUDIT-01 — identity-leakage scanner.

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
from typing import Dict, List, Optional, Tuple

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
            except (OSError, UnicodeDecodeError):
                continue
    return findings
