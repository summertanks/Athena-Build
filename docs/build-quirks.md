# Build-machine quirks & gotchas

A living catalogue of Debian packaging / toolchain behaviours that have
actually bitten this project.  Each entry: the quirk, the incident where
we hit it, and the rule we follow now.  **Add an entry whenever a build
failure or drift investigation uncovers a new one** — the incident date
and package name are the valuable part; they make the entry findable
from a failing log years later.

Most entries trace to a longer post-mortem in `docs/done.md`, a patch
under `patch/source/`, or a `.buildlog` delta from the thor1 rebuild.

---

## 1. apt & dependency resolution

### 1.1 A real package name is never substitutable by a Provides alias
`Provides:` claims dependency-graph substitutability, **not** file-level
equivalence.  apt itself installs the real package when one exists and
consults providers only for purely-virtual names.
**Incident (2026-06-07, gstreamer1.0):** our build-dep expansion rewrote
the real name `libunwind-dev` into a provider OR-chain with LLVM's
`libunwind-14-dev` first (alphabetical) and the real package last.  LLVM's
libunwind ships no `libunwind.pc`; gstreamer configures with
`-Dauto_features=enabled` → `meson: Dependency "libunwind" not found`,
exit 2, deterministic across retries.
**Rule:** expand a dep name to provider alternatives **only when no real
package exists under that name** (`_expand_virtual_alternatives`,
`scripts/package.py`).
**Second victim, now resolved (xorg-server):** the same mis-pick disabled
libunwind in xserver — but *silently*, because xorg's configure treats
libunwind as optional (LLVM's `libunwind-14-dev` shipped `libunwind-14.pc`
not `libunwind.pc`, the pkg-config probe failed, the feature auto-disabled).
A workaround patch (`9001-rules-disable-libunwind.patch`) forced
`--disable-libunwind` to unblock the build.  Once the resolver fix above
landed (real `libunwind-dev 1.6.2-3` resolved verbatim — it *does* ship
`libunwind.pc`), the workaround became obsolete and was removed; the loss
showed up as STA-24 dep-loss drift (xserver-xorg-core/xephyr → libunwind8).
Re-enabling restores upstream parity (native crash backtraces in
`Xorg.0.log`).  **Needs an xorg-server rebuild to take effect.**

### 1.2 Multi-provider purely-virtual names can't be apt-installed non-interactively
`apt-get install libcurl4-dev` fails with "has no installation candidate"
when the name is purely virtual with ≥2 providers — apt refuses to pick
one from the CLI.  (Single-provider virtuals install fine: apt
auto-selects with a "selecting X instead of Y" note.)
**Why it matters:** this is the *only* reason the build-dep expansion in
1.1 exists at all.
**Rule:** expansion is the workaround for exactly this case and nothing
else; chain shape `providerA | providerB | … | virtual-name`.

### 1.3 An unversioned Provides cannot satisfy a versioned Depends
dpkg treats `Provides: tasksel` (no version) as version `<none>`; a
downstream `Depends: tasksel (= 3.73)` is unsatisfiable.
**Incident:** athena-tasksel's unversioned Provides broke consumers that
pin the upstream version.
**Rule:** fork packages always use `Provides: <pkg> (= <upstream-version>)`;
audit upstream's reverse-deps for version constraints before shipping any
Provides-based fork.

