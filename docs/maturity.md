# Project maturity — detailed assessment

This is the long-form companion to the maturity summary in the [README](../README.md). It scores the project by *distance to the intended goal* — a fully from-source, trustworthy Debian derivative — not by activity. Each dimension carries a score, the reasoning behind it, and the direction it is moving since the last assessment.

Calibrated 2026-06-26.

## The three tracks

- **Athena** — the build toolchain. The mature layer: source in, signed bootable images out.
- **Asgard** — the distribution Athena produces. Boots under virtualisation; still earning its hardware stripes.
- **Federation** — the multi-builder publishing layer. Signed and append-only; the trust model is still tightening.

## By dimension

| Dimension | Score | Assessment | Trend |
|---|:--:|---|:--:|
| Functional completeness | ~80% | All three output surfaces — live ISO, installer ISO, and qcow2 disk image — build end to end and boot under virtualisation; federated multi-target publish works. The build emits binaries but not their source, so the output is not yet independently rebuildable, and real-hardware boot is unproven. | → |
| Architecture | ~87% | Staged pipeline behind an explicit state machine, composable command modules, parallel source builds with heavy-package serialisation, per-surface dependency closures, and parallel binary/installer dependency trees. Local and remote builds now share one recipe path. Multi-arch and multi-distro remain cross-cutting. | ▲ |
| Code quality | ~83% | Lint and type checks are enforced CI gates rather than advice; structured `(ok, detail)` returns throughout; comments explain the *why* with incident dates. Type checking still runs permissively and a little dead code lingers. | ▲ |
| Tests | ~82% | ~1,300 tests in a single suite, including policy enforcers that pin invariants — read-only commands cannot call destructive helpers, every test must be registered, identity rules are grep-enforced. A few UI tests are timing-fragile, some no-op without host tools, and reproducibility is not yet tested. | ▲ |
| Reproducibility | ~70% | Snapshot-pinned package fetches plus a CycloneDX SBOM per build give strong provenance. There is no automated bit-for-bit rebuild check, so the same source can still hash differently between runs. | → |
| Security | ~84% | Mirror metadata is GPG-verified before use; the signing key passes a sign+verify roundtrip before any chroot work; per-builder claims are signed; build containers mount sources read-only; the Docker daemon URL is guarded. The signing key is passphrase-less by design, the live image ships a fixed default password with no media checksums, and builder identity is not yet hijack-proof. | → |
| Operability | ~88% | A curses TUI and a headless CLI share one console; a key-protected HTTP API exposes state and a command dispatcher; logs are timestamped per build; one-shot command, auto-yes, and env-var sudo support automation; the toolchain version is stamped into artifacts. No localisation yet, and session resume is present but dormant. | ▲ |
| Identity / branding | ~86% | A three-layer identity model — toolchain, distribution, codename — is enforced by token substitution and a collision gate that fails the build when an upstream version would shadow a fork, backed by a residue audit for stray upstream branding. The final squashed image root is not yet inside that audit. | ▲ |
| Documentation | ~77% | Broad coverage — architecture, patching, release, mirror federation, a per-module walkthrough, a changelog. The fast pace has let the entry docs drift from the code in places; this overhaul is the correction. | ▼ |
| Scale / portability | ~50% | One architecture (amd64) and one base distribution (Debian) are fully supported; everything beyond that is deliberately deferred. | → |
| Source availability | ~30% | The pipeline produces binary packages but does not yet re-publish the corresponding source — which is also what would make the output independently rebuildable and cleanly license-compliant. The clearest single gap between "a tool that builds a distro" and "a distribution." | ◆ new |

*Trend: ▲ improving · → steady · ▼ slipped · ◆ newly surfaced.*
