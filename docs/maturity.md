# Project maturity — distance to the end goal

This is the long-form companion to the maturity summary in the
[README](../README.md).  Earlier versions of this page scored the project
by engineering dimension — code quality, tests, architecture.  Those are
means, not ends, and they flattered the parts of the road already walked.
This version measures the only thing that matters: **distance to the end
state**, assessed along the journeys the finished distribution must serve.

Calibrated 2026-07-04.  Recalibrating means re-verifying each claim
against the tree — not re-scoring from memory; this page has drifted
within days before.

## The end state

When this project is done, all of the following are true:

> A user downloads Asgard install media from a public mirror, verifies
> it, installs it on real hardware, and uses it daily.  Their machine
> receives signed security updates from the project's own mirrors — no
> Debian infrastructure anywhere in the loop.  Every binary they run is
> traceable to source the project publishes, and anyone can rebuild that
> source and verify they get the same distribution.  Nothing on the
> machine says Debian.  Behind the scenes, a federation of builders —
> any of which can fail or be replaced — builds, signs, and serves all
> of it, release after release.

Three journeys have to work for that paragraph to be true: the **end
user's**, the **auditor's**, and the **operator's**.  Each stage below
carries a distance marker:

- ✔ **proven** — works, exercised end to end where it matters
- ◑ **works, unproven where it counts** — built and passing, but the
  load-bearing validation (real hardware, real users, real time) hasn't
  happened
- ◔ **partial** — meaningful pieces exist; the stage doesn't hold up
  alone yet
- ○ **not started** — design intent at most

---

## Journey 1 — the end user

*Discover → download & verify → install → daily use → stay updated →
move to the next release.*

| Stage | | Where it stands |
|---|:--:|---|
| Discover & download | ◔ | Mirrors serve a landing page, a machine-readable release manifest, and the ISOs with sha256s.  But there has been no public release: no hosted mirror a stranger can reach, no announcement surface, no user-facing documentation (everything under `docs/` addresses the operator building the distro, not the person installing it). |
| Verify the media | ✔ | ISO sha256s in the release manifest; on-media `md5sum.txt` for d-i's media check; the apt repo behind it GPG-signed end to end. |
| Install | ◑ | The installer reaches `finish-install` cleanly — but only on VMware BIOS/EFI.  Real hardware is untested (one BIOS box, one UEFI box, USB media are the stated gate).  UX is stock text-mode d-i: no guided "erase disk and install Asgard" recipe, English-only, no accessibility boot path yet. |
| Daily use | ◑ | The GNOME live/installed closure boots and runs; branding (GRUB, os-release, wallpaper) verified on fresh installs.  Two honest caveats: the final squashed image root is not yet swept by the identity audit, and nobody has daily-driven an installed Asgard machine for weeks and reported back. |
| Stay updated | ◔ | The plumbing exists: installed systems point apt at our mirrors and nothing else, publishes are signed and append-only, CVE tracking runs grype over the per-build SBOM.  What doesn't exist: a proven loop.  No installed machine has yet received a security fix published after its install — the advisory → rebuild → publish → `apt upgrade` path has never been exercised end to end, and the response process is manual. |
| Next release | ○ | thor → whatever-comes-next has no story.  No dist-upgrade path, no supported-lifetime statement, no test of upgrading an installed system across a codename.  (The version scheme was deliberately designed so this *can* work — update ordinals survive rebases — but nothing exercises it.) |

**The user journey in one sentence:** a user could install and run
Asgard today if we handed them the ISO personally and they owned
VMware; everything from "stranger finds it" to "machine stays patched
for a year" is unproven or missing.

## Journey 2 — the auditor

*Fetch the source → rebuild it → verify the result matches → inspect
what's inside.*

| Stage | | Where it stands |
|---|:--:|---|
| Fetch the source | ○ | **The single clearest gap in the project.**  The pipeline builds ~4,600 binary packages and publishes zero source packages.  Until the mirror carries the `.dsc` + tarballs alongside every binary, the output is not independently rebuildable and not cleanly license-compliant — this is what separates "a tool that builds a distro" from "a distribution." |
| Rebuild it | ◔ | Snapshot pinning means an operator with this repo can reconstruct the exact input universe, and the build containers are hermetic enough in practice.  But rebuilding requires *being us* — the toolchain, the config, the patch trees.  An outsider can't start from published source (there is none) and arrive at the same binaries. |
| Verify bit-for-bit | ○ | No build-twice/diffoscope check exists, even internally.  Builds are likely time-stable (SOURCE_DATE_EPOCH is anchored, and the post-build repack was made byte-reproducible after a live incident), but "likely" is the point — nothing measures it. |
| Inspect what's inside | ◑ | Strong: a CycloneDX SBOM per build with upstream-version provenance, patch counts, and content hashes; upstream provenance also stamped into every rewritten binary.  Weak: nobody outside the project has ever consumed these artifacts. |

