## Features
- Building Debian Distribution from source
- Support for Patching at Source, Pre-Install and Post-Install
- Modular Installation System
- Give process transparency and readability

# Athena-Build

## Introduction
Athena Build system is(trying to be) a (mostly) hands off 'build system' to build and install custom Debian Linux distribution. The distinction is that  sources are built rather than using the prepared packages. It is aimed to be the more transparent and flexible version of debbootstrap and live-build.

The genesis of this project came from the conversation - while the Linux ecosystem as part of the FOSS world, but as the platform matured, can we really build the solution from source? As the build systems are becoming more complex, sparsely documented and obfuscated (personal opinion).

## FYI
 - This will be a maturing solution and not immediately suitable to building production system. Currently, best used for tinkering. See **Project maturity** below for a calibrated honest assessment of where the code actually is.
 - Can this be faster, YES. Is it worth making it faster (e.g. shifting to C, trading space with time, etc.) NO
 - It is NOT currently (or ever may be) supported by any of Debian Linux Houses (e.g. debian, ubuntu, etc)
 - Does it have Bugs - YES / MANY, please reach out to me and lets fix what you find.
 - REMEMBER, and this is especially important is that, it is a source build platform, it does nothing to upstream source packages. What you get is what you get. You will (rather quickly) realise as I have that just because source code is available doesnt mean it is ammeniable to being built. Fixing that is completely on you. You will learn to embrace a whole new level of 'oh, but it builds on my system'.

## Project maturity

> **TL;DR:** ~70–75% of a v1.0 derivative-distribution toolchain. The full pipeline (cache → dep parse → source build → chroot → ISO) works end-to-end on VMware BIOS + EFI and produces signed, incrementally-publishable apt repositories. Real-hardware coverage, parallel builds, and multi-arch are the largest gates between here and "I would ship this to a paying customer." Last calibrated by a 7-phase consolidation audit on 2026-05-28.

The README's older self-assessment ("best used for tinkering") undersells where the code actually is — the pipeline produces verified bootable ISOs from source with signed metadata, an incremental update story (`+asg<R>u<N>` versioning, append-only multi-version publish), and a from-source debian-installer rebuilt through a parallel udeb dep tree. But it isn't yet at the operational maturity of an OBS or Yocto either.

### Where the code is, by dimension

