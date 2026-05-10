# Plan — COMP-01: debian-installer-based Athena installer

## Status: PLANNED (2026-05-10) — ready to execute

User decision after evaluating UI options (custom Python curses, Textual rewrite, dialog/whiptail, Calamares, debian-installer): **go with debian-installer (d-i)**.

Reasoning summary from the discussion:
- Custom Python TUI (reuse `tui.py`) and Textual rewrite both require shipping the Python interpreter + curses/textual + transitive deps in `pkg_installer.list` — significant source-build surface.
- Calamares is too heavy (Qt + X server on installer ISO) and contradicts the TUI design intent.
- d-i is C + shell, runs from initrd, no Python interpreter required.
- d-i is battle-tested across every Debian derivative (Ubuntu, Mint, Tails, Kali) and configurable via preseed, debconf strings, late_command, and theme udebs.

## Locked decisions

| Axis | Decision |
|---|---|
| **Installer tech** | debian-installer (d-i) |
| **Interactivity** | Mostly preseeded; operator answers hostname, root pw, user account, target disk (~5 screens) |
| **Partition recipe** | Whole-disk: ESP + ext4 root, no swap |
| **Firmware** | Both UEFI and BIOS (detect at install time) |
| **Apt pool on installer ISO** | Full pool from our repo on ISO; preseed `mirror/protocol=file`, `mirror/file/directory=/cdrom` |
| **No Debian fallback** | Ever — `apt-setup/use_mirror=false`, no `*.debian.org` anywhere on installer ISO or installed system. Re-adding Debian sources is a manual operator action. |
| **Installed system sources.list** | `deb [trusted=yes] file:///var/cache/athena-repo <suite> main` — pool copied to disk at install time via late_command |
| **Branding** | Strings + colors + boot splash (custom theme udeb; requires Athena logo asset) |

## Context

COMP-01 (P1) is the missing installer for Athena. Today the build pipeline produces a live ISO (rootfs + bootloader, no installer). To ship Athena as something a user installs onto disk, an installer ISO with the d-i flow is needed.

The constraint that drove the technology choice: Athena is self-contained (see memory `project_self_contained_repo.md`). The installer must not reference `deb.debian.org` at any point. Its apt pool is exactly what's in our repo — built by `source build` or imported by `package tunnel`.

The constraint that ruled out Python: `pkg_installer.list` should be small, since these packages are above-and-beyond `required` + `important` and cannot rely on `selected` (user-mutable). Shipping a Python installer means adding python3 + ~30-50 transitive deps. d-i adds ~30-50 udebs but they're micro-packages (typically <100 KB each) rather than full interpreters and libraries.

## Pipeline shape

```
buildroot/live/        chroot build live      iso build live      ← existing path, renamed
buildroot/installer/   chroot build installer iso build installer ← new path
```

| Command | Behaviour |
|---|---|
| `source build live` | Existing — builds packages destined for live ISO rootfs |
| `source build installer` | **New** — builds `debian-installer` source package + all required udebs + custom theme udeb |
| `chroot build live` | Existing logic, renamed |
| `chroot build installer` | **New** — sets up build chroot with `debian-installer-utils`, `mklibs`, etc.; runs `make build_cdrom` against d-i source tree pointed at our repo; output: `initrd.gz` + `vmlinuz` + udeb manifest |
| `iso build live` | Existing logic, renamed; gated on `buildroot/live/` existing |
| `iso build installer` | **New** — wraps `initrd.gz` + `vmlinuz` + full `pool/` + isolinux/grub config + preseed.cfg + branding theme into hybrid bootable ISO; gated on `buildroot/installer/` existing |

## New artifacts in repo

| Path | Purpose |
|---|---|
| `pkg_installer.list` | List of source packages d-i needs (debian-installer + udebs + theme). Above-and-beyond `pkg_required` and `pkg_important`. Never derived from `selected` |
| `installer/preseed.cfg` | Preseed answers — baked into installer initrd at build time |
| `installer/branding/strings.po` | gettext catalog overriding visible debconf prompts to say Athena |
| `installer/branding/theme/` | Boot splash PNG, isolinux/grub theme files |
| `installer/late_command.sh` | Shell run in target chroot before reboot — copies pool to `/var/cache/athena-repo`, writes sources.list, runs `apt update` |
| `installer/athena-theme/` | Source tree for the custom udeb that ships branding + theme |
| `installer/isolinux.cfg` / `installer/grub.cfg` | Boot menu config (Install / Expert install entries) |

## Preseed sketch (key lines)

