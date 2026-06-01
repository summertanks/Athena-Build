# Athena-Build — Project TODO

> Serialized, trackable backlog produced from a maturity / stability /
> completeness review of the source tree at the date below. Each task has a
> stable ID (`AREA-NN`); cite the ID when committing or referencing work.
> Update the **Status** column inline rather than renumbering.

- **Review date:** 2026-05-07 (initial), maintenance pass 2026-05-14 (`master` @ `37cabc9`), consolidation audit 2026-05-21 (`master` @ `a1e193c`), maintenance pass 2026-05-25 (`master` @ `564cf19`)
- **Reviewer:** Claude (read-only audit, with subsequent maintenance reconciling actual state vs ticket status)
- **Tree size:** ~40,100 LOC (scripts/ + tests/) — 21 scripts modules + an 11-file `tui/` package, single-file test suite (~15.9k LOC, 473 tests)
- **Pipeline state:** end-to-end working — `cache build → dep parse →
  source sync → container init → source build → chroot build
  (+verify) → iso build live → iso build installer`.  Installer ISO
  reaches `finish-install` cleanly on VMware BIOS + EFI as of
  2026-05-14 (commit `68266a2`); real-hardware validation still
  pending under COMP-01h.  Identity/branding pass (base-files
  same-name fork, GRUB + wallpaper + os-release) verified on a
  fresh install 2026-05-21 (working tag `working-branding-pathx-2026-05-21`).
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
6. **~~Module-level globals in `build.py`~~** — resolved by ARCH-01;
   pipeline state now lives on `BuildSession` (cache / dep_tree /
   container / config / flags are instance attrs).  Console facade
   ownership also normalised — `tui.console` is the single surface;
   `cli.py` registers as `tui.tui_instance` so `--headless` mode works
   through the same facade.  Both risks retired 2026-05-09 ↔ 2026-05-14.

---

## 1. Stability & correctness — P0 / P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|

