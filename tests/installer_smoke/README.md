# tests/installer-smoke/ — Phase F (CI gate)

COMP-12 Phase F.  Boots the latest installer ISO under QEMU, captures
the serial log for a configurable duration, scans for known-bad
patterns, fails on any match.

See `docs/plans/comp-02-robust-build.md` § Phase F for the original
spec.

## Prerequisites

```
apt install qemu-system-x86 qemu-utils
# Optional, for EFI mode:
apt install ovmf
# Optional, for KVM acceleration (10× faster full installs):
apt install qemu-kvm   # plus your user in the kvm group
```

## Quick gate (default mode, ~3 min wall time)

The "does d-i start cleanly?" check.  Catches:
- Broken initrd (kernel panic before d-i loads)
- Missing udeb from installer.list closure
- Fatal cdebconf failure on first dialog
- COMP-01f Phase 1+2 regressions (main-menu strings, splash)
- COMP-14 / GRUB regressions (no-video-mode, can't load gfxterm)

```
python3 tests/installer-smoke/run.py \
    --iso image/athena-installer-0.1-amd64.iso \
    --mode bios \
    --output-dir /tmp/smoke-bios
```

Add `--mode efi` for the EFI variant (needs `ovmf` installed).

**Expected:** boots, prints `smoke: OK (...)`, exits 0.  Serial log
left at `/tmp/smoke-bios/serial.log` for inspection.

## Full install gate (~15-30 min wall time)

Drives an unattended install via `preseed.cfg` and waits for the
"Installation complete" signal from finish-install.

```
python3 tests/installer-smoke/run.py \
    --iso image/athena-installer-0.1-amd64.iso \
    --full \
    --output-dir /tmp/smoke-full
```

**NOTE (v1):** `preseed.cfg` ships a CONSERVATIVE single-disk full-
overwrite partman layout.  The first full-mode run typically needs
preseed iteration — the install will halt at any un-preseeded prompt
and the harness times out (exit 124).  Inspect `serial.log` to see
where it halted, add the missing `d-i <key> <type> <value>` line,
re-run.  Once it completes cleanly, that preseed becomes the CI
baseline.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No fatal patterns matched within timeout |
| `1` | One or more fatal patterns matched (regression) |
| `2` | Setup failure (QEMU not installed, ISO missing, …) |
| `124` | --full timed out with no completion signal (preseed gap or hang) |

## Files

| File | Role |
|---|---|
| `run.py` | Entry point — QEMU driver + log capture + parser invocation |
| `known_bad_patterns.py` | Pattern catalogue + `scan_log()` function |
| `preseed.cfg` | Unattended-install answers (used by `--full`) |
| `__init__.py` | Module marker (empty) |

## Adding a new known-bad pattern

When a new install bug is fixed, add a pattern so it can't silently
regress.  Edit `known_bad_patterns.py`:

```python
KNOWN_BAD = [
    ...
    (
        r'<your regex>',
        'fatal',    # or 'warn' for tracking-only
        'short explanation of what this means',
    ),
]
```

Then run the harness against a known-good ISO to sanity-check the
pattern doesn't trigger on clean output.  If it does, refine the
regex.

## CI integration sketch

The harness is a single Python script, no test framework dependency.
A CI step shape:

```yaml
- name: Build installer ISO
  run: <existing build invocation>

- name: Smoke quick
  run: |
    python3 tests/installer-smoke/run.py \
        --iso image/athena-installer-*.iso \
        --mode bios

- name: Smoke quick (EFI)
  run: |
    python3 tests/installer-smoke/run.py \
        --iso image/athena-installer-*.iso \
        --mode efi
```

Nightly (or pre-release) job runs `--full` instead of `--quick`.
Full runs are too slow for per-commit gating (~30 min × N modes vs
~3 min for quick).

## Pattern unit-test coverage

The pattern parser (`scan_log` + `has_fatal`) is unit-tested in
`tests/test_module.py` (see `test_installer_smoke_*`).  Tests are
fast — they synthesize stub log content, don't invoke QEMU.

## Known limitations (intentional v1 trade-offs)

- **No HTTP preseed server.**  `--full` uses `preseed/file=/cdrom/
  preseed.cfg` (the in-ISO preseed, not this dir's).  Future iteration
  could start a tmp `python -m http.server` and pass
  `preseed/url=http://10.0.2.2:PORT/preseed.cfg` for hot-iterating
  the preseed without rebuilding the ISO.  For now, the in-ISO
  preseed at `installer/preseed/preseed.cfg` is the source of truth
  for `--full`; copy + adapt this dir's `preseed.cfg` into that file
  when ready.

- **No installed-system boot-verify.**  After install completes,
  the plan calls for a second QEMU boot (no ISO) that confirms the
  target reaches a login prompt.  v1 stops at install completion
  (QEMU exit).  Adding the second boot is ~30 LOC + an additional
  ~3-min timeout — a follow-up if the bare install-completion gate
  proves insufficient.

- **No graphical comparison.**  COMP-01f Phase 2 added a splash;
  v1 doesn't validate it visually.  The presence of the asset on
  the ISO is pinned by the existing test
  `test_installer_grub_cfg_wires_background_image` in
  `tests/test_module.py`.
