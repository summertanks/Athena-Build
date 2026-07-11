"""Version-suffix logic — the single, manually-reviewable home for every
rule that decides what version string an artifact carries.

The scheme is CONTENT-ORDER: we rebuild from source and keep the upstream
version almost unchanged, translating only a TRAILING stable-update marker
(`+debNuK` / `~debNuK`) into our own (`+asg<R>uK` / `~asg<R>uK`).  The update
number K is intrinsic to the upstream version, so a faithful rebuild needs no
ledger and older content always sorts lower (true downgrades).

Key entry points:

  * `transpose` — rewrite a trailing update marker in place (the core move).
    `transpose_control_text` / `transpose_deb` apply it to a DEBIAN/control text
    and to a built .deb (filename + control + dependency constraints + sibling
    pins) in a single repack.
  * `transposed_version` / `compute_transposed_versions` — the pure predictors
    that mirror the build path without running it.
  * `decide_patch_bump_count` — our patch level P on the current base.
    `_append_patch_force` anchors `+pP` / `+bN` inside the update namespace so
    they sort below the next upstream update.

`utils` re-exports every public name here, so existing `utils.<name>` call
sites keep working unchanged; new code may import from `bump` directly.  This
module has NO dependency on `utils` (one-way: utils -> bump), which keeps the
import graph acyclic.

See docs/versioning-mechanics.md for the full rationale and the exact rules.
"""

import hashlib
import os
import re
from typing import Callable, Dict, Optional

# sha256 of the empty byte string — the patch_set_hash an UNPATCHED source
# carries (utils.patch_set_hash([]) hashes "").  A record whose patch_set_hash
# differs from this carries real Athena patches.
EMPTY_PATCH_SET_HASH = hashlib.sha256(b'').hexdigest()


def parse_deb_filename(filename: str) -> 'Optional[tuple]':
    """Split a Debian binary package filename into (name, version, arch, ext).

    Convention: ``<name>_<version>_<arch>.{deb,udeb}`` — no underscore inside
    any of the three fields (Debian policy).  The single structural splitter
    in the codebase; every strict ``name_version_arch`` parse routes
    here.  Two deliberate contract points:

    * *name* keeps any directory prefix the caller passed, so a pool
      ``Filename`` (``pool/main/f/foo/foo_1-2_amd64.deb``) round-trips and the
      callers that reconstruct a path stay byte-exact.
    * *version* is the RAW filename form — an epoch stays encoded as ``%3a``,
      again so filename reconstruction is lossless.  Decode with
      ``.replace('%3a', ':')`` when the control-file form is wanted.

    Returns None when *filename* is not a .deb/.udeb or does not split into
    exactly three underscore-separated parts; each caller maps the miss to its
    own convention (skip / raise / status dict / sentinel).
    """
    for _ext in ('.udeb', '.deb'):
        if filename.endswith(_ext):
            _base = filename[:-len(_ext)]
            break
    else:
        return None
    _parts = _base.split('_')
    if len(_parts) != 3:
        return None
    return _parts[0], _parts[1], _parts[2], _ext


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
    _r = parse_deb_filename(file)
    if _r is None:
        raise ValueError(f"Incorrectly formatted package filename: {file!r}")
    _pkg_name, _version, _arch, _ext = _r
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
    _r = parse_deb_filename(filename)
    if _r is None:
        return filename
    _pkg, _ver, _arch, _ext = _r
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


# Trailing binary-only rebuild marker: +bN (modern binNMU) or ~bpoN+M
# (backport).  NARROWER than _NMU_SUFFIX_RE — it deliberately does NOT touch
# +debNuN/~debNuN (redistribution ordinals, which the TRANSPOSE scheme rewrites
# rather than strips) nor +nmuN / dotted Debian revisions / +really (real
# source identity).  Used by the Pass-1 rebuild decision to compare a source
# version independent of a buildd's binary-only rebuild.
_BINNMU_SUFFIX_RE = re.compile(r'(?:\+b\d+|~bpo\d+\+\d+)$')


def strip_binNMU(version: str) -> 'tuple[str, str]':
    """Split a trailing binary-only rebuild marker off a version string.

    Returns ``(base, marker)`` where *marker* is the trailing ``+bN`` /
    ``~bpoN+M`` (``''`` when absent) and *base* is the version with that marker
    removed.  Unlike :func:`strip_nmu_suffix` this does NOT touch
    ``+debNuN``/``~debNuN`` (transposed, not stripped) nor ``+nmuN`` / a dotted
    Debian revision / ``+really`` (real source identity — kept verbatim).

        strip_binNMU('1.0-2+b1')          → ('1.0-2', '+b1')
        strip_binNMU('1.0-2~bpo12+1')     → ('1.0-2', '~bpo12+1')
        strip_binNMU('1.0-2+deb12u3+b1')  → ('1.0-2+deb12u3', '+b1')
        strip_binNMU('1.0-2+deb12u3')     → ('1.0-2+deb12u3', '')
        strip_binNMU('2.10.4+nmu1')       → ('2.10.4+nmu1', '')
    """
    _m = _BINNMU_SUFFIX_RE.search(version)
    if not _m:
        return version, ''
    return version[:_m.start()], _m.group(0)


