# Plan — COMP-02: Robust, Debian-compliant build process

## Status: PLANNED (2026-05-12) — supersedes the ad-hoc Python-helper workarounds added during COMP-01 install-debugging

## Context

COMP-01b's installer ISO now boots and completes an install end-to-end (verified
2026-05-12 on VMware BIOS-mode VM, full log at `log/athena.log`). Getting there
required ~5 hours of incremental debugging that produced a series of Python
helpers patching unpacked udeb contents in the chroot. Those helpers work but
violate the Debian build model: customizations live in engine code, not in
versioned source packages, so upstream udeb churn breaks us silently.

This plan migrates every COMP-01b workaround to the Debian-conventional shape
(custom source packages built through our existing source-build pipeline +
debian/patches/ entries on forked upstream sources) and fixes the latent
issues in the installed system surfaced by the working-install log.

## What is currently cranky

### In the installed system

| Issue | Log line(s) | Severity | Symptom for the user |
|---|---|---|---|
| `apt-cdrom-setup` chain fails end-to-end | 1473, 1678, 1682 (`40cdrom output did not verify`) | **HIGH** | Installed system has empty/broken `/etc/apt/sources.list`. `apt-get install` returns "Unable to locate package" for anything not already on disk. |
| `91security` generator returns error code 1 | 1683 | Low | Discarded silently. Cosmetic for us (no internet model). |
| `eject` binary missing on target | 1927 (`/usr/lib/finish-install.d/15cdrom-detect: line 21: eject: not found`) | Low | CD doesn't auto-eject at end of install. |
| `xserver-xorg-core` / `task-desktop` not found | 1889, 1890 | None | Expected — we don't ship X. |

### In the installer runtime (cosmetic)

| Issue | Log line(s) | Why it happens |
|---|---|---|
| `depmod: WARNING: could not open modules.builtin.modinfo` | 145 | `kernel-image-di` udeb doesn't include the modinfo file. Modules still load. |
| `mount: mounting none on /sys/firmware/efi/efivars failed` | 146 | BIOS-mode VM — expected on non-EFI hardware. |
| `cat: can't open '/etc/default-release'` | 117 | Stock d-i quirk; some script expects a Debian release identifier file. |
| `Falling back to the package description for ext4-modules-...-di` (×4) | 133-136 | Module-udeb `Description:` fields aren't loaded into cdebconf. |
| `dpkg-divert: warning: ... use --no-rename` (~20+) | many | Stock `chroot-setup.sh` uses `--rename` for `start-stop-daemon` divert. Spams logs. |
| `/target/etc/mtab won't be updated since it is a symlink` (~30+) | many | Stock d-i still tries the legacy mtab write; modern Debian makes mtab a `/proc/self/mounts` symlink. Cosmetic. |
| `cat: can't open '/tmp/apt-setup.components'` | 1685 | Downstream of `40cdrom` failure — `apt-setup` couldn't write its components list. |

### In the build process

Every entry in this table is a workaround that mutates unpacked udeb content at
chroot-build time, instead of being a Debian source package shipped as a udeb.
Each is fragile to upstream churn.

| Helper / file | What it does | Replaces |
|---|---|---|
| `scripts/installer_chroot.py:_create_runtime_dirs` | sudo-mkdir `/tmp`, `/var/tmp`, `/root` in unpacked chroot. mode 1777/1777/0700. | Should be ROOT-OWNED dirs shipped by an `athena-rootskel-extras` udeb. |
| `scripts/installer_chroot.py:_install_debootstrap_codename_script` | sudo-cp `sid` → `<codename>` inside the chroot's `/usr/share/debootstrap/scripts/`. | Should be a forked `debootstrap-udeb` source package that ships our codename, OR an `athena-debootstrap-codenames` udeb that drops files into `debootstrap/scripts/`. |
| `installer/templates/athena-stubs.templates` (overlay) | Declares `mirror/protocol` so unguarded `db_get mirror/protocol` in bootstrap-base.postinst doesn't trip `set -e`. | Should be a `templates` file inside an `athena-installer-stubs-udeb` source package. |
| `installer/preseed/load-preseed.sh` (overlay → `S25-load-preseed`) | Runs `debconf-set-selections /preseed.cfg` because stock d-i has no auto-loader for `/preseed.cfg` in the initrd. | Could be replaced by booting with kernel cmdline `auto=true file=/preseed.cfg` (stock d-i mechanism). Or ship as a tiny udeb. |
| `config/pkg.list: grub-pc` | Pre-installs `grub-pc` so `grub-installer`'s `apt-install grub-pc` succeeds (as "already newest version") even though apt sources are broken. | Symptom-level workaround. Real fix is to fix apt-cdrom-setup; once that works, ship `grub-pc-bin` + `grub-efi-amd64-bin` and let `grub-installer` pick the meta-package at install time. |

### Items that are NOT cranky (kept as-is)

- `scripts/iso_installer.py:_stage_base_include` — writes `.disk/base_include`. This is the d-i convention used as intended.
- `scripts/iso_installer.py:_write_subdir_release` and `_generate_top_release` — apt-ftparchive based, standard.
- `scripts/build.py` autorun chain extensions — orchestration, not customization.
- `installer/boot/grub.cfg` debug-mode kernel cmdline — known dev convenience, will get a `quiet` flag for release builds.