### 1.4 Provides overlap between sibling binaries silently drops the real provider
**Incident:** `athena-cdrom-setup` wrongly carried `Provides:
apt-mirror-setup` (its sibling's upstream name) — apt considered the dep
satisfied, the real mirror-setup payload never installed, and the
installer's `50mirror` step vanished without an error anywhere.
**Rule:** a fork binary's Provides names ONLY the upstream package it
itself ships/replaces — never a sibling's.

### 1.5 apt prefers the real upstream package over a fork's virtual Provides
With both `tasksel` (real, upstream) and `athena-tasksel`
(`Provides: tasksel`) visible, a bare `Depends: tasksel` resolves to
upstream — the fork is silently bypassed.
**Rule:** inter-binary Depends inside a fork name the fork binary
explicitly; never rely on the virtual name to route to the fork.

---

## 2. Build OPTIONS vs build PROFILES

### 2.1 `DEB_BUILD_OPTIONS` and `DEB_BUILD_PROFILES` are different namespaces doing different jobs
The same token (e.g. `nodoc`) means two different things.  **Option** =
build-time behaviour: doc helpers (`dh_installdocs`, `dh_installman`,
`dh_doxygen`, …) skip *staging content*; the binary package is still
emitted.  **Profile** = emission set: `dh_listpackages` *drops* binaries
whose control stanza carries `Build-Profiles: <!nodoc>` — never built at
all.
**Corpus numbers (thor1):** 69 `-doc` packages carry the annotation
(profile-dropped); **206 do not** and are emitted as near-empty packages
under the option.
**Rule:** set `nodoc` in BOTH (as we do).  Option-only risks empty
packages plus the failures in 2.2; profile-only would need doc tooling
in the build pool for the 206.

### 2.2 `nodoc` as option hard-fails packages whose rules reference doc files
If `debian/rules` / `debian/<pkg>.install` names a doc artifact that the
nodoc option prevented from being generated, `dh_install` dies with
"cannot find file".
**Incidents:** the whole `patch/source/*nodoc*` family — libyaml
(doxygen), protobuf (examples install), p7zip (absent manual), wpa
(examples sed), freetype/perl/bash/libzstd/krb5/tar/librsvg
(installdocs/installman/changelog-mv guards).
**Rule:** per-package version-pinned tolerance patch; never a
config-wide change.  Lifting `nodoc` entirely is CONF-08.

### 2.3 `nocheck` drops no packages; `noinsttest` is what drops the test debs
`nocheck` (option) skips *running* the test suite; `nocheck` (profile)
prunes test-only Build-Depends.  Zero binaries in the corpus are gated
`profile=!nocheck`.  The "installed-tests" debs (`dbus-tests`,
`libglib2.0-tests`, `gtk-4-tests`, `systemd-tests`, … 16 across 13
sources) are gated `profile=!noinsttest`.
**Rule:** don't expect `nocheck` to shrink the package set; don't drop
`noinsttest` expecting only test *runs* to change.

### 2.4 Restriction groups are OR'd inside a group
`<!nocheck> <!noinsttest>` keeps a build-dep active unless **both**
profiles are set — one group satisfied = dep stays.
**Incident:** misread annotation nearly led to patching a dep that the
profile set already handled.
**Rule:** read the full formula before concluding a dep/binary is
filtered.

### 2.5 dbgsym packages are auto-generated and never declared
`-dbgsym` debs come from `dh_strip`, appear in no `Package-List`, and
`noautodbgsym` (option) suppresses them entirely.
**Incident:** old libzstd dpkg logs showed "6 built" vs 4 declared —
the phantom 2 were dbgsym from a pre-`noautodbgsym` era; their later
absence read as "pruned" until traced.
**Rule:** any built-vs-declared accounting must treat dbgsym as
out-of-band; with `noautodbgsym` they simply don't exist.

---

## 3. debhelper & debian/rules

### 3.1 dh helper files are keyed by BINARY name — silently ignored on mismatch
`debian/<binary>.install`, `.templates`, `.dirs`, `.isinstallable`, …
must match the binary package name exactly.  Renaming a fork's binary
without renaming these produces an **empty package with no error**.
**Incident:** athena fork renames; caught only by inspecting the
produced .deb contents.
**Rule:** when renaming a fork binary, `git mv` every
`debian/<oldname>.*` in the same change.

### 3.2 Parallel-make races in old-compat rules
Packages on debhelper compat < 10 with hand-rolled parallel targets can
race on `debian/.debhelper/generated/<pkg>/` bookkeeping.
**Incident (2026-06-06, console-setup 1.221, compat 9):** `dh_install:
error: open …installed-by-dh_install: No such file` in
`deb-install-freebsd` under `make -j`; **passed unchanged on retry**
after the power-failure restart re-queued it.
**Rule:** a one-off dh_install/bookkeeping failure in an old-compat
package is retry-first; patch (`.NOTPARALLEL`) only if it reproduces.

### 3.3 `arch=any` in Package-List does not guarantee emission
`debian/rules` may procedurally skip a declared binary based on target
arch or build configuration — the static declaration can't tell you.
**Incidents (2026-06-07, gcc-12):** `libhwasan0` (`arch=any`, but
hwasan is AArch64-only — rules skip it on amd64); the whole gccgo/libgo
family dropped as a side effect of our own
`9002-fix-disable-bootstrap.patch` (language-subset selection is coupled
to bootstrap config).
**Rule:** the `.buildlog` DELTA section is the ground truth for what a
source actually emits; declared-set reasoning is only an upper bound.

### 3.4 dpkg-shlibdeps emits NO dependency for libs it resolves via a private `-l` dir
A library found only through `dpkg-shlibdeps -l<dir>` (a package's own
in-tree build dir) produces **no** `shlibs:Depends` entry — silently, by
design; the `-L shlibs.local` mapping is what turns those lookups into
real dependencies.
**Incident (2026-06-11, e2fsprogs):** our
`9001-fix-shlibdeps-symbolfile-crash.patch` dropped upstream's
`-- -L debian/e2fsprogs.shlibs.local` args (to dodge the dpkg-dev
1.21.x SymbolFile.pm crash) and with them the sibling-lib deps:
`e2fsprogs`/`fuse2fs`/`e2fsprogs-udeb` shipped **missing**
`libext2fs2`/`libcom-err2`/`libss2`.  The SURFACES-01 disk closure
faithfully followed the broken metadata (the legacy ship-everything
pool had masked it for weeks) → root fsck exec-failed at boot → reboot
loop (see 9.1).  Fixed by injecting the deps into the `.substvars`
files inside the same patch; rebuilt as `+asg1u2` (commit `00d45f9`).
**Rule:** any patch that drops a `-L shlibs.local` mapping must
re-inject the equivalent deps via substvars, mirroring upstream's
resolved constraints; dpkg-shlibdeps' silence here is documented
behaviour, not an error you'll see in a log.

**Workaround still required (checked 2026-06-13):** the underlying
SymbolFile.pm crash is *fixed in dpkg-dev 1.22.x* (per the patch
header), but our build base is **bookworm** with `dpkg-dev 1.21.23`
(still the broken 1.21 series), so the patch — drop `-L shlibs.local`
+ inject the deps — remains load-bearing.  It becomes removable (revert
to the plain `-- -L debian/e2fsprogs.shlibs.local` form, which provides
the deps automatically) once the base moves to **trixie** (dpkg-dev
1.22.x) under COMP-07/COMP-11, or the container's dpkg-dev is upgraded
to ≥ 1.22.  **STA-24** now WARNs at dep-drift time on exactly this
class — a built `.deb` that lost a `Depends` edge on a selected package
— so a future recurrence is loud instead of a silent boot loop.

---

## 4. Architecture & version semantics

### 4.1 `amd64` means `linux-amd64`; kernel-cpu pairs are concrete arches
Debian arch strings are `[kernel-]cpu` with `any` as the only wildcard
component.  `kfreebsd-amd64` and `hurd-amd64` are **foreign arches**, not
flavours of amd64; `any-amd64` is the wildcard that matches us.
**Incident (2026-06-07, glibc):** virtual-build's arch gate used
`endswith('-amd64')` and predicted the six `libc0.1*` kFreeBSD packages;
the real build correctly never emits them.  95 such terms corpus-wide
(gcc-12, binutils, boost1.74, grub-installer, …).
**Rule:** match component-wise — kernel ∈ {any, linux}, cpu ∈ {any,
amd64} — never by string suffix.

### 4.2 Epochs exist in versions but never in filenames
`attr` is version `1:2.5.1-4`; the artifact is
`attr_2.5.1-4_amd64.deb`.  The epoch lives only in metadata
(control, Packages, constraints).
**Rule:** filename↔version conversions must strip/restore the epoch
explicitly; comparing a filename version to a control version without
normalising is a latent bug.

### 4.3 Versions don't sort lexicographically
`6.10` < `6.9` as strings.
**Incident:** `sorted(vmlinuz_list)[-1]` picked the wrong kernel image
for the ISO.
**Rule:** always version-aware compare (`apt_pkg.version_compare` /
dpkg semantics); pair initrd to kernel by exact suffix, never by
parallel sort order.

### 4.4 Mirror filenames carry binNMU suffixes that source builds never produce
A buildd upload is `foo_1.2-3+b2_amd64.deb` on the mirror while
`dpkg-buildpackage` from source yields `foo_1.2-3_amd64.deb`.
**Incident:** tunnel downloads 404'd when we "corrected" the upstream
`Filename:`; conversely, on-disk artifacts must be NMU-stripped to stay
pristine.
**Rule:** download by the upstream `Filename:` verbatim; normalise
(strip-NMU → optional `+asg` stamp) only after the bytes are local.

---

## 5. dpkg filesystem level

### 5.1 Two packages shipping the same path is unpack-order russian roulette
Without a declared relationship, the second unpack needs
`--force-overwrite` and dpkg's file ownership records whoever unpacked
last.
**Rule:** to claim a path owned by upstream package X, use
Provides/Conflicts/Replaces, `dpkg-divert`, or a same-name fork — never
ship-the-file-and-hope.  Audit any new `data/` file in a fork for
collisions before shipping.

### 5.2 `dpkg-divert --rename` vs `--no-rename` is load-bearing in d-i
**Incident (commit 34905d9, reverted):** switching d-i's
`chroot-setup.sh` divert to `--no-rename` destroyed
`start-stop-daemon` in the target and bricked `grub-installer`.
**Rule:** the divert/undivert pair in chroot-setup stays `--rename`;
treat any "modernisation" of that flag as a regression.

---

## 6. Repo metadata generation

### 6.1 apt-ftparchive will hash its own partially-written output
Streaming `apt-ftparchive release dists/<suite>` straight into
`dists/<suite>/Release` makes the tool scan the file it is producing —
stale/self-referential hashes → `InRelease` SHA256 mismatch at apt
update.
**Rule:** generate to a temp file **outside** `dists/<suite>/`, then
`mv` into place (`_generate_top_release`).

---

## 7. Build container & toolchain

### 7.1 Snapshot-pinned apt can't always bootstrap itself — SUPERSEDED
Installing the toolchain layer directly from the snapshot failed
"Unable to locate package" for ~15 packages.
**Incident (CONF-15):** Dockerfile had to install from the live mirror
first, then rewrite sources to the snapshot and `dist-upgrade` to
realign.
**Superseded (2026-07):** the base is now our own
`mmdebstrap --variant=buildd` bootstrap from the pinned snapshot
(`scripts/base_rootfs.py`; `config/Dockerfile` is `FROM scratch` +
`ADD base-rootfs.tar`), so the image is at snapshot from birth and the
install-then-realign dance is gone.  The root cause of the "unable to
locate" wall was apt's exit-0-with-warnings default on a failed index
fetch — see 7.1a.

### 7.1a The buildd base has no CA store, and apt update "succeeds" without lists
Two traps in the mmdebstrap base, both hit on 2026-07-08:
- The buildd variant ships **no `ca-certificates`** — an https snapshot
  URL fails every index fetch until the bootstrap includes it
  (`--include=ca-certificates` in `base_rootfs.py`; Docker Hub's slim
  base lacked it too, which is why CONF-15's live-mirror step was plain
  http).
- `apt-get update` **exits 0 with warnings** on a failed fetch, leaving
  empty package lists and a wall of "Unable to locate package" at
  install time (also the historic CONF-15 anomaly).
**Rule:** every scripted `apt-get update` in the container layers runs
with `-o APT::Update::Error-Mode=any` so a failed fetch fails the
build at the update, not later at the install.

### 7.2 debconf must be silenced before the first dpkg call, not at it
`DEBIAN_FRONTEND=noninteractive` alone is not enough in a fresh chroot.
**Rule:** also set `DEBCONF_NONINTERACTIVE_SEEN=true` and write a
minimal `/etc/debconf.conf` into the chroot before the first
`dpkg --configure` (`_init_dpkg_database`), or maintainer scripts hang
on prompts.

### 7.3 Chrootless dpkg configure runs maintainer scripts on the HOST
Until `dpkg` and `sh` exist *inside* the chroot, our bring-up configures
packages chrootless — which means every postinst executes against the
host's filesystem.  A postinst that execs a binary the package itself
ships fails with exit 127 (the binary is in the chroot, not on the
host), leaving the package half-configured.
**Incident (2026-06-11, python3.11-minimal, SURFACES-01 first live
build):** its postinst runs its own shipped `/usr/bin/python3.11` —
absent on the host — so it sat half-configured; `python3-minimal`'s
Pre-Depends then couldn't unpack, the failure cascaded to `python3` and
`gnome-menus`, and only the in-chroot final sweep healed the chain.
**Rule:** Kahn schedules the dependency closure of
`dpkg, dash, coreutils, sed, grep` in the earliest batches
(debootstrap-style essential bootstrap, commit `8a388ab`) so every
later batch configures in-chroot; the post-sweep unpack-retry rounds
(`d41de39`) stay as defence-in-depth, not as the primary mechanism.

### 7.4 `dpkg --get-selections` can NEVER show a half-configured package
`--get-selections` reports the *selection* state (install/hold/
deinstall), not the *configuration* state — a completion gate built on
it prints success unconditionally.  Two adjacent traps in the same
incident: dpkg's `error processing package` lines go to **stderr**, and
the package name is **not** the line's last token (the last token is
`(--configure):`).
**Incident (2026-06-11, SURFACES-01 first live build):** the chroot
build printed "all packages fully configured" while `gnome-menus` was
half-configured and `python3` was never installed; the final-pass
summary parsed stdout for error lines and grabbed the last token, so it
also reported "0 package(s) failed" (commit `9ad420d`).
**Rule:** the authoritative completion gate is `dpkg-query -W
-f '${Package} ${Status}'` diffed against the install plan (broken vs
never-installed classified separately); parse both output streams with
a real regex when extracting failing package names.

