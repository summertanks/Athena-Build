#!/usr/bin/env python3
"""Self-contained remote local-build-mirror populator.

Runs ON a remote build host (the same hosts `remote_build.py` targets) to build
the snapshot-pinned local apt mirror of the build-dep closure THERE — so the
remote's build containers serve Build-Depends off local disk (file:///localmirror)
instead of re-downloading from snapshot.debian.org on every build.  The remote
populates it over ITS OWN connection (vs BS1 downloading + pushing GBs over a thin
link).

It is GENERIC: it knows nothing Athena-specific.  BS1 computes the plan (the
closure → file list + snapshot URLs + a ready Packages index built from the cache
stanzas) and ships it as ONE json file:

    plan.json:
      {
        "snapshot_ts":    "20260101T000000Z",
        "total_size":     123456789,
        "entries":        [{"basename","url","sha256","size"}, ...],
        "packages_index": "Package: foo\\nVersion: ...\\nFilename: ./foo.deb\\n..."
      }

This script (1) downloads every .deb into <dir> — sha256/size-skipping complete
files and HTTP-Range-resuming partials, so a multi-GB run survives interruption
and re-running just continues — then (2) writes the flat apt index
(Packages{,.gz,.xz} from the shipped blob + a Release carrying
`Origin: AthenaLocalMirror`) and the `.snapshot` marker.  Progress is streamed as
machine-readable marker lines so the BS1 orchestrator can drive a two-bar display
(cumulative total + per-file with rate):

    __ATHENA_LM_PROGRESS__ {"i":N,"n":M,"basename":"foo.deb","file_done":..,
                            "file_total":..,"cum_done":..,"cum_total":..}
    __ATHENA_LM_RESULT__   {"downloaded":N,"skipped":N,"failed":N,
                            "indexed":true,"failures":[["foo.deb","reason"],...]}

Dependencies on the remote: python3 (stdlib only).  No docker, no dpkg-dev, no
Athena code — the mirror is built natively; the build IMAGE only bind-mounts the
finished directory at /localmirror at actual build time.
"""

import argparse
import gzip
import hashlib
import http.client
import json
import lzma
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from email.utils import formatdate

PROGRESS_MARKER = "__ATHENA_LM_PROGRESS__"
RESULT_MARKER = "__ATHENA_LM_RESULT__"
LOCAL_ORIGIN = "AthenaLocalMirror"

_CHUNK = 1 << 16            # 64 KiB read chunk
_EMIT_EVERY = 1 << 16      # emit a progress marker every 64 KiB (== read chunk)


def _log(msg: str) -> None:
    print(f"[remote-localmirror] {msg}", flush=True)


def _emit(payload: dict) -> None:
    print(f"{PROGRESS_MARKER} {json.dumps(payload)}", flush=True)


def _hash(path: str, algo: str) -> str:
    _h = hashlib.new(algo)
    with open(path, 'rb') as _f:
        for _chunk in iter(lambda: _f.read(1 << 20), b''):
            _h.update(_chunk)
    return _h.hexdigest()


def _download(url: str, dest: str, expected_sha: str, expected_size: int,
              on_bytes) -> 'tuple[bool, str]':
    """Download `url` → `dest`, resuming a partial via HTTP Range.  Calls
    `on_bytes(n)` for each n freshly-written bytes (drives the per-file bar).
    Returns (ok, detail).  sha256 is verified on the bytes we actually fetch."""
    _existing = os.path.getsize(dest) if os.path.exists(dest) else 0
    if expected_size and _existing > expected_size:
        # bigger than it should be → corrupt; start over.
        try:
            os.remove(dest)
        except OSError:
            pass
        _existing = 0
    _req = urllib.request.Request(url)
    if _existing:
        _req.add_header('Range', f'bytes={_existing}-')
    try:
        _resp = urllib.request.urlopen(_req, timeout=120)
    except urllib.error.HTTPError as _e:
        # Defensive: callers only fetch when _existing < expected_size, so the
        # 416 'range past EOF = already complete' arm is not normally reached;
        # kept so a redundant re-fetch of a finished file degrades gracefully.
        if _e.code == 416 and expected_size and _existing >= expected_size:
            return (True, 'already complete')
        return (False, f'HTTP {_e.code}')
    except (urllib.error.URLError, OSError, http.client.HTTPException) as _e:
        return (False, f'{type(_e).__name__}: {_e}')
    # If we asked to resume but the server ignored Range (200, not 206), the
    # body is the WHOLE file → restart from scratch so we don't corrupt it.
    _mode = 'ab'
    if _existing and _resp.getcode() == 200:
        _existing = 0
        _mode = 'wb'
    try:
        with open(dest, _mode) as _fh:
            while True:
                _buf = _resp.read(_CHUNK)
                if not _buf:
                    break
                _fh.write(_buf)
                on_bytes(len(_buf))
    except (urllib.error.URLError, OSError, http.client.HTTPException) as _e:
        # http.client.IncompleteRead (a dropped chunked transfer) subclasses
        # HTTPException, NOT OSError — without it a mid-stream drop would abort
        # the whole populate() instead of recording one failure + continuing.
        return (False, f'{type(_e).__name__}: {_e}')
    finally:
        _resp.close()
    if expected_sha and _hash(dest, 'sha256') != expected_sha:
        try:
            os.remove(dest)
        except OSError:
            pass
        return (False, 'sha256 mismatch')
    return (True, 'ok')


