# Repo publishing + updates — design discussion

**Status: DRAFT (2026-05-18) — pre-decision discussion document.**
None of the proposals below are committed.  This exists so the design
can be picked piecewise; see the consolidated "Open decisions" section
at the bottom for the full list to walk through.

Related tickets: `COMP-02` (publish_repo) — implementation slot once
the design lands.

---

## Context

Right now the build produces a private `repo/` on the build host.
Installed systems have no upstream-of-truth to query for updates; the
ISO is a one-shot deliverable.  To consume Thor over time (security
patches, point releases, install fresh from latest media) we need:

  - A publicly reachable apt repository (signed, standard wire format)
  - A model for how security patches flow into installed systems
    without each install drifting at a different pace
  - A way to cut new ISOs that aren't instantly obsolete the day after
    release while not flooding installed systems with churn
  - A release-tracking surface so an installed system can answer
    "am I current?"

This document walks through the questions raised in the 2026-05-18
discussion and offers a unified design as a starting point.

---

## The unified design (proposed)

A five-layer model.  Each layer answers one or two of the questions
below; the layers compose into the full publish/update pipeline.

### Layer 1 — Suite structure

Mirrors Debian's `<release>` / `<release>-security` / `<release>-updates`
pattern, scoped down:

```
thor            frozen at point-release snapshot.  Stable for the
                lifetime of the release.  This is what the ISO ships.
thor-security   rolling, additive.  Only packages with security fixes
                land here, rebuilt from upstream-snapshot-of-the-day.
thor-updates    (deferred) routine non-security bumps.  Can wait until
                the next point release; activate later if needed.
```

Three suites; client opts in per suite in `sources.list`.  Defaulting
`thor` + `thor-security` enabled out of the box matches Debian's "you
get security but not random updates" posture.

### Layer 2 — Release cadence

ISO point releases are tied to a **frozen snapshot timestamp**.  We cut
a new release when one of:

  - Debian cuts a point release (12.6 → 12.7, ~bi-monthly)
  - `thor-security` has accumulated enough significant fixes worth
    re-baselining the ISO

Each ISO is reproducible: the same `[Build] Snapshot = <ts>` + same
source corpus = the same `.iso` bytes.  No single ISO lives forever;
each release is fresh against a then-current snapshot.

### Layer 3 — Security model: triage-driven thor-security

Daily-ish automated job:

  1. Poll `https://security-tracker.debian.org/tracker/data/json`
  2. Compute intersection: CVE-affected packages ∩ packages we ship
  3. For each hit, rebuild from upstream's snapshot at the fix's
     timestamp (overrides cache pin temporarily for that one package)
  4. Publish to `thor-security`

`thor` itself does not change between point releases — frozen.
Operator with default sources gets security via thor-security but
nothing else moves.  Operator who wants routine refreshes additionally
enables `thor-updates`.

### Layer 4 — Pool / publication

Standard Debian filesystem layout:

```
repo.thor.example.org/
├── dists/
│   ├── thor/
│   │   ├── InRelease            (signed via CONF-02 key)
│   │   ├── Release
│   │   ├── Release.gpg
│   │   └── main/
│   │       ├── binary-amd64/Packages.{,gz,xz}
│   │       └── source/Sources.{,gz}
│   ├── thor-security/           (same shape; different package list)
│   └── thor-updates/            (when activated)
└── pool/
    └── main/
        ├── g/glibc/libc6_2.36-9+deb12u14+thor1_amd64.deb
        ├── liba/libapt-pkg/...
        └── ...
```

`pool/` is shared across suites; each Release file is a manifest of
which pool entries compose that suite.  A security rebuild lands a new
`.deb` in `pool/` AND adds the entry to `thor-security`'s Packages
file; older versions stay in `thor`'s Packages file.  Both available
to apt; apt resolves to highest.

### Layer 5 — Versioning / tracking

Each ISO embeds three identity files:

| File | Content | Purpose |
|---|---|---|
| `/etc/thor/release-id` | `0.1` (Thor version) | Self-identification |
| `/etc/thor/build-snapshot` | `20260514T083402Z` | Reproducibility + delta computation |
| `/etc/thor/release-channel` | `stable` / `testing` | Future: parallel release tracks |

Plus a public manifest tree:

```
repo.thor.example.org/releases/
├── index.json                   # all releases ever
├── 0.1/
│   ├── manifest.json            # version + snapshot + sha256
│   ├── packages.tsv             # name → version (every pkg in this ISO)
│   ├── security-since-prior.md  # CVEs fixed since previous release
│   └── thor-0.1-amd64.iso       # the ISO itself (or symlink)
├── 0.1.1/
└── ...
```

