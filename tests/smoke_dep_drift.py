#!/usr/bin/env python3
"""Smoke driver for v0.2 step-1 (#10) and step-2 (#19) validation.

Loads BuildConfig + Cache + DependencyTree (no TUI, no sudo, no source build),
then runs the dep-drift detection loop from BuildSystem._check_dep_drift in
isolation against the .debs in dir_repo. Reports per-package drifts.

Usage:
    python3 tests/smoke_dep_drift.py [path/to/build.conf]

Exit:
    0 — drift report produced (regardless of count)
    1 — pipeline failure (cache load, dep tree, etc.)

Designed to run autonomously without tty. Stubs tui.console, Spinner,
ProgressBar, Prompt so any interactive paths short-circuit.
"""
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

# ── Stub tui BEFORE any other module imports it ─────────────────────────────
import tui  # noqa: E402

class _StubConsole:
    def print(self, msg, *_args, **_kw): print(msg, flush=True)
    def info(self, msg):    print(f"[INFO] {msg}", flush=True)
    def warning(self, msg): print(f"[WARN] {msg}", flush=True)
    def error(self, msg):   print(f"[ERR ] {msg}", flush=True)
    def mark(self): return 0
    def trim_to(self, *_a): pass

class _StubSpinner:
    def __init__(self, msg, *_a, **_kw): print(f"... {msg}", flush=True)
    def done(self): pass

class _StubProgressBar:
    def __init__(self, *_a, **_kw): pass
    def step(self, *_a, **_kw): pass
    def label(self, *_a, **_kw): pass
    def close(self, *_a, **_kw): pass

class _StubPrompt:
    """Auto-picks option '1' for any prompt; logs the situation."""
    def __init__(self, kind, message, options=None, *_a, **_kw):
        self._kind = kind
        self._message = message
        self._options = options or []
        if self._options:
            print(f"[PROMPT] {message} → auto-pick {self._options[0]} "
                  f"(out of {self._options})", flush=True)
        else:
            print(f"[PROMPT] {message} → auto-respond 'y'", flush=True)
    def get_response(self):
        return self._options[0] if self._options else 'y'

tui.console      = _StubConsole()
tui.Spinner      = _StubSpinner
tui.ProgressBar  = _StubProgressBar
tui.Prompt       = _StubPrompt
# These constants live on tui too
tui.PROMPT_OPTIONS  = 'options'
tui.PROMPT_YESNO    = 'yesno'
tui.PROMPT_PASSWORD = 'password'

# Make sure submodules that did `from tui import X` get our stubs.
# Trick: re-bind the names in already-imported modules below as we encounter them.

# ── Now safe to import the rest ─────────────────────────────────────────────
from utils import BuildConfig  # noqa: E402

# Patch tui re-exports in modules that imported names directly
def _patch_module(mod):
    if hasattr(mod, 'Spinner'):     mod.Spinner     = _StubSpinner
    if hasattr(mod, 'ProgressBar'): mod.ProgressBar = _StubProgressBar
    if hasattr(mod, 'Prompt'):      mod.Prompt      = _StubPrompt

import utils  # noqa: E402
_patch_module(utils)

import cache as cache_mod  # noqa: E402
_patch_module(cache_mod)
from cache import Cache  # noqa: E402

import dependencytree as deptree_mod  # noqa: E402
_patch_module(deptree_mod)
from dependencytree import DependencyTree  # noqa: E402

import package as _pkg_module  # noqa: E402


# ── Pipeline ────────────────────────────────────────────────────────────────

def load_config(conf_path):
    print(f"\n=== load_config({conf_path}) ===")
    # BuildConfig reads --config-file from sys.argv via argparse; rewrite argv
    # to point at the path we want (and absolutise it so cwd doesn't matter).
    sys.argv = [sys.argv[0], '--config-file', os.path.abspath(conf_path)]
    cfg = BuildConfig()
    if cfg.error_str:
        print(f"BuildConfig error: {cfg.error_str}")
        sys.exit(1)
    print(f"  arch={cfg.arch}  snapshot_enabled={cfg.snapshot_enabled}  "
          f"snapshot_ts={cfg.snapshot_timestamp_config}")
    print(f"  mirrors: {[m.id for m in cfg.mirrors]}")
    print(f"  dir_cache={cfg.dir_cache}")
    print(f"  dir_repo={cfg.dir_repo}")
    return cfg


def build_cache(cfg):
    print("\n=== build_cache ===")
    t0 = time.time()
    c = Cache(cfg)
    if not c.is_valid:
        print(f"Cache invalid: {c.error_str}")
        sys.exit(1)
    print(f"  cache built in {time.time() - t0:.1f}s")
    return c