def _write_index(directory: str, packages_blob: str, snapshot_ts: str) -> bool:
    """Write Packages{,.gz,.xz} from the shipped blob + a flat Release (byte-
    compatible with local_mirror._write_release) + the .snapshot marker."""
    _data = packages_blob.encode('utf-8') if isinstance(
        packages_blob, str) else packages_blob
    if not _data:
        _log("index: empty packages_index blob — refusing to write")
        return False
    _pkgs = os.path.join(directory, 'Packages')
    with open(_pkgs, 'wb') as _f:
        _f.write(_data)
    with gzip.open(_pkgs + '.gz', 'wb') as _g:
        _g.write(_data)
    with lzma.open(_pkgs + '.xz', 'wb') as _x:
        _x.write(_data)
    _rows = []
    for _name in ('Packages', 'Packages.gz', 'Packages.xz'):
        _p = os.path.join(directory, _name)
        if os.path.isfile(_p):
            _rows.append((_name, os.path.getsize(_p),
                          _hash(_p, 'md5'), _hash(_p, 'sha256')))
    _lines = [
        f'Origin: {LOCAL_ORIGIN}',
        'Label: Athena Local Build Mirror',
        'Suite: local',
        'Codename: local',
        'Architectures: amd64 all',
        'Components:',
        f'Date: {formatdate(time.time(), usegmt=True)}',
        'Description: Athena local build-closure mirror',
        'MD5Sum:',
    ]
    _lines += [f' {_md5} {_size:>16} {_name}'
               for _name, _size, _md5, _ in _rows]
    _lines.append('SHA256:')
    _lines += [f' {_sha} {_size:>16} {_name}'
               for _name, _size, _, _sha in _rows]
    with open(os.path.join(directory, 'Release'), 'w') as _fh:
        _fh.write('\n'.join(_lines) + '\n')
    with open(os.path.join(directory, '.snapshot'), 'w') as _fh:
        _fh.write((snapshot_ts or '') + '\n')
    return True


