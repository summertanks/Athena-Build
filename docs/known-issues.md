# Known issues

Live tracker of latent bugs and cosmetic warnings observed in current
builds.  Every entry links to the COMP-02 phase that addresses it.  When
an issue is fixed, move its entry to the bottom under "Fixed" with the
commit hash that closed it.

The reference run is the installer log at `log/athena.log` from a
working install captured 2026-05-12 on VMware BIOS-mode VM.

---

## Critical — affects installed system

### apt-cdrom-setup chain is broken end-to-end

- **Symptom**: `/target/etc/apt/sources.list` ends up empty or broken on
  the installed system.  Running `apt-get update` post-install reports
  "does not have a Release file".  Running `apt-get install <pkg>`
  reports "Unable to locate package".
- **Evidence in log**:
  - line 1473: `base-installer: Please use apt-cdrom to make this CD-ROM
    recognized by APT. apt-get update cannot be used to add new CD-ROMs`
    (configure_apt's apt-cdrom add inside the chroot fails)
  - line 1678: same error from apt-setup-udeb's verification step
  - line 1682: `apt-setup: warning: /usr/lib/apt-setup/generators/40cdrom
    output did not verify`
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
- **Evidence in log**: line 1683 — `apt-setup: warning:
  /usr/lib/apt-setup/generators/91security returned error code 1;
  discarding output`.
- **Severity**: Low for us — no network mirror model.  But it would
  matter if we add a network mirror later.
- **Fix**: COMP-02 phase E (cosmetic / latent cleanup).  Likely
  related to the broken apt-cdrom-setup chain; may auto-resolve once
  phase C lands.

---

## Latent — would surprise an operator

### ~~`eject` binary missing on target~~ *(fixed pending next build)*

- **Symptom**: CD did not auto-eject at end of install.  Operator had
  to manually eject before reboot, otherwise BIOS may boot from CD again.
- **Evidence in log**: line 1927 — `finish-install:
  /usr/lib/finish-install.d/15cdrom-detect: line 21: eject: not found`.
- **Root cause**: TWO `eject` packages: the `eject` deb (target system,
  `/usr/bin/eject`) and the `eject-udeb` udeb (installer ramdisk,
  `/bin/eject`).  `15cdrom-detect` runs INSIDE the installer chroot,
  so it needed the udeb.  Adding `eject` to pkg.list put it on the
  installed target but didn't help the chroot.
- **Fix shipped 2026-05-12**:
  - `config/pkg.list`: `eject` (target system has it for post-install)
  - `config/installer.list`: `eject-udeb` (installer ramdisk has
    `/bin/eject` so `15cdrom-detect` can eject before reboot)
- **Verification**: pending next `chroot build installer` + boot test.

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

## Build-process — engine-side workarounds we want to remove

These are not user-visible but make the build fragile to upstream
churn.  Each is a Python helper that mutates unpacked udeb content.

### `_create_runtime_dirs` (scripts/installer_chroot.py)

- **What it patches**: chroot's `/tmp`, `/var/tmp`, `/root` — none
  shipped by any udeb in our closure.
- **Symptom if removed**: `bootstrap-base.postinst` and several other
  d-i scripts fail because they `> /tmp/some.tmp` and the redirect
  errors.  Caught 2026-05-11 via `sh -x` trace.
- **Fix**: COMP-02 phase B — `athena-installer-stubs-udeb` ships the
  dirs via `debian/dirs`.

### `_install_debootstrap_codename_script` (scripts/installer_chroot.py)

- **What it patches**: sudo-cp the chroot's
  `/usr/share/debootstrap/scripts/sid` to `scripts/<codename>` so
  `debootstrap` recognizes our derivative codename.
- **Symptom if removed**: `bootstrap-base` exits 10 silently (no script
  for suite) — main-menu loops on the step-selection menu.  Caught
  2026-05-11 on first VMware install attempt.
- **Fix**: COMP-02 phase B — `athena-debootstrap-codenames-udeb`
  ships `scripts/<codename>` via `debian/install`.  Codename read from
  the udeb's own `debian/changelog`.

### `installer/templates/athena-stubs.templates` (overlay)

- **What it patches**: drops a `.templates` file into
  `/var/lib/dpkg/info/` declaring `mirror/protocol` (which would
  normally come from `choose-mirror-udeb`, not shipped in our
  cdrom-only model).
- **Symptom if removed**: `bootstrap-base.postinst`'s unguarded
  `db_get mirror/protocol` returns 10 → `set -e` exits → bootstrap-base
  silent exit 10.  Caught 2026-05-11.
- **Fix**: COMP-02 phase B — `athena-installer-stubs-udeb` ships this
  via its own `debian/templates`.

### `installer/preseed/load-preseed.sh` (overlay → `S25-load-preseed`)

- **What it patches**: shell script in
  `/lib/debian-installer-startup.d/` that runs
  `debconf-set-selections /preseed.cfg` at boot, because stock d-i has
  no auto-loader for `/preseed.cfg` in the initrd.
- **Currently not load-bearing**: our `preseed.cfg` is empty so the
  script is a no-op.  Kept as a hook for future preseed work.
- **Fix**: COMP-02 phase B — try `auto=true file=/preseed.cfg` kernel
  cmdline first (stock d-i mechanism).  If that works, remove the
  overlay entirely.  Otherwise ship as a tiny udeb.

---

## Cosmetic — noisy logs but no functional impact

### `depmod: WARNING: could not open modules.builtin.modinfo`

- **Evidence in log**: line 145.
- **Cause**: `kernel-image-6.1.0-47-amd64-di` udeb (built from
  `linux-signed-amd64` source) doesn't include the modinfo file.
  Modules still load — depmod just can't compute reverse-deps for the
  `builtin` modules.
- **Fix**: COMP-02 phase E — investigate upstream; may be a real bug
  in linux-signed-amd64's modules-udeb packaging.

### `mount: mounting none on /sys/firmware/efi/efivars failed`

- **Evidence in log**: line 146.
- **Cause**: BIOS-mode VM — no EFI variables to mount.  Expected on
  non-EFI hardware.
- **Fix**: None needed for BIOS; gets exercised correctly once COMP-02
  phase D adds EFI support.

### `cat: can't open '/etc/default-release'`

- **Evidence in log**: line 117.
- **Cause**: Some d-i script tries to read the Debian release identifier;
  we don't ship that file.
- **Fix**: COMP-02 phase E — `athena-installer-stubs-udeb` ships
  `/etc/default-release` with content `thor` (or whatever the codename
  is at build time).

### `Falling back to the package description for ext4-modules-...-di` (×4)

- **Evidence in log**: lines 133-136.
- **Cause**: `ext4-modules-X-di` and `fat-modules-X-di` udebs ship
  templates but their `Description:` fields aren't getting loaded into
  cdebconf's runtime DB.
- **Fix**: COMP-02 phase E — investigate; likely the templates files
  use a description format cdebconf doesn't accept, or the udebs aren't
  marked correctly for template registration.

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

- **Evidence in log**: line 1685.
- **Cause**: Downstream of `40cdrom` failure — apt-setup couldn't write
  its components list because `40cdrom`'s verification failed.
- **Fix**: Auto-resolves when COMP-02 phase C fixes apt-cdrom-setup.

### `dpkg-query: no packages found matching xserver-xorg-core` / `task-desktop`

- **Evidence in log**: lines 1889, 1890.
- **Cause**: `finish-install` runs `hw-detect` which probes for X
  server config; we don't ship X.
- **Fix**: None needed — expected for a server install.

---

## Fixed

*(empty — populate as COMP-02 phases land)*
