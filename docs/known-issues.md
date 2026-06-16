# Known issues

Live tracker of latent bugs and cosmetic warnings observed in current
builds.  Every entry links to the COMP-02 phase or the `TODO.md` ticket
that addresses it.  When an issue is fixed, move its entry to the
bottom under "Fixed" with the commit hash that closed it.

The reference run is the installer log at `log/athena.log` from a
working install captured 2026-05-13 on VMware BIOS-mode VM (post
Phases B + C — main-menu boots cleanly, apt-cdrom signs and verifies
end-to-end, install runs through `finish-install.d/95umount`).

---

## Critical — affects installed system

*(`apt-cdrom-setup chain` and `91security` entries moved to Fixed
2026-05-13 — Phase C signing + base-installer keyring patch verified
end-to-end in the 15:57 reference install.)*

### `50mirror` + `91security` generators return error 1

- **Symptom**: `apt-setup: warning: /usr/lib/apt-setup/generators/50mirror
  returned error code 1; discarding output` (log line 1697) and same
  for `91security` (log line 1698).  Phase C ruled out the
  Release-signing chain as the cause (those resolved cleanly).
- **Root cause**: We don't ship a network mirror (cdrom-only
  architecture per `project_self_contained_repo`).  `50mirror` fails
  with `choose-mirror is not available; cannot offer network mirror`
  (line 1696) because the `choose-mirror-bin` udeb isn't seeded.
  `50mirror` would normally write `/tmp/apt-setup.components`; without
  it, `91security` reads an absent file and exits non-zero too
  (`cat: can't open '/tmp/apt-setup.components'`, line 1700).  Same
  for `92updates` / `93backports` — all read the same file.
- **Severity**: Low.  An installed cdrom-only target wouldn't get
  security updates over the network anyway; the missing entries are
  semantically correct.
- **Fix**: COMP-02 phase E — either (a) silence the generators by
  shipping stub `/tmp/apt-setup.components` with `main` so they no-op
  cleanly, or (b) accept the warnings as documented architecture noise.
  Lean (b) — they're informational and the install completes fine.

---

## Latent — would surprise an operator

*(`Installer ISO is BIOS-only` moved to Fixed 2026-05-13 — Phase D
shipped bin-only `grub-pc-bin` + `grub-efi-amd64-bin` so
`grub-installer` picks the right meta-package at install time.
Phase D follow-up 2026-05-14: shipped `config/pool.list` to inject
the meta `.deb`s into the cdrom pool — see Fixed below.)*

### Dep-drift check is silent when a dependency *disappears*

- **Symptom**: a built `.deb` can lose a dependency it used to declare
  and nothing in the pipeline warns — the broken metadata flows
  straight into the chroot/ISO closures.  Observed 2026-06-11:
  e2fsprogs `+asg1u1` shipped **missing** its `libext2fs2` /
  `libcom-err2` / `libss2` deps (a patch dropped the `-L shlibs.local`
  mapping — see `docs/build-quirks.md` § 3.4) and the SURFACES-01 disk
  closure faithfully followed the bad metadata; the disk image
  boot-looped (fsck exec failure, quirk 9.1).  The legacy
  ship-everything pool had masked the gap.
- **Cause**: dep-drift verification compares constraint *versions*
  between cache record and on-disk artifact; a dep that is simply
  *absent* on the built side (for a package that is itself selected)
  raises nothing.
- **Severity**: Medium — only bites when a patch or toolchain quirk
  eats a dependency, but when it does the failure shows up at *boot*,
  far from the cause.
- **Fix**: TODO `STA-24` — WARN at drift-check time when a dep present
  on the upstream record is missing from the built artifact.

### `repo repair cleanup` can delete files that still have live remote claims

- **Symptom**: the local prune of superseded/obsolete `.deb`s does not
  check the remote claim ledger first — an operator can delete bytes
  locally that a published claim on a mirror still names as live.
  The receiving side of `mirror publish` is protected since 2026-06-11
  (commit `b356e56` stops `rsync --delete` from reaping remote pool
  files — a local prune had propagated and deleted 17
  obsolete/deprecated files from the append-only remote pool), but the
  local deletion itself is still silent.
- **Cause**: cleanup walks local repo state only; UPD-01's
  publish-before-prune discipline is documented, not enforced.
- **Severity**: Low-medium — `mirror pull` can re-fetch peer-owned
  bytes, but own-claim bytes deleted before deprecation/obsolescence
  is published are gone.
- **Fix**: TODO `STA-25` — warn (and require confirmation) when a
  cleanup target filename matches a live published claim.

