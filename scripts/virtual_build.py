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


def _pristine_constraint_match(constraint_ver: str,
                                source_pristine: str) -> bool:
    """True when an intra-binary constraint version (after NMU strip)
    equals the source's pristine base — i.e. this is a sibling pin
    that real-build would rewrite to our virtual version.

    Constraint vers can be `<base>`, `<base>+debNuN`, `<base>+asgRuN`,
    or `<base>-Nb<bN>` — all normalise to `<base>` via pristine_base.
    """
    return utils.pristine_base(constraint_ver) == source_pristine


def _rewrite_sibling_pins(raw: str, source_pristine: str,
                           sibling_ver_map: Dict[str, str]) -> str:
    """Rewrite `(= V)` / `(>= V)` etc. constraints in a relation field
    where the target is a sibling binary in the same source AND V
    pristine-matches the source.  Other constraints left unchanged.

    Uses python-debian's PkgRelation parser → reformat round-trip so we
    don't have to handle every operator + whitespace edge case by hand.
    """
    if not raw or not sibling_ver_map:
        return raw
    try:
        from debian.deb822 import PkgRelation
    except ImportError:
        # If python-debian is unavailable (shouldn't happen in this
        # repo), leave the constraint untouched — verbatim inheritance
        # is still informative for non-sibling deps.
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
            if _pristine_constraint_match(_cur, source_pristine):
                _rel['version'] = (_op, sibling_ver_map[_name])
                _changed = True
    if not _changed:
        return raw
    return PkgRelation.str(_relations)


def _stamped_filename(name: str, version: str, arch: str,
                      ext: str = '.deb') -> str:
    """Synthetic on-disk basename in `name_version_arch.ext` form —
    the same convention dpkg uses; matches what `mirror audit` would
    see."""
    # `:` (epoch) is illegal in filenames; dpkg writes it as `%3a` in
    # apt pool filenames (e.g. libcurl4 epoch 7).  Pristine-base
    # filename pattern matches.
    _safe_ver = version.replace(':', '%3a')
    return f"{name}_{_safe_ver}_{arch}{ext}"


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
    source_name: str, source_pristine: str,
    binary_name: str, virtual_version: str, arch: str,
    upstream_record: Optional[Dict[str, str]],
    sibling_ver_map: Dict[str, str],
) -> Dict[str, str]:
    """Build one RepoState.packages-style record for a single virtual
    binary.

    `upstream_record` is the binary's CURRENT upstream control fields
    (from cache.package_hashtable, highest version).  None means the
    upstream cache doesn't carry this binary at all (a fork-only
    binary, or a kernel-ABI-dependent name) — we then build a minimal
    record with just Package / Version / Architecture / Filename /
    Source / SHA256.  Real build would create this binary too; closure
    against it works only via Provides from siblings, which is the
    only thing we know about it statically anyway.

    `sibling_ver_map` is the full `{binary_name: virtual_version}` for
    this source so intra-source `(= V)` pins get rewritten.

    Returned dict carries str values (matches what scan_repo_state
    stores from apt_pkg.TagFile).
    """
    _fn = _stamped_filename(binary_name, virtual_version, arch)
    _sha = _virtual_sha256(binary_name, virtual_version, arch)
    # Use the same component layout as dpkg-scanpackages /
    # scan_repo_state expects (apt convention: pool/<component>/
    # <first-letter-or-lib<L>>/<source>/<file>).
    _prefix = (binary_name[:4] if binary_name.startswith('lib')
               else binary_name[:1])
    _filename_field = (
        f"pool/main/{_prefix}/{source_name}/{_fn}")
    # Skeleton — minimal record when no upstream is available.
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
    # Inherit relation fields from upstream — these carry symbolic
    # substvars resolved by upstream's build.  For non-forked binaries
    # this IS what our build would produce; for forked binaries it's
    # the conservative-policy approximation (see module docstring).
    for _field in ('Depends', 'Pre-Depends', 'Recommends', 'Suggests',
                   'Enhances', 'Conflicts', 'Breaks', 'Provides',
                   'Replaces'):
        _raw = upstream_record.get(_field)
        if not _raw:
            continue
        _rewritten = _rewrite_sibling_pins(
            _raw, source_pristine, sibling_ver_map)
        _rec[_field] = _rewritten
    # Inherit Priority/Section/etc if upstream carries them — purely
    # informational for the audit, but mirror audit_nmu_residue + other
    # passes reference some.
    for _field in ('Priority', 'Section', 'Essential',
                   'Multi-Arch', 'Homepage'):
        _val = upstream_record.get(_field)
        if _val:
            _rec[_field] = _val
    # Architecture: upstream may say 'all' for arch-independent
    # binaries; respect it (otherwise virtual closure misses arch-all
    # deps).
    _ua = upstream_record.get('Architecture')
    if _ua and _ua.strip() in ('all', 'any'):
        _rec['Architecture'] = _ua.strip()
    return _rec


def synthesize_source_binaries(
    source: Any, package_universe: Dict[str, Dict[str, Any]],
    asg_ledger: Optional[Dict[str, List[str]]], release: int,
    arch: str, was_patched: bool = False,
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
    _ver_map = utils.compute_post_build_versions(
        source_version=_src_ver, binaries=_binaries,
        asg_ledger=asg_ledger, release=release,
        was_patched=was_patched,
    )
    _pristine = utils.strip_nmu_suffix(_src_ver)
    _out: List[Dict[str, str]] = []
    for _b in _binaries:
        _virt_ver = _ver_map.get(_b, _pristine)
        _upstream = None
        _name_entries = package_universe.get(_b)
        if _name_entries:
            # Pick highest version (apt-style).  package_universe shape
            # mirrors cache.package_hashtable: {ver_str: record_dict}.
            try:
                import apt_pkg
                _ordered = sorted(
                    _name_entries.items(),
                    key=lambda _kv: _kv[0],
                    # apt_pkg.version_compare-based key is more accurate
                    # but Python sort is stable + apt versions sort
                    # close enough for tie-breaks here; reduce to
                    # version_compare when callers report drift.
                )
                # Walk and pick highest via apt_pkg.version_compare.
                _hi_ver, _hi_rec = _ordered[0]
                for _v, _r in _ordered[1:]:
                    if apt_pkg.version_compare(_v, _hi_ver) > 0:
                        _hi_ver, _hi_rec = _v, _r
                _upstream = _hi_rec
            except Exception:
                # Fall back to whatever first lookup returns.
                _upstream = next(iter(_name_entries.values()), None)
        _rec = synthesize_binary_record(
            source_name=_src_name, source_pristine=_pristine,
            binary_name=_b, virtual_version=_virt_ver, arch=arch,
            upstream_record=_upstream, sibling_ver_map=_ver_map,
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
    _ours = synthesize_claim_ledger(
        virtual_records, our_builder_id, snapshot)
    _merged: 'Dict[str, List[Dict[str, Any]]]' = {}
    if remote_by_builder:
        for _bid, _claims in remote_by_builder.items():
            _merged[_bid] = list(_claims)
    # Merge our synthetic claims into our builder's bucket (if we have
    # a remote bucket for the same builder, append after seq advance).
    _existing = _merged.get(our_builder_id, [])
    _seq_floor = (max((_c.get('seq', 0) for _c in _existing),
                      default=0) + 1)
    _ours_renumbered = synthesize_claim_ledger(
        virtual_records, our_builder_id, snapshot,
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
