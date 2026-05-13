# Known issues

Live tracker of latent bugs and cosmetic warnings observed in current
builds.  Every entry links to the COMP-02 phase that addresses it.  When
an issue is fixed, move its entry to the bottom under "Fixed" with the
commit hash that closed it.

The reference run is the installer log at `log/athena.log` from a
working install captured 2026-05-13 on VMware BIOS-mode VM (post
Phase B — main-menu boots cleanly, install runs through
`finish-install.d/20final-message`).

---

## Critical — affects installed system

### apt-cdrom-setup chain is broken end-to-end

- **Symptom**: `/target/etc/apt/sources.list` ends up empty or broken on
  the installed system.  Running `apt-get update` post-install reports
  "does not have a Release file".  Running `apt-get install <pkg>`
  reports "Unable to locate package".
- **Evidence in log**:
  - line 1506: `base-installer: Please use apt-cdrom to make this CD-ROM
    recognized by APT. apt-get update cannot be used to add new CD-ROMs`
    (configure_apt's apt-cdrom add inside the chroot fails)
  - line 1509: `E: The repository ... does not have a Release file.`
  - lines 1675-1678: same errors from apt-setup-udeb's verification step
  - line 1679: `apt-setup: warning: /usr/lib/apt-setup/generators/40cdrom
    output did not verify`
  - lines ~1916-1928 (downstream): `Package open-vm-tools is not available`
    and `Unable to locate package intel-microcode` — `hw-detect` /
    `finish-install` apt-installing on the target via the broken sources.
    Same root cause; resolves with phase C.
- **Reproduction**: install Athena from current ISO; on first boot of
  installed target run `apt-get update`.
- **Workaround in place**: `config/pkg.list` pre-installs `grub-pc` via
  `.disk/base_include` so `grub-installer`'s `apt-install grub-pc` step
  succeeds as "already the newest version" rather than failing on broken
  apt sources.  This bypasses the immediate install-time blocker but
  leaves the installed system without working apt sources.
- **Fix**: COMP-02 phase C — proper diagnosis + either patch
  apt-cdrom-setup via `debian/patches/` or fork as
  `athena-apt-cdrom-setup`.  See docs/plans/comp-02-robust-build.md.

### `91security` apt-setup generator returns error code 1

- **Symptom**: discarded silently by apt-setup; no security updates
  configured in `/target/etc/apt/sources.list.d/`.
- **Evidence in log**: line 1682 — `apt-setup: warning:
  /usr/lib/apt-setup/generators/91security returned error code 1;
  discarding output`.
- **Severity**: Low for us — no network mirror model.  But it would
  matter if we add a network mirror later.
- **Fix**: COMP-02 phase E (cosmetic / latent cleanup).  Likely
  related to the broken apt-cdrom-setup chain; may auto-resolve once
  phase C lands.

---

## Latent — would surprise an operator

### Installer ISO is BIOS-only

- **Symptom**: An EFI-mode VM boots the ISO but the *installed* system
  has only `grub-pc` (BIOS bootloader).  An EFI target would have an
  unbootable installed system.
- **Evidence**: `config/pkg.list:grub-pc` (the workaround for the
  apt-cdrom-setup failure pre-installs the BIOS meta-package only).
- **Fix**: COMP-02 phase D — once phase C ships, replace pre-installed
  `grub-pc` with both bin-only packages (`grub-pc-bin` +
  `grub-efi-amd64-bin`) and let `grub-installer` pick at install time.

---

## Build-process — accepted Python helpers (not pending work)

These are Python helpers in `scripts/installer_chroot.py` that perform
actions stock d-i does at image-build time (in its own Makefile) but
that no udeb in our closure ships.  Per the locked Phase B priority
hierarchy (2026-05-12):

> build-pipeline action in installer_chroot.py
>     > stock kernel-cmdline knob
>         > custom Athena udeb
>             > quilt patch on stock source

these helpers are **priority #1** — the preferred solution, not a
workaround to remove.  Listed here so future-us doesn't re-open them
as "tech debt" without re-reading the decision.

### `_create_runtime_dirs`

- **Action**: chmods `/tmp`, `/var/tmp`, `/root` into the unpacked
  chroot — none ships in any udeb in our closure.
- **Why a helper, not a custom udeb**: stock d-i creates these in its
  build/Makefile (line 351), not via udeb postinst.  Mirroring stock
  is the documented goal.

### `_install_debootstrap_codename_script`

- **Action**: copies `/usr/share/debootstrap/scripts/sid` to
  `scripts/<codename>` so debootstrap recognises our derivative
  codename.
- **Why a helper, not a custom udeb**: codename is read from
  `BuildConfig` at build time (see `cmd_build_chroot_installer`).
  Wrapping that in a udeb would mean either (a) hard-coding the
  codename in the udeb's debian/install (fragile) or (b) generating
  the udeb at build time (more moving parts than the helper).

---

## Cosmetic — noisy logs but no functional impact

### `depmod: WARNING: could not open modules.builtin.modinfo`

- **Evidence in log**: line 187.  Note: this is `hw-detect` re-running
  depmod in-target.  Our `_run_depmod` build-pipeline helper ran
  cleanly at chroot-build time (rc=0).  Same warning in both contexts;
  upstream packaging issue rather than ours.
- **Cause**: `kernel-image-6.1.0-47-amd64-di` udeb (built from
  `linux-signed-amd64` source) doesn't include the modinfo file.
  Modules still load — depmod just can't compute reverse-deps for the
  `builtin` modules.
- **Fix**: COMP-02 phase E — investigate upstream; may be a real bug
  in linux-signed-amd64's modules-udeb packaging.

### `mount: mounting none on /sys/firmware/efi/efivars failed`

- **Evidence in log**: line 188.
- **Cause**: BIOS-mode VM — no EFI variables to mount.  Expected on
  non-EFI hardware.
- **Fix**: None needed for BIOS; gets exercised correctly once COMP-02
  phase D adds EFI support.

### `Falling back to the package description for *-udeb` (×many)

- **Evidence in log**: lines 24, 26-32, 53-55, 89, 103-148, 168-175,
  191-193, 1644-1646, 1687-1693 — repeated for `brltty-udeb`,
  `ext4-modules-X-di`, `fat-modules-X-di`, `os-prober-udeb`.  Roughly
  20+ occurrences across the install.
- **Cause**: These udebs ship templates but their `Description:` fields
  aren't getting loaded into cdebconf's runtime DB.  Likely the
  templates files use a description format cdebconf doesn't accept, or
  the udebs aren't marked correctly for template registration.
  brltty-udeb and os-prober-udeb are new since the 2026-05-12 reference
  run (added by stock-cdrom seed completeness in COMP-02 phase B); the
  ext4/fat-modules occurrences are unchanged.
- **Fix**: COMP-02 phase E — investigate per-udeb; likely a single
  fix across all four.

### `dpkg-divert: warning: ... use --no-rename` (~20+ occurrences)

- **Evidence in log**: spread across the install.
- **Cause**: Stock `chroot-setup.sh` uses `--rename` when diverting
  `/sbin/start-stop-daemon`; modern dpkg recommends `--no-rename` for
  diverts of files from Essential packages.
- **Fix**: COMP-02 phase E — single-line `debian/patches/` entry on
  our fork of the d-i source that owns `chroot-setup.sh`.

### `/target/etc/mtab won't be updated since it is a symlink` (~30+)

- **Evidence in log**: spread across in-target invocations.
- **Cause**: Modern Debian symlinks `/etc/mtab` to `/proc/self/mounts`;
  d-i's `in-target` wrapper still tries the legacy file write.
- **Fix**: COMP-02 phase E — either suppress in our `in-target` fork
  or ensure `/target/etc/mtab` is a regular file via base_include
  (probably the former; matches modern Debian).

### `dpkg: warning: trying to overwrite '/sbin/depmod' ... busybox-udeb`

- **Evidence in `chroot build installer` output (2026-05-12)**:
  ```
  dpkg: warning: trying to overwrite '/sbin/depmod', which is also in package busybox-udeb (1:1.35.0-4)
  dpkg: warning: trying to overwrite '/sbin/insmod', which is also in package busybox-udeb (1:1.35.0-4)
  dpkg: warning: trying to overwrite '/sbin/lsmod', which is also in package busybox-udeb (1:1.35.0-4)
  dpkg: warning: trying to overwrite '/sbin/modinfo', which is also in package busybox-udeb (1:1.35.0-4)
  dpkg: warning: trying to overwrite '/sbin/modprobe', which is also in package busybox-udeb (1:1.35.0-4)
  dpkg: warning: trying to overwrite '/sbin/rmmod', which is also in package busybox-udeb (1:1.35.0-4)
  ```
- **Cause**: `kmod-udeb` ships real `/sbin/depmod` / `insmod` / `lsmod` /
  `modinfo` / `modprobe` / `rmmod` binaries; `busybox-udeb` ships the
  same names as multicall stubs.  Our chroot-build's `dpkg --unpack`
  runs with `--force-overwrite` so the second package's files win —
  cosmetically that's the kmod versions overwriting busybox stubs,
  which is the right outcome.  The warnings are noise.
- **Functional impact**: none — install completes, modules load
  correctly at install time (verified 2026-05-12 install ran through
  partman + bootstrap-base + grub-installer + finish-install).
- **Fix**: COMP-02 phase E — either (a) add a `--force-overwrite`
  exception list to dpkg invocation so these specific overlaps don't
  warn, or (b) drop `busybox-udeb` from `config/installer.list` if
  `kmod-udeb`'s tools cover everything we need (check via
  `Depends:` chain — busybox-udeb might be pulled in transitively
  even if we drop the seed).