## 2. Conformity to Debian/Ubuntu process — P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| CONF-03 | P1 | todo | Honour the Debian source format: today `BuildContainer.build` calls `dpkg-source -x` then patches and runs `dpkg-buildpackage -us -uc -nc`. For a real distro derivative, the per-package patches should land in `debian/patches/` (quilt format) with a versioned `debian/changelog` entry, then `dpkg-buildpackage` produces a properly-named binNMU `.deb`. Document the chosen path (we are *not* doing it today) and decide whether to keep the current “patch outside debian/” approach. |
| CONF-06 | P2 | todo | Adopt `reprotest` (or equivalent) to verify built `.debs` are reproducible across two runs of `source_build` from the same snapshot. |
| CONF-08 | P2 | todo | **Lift `nodoc` from `[Source] BuildOptions` / `BuildProfiles`** (currently `nodoc, nocheck` — `nodoc` is a temporary workaround for missing doc-build tooling in the build pool, not a permanent policy).  Four `patch/source/` entries exist solely to make doc-related rule fragments nodoc-tolerant and should be deleted when this lifts: `libyaml/0.2.5-1/9001-skip-doxygen-under-nodoc.patch` (wraps `$(MAKE) html` + `dh_doxygen` in `ifeq nodoc`), `protobuf/3.21.12-3/9001-guard-doc-install-on-nodoc.patch` (defensive `[ ! -e ]` / `rm -f` for examples README + .gitignore), `p7zip/16.02+dfsg-8/9001-graceful-skip-when-manual-absent.patch` (exit 0 instead of exit 1 when manual absent), `wpa/2.10-12+deb12u3/9001-skip-examples-sed-under-nodoc.patch` (wraps `sed -i debian/*/usr/share/doc/*/examples/*.c` in `ifeq nodoc`).  All four preserve upstream behaviour when docs are present — verified.  Steps: (1) audit which doc-build tools (`doxygen`, sphinx, `texinfo`, gtk-doc-tools, etc.) need to be in the build pool / container; (2) add them to `config/installer.list` and/or pool.list as appropriate; (3) drop `nodoc` from `BuildOptions` and `BuildProfiles` in `config/build.conf`; (4) rebuild end-to-end; (5) `git rm` the four patch trees once builds pass. |
| CONF-14 | P2 | todo | **Fork-version scheme — bump mechanics for `<upstream>+athenaN` → `<base>+athenaN`.**  Today's same-name forks (FORK-01 Path X — `base-files` 12.4+deb12u14+athena1 → +athena2) preserve the upstream NMU suffix in the fork version so the collision gate (`Version(upstream) >= Version(fork)` fails the build) accepts our value.  Concretely: upstream pool has `base-files_12.4+deb12u14`; we ship `12.4+deb12u14+athena1` (strictly greater via string-compare on `+athena1` vs end-of-string).  This works but bakes the upstream `+deb12u14` into our version forever — every upstream security release would require a rebase to `12.4+deb12u15+athena1` (then athena2…), threading upstream bump granularity into our changelog.  **What we want eventually:** `12.4+athena1` (pristine upstream version + our suffix only) — matches the cleaner half of the Devuan/Parrot/Mint convention.  **The hiccup:** `+athena1` < `+deb12uN` in Debian version order (`a` < `d`), so the collision gate would fail.  **Three candidate fixes**, each with its own cost: (a) **NMU-strip recognises +athenaN as a strippable layer** — post-strip upstream version is `12.4` (we already strip +debNuN), upstream `12.4+deb12u14` → `12.4`, our `12.4+athena1` → `12.4` post-strip, neither dominates the other (equal).  Collision gate would need to gain "equal is OK as long as the fork is the kept record" semantics.  (b) **Use a suffix starting with a letter > 'd'** (e.g. `+thor1`, `+xathena1`) so `+xathena1` > `+deb12uN` purely lexicographically.  Reads weird; future readers wonder why the prefix.  (c) **Use Debian epoch** — `1:12.4+athena1` outranks any non-epoch upstream version.  Kali does this.  Nuclear option but unambiguous.  Lean: (a) — most honest, preserves the pristine-upstream invariant we already enforce post-strip, only requires loosening the collision gate.  Tracks the broader "version-scheme cleanup" theme; tied to CONF-13 (upstream-NMU gate) which already special-cases the strippable-suffix family. |

