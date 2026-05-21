# Strip-NMU and the "same-upstream-sibling" constraint idiom

Reference notes for `utils.py:strip_nmu_from_control_text`'s pair-rewriter
pass — why it exists, why we rewrite the way we do, what the alternatives
were, and what the risk analysis covered.

## The problem

Athena strips Debian NMU/binNMU/backport suffixes from every package's
Version field (utils.strip_nmu_from_deb).  This was a deliberate design
choice (memory: feedback_strip_nmu_at_build): ship at the pristine upstream
source version, drop the `+debNuN` / `+bN` / `~bpoN+N` machinery.

A subset of upstream Debian packages encode an "any debian-revision of the
same upstream" constraint between sibling binaries using the pattern:

```
Depends: ..., X (>> V), X (<< V-.)
```

where `V` is the source's bare upstream version (substituted from
`${source:Upstream-Version}` at build time).  Per Debian Policy §5.6.12,
the missing debian-revision in `V` is treated as `'0'`, so:

- `apt_pkg.version_compare('V-0', 'V')` → `0` (equal)
- `apt_pkg.check_dep('V-0', '>>', 'V')` → `False`

Upstream Debian builds at `V-0+debNuN` (or similar), which IS strictly
greater than bare `V`.  After our strip, our sibling binary lands at `V-0`,
which apt treats as equal.  The `>>` half of the constraint fails:

```
ERROR: dependency git -> git-man unresolved
  git Depends: git-man (>> 1:2.39.5) — no satisfying pkg in repo/
```

The audit reports it; at install time apt would also reject.

## What the idiom is FOR

The maintainer's intent: lock sibling binary X to the same upstream as the
consumer, while permitting per-binary debian-revision drift.  In upstream
Debian, this lets a documentation-only point bump of `git-man` (e.g.
`-1` → `-2`) ship without forcing a rebuild of `git` itself.  Our atomic
build pipeline does not exercise this flexibility — `dpkg-buildpackage`
always emits all sibling binaries together at the same Version field.  So
the "loose" lower bound was never load-bearing for us.

## Repo prevalence

A scan of every `.deb` in `repo/main` (2026-05-21) for the AND-pair
pattern `X (>> V), X (<< V-.)`:

| Source family | Binaries using the idiom |
|---|---|
| git | 11 (git, git-man, git-all, gitk, gitweb, git-cvs, git-svn, git-daemon-{run,sysvinit}, git-email, git-gui, git-mediawiki) |
| (all other sources) | 0 |

The pattern is a git-family convention.  Other sources use `(=
${binary:Version})` for sibling locks, which works transparently
because sibling binaries share Version after strip.

## The four rewrite options considered

| Option | What it does | Pros | Cons |
|---|---|---|---|
| A. Per-pkg DEP-3 patch | Edit `debian/control`: `>>` → `>=` | Minimal diff; surgical | Per-version path; doesn't generalise |
| B. Pipeline rewrite `>>` → `>=` | At strip time, swap operator only | Generic; small code; preserves "any debrev" semantics | Lower bound slightly broader than upstream-intent |
| C. Pipeline rewrite to `(= our_version)` | At strip time, collapse pair into exact-sibling-version | Tightest; encodes our atomic-build invariant; no per-pkg patches | Slightly bigger code (pair detection + format roundtrip) |
| D. Selectively bump target binary | Keep `+debNuN` on idiom targets only | Preserves upstream constraint as-is | Re-introduces bump machinery we removed; cascading to N-deep transitive targets; filename normalisation upheaval |

## Impact analysis of Option C (pair → `(= our_version)`)

The rewrite is **strictly bound** to the detected pair pattern.  Trigger
conditions:

1. Field is `Depends` or `Pre-Depends`
2. Two AND-level entries (not OR-alternatives) target the **same name** X
3. One entry has operator `>>` with version V
4. The other entry has operator `<<` with version exactly `V-.`

If any of these fail, the constraints are left unchanged.

### Scenarios that could newly satisfy the rewritten `(= our_version)`

`our_version` is our binary's own (post-strip) Version field, e.g.
`1:2.39.5-0` for git.  The rewritten constraint accepts only that exact
value.  This is **strictly narrower** than both the original `(>> V),
(<< V-.)` (which accepted any `V-N` for N ≥ 1) and the simpler `>=`
relaxation (which would have accepted V, V-0, V-1, ...).