# ---------------------------------------------------------------------------
# Athena update-version suffix:  +asg<R>u<N>
#
# Mirrors Debian's `debNuN` shape but is distribution- not codename-derived,
# so it stays monotonic across codename changes (thor→loki): the release
# number R (from [Build] VERSION) orders the suffix, NEVER the codename
# (`+thorN` was rejected because `loki1 < thor1` would silently break
# release-upgrades).  `asg` is the FIXED Asgard build-origin tag — the
# parallel to Debian's `deb` — not derived from the configurable
# distribution name, so the suffix shape is stable regardless of config.
#
#   pristine upstream, built untouched :  3.0.15-1
#   our delta (security/update/patch)  :  3.0.15-1+asg1u1, +asg1u2, …
#
# Sorts above pristine and is monotonic across releases (asg2u1 > asg1u9),
# verified with `dpkg --compare-versions`.
# ---------------------------------------------------------------------------
# Matches our marker at the end of a version: the `+asg<R>uK` update marker OR
# its `~asg<R>uK` below-pristine form, followed by any chain of our patch
# (`+pP`) / force-binNMU (`+bN`) suffixes.  Anchored at `$`, with the sign and
# the trailing chain both optional-aware, so it reduces every shape the
# transpose scheme emits: `+asg1u3`, `~asg1u2`, `+asg1u0+p1`, `+asg1u3+p1+b1`.
ASG_SUFFIX_RE = re.compile(r'[+~]asg(\d+)u(\d+)(?:\+p\d+|\+b\d+)*$')


def parse_asg_suffix(version: str) -> 'Optional[tuple[int, int]]':
    """Return (release, K) parsed from a trailing `+asg<R>uK` / `~asg<R>uK`
    marker (ignoring any `+pP` / `+bN` tail), else None."""
    _m = ASG_SUFFIX_RE.search(version)
    if not _m:
        return None
    return int(_m.group(1)), int(_m.group(2))


def pristine_base(version: str) -> str:
    """Strip our marker (and its `+pP` / `+bN` tail) AND any upstream NMU layer,
    yielding the pristine source base version — the grouping key.

        pristine_base('3.0.15-1')                → '3.0.15-1'
        pristine_base('3.0.15-1+asg1u2')         → '3.0.15-1'
        pristine_base('3.0.15-1+asg1u0+p1')      → '3.0.15-1'  (patch on pristine)
        pristine_base('3.0.15-1+asg1u3+p1+b1')   → '3.0.15-1'
        pristine_base('2.4.67-1~asg1u2+p1')      → '2.4.67-1'  (~ form)
        pristine_base('3.0.15-1+deb12u2')        → '3.0.15-1'
        pristine_base('1:2.38.1-5+asg1u1')       → '1:2.38.1-5'  (epoch kept)
    """
    return strip_nmu_suffix(ASG_SUFFIX_RE.sub('', version))


def apply_asg_suffix(base: str, release: int, n: int) -> str:
    """Stamp a pristine base version with our update marker → base+asg<R>u<N>.
    Epoch (if any) is preserved — the suffix only appends to the end."""
    return f'{base}+asg{release}u{n}'


# ---------------------------------------------------------------------------
# TRANSPOSE — the content-order scheme.  Rather than strip a +debNuN to
# pristine and re-stamp a ship-order +asg<R>u<N>, we rewrite ONLY the TRAILING
# redistribution token (+debNuK / ~debNuK) to our own +asg<R>uK / ~asg<R>uK,
# in place.  K (the upstream point-release ordinal) is intrinsic — it rides in
# the upstream version, so a faithful rebuild needs no ledger and older content
# always sorts lower (true downgrades).  Everything before the trailing token —
# epoch, an EMBEDDED +deb (shim's `1.44~1+deb12u1+15.8-1...`), +dfsg/+ds/+git,
# a sourceful +nmuN, a dotted Debian revision — is preserved verbatim.  The
# leading +/~ sign is kept (a ~deb stays ~asg, sorting BELOW pristine, by
# design).  See docs/versioning-mechanics.md.
# ---------------------------------------------------------------------------
# Trailing redistribution token.  `tail` captures an optional `~` that
# constraint floors append after the ordinal (e.g. `>= 0.8.0-2+deb12u1~`); it
# is carried through so a transposed floor keeps sorting the same way.
_TRANSPOSE_RE = re.compile(r'(?P<sign>[+~])deb\d+u(?P<k>\d+)(?P<tail>~?)$')