| Dimension | Score | What works | What's missing |
|---|---:|---|---|
| Functional completeness | 80% | Live ISO + installer ISO + qcow2 disk image, end-to-end on VMware BIOS + EFI. Installer reaches `finish-install.d/20final-message` cleanly. | Real-hardware coverage (COMP-01h). |
| Architecture | 80% | 12-flag BuildFlags FSM, 3 autorun pipelines, parallel deb/udeb dep trees over a shared source corpus, 5 audit-cohort scopes, idempotency guards on every long-running stage. | `MaxParallelBuilds` knob exists but `cmd_source_build` is strictly serial (COMP-03). |
| Code quality | 80% | ruff-clean, comments explain *why* with date-stamped incident notes, structured `(ok, detail)` error returns, clear data-plane separation. | Two shell-string subprocess patterns flagged for consolidation (STA-20), one quiet trust-degradation in the signed-manifest reader (STA-19). |
| Tests | 75% | 615 tests, single file by policy, policy-enforcer tests pin invariants (read-only-named commands can't call destructive helpers, stage-D path bans, fork helper-prefix shape, etc.). 558/558 pass under `python3 tests/test_module.py`. | ~10–15% silently no-op when host tools (dpkg-deb, gpg) are absent; ~22 `time.sleep` calls in TUI tests are timing-fragile. |
| Reproducibility | 70% | Snapshot pinning via snapshot.debian.org, Docker image tagged + Dockerfile-hashed, post-build NMU strip yields pristine versions, sources.list rewritten in-container with `[check-valid-until=no]`. | No `reprotest` gate yet (CONF-06); SBOM (CycloneDX/SPDX) not generated (CONF-07). |
| Security | 80% | InRelease GPG verified per mirror against debian-archive-keyring; project signing-key with sign+verify roundtrip gate before any chroot work; signed `Release`/`InRelease` on the published repo; Docker daemon URL guard; sudo password scrubbed after use. | STA-19 (manifest reader fail-closed); signing key uses `%no-protection` (no passphrase) — documented but operator-exposed. |
| Operability | 75% | Curses TUI + headless `--cli` backend share one console facade; 12 top-level commands with `_group_help` tables; rich `print state/config/extras/…` views; per-build timestamped log files; structured autorun summary. | UX-05 sub-items pending (`--yes`, env-var sudo, ANSI color in CLI mode, `-c <cmd>` one-shot, Ctrl+C grace); no localization (UX-06). |
| Identity / branding | 85% | Three-layer model (Athena toolchain / Asgard distribution / thor codename) enforced through `@DISTRIBUTION@/@BASE_ID@/@CODENAME@` token-subst in fork content. Collision gate FAILS the cache when upstream would dominate a fork. 8 forks audited clean. | A few Debian-named packages (`debian-faq`, `reportbug`) intentionally kept in pool for `[standard]` task; CONF-10 redesign pending. |
| Documentation | 65% | This README, `TODO.md`, `docs/done.md`, 8 plan docs, `docs/branding-methodology.md`, `docs/pseudocode.md`, `docs/diagrams/build-fsm.{dot,png}`. | `docs/architecture.md` (DOC-02), `docs/patching.md` (DOC-03), `docs/release.md` (DOC-04). |
| Scale / portability | 50% | Single-arch (amd64). Single-distro derivation (Debian bookworm). | COMP-04 (arm64), COMP-07 (per-release containers), COMP-11 (`Distro = ubuntu`). |

### Comparative landscape

The Linux distro build/composition tooling sits in two broad camps. Athena is squarely in the second.

**Prebuilt-binary composers** (faster, narrower, NOT what Athena is):

| Tool | Comparison |
|---|---|
| **debootstrap** | Bootstraps a minimal Debian root from a mirror. Mature, narrow, used as a building block — not a comparator. |
| **live-build (Debian Live)** | Closest *shape* comparator. Pulls prebuilt `.debs` into a chroot, wraps in squashfs + GRUB. Athena's pipeline structure is similar but **Athena builds every package from source**; live-build trades that 30–60 min build time away for upstream-current binaries. |
| **simple-cdd / mkarchiso / ubuntu-cdimage** | Distro-specific equivalents of live-build. Canonical's tooling is the most operationally mature; closed/internal. |
| **Kickstart / anaconda (RHEL/Fedora)** | rpm/dnf side equivalent of d-i + simple-cdd combined. Different ecosystem, same camp. |

**Source builders** (slower, more ambitious, what Athena is):

| Tool | Comparison |
|---|---|
| **Yocto / OpenEmbedded** | Closest in *spirit*. Industrial-strength, ~15 years mature, multi-arch by design, parallel by default, infamous learning curve. Yocto targets embedded; Athena targets a Debian-faithful desktop/server derivative. Yocto is several orders of magnitude larger as a codebase + community. |
| **buildroot** | Embedded Linux from source, simpler than Yocto. Same spirit, smaller surface. |
| **Gentoo's catalyst** | Builds Gentoo stage tarballs from source via portage. Mature but Gentoo-specific. |
| **Open Build Service (openSUSE/SUSE)** | Industrial multi-distro package build service with web UI + build farm. Builds *packages*; Athena builds *distribution compositions* on top of building packages. Different scope. |
| **NixOS / Nix** | Different paradigm — declarative, content-addressed, no FHS. Not a direct comparator. |
| **Devuan's build process** | Closest *ideological* neighbor. Devuan rebuilds ~50 systemd-tainted packages; Athena rebuilds the whole `pkg.list` closure (~800–900 sources). "Devuan but more total." |
| **Pop!\_OS / elementary / LMDE** | Same end-state ambition as Asgard; build tooling private/opaque. Athena is the tooling you'd write if you wanted to be one of these but transparent. |
| **Lunar / Sorcery / Frugalware** | Hobbyist source-built distros. Lower activity, less rigorous. Athena is meaningfully more current. |

### Where Athena is unusually strong

- **Inline incident archeology.** Comments explain *why* with date-stamped notes pointing at the original failure. Most peers don't document at this depth.
- **Append-only multi-version publish.** The `+asg<R>u<N>` versioning + signed-manifest authority is more rigorous than live-build's "rebuild and overwrite" model.
- **Fork collision gate.** The cache build FAILS when an upstream version would dominate a fork's version. Most derivatives silently ship the regression.
- **Fail-loud identity policy.** No Debian residue allowed without an explicit allowlist; tested by source-grep policy enforcers.
- **Snapshot pinning to `snapshot.debian.org`.** Full reproducibility across runs in a way live-build is not by default.

### Where Athena is unusually weak

- **Single-host, single-operator.** No build farm, no CI matrix, no per-PR build validation.
- **Single arch.** Multi-arch is a significant cross-cutting refactor (COMP-04).
- **No SBOM.** Increasingly expected of derivative distros; CONF-07 is open.
- **Real-hardware testing depends on the operator** having access to varied hardware. Yocto has YP-CI; Canonical has hardware-enabled labs; Athena has the operator's local machines + VMware.

### Honest bottom line

For a derivative-distribution project, today's maturity is at the point where it **could ship a v1.0 to early adopters** — the pipeline is correct, the metadata is signed, the update story works. The largest gates to genuine production use are COMP-01h (real-hardware), COMP-03 (parallel builds), CONF-06 (reproducibility verification), and an enlarged test matrix. Operators with a tolerance for build time and a willingness to fix the occasional upstream-source-doesn't-build problem will find the tool more capable than the README's "tinkering" framing suggests.

### Linux
The first question always is - What is Linux?  Linus Torvalds while studying at the University of Helsinki, wrote (for multiple reasons that I am not getting into here) a clone of UNIX operating system called 'Minix' and was supposed to be compatible to ***System V***. 

Accordingly, We ended up with the Ver 0.1 of the **Linux Kernel**. Unfortunately, the Kernel had no application ecosystem to run as remained as such an essential cog in a non-existing ecosystem. Then came along Richard Stallman and GNU and gave it purpose. They brought the application stack that gave Linux Kernel purpose, and hence was born the Linux Distribution, or more colloquially just called **Linux Distribution**. The conversation of distinction between 'Linux Distribution' and 'Linux OS' is a petridish for violence amongst geeks, but for the purpose of this project lets assert debian is a 'Linux Distribution' and stay away from the phrase OS as puch as possible.

The first Linux distribution, called "Softlanding Linux System" (SLS), was released by 1992. and within the next three years we saw the advent of Slackware, Red Hat and Debian. The rest as they say is history.

PS: Red Hat vs Debian - Red Hat was founded with the goal of creating a commercial distribution of Linux that could be sold and supported. Red Hat's approach was to take the existing Linux codebase, add value in the form of support, services, and tools, and sell it to enterprise customers. On the other hand, Debian was founded  with the goal of creating a community-driven Linux distribution that was completely free, open-source and built from scratch, with a focus on stability, security, and ease of use. 

We are currently only looking at Debian & Debian based distributions

### Linux Distribution
A Linux distribution is a complete set of packages included (but not limited to) the Linux kernel, system utilities, applications, and software libraries, along with a package management system and other tools for managing and configuring the system. A Linux distribution is typically designed and packaged by a community or organization, and is intended to provide a complete, ready-to-use OS that can be installed and configured on a variety of hardware platforms.

### Packages and Package Manager
In a Linux distribution, a package is akin to SKU (Stock Kepping Unit) of software that can be installed and managed by the operating system's package manager. A package may include one or more applications, libraries, along with configuration files required to run the software on the system. 

The packages may intrensicly also define other packages as dependencies and it is usually the package manager which checks to ensure that all required dependencies are present and installs any missing dependencies as needed. THe package manager abstracts away the complexity of installing, managing, updating, and removing software packages.

Packages in a Linux distribution may be maintained by the distribution's own developers or by third-party contributors. In this context Debian. They (Debian) identify application, wrap the application's build system to produce the installables as a package construct, i.e. deb - debian package file, test it, patch it, abd publish it in a repository.

Dpkg is a low-level package manager that is used by the Debian and Ubuntu distributions. It is responsible for managing the installation and removal of individual software packages on a system. Dpkg works by maintaining a database of installed packages and their dependencies, and it uses this information to ensure that all required packages are present and properly configured when a package is installed.

Apt, or Advanced Package Tool, is a higher-level package manager that builds on top of dpkg. It is used to manage the entire software repository for a distribution, including all official packages and any third-party repositories that have been added. Apt provides a more user-friendly interface than dpkg, with features such as automatic dependency resolution, automatic package updating, and easy installation of packages from remote repositories.

### Repositories

Debian's package repositories are organized into several official repositories, including "main", "contrib", and "non-free", as well as a "backports" repository for newer software versions. The "main" repository contains packages that are completely free and open-source, while the "contrib" and "non-free" repositories contain packages that may have non-free or proprietary components. 



### RHEL/Debian/Ubuntu

The Linux distribution world (broadly) splits into two camps that have agreed on package format: the dpkg/apt camp (Debian and its many descendants), and the rpm/dnf camp (Red Hat and its descendants — RHEL, CentOS, Rocky, AlmaLinux, Fedora). They look superficially the same from a user's seat — kernel, userland, init, package manager — but the *philosophies* are different, and the philosophy bleeds into how you build.

Red Hat optimises for a controlled commercial pipeline. Packages are RPMs, the policy is set by the vendor, and the source for any given binary goes through the distribution's own build farm. Signing and release cadence is tighter and slower, which is exactly what enterprise customers buying support contracts wanted.

Debian sits at the other end of the same axis. Packaging is community-driven, every package has a *maintainer* (a real person, who has signed a social contract), the source is downloadable and rebuildable by anyone, and the Free Software requirements are taken seriously enough that an entire `non-free` archive exists to keep `main` clean. The cost is that Debian moves at the pace of consensus — stable releases land when they land.

Ubuntu (Canonical) is the pragmatic middle. It takes Debian's package format, source policy, and most of its archive, then adds a more predictable six-month release cadence, an opinionated default install, paid LTS support, and a willingness to ship `non-free` bits in `main` (drivers, firmware) where Debian wouldn't. From a *build* perspective Ubuntu and Debian are very close — same `apt`/`dpkg`/`dpkg-buildpackage` toolchain, same source-package layout — which is why this project's design choices port to either. (See COMP-11 in `TODO.md` for the day Athena gains a `Distro = ubuntu` switch.)

A practical note for anyone coming from RHEL: this project will be largely incomprehensible for the first afternoon. Conventions are different, assumed reading is different. Stick with it; the underlying ideas are the same.

### Stiched together

So we have a kernel, a libc, a userland, a package manager, mirrors that hand out signed metadata, source archives that build into binary packages — what does it actually take to *stitch* this into a working system?

Roughly: bootstrap a minimal root filesystem, teach it to find packages, install everything you want it to ship with, wire up the boot loader and the init system, write the identity files (`/etc/os-release`, `/etc/hostname`, network config), and wrap the whole tree into something a machine can boot from — a live ISO, an installer ISO, or a disk image.

Done by hand this is a long week. `debootstrap` automates the bootstrap step. `live-build` automates the live-ISO bit. `debian-installer` automates the installer-ISO bit. Each tool does one piece well, and gluing them together for a *custom* distribution — your own package set, your own patches, your own branding — is where the friction usually shows up.

Athena-Build is one attempt at that glue, with the additional constraint that everything ships from source. The chapters that follow walk through what that looks like in practice — what to install on the host before you start, how to drive the build, where to look when something breaks (and it will).


## Building Image

### Intro

The build system is a curses TUI driven by `build-system.sh`. There is one shipped pipeline (`autorun`) that drives the build through to a verified chroot; the final ISO step is left as a separate manual command on purpose, so you have a chance to inspect or fiddle with the chroot before it gets sealed into a squashfs.

The pipeline (eight stages, plus one optional side-channel):

1. `cache build` — pulls `Packages` and `Sources` indices from each configured mirror, GPG-verifies the `InRelease` signature against `debian-archive-keyring`, and assembles an in-memory APT cache.
2. `dep parse` — resolves the package list in `config/pkg.list` into a closed dependency graph (binary deps + matching source packages).
3. `source sync` — fetches `.dsc`, `.orig.tar.*`, and `.debian.tar.*` files for every selected source.
4. `container init` — builds a per-release Docker image carrying the build-deps for the source packages.
5. `source build` — runs `dpkg-buildpackage` inside the container for each source, applies any patches under `patch/source/<pkg>/<ver>/`, drops the resulting `.deb` files (and `.udeb`s, when the source declares them) into `repo/`.  Default mode (= `source build pkg`) builds the **pkg.list closure only** — the user-choices layer minus live extras, installer udebs, and Recommends-only extras.  Layered subsets: `source build live` builds the live extras (live-boot, live-config, …); `source build installer` builds the udeb closure for the installer ramdisk + the installer-exclusive deb extras; `source build recommended` builds the depth-1 Recommends pulled in by `dep parse` for the future apt-repo.  A bracket-token like `source build live-boot-doc [nocheck]` overrides `DEB_BUILD_PROFILES` + `DEB_BUILD_OPTIONS` for that one build — useful when you want a `-doc` binary the default `nodoc` profile would skip.
6. `chroot build` — installs the built `.deb`s into a chroot under `buildroot/` in topo-sorted batches, handling the libc bootstrap cycle, post-install patch overlays, and the canonical libdevmapper/dmsetup/systemd cycle (ARCH-12).  Runs `chroot verify` automatically as its tail end.  Bare `chroot build` is shorthand for `chroot build live` (today's default); `chroot build installer` is the d-i installer-chroot path — currently a stub (COMP-01a).
7. `chroot verify` — the 8-check verifier from step 6, also invokable on its own (e.g. after a manual edit of the chroot tree).  Fails loud; gates `iso build live`.
8. `iso build live` — wraps the chroot into a squashfs, runs `grub-mkrescue` to produce a hybrid BIOS/EFI bootable ISO under `image/`.  (`iso build installer` is the parallel installer-ISO path — stubbed today, lands with COMP-01a.)

Plus, off to one side:

- `iso build disk [size_gb]` (COMP-09) — pre-installed bootable qcow2 disk image from the live chroot.  Same gate as `iso build live` (chroot must be verified).  Output: `image/<distribution>-<version>-<arch>.qcow2`.  Boots directly into the running OS — no installer step.  Extra host prereqs: `rsync`, `dosfstools` (`mkfs.fat`), `qemu-utils` (`qemu-img`).  Defaults to 5 GB sparse; override per-invocation or via `[Build] DiskImageSizeGB`.
- `repo tunnel` — for packages you've explicitly *chosen not* to build from source.  Pulls the prebuilt `.deb` straight from the base Debian repo and drops it into `repo/` alongside the from-source builds.  Reads the `Tunneled` list in `config/build.conf` (or accepts package names as args).  Not part of `autorun` — opt-in, run after `dep parse` and before `chroot build`.

Each stage sets a `BuildFlags` bit on success; later stages refuse to run unless their prerequisites are set.  `autorun` walks stages 1–6 in order (which gets you a verified chroot) and bails on the first failure; you then run `iso build live` once you're happy.

### Prerequisites

A Debian-derived host. Development happens on Debian bookworm; trixie should work, current Ubuntu LTS likely too. You need:

- **sudo**. The chroot install steps shell out to `mount --bind`, `chroot`, `dpkg`. Run the build as a normal user; the TUI will prompt for your sudo password at the start of `chroot build` (and again at the start of `iso build live`) and zero it from memory the instant each command exits — pass or fail (see STA-07).
- **Docker Engine** (not Docker Desktop). The source-build container runs build-deps in isolation. See [`docs/install-docker.md`](docs/install-docker.md) for the apt incantation that gets you an up-to-date Engine from Docker's own repo (the distro packages are usually too old).
- **Python ≥ 3.9** plus `python3-apt`, `python3-debian`, `python3-gnupg`, `python3-requests`, `python3-psutil`, `python3-docker`. The wrapper `build-system.sh` checks `py_requirements.txt` and tells you what's missing.
- **debian-archive-keyring** — used to GPG-verify mirror `InRelease` files. On a Debian host it's almost always there; on Ubuntu you may need to apt-install it (see `[Security]` in `config/build.conf` for the keyring path).
- **Disk** — budget ~30 GB for a full bookworm-derived build. The bulk lives in `source/` (raw upstream tarballs), `build/` (per-package build trees inside the container), `repo/` (the produced `.deb`s), and `buildroot/` (the chroot the ISO is built from).
- **RAM** — 8 GB is workable; 16 GB makes the source-build stage less painful, particularly once parallel builds land (COMP-03).
- **Bandwidth** — first `cache build` + `source sync` will pull a few GB. The rest is local.

### First run

Clone the repo, then:

```
cd Athena-Build
./build-system.sh
```

The wrapper will tell you about any missing Python deps. Install them and re-run. The TUI launches.

From there:

- `print config` shows what `config/build.conf` resolved to — which mirrors are active, whether snapshot pinning is on, what `[Build]` codename will be baked into `/etc/os-release`. Run this first to confirm you're building what you think you're building.  (`print help` lists the other views.)
- `autorun` runs stages 1–7 (cache → parse → sync → container → source build (pkg) → source build live → chroot+verify). Phase 4 of COMP-01b split source build into the `pkg` and `live` arms; both must run before chroot build live. Expect ~30–60 minutes on a warm cache, longer on a first run because of the source download and the container build.
- When `autorun` finishes cleanly, run `iso build live` to produce the final image. (If you want to drop extra files into `buildroot/` first — custom motd, extra config — this is the moment.)
- If a stage fails, fix the cause and re-run that stage by name (`source build`, `chroot build`, etc.). `autorun` is a convenience, not a state machine — there is no resume.
- TUI keys: arrows to scroll the active tab, `Tab` to cycle between tabs (console / log), `q`/`Q` to quit. Resize is handled automatically.
- `print extras` shows the depth-1 *Recommends* of your selected packages that have been pulled into `repo/` but kept *out* of the chroot install — there to support `apt install <thing>` from a booted Athena system later, not to bloat the live ISO.  Toggle off with `[Build] IncludeRecommendsInRepo = false` if you want a strictly minimal repo.  See §Working with the package set below.
- `key generate` (one-time) creates the project's GPG keypair under `<dir_gnupg>/signing/`; `key verify` sign+verifies a test payload to confirm it's usable; `print signing` shows the key's fingerprint + uid + state.  The signed apt-repo (CONF-01/02 phase 2) builds on top of this; for now the key just sits there ready to be used.  See `[Repo] SigningKeyUid` in `config/build.conf` for the identity string.  **`chroot build` gates on the signing key up front**: it runs a sign+verify roundtrip before any sudo/mount/dpkg work.  If no key exists, you get a `Generate a new signing key now? [y/n]` prompt — say yes to generate-and-continue (~30-60s) or no to abort.  Once verified, `chroot build` copies the public key into the chroot at `/usr/share/keyrings/athena-archive-keyring.gpg` (the conventional distro-signing-key location, ready for any future `apt source [signed-by=...]` entry).  `chroot verify` reports keyring presence on every run.

The final ISO appears under `image/` named `athena-<version>-amd64.iso`. A sidecar `<iso>.user` file next to it carries the per-build random username for the live boot (see SEC-04).

### Working with the package set

The shipped pipeline already handles two distinct flavours of "package":

1. **Install set** — what actually goes into the live ISO.  Comes from `config/pkg.list` plus the required/important closure plus their hard `Depends`.  These get built (or tunneled) by default `source build` and installed by `chroot build`.
2. **Extras** — depth-1 *Recommends* of the install set.  They land in `selected_pkgs` (so `source sync` fetches their tarballs) but are filtered out of the chroot install and out of the default `source build` run.  When you eventually publish the repo (CONF-01/02), they're what someone running `apt install <thing>` on a booted Athena system gets that *isn't* already on the disk.

You don't have to think about this on a normal `autorun` — the defaults do the right thing.  When you do want to poke at it, the relevant commands are:

```
print extras                          # what's in the extras pool, with source mapping
print stats                           # counts incl. "extras (recom): N pkg(s) (toggle=on|off)"

source build                          # build pkg.list closure only (Phase 4 default)
source build pkg                      # explicit form of the default
source build live                     # build live-exclusive sources only
source build installer                # build udeb closure + installer-exclusive deb extras
source build recommended              # build ONLY the extras-only sources
source build foo                      # build a specific source
source build force foo                # rebuild even if .result says PASS
```

For a complete live ISO you need `source build` followed by `source build live`. autorun chains both before chroot build.

#### Overriding build profiles per invocation

`config/build.conf`'s `[Source] BuildProfiles = nodoc, nocheck` is sensible for the install set — most people don't want man-pages and test runs in the live ISO.  But when an extras package depends on those profiles being *off* (the obvious case being `live-boot-doc`, whose binary stanza in `debian/control` carries `Build-Profiles: <!nodoc>`), the default build silently drops it.

The escape hatch is a bracket-token in the `source build` args:

```
source build live-boot-doc [nocheck]
```

The bracket-token replaces *both* `DEB_BUILD_PROFILES` and `DEB_BUILD_OPTIONS` for that single invocation.  The example above drops `nodoc`, so `dpkg-buildpackage` produces `live-boot-doc.deb` and it lands in `repo/`.  Other valid forms:

| Command | What it does |
|---|---|
| `source build foo []` | rebuild `foo` with NO profiles/options at all (most permissive — runs tests, generates docs) |
| `source build foo [nocheck]` | rebuild `foo` with profiles/options = `nocheck` only |
| `source build foo [nocheck,nodoc]` | rebuild `foo` with both |
| `source build [nocheck]` | rebuild all pkg-layer sources with the override |
| `source build recommended [nocheck]` | rebuild all extras-only sources with the override |

The override implies `force` — a prior `.result` file reflects the *old* profiles, so the cache check would falsely short-circuit the rebuild.  The TUI prints a clear note when this auto-flip happens so you're not surprised.

Multiple bracket-tokens in one invocation is an error (we don't try to merge or pick).  `recommended` and named packages are mutually exclusive — pick one mode.

### Where logs live

Everything logged at INFO and above (and `dpkg`/`mksquashfs`/`grub-mkrescue` subprocess transcripts, at DEBUG) goes to a single file:

```
log/build-YYYY-MM-DDTHH-MM-SS.log
```

One file per `build-system.sh` invocation, timestamped at start. Inside the running TUI the same content is split across two tabs: a *console* tab that mirrors what `tui.console.print` writes (the loud, user-visible traffic) and a *log* tab carrying the structured INFO/WARNING/ERROR/DEBUG records. After the run ends, the on-disk log is the canonical source of truth — the TUI buffers are gone.

If you only want the warnings and errors from a session:

```
grep -E "WARNING|ERROR" log/build-2026-05-09T*.log
```

### Consuming the built repo as an apt source

After `source build` populates `repo/`, run `repo index` to generate the apt metadata (signed `Release` + `InRelease`, per-component `Packages` and `Sources` indexes).  Layout produced (CONF-01, 2026-05-22):

```
repo/
└── dists/
    ├── <codename>/                              (the main suite)
    │   ├── Release, InRelease, Release.gpg
    │   ├── main/binary-amd64/{Packages*, Release, *.deb}
    │   ├── main/debian-installer/binary-amd64/{Packages*, Release, *.udeb}
    │   ├── main/source/{Sources*, Release, *.{dsc,tar.*}}
    │   ├── doc/binary-amd64/{Packages*, Release, *.deb}
    │   └── tests/binary-amd64/{Packages*, Release, *.deb}
    └── <codename>-debug/                        (separate suite for dbgsyms)
        ├── Release, InRelease, Release.gpg
        └── main/binary-amd64/{Packages*, Release, *.deb}
```

`<codename>` is `[Build] CODENAME` in `config/build.conf` (`thor` by default).

To consume locally via `apt`, drop a `sources.list.d` entry pointing at `file:///path/to/repo`:

```
# /etc/apt/sources.list.d/asgard-local.list
deb [signed-by=/etc/apt/keyrings/asgard.gpg] file:///path/to/repo thor main
# Optional add-ons:
deb [signed-by=/etc/apt/keyrings/asgard.gpg] file:///path/to/repo thor doc tests
deb [signed-by=/etc/apt/keyrings/asgard.gpg] file:///path/to/repo thor-debug main
```

`signed-by` should point at the project pubkey (`gnupg/pubkey.gpg`, exported during `key generate`).  For testing without the keyring, use `[trusted=yes]` instead — `apt-get update` will warn but proceed.

Publishing the repo to a remote endpoint (S3, rsync, HTTP server) is COMP-02 — the same generated tree is byte-for-byte the right shape for a remote mirror.

### Common failure modes

Things that go wrong, ordered by how often they actually bite:

**`InRelease` signature verification fails.** The mirror you pointed at is not signed by the keys in `debian-archive-keyring`, or your keyring is stale. Check `[Security]` in `config/build.conf`. The honest fix is to apt-install a fresher `debian-archive-keyring`. You *can* set `Disabled = true` to bypass for one run, but then you have no GPG check on what you're building from — don't do that on a mirror you don't run yourself.

**`cache build` says some `.deb` is missing from the mirror.** Mirrors lag. If you have `[Snapshot] Enabled = true` (the shipped default — see STA-03), you've pinned a specific timestamp and the file *should* be there. If it isn't, either the snapshot timestamp is broken or you typed it wrong; pick a fresh one from <https://snapshot.debian.org> and edit `[Snapshot] Timestamp`. If snapshot is off, the live mirror just doesn't have the file you asked for — pick a different mirror or wait for sync.

**`source build` fails with "missing build-dep `libfoo-dev`".** The build container doesn't have the build-deps for that source package. Either the container hasn't been rebuilt against the current `pkg.list`, or `libfoo-dev` lives in `non-free` / `contrib` and your mirror config doesn't include those components. Re-run `container init`, or add the right component to the mirror block in `config/build.conf`.

**A binNMU package isn't found on disk.** APT advertises `foo_1.2-3+b2_amd64.deb` but `dpkg-buildpackage` produced `foo_1.2-3_amd64.deb` from source. The pipeline strips the `+bN` suffix automatically (see `utils.strip_build_version` and STA-15); if you see this error anyway it usually means the source package failed silently in stage 5 and no `.deb` was produced. Re-run `source build` and watch for the failed source — its dpkg log will be in the run log.

**A `-doc` (or other Build-Profiles-gated) binary isn't in `repo/`.** `[Source] BuildProfiles = nodoc, nocheck` is the shipped default — it strips any `debian/control` stanza marked `Build-Profiles: <!nodoc>`, which silently drops doc binaries.  If you want the doc binary in your repo (so it's installable later via apt), rebuild that one source with the profile override:  `source build live-boot-doc [nocheck]`.  Drops `nodoc` for that invocation only, produces the doc `.deb`, lands it in `repo/`.  See §Working with the package set.

**Patch fails to apply with "fuzz" or "hunk failed".** A file under `patch/source/<pkg>/<ver>/9001-*.patch` no longer applies to the upstream source — the upstream changed between when the patch was written and now. Either pin the package version in `config/pkg.list` to the version the patch was written against, or regenerate the patch (see the `Source Code Patching` section below). Both are valid; pinning is faster, regenerating is correct.

**`chroot build` aborts on a single package's `dpkg --configure` step.** Look for the dpkg transcript in `log/build-*.log`. The project runs every dpkg/apt invocation under `DEBIAN_FRONTEND=noninteractive` plus `DEBCONF_NONINTERACTIVE_SEEN=true`, and writes a minimal `/etc/debconf.conf` into the chroot before the first dpkg call (see `_init_dpkg_database` in `chroot.py`), so debconf takes its defaults instead of prompting. If a package still fails configure, it's almost always a real maintainer-script error — read the dpkg log for the actual cause; it's no longer being swallowed by `--force-depends` (STA-02 removed that mask).

**`chroot verify` reports "linux-image installed but no kernel in /boot/".** A kernel package was unpacked but its post-install hook didn't fire — usually because `/proc` wasn't bind-mounted at the right moment. Re-run `chroot build` from clean (delete `buildroot/` first); the second pass typically catches it. STA-10 hardened the mount checks, so this should be rare on the current code.

**Dep-graph cycle the libc-seed didn't break.** ARCH-12 handles the canonical libdevmapper ↔ dmsetup ↔ systemd cycle automatically by emitting a terminal "force-depends" batch. A custom `pkg.list` can introduce a *different* cycle that escapes the seed. The chroot builder will name the offending packages in the log; usually you can break the cycle by removing whichever one isn't actually needed, or by accepting the force-depends batch and letting `_configure_packages` recover.

**Parallel `source build` oversubscribes the host.** With `[Build] MaxParallelBuilds > 1`, N concurrent `dpkg-buildpackage` runs each spawn their own `make -jM` inside the container. On a 16-thread laptop the math is unkind to firefox-esr + gcc-12 running together. Two fixes, both in `config/build.conf`: set `[Build] BuildCpus` (e.g. `4.0`) and `BuildMemory` (e.g. `8g`) for a per-container cgroup cap on cpu + RAM, and/or list the worst offenders in `[Build] HeavyPackages` (e.g. `firefox-esr, gcc-12, llvm-toolchain-15`) — the scheduler then drains every in-flight build before starting a heavy one and pauses new submissions while it runs. If a container exits 137 ("OOMKilled — likely OOMKilled; raise [Build] BuildMemory" in the log + console), that's the cgroup OOM-killer; raise BuildMemory or add the source to HeavyPackages. Ctrl+C during a parallel pool is now responsive — every in-flight container is force-removed within ~1s (COMP-03).

**The TUI window goes blank, fragments, or won't take input after a resize.** Curses got an inconsistent state from the resize. The TUI listens for `KEY_RESIZE` and is supposed to redraw itself; if it didn't, your only out is to close and re-open the terminal (the build state itself is fine — re-launch `build-system.sh` and your `BuildFlags` reset, but the cache/source/repo on disk is unchanged). The underlying issue is filed as ARCH-14 (TUI holistic rework).

When in doubt, the on-disk log under `log/` has more than the TUI shows. Start there.

### Source Code Patching
Using quilt to create patches. can use standard diff also. Mostly templates are nice in quilt. While we would have prefered to use quilt natively for applying patch too but that requires the patch file being in the tarball, else a lot of 'fuzz' errors. So to apply patching still using standard 'patch'.

Creating patch file involves > expand source > define patch file in quilt > make changes > refresh quilt > edit header (template, optional) > save patch
```
dpkg-source -x package_version.dsc
cd package-version
quilt new xxxx-description.patch
quilt header -e --dep3 
...
# make the changes, use quilt edit file
# the -e edits the header in $EDITOR
...
quilt refresh xxxx-description.patch
cp debian/patch/xxxx-description.patch <patch dir>/package/version/xxxx-description.patch
```

if you want to apply the same on expanded source package
```
patch -p1 < <patch dir>/package/version/xxxx-description.patch
```

The patch file numbering is four digit, preferably start from 9001 for simplicity sake
The patch folder will have folder for each source package 'name' and sub folders for respective versions. each patch is version specific and saved in that version folder.

## Development

Lint and unit tests run in CI on every push (`.github/workflows/ci.yml`).
To run them locally:

```
pip install ruff mypy            # one-time
ruff check .                     # style + likely-bug checks
mypy scripts/                    # type checks (advisory)
python3 tests/test_module.py     # unit tests
```

The ruff rule set is conservative — only pyflakes (`F`) plus syntax-level
errors (`E9`).  Tightening the catalogue (style, import order, modernisation)
is a follow-on task; see `ARCH-06` in `TODO.md`.  mypy is `continue-on-error`
in CI for the same reason.

## Further reading

- [`docs/architecture.md`](docs/architecture.md) — pipeline stages, `BuildFlags` contract, module overview.
- [`docs/patching.md`](docs/patching.md) — source / pre-install / post-install patch conventions + DEP-3.
- [`docs/release.md`](docs/release.md) — release runbook (snapshot pin → key → build → publish → tag).
- [`docs/repo-publish-vm-setup.md`](docs/repo-publish-vm-setup.md) — ssh + local publish + `repo summary`.
- [`docs/install-docker.md`](docs/install-docker.md) — Docker Engine install on the build host.
- [`docs/pseudocode.md`](docs/pseudocode.md) — natural-English walkthrough of every module.
- [`TODO.md`](TODO.md) — open work, with severity + status + history preserved.

---

*A note to future-me (and anyone landing patches): this README is meant to track the state of the project, not just the day it was written. If you add a pipeline stage, change a default in `config/build.conf`, rename a command, retire a failure mode (or discover a new one), update this file in the same change. A README that lies is worse than a README that's missing — see `DOC-06` in `TODO.md`.*
