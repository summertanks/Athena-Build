"""Source-package emit engine — MAT-02 stage 2 (docs/plans/
mat-02-source-packages.md).

Produces the PUBLISHED source package for a built source, per the plan's
design decisions:

  D1  publish class: PRISTINE (version unchanged, unpatched, no
      Debian-layer literals in any relation field) → the upstream
      `.dsc` + tarballs republished BYTE-VERBATIM (provenance: Debian's
      signature survives).  Everything else → RE-EMIT via
      `dpkg-source -b` from the exact tree the binaries were built from.
  D2  the emitted source's version == its binaries' version sans any
      force-+bN (bump.source_package_version).
  D4  the emitted `debian/control` (and hence the `.dsc`) has EVERY
      literal relation constraint transposed — runtime fields AND
      Build-Depends*/Build-Conflicts* (bump.transpose_source_relations);
      substvars pass through.
  D4b native/non-native awareness: helpers to read an os-release ID and
      decide whether the transpose pipeline applies (`debian` container
      → transpose ON; the native distribution → OFF).  Wiring into the
      build path lands with the per-build emit integration.

Determinism (oracle-proven over 1007 sources, 2026-07-12): the
synthesized changelog entry reuses the UPSTREAM changelog's top-entry
date (stable across builders — REQUIRED: two federation builders
emitting the same source must produce byte-identical files) and a
distribution-stable maintainer identity; `dpkg-source -b` runs under a
SOURCE_DATE_EPOCH pinned to that same date.  `.orig.tar.*` is always
passed through verbatim, never regenerated.

Standalone: no session state; needs `dpkg-source` on the host (preflight
gates dpkg-dev) and the python3-debian Dsc parser.
"""
import email.utils
import glob
import hashlib
import os
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional

from bump import (
    source_package_version,
    transpose,
    transpose_source_relations,
)

# Debian-layer literal in a relation bound — the D1 demotion probe and the
# reemit trigger beyond version/patches.  Mirrors what
# transpose_source_relations would rewrite (modern + legacy + backport).
_DEB_LAYER_BOUND_RE = re.compile(
    r'\(\s*(?:<=|>=|<<|>>|=)\s*[^)]*(?:[+~]deb\d+u\d+|\+b\d+|~bpo\d+\+\d+'
    r'|\db\d*\s*\))')

# ` -- Name <email>  Date` — the changelog trailer line carrying the entry
# date; the FIRST one in the file belongs to the top (newest) entry.
_CHANGELOG_TRAILER_RE = re.compile(r'^ -- .+?>  (.+)$', re.MULTILINE)


class SourceEmitError(Exception):
    """Emit failed in a way the caller must handle (bad input, tool error,
    same-name byte conflict in the output dir)."""


def _sha256(path: str) -> str:
    _h = hashlib.sha256()
    with open(path, 'rb') as _fh:
        for _chunk in iter(lambda: _fh.read(1 << 20), b''):
            _h.update(_chunk)
    return _h.hexdigest()


def parse_dsc(dsc_path: str) -> 'Dict[str, Any]':
    """Parse a (possibly clearsigned) .dsc → {source, version, format,
    files} where files is the referenced basenames (tarballs).  Raises
    SourceEmitError on a malformed file."""
    from debian import deb822
    try:
        with open(dsc_path, encoding='utf-8', errors='replace') as _fh:
            _d = deb822.Dsc(_fh)
    except Exception as _e:                                 # noqa: BLE001
        raise SourceEmitError(f'unparseable dsc {dsc_path}: {_e}') from _e
    _src = _d.get('Source')
    _ver = _d.get('Version')
    if not _src or not _ver:
        raise SourceEmitError(f'{dsc_path}: missing Source/Version')
    _files = [_row.get('name') for _row in (_d.get('Files') or [])
              if _row.get('name')]
    return {'source': _src, 'version': _ver,
            'format': _d.get('Format', ''), 'files': _files,
            'body': _d.dump()}


def patch_level_from_version(version: str) -> int:
    """The uniform patch level P encoded in a shipped version's `+pP` tail
    (0 when absent) — how the backfill recovers the P a build carried
    without re-deriving the patch-set history."""
    _m = re.search(r'\+p(\d+)', version or '')
    return int(_m.group(1)) if _m else 0


def patch_level_from_outputs(outputs: 'List[str]') -> int:
    """The uniform +pP carried by a record's OUTPUT filenames (0 when
    absent) — the shipped truth.  Older records' intended_version predates
    the transpose stamp (bash: intended 5.2.15-2, shipped
    bash_5.2.15-2+asg1u0+p1_amd64.deb), so filename versions are the
    authority; P is uniform per source, so max() is exact."""
    from bump import parse_deb_filename
    _p = 0
    for _f in outputs or []:
        _r = parse_deb_filename(os.path.basename(_f))
        if _r is not None:
            _p = max(_p, patch_level_from_version(_r[1]))
    return _p