def transpose(version: str, prefix: str, release: int) -> str:
    """Rewrite a TRAILING +debNuK / ~debNuK token to +<prefix><R>uK /
    ~<prefix><R>uK, in place.  Returns *version* unchanged when it carries no
    trailing redistribution token.

        transpose('2.36-9+deb12u14', 'asg', 1)  → '2.36-9+asg1u14'
        transpose('2.4.67-1~deb12u2', 'asg', 1) → '2.4.67-1~asg1u2'   (~ kept)
        transpose('7:5.1.9-0+deb12u1', 'asg', 1)→ '7:5.1.9-0+asg1u1'  (epoch kept)
        transpose('1.44~1+deb12u1+15.8-1~deb12u1', 'asg', 1)
                                 → '1.44~1+deb12u1+15.8-1~asg1u1' (embedded kept)
        transpose('2.10.4+nmu1', 'asg', 1)       → '2.10.4+nmu1'   (no trailing deb)
        transpose('2.10.4+nmu1+b1', 'asg', 1)    → '2.10.4+nmu1+b1'(trailing +b1)
        transpose('0.8.0-2+deb12u1~', 'asg', 1)  → '0.8.0-2+asg1u1~' (floor tail)
    """
    return _TRANSPOSE_RE.sub(
        lambda m: f"{m.group('sign')}{prefix}{release}u{m.group('k')}{m.group('tail')}",
        version,
    )


def asg_filename(filename: str, release: int, n: int) -> str:
    """Map a pristine repo filename to its +asg<R>u<N> stamped form
    (name_base_arch.ext → name_base+asgRuN_arch.ext).  No-op on a filename
    not in name_version_arch shape."""
    _r = parse_deb_filename(filename)
    if _r is None:
        return filename
    _pkg, _ver, _arch, _ext = _r
    return f'{_pkg}_{apply_asg_suffix(_ver, release, n)}_{_arch}{_ext}'


def match_pristine_base(predicted_fn: str, ondisk_fn: str) -> bool:
    """True when an on-disk artifact is the dep-tree's predicted (pristine)
    filename, optionally carrying a trailing +asg<R>u<N> on its version
    segment.  Reconciles the pristine filename the predictor computes
    (normalize_repo_filename) with a stamped on-disk .deb so the post-build
    existence checks (check_build / _source_state) don't loop forever the way
    the exact-filename match did.

    Requires identical package name, architecture, and extension; the on-disk
    version must equal the predicted version OR be a transpose of the same
    pristine base carrying our +asg<R>u<N> / ~asg<R>u<N> layer.
    """
    if predicted_fn == ondisk_fn:
        return True
    _p = parse_deb_filename(predicted_fn)
    _o = parse_deb_filename(ondisk_fn)
    if _p is None or _o is None:
        return False
    if _p[3] != _o[3]:                                 # ext must match
        return False
    if _p[0] != _o[0] or _p[2] != _o[2]:               # name, arch must match
        return False
    if _o[1] == _p[1]:                                 # exact (pristine) match
        return True
    if parse_asg_suffix(_o[1]) is None:                # on-disk has no asg layer
        return False
    # On-disk carries our asg layer.  Compare PRISTINE BASES, not just the
    # asg-stripped on-disk version, to reconcile the transpose model with the
    # predictor: transpose() rewrites only the TRAILING +debNuK/~debNuK and
    # PRESERVES an embedded +debN (a Debian-N update backported to Debian-M —
    # 6.16-1+deb13u1~deb12u1 -> 6.16-1+deb13u1~asg1u1), while the predictor
    # strips to bare pristine (6.16-1).  Stripping only the asg layer left the
    # embedded +deb13u1 behind and false-flagged the artifact as missing
    # (source audit stale_pass, 2026-07-09).  pristine_base strips the asg
    # layer AND any residual redistribution token, so both sides reduce to the
    # same key.  Strictly a superset of the old asg-strip equality (when that
    # matched, the bases match too), so no artifact that matched before stops.
    return pristine_base(_o[1]) == pristine_base(_p[1])


def find_matching_artifact(dst_dir: str,
                           predicted_filename: str,
                           dir_listing: 'Optional[list]' = None) -> 'Optional[str]':
    """Return the on-disk path of `predicted_filename` in `dst_dir`, or of an
    +asg<R>u<N>-stamped variant of it (match_pristine_base), else None.

    Reconciles the dep-tree's pristine filename prediction with a possibly
    stamped artifact so the post-build existence checks (check_build /
    _source_state) don't loop the way the exact-filename match did.
    The exact-name fast path avoids a listdir in the common pristine case;
    only a missing exact file triggers the (single-version, single-snapshot
    local) directory scan.

    `dir_listing` lets a caller that resolves MANY files against the same
    `dst_dir` (e.g. the chroot unpack/retry passes) pass `os.listdir(dst_dir)`
    once so the stamped-variant scan doesn't re-listdir per file.  None ⇒ scan
    the directory here (historical behaviour)."""
    _exact = os.path.join(dst_dir, predicted_filename)
    if os.path.isfile(_exact):
        return _exact
    try:
        _entries = os.listdir(dst_dir) if dir_listing is None else dir_listing
        for _cand in _entries:
            if (_cand.endswith(('.deb', '.udeb'))
                    and match_pristine_base(predicted_filename, _cand)
                    and os.path.isfile(os.path.join(dst_dir, _cand))):
                return os.path.join(dst_dir, _cand)
    except OSError:
        pass
    return None