---

## 8. d-i / cdebconf surfaces (build-adjacent)

### 8.1 cdebconf silently drops tasks with the "wrong" Section or punctuation
A `debian-tasks.desc` entry with `Section:` ≠ `user`, or commas /
em-dashes / parentheses in `Description:`, is dropped from the tasksel
dialog with no log line anywhere.
**Rule:** mirror upstream's `.desc` shape verbatim — `Section: user`,
plain-ASCII one-line descriptions.

### 8.2 tasksel filters by environment you didn't set
**Incident:** four debugging rounds on "our tasks don't show" ended at
`DEBIAN_TASKS_ONLY=1` inside tasksel — an env filter, not a data
problem.  Sibling rule: a task whose `Key:` names anything that fails
`apt-cache dumpavail` (e.g. a virtual alias) is also silently hidden.
**Rule:** when a consumer "doesn't see" data we ship correctly, read the
consumer's source for env/config filters FIRST; keep `Key:` lists to
real package names (test-enforced against `config/pkg.list` groups).

---

## 9. Boot & runtime surfaces (artifact verification)

### 9.1 fsck exit codes are a bitmask — exec failure (127) reads as "reboot required"
systemd-fsck interprets fsck's exit status bit-by-bit; bit 2 means
"system should be rebooted".  Exit 127 ("command found but could not be
executed", e.g. a missing shared library) has bit 2 set, so a fsck
binary that can't even *start* triggers an immediate, irreversible
reboot — on **every** boot.
**Incident (2026-06-11, disk image, SURFACES-01):** e2fsprogs shipped
missing its `libext2fs2` dep (quirk 3.4) → `fsck.ext4` exec-failed with
127 → systemd-fsck rebooted the machine seconds into every boot, before
any login.  Debug recipe that cracked it: serial console +
`systemd.log_level=debug systemd.log_target=kmsg` on the kernel
cmdline.
**Rule:** a first-boot reboot loop means "check that fsck can EXEC"
before suspecting filesystem corruption; keep the serial+kmsg recipe at
hand — the loop kills the journal before it persists anything.

### 9.2 `nomodeset` kills GNOME — no KMS means no `/dev/dri`
Without kernel modesetting there is no DRM device, so gdm's Wayland
attempt and the Xorg modesetting fallback both fail; the session
restart loop burns out and leaves the VT in a dead graphics state.
**Incident (2026-06-11, first GNOME live boot, SURFACES-01):** the live
grub.cfg still carried `nomodeset` from the console-only era — screen
flashed a few times, no GUI, console switching looked broken too
(commit `53e9225`).  VMs are fine with KMS (virtio-gpu/qxl/vmwgfx/
bochs-simpledrm all provide it).
**Rule:** the default boot entry boots with KMS; `nomodeset` lives only
on an explicit "safe graphics — console only" fallback entry.

### 9.3 systemd-firstboot silently overwrites identity files written earlier
`systemd-firstboot --hostname=X` wins over an `/etc/hostname` written
by an earlier build step — no warning, last writer wins.
**Incident (2026-06-11, disk image):** `generate_system_configs` wrote
the correct `asgard` hostname, then the firstboot call two steps later
carried a hardcoded `--hostname=athena` (toolchain name — a three-layer
identity violation) and overwrote it; caught only on the serial log's
"Hostname set to <athena>" (commit `ebdbc2d`).
**Rule:** every firstboot/identity argument derives from the same
config source as the file writes (never a literal); audit shipped
identity against `project_three_layer_identity` — Athena is the
toolchain, Asgard is what boots.

---

## How to add an entry

One quirk per `###` heading: a one-line statement of the behaviour, the
**Incident** (date, package, symptom — grep-friendly), and the **Rule**.
Link the related memory / done.md ticket / patch path when one exists.
Keep it to what was *observed*, not what is assumed.