def find_dsc(source: str, search_dirs: 'List[str]') -> 'Optional[str]':
    """Locate `<source>_*.dsc` across *search_dirs* (upstream `source/`,
    fork `fork/source/repo/`).  Multiple versions → the highest (Debian
    version compare via python-debian, no apt_pkg init needed)."""
    _hits: 'List[str]' = []
    for _d in search_dirs:
        _hits.extend(glob.glob(os.path.join(_d, f'{source}_*.dsc')))
    if not _hits:
        return None
    if len(_hits) == 1:
        return _hits[0]
    from debian.debian_support import Version as _V

    def _ver(_p: str) -> '_V':
        _raw = os.path.basename(_p)[len(source) + 1:-len('.dsc')]
        return _V(_raw.replace('%3a', ':'))
    return max(_hits, key=_ver)


def apply_emit_to_record(record: 'Dict[str, Any]',
                         result: 'Dict[str, Any]') -> 'Dict[str, Any]':
    """Fold an emit result into a build record — under SEPARATE keys
    (`source_outputs` / `source_output_hashes` / `source_emit_class`), never
    `outputs`/`output_hashes`: every existing consumer of those (chroot
    preflight, verify/refresh_output_hashes, reclaim's disk walk) locates
    files through the .deb destination map and would misread a `.dsc`.
    Claims/ledger read the new keys deliberately (MAT-02 stage 5).
    Returns the mutated record (caller re-signs via write_build_record)."""
    record['source_outputs'] = sorted(result.get('files') or {})
    record['source_output_hashes'] = dict(result.get('files') or {})
    record['source_emit_class'] = result.get('class', '')
    return record


def patches_for(patch_root: str, source: str, version: str) -> 'List[str]':
    """Our patch set for (source, version): sorted
    `patch/source/<src>/<version-noepoch>/*.patch` — the same set the build
    applies.  Empty when unpatched."""
    _noepoch = version.split(':', 1)[-1]
    return sorted(os.path.abspath(_p) for _p in glob.glob(
        os.path.join(patch_root, source, _noepoch, '*.patch')))


def classify(dsc_info: 'Dict[str, Any]', patched: bool,
             prefix: str, release: int) -> str:
    """'verbatim' or 'reemit' per D1 (with the demotion rule): verbatim ONLY
    when the version is already the published version (no trailing
    redistribution token), the source is unpatched, AND no relation field
    carries a Debian-layer literal (measured 2026-07-12: zero live
    demotions; guard for future snapshots)."""
    if patched:
        return 'reemit'
    _ver = dsc_info['version']
    if transpose(_ver, prefix, release) != _ver:
        return 'reemit'
    _body = dsc_info.get('body', '')
    _rewritten, _n = transpose_source_relations(_body, prefix, release)
    if _n:
        return 'reemit'
    return 'verbatim'


# `+++ b/<path>` targets of a unified diff — the fold classifier: a patch
# touching upstream files cannot ride out-of-band in a 3.0 (quilt) source
# (dpkg-source -b refuses "unexpected upstream changes"); it must fold into
# debian/patches + series.  debian/-only patches stay out-of-band (a series
# patch may not modify debian/).
_DIFF_TARGET_RE = re.compile(r'^\+\+\+ (\S+)', re.MULTILINE)


def _patch_targets(patch_text: str) -> 'List[str]':
    _out = []
    for _t in _DIFF_TARGET_RE.findall(patch_text):
        if _t == '/dev/null':
            continue
        _t = re.sub(r'^[ab]/', '', _t)
        _out.append(_t)
    return _out


def _changelog_top_date(changelog_text: str) -> 'Optional[str]':
    _m = _CHANGELOG_TRAILER_RE.search(changelog_text)
    return _m.group(1).strip() if _m else None


def _place(src_path: str, out_dir: str) -> 'tuple[str, str]':
    """Copy src_path into out_dir (same basename).  Append-only: an existing
    same-name file must be byte-identical (return its sha, no rewrite);
    different bytes raise (segregate invariant — never silently replace).
    Returns (basename, sha256)."""
    _base = os.path.basename(src_path)
    _dst = os.path.join(out_dir, _base)
    _src_sha = _sha256(src_path)
    if os.path.isfile(_dst):
        _dst_sha = _sha256(_dst)
        if _dst_sha != _src_sha:
            raise SourceEmitError(
                f'{_base}: same-name different-bytes in {out_dir} '
                f'(have {_dst_sha[:12]}, new {_src_sha[:12]}) — append-only')
        return _base, _dst_sha
    shutil.copy2(src_path, _dst)
    return _base, _src_sha