## Locked principles

1. **Stop patching unpacked files.** Every Athena customization becomes either:
   (a) an Athena source package under `sources/athena-*/` that builds a `.udeb`
   pulled into the chroot via normal dependency resolution, or
   (b) a `debian/patches/` entry on a forked upstream source we already build.
2. **The installer chroot build does no post-unpack mutation.** Once udebs are
   unpacked into `buildroot/installer/`, the chroot is final. Verification just
   confirms the unpacked set matches the resolved closure.
3. **Every workaround is recorded** in this repo's `debian/changelog` (one
   per Athena source package) with its motivating bug captured in the same
   commit. No silent forks of upstream code.
4. **Upstream version bumps go through the normal Debian patch workflow** —
   when an upstream udeb gets a new release, our patches either apply cleanly
   or fail loud; we update the patches before shipping. No silent breakage.

## Phased work

### Phase A — Capture the current working state (1 day)

- Commit the COMP-01b workaround code that ships a working install today, with
  one `# COMP-02 TODO: replace with <udeb name> (see docs/plans/comp-02-...)`
  comment per helper / overlay file.
- Add `docs/known-issues.md` listing each latent issue from the table above
  with reproduction notes and a link to the phase that addresses it.
- Tag this commit `comp-01b-install-working-pre-cleanup` so the rollback path
  is one `git checkout` if any phase below regresses the install.

### Phase B — Athena udebs replace Python helpers (3-5 days)

1. **`sources/athena-installer-stubs/`** — new source package, builds
   `athena-installer-stubs-udeb`. Contents:
   - `debian/templates`: `mirror/protocol` (string, default `file`) — same content
     as today's `installer/templates/athena-stubs.templates`.
   - `debian/dirs`: `/tmp 1777`, `/var/tmp 1777`, `/root 0700` — these become
     part of the udeb's file list.
   - `Depends: rootskel` so it gets pulled in whenever the installer is built.
   - `Installer-Menu-Item:` omitted — it's a passive provider.
   - Add to `config/installer.list`.
   - Delete `_create_runtime_dirs`, `installer/templates/`, and the related
     tests after CI confirms the new udeb provides them.

2. **`sources/athena-debootstrap-codenames/`** — new source package, builds
   `athena-debootstrap-codenames-udeb`. Contents:
   - `debian/install`: `scripts/* /usr/share/debootstrap/scripts/`
   - Single file `scripts/<codename>` that's a copy of `scripts/sid` (or a
     symlink to it, depending on what debootstrap accepts).
   - The codename is read from `debian/changelog` at build time so a future
     codename change is one source-package version bump, not an engine-code
     edit.
   - `Depends: debootstrap-udeb`.
   - Add to `config/installer.list`.
   - Delete `_install_debootstrap_codename_script` + tests.

3. **`sources/athena-preseed-loader/`** — *optional*. Consider replacing
   first with the stock d-i kernel-cmdline mechanism (`auto=true
   file=/preseed.cfg` in grub.cfg). If that works, we drop the
   `S25-load-preseed` overlay entirely and don't need a new udeb.
   - Verify by booting with the cmdline change and checking `/preseed.cfg`
     values land in cdebconf (`db_get base-installer/includes` shows the
     value, etc.). If verified, delete `installer/preseed/load-preseed.sh`
     + overlay map entry + tests.

4. **Drop overlay-map entries for the replaced items**, leaving the overlay
   mechanism for genuine data-layer files only (preseed.cfg, cdebconf.conf,
   grub.cfg, .disk/info, debug/syslog-to-serial.sh).

### Phase C — Fix apt-cdrom-setup properly (2-3 days)

1. **Diagnose precisely.** Boot a stuck-state ISO and run a focused capture
   script on tty2 that records every step of `apt-cdrom add` inside the
   target chroot (`Debug::aptcdrom=true`, `Debug::Acquire::cdrom=true`,
   strace of `mount` syscalls, lsof of /dev/sr0). Identify the exact failing
   check — most likely candidates ranked by prior evidence:
   - apt-cdrom uses `cdrom://` as the source-list scheme; apt's cdrom acquire
     method requires the disc fingerprint to be in `/target/var/lib/apt/cdroms.list`
     AND the disc reachable at `Acquire::cdrom::mount` path. We've seen apt-cdrom
     fail to write that fingerprint cleanly.
   - Possible: `00CDMountPoint` says `/media/cdrom` but apt-cdrom internally
     defaults to `/cdrom` for some operations — a config-vs-default mismatch.
   - Possible: kernel-level `EBUSY` on `/dev/sr0` because the device is locked
     by the host mount and apt-cdrom inside chroot tries an exclusive mount.