### Initramfs ships no fsck tools — chroot fstab is empty at `update-initramfs` time

- **Symptom**: the generated initramfs carries no `fsck.ext4` /
  `fsck.vfat`, so the root filesystem is never checked from the
  initramfs; checking falls through to `systemd-fsck` running off the
  real root.  Noticed while debugging the 2026-06-11 disk-image
  reboot loop (it was *not* the cause — but it removed a layer of
  diagnosis).
- **Cause**: `initramfs-tools` decides which fsck binaries to copy by
  reading `/etc/fstab`; the kernel postinst runs `update-initramfs`
  during the chroot build, *before* our fstab is generated, so fstab
  is empty at that moment.
- **Severity**: Low — systemd-fsck on the real root works for healthy
  filesystems; the gap matters when the root fs needs repair before
  it can be mounted read-write.
- **Fix**: RESOLVED (`STA-26`) — `disk_image.build_disk_image` re-runs
  `update-initramfs -u -k all` inside the image chroot right after the
  real fstab is written (step 7, ext4 root passno 1) and the bind-mounts
  are up, so initramfs-tools' fsck hook reads the correct root fstype and
  pulls `fsck.ext4` in.  Verified on the built qcow2 (2026-06-16):
  `lsinitramfs … | grep fsck` → `usr/sbin/fsck.ext4`.  The ESP's
  `fsck.vfat` is intentionally NOT in the initramfs — the initramfs only
  mounts the root fs; `/boot/efi` (vfat, passno 2) is checked later by
  `systemd-fsck` from the real root, where dosfstools lives.  Scoped to
  the disk surface only — the live ISO boots a squashfs/live-boot root
  that needs no fsck.  Non-fatal on failure (the image still boots via
  systemd-fsck on the real root).

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

### `dpkg-divert: warning: ... use --no-rename` (~23 occurrences)

- **Evidence in log**: spread across base-installer, apt-setup, and
  finish-install steps — every chroot_setup() call emits one.
- **Cause**: `chroot-setup.sh` (in `debian-installer-utils 1.146`)
  calls `dpkg-divert --rename` against `/sbin/start-stop-daemon`
  (Essential file from `dpkg`).  Modern dpkg warns on every such call.
- **DO NOT "fix" this with `--no-rename`**.  The Phase E patch that
  did this (commit `34905d9`) was reverted because it bricks
  `grub-installer`.  Why: stock `chroot-setup.sh` writes the daemon
  stub directly OVER the original path:
  ```
  if [ -e /target/sbin/start-stop-daemon ]; then
      divert /sbin/start-stop-daemon          # --rename: real → .REAL
  fi
  cat > /target/sbin/start-stop-daemon <<EOF  # writes stub at ORIGINAL
  ```
  With `--rename`, the real binary is moved to `.REAL` first, then the
  stub overwrites the (now-empty) original path safely; `undivert`
  later moves `.REAL` back.  With `--no-rename`, divert only creates a
  metadata record — the real binary is **not moved** — so the `cat >`
  destroys it.  `undivert` then `rm`s the stub, leaving no
  start-stop-daemon at all.  After the first chroot_setup→cleanup
  cycle the binary is permanently gone, and `grub-installer` (which
  runs `chroot /target dpkg ...` directly without going through
  in-target → no fresh stub) fails with `dpkg: 'start-stop-daemon'
  not found in PATH`.  Verified on the 2026-05-14 install run.
- **Fix**: Either suppress the warning at the dpkg-divert call sites
  (a different patch — e.g. `2>/dev/null` or `--quiet --no-warnings`
  if dpkg supports it), or accept it as documented noise.

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

- **Evidence in log**: line 1700.
- **Cause**: Downstream of `50mirror` failing (no network mirror — see
  the dedicated entry under Critical above).  Same root cause; same
  fix path.

### `dpkg-query: no packages found matching xserver-xorg-core` / `task-desktop`

- **Evidence in log**: lines 1898, 1899.
- **Cause**: `finish-install` runs `hw-detect` which probes for X
  server config; we don't ship X.
- **Fix**: None needed — expected for a server install.

*(`Unable to locate package intel-microcode` moved to Fixed 2026-05-28
— non-free / contrib / non-free-firmware became real repo components
and a curated microcode + firmware set is tunneled into the pool.
See Fixed below.)*

*(`open-vm-tools` moved to Fixed 2026-05-14 — added to `config/pool.list`
so `hw-detect`'s `apt-install open-vm-tools` succeeds on VMware
targets.  Verification pending next install on a VMware host.)*

### Fresh disk images carry a benign ext4 orphan list

