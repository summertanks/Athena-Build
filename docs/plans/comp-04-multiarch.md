# Plan — COMP-04: architecture support beyond amd64 (i386 + arm64)

## Status: STAGE 1.5 COMPLETE (2026-07-25) — B1/B2/B9 landed +
## dead constant + per-pid crash log; STAGE 1 COMPLETE (2026-07-19) — arch_profile.py landed, six
## literal clusters dead, amd64 behavior pinned unchanged (954be65).
## Stage 0 oracle GREEN.  Next: stage 2 (seed-list [arch] qualifiers)

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

### D1 — one builder = one arch; PER-ARCH REPO TREES, one shared coord

(Revised 2026-07-19, operator decision: per-arch repos.)

Each builder machine keeps today's single-arch model end to end
(selection, cache, container, records, images) with its own
`distro.conf ARCH`, and **publishes to its own complete apt repo
tree** on the mirror host:

    repo-amd64/
    repo-i386/
    repo-arm64/
    repo-coord/      (SHARED — one federation for all builders)

Uniform, distro-neutral naming for all three arches (operator
decisions: nothing is released to the environment yet, so no legacy
path is carried; and directory names do NOT embed the distribution
name — where a branded name is needed it is derived from the distro
identity in `distro.conf`, never baked into the layout).  The existing
`asgard/` tree is renamed `repo-amd64/` at stage-3 rollout (server-
side `mv` + re-registration of the mirror URL and public_url) and the
coord tree moves to `repo-coord/` the same way.  Local working dirs
use the identical convention per checkout
(`[Directories] Repo = repo-<arch>`) — one naming scheme everywhere.

Each per-arch repo is a full single-arch repo exactly like today's —
own `dists/thor/…/binary-<arch>/`, own pool, own InRelease.  No merged
multi-arch `dists/`, no derive-`Architectures:`-from-disk Release
logic, no cross-builder contention inside one tree: publish is
byte-for-byte today's flow pointed at a different remote root.
Builder-side, `dir_repo` already comes from `[Directories] Repo`
(`utils.py:3215`) and the remote root from the registered mirror URL —
per-arch checkouts just configure different values (zero layout code).

**Shared coord (operator decision).**  `coord_root_for()` derives the
coord tree from the pool URL (`mirror.py:2279`: `repo-i386` would
silently become `repo-i386-coord`), so mirror registration gains an
optional explicit `coord_url` key (default: derived) and all builders
point it at `repo-coord/`.  This keeps the year's federation
invariants global: one provenance chain, one ownership matrix,
cross-builder hash-conflict scan, one keyring/coord-head lineage.

