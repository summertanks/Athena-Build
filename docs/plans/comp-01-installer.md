# Plan — COMP-01: Installer ISO via parallel udeb dep tree (d-i from source)

## Status: PLANNED (2026-05-10) — Phase 1 done, Phase 2+ ready to execute

**Architectural pivot from the original plan.** Earlier version of this document (commit `9221bf3`) proposed building the installer by source-building `debian-installer` upstream and running its `make build_cdrom` against our repo — i.e. importing Debian's full d-i build pipeline. The user rejected that approach in favour of **rebuilding d-i's runtime from source through Athena's existing pipeline**.

This document supersedes the original plan in full.

## Why the pivot

The original plan was rejected after walking through the alternatives:

1. **Importing vanilla d-i** — heavy (50+ udeb sources, complex Make-based assembly, inherits d-i's whole build infrastructure). Discarded.
2. **Shell + whiptail installer running on the live system** — small but doesn't give the d-i look the user wants; needs the live ISO to also be the installer ISO. Discarded.
3. **Mixed deb+udeb installer chroot** — model violation; udeb deps reference udeb names, deb deps reference deb names; no clean dep namespace. Discarded.
4. **Parallel udeb dep tree built from source through our pipeline** — adopted. Same source corpus produces both `.deb` and `.udeb` outputs (already happens today — `dpkg-buildpackage` reads `debian/control` binary stanzas and emits everything declared); the udeb world becomes a parallel dep tree resolved against `dists/<suite>/main/debian-installer/binary-amd64/Packages` (in the same signed Release file — STA-01 covers it).

## Locked decisions

| Axis | Decision |
|---|---|
| **Architecture** | Parallel udeb dep tree built through our existing source-build pipeline. Two distinct dep graphs over a single source corpus. |
| **Installer ramdisk content** | Pure udeb closure. No debs in the installer chroot. No systemd. |
| **Installer init** | `rootskel` + busybox init as PID 1. Starts cdebconf + main-menu + the d-i step udebs. Authentic d-i minimal-init model. |
| **UI layer** | `cdebconf-text-udeb` (or `cdebconf-newt-udeb`) — d-i's actual UI engine. |
| **Customization** | Source patches under `patch/source/cdebconf/<version>/` (and similar for other udeb sources). Post-install patch flow deferred — only source patches for now. |
| **Branding** | Custom theme udeb (custom source package shipping splash + cdebconf colors). Placeholder graphics generated programmatically; refine later. |
| **Operator config file** | Keep filename `installer.list` (no rename). Contents become MIXED — udeb names AND deb names. The resolver dispatches per-entry. |
| **`installer.list` semantics** | Each entry resolved against both indices: udeb match → `udeb_selected`; deb match → `deb_selected` extras (lands in pool); both match → both happen. |
| **`efibootmgr`/`grub-pc-bin`** | In BOTH `live.list` AND `installer.list`. Defensive duplication: live.list ensures they're on every installed system; installer.list ensures the pool has them when grub-installer asks at install time. |
| **`apt-setup-udeb` writes target sources.list** | `deb [trusted=yes] file:///var/cache/athena-repo athena main` after late_command-style copy of pool to disk. No Debian fallback (per project memory `project_self_contained_repo.md`). |
| **Partitioning** | `partman-auto` recipe for whole-disk ESP + ext4, no swap (locked from prior planning). |
| **Firmware** | UEFI + BIOS, both. |

## Pipeline shape

```
config:
  pkg.list        - deb seeds (kernel, grub-efi, essentials, user choices)
  live.list       - deb seeds (live extras + efibootmgr + grub-pc-bin)
  installer.list  - mixed (udeb seeds + efibootmgr + grub-pc-bin)

cache build:
  fetch dists/<suite>/main/binary-amd64/Packages              -> package_hashtable
  fetch dists/<suite>/main/debian-installer/binary-amd64/Packages -> udeb_hashtable
  Same signed Release file - single GPG verify path (STA-01)

dep parse:
  Pass I-III: deb required + important + pkg.list -> selected_pkgs
  Pass IV:    live.list -> live_exclusive_pkg_names (deb)
  Pass V:     installer.list per-entry dispatch:
                udeb names -> udeb_selected_pkgs (parallel tree)
                deb names  -> selected_pkgs as installer_exclusive
  udeb tree: udeb_required + udeb_important + udeb seeds
             -> udeb_selected_pkgs + udeb_selected_srcs

source download / source build:
  Union of selected_srcs + udeb_selected_srcs (deduped by source name)
  source build installer = sources whose outputs are in udeb closure
                          (or whose only deb outputs are installer extras)

chroot build live:
  apt/dpkg installs deb closure into buildroot/live/

chroot build installer:
  dpkg installs udeb closure into buildroot/installer/
  rootskel sets PID 1 -> busybox init -> cdebconf + main-menu

iso build live:
  buildroot/live -> squashfs -> hybrid bootable ISO with apt pool

iso build installer:
  buildroot/installer -> initrd -> hybrid bootable ISO with kernel + apt pool
```

## Sub-phases

### Phase 1 (DONE 2026-05-10, commit `c17c238`) — File split + dep-tree exclusives

- Split `pkg.list` into `pkg.list` + `live.list` + `installer.list`
- BuildConfig: `--live-list` / `--installer-list` argparse flags + paths
- DependencyTree: `live_exclusive_pkg_names` / `installer_exclusive_pkg_names` / `*_src_names` fields
- `cmd_parse_dependency` Pass IV (live) + Pass V (installer)
- `derive_subset_exclusive_src_names()` mirror of `derive_extras_src_names`
- `print live` / `print installer` views; `print selected` annotates per-subset
- 7 new tests; 163/163 pass

NOTE: under the parallel-universe model, `installer_exclusive_pkg_names` will be repurposed once the udeb tree lands. For Phase 1 it tracks "deb things in installer.list closure beyond pkg" which is fine but incomplete — the new authoritative tracking becomes `udeb_selected_pkgs`.

### Phase 2 — Cache parses udeb index

- `Cache._release_url` flow extended to fetch `dists/<suite>/main/debian-installer/binary-amd64/Packages` per mirror
- New `Cache.udeb_hashtable: Dict[name, Dict[Version, List[Package]]]` parallel to `package_hashtable`
- `Cache.udeb_required` / `Cache.udeb_important` populated from udeb Priority tags (sparse — bookworm has 3 of each)
- `Package` records carrying `Section: debian-installer` go into `udeb_hashtable`; everything else stays in `package_hashtable`
- Same signed Release file → no new GPG verify path
- Tests: udeb index fetch + parse + bucketing

**Estimate: 3-4 days**

### Phase 3 — Parallel udeb DependencyTree

- `BuildSession.udeb_dep_tree` instance, parallel to `dep_tree`
- `cmd_parse_dependency` runs the udeb tree resolution after the deb tree:
  - Seeds = `Cache.udeb_required + Cache.udeb_important + udeb_names_from_installer_list`
  - Resolves transitively against `udeb_hashtable`
- `installer.list` per-entry dispatch:
  - if `name in udeb_hashtable` → seed for udeb tree
  - if `name in package_hashtable` → already handled by Pass V (lands in deb extras)
  - if both → both
- `udeb_dep_tree.selected_srcs` merged into the unified source corpus
- `parse_sources` walks both deb+udeb selected_pkgs to build the union source list
- New `print udebs` view
- Repurpose `installer_exclusive_*` data: keep for deb-extras tracking; add new fields for udeb tracking
- Tests: parallel tree resolves correctly; mixed installer.list dispatches per-entry

**Estimate: 4-5 days**

### Phase 4 — Source build preserves .udeb outputs; repo ingests them

- Verify `buildcontainer.build()` keeps `.udeb` files alongside `.deb` outputs (likely already does — `dpkg-buildpackage` emits both into the parent dir; we copy `*.deb`/`*.udeb` patterns)
- Ensure `repo/` ingestion preserves both extensions
- `source build installer` filter resolves to "sources exclusive to udeb closure" (sources whose every output is in udeb_selected, or whose only deb outputs are installer extras)
- Source-mapping: a source like `cdebconf` produces both `cdebconf*.deb` and `cdebconf*-udeb` files — `selected_srcs[cdebconf].pkgs` should list both. Today's parser may need a small extension to track udeb filenames.
- Tests: building cdebconf produces `.udeb` artefacts; they land in `repo/`; `source build installer` filter selects the right source set

**Estimate: 1-2 days**

### Phase 5 — `chroot build installer` from udeb closure

- New `BuildSystem` codepath (or chroot.py extension) for installing udebs into `buildroot/installer/`
- Use `dpkg -i --force-depends` (or `udpkg` if available — but `udpkg` itself is a udeb, so we need a bootstrap step)
- Install order: udeb topo-sorted via udeb dep tree (similar to `_compute_install_batches` for debs but operating on udeb_dep_tree)
- `rootskel` provides the PID-1 init scaffolding — verify it lands in `/init` or wherever the initrd expects
- Configure cdebconf + main-menu autostart
- Skip the chroot-build steps that don't apply (no apt-setup, no kernel install — kernel is added at iso-mastering time)
- Tests: installer chroot directory layout, init script presence, key udebs unpacked

**Estimate: 1 week**

### Phase 6 — Branding (cdebconf customization + theme udeb)

- Source patches under `patch/source/cdebconf/<version>/9001-athena-strings.patch`:
  - Rewrite visible debconf templates: "Debian" → "Athena"
  - Set cdebconf color theme via patched defaults
- Custom source package: `athena-installer-theme/`
  - Builds an `athena-installer-theme-udeb`
  - Ships boot splash PNG (placeholder graphics generated by build script)
  - Ships overrides for cdebconf colors / strings
  - Adds itself to udeb closure via installer.list
- Placeholder graphics: ImageMagick-generated 640×480 dark background with "Athena Installer" centered
- Boot menu (isolinux/grub.cfg) themed with same palette
- Tests: theme udeb builds, lands in udeb closure, splash file present in chroot

**Estimate: 3-5 days**

### Phase 7 — `iso build installer`

- Wrap `buildroot/installer/` as a compressed initrd (cpio.gz or initramfs)
- Bundle kernel (already built) + initrd + branding splash + isolinux/grub.cfg
- Bundle the regular `repo/` apt pool — installer reads from `/cdrom/pool` at install time
- Hybrid BIOS/EFI ISO via xorriso (same machinery as `iso build live`)
- Likely opportunity to merge `iso build live` and `iso build installer` into a single `iso build` parameterized by `--target=live|installer` once both implementations exist and the diff is small. Defer the merge decision to end of Phase 7.
- Tests: ISO size sanity, boot artifacts present

**Estimate: 3-5 days**

### Phase 8 — Hardware testing

- QEMU UEFI + BIOS smoke tests
- Real hardware: at least one BIOS box + one UEFI box
- Edge cases: small disk, no network, USB-only install media
- Validate target boots cleanly after install completes

**Estimate: 1-2 weeks**

## Total scope

**~5-7 weeks elapsed**, same envelope as the original plan — but built on infrastructure we own end-to-end instead of inheriting d-i's build system.

## Files to modify

- `scripts/cache.py` — udeb index fetch + parse + `udeb_hashtable` field (Phase 2)
- `scripts/dependencytree.py` — confirm class works as parallel instance against either hashtable; possibly small extensions (Phase 3)
- `scripts/build.py` — `cmd_parse_dependency` runs udeb tree resolution; `cmd_source_build` understands installer subset; `cmd_build_chroot_installer` no longer a stub (Phases 3, 4, 5)
- `scripts/buildcontainer.py` — verify `.udeb` outputs preserved (Phase 4)
- `scripts/chroot.py` — new udeb-install codepath for installer chroot (Phase 5)
- `scripts/iso.py` — installer ISO mastering (Phase 7)
- `scripts/print_commands.py` — `print udebs` view (Phase 3)
- `scripts/utils.py` — possibly extend BuildConfig for installer-specific paths
- `tests/test_module.py` — coverage per phase

## Files to create

- `patch/source/cdebconf/<version>/9001-athena-strings.patch` (Phase 6)
- `athena-installer-theme/` source tree (Phase 6) — debian/control declaring udeb output, postinst, splash assets

## Risks

| Risk | Mitigation |
|---|---|
| `dpkg -i --force-depends` for udebs in non-d-i environment hits unexpected file conflicts | Phase 5 spike: install one udeb manually first, observe |
| `rootskel` assumes specific init context not present in our chroot | Phase 5 spike: check rootskel's `/init` script assumptions |
| Source build pipeline silently drops `.udeb` outputs today | Phase 4 verification — small fix if so |
| udeb dep cycles different from deb dep cycles | Existing `_compute_install_batches` handles deb cycles; udeb version may need similar tuning |
| Cdebconf source patches don't survive upstream version bumps | Standard quilt-patch refresh — DEP-3 headers required (CONF-05) |
| Installer ramdisk too large (initrd > some threshold) | Spike at end of Phase 7; if blocking, move some udeb-pulled deps to "load on demand from /cdrom" |

## Verification gates

End-to-end smoke after each phase:

- **Phase 2**: `cache build` produces `udeb_hashtable` with ~440 entries from bookworm
- **Phase 3**: `dep parse` produces `udeb_selected_pkgs` with the closure of `installer.list`'s udeb seeds
- **Phase 4**: `source build cdebconf` produces both `.deb` and `.udeb` artefacts in `repo/`
- **Phase 5**: `chroot build installer` produces `buildroot/installer/` with key udebs unpacked + init scripts present
- **Phase 6**: branded splash + Athena strings visible in any test render
- **Phase 7**: `iso build installer` ISO boots in QEMU; cdebconf comes up; main-menu visible
- **Phase 8**: install completes on real hardware; target boots

Automated:
- New tests in `tests/test_module.py` per phase
- All existing tests pass unchanged
- F541-grep before push (per memory)

## Open questions to resolve at execution time

1. **`udpkg` vs `dpkg --force-depends`** for udeb install in chroot build. udpkg is purpose-built but bootstrapping it (it's a udeb) into the chroot is its own dance. Try `dpkg --force-depends` first; fall back to udpkg if file/directory layout misbehaves.
2. **Initrd format**: cpio.gz vs squashfs-as-initrd vs initramfs. d-i traditionally uses initrd.gz; Phase 7 picks based on size and boot speed.
3. **Boot menu layout**: single ISO with two entries (Try / Install) vs separate ISOs (live + installer). Today's plan keeps two ISOs (`iso build live` and `iso build installer`); single-ISO with dual menu is a possible end-of-phase-7 simplification.