```
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i time/zone string Etc/UTC
d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string  # ← operator answers
d-i mirror/protocol string file
d-i mirror/file/directory string /cdrom
d-i apt-setup/use_mirror boolean false
d-i apt-setup/services-select multiselect
d-i apt-setup/security_host string
d-i partman-auto/method string regular
d-i partman-auto/expert_recipe string \
  athena-root :: \
    538 538 1075 free $primary{ } method{ efi } format{ } . \
    1000 10000 -1 ext4 $primary{ } $bootable{ } method{ format } \
                       format{ } use_filesystem{ } filesystem{ ext4 } \
                       mountpoint{ / } .
d-i partman-auto/choose_recipe select athena-root
d-i partman/confirm boolean true
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm_nooverwrite boolean true
d-i passwd/root-password password  # ← operator answers
d-i passwd/user-fullname string    # ← operator answers
d-i passwd/username string         # ← operator answers
d-i tasksel/first multiselect
d-i pkgsel/include string  # bare base; rest comes from pkg_required/important on the ISO
d-i grub-installer/bootdev string default
d-i preseed/late_command string /cdrom/late_command.sh
d-i finish-install/reboot_in_progress note
```

Operator screens (in order): hostname → user account → root password → disk picker → confirm. ~5 screens, then unattended through to reboot.

## late_command.sh sketch

```bash
#!/bin/sh
set -e
TARGET=/target
mkdir -p $TARGET/var/cache/athena-repo
cp -a /cdrom/pool   $TARGET/var/cache/athena-repo/
cp -a /cdrom/dists  $TARGET/var/cache/athena-repo/
cat > $TARGET/etc/apt/sources.list <<EOF
deb [trusted=yes] file:///var/cache/athena-repo athena main
EOF
in-target apt-get update
# (no upgrade — pool is already what we want)
```

## Phased execution plan

### Phase 1 (1-2 weeks) — Vanilla d-i, no preseed, no branding

- Add `pkg_installer.list` with the d-i source set; verify all build cleanly through existing source-build pipeline
- Add `chroot build installer` — sets up the d-i build chroot, runs `make build_cdrom`, captures `initrd.gz` + `vmlinuz`
- Add `iso build installer` — wraps initrd + kernel + pool into a hybrid ISO
- Verify: ISO boots in QEMU, d-i comes up and walks through stock screens, install completes with stock Debian-flavored prompts

### Phase 2 (3-5 days) — Preseed + apt-source rewrite

- Write `installer/preseed.cfg` with the locked recipe
- Write `installer/late_command.sh`; bake both into initrd at d-i build time
- Verify: install completes with only the 5 operator screens; installed system has working `apt update` against `/var/cache/athena-repo`; `/etc/apt/sources.list` contains zero Debian references

### Phase 3 (1 week) — Pipeline command surface

- Rename `chroot build` → `chroot build live`; rename `iso build` → `iso build live`
- Add `source build installer` filter (parallel to existing `recommended`)
- Wire prerequisite gates (`iso build installer` requires `buildroot/installer/`; `iso build live` requires `buildroot/live/`)
- Update README, TODO.md row for COMP-01, build-system.sh
- Tests in `tests/test_module.py` for the new dispatchers and gates

### Phase 4 (3-5 days) — Branding

- String catalog: `installer/branding/strings.po` rewriting visible prompts to say Athena
- Custom theme udeb (`athena-theme/`) for boot splash + cdebconf colors
- Add to `pkg_installer.list`; preseed pulls it in
- Requires: Athena logo asset (PNG, ~640×480 for boot splash)

### Phase 5 (1-2 weeks) — Hardware testing

- QEMU coverage for UEFI + BIOS
- Real hardware: at least one BIOS box and one UEFI box
- Edge cases: small disk (< partition recipe min), USB-only install media, no network, network present

**Total elapsed: 5-7 weeks**

## Files to modify

- `scripts/build.py` — `cmd_chroot` and `cmd_iso` dispatchers gain `live` / `installer` action variants; `cmd_source` gains `installer` filter; new prerequisite gates; new prerequisite error strings
- `scripts/parsing.py` — recognise `pkg_installer.list` as a new package category; handle `Section: debian-installer` udeb packages distinctly from regular .debs
- `scripts/cache.py` — likely no changes; pool format is unchanged
- `build-system.sh` — message strings updated for renamed commands (`chroot build` → `chroot build live`, etc.)
- `README.md` — document the installer ISO build flow + the two-pipeline split
- `TODO.md` — replace COMP-01 row with sub-phase breakdown; track Phase 1-5 separately
- `tests/test_module.py` — new dispatcher tests for `chroot live` / `chroot installer` / `iso live` / `iso installer` / `source build installer`; gate tests asserting `iso build installer` errors when `buildroot/installer/` is absent

## Files to create

