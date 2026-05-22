# CONF-01 — Unified `repo/` layout migration

**Status:** in-progress (2026-05-22)
**Revert point:** tag `working-pre-repo-migration-2026-05-22` (commit `b5438c3`)
**Lead:** ticket CONF-01 in TODO.md §2

## Goal

Migrate `repo/` from the current segregated-by-role layout
(`repo/{main,doc,dbgsym,tests}/<pkg>.deb`) to an **apt-conformant
unified layout** where the directories ARE the apt repo:

```
repo/
└── dists/
    ├── thor/                                               (main suite)
    │   ├── Release, InRelease                              (signed)
    │   ├── main/
    │   │   ├── binary-amd64/
    │   │   │   ├── Packages, Packages.gz, Packages.xz
    │   │   │   ├── Release                                 (per-arch)
    │   │   │   └── *.deb                                   (.debs co-located)
    │   │   ├── debian-installer/binary-amd64/
    │   │   │   ├── Packages, Packages.gz
    │   │   │   └── *.udeb                                  (udebs co-located)
    │   │   └── source/
    │   │       ├── Sources, Sources.gz
    │   │       └── *.{dsc, tar.*, debian.tar.*}            (Q1 decision)
    │   ├── doc/binary-amd64/      {Packages*, Release, *.deb}
    │   └── tests/binary-amd64/    {Packages*, Release, *.deb}
    └── thor-debug/                                         (debug suite — Q2 decision)
        ├── Release, InRelease
        └── main/binary-amd64/     {Packages*, Release, *.deb}    (dbgsym pkgs)
```

Operator consumes via:
```
# Default — what the installer + post-install use:
deb [signed-by=/etc/apt/keyrings/asgard.gpg] file:///path/to/repo thor main

# Optional add-ons:
deb [signed-by=…] file:///path/to/repo thor main doc tests
deb [signed-by=…] file:///path/to/repo thor-debug main      ← dbgsyms
```

## Decisions

| Q | Choice | Rationale |
|---|---|---|
| **Q1**: where source artifacts (`.dsc`, `.tar.*`) live | `dists/thor/main/source/` | apt convention; `dpkg-scansources` targets it naturally |
| **Q2**: dbgsym as suite or component | **separate suite** `thor-debug` | matches Debian/Ubuntu strict convention (`bookworm-debug`, `jammy-debug`) |

## Why this layout

- One source of truth: the apt indexes (Packages files) instead of "walk
  the filesystem then re-derive metadata."  Audits can read the index
  directly, no DebFile-per-pkg fs walk (the perf trap that bit STA-19).
- `apt_repo.py` generation becomes trivial: the dirs ARE the apt repo;
  no flatten step, no symlink farm, no staging copy.
- Removes redundancy: today iso_installer.py copies `repo/` to
  `staging/pool/` just to run dpkg-scanpackages on a flat tree.  After
  migration the staging-copy stays (for cdrom assembly) but its
  generation is read-from-already-indexed-state, not re-derive-from-fs.