def populate(plan: dict, directory: str) -> dict:
    """Download the plan's entries into `directory` (resumable) then index it.
    Returns the result dict reported via RESULT_MARKER."""
    os.makedirs(directory, exist_ok=True)
    _entries = plan.get('entries', [])
    _n = len(_entries)
    # Disk preflight (mirrors local_mirror.disk_check, 5% headroom): refuse a
    # multi-GB run we can't finish up front, rather than filling the disk and
    # failing N files one OSError at a time.  Returns a single __disk__ failure
    # so main() exits non-zero and the BS1 orchestrator reports it cleanly.
    _total = int(plan.get('total_size', 0) or 0)
    if _total:
        try:
            _free = shutil.disk_usage(directory).free
        except OSError:
            _free = -1
        _needed = int(_total * 1.05)
        if 0 <= _free < _needed:
            _log(f"insufficient disk at {directory}: need ~{_needed} B, "
                 f"have {_free} B free")
            return {'downloaded': 0, 'skipped': 0, 'failed': len(_entries),
                    'indexed': False,
                    'failures': [['__disk__',
                                  f'insufficient space: need {_needed}B '
                                  f'have {_free}B']]}
    # `or 0` guards a JSON null total_size (int(None) raises) — match line 187.
    _cum_total = int(plan.get('total_size', 0) or 0) or 1
    _cum_done = 0
    _downloaded = _skipped = 0
    _failures: 'list[list[str]]' = []

    for _i, _e in enumerate(_entries, 1):
        _basename = _e['basename']
        _size = int(_e.get('size', 0) or 0)
        _dest = os.path.join(directory, _basename)
        _emit({'i': _i, 'n': _n, 'basename': _basename, 'file_done': 0,
               'file_total': _size, 'cum_done': _cum_done,
               'cum_total': _cum_total})
        # Already complete? Trust the size (snapshot .debs are immutable; a
        # truncated file is strictly smaller, never equal).  Avoids re-hashing
        # GBs on every re-run — sha256 is still verified on bytes we fetch.
        if _size and os.path.exists(_dest) and os.path.getsize(_dest) == _size:
            _skipped += 1
            _cum_done += _size
            _emit({'i': _i, 'n': _n, 'basename': _basename, 'file_done': _size,
                   'file_total': _size, 'cum_done': _cum_done,
                   'cum_total': _cum_total})
            continue

        _file_done = os.path.getsize(_dest) if os.path.exists(_dest) else 0
        _since_emit = [0]

        def _on_bytes(_nbytes, _i=_i, _basename=_basename, _size=_size,
                      _since_emit=_since_emit):
            nonlocal _cum_done, _file_done
            _file_done += _nbytes
            _cum_done += _nbytes
            _since_emit[0] += _nbytes
            if _since_emit[0] >= _EMIT_EVERY:
                _since_emit[0] = 0
                _emit({'i': _i, 'n': _n, 'basename': _basename,
                       'file_done': _file_done, 'file_total': _size,
                       'cum_done': _cum_done, 'cum_total': _cum_total})

        # _file_done seeds the resume offset; rewind cum/file accounting so the
        # bytes already on disk aren't double-counted as freshly downloaded.
        _cum_done += _file_done
        _ok, _detail = _download(
            _e['url'], _dest, _e.get('sha256', ''), _size, _on_bytes)
        if not _ok:
            # roll back this file's contribution; it failed.
            _cum_done -= _file_done
            _failures.append([_basename, _detail])
            _log(f"FAIL {_basename}: {_detail}")
            continue
        _downloaded += 1
        # normalise cum_done to the authoritative size (range math is exact, but
        # guard against a server sending a few extra/fewer bytes).
        _cum_done += _size - _file_done
        _emit({'i': _i, 'n': _n, 'basename': _basename, 'file_done': _size,
               'file_total': _size, 'cum_done': _cum_done,
               'cum_total': _cum_total})

    _indexed = _write_index(
        directory, plan.get('packages_index', ''), plan.get('snapshot_ts', ''))
    return {'downloaded': _downloaded, 'skipped': _skipped,
            'failed': len(_failures), 'indexed': _indexed,
            'failures': _failures}


def main(argv: 'list[str] | None' = None) -> int:
    _p = argparse.ArgumentParser(
        prog='remote_localmirror.py',
        description='Populate the snapshot-pinned local build mirror on this '
                    'host from a BS1-computed plan.')
    _p.add_argument('--plan', default='plan.json',
                    help='path to plan.json (default: ./plan.json)')
    _p.add_argument('--dir', default='localmirror',
                    help='target mirror directory (default: ./localmirror)')
    _a = _p.parse_args(argv)
    with open(_a.plan) as _fh:
        _plan = json.load(_fh)
    # expanduser so BS1 can pass a literal `~/athena-localmirror` and the remote
    # resolves it (the build mount uses the same path).
    _dir = os.path.abspath(os.path.expanduser(_a.dir))
    _log(f"plan={os.path.abspath(_a.plan)} dir={_dir} "
         f"entries={len(_plan.get('entries', []))} "
         f"total={_plan.get('total_size', 0)}B "
         f"snapshot={_plan.get('snapshot_ts', '?')}")
    _result = populate(_plan, _dir)
    _log(f"done: {_result['downloaded']} downloaded, {_result['skipped']} "
         f"skipped, {_result['failed']} failed, indexed={_result['indexed']}")
    print(f"{RESULT_MARKER} {json.dumps(_result)}", flush=True)
    return 0 if (_result['indexed'] and not _result['failed']) else 1


if __name__ == '__main__':
    sys.exit(main())
