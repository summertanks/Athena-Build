# Virtual build (`virtual build`)

A dry-run simulation of the full build → audit → publish pipeline
that runs entirely from cache data + version-math.  No source compile,
no dpkg-buildpackage, no .deb on disk, no mirror writes.  The point:
catch the **integration** failures that have historically cost us
hours per round-trip — bump arithmetic, sibling-pin drift, closure
breaks, ownership blocks — before any build runs.

## What it catches

| Failure class | How virtual build sees it |
|---|---|
| `+asg<R>u<N>` arithmetic mistakes | `compute_post_build_versions` runs the SAME decision tree as the real normaliser |
| NMU-strip residue in dep constraints | `synthesize_binary_record` reads pristine bases via `pristine_base` |
| Intra-source sibling pin drift (kernel-meta scenario) | `_rewrite_sibling_pins` rewrites `(= V)` to virtual ver across siblings |
| Hard `Depends:` closure breaks under the install corpus | real `repo_audit.audit_dep_closure` against synthetic state |
| Conflict/Breaks within cohorts | real `repo_audit.audit_conflict_cohort` |
| Hash conflicts across builders | real `coord.reconcile.detect_hash_conflicts` |
| Ownership blocks at publish time | `coord.store.project_owners` + the MIRROR-02 ownership decision tree as a dry-run |
| Same-source duplicate binaries | `synthesize_repo_state` highest-version-wins + INFO |
| Missing sources in the configured scope | per-source warning before the audit pass |

## What it CANNOT catch

| Blind spot | Why |
|---|---|
| Compile-time failures (gcc errors, dh-helper bugs, autoconf failures) | We don't actually build anything |
| Linkage drift in fork patches WHEN NO LOCAL BUILD EXISTS | Falls back to upstream cache; once the source is built once, the local `.deb` is authoritative |
| Non-deterministic binary lists (kernel meta whose names depend on upstream ABI bumps) | We trust `Source.binary` (Binary: field) verbatim |
| Stale mirror state | Operator must run `mirror pull` first for accurate ownership / cross-builder findings |
| Substvars on never-built sources | Inherited from upstream cache; first real build re-grounds the prediction |

The blind spots are deliberately not engineered around — adding them
would either re-implement `dh_shlibdeps` (the substvar resolver) or
duplicate the actual build.  Real builds are still the source of
truth; virtual build is the cheap pre-filter.

## Operating principle

Virtual build runs **BEFORE** any source build.  It must not depend
on any post-build artifact (on-disk `.deb`, build log, etc.).  Its
inputs are exactly what `cache parse` + `source sync` see:

- upstream cache (Packages + Sources records, per snapshot)
- our local fork sources (Binary list, Build-Depends, patches)
- the asg ledger (published manifest)
- cache-parse's selection decision (`dep_tree.selected_srcs` etc.)

If a dep can't be determined from those inputs, virtual reports the
uncertainty rather than guess.

## Resolution order per binary

1. **Upstream cache** — read the binary's upstream Package record; inherit `Depends:`, `Conflicts:`, `Provides:`, etc. with these transformations applied:
   - NMU strip on every constraint version (mirrors real build's `strip_nmu_from_control_text`)
   - Intra-source sibling-pin rewrite at the per-binary pristine base
   - Cross-source global-pin rewrite (constraint pristine matches state.packages[target] pristine)
   - Canonical-source filter (when upstream Package has a `Source:` field that names a peer source in scope)
2. **Skeleton** — when upstream cache doesn't carry the binary (kernel-ABI floats, fork-only renames), emit a minimal record with just Package/Version/Arch/Source/Filename/SHA256.  No Depends inherited.

When two sources collide on the same Package name AND neither carries
an upstream `Source:` signal, virtual cannot determine which source
truly produces the binary.  Dedup picks the highest version and emits
a `WARNING virtual_dedup_ambiguous` finding; closure findings against
those names get downgraded to `WARNING virtual_closure_break_ambiguous`.

## Severity convention

| Kind | Severity | Why |
|---|---|---|
| `virtual_closure_break` | CRITICAL | Target present at wrong version; pre-build provable synth/asg-stamp bug |
| `virtual_closure_break_ambiguous` | WARNING | Target derived from ambiguous cross-source dedup; apparent break may be a dedup artifact |
| `virtual_dedup_ambiguous` | WARNING | Cross-source collision with no upstream-canonical signal; data alone can't say which source produces the binary |
| `virtual_binary_list_overlap` | INFO | Binary-list declaration overlap between sources (kernel signing chain pattern) — real builds emit subsets |
| `virtual_invalid_record` | CRITICAL | Synthesis produced a record missing Package/Version (real bug) |

**Absent targets are silent.**  When a consumer's Depends references
a binary that isn't in synth state (because cache parse didn't pull
the source), virtual build emits NO finding.  Cache parse's mandate is
the installed-system runtime closure; build-time substvar resolution
runs in the build chroot (today: upstream Debian) and decides what
ends up in the consumer's actual Depends.  Virtual cannot determine
that outcome statically.  Real `repo audit` post-build reads the
on-disk Depends and surfaces any genuine closure break.