def _rewrite_control_text(
        content: str,
        version_op: 'Callable[[str], str]',
        constraint_op: 'Optional[Callable[[str, Optional[str]], str]]' = None
        ) -> 'tuple[str, int]':
    """Shared scaffold for :func:`strip_nmu_from_control_text` and
    :func:`transpose_control_text`.  Applies ``version_op`` (a version-string
    rewrite) to the Version field, and ``constraint_op`` (defaults to
    ``version_op``, ignoring the name) to every version constraint in
    dep-related fields of a DEBIAN/control text; records the pre-rewrite
    Version in ``X-Athena-Upstream-Version`` when it changed (CVE-tracking
    provenance); and collapses the same-upstream-sibling ``>>/<<`` idiom to
    ``(= <new Version>)``.

    ``constraint_op`` receives ``(version, target_name)`` — the bound's
    version string and the package name the constraint targets (arch
    qualifier stripped; None when the name isn't on the same line, e.g. a
    wrapped continuation).  A DISTINCT ``constraint_op`` lets the transpose
    scheme strip a Debian binNMU / backport layer (``+bN`` / ``~bpoN+M``)
    from CONSTRAINT bounds — which reference OTHER packages' buildd-rebuild
    versions that don't exist in our repo — while the Version field keeps
    the transpose-only op so a tunnelled ``X+deb…+b1`` own version is
    preserved per :func:`transpose_deb`'s frozen-sibling-pin design.
    Without this the bounds retained NMU residue and 3.2k `-dev`→lib
    ``(= X+bN)`` pins failed to resolve against the stripped repo
    (full-universe audit 2026-07-09).  The target NAME is what lets
    :func:`transpose_control_text` exempt bounds aimed at tunnelled
    binaries, whose shipped version KEEPS the layer.

    Walks fields line-by-line (handles multi-line continuation per the deb822
    wrap convention: a field starts at column 0 with ``Name:``; continuation
    lines start with whitespace).  Returns (new_text, change_count).  Idempotent
    on already-rewritten text (zero changes; X-field insertion skipped).
    """
    _total = 0
    _content = content

    # Snapshot the original Version up front so we know what (if anything) to
    # record in X-Athena-Upstream-Version — before the rewrite mutates it.
    _orig_match = re.search(r'^Version: (\S+)\s*$', _content, re.MULTILINE)
    _orig_version = _orig_match.group(1) if _orig_match else ''
    _new_version = version_op(_orig_version) if _orig_version else ''
    _version_changed = bool(_new_version) and _new_version != _orig_version

    def _sub_version(_m: 're.Match') -> str:
        nonlocal _total
        _old = _m.group(1)
        _new = version_op(_old)
        if _new != _old:
            _total += 1
        return f'Version: {_new}'

    _content = re.sub(
        r'^Version: (\S+)\s*$',
        _sub_version, _content, count=1, flags=re.MULTILINE,
    )

    # Provenance: record the pre-rewrite upstream Version as an X-field right
    # after the (now-rewritten) Version line — only when it actually changed and
    # the field isn't already present (idempotent re-runs).
    if _version_changed and 'X-Athena-Upstream-Version:' not in _content:
        _content = re.sub(
            r'^(Version: \S+)\s*$',
            lambda _m: (
                f'{_m.group(1)}\n'
                f'X-Athena-Upstream-Version: {_orig_version}'
            ),
            _content, count=1, flags=re.MULTILINE,
        )

    # Per-relation version-constraint rewriter: matches `name (OP VERSION)`
    # within a dep-field's value.  The target name (with optional `:arch`
    # qualifier) is OPTIONAL — a wrapped continuation line can open with the
    # bare parenthesis — and is re-emitted verbatim; operator preserved.
    _constraint_re = re.compile(
        r'(?:([A-Za-z0-9][A-Za-z0-9.+-]*)((?::[A-Za-z0-9-]+)?\s*))?'
        r'\(\s*(<=|>=|<<|>>|=)\s*([^)]+?)\s*\)')

    if constraint_op is not None:
        _con_op = constraint_op
    else:
        def _con_op(_v: str, _name: 'Optional[str]') -> str:
            return version_op(_v)

    def _sub_constraint(_m: 're.Match') -> str:
        nonlocal _total
        _name, _tail, _op, _ver = _m.groups()
        _new_ver = _con_op(_ver, _name)
        if _new_ver != _ver:
            _total += 1
        _prefix = f'{_name}{_tail}' if _name is not None else ''
        return f'{_prefix}({_op} {_new_ver})'

    _new_lines: 'list[str]' = []
    _in_target = False
    for _line in _content.splitlines(keepends=True):
        if _line and _line[0] not in (' ', '\t'):
            _m = re.match(r'^([A-Za-z][A-Za-z0-9-]*):', _line)
            _in_target = _m is not None and _m.group(1) in _NMU_STRIP_FIELDS
        # (continuation lines inherit the surrounding _in_target state)
        if _in_target:
            _new_lines.append(_constraint_re.sub(_sub_constraint, _line))
        else:
            _new_lines.append(_line)
    _content = ''.join(_new_lines)

    # Pair-rewrite pass: collapse the same-upstream-sibling idiom
    # (`X (>> V), X (<< V-.)`) to `X (= our_version)`.  See
    # docs/versioning-mechanics.md (Appendix B) for the rationale.
    _our_version = _extract_version(_content)
    if _our_version:
        _content, _pairs = _rewrite_sibling_idiom_in_text(_content, _our_version)
        _total += _pairs

    return _content, _total


