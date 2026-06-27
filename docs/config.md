# Configuring your distribution

*Who this is for: someone new to Athena who wants to shape their own Linux distribution by editing config files — without reading the source code. You do not need to be a Debian packaging expert. Terms are explained as they appear.*

The most important thing to know first: **you can change nothing at all and still get a working distribution.** Out of the box the config builds a distro called *Asgard*, codenamed *thor*, derived from Debian 12 (bookworm). Everything below is about making it *yours* — your name, your package selection, your tweaks.

After any change, run `print config` inside the build tool to see exactly what your edits resolved to before you build.

---

## Words you'll see

A handful of terms come up constantly. Plain-English versions below; the full project [glossary](glossary.md) covers the rest.

- **Package** — one unit of installable software (a `.deb` file). `firefox-esr`, `coreutils`, and `gnome-shell` are each a package.
- **Dependency / closure** — a package usually needs other packages to work, and those need still others. The complete set your selection drags in, all the way down, is its **closure**. You pick a handful of packages; Athena works out the hundreds they actually require.
- **Mirror** — a server that hands out Debian's packages (e.g. `deb.debian.org`). Athena downloads *source code* from mirrors and builds it.
- **Repository (repo)** — the folder of finished packages plus a signed index that `apt` reads. Building produces *your* repo; users install and update from it.
- **Snapshot** — a frozen, dated copy of Debian. Building against a snapshot means you can rebuild months later and get byte-for-byte the same inputs, instead of whatever Debian happens to be serving today.
- **Surface** — one of the three things Athena can produce: a **live ISO** (boots from USB without installing), an **installer ISO** (installs onto a disk), and a **disk image** (a ready-to-run virtual-machine disk).
- **Group** — a named bundle of packages you define in `pkg.list` (for example `base` or `gnome-desktop`). You compose a surface by choosing which groups it includes.
- **Fork** — a package you've taken over and modified — most often to replace Debian's branding with your own.

---

## The three config files

All configuration lives in the `config/` folder. There are three files, and they divide cleanly by *who owns them*:

| File | Answers | Edit it? |
|---|---|---|
| `config/distro.conf` | **What am I building *from*, and what is my distro called?** | Yes — this is where identity lives. |
| `config/build.conf` | **What goes *in* it, and how is it built?** | Yes — this is your recipe. |
| `config/local.conf` | Settings specific to *this one machine* (tuning, endpoints). | No — the tool manages it for you. |

The first two are tracked in git and shipped as templates; they contain nothing tied to your particular computer. The third is created automatically and is machine-local — leave it to the tool unless you have a specific reason.

A quick mental model: **`distro.conf` = identity and source. `build.conf` = contents and build behaviour.**

---

## `distro.conf` — what you build from, and who you are

### Your distribution's identity — `[Build]`

```ini
[Build]
ARCH = amd64
DISTRIBUTION = Asgard
CODENAME = thor
VERSION = 1
CHANNEL = "stable"
IncludeRecommends = true
```

- **`ARCH`** — the CPU architecture you're building for. `amd64` (64-bit Intel/AMD) is the supported, tested value today; broader architecture support is on the way.
- **`DISTRIBUTION`** — your product's name, the way "Ubuntu" or "Debian" is a name. This is what shows up in the boot menu, on the login screen, and in the system's identity file (`/etc/os-release`). The lowercased form (`asgard`) becomes the internal system ID. Think of it as the long-lived brand.
- **`CODENAME`** — the name of *this particular release*, the way Debian has "bookworm". It's used as the label `apt` sees for your repo. You bump this when you cut a brand-new release.
- **`VERSION`** — a plain release number (1, 2, 3 …). It feeds the version string users see and the ordering of updates, so keep it a whole number.
- **`CHANNEL`** — a free-text label for the release track (e.g. `stable`).
- **`IncludeRecommends`** — Debian packages often list *Recommends*: companion packages that are suggested but not strictly required. When this is `true`, those companions are built and placed in your repo so users can install them later with `apt`, but they are **kept off** the live image so it stays lean. Set it `false` for a stricter, smaller repo.