A small `thor-update-check` tool on the installed system reads
`index.json`, finds the latest, diffs against `/etc/thor/release-id`,
prints "Thor 0.1.2 is out, fixes 14 CVEs since your 0.1."

---

## Discussion points

Each section maps to one of the seven questions raised on 2026-05-18.

### DP-1 — How will we publish our repo?

**Original question:** how will we publish our repo

**Proposal:** object storage + CDN.  Standard Debian wire format
(directory layout above); signed `Release` files via CONF-02; URL is
something like `https://repo.thor.example.org/`.

**Options:**

| Option | Cost | Notes |
|---|---|---|
| **Cloudflare R2 + Workers** | ~$0-5/mo | Zero egress fees; S3-compatible API; we'd build a thin upload script.  Lean. |
| AWS S3 + CloudFront | low double-digit USD/mo at scale | Standard.  Egress costs once traffic grows. |
| Backblaze B2 + Cloudflare CDN | ~$0-5/mo | Zero-egress combo via bandwidth alliance.  Slightly more setup. |
| Self-hosted VPS + nginx | ~$5/mo flat | Full control; SSH access; needs ongoing admin. |
| GitHub Releases | free | Per-release file attachments only; not a real APT repo (apt needs `dists/` structure GitHub doesn't serve). |

**Open sub-decisions:**

  - Hosting backend (R2 / S3 / B2 / VPS / other)
  - Domain (`repo.thor.example.org`?  registered yet?)
  - HTTPS cert source (CDN-issued / Let's Encrypt / managed)

**Depends on:** nothing.  Blocks DP-3 (installed-system sources.list
needs a real URL).

---

### DP-2 — How will updates & security patches be structured?

**Original question:** how will updates & security patches be
structured

**Proposal:** three suites (`thor`, `thor-security`, `thor-updates`),
following Debian's pattern.  See Layer 1 above.

**Options:**

| Option | Description | Trade-off |
|---|---|---|
| **Three suites** | thor / thor-security / thor-updates | Matches Debian; full operator control over what flows in. |
| Two suites | thor / thor-security | Simpler; lose ability to ship routine non-security refreshes without a point release. |
| Single suite | thor | Simplest; can't separate "I want security only" from "give me everything." |

**Cadence questions:**

  - **Continuous** (rebuild every push to source) — impractical for
    operators; version churn.
  - **Rolling stable** (rebuild on stable upstream changes) — closer
    to Arch / rolling distros.  More work, less predictable installs.
  - **Point releases** (Thor 0.1, 0.1.1, 0.1.2) — each ISO has a
    frozen package set; security layered via thor-security; routine
    refreshes via thor-updates or next point release.  Lean.

**Open sub-decisions:**

  - Suite count (1 / 2 / 3)
  - Cadence model (continuous / rolling / point-release)
  - Whether `thor-updates` ships from day one or activates later

**Depends on:** nothing fundamental.  Decisions here drive DP-3, DP-5,
DP-6.

---

### DP-3 — Updates for installed systems

**Original question:** right now we are pinning to latest snapshot so
each iso build will present updates packages, but for systems already
installed how do we want to present the updates & security patches.

**Proposal:** installed system gets the three suites in
`/etc/apt/sources.list` (shipped via `athena-installer-data`); default
enables `thor` + `thor-security`.  Standard `apt update && apt upgrade`
flow works exactly as Debian operators expect.

**Mechanism:**

```
# /etc/apt/sources.list  (shipped by athena-installer-data)
deb [signed-by=/usr/share/keyrings/thor-archive-keyring.gpg] \
    https://repo.thor.example.org/thor thor main
deb [signed-by=/usr/share/keyrings/thor-archive-keyring.gpg] \
    https://repo.thor.example.org/thor thor-security main

# Operator opts in to routine updates by uncommenting:
# deb [signed-by=...] https://repo.thor.example.org/thor thor-updates main
```

Security triage means an operator with the default config gets ONLY
security fixes between point releases.  Predictable.  Doesn't drift.

**Three flavours of update flow to consider:**

| Flavour | What | Trade-off |
|---|---|---|
| **Operator-driven** | apt update + apt upgrade when operator runs it | Standard; no surprise; no auto-reboot. |
| **Auto-update** (unattended-upgrades) | systemd timer runs apt upgrade for thor-security only | Ubuntu/Debian default for many users; auto-reboots risk service downtime. |
| **Pull notification only** | `thor-update-check` cron tells operator "N updates available", operator runs upgrade | Middle ground. |

**Open sub-decisions:**

  - Default sources content (which suites enabled by default)
  - Whether to ship `unattended-upgrades` configured by default
  - Update-notification mechanism on the installed system (cron /
    systemd-timer / motd / none)

**Depends on:** DP-1 (URL), DP-2 (suite structure).

---

### DP-4 — What will be the folder structure of the repo?

**Original question:** what will be the folder structure of the repo

**Proposal:** standard Debian layout (see Layer 4 above for the tree).

```
dists/<suite>/InRelease + Release + Release.gpg
dists/<suite>/<component>/binary-<arch>/Packages{,.gz,.xz}
dists/<suite>/<component>/source/Sources{,.gz}
pool/<component>/<letter-or-libletter>/<pkg>/<pkg>_<ver>_<arch>.deb
```

`<component>` = `main` for everything we ship.  Could add `contrib` /
`non-free` later if we ever ship those (today we don't; CONF-08 era
deferred).

`<letter-or-libletter>`: Debian convention — first letter of the
package name, OR `lib<letter>` for packages starting with `lib`
(spreads load across more directories since lib* is common).

**Open sub-decisions:**

  - Component count (just `main` / also `contrib` + `non-free` /
    Thor-specific names)
  - Architecture coverage (amd64 only / + i386 / + arm64 — see also
    [ARCH-13?] in TODO if exists)
  - Whether to also publish source packages or binaries only (we
    build both today; source publication has size cost but enables
    `apt source` on installed systems)

**Depends on:** nothing; pure mechanical choice.

---

### DP-5 — How will build system pin the original snapshot to which
the ISO was built and segregate which new packages are updates?

**Original question:** how will build system pin the original snapshot
to which the iso was built and segregate which new packages are
updates

**Proposal:** the ISO carries its build snapshot timestamp embedded;
`thor-security` rebuilds are tagged separately by their thor-security
publication timestamp.

**Mechanism:**

  - Build sets `[Build] Snapshot = <fixed-ts>` for each point release;
    timestamp is committed to git alongside the release tag.
  - Build emits `/etc/thor/build-snapshot` into the ISO (and into the
    installed target via `athena-installer-data` or a new fork pkg).
  - `thor-security` rebuilds use a per-package snapshot override:
    only THAT package is fetched from a later snapshot; the rest of
    the dep tree stays consistent with the originally-pinned snapshot.
  - Released ISO bundles include `releases/<ver>/manifest.json` with
    the full package list at release time.  Anything in
    `thor-security` that came AFTER the release date is by definition
    an update.

**Segregation question — how does `apt` know what's "the update"?**

apt doesn't care; it just resolves to highest version.  What the
OPERATOR cares about is "what changed since my install."  Two tools:

  - `apt list --upgradable` — built-in, lists packages with higher
    versions available.
  - `thor-update-check` — fetches `releases/index.json`, compares
    against `/etc/thor/release-id` + `/etc/thor/build-snapshot`,
    prints a structured "since your release: N security fixes, K
    routine updates available."

**Open sub-decisions:**

  - Whether per-package snapshot override is implementable cleanly
    (today `[Snapshot] Timestamp = X` pins ALL mirrors uniformly;
     supporting per-pkg override needs a config-shape change)
  - Where to embed `/etc/thor/build-snapshot` from (fork pkg vs
    chroot.py rendering)
  - Whether `thor-update-check` ships with the ISO from day one or
    later

**Depends on:** DP-2 (suite structure dictates what "an update" is).

---

### DP-6 — How will we build new ISOs because if we pin to only one
snapshot it may lead to all packages on the ISO being obsolete?

**Original question:** how will we build new iso's because if we pin
to only one snapshot it may lead to all packages ont he iso being
obselete

**Proposal:** cut new point releases periodically, each pinned to a
fresh snapshot.  No single ISO lives forever.

**Cadence options:**

| Trigger | Frequency | Trade-off |
|---|---|---|
| **Debian point release** | bi-monthly | Free freshness work — Debian already did the integration testing; piggyback. |
| Time-based (monthly / quarterly) | predictable | Doesn't necessarily align with where Debian sees stability. |
| CVE accumulation (N significant fixes piled in thor-security) | event-driven | Right thing to do when security pressure is high; needs subjective "significant" judgement. |
| Hybrid (Debian-point-release OR N-CVE accumulation, whichever first) | mixed | Captures both pressures; more rules. |

**Mechanism between releases:**

  - Source `[Build] Snapshot = <old-ts>` stays unchanged for that
    release line.
  - New release bumps to fresh snapshot, builds full ISO.
  - Old ISOs remain downloadable (operator may want to install at
    the original snapshot then upgrade).

**Open sub-decisions:**

  - Cadence trigger (Debian-point-release / time / CVE / hybrid)
  - How many historical ISOs to keep published (last N / all)
  - Whether to publish nightly/weekly "testing" ISOs from rolling
    snapshot for early adopters

**Depends on:** DP-2 (suite structure), DP-3 (update flow on installed
systems determines how urgent fresh ISOs are).

---

### DP-7 — How will we track which ISOs have been released, and the
patches/updates in each ISO till latest version?

**Original question:** how will we track which all iso have been
released, and the patches/ updates in each iso till latest version

**Proposal:** publish `releases/index.json` + per-release directories
under `releases/<ver>/`.

**Index shape:**

```json
{
  "schema": 1,
  "releases": [
    {
      "id": "0.1",
      "released": "2026-05-14T12:00:00Z",
      "build_snapshot": "20260514T083402Z",
      "iso_sha256": "...",
      "iso_url": "https://repo.thor.example.org/releases/0.1/thor-0.1-amd64.iso",
      "manifest_url": "https://repo.thor.example.org/releases/0.1/manifest.json"
    },
    {
      "id": "0.1.1",
      "released": "2026-07-14T12:00:00Z",
      "build_snapshot": "20260714T120000Z",
      "supersedes": "0.1",
      "security_count": 14,
      "iso_sha256": "...",
      ...
    }
  ]
}
```

**Per-release directory:**

```
releases/0.1.1/
├── manifest.json              # full pkg name+version list at release
├── packages.tsv               # flat name<TAB>version (grep-friendly)
├── security-since-prior.md    # CVE list, human-readable
├── security-since-prior.json  # CVE list, machine-readable
└── thor-0.1.1-amd64.iso       # the ISO
```

**Tooling:**

  - On the installed system, `thor-update-check` polls
    `releases/index.json`, finds the entry whose `id` >
    `/etc/thor/release-id`, prints a one-screen summary.
  - On a build/release host, a `release publish` command in
    Athena-Build assembles the per-release directory + appends to
    `releases/index.json` + uploads to hosting (DP-1).

**Open sub-decisions:**

  - Whether to expose `releases/` via a static index page (operator
    browses) or JSON-only (operator uses tooling)
  - How to format `security-since-prior` — auto-generated from
    Debian security tracker JSON, or hand-curated narrative
  - Whether to GPG-sign the manifests (matches the Release-signing
    posture; nice-to-have)

**Depends on:** DP-1 (URL), DP-5 (build-snapshot embedding), DP-6
(release cadence determines how many entries accumulate).

---

## Open decisions (consolidated)

Walk through these one at a time when ready.  Numbering is for
reference, not priority — DP-1 and DP-2 are foundation; the rest
can take any order.

  1. **Hosting backend** (DP-1)
     R2 / S3 / B2 / VPS / other?

  2. **Domain + HTTPS** (DP-1)
     Domain name; cert source.

  3. **Suite count** (DP-2)
     1 / 2 / 3 suites?

  4. **Release cadence model** (DP-2)
     Point-release / rolling / continuous?

  5. **`thor-updates` activation** (DP-2)
     Day-one / deferred?

  6. **Default sources on installed system** (DP-3)
     Which suites enabled by default?

  7. **Unattended-upgrades shipping** (DP-3)
     Pre-configured / opt-in / not shipped?

  8. **Update-notification mechanism** (DP-3)
     cron / systemd-timer / motd / none?

  9. **Component count** (DP-4)
     `main` only / + contrib + non-free / Thor-specific?

  10. **Architecture coverage** (DP-4)
      amd64 only / + others?

  11. **Source publication** (DP-4)
      Publish source packages too / binary only?

  12. **Per-package snapshot override** (DP-5)
      Implement / defer / use full re-snapshot for security pkgs?

  13. **`build-snapshot` embedding mechanism** (DP-5)
      Fork pkg / chroot.py / both?

  14. **`thor-update-check` tool** (DP-5 + DP-7)
      Ship day-one / later / never (rely on apt list --upgradable)?

  15. **Cadence trigger** (DP-6)
      Debian-point-release / time-based / CVE-accumulation / hybrid?

  16. **Historical ISO retention** (DP-6)
      Keep all / last N?

  17. **Testing ISOs** (DP-6)
      Publish nightly/weekly testing builds / no testing channel?

  18. **`releases/` browse surface** (DP-7)
      Static HTML index / JSON-only?

  19. **`security-since-prior` source** (DP-7)
      Auto-generated from Debian tracker / hand-curated / hybrid?

  20. **Manifest signing** (DP-7)
      Sign releases/<ver>/*.json / leave unsigned?

---

## Out of scope for this document

  - Specific implementation of `publish_repo` command — that's
    COMP-02 (slot for when this design is locked).
  - Security-tracker polling job implementation — separate ticket
    once Layer 3 is approved.
  - Web frontend / documentation site — separate concern.
  - Cross-distribution mirroring (someone mirrors Thor's repo on
    their own infra) — addressable later via the standard mirror
    protocol since we're shipping a standard apt repo.

---

## Decision log

(Populate as decisions land.  Each entry: date, decision number from
"Open decisions" above, choice, rationale.)

  - (empty)