- `pkg_installer.list` — at repo root alongside `pkg_required.list` / `pkg_important.list`
- `installer/preseed.cfg`
- `installer/late_command.sh`
- `installer/branding/strings.po`
- `installer/branding/theme/` (boot splash assets)
- `installer/athena-theme/` (custom udeb source tree — debian/, control, postinst, etc.)
- `installer/isolinux.cfg`
- `installer/grub.cfg`

## Existing code to reuse

- `scripts/utils.run_cmd_with_logging` (or current equivalent) — for invoking `make build_cdrom`, `xorriso`, etc.
- The existing chroot bootstrap helpers in `scripts/buildcontainer.py` (or wherever chroot setup lives) — likely shared between `chroot build live` and `chroot build installer`. Worth extracting a `chroot bootstrap` primitive that both call before they layer their own package set.
- The existing ISO mastering logic — `iso build live` and `iso build installer` differ in payload (rootfs vs initrd+kernel) but share the xorriso invocation, hybrid boot config, and pool inclusion steps
- `tui.Console` / `Spinner` / `ProgressBar` / `Prompt` — used for the *build host* operator UX of running `chroot build installer` etc.; the installer running on the *target* uses d-i's cdebconf, not these

## Risks

| Risk | Mitigation |
|---|---|
| d-i source packages may pull build deps not currently in `pkg_required` | Phase 1 is the discovery phase — expect to grow `pkg_required` to absorb d-i build deps. Plan: start with a vanilla `make build_cdrom`, observe failures, iterate |
| d-i is strict about apt repo `Release` file signing | If our repo isn't signed today, use `[trusted=yes]` in `sources.list` as an interim. Signing is separate work (likely a separate ticket — check TODO for one tracking repo signing) |
| Late_command shell is fragile — easy to ship a broken installer | Keep `late_command.sh` short; do anything complex in a proper post-install script that runs from a systemd `oneshot` on first boot |
| `pkg_required` cannot include the installer's own runtime deps cleanly — they're udebs, not regular .debs | Two parallel package universes (d-i udebs vs target-system .debs). The parser needs to handle `Section: debian-installer` packages. Verify what `parsing.py` does with udebs today |
| Theme udeb requires C/shell + dh_installdebconf — outside the existing source-build idiom | Phase 4 spike before committing to a logo budget. Could also defer theme udeb and ship strings-only branding for v1 |
| QEMU testing won't catch firmware quirks | Phase 5 budgets real-hardware time. Plan: at minimum one BIOS + one UEFI host |
| Pool size on installer ISO could exceed reasonable USB stick sizes | If pool grows past ~4 GB, consider splitting `selected` into core/extra and shipping only core on the installer; rest fetched after install. Defer until measured |

## Verification

End-to-end smoke for each phase:

**Phase 1:** `iso build installer && qemu-system-x86_64 -cdrom buildroot/installer/athena-installer.iso` → d-i boots, walks the stock flow, install completes, target boots into base system.

**Phase 2:** Same as Phase 1 but operator only sees 5 screens; post-install: `cat /target/etc/apt/sources.list` shows `file:///var/cache/athena-repo` and zero Debian references; `apt-get update` in target succeeds.

**Phase 3:** `cache build && dep parse && source download && source build installer && container init && chroot build installer && iso build installer` runs clean. Same sequence with `live` everywhere produces working live ISO. `iso build installer` fails cleanly when `buildroot/installer/` is missing.

**Phase 4:** Boot menu shows Athena splash; every visible debconf prompt says Athena (grep d-i transcript for "debian", expect zero matches outside compatibility strings).

**Phase 5:** Install on real hardware (BIOS + UEFI), boot installed system, verify network + user login + apt update.

Automated:
- `tests/test_module.py` — new dispatcher tests for the 4 renamed/new commands; gate assertions
- All existing 100+ tests pass unchanged
- F541-grep on diff before push (per memory `feedback_check_f_strings_before_push.md`)

## Open questions to resolve at execution time

When Phase 1 starts, these need answering:

1. **Does our repo's `Release` file already include the metadata d-i needs?** — `Codename`, `Suite`, `Components`, `Architectures`. Check before committing to "use our repo as d-i mirror" works at all.
2. **What's the exact udeb set?** — Phase 1 discovery. Vanilla d-i pulls a known set; ours may differ if we want to skip components (e.g. wifi support if Athena doesn't ship wifi firmware).
3. **Suite/codename naming** — installer expects e.g. `bookworm` or `athena-1.0`. Pick a stable naming scheme before writing preseed.
4. **Where does the Athena logo come from?** — Phase 4 prerequisite. If no logo exists, Phase 4 either gets a placeholder or branding is downgraded to strings-only for v1.
5. **Does `parsing.py` already handle `Section: debian-installer` udebs, or does it skip them?** — Phase 1 spike question.
