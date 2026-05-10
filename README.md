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
 - This will be a maturing solution and not immediately suitable to building production system. Currently, best used for tinkering.
 - Can this be faster, YES. Is it worth making it faster (e.g. shifting to C, trading space with time, etc.) NO
 - It is NOT currently (or ever may be) supported by any of Debian Linux Houses (e.g. debian, ubuntu, etc)
 - Does it have Bugs - YES / MANY, please reach out to me and lets fix what you find.
 - REMEMBER, and this is especially important is that, it is a source build platform, it does nothing to upstream source packages. What you get is what you get. You will (rather quickly) realise as I have that just because source code is available doesnt mean it is ammeniable to being built. Fixing that is completely on you. You will learn to embrace a whole new level of 'oh, but it builds on my system'.

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

1. `build_cache` — pulls `Packages` and `Sources` indices from each configured mirror, GPG-verifies the `InRelease` signature against `debian-archive-keyring`, and assembles an in-memory APT cache.
2. `parse_dependency` — resolves the package list in `config/pkg.list` into a closed dependency graph (binary deps + matching source packages).
3. `source_download` — fetches `.dsc`, `.orig.tar.*`, and `.debian.tar.*` files for every selected source.
4. `build_container` — builds a per-release Docker image carrying the build-deps for the source packages.
5. `source_build` — runs `dpkg-buildpackage` inside the container for each source, applies any patches under `patch/source/<pkg>/<ver>/`, drops the resulting `.deb` files into `repo/`.  Default mode skips *extras-only* sources (depth-1 Recommends pulled in by `parse_dependency` for the future apt-repo); `source_build recommended` builds those.  A bracket-token like `source_build live-boot-doc [nocheck]` overrides `DEB_BUILD_PROFILES` + `DEB_BUILD_OPTIONS` for that one build — useful when you want a `-doc` binary the default `nodoc` profile would skip.
6. `build_chroot` — installs the built `.deb`s into a chroot under `buildroot/` in topo-sorted batches, handling the libc bootstrap cycle, post-install patch overlays, and the canonical libdevmapper/dmsetup/systemd cycle (ARCH-12).  Runs `verify_chroot` automatically as its tail end.
7. `verify_chroot` — the 8-check verifier from step 6, also invokable on its own (e.g. after a manual edit of the chroot tree).  Fails loud; gates `build_iso`.
8. `build_iso` — wraps the chroot into a squashfs, runs `grub-mkrescue` to produce a hybrid BIOS/EFI bootable ISO under `image/`.

Plus, off to one side:

- `tunnel_package` — for packages you've explicitly *chosen not* to build from source.  Pulls the prebuilt `.deb` straight from the base Debian repo and drops it into `repo/` alongside the from-source builds.  Reads the `Tunneled` list in `config/build.conf` (or accepts package names as args).  Not part of `autorun` — opt-in, run after `parse_dependency` and before `build_chroot`.

Each stage sets a `BuildFlags` bit on success; later stages refuse to run unless their prerequisites are set.  `autorun` walks stages 1–6 in order (which gets you a verified chroot) and bails on the first failure; you then run `build_iso` once you're happy.

### Prerequisites

A Debian-derived host. Development happens on Debian bookworm; trixie should work, current Ubuntu LTS likely too. You need:

- **sudo**. The chroot install steps shell out to `mount --bind`, `chroot`, `dpkg`. Run the build as a normal user; the TUI will prompt for your sudo password at the start of `build_chroot` (and again at the start of `build_iso`) and zero it from memory the instant each command exits — pass or fail (see STA-07).
- **Docker Engine** (not Docker Desktop). The source-build container runs build-deps in isolation. The `Misc / Installing Docker` section at the bottom has the apt incantation for an up-to-date Engine.
- **Python ≥ 3.9** plus `python3-apt`, `python3-debian`, `python3-gnupg`, `python3-requests`, `python3-psutil`, `python3-docker`. The wrapper `build-system.sh` checks `py_requirements.txt` and tells you what's missing.
- **debian-archive-keyring** — used to GPG-verify mirror `InRelease` files. On a Debian host it's almost always there; on Ubuntu you may need to apt-install it (see `[Security]` in `config/build.conf` for the keyring path).
- **Disk** — budget ~30 GB for a full bookworm-derived build. The bulk lives in `source/` (raw upstream tarballs), `build/` (per-package build trees inside the container), `repo/` (the produced `.deb`s), and `buildroot/` (the chroot the ISO is built from).
- **RAM** — 8 GB is workable; 16 GB makes the source-build stage less painful, particularly once parallel builds land (COMP-03).
- **Bandwidth** — first `build_cache` + `source_download` will pull a few GB. The rest is local.

### First run

Clone the repo, then:

```
cd Athena-Build
./build-system.sh
```

The wrapper will tell you about any missing Python deps. Install them and re-run. The TUI launches.

From there:

