# Position-X — `+asg` bumps only on real deltas, never on dep-constraint strips

**Decision date:** 2026-06-08
**Status:** implemented (buildcontainer + detector + out-of-band re-normaliser)

## TL;DR

The build stamps `+asg<R>u<N>` on a binary only when the binary is a genuine
delta from pristine upstream. There are four candidate triggers; **three are
kept, one is dropped:**

| Case | Trigger | Real content delta? | Bump? |
|------|---------|---------------------|-------|
| **A** | we applied a patch (`was_patched`) | yes — our bytes | **keep** |
| **B** | source version carried a strippable Debian suffix (`_src_is_delta`) | yes — security-vs-original collision risk | **keep** |
| **C** | a strip rewrote only a **dependency constraint** (`_any_stripped`) | **no** — bytes identical to upstream | **DROPPED** |
| **D** | the ledger already has a `+asg` at this base (`lineage`) | yes — version-ordering / `=`-pin continuity | **keep** |

After this change the real build (`buildcontainer._normalize_built_artifacts`)
agrees with the two predictors that never had Case C
(`utils.compute_post_build_versions`, `virtual_build.synthesize_source_binaries`).

## Why Case C was wrong

When we build, e.g., a perl-XS module, `dpkg-shlibdeps` captures a dependency
floor against whatever lib is installed in the **build container** — which, in
the Debian-bootstrap era, carries security suffixes:

```
build container's perl:   5.36.0-7+deb12u3
generated dep:            perl (>= 5.36.0-7+deb12u3)
post-build strip:         perl (>= 5.36.0-7)         ← +deb12u3 peeled off
```

The **strip is mandatory** and stays: `+asg` sorts *below* `+deb` in Debian
version ordering (`"asg" < "deb"`), so an unstripped `>= …+deb12u3` floor would
be unsatisfiable by our own `+asg`-stamped dep targets and the **whole repo
would be uninstallable**. (See `utils.strip_nmu_from_deb`.)

But the package's *own bytes* are identical to pristine upstream — only a
dependency floor moved, and we just normalised it away. Treating that as an
Athena delta:

- mints a spurious `+asg<R>u<N>` on a byte-identical package;
- **churns on every snapshot advance** — each newly-security-updated build-dep
  re-bumps every consumer;
- inflates the surface of the cross-source `=`-pin hazard (below).

The thor1 rebuild produced **19** such sources (perl-XS modules + a few C libs:
`libclone-perl`, `libcairo-perl`, `gobject-introspection`, `mpfr4`, `xz-utils`,
…). None protect anything in a self-contained repo (apt installs the highest
version satisfying a floor; the stricter floor excludes only versions that don't
exist in our pool).

## Self-hosting makes Case C vanish anyway

Case C is purely an artifact of building Asgard **on a Debian base**. Once
Asgard builds Asgard (source-only from upstream), the build container carries
*our* pristine/`+asg` deps — there is no Debian suffix to strip from a generated
constraint, so `_any_stripped` would never fire. Dropping Case C now simply
aligns today's behaviour with where self-hosting lands, and keeps the lineage
ledger free of spurious generations we'd otherwise carry across the transition.

`_src_is_delta` (Case B) stays active forever: as long as we ingest Debian
*source* packages, their versions carry `+debNuN` and we strip-and-restamp them.

## Infinite-build safety (the load-bearing analysis)

Dropping Case C **cannot** cause a rebuild storm or version oscillation:

1. **No re-trigger of the 19.** `utils.find_matching_artifact` accepts a `+asg`
   variant for a pristine prediction (it exists precisely to avoid the CONF-13
   filename-loop). So the on-disk `…+asg1u1` files satisfy the new pristine
   prediction — `check_build` sees them present, no rebuild.
2. **Pristine is a fixed point.** For the 19: not patched (A✗), pristine source
   (B✗), Case C removed (C✗), and the **published ledger already has them at
   pristine** so lineage never fires (D✗). No trigger → pristine forever.
3. **Ordering rule.** The code change must land **before** any re-publish. If
   you published the current `+asg1u1` build records first, you would cement
   `+asg1u1` into the ledger (activating Case-D lineage) and a later rebuild
   would re-mint it. With Position-X in place first, every step converges
   monotonically to pristine.

## The cross-source `=`-pin hazard + detector

Independent of the bump decision, an exact pin across sources can dangle:

```
A depends:  B (= X+deb12u7)   →  strip  →  B (= X)
our repo B: X+asg1u3           →  X+asg1u3 ≠ X  →  A uninstallable
```

`>=` floors are safe (`X+asg1u3 >= X`); intra-source sibling `=` pins are safe
(uniform-N restamp rewrites them to `(= X+asg…)`). Only **cross-source `=`
pins** stripped to bare pristine, against an `+asg`-stamped target, break — and
**self-hosting removes this too** (build-container B == repo B).

`repo_audit.audit_dep_closure` already reports these as generic unresolved deps.
`repo_audit.detect_dangling_asg_equals_pins` **classifies** the actionable
subclass and is surfaced by `repo audit` with a remedy
(*relax to `>=` or rewrite the pin to the `+asg` version*).

## Out-of-band re-normaliser

`scripts/oob_normalize_case_c.py` corrects the 19 already-built artifacts.
DRY-RUN by default; `--apply` to mutate; idempotent.

Per source it:
1. de-stamps each on-disk `+asg` artifact → pristine (`utils.destamp_asg_deb`,
   the verified inverse of `restamp_asg_deb` — filename, control Version,
   intra-source sibling pins);
2. rewrites the build.json record (outputs → pristine, hashes recomputed) and
   re-signs (local HMAC key).

It does **not** touch `config/published.manifest`: that ledger already carries
the 19 at pristine, so there is nothing to purge.

**Runbook:**
```
# 1. (already done) Position-X code change is in buildcontainer.py
# 2. preview
python3 scripts/oob_normalize_case_c.py
# 3. apply
python3 scripts/oob_normalize_case_c.py --apply
# 4. regenerate the repo index (mirror publish / chroot build auto-reindex)
# 5. verify, then publish
#    source audit   # 19 now pristine, closure clean
#    repo audit     # no dangling =-pins
```