**arch:all bytes in foreign repos.**  apt on i386/arm64 needs
`_all.deb` files indexed and present in ITS repo's pool, but under D3
only BS1 claims them.  Foreign builders therefore *republish the
primary's bytes verbatim*: fetched claim-verified via the
`neighbours_known` peer path from the primary pool, placed in the
foreign repo, indexed there — same filename, same bytes everywhere, so
the shared-coord conflict scan sees only harmless
`reproducible_duplicate`s.  A foreign builder's locally-rebuilt `_all`
with differing bytes is a publish-gate refusal (same `local_ahead`
machinery, message points at the primary's claim).

Builders: **BS1** (amd64, existing GCP VM) — also hosts the i386
builder as a second checkout + second builder identity (**BS3**,
`ARCH = i386`, 32-bit containers on the same CPU).  **BS2** (arm64) —
new ARM VM (GCP Axion/C4A class; T2A/C4A region availability to be
checked at provision time), federating over the same mirror SSH.

**Sources live in the primary repo only** (operator decision):
arch-independent by definition, indexed once under `repo-amd64/`;
i386/arm64 images bake an extra `deb-src` line pointing at the primary
repo — Debian-conventional, and the mirror's source pool does not
triple.

Client sources.list per arch (baked at image build via the D2
profile): `deb http://<mirror>/repo-<arch> thor main`, plus the
shared `deb-src http://<mirror>/repo-amd64 thor main` (primary).

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
  expect the claim filtered AND the byte-push refused until the
  primary's verbatim bytes are restored (per-arch repos, D1).
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
| Publish/Release | `apt_repo.py:144,780`, `remote_localmirror.py:162` — all → `config.arch` (per-arch repos: each Release advertises its ONE arch) | mechanical |
| Coord decoupling | mirror registration `coord_url` override (`coord_root_for`, `mirror.py:2279`); peer fetch of `_all` bytes via `neighbours_known` | small feature (D1) |
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
- **Stage 1 — kill the literals.**  Clusters 1-5 → `config.arch` (per-
  arch repos mean every Release simply advertises its own single arch —
  no multi-arch derivation anywhere); new `arch_profile.py` with the
  D2 table; the three kernel-regex sites consume it.  Zero behavior
  change on amd64 (test-pinned).
- **Stage 2 — seed-list arch qualifiers (D5).**  Parser + qualify the
  ~12 arch-specific entries.  amd64 selection closure byte-identical
  (pin via selection lock).
- **Stage 3 — federation arch-awareness.**  Claim `arch` field (D4),
  `archallowner` role + `_all`/source claim filter (D3), `coord_url`
  override + shared-coord registration (D1), peer-fetch of primary
  `_all` bytes into foreign repos, multi-repo mirror audit awareness
  (per-arch repos audited independently; shared coord folded once).
  **`mirror pull` destination routing (operator requirement
  2026-07-19): pulled files must land in the RIGHT directory for the
  new layout** — `.deb`/`.udeb` into the pulling builder's own
  `repo-<arch>/dists/<codename>/<comp>/binary-<arch>/` (resp.
  `debian-installer/binary-<arch>/`) tree, source artifacts into the
  PRIMARY repo's `source/` dir only (D1: sources are primary-only —
  a foreign builder's pull fetches source files for local build use
  but never places them in its own publishable tree).  The MAT-02
  `_artifact_dest_dir` source-routing and the restore-own path in
  `cmd_mirror` are the code to extend; both currently assume the
  single-repo layout.  Federation-lab tests: two-arch publish into
  sibling repo roots, pull-side routing round-trip (deb → own tree,
  source → primary/local-only), A1 adversarial case (foreign builder
  with differing `_all` bytes → publish-gate refusal, not
  hash-conflict), A7 re-pin.
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
- **Storage:** three sibling repo trees; concrete-arch pools are
  disjoint (+~9-10 GB), `_all.deb` bytes DUPLICATED per repo
  (+~2-3 GB total, the price of per-arch repo simplicity), sources
  NOT duplicated (primary-only); ISO surface ×3; BS1
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

## Stage 0 — RESULTS (2026-07-19, oracle run on BS1/GCP)

Method: isolated per-arch working dirs (`~/oracle-comp04/{i386,arm64}`),
real `cache build` + `cache parse` machinery, fresh selection baseline,
full 13-source layered index set (bookworm + -updates + -security ×
{main, contrib, non-free-firmware} + fork mirror) at the pinned
snapshot `20260705T190150Z`.  Probes over raw layered indexes for skew
and transpose.  amd64 reference = the live BS1 selection (1,615 bins /
1,007 sources).

### Verdict: GREEN — proceed to stage 1 as planned

| Probe | i386 | arm64 | Verdict |
|---|---|---|---|
| Closure size (debs) | 1,599 | 1,587 | vs amd64 1,615 — no mass loss |
| Sources locked | 999 | 991 | vs 1,007 |
| Udeb closure | 157 | 149 | seeds resolve cross-arch (see U1) |
| Dropped vs amd64 | 18 | 31 | ALL explainable (see below) |
| Gained vs amd64 | 2 | 3 | per-arch toolchain/sanitizer |
| Real version skew (layered) | **0** | **0** | after bookworm+updates+security overlay |
| binNMU-only skew | 2 (`apg`, `bc`) | 2 (same) | existing +bN machinery covers it |
| Transpose invariants | 0 fails / 21,354 versions (683 novel shapes) | 0 fails / 21,363 (583 novel) | arch-portable, proven at universe scale |
| Fork packages | selected `+athena2` @ i386 | same @ arm64 | per-arch builders rebuild forks natively (D1 ✓) |

### Closure diff — every drop explained

- **Kernel flavor (both arches)**: `linux-image/headers-amd64`,
  `linux-*-6.1.0-50-amd64`, `linux-kbuild`, `linux-compiler-gcc-12-x86`
  drop because the seeds are amd64-named — and **no replacement kernel
  is seeded** (the i386 run selects no `-686-pae`, arm64 no `-arm64`
  kernel).  Exactly the D2/D5 work items; the oracle run proves the
  seed mapping is the ONLY missing piece.
- **Secure-boot chain (both)**: `shim-*`, `mokutil`,
  `grub-efi-amd64-signed` — bookworm shim is amd64-only; consistent
  with the existing "no Secure Boot" posture (`pool.list:53`).
