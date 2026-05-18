# Athena-Build — Project TODO

> Serialized, trackable backlog produced from a maturity / stability /
> completeness review of the source tree at the date below. Each task has a
> stable ID (`AREA-NN`); cite the ID when committing or referencing work.
> Update the **Status** column inline rather than renumbering.

- **Review date:** 2026-05-07 (initial), maintenance pass 2026-05-14 (`master` @ `37cabc9`)
- **Reviewer:** Claude (read-only audit, with subsequent maintenance reconciling actual state vs ticket status)
- **Tree size:** ~7,600 LOC (≈6,200 Python, 281 Bash)
- **Pipeline state:** end-to-end working — `cache build → dep parse →
  source download → container init → source build → chroot build
  (+verify) → iso build live → iso build installer`.  Installer ISO
  reaches `finish-install` cleanly on both BIOS and EFI as of
  2026-05-14 (commit `68266a2`).
- **Status legend:** `todo | wip | done | blocked | wontfix`
- **Severity legend:** `P0` blocker, `P1` important, `P2` quality-of-life,
  `P3` future / nice-to-have
- **Archived tickets:** completed (`done`) and abandoned (`wontfix`)
  rows are moved to [`docs/done.md`](docs/done.md) once their status
  changes.  When closing a ticket here, copy the row to docs/done.md
  in the matching section and remove it from below.

---

## 0. Analysis summary

The project is more mature than the README suggests. The pipeline already
produces a bootable hybrid BIOS/EFI ISO from-source on Debian bookworm,
with multi-mirror APT cache, snapshot.debian.org pinning, dep-drift
verification against on-disk `.deb` files, a curses TUI with live widgets,
and a chroot-aware install path that handles `usrmerge`, debconf
pre-seeding, the libc bootstrap circular-dep, post-install patch overlays,
and an 8-check chroot verifier that gates ISO build.

**Strengths**

- Clear pipeline-stage architecture (`BuildFlags` gates each step).
- Comments and docstrings explain *why*, not just *what* — load-bearing
  invariants are documented inline (e.g. `_check_dep_drift`,
  `_build_chroot_directories`, snapshot resolution).
- Multi-mirror ingest (main / updates / security) with per-mirror SHA256
  gating and per-source `_mirror` stamping so downloads use the correct pool.
- Sound chroot bring-up: real chroot for `dpkg --configure -a` once dpkg is
  unpacked, chrootless fallback only for the bootstrap rounds.
- Tests + smoke driver exist (`tests/test_module.py`, `tests/smoke_dep_drift.py`).
- Reproducibility hook is in place via Snapshot pinning (off by default).

**Top risks (expanded as P0/P1 tasks below)**

1. **No GPG verification of `InRelease`** — `Cache.__get_files` trusts
   whatever `deb.debian.org` serves; a MITM or compromised mirror can
   inject arbitrary `Packages` entries.
2. **`MaxParallelBuilds` is a lie** — config field exists, parser reads it,
   `source_build` is strictly serial.
3. **`--force-depends` in `dpkg --configure -a`** is acknowledged
   `TEMPORARY` in code (`buildsystem.py:419`, `:428`) and silently masks
   real dep skew. Should fail loudly once snapshot pinning is the default.
4. **No APT-repo metadata** generated for the built `.debs` — `repo/` is a
   bag of files, not a usable apt source. Blocks the “build a derivative
   distribution” end-state.
5. **No installer image** — `iso build live` only produces a *live* ISO via
   `live-boot`. The README/user goal of "including installation" needs an
   installer ISO; design **pivoted 2026-05-10** to "rebuild d-i from source
   via a parallel udeb dep tree through our existing pipeline" (replaces
   the original "import vanilla d-i" plan). Full plan at
   `docs/plans/comp-01-installer.md`. Phase 1 done; tracked under
   COMP-01a..h.
6. **Module-level globals in `build.py`** (`build_config`, `build_cache`,
   `dependency_tree`, `build_container`, `console`, `_tui`) make the code
   hard to test and reason about; nothing else can drive the pipeline
   programmatically without standing up the whole TUI.
7. **`tui.console` plumbing** is split between `tui.console` (module) and
   `console` (in `build.py`); during early init the order of assignments
   determines who wins. Easy to break by accident.

---

