"""Patterns that signal an installer regression — the harness fails
on any match.

Pattern sources:
  - docs/plans/comp-02-robust-build.md § Phase F (the originals)
  - Past install-debug sessions (failures we've fixed and don't want
    to silently regress)
  - Stock d-i error vocabulary (`succeeded but requested to be left
    unconfigured` etc.) that always indicates a real problem

To extend: add a tuple to `KNOWN_BAD`.  Each entry is
    (regex, severity, what-it-means).
- regex      str — pattern.re-compatible; matched against each log line
                   (re.search semantics, not re.fullmatch)
- severity   str — 'fatal' / 'warn'; only 'fatal' patterns fail the
                   smoke run (warns surface in the report but don't
                   abort).  Use 'warn' for patterns that are noise on
                   some configs but worth tracking.
- meaning    str — human-readable explanation, shown in the report.

Anti-pattern to avoid: don't add patterns that ALWAYS appear in the
log (e.g. version banners).  They'd fire on every clean run.
Inspect log/athena_*.log from a known-good install to sanity-check.
"""

import re
from typing import Iterable, List, Tuple


# Each entry is (regex_string, severity, meaning).
# Compile lazily so the file is cheap to import.
KNOWN_BAD: 'List[Tuple[str, str, str]]' = [
    # — d-i step machinery —
    (
        r'succeeded but requested to be left unconfigured',
        'fatal',
        'd-i step completed but flagged itself for re-run/inspection — '
        'usually means a hard failure further down the chain',
    ),
    (
        r'failed with error code',
        'fatal',
        'd-i step exited non-zero; main-menu noticed and logged this',
    ),
    (
        r'unable to find required udeb',
        'fatal',
        'cdebconf or main-menu wanted a udeb that\'s not in the ISO\'s '
        'installer.list closure — our installer.list resolution missed it',
    ),
    # — main-menu spurious warnings —
    (
        r'WARNING \*\*:.*main-menu',
        'fatal',
        'main-menu emitted a non-fallback warning — would normally be '
        'a real cdebconf template/registration issue',
    ),
    # — apt-cdrom-setup chain failures (regression on Phase C work) —
    (
        r'40cdrom output did not verify',
        'fatal',
        'apt-setup\'s cdrom helper rejected our signed Release — '
        'pubkey or InRelease signature broken (Phase C regression)',
    ),
    # — known-noise patterns surfaced as 'warn' for tracking only —
    (
        r'depmod: WARNING: could not open modules\.builtin\.modinfo',
        'warn',
        'kernel-image-di doesn\'t ship modinfo.builtin; modules still '
        'load — cosmetic, present on every run today',
    ),
    (
        r"can't open '/etc/default-release'",
        'warn',
        'stock d-i quirk; we ship the file via athena-installer-data '
        'so this should be rare post-FORK-01 — surface if it returns',
    ),
]


def scan_log(
    log_path: str,
    extra_patterns: 'Iterable[Tuple[str, str, str]] | None' = None,
) -> 'List[dict]':
    """Scan the serial log for known-bad patterns.

    Args:
        log_path:        Path to the captured serial log.
        extra_patterns:  Additional (regex, severity, meaning) tuples
                         to scan with — useful when a specific install
                         pass needs ad-hoc gating without editing the
                         canonical list.

    Returns a list of finding dicts:
        {
            'severity': 'fatal' | 'warn',
            'pattern':  '<the regex source>',
            'meaning':  '<the human-readable explanation>',
            'line_no':  int,
            'line':     '<the matching log line, stripped>',
        }
    Findings are returned in log-order.  Caller decides what to do —
    canonically, any 'fatal' finding fails the smoke run; 'warn'
    findings surface in the report but don't abort.
    """
    _patterns = list(KNOWN_BAD) + list(extra_patterns or ())
    _compiled = [(re.compile(_re), _sev, _why) for _re, _sev, _why in _patterns]

    _findings: 'List[dict]' = []
    try:
        with open(log_path, 'r', errors='replace') as fh:
            for _ln_no, _line in enumerate(fh, start=1):
                _stripped = _line.rstrip('\n')
                for _re, _sev, _why in _compiled:
                    if _re.search(_stripped):
                        _findings.append({
                            'severity': _sev,
                            'pattern':  _re.pattern,
                            'meaning':  _why,
                            'line_no':  _ln_no,
                            'line':     _stripped,
                        })
                        # One finding per (line, pattern) — but a
                        # single line CAN match multiple patterns and
                        # we want to surface all of them.
    except OSError:
        return []
    return _findings


def has_fatal(findings: 'List[dict]') -> bool:
    """True iff any finding has severity 'fatal'."""
    return any(_f['severity'] == 'fatal' for _f in findings)
