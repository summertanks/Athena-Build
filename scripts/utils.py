import dataclasses
import hashlib
import logging
import os
import pathlib
import re
import configparser
import argparse

import requests
import tui
from tui import Prompt, Spinner, ProgressBar
from typing import List, Optional

logger = logging.getLogger('athena')


# Version constraint operators accepted by apt_pkg.check_dep — the seven
# Debian-defined comparison sigils.  Anything else falls back to '>='
# at call sites (defensive default for malformed input).  Centralised
# here so the cache + dependency-tree modules import from one source of
# truth rather than each carrying its own identical copy.
VALID_CONSTRAINTS = frozenset({'=', '>=', '<=', '>>', '<<', '>', '<'})


# Defaults previously hardcoded as `fallback=` literals inline in
# BuildConfig.__init__ (HK-01e consolidation).  Promoted here so the
# next-time-we-need-it edit happens in one place and is greppable.
#
# _DEFAULT_CONTAINER_RELEASE — Debian release the build container
# pins to when [Build] CONTAINER_RELEASE is absent.  Today's only
# tested target is bookworm; trixie would work but is unverified.
# A non-Debian distro under COMP-11 would override this.
_DEFAULT_CONTAINER_RELEASE = 'bookworm'

# _DEFAULT_SECURITY_KEYRING — InRelease verification keyring.  The
# host-installed debian-archive-keyring is the canonical default on
# any Debian-derived build host.  A fork shipping its own keyring
# would override via [Security] Keyring.
_DEFAULT_SECURITY_KEYRING = '/usr/share/keyrings/debian-archive-keyring.gpg'


# repo/ is segregated by package role.  Layout:
#   repo/main/     installable binaries (base + pkg.list + live + installer +
#                  pool, plus udebs).  ALSO includes -dev side artifacts —
#                  see classify_repo_subdir for the rationale.
#   repo/doc/      `-doc` side artifacts (mostly absent under build profile
#                  `nodoc`; kept for the rare source that produces docs
#                  regardless)
#   repo/dbgsym/   `-dbgsym` side artifacts (mostly absent under build
#                  option `noautodbgsym`)
#   repo/tests/    `-test` / `-tests` side artifacts
#
# Each subdir has its own Packages index.  `repo audit` scopes repo/main.
_REPO_SUBDIRS = ('main', 'doc', 'dbgsym', 'tests')


def classify_repo_subdir(filename: str) -> str:
    """Return the subdir under repo/ where `filename` belongs.

    Classification is by binary PACKAGE NAME suffix (everything before
    the first underscore in the filename):

        libfoo_1.0-2_amd64.deb         → 'main'
        libfoo-dev_1.0-2_amd64.deb     → 'main'   (see note)
        libfoo-doc_1.0-2_all.deb       → 'doc'
        libfoo-dbgsym_1.0-2_amd64.deb  → 'dbgsym'
        libfoo-tests_1.0-2_amd64.deb   → 'tests'  (plural)
        libfoo-test_1.0-2_amd64.deb    → 'tests'  (singular)
        foo-udeb_1.0-2_amd64.udeb      → 'main'   (udebs are installable)

    `-dev` packages live in `main` even though they're "side artifacts"
    by traditional Debian taxonomy.  Reason: install-corpus packages
    hard-depend on them at runtime — `build-essential` Depends
    `libc6-dev`, `gcc-12` Depends `libgcc-12-dev`, `g++-12` Depends
    `libstdc++-12-dev`.  Without -dev in main, those hard deps go
    unresolved at install time.  Moving -dev to its own subdir
    (tried 2026-05-20 then reverted) just shifted the problem.

    Unknown extensions / malformed filenames default to 'main'.
    """
    _base = filename.rsplit('.', 1)[0]
    _pkg = _base.split('_', 1)[0]
    if _pkg.endswith('-doc'):
        return 'doc'
    if _pkg.endswith('-dbgsym'):
        return 'dbgsym'
    if _pkg.endswith('-tests') or _pkg.endswith('-test'):
        return 'tests'
    return 'main'


def strip_build_version(file: str) -> str:
    """Remove a Debian binNMU rebuild suffix `+bN` from a `.deb` filename.

    Input must be `name_version_arch.ext` shape (e.g. `pkg_1.0-2+b1_amd64.deb`).
    Strips `+bN` only when it sits at the *end* of the version field, leaving
    legitimate point-release / security-update suffixes (`+deb12u1`) intact:

        foo_1.0-2+b1_amd64.deb       → foo_1.0-2_amd64.deb
        foo_1.0-2+deb12u1_amd64.deb  → foo_1.0-2+deb12u1_amd64.deb (unchanged)
        foo_1.0-2+deb12u1+b3_amd64.deb → foo_1.0-2+deb12u1_amd64.deb
        foo_1.0+b1-2_amd64.deb       → foo_1.0+b1-2_amd64.deb (binNMU embedded
                                       in version, not at end → not a rebuild
                                       suffix; left alone)

    Used to map an APT-resolved `Filename` (which carries the buildd's binNMU
    rebuild suffix) onto the file `dpkg-buildpackage` actually produced from
    source (which uses the source version, never `+bN`).

    Raises:
        ValueError: filename is not in `name_version_arch.ext` shape.
    """
    _name, _ext = os.path.splitext(file)
    _parts = _name.split('_')
    if len(_parts) != 3:
        raise ValueError(f"Incorrectly formatted package filename: {file!r}")
    _pkg_name, _version, _arch = _parts
    _version = re.sub(r"\+b\d+$", "", _version)
    return f"{_pkg_name}_{_version}_{_arch}{_ext}"


# NMU/binNMU/backport suffix matcher — strips at END of a version string.
# Matches in order (greedy from end), can stack:
#   +debNuN       — Debian security/point-release update (sorts AFTER base)
#   ~debNuN       — security update that sorts BEFORE base (rarer form;
#                   used when upstream wants the security backport to
#                   take a lower version slot — e.g. dnsmasq's
#                   `2.90-4~deb12u2`)
#   ~bpoN+N       — backport
#   +rpiN, +rptN  — Raspberry Pi rebuilds
#   +bN           — modern binNMU
#   (?<=\d)b\d*   — legacy binNMU form (`-2b1`, `-2b`); requires a digit
#                   immediately before `b` so we don't accidentally munch
#                   into upstream-version letters like `2alpha`
# Trailing `+` after the group means: strip MULTIPLE stacked layers
# (`1.0-2+deb12u3+b1` → strip both → `1.0-2`).
# Trailing `~?` consumes the pre-release tilde marker that constraint
# versions commonly tack on AFTER the NMU suffix (e.g. libflatpak0's
# `Depends: bubblewrap (>= 0.8.0-2+deb12u1~)` — the `~` means "any
# build >= the pre-release of 0.8.0-2+deb12u1").  Without `~?`, the
# regex required `$` immediately after the NMU group; the `~` blocked
# the match and the constraint version survived strip unchanged,
# producing spurious unresolved-dep findings against our stripped repo
# (audit 2026-05-21 — libflatpak0 → bubblewrap).  Note: applies ONLY
# when the `~` directly follows an NMU group; bare-version `~rc1`
# pre-release markers (no NMU) are unaffected.
_NMU_SUFFIX_RE = re.compile(
    r'(?:\+deb\d+u\d+|~deb\d+u\d+|~bpo\d+\+\d+|\+rpi\d+|\+rpt\d+|'
    r'\+b\d+|(?<=\d)b\d*)+~?$'
)

# Relation fields whose version constraints should also have NMU stripped.
# Includes Conflicts/Breaks/Replaces — operator's intent per the rework
# (we don't relax the relation SHAPE, just normalise the version
# representation; if libfoo Conflicts: libbar (<< 1.0-2+deb12u3) the
# strip yields Conflicts: libbar (<< 1.0-2) — broader-but-still-honest).
_NMU_STRIP_FIELDS = (
    'Depends', 'Pre-Depends', 'Recommends', 'Suggests',
    'Enhances', 'Provides', 'Conflicts', 'Breaks', 'Replaces',
)


def normalize_repo_filename(filename: str) -> str:
    """Map an upstream Packages-index Filename to its repo/main on-disk
    form.  Strips both layers our build pipeline removes:

      1. +bN — binNMU rebuild suffix (Debian buildd-only metadata)
      2. +debNuN / ~bpoN+N / +rpiN / +rptN / legacy -Nb — upstream NMU
         layers, removed by BuildContainer's post-`dpkg-buildpackage`
         strip pass (utils.strip_nmu_from_deb).

    Examples:
        normalize_repo_filename('libc6_2.36-9+deb12u13_amd64.deb')
            → 'libc6_2.36-9_amd64.deb'
        normalize_repo_filename('libfoo_1.0-2+b1_amd64.deb')
            → 'libfoo_1.0-2_amd64.deb'
        normalize_repo_filename('libfoo_1.0-2+deb12u3+b1_amd64.deb')
            → 'libfoo_1.0-2_amd64.deb'
        normalize_repo_filename('libfoo_1.0-2_amd64.deb')
            → 'libfoo_1.0-2_amd64.deb'  (no-op)

    Use anywhere code resolves an upstream Filename onto a disk path
    under repo/main: dep-tree filename prediction, chroot install
    resolution, installer udeb lookup, dep-drift scan.

    Malformed filenames (not in `name_version_arch.ext` shape) are
    returned unchanged — best-effort, doesn't raise.
    """
    if not (filename.endswith('.deb') or filename.endswith('.udeb')):
        return filename
    _name, _ext = os.path.splitext(filename)
    _parts = _name.split('_')
    if len(_parts) != 3:
        return filename
    _pkg, _ver, _arch = _parts
    _new_ver = strip_nmu_suffix(_ver)   # regex includes +bN, +debNuN, etc.
    return f'{_pkg}_{_new_ver}_{_arch}{_ext}'


def strip_nmu_suffix(version: str) -> str:
    """Strip trailing NMU/binNMU/backport suffix layers from a Debian
    version string.  Returns the pristine source version.

        strip_nmu_suffix('1.0-2')                  → '1.0-2'
        strip_nmu_suffix('1.0-2+b1')               → '1.0-2'
        strip_nmu_suffix('1.0-2+deb12u3')          → '1.0-2'
        strip_nmu_suffix('1.0-2+deb12u3+b1')       → '1.0-2'
        strip_nmu_suffix('1.0-2~bpo12+1')          → '1.0-2'
        strip_nmu_suffix('0.15.5-2b')              → '0.15.5-2'   (legacy form)
        strip_nmu_suffix('0.15.5-2b1')             → '0.15.5-2'   (legacy form)
        strip_nmu_suffix('2:1.0-2+b1')             → '2:1.0-2'    (epoch kept)
        strip_nmu_suffix('1.0-2alpha')             → '1.0-2alpha' (no strip;
                                                                  not an NMU)
    """
    return _NMU_SUFFIX_RE.sub('', version)