def emit_source(dsc_path: str, out_dir: str, *,
                prefix: str = 'asg', release: int = 1,
                codename: str = 'thor',
                distribution: str = 'Asgard',
                patch_root: str = '',
                patch_level: int = 0,
                keep_binnmu_names: 'Optional[frozenset]' = None,
                universe_lookup:
                'Optional[Callable[[str], Optional[str]]]' = None,
                force_class: 'Optional[str]' = None,
                work_root: 'Optional[str]' = None) -> 'Dict[str, Any]':
    """Produce the published source package for *dsc_path* into *out_dir*.

    Returns {'status': 'verbatim'|'emitted', 'source', 'version',
    'files': {basename: sha256}, 'class'} — or raises SourceEmitError.

    `patch_level` is the SAME uniform P the source's binaries carry
    (decide_patch_bump_count); the emitted version is
    source_package_version(upstream, P).  `keep_binnmu_names` is the
    tunnelled-binary exemption set, passed through to the relation
    rewrite.  `force_class='verbatim'` bypasses classify — the tunnelled
    path (upstream source republished verbatim)."""
    import tempfile
    _info = parse_dsc(dsc_path)
    _src, _upver = _info['source'], _info['version']
    _patches = patches_for(patch_root, _src, _upver) if patch_root else []
    # NOTE: patches with patch_level=0 is a legitimate shape — the emitted
    # version must match what the binaries SHIPPED at (the record is the
    # authority), and pre-scheme patched builds shipped without +p.
    # force_class='verbatim': the TUNNELLED path — we redistribute
    # upstream's binaries unmodified, so the source republishes verbatim
    # regardless of its version shape (MAT-02 D1); the binaries' Source:
    # stamp (transpose_deb source_version) points at it.
    _class = force_class or classify(_info, bool(_patches), prefix, release)
    _new_ver = source_package_version(_upver, prefix, release,
                                      patch_level=patch_level)
    os.makedirs(out_dir, exist_ok=True)
    _dsc_dir = os.path.dirname(os.path.abspath(dsc_path))

    if _class == 'verbatim':
        _out: 'Dict[str, str]' = {}
        for _f in [os.path.basename(dsc_path)] + _info['files']:
            _p = os.path.join(_dsc_dir, _f)
            if not os.path.isfile(_p):
                raise SourceEmitError(f'{_src}: referenced file missing: {_f}')
            _b, _sha = _place(_p, out_dir)
            _out[_b] = _sha
        # a verbatim source PUBLISHES at its upstream version (for the
        # pristine class the two are equal; for the tunnelled force-class
        # the upstream version is the whole point — it is what the
        # binaries' Source: stamp references).
        return {'status': 'verbatim', 'class': _class, 'source': _src,
                'version': _upver, 'files': _out}

    # ---- re-emit path ----
    with tempfile.TemporaryDirectory(prefix='source-emit-',
                                     dir=work_root) as _work:
        _r = _run(['dpkg-source', '--no-check', '-x', dsc_path,
                   os.path.join(_work, 'tree')])
        if _r.returncode != 0:
            raise SourceEmitError(
                f'{_src}: extract failed: '
                f'{_r.stderr.decode(errors="replace")[-400:]}')
        _tree = os.path.join(_work, 'tree')
        # orig tarballs must sit next to the tree for -b (and pass through
        # verbatim to the output).
        _origs = [_f for _f in _info['files'] if '.orig.tar' in _f]
        for _f in _origs:
            _p = os.path.join(_dsc_dir, _f)
            if os.path.isfile(_p) and not os.path.isfile(
                    os.path.join(_work, _f)):
                shutil.copy2(_p, os.path.join(_work, _f))
        _fold: 'List[str]' = []
        _is_quilt = '3.0 (quilt)' in (_info.get('format') or '')
        for _p in _patches:
            _r = _run(['patch', '-p1', '-N', '-i', _p], cwd=_tree)
            if _r.returncode != 0:
                raise SourceEmitError(
                    f'{_src}: patch {os.path.basename(_p)} failed: '
                    f'{_r.stdout.decode(errors="replace")[-300:]}')
            if not _is_quilt:
                continue          # 1.0/native absorb upstream changes natively
            with open(_p, encoding='utf-8', errors='replace') as _fh:
                _targets = _patch_targets(_fh.read())
            _up = [_t for _t in _targets if not _t.startswith('debian/')]
            if not _up:
                continue          # debian/-only: stays out-of-band
            if len(_up) != len(_targets):
                raise SourceEmitError(
                    f'{_src}: patch {os.path.basename(_p)} touches BOTH '
                    'upstream and debian/ paths — split it (a quilt series '
                    'patch may not modify debian/)')
            _fold.append(_p)
        if _fold:
            # CONF-03 mini-fold: an upstream-touching patch must be PART of
            # the quilt series or dpkg-source -b refuses the residual diff.
            # It is already applied to the tree; registering it in series
            # makes the tree state exactly series-applied — zero residue.
            _pdir = os.path.join(_tree, 'debian', 'patches')
            os.makedirs(_pdir, exist_ok=True)
            _series = os.path.join(_pdir, 'series')
            with open(_series, 'a', encoding='utf-8') as _fh:
                for _p in _fold:
                    shutil.copy2(_p, os.path.join(_pdir,
                                                  os.path.basename(_p)))
                    _fh.write(os.path.basename(_p) + '\n')
        # D4: transpose EVERY literal relation constraint in debian/control.
        _ctrl_path = os.path.join(_tree, 'debian', 'control')
        with open(_ctrl_path, encoding='utf-8', errors='replace') as _fh:
            _ctrl = _fh.read()
        _new_ctrl, _ = transpose_source_relations(
            _ctrl, prefix, release, keep_binnmu_names=keep_binnmu_names,
            universe_lookup=universe_lookup)
        if _new_ctrl != _ctrl:
            with open(_ctrl_path, 'w', encoding='utf-8') as _fh:
                _fh.write(_new_ctrl)
        # Synthesized changelog entry at the published version.  Date =
        # the UPSTREAM top entry's date: deterministic AND identical across
        # federation builders (byte-identical emits, append-only pool).
        _ch_path = os.path.join(_tree, 'debian', 'changelog')
        with open(_ch_path, encoding='utf-8', errors='replace') as _fh:
            _ch = _fh.read()
        _date = _changelog_top_date(_ch)
        if not _date:
            raise SourceEmitError(f'{_src}: no parseable changelog date')
        _reason = (f'transpose of {_upver}' if not _patches
                   else f'transpose of {_upver} + {distribution} patch set '
                        f'p{patch_level}')
        _entry = (f'{_src} ({_new_ver}) {codename}; urgency=medium\n\n'
                  f'  * Rebuilt for {distribution} ({_reason}).\n\n'
                  f' -- {distribution} Build '
                  f'<build@{distribution.lower()}>  {_date}\n\n')
        with open(_ch_path, 'w', encoding='utf-8') as _fh:
            _fh.write(_entry + _ch)
        try:
            _epoch = int(email.utils.parsedate_to_datetime(
                _date).timestamp())
        except (TypeError, ValueError) as _e:
            raise SourceEmitError(
                f'{_src}: unparseable changelog date {_date!r}') from _e
        _r = _run(['dpkg-source', '-b', 'tree'], cwd=_work,
                  sde=str(_epoch))
        if _r.returncode != 0:
            raise SourceEmitError(
                f'{_src}: dpkg-source -b failed: '
                f'{_r.stderr.decode(errors="replace")[-400:]}')
        _out = {}
        # emitted artifacts (dsc + debian.tar/diff/native tar) + orig
        # passthrough — orig copied from the SOURCE dir (original bytes).
        for _f in sorted(os.listdir(_work)):
            _p = os.path.join(_work, _f)
            if not os.path.isfile(_p) or _f in _origs:
                continue
            _b, _sha = _place(_p, out_dir)
            _out[_b] = _sha
        for _f in _origs:
            _p = os.path.join(_dsc_dir, _f)
            if os.path.isfile(_p):
                _b, _sha = _place(_p, out_dir)
                _out[_b] = _sha
        return {'status': 'emitted', 'class': _class, 'source': _src,
                'version': _new_ver, 'files': _out}