def strip_nmu_from_control_text(content: str) -> 'tuple[str, int]':
    """Return (new_text, strip_count) — strips NMU suffix from the Version
    field AND every version constraint in dep-related fields of a DEBIAN/control
    text, recording the pre-strip Version in ``X-Athena-Upstream-Version`` (CVE
    provenance; see docs/cve-tracking.md) when it changed.  Pristine sources
    (Version unchanged) get no X-field.  Idempotent.  Thin wrapper over
    :func:`_rewrite_control_text` with the per-version op = strip_nmu_suffix."""
    return _rewrite_control_text(content, strip_nmu_suffix)


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


def _content_date_epoch(work_dir: str) -> int:
    """Newest file mtime under an extracted .deb tree, as an integer epoch.

    `dpkg-buildpackage` clamps every file's mtime to SOURCE_DATE_EPOCH
    (derived from debian/changelog), so the newest file mtime equals that
    changelog date.  Reusing it as the repack's SOURCE_DATE_EPOCH makes the
    strip/restamp output byte-identical to the original build's clamping and
    independent of WHEN the repack runs.

    Call BEFORE rewriting DEBIAN/control — the rewrite re-dates control to
    `now`, which would otherwise win the max and defeat the clamp.
    """
    _sde = 0
    for _dp, _dirs, _files in os.walk(work_dir):
        for _n in _files:
            try:
                _m = int(os.lstat(os.path.join(_dp, _n)).st_mtime)
            except OSError:
                continue
            if _m > _sde:
                _sde = _m
    return _sde


def _repack_deb(work_dir: str, out_path: str, source_date_epoch: int) -> None:
    """`dpkg-deb --root-owner-group -b work_dir out_path`, clamped to
    SOURCE_DATE_EPOCH so the repack is byte-reproducible.

    Without a fixed epoch, dpkg-deb stamps the ar member + data.tar root
    timestamps with the current build time, so every strip/restamp of the
    same input produced different bytes — the cause of the 2026-06 mass
    hash-divergence between rebuilds and the published mirror (the package
    CONTENT was identical; only the wrapper timestamps moved).  See
    docs/versioning-mechanics.md / docs/build-quirks.md.
    """
    import subprocess
    _env = dict(os.environ)
    if source_date_epoch > 0:
        _env['SOURCE_DATE_EPOCH'] = str(source_date_epoch)
    subprocess.run(
        ['dpkg-deb', '--root-owner-group', '-b', work_dir, out_path],
        check=True, capture_output=True, env=_env,
    )


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
    if not (_base.endswith(('.deb', '.udeb'))):
        _result['status'] = 'skipped'
        return _result
    _r = parse_deb_filename(_base)
    if _r is None:
        _result['status'] = 'malformed'
        return _result
    _pkg, _old_filename_ver, _arch, _ext = _r

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
        _sde = _content_date_epoch(_work)   # before the control rewrite
        _ctrl_disk = os.path.join(_work, 'DEBIAN', 'control')
        with open(_ctrl_disk, 'w') as _fh:
            _fh.write(_new_ctrl_text)
        _repack_deb(_work, _new_path, _sde)

    if _new_path != deb_path:
        os.remove(deb_path)
    _result.update({
        'status': 'rewritten',
        'new_path': _new_path,
        'strips_count': _strips + (1 if _filename_changed else 0),
    })
    return _result


