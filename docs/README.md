# Athena-Build documentation

Welcome. This folder holds the deeper documentation; the [project README](../README.md) is the place to start, and its **First run** walkthrough takes you from a clone to a built ISO.

New here? Read [config.md](config.md) to shape your own distribution, and keep [glossary.md](glossary.md) open for any unfamiliar term.

## Getting started

| Doc | What it's for |
|---|---|
| [config.md](config.md) | Shape your distribution by editing the config files — name, package set, behaviour. Start here to customize. |
| [glossary.md](glossary.md) | Plain-English definitions of the terms used across these docs. |

## Understanding how it works

| Doc | What it's for |
|---|---|
| [architecture.md](architecture.md) | How the build pipeline fits together, stage by stage. |
| [versioning-mechanics.md](versioning-mechanics.md) | The version-numbering rules — what gets translated, patched and bumped, and when; includes the sibling-constraint deep dive. |
| [branding-methodology.md](branding-methodology.md) | How your identity (name, codename, logos) is applied and enforced. |
| [collision-gate.md](collision-gate.md) | How Athena stops an upstream version from silently overriding one of your forks. |
| [security.md](security.md) | The security model — what gets signed and verified, and how. |
| [virtual-build.md](virtual-build.md) | A dry run that predicts a build's outcome without compiling anything. |

## Operating a build

| Doc | What it's for |
|---|---|
| [patching.md](patching.md) | Patching upstream source at the source, pre-install, and post-install stages. |
| [mirror-setup.md](mirror-setup.md) | Everything about publish mirrors: what lives on the host, setup and registration, publishing, federation, audit and recovery. |
| [remote-build.md](remote-build.md) | Spreading builds across remote machines, plus the local build mirror. |
| [release.md](release.md) | The checklist for cutting a numbered release. |
| [cve-tracking.md](cve-tracking.md) | Tracking security advisories against what you've built. |
| [api.md](api.md) | The HTTP API for driving builds from another program or UI. |

## When something goes wrong

| Doc | What it's for |
|---|---|
| [build-quirks.md](build-quirks.md) | Debian packaging gotchas the project has hit, with the fix for each. |
| [known-issues.md](known-issues.md) | Current rough edges and their workarounds. |

## Project status

| Doc | What it's for |
|---|---|
| [maturity.md](maturity.md) | An honest distance-to-goal assessment, measured along the end-user, auditor, and operator journeys. |
| [done.md](done.md) | Archive of completed and closed work. |

## For contributors (deeper / advanced)

| Doc | What it's for |
|---|---|
| [pseudocode.md](pseudocode.md) | A natural-English walkthrough of every module. |
| [plans/](plans/) | Design documents for in-progress and upcoming features. |
| [diagrams/](diagrams/) | The build state-machine diagram (`build-fsm`). |