| Test value | Original verdict | Option C verdict |
|---|---|---|
| `V-0` (our binary) | reject | **accept** |
| `V-1`, `V-2`, ... `V-Z` | accept | reject (different from ours) |
| `V+suffix` (any +debNuN, +bN) | accept | reject (different from ours) |
| Different upstream (`V+1`, `V-1` upstream) | reject | reject |

Option C **rejects** values the original accepted (e.g. `V-1`).  In
practice this is harmless because:

- Our pipeline never produces `V-1` — every sibling-binary build is at the
  same exact Version.
- If a hand-crafted .deb at `V-1` landed in `repo/main`, the audit would
  flag it as a duplicate of our `V-0` (highest-version dedup), and dpkg
  at install time would prefer the higher version.  So the operator
  always sees and resolves the duplicate before it can be picked up.

### Cross-source binary collision

Debian namespace is single-owner per binary name.  Two different sources
cannot both produce `git-man`.  apt's resolver picks one canonical
version per name.  In our self-contained repo (memory:
project_self_contained_repo), there is no alternative archive that
could provide a parallel git-man.  Not realisable.

### Snapshot rollover mid-source-build

`dpkg-buildpackage` is atomic per source.  git's source emits git AND
git-man in one invocation, sharing the exact Version field.  Stale
artefacts from a previous snapshot are handled by repo dedup (highest
wins).  Cross-snapshot mismatches are detected by other mechanisms
(dep_drift) before they can be installed.

### Stale duplicate in repo

`repo_audit.scan_repo_state` keeps only highest version per name
(`repo_audit.py:257`).  `dpkg-scanpackages` does the same.  dpkg
prefers highest at install.  Whichever copy is highest wins;
behaviour is identical between the original and rewritten constraints.

### Versioned Provides from a third party

Original `>> V` rejected any `(= V)` Provides; rewritten `(= V-0)`
accepts only an exact-version Provides match.  Scanning our repo
(2026-05-21): zero packages declare a versioned Provides for
git, git-man, bubblewrap, or libdpkg-perl.  Same outcome for both
constraint shapes in practice.

### Hand-crafted .deb at bare `V`

`dpkg-buildpackage` never emits a Version field without a debian-revision.
Bare-`V` could only appear via manually-dropped .deb.  Option C
rejects it (exact-match to our `V-0`); original rejected it as well
(via `>>`).  Both safe.

## What "designed to run with" preserves

The maintainer's hard intent — same upstream V, ABI-compatible sibling —
is preserved by Option C in a stricter form: not just "same upstream"
but "same source-build instance".  No looser, no looser-by-accident.

The maintainer's soft intent — "any debrev that's not the unpackaged
upstream" — was a defensive heuristic against manually-built siblings.
That threat doesn't apply in any apt-installable workflow.  Discarding
it loses nothing operational.

## Why Option C over Option B (the simpler `>=` swap)

- **Tighter**: B accepts the value `V-0` plus theoretically `V-1`, `V-2`
  etc.  C accepts only our binary's exact version.  Less surface area
  for accidental satisfier injection.
- **Self-documenting**: `(= 1:2.39.5-0)` reads as "must be the exact
  same as us"; `(>= 1:2.39.5)` reads as "must be greater than upstream",
  which is conceptually misleading after our strip.
- **No semantic relaxation**: C says "lock to our version", which IS the
  invariant our build pipeline preserves.  B says "any version above
  upstream", which is a broader claim.

The minor cost of C is one extra round-trip through `PkgRelation.parse`
and `PkgRelation.str` per Depends field — negligible vs. dpkg-deb's
own pack/unpack.

## Implementation summary

`utils.py:_rewrite_sibling_idiom_in_text` is called by
`strip_nmu_from_control_text` after the per-constraint version-string
strip pass.  It:

1. Parses each Depends / Pre-Depends field via
   `debian.deb822.PkgRelation.parse_relations`
2. Walks the AND-level relation list
3. For each single-entry `(X >> V)` AND-group, looks for a matching
   single-entry `(X << V-.)` AND-group with the same name and the
   `-.` upper-bound version
4. When the pair matches, rewrites the `>>` entry to
   `(X = our_version)` and removes the `<<` entry
5. Re-serialises via `PkgRelation.str`

`our_version` is the binary's own Version field, extracted AFTER the
strip pass (so it's the pristine stripped value).  Same atomic
source-build invariant means this matches every sibling's Version.

## When this can be retired

If the strip-NMU policy reverts (CONF-09 thor1-full-rebuild brings a
controlled bump layer, memory: project_phase_e_followups_deferred),
the original idiom satisfies again without rewrite, and the
pair-rewriter can be removed.  Until then, this is a permanent part of
the strip pipeline.