def parse_deps(cfg, c):
    print("\n=== parse_dependency ===")
    t0 = time.time()
    dt = DependencyTree(c, select_recommended=False, arch=cfg.arch,
                        build_profiles=cfg.build_profiles)

    print("  Pass I: required")
    dt.resolve_packages(c.required)
    print(f"    selected={dt.selected_count}")

    print("  Pass II: important")
    dt.resolve_packages(c.important)
    print(f"    selected={dt.selected_count}")

    # Pass III: pkglist
    try:
        with open(cfg.pkglist_path) as fh:
            pkglist = [
                ln.strip() for ln in fh
                if ln.strip() and not ln.lstrip().startswith('#')
            ]
    except OSError as e:
        print(f"  Pass III: pkglist unreadable: {e} — skipping")
        pkglist = []
    if pkglist:
        print(f"  Pass III: pkglist ({len(pkglist)} entries)")
        dt.resolve_packages(pkglist)
        print(f"    selected={dt.selected_count}")

    # Map binaries → sources
    if hasattr(dt, 'parse_sources'):
        try:
            dt.parse_sources()
            print(f"  selected_srcs={len(dt.selected_srcs)}")
        except Exception as e:
            print(f"  parse_sources failed (not fatal for drift): "
                  f"{type(e).__name__}: {e}")

    print(f"  parse done in {time.time() - t0:.1f}s")
    return dt


def drift_report(cfg, dt):
    """Mirror of BuildSystem._check_dep_drift, no sudo, no chroot."""
    print("\n=== drift_check ===")
    t0 = time.time()

    drift_pkgs = []
    no_disk    = 0
    checked    = 0

    canonical = (dt.canonical_pkgs if hasattr(dt, 'canonical_pkgs')
                 else dt.selected_pkgs)

    for pkg_name, pkg_obj in canonical.items():
        filename = os.path.basename(pkg_obj.get('Filename', ''))
        if not filename:
            continue
        # strip_build_version lives on BuildSystem; replicate inline
        # Strip Debian binNMU suffix '+bN' and possibly architecture suffix
        # from the deb filename so we look up '...-2_amd64.deb' instead of
        # '...-2+b2_amd64.deb'.
        import re
        filename = re.sub(r'\+b\d+_', '_', filename)
        deb_path = os.path.join(cfg.dir_repo, filename)
        if not os.path.exists(deb_path):
            no_disk += 1
            continue
        checked += 1

        proc = subprocess.run(['dpkg-deb', '-f', deb_path],
                              capture_output=True, text=True)
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        deb_pkg = _pkg_module.Package(proc.stdout)
        if not deb_pkg.isvalid:
            continue

        per_pkg = []
        for field in ('pre_depends', 'depends', 'alt_pre_depends', 'alt_depends'):
            cv = getattr(pkg_obj, field)
            dv = getattr(deb_pkg, field)
            if cv != dv:
                per_pkg.append((field, cv, dv))
        if per_pkg:
            drift_pkgs.append((
                pkg_name,
                pkg_obj.get('Version', '?'),
                deb_pkg.get('Version', '?'),
                per_pkg
            ))

    print(f"  scanned canonical={len(canonical)}  on-disk={checked}  "
          f"missing-from-repo={no_disk}")
    print(f"  drift packages: {len(drift_pkgs)}")
    print(f"  drift_check done in {time.time() - t0:.1f}s")

    # Sample first 10 drifts so the log isn't dominated by them
    for pkg_name, cv, dv, per_pkg in drift_pkgs[:10]:
        print(f"    {pkg_name}  cache={cv}  disk={dv}")
        for field, cval, dval in per_pkg:
            cs = str(cval)
            ds = str(dval)
            if len(cs) > 80: cs = cs[:77] + '...'
            if len(ds) > 80: ds = ds[:77] + '...'
            print(f"      {field}: cache={cs}")
            print(f"      {field}: disk ={ds}")
    if len(drift_pkgs) > 10:
        print(f"    ... and {len(drift_pkgs) - 10} more")

    return len(drift_pkgs)


def main():
    conf = sys.argv[1] if len(sys.argv) > 1 else 'config/build.conf'
    cfg = load_config(conf)
    c = build_cache(cfg)
    dt = parse_deps(cfg, c)
    n = drift_report(cfg, dt)
    print(f"\n=== SUMMARY ({conf}) ===")
    print(f"  snapshot_enabled = {cfg.snapshot_enabled}")
    print(f"  drift_packages   = {n}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
