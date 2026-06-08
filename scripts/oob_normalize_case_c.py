#!/usr/bin/env python3
"""Out-of-band re-normaliser: correct artifacts that were spuriously
`+asg`-stamped under the retired dep-constraint-strip trigger ("Case C").

Background
----------
Before Position-X (docs/asg-bump-position-x.md) the build stamped `+asg<R>u<N>`
whenever ANY strip rewrote a built `.deb` — including a strip that touched only
a *dependency constraint* (e.g. a perl-XS module built against the build
container's security-suffixed `perl (>= 5.36.0-7+deb12u3)`).  Those packages are
byte-identical to pristine upstream; the stamp was noise.  The thor1 rebuild
produced 19 such sources.

State at time of writing (verified, 2026-06-08)
-----------------------------------------------
For every one of the 19 the spurious `+asg1u1` exists ONLY on disk + in the
signed build.json record.  The published ledger (config/published.manifest)
already carries them at the pristine base, so:

  * Case-D lineage will NOT resurrect the bump (no `+asg` in the ledger at
    these bases) — so this tool does NOT touch the manifest.
  * After the Position-X code change, the predictor expects the pristine name;
    `find_matching_artifact` already accepts the on-disk `+asg` variant, so the
    repo is stable EVEN BEFORE this runs (no rebuild storm).  This tool simply
    makes the on-disk artifact match the prediction exactly.

What this does (per source, in order)
-------------------------------------
  1. de-stamp each on-disk `+asg` artifact back to pristine
     (utils.destamp_asg_deb — peels filename, control Version, sibling pins)
  2. rewrite the build.json record: outputs -> pristine names,
     output_hashes -> recomputed SHA-256, then re-sign (local HMAC key)

It is DRY-RUN by default.  Pass --apply to mutate.  It is IDEMPOTENT: a source
whose record is already pristine, or a `.deb` with no `+asg` marker, is skipped.

Infinite-build safety
---------------------
Run order matters.  The Position-X code change (buildcontainer: drop the
dep-strip trigger) MUST already be in place before publishing, otherwise a
re-publish would cement `+asg1u1` into the ledger and a later rebuild would
re-mint it.  With Position-X in place, pristine is a fixed point for these 19
(no A/B/C/D trigger fires) — running this tool, then reindexing, then
publishing, converges and never oscillates.

After running with --apply, regenerate the repo index before publishing
(the normal `mirror publish` / `chroot build` flow auto-reindexes a stale
InRelease) and confirm with `source audit` / `repo audit`.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import utils  # noqa: E402

# SHA-256 of the empty string == patch_set_hash when no patches were applied.
_EMPTY_PATCH_HASH = (
    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')


def _section_count(text: str, name: str) -> int:
    _m = re.search(rf'--- {re.escape(name)} \((\d+)\)', text)
    return int(_m.group(1)) if _m else 0


def classify_spurious(buildlog_dir: str) -> 'list[str]':
    """Return the sources whose ONLY `+asg` trigger was a dep-constraint
    strip (the retired Case C): ASG STAMP > 0, NMU STRIP == 0 (no own-version
    strip -> not Case B), empty patch_set_hash (not Case A), generation u1
    (not Case D lineage)."""
    _out: 'list[str]' = []
    for _lg in sorted(glob.glob(os.path.join(buildlog_dir, '*.buildlog'))):
        _pkg = os.path.basename(_lg)[:-len('.buildlog')]
        try:
            _txt = open(_lg).read()
        except OSError:
            continue
        if _section_count(_txt, 'ASG STAMP') == 0:
            continue
        if _section_count(_txt, 'NMU STRIP') != 0:
            continue          # own-version strip -> Case B, legitimate
        _rec = utils.read_build_record(buildlog_dir, _pkg)
        if _rec is None:
            continue
        if _rec.get('patch_set_hash', '') != _EMPTY_PATCH_HASH:
            continue          # patched -> Case A, legitimate
        _gen = 1
        for _o in _rec.get('outputs', []):
            _m = re.search(r'\+asg\d+u(\d+)', _o)
            if _m:
                _gen = max(_gen, int(_m.group(1)))
        if _gen > 1:
            continue          # lineage -> Case D, legitimate
        _out.append(_pkg)
    return _out


def _locate_deb(repo_dir: str, filename: str) -> 'str | None':
    """Find `filename` anywhere under `repo_dir` (component-agnostic)."""
    for _p in glob.glob(os.path.join(repo_dir, '**', filename), recursive=True):
        if os.path.isfile(_p):
            return _p
    return None


def _sha256(path: str) -> str:
    _h = hashlib.sha256()
    with open(path, 'rb') as _fh:
        for _chunk in iter(lambda: _fh.read(1 << 20), b''):
            _h.update(_chunk)
    return _h.hexdigest()


def normalize_source(pkg: str, repo_dir: str, buildlog_dir: str,
                     apply: bool) -> dict:
    """De-stamp a single source's `+asg` artifacts and rewrite its record.
    Returns a report dict; mutates disk only when `apply` is True."""
    _debs: 'list[str]' = []
    _errors: 'list[str]' = []

    def _mk(record: str, skipped: bool = False) -> dict:
        return {'package': pkg, 'debs': _debs, 'record': record,
                'skipped': skipped, 'errors': _errors}

    _rec = utils.read_build_record(buildlog_dir, pkg)
    if _rec is None:
        _errors.append('record missing/unverifiable')
        return _mk('unchanged')

    _outputs = list(_rec.get('outputs', []))
    _hashes = dict(_rec.get('output_hashes', {}))
    if not any('+asg' in _o for _o in _outputs):
        return _mk('unchanged', skipped=True)   # already pristine -> no-op

    _new_outputs: 'list[str]' = []
    _new_hashes: 'dict[str, str]' = {}
    for _o in _outputs:
        if '+asg' not in _o:
            _new_outputs.append(_o)
            if _o in _hashes:
                _new_hashes[_o] = _hashes[_o]
            continue
        _path = _locate_deb(repo_dir, _o)
        if _path is None:
            _errors.append(f'{_o}: not found under repo')
            # keep the entry as-is so we never silently drop a record line
            _new_outputs.append(_o)
            if _o in _hashes:
                _new_hashes[_o] = _hashes[_o]
            continue
        if apply:
            _r = utils.destamp_asg_deb(_path)
            if _r['status'] != 'rewritten':
                _errors.append(f'{_o}: destamp {_r["status"]}')
                _new_outputs.append(_o)
                if _o in _hashes:
                    _new_hashes[_o] = _hashes[_o]
                continue
            _new_name = os.path.basename(_r['new_path'])
            _new_outputs.append(_new_name)
            _new_hashes[_new_name] = _sha256(_r['new_path'])
            _debs.append(f'{_o} -> {_new_name}')
        else:
            # dry-run: predict the pristine name without touching disk
            _name, _ext = os.path.splitext(_o)
            _p, _v, _a = _name.split('_')
            _new_name = f'{_p}_{utils.ASG_SUFFIX_RE.sub("", _v)}_{_a}{_ext}'
            _new_outputs.append(_new_name)
            _new_hashes[_new_name] = '<recomputed-on-apply>'
            _debs.append(f'{_o} -> {_new_name}  (dry-run)')

    if apply:
        _rec['outputs'] = _new_outputs
        _rec['output_hashes'] = _new_hashes
        _rec['output_count'] = len(_new_outputs)
        utils.write_build_record(buildlog_dir, _rec)   # re-signs
        return _mk('rewritten+resigned')
    return _mk('would-rewrite')


def main(argv: 'list[str] | None' = None) -> int:
    _ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    _ap.add_argument('--repo', default='repo',
                     help='repo pool root (default: repo)')
    _ap.add_argument('--buildlog-dir', default=os.path.join('log', 'build'),
                     help='build-record dir (default: log/build)')
    _ap.add_argument('--apply', action='store_true',
                     help='mutate disk (default: dry-run)')
    _ap.add_argument('--only', default='',
                     help='comma-separated subset of sources to act on')
    _args = _ap.parse_args(argv)

    _sources = classify_spurious(_args.buildlog_dir)
    if _args.only:
        _want = {_s.strip() for _s in _args.only.split(',') if _s.strip()}
        _sources = [_s for _s in _sources if _s in _want]

    _mode = 'APPLY' if _args.apply else 'DRY-RUN'
    print(f"[oob-normalize-case-c] {_mode}: {len(_sources)} spurious source(s)")
    _n_debs = 0
    _n_err = 0
    for _pkg in _sources:
        _rep = normalize_source(_pkg, _args.repo, _args.buildlog_dir,
                                _args.apply)
        if _rep['skipped']:
            print(f"  {_pkg:32} already pristine (skip)")
            continue
        for _d in _rep['debs']:
            print(f"  {_pkg:32} {_d}")
            _n_debs += 1
        print(f"  {_pkg:32}   record: {_rep['record']}")
        for _e in _rep['errors']:
            print(f"  {_pkg:32}   ERROR: {_e}")
            _n_err += 1
    print(f"[oob-normalize-case-c] {_mode} done: "
          f"{_n_debs} deb(s), {_n_err} error(s)")
    if not _args.apply:
        print("  re-run with --apply to mutate, then regenerate the repo "
              "index (mirror publish / chroot build auto-reindex) and "
              "verify with `source audit`.")
    elif _n_err == 0:
        print("  NEXT: regenerate the repo index before publishing, then "
              "`source audit` to confirm pristine + closed.")
    return 1 if _n_err else 0


if __name__ == '__main__':
    raise SystemExit(main())
