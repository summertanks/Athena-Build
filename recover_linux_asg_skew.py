#!/usr/bin/env python3
"""
recover_linux_asg_skew.py — ONE-OFF recovery for the linux family's
mixed +asg<R>u<N> generation state observed 2026-06-05.

THROW-AWAY SCRIPT.  Not main-codebase logic.  Delete after use.

CONTEXT:
The repo currently holds linux-source AND linux-signed-amd64-source
binaries at three intermixed generations:
  - pristine 6.1.174-1
  - 6.1.174-1+asg1u1
  - 6.1.174-1+asg1u2
plus older 6.1.170-3 and 6.1.172-1+asg1u1 leftovers and orphan
`.verified` sidecars with no matching `.deb`.  Repo audit fails because
linux-signed-amd64 metas pin `(= 6.1.174-1+asg1u1)` while ABI siblings
are at pristine or +asg1u2.

STRATEGY:
Strip everything in the linux family to PRISTINE 6.1.174-1 in place:
  - Phase 1: wipe old-generation files (not at the target pristine base).
  - Phase 2: dedup multi-variant binaries — for each binary name, pick
             ONE variant (prefer most-recently-stamped), delete others.
  - Phase 3: strip kept variants to pristine.  Uses pristine_base()
             which strips BOTH NMU and asg suffixes from the Version
             field AND every version constraint in dep-related fields
             (covers cross-source pins like linux-signed-amd64 metas'
             `Depends: linux-headers-6.1.0-49-amd64 (= 6.1.174-1+asg1u1)`).
  - Phase 4: wipe any remaining orphan `.verified` sidecars.

Default is DRY-RUN.  Pass `--apply` to actually modify files.

After running with --apply:
  > repo index       # regenerate Packages
  > repo audit       # verify dep chain resolves
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

# Use main codebase utils for pristine_base + .deb parsing.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))
import utils  # noqa: E402

REPO_DIR = os.path.join(os.path.dirname(__file__), 'repo')
LINUX_DIR = os.path.join(REPO_DIR, 'dists', 'thor', 'main', 'binary-amd64')

# Target pristine base — anything not matching this base is older-generation
# and gets wiped (Phase 1).
TARGET_PRISTINE_BASE = '6.1.174-1'

# Filename prefix(es) the recovery touches.  Pragmatic scope: `linux-*` and
# bare `linux_` capture every binary from both `linux` and
# `linux-signed-amd64` source packages.  Non-prefixed siblings from the
# `linux` source (bpftool, hyperv-daemons, usbip, rtla, libcpupower*) are
# left alone — they don't have strict-equality pins to ABI binaries, so
# their version skew doesn't break the audit.
LINUX_PREFIX_RE = re.compile(r'^(linux-|linux_)')

# Packages whose name starts with `linux-` but come from a DIFFERENT
# source — must NOT be touched by this recovery.
LINUX_FAMILY_EXCLUDE = {
    'linux-base',          # from `linux-base` source — unrelated, never asg-stamped
}


def parse_filename(filename: str) -> Optional[Tuple[str, str, str, str]]:
    """`<pkg>_<ver>_<arch>.deb` → (pkg, ver, arch, ext).  None on malformed."""
    if not filename.endswith(('.deb', '.udeb')):
        return None
    name, ext = os.path.splitext(filename)
    parts = name.split('_')
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2], ext


def strip_control_to_pristine(content: str) -> Tuple[str, int]:
    """Rewrite control text: strip Version + every version constraint in
    dep-related fields to `pristine_base()`.  Returns (new_text, count).

    Differs from utils.strip_nmu_from_control_text in that it uses
    pristine_base (strips +asg too) rather than strip_nmu_suffix.
    """
    total = 0

    def _ver_sub(m: re.Match) -> str:
        nonlocal total
        old = m.group(1)
        new = utils.pristine_base(old)
        if new != old:
            total += 1
        return f'Version: {new}'

    content = re.sub(
        r'^Version: (\S+)\s*$', _ver_sub,
        content, count=1, flags=re.MULTILINE,
    )

    # Strip constraint suffixes inside dep-related fields.
    _dep_fields = (
        'Depends', 'Pre-Depends', 'Conflicts', 'Breaks',
        'Replaces', 'Provides', 'Recommends', 'Suggests', 'Enhances',
    )
    _constraint_re = re.compile(
        r'\(\s*(<=|>=|<<|>>|=)\s*([^)]+?)\s*\)'
    )

    def _con_sub(m: re.Match) -> str:
        nonlocal total
        op, ver = m.group(1), m.group(2)
        new_ver = utils.pristine_base(ver)
        if new_ver != ver:
            total += 1
        return f'({op} {new_ver})'

    new_lines: List[str] = []
    in_dep_field = False
    for line in content.splitlines(keepends=True):
        if line and line[0] not in (' ', '\t'):
            m = re.match(r'^([A-Za-z][A-Za-z0-9-]*):', line)
            in_dep_field = m is not None and m.group(1) in _dep_fields
        if in_dep_field:
            new_lines.append(_constraint_re.sub(_con_sub, line))
        else:
            new_lines.append(line)

    return ''.join(new_lines), total


def strip_deb_to_pristine(deb_path: str, dry_run: bool) -> Tuple[str, str]:
    """Rewrite a .deb's control + rename file to pristine version.
    Returns (status, new_path) where status is one of:
      'unchanged' | 'rewritten' | 'malformed' | 'error'
    """
    base = os.path.basename(deb_path)
    parsed = parse_filename(base)
    if parsed is None:
        return ('malformed', deb_path)
    pkg, old_ver, arch, ext = parsed

    try:
        from debian.debfile import DebFile
        with DebFile(deb_path) as deb:
            ctrl_bytes = deb.control.get_content('control')
    except Exception as e:
        print(f"  ERROR reading {base}: {type(e).__name__}: {e}")
        return ('error', deb_path)
    if ctrl_bytes is None:
        return ('malformed', deb_path)

    ctrl_text = ctrl_bytes.decode('utf-8', errors='replace')
    new_ctrl_text, strips = strip_control_to_pristine(ctrl_text)

    new_ver = utils.pristine_base(old_ver)
    filename_changed = new_ver != old_ver
    if not filename_changed and strips == 0:
        return ('unchanged', deb_path)

    new_base = (f'{pkg}_{new_ver}_{arch}{ext}'
                if filename_changed else base)
    new_path = os.path.join(os.path.dirname(deb_path), new_base)

    if dry_run:
        print(f"  WOULD strip: {base} → {new_base} "
              f"({strips} constraints rewritten)")
        return ('rewritten', new_path)

    # If a file already exists at the new path, drop it — we're about
    # to write a fresh pristine.  Edge case: pristine and asg variants
    # both on disk for the same binary; Phase 2 dedup should have
    # removed the pristine before we get here, but be defensive.
    if new_path != deb_path and os.path.exists(new_path):
        os.remove(new_path)

    with tempfile.TemporaryDirectory(prefix='strip-asg-') as work:
        subprocess.run(
            ['dpkg-deb', '-R', deb_path, work],
            check=True, capture_output=True,
        )
        with open(os.path.join(work, 'DEBIAN', 'control'), 'w') as fh:
            fh.write(new_ctrl_text)
        subprocess.run(
            ['dpkg-deb', '--root-owner-group', '-b', work, new_path],
            check=True, capture_output=True,
        )

    # Remove the old .deb (now superseded) and any matching .verified sidecar.
    if new_path != deb_path:
        os.remove(deb_path)
        for sidecar in (deb_path + '.verified',):
            if os.path.exists(sidecar):
                os.remove(sidecar)

    print(f"  stripped: {base} → {new_base}")
    return ('rewritten', new_path)


def is_linux_family(filename: str) -> bool:
    """The recovery's scope filter — files from `linux` or
    `linux-signed-amd64` source packages.  Excludes look-alike names
    from other sources (e.g. `linux-base` from the `linux-base` source)."""
    if not LINUX_PREFIX_RE.match(filename):
        return False
    pkg_name = filename.split('_', 1)[0]
    return pkg_name not in LINUX_FAMILY_EXCLUDE


def main():
    parser = argparse.ArgumentParser(
        description='Recover linux-family +asg skew (one-off).')
    parser.add_argument('--apply', action='store_true',
                        help='Apply changes (default is dry-run).')
    parser.add_argument('--target-base', default=TARGET_PRISTINE_BASE,
                        help=f'Pristine base to keep '
                             f'(default {TARGET_PRISTINE_BASE}).')
    args = parser.parse_args()

    dry_run = not args.apply
    target_base = utils.pristine_base(args.target_base)

    if not os.path.isdir(LINUX_DIR):
        print(f"ERROR: {LINUX_DIR} not found", file=sys.stderr)
        return 1

    print(f"Recovery: {LINUX_DIR}")
    print(f"  Target pristine base: {target_base}")
    print(f"  Mode: {'DRY-RUN (pass --apply to commit)' if dry_run else 'APPLY'}")
    print()

    # Track files marked for removal so dry-run later phases see the
    # post-phase state.  In --apply mode each phase mutates disk
    # directly so this set just mirrors that.
    _scheduled_removal: set = set()

    # ── Phase 1: wipe old-generation .debs (not at target_base) ─────────────
    print(f"=== Phase 1: wipe linux-family files NOT at base {target_base} ===")
    old_removed = 0
    for fn in sorted(os.listdir(LINUX_DIR)):
        if not is_linux_family(fn):
            continue
        parsed = parse_filename(fn)
        if parsed is None:
            continue
        _, ver, _, _ = parsed
        if utils.pristine_base(ver) == target_base:
            continue
        path = os.path.join(LINUX_DIR, fn)
        sidecar = path + '.verified'
        if dry_run:
            print(f"  WOULD rm old-gen: {fn}")
            _scheduled_removal.add(fn)
            _scheduled_removal.add(fn + '.verified')
        else:
            os.remove(path)
            if os.path.exists(sidecar):
                os.remove(sidecar)
            print(f"  rm old-gen: {fn}")
        old_removed += 1
    print(f"  → {old_removed} old-generation files\n")

    # ── Phase 2: dedup multi-variant binaries — keep one per pkg name ──────
    print("=== Phase 2: dedup multi-variant binaries (keep one per name) ===")
    by_name: dict = {}
    for fn in sorted(os.listdir(LINUX_DIR)):
        if fn in _scheduled_removal:
            continue
        if not is_linux_family(fn):
            continue
        parsed = parse_filename(fn)
        if parsed is None:
            continue
        pkg, ver, arch, ext = parsed
        if utils.pristine_base(ver) != target_base:
            continue
        by_name.setdefault(pkg, []).append(fn)

    deduped = 0
    for pkg in sorted(by_name):
        variants = by_name[pkg]
        if len(variants) == 1:
            continue
        # Prefer the most-stamped variant (lexically highest version
        # works: pristine < +asg1u1 < +asg1u2 in apt version compare,
        # and we'd rather strip a stamped file (control is rewritten
        # consistently) than choose pristine + risk inconsistency).
        variants_sorted = sorted(
            variants,
            key=lambda fn: parse_filename(fn)[1],   # type: ignore
            reverse=True,
        )
        keep = variants_sorted[0]
        for v in variants_sorted[1:]:
            p = os.path.join(LINUX_DIR, v)
            sidecar = p + '.verified'
            if dry_run:
                print(f"  WOULD rm dup: {v}  (keep {keep})")
                _scheduled_removal.add(v)
                _scheduled_removal.add(v + '.verified')
            else:
                os.remove(p)
                if os.path.exists(sidecar):
                    os.remove(sidecar)
                print(f"  rm dup: {v}  (keep {keep})")
            deduped += 1
    print(f"  → {deduped} duplicate variants removed\n")

    # ── Phase 3: strip kept variants to pristine ───────────────────────────
    print("=== Phase 3: strip kept variants to pristine ===")
    stripped = 0
    unchanged = 0
    for fn in sorted(os.listdir(LINUX_DIR)):
        if fn in _scheduled_removal:
            continue
        if not is_linux_family(fn):
            continue
        parsed = parse_filename(fn)
        if parsed is None:
            continue
        _, ver, _, _ = parsed
        if utils.pristine_base(ver) != target_base:
            continue
        path = os.path.join(LINUX_DIR, fn)
        status, _ = strip_deb_to_pristine(path, dry_run=dry_run)
        if status == 'rewritten':
            stripped += 1
            # In dry-run mode the strip didn't actually rename the file;
            # the old .verified sidecar (if any) would become orphan
            # post-strip.  Schedule it so Phase 4 reports it.
            if dry_run:
                _scheduled_removal.add(fn + '.verified')
        elif status == 'unchanged':
            unchanged += 1
    print(f"  → {stripped} stripped, {unchanged} already pristine\n")

    # ── Phase 4: wipe any remaining orphan .verified sidecars ──────────────
    print("=== Phase 4: wipe orphan .verified sidecars ===")
    orphans_removed = 0
    for fn in sorted(os.listdir(LINUX_DIR)):
        if not fn.endswith('.verified'):
            continue
        deb_fn = fn[:-len('.verified')]
        if not is_linux_family(deb_fn):
            continue
        sidecar = os.path.join(LINUX_DIR, fn)
        deb_path = os.path.join(LINUX_DIR, deb_fn)
        if os.path.exists(deb_path):
            continue
        if dry_run:
            print(f"  WOULD rm orphan: {fn}")
        else:
            os.remove(sidecar)
            print(f"  rm orphan: {fn}")
        orphans_removed += 1
    print(f"  → {orphans_removed} orphan sidecars\n")

    print(f"{'Dry-run' if dry_run else 'Recovery'} complete: "
          f"{old_removed} old-gen + {deduped} dups + {stripped} stripped "
          f"+ {orphans_removed} orphans.")
    if dry_run:
        print("Re-run with --apply to commit.")
    else:
        print("\nNext steps:")
        print("  1. `repo index`   — regenerate Packages")
        print("  2. `repo audit`   — verify dep chain resolves")
    return 0


if __name__ == '__main__':
    sys.exit(main())