### `cat: can't open '/tmp/apt-setup.components'`

- **Evidence in log**: line 1684.
- **Cause**: Downstream of `40cdrom` failure — apt-setup couldn't write
  its components list because `40cdrom`'s verification failed.
- **Fix**: Auto-resolves when COMP-02 phase C fixes apt-cdrom-setup.

### `dpkg-query: no packages found matching xserver-xorg-core` / `task-desktop`

- **Evidence in log**: lines 1898, 1899.
- **Cause**: `finish-install` runs `hw-detect` which probes for X
  server config; we don't ship X.
- **Fix**: None needed — expected for a server install.

---

## Fixed

### ~~`eject` binary missing on target~~ — 2026-05-13

- **Was**: `15cdrom-detect` ran inside the installer chroot and called
  `eject`, but the chroot only had `eject` on the target side (via
  pkg.list `eject` deb).  Disc didn't auto-eject before reboot.
- **Fix shipped**: `config/installer.list:eject-udeb` (installer
  ramdisk has `/bin/eject`).
- **Verified 2026-05-13** at log line 1931:
  `cdrom-detect: Unmounting and ejecting '/dev/sr0'` — clean eject,
  no `eject: not found` anywhere in the log.

### ~~Phase B sudo-password leak via `_sudo_write` → `tee`~~ — 2026-05-13