def _restamp_control_text(content: str, old_version: str,
                          new_version: str) -> str:
    """Rewrite the Version field old→new, and bump same-source sibling pins
    `(= old_version)` → `(= new_version)` within dep-relation fields.  Only
    `(=)` constraints whose version equals THIS binary's own version are
    touched (the collapsed >>/<< sibling idiom); other constraints are left."""
    content = re.sub(
        r'^Version: \S+\s*$', lambda _m: f'Version: {new_version}',
        content, count=1, flags=re.MULTILINE,
    )
    _pin_re = re.compile(r'\(\s*=\s*' + re.escape(old_version) + r'\s*\)')
    _new_lines: 'list[str]' = []
    _in_target = False
    for _line in content.splitlines(keepends=True):
        if _line and _line[0] not in (' ', '\t'):
            _m = re.match(r'^([A-Za-z][A-Za-z0-9-]*):', _line)
            _in_target = _m is not None and _m.group(1) in _NMU_STRIP_FIELDS
        if _in_target:
            _new_lines.append(_pin_re.sub(lambda _m: f'(= {new_version})', _line))
        else:
            _new_lines.append(_line)
    return ''.join(_new_lines)


def transposed_version(upstream_version: str, prefix: str, release: int,
                       patch_level: int = 0,
                       force_bn: 'Optional[int]' = None) -> str:
    """Pure predictor: the Version a binary at *upstream_version* carries under
    the transpose scheme.  ``V = transpose(upstream) [+ +p<P>] [+ +b<bN>]``.

        transposed_version('2.36-9+deb12u14', 'asg', 1)       → '2.36-9+asg1u14'
        transposed_version('5.36.0-7+deb12u3', 'asg', 1, 1)   → '5.36.0-7+asg1u3+p1'
        transposed_version('5.2.15-2', 'asg', 1, 1)           → '5.2.15-2+asg1u0+p1'
        transposed_version('1.0-2', 'asg', 1, force_bn=1)     → '1.0-2+asg1u0+b1'
    """
    return _append_patch_force(
        transpose(upstream_version, prefix, release),
        patch_level, force_bn, prefix, release)


def compute_transposed_versions(
    binary_versions: 'Dict[str, str]', prefix: str, release: int,
    patch_level: int = 0, force_bn: 'Optional[int]' = None,
) -> 'Dict[str, str]':
    """Pure version math mirroring the build path: transpose each binary's OWN
    upstream version (so a binary whose base differs from its source — e2fsprogs
    comerr-dev, lvm2 libdevmapper, samba ldb — is handled correctly), applying
    the SAME per-source ``patch_level`` / ``force_bn`` to every binary (uniform
    P/bN per source keeps sibling pins consistent).  Needs no ledger: K is
    intrinsic to each upstream version.

    `binary_versions` shape: ``{binary_name: upstream_version_str}``.
    Returns ``{binary_name: predicted_version_str}``.
    """
    return {
        _name: transposed_version(_up, prefix, release, patch_level, force_bn)
        for _name, _up in binary_versions.items()
    }


def transpose_control_text(content: str, prefix: str, release: int,
                           keep_binnmu_names: 'Optional[frozenset]' = None
                           ) -> 'tuple[str, int]':
    """Return (new_text, change_count) — TRANSPOSE the Version field and every
    version constraint in dep-related fields of a DEBIAN/control text (trailing
    +debNuK → +<prefix><R>uK only).  The Version's pre-transpose upstream value
    is recorded in ``X-Athena-Upstream-Version`` (CVE-tracking provenance) when
    it changed.  The same-upstream-sibling ``>>/<<`` idiom is collapsed to
    ``(= <transposed Version>)``.

    ``keep_binnmu_names`` — binary names whose bounds must NOT have a Debian
    binNMU / backport layer stripped: the TUNNELLED binaries, which ship at
    their upstream version verbatim when a ``+bN`` is trailing
    (:func:`transpose_deb` keeps it), so a bound targeting one must keep
    referencing it.  transpose() alone produces exactly the tunnelled
    target's shipped version in both shapes (trailing ``+bN`` → no-op =
    verbatim; trailing ``+deb`` → transposed).  Without the exemption the
    frozen sibling ``=`` pins inside a tunnelled source (firmware-nonfree's
    firmware-linux → firmware-misc-nonfree chain) go unresolvable the moment
    upstream binNMUs it (full-universe oracle 2026-07-11: current op breaks
    3 bounds under a simulated +b1, exempted op breaks 0, zero live-universe
    rewrite differences).

    The +pP / +bN patch/force suffix is NOT applied here — that is the deb-level
    step (:func:`transpose_deb`), which restamps the collapsed sibling pins to
    the exact final version.  Idempotent on already-transposed text.  Thin
    wrapper over :func:`_rewrite_control_text` with a transpose-only Version op
    and a binNMU-stripping CONSTRAINT op (see below).
    """
    def _constraint_op(_v: str, _name: 'Optional[str]') -> str:
        # Constraint bounds reference OTHER packages' versions; a Debian
        # binNMU (+bN) / backport (~bpoN+M) layer on them is buildd metadata
        # that doesn't exist in our repo, so strip it BEFORE transposing the
        # trailing +deb token (leaving +deb / embedded +deb / +nmuN / +dfsg
        # intact — strip_binNMU is narrow).  EXEMPT: a bound targeting a
        # tunnelled binary (keep_binnmu_names) — its shipped version KEEPS
        # the layer, so the bound must too.  GUARD: a bound that already
        # carries OUR +asg force level (+asg…+bN) is ours, not Debian's —
        # leave it to transpose() untouched so a re-normalise pass can't strip
        # a legitimate force-rebuild pin.
        if _name is not None and keep_binnmu_names \
                and _name in keep_binnmu_names:
            return transpose(_v, prefix, release)
        if parse_asg_suffix(_v) is not None:
            return transpose(_v, prefix, release)
        return transpose(strip_binNMU(_v)[0], prefix, release)
    return _rewrite_control_text(
        content, lambda _v: transpose(_v, prefix, release),
        constraint_op=_constraint_op)


