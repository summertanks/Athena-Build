# Plan — MAT-02: publish source packages (Sources index + pool `.dsc`s)

## Status: DRAFT (2026-07-12) — awaiting operator review; oracle-validated

## Context

The archive ships 4589 binaries and **zero** source packages: the input
`.dsc` is consumed at container extraction and never re-emitted, so the
already-wired `dpkg-scansources` pass (`apt_repo.generate_repo_indexes`,
`dists/<codename>/main/source/`) indexes an empty set.  Consequences:
not DFSG/GPL source-available, not third-party rebuildable, no
provenance from a `.deb` back to the source that built it.

Everything below is grounded in a full-archive oracle
(2026-07-12, `dpkg-source -b` over all 1007 synced sources, two
independent extract→patch→emit cycles each, fixed `SOURCE_DATE_EPOCH`
and synthesized changelog):

| Class | Count | Oracle result |
|---|---|---|
| Pristine — version unchanged, unpatched | 795 | n/a (publish verbatim, no re-emit) |
| Transposed-only (`+debNuK → +asg<R>uK`) | 170 | all emit clean, **byte-deterministic** |
| Patched, patches confined to `debian/` | 31 | all emit clean, byte-deterministic |
| Patched, patches touch upstream files | **2** (`cryptsetup`, `curl`) | `dpkg-source -b` refuses out-of-band upstream changes — need the CONF-03 quilt-fold |

Zero non-deterministic emits: the append-only segregate invariant and
build-record hashing survive re-emits unchanged.

## Design decisions

### D1 — publish class per source

- **Pristine** (shipped version == upstream version, no patches):
  republish the upstream `.dsc` + tarballs **byte-verbatim**.  Never
  re-emit: the re-emitted `.dsc` would carry the *same filename* with
  different bytes (same-name ⟹ same-bytes invariant), and verbatim
  keeps Debian's signature on the `.dsc` as provenance.
  **Demotion rule (native coherence, D4):** a pristine source whose
  control/dsc relation fields carry a literal Debian layer
  (`+debNuK` / `+bN` / `~bpoN+M` in any constraint) is demoted to the
  re-emit class so those fields get transposed.  Measured 2026-07-12:
  ZERO pristine-class sources in the current archive trip this (the 6
  dscs with such Build-Depends literals are all transposed-version or
  tunneled already) — the rule is a guard for future snapshots, not a
  live cost.
- **Transposed and/or patched**: re-emit with `dpkg-source -b` from the
  exact tree the binaries were built from (post-patch), with a
  synthesized top `debian/changelog` entry at the final version and a
  fixed `SOURCE_DATE_EPOCH` (changelog-entry date) so the emit is
  byte-deterministic.  `.orig.tar.*` is **always passed through
  verbatim from the snapshot, never regenerated** — it is shared across
  `+p` revisions and across builders; a repacked orig is an instant
  federation `hash_conflict`.
- **Forks**: `fork_mirror` already emits their `.dsc`
  (`fork/source/repo/`) — publish those as-is.
- **Tunneled**: we did not build them; re-emitting at an `+asg` version
  would fabricate provenance.  Publish their upstream source verbatim
  and fix the binary↔source linkage via D3.

### D2 — source version = the version its binaries ship at

`transposed_version(source_version, patch_level=P)` — identical rule to
the binaries (uniform P per source; forced `+bN` rebuilds are
binary-only and never move the source).  Filenames:
`<src>_<ver-noepoch>.dsc`, `<src>_<ver-noepoch>.debian.tar.xz`,
`<src>_<base-noepoch>.orig.tar.*` (unchanged).  Rationale: a built
binary's control carries `Source: <name>` with **no version**, so apt
infers source-version = binary version; the source must exist at
exactly that version for `apt-get source` to resolve.

### D3 — `Source:` field stamping in `transpose_deb`

`transpose_control_text` never touches the `Source:` field today.
Three cases dangle and must be stamped/rewritten at transpose time:

