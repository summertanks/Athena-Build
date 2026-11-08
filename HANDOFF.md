# Athena-Build — handoff note (2026-11-08): project scope & loose ends

Code: `master @ c005e84`, fully pushed, tests green (1455/1455).

## What the project is

Asgard Linux (codename thor) — a from-source Debian-bookworm
derivative: ~1,000 sources selected from the pinned snapshot
`20260705T190150Z`, rebuilt natively under the `+asg` version-transpose
scheme, published to a federated apt mirror with signed claims, shipped
as live ISO + installer ISO + qcow2 disk image.  v0.1.4 released; full
amd64 surface built, published, and audited clean in July 2026.

## Active epic — COMP-04: multi-arch (the main thread to resume)

Scope (operator-confirmed): **i386 as a full installable distribution +
arm64**, native builders per arch (i386 = 32-bit personality containers
on the amd64 host; arm64 = real ARM builder), **per-arch repo trees**
(`repo-amd64/ repo-i386/ repo-arm64/`) with **one shared federation**
(`repo-coord/`); the primary (amd64) builder solely owns arch:all
packages and source artifacts.  armhf out of scope.  i386 is
time-bounded: the bookworm pin is its last full release arch.

Plan, history, and runbook: `docs/plans/comp-04-multiarch.md`.

Done (stages 0–3, all code landed and pushed):
- 0: selection/transpose oracle over i386+arm64 — GREEN.
- 1/1.5: `arch_profile.py` single authority; amd64 literals dead;
  arch-qualified docker tags; `setarch linux32`; host-arch preflight.
- 2: `[arch]` qualifiers in seed lists, all readers filter by arch,
  amd64 closure proven byte-identical.
- 3: federation arch-awareness — claim `arch` + `patch_set_hash`
  fields, primary-owner gates, per-arch coord-head sha maps, union
  closure ledger, arch-scoped audits/pulls, `--coord-url` /
  `--primary-url` registration, `_all`-convergence from primary,
  federated `+pP` adoption.

Remaining:
- **Stage 3 operational rollout** — never executed; on a fresh repo
  server simply create `repo-amd64/` + `repo-coord/` and register with
  `--coord-url` (the runbook's `mv` steps only apply to migrating the
  old host in place).
- **Stage 4**: i386 builder (BS3) bring-up — second checkout, own
  builder identity, `ARCH = i386`, `Role = federation`
  (⇒ arch_all_owner=false), world build (needs a big VM).
- **Stage 5**: i386 images (BIOS machinery exists; profile-driven).
- **Stage 6**: arm64 builder (BS2) on a real ARM VM, ≥16 GB for the
  world build.
- **Stage 7**: arm64 images — the one genuinely new surface: EFI-only
  disk-image path (no BIOS-Boot partition, `arm64-efi` grub,
  `BOOTAA64.EFI`), arm64 grub-mkrescue ISO, arm64 d-i seed,
  `console_extra` consumption, aarch64 QEMU smoke harness.
- **Stage 8**: docs + close (versioning-mechanics per-arch binNMU
  note, mirror-setup multi-builder, onboarding wizard ARCH prompt).

## Loose ends

- **TODO.md SELECT-02 row** still reads "todo" — it was operationally
  closed in July (two-pass selection live, deprecations published,
  audit clean); flip the row to DONE.
- **U4 — headless prompt gap**: `--yes` does not answer multi-choice
  provider prompts; a fresh-state `cache parse --headless` dies with
  EOFError.  Workaround `yes 1 |`; proper fix = `--yes` takes the
  displayed default.  Worth filing as a small TODO.
- **Mirror DNS**: the registered mirror is raw-IP (`host_type: ip`),
  and `http://140.245.198.222/asgard` is baked verbatim into every
  July image's apt line — images die with the server.  New mirror
  should be registered `fqdn` with a real hostname.
- **Mirror disk**: three-arch pools + ISO surface need roughly
  +20 GB over today's 19 GB repo; size the new repo server ~60 GB+.
- **MAT-03 allowlist TODOs** (deliberate, low-visibility): the
  "Debian" desktop-directory submenu label and base-files
  `info.dir` template prose remain allowlisted rebrand TODOs.
- **adversarial-v2 stage-5/7 extras** (tracked in the plan doc):
  choose-mirror `Mirrors.masterlist` bakes `/asgard/`; the D1
  "deb-src → repo-amd64" line needs a finish-install hook (currently
  masked by `enable-source-repositories false`); remote-build
  machinery is arch-blind (agent must report its arch);
  `build-system.sh` host-package hints are amd64-hardcoded;
  QEMU smoke harness is x86-only.
- **Backlog beyond COMP-04** (unchanged): CONF-08 (lift nodoc — needs
  big VM + world rebuild), CONF-14 (fork-version bump mechanics),
  AUDIT-02 (reproducibility gate), INST-01..04 (installer i18n /
  accessibility / guided partman / GUI investigation), FED-02
  (snapshot-advance coherence), UX-07 (--offline), UX-08 (per-fork
  enable/disable), CONF-17 (pool layout — parked on COMP-04), SUP-01,
  UX-06, DIST-01.