# Exact-pin matcher: a `<name> (= <version>)` constraint inside a relation
# field.  Name capture allows the Debian package-name character set.
_EXACT_PIN_RE = re.compile(
    r'([A-Za-z0-9][A-Za-z0-9.+-]*)(\s*\(\s*=\s*)([^)]+?)(\s*\))')


def _restamp_sibling_pins(content: str, sibling_names: 'set',
                          patch_level: int, force_bn: 'Optional[int]',
                          prefix: str, release: int) -> str:
    """Apply our patch (`+pP`) / force (`+bN`) suffix to every exact `(= V)` pin
    in a dep-relation field whose TARGET is a same-source sibling.

    A patched source stamps every binary with the same uniform P, so its
    sibling `=` pins must land on the sibling's exact final version.  Matching by
    TARGET NAME (not by "the version equals this binary's own version") is what
    makes that hold when siblings carry DIFFERENT bases — the case
    `_restamp_control_text` misses.  The pins were already transposed by
    `transpose_control_text`, so this only appends our uniform tail.
    """
    if not sibling_names or (patch_level == 0 and force_bn is None):
        return content

    def _sub(_m: 're.Match') -> str:
        _name, _lhs, _ver, _rhs = _m.groups()
        if _name in sibling_names:
            _ver = _append_patch_force(_ver, patch_level, force_bn,
                                       prefix, release)
        return f'{_name}{_lhs}{_ver}{_rhs}'

    _lines: 'list[str]' = []
    _in_target = False
    for _line in content.splitlines(keepends=True):
        if _line and _line[0] not in (' ', '\t'):
            _m = re.match(r'^([A-Za-z][A-Za-z0-9-]*):', _line)
            _in_target = _m is not None and _m.group(1) in _NMU_STRIP_FIELDS
        _lines.append(_EXACT_PIN_RE.sub(_sub, _line) if _in_target else _line)
    return ''.join(_lines)


def transpose_deb(deb_path: str, prefix: str, release: int,
                  patch_level: int = 0,
                  force_bn: 'Optional[int]' = None,
                  sibling_names: 'Optional[set]' = None,
                  keep_binnmu_names: 'Optional[frozenset]' = None) -> dict:
    """Transpose + stamp a built .deb/.udeb in place — the single-cycle
    replacement for ``strip_nmu_from_deb`` + ``restamp_asg_deb`` on the build
    and tunnel paths.  Updates filename, DEBIAN/control Version, dep
    constraints, X-Athena-Upstream-Version, and same-source sibling pins.

    Returns ``{'status': 'rewritten'|'unchanged'|'malformed'|'skipped',
                'new_path': str, 'version': Optional[str]}``.

    A binary whose only trailing token is a binNMU (a tunnelled ``X+deb…+b1``)
    transposes to a no-op on the embedded +deb and KEEPS the +bN — by design
    (its frozen sibling pins reference that +bN).  For those pins to survive
    the constraint rewrite, pass the tunnelled binary names as
    ``keep_binnmu_names`` (see :func:`transpose_control_text`).  Idempotent.
    """
    import subprocess
    import tempfile
    from debian.debfile import DebFile

    _result = {'status': 'unchanged', 'new_path': deb_path, 'version': None}
    _base = os.path.basename(deb_path)
    if not _base.endswith(('.deb', '.udeb')):
        _result['status'] = 'skipped'
        return _result
    _r = parse_deb_filename(_base)
    if _r is None:
        _result['status'] = 'malformed'
        return _result
    _pkg, _old_file_ver, _arch, _ext = _r

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

    # Step 1: transpose Version + constraints to the K-version (no P/bN yet).
    _k_text, _changes = transpose_control_text(
        _ctrl_text, prefix, release, keep_binnmu_names=keep_binnmu_names)
    _k_ver = _extract_version(_k_text)
    if not _k_ver:
        _result['status'] = 'malformed'
        return _result
    # Step 2: append +pP / +bN and restamp sibling pins to the exact final.
    _final_ver = _append_patch_force(_k_ver, patch_level, force_bn,
                                     prefix, release)
    if _final_ver != _k_ver:
        if sibling_names is not None:
            # Set the Version field, then restamp EVERY same-source sibling pin
            # by target name — so cross-base siblings (e2fsprogs, samba, …) also
            # land on their exact final version under a patched/force build.
            _new_text = re.sub(
                r'^Version: \S+\s*$', lambda _m: f'Version: {_final_ver}',
                _k_text, count=1, flags=re.MULTILINE)
            _new_text = _restamp_sibling_pins(
                _new_text, sibling_names, patch_level, force_bn,
                prefix, release)
        else:
            # No sibling set (tunnel / standalone): same-base own-version pins.
            _new_text = _restamp_control_text(_k_text, _k_ver, _final_ver)
    else:
        _new_text = _k_text

    _new_file_ver = _append_patch_force(
        transpose(_old_file_ver, prefix, release), patch_level, force_bn,
        prefix, release)
    _filename_changed = _new_file_ver != _old_file_ver

    if not _filename_changed and _changes == 0 and _final_ver == _k_ver:
        return _result      # 'unchanged'

    _new_base = (f'{_pkg}_{_new_file_ver}_{_arch}{_ext}'
                 if _filename_changed else _base)
    _new_path = os.path.join(os.path.dirname(deb_path), _new_base)

    with tempfile.TemporaryDirectory(prefix='transpose-') as _work:
        subprocess.run(['dpkg-deb', '-R', deb_path, _work],
                       check=True, capture_output=True)
        _sde = _content_date_epoch(_work)   # before the control rewrite
        _ctrl_disk = os.path.join(_work, 'DEBIAN', 'control')
        with open(_ctrl_disk, 'w') as _fh:
            _fh.write(_new_text)
        _repack_deb(_work, _new_path, _sde)

    if _new_path != deb_path:
        os.remove(deb_path)
    _result.update({'status': 'rewritten', 'new_path': _new_path,
                    'version': _final_ver})
    return _result