1. **Tunneled** binaries whose Version transposes (`shim-signed` at
   `…~asg1u1`): stamp `Source: <name> (<upstream source version>)` (the
   pre-transpose value is already in hand — it feeds
   `X-Athena-Upstream-Version`), so apt resolves the verbatim-published
   upstream source.
2. **Cross-base sibling binaries** (e2fsprogs `comerr-dev` style) whose
   control already carries `Source: <name> (<version>)` — that embedded
   version must be **transposed** to point at the re-emitted source.
3. **Forced rebuilds** (`+bN` appended): stamp
   `Source: <name> (<version-sans-+bN>)` — the source never carries our
   binary-only suffix.

### D4 — the emitted source is a NATIVE Asgard source: ALL relation
### fields transposed; native Asgard is the only supported build platform

The transpose machinery exists because we build in a **Debian**
container from **Debian**-versioned inputs; a published Asgard source
must instead build on a **native Asgard** container with ZERO
post-build rewriting.  Therefore the re-emit transposes every literal
version constraint in `debian/control` AND the `.dsc`:

- the nine runtime relation fields (`Depends` … `Replaces`) in the
  binary stanzas — hand-written `+deb` constraints must not survive
  into a natively-built binary.  Substvars
  (`${shlibs:Depends}`, `(= ${binary:Version})`) pass through the
  rewriter unchanged and resolve natively at build time.