def strip_nmu_from_control_text(content: str) -> 'tuple[str, int]':
    """Return (new_text, strip_count) — strips NMU suffix from the
    Version field AND every version constraint in dep-related fields
    of a DEBIAN/control text.

    Walks fields line-by-line (handles multi-line continuation).
    Idempotent: re-running on already-stripped text counts zero strips.
    """
    _total = 0
    _content = content

    # Strip from the Version: field.
    def _sub_version(_m):
        nonlocal _total
        _old = _m.group(1)
        _new = strip_nmu_suffix(_old)
        if _new != _old:
            _total += 1
        return f'Version: {_new}'

    _content = re.sub(
        r'^Version: (\S+)\s*$',
        _sub_version, _content, count=1, flags=re.MULTILINE,
    )

    # Per-relation version-constraint stripper: matches `(OP VERSION)`
    # within a dep-field's value.  Operator is preserved verbatim.
    _constraint_re = re.compile(
        r'\(\s*(<=|>=|<<|>>|=)\s*([^)]+?)\s*\)'
    )

    def _sub_constraint(_m):
        nonlocal _total
        _op, _ver = _m.group(1), _m.group(2)
        _new_ver = strip_nmu_suffix(_ver)
        if _new_ver != _ver:
            _total += 1
        return f'({_op} {_new_ver})'

    # Walk lines; only rewrite inside a relation field block.  Uses
    # the deb822 wrap-on-continuation convention: a field starts at
    # column 0 with `Name:`; continuation lines start with whitespace.
    _new_lines: 'list[str]' = []
    _in_target = False
    for _line in _content.splitlines(keepends=True):
        if _line and _line[0] not in (' ', '\t'):
            _m = re.match(r'^([A-Za-z][A-Za-z0-9-]*):', _line)
            if _m:
                _in_target = _m.group(1) in _NMU_STRIP_FIELDS
            else:
                _in_target = False
        # (continuation lines inherit the surrounding _in_target state)
        if _in_target:
            _new_lines.append(_constraint_re.sub(_sub_constraint, _line))
        else:
            _new_lines.append(_line)

    _content = ''.join(_new_lines)

    # Pair-rewrite pass: detect the upstream "same-upstream-sibling"
    # idiom (`X (>> V), X (<< V-.)`) and collapse it to `X (= our_version)`.
    # See docs/strip-nmu-sibling-constraint-idiom.md for the full
    # rationale + impact analysis.  Briefly: the >> half fails after
    # strip-NMU because Policy §5.6.12 makes our `V-0` equal to bare `V`;
    # the maintainer's intent was "any debrev of same upstream", which
    # in our atomic-source-build pipeline reduces to "the exact sibling
    # binary version we produce".
    _our_version = _extract_version(_content)
    if _our_version:
        _content, _pairs = _rewrite_sibling_idiom_in_text(_content, _our_version)
        _total += _pairs

    return _content, _total


def _extract_version(content: str) -> 'Optional[str]':
    """Read the Version: field value from a control-file text."""
    _m = re.search(r'^Version: (\S+)\s*$', content, re.MULTILINE)
    return _m.group(1) if _m else None


def _rewrite_sibling_idiom_in_text(content: str,
                                    our_version: str) -> 'tuple[str, int]':
    """Rewrite the upstream `X (>> V), X (<< V-.)` AND-pair idiom in
    Depends / Pre-Depends to a single `X (= our_version)` entry.

    Strictly bound to the four pair conditions — anything that doesn't
    match the full signature is left untouched:

      1. Field is Depends or Pre-Depends (Recommends/Suggests/etc skipped:
         the idiom is a hard-link convention).
      2. Both entries are at AND-level (each is its own single-entry
         OR-group — Debian Policy forbids alternatives in the idiom).
      3. Same target name X on both halves.
      4. `>>` half's version is V; `<<` half's version is exactly `V-.`
         (the canonical "any debrev of V" upper bound).

    Returns (new_content, pairs_rewritten_count).  Idempotent: re-running
    on already-rewritten content returns count=0.
    """
    from debian.deb822 import PkgRelation
    _pairs_total = 0
    _new_lines: 'list[str]' = []
    _lines = content.splitlines(keepends=True)
    _i = 0
    while _i < len(_lines):
        _line = _lines[_i]
        # Field header detection: column 0 + Name: at the start.
        _m = re.match(r'^(Depends|Pre-Depends):\s*(.*)$', _line)
        if not _m or (_line and _line[0] in (' ', '\t')):
            _new_lines.append(_line)
            _i += 1
            continue
        _field = _m.group(1)
        _value_parts = [_m.group(2).rstrip('\n')]
        # Collect continuation lines (lead-with-whitespace).
        _j = _i + 1
        while _j < len(_lines) and _lines[_j] and _lines[_j][0] in (' ', '\t'):
            _value_parts.append(_lines[_j].strip())
            _j += 1
        _full_value = ' '.join(p for p in _value_parts if p)
        _new_value, _pairs = _collapse_sibling_pair(_full_value, our_version)
        if _pairs > 0:
            # Determine trailing newline shape from the original first line.
            _eol = '\n' if _line.endswith('\n') else ''
            _new_lines.append(f'{_field}: {_new_value}{_eol}')
            _pairs_total += _pairs
        else:
            # No rewrite needed — preserve original line shape (including
            # operator-authored continuation wrapping).
            _new_lines.append(_line)
            for _k in range(_i + 1, _j):
                _new_lines.append(_lines[_k])
        _i = _j
    if _pairs_total == 0:
        return content, 0
    return ''.join(_new_lines), _pairs_total


def _collapse_sibling_pair(value: str,
                            our_version: str) -> 'tuple[str, int]':
    """Parse a Depends-field value, collapse every `X (>> V), X (<< V-.)`
    pair into `X (= our_version)`.  See _rewrite_sibling_idiom_in_text
    docstring for the exact match criteria."""
    from debian.deb822 import PkgRelation
    try:
        _relations = PkgRelation.parse_relations(value)
    except Exception:
        return value, 0
    _skip: 'set[int]' = set()
    _count = 0
    _new_relations: 'list[list]' = []
    for _i, _or_group in enumerate(_relations):
        if _i in _skip:
            continue
        # Match guard 1: AND-level entries are single-element OR-groups.
        # Alternatives like `(X >> V) | (Y >> V)` are not the idiom.
        if len(_or_group) != 1:
            _new_relations.append(_or_group)
            continue
        _entry_i = _or_group[0]
        _ver_i = _entry_i.get('version')
        if not _ver_i or _ver_i[0] != '>>':
            _new_relations.append(_or_group)
            continue
        _name = _entry_i['name']
        _v = _ver_i[1]
        # Look forward for the matching `<<` half.  Architecture
        # restrictions, arch-qualifiers, and build-profile restrictions
        # must also match to confirm it's the same logical constraint.
        _matched_j: 'Optional[int]' = None
        for _j in range(_i + 1, len(_relations)):
            if _j in _skip:
                continue
            if len(_relations[_j]) != 1:
                continue
            _entry_j = _relations[_j][0]
            _ver_j = _entry_j.get('version')
            if not _ver_j or _ver_j[0] != '<<':
                continue
            if _entry_j['name'] != _name:
                continue
            if _ver_j[1] != f'{_v}-.':
                continue
            if (_entry_j.get('arch') != _entry_i.get('arch')
                    or _entry_j.get('archqual') != _entry_i.get('archqual')
                    or _entry_j.get('restrictions') != _entry_i.get('restrictions')):
                continue
            _matched_j = _j
            break
        if _matched_j is None:
            _new_relations.append(_or_group)
            continue
        # Pair confirmed.  Collapse to a single `(X = our_version)` entry
        # in place of the `>>` half; drop the `<<` half.
        _collapsed = {
            'name':         _name,
            'version':      ('=', our_version),
            'arch':         _entry_i.get('arch'),
            'archqual':     _entry_i.get('archqual'),
            'restrictions': _entry_i.get('restrictions'),
        }
        _new_relations.append([_collapsed])
        _skip.add(_matched_j)
        _count += 1
    if _count == 0:
        return value, 0
    return PkgRelation.str(_new_relations), _count


def strip_nmu_from_deb(deb_path: str) -> dict:
    """Strip NMU suffix layers from a .deb/.udeb in place.

    Single dpkg-deb -R / -b cycle.  Updates:
      - The filename (if its version segment had an NMU suffix)
      - DEBIAN/control Version field
      - Every version constraint in dep fields (incl. Conflicts/Breaks)

    Returns dict:
      {'status': 'rewritten' | 'unchanged' | 'malformed' | 'skipped',
       'new_path': str,
       'strips_count': int}

    Idempotent: re-running on already-stripped .deb returns 'unchanged'
    without I/O.  Cheap pre-check via DebFile (no data archive extract)
    rules out the no-op case.
    """
    import subprocess
    import tempfile
    from debian.debfile import DebFile

    _result = {
        'status': 'unchanged', 'new_path': deb_path, 'strips_count': 0,
    }
    _base = os.path.basename(deb_path)
    if not (_base.endswith('.deb') or _base.endswith('.udeb')):
        _result['status'] = 'skipped'
        return _result
    _name, _ext = os.path.splitext(_base)
    _parts = _name.split('_')
    if len(_parts) != 3:
        _result['status'] = 'malformed'
        return _result
    _pkg, _old_filename_ver, _arch = _parts

    try:
        with DebFile(deb_path) as _deb:
            _ctrl_bytes = _deb.control.get_content('control')
    except Exception:
        _result['status'] = 'malformed'
        return _result
    if _ctrl_bytes is None:
        _result['status'] = 'malformed'
        return _result
    _ctrl_text = _ctrl_bytes.decode('utf-8', errors='replace')

    _new_ctrl_text, _strips = strip_nmu_from_control_text(_ctrl_text)
    _new_filename_ver = strip_nmu_suffix(_old_filename_ver)
    _filename_changed = _new_filename_ver != _old_filename_ver

    if not _filename_changed and _strips == 0:
        return _result      # 'unchanged'

    if _filename_changed:
        _new_base = f'{_pkg}_{_new_filename_ver}_{_arch}{_ext}'
    else:
        _new_base = _base
    _new_path = os.path.join(os.path.dirname(deb_path), _new_base)

    with tempfile.TemporaryDirectory(prefix='strip-nmu-') as _work:
        subprocess.run(
            ['dpkg-deb', '-R', deb_path, _work],
            check=True, capture_output=True,
        )
        _ctrl_disk = os.path.join(_work, 'DEBIAN', 'control')
        with open(_ctrl_disk, 'w') as _fh:
            _fh.write(_new_ctrl_text)
        subprocess.run(
            ['dpkg-deb', '--root-owner-group', '-b', _work, _new_path],
            check=True, capture_output=True,
        )

    if _new_path != deb_path:
        os.remove(deb_path)
    _result.update({
        'status': 'rewritten',
        'new_path': _new_path,
        'strips_count': _strips + (1 if _filename_changed else 0),
    })
    return _result