def decide_patch_bump_count(prior: 'Optional[dict]', intended_version: str,
                            patch_set_hash: str) -> int:
    """Compute P — our patch level on the current source version — per the
    transpose Pass-1 rules, comparing this build against the prior build record.

      * patched := patch_set_hash is non-empty real patches
      * no prior, OR the source version changed → P = 1 if patched else 0
        (a new base re-baselines; our patch on it is patch #1)
      * same version, patch_set changed → P = prior.P + 1 if patched else 0
        (we edited our patch; removing it entirely drops to 0 — faithful)
      * same version, same patch → P = prior.P  (idempotent rebuild reuses)

    Mirrors the version-decision rules in docs/versioning-mechanics.md; computed at build time where the
    prior record is still on hand, so no separate cache-parse pass is needed.
    """
    _patched = bool(patch_set_hash) and patch_set_hash != EMPTY_PATCH_SET_HASH
    if not prior:
        return 1 if _patched else 0
    _prev_ver = prior.get('intended_version')
    _prev_hash = prior.get('patch_set_hash')
    try:
        _prev_p = int(prior.get('patch_bump_count', 0) or 0)
    except (TypeError, ValueError):
        _prev_p = 0
    if _prev_ver != intended_version:
        return 1 if _patched else 0
    if _prev_hash != patch_set_hash:
        return (_prev_p + 1) if _patched else 0
    return _prev_p


def _append_patch_force(version: str, patch_level: int,
                        force_bn: 'Optional[int]',
                        prefix: str, release: int) -> str:
    """Append our patch (+pP) and/or force-binNMU (+bN) suffix to an
    already-transposed version.  Order: +p then +b.

    Both `p` and `b` sort ABOVE `<prefix>` (`a`...) in Debian version ordering,
    so a +pP / +bN attached DIRECTLY to a pristine base would outrank every
    upstream-update marker (+<prefix><R>uK) and never be superseded.  To keep
    our changes sorting BELOW the next upstream update, anchor them inside the
    update namespace: when the version has no trailing +<prefix><R>uK marker
    (the base is pristine, no upstream update), synthesize ``u0`` first.

        1.2.3-4            +p1  → 1.2.3-4+asg1u0+p1   (< 1.2.3-4+asg1u1)
        1.2.3-4+asg1u3     +p1  → 1.2.3-4+asg1u3+p1   (anchor already present)
        2.4.67-1~asg1u2    +p1  → 2.4.67-1~asg1u2+p1  (~ form counts as anchor)
    """
    if patch_level > 0 or force_bn is not None:
        if not re.search(rf'[+~]{re.escape(prefix)}{release}u\d+$', version):
            version = f'{version}+{prefix}{release}u0'
    if patch_level > 0:
        version = f'{version}+p{patch_level}'
    if force_bn is not None:
        version = f'{version}+b{force_bn}'
    return version
