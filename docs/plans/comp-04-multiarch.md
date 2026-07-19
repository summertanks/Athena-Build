# Plan — COMP-04: architecture support beyond amd64 (i386 + arm64)

## Status: DRAFT (2026-07-19) — full-tree arch audit complete; stage 0
## oracle not yet run; no code landed

## Scope (operator-confirmed 2026-07-19)

- **arm64** (aarch64) — modern ARM, EFI boot, full Debian bookworm coverage.
- **i386** — **full installable distribution** (own pool, installer ISO,
  live ISO, disk image) for legacy 32-bit/BIOS x86.
- **Build strategy: native builders per arch.**  i386 builds in 32-bit
  personality containers on existing amd64 hardware (no emulation —
  the CPU executes 32-bit code natively).  arm64 builds on a real ARM
  builder VM that **federates into the same mirror** as BS1.  No qemu
  user emulation, no cross-compilation — this is how Debian itself
  builds, and it is what the federation layer (builder claims, ownership,
  hash-conflict gates) was built to support.
- armhf: out of scope.

## Current state — what the audit found (2026-07-19, 3-way full-tree sweep)

The tree is in *far* better shape than the TODO row suggests.  The
selection / dependency / versioning core is **already arch-clean**:

| Layer | Verdict | Evidence |
|---|---|---|
| Config key | EXISTS — `[Build] ARCH` in `config/distro.conf:15` ("i386, amd64 or all" comment) → `utils.py:2781` | no fallback; missing key raises |
| Index fetch | arch-parameterized — `binary-{arch}` / `debian-installer/binary-{arch}` (`utils.py:520,539`) | cache filenames arch-disambiguated via URI |
| Cache/selection | arch-clean — dpkg `DpkgArchTable.matches_architecture` gates debs (`cache.py:674`), udebs (`cache.py:1022`), source wildcards (`cache.py:733-744`); `Architecture: all` handled | zero amd64 literals in cache.py |
| Dependency res. | arch-clean — `arch_filter.py` foreign-toolchain drop is `build_arch`-relative; `virtual_build.py` filename synthesis parameterized | verify, don't rewrite |
| Build container | arch-parameterized — `mmdebstrap --architectures={config.arch}` (`base_rootfs.py:99`), `dpkg-buildpackage -a {arch}` (`buildcontainer.py:1222`) | Dockerfile is `FROM scratch` + rootfs tar |
| Version machinery | arch-neutral and arch-preserving — `bump.py` parses/reconstructs the `_<arch>` filename token, never interprets it | transpose identical across arches |
| Repo dirs | arch-parameterized — all `dists/.../binary-{self.arch}` from config (`utils.py:3232-3263`) | |
| Federation ledger | arch-aware — closure ledger keys `<package>|<arch>` (`schema.py:84`, `mirror.py:1915`) | |

The debt concentrates in **six literal clusters** and **two structural
gaps**:

### Literal clusters (mechanical, amd64-behavior-preserving to fix)

1. **Publish-side Release**: `apt_repo.py:144` `_ARCH='amd64'` (ISO repo
   path, `iso_installer.py:211`) and `apt_repo.py:780`
   `APT::FTPArchive::Release::Architectures=amd64` — the latter poisons
   even the otherwise arch-aware `generate_repo_indexes` path (both
   generators call `_generate_top_release`, `apt_repo.py:209,403`).
2. **Kernel-deb regex, triplicated**: `iso_installer.py:58`
   (`^linux-image-(...)-amd64_`), `iso_installer.py:338` (glob),
   `commands/cmd_build.py:809` — the load-bearing image-side hardcode.
3. **Artifact names**: `iso.py:359-360`, `commands/cmd_build.py:606-607`,
   `commands/cmd_mirror.py:1067,1071`, `print_commands.py:497-498,606-609`
   (`print_commands.py:467` already computes `_arch` and then doesn't
   use it).