- **Was**: `_sudo_write` passed `password\\ncontent` as stdin to
  `sudo -S tee`.  When sudo's credential cache was hot (every call
  after the first auth in the build run), `sudo -S` did NOT consume
  the password line — `tee` wrote `password\\ncontent` to the
  destination.  Operator's plaintext sudo password landed at line 1
  of `/var/lib/dpkg/status`, plus `/etc/lsb-release`,
  `/etc/default-release`, and `/var/lib/dpkg/info/athena-stubs.templates`
  — all shipped on the installer ISO.  Also broke main-menu via
  `parser_rfc822: Iek!` segfault (line 1 wasn't valid RFC-822).
- **Fix shipped**: `_sudo_write` now does `sudo -S -v` first
  (consumes the password line — that's `-v`'s entire purpose), then
  runs the actual `sudo tee` (no `-S`) with clean stdin = content
  only.
- **Regression test**:
  `tests/test_module.py::test_installer_chroot_sudo_write_does_not_leak_password_to_tee`
  asserts the password never appears in tee's stdin or on disk.
- **Verified 2026-05-13**: clean install through `finish-install.d/
  20final-message`; no `parser_rfc822` warning.
- **Operator follow-up if you ran the broken build**: rotate sudo
  password, wipe `image/athena-installer-*.iso`, wipe
  `buildroot/installer/`, scan for the leaked password elsewhere
  (`grep -r '<old-pw>' image/ buildroot/ log/`).

### ~~`installer/templates/athena-stubs.templates` (overlay)~~ — 2026-05-12

- Replaced by `_write_athena_stub_template` Python helper in
  `scripts/installer_chroot.py`.  Build-pipeline action writes the
  template content to `/var/lib/dpkg/info/athena-stubs.templates`
  directly during the installer chroot build (priority #1 in the
  Phase B hierarchy — preferred over a custom udeb).
- Why moved here from "build-process workarounds": the workaround
  *itself* is gone (the overlay file deleted, the _OVERLAY_MAP entry
  gone), replaced by a stock-d-i-image-build-conformant helper.

### ~~`installer/preseed/load-preseed.sh` (overlay → `S25-load-preseed`)~~ — 2026-05-12

- Replaced by `auto=true preseed/file=/preseed.cfg` on the kernel
  cmdline in `installer/boot/grub.cfg`.  This is the stock d-i
  mechanism — `preseed-common.udeb` (already in installer.list) reads
  these cmdline params at boot and runs `debconf-set-selections
  /preseed.cfg` before any consumer udeb queries the values.
- Priority #2 in the Phase B hierarchy (stock cmdline knob), chosen
  over priority #3 (custom udeb) because preseed-common is already
  doing exactly this job — we just needed to invoke it.

### ~~`cat: can't open '/etc/default-release'`~~ — 2026-05-12

- Replaced by `_write_release_files` Python helper.  Writes
  `/etc/default-release` (codename) and `/etc/lsb-release` (distrib
  info) at chroot-build time, matching stock d-i Makefile lines
  517-533.

### ~~Stock d-i image-build steps we were skipping~~ — 2026-05-12

- `_run_depmod`: indexes kernel modules per kernel ABI under
  `<chroot>/lib/modules/`.  Matches stock d-i Makefile lines 467-475.
- `_register_self_in_dpkg_status`: appends a dummy
  `Package: debian-installer` stanza so `dpkg-query -W debian-installer`
  succeeds.  Matches stock d-i Makefile lines 564-573.
