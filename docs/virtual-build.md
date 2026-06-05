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
| Linkage drift in fork patches | Substvar policy is conservative — see below |
| Non-deterministic binary lists (kernel meta whose names depend on upstream ABI bumps) | We trust `Source.binary` (Binary: field) verbatim |
| Stale mirror state | Operator must run `mirror pull` first for accurate ownership / cross-builder findings |
| Substvars from upstream's actual link graph | Inherited as text from upstream's already-resolved binary `Depends:` |

The blind spots are deliberately not engineered around — adding them
would either re-implement `dh_shlibdeps` (the substvar resolver) or
duplicate the actual build.  Real builds are still the source of
truth; virtual build is the cheap pre-filter.

## Substvar policy (conservative, locked)

Each synthesized binary inherits its upstream binary's `Depends:`,
`Pre-Depends:`, `Conflicts:`, `Breaks:`, `Provides:`, `Replaces:`,
`Recommends:` **verbatim**.  Two transformations:

1. **NMU stripping**: constraint versions of form `<base>+debNuN`,
   `<base>+asgRuN`, `<base>-Nb<bN>` are normalised to `<base>` (via
   `pristine_base`) so a sibling pin `(= 1.0-1+deb12u1)` reads as
   `(= 1.0-1)`.
2. **Sibling pin rewrite**: when a constraint's TARGET is a sibling
   binary in the same source AND its (normalised) version equals our
   source's pristine base, the version is rewritten to the sibling's
   predicted virtual version.  Catches the intra-source `(= ver)` pin
   that real-build's `restamp_asg_deb` rewrites.

External constraints (libc6, soname pins to unrelated sources, kernel
ABI pins) are LEFT UNCHANGED — apt resolves them at install time
against whatever versions of those packages happen to be in the repo.

### False-clean risk

A fork patch that changes which shared library the binary links to
(switches from `libssl1.1` to `libssl3`, say) updates `Depends:` only
when `dh_shlibdeps` runs at real-build time.  Virtual build inherits
the upstream's pre-patch `Depends:` — so the virtual closure check
sees the OLD soname constraint, not the new one.  If the new soname
isn't in the repo, the real build will fail closure but virtual would
pass.

Mitigation when this bites: tighten policy to "parse fork's
`debian/control` and use its declared `Depends:`" for forks under
`fork/source/`.  Deferred until a false-clean ships.

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
