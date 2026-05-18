# Fork ↔ upstream collision gate

The cache build aborts when any fork package's binary name collides
with an upstream package AND the upstream version is greater-or-equal
to the fork's version.

## The shape

For every fork package under `fork/source/<name>/`, the build:

1. Walks the fork mirror first → adds binary name(s) to a "fork claims
   this name" set.
2. Walks upstream mirrors → when an upstream record has a name in that
   set, the upstream record is **dropped** (so apt resolution picks
   the fork unconditionally).
3. At end of cache build, the collision gate compares the fork's version
   against every dropped upstream version.  If any upstream ≥ fork,
   the build aborts.

## What "collision" means

apt picks the highest-versioned record by name.  The cache supersede
hides upstream's record so the fork's record is the only candidate —
but this is a one-way implicit win.  If upstream's hidden version is
actually *newer* (bug fixes, security patches, ABI updates), the fork
ships under a version label that looks higher (e.g. `0.79+thor1`) but
the code is still at upstream `0.79`.  Operator sees the latest
version installed; everyone misses the actual upstream bug fix in
`0.80`.

The gate rejects this state.

## Why "bump the version" is NOT a valid fix

If upstream is at `0.80` and the fork is at `0.79+thor1`, the gate
fails.  It is tempting to bump the fork's `debian/changelog` to
`0.80` so version dominance kicks in: `0.80+thor1` > `0.80`.  Don't.

The version label is a claim about the codebase.  Bumping says
"this is upstream 0.80 plus our patch."  But the fork is still
based on upstream 0.79.  Upstream 0.80 may contain bug fixes,
security patches, behavioural changes, or ABI updates that the
fork doesn't have.

A version-bump-only fix:

- Hides the version-skew from the gate (its job done).
- Ships the older code under the newer version label.
- Makes future debugging confusing — operator sees version `0.80+thor1`
  installed but a 0.80-fixed bug still reproduces.
- Suppresses the gate's signal that the fork needs maintenance.

## The two valid mitigations

### 1. Rebase the fork onto upstream's new version

Pull upstream's new source, re-apply the fork's delta, fix conflicts,
ship at the new upstream base.

Example (assuming the fork is `fork/source/pkgsel/`, upstream bumped
to `0.80`):

```bash
# 1. Capture the current fork's delta
cd fork/source/pkgsel
git diff <previous-upstream-tag>..HEAD -- . > /tmp/our-delta.patch

# 2. Replace with upstream 0.80 source
cd ..
apt-get source pkgsel=0.80
rm -rf pkgsel
mv pkgsel-0.80 pkgsel

# 3. Re-apply our delta
cd pkgsel
git apply /tmp/our-delta.patch  # resolve conflicts manually if any

# 4. Update changelog to 0.80
dch -v 0.80
```

The DistroSuffix mechanism appends `+thor1` at build time → produces
`pkgsel 0.80+thor1`, beats upstream `0.80`, gate passes.

### 2. Rename the fork

If the collision is incidental — the fork's binary name happens to
coincide with an unrelated upstream binary that the operator
doesn't want our fork to supersede — rename the fork to something
unique.  By convention, prefix with `athena-` to mark toolchain
origin (see `memory/project_three_layer_identity.md`).

```bash
git mv fork/source/installer-data fork/source/athena-installer-data
# edit fork/source/athena-installer-data/debian/control:
#   Source: athena-installer-data
#   Package: athena-installer-data
# edit fork/source/athena-installer-data/debian/changelog stanza header
```

## Reading the diagnostic

```
cache build aborted — 2 fork collision(s) where upstream version >= fork version:
  deb: pkgsel (fork 0.79+thor1 < upstream 0.80 [main])
  udeb: pkgsel-udeb (fork 0.79+thor1 < upstream 0.80 [main])
```

Each line: `<kind>: <pkg-name> (fork <fork-version> < upstream <upstream-version> [<mirror-id>])`.

- `<kind>` = `deb` or `udeb` (separate namespaces; both gated).
- The mirror ID identifies which upstream mirror shipped the colliding
  version (`main`, `updates`, `security`, etc.).
- If the same name collides in multiple mirrors, one line per mirror —
  shows the full landscape.

## Source-pkg collisions are NOT gated

The gate fires on binary (`deb`) and udeb collisions only.  Source-
package supersede continues to drop silently (today's behavior).
Source dominance has no apt-resolution consequence on the installed
system; the gate is about ensuring binary versions ship correctly.