- `print config` shows what `config/build.conf` resolved to — which mirrors are active, whether snapshot pinning is on, what `[Build]` codename will be baked into `/etc/os-release`. Run this first to confirm you're building what you think you're building.  (`print help` lists the other views.)
- `autorun` runs stages 1–6 (cache → parse → download → container → source_build → chroot+verify). Expect ~30–60 minutes on a warm cache, longer on a first run because of the source download and the container build.
- When `autorun` finishes cleanly, run `build_iso` to produce the final image. (If you want to drop extra files into `buildroot/` first — custom motd, extra config — this is the moment.)
- If a stage fails, fix the cause and re-run that stage by name (`source_build`, `build_chroot`, etc.). `autorun` is a convenience, not a state machine — for actual resume across a restart, use `resume`.
- `resume` (or `--resume` at startup) skips the slow rebuilds: loads the persisted `Cache` + `DependencyTree` from `<dir_cache>/{cache,deptree}.pkl.gz` (SHA256-verified), then re-validates downloads / Docker container / per-package builds (each step is internally idempotent on warm state), then runs `verify_chroot` (or `build_chroot` if not previously built).  Useful after a TUI crash, machine reboot, or just when coming back to a build the next morning.  `BuildFlags` is autosaved to `<dir_cache>/buildflags.json` on every flag change so you can also see "where you left off" via `print state`.
- TUI keys: arrows to scroll the active tab, `Tab` to cycle between tabs (console / log), `q`/`Q` to quit. Resize is handled automatically.
- `print extras` shows the depth-1 *Recommends* of your selected packages that have been pulled into `repo/` but kept *out* of the chroot install — there to support `apt install <thing>` from a booted Athena system later, not to bloat the live ISO.  Toggle off with `[Build] IncludeRecommendsInRepo = false` if you want a strictly minimal repo.  See §Working with the package set below.
- `generate_signing_key` (one-time) creates the project's GPG keypair under `<dir_gnupg>/signing/`; `verify_signing_key` sign+verifies a test payload to confirm it's usable; `print signing` shows the key's fingerprint + uid + state.  The signed apt-repo (CONF-01/02 phase 2) builds on top of this; for now the key just sits there ready to be used.  See `[Repo] SigningKeyUid` in `config/build.conf` for the identity string.  **`build_chroot` gates on the signing key up front**: it runs a sign+verify roundtrip before any sudo/mount/dpkg work.  If no key exists, you get a `Generate a new signing key now? [y/n]` prompt — say yes to generate-and-continue (~30-60s) or no to abort.  Once verified, `build_chroot` copies the public key into the chroot at `/usr/share/keyrings/athena-archive-keyring.gpg` (the conventional distro-signing-key location, ready for any future `apt source [signed-by=...]` entry).  `verify_chroot` reports keyring presence on every run.

The final ISO appears under `image/` named `athena-<version>-amd64.iso`. A sidecar `<iso>.user` file next to it carries the per-build random username for the live boot (see SEC-04).

### Working with the package set

The shipped pipeline already handles two distinct flavours of "package":

1. **Install set** — what actually goes into the live ISO.  Comes from `config/pkg.list` plus the required/important closure plus their hard `Depends`.  These get built (or tunneled) by default `source_build` and installed by `build_chroot`.
2. **Extras** — depth-1 *Recommends* of the install set.  They land in `selected_pkgs` (so `source_download` fetches their tarballs) but are filtered out of the chroot install and out of the default `source_build` run.  When you eventually publish the repo (CONF-01/02), they're what someone running `apt install <thing>` on a booted Athena system gets that *isn't* already on the disk.

You don't have to think about this on a normal `autorun` — the defaults do the right thing.  When you do want to poke at it, the relevant commands are:

```
print extras                          # what's in the extras pool, with source mapping
print stats                           # counts incl. "extras (recom): N pkg(s) (toggle=on|off)"

source_build                          # build the install set (default — skips extras-only sources)
source_build recommended              # build ONLY the extras-only sources
source_build foo                      # build a specific source
source_build force foo                # rebuild even if .result says PASS
```

#### Overriding build profiles per invocation

`config/build.conf`'s `[Source] BuildProfiles = nodoc, nocheck` is sensible for the install set — most people don't want man-pages and test runs in the live ISO.  But when an extras package depends on those profiles being *off* (the obvious case being `live-boot-doc`, whose binary stanza in `debian/control` carries `Build-Profiles: <!nodoc>`), the default build silently drops it.

The escape hatch is a bracket-token in the `source_build` args:

```
source_build live-boot-doc [nocheck]
```

The bracket-token replaces *both* `DEB_BUILD_PROFILES` and `DEB_BUILD_OPTIONS` for that single invocation.  The example above drops `nodoc`, so `dpkg-buildpackage` produces `live-boot-doc.deb` and it lands in `repo/`.  Other valid forms:

| Command | What it does |
|---|---|
| `source_build foo []` | rebuild `foo` with NO profiles/options at all (most permissive — runs tests, generates docs) |
| `source_build foo [nocheck]` | rebuild `foo` with profiles/options = `nocheck` only |
| `source_build foo [nocheck,nodoc]` | rebuild `foo` with both |
| `source_build [nocheck]` | rebuild all install-set sources with the override |
| `source_build recommended [nocheck]` | rebuild all extras-only sources with the override |

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

### Common failure modes

Things that go wrong, ordered by how often they actually bite:

**`InRelease` signature verification fails.** The mirror you pointed at is not signed by the keys in `debian-archive-keyring`, or your keyring is stale. Check `[Security]` in `config/build.conf`. The honest fix is to apt-install a fresher `debian-archive-keyring`. You *can* set `Disabled = true` to bypass for one run, but then you have no GPG check on what you're building from — don't do that on a mirror you don't run yourself.

**`build_cache` says some `.deb` is missing from the mirror.** Mirrors lag. If you have `[Snapshot] Enabled = true` (the shipped default — see STA-03), you've pinned a specific timestamp and the file *should* be there. If it isn't, either the snapshot timestamp is broken or you typed it wrong; pick a fresh one from <https://snapshot.debian.org> and edit `[Snapshot] Timestamp`. If snapshot is off, the live mirror just doesn't have the file you asked for — pick a different mirror or wait for sync.

**`source_build` fails with "missing build-dep `libfoo-dev`".** The build container doesn't have the build-deps for that source package. Either the container hasn't been rebuilt against the current `pkg.list`, or `libfoo-dev` lives in `non-free` / `contrib` and your mirror config doesn't include those components. Re-run `build_container`, or add the right component to the mirror block in `config/build.conf`.

**A binNMU package isn't found on disk.** APT advertises `foo_1.2-3+b2_amd64.deb` but `dpkg-buildpackage` produced `foo_1.2-3_amd64.deb` from source. The pipeline strips the `+bN` suffix automatically (see `utils.strip_build_version` and STA-15); if you see this error anyway it usually means the source package failed silently in stage 5 and no `.deb` was produced. Re-run `source_build` and watch for the failed source — its dpkg log will be in the run log.

**A `-doc` (or other Build-Profiles-gated) binary isn't in `repo/`.** `[Source] BuildProfiles = nodoc, nocheck` is the shipped default — it strips any `debian/control` stanza marked `Build-Profiles: <!nodoc>`, which silently drops doc binaries.  If you want the doc binary in your repo (so it's installable later via apt), rebuild that one source with the profile override:  `source_build live-boot-doc [nocheck]`.  Drops `nodoc` for that invocation only, produces the doc `.deb`, lands it in `repo/`.  See §Working with the package set.

**Patch fails to apply with "fuzz" or "hunk failed".** A file under `patch/source/<pkg>/<ver>/9001-*.patch` no longer applies to the upstream source — the upstream changed between when the patch was written and now. Either pin the package version in `config/pkg.list` to the version the patch was written against, or regenerate the patch (see the `Source Code Patching` section below). Both are valid; pinning is faster, regenerating is correct.

**`build_chroot` aborts on a single package's `dpkg --configure` step.** Look for the dpkg transcript in `log/build-*.log`. The project runs every dpkg/apt invocation under `DEBIAN_FRONTEND=noninteractive` plus `DEBCONF_NONINTERACTIVE_SEEN=true`, and writes a minimal `/etc/debconf.conf` into the chroot before the first dpkg call (see `_init_dpkg_database` in `chroot.py`), so debconf takes its defaults instead of prompting. If a package still fails configure, it's almost always a real maintainer-script error — read the dpkg log for the actual cause; it's no longer being swallowed by `--force-depends` (STA-02 removed that mask).

**`verify_chroot` reports "linux-image installed but no kernel in /boot/".** A kernel package was unpacked but its post-install hook didn't fire — usually because `/proc` wasn't bind-mounted at the right moment. Re-run `build_chroot` from clean (delete `buildroot/` first); the second pass typically catches it. STA-10 hardened the mount checks, so this should be rare on the current code.

**Dep-graph cycle the libc-seed didn't break.** ARCH-12 handles the canonical libdevmapper ↔ dmsetup ↔ systemd cycle automatically by emitting a terminal "force-depends" batch. A custom `pkg.list` can introduce a *different* cycle that escapes the seed. The chroot builder will name the offending packages in the log; usually you can break the cycle by removing whichever one isn't actually needed, or by accepting the force-depends batch and letting `_configure_packages` recover.

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

## Misc

### Installing Docker

Installing docker manually, the distribution repo packages are old.
Everything under superuser
```commandline
apt-get remove docker docker-engine docker.io containerd runc
apt-get install ca-certificates curl gnupg lsb-release
mkdir -m 0755 -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" |  tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker $USER
```

---

*A note to future-me (and anyone landing patches): this README is meant to track the state of the project, not just the day it was written. If you add a pipeline stage, change a default in `config/build.conf`, rename a command, retire a failure mode (or discover a new one), update this file in the same change. A README that lies is worse than a README that's missing — see `DOC-06` in `TODO.md`.*