- **i386-specific**: `liblsan0`/`libtsan2` (sanitizers absent on
  i386 — gcc-12's arch-conditional deps followed correctly; note
  arm64 GAINS `libhwasan0` the same way), `libmfx1` (Intel media,
  64-bit only).
- **arm64-specific**: the whole x86 surface — grub-pc/efi-amd64,
  intel/amd64-microcode + `iucode-tool`, Intel/VMware/QXL video
  (`libdrm-intel1`, `xserver-xorg-video-{intel,vmware,qxl}`,
  `libxatracker2`, `libxvmc1`, `libsmbios-c2`), `libquadmath0`
  (no quad-math on arm64).
- **Gains**: `binutils-{i686,aarch64}-linux-gnu` (arch's own binutils
  alias), `fwupd-{i386,arm64}-signed`, `libhwasan0`.

**No surprise losses**: browsers, desktop, toolchain, federation
surface all intact on both arches.

### Additional findings

- **U1 — udeb seeds are virtual-name portable.**  `installer.list`
  seeds by virtual module names; each arch's flavored udebs
  (`*-686-pae-di`, `*-arm64-di`) Provide them, so the udeb closure
  resolved WITHOUT list changes (86 seeds resolve on i386, 82 on
  arm64 — the 4-seed delta is x86-only modules, to fold into D5).
- **U2 — SELECT-02 fixpoint machinery is arch-clean**: the two-pass
  shadow ran on both arches (i386 converged 1,348 → 1,344).
- **U3 — mirror host disk is a stage-4/6 blocker**: 45 GB total,
  23 GB free, repo 19 GB; three-arch pools + ISO surface ≈ +20 GB.
  Grow the mirror volume before the first foreign-arch publish.
- **U4 — headless UX gap (minor, file separately)**: `--yes` does not
  answer multi-choice provider prompts; a fresh-state headless
  `cache parse` dies with EOFError (`no input for required choice`).
  Oracle worked around via piped answers.  Candidate: `--yes` takes
  the displayed default.
- **U5 — per-arch fwupd signing chain**: `fwupd-<arch>-signed` exists
  per arch and was auto-selected — the Tunneled list in `build.conf`
  (amd64-named entries) needs D5 arch-qualification like the seeds.

### Gate

Operator review of the diff above.  On acceptance: proceed to
Stage 1 (literal kill + `arch_profile.py`) — no compute dependency,
all work on existing hardware.

## Adversarial analysis v2 — module-by-module (2026-07-19, five-sweep)

Five parallel adversarial sweeps over ALL modules (selection/build path,
repo/mirror/federation, images/installer, onboarding/operations,
source/versioning) against the decided design.  Every finding verified
in code with file:line.  Net verdict: the ownership/conflict core and
the selection machinery are genuinely ready; the coord-head/ledger/
audit trio, the patch/P machinery, and a handful of image-side paths
carry the real risk.  Stage sizing revised below.

### Blockers