- `Build-Depends`/`-Arch`/`-Indep` + `Build-Conflicts*` (NEW to the
  field set — `_NMU_STRIP_FIELDS` doesn't include them): `+asg` sorts
  **below** `+deb` (`a` < `d`), so an untransposed floor
  `>= 1.2-3+deb12u1` is unsatisfiable against our repo.

Rewriting uses the existing `bump._rewrite_control_text` machinery —
binNMU/backport strip and the tunnelled-target exemption
(`keep_binnmu_names`) included.  Stance, stated honestly:

- **Supported build platform: native Asgard only.**  Building an
  independent distribution's source in an upstream container is a
  non-goal.  Until `IncludeBuildClosure` ships the build closure in the
  repo (self-hosting milestone), the published sources are provenance/
  availability artifacts that nothing can officially rebuild; the fact
  that Debian's `+deb` versions happen to satisfy `>= +asg` floors
  (making snapshot-environment rebuilds de-facto work) is incidental,
  not supported.
- No existential poison pill (a fork-only Build-Depends) to hard-block
  Debian builds: version math cannot block them anyway (`+athena` also
  sorts below `+deb`) and it buys nothing.
- Verbatim-published pristine/tunneled sources keep their upstream
  fields untouched (no `+asg` anywhere in them; they are upstream
  artifacts, republished for provenance).

### D4b — native / non-native environment awareness

Building OUR emitted source on native Asgard must not re-enter the
transpose pipeline.  Today that holds *by construction* (`transpose()`
no-ops without a trailing `+debNuK`; the `+asg` constraint guard; the
patch set folded into `debian/patches` hashes empty → `P=0`, no second
`+p`) — but accidental correctness is not a mode.  The build path
gains an explicit environment check: read the build container's
`/etc/os-release` `ID` (`debian` = non-native → transpose pass ON;
`asgard` = native → transpose pass OFF, and version/dep inputs are
asserted already-`+asg`).  A mismatch (non-native pipeline pointed at a
native container, or vice versa) fails loudly.  This check is also the
gate for running athena-build ON Asgard itself later; the native
container base (bootstrapped from OUR mirror, not the snapshot) is
IncludeBuildClosure-era work, out of MAT-02 scope.

### D5 — identity-scrub exemption

Source packages are provenance artifacts: their content (debian/ dirs,
changelogs, upstream code) legitimately says "Debian" everywhere, and
scrubbing would corrupt checksums and patches.  **Sanctioned
exception** to the ship-as-Asgard invariant, scoped to
`dists/*/main/source/` — needs an explicit carve-out in the identity
audit so MAT-03-adjacent scans don't false-positive on it.

### D6 — dbgsym suite gets no Sources

`<codename>-debug` remains binary-only (Debian convention).

## Federation & repo mechanics (the checklist the assessment surfaced)

- **Build records**: emitted source files land in the record's
  `outputs` + `output_hashes` (they then flow through claims,
  `verify/refresh_output_hashes`, chroot preflight, and reclaim for
  free).
- **Claims + closure ledger**: source files are claimed like any output
  and MUST appear in `published_ledger_entries` (else peers can't pull
  them and `mirror audit` reads them as orphans).  Component routing:
  same component as the source's binaries.
- **`mirror pull`**: destination routing is `.deb`-shaped
  (`deb_dest_for_filename`) — extend for `.dsc`/`.tar.*`/`.asc` to
  `dir_repo_main_source`.  Restore-own covers them once records carry
  their hashes.
- **Prune safety**: obsolescence pruning must never reap an
  `.orig.tar.*` still referenced by a live `.dsc` (orig is shared
  across revisions).  The prune walk needs a dsc-reference count.
- **Audit**: InRelease already pins the Sources index (apt_repo emits +
  signs it); `mirror audit` and `repo audit` must add Sources-index
  cross-checks (claim ↔ Sources entry ↔ pool file) mirroring the
  Packages checks.
- **Size**: first source publish is tens of GB (orig tarballs; firefox
  ~500 MB, libreoffice, linux…).  One-off rsync cost to the mirror
  host; check mirror disk headroom first.
- **ISO**: sources are NOT staged into `/cdrom/pool` (size); GPL
  availability is satisfied by the mirror (written-offer model).

## Oracle 2 — verdict preservation over the full pool (2026-07-12)

Every literal constraint in the snapshot universe (77,897 build-relation
bounds from Sources + 237,046 runtime bounds from Packages) was checked
for VERDICT PRESERVATION: Debian-world verdict == transposed-Asgard-world
verdict.  Result: **106 flips (0.034%)**; all 22,934 exact `=` pins and
all 31 tunnelled-target bounds preserve.  Our 1007 sources: 11,023
build-dep groups, 6 unsatisfiable in the Debian world vs 8 in the
full-asg world.  IncludeBuildClosure gap measured: 874 build-dep names
absent from the current repo.  The 106 flips fall in three classes:

1. **Legacy `Nb` binNMU floors** (2; `gnome-contacts` → `libfolks-dev
   (>= 0.15.5-2b)`): the legacy no-`+` binNMU form isn't stripped by the
   constraint op.  Fix in stage 1.
2. **Punctuation-tail ceilings** (5; `erlang-base (<< …+deb12u1.0)`,
   `mosquitto (<< …+deb12u1.1~)`): `_TRANSPOSE_RE` requires the token at
   `$` (or a bare `~` tail), so a `.0` / `.1~` apt-ceiling suffix blocks
   the transpose.  Fix in stage 1 (extend the tail to punctuation-only
   suffixes).
3. **binNMU-era `Breaks`/`Conflicts` ceilings** (99): upstream encodes
   "built before transition X" in buildd rebuild ordinals that our world
   erases; no pure version rewrite preserves the verdict.  Correct fix
   is TARGET-AWARE: when the Debian-world verdict against the universe
   target was False only because of the target's `+bN`, demote the
   ceiling `X` → `X~` so our (always co-rebuilt) package escapes too.
   **LIVE finding**: shipped `libc6-dev` Breaks pool-shipped
   `libasyncns-dev 0.8-6` and `libatm1-dev 1:2.5.1-4` today (pre-existing
   597a2ca blind spot, install closure unaffected).  Own ticket —
   operator to assign a TODO id; lands in the same
   `transpose_control_text` machinery.

## Implementation stages (each triad-gated)

1. **bump: predictor + `Source:` stamping + oracle-class fixes.**
   `source_package_version()` predictor (D2); the three `Source:`
   field cases (D3) in `transpose_deb`/`transpose_control_text`;
   oracle-2 class-1 (legacy `Nb` strip in the constraint op) and
   class-2 (`_TRANSPOSE_RE` punctuation tail) fixes.  Pure-function
   tests; oracle 2 re-run must drop 106 → 99 with zero new flips.
2. **`source_emit.py`: the emit engine.**  Productionize the oracle:
   classify (pristine-verbatim / re-emit), synthesize changelog,
   deterministic `dpkg-source -b`, full relation-field transposition
   incl. `Build-Depends*`/`Build-Conflicts*` (D4; substvars pass
   through), sha-record.  Standalone, no session state.  The oracle
   script becomes its regression harness (fixture on a handful of
   synthetic sources + the real-archive JSONL as a pinned baseline).
   Includes the native/non-native environment check (D4b) in the build
   path: os-release `ID` of the container ↔ pipeline transpose mode,
   mismatch = loud failure.
3. **`source emit` operator command (backfill path first).**  Walks
   built build-records, emits from `source/` + `patch/source/` into
   `dists/<codename>/<comp>/source/`-adjacent pool, updates each
   record's `outputs`/`output_hashes`.  Retroactive for all 1005
   currently-built sources — **no rebuilds needed**.  Then wire the
   same emit into the per-build path (post-patch, pre-`dpkg-buildpackage`)
   so future builds emit as they go.
4. **Index + audit.**  Verify `dpkg-scansources` picks up the pool
   (already wired); extend `repo audit` + `mirror audit` with the
   Sources cross-checks; identity-audit carve-out (D5).
5. **Federation.**  Claims for source outputs (they follow from stage 3
   records via `generate_pending_claims` — verify, don't assume),
   closure-ledger inclusion, `mirror pull` routing + restore-own for
   source artifacts, orig-aware prune guard.
6. **CONF-03 mini-scope.**  Quilt-fold for the 2 upstream-touching
   patch sets (`cryptsetup`, `curl`) — fold `patch/source/<pkg>` into
   `debian/patches` + series via quilt at emit time (NOT a general
   process change; full CONF-03 stays its own ticket).
   Stopgap if deferred: `dpkg-source -b --auto-commit` emits a single
   folded patch — decide explicitly, don't default into it.
7. **End-to-end validation.**  `source emit` all → `repo index` →
   `apt-get source <pkg>` round-trip against the local repo for one
   package per class (pristine / transposed / patched / fork /
   tunneled / cross-base) → `dpkg-buildpackage` one re-emitted source
   in the build container → `mirror publish` → remote `mirror audit`
   clean → peer-side `mirror pull` fetches sources.
8. **Docs** (separate branch, operator-validated): versioning-mechanics
   gains a "source packages" section (D1–D4 tables), mirror-setup layout
   + pull notes, release.md, README feature line.  CHANGELOG one-liner:
   `- **Source packages published (Sources index + pool .dsc)** (scripts/source_emit.py; see docs/versioning-mechanics.md).`

## Open questions (operator)

1. `.orig.tar.asc` upstream-signature files: `source sync` doesn't fetch
   them today (oracle warning on cryptsetup/curl).  Fetch + publish
   them alongside (cheap, better provenance), or drop the signing-key
   reference at emit?
2. CONF-03 mini vs `--auto-commit` stopgap for stage 6.
3. Mirror disk / transfer budget for the first source publish
   (~estimate before pushing; `linux` + `libreoffice` + `firefox-esr`
   orig tarballs alone are ~1 GB+).
4. Does `source emit` claim source files under the SAME seq stream as
   binaries (one publish transaction), or as a separate labelled
   publish?  (Plan assumes same stream — simplest and audit-coherent.)

## Risks

- **`Source:` stamping touches every future transpose** — regression
  surface on the just-fixed constraint path; stage 1 pins all shapes
  before stage 3 mass-emits.
- **Same-name re-emits after a patch-level bump** produce new filenames
  (version moves), so append-only holds; the only same-name writes are
  pristine verbatim copies (byte-identical by construction).
- **`dpkg-source` version skew** host vs container: emit runs on the
  HOST (dpkg-dev now preflight-gated ≥ 1.20); determinism was proven
  with the host's 1.22.22.  Pin the expectation in the emit engine's
  startup check.
- **Snapshot advance mid-arc**: sources re-emitted for version N are
  frozen; a snapshot bump changes upstream versions → new emits, old
  ones age out via the normal obsolescence fold.