- File path uniqueness across suites: thor + thor-debug have disjoint
  binary-amd64/ dirs, so a `package-foo_1.0_amd64.deb` and its
  `package-foo-dbgsym_1.0_amd64.deb` don't collide on filename
  (they live in different suites' main/binary-amd64/ dirs).

## Non-goals

- **Pool layout Debianisation** (`pool/main/<initial>/<src>/<deb>`):
  not adopted.  Debian uses initial-letter hashing for fs perf at
  millions-of-packages scale; our ~5k packages fit fine in flat dirs.
  Plus the conventional pool/ split optimises for multi-suite dedup,
  which we don't have.

- **Migration to a different sources.list scheme** (deb822, signed-by
  rotation, multi-mirror at the same suite): out of scope for CONF-01;
  filed as separate considerations under CONF-02 / COMP-02 when those
  come up.

## Sequencing — 6 stages

Each stage is one commit.  Stage ordering is **deliberately
additive-first** so the breakage window for any consumer is the single
Stage D commit (a single atomic update of every path-reader).

### Stage A — `scripts/apt_repo.py` scaffold (no behaviour change)

Lift the 7 existing helpers from `iso_installer.py` into a new
`scripts/apt_repo.py`.  iso_installer.py imports from the new module.
Zero behavioural change to the existing ISO build path — pure code
motion.

- **Files touched:** `scripts/apt_repo.py` (new),
  `scripts/iso_installer.py` (delete locals + add imports).
- **Tests:** existing ISO-build tests pass unchanged.
- **Operator action required:** none — verify by gates only.

### Stage B — `cmd_index_repo` command (in-place generation)

Add `cmd_index_repo` dispatcher in `build.py`.  Reads
`config.build_codename`, generates indexes under
`repo/dists/<suite>/...` against the FUTURE .deb locations (paths
under `dists/<suite>/<comp>/binary-<arch>/`).  At this stage the
.debs are still at the OLD locations (`repo/<comp>/`), so the
indexes will be **wrong** until Stage C runs — but they don't
affect any code path until Stage D updates consumers.

- **Files touched:** `scripts/build.py` (new cmd), `scripts/apt_repo.py`
  (gain `generate_repo_indexes(repo_root, suites_spec, …)` orchestrator).
- **Tests:** unit test against a tmp dir with fake .debs (uses
  `_make_fake_deb` pattern from the COMP-14 tests).
- **Operator action required:** none for shipping; optional `index_repo`
  invocation to look at the generated output and confirm shape — but
  the indexes are intentionally "wrong" (point at FUTURE paths) until
  Stage C, so the smoke test is mostly "the command runs cleanly and
  produces the expected file tree."

### Stage C — `cmd_migrate_repo_layout` one-shot migrator

Add `cmd_migrate_repo_layout [--dry-run]` in `build.py`.  Walks the
current `repo/{main,doc,dbgsym,tests}/` dirs and `os.rename`s each
.deb / .udeb / .dsc / .tar.* into the new layout.  Pre-migration
snapshot of `repo/` to `/tmp/repo-pre-migration-<timestamp>.tar.zst`
as safety net.  `os.rename` is atomic on the same filesystem — no
half-migrated state possible.

- **Files touched:** `scripts/build.py` (new cmd).
- **Tests:** unit test with fake repo fixture — pre-state has
  `repo/{main,doc,...}/foo.deb`, post-state has them all at the new
  paths, snapshot tarball exists.
- **Operator action required:**
    1. Run `cache build` to ensure repo/ is in a known consistent state.
    2. Run `package migrate_repo_layout --dry-run`.
       **Expected output:** a printed table listing each .deb that
       would move (source → destination), grouped by suite/component.
       No actual fs changes.  Exit 0.
    3. Review the dry-run output.  Confirm: file counts match
       `find repo/main -name "*.deb" | wc -l` etc.
    4. Run `package migrate_repo_layout`.
       **Expected output:** `Snapshot: /tmp/repo-pre-migration-<ts>.tar.zst`
       followed by the same table with `[moved]` markers, plus
       a summary line `N debs, M udebs, K sources, J dbgsyms moved`.
       Exit 0.
    5. Spot-check: `ls repo/dists/thor/main/binary-amd64/ | head -5`
       and `ls repo/dists/thor-debug/main/binary-amd64/ | head -5`.
       Both should list .debs.  Old `repo/main/`, `repo/doc/`,
       `repo/dbgsym/`, `repo/tests/` should be empty (or absent).

After Stage C, the layout is migrated but consumers still look at OLD
paths.  Until Stage D lands, anything that reads `repo/main/*.deb`
WILL fail.  The two stages MUST be applied together.

### Stage D — consumer updates (one big atomic commit)

Update every code path that reads/writes repo path strings to the new
layout.  Roughly:

- `utils.py:BuildConfig` — re-target `dir_repo_main`, `dir_repo_doc`,
  `dir_repo_dbgsym`, `dir_repo_tests` to the new `dists/<suite>/...`
  paths; add `dir_repo_main_udeb`, `dir_repo_main_source`,
  `dir_repo_debug_main` (thor-debug equivalent).
- `buildcontainer.py:_segregate_built_artifacts` — re-target the
  rename destinations to the new layout.  Uses
  `utils.classify_repo_subdir` to choose component + suite.
- `cache.py` — re-target fork-pkg discovery walk.
- `iso_installer.py:_stage_pool`, `_find_kernel` — re-target source
  paths.
- `dependencytree.py:parse_sources` — re-target source artifact
  discovery to `dists/thor/main/source/`.
- `grub_assembly.py:_find_grub_deb` — re-target to
  `dists/thor/main/binary-amd64/`.
- `chroot.py` — re-target.
- `package.py` audit helpers — re-target (Stage E will rewrite these
  to read Packages files, but Stage D first makes them functional
  again on the new layout).
- All `cmd_*` in build.py touching repo paths.
- `tests/test_module.py` — update ~30 fixture-construction call sites.

- **Files touched:** ~10 production files, ~30 tests.
- **Tests:** full suite must pass.  Particularly anti-regression on
  fork-mirror tree-hash detection (cache build should not see any
  .debs as "missing" or "stale" post-migration).
- **Operator action required:**
    1. Pull the Stage D commit.
    2. Run `cache build`.
       **Expected output:** NO `tree-hash mismatch` / `wiped stale` /
       `invalidating` log lines for any fork package (athena-branding,
       athena-installer-data, base-files, etc.).  Cache build should
       complete with `XXXXX package names indexed` and no rebuilds
       triggered.
    3. Run `dep parse`.
       **Expected output:** same package counts as the
       pre-migration build log (e.g. "1339 canonical packages, 815
       source packages").
    4. Run `source build athena-installer-data` (small + fast).
       **Expected output:** the new .udeb lands at
       `repo/dists/thor/main/debian-installer/binary-amd64/athena-installer-data_1.2.0_all.udeb`,
       NOT at the old `repo/main/`.
    5. Run `iso build installer`.
       **Expected output:** completes without errors; the produced
       ISO contains the same package set as before.
    6. Boot the ISO in QEMU; confirm install completes end-to-end
       (smoke test, not exhaustive).

### Stage E — audits use the Packages index

Rewrite `package audit`, `package audit_nmu`, `package strip` (the
audit-only helpers — `strip` writes too) to read the Packages files
under `repo/dists/<suite>/<comp>/binary-<arch>/` instead of walking
the filesystem and DebFile-opening each .deb.

- **Files touched:** `scripts/package.py`, related `cmd_*` in
  build.py, tests.
- **Tests:** existing audit-test fixtures rebuild against the new
  index-based code path.
- **Performance impact:** `package audit_nmu` over the full repo
  drops from minutes (DebFile-per-deb) to seconds (single Packages
  parse).
- **Operator action required:**
    1. `package audit_nmu`.
       **Expected output:** runs in <5 seconds (vs. ~minutes pre-fix);
       exits 0 if all packages are NMU-clean (they should be — STA-19
       was the last NMU-related fix).

### Stage F — docs + close CONF-01

- Update `fork/source/README.md` paths (Current packages table
  references to repo/main/ etc.).
- Update `docs/branding-methodology.md` if any catalogue row
  references repo paths.
- Update `README.md` with the apt-repo consumption snippet
  (sources.list one-liner + signed-by setup).
- Close CONF-01 in TODO.md; move to docs/done.md.
- Mark CONF-02 (signing sources.list.d wiring) unblocked.

## Rollback procedure

If anything goes catastrophically wrong:

```
git reset --hard working-pre-repo-migration-2026-05-22
# If repo/ was migrated by Stage C:
tar xf /tmp/repo-pre-migration-<timestamp>.tar.zst -C /
# Verify:
ls repo/main/ | head    # should show .debs at old location again
```

Or, less drastically, revert individual stages via `git revert
<commit>` — they're additive enough to revert individually except
for Stage C+D which must be reverted together.

## Files touched per stage (summary)

| Stage | New files | Modified files | Test files |
|---|---|---|---|
| A | `scripts/apt_repo.py` | `scripts/iso_installer.py` | tests/test_module.py (~2 new) |
| B | — | `scripts/build.py`, `scripts/apt_repo.py` | tests/test_module.py (~3 new) |
| C | — | `scripts/build.py` | tests/test_module.py (~3 new) |
| D | — | ~10 production files | tests/test_module.py (~30 modified) |
| E | — | `scripts/package.py`, `scripts/build.py` | tests/test_module.py (audit tests) |
| F | — | README, fork/source/README, branding-methodology, TODO, done | none |

## Open items / unknowns

- **Stage C atomic-rename limitation:** `os.rename` is atomic only on
  the same filesystem.  If `repo/` and `repo/dists/...` end up on
  different mounts (unlikely — they're under the same working_dir),
  `os.rename` raises `OSError(EXDEV)`.  Detect at start and fall back
  to `shutil.move` (which copies+removes — slower but correct cross-fs).

- **Stage D fork-mirror tree-hash:** fork_mirror keys tree-hash on
  source dirs (`fork/source/<pkg>/`), not built artifacts.  Migration
  shouldn't trigger any tree-hash mismatch.  If it does, the bug is
  in fork-mirror's code, not the migration — bail and investigate.

- **Stage D apt index regeneration:** after Stage D, the indexes
  generated by Stage B (which assumed .debs at new paths) become
  correct.  But they need re-generating because new .debs may have
  landed in the meantime.  Stage D's verification step 4 (source
  build athena-installer-data) will naturally trigger reindex via
  buildcontainer.py's segregate step calling apt_repo regeneration —
  OR we can make `cmd_index_repo` part of the normal source-build
  finalisation (extension to Stage E).