- **Symptom**: the first boot of a freshly-built qcow2 disk image logs
  the kernel/e2fsck cleaning a small ext4 orphan-inode list on the
  root filesystem.  Harmless — the cleanup succeeds and never recurs.
- **Cause**: files deleted inside the chroot while the image's loop
  mount is live leave entries on the ext4 orphan list when
  `disk_image.py` unmounts; nothing replays the journal/orphans on the
  artifact before it ships.  Both the root fs and the ESP otherwise
  verify clean via `e2fsck` / `fsck.fat` against the shipped artifact
  (checked 2026-06-11 during the disk-image first-boot debugging).
- **Fix**: TODO `HK-06` — add a post-umount `e2fsck -f -p` pass in
  `disk_image.py` so the shipped image is pristine.

---

## Fixed

### ~~`Unable to locate package intel-microcode`~~ — 2026-05-28

- **Was**: `finish-install`'s `hw-detect` probes for CPU-microcode
  packages and apt-installs them on the target; `intel-microcode`
  lives in `non-free-firmware`, which our cache didn't index and our
  pool didn't carry — the apt-install failed with "Unable to locate"
  (reference log lines 1922-1940).
- **Fix shipped** (commit `3bf61d7`): `non-free` / `contrib` /
  `non-free-firmware` became real repo components, and
  `config/pool.list` now carries a curated tunneled set —
  `intel-microcode`, `amd64-microcode`, plus the common Wi-Fi/NIC/GPU
  firmware packages (`firmware-iwlwifi`, `firmware-realtek`,
  `firmware-atheros`, `firmware-brcm80211`, `firmware-amd-graphics`,
  `firmware-misc-nonfree`).  These are prebuilt non-free binaries, so
  they tunnel rather than build from source; they ship in
  `/cdrom/pool` only, never pre-installed in any chroot.
- **Note**: the cdrom-disable finish-install hook had to be numbered
  *after* `08hw-detect` (11, not 06) or the offline apt-install of
  microcode / `open-vm-tools-desktop` would fail — see memory
  `feedback_iso_pool_and_finish_install_ordering`.  These packages are
  also kept as installer-default roots under the SURFACES-01
  manifest-driven pool (`config/installer-defaults.list`), so the
  2026-06-11 pool shrink did not drop them — verified present in the
  staged manifest.

### ~~Installer ISO is BIOS-only~~ — 2026-05-13 *(verification pending)*

- **Was**: `config/pkg.list` pre-installed full `grub-pc` (BIOS meta)
  as the Phase A workaround for broken apt-cdrom-setup; an EFI-mode
  target would have an unbootable installed system.
- **Fix shipped** (commit `875200e`): swapped `grub-pc` for the
  bin-only pair `grub-pc-bin` + `grub-efi-amd64-bin`.  Unlike the
  full meta-packages, the `-bin` variants do NOT Conflict, so both
  coexist on the live + installer + target.  `grub-installer`
  detects firmware mode at install time and apt-installs whichever
  meta-package fits; that meta Depends on its `-bin` (already
  present), so apt only fetches the small meta + glue.
- **Verification**: pending — boot the rebuilt ISO in EFI-mode VM
  and confirm `grub-installer` picks `grub-efi-amd64`.

### ~~grub-installer fails: `Package grub-pc / grub-efi-amd64 has no installation candidate`~~ — 2026-05-14 *(verification pending)*

- **Was**: Phase D regression — swapping `grub-pc` (META) for
  `grub-pc-bin` in `pkg.list` removed the META from `selected_pkgs`,
  which is the input to `_pool_whitelist`.  `_select_pool_files`
  then dropped `grub-pc_*.deb` and `grub-efi-amd64_*.deb` from
  `staging/pool/` (even though both .debs are present in `repo/` as
  side artefacts of the grub2 source build).  At install time
  `grub-installer` calls `apt-install grub-pc` (BIOS) /
  `apt-install grub-efi-amd64` (EFI) against `/cdrom/pool`, gets
  "no installation candidate", and aborts.  The 2026-05-14 install
  reproduced this on **both** modes (`log/athena_bios.log` line
  1829, `log/athena_efi.log` line 1840).
- **Why we couldn't simply `installer.list` them**: the metas
  Conflict with each other.  Putting both in `installer.list` would
  send them through `dependencytree.resolve_packages` →
  `validate_selection`, which fires DEPENDENCY HELL on the
  install-time conflict.  But install-time conflict is a property
  of the *target system* — both .debs can sit indexed in a pool
  side-by-side; only one ever gets installed.