def _run(cmd: 'List[str]', cwd: 'Optional[str]' = None,
         sde: 'Optional[str]' = None) -> 'subprocess.CompletedProcess':
    _env = dict(os.environ)
    if sde is not None:
        _env['SOURCE_DATE_EPOCH'] = sde
    return subprocess.run(cmd, cwd=cwd, env=_env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


# ---------------------------------------------------------------------------
# D4b — native / non-native environment awareness.  The transpose pipeline
# compensates for building Debian-versioned inputs in a Debian container; a
# NATIVE build (our own distribution's container, sources already +asg)
# must not re-enter it.  These helpers give the build path one authority
# for that decision; wiring lands with the per-build emit integration.
# ---------------------------------------------------------------------------
_OS_RELEASE_ID_RE = re.compile(r'^ID=["\']?([A-Za-z0-9._-]+)["\']?\s*$',
                               re.MULTILINE)


def environment_id(os_release_text: str) -> str:
    """The `ID=` value of an os-release text ('' when absent)."""
    _m = _OS_RELEASE_ID_RE.search(os_release_text)
    return _m.group(1).lower() if _m else ''


def transpose_applies(env_id: str, native_id: str) -> bool:
    """True when the build environment is NON-native (the transpose pipeline
    compensates for Debian-versioned inputs); False on the native
    distribution (inputs are already +asg — transposing again is a mode
    error the caller must refuse loudly, not silently no-op)."""
    return env_id != native_id.lower()
