"""CycloneDX 1.5 SBOM generation.

Walks `dep_tree.selected_srcs` (∪ udeb tree) and emits a CycloneDX 1.5
JSON document listing every source package that contributed to the
build, with provenance tied back to the upstream archive + the local
patch set.

Per-source component carries:
  - upstream name + version (post-cache, pre-strip — the version the
    .dsc was parsed at)
  - PURL identifier `pkg:deb/<base-id>/<name>@<version>` (CycloneDX
    Debian convention)
  - DSC sha256 hash when present (links to the exact source archive)
  - patch count + patch-set hash (`utils.patch_set_hash` over the
    sorted *.patch files in patch/source/<name>/<version>/); a pristine
    source (no patches) carries the canonical empty-input SHA-256 digest,
    not a literally empty string

Top-level `metadata.component` records distribution + version +
codename + arch + snapshot timestamp so consumers can correlate
against a particular Asgard build.

Output schema validates against the public CycloneDX 1.5 JSON
schema — `bomFormat`, `specVersion`, `serialNumber`, `version`,
`metadata.timestamp`, `metadata.component`, and `components` are all
present and well-typed.

Generator only — wire into operator surface via build.py's
`cmd_sbom`.  Default output path matches the ISO basename so the
SBOM ships alongside the ISO it describes.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import uuid as _uuid
from typing import Any, Dict, List, Optional

import utils

logger = logging.getLogger('athena')  # cross-stage helper → log tab


CDX_BOM_FORMAT = 'CycloneDX'
CDX_SPEC_VERSION = '1.5'


def _purl_for_source(baseid: str, name: str, version: str) -> str:
    """CycloneDX-compatible package URL for a Debian source.

    Per the PURL spec deb type:
        pkg:deb/<vendor>/<name>@<version>
    See https://github.com/package-url/purl-spec — deb type definition."""
    return f'pkg:deb/{baseid}/{name}@{version}'


def _dsc_sha256(src: Any) -> str:
    """Find the source's .dsc file and return its sha256, or '' if
    absent.  `src.files` is keyed by filename; the .dsc is always
    present for a parsed source but the sha256 field may be empty
    if upstream's Checksums-Sha256 wasn't populated."""
    for _fn, _meta in (getattr(src, 'files', None) or {}).items():
        if _fn.endswith('.dsc'):
            return str(_meta.get('sha256') or '')
    return ''


def _list_patch_files(patch_dir: str) -> List[str]:
    """Sorted *.patch filenames in patch_dir, or [] when the dir
    doesn't exist (pristine source — no fork patches)."""
    try:
        return sorted(
            _f for _f in os.listdir(patch_dir)
            if _f.endswith('.patch')
        )
    except OSError:
        return []


def _component_for_source(src: Any, baseid: str,
                          patch_root: str) -> Dict[str, Any]:
    """Build one CycloneDX component dict from a Source instance."""
    _name = str(src.package)
    _version = str(src.version)

    _comp: Dict[str, Any] = {
        'type':    'library',
        'name':    _name,
        'version': _version,
        'purl':    _purl_for_source(baseid, _name, _version),
    }

    _sha = _dsc_sha256(src)
    if _sha:
        _comp['hashes'] = [{'alg': 'SHA-256', 'content': _sha}]

    # Patch provenance — 0 count + the canonical empty-input SHA-256 digest
    # (not a literally empty string) for pristine sources.
    _pkg_patch_dir = os.path.join(
        patch_root, _name, utils.version_no_epoch(src.version),
    )
    _patch_files = _list_patch_files(_pkg_patch_dir)
    # The version-independent prebuild script shapes the build like a
    # patch — same hash fold as compose_recipe/_refresh_patches, plus its
    # own property so the SBOM discloses the intervention explicitly.
    _pb_path = utils.prebuild_script_path(patch_root, _name)
    _pb_exists = os.path.isfile(_pb_path)
    _patch_hash = utils.patch_set_hash(
        _pkg_patch_dir, _patch_files,
        prebuild_path=_pb_path if _pb_exists else None)
    _props: List[Dict[str, str]] = [
        {'name': 'athena:patch-count',    'value': str(len(_patch_files))},
        {'name': 'athena:patch-set-hash', 'value': _patch_hash},
    ]
    if _patch_files:
        _props.append({
            'name':  'athena:patch-files',
            'value': ', '.join(_patch_files),
        })
    if _pb_exists:
        _props.append({'name': 'athena:prebuild', 'value': 'prebuild.sh'})
    _comp['properties'] = _props
    return _comp