- **Fix shipped**: new `config/pool.list` — a third tier of package
  selection alongside `pkg.list` (installed in live + installer +
  target) and `installer.list` (installed in installer ramdisk and
  /target).  Pool entries are resolved through the normal dep tree
  in a new **Pass VII** (so all transitive Depends are pulled in
  and source-built), but with `check_conflicts=False`.
  `validate_selection` skips Breaks/Conflicts where either side is
  in `pool_extras_pkg_names`.  `cmd_build_iso_installer` subtracts
  pool extras from `_base_include` so debootstrap doesn't put them
  on the target — they ship in `/cdrom/pool` only, available to apt
  at install time and post-install.  Seeded with `grub-pc` +
  `grub-efi-amd64` + `open-vm-tools` + `console-setup` +
  `keyboard-configuration` + `xkb-data`.  See `config/pool.list` for
  the contract and the failure-mode story inline; see
  `scripts/dependencytree.py:validate_selection` for the
  membership-based bypass.
- **`shim-signed` still missing**: the EFI log also shows
  `Additionally installing shim-signed to go with grub-efi-amd64` →
  `Package 'shim-signed' has no installation candidate`.
  shim-signed isn't in our cache or build pipeline (separate Microsoft-
  signed source).  Without it, EFI installs work but Secure Boot
  doesn't.  Deferred — see the `intel-microcode` / `open-vm-tools`
  pattern under `docs/plans/comp-02-robust-build.md` § Phase E
  Deferred for the same kind of "needs upstream source we don't
  ship" follow-up.
- **Verification**: pending — rebuild ISO and boot in both BIOS and
  EFI VMs.  Both should now reach `finish-install`.

### ~~`Failed to install keyboard-configuration / console-setup into /target/: 100`~~ — 2026-05-14 *(verification pending)*

- **Was**: `base-installer`'s `apt-install` queues both packages for
  /target's console keymap, but neither was in our cdrom pool —
  `console-setup` (.deb sat in `repo/` from a side-build but wasn't
  in `_pool_whitelist`); `xkb-data` wasn't built at all (its source
  package is `xkeyboard-config`).  Verified on the 2026-05-14
  install run: `keyboard-configuration : Depends: xkb-data
  (>= 2.35.1~) but it is not installable` →
  `Failed to install keyboard-configuration into /target/: 100` →
  `Package 'console-setup' has no installation candidate` →
  `Failed to install console-setup into /target/: 100`.  Pre-dated
  the Phase D regression (same in the 2026-05-13 reference run);
  was just drowned out by the grub-installer crash before now.
- **Fix shipped**: add `console-setup`, `keyboard-configuration`,
  `xkb-data` to `config/pool.list`.  Pass VII pulls the transitive
  closure through the source-build pipeline (including the
  `xkeyboard-config` source build that produces `xkb-data`), and
  `_pool_whitelist` ships them in `/cdrom/pool` so base-installer's
  in-target apt finds them.  Same shape as the bootloader metas /
  open-vm-tools — target-side only, no live image bloat.
- **Verification**: pending — rebuild ISO, install, and confirm
  base-installer no longer logs the two `Failed to install` lines.

### ~~apt-cdrom-setup chain broken end-to-end~~ — 2026-05-13

- **Was**: apt rejected our unsigned Release with "does not have a
  Release file" → `apt-setup` discarded `40cdrom` output → final
  `/target/etc/apt/sources.list` had no cdrom entry → post-install
  `apt-get install <pkg>` failed with "Unable to locate package".
- **Fix shipped** (commits `42e03ea` + `32be5bd`):
  1. `_sign_release_files` in `scripts/iso_installer.py` — gpg
     --detach-sign → `Release.gpg`, --clearsign → `InRelease`.
  2. `_export_pubkey_to_staging` — pubkey at `.disk/archive-key.gpg`.
  3. `patch/source/base-installer/1.213/9001-install-athena-archive-keyring.patch`
     — quilt patch on `library.sh:configure_apt` that copies the
     keyring into `/target/etc/apt/trusted.gpg.d/` before
     `apt-cdrom add`.
  4. `_refresh_patches` deletes the build record (`<pkg>.build.json`)
     when its stored `patch_set_hash` no longer matches the current
     on-disk patch content, so autorun's source-build step rebuilds
     packages with new patches.
- **Verified 2026-05-13 in reference run**:
  - line 1501: `base-installer: gpgv: Good signature from "Athena Build"`
  - line 1519: `Hit:1 cdrom://... thor InRelease` (was `Err:2`)
  - line 1672: same Good signature reported by `apt-setup-udeb`
  - no `40cdrom output did not verify` anywhere
  - install ran through `finish-install.d/95umount` (last hook).

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
