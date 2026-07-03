# Changelog

Major **foundational capabilities** of the Athena-Build toolchain, by release.
This is deliberately coarse: it records capability milestones, not every fix or
refinement — those live in git history and `TODO.md`. Versioning is the
toolchain SemVer (`scripts/_version.py`); per-commit dev versions are automatic.

## [Unreleased]

- **Reworked the version-numbering framework** (content-order / "transpose"
  scheme; see `docs/versioning-mechanics.md`).

- **Resilient remote-build control plane.** Remote source builds are now driven
  by a detached on-host agent exposing a localhost poll API over an SSH tunnel,
  with a progress-based watchdog and named-container reaping — so a build
  survives orchestrator disconnects and a wedged build is killed by lack of
  progress rather than a wall-clock timer.

- **Federation identity & integrity hardening.** Builder identities are anchored
  by tier-1-signed `id→pubkey` bindings in the coord-head (trust-on-first-use on
  register + strict verification on adoption), so a key dropped by anyone with
  SSH write but no signing key can no longer hijack a builder or strand a peer's
  files. Adds publish stale-lock recovery (`mirror unlock`) and stale /
  closure-limited pull-ledger detection.

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