## 3. Completeness — P1 (toward derivative-distro state)

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| COMP-01 | P1 | todo | **Installer ISO** path. Today `iso build live` produces a live ISO only. *(architecture **pivoted 2026-05-10** from "import vanilla d-i" to "rebuild d-i runtime from source via parallel udeb dep tree through our existing pipeline" — full plan: `docs/plans/comp-01-installer.md`, project memory `project_installer_from_source.md`. Two distinct dep graphs (deb world + udeb world) over a single source corpus; same `source build` produces both `.deb` and `.udeb` outputs. Installer ramdisk is pure udeb closure (no debs, no systemd) — `rootskel`+busybox init runs cdebconf+main-menu+d-i step udebs. UI is `cdebconf-text-udeb`. Branding via debconf-overrides + companion udeb (no upstream source patches — see `docs/branding-methodology.md`). `installer.list` is mixed-universe — udeb names AND deb names; resolver dispatches per-entry. Sub-phases COMP-01a..h below; total ~5-7 weeks elapsed.)* |
| COMP-01h | P1 | todo | **Installer Phase 8 — hardware testing** (1-2w). QEMU UEFI + BIOS smoke tests. Real hardware: at least one BIOS box + one UEFI box. Edge cases: small disk, no network, USB-only install media. Validate target boots cleanly after install completes. Treat as the gate before declaring COMP-01 done. *(VMware BIOS + EFI verified end-to-end through `finish-install.d/20final-message` 2026-05-14.  Real-hardware smoke + edge cases still pending.  Tracked in `docs/plans/comp-02-robust-build.md` § Phase F as the CI gate that would automate this.)* |
| COMP-04 | P1 | todo | **Architecture support** beyond `amd64`. The code reads `arch` from config but `Dockerfile`, `pkg.list` (`linux-image-amd64`, `grub-efi-amd64`), and `build_iso` ISO name are amd64-hardcoded. Decide on second-arch target (likely `arm64`), parameterise. |
| COMP-07 | P2 | todo | **Cross-build container per release** — `Dockerfile` is currently hard-pinned to `bookworm`. Auto-rebuild a per-release image (`bookworm`, `trixie`, `noble`, …) when `CONTAINER_RELEASE` changes; keep them in parallel so the user can cross-target. |
| COMP-11 | P2 | todo | **Distro abstraction**: `[Build] Distro = debian \| ubuntu` selects parallel artifact sets — `pkg.list.<distro>` (Ubuntu uses `casper` instead of `live-boot`, plus its own initramfs hooks and grub package names), `Dockerfile.<distro>` (`FROM ubuntu:${RELEASE}`), and the `os-release` ID/ID_LIKE/VENDOR_NAME fields (subsumes COMP-10).  `[Snapshot] Enabled = false` becomes the implicit default for non-Debian distros (no snapshot.d.o equivalent).  Cluster: ARCH-13 + COMP-11 + HK-05 = "distro-portability".  Hardest piece: maintaining two parallel pkg.lists in sync; consider a shared base + per-distro overlay file. |
| INST-01 | P2 | todo | **Installer i18n preseeding + non-en_US locale set.**  Today the language step is interactive at boot (`installer/preseed.cfg` ships no `debian-installer/locale`); installer strings are English-only (`S40-athena-branding` applies `@DISTRIBUTION@` substitutions only, no locale-driven template variants).  Preseed default locale, ship a chosen locale set (en_US + 2-3 candidates — pick from operator preference), pre-bake keymap defaults.  Reuses the existing `fork/source/athena-installer-data/data/S40-athena-branding` mechanism — adds locale overrides alongside the existing string overrides, no new infrastructure.  Touches `config/pool.list` (locale udebs).  Stays inside newt — no front-end change.  Filed 2026-05-29 from the comparative-analysis capability-gap audit (vs elementary / Pop / Mint / similar). |
| INST-02 | P2 | todo | **Installer accessibility — speakup + brltty.**  No `speakup` (screen reader) or `brltty` (braille) udebs in the installer closure today (`config/pool.list` / `config/pkg.list` mark a11y as deferred).  Adds the udebs, documents boot-menu shortcut (`s` for speakup at GRUB), validates with a screen-reader smoke pass under QEMU.  Real accessibility gap vs stock Debian d-i.  Touches `config/pool.list` + `installer/grub.cfg` boot menu.  Depends structurally on INST-01 (accessible prompts work better in user's preseeded locale).  Note ISO size cost — verify against current ISO budget before committing.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |
| INST-03 | P2 | todo | **Guided partition recipe — "Erase disk and install Asgard".**  Vanilla partman today: `installer/preseed.cfg` has zero partman entries; operator gets the full manual / guided picker.  Adds a custom partman recipe ("erase whole disk, single root, swap file, EFI on UEFI / BIOS-boot on legacy") + preseed defaults for the common path; keeps the manual mode reachable.  Removes the steepest cliff in the install flow today.  Stays inside d-i's partman — no front-end change.  New `fork/source/athena-installer-data/data/partman-recipe-asgard`.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |
| INST-04 | P3 | todo | **Front-end uplift investigation — graphical d-i vs Calamares.**  Newt is text-only; vs elementary / Pop / Mint / similar's polished GUI installers this is the most visible UX delta.  Investigation only at this stage — scope the gtk-frontend rebuild cost (additional udeb closure, Asgard branding through gtk theming, accessibility implications) vs replacing d-i with a Calamares fork.  No code change; writes a separate plan file under `docs/plans/`.  Picked up only after INST-01..03 land — those define the "do we still need this?" bar.  Most likely the way to spend XL effort for diminishing UX returns if started prematurely.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |

## 4. Architecture & coding practices — P1 / P2

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|

## 5. Tests & CI — P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| CI-01 | P1 | todo | **Wire `tests/installer_smoke/run.py` (quick mode) into nightly CI.**  Harness exists today (QEMU-based, quick = ~3min "does d-i boot cleanly?", full = ~15-30min unattended preseed install) but is not in the CI loop — operator must invoke manually.  Add a `.github/workflows/installer-smoke.yml` (scheduled nightly + manual dispatch) that fetches the latest published ISO and runs `tests/installer_smoke/run.py --mode quick`; gate failures.  Defer full-mode integration until quick-mode has been stable a week.  Reuses everything in `tests/installer_smoke/`; only new code is the workflow yaml + ISO-fetch step.  Highest leverage per LOC of any open item — catches d-i boot regressions before they reach an operator.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |
| AUDIT-02 | P2 | todo | **Reproducibility gate — `cmd source reproduce <pkg>` + nightly subset.**  Picks 3-5 representative forked sources (one heavy with `dh_strip` invariants e.g. `linux`; one fork with patch overlay e.g. `base-files`; one pure-data fork e.g. `athena-tasksel`), builds each twice in clean COMP-03 scratch dirs, runs `diffoscope` on the resulting `.deb` pair, fails on non-identical output.  Reuses `BuildContainer.build()` end-to-end + the per-worker scratch dir from COMP-03 (no race between concurrent reproduce runs).  Schedule via nightly CI job (separate from the per-commit gate — diffoscope is slow).  More concrete than CONF-06's general "adopt reprotest" — narrower scope (3-5 sources, not the full corpus), specific reproducer command operator can run locally.  CONF-06 stays open as the eventual full-corpus version.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |

## 6. Documentation — P1 / P2

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| DOC-06 | P2 | todo | Keep `README.md` in sync with the code as the project evolves.  When a pipeline stage is added/renamed, a default in `config/build.conf` changes, a new common failure mode appears (or an old one is fixed), or the operator workflow shifts — update the README in the same PR.  Periodic audit: when closing each ticket touching `scripts/` or `config/`, scan README §Building Image and amend if the change is operator-visible.  Footer note in the README points future-me here. |

## 7. Security & supply-chain — P0 / P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|

## 8. Operator UX — P2 / P3

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| UX-06  | P3 | todo | Localised messages — today everything is English-only. |
| OBS-01 | P2 | todo | **Per-source structured metrics — JSON sidecar.**  Today `log/build/<pkg>` carries raw container stdout and `<pkg>.result` is a single-line `PASS`/`FAIL`.  Wall clock per source is not captured; only the whole `AutorunTiming` session (`scripts/print_commands.py:31`) is timed.  Adds `time.monotonic()` brackets around `BuildContainer.build()` and writes `log/build/<pkg>.metrics.json` per build: `{started, finished, elapsed_seconds, status, exit_code, oom_killed, patch_set_hash, source_version, output_count}`.  Surfaces in `print summary` as a per-source elapsed column.  Foundation for OBS-02 / OBS-03 — schema is the contract.  Independent of CI / installer work, low risk.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |
| OBS-02 | P2 | todo | **Persistent build history — append-only ledger.**  Aggregates OBS-01 records across runs into `log/build-history.jsonl` (one line per (source, run, timestamp)).  New `cmd build history [pkg]` queries the ledger: per-package failure frequency, last N runs, rolling pass rate.  Answers "what's been flaky for the last month?" — unanswerable today.  Apply the publish-before-prune discipline from UPD-01's remote ledger when sizing the rotation strategy (memory `project_upd01_update_architecture`).  Depends on OBS-01 schema.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |
| OBS-03 | P2 | todo | **Resource telemetry per build.**  COMP-03 surfaces OOM only on exit-code-137 (post-failure hint).  Adds a poll thread (`docker stats --no-stream` every ~2s during `build()`) capturing peak RSS, time-in-state, sampled CPU; merges into OBS-01 metrics JSON.  Helps tune `BuildCpus` / `BuildMemory` / `HeavyPackages` empirically instead of by feel.  Depends on OBS-01.  Cleanup tied into `_deregister_live` so the poller can't outlive the container.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |

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

