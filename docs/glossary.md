# Glossary

Plain-English definitions of the terms used across Athena's documentation. You don't need all of these to get started — skim, and come back when a word trips you up. Debian-specific terms are marked *(Debian)*; the rest are Athena's own.

---

**apt / dpkg** *(Debian)* — the two tools that install software on a Debian system. `dpkg` installs one package file; `apt` sits on top of it, fetching packages from a repository and sorting out what else they need. Your finished distribution uses both, and so does the build.

**Binary package** *(Debian)* — a finished, installable software file (`.deb`). It contains the compiled program plus instructions for installing it. This is what ends up on a user's machine.

**Build option / build profile** *(Debian)* — switches that change how a package is compiled: for example "skip the documentation" or "skip the test suite". Athena sets sensible defaults (see `BuildOptions` / `BuildProfiles` in [`config.md`](config.md)).

**Chroot** — a complete miniature Linux filesystem assembled inside a folder on your build machine. Athena installs your chosen packages into a chroot, and then that tree is packaged into a bootable image. Think of it as the distro-in-progress before it's sealed into an ISO.

**Closure (dependency closure)** — the *complete* set of packages your selection pulls in once you follow every dependency, and their dependencies, all the way down. You list a handful; the closure is the hundreds they actually require.

**Codename** — the name of one specific release of a distribution (Debian's "bookworm", Athena's "thor"). It changes each release, unlike the product name.

**Component** *(Debian)* — Debian splits its software into areas by licensing: **main** (fully free, the default), **contrib** and **non-free** (software with non-free parts), and **non-free-firmware** (hardware firmware blobs, e.g. for Wi-Fi). You choose which to draw from in [`config.md`](config.md).

**Dependency** *(Debian)* — a package another package needs. Debian distinguishes hard dependencies (**Depends** — required) from softer ones (**Recommends** — suggested companions that are usually but not strictly wanted).

**Derivative** — a distribution built *on top of* another. Athena builds Debian derivatives: your distro starts from Debian and diverges with your packages, patches, and branding.

**Disk image** — a ready-to-run virtual-machine disk (a `.qcow2` file) with your system already installed on it. Boots straight into the OS — no installer step. One of Athena's three output *surfaces*.

**Federation** — Athena's model for letting several people publish to a shared repository safely. Each builder signs what they publish, and updates are append-only, so multiple contributors can extend one mirror without overwriting each other. (See [`mirror-setup.md`](mirror-setup.md).)

**Fork** — a package you've taken over and modified, kept under `fork/source/`. Most forks exist to swap Debian's branding for yours; Athena builds these from your local copy instead of fetching Debian's.

**Group** — a named bundle of packages you define in `pkg.list` (e.g. `base`, `gnome-desktop`). You compose each output image by choosing which groups it includes, rather than listing packages one by one.

**Heavy package** — a package that's notoriously large and memory-hungry to build (Firefox, GCC, LibreOffice, the Linux kernel …). Athena builds these one at a time to avoid exhausting your machine's memory.

**Installer ISO** — a bootable image that *installs* your distribution onto a machine's disk, the way the Debian or Ubuntu installer does. One of the three *surfaces*.

**Live ISO** — a bootable image that runs your distribution directly from a USB stick or DVD without installing anything. One of the three *surfaces*.

**Mirror** — a server that hosts a distribution's packages. Athena downloads Debian's *source code* from mirrors to build it; "publishing to a mirror" means putting *your* finished packages on a server for others to install from.

**Package** — one unit of installable software. The general word; in Debian it takes the concrete form of a *binary package* (`.deb`) built from a *source package*.

**Repository (repo)** — a folder of packages plus a signed index that `apt` reads. Building produces your repo; your users install and update from it.

**Snapshot** — a frozen, dated copy of Debian (served from `snapshot.debian.org`). Building against a snapshot means a rebuild months later uses identical inputs, instead of whatever Debian is serving that day. This is what makes Athena builds **reproducible**.

**Source package** *(Debian)* — the source code and build instructions a binary package is built from. Athena's defining trait is that it builds *everything* from source packages rather than reusing Debian's prebuilt binaries.

**Surface** — one of the three things Athena can produce from your configuration: a **live ISO**, an **installer ISO**, or a **disk image**. Each is built from the groups you assign to it.

**Tunneled package** — a package taken *directly from Debian as a finished binary* instead of being rebuilt from source. Used for things that shouldn't be rebuilt — CPU microcode and cryptographically-signed boot components — where Debian's official signed file is exactly what you want.

**udeb** *(Debian)* — a stripped-down package used only *inside the installer* (the "u" is for micro). They make up the installer environment and never get installed on the final system.

**Verification / signing** *(Debian)* — packages and repository indexes are cryptographically signed so a machine can confirm they're genuine and untampered. Athena verifies Debian's signatures when downloading, and signs your repo so your users can verify it in turn.
