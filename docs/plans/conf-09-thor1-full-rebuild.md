# CONF-09 — Retire rebump, validate full +thor1 source rebuild

## Context

The `+thor1` distro suffix was retrofitted onto the existing built
`repo/` corpus via three recovery passes in May 2026:

1. `package rebump` (utils.rebump_deb_file) — appended `+thor1` to
   every binary's Version field and filename
2. epoch restoration (utils.restore_deb_epoch) — recovered the
   stripped `2:` / `1:` etc. epochs from cache lookup
3. strict-equal cross-ref rewrite (utils.rewrite_intra_thor1_strict_
   equals) — fixed 1699 same-source `(= X)` sibling references that
   the rebump didn't touch

The retrofit is **operationally fragile**: each pass exposed a new
class of baked-in-at-build-time assumptions that rebump can't see.
Going forward we want every binary in `repo/` to be a clean product
of `BuildContainer.build` with the changelog-prepend (commit
`e60f562`).  That guarantees consistency because `dpkg-gencontrol`
substitutes `${binary:Version}` AT BUILD TIME against the (already
bumped) source version, so sibling cross-refs resolve to `+thor1`
automatically.

## Plan: validate-then-commit

A full source rebuild is **24-36h**.  Before committing we validate
on a representative subset that the `BuildContainer` changelog-bump
produces the expected output shape.

### Validation set (~6-8 packages)

Pick a small but representative subset covering the failure modes
we already know about:

| Package | Why we picked it |
|---|---|
| `systemd` | multi-binary source, 6 binaries, many `(= ${binary:Version})` sibling refs — the canonical Option 1 failure case |
| `gmp` | source has an epoch (`2:6.2.1+dfsg1-1.1`) — exercises epoch preservation in the changelog prepend |
| `perl` | source has an epoch (`5.36.0-7+deb12u3`) and many sibling binaries (perl, perl-base, perl-modules-5.36, libperl5.36) |
| `glib2.0` | wide reverse-dep fan-out across our corpus; subtle inter-binary deps |
| `libyaml` | currently has a CONF-08 nodoc patch in `patch/source/` — exercises our patch-overlay path with the new build |
| `firefox-esr` | large package (>200MB) — exercises the binary-only `-b` flag (no source rebuild attempt) under our changelog mismatch |
| `binutils` | multi-binary with cross-arch sub-packages (binutils, binutils-common, binutils-x86-64-linux-gnu) |

### Validation steps (per package)

For each package above:

1. **Wipe artefacts**:
   ```
   rm -f repo/<pkg>_*.deb repo/<pkg>-*_*.deb
   rm -f log/build/<pkg>.{result,patchhash}
   ```

2. **Build**: `source build <pkg>` (or via dispatcher group)

3. **Assert (per produced .deb)**:
   - Filename ends in `+thor1_<arch>.{deb,udeb}` (or `+thor1_all.{deb,udeb}`)
   - `dpkg-deb -f <file> Version` → ends with `+thor1`, epoch preserved if source had one
   - `dpkg-deb -f <file> Depends Pre-Depends` →
     - All sibling-binary `(= X)` constraints have `+thor1` suffix
     - All cross-source `(>= X)` constraints stay at upstream X (no `+thor1`)
   - `dpkg-deb -f <file> Provides` → if Provides exist with `(= X)`, the X has `+thor1`

4. **Cross-binary inter-source check** (run after all subset built):
   - Build dep tree against the subset's binaries
   - Verify `apt-get install -s <each-subset-pkg>` simulates clean (no
     unmet deps) on a synthetic chroot whose pool is just the subset

5. **Smoke install** (the existence proof):
   - Wipe buildroot + build live chroot using ONLY the subset for the
     overlapping packages, falling back to current repo/ for the rest
   - Verify dpkg --configure -a succeeds without `(= X)` resolution errors

### Pass / fail criteria

- **Pass**: every produced .deb has consistent `+thor1` references in
  Depends/Pre-Depends/Provides; smoke install completes
- **Fail**: any unexpected version-mismatch failure → halt, diagnose
  the substvar / shlibdeps path that didn't propagate the suffix

If pass:

```
rm -rf repo/                       # wipe all binaries
clean source
source build                       # full rebuild, 24-36h
```

Retire the three recovery scripts (rebump_deb_file, restore_deb_epoch,
rewrite_intra_thor1_strict_equals) and remove the `package rebump`
command — once the corpus is clean-built, those helpers serve no
purpose.

### Risk gates

| Risk | Mitigation |
|---|---|
| Build container apt cache changes between subset validation and full rebuild | Pin snapshot timestamp during validation; reuse same timestamp for the big run |
| 24-36h run interrupted mid-flight | `source build` is per-package idempotent; resumable via `check_build` |
| Some packages fail with the new `-b` flag (binary-only) where before they worked because source-build was forgiving | Catch during validation; add to `Tunneled` list if unfixable |
| Disk fill during rebuild | `noautodbgsym` already lands so no dbgsym blobs; firefox + linux still need ~5-10 GB headroom |
| Build cache invalidation forces re-tunnel of packages we don't want to rebuild | Audit `Tunneled` list before kick-off |

## Why this matters

Once the corpus is clean-built:
- No more retrofit gotchas
- `${binary:Version}` substvars resolve correctly at gencontrol time
- The three recovery helpers can be deleted
- `package rebump` command can be removed — operators just `source build`
- Provenance is honest: every binary is a fresh +thor1 product, not a relabelled upstream

## Status

- **2026-05-18**: Plan filed.  Retrofit path active (rebump + epoch
  restore + equals rewrite) as a stopgap while we validate.  No code
  changes yet for the validation harness or the full-rebuild trigger.
