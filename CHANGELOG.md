# Changelog

Major **foundational capabilities** of the Athena-Build toolchain, by release.
This is deliberately coarse: it records capability milestones, not every fix or
refinement — those live in git history and `TODO.md`. Versioning is the
toolchain SemVer (`scripts/_version.py`); per-commit dev versions are automatic.

## [Unreleased]

## [0.1.4] — 2026-07-14

- **Source packages published (Sources index + pool `.dsc` + federation
  claims/ledger/pull)** (`scripts/source_emit.py`, `source emit`; see
  `docs/versioning-mechanics.md`).

- **Build container base changed from debian-slim to a custom
  snapshot-bootstrapped rootfs** (`scripts/base_rootfs.py`).

- **Per-package prebuild scripts** (`patch/source/<pkg>/prebuild.sh`; see
  `docs/patching.md`).

- **Reworked the version-numbering framework** (content-order / "transpose"
  scheme; see `docs/versioning-mechanics.md`).

- **Resilient remote-build control plane** (`scripts/remote_orchestrate.py`;
  see `docs/remote-build.md`).

- **Federation identity & integrity hardening** (`scripts/coord/`; see
  `docs/mirror-setup.md`).

## [0.1.2] — 2026-06-25

- **Remote container workloads enabled.** Source builds can fan out across
  remote Docker hosts over SSH (`source remotebuild`) — the recipe is computed
  locally, a self-contained bundle runs ON the remote, and the `.debs` are
  recovered locally — with an opt-in snapshot-pinned local build mirror served
  to the build container.

## [0.1.1] — 2026-06-22

- **Baseline at the first version tag.** From-source Debian-derivative build
  pipeline (cache → source build → chroot → bootable BIOS/EFI ISO) with
  snapshot-pinned reproducible builds, and a federated, claim-based publish
  mirror (multi-builder ownership + signed coordination).