**The auditor journey in one sentence:** we can show our work but not
yet hand it over — publish the source and the rest of this journey
becomes achievable; without it, none of it is.

## Journey 3 — the operator

*Stand up a builder → build the distribution → publish it → keep it
alive → grow it.*

| Stage | | Where it stands |
|---|:--:|---|
| Stand up a builder | ✔ | The strongest stage in the project.  A guided first-run wizard (origin and federation-peer flows, validate-as-you-go), one-command mirror preparation that is idempotent and self-checking, and operator docs that were just consolidated and walked prompt-by-prompt. |
| Build from source | ✔ | The full pipeline — snapshot-pinned cache, dependency closure, parallel containerised source builds, three image surfaces as independent closures — runs end to end and is guarded by ~1,400 tests including policy enforcers that pin operational invariants. |
| Publish & federate | ◑ | Signed per-builder claims, append-only pools, ownership and installability gates, byte-freeze with an explicit reclaim escape hatch, stale-lock recovery, peer decommissioning — all built, all tested, exercised across a real three-builder fleet.  Two honest limits: the shared tier-1 key means the trust model stops outsiders but not a malicious *peer*, and coord-head freshness is designed but not enforced (a rollback served by a compromised mirror would not be detected today). |
| Keep it alive | ◔ | Snapshot advance, update builds, drift detection, and CVE visibility all work as operator-driven actions.  What's missing is cadence and obligation: no release schedule, no defined security-response time, no monitoring that *tells* the operator a machine or mirror needs attention rather than waiting to be asked. |
| Grow it | ◔ | One architecture (amd64), one upstream (Debian bookworm), heavy hardware requirements.  Deliberately deferred rather than blocked — and the one structural trap on that road (the repo layout that doesn't scale to multi-arch/multi-suite) is identified with its migration trigger recorded, so growth won't start on the wrong foundation. |

**The operator journey in one sentence:** a competent operator can
build and serve this distribution today, alone or federated — what they
can't yet do is promise anyone else it will still be patched and
current next quarter without them personally driving every step.

---

## Engineering health (the means)

The old per-dimension scores lived here; compressed to what still
matters as *support* for the journeys above:

- **Pipeline & architecture** — staged state machine, composable
  command mixins, per-surface closures, one recipe path local and
  remote.  Carries the operator journey; not the bottleneck for any
  other.
- **Tests** — 1,399 tests across 16 per-subsystem files with a
  registration guard, run by one aggregator; policy enforcers pin
  invariants (read-only commands can't destroy, identity rules,
  UTC-only timestamps).  Reproducibility is the one journey-critical
  hole in coverage.
- **Code quality** — ruff and mypy as hard gates; structured returns;
  incident-dated comments.  Pre-release legacy compatibility has been
  actively stripped rather than accumulated.
- **Documentation** — operator docs are current and consolidated
  (mirror setup/layout, versioning mechanics, first-run).  The
  per-module walkthrough lags the code, and *end-user* documentation
  does not exist yet — that's a Journey-1 gap, not a polish gap.

## What moves the needle

Not a backlog — the three moves that change what the *journeys* look
like, in the order that unblocks the most:

1. **Publish source packages alongside binaries.**  Flips the entire
   auditor journey from ○ to achievable, resolves the license question,
   and is a precondition for anyone trusting the output who doesn't
   already trust us.
2. **Prove the loop on real iron: install on hardware, then ship that
   machine a security update.**  One BIOS box, one UEFI box, one
   advisory carried end to end.  Converts the two ◑/◔ stages that
   define "usable distribution" into ✔, and will surface the
   process gaps no VM test can.
3. **Cut release 1 in public.**  A hosted mirror, an announcement, a
   user-facing install page, a stated support window.  Everything in
   Journey 1's first stage exists in pieces; a real release is what
   forces them together — and starts the clock on the next-release
   story that currently doesn't exist.

Everything else — more architectures, a graphical installer,
localisation, reproducibility proofs — matters *after* those three,
because each of the three changes who can use the project; the rest
changes how pleasant it is.