4. **Container grub toolset**: `buildcontainer.py:1873` installs
   `grub-pc-bin grub-efi-amd64-bin` by literal.
5. **Mirror-side defaults**: `mirror.py:1194` `arches=('amd64',)`,
   `remote_localmirror.py:162` `'Architectures: amd64 all'`,
   `commands/cmd_mirror.py:1153` `arch='amd64'` call-site literal.
6. **Config lists**: `pkg.list:12,16,47-48` (`linux-image-amd64`,
   `linux-headers-amd64`, grub bins), `installer-defaults.list:21-29`
   (grub metas + microcode), `pool.list:51-52,142`, `build.conf:126`
   Tunneled arch-named packages, `installer.list:262-311` (stock d-i
   `cdrom/amd64.cfg` seed).

### Structural gaps (genuinely new logic)

- **G1 — kernel-flavor ≠ dpkg arch.**  dpkg arch `i386` boots kernel
  flavor `686-pae`; `arm64` → `arm64`; `amd64` → `amd64`.  Every image
  builder currently collapses this distinction into the one literal.
  Needs a single authority table (see D2).
- **G2 — arch:all ownership across builders.**  Claims carry **no arch
  field** (`schema.py:187`); conflict detection keys on filename alone
  (`reconcile.py:334-407`, docstring at `:338` wrongly assumes filename
  encodes arch uniquely).  Two builders of different arches both
  producing `foo_1.0_all.deb` with non-identical bytes ⇒ CRITICAL
  `hash_conflict` ⇒ publish halt.  Multi-builder multi-arch **cannot
  ship** without an ownership rule (D3).