## 1. Stability & correctness — P0 / P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| STA-18 | P3 | todo | **DependencyTree records the provider's `Version:` instead of the `Provides:` clause version for virtual-name aliases.**  Symptom: when package X declares `Provides: libfoo (= 1:N.N.N-...)`, our resolver registers `libfoo` in `selected_pkgs` keyed to X — but the version it associates with the `libfoo` name is X's *own* `Version:` field (which lacks the epoch the Provides clause carried).  Downstream constraint checks like `Depends: libfoo (>= 1:4.0)` then fail because the recorded version compares as epoch-0 against the dep's epoch-1 lower bound.  **Concrete case** (surfaced during GROUPS-01 phase 3 gnome build, 2026-05-14): `libgcc1` in bookworm doesn't exist as a real package — only as `Provides: libgcc1 (= 1:12.2.0-14+deb12u1)` from `libgcc-s1`.  `libwebrtc-audio-processing1` `Depends: libgcc1 (>= 1:4.0)`.  Our resolver records the `libgcc1` alias at version `12.2.0-14+deb12u1` (libgcc-s1's `Version:` field, epoch 0) — debian-version-compare puts that *below* `1:4.0` (epoch 1), and `parse_dependency` returns None with `WARNING: unresolved dependency 'libgcc1' for libwebrtc-audio-processing1`.  **Not blocking** — apt on /target resolves Provides+epoch correctly at install time, so the package actually installs.  The wart is purely in our internal dep-graph bookkeeping: one missing forward/reverse edge per affected alias.  **Fix**: when registering virtual-name aliases in `selected_pkgs`, parse the version *from the `Provides:` clause* (if a versioned Provides is declared) rather than falling back to the provider package's `Version:`.  Touch points: `package.py`'s Provides parser + wherever `dependencytree.py` does the alias registration (look for the `get_provides()` consumers near `parse_dependency` / `add_lookahead`).  Test coverage: a fixture with provider-V1 providing target-V2 + a downstream consumer requiring target >= V2 — confirm the consumer's dep resolves cleanly. |