| # | Site | Failure | Fix home |
|---|---|---|---|
| B1 | `buildcontainer.py:229` | Docker image tag omits arch — BS1/BS3 share tag `athenalinux:build-<rel>-<snap>` under the shared snapshot pin; reuse check hashes only the (arch-identical) Dockerfile → i386 builds silently run in the amd64 rootfs image (found independently by two sweeps) | stage 1.5: arch in tag + `athena.arch` label asserted on reuse |
| B2 | `buildcontainer.py:1222` | No 32-bit personality: nothing wraps the build in `setarch i386`; `uname -m` in the i386 container returns x86_64 → autotools/cmake mis-target (Debian's i386 buildds always run linux32) | stage 1.5: `linux_personality` field in D2 profile, wrap build cmd |
| B3 | `coord/publish.py:1484,1589` | Shared coord-head pins ONE scalar `inrelease_sha256` + `closure_ledger_sha256` — last publisher's arch wins; every other builder's audit fires CRITICAL `inrelease_sha_mismatch` and the anti-rollback pull check misfires | stage 3: per-arch map in coord-head (REAL schema bump, not additive) |
| B4 | `cmd_mirror.py:2912,894`; `mirror.py:1399` | Audits compare ALL-builder claims against ONE pool: `claim_not_in_apt_index` + `missing_on_disk` CRITICALs for every foreign-arch claim; no per-claim→pool binding exists | stage 3: D4 arch field + claim filters to `{own arch, all, source-if-primary}` (adopt `audit_closure_ledger`'s `_audited_arches` pattern) |
| B5 | `coord/publish.py:1557-1573` | Closure ledger generated from the LOCAL repo only, then overwrites the shared one — single-arch ledger; foreign builders see `closure_ledger_entry_missing` + every own claim "stranded" at pull | stage 3: union ledger (fetched ∪ own arch slice), never overwrite |
| B6 | `cmd_mirror.py:2049-2096,3654`; `utils.py:520` | `mirror pull` walks the whole ledger with no arch filter and routes EVERYTHING into own `binary-<arch>` (foreign debs 404 or corrupt the tree; sources land in foreign publishable tree against D1) | stage 3: the already-filed pull-routing item — plus arch filtering |
| B7 | `patch/source/<pkg>/<ver>/` + `bump.py:1293-1321` | Patch tree has NO arch dimension (i386-only fix inexpressible) AND P is history-dependent (`prior.P+1` vs per-checkout record) — independent builders converge on identical patch bytes with different P → foreign binary versions reference source versions the primary never published; `patch_set_hash` never reaches claims so no audit can see it | stage 3: P = pure function of patch content; `patch_set_hash` into claims; optional per-arch patch subdirs |
| B8 | `cmd_source.py:1912,2158`; `cmd_tunnel.py:520`; `publish.py:215,253` | D3 unimplemented: `_emit_after_build` fires unconditionally (3 sites) and `generate_pending_claims` exempts sources from ANY filter — a second builder against shared coord is unsafe TODAY | stage 3: role-gate emit; drop `_all`+source pending claims on non-primary |
| B9 | `utils.py:2781`; `build-system.sh`; `disk_image.py:381-477` | A5 host-arch preflight confirmed absent at every candidate hook; image code chroots into target rootfs (`useradd`, `update-initramfs`, `grub-install`) → cross-arch run dies mid-build with Exec format error instead of clean refusal | stage 1.5: config-ARCH vs `dpkg --print-architecture` gate (allowing i386-on-amd64) |
| B10 | `disk_image.py:114,458,821`; `cmd_build.py:976` | `build_disk_image` takes NO arch param; `--target=x86_64-efi` is mandatory (hard fail on both new arches); `_verify_grub_artifacts` checks the x86_64-efi module dir | stage 5/7: thread profile through disk_image (G3 core) |
| B11 | `mirror.py:2280-2296` | No `coord_url` override — per-arch pool URLs silently derive per-arch coord trees; shared federation unreachable (several call sites: publish/audit/pull/reconcile/sync) | stage 3: registration override threaded through all `coord_root_for` sites |

### Majors

- **Peer `_all` fetch is all-new machinery** — `neighbours_known` is
  display/drift-only (`cmd_mirror.py:819,1571`); no primitive fetches a
  file from a peer pool.  Bigger than D1's "small feature" sizing.
- **`_all` reclaim/deprecation divergence** — primary rewriting or
  deprecating an `_all` leaves stale copies indexed in foreign repos;
  reclaim → foreign `claim_apt_sha_mismatch` CRITICAL, deprecation →
  silently served stale (presence check suppressed).  Propagation
  needed (`schema.py:48-108`, `store.py:396`).
- **`src_idx {} vs None` conflation** (`mirror.py:1945`) — foreign
  repo with legitimately no source tree → false
  `closure_ledger_entry_not_published` CRITICALs once the union ledger
  lands.  One-line gate fix; land WITH stage 3.
- **Emit `.dsc` bytes are arch-universe-dependent** — re-emit passes
  the local cache to the STA-55 demotion (`source_emit.py:332`);
  moot once D3 lands (foreign builders don't emit) but
  belt-and-suspenders: primary-universe or no lookup at emit.
- **`[arch]` qualifiers touch FOUR readers + one parser, not one**:
  `utils.py:4182,4208` (`parse_pkg_list_groups`), `surfaces.py:144`
  (`read_flat_roots`), `selection_lock.py:224` (`_read_flat_seeds` —
  duplicate reader feeding the SIGNED seeds), and the `Tunneled`
  comma-parser (`utils.py:3022`).  Plus `render_pkg_list` restore
  fallback (`selection_lock.py:382`) would strip qualifiers/entries on
  legacy lockfiles — forbid or make round-trip-faithful.  Stage 2
  re-sized accordingly.
- **choose-mirror bakes `/asgard/`** (`fork/source/choose-mirror/
  Mirrors.masterlist:4`) — the installed system's apt line; needs
  arch-parameterization at installer build.  And the D1 promise
  "deb-src → repo-amd64" has NO mechanism: choose-mirror carries ONE
  directory for both lines (`generators/50mirror:288`); needs a new
  finish-install hook (currently masked by
  `enable-source-repositories false`).
- **Remote-build machinery is arch-blind** (`remote_orchestrate.py`,
  `remote_build.py`, `remote_agent.py`) — agent never reports its
  arch; an arm64 remote would produce wrong-arch debs published as
  amd64.  Handshake must carry `dpkg --print-architecture` + refusal.
- **D1 wording vs implemented signing model**: the tier-1 GPG repo
  key is SHARED (onboarding imports + verifies it,
  `onboarding.py:363-482`); client images bake ONE archive keyring
  from the local signing home.  D1's "each builder signs with its own
  gnupg key" is wrong — per-builder keys would break cross-repo trust
  (no aggregation exists).  Keep the shared tier-1 key; correct D1.
- **`build-system.sh:343,365`** amd64-hardcoded host-package hints;
  **QEMU smoke harness x86-only** (`tests/installer_smoke/run.py:167,
  94-103`); **`console_extra` consumed nowhere** (both grub.cfg
  emitters hardcode x86 consoles); **partition renumber cascade** in
  `disk_image` when arm64 drops BIOS-Boot p1 (decision: renumber vs
  keep placeholder — default to Debian convention, renumber);
  **docs/mirror-setup.md + onboarding wizard** assume single repo
  throughout (`onboarding.py:104-109,335`); **onboarding never
  prompts/validates ARCH**.

### Minors

`/tmp/athena_crash.log` fixed path clobbered across checkouts
(`build.py:27`); dead amd64-pinned `_KERNEL_PKG_RE` constant left by
stage 1 (`iso_installer.py:58`) — delete; flock is host-global by
design — KEEP it host-global when coord_url lands (`mirror.py:2431`).

### Refuted (verified safe — no work needed)

Ownership/conflict/supersession all key on FILENAME (or name+arch),
never bare name — foreign-arch rebuilds of every package are safe
(`publish.py:551`, `store.py:349`, `reconcile.py:360,379`).  Container
names/networks/build-cache collision-free; pid-scoped reaping safe.
Selection HMAC + mirror.conf are per-checkout (no cross-checkout
hazard).  localmirror is a per-checkout bind-mount (no ports).
`arch_filter` correct for i386 (cputable i686 mapping verified).
Sources chain safe-skips on a repo with no source tree.  Image code
has zero live hardcoded `repo/` paths.  Identity-scan manifest
arch-clean.  `installer_chroot` is cross-arch safe (pure unpack).
Tunnel writes no config at runtime.  Builder-ID collision
TOFU-guarded.  D4b native-base guard behaves on i386.  Empty Sources
index on foreign ISOs benign.

### Revised stage sizing

- **NEW stage 1.5 (small, immediate, amd64-safe):** DONE 2026-07-25 —
  B1 arch-qualified image tag (`build-<rel>-<arch>-<snap>`) +
  `athena.arch` label stamped at build and asserted on reuse; B2
  `linux_personality`/`host_arches` profile fields + both
  containers.run sites route through the setarch-wrapping
  `_container_command`; B9 `assert_host_compatible` gate at the
  BuildConfig ARCH read (i386-on-amd64 native-allowed,
  `ATHENA_ALLOW_FOREIGN_ARCH=1` analysis escape); dead
  `_KERNEL_PKG_RE` deleted (test now pins the profile regex);
  crash log is pid-scoped, removed on clean exit when empty.
- **Stage 2 (bigger than planned):** four readers + Tunneled parser +
  restore-fallback guard, one shared qualifier-splitting helper.
- **Stage 3 (the true epic core — substantially under-scoped before):**
  coord-head per-arch schema bump (B3), union ledger (B5), audit arch
  filters on D4 arch field (B4), pull arch-filter + routing (B6),
  peer-fetch primitive + `_all` propagation on reclaim/deprecate,
  D3 emit/claim gates (B8), P-determinism + `patch_set_hash` in claims
  (B7), `coord_url` override (B11), `src_idx` gate fix, D1 signing
  wording correction.
- **Stage 5/7 additions:** disk_image arch threading (B10) +
  partition renumber, choose-mirror masterlist templating + deb-src
  finish-install hook, `console_extra` consumption in both grub.cfg
  emitters, aarch64 QEMU harness.
- **Stage 8 additions:** mirror-setup docs + onboarding wizard
  (ARCH prompt, per-arch remote paths, shared coord), build-system.sh
  arch-aware hints, remote-build arch handshake.