def version_no_epoch(version) -> str:
    """Return a Debian Version's string form with the epoch stripped.

    Debian versions have the grammar `[epoch:]upstream[-revision]`.
    Filenames (`.dsc`, `.deb`, `.udeb`) and convention-based directory
    layouts strip the epoch entirely — `git_2.39.5-0+deb12u3.dsc`
    matches the source whose `Version: 1:2.39.5-0+deb12u3`.  This
    helper produces the no-epoch form for any code that maps a
    Source/Package Version onto a filename or filesystem path.

    Accepts either a `debian.debian_support.Version` instance or a
    plain string — both are coerced via `str()` first, then split
    on the first `:` and the remainder taken.  No `:` → returned
    unchanged.

    Surfaced 2026-05-15 when patches under
    `patch/source/git/2.39.5-0+deb12u3/` and
    `patch/source/llvm-toolchain-15/15.0.6-4/` were silently ignored
    by `_refresh_patches` and `BuildContainer.build()`'s fresh-disk
    read because both call sites used `str(src.version)` (epoch
    intact) to compose the directory path.  Patches at sibling
    paths for sources without an epoch (libevdev, librsvg, bluez,
    lilv, libde265, firefox-esr) discovered fine.
    """
    _s = str(version)
    _colon = _s.find(':')
    if _colon < 0:
        return _s
    return _s[_colon + 1:]