2. **Choose remediation**:
   - **C1: Patch apt-cdrom-setup's `40cdrom` via debian/patches/.** Fork
     `sources/apt-cdrom-setup/` (we already build it through our pipeline),
     add a quilt patch series under `debian/patches/series` containing
     `0001-athena-skip-mount-teardown.patch` and
     `0002-athena-pass-d-flag.patch` (the two changes we tried via sed but
     reverted). Bump `debian/changelog`. Rebuild the udeb. The
     `_patch_apt_setup_40cdrom` Python helper goes away — the patched udeb
     IS the fix.
   - **C2: Fork as `athena-apt-cdrom-setup`.** If the upstream design is
     fundamentally incompatible with our cdrom-only model, write our own
     replacement udeb that ships an `/usr/lib/apt-setup/generators/40cdrom`
     bypassing the apt-cdrom registration dance entirely and writing
     `deb [trusted=yes] file:///cdrom <codename> main`. Same chassis as
     stock apt-cdrom-setup, different implementation.

3. **Verify the installed system has a working `/etc/apt/sources.list`.**
   After Phase C, the post-install user can run `apt-get update` and
   `apt-get install <pkg>` successfully (against the disc, until they edit
   sources.list themselves).

4. **Drop the `grub-pc` workaround in `pkg.list`** — once apt-cdrom-setup
   works, grub-installer's `apt-install grub-pc` succeeds normally. Replace
   `grub-pc` line with `grub-pc-bin` + `grub-efi-amd64-bin` (binaries don't
   conflict) and let grub-installer pick at install time.

### Phase D — EFI support (2 days)

- Once Phase C ships, grub-installer can install either `grub-pc` or
  `grub-efi-amd64` from the disc. Verify against an EFI-mode VMware VM
  (enable Firmware: UEFI in VM settings).
- Confirm `/sys/firmware/efi/efivars` available, `efibootmgr` runs, target
  boots into Athena via EFI.
- Document any extra udeb seeds needed (probably none — `partman-efi` and
  `grub-installer` are already in `installer.list`).

### Phase E — Cosmetic / latent cleanup (1 day)

| Issue | Fix |
|---|---|
| `eject: not found` at finish-install | Add `eject` to `pkg.list` (small, target-only; <100K). |
| `dpkg-divert: --no-rename` warnings | Patch `chroot-setup.sh` in our fork of d-i to use `--no-rename` for `start-stop-daemon`. One-line debian/patches/. |
| mtab symlink warnings | Either suppress in `in-target` wrapper (patch via debian/patches/) or ensure `/target/etc/mtab` is a regular file via base_include. |
| `modules.builtin.modinfo` missing | Likely upstream bug in `kernel-image-X-di`. Investigate; file upstream bug if real, else ignore (modules load fine). |
| `ext4-modules-*-di` description fallback | Cosmetic. Investigate the templates file shape in those udebs. |
| `/etc/default-release` missing in installer | Ship a `/etc/default-release` file (single line `thor`) in `athena-installer-stubs-udeb`. |

### Phase F — CI gate (1-2 days)

A test harness in `tests/installer-smoke/` that:

1. Pulls the latest built installer ISO from `image/`.
2. Boots it in QEMU headless (`-nographic` with serial-captured log).
3. Drives partman + base-install via preseed.
4. Captures the full installer syslog over the run.
5. Parses for **known-bad strings** and fails CI if any new ones appear:
   - `succeeded but requested to be left unconfigured`
   - `failed with error code`
   - `unable to find required udeb`
   - any new `WARNING **:` from main-menu
6. Captures the boot of the installed target system (one cycle) and confirms
   it reaches a login prompt.

This catches upstream-induced regressions at PR time, not after release.

## Sequencing and dependencies

```
Phase A ──┬── Phase B (independent — Athena udebs)
          │
          └── Phase C (apt-cdrom-setup fix)
                │
                ├── Phase D (EFI — needs working apt sources)
                │
                └── Phase E (cosmetic — independent of C/D)

Phase F ──── parallel; runs against every phase's output
```

A and B can be done in parallel. C is the technical risk — proper diagnosis
time matters. D depends on C (EFI needs working apt). E and F are
independent and stackable.

## What this gives us

- **Upstream changes don't silently break us.** Our customizations live in
  Athena source packages with explicit version bumps. `apt-cdrom-setup`
  shipping a new upstream version either applies our patches cleanly (CI
  passes, we ship) or the patches fail to apply (CI fails, we update them
  before shipping). No more "rebuild and find out" cycles.

- **Audit trail.** Every Athena-ism is a Debian source package with a
  `debian/changelog`. `dpkg -l | grep athena-` on the installer (or on the
  installed system) shows exactly which Athena udebs ran and at which version.

- **Testability.** Each udeb has its own `debian/tests/` autopkgtest covering
  its specific contract. CI runs the tests before the udeb gets into a
  build.

- **No more incremental-patch debugging sessions.** When something breaks,
  we find the responsible udeb, look at its tests, look at its postinst.
  Same workflow Debian itself uses.

## Non-goals for COMP-02

- **Signing apt repo metadata** — that's CONF-02 phase 2.
- **Reproducible builds** — separate component, doesn't block this work.
- **Mirror selection / network install** — out of scope, locked
  cdrom-only model per `project_self_contained_repo.md`.
- **A custom-themed installer UI** — covered by COMP-01's branding plan.