## 2. Conformity to Debian/Ubuntu process — P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| CONF-01 | P1 | wip | After `source_build`, run `dpkg-scanpackages` / `apt-ftparchive` on `repo/` to generate `Packages`, `Packages.gz`, `Release`, `Release.gpg`, `InRelease`. Without this, the built bag of `.debs` is not a usable apt source for anyone (or any later install step) — this is the gate to a real derivative distro.  *(EXTRAS-01 has prepared the repo content — `repo/` now holds depth-1 Recommends beyond what the chroot installs.  **Partially shipped 2026-05-13** as `iso_installer._generate_apt_repo` + `_sign_release_files` (commit `42e03ea`): on installer-ISO build, `staging/pool/` gets `dpkg-scanpackages` + `dpkg-scansources` + `apt-ftparchive release` + signed `Release.gpg` / `InRelease`.  **Still open**: standalone `cmd_index_repo` that runs the same machinery against `repo/` directly (so the apt index is available outside an ISO build).  Lift the helpers from `iso_installer.py` into a shared module.)* |
| CONF-02 | P1 | wip | Generate and include a project-owned signing key; sign the built `Release` file. Document key rotation.  *(**Phase 1 done 2026-05-09**: `scripts/signing.py` (~210 LOC) wraps `gpg --batch --gen-key` and `gpg --detach-sign`/`--verify` to manage the keypair under `<dir_gnupg>/signing/` (separate gnupg homedir from the verification keyring at `<dir_gnupg>/` — different trust scopes).  Three operator commands: `generate_signing_key [force]` (refuses overwrite without force; PROMPT_YESNO confirmation; RSA-4096 / never-expires), `verify_signing_key` (sign+verify roundtrip against a test payload, prints OK/FAIL plus fingerprint+uid+created+expires), and `print signing` (state snapshot, no roundtrip).  New `[Repo] SigningKeyUid` config field (default `Athena Build <athena@local>`).  Public key exported to `<dir_gnupg>/signing/athena-archive-keyring.gpg` for downstream pickup.  8 unit tests incl. a real-gpg sign+verify integration roundtrip (RSA-2048 for ~3s test speed).  **Phase 3 done 2026-05-09**: `_install_signing_keyring` in `chroot.py` (called from `generate_system_configs`) `sudo cp`s the exported pubkey into the chroot at `/usr/share/keyrings/athena-archive-keyring.gpg` (the conventional Debian distro-signing-key location — Debian itself ships at `debian-archive-keyring.gpg` next to it).  `_verify_chroot` gains a non-gating informational line reporting keyring presence/absence at the verify step.  3 mocked-subprocess tests (skip-on-absent, cp-when-present, warn-on-failure).  **Plus build_chroot now gates on the signing key up front**: new `signing_key_verified` flag in `BuildFlags`; new `_ensure_signing_key_verified()` helper called at the very top of `cmd_build_chroot` (before any sudo/mount/dpkg work); helper runs `signing.verify_key` (real sign+verify roundtrip) and on success sets the flag + prints fingerprint/uid/created/expires; on failure prompts PROMPT_YESNO to generate now and re-verifies, refusing to proceed if operator declines.  Surfaced in `print state` as a stage row.  5 mocked tests cover all four branches (key present, decline, accept-then-success, accept-then-generate-fails) plus the flag's default-False invariant.  **Phase 2 partially shipped 2026-05-13** (commit `42e03ea`): installer ISO `Release` is signed end-to-end via `iso_installer._sign_release_files` (detached `.gpg` + clearsigned `InRelease`); `_export_pubkey_to_staging` ships pubkey at `.disk/archive-key.gpg`; `base-installer` quilt patch (`9001-install-athena-archive-keyring.patch`) copies it to `/target/etc/apt/trusted.gpg.d/` before `apt-cdrom add`.  Verified end-to-end on 2026-05-13 install run (`Hit:1 cdrom://... thor InRelease`).  **Still open**: wire signing into the standalone `cmd_index_repo` once CONF-01 lands (lift the helpers from `iso_installer.py` into a shared module).  **Sources.list.d wiring still open**: an `/etc/apt/sources.list.d/athena.list` entry with `[signed-by=...]` pointing at the keyring needs a real URL the booted system can reach — defer to COMP-02 (publish_repo) since the URL is meaningless without a publishing endpoint.  Key rotation via `generate_signing_key force` works today; docs/release.md (DOC-04) blocked on CONF-01..03 landing.)* |
| CONF-03 | P1 | todo | Honour the Debian source format: today `BuildContainer.build` calls `dpkg-source -x` then patches and runs `dpkg-buildpackage -us -uc -nc`. For a real distro derivative, the per-package patches should land in `debian/patches/` (quilt format) with a versioned `debian/changelog` entry, then `dpkg-buildpackage` produces a properly-named binNMU `.deb`. Document the chosen path (we are *not* doing it today) and decide whether to keep the current “patch outside debian/” approach. |
| CONF-06 | P2 | todo | Adopt `reprotest` (or equivalent) to verify built `.debs` are reproducible across two runs of `source_build` from the same snapshot. |
| CONF-07 | P2 | todo | Generate an SBOM (CycloneDX or SPDX) listing every source name, version, and Debian patchset hash that went into the build. Required for any “downstream distro” aspirations. |
| CONF-10 | P2 | todo | **Debian-residue audit sweep + durable handling mechanism.**  Two-part ticket: (1) one-time sweep to find every Debian-named artifact still surfacing on a fresh install (logs / config files / boot menu / `/etc/*-release` / `/etc/issue` / `/etc/motd` / `/etc/debian_version` / sources.list comments); (2) replace the current ad-hoc strip mechanism with one that doesn't rot on upstream rebuilds.  **Why the redesign:** today's `installer_chroot._strip_debian_residue_hooks` carries a hardcoded path list (`20install-hwpackages`, `50save-logs`).  If upstream renames a hook, splits it across files, or adds a NEW Debian-only hook in a future udeb rev, our hardcoded list silently misses it — same reactive cycle that produced the list in the first place.  **Sweep tokens to grep for:** `Debian`, `debian.org`, `discover`, `installation-report`, `reportbug`, `popularity-contest`, `popcon`, `apt-listchanges`, `bug-buddy`.  **Candidate durable mechanisms** (pick one in the redesign):  (a) **build-time audit + explicit allow-list**: parse every shipped `pre-pkgsel.d/*` (and similar hook trees) for `apt-install X` calls; cross-reference X against `repo/`; if X isn't in pool, FAIL the build with an actionable diagnostic unless the hook is on `installer/strip-hooks-allowlist`.  Forces explicit per-hit operator decision (strip / add-to-pool / acknowledge); catches new hooks automatically.  (b) **fork the offending udebs**: `fork/source/hw-detect/` + `fork/source/save-logs/` with Debian-only hooks removed in `debian/install`.  Most "Athena ships as Athena" pure but per-source maintenance cost.  (c) **wrap `apt-install`** in the installer ramdisk so a missing-pool target silently no-ops — makes any pre-pkgsel hook safe without modifying it.  Generic but masks failures we MIGHT want to see.  Lean: option (a) — fails loud at build time, list of stripped-by-design hooks is reviewable in `installer/strip-hooks-allowlist`, no runtime monkey-patching.  Memory entry `project_filter_debian_specific_installer_hooks.md` is the principle reference.  Tracked as fallout from the 2026-05-18 install runs; not install-correctness-blocking but visible on every fresh Thor install. |
| CONF-09 | P1 | todo | **Retire rebump; full +thor1 source rebuild after subset validation.**  The `+thor1` distro suffix was retrofitted onto the existing `repo/` via three recovery passes (rebump → epoch restore → strict-equal cross-ref rewrite).  Each pass exposed a new class of baked-in build-time assumptions the rebump approach can't see, and there's no guarantee we've found them all.  Plan: validate `BuildContainer.build`'s changelog-prepend on a 6-8 pkg subset (systemd, gmp, perl, glib2.0, libyaml, firefox-esr, binutils), assert sibling cross-refs resolve to +thor1 via `${binary:Version}` substvars, then commit to a full 24-36h source rebuild that produces a clean corpus.  After full rebuild: delete `utils.rebump_deb_file` / `restore_deb_epoch` / `rewrite_intra_thor1_strict_equals` and the `package rebump` command — they're retrofit-only and no longer needed.  Full plan at `docs/plans/conf-09-thor1-full-rebuild.md`. |
| CONF-08 | P2 | todo | **Lift `nodoc` from `[Source] BuildOptions` / `BuildProfiles`** (currently `nodoc, nocheck` — `nodoc` is a temporary workaround for missing doc-build tooling in the build pool, not a permanent policy).  Four `patch/source/` entries exist solely to make doc-related rule fragments nodoc-tolerant and should be deleted when this lifts: `libyaml/0.2.5-1/9001-skip-doxygen-under-nodoc.patch` (wraps `$(MAKE) html` + `dh_doxygen` in `ifeq nodoc`), `protobuf/3.21.12-3/9001-guard-doc-install-on-nodoc.patch` (defensive `[ ! -e ]` / `rm -f` for examples README + .gitignore), `p7zip/16.02+dfsg-8/9001-graceful-skip-when-manual-absent.patch` (exit 0 instead of exit 1 when manual absent), `wpa/2.10-12+deb12u3/9001-skip-examples-sed-under-nodoc.patch` (wraps `sed -i debian/*/usr/share/doc/*/examples/*.c` in `ifeq nodoc`).  All four preserve upstream behaviour when docs are present — verified.  Steps: (1) audit which doc-build tools (`doxygen`, sphinx, `texinfo`, gtk-doc-tools, etc.) need to be in the build pool / container; (2) add them to `config/installer.list` and/or pool.list as appropriate; (3) drop `nodoc` from `BuildOptions` and `BuildProfiles` in `config/build.conf`; (4) rebuild end-to-end; (5) `git rm` the four patch trees once builds pass. |

## 3. Completeness — P1 (toward derivative-distro state)

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| COMP-01 | P1 | todo | **Installer ISO** path. Today `iso build live` produces a live ISO only. *(architecture **pivoted 2026-05-10** from "import vanilla d-i" to "rebuild d-i runtime from source via parallel udeb dep tree through our existing pipeline" — full plan: `docs/plans/comp-01-installer.md`, project memory `project_installer_from_source.md`. Two distinct dep graphs (deb world + udeb world) over a single source corpus; same `source build` produces both `.deb` and `.udeb` outputs. Installer ramdisk is pure udeb closure (no debs, no systemd) — `rootskel`+busybox init runs cdebconf+main-menu+d-i step udebs. UI is `cdebconf-text-udeb`. Branding via cdebconf source patches + custom `athena-installer-theme-udeb`. `installer.list` is mixed-universe — udeb names AND deb names; resolver dispatches per-entry. Sub-phases COMP-01a..h below; total ~5-7 weeks elapsed.)* |
| COMP-01f | P1 | todo | **Installer Phase 6 — branding** (3-5d). Source patches under `patch/source/cdebconf/<version>/9001-athena-strings.patch` rewriting visible debconf templates "Debian"→"Athena" and setting cdebconf color theme. Custom source package `athena-installer-theme/` builds `athena-installer-theme-udeb` shipping boot splash PNG + color overrides. Placeholder graphics generated programmatically (ImageMagick 640×480 dark-bg with "Athena Installer"). Boot menu themed with same palette. Theme udeb added to installer.list. |
| COMP-01h | P1 | todo | **Installer Phase 8 — hardware testing** (1-2w). QEMU UEFI + BIOS smoke tests. Real hardware: at least one BIOS box + one UEFI box. Edge cases: small disk, no network, USB-only install media. Validate target boots cleanly after install completes. Treat as the gate before declaring COMP-01 done. *(VMware BIOS + EFI verified end-to-end through `finish-install.d/20final-message` 2026-05-14.  Real-hardware smoke + edge cases still pending.  Tracked in `docs/plans/comp-02-robust-build.md` § Phase F as the CI gate that would automate this.)* |
| COMP-02 | P1 | todo | **Repository publishing** — given CONF-01, expose a `publish_repo` command that copies `repo/` + generated metadata to a configured destination (local dir, S3-compatible bucket, rsync target). Without this the project cannot be *consumed*. |
| COMP-03 | P1 | todo | **Parallel `source_build`** — `MaxParallelBuilds` is read from config but ignored. Implement a worker-pool that respects build-dep ordering (a topological-sort batching like `BuildSystem.get_install_sequence` already exists; reuse it). |
| COMP-04 | P1 | todo | **Architecture support** beyond `amd64`. The code reads `arch` from config but `Dockerfile`, `pkg.list` (`linux-image-amd64`, `grub-efi-amd64`), and `build_iso` ISO name are amd64-hardcoded. Decide on second-arch target (likely `arm64`), parameterise. |
| COMP-06 | P1 | todo | **Package-set TUI** — today the user edits `config/pkg.list` by hand. Write a `select_packages` TUI command that lets the operator toggle packages with available metadata (size, deps), commits the result back to `pkg.list`. (Buildroot has this; we should match.) |
| COMP-07 | P2 | todo | **Cross-build container per release** — `Dockerfile` is currently hard-pinned to `bookworm`. Auto-rebuild a per-release image (`bookworm`, `trixie`, `noble`, …) when `CONTAINER_RELEASE` changes; keep them in parallel so the user can cross-target. |
| COMP-09 | P2 | todo | **Disk-image (raw / qcow2)** output alongside the ISO, for direct VM/cloud use. |
| COMP-11 | P2 | todo | **Distro abstraction**: `[Build] Distro = debian \| ubuntu` selects parallel artifact sets — `pkg.list.<distro>` (Ubuntu uses `casper` instead of `live-boot`, plus its own initramfs hooks and grub package names), `Dockerfile.<distro>` (`FROM ubuntu:${RELEASE}`), and the `os-release` ID/ID_LIKE/VENDOR_NAME fields (subsumes COMP-10).  `[Snapshot] Enabled = false` becomes the implicit default for non-Debian distros (no snapshot.d.o equivalent).  Cluster: ARCH-13 + COMP-11 + HK-05 = "distro-portability".  Hardest piece: maintaining two parallel pkg.lists in sync; consider a shared base + per-distro overlay file. |
| GROUPS-01 | P1 | wip | **pkg.list install-time groups** — operator-defined groups (`[base]`, `[development-tools]`, `[gnome]`, …) in `config/pkg.list`.  `[base]` is always installed (live image + target debootstrap); other groups ship in `/cdrom/pool` and the installer (tasksel) apt-installs the operator-selected subset on /target at install time.  **Phase 1 done 2026-05-14**: INI parser (`utils.parse_pkg_list_groups`) with backward-compat (flat pkg.list → implicit `[base]`); per-group resolution in Pass III with declaration-order respected (a package also reachable from an earlier group's closure is credited there, not duplicated); `DependencyTree.pkg_group_pkg_names` per-group canonical sets; `pkg_group_extras_pkg_names` (union of non-base groups) filtered from live install batches (`chroot.py:_compute_install_batches`) AND from `_base_include` (target debootstrap) but KEPT in `_pool_whitelist` (cdrom pool); `iso_installer._stage_group_manifests` writes `.disk/groups/<group>.list` per group; `print groups` view added.  7 new tests (275→282).  **Phase 2 done 2026-05-14**: generate tasksel `.desc` files at ISO-build time + installer pre-pkgsel hook + pkgsel/tasksel install-list wiring.  Shape: `iso_installer._stage_tasksel_desc()` writes `staging/.disk/athena-tasks.desc` with one RFC-822 stanza per non-`[base]` group (Section: athena, Description: from operator `## Description:` comment or fallback, Key: alpha-sorted seed names).  `utils.parse_pkg_list_group_meta()` parses `## Description: …` comments after `[group]` headers in pkg.list (sibling parser to parse_pkg_list_groups, no API break).  `installer/pkgsel/pre-pkgsel.d-athena-tasks` shell hook overlays into the installer chroot at `/usr/lib/pre-pkgsel.d/05-athena-tasks` via `_OVERLAY_MAP`; copies the .desc from /cdrom to /target/usr/share/tasksel/descs/ before pkgsel runs `in-target tasksel --new-install`.  `pkgsel` udeb added to `installer.list` (drives the Software-selection step at install time); `tasksel` deb added to pkg.list `[base]` so it's debootstrapped onto every /target.  8 new tests (282→290): meta parser (description extraction, flat-file fallback), tasksel .desc generation (RFC-822 stanzas, [base] skipped, operator description vs fallback, Key sorting), hook script exists + executable + correct cdrom→target path, OVERLAY_MAP entry, installer.list pkgsel pin, pkg.list [base] tasksel pin.  **Phase 3 done 2026-05-14**: end-to-end install validation tooling + example group.  Shape: (a) pre-pkgsel hook refactored to accept `ATHENA_TASKS_SRC` / `ATHENA_TASKS_DST_DIR` env-var overrides (defaults unchanged) so the smoke test can drive it without touching real `/cdrom` or `/target`.  (b) Two subprocess smoke tests pin the hook's install-time behaviour: copies the .desc to `<target>/usr/share/tasksel/descs/athena.desc` when source exists, exits 0 cleanly when source is absent.  (c) Pre-flight integrity check in `cmd_build_iso_installer` warns when a group resolves to zero canonical packages (operator typo'd every seed) or when non-base groups exist but `pkg_group_extras_pkg_names` is empty (declaration-order bug — all packages credited to [base]).  (d) Real example group `[development-tools]` (build-essential, git, vim, less, htop, strace) added to pkg.list with a `## Description:` — small enough for a tractable install test (~100 MB on top of [base]).  (e) `print groups` view now shows per-group repo size in MB by walking `repo/` for matching .debs.  3 new tests (290→293).  Pkgsel's pre-pkgsel.d invocation verified against the actual pkgsel 0.79 udeb (extracted + read its postinst at iso-build time): scans `/usr/lib/pre-pkgsel.d/*`, requires `-x`, runs in installer environment with /target mounted.  **Remaining manual step**: rebuild the ISO with `[development-tools]` declared, boot in QEMU, confirm tasksel surfaces `athena-development-tools` as a checkbox, install it, verify `dpkg -l` on /target shows the packages.  No code blocker for this; pure VM-time validation by the operator. |
| COMP-12 | P1 | wip | **Installer robustness pass** (`docs/plans/comp-02-robust-build.md`).  Distinct from the `COMP-02` row above (which is `publish_repo`) — this tracks the post-COMP-01 hardening work to take the installer ISO from "boots and starts d-i" to "completes a clean install end-to-end on both BIOS and EFI".  Phases A/B (stock-cdrom seed completeness, default-release helper) + Phase C (sign Release + ship pubkey + base-installer keyring patch, commit `42e03ea`) + Phase D (bin-only `grub-pc-bin` + `grub-efi-amd64-bin`, commit `875200e`) + Phase D follow-up (new `pool.list` mechanism for ship-but-don't-install packages — bootloader metas + `open-vm-tools` + `console-setup` + `keyboard-configuration` + `xkb-data`, commit `68266a2`) all shipped.  **Verified**: 2026-05-14 BIOS + EFI VMware installs both reach `finish-install.d/20final-message` cleanly.  **Phase E Deferred** items track cosmetic noise (intel-microcode pool entry, shim-signed pool entry, dpkg-divert warning silencing, mtab-symlink warning, brltty-udeb description fallback, etc.) — see plan doc for the full set with effort.  **Phase F (CI gate)** still open: harness in `tests/installer-smoke/` to boot the built ISO in QEMU and assert install reaches finish-install. |

## 4. Architecture & coding practices — P1 / P2

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| ARCH-14 | P3 | todo | TUI holistic rework — pulls together log-tab line wrapping (must reflow on resize, so render-time wrap, not append-time; would have shipped with ARCH-07 but punted because it belongs with a broader pass), keystroke routing for scroll-by-display-row vs scroll-by-record, theming, and any other accumulated TUI UX gaps.  Touchpoints today: `Tui._refreshtab` (currently one display row per buffer entry — clips long lines via `_safe_addstr`), `Tui._handle_key` KEY_UP/DOWN, `_TabEntry` schema if buffers need a richer per-entry shape. |
| ARCH-16 | P2 | todo | **Per-package `DEB_BUILD_OPTIONS` / `DEB_BUILD_PROFILES` overrides** in `config/build.conf`.  Today `[Source] BuildOptions` / `BuildProfiles` are global (`nodoc, nocheck`) and applied to every source build via `buildcontainer.py:253-256`.  Some packages need per-package tuning that doesn't fit the global axis — currently the workaround is a per-source `debian/rules` patch (cf. firefox-esr 9002 appending `parallel=1` for amd64).  Add a `[Source.<pkg>] BuildOptions = …` / `BuildProfiles = …` block (mirroring how `[Snapshot]` reads optional sub-keys); `BuildContainer.build()` checks for a per-source override and falls back to the global value when absent.  Surfaced 2026-05-15 when firefox-esr needed `parallel=1` to keep cc1plus under the 16 GB OOM threshold; the patch-based workaround is fine for one package but a wider mem-budget tuning surface (or any per-package profile tweak) would benefit from operator-facing config.  Scope: ~50 LOC across `utils.BuildConfig`, `buildcontainer.py`, plus 3-4 unit tests for the override-lookup and fallback paths. |

## 5. Tests & CI — P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| TEST-02 | P1 | todo | Add unit tests for `DependencyTree.parse_dependency` — auto-pick path, alt-deps, virtual-package resolution, version constraint propagation. None exist today. |
| TEST-05 | P1 | todo | Add a fixture `Cache` built from a tiny in-tree `Packages` / `Sources` blob so cache/dep-tree tests run offline. |
| TEST-07 | P2 | todo | Integration test: `cmd_auto_run` against a fixture mirror inside Docker, asserting the chroot 8-check verifier returns all green. Tag as `slow` so it only runs nightly / on demand. |
| TEST-08 | P2 | todo | Property test for `Mirror.with_snapshot` — no-op on `None`, idempotent, preserves `suite`. |

## 6. Documentation — P1 / P2

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| DOC-02 | P1 | todo | A `docs/architecture.md` describing the pipeline stages and `BuildFlags` contract. Today this knowledge is only in commit messages and inline docstrings. |
| DOC-03 | P1 | todo | A `docs/patching.md` formalising the `patch/source/<pkg>/<ver>/9001-*.patch` and `patch/{pre,post}-install/` conventions. The README has fragments. |
| DOC-04 | P2 | todo | `docs/release.md` describing how to cut a derivative distro release once CONF-01..03 land (signing key, snapshot timestamp, pkg.list freeze). |
| DOC-05 | P2 | todo | Move the inline “Installing Docker” block out of `README.md` into `docs/install-docker.md`. |
| DOC-06 | P2 | todo | Keep `README.md` in sync with the code as the project evolves.  When a pipeline stage is added/renamed, a default in `config/build.conf` changes, a new common failure mode appears (or an old one is fixed), or the operator workflow shifts — update the README in the same PR.  Periodic audit: when closing each ticket touching `scripts/` or `config/`, scan README §Building Image and amend if the change is operator-visible.  Footer note in the README points future-me here. |

## 7. Security & supply-chain — P0 / P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| SEC-05 | P2 | todo | The build container runs `apt-get install -y` on whatever the resolved build-deps are — without dep-graph review. Acceptable today, but add an opt-in “show me what is about to be installed” gate for hostile-mirror scenarios. |

## 8. Operator UX — P2 / P3

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| UX-04  | P2 | hold | Persist `BuildFlags` to disk between runs so a re-launched TUI can resume mid-pipeline. *(**On hold 2026-05-10** — first attempt (commits A `ceb3d1a` + B `a53eea9`, both reverted in `0398dd0` / `d919b2e`) shipped: BuildFlags JSON autosave, `Cache` + `DependencyTree` pickle to `<dir_cache>/{cache,deptree}.pkl.gz` with SHA256 sidecars, `cmd_resume` orchestrator, `--resume` flag in `build-system.sh`, Package/Source `__getstate__`/`__setstate__` to work around python-debian's weakref-bearing `OrderedSet`.  **Why reverted:** end-to-end resume time was roughly the same as building from scratch — the unpickle + dict-to-defaultdict rebuild + `_arch_table` regen + DT cache rewire on a real bookworm index (~98k packages, ~34k sources) costs about as much as parsing the on-disk Packages/Sources files we already have.  The pickle blob is also gigantic.  **What to try next instead:** define a perf budget first (target: resume within Xs of fresh-build time, X ≪ fresh build), then pick a different strategy — candidates: (a) write a compact per-stage cache as plain JSON/CBOR of just the parsed-out fields we actually use (skip the Deb822 wrapper entirely), (b) pickle only `DependencyTree` (smaller, slower to recompute) and rebuild `Cache` by re-parsing — Cache build is dominated by I/O which is already cached on disk, (c) memoise the slow-step inputs and rebuild lazily on access.  See reverted commits for the design surface to NOT re-do as-is.)* |
| UX-05  | P3 | todo | **Make the TUI / CLI more robust and flexible.**  *(Re-scoped 2026-05-14: previously "replace curses with Textual"; **explicitly NOT shifting to Textual** — the curses code is solid and we get more value from polishing it in-place than re-architecting.  ARCH-14 owns the structural rework (render-time line wrap, scroll-by-row vs scroll-by-record, theming, broader keystroke routing); UX-05 now owns the operator-facing flexibility gaps that have piled up.)*  Concrete sub-items worth filing as separate tickets when picked up: **(a) `--yes` auto-answer flag** for unattended runs (CI / scripted installs); **(b) `ATHENA_SUDO_PASSWORD` env-var pickup** so passwords never hit prompt buffers in scripted contexts; **(c) ProgressBar output throttling** in CLI mode so a fast inner loop doesn't flood stdout / log files; **(d) ANSI colour in CLI/TTY mode** mirroring the curses colour scheme so log-grep stays readable; **(e) `-c <cmd>` one-shot execution** (`./build-system.sh -c "cache build" -c "dep parse"`) for ad-hoc scripting without a REPL; **(f) prompt UX** — distinguish hard-required prompts (sudo password, conflict-resolution choices) from informational confirmations that could default-yes under `--yes`; **(g) graceful Ctrl+C** mid-resolve without corrupting `BuildFlags` state.  *(**Path B done 2026-05-09**: `--headless` flag in `build-system.sh` + `scripts/cli.py` (~280 LOC) implementing a Cli class that mirrors the Tui surface (print, INFO/WARNING/ERROR, add_widget/del_widget, console_mark/console_trim_to, prompt, register_command, run, wait, exit, sig_shutdown).  Cli registers as `tui.tui_instance` on construction so all 291 console.print + 10 Prompt + 12 register_command + Spinner/ProgressBar callsites work unchanged.  Stdout for operator output, stderr for diagnostic logger noise (separable via shell redirection).  ProgressBar gets `[start]`/`[done: N/M]` markers; Spinner keeps its own `… done` line.  17 unit tests pin the contract incl. a Console-facade end-to-end exercise that catches name-drift between facade and backend.  Path B is the foundation the (a)–(g) items above all build on top of.  Full research + Path B implementation outline in `~/.claude/plans/piped-tinkering-milner.md`.)* |
| UX-06  | P3 | todo | Localised messages — today everything is English-only. |

## 9. House-cleaning — P3

| ID     | Sev | Status | Title |
|--------|-----|--------|-------|

---

## How to update this file

- When you start a task, change its status to `wip` and (optionally) add a
  parenthetical with the commit / PR.
- When you finish, change to `done` and append the closing commit hash in
  italics on the same row.  **Then move the row to `docs/done.md`** under
  the matching section header — keeps this file focused on open work.
  Same drill for `wontfix`.  Do **not** delete the row outright; the
  embedded history (commit hashes, run notes, decision rationale) is the
  audit trail.
- New tasks: append to the relevant section here with the next free ID
  (`STA-18`, `COMP-11`, etc.). Never re-use an ID, including IDs that
  already live in `docs/done.md`.
- If priorities change, edit the `Sev` column; do not move rows between
  sections.