def _resolve_snapshot_ts(buildconfig: Any, container: Any) -> str:
    """Best-effort snapshot timestamp.  Prefer the container's
    already-resolved value (BuildContainer.__init__ memoises it);
    fall back to a fresh resolve from config."""
    _ts = getattr(container, 'snapshot_ts', '') if container is not None else ''
    if _ts:
        return str(_ts)
    try:
        return str(utils.resolve_snapshot_timestamp(buildconfig) or '')
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"sbom: cannot resolve snapshot timestamp: {e}")
        return ''


def generate_cdx(buildconfig: Any,
                 dep_tree: Any,
                 udeb_dep_tree: Optional[Any] = None,
                 out_path: str = '',
                 container: Any = None) -> str:
    """Emit a CycloneDX 1.5 SBOM at `out_path`.

    Walks the deduped union of `dep_tree.selected_srcs` and
    `udeb_dep_tree.selected_srcs`; one component per source.  Returns
    the path on success, '' on write failure.

    `container=` is optional; when supplied the snapshot timestamp is
    read from `container.snapshot_ts` instead of re-resolving.
    `out_path` must be set — the caller (cmd_sbom) chooses the
    location.  Empty path → empty return (no-op, no write)."""
    if not out_path:
        logger.error("sbom.generate_cdx: out_path is empty")
        return ''

    # Deduped source set — udeb-only sources merge on top of the deb tree.
    _srcs: Dict[str, Any] = {}
    for _n, _s in getattr(dep_tree, 'selected_srcs', {}).items():
        _srcs[_n] = _s
    if udeb_dep_tree is not None:
        for _n, _s in getattr(udeb_dep_tree, 'selected_srcs', {}).items():
            _srcs.setdefault(_n, _s)

    _baseid     = buildconfig.build_base_id
    _patch_root = buildconfig.dir_patch_source

    # build_distribution / build_version / build_codename are already
    # _strip_quotes'd when BuildConfig loads them (utils.py) — the canonical
    # stripping site — so use them directly here (str() for safety only).
    _meta_component: Dict[str, Any] = {
        'type':    'operating-system',
        'name':    str(buildconfig.build_distribution),
        'version': str(buildconfig.build_version),
        'properties': [
            {'name': 'athena:codename',
             'value': str(buildconfig.build_codename)},
            {'name': 'athena:arch',
             'value': str(buildconfig.arch)},
            {'name': 'athena:base-id',
             'value': str(_baseid)},
            {'name': 'athena:snapshot-timestamp',
             'value': _resolve_snapshot_ts(buildconfig, container)},
        ],
    }

    _components = [
        _component_for_source(_s, _baseid, _patch_root)
        for _s in sorted(_srcs.values(), key=lambda s: str(s.package))
    ]

    _bom: Dict[str, Any] = {
        'bomFormat':    CDX_BOM_FORMAT,
        'specVersion':  CDX_SPEC_VERSION,
        'serialNumber': f'urn:uuid:{_uuid.uuid4()}',
        'version':      1,
        'metadata': {
            # CycloneDX requires UTC ISO-8601 with seconds precision.
            'timestamp': datetime.datetime.now(
                datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'component': _meta_component,
            'tools': [{
                'vendor': 'Athena Build',
                'name':   'sbom.generate_cdx',
            }],
        },
        'components': _components,
    }

    try:
        _parent = os.path.dirname(out_path)
        if _parent:
            os.makedirs(_parent, exist_ok=True)
        # Atomic write so a crash mid-write can't leave a truncated SBOM
        # (low stakes — the SBOM is a regenerable artifact).
        utils._atomic_write_bytes(
            out_path,
            (json.dumps(_bom, indent=2) + '\n').encode('utf-8'),
            mode=utils._existing_mode(out_path),
        )
    except OSError as e:
        logger.error(f"sbom: cannot write {out_path}: {e}")
        return ''
    logger.info(
        f"sbom: emitted {out_path} ({len(_components)} component(s))"
    )
    return out_path