The CRITICAL bar is reserved for things we can *prove* will break.
Unresolvable ambiguities are observable; out-of-scope absences are
beyond virtual's scope.

## What virtual can and cannot say

**Can prove pre-build** (CRITICAL when broken):
- asg-stamp arithmetic against the published ledger
- intra-source sibling-pin rewriting
- cross-source pin rewriting (pristine matches)
- ownership and hash-conflict gates in the publish dry-run
- synthesis record well-formedness

**Cannot say pre-build** (silent):
- whether `dh_shlibdeps` will include a given dep in the produced binary
- whether ffmpeg's configure will detect and disable a missing codec
- whether the installer will succeed against the predicted state
  (real `repo audit` after real build is the authoritative check)

## Architectural note: build chroot today vs end-state

Today, the build container is constructed from upstream Debian's repo.
Build-deps come from there; our self-contained distro is the install
target only.  Cache parse's `parse_sources` walks the runtime closure
of pkg.list / live.list / installer.list / pool.list to populate
`selected_srcs` — by design, NOT Build-Depends.  When/if the build
chroot moves to our own distro end-to-end, cache parse will need an
additional Build-Depends walk; until then, virtual build respects the
current scope decision.

## Phases (vs the real pipeline)

```
real pipeline                         virtual build
─────────────────                     ──────────────
cache build         real, downloads   N/A  (same cache reused)
cache parse         real, interactive REAL — operator picks pkgs
source sync         real downloads    virtual (trust cache.source_hashtable)
source build        real builds       virtual (compute_post_build_versions
                                      + per-binary upstream-inherit +
                                      sibling pin rewrite)
source audit        repo_audit        virtual_repo_audit on synthetic state
repo audit          audit_dep_closure  ↑ (same call, virtual input)
mirror publish      writes + claims   virtual_publish_dry_run (no writes)
  hash conflict     detect_hash_conf   ↑ (same)
  ownership rule    generate_pending   ownership decision via project_owners
  closure gate      audit_dep_closure  same as repo audit phase
```

Every "real" function in the right column is literally the function
from the left column — the only synthesizer code is the binary
record builder.  The audits are the source of truth.

## Running it

```
virtual build                # everything in dep_tree.selected_srcs
virtual build all            # same
virtual build indl           # only names in config/build_pkg.list
virtual build firefox-esr    # explicit source(s)
virtual build openssl curl   # multiple explicit sources
virtual run                  # alias for `virtual build`
```

Pre-requisites:
- `cache build` + `cache parse` must have completed (operator-interactive
  selection step is where virtual build "trusts" what's selected)
- For ownership/cross-builder checks: `mirror pull` should be recent
  (warning printed if no cached remote state found)

## Output

```
virtual build: scope=all  arch=amd64  release=1
  policy: conservative substvars (inherit upstream Depends; rewrite sibling pins only).  Compile errors NOT detected.
  ok        synthesized 1582 virtual binary record(s) across 985 source(s)

virtual repo audit:
  ok        synthetic closure clean

virtual publish dry-run:
  ok        no cross-builder conflicts; no ownership blocks

virtual build: PASS — pipeline projection clean.
```

Failures are per-phase:

```
virtual repo audit:
  CRITICAL  virtual_closure_break: linux-image-amd64: Depends = 'linux-image-6.1.0-49-amd64' — no satisfying pkg in repo/

virtual build: BLOCKED — 1 CRITICAL finding(s).
```

When a CRITICAL fires, the message tells you the consumer + the
unresolved constraint.  Fix at the source (add a missing dep to the
selection, bump a constrained version, etc.) and re-run.

## When to use it

- **Before any `source build` round** — catches the bump-math + closure
  blocks that historically cost a build cycle to discover.
- **Before any `mirror publish`** — catches ownership blocks before
  the publish attempt halts at the gate.
- **In CI / pre-merge gates** — virtual build is fast and writes
  nothing; it's the natural gate before "real build everything."
- **After editing `fork/source/`** — confirms the fork's binaries
  synthesize cleanly and don't break closure for downstream consumers.

## What it's NOT

- Not a replacement for `repo audit` or `mirror audit` against actual
  state — those still operate on real on-disk artifacts and real
  remote sidecars.
- Not a replacement for the build itself — it can't verify the source
  compiles.
- Not authoritative for shipped artifacts — virtual_publish_dry_run
  emits a synthetic claim ledger that's discarded at end of run; it
  doesn't propagate anywhere.