- **G3 — arm64 boot is EFI-only.**  `disk_image.py:458,478,650,821`
  hardcode `x86_64-efi`/`i386-pc` grub targets and module dirs; the
  BIOS-Boot partition + i386-pc branch must be skipped wholesale on
  arm64 (Debian's own arm64 media are GRUB-EFI-only, `BOOTAA64.EFI`).
  i386 conversely keeps the BIOS path and only swaps the EFI flavor.

## Design decisions

### D1 — one builder = one arch; the *mirror* is where multi-arch happens

Each builder machine keeps today's single-arch model end to end
(selection, cache, container, records, images) with its own
`distro.conf ARCH`.  The published repo becomes multi-arch by
federation: `dists/thor/main/binary-{amd64,i386,arm64}/` each populated
by its owning builder, one shared `pool/`, one top-level Release whose
`Architectures:` line is **derived from the on-disk `binary-*` dirs**
(the pattern `local_mirror.py:283-295` already uses) instead of the
config arch.  No per-machine code learns to loop over arches — this is
the smallest possible perturbation of a heavily-validated single-arch
pipeline, and it is exactly the topology the federation layer (claims,
flock, ownership matrix, coord-head) was designed for.

Builders: **BS1** (amd64, existing GCP VM) — also hosts the i386
builder as a second checkout + second builder identity (**BS3**,
`ARCH = i386`, 32-bit containers on the same CPU).  **BS2** (arm64) —
new ARM VM (GCP Axion/C4A class; T2A/C4A region availability to be
checked at provision time), federating over the same mirror SSH.

### D2 — `arch_profile.py`: single authority for per-arch facts

New small module consumed by every image builder, `cmd_build`'s kernel
pick, `buildcontainer`'s grub toolset, and the seed-list machinery:

| key | amd64 | i386 | arm64 |
|---|---|---|---|
| kernel_meta / headers | linux-image-amd64 | linux-image-686-pae | linux-image-arm64 |
| kernel_flavor_re | `-amd64` | `-686(-pae)?` | `-arm64` |
| grub_bins (container + lists) | grub-pc-bin, grub-efi-amd64-bin | grub-pc-bin, grub-efi-ia32-bin | grub-efi-arm64-bin |
| grub_install_targets (disk) | x86_64-efi + i386-pc | i386-efi + i386-pc | arm64-efi only |
| efi_removable_name | BOOTX64.EFI | BOOTIA32.EFI | BOOTAA64.EFI |
| bios_boot (El-Torito/BIOS-Boot part) | yes | yes | **no** |
| microcode roots | intel-, amd64-microcode | intel-, amd64-microcode | — |
| console_extra | (none) | (none) | `console=ttyAMA0` fallback entry |
| d-i stock seed | cdrom/amd64.cfg | cdrom/i386.cfg | cdrom/arm64.cfg |

This kills the triplicated kernel regex and gives G3 a single switch.

### D3 — arch:all and source artifacts are owned by the PRIMARY builder

New role in local.conf: `archallowner = true` on BS1 only.  Non-primary
builders build `_all.deb` outputs as build by-products (records keep
them; local repo keeps them for closure) but `generate_pending_claims`
**drops `_all.deb` and source-artifact pending claims** on non-primary
builders — mirroring the existing foreign-toolchain drop at the same
choke point (`publish.py:215`).  Foreign builders receive arch:all
packages and sources via `mirror pull` like any other already-claimed
file.  This resolves G2 without touching the filename-keyed conflict
detector — by construction only one builder ever claims an `_all`
filename.  MAT-02 source publish (arch-independent by definition)
stays a BS1 concern; foreign builders run `source emit verify` only.

### D4 — claims gain an `arch` field (additive schema)

Populate from `build_arch` at claim generation; additive like the
`seeds_raw` precedent (no schema version bump, HMAC covers it).  Not
load-bearing for D3 (which is a publish-side filter) but makes audits,
ownership displays, and future per-arch deprecation explicit instead of
filename-inferred.

### D5 — arch-qualified entries in seed lists (dpkg syntax)

Seed lists gain optional dpkg-style arch restrictions:
`linux-image-amd64 [amd64]`, `grub-efi-arm64-bin [arm64]`,
`amd64-microcode [amd64 i386]`.  The surfaces reader filters against
`config.arch` via the same `DpkgArchTable` match the cache uses.
Unqualified lines apply to every arch (today's behavior — zero
migration for the 400+ existing entries).  SELECT-01 `seeds_raw`
byte-fidelity is unaffected (raw bytes stored verbatim).  Alternative
rejected: per-arch list files — 3-way duplication of ~156 operator
doc-comment lines and a standing drift hazard.

### D6 — one snapshot pin across all builders

All arches select from the SAME snapshot id (today
`20260705T190150Z`).  Claims already carry `snapshot`; the federation
gate should WARN on cross-builder snapshot skew (FED-02 overlap — this
plan takes the warn-only half).

### D7 — i386 lifetime bound

The pinned snapshot is bookworm: i386 is a full release arch there.
Debian 13 (trixie) drops i386 as an installable arch — **a future
snapshot advance to trixie ends the i386 port**.  Documented as an
accepted, dated constraint, not a blocker.

## Adversarial analysis

- **A1 — non-reproducible arch:all collision (G2).**  Worst failure
  mode: publish halt (CRITICAL hash_conflict) or, worse, silent
  strand of a signed claim if pushed around the gate.  Killed by D3 —
  and stage-3 tests must include the adversarial case: non-primary
  builder with a *differing* `_all.deb` in its record attempts publish;
  expect the claim filtered, not conflicted.
- **A2 — per-arch binNMU skew.**  Debian binNMUs are per-arch: the
  same source can sit at `1.0+b1` on amd64 and `1.0` on arm64, and
  *selected versions may differ per arch*.  Our transpose strips/anchors
  `+bN` per-arch from each arch's own Packages index, and
  `universe_lookup` (STA-55 ceiling demotion) is built from the
  builder's own cache — self-consistent per arch.  Residual risk:
  arch:all packages' rewritten relations are computed on BS1's amd64
  universe but installed on i386/arm64 targets whose binary versions
  may differ.  The +asg anchoring makes constraints version-shape
  compatible; **stage-0 oracle must quantify actual skew** before this
  is waved through.
- **A3 — selection closure holes on i386/arm64.**  Not every selected
  package exists on every arch (browser/toolchain edges).  A closure
  that silently shrinks is a SELECT-02-class hazard.  Stage 0 runs the
  full `cache parse` closure against i386 and arm64 bookworm indexes
  offline and diffs the selected sets three ways; any hole is an
  operator decision (drop from that arch's images vs drop the seed)
  BEFORE code lands.
- **A4 — udeb virtual-name drift.**  d-i module udebs are arch-flavored
  (`ext4-modules-6.1.0-50-686-pae-di`); `installer.list`'s seed section
  is a copy of the stock amd64 cdrom list.  The udeb closure machinery
  is arch-clean, but the *seed names* must come from the per-arch
  stock list (D2 table row).  Stage 0 diffs the three stock lists.
- **A5 — foreign-arch bootstrap has no emulation net.**  There is no
  qemu-user-static/binfmt anywhere in the tree (verified) — by design
  under D1 (native builders never need it).  The i386-on-amd64 case is
  native (personality), and mmdebstrap `--architectures=i386` on an
  amd64 host works without emulation.  Risk is operational: someone
  runs an arm64 checkout on an amd64 host; the D4b native-base guard
  (`_assert_nonnative_base`) plus a new arch-vs-host preflight check
  must fail this loudly instead of at first configure script.
- **A6 — heavyweights on the ARM builder.**  libreoffice peaked at
  15.6 GB RSS on amd64; the ARM VM must be sized accordingly
  (≥16 GB, ideally 32 GB during world-build), then can be downsized
  for steady-state.  Same lesson as BS1's own sizing note.
- **A7 — concurrent publish.**  Two builders publishing near-
  simultaneously is already serialized by the remote flock +
  just-fetched revalidation; multi-arch adds no new window.  Stage-3
  tests re-pin this with two different-arch builders in the
  federation lab.
- **A8 — mirror disk.**  Pool grows ~3× (amd64 4.6 GB debs → est.
  ~13-14 GB three-arch, sources shared).  The Oracle mirror host's
  capacity must be verified in stage 0; ISO surface also triples.
- **A9 — generated state is arch-baked.**  `published.manifest`,
  closure ledger, selection locks embed `binary-amd64` paths and
  `arch: amd64` rows.  These are per-builder artifacts under D1
  (each builder has its own) — no migration, but stage-3 audit code
  must not assume a single global arch when folding per-builder
  ledgers.

## Cascade impact map

| Subsystem | Files | Change class |
|---|---|---|
| Config read | `utils.py:2781` (+validation), `distro.conf` | mechanical |
| Arch profile (new) | `scripts/arch_profile.py` + consumers | new module |
| Publish/Release | `apt_repo.py:144,780` (+derive-from-disk), `remote_localmirror.py:162` | mechanical + D1 derive |
| Mirror pull/audit | `mirror.py:1194` (+multi-arch index walk), `cmd_mirror.py:1153` | mechanical + loop |
| Kernel pick | `iso_installer.py:58,338`, `cmd_build.py:809` → profile | mechanical via D2 |
| Artifact names | `iso.py:359`, `cmd_build.py:606`, `cmd_mirror.py:1067`, `print_commands.py` | mechanical |
| Container toolset | `buildcontainer.py:1873` → profile | mechanical via D2 |
| Disk image | `disk_image.py:456-481,644-650,820` | per-arch branches (G3) |
| Live/installer ISO | `iso.py` (EFI-only path), `iso_installer.py` (seed) | per-arch branches |
| Seed lists | `pkg.list`, `pool.list`, `installer-defaults.list`, `installer.list`, `build.conf` Tunneled | D5 qualifiers |
| List parser | `surfaces.read_*` | small feature (D5) |
| Federation | `schema.py` (+arch field), `publish.py` (D3 filter), `reconcile.py` docstring | additive + filter |
| Tests | `test_federation_coord.py` fixtures, images/installer pins, new per-arch cases | broad but mechanical |

## Stepwise plan

Each stage lands independently; amd64 behavior is pinned unchanged
throughout (stages 1-3 are pure refactor + additive under amd64).

- **Stage 0 — oracle (no code).**  Fetch bookworm i386 + arm64 indexes
  at the pinned snapshot; run the selection closure + transpose oracle
  offline for both; diff selected sets, binNMU skew (A2), udeb seed
  names (A4); verify mirror-host disk (A8).  **GATE: operator reviews
  the three-way diff before any code.**
- **Stage 1 — kill the literals.**  Clusters 1-5 → `config.arch` /
  derive-from-disk; new `arch_profile.py` with the D2 table; the three
  kernel-regex sites consume it.  Zero behavior change on amd64
  (test-pinned).
- **Stage 2 — seed-list arch qualifiers (D5).**  Parser + qualify the
  ~12 arch-specific entries.  amd64 selection closure byte-identical
  (pin via selection lock).
- **Stage 3 — federation arch-awareness.**  Claim `arch` field (D4),
  `archallowner` role + `_all`/source claim filter (D3), multi-arch
  Release derivation (D1), multi-arch mirror audit walk.  Federation-lab
  tests: two-arch publish, A1 adversarial case, A7 re-pin.
- **Stage 4 — i386 bring-up (BS3 on BS1 hardware).**  Second checkout +
  builder identity, i386 base rootfs + container, `cache parse` (expect
  stage-0 predicted set), world build (VM temporarily resized), publish
  federated, `mirror audit` clean with both arches.
- **Stage 5 — i386 images.**  Installer/live/disk via the profile —
  BIOS machinery already exists; validates D2 end to end on the easy
  port.  QEMU BIOS+EFI smoke (COMP-01h harness).
- **Stage 6 — arm64 bring-up (BS2).**  Provision ARM VM (≥16 GB for
  world build), migrate-style setup (mirror.key, federation register),
  native base rootfs, world build, federated publish + audit.
- **Stage 7 — arm64 images.**  EFI-only disk-image path, arm64
  grub-mkrescue ISO, arm64 d-i seed; QEMU (`-M virt` + AAVMF) smoke.
- **Stage 8 — docs + close.**  versioning-mechanics (per-arch binNMU
  note), mirror-setup (multi-builder), federation docs (D3 role),
  TODO/done.

## Impact analysis

- **Effort:** stages 0-3 ≈ 1-2 weeks of toolchain work, mostly
  mechanical with heavy test-pinning; stages 4-7 are compute-dominated
  (two full world builds ≈ 1007 sources each) plus image-boot
  debugging on arm64 (the only genuinely novel surface).
- **Compute cost:** i386 world build on temporarily-resized BS1
  (e2-standard-8/16); arm64 world build on a C4A/T2A VM sized 16-32 GB
  for the build window, downsized after.  Steady-state adds one small
  ARM VM.
- **Storage:** mirror pool ×3 (est. +9-10 GB), ISO surface ×3; BS1
  disk gains a second (i386) checkout+pool — the 80 GB disk will need
  review at stage 4.
- **Risk posture:** highest-risk items are front-loaded into stage 0
  (closure holes, binNMU skew) and stage 3 (arch:all ownership — the
  one place multi-arch touches the federation invariants).  arm64 boot
  (stage 7) is new code but low blast-radius (image-side only, no
  repo/claims interaction).
- **Explicit non-goals:** Multi-Arch: same co-installability semantics
  (`package.py:199` strips `:any`/`:native` — fine for distinct
  per-arch pools, revisit only if mixed-arch installs become a goal),
  armhf, qemu-emulated builds, cross-compilation, CONF-17 pool-layout
  migration (unified layout works multi-arch; CONF-17 stays parked).
