#!/usr/bin/env python3
"""
recover_build_records.py — ONE-OFF.

Backfills `outputs` + `output_hashes` on existing build.json records
that were written under the pre-2026-06-05 bug where the hash loop
iterated stale pre-normalize paths and produced empty hashes.

Strategy: walk repo/dists/<codename>/<comp>/binary-<arch>/*.deb (+
.udeb), read each file's `Source:` control field, group on-disk files
by source name, then for each `<src>.build.json` REPLACE outputs +
output_count + output_hashes with the disk-truth and re-sign.

THROW-AWAY.  Not main-codebase logic.  Delete after use.

Default is DRY-RUN.  Pass `--apply` to commit.

Next steps after `--apply`:
  > coord publish     # athena-primary.jsonl will now contain ~all
                      # sources' binaries, not just the ~98 whose
                      # records happened to have hashes.
"""

import argparse
import os
import sys
from collections import defaultdict

# Use main codebase utils for HMAC-signed build-record I/O.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))
import utils  # noqa: E402

from debian.debfile import DebFile  # noqa: E402

REPO_DISTS = os.path.join(os.path.dirname(__file__), 'repo', 'dists')
BUILDLOG = os.path.join(os.path.dirname(__file__), 'log', 'build')


def discover_source(deb_path: str):
    """Return (source_name, sha256) from a .deb/.udeb on disk.

    Source field can be `<name>` or `<name> (<version>)`; the version
    qualifier is dropped.  When Source is absent — single-binary
    source — the source name equals the Package name.  Returns
    (None, '') on parse failure (caller skips).
    """
    try:
        with DebFile(deb_path) as df:
            ctrl = df.debcontrol()
    except Exception as e:
        return None, '', f"DebFile open failed: {type(e).__name__}: {e}"
    src = ctrl.get('Source')
    if src:
        src_name = src.split(' ', 1)[0].strip()
    else:
        pkg = ctrl.get('Package')
        if not pkg:
            return None, '', "control missing Source and Package"
        src_name = pkg.strip()
    sha = utils.get_sha256(deb_path, use_cache=True)
    if not sha:
        return None, '', "get_sha256 returned empty"
    return src_name, sha, ''


def scan_repo():
    """Yield (source_name, filename, sha256) for every .deb/.udeb under
    repo/dists/.  Skips files whose control we can't read."""
    if not os.path.isdir(REPO_DISTS):
        return
    for codename in sorted(os.listdir(REPO_DISTS)):
        cd = os.path.join(REPO_DISTS, codename)
        if not os.path.isdir(cd):
            continue
        for comp in sorted(os.listdir(cd)):
            cp = os.path.join(cd, comp)
            if not os.path.isdir(cp):
                continue
            for arch in sorted(os.listdir(cp)):
                ap = os.path.join(cp, arch)
                if not os.path.isdir(ap):
                    continue
                for fn in sorted(os.listdir(ap)):
                    if not fn.endswith(('.deb', '.udeb')):
                        continue
                    yield ap, fn


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true',
                   help='Write changes (default is dry-run).')
    args = p.parse_args()
    dry_run = not args.apply

    print(f"Mode: {'DRY-RUN' if dry_run else 'APPLY'}")
    print(f"  repo dists: {REPO_DISTS}")
    print(f"  buildlog:   {BUILDLOG}")
    print()

    if not os.path.isdir(BUILDLOG):
        print(f"ERROR: {BUILDLOG} not found", file=sys.stderr)
        return 1

    # ── Phase 1: walk every binary, group by source ─────────────────────────
    print("=== Phase 1: scan repo/dists/ → group by source ===")
    by_source: 'defaultdict[str, dict[str, str]]' = defaultdict(dict)
    scanned = 0
    parse_errors = 0
    sample_errors: 'list[str]' = []
    for ap, fn in scan_repo():
        fp = os.path.join(ap, fn)
        src, sha, err = discover_source(fp)
        scanned += 1
        if src is None or not sha:
            parse_errors += 1
            if len(sample_errors) < 5:
                sample_errors.append(f"{fn}: {err}")
            continue
        by_source[src][fn] = sha
        if scanned % 500 == 0:
            print(f"  scanned {scanned} files, {len(by_source)} sources so far")
    print(f"  → scanned {scanned} files; "
          f"{len(by_source)} distinct sources; "
          f"{parse_errors} unreadable")
    if sample_errors:
        print("  sample unreadable files:")
        for s in sample_errors:
            print(f"    {s}")
    print()

    # ── Phase 2: update each build record ───────────────────────────────────
    print("=== Phase 2: rewrite build records to match disk-truth ===")
    updated = 0
    already_correct = 0
    no_record = 0
    record_no_files = 0
    other_errors = 0
    sample_updates: 'list[str]' = []

    for fn in sorted(os.listdir(BUILDLOG)):
        if not fn.endswith('.build.json'):
            continue
        pkg = fn[:-len('.build.json')]
        rec = utils.read_build_record(BUILDLOG, pkg)
        if rec is None:
            no_record += 1
            continue
        if pkg not in by_source:
            record_no_files += 1
            continue
        files = by_source[pkg]
        new_outputs = sorted(files.keys())
        new_hashes = dict(files)

        existing_outputs = list(rec.get('outputs') or [])
        existing_hashes = dict(rec.get('output_hashes') or {})

        if (sorted(existing_outputs) == new_outputs
                and existing_hashes == new_hashes):
            already_correct += 1
            continue

        if dry_run:
            if len(sample_updates) < 8:
                sample_updates.append(
                    f"{pkg}: outputs {len(existing_outputs)}→{len(new_outputs)}, "
                    f"hashes {len(existing_hashes)}→{len(new_hashes)}")
        else:
            try:
                utils.update_build_record(
                    BUILDLOG, pkg,
                    outputs=new_outputs,
                    output_count=len(new_outputs),
                    output_hashes=new_hashes,
                )
            except Exception as e:
                other_errors += 1
                print(f"  ERROR updating {pkg}: "
                      f"{type(e).__name__}: {e}")
                continue
        updated += 1

    print(f"  → records updated:           {updated}")
    print(f"    already correct:           {already_correct}")
    print(f"    no record (skipped):       {no_record}")
    print(f"    record has no disk files:  {record_no_files}")
    if other_errors:
        print(f"    update errors:             {other_errors}")
    if dry_run and sample_updates:
        print()
        print("  sample of would-update records (first 8):")
        for s in sample_updates:
            print(f"    {s}")
    print()

    if dry_run:
        print("Re-run with --apply to commit.")
    else:
        print("Next step:  > mirror publish")
    return 0


if __name__ == '__main__':
    sys.exit(main())