def _strip_quotes(s: str) -> str:
    """Remove a single matching pair of surrounding quotes from a config value.

    configparser does not unquote values: `KEY = "value"` reads back as the
    literal 6-char string `"value"`. Apply to string values where the operator
    may have wrapped them in quotes by INI convention from other tools.
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


@dataclasses.dataclass(frozen=True)
class Mirror:
    """A single archive source (e.g. bookworm main, bookworm-security main).

    Immutable (frozen dataclass) so a Mirror passed around the pipeline
    can't be silently mutated by a downstream consumer.  Construction is
    the only place input shape is validated; afterwards every URL is
    composed from the same set of normalised fields via @property
    accessors.

    Composition:
        url   = <baseurl>/<baseid>          # e.g. http://deb.debian.org/debian
        suite = <release><suffix>           # e.g. bookworm, bookworm-security
    The release / suffix split exists so rebasing to a different release
    only requires changing one [Base].RELEASE field, not every
    [Mirror.*] section.

    Validation is intentionally narrow (non-empty + URL scheme +
    suffix shape) — anything that could legitimately vary across forks
    is accepted.  Operators get a clear ValueError at construct time
    rather than a confusing 404 deep in the download path.
    """

    id:        str
    baseurl:   str
    baseid:    str
    release:   str
    suffix:    str
    component: str
    arch:      str

    def __post_init__(self) -> None:
        # Normalise: strip trailing slash on baseurl, leading/trailing on
        # baseid.  Frozen dataclass needs object.__setattr__ for in-place
        # mutation — the freeze only prevents external writes.
        object.__setattr__(self, 'baseurl', self.baseurl.rstrip('/'))
        object.__setattr__(self, 'baseid',  self.baseid.strip('/'))
        # Normalise None suffix to empty string for back-compat with
        # callers that pass `suffix=None`.
        if self.suffix is None:
            object.__setattr__(self, 'suffix', '')

        # Validation — fail early with a useful message.
        # `component` is intentionally NOT in this list: the empty string
        # is a valid value (signalling flat-layout mirror, used by
        # fork mirror — see scripts/fork_mirror.py).  All other fields
        # are required non-empty strings.
        for _field in ('id', 'baseurl', 'baseid', 'release', 'arch'):
            _val = getattr(self, _field)
            if not _val or not isinstance(_val, str) or not _val.strip():
                raise ValueError(
                    f"Mirror.{_field}: non-empty string required, got {_val!r}"
                )
        if not isinstance(self.component, str):
            raise ValueError(
                "Mirror.component: string required (may be empty for "
                f"flat layout), got {self.component!r}"
            )
        if '://' not in self.baseurl:
            raise ValueError(
                f"Mirror.baseurl must include a scheme (http://, https://, "
                f"or file://), got {self.baseurl!r}"
            )
        if self.suffix and not self.suffix.startswith('-'):
            raise ValueError(
                f"Mirror.suffix must be empty or start with '-' "
                f"(e.g. '-updates', '-security'), got {self.suffix!r}"
            )

    @property
    def suite(self) -> str:
        return f'{self.release}{self.suffix}'

    @property
    def url(self) -> str:
        return f'{self.baseurl}/{self.baseid}'

    @property
    def is_flat(self) -> bool:
        """Flat-layout mirror — Release + index files sit at the URL root,
        no `dists/<suite>/<component>/...` hierarchy.  Triggered by
        component='' (the fork mirror sets this; see scripts/fork_mirror.py).
        apt's equivalent declaration is `deb file://... ./`.
        """
        return self.component == ''

    @property
    def dist_url(self) -> str:
        # Flat layout: 'file:///working_dir/fork/'
        # Standard layout: 'http://deb.debian.org/debian/dists/bookworm/'
        if self.is_flat:
            return f'{self.url}/'
        return f'{self.url}/dists/{self.suite}/'

    @property
    def packages_path(self) -> str:
        # Flat layout: 'Packages' (sits next to Release at mirror root)
        # Standard layout: 'main/binary-amd64/Packages'
        if self.is_flat:
            return 'Packages'
        return f'{self.component}/binary-{self.arch}/Packages'

    @property
    def sources_path(self) -> str:
        # Flat layout: 'Sources'.  Standard: 'main/source/Sources'.
        if self.is_flat:
            return 'Sources'
        return f'{self.component}/source/Sources'

    @property
    def udeb_packages_path(self) -> str:
        # d-i (udeb) Packages index.  Only main typically
        # publishes this; updates/security mirrors won't have it.  Cache
        # treats this path as OPTIONAL — missing-from-Release is fine.
        # Flat layout: 'Packages-udeb' (separate file from regular Packages
        # to keep deb/udeb routing simple; cache reads each into its own
        # hashtable).
        if self.is_flat:
            return 'Packages-udeb'
        return f'{self.component}/debian-installer/binary-{self.arch}/Packages'

    def with_snapshot(self, ts, baseurl: str = 'https://snapshot.debian.org/archive') -> 'Mirror':
        """Return a copy of this Mirror rewritten to the snapshot service.

        The default targets snapshot.debian.org's layout
            <baseurl>/<baseid>/<TS>/dists/<suite>/...
        so we only need to rewrite baseurl + baseid; everything else
        (suite, component, packages_path, sources_path) is unchanged.
        Passing ts=None returns self — call sites can use this method
        unconditionally.

        `baseurl` defaults to the Debian snapshot service for back-compat
        with callers that don't have a BuildConfig.  Live call sites in
        `resolve_snapshot_timestamp` thread `config.snapshot_baseurl`
        through so a fork's snapshot mirror can be configured via
        `[Snapshot] BaseUrl` without touching this method.
        """
        if ts is None:
            return self
        # file:// mirrors are local trees (e.g. fork mirror); snapshot
        # rewriting points at a remote service and would break the URL.
        if self.baseurl.startswith('file://'):
            return self
        return dataclasses.replace(
            self,
            baseurl = baseurl,
            baseid  = f'{self.baseid}/{ts}',
        )

    def __repr__(self) -> str:
        return f"Mirror({self.id}: {self.url} {self.suite} {self.component})"


# Module-level memo so resolve_snapshot_timestamp() doesn't repeat the
# network query within one process.  Keyed by (state_file_path, config_ts)
# so different BuildConfig instances under different cache dirs don't
# collide in tests.
_SNAPSHOT_TS_CACHE: dict = {}

# Format check: Debian snapshot timestamps are YYYYMMDDTHHMMSSZ (15 chars).
_SNAPSHOT_TS_RE = re.compile(r'^\d{8}T\d{6}Z$')

# Module-level memo for GPG verifier instances.  Keyed by (keyring_path,
# work_dir) so re-using the same keyring across multiple verify_inrelease()
# calls in one Cache build avoids re-importing the (large) Debian keyring
# per mirror.
_GPG_VERIFIER_CACHE: dict = {}


def verify_inrelease(signed_path: str, keyring_path: str, work_dir: str) -> tuple:
    """Verify the inline GPG signature on a Debian InRelease file.

    InRelease is an inline-signed (cleartext-signed) document that lists
    SHA256 sums for the index files (Packages, Sources) it covers.  Once
    this signature is verified against a trusted keyring, those SHA256
    sums extend trust to the index files via the existing per-file hash
    check in Cache.__get_files — same chain apt uses.

    Args:
        signed_path:  Path to the downloaded InRelease file.
        keyring_path: Path to a keyring file (legacy .gpg / .kbx keybox /
                      OpenPGP cert dump — gpg --import accepts all three).
        work_dir:     Existing gnupg homedir, mode 0700, writable.  Created
                      by build-system.sh / BuildConfig; this function does
                      not create or chmod it (single-source-of-truth for
                      project directory layout).

    Returns:
        (ok, detail) — ok is True when the signature is mathematically
        valid and the signing key is in the keyring; detail is a short
        human-readable status string suitable for both console and log.
    """
    import gnupg

    if not os.path.isfile(signed_path):
        return False, f"signed file missing: {signed_path}"
    if not os.path.isfile(keyring_path):
        return False, f"keyring missing: {keyring_path}"
    if not os.access(keyring_path, os.R_OK):
        return False, f"keyring unreadable: {keyring_path}"
    if not os.path.isdir(work_dir):
        return False, f"gnupg work_dir missing: {work_dir}"

    # Per-build cache: one GPG instance per (keyring, work_dir) pair.
    # Re-importing the full Debian keyring (1k+ keys) for every mirror
    # would be wasteful — a single import_keys is plenty.
    cache_key = (os.path.realpath(keyring_path), os.path.realpath(work_dir))
    gpg = _GPG_VERIFIER_CACHE.get(cache_key)
    if gpg is None:
        try:
            gpg = gnupg.GPG(gnupghome=work_dir)  # type: ignore[attr-defined]
        except (OSError, ValueError) as e:
            return False, f"gnupg init failed: {e}"

        try:
            with open(keyring_path, 'rb') as fh:
                import_result = gpg.import_keys(fh.read())
        except OSError as e:
            return False, f"keyring read failed: {e}"

        # import_keys returns an ImportResult; .count is the number of
        # keys processed.  Zero means the file parsed as 0 keys —
        # malformed or empty file — and verification cannot succeed.
        if not getattr(import_result, 'count', 0):
            return False, (
                f"no keys imported from {keyring_path} "
                f"(stderr: {getattr(import_result, 'stderr', '')[:120]})"
            )

        _GPG_VERIFIER_CACHE[cache_key] = gpg

    try:
        with open(signed_path, 'rb') as fh:
            v = gpg.verify_file(fh)
    except OSError as e:
        return False, f"signed file read failed: {e}"

    if not v.valid:
        # python-gnupg surfaces the gpg --status-fd reason in v.status
        # when available; fall back to the human-readable v.stderr line.
        _why = getattr(v, 'status', None) or (
            (v.stderr or '').splitlines()[-1] if getattr(v, 'stderr', '') else ''
        ) or 'signature invalid'
        return False, str(_why)[:200]

    _ident = getattr(v, 'username', '') or getattr(v, 'fingerprint', '') or 'unknown'
    return True, f"signed by {_ident}"


def check_dep3_header(patch_path: str) -> list:
    """Inspect the leading prose of a patch file for required DEP-3 headers.

    DEP-3 (https://dep-team.pages.debian.net/deps/dep3/) defines the
    pseudo-headers Debian expects above the `--- a/...` diff line:

      Description: (or Subject:)  what the patch does and why
      Origin: / Author:           where it came from / who wrote it
      Forwarded:                  whether the upstream knows about it

    Returns the list of required field names that are missing.  An
    empty list means the header passes; a non-empty list is what the
    caller surfaces in a warning.  We scan only the first ~40 lines —
    DEP-3 headers must precede the diff hunks anyway.

    Soft check: returns missing fields, does not raise.  The caller
    (cmd_parse_dependency) logs them as warnings; the patch is still
    applied at build time.  Keep DEP-3 a guideline rather than a gate
    so an operator's ad-hoc one-off patch is not blocked, but the
    project's own patch tree is held to the convention.
    """
    REQUIRED = ('Description', 'Origin')   # Author satisfies Origin
    found: set = set()
    try:
        with open(patch_path, 'r', errors='replace') as fh:
            for _i, _line in enumerate(fh):
                if _i >= 40 or _line.startswith('---'):
                    break       # past the header, into the diff
                _s = _line.strip()
                if _s.startswith('Description:') or _s.startswith('Subject:'):
                    found.add('Description')
                elif _s.startswith('Origin:') or _s.startswith('Author:'):
                    found.add('Origin')
    except OSError:
        # Caller will warn separately on read failure; pretend complete.
        return []
    return [_f for _f in REQUIRED if _f not in found]


def compute_tree_hash(root: str,
                      skip_dirs: tuple = ('.git', '__pycache__')) -> str:
    """SHA256 over the content of every regular file under `root`.

    Walks recursively; sorts paths for determinism; hashes
    (relative_path + NUL + content + NUL) per file in sorted order.
    Mirrors patch_set_hash's encoding so the two functions are
    interchangeable in shape — only the scope differs.

    Used by fork_mirror's invalidation pass to detect any change to
    a fork/source/<pkg>/ tree.  Same hash → tree unchanged → reuse
    cached artifacts.  Different hash → wipe and rebuild.

    Skips directories named in `skip_dirs` (default: VCS metadata
    and Python bytecode caches — neither carries semantic content
    for source builds).  Unreadable files are skipped silently
    (their absence shifts the hash, which is the conservative
    behaviour — see patch_set_hash).

    Missing root → digest of empty input ("").
    """
    _h = hashlib.sha256()
    if not os.path.isdir(root):
        return _h.hexdigest()

    _files: list = []
    for _dirpath, _dirnames, _filenames in os.walk(root):
        _dirnames[:] = [_d for _d in _dirnames if _d not in skip_dirs]
        for _fn in _filenames:
            _abs = os.path.join(_dirpath, _fn)
            _rel = os.path.relpath(_abs, root)
            _files.append(_rel)
    _files.sort()

    for _rel in _files:
        try:
            with open(os.path.join(root, _rel), 'rb') as fh:
                _content = fh.read()
        except OSError:
            continue
        _h.update(_rel.encode('utf-8'))
        _h.update(b'\0')
        _h.update(_content)
        _h.update(b'\0')
    return _h.hexdigest()


def patch_set_hash(patch_dir: str, patch_files: list) -> str:
    """SHA256 over (filename + NUL + content + NUL) tuples in the given order.

    Used by `_refresh_patches` (build.py) to confirm whether a patch file
    whose mtime is newer than the corresponding .result actually changed
    in *content*, or merely had its mtime bumped by a header / comment
    edit.  Same hash → no rebuild needed (just touch the .result); hash
    differs → real patch change, invalidate the .result.

    Also written next to .result on every successful build (in
    BuildContainer.build) so future refreshes have a stable baseline.

    Returns hex digest.  Empty patch_files → digest of "".
    Missing/unreadable patch files are skipped silently (mirrors
    _refresh_patches's defensive os.path.exists guard); the resulting
    hash naturally differs from a baseline taken when the file was
    readable, which is the conservative behaviour.
    """
    _h = hashlib.sha256()
    for _pf in patch_files:
        try:
            with open(os.path.join(patch_dir, _pf), 'rb') as fh:
                _content = fh.read()
        except OSError:
            continue
        _h.update(_pf.encode('utf-8'))
        _h.update(b'\0')
        _h.update(_content)
        _h.update(b'\0')
    return _h.hexdigest()


def _query_snapshot_latest(api_url: str, archive_keys: 'List[str]') -> str:
    """Fetch the latest snapshot timestamp covering every archive in
    `archive_keys` (typically `['debian', 'debian-security']`).

    Returns min(latest_per_key) so the chosen TS is valid for every
    archive tree (the service resolves a missing exact TS to the nearest
    snapshot ≤ TS via 302, but we want the symmetric guarantee that
    every archive has a snapshot at or before the timestamp).

    Endpoint:  GET <api_url>
    Response:  {"result": {"<key>": [...sorted ts list...], ...}}
    The list is sorted lexicographically which matches chronological order
    for the YYYYMMDDTHHMMSSZ format, so `[-1]` is the latest.

    Both `api_url` and `archive_keys` are sourced from BuildConfig
    ([Snapshot] TimestampApi / ArchiveKeys) so a fork running its own
    snapshot mirror can configure them without code changes.
    """
    try:
        resp = requests.get(api_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"Failed to query {api_url}: {e}") from e

    try:
        result = data['result']
        _latest_per_key = {_k: result[_k][-1] for _k in archive_keys}
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(
            f"Unexpected response shape from {api_url}: {e}; "
            f"top-level keys={list(data.keys())}, "
            f"requested archive_keys={archive_keys}"
        ) from e

    for _label, _ts in _latest_per_key.items():
        if not _SNAPSHOT_TS_RE.match(_ts):
            raise RuntimeError(
                f"snapshot service returned malformed timestamp for {_label}: {_ts!r}"
            )

    # Lexical min == chronological min for YYYYMMDDTHHMMSSZ
    chosen = min(_latest_per_key.values())
    _per_key_str = ', '.join(f"{_k}={_v}" for _k, _v in _latest_per_key.items())
    tui.console.print(f"snapshot latest: {_per_key_str}, picking {chosen}")
    return chosen


def _validate_snapshot_timestamp(
        ts: str, mirrors: 'List[Mirror]',
        snapshot_baseurl: str = 'https://snapshot.debian.org/archive',
) -> bool:
    """HEAD-validate that every mirror has an InRelease available at the
    given snapshot timestamp.  Catches typos and timestamps that predate a
    given suite (e.g. picking 20180101 when bookworm didn't exist yet).

    The snapshot service responds 302 → /file/<sha> for any timestamp it
    can serve (nearest-≤ resolves arbitrary timestamps), so we follow the
    redirect and check the final 200.  A 404 at the redirect target means
    the file doesn't exist for that timestamp and the validation fails.

    `snapshot_baseurl` defaults to the Debian service for back-compat;
    live call sites pass `config.snapshot_baseurl`.
    """
    for m in mirrors:
        snap = m.with_snapshot(ts, baseurl=snapshot_baseurl)
        url  = snap.dist_url + 'InRelease'
        try:
            resp = requests.head(url, timeout=15, allow_redirects=True)
        except Exception as e:
            logger.error(f"snapshot validate: HEAD {url} failed: {e}")
            return False
        if resp.status_code != 200:
            logger.error(
                f"snapshot validate: {url} returned HTTP {resp.status_code} — "
                f"timestamp {ts} does not cover suite {snap.suite} on archive {m.baseid}"
            )
            return False
        tui.console.print(f"snapshot validate: OK {url}")
    return True


def format_snapshot_timestamp(ts: str) -> str:
    """Render a Debian-snapshot YYYYMMDDTHHMMSSZ string as a human-readable
    UTC datetime, e.g. '20260506T120451Z' → '2026-05-06 12:04:51 UTC'.

    Falls back to returning the input unchanged if it does not match the
    expected snapshot format — caller can still display *something*.
    """
    if not _SNAPSHOT_TS_RE.match(ts):
        return ts
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]} UTC"


def resolve_snapshot_timestamp(config: 'BuildConfig') -> Optional[str]:
    """Resolve the effective snapshot timestamp for this build.

    Returns None when snapshot pinning is disabled — call sites can pass
    the result straight to Mirror.with_snapshot() unconditionally.

    Resolution rules:
      - snapshot_enabled is False           → None
      - snapshot_timestamp_config = 'latest':
          * if cache/snapshot.timestamp exists and looks valid, use it
            (reproducible across runs; delete the file to advance)
          * otherwise call _query_snapshot_latest() and persist
      - snapshot_timestamp_config is explicit:
          * format-check, validate via HEAD, return as-is (do NOT persist —
            the explicit config is already the source of truth)

    Memoised per (state_file, config_ts) so this is safe to call from
    multiple sites in one run without re-resolving.
    """
    if not config.snapshot_enabled:
        return None

    state_file = os.path.join(config.dir_cache, 'snapshot.timestamp')
    cfg_ts     = config.snapshot_timestamp_config
    cache_key  = (state_file, cfg_ts)
    if cache_key in _SNAPSHOT_TS_CACHE:
        return _SNAPSHOT_TS_CACHE[cache_key]

    if cfg_ts == 'latest':
        # Prefer persisted value for reproducibility
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r') as fh:
                    persisted = fh.read().strip()
                if _SNAPSHOT_TS_RE.match(persisted):
                    tui.console.print(f"Snapshot pin: using persisted {persisted} from {state_file}")
                    _SNAPSHOT_TS_CACHE[cache_key] = persisted
                    return persisted
                logger.warning(
                    f"Snapshot state file {state_file} contains invalid timestamp "
                    f"{persisted!r}; re-resolving"
                )
            except OSError as e:
                logger.warning(f"Cannot read {state_file}: {e}; re-resolving")

        # Cold path: ask the snapshot service
        ts = _query_snapshot_latest(
            config.snapshot_timestamp_api,
            config.snapshot_archive_keys,
        )
        try:
            with open(state_file, 'w') as fh:
                fh.write(ts + '\n')
            tui.console.print(f"Snapshot pin: resolved 'latest' → {ts}, persisted to {state_file}")
        except OSError as e:
            logger.warning(
                f"Cannot persist snapshot timestamp to {state_file}: {e}; "
                f"build will re-resolve next run"
            )
        _SNAPSHOT_TS_CACHE[cache_key] = ts
        return ts

    # Explicit timestamp from config
    if not _SNAPSHOT_TS_RE.match(cfg_ts):
        raise ValueError(
            f"Snapshot.Timestamp = {cfg_ts!r} is not a valid Debian snapshot "
            f"timestamp (expected YYYYMMDDTHHMMSSZ, e.g. 20260506T120451Z, or 'latest')"
        )
    if not _validate_snapshot_timestamp(
            cfg_ts, config.mirrors,
            snapshot_baseurl=config.snapshot_baseurl):
        raise ValueError(
            f"Snapshot.Timestamp = {cfg_ts!r} does not cover all configured "
            "mirrors on the snapshot service (see prior log lines)"
        )
    tui.console.print(f"Snapshot pin: explicit {cfg_ts} validated")
    _SNAPSHOT_TS_CACHE[cache_key] = cfg_ts
    return cfg_ts


class BuildConfig:

    arch: str
    mirrors: 'List[Mirror]'
    baseid: str
    release: str
    baseversion: str
    snapshot_enabled: bool
    snapshot_timestamp_config: str
    # Externalised snapshot endpoints — defaults target Debian's
    # snapshot.debian.org service.  Operators running a fork's own
    # snapshot mirror override via [Snapshot] BaseUrl / TimestampApi /
    # ArchiveKeys.
    snapshot_baseurl: str
    snapshot_timestamp_api: str
    snapshot_archive_keys: list[str]
    build_distribution: str
    build_base_id: str
    build_codename: str
    build_version: str
    container_release: str
    docker_server: str

    skip_build_test: list[str]
    tunnel_packages: list[str]
    build_profiles: frozenset
    build_options: frozenset
    max_parallel_builds: int

    security_keyring: str
    security_disabled: bool

    error_str: str
    config_path: str

    dir_working: str
    dir_pkglist: str
    dir_download: str
    dir_log: str
    dir_cache: str
    dir_temp: str
    dir_source: str
    dir_repo: str
    # The dir_repo_* attrs point at the *.deb / *.udeb dirs in the
    # unified apt-repo layout (CONF-01 Stage D, 2026-05-22).  Each
    # binary-<arch>/ subdir is also the location of its corresponding
    # Packages index (co-located per Q1).  See
    # docs/plans/conf-01-repo-layout-migration.md.
    dir_repo_main: str           # dists/<codename>/main/binary-<arch>/
    dir_repo_main_udeb: str      # dists/<codename>/main/debian-installer/binary-<arch>/
    dir_repo_main_source: str    # dists/<codename>/main/source/  (.dsc, .tar.*)
    dir_repo_doc: str            # dists/<codename>/doc/binary-<arch>/
    dir_repo_dbgsym: str         # dists/<codename>-debug/main/binary-<arch>/   (Q2 — debug suite)
    dir_repo_tests: str          # dists/<codename>/tests/binary-<arch>/
    dir_config: str
    dir_patch: str
    dir_gnupg: str
    dir_image: str
    dir_chroot: str

    dir_patch_source: str
    dir_patch_preinstall: str
    dir_patch_postinstall: str
    dir_patch_empty: str

    _config_valid: bool
    
    def __init__(self):

        # Set when config is validated
        self._config_valid: bool = False

        # Setting up config parsers
        config_parser = configparser.ConfigParser()
        
        self.error_str = ''

        try:
            # let defaults be relative to current working directory
            working_dir = os.path.abspath(os.path.curdir)
            config_path = os.path.join(working_dir, 'config/build.conf')
            pkglist_path = os.path.join(working_dir, 'config/pkg.list')
            livelist_path = os.path.join(working_dir, 'config/live.list')
            installerlist_path = os.path.join(working_dir, 'config/installer.list')
            poollist_path = os.path.join(working_dir, 'config/pool.list')

            parser = argparse.ArgumentParser(description='Dependency Parser - Athena Build System')
            parser.add_argument('--working-dir', type=str, help='Specify Working directory', required=False, default=working_dir)
            parser.add_argument('--config-file', type=str, help='Specify Configs File', required=False, default=config_path)
            parser.add_argument('--pkg-list', type=str, help='Specify user-selected pkg list', required=False, default=pkglist_path)
            parser.add_argument('--live-list', type=str, help='Specify live-only pkg list', required=False, default=livelist_path)
            parser.add_argument('--installer-list', type=str, help='Specify installer-only pkg list', required=False, default=installerlist_path)
            parser.add_argument('--pool-list', type=str, help='Specify pool-only pkg list (ship in apt pool, never installed)', required=False, default=poollist_path)
            args = parser.parse_args()

            # if paths are specified, they are absolute
            self.working_dir = os.path.abspath(args.working_dir)
            self.config_path = os.path.abspath(args.config_file)
            self.pkglist_path = os.path.abspath(args.pkg_list)
            self.livelist_path = os.path.abspath(args.live_list)
            self.installerlist_path = os.path.abspath(args.installer_list)
            self.poollist_path = os.path.abspath(args.pool_list)

            if not os.access(self.config_path, os.R_OK):
                raise PermissionError(f'Config file is not readable: {self.config_path}')

        except (argparse.ArgumentError, OSError, SystemExit) as e:
            self.error_str = f"Failed to parse arguments: {e}"
            return

        # read config file
        try:
            config_parser.read(self.config_path)
            self.arch = config_parser.get('Build', 'ARCH')

            # [Base] defaults — per-mirror sections may override BASEURL/BASEID
            _default_baseurl = config_parser.get('Base', 'BASEURL')
            _default_baseid  = config_parser.get('Base', 'BASEID')
            self.release     = config_parser.get('Base', 'RELEASE')
            self.baseid      = _default_baseid
            self.baseversion = config_parser.get('Base', 'BASEVERSION')

            self.mirrors = []
            for _section in config_parser.sections():
                if not _section.startswith('Mirror.'):
                    continue
                _id = _section.split('.', 1)[1]
                self.mirrors.append(Mirror(
                    id        = _id,
                    baseurl   = config_parser.get(_section, 'BASEURL', fallback=_default_baseurl),
                    baseid    = config_parser.get(_section, 'BASEID',  fallback=_default_baseid),
                    release   = self.release,
                    suffix    = config_parser.get(_section, 'Suffix',    fallback=''),
                    component = config_parser.get(_section, 'Component', fallback='main'),
                    arch      = self.arch,
                ))
            if not self.mirrors:
                self.error_str = "No [Mirror.*] sections in config"
                return

            # Snapshot pinning — opt-in.  Default off keeps the existing
            # live-mirror behaviour for users who haven't migrated yet.
            self.snapshot_enabled = config_parser.getboolean('Snapshot', 'Enabled', fallback=False)
            self.snapshot_timestamp_config = config_parser.get('Snapshot', 'Timestamp', fallback='latest').strip()
            # Snapshot endpoints — defaults preserve current Debian
            # behaviour; overridable for forks / derivative distros
            # running their own snapshot mirror.
            self.snapshot_baseurl = config_parser.get(
                'Snapshot', 'BaseUrl',
                fallback='https://snapshot.debian.org/archive').rstrip('/')
            self.snapshot_timestamp_api = config_parser.get(
                'Snapshot', 'TimestampApi',
                fallback='https://snapshot.debian.org/mr/timestamp/').strip()
            _archive_keys_raw = config_parser.get(
                'Snapshot', 'ArchiveKeys',
                fallback='debian, debian-security')
            self.snapshot_archive_keys = [
                _k.strip() for _k in _archive_keys_raw.split(',') if _k.strip()
            ]
            # Three-layer identity (see memory/project_three_layer_identity.md):
            #   build_distribution — the DISTRIBUTION (user-visible name on
            #     /etc/os-release NAME, GRUB menu, hostname, lsb-release).
            #     E.g. "Asgard".
            #   build_base_id      — lowercased distribution, used for
            #     /etc/os-release ID and as the dpkg ID namespace.
            #     Derived from build_distribution; no separate config slot
            #     today (override path: add BASE_ID to [Build] if a future
            #     case needs ID independent of lower(NAME), e.g. openSUSE's
            #     "opensuse-leap").
            #   build_codename     — the RELEASE codename (Debian's
            #     "bookworm" equivalent), used for /etc/os-release
            #     VERSION_CODENAME and the apt suite name.  E.g. "thor".
            #
            # Athena-Build itself (the toolchain) is a separate layer not
            # represented in config — it's the identity of THIS repository
            # and shows in fork package-name prefixes (athena-*),
            # ATHENA_CODENAME env var, and Maintainer fields.
            self.build_distribution = _strip_quotes(config_parser.get('Build', 'DISTRIBUTION'))
            self.build_base_id      = self.build_distribution.lower()
            self.build_codename     = _strip_quotes(config_parser.get('Build', 'CODENAME'))
            self.build_version      = _strip_quotes(config_parser.get('Build', 'VERSION'))

            self.container_release = config_parser.get('Build', 'CONTAINER_RELEASE', fallback=_DEFAULT_CONTAINER_RELEASE)
            self.docker_server = config_parser.get('Build', 'DOCKER_SERVER', fallback='')
            # When true, depth-1 Recommends of selected packages are
            # pulled into selected_pkgs / selected_srcs (downloaded but not
            # built by default; not installed in chroot).  See build.conf for
            # the full operator-facing rationale.
            self.include_recommends_in_repo = config_parser.getboolean(
                'Build', 'IncludeRecommendsInRepo', fallback=True
            )
            # COMP-09: default size for `iso build disk` output.  Sparse
            # qcow2 — actual on-disk footprint is much smaller (~chroot
            # size + metadata).  Operator overrides via `iso build disk
            # <n>` arg.
            self.disk_image_size_gb = config_parser.getint(
                'Build', 'DiskImageSizeGB', fallback=5,
            )
            # CONF-02: identity for the project's signing key — used by
            # generate_signing_key / verify_signing_key / print signing.
            # Format 'Name <email>'.  See [Repo] section in build.conf.
            self.signing_key_uid = config_parser.get(
                'Repo', 'SigningKeyUid',
                fallback='Athena Build <athena@local>'
            ).strip()
            # CONF-02: optional network apt source for the INSTALLED system.
            # When set, build_chroot writes /etc/apt/sources.list.d/athena.list
            # with [signed-by=athena-archive-keyring.gpg] pointing here, so a
            # booted Asgard system pulls signed updates from the published
            # Athena repo.  Empty (default) = no network source written; the
            # target relies on the /cdrom/pool source added at install time.
            # The URL is operator-supplied once the repo is published (COMP-02).
            self.apt_source_url = config_parser.get(
                'Repo', 'AptSourceURL', fallback=''
            ).strip()
            self.skip_build_test = config_parser.get('Source', 'SkipTest').split(', ')
            _tunneled_raw = config_parser.get('Source', 'Tunneled', fallback='')
            self.tunnel_packages: list[str] = [p.strip() for p in _tunneled_raw.split(',') if p.strip()]
            # BuildProfiles → DEB_BUILD_PROFILES (which Build-Depends a
            # source package activates at build time).
            # BuildOptions  → DEB_BUILD_OPTIONS  (how the build itself
            # behaves: nodoc, nocheck, parallel=N, …).
            # The two share names like nodoc / nocheck but are distinct
            # namespaces with distinct semantics.
            #
            # Backward compat: when BuildOptions is missing, mirror
            # BuildProfiles so existing build.conf files keep working.
            _profiles_raw = config_parser.get('Source', 'BuildProfiles', fallback='')
            self.build_profiles: frozenset[str] = frozenset(
                p.strip() for p in _profiles_raw.split(',') if p.strip()
            )
            _options_raw = config_parser.get('Source', 'BuildOptions', fallback='').strip()
            if _options_raw:
                self.build_options: frozenset[str] = frozenset(
                    p.strip() for p in _options_raw.split(',') if p.strip()
                )
            else:
                self.build_options = self.build_profiles
            self.max_parallel_builds = config_parser.getint('Build', 'MaxParallelBuilds', fallback=4)

            # Mirror InRelease GPG verification.  Default: enabled,
            # using the host-provided debian-archive-keyring.  Disabled
            # = true is for offline test fixtures and dev sandboxes only;
            # Cache.__get_files emits a per-build WARN when bypassed.
            self.security_keyring = config_parser.get(
                'Security', 'Keyring',
                fallback=_DEFAULT_SECURITY_KEYRING
            ).strip()
            self.security_disabled = config_parser.getboolean(
                'Security', 'Disabled', fallback=False
            )
            if not self.security_disabled:
                if not os.path.isfile(self.security_keyring):
                    self.error_str = (
                        f"Security.Keyring not found: {self.security_keyring} "
                        f"(install debian-archive-keyring, point Keyring "
                        f"at a different file, or set Disabled=true)"
                    )
                    return
                if not os.access(self.security_keyring, os.R_OK):
                    self.error_str = (
                        f"Security.Keyring not readable: {self.security_keyring}"
                    )
                    return

            # NOTE: The directories are relative to the working directory
            self.dir_download = os.path.join(self.working_dir, config_parser.get('Directories', 'Download'))
            self.dir_log = os.path.join(self.working_dir, config_parser.get('Directories', 'Log'))
            self.dir_cache = os.path.join(self.working_dir, config_parser.get('Directories', 'Cache'))
            self.dir_temp = os.path.join(self.working_dir, config_parser.get('Directories', 'Temp'))
            self.dir_source = os.path.join(self.working_dir, config_parser.get('Directories', 'Source'))
            self.dir_repo = os.path.join(self.working_dir, config_parser.get('Directories', 'Repo'))
            # repo/ uses the apt-conformant unified layout (CONF-01
            # Stage D, 2026-05-22).  classify_repo_subdir's labels
            # ('main' / 'doc' / 'dbgsym' / 'tests') still apply but now
            # map to nested paths:
            #   main   → dists/<codename>/main/binary-<arch>/
            #   doc    → dists/<codename>/doc/binary-<arch>/
            #   tests  → dists/<codename>/tests/binary-<arch>/
            #   dbgsym → dists/<codename>-debug/main/binary-<arch>/
            #            (Q2 — separate debug suite per Debian convention)
            # Plus two siblings of main:
            #   main udebs   → dists/<codename>/main/debian-installer/binary-<arch>/
            #   main sources → dists/<codename>/main/source/  (.dsc, .tar.*)
            # All dirs are mkdir+writability checked below.
            _codename = self.build_codename if hasattr(self, 'build_codename') else 'thor'
            # Strip stray surrounding quotes (build.conf VERSION="0.1" idiom)
            _codename = _codename.strip('"').strip("'")
            self._codename_for_repo = _codename
            self.dir_repo_main = os.path.join(
                self.dir_repo, 'dists', _codename, 'main',
                f'binary-{self.arch}')
            self.dir_repo_main_udeb = os.path.join(
                self.dir_repo, 'dists', _codename, 'main',
                'debian-installer', f'binary-{self.arch}')
            self.dir_repo_main_source = os.path.join(
                self.dir_repo, 'dists', _codename, 'main', 'source')
            self.dir_repo_doc = os.path.join(
                self.dir_repo, 'dists', _codename, 'doc',
                f'binary-{self.arch}')
            self.dir_repo_dbgsym = os.path.join(
                self.dir_repo, 'dists', f'{_codename}-debug', 'main',
                f'binary-{self.arch}')
            self.dir_repo_tests = os.path.join(
                self.dir_repo, 'dists', _codename, 'tests',
                f'binary-{self.arch}')
            self.dir_config = os.path.join(self.working_dir, config_parser.get('Directories', 'Config'))
            self.dir_image = os.path.join(self.working_dir, config_parser.get('Directories', 'Image'))
            # The [Directories] Chroot value is the PARENT directory holding
            # both chroots.  Live lands
            # at <parent>/live and installer at <parent>/installer — siblings
            # under one root.  Operator overrides the parent via build.conf;
            # both child paths follow.  This keeps the live chroot OUTSIDE
            # the installer chroot's content tree (and vice versa) without
            # the parent-and-sibling-with-suffix shape the original Phase 5
            # used.
            self.dir_buildroot        = os.path.join(self.working_dir, config_parser.get('Directories', 'Chroot'))
            self.dir_chroot           = os.path.join(self.dir_buildroot, 'live')
            self.dir_chroot_installer = os.path.join(self.dir_buildroot, 'installer')
            
            self.dir_patch = os.path.join(self.working_dir, config_parser.get('Directories', 'Patch'))
            self.dir_patch_source = os.path.join(self.dir_patch, 'source')
            self.dir_patch_preinstall = os.path.join(self.dir_patch, 'pre-install')
            self.dir_patch_postinstall = os.path.join(self.dir_patch, 'post-install')
            self.dir_patch_empty = os.path.join(self.dir_patch, 'empty')

            # Athena-owned Debian source packages discovered + built
            # from local trees under <dir_fork_source>/<pkg>/ instead
            # of via `apt-get source <pkg>`.  Walked at cache-build
            # time; auto-included in source build.  See
            # docs/plans/fork-source.md.
            self.dir_fork = os.path.join(self.working_dir, config_parser.get('Directories', 'Fork'))
            self.dir_fork_source = os.path.join(self.dir_fork, 'source')
            # dpkg-source -b output dir; populated by scripts/fork_mirror.py
            # at cache-build time.  Not parsed for source trees (the walker
            # explicitly skips this dir under fork/source/).
            self.dir_fork_source_repo = os.path.join(self.dir_fork_source, 'repo')

            # Isolated gnupg homedir for InRelease verification.  The
            # build-system.sh bootstrap creates this with mode 0700;
            # mirror that here so a Python-only invocation (e.g. tests)
            # gets the same layout without depending on the bash script.
            self.dir_gnupg = os.path.join(self.working_dir, config_parser.get('Directories', 'Gnupg'))

        except (configparser.Error, OSError) as e:
            self.error_str = str(e)
            return
        
        try:
            if not os.access(self.working_dir, os.W_OK):
                raise PermissionError(f'Working directory is not writable: {self.working_dir}')

            pathlib.Path(self.dir_download).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_log).mkdir(parents=True, exist_ok=True)
            
            pathlib.Path(self.dir_cache).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_temp).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_repo).mkdir(parents=True, exist_ok=True)
            for _sub in (
                self.dir_repo_main, self.dir_repo_main_udeb,
                self.dir_repo_main_source,
                self.dir_repo_doc, self.dir_repo_dbgsym, self.dir_repo_tests,
            ):
                pathlib.Path(_sub).mkdir(parents=True, exist_ok=True)

            # CONF-01 Stage D fix-up (2026-05-22): if any of the pre-
            # Stage-D dir names exist at the repo root and are empty,
            # rmdir them.  Stage C's migration removed them once but
            # a pre-Stage-D version of this very block would have
            # silently recreated them on the next session.  Now we
            # actively clean up.  Skip non-empty dirs: those weren't
            # successfully migrated and the operator should investigate
            # manually before they're cleaned up.
            for _legacy in ('main', 'doc', 'dbgsym', 'tests'):
                _path = os.path.join(self.dir_repo, _legacy)
                try:
                    if os.path.isdir(_path) and not os.listdir(_path):
                        os.rmdir(_path)
                except OSError:
                    # Best-effort: a permission error or race
                    # condition shouldn't fail BuildConfig init.
                    pass

            # CONF-01 Stage D fix-up: stale audit-Packages cache files
            # in dir_temp (pre-Stage-D filename shape: no path-hash
            # suffix).  These could serve empty cached packages
            # against the new (populated) layout until the operator
            # ran `repo audit refresh`.  Hashing the path into
            # the filename eliminates new collisions; this loop
            # cleans up the old uncached files left behind.
            try:
                for _f in os.listdir(self.dir_temp):
                    # Old shape: 'audit-Packages-<subdir>' (no hash)
                    # New shape: 'audit-Packages-<subdir>-<hash>'
                    if _f.startswith('audit-Packages-') and _f.count('-') == 2:
                        try:
                            os.remove(os.path.join(self.dir_temp, _f))
                        except OSError:
                            pass
            except OSError:
                pass

            pathlib.Path(self.dir_patch).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_empty).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_preinstall).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_patch_postinstall).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_fork).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_fork_source).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_fork_source_repo).mkdir(parents=True, exist_ok=True)

            pathlib.Path(self.dir_image).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_buildroot).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_chroot).mkdir(parents=True, exist_ok=True)
            pathlib.Path(self.dir_chroot_installer).mkdir(parents=True, exist_ok=True)

            # gpg refuses any homedir whose mode is broader than 0700,
            # so create with mode 0700 directly.  os.makedirs respects
            # the umask, so we follow with an explicit chmod to be safe
            # on hosts whose umask leaves the dir 0755.
            pathlib.Path(self.dir_gnupg).mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(self.dir_gnupg, 0o700)

            for _dir in (
                self.dir_download, self.dir_log, self.dir_cache, self.dir_temp,
                self.dir_source, self.dir_repo, self.dir_patch, self.dir_patch_empty,
                self.dir_patch_source, self.dir_patch_preinstall, self.dir_patch_postinstall,
                self.dir_fork, self.dir_fork_source, self.dir_fork_source_repo,
                self.dir_image, self.dir_buildroot, self.dir_chroot,
                self.dir_chroot_installer, self.dir_gnupg,
            ):
                if not os.access(_dir, os.W_OK):
                    raise PermissionError(f'Build directory is not writable: {_dir}')

            pathlib.Path(os.path.join(self.dir_log, 'build')).mkdir(parents=True, exist_ok=True)

        except OSError as e:
            self.error_str = f"Failed to prepare build directories: {e}"
            return
        
        self._config_valid = True
    
    @property
    def is_valid(self) -> bool:
        """
        Returns:
            bool: True if config is valid, False otherwise
        """
        return self._config_valid

    def deb_dir_for(self, label: str) -> str:
        """Map a repo subdir label to its on-disk dir under the CONF-01
        Stage D unified apt-repo layout.

        Recognised labels:
          main       → dists/<codename>/main/binary-<arch>/        (.deb)
          main-udeb  → dists/<codename>/main/debian-installer/
                        binary-<arch>/                              (.udeb)
          doc        → dists/<codename>/doc/binary-<arch>/
          dbgsym     → dists/<codename>-debug/main/binary-<arch>/
          tests      → dists/<codename>/tests/binary-<arch>/

        The four non-`main-udeb` labels match `classify_repo_subdir`
        output; `main-udeb` is the parallel udeb namespace inside main,
        added so `repo_audit.scan_repo_state` can produce a RepoState
        for the udeb side (used by `source verify`).

        Replaces the pre-CONF-01 idiom of
        `os.path.join(config.dir_repo, label)` — which constructed
        flat paths like repo/main/.
        """
        _mapping = {
            'main':      self.dir_repo_main,
            'main-udeb': self.dir_repo_main_udeb,
            'doc':       self.dir_repo_doc,
            'dbgsym':    self.dir_repo_dbgsym,
            'tests':     self.dir_repo_tests,
        }
        if label not in _mapping:
            raise ValueError(
                f"unknown repo subdir label: {label!r} "
                f"(expected one of {sorted(_mapping)})"
            )
        return _mapping[label]

    def deb_dest_for_filename(self, filename: str) -> str:
        """Compose classify_repo_subdir + deb_dir_for, with the udeb
        special-case (udebs route to main's debian-installer/ sibling,
        not main/binary-<arch>/).

        The one helper callers (buildcontainer's _segregate, audits,
        cleanups) should use to find "where does THIS .deb/.udeb go?"
        — eliminates path-construction at the call site.
        """
        _label = classify_repo_subdir(filename)
        if filename.endswith('.udeb') and _label == 'main':
            return self.dir_repo_main_udeb
        return self.deb_dir_for(_label)

    def all_deb_dirs(self) -> 'list[str]':
        """All on-disk dirs that hold .deb / .udeb files.  For repo
        walks (cmd_strip_repo, cmd_package_cleanup, cmd_audit_nmu,
        etc.) that need to find every binary artifact.

        Replaces the pre-CONF-01 idiom of `for sub in _REPO_SUBDIRS:
        join(dir_repo, sub)`.  Order is stable (binaries first, udebs
        next, then doc/dbgsym/tests in alphabetical order).
        """
        return [
            self.dir_repo_main,
            self.dir_repo_main_udeb,
            self.dir_repo_doc,
            self.dir_repo_dbgsym,
            self.dir_repo_tests,
        ]
    
    def error(self) -> str:
        """
        Returns:
            str: Error string if config is invalid, empty string otherwise
        """
        return self.error_str
    
def download_file(url: str, filename: str) -> tuple:
    """Downloads file and updates progressbar in incremental manner.

    Args:
        url: URL to download from.  Supports http(s)://, https://, and
             file:// schemes.  file:// is a local copy fast-path used
             primarily for fork mirror metadata (see scripts/fork_mirror.py).
        filename: Local path to write to; location must be writable.

    Returns:
        (size, detail) — size is bytes on success or -1 on failure;
        detail is '' on success or a short human-readable cause on
        failure (e.g. 'HTTP 404 Not Found', 'connection timeout',
        'OS write error: ...').  Callers should surface detail in
        their own error_str so the operator sees the actual reason
        rather than a generic "download failed".
    """
    from urllib.parse import urlsplit, unquote
    from requests import Timeout, TooManyRedirects, HTTPError, RequestException

    # file:// fast-path: local copy via shutil.copy, no HTTP, no progress
    # bar (transfers are small + instant).  Returns same (size, detail)
    # contract as the HTTP path.
    _parts = urlsplit(url)
    if _parts.scheme == 'file':
        _src_path = unquote(_parts.path)
        try:
            if not os.path.isfile(_src_path):
                return -1, f"file:// source missing: {_src_path}"
            _size = os.path.getsize(_src_path)
            import shutil as _shutil
            _shutil.copyfile(_src_path, filename)
            return _size, ''
        except OSError as e:
            _detail = f"file:// copy error: {e}"
            logger.error(f"download_file({url}): {_detail}")
            return -1, _detail

    try:
        name_strip: str = urlsplit(url).path.split('/')[-1].ljust(15, ' ')

        # Probe size via HEAD first (kept per memory note: pre-`f1cf373`
        # GET-only path returned 0 for some Debian mirrors and crashed the
        # bar with maxvalue=0).  Take the larger of HEAD / GET content-length
        # so whichever one knows wins, and fall back to a 1 MB seed if both
        # are 0 — the bar will expand dynamically as bytes arrive.
        head = requests.head(url, timeout=10)
        head_size = int(head.headers.get('content-length', 0))

        with requests.get(url, stream=True, timeout=10) as response:
            response.raise_for_status()
            get_size = int(response.headers.get('content-length', 0))
            expected_size = max(head_size, get_size) or (1 << 20)
            current_max  = expected_size

            progress_bar = tui.ProgressBar(label=name_strip, itr_label='B/s',
                                           maxvalue=expected_size)

            bytes_written = 0
            with open(filename, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)
                        # If the stream out-runs the size hint (HEAD/GET
                        # under-reported, or both were 0), grow the max so
                        # the bar keeps animating instead of freezing at
                        # 100% partway through.
                        if bytes_written > current_max:
                            current_max = int(bytes_written * 1.25)
                            progress_bar.set_max(current_max)
                        progress_bar.step(len(chunk))

            # Final correction so the persisted display matches reality
            # (in case the size hint over-reported and the bar stopped short).
            progress_bar.set_max(bytes_written)
            progress_bar.close()
            return bytes_written, ''

    except HTTPError as e:
        # raise_for_status path — preserve the HTTP status line so the
        # operator sees "HTTP 404 Not Found" rather than the generic
        # "download failed" the legacy single-int return produced.
        _resp = getattr(e, 'response', None)
        if _resp is not None:
            _detail = f"HTTP {_resp.status_code} {_resp.reason}"
        else:
            _detail = f"HTTPError: {e}"
        tui.console.print(f"ERROR: {_detail} for {url}")
        logger.error(f"download_file({url}): {_detail}")
        return -1, _detail
    except (ConnectionError, Timeout, TooManyRedirects, RequestException) as e:
        _detail = f"{type(e).__name__}: {e}"
        tui.console.print(f"ERROR: download failed for {url}")
        logger.error(f"download_file({url}): {_detail}")
        return -1, _detail
    except OSError as e:
        _detail = f"OS write error: {e}"
        tui.console.print(f"ERROR: cannot write to {filename}")
        logger.error(f"download_file write {filename}: {e}")
        return -1, _detail
    except ValueError as e:
        _detail = f"malformed response: {e}"
        tui.console.print(f"ERROR: malformed response from {url}")
        logger.error(f"download_file parse {url}: {e}")
        return -1, _detail
    except Exception as e:
        _detail = f"{type(e).__name__}: {e}"
        tui.console.print(f"ERROR: unexpected failure downloading {url}")
        logger.error(f"download_file({url}): {_detail}")
        return -1, _detail


def download_source(dependency_tree, dir_download):
    from urllib.parse import urljoin, urlsplit, unquote
    from requests import Timeout, TooManyRedirects, HTTPError, RequestException
    import shutil as _shutil

    _downloaded_size = 0
    _download_size = dependency_tree.download_size

    # Per-file: {filename: file_meta}, parallel {filename: Mirror}.  Each
    # Source object carries the mirror it was parsed from so we hit the
    # correct pool (sources in bookworm-security live under a different
    # baseid than main).
    _file_list: dict = {}
    _file_mirror: dict = {}
    for _pkg_name in dependency_tree.selected_srcs:
        _src = dependency_tree.selected_srcs[_pkg_name]
        if _src._mirror is None:
            logger.error(f"download_source: source {_pkg_name} has no _mirror — cache ingest bug")
            continue
        _file_list.update(_src.files)
        for _fname in _src.files:
            _file_mirror[_fname] = _src._mirror

    _index = 1
    _skipped = 0
    _total = len(_file_list)

    try:
        progress_bar = ProgressBar(label='Downloading', itr_label='B/s', maxvalue=max(1, _download_size))
    except Exception as e:
        progress_bar = None
        tui.console.print("WARNING: progress bar unavailable, continuing without it")
        logger.error(f"download_source ProgressBar: {type(e).__name__}: {e}")

    for _file in _file_list:
        if progress_bar is not None:
            progress_bar.label(f'({_index}/{_total}) {_file[:20]}')

        _mirror = _file_mirror[_file]
        _base_url = _mirror.url + '/'
        _url = urljoin(_base_url, _file_list[_file]['path'])
        _sha256 = _file_list[_file]['sha256']
        _expected_size = int(_file_list[_file]['size'])
        _download_path = os.path.join(dir_download, _file)

        if get_sha256(_download_path) != _sha256:
            # file:// fast-path for fork mirror — local copy via shutil
            # instead of HTTP.  Fork's Sources index ships real SHA256 +
            # Size (helper computed them at cache time), so the existing
            # verification below still gates the copy.  See
            # scripts/fork_mirror.py.
            _parts = urlsplit(_url)
            if _parts.scheme == 'file':
                _src_path = unquote(_parts.path)
                try:
                    _shutil.copyfile(_src_path, _download_path)
                except OSError as e:
                    tui.console.print(f"ERROR: file:// copy failed for {_url}")
                    logger.error(f"download_source({_url}): {e}")
                    continue
                if progress_bar is not None:
                    try:
                        progress_bar.step(os.path.getsize(_download_path))
                    except OSError:
                        pass
                # Fall through to the size + sha256 verification below
                # (which gates _file_list[_file] for downstream consumers).
                pass
            else:
                # Use the size from the InRelease-verified Sources index instead
                # of a HEAD probe — saves one round-trip per file and removes
                # the prior bug where a HEAD that returned 0 still produced
                # `_downloaded_size += 0` while the GET silently 404ed.
                try:
                    with requests.get(_url, stream=True, timeout=30) as response:
                        # raise_for_status surfaces 4xx/5xx as HTTPError so the
                        # existing requests-exception handler logs a clear
                        # "HTTP <status>" message instead of the prior cryptic
                        # downstream "Hash mismatch" the user saw on 404s.
                        response.raise_for_status()
                        with open(_download_path, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=1024):
                                if chunk:
                                    f.write(chunk)
                                    if progress_bar is not None:
                                        progress_bar.step(len(chunk))

                except (ConnectionError, Timeout, TooManyRedirects, HTTPError, RequestException) as e:
                    tui.console.print(f"ERROR: HTTP failure for {_url}")
                    logger.error(f"download_source({_url}): {e}")
                    continue
                except OSError as e:
                    tui.console.print(f"ERROR: cannot write {_download_path}")
                    logger.error(f"download_source write {_download_path}: {e}")
                    continue
                except ValueError as e:
                    tui.console.print(f"ERROR: malformed response for {_url}")
                    logger.error(f"download_source parse {_url}: {e}")
                    continue
                except Exception as e:
                    tui.console.print(f"ERROR: unexpected failure for {_url}")
                    logger.error(f"download_source({_url}): {type(e).__name__}: {e}")
                    continue

            # Validate what landed on disk *before* the sha256 check, so a
            # short/truncated 200 surfaces as a precise byte-count error
            # rather than the more cryptic "hash mismatch".  Skipping the
            # check when expected size is 0 lets older Sources stanzas
            # without size metadata still pass through to the sha256 gate.
            try:
                _on_disk = os.path.getsize(_download_path)
            except OSError as e:
                tui.console.print(f"ERROR: cannot stat downloaded {_download_path}")
                logger.error(f"download_source stat {_download_path}: {e}")
                continue

            if _expected_size > 0 and _on_disk != _expected_size:
                tui.console.print(
                    f"ERROR: short download for {_file} — "
                    f"got {_on_disk} bytes, expected {_expected_size}"
                )
                logger.error(
                    f"short_download {_url}: {_on_disk}/{_expected_size} bytes"
                )
                continue

            if get_sha256(_download_path) != _sha256:
                tui.console.print(f"ERROR: Hash mismatch for {_file} — download may be corrupt")
                logger.error(f"sha256 mismatch: {_download_path} expected {_sha256}")
                continue

            _downloaded_size += _on_disk

        else:
            _skipped += 1
            if progress_bar is not None:
                progress_bar.step(_expected_size)
            _downloaded_size += _expected_size

        _index += 1

    if progress_bar is not None:
        progress_bar.close(persist=True)

    tui.console.print(f"Downloading {_total - _skipped} files, Skipped {_skipped} files")
    return _downloaded_size


def search(re_string: str, base_string: str) -> str:
    """
    Internal function to simplify re.search() execution
    Args:
        re_string: the regex to execute
        base_string: the content on which it is to be executed

    Returns:
        str: Match group, empty string on no match
    """
    _match = re.search(re_string, base_string)
    if _match is not None:
        return _match.group(1)
    return ''


def get_md5(filepath: str) -> str:
    """
    Internal function to calculate the md5 of given file
    Args:
        filepath: The file to calculate md5 hash of

    Returns:
        str: md5
    """
    if not os.path.isfile(filepath):
        return ''
    try:
        h = hashlib.md5()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        logger.warning(f"get_md5: cannot read {filepath}: {e}")
        return ''


def _compute_sha256(filepath: str) -> str:
    """Compute SHA-256 of `filepath` — pure I/O, no caching.  Returns
    empty string on missing file or read error (logged as warning).

    Separate from `get_sha256` so the cache layer in that function
    has a clean monkey-patch target for the unit tests (tests assert
    cache hits by replacing this with a counter)."""
    if not os.path.isfile(filepath):
        return ''
    try:
        with open(filepath, 'rb') as f:
            return hashlib.file_digest(f, 'sha256').hexdigest()
    except OSError as e:
        logger.warning(f"_compute_sha256: cannot read {filepath}: {e}")
        return ''


def get_sha256(filepath: str, use_cache: bool = True) -> str:
    """Return the SHA-256 hash of `filepath` (hex digest), empty
    string on missing file / read error.

    With `use_cache=True` (default), checks `<filepath>.verified` for
    a cached hash recorded against the file's `(size, mtime_ns)`.
    When the recorded pair matches the current file stat, returns
    the cached hash without re-reading the file — a no-op verify
    run over hundreds of source tarballs drops from ~30 seconds of
    SHA-256 to milliseconds of `stat()`+sidecar-read.

    Sidecar format — single line, space-separated:

        <size_bytes> <mtime_ns> <sha256_hex>

    Cache miss (sidecar missing OR malformed OR (size, mtime_ns)
    don't match) → recomputes via `_compute_sha256`, writes a fresh
    sidecar.  Sidecar write failures are non-fatal: the computed
    hash is returned and the next call simply recomputes again.

    Cache invariant: `(size, mtime_ns)` unchanged ⇒ file content
    unchanged.  Holds in our pipeline — downloads write fresh files
    with new mtime; we never edit in place.  Operators who replace
    a file out-of-band (e.g. manual `cp --preserve=timestamps`) must
    also delete `<file>.verified` to force a recompute.

    `use_cache=False` forces a fresh compute AND does not touch the
    sidecar — for callers that just wrote the file and want to
    round-trip it from disk to confirm what was written.
    """
    if not use_cache:
        return _compute_sha256(filepath)

    if not os.path.isfile(filepath):
        return ''

    try:
        _stat = os.stat(filepath)
        _size = _stat.st_size
        _mtime_ns = _stat.st_mtime_ns
    except OSError as e:
        logger.warning(f"get_sha256: cannot stat {filepath}: {e}")
        return ''

    _sidecar = filepath + '.verified'
    try:
        with open(_sidecar, 'r', encoding='utf-8') as fh:
            _parts = fh.readline().strip().split()
        if (len(_parts) == 3
                and int(_parts[0]) == _size
                and int(_parts[1]) == _mtime_ns):
            return _parts[2]
    except (OSError, ValueError):
        # Sidecar missing or malformed — fall through to recompute.
        pass

    _sha = _compute_sha256(filepath)
    if _sha:
        try:
            with open(_sidecar, 'w', encoding='utf-8') as fh:
                fh.write(f"{_size} {_mtime_ns} {_sha}\n")
        except OSError as e:
            # Non-fatal: caller still gets the correct hash; next
            # invocation will recompute and try the sidecar write again.
            logger.debug(
                f"get_sha256: cannot write sidecar {_sidecar}: {e}"
            )
    return _sha


def parse_pkg_list_groups(path: str) -> 'dict[str, list[str]]':
    """Parse a pkg.list file into named groups.

    Two supported layouts:

    1. **Flat** (legacy, no `[section]` markers) — every non-comment,
       non-blank line is a seed.  Treated as a single `[base]` group so
       older configs without groups still parse cleanly.

    2. **INI-style** — `[group_name]` headers split the file into named
       groups, each containing one seed name per line.  Comments (`#`)
       and blank lines are allowed within sections; group names are
       free-form identifiers (no validation beyond non-empty).

    Returns a dict mapping group name → list of seed names (declaration
    order preserved, which `dict[str, ...]` guarantees in Python 3.7+).

    Raises:
        ValueError: in INI-style mode, a seed appears before any
        `[section]` header (operator probably forgot `[base]`).
        OSError: path is unreadable.
    """
    _raw = readfile(path)
    _lines = _raw.splitlines()

    # First pass: does the file contain ANY `[section]` header?  Decides
    # which mode to parse in.
    _section_re = re.compile(r'^\s*\[([^\]]*)\]\s*$')
    _has_sections = any(_section_re.match(_l) for _l in _lines)

    if not _has_sections:
        # Flat mode — every package becomes a [base] seed.
        _seeds = []
        for _l in _lines:
            _name = _l.strip()
            if not _name or _name.startswith('#'):
                continue
            _seeds.append(_name)
        return {'base': _seeds}

    # INI mode.  Track current section; reject seeds before any header.
    _groups: 'dict[str, list[str]]' = {}
    _current: 'Optional[str]' = None
    for _lineno, _l in enumerate(_lines, start=1):
        _stripped = _l.strip()
        if not _stripped or _stripped.startswith('#'):
            continue
        _m = _section_re.match(_l)
        if _m:
            _current = _m.group(1).strip()
            if not _current:
                raise ValueError(
                    f"{path}:{_lineno}: empty group name in section header"
                )
            _groups.setdefault(_current, [])
            continue
        if _current is None:
            raise ValueError(
                f"{path}:{_lineno}: package {_stripped!r} appears before any "
                "`[group]` header; INI-style pkg.list requires every "
                "package under a named section.  Add `[base]` at the top "
                "if the file was previously flat."
            )
        _groups[_current].append(_stripped)
    return _groups


def parse_pkg_list_group_meta(path: str) -> 'dict[str, dict[str, str]]':
    """Parse per-group metadata from a pkg.list file.

    Format: a `## Description: ...` comment line directly after a
    `[group]` header (or anywhere within the group's body, but
    convention is right under the header) becomes the group's
    `Description:` field in the generated tasksel `.desc` file.

    Returns dict mapping group name → dict of metadata.  Currently
    the only key is `'description'`; the shape is extensible (e.g.
    `'section'`, `'mandatory'`) without breaking call sites.

    A flat (legacy) pkg.list with no `[section]` markers returns
    `{'base': {}}` — implicit base group with empty metadata.

    Missing description → group is absent from the returned dict's
    entry value (caller falls back to a default).
    """
    _raw = readfile(path)
    _lines = _raw.splitlines()
    _section_re = re.compile(r'^\s*\[([^\]]*)\]\s*$')
    _desc_re = re.compile(r'^\s*##\s*Description:\s*(.+?)\s*$')

    _has_sections = any(_section_re.match(_l) for _l in _lines)
    if not _has_sections:
        return {'base': {}}

    _meta: 'dict[str, dict[str, str]]' = {}
    _current: 'Optional[str]' = None
    for _l in _lines:
        _stripped = _l.strip()
        if not _stripped:
            continue
        _m_sec = _section_re.match(_l)
        if _m_sec:
            _current = _m_sec.group(1).strip()
            _meta.setdefault(_current, {})
            continue
        if _current is None:
            continue
        _m_desc = _desc_re.match(_l)
        if _m_desc:
            _meta[_current]['description'] = _m_desc.group(1)
    return _meta


def readfile(filename: str) -> str:
    try:
        with open(filename, 'r') as f:
            return f.read()
    except OSError as e:
        raise OSError(f'Cannot read file {filename}: {e}') from e


def create_folders(folder_structure: str):
    if not os.path.isabs(folder_structure):
        raise ValueError(f'create_folders requires an absolute path, got: {folder_structure!r}')

    # split the folder structure string into individual path components
    components = folder_structure.split('/')

    # iterate over the path components and create the directories
    path = '/'
    try:
        for component in components:
            if '{' in component:
                # expand the braces and create directories for each combination
                subcomponents = component.strip('{}').split(',')
                for subcomponent in subcomponents:
                    new_path = os.path.join(path, subcomponent)
                    os.makedirs(new_path, exist_ok=True)
            else:
                path = os.path.join(path, component)
                os.makedirs(path, exist_ok=True)
    except Exception as e:
        tui.console.print(f"ERROR: Failed to build folder structure: {e}")
        logger.error(f"create_folders({folder_structure}): {e}")


