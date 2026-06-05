"""Virtual build pipeline — static simulation of cache parse → source
build → repo + publish audit, WITHOUT running dpkg-buildpackage.

The trick: every audit in the real pipeline already consumes abstract
state (RepoState, claim ledger).  Real builds only exist to populate
that state.  This module synthesizes the same state from cache data
plus the pure version-math helpers and feeds it back into the REAL
audit functions.

What it catches:
  - asg-stamp / NMU-strip arithmetic mistakes before any build runs
  - closure breaks in the predicted post-build repo
  - ownership / conflict / hash-conflict gate failures before publish
  - intra-source sibling pin drift (the kernel-meta scenario)

What it CANNOT catch:
  - compile-time failures (gcc errors, dh-helper bugs, autoconf)
  - linkage drift in fork patches (substvars are blind — we trust the
    upstream binary's Depends verbatim; see docs/virtual-build.md)
  - non-deterministic binary list (kernel meta packages whose binary
    names depend on upstream ABI bumps)

Substvar policy (locked, [[virtual-build]] design):
    Inherit upstream binary Package's Depends/Conflicts/Provides
    verbatim; rewrite version constraints whose target matches a
    SIBLING binary in the same source AND whose pristine base equals
    our source's pristine base (those are the intra-source pins
    `(= sibling-ver)` that strip_nmu + asg_stamp would rewrite at
    real build time).  External constraints (libc6, kernel ABI sonames)
    are LEFT UNCHANGED — that's how apt resolves them at install time
    anyway.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import utils

logger = logging.getLogger('athena')


# ─────────────────────────── Synthesizer primitives ──────────────────


def _strip_nmu_from_relation(raw: str) -> str:
    """Strip NMU layers from EVERY constraint version in a Depends-like
    relation string.  Mirrors what real-build's
    :func:`utils.strip_nmu_from_control_text` does post-build to the
    binary's on-disk control file.  Without this, virtual build's
    inherited-from-upstream Depends still pin to upstream NMU
    versions (``grub-common (>= 2.06-13+deb12u2)``) while our own
    synthesized binaries strip+stamp to ``2.06-13+asg1uN`` —
    apt's version_compare sees the asg-stamped version as LOWER
    than ``+deb12u2`` (ASCII ``a`` < ``d``) and the closure breaks.

    Run BEFORE sibling-pin rewriting so the latter sees pristine bases.
    """
    if not raw:
        return raw
    try:
        from debian.deb822 import PkgRelation
    except ImportError:
        return raw
    try:
        _relations = PkgRelation.parse_relations(raw)
    except Exception:
        return raw
    _changed = False
    for _or_group in _relations:
        for _rel in _or_group:
            _vc = _rel.get('version')
            if not _vc:
                continue
            _op, _cur = _vc
            _stripped = utils.strip_nmu_suffix(_cur)
            if _stripped != _cur:
                _rel['version'] = (_op, _stripped)
                _changed = True
    if not _changed:
        return raw
    return PkgRelation.str(_relations)


def _upstream_canonical_source(upstream_record: Dict[str, str]) -> str:
    """Extract the canonical source-package name from an upstream
    binary Package record.  Per Debian Packages format, the ``Source:``
    field carries either ``<srcname>`` or ``<srcname> (<version>)``;
    when omitted, the source name equals the binary name.

    Used by :func:`synthesize_source_binaries` to filter out
    falsely-declared binaries: a ``-signed-amd64`` source's
    ``Binary:`` field over-reports the udebs (the unsigned source
    actually emits them), and the upstream binary record's
    ``Source:`` field is the authoritative producer.
    """
    _src = (upstream_record.get('Source') or '').strip()
    if not _src:
        return (upstream_record.get('Package') or '').strip()
    return _src.split(' ', 1)[0]


def _rewrite_sibling_pins(raw: str,
                           sibling_ver_map: Dict[str, str],
                           sibling_pristine_map: Dict[str, str]) -> str:
    """Rewrite `(= V)` / `(>= V)` etc. constraints in a relation field
    where the target is a sibling binary in the same source AND V's
    pristine base equals THAT SIBLING'S pristine base (i.e. the
    constraint pins the sibling's upstream version, which real-build
    would re-stamp to the sibling's virtual version).

    Why per-target (not per-source) pristine matching: in a metapackage
    source like ``gcc-defaults`` (source version ``1.213``), the
    upstream binary ``gcc``'s control reads
    ``Depends: cpp (= 4:12.2.0-3)`` — the constraint pins ``cpp`` at
    the COMPILER upstream version, not the source version.  Matching
    against the SIBLING's pristine catches this; matching against the
    SOURCE's pristine misses it and leaves the dep referring to a
    binary version that doesn't exist in the synthesized state →
    spurious closure break.

    Other constraints (external pkgs, non-matching versions) left
    untouched.  Uses python-debian's PkgRelation parser → reformat
    round-trip so we don't have to handle every operator + whitespace
    edge case by hand.
    """
    if not raw or not sibling_ver_map:
        return raw
    try:
        from debian.deb822 import PkgRelation
    except ImportError:
        return raw
    try:
        _relations = PkgRelation.parse_relations(raw)
    except Exception:
        return raw
    _changed = False
    for _or_group in _relations:
        for _rel in _or_group:
            _name = _rel.get('name', '')
            if _name not in sibling_ver_map:
                continue
            _vc = _rel.get('version')
            if not _vc:
                continue
            _op, _cur = _vc
            _target_pristine = sibling_pristine_map.get(_name, '')
            if (_target_pristine
                    and utils.pristine_base(_cur) == _target_pristine):
                _rel['version'] = (_op, sibling_ver_map[_name])
                _changed = True
    if not _changed:
        return raw
    return PkgRelation.str(_relations)


def _strip_epoch(version: str) -> str:
    """Drop the ``<epoch>:`` prefix from a Debian version string.

    On-disk ``.deb`` filenames OMIT the epoch entirely — dpkg-buildpackage
    writes ``gcc_12.2.0-3_amd64.deb`` even though the internal
    ``Version:`` is ``4:12.2.0-3``.  Verified against the live build
    log 2026-06-05.  (HTTP URL-encoding to ``%3a`` is a separate apt
    pool quirk that only kicks in if the filename DID carry the epoch
    — which dpkg's filenames don't.)
    """
    return version.split(':', 1)[-1] if ':' in version else version


def _stamped_filename(name: str, version: str, arch: str,
                      ext: str = '.deb') -> str:
    """Synthetic on-disk basename in `name_version_arch.ext` form —
    the same convention dpkg uses; matches what `mirror audit` would
    see.  Strips the epoch from the version segment (dpkg's filename
    convention; see :func:`_strip_epoch`)."""
    return f"{name}_{_strip_epoch(version)}_{arch}{ext}"


def _virtual_sha256(name: str, version: str, arch: str) -> str:
    """Deterministic synthetic SHA-256 for a virtual binary — uniqueness
    by (name, version, arch).  Hash-conflict checks use it to compare
    ACROSS BUILDERS; same triple → same synthetic hash → no spurious
    conflict.  Different triple → different hash → real conflict if
    two builders synthesized the same (pkg, version) at different
    sources, which the audit MUST flag."""
    import hashlib
    _key = f"virtual:{name}:{version}:{arch}".encode('utf-8')
    return hashlib.sha256(_key).hexdigest()


def _control_lookup_from_upstream(
    upstream_record: Dict[str, str], rename_map: Dict[str, str],
) -> Dict[str, str]:
    """Copy the upstream binary's control fields verbatim, then strip
    NMU residue from every version constraint (so sibling pins read
    pristine) and rewrite sibling-pin pristine vers → virtual vers.

    `rename_map` is the source's `{sibling_name: virtual_version}`
    map.  `upstream_record` shape matches RepoState.packages entries:
    `{control_field: value_str}`.
    """
    _out: Dict[str, str] = {}
    for _field, _val in upstream_record.items():
        _out[_field] = _val
    return _out


def synthesize_binary_record(
    source_name: str, binary_name: str, virtual_version: str, arch: str,
    upstream_record: Optional[Dict[str, str]],
    sibling_ver_map: Dict[str, str],
    sibling_pristine_map: Dict[str, str],
) -> Dict[str, str]:
    """Build one RepoState.packages-style record for a single virtual
    binary.

    `upstream_record` is the binary's CURRENT upstream control fields
    (from cache.package_hashtable, highest version).  None → minimal
    skeleton with just Package / Version / Architecture / Filename /
    Source / SHA256.

    `sibling_ver_map` is the full ``{binary_name: virtual_version}``
    for this source.  `sibling_pristine_map` is the parallel
    ``{binary_name: pristine_upstream_version}`` map — per-binary,
    because metapackage sources (gcc-defaults, glibc) ship binaries at
    DIFFERENT upstream versions than the source itself, so source-level
    pristine matching would miss intra-source pins.  See
    :func:`_rewrite_sibling_pins`.

    Returned dict carries str values (matches what scan_repo_state
    stores from apt_pkg.TagFile).
    """
    _fn = _stamped_filename(binary_name, virtual_version, arch)
    _sha = _virtual_sha256(binary_name, virtual_version, arch)
    _prefix = (binary_name[:4] if binary_name.startswith('lib')
               else binary_name[:1])
    _filename_field = (
        f"pool/main/{_prefix}/{source_name}/{_fn}")
    _rec: Dict[str, str] = {
        'Package':      binary_name,
        'Version':      virtual_version,
        'Architecture': arch,
        'Source':       source_name,
        'Filename':     _filename_field,
        'SHA256':       _sha,
        'Size':         '0',
    }
    if upstream_record is None:
        return _rec
    for _field in ('Depends', 'Pre-Depends', 'Recommends', 'Suggests',
                   'Enhances', 'Conflicts', 'Breaks', 'Provides',
                   'Replaces'):
        _raw = upstream_record.get(_field)
        if not _raw:
            continue
        # Two-pass rewriting: NMU strip first (every constraint
        # version), THEN sibling pin rewriting (per-target).  Same
        # order as real-build: strip_nmu_from_control_text runs
        # before any sibling-pin restamping.
        _stripped = _strip_nmu_from_relation(_raw)
        _rewritten = _rewrite_sibling_pins(
            _stripped, sibling_ver_map, sibling_pristine_map)
        _rec[_field] = _rewritten
    for _field in ('Priority', 'Section', 'Essential',
                   'Multi-Arch', 'Homepage'):
        _val = upstream_record.get(_field)
        if _val:
            _rec[_field] = _val
    _ua = upstream_record.get('Architecture')
    if _ua and _ua.strip() in ('all', 'any'):
        _rec['Architecture'] = _ua.strip()
    return _rec


def synthesize_source_binaries(
    source: Any, package_universe: Dict[str, Dict[str, Any]],
    asg_ledger: Optional[Dict[str, List[str]]], release: int,
    arch: str, was_patched: bool = False,
    peer_sources: 'Optional[set[str]]' = None,
) -> List[Dict[str, str]]:
    """End-to-end: take one Source and return one synthesized RepoState
    record per binary it would emit.

    `source` is a parsed Source instance (cache.source_hashtable
    entry).  Required attributes: `.package`, `.version`, `.binary`
    (the comma-separated upstream Binary: list).

    `package_universe` is the cache's binary index — a mapping of the
    shape `{binary_name: {version_str: package_record_dict}}` so we
    can pluck the highest-version upstream record for each sibling.
    The wrapper helper :func:`from_cache` packs cache.package_hashtable
    into this shape automatically.

    `asg_ledger` is the published-versions map keyed by binary name
    (the same shape `compute_post_build_versions` expects).  None →
    treat as empty.

    `arch` is the build architecture for filename synthesis (the
    BuildConfig.arch).  We don't actually filter by Architecture: here
    — every binary upstream declared as compatible ships; the closure
    pass handles arch-mismatched targets.

    Returns the list of synthesized records.  Empty list when source
    has no `.binary` field (e.g. `<arch only>` source-only packages).
    """
    _src_name = getattr(source, 'package', None)
    _src_ver = str(getattr(source, 'version', ''))
    if not _src_name or not _src_ver:
        return []
    _binaries: List[str] = list(getattr(source, 'binary', []) or [])
    if not _binaries:
        return []
    _ledger = asg_ledger or {}

    # Step 1 — per-binary upstream resolution.  Real dpkg-buildpackage
    # writes each binary's filename with its OWN Version from
    # debian/control (NOT the source's Version: field).  For
    # metapackages (gcc-defaults source @ 1.213 → gcc binary @
    # 4:12.2.0-3) the difference matters.  Look up each binary's
    # upstream Package record and use ITS Version as the base.  Falls
    # back to source.version when upstream cache doesn't carry the
    # binary (kernel-ABI names, fork-only binaries).
    import apt_pkg
    apt_pkg.init_system()
    _upstream_per_binary: Dict[str, Optional[Dict[str, str]]] = {}
    _base_ver_per_binary: Dict[str, str] = {}
    _emit_binaries: List[str] = []
    for _b in _binaries:
        _upstream_per_binary[_b] = None
        _name_entries = package_universe.get(_b)
        if _name_entries:
            _hi_ver, _hi_rec = next(iter(_name_entries.items()))
            for _v, _r in _name_entries.items():
                try:
                    _cmp = apt_pkg.version_compare(_v, _hi_ver)
                except Exception:
                    _cmp = 0
                if _cmp > 0:
                    _hi_ver, _hi_rec = _v, _r
            # Canonical-source check: the upstream binary's `Source:`
            # field names its REAL producer.  When the canonical
            # producer is ALSO being synthesized in this run
            # (peer_sources knows the scope), skip this binary so the
            # canonical source's synth emits it.  Catches the
            # linux + linux-signed-amd64 over-declaration where one
            # source lists -di udebs in `Binary:` but the other
            # actually emits them.
            #
            # When peer_sources is None (test fixtures, ad-hoc
            # synthesis) OR the canonical source is NOT in scope (we
            # are an Athena fork that legitimately renames + emits
            # upstream-named binaries), DON'T skip — our source IS
            # the producer in that universe.
            _canon_src = _upstream_canonical_source(_hi_rec)
            if (_canon_src and _canon_src != _src_name
                    and peer_sources is not None
                    and _canon_src in peer_sources):
                continue
            _upstream_per_binary[_b] = _hi_rec
            _rec_ver = _hi_rec.get('Version', '') if _hi_rec else ''
            _base_ver_per_binary[_b] = _rec_ver or str(_hi_ver)
        else:
            _base_ver_per_binary[_b] = _src_ver
        _emit_binaries.append(_b)
    # All downstream loops use _emit_binaries (post-canonical-source
    # filter) rather than the raw _binaries list — otherwise we'd
    # still emit synthetic records for binaries belonging to other
    # sources, defeating the filter.
    _binaries = _emit_binaries
    if not _binaries:
        return []

    # Step 2 — pristine base per binary (strip NMU residue).  asg-stamp
    # math + intra-source pin rewriting both work on the pristine.
    _pristine_per_binary: Dict[str, str] = {
        _b: utils.strip_nmu_suffix(_v)
        for _b, _v in _base_ver_per_binary.items()
    }

    # Step 3 — delta + lineage decision, uniform N across siblings.
    # Mirrors BuildContainer._normalize_built_artifacts exactly: any
    # binary triggers delta → all stamp; ledger entry at any sibling's
    # pristine → lineage trigger; N = max(per-binary asg_next_n).
    _any_delta = was_patched
    for _b in _binaries:
        if _pristine_per_binary[_b] != _base_ver_per_binary[_b]:
            _any_delta = True
            break
    _has_lineage = False
    for _b in _binaries:
        for _prev in _ledger.get(_b, []):
            if (utils.pristine_base(_prev) == _pristine_per_binary[_b]
                    and utils.parse_asg_suffix(_prev) is not None):
                _has_lineage = True
                break
        if _has_lineage:
            break

    _virtual_ver_per_binary: Dict[str, str] = {}
    if _any_delta or _has_lineage:
        _candidates = [
            utils.asg_next_n(
                _ledger.get(_b, []), _pristine_per_binary[_b], release)
            for _b in _binaries
        ]
        _n = max(_candidates) if _candidates else 1
        for _b in _binaries:
            _virtual_ver_per_binary[_b] = utils.apply_asg_suffix(
                _pristine_per_binary[_b], release, _n)
    else:
        for _b in _binaries:
            _virtual_ver_per_binary[_b] = _pristine_per_binary[_b]

    # Step 4 — synthesize per-binary records using the per-target
    # pristine map so sibling-pin rewriting catches metapackage cases.
    _out: List[Dict[str, str]] = []
    for _b in _binaries:
        _rec = synthesize_binary_record(
            source_name=_src_name,
            binary_name=_b,
            virtual_version=_virtual_ver_per_binary[_b],
            arch=arch,
            upstream_record=_upstream_per_binary[_b],
            sibling_ver_map=_virtual_ver_per_binary,
            sibling_pristine_map=_pristine_per_binary,
        )
        _out.append(_rec)
    return _out


def from_cache(cache: Any) -> Dict[str, Dict[str, Any]]:
    """Project the Cache's package_hashtable into the shape
    `synthesize_source_binaries` expects.

    Cache's hashtable is `{name: {ver: [Package, ...]}}` (lists because
    of Provides aliasing) — we collapse to `{name: {ver: control_dict}}`
    where control_dict is a flat str→str mapping suitable for
    RepoState consumers.  Picks the first Package per (name, ver)
    (Provides-aliased entries share control fields with the real
    provider).
    """
    _out: Dict[str, Dict[str, Any]] = {}
    _ht = getattr(cache, 'package_hashtable', None)
    if _ht is None:
        return _out
    for _name, _versions in _ht.items():
        _per_name: Dict[str, Dict[str, str]] = {}
        for _ver, _pkgs in _versions.items():
            if not _pkgs:
                continue
            _p = _pkgs[0]
            _ctrl: Dict[str, str] = {}
            # Package is a deb822 subclass — iterate its raw fields.
            try:
                for _field in _p:
                    _val = _p.get(_field)
                    if _val is None:
                        continue
                    _ctrl[_field] = str(_val)
            except Exception:
                continue
            _per_name[str(_ver)] = _ctrl
        if _per_name:
            _out[_name] = _per_name
    return _out


# Module-private regex — left in for potential reuse by future chunks
# (the bump-rewrite path could share this).  Currently unused by chunk
# 2 since PkgRelation handles the parse round-trip.
_PIN_RE = re.compile(r'\(\s*(<=|>=|<<|>>|=)\s*([^)]+?)\s*\)')


# ─────────────────────────── Self-validation against build records ──


def validate_against_build_records(
    source_names: 'List[str]',
    source_lookup: 'Any',  # callable: name -> Source or dict
    package_universe: 'Dict[str, Dict[str, Any]]',
    asg_ledger: 'Optional[Dict[str, List[str]]]', release: int,
    arch: str, buildlog_dir: str,
) -> 'Tuple[Dict[str, Any], List[Tuple[str, str, str]]]':
    """Run the synthesizer against each source for which a successful
    build.json exists on disk; compare the predicted filenames
    against the ACTUAL ``output_hashes`` keys produced by real
    dpkg-buildpackage.

    Returns ``(stats_dict, findings)``.  Stats:
      ``sources_checked``  — sources with a matching build.json
      ``sources_matched``  — synthesizer predictions == real filenames
      ``sources_drifted``  — at least one predicted filename absent
                             from real output, or vice versa

    Findings (per drifted source):
      ``virtual_validate_predicted_missing``  WARNING — real build
            emitted a file the synthesizer did NOT predict (side
            artifact, or upstream Binary: list under-reports)
      ``virtual_validate_predicted_extra``    WARNING — synthesizer
            predicted a file real build did NOT emit (Build-Profile
            filter, arch mismatch)
      ``virtual_validate_version_drift``      CRITICAL — predicted
            filename's name matches a real one but version segment
            differs (asg-stamp math wrong, or upstream-version
            lookup wrong)

    Self-test for the synthesizer; intended to be run after every
    significant successful build round so drift surfaces immediately.
    """
    import utils as _utils
    _findings: 'List[Tuple[str, str, str]]' = []
    _stats: 'Dict[str, int]' = {
        'sources_checked': 0,
        'sources_matched': 0,
        'sources_drifted': 0,
    }
    for _name in source_names:
        _rec = _utils.read_build_record(buildlog_dir, _name)
        if _rec is None:
            continue
        _real_files: 'List[str]' = list(
            (_rec.get('output_hashes') or {}).keys())
        if not _real_files:
            continue
        _stats['sources_checked'] += 1
        _src = source_lookup(_name)
        if _src is None:
            continue
        _was_patched = bool(getattr(_src, 'patch_list', None))
        _virt = synthesize_source_binaries(
            source=_src, package_universe=package_universe,
            asg_ledger=asg_ledger, release=release,
            arch=arch, was_patched=_was_patched,
            peer_sources=set(source_names),
        )
        _pred_files: 'List[str]' = [
            os.path.basename(_r.get('Filename', '') or '')
            for _r in _virt
        ]
        # output_hashes accumulates HISTORY across rebuilds (e.g.
        # glibc carries both +asg1u1 and +asg1u2 of the same binary).
        # The synthesizer predicts ONE-NEXT-BUILD, not history; so
        # compare by binary name (the first `_` segment) instead of
        # full filename.
        def _binary_name(_fn: str) -> str:
            return _fn.split('_', 1)[0] if '_' in _fn else _fn

        _real_names: 'set[str]' = {_binary_name(_f) for _f in _real_files}
        _pred_names: 'set[str]' = {_binary_name(_f) for _f in _pred_files}
        _missing = _real_names - _pred_names   # real has, pred doesn't
        _extra = _pred_names - _real_names     # pred has, real doesn't
        # Version drift: same binary name, but predicted filename
        # doesn't appear in the real output_hashes' history.  A
        # plausible prediction is one of the historic versions; if
        # NONE match, the asg-stamp math is wrong.
        _real_by_name: 'Dict[str, set[str]]' = {}
        for _fn in _real_files:
            _real_by_name.setdefault(_binary_name(_fn), set()).add(_fn)
        _version_drift: 'List[Tuple[str, str, str]]' = []
        for _bn in _real_names & _pred_names:
            _pred_match = [_f for _f in _pred_files
                           if _binary_name(_f) == _bn]
            _real_match = _real_by_name.get(_bn, set())
            if not any(_p in _real_match for _p in _pred_match):
                _version_drift.append((
                    _bn,
                    ','.join(sorted(_real_match)[:3]),
                    ','.join(_pred_match[:3])))
        if not _missing and not _extra and not _version_drift:
            _stats['sources_matched'] += 1
            continue
        _stats['sources_drifted'] += 1
        for _bn, _real_str, _pred_str in _version_drift[:5]:
            _findings.append((
                'CRITICAL', 'virtual_validate_version_drift',
                f"{_name}/{_bn}: predicted={_pred_str!r} but built "
                f"history has {_real_str!r}"))
        if _missing:
            _findings.append((
                'WARNING', 'virtual_validate_predicted_missing',
                f"{_name}: real build emitted {len(_missing)} "
                "binary name(s) synthesizer did NOT predict — first: "
                f"{next(iter(sorted(_missing)))}"))
        if _extra:
            _findings.append((
                'WARNING', 'virtual_validate_predicted_extra',
                f"{_name}: synthesizer predicted {len(_extra)} binary "
                "name(s) real build did NOT emit — first: "
                f"{next(iter(sorted(_extra)))}"))
    return _stats, _findings


# ─────────────────────────── RepoState assembly ──────────────────────


def synthesize_repo_state(
    virtual_records: 'List[Dict[str, str]]',
) -> 'Tuple[Any, List[Tuple[str, str, str]]]':
    """Project a list of synthesized binary records into a RepoState
    object that the existing repo_audit functions can consume.

    Returns ``(repo_state, findings)``.  Findings carry
    ``(severity, kind, message)`` triples matching the mirror-audit
    convention so the CLI layer can render them uniformly.

    Behaviour mirrors :func:`repo_audit.scan_repo_state`'s parser:
      - on duplicate Package names, the highest-Version record wins
        (apt-style) — silently for the common kernel-signed chain
        pattern (linux + linux-signed-amd64 both declare the same
        ``*-di`` udebs in ``Binary:``; in real builds only `linux`
        emits them).  A single summary INFO records the count so the
        operator knows dedup happened.
      - missing required fields → CRITICAL ``virtual_invalid_record``
      - provides_index built via the same `_build_provides_index`
        helper repo_audit uses, so downstream audits see virtual
        records the same way they see scanned ones
    """
    import apt_pkg
    from repo_audit import RepoState, _build_provides_index
    apt_pkg.init_system()

    _findings: 'List[Tuple[str, str, str]]' = []
    _packages: 'Dict[str, Dict[str, str]]' = {}
    _dup_count = 0
    _dup_sources: 'set[tuple[str, str]]' = set()
    for _r in virtual_records:
        _name = _r.get('Package', '')
        _ver = _r.get('Version', '')
        if not _name or not _ver:
            _findings.append((
                'CRITICAL', 'virtual_invalid_record',
                f"synthesized record missing Package/Version: "
                f"{_r!r}"))
            continue
        _prev = _packages.get(_name)
        if _prev is not None:
            _prev_ver = _prev.get('Version', '')
            try:
                _cmp = apt_pkg.version_compare(_ver, _prev_ver)
            except Exception:
                _cmp = 0
            _dup_count += 1
            _src_pair = tuple(sorted([
                _prev.get('Source', '?'), _r.get('Source', '?')]))
            _dup_sources.add(_src_pair)  # type: ignore[arg-type]
            if _cmp <= 0:
                continue
        _packages[_name] = dict(_r)
    if _dup_count:
        _pair_preview = ', '.join(
            f"{_a}+{_b}" for _a, _b in sorted(_dup_sources)[:3])
        _more = ''
        if len(_dup_sources) > 3:
            _more = f" (+{len(_dup_sources) - 3} more pairs)"
        _findings.append((
            'INFO', 'virtual_duplicate_name',
            f"deduped {_dup_count} cross-source binary name(s) "
            f"across {len(_dup_sources)} source-pair(s): "
            f"{_pair_preview}{_more} — kept highest version "
            "(expected for kernel-signed / installer-udeb chains)"))
    _provides = _build_provides_index(_packages)
    _state = RepoState(
        packages=_packages, provides_index=_provides,
        packages_file='<virtual>', repo_mtime=0.0,
    )
    return _state, _findings


def virtual_repo_audit(
    virtual_records: 'List[Dict[str, str]]',
    install_corpus: 'Optional[frozenset[str]]' = None,
    live_cohort: 'Optional[frozenset[str]]' = None,
    installer_cohort: 'Optional[frozenset[str]]' = None,
) -> 'Tuple[Any, List[Tuple[str, str, str]]]':
    """Assemble a virtual RepoState and run the real audit primitives
    against it.  Returns ``(repo_state, findings)`` so the caller can
    chain into virtual_publish_dry_run (chunk 4) without re-scanning.

    `install_corpus` is the union of binary names that would be
    dpkg-installed somewhere in the live chroot / installer / pool —
    same shape `audit_dep_closure` expects.  Pass `None` to audit every
    binary in the synthetic state (whole-repo scan; useful for
    standalone "would this corpus install?" checks).

    `live_cohort` / `installer_cohort` are passed to
    :func:`repo_audit.audit_conflict_cohort` — Conflict/Breaks within
    each cohort fire ``CRITICAL virtual_cohort_conflict``.  Omit either
    to skip its conflict pass.

    Closure breaks → ``CRITICAL virtual_closure_break``.
    Weak (Recommends) misses → ``WARNING virtual_recommends_miss``
    (informational only; matches real-pipeline policy where weak deps
    don't gate publish).
    """
    from repo_audit import audit_dep_closure, audit_conflict_cohort

    _state, _findings = synthesize_repo_state(virtual_records)

    _unresolved, _weak = audit_dep_closure(
        _state, consumer_set=install_corpus)
    for _pkg, _field, _rel, _why in _unresolved:
        _findings.append((
            'CRITICAL', 'virtual_closure_break',
            f"{_pkg}: {_field} = {_rel!r} — {_why}"))
    for _pkg, _field, _rel in _weak:
        _findings.append((
            'WARNING', 'virtual_recommends_miss',
            f"{_pkg}: {_field} = {_rel!r}"))

    if live_cohort:
        _live_conflicts = audit_conflict_cohort(_state, live_cohort)
        for _pkg, _field, _other, _rel in _live_conflicts:
            _findings.append((
                'CRITICAL', 'virtual_cohort_conflict',
                f"{_pkg} (live): {_field} = {_rel!r} → {_other}"))
    if installer_cohort:
        _inst_conflicts = audit_conflict_cohort(_state, installer_cohort)
        for _pkg, _field, _other, _rel in _inst_conflicts:
            _findings.append((
                'CRITICAL', 'virtual_cohort_conflict',
                f"{_pkg} (installer): {_field} = {_rel!r} → {_other}"))
    return _state, _findings


# ─────────────────────────── Claim ledger + publish dry-run ──────────


def synthesize_claim_ledger(
    virtual_records: 'List[Dict[str, str]]', our_builder_id: str,
    snapshot: str, seq_start: int = 1,
    built_at: str = '1970-01-01T00:00:00Z',
) -> 'Dict[str, List[Dict[str, Any]]]':
    """One synthetic claim per virtual binary, all owned by
    `our_builder_id`.  Returns the by_builder dict shape that
    coord.store / coord.reconcile / mirror.audit_* consume.

    `seq_start` lets the caller chain claims onto an existing remote
    ledger (a real publish would do `len(existing) + 1`).

    `built_at` is a deterministic placeholder — virtual builds have no
    wall-clock event; tests pin it to avoid Date.now() flakiness.
    Mirror audit ignores this field for gate decisions.
    """
    from coord import schema as _sch
    _claims: 'List[Dict[str, Any]]' = []
    _seq = seq_start
    for _r in virtual_records:
        _name = _r.get('Package', '')
        _ver = _r.get('Version', '')
        _fn = os.path.basename(_r.get('Filename', '') or '')
        _sha = _r.get('SHA256', '')
        if not _name or not _ver or not _fn or not _sha:
            continue
        try:
            _size = int(_r.get('Size', 0) or 0)
        except (TypeError, ValueError):
            _size = 0
        _claim = _sch.new_claim(
            builder=our_builder_id, seq=_seq, package=_name,
            intended_version=_ver, built_version=_ver,
            filename=_fn, sha256=_sha, size=_size,
            snapshot=snapshot, built_at=built_at,
            claim_state=_sch.CLAIM_STATE_PUBLISHED,
        )
        _claims.append(_claim)
        _seq += 1
    return {our_builder_id: _claims}


def virtual_publish_dry_run(
    virtual_records: 'List[Dict[str, str]]', our_builder_id: str,
    snapshot: str,
    remote_by_builder: 'Optional[Dict[str, List[Dict[str, Any]]]]' = None,
) -> 'Tuple[Dict[str, List[Dict[str, Any]]], List[Tuple[str, str, str]]]':
    """Project the publish that virtual-build would attempt and run
    the real publish-time gates against the projection.

    Returns ``(merged_by_builder, findings)`` where ``merged_by_builder``
    is our synthetic claims unioned with `remote_by_builder` (the
    last-fetched view of each peer's sidecar).  Findings:

      ``virtual_hash_conflict``       CRITICAL — same filename + sha
                                      across builders disagree (real
                                      ``detect_hash_conflicts`` raised
                                      it)
      ``virtual_ownership_blocked``   CRITICAL — our claim's filename is
                                      currently owned by another builder
                                      AND we're not strictly higher
                                      version (the chunk-8 ownership
                                      rule from the MIRROR-02 plan)
      ``virtual_ownership_transfer``  INFO — we'd take ownership of a
                                      currently-tunneled or
                                      lower-version filename; not a
                                      block, just visibility

    When `remote_by_builder` is None, ownership and cross-builder hash
    checks are skipped (only intra-our-claims hash-conflict can fire,
    which never does because synthetic SHAs are deterministic by
    triple — but the call is still made so a regression breaks loudly).
    """
    from coord import reconcile as _reconcile
    from coord import store as _store
    import apt_pkg
    apt_pkg.init_system()

    _findings: 'List[Tuple[str, str, str]]' = []
    _merged: 'Dict[str, List[Dict[str, Any]]]' = {}
    if remote_by_builder:
        for _bid, _claims in remote_by_builder.items():
            _merged[_bid] = list(_claims)

    # Skip virtual records for filenames already published by us on
    # the remote.  A real publish wouldn't re-emit them (the .deb
    # already exists on the mirror at our claim's sha) — and
    # generating a NEW synthetic claim with a DIFFERENT (synthetic)
    # sha for the same filename would fire detect_hash_conflicts as
    # "athena-primary vs athena-primary" — a false-positive specific
    # to the dry-run model.  Real ownership decisions only apply to
    # filenames we'd actually push.
    _existing = _merged.get(our_builder_id, [])
    _existing_fns: 'set[str]' = {
        str(_c.get('filename') or '')
        for _c in _existing
        if _c.get('claim_state') != 'retracted'
    }
    _new_records = [
        _r for _r in virtual_records
        if os.path.basename(_r.get('Filename', '') or '')
        not in _existing_fns
    ]
    _seq_floor = (max((_c.get('seq', 0) for _c in _existing),
                      default=0) + 1)
    _ours_renumbered = synthesize_claim_ledger(
        _new_records, our_builder_id, snapshot,
        seq_start=_seq_floor)
    _merged[our_builder_id] = _existing + _ours_renumbered[our_builder_id]

    # Hash-conflict scan — real reconcile, virtual input.
    _hc = _reconcile.detect_hash_conflicts(_merged)
    for _f in _hc:
        if _f.severity == 'CRITICAL':
            _findings.append((
                'CRITICAL', 'virtual_hash_conflict',
                f"{_f.kind}: {_f.message}"))

    # Ownership decision per virtual claim, against pre-merge state.
    if remote_by_builder is not None:
        _pre_owners = _store.project_owners(remote_by_builder)
        for _claim in _ours_renumbered[our_builder_id]:
            _fn = _claim['filename']
            _our_ver = _claim['built_version']
            _owner = _pre_owners.get(_fn)
            if _owner is None:
                continue   # no existing claim, free to take
            _owner_builder = _owner.get('builder')
            _owner_ver = _owner.get('version', '')
            if _owner_builder is None:
                _findings.append((
                    'INFO', 'virtual_ownership_transfer',
                    f"{_fn}: tunneled on mirror — virtual publish "
                    "would take ownership"))
                continue
            if _owner_builder == our_builder_id:
                continue
            try:
                _cmp = apt_pkg.version_compare(_our_ver, _owner_ver)
            except Exception:
                _cmp = 0
            if _cmp > 0:
                _findings.append((
                    'INFO', 'virtual_ownership_transfer',
                    f"{_fn}: currently owned by {_owner_builder} "
                    f"@ {_owner_ver}; virtual publish at {_our_ver} "
                    "would transfer ownership (higher version)"))
            else:
                _findings.append((
                    'CRITICAL', 'virtual_ownership_blocked',
                    f"{_fn}: owned by {_owner_builder} @ {_owner_ver}; "
                    f"virtual publish at {_our_ver} would be REFUSED "
                    "(not strictly higher)"))
    return _merged, _findings