> **Rename your distro:** change `DISTRIBUTION` and `CODENAME` here. That alone gives you a differently-named distro. (Logos and default settings are a separate step — see [Branding](#a-note-on-branding) below.)

### Where it's derived from — `[Base]`

```ini
[Base]
BASEURL     = http://deb.debian.org
BASEID      = debian
RELEASE     = bookworm
BASEVERSION = 12.0
```

This is the upstream Debian release you build *on top of*. The one you'll most likely touch is **`RELEASE`** — change it to rebase your whole distribution onto a different Debian release (for example `trixie`). `BASEURL` and `BASEID` only change if you mirror Debian somewhere other than the official servers.

### Which parts of Debian to pull — `[Mirror.*]`

Below `[Base]` is a series of `[Mirror.…]` sections. You rarely need to edit these; they tell Athena which slices of Debian to make available:

- **main** — the fully-free core of Debian (the default).
- **contrib / non-free / non-free-firmware** — software with non-free pieces. The most common reason to care is **firmware** (e.g. Wi-Fi chips that need a binary blob to work). These sections are present so such packages *can* be selected; only the ones you actually list in `pool.list` (and a few pulled in automatically) end up shipping.

Each mirror section also has `-updates` and `-security` variants — those bring in ongoing bug-fix and security updates for the release.

### Trust and reproducibility — `[Security]` and `[Snapshot]`

```ini
[Security]
Keyring = /usr/share/keyrings/debian-archive-keyring.gpg
Disabled = false

[Snapshot]
Enabled = true
```

- **`[Security] Keyring`** — the cryptographic keyring used to verify that what you download from Debian is genuinely from Debian. Leave it pointed at the default unless you run your own signed mirror.
- **`[Security] Disabled`** — turns that verification *off*. Don't, except in an offline test sandbox; with it off you have no guarantee about what you're building from.
- **`[Snapshot] Enabled`** — when `true` (the default), every download is pinned to a frozen, dated copy of Debian. This is what makes your builds **reproducible**: build today or in six months and the inputs are identical. The actual date it pins to is chosen for you on first run and stored separately (it's deliberately *not* in this shared file, so a fresh clone doesn't inherit your machine's pin — manage it with the `snapshot` command). Turn this off only for throwaway experiments where reproducibility doesn't matter.

### Keeping your branding honest — `[Audit]`

```ini
[Audit]
IdentityScan = true
```

When `true` (recommended), the build **fails loudly** if leftover Debian branding slips into your distro by accident — so you never ship a "Debian" label you meant to replace. Set it `false` only while you're mid-rebrand and expect incomplete results.

### Folder names — `[Directories]`

The last section just names the working folders (`source/`, `build/`, `repo/`, and so on). You can almost always leave it untouched.

---

## `build.conf` — what goes in, and how it's built

### Composing your surfaces — `[Live]` and `[Disk]`

```ini
[Live]
Groups = base, gnome-desktop      # the live ISO / default system

[Disk]
Groups = base                     # the minimal pre-installed disk image
```

This is the heart of "what goes in it". Each **surface** is built from one or more **groups** — and a group is just a named list of packages you define in `pkg.list` (next section). The live image above is `base` plus a full `gnome-desktop`; the disk image is a minimal `base`-only console system.

To put a different desktop or toolset on your live image, you define a group in `pkg.list` and add its name here. You don't list individual packages here — only group names.

### Which package lists to read — `[Packages]`

```ini
[Packages]
Pkg_List       = pkg.list
Pool_List      = pool.list
Live_List      = live.list
Installer_List = installer.list
Build_Pkg_List = build_pkg.list
```

These just name the files that hold your package selection. You only change them if you rename those files; what's *inside* them is covered in [The package lists](#the-package-lists) below.

### How packages are built — `[Source]`

This section controls the build itself. The two you're most likely to care about:

```ini
[Source]
BuildOptions  = nodoc, nocheck, noautodbgsym
BuildProfiles = nodoc, nocheck, noinsttest
```

- **`nodoc`** — skip building manual pages and documentation. Most people don't want these on a live image; it also builds faster.
- **`nocheck`** — skip running each package's test suite during its build. Big time saver; turn it off if you specifically want upstream tests to run.
- **`noautodbgsym`** — skip producing the extra "debug symbol" packages used for deep crash analysis. Leaving this on saves several gigabytes.

(`BuildOptions` and `BuildProfiles` are two related Debian knobs that happen to share these names; keeping them in step, as shipped, is the safe default.)

Two more settings worth knowing:

- **`Tunneled`** — a list of packages that are taken **directly from Debian as finished binaries** instead of being rebuilt from source. This is for things that shouldn't be rebuilt — CPU microcode and cryptographically-signed boot components — where Debian's official signed binary is exactly what you want.
- **`HeavyPackages`** — a list of notoriously large, memory-hungry packages (Firefox, GCC, LibreOffice, the Linux kernel …). Athena builds these **one at a time, alone**, so a normal machine isn't asked to compile two giants at once and run out of memory. Add a package here if it keeps exhausting your RAM.

There's also `IncludeBuildClosure` (off by default) — turning it on additionally builds the *build tools themselves* from source. It's powerful but roughly multiplies the work several-fold; leave it off until you specifically need it.

---

## The package lists

Your actual package selection lives in plain-text files in `config/`, one package name per line. Lines are grouped into named **groups** by blank lines / group headers, and those group names are what you reference in `build.conf`'s `[Live]` and `[Disk]` sections.

| File | Holds |
|---|---|
| `pkg.list` | The master selection, organised into groups (`base`, `gnome-desktop`, …). This is the seed Athena expands into the full closure. |
| `live.list` | Extra packages only the live ISO needs (the bits that make a USB stick boot as a live system). |
| `installer.list` | Packages used by the installer ISO. |
| `pool.list` | Packages you want *available in your repo* for users to `apt install` later, without shipping them on the image itself. |

So: **to add software to your distro, add it to a group in `pkg.list`** (and make sure that group is listed under `[Live]` or `[Disk]` in `build.conf`). To make something installable-later-but-not-preinstalled, put it in `pool.list`.

---

## `config/local.conf` — leave it to the tool

Some settings are about *your specific build machine*, not your distribution: how many builds to run in parallel, where your publishing mirror lives, which remote build hosts you've registered. These live in `local.conf`, which the tool creates and updates for you (largely through its interactive setup). You normally never edit it by hand, and it's deliberately **not** shared in git — your machine's tuning shouldn't follow the project to someone else's computer.

---

## A note on branding

Changing `DISTRIBUTION` and `CODENAME` in `distro.conf` renames your distro everywhere the *name* appears. Deeper branding — logos, wallpapers, default settings — is carried by a small set of packages under `fork/source/`, where placeholders like `@DISTRIBUTION@` are filled in with your values at build time. Athena will refuse to build if it detects leftover upstream branding (see `[Audit] IdentityScan` above), so a half-finished rebrand fails at build time rather than silently shipping. The full branding walkthrough is in [`branding-methodology.md`](branding-methodology.md).

---

## "I just want to…" — quick recipes

- **Rename my distro** → in `distro.conf`, set `DISTRIBUTION` and `CODENAME`. Run `print config` to confirm.
- **Add a package to the live image** → add its name to the relevant group in `config/pkg.list`; make sure that group is listed under `[Live] Groups` in `build.conf`.
- **Make a package installable later but not preinstalled** → add it to `config/pool.list`.
- **Build a leaner image** → set `IncludeRecommends = false` in `distro.conf`, and/or trim the groups under `[Live] Groups`.
- **Include Wi-Fi / hardware firmware** → it already comes from the `non-free-firmware` mirror; list the specific firmware package you need in `config/pool.list`.
- **Base my distro on a different Debian release** → change `RELEASE` in `distro.conf` (e.g. to `trixie`).
- **Stop a giant package from exhausting memory** → add its source name to `[Source] HeavyPackages` in `build.conf`.
- **Turn off reproducibility for a quick experiment** → set `[Snapshot] Enabled = false` in `distro.conf` (remember to turn it back on).

---

## Where to go next

- [`architecture.md`](architecture.md) — how the build pipeline actually turns this config into images, stage by stage.
- [`branding-methodology.md`](branding-methodology.md) — the full identity / branding system.
- [`mirror-setup.md`](mirror-setup.md) — publishing your repo so others can install and update from it.

Every key in `distro.conf` and `build.conf` also carries an inline comment in the file itself — this guide explains the ones that matter most for getting started.

---

## Appendix — every setting

A complete reference to every key in the two files. The body above explains the *why*; this is the quick lookup. Defaults are what ships in the templates.

### `distro.conf`

| Section | Key | Default | What it does |
|---|---|---|---|
| `[Build]` | `ARCH` | `amd64` | Target CPU architecture. `amd64` is the tested value today. |
| `[Build]` | `DISTRIBUTION` | `Asgard` | Your product name (boot menu, login screen, system identity). |
| `[Build]` | `CODENAME` | `thor` | The name of this release; the label `apt` uses for your repo. |
| `[Build]` | `VERSION` | `1` | Whole-number release number; feeds the version string and update ordering. |
| `[Build]` | `CHANNEL` | `stable` | Free-text release-track label. |
| `[Build]` | `IncludeRecommends` | `true` | Build Debian *Recommends* into your repo (but keep them off the live image). |
| `[Base]` | `BASEURL` | `http://deb.debian.org` | Where to fetch upstream Debian from. |
| `[Base]` | `BASEID` | `debian` | Upstream distribution id. |
| `[Base]` | `RELEASE` | `bookworm` | The Debian release you derive from. Change this to rebase. |
| `[Base]` | `BASEVERSION` | `12.0` | Numeric version of that release. |
| `[Base]` | `CONTAINER_RELEASE` | *(unset)* | Pins the build environment to a different Debian release than the target. Leave unset to keep them in step. |
| `[Mirror.*]` | `Suffix` | *(varies)* | Selects the suite: empty = the release, `-updates`, or `-security`. |
| `[Mirror.*]` | `Component` | *(varies)* | Which Debian area this mirror pulls: `main` / `contrib` / `non-free` / `non-free-firmware`. |
| `[Mirror.*]` | `BASEID` | inherits `[Base]` | Per-mirror override (security uses `debian-security`). |
| `[Security]` | `Keyring` | debian-archive-keyring | Keyring used to verify Debian's signatures. |
| `[Security]` | `Disabled` | `false` | Turns signature verification off. Test sandboxes only. |
| `[Security]` | `AuditBuildDeps` | `false` | Prompt to review each package's build-dependencies before building. For targeted audits only (a full build would prompt 800+ times). |
| `[Audit]` | `IdentityScan` | `true` | Fail the build if leftover upstream branding is detected. |
| `[Snapshot]` | `Enabled` | `true` | Pin all downloads to a frozen, dated Debian for reproducible builds. |
| `[Snapshot]` | `BaseUrl` | snapshot.debian.org/archive | Where snapshots are served from. |
| `[Snapshot]` | `TimestampApi` | snapshot.debian.org/mr/timestamp/ | Endpoint used to resolve a snapshot date. |
| `[Snapshot]` | `ArchiveKeys` | `debian, debian-security` | Which archives a chosen snapshot must cover. |
| `[Directories]` | `Log`, `Cache`, `Temp`, `Source`, `Build`, `Repo`, `LocalMirror`, `Config`, `Patch`, `Fork`, `Image`, `Chroot`, `Gnupg` | folder names | Names of the working folders. Almost always left as-is. |

> The actual snapshot *date* is not in this file — it's chosen on first run and stored in an untracked sidecar, managed with the `snapshot` command.

### `build.conf`

| Section | Key | Default | What it does |
|---|---|---|---|
| `[Live]` | `Groups` | `base, gnome-desktop` | Which `pkg.list` groups compose the live ISO / default system. |
| `[Disk]` | `Groups` | `base` | Which groups compose the minimal disk image. |
| `[Packages]` | `Pkg_List` | `pkg.list` | The master package-selection file. |
| `[Packages]` | `Pool_List` | `pool.list` | Packages available in your repo but not preinstalled. |
| `[Packages]` | `Live_List` | `live.list` | Extra packages the live ISO needs. |
| `[Packages]` | `Installer_List` | `installer.list` | Packages used by the installer ISO. |
| `[Packages]` | `Build_Pkg_List` | `build_pkg.list` | Package list used in build-mode (building a subset for a team). |
| `[Source]` | `SkipTest` | *(empty)* | Source packages whose test suites to skip individually. |
| `[Source]` | `IncludeBuildClosure` | `false` | Also build the build-*tools* from source. Powerful but multiplies the work. |
| `[Source]` | `BuildOptions` | `nodoc, nocheck, noautodbgsym` | How packages are compiled: skip docs, skip tests, skip debug-symbol packages. |
| `[Source]` | `BuildProfiles` | `nodoc, nocheck, noinsttest` | The dependency-side counterpart to `BuildOptions`; keep the two in step. |
| `[Source]` | `Tunneled` | microcode + signed-boot pkgs | Packages taken from Debian as finished binaries instead of rebuilt. |
| `[Source]` | `HeavyPackages` | firefox-esr, gcc-12, … | Packages built one-at-a-time to avoid running out of memory. |
| `[Source.<pkg>]` | `BuildOptions` / `BuildProfiles` | *(falls back to global)* | Per-package overrides of the two settings above. |

> Machine-specific build tuning (how many builds run at once, memory caps, mirror and remote-host endpoints) lives in the auto-managed `local.conf`, not here.
