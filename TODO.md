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
| CONF-07 | P2 | todo | Generate an SBOM (CycloneDX or SPDX) listing every source name, version, and Debian patchset hash that went into the build. Required for any “downstream distro” aspirations. |
| CONF-10 | P2 | todo | **Debian-residue audit sweep + durable handling mechanism.**  Two-part ticket: (1) one-time sweep to find every Debian-named artifact still surfacing on a fresh install (logs / config files / boot menu / `/etc/*-release` / `/etc/issue` / `/etc/motd` / `/etc/debian_version` / sources.list comments); (2) replace the current ad-hoc strip mechanism with one that doesn't rot on upstream rebuilds.  **Why the redesign:** today's `installer_chroot._strip_debian_residue_hooks` carries a hardcoded path list (`20install-hwpackages`, `50save-logs`).  If upstream renames a hook, splits it across files, or adds a NEW Debian-only hook in a future udeb rev, our hardcoded list silently misses it — same reactive cycle that produced the list in the first place.  **Sweep tokens to grep for:** `Debian`, `debian.org`, `discover`, `installation-report`, `reportbug`, `popularity-contest`, `popcon`, `apt-listchanges`, `bug-buddy`.  **Candidate durable mechanisms** (pick one in the redesign):  (a) **build-time audit + explicit allow-list**: parse every shipped `pre-pkgsel.d/*` (and similar hook trees) for `apt-install X` calls; cross-reference X against `repo/`; if X isn't in pool, FAIL the build with an actionable diagnostic unless the hook is on `installer/strip-hooks-allowlist`.  Forces explicit per-hit operator decision (strip / add-to-pool / acknowledge); catches new hooks automatically.  (b) **fork the offending udebs**: `fork/source/hw-detect/` + `fork/source/save-logs/` with Debian-only hooks removed in `debian/install`.  Most "Athena ships as Athena" pure but per-source maintenance cost.  (c) **wrap `apt-install`** in the installer ramdisk so a missing-pool target silently no-ops — makes any pre-pkgsel hook safe without modifying it.  Generic but masks failures we MIGHT want to see.  Lean: option (a) — fails loud at build time, list of stripped-by-design hooks is reviewable in `installer/strip-hooks-allowlist`, no runtime monkey-patching.  Memory entry `project_filter_debian_specific_installer_hooks.md` is the principle reference.  Tracked as fallout from the 2026-05-18 install runs; not install-correctness-blocking but visible on every fresh Thor install. |
| CONF-11 | P2 | todo | **Fork-tree completeness audit at cache-build.**  Surface latent breakages where a `fork/source/<pkg>/` tree is missing files that the upstream tarball ships and the package's own Makefile/debian-rules expect.  **Why:** discovered 2026-05-18 — `fork/source/athena-tasksel/packages/list` was missing from the fork (never tracked in git, probably deleted in an unrecorded cleanup pass shortly after the initial `apt-get source tasksel` seed); the original successful build's `athena-tasksel*.deb` survived in `repo/` and `check_build` skipped subsequent rebuilds, hiding the problem until step-7 prep deleted the existing artifact to force a fresh build.  Symptom: `install: cannot stat 'packages/list': No such file or directory` deep inside the Makefile install target.  Generalised: ANY fork pkg could have a similar latent gap — there's no automated check.  **Proposed mechanism:** at fork-mirror generation time, for each `fork/source/<pkg>/`, fetch the matching upstream tarball (we already pull it via `apt-get source` in the build container, or use the snapshot URL directly), extract to a temp dir, diff against the fork tree.  Emit a warning per missing-upstream file unless the file is on a per-package allow-list `fork/source/<pkg>/.upstream-omit` (one path per line + `# reason`).  Optional escalation: gate (fail cache-build) when missing files are referenced by `Makefile` / `debian/rules` / `debian/*.install`.  **Out of scope:** auto-restoring missing files (operator decision — could be a legitimate trim); diffing for files the fork HAS that upstream doesn't (those are the intentional delta).  Memory `feedback_verify_patch_layer_via_logs.md` is the related principle. |
| CONF-08 | P2 | todo | **Lift `nodoc` from `[Source] BuildOptions` / `BuildProfiles`** (currently `nodoc, nocheck` — `nodoc` is a temporary workaround for missing doc-build tooling in the build pool, not a permanent policy).  Four `patch/source/` entries exist solely to make doc-related rule fragments nodoc-tolerant and should be deleted when this lifts: `libyaml/0.2.5-1/9001-skip-doxygen-under-nodoc.patch` (wraps `$(MAKE) html` + `dh_doxygen` in `ifeq nodoc`), `protobuf/3.21.12-3/9001-guard-doc-install-on-nodoc.patch` (defensive `[ ! -e ]` / `rm -f` for examples README + .gitignore), `p7zip/16.02+dfsg-8/9001-graceful-skip-when-manual-absent.patch` (exit 0 instead of exit 1 when manual absent), `wpa/2.10-12+deb12u3/9001-skip-examples-sed-under-nodoc.patch` (wraps `sed -i debian/*/usr/share/doc/*/examples/*.c` in `ifeq nodoc`).  All four preserve upstream behaviour when docs are present — verified.  Steps: (1) audit which doc-build tools (`doxygen`, sphinx, `texinfo`, gtk-doc-tools, etc.) need to be in the build pool / container; (2) add them to `config/installer.list` and/or pool.list as appropriate; (3) drop `nodoc` from `BuildOptions` and `BuildProfiles` in `config/build.conf`; (4) rebuild end-to-end; (5) `git rm` the four patch trees once builds pass. |
| CONF-15 | P1 | todo | **Pin Dockerfile toolchain layer to our snapshot, not the live Debian mirror.**  `config/Dockerfile`'s toolchain `RUN apt-get install …` uses the base image's stock sources.list (= `deb.debian.org`).  When the live mirror advances past our snapshot pin (`[Snapshot] Timestamp` in `build.conf`), any pkg pre-installed in the image is at a **newer** version than what our snapshot/repo ship.  Per-build `apt-get install` won't downgrade an already-installed pkg, so the build container runs against a hybrid universe: most build-deps from snapshot, a handful of pre-installed library pkgs from a future Debian.  `dpkg-shlibdeps` then reads `.shlibs` files from the pre-installed (newer) lib and embeds a minimum-version constraint pointing at a version we don't ship.  Drove the 2026-05-23 false-bind where freshly-built `wpasupplicant-udeb` carried `Depends: libcrypto3-udeb (>= 3.0.20)` while our libcrypto3-udeb shipped at `3.0.19-1` — `source verify` mis-reported this as a stale .deb when in fact the build container itself was contaminated.  **Two-layer workaround shipped 2026-05-23** (commits `a66d650` + follow-on): (1) removed every library `-dev` pkg from the Dockerfile preinstall list so per-build apt picks them fresh from snapshot; (2) added `apt-get -y --allow-downgrades upgrade` to every per-build command sequence — runs AFTER the sources.list rewrite but BEFORE build-dep install, so the container's already-installed library set (libssl3, libc6, etc. pulled in by `debian:bookworm-slim`) is downgraded to snapshot's view before `dpkg-shlibdeps` reads any `.shlibs`.  `upgrade` (not `dist-upgrade`) keeps the impact bounded to version changes on already-installed names; no add/remove.  Per-build cost: ~5-10s of apt download + install on first run after a snapshot advance, then near-instant (apt's cache is warm).  **Real fix**: thread `${SNAPSHOT_BASEURL}/${ARCHIVE_KEY}/${SNAPSHOT_TS}/dists/${RELEASE}` through as a Dockerfile ARG, write `/etc/apt/sources.list` BEFORE the `apt-get install` so even the toolchain comes from snapshot.  Bake the snapshot-TS prefix into the image tag (`athenalinux:build-bookworm-<ts8>`) so a snapshot advance invalidates the image cache and forces a fresh build.  Once that lands, the per-build `--allow-downgrades upgrade` becomes a no-op and can be deleted.  Memory: `feedback_strip_nmu_at_build.md` documents the broader pristine-upstream invariant that this drift violates. |
| CONF-14 | P2 | todo | **Fork-version scheme — bump mechanics for `<upstream>+athenaN` → `<base>+athenaN`.**  Today's same-name forks (FORK-01 Path X — `base-files` 12.4+deb12u14+athena1 → +athena2) preserve the upstream NMU suffix in the fork version so the collision gate (`Version(upstream) >= Version(fork)` fails the build) accepts our value.  Concretely: upstream pool has `base-files_12.4+deb12u14`; we ship `12.4+deb12u14+athena1` (strictly greater via string-compare on `+athena1` vs end-of-string).  This works but bakes the upstream `+deb12u14` into our version forever — every upstream security release would require a rebase to `12.4+deb12u15+athena1` (then athena2…), threading upstream bump granularity into our changelog.  **What we want eventually:** `12.4+athena1` (pristine upstream version + our suffix only) — matches the cleaner half of the Devuan/Parrot/Mint convention.  **The hiccup:** `+athena1` < `+deb12uN` in Debian version order (`a` < `d`), so the collision gate would fail.  **Three candidate fixes**, each with its own cost: (a) **NMU-strip recognises +athenaN as a strippable layer** — post-strip upstream version is `12.4` (we already strip +debNuN), upstream `12.4+deb12u14` → `12.4`, our `12.4+athena1` → `12.4` post-strip, neither dominates the other (equal).  Collision gate would need to gain "equal is OK as long as the fork is the kept record" semantics.  (b) **Use a suffix starting with a letter > 'd'** (e.g. `+thor1`, `+xathena1`) so `+xathena1` > `+deb12uN` purely lexicographically.  Reads weird; future readers wonder why the prefix.  (c) **Use Debian epoch** — `1:12.4+athena1` outranks any non-epoch upstream version.  Kali does this.  Nuclear option but unambiguous.  Lean: (a) — most honest, preserves the pristine-upstream invariant we already enforce post-strip, only requires loosening the collision gate.  Tracks the broader "version-scheme cleanup" theme; tied to CONF-13 (upstream-NMU gate) which already special-cases the strippable-suffix family. |

## 3. Completeness — P1 (toward derivative-distro state)

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| COMP-01 | P1 | todo | **Installer ISO** path. Today `iso build live` produces a live ISO only. *(architecture **pivoted 2026-05-10** from "import vanilla d-i" to "rebuild d-i runtime from source via parallel udeb dep tree through our existing pipeline" — full plan: `docs/plans/comp-01-installer.md`, project memory `project_installer_from_source.md`. Two distinct dep graphs (deb world + udeb world) over a single source corpus; same `source build` produces both `.deb` and `.udeb` outputs. Installer ramdisk is pure udeb closure (no debs, no systemd) — `rootskel`+busybox init runs cdebconf+main-menu+d-i step udebs. UI is `cdebconf-text-udeb`. Branding via debconf-overrides + companion udeb (no upstream source patches — see `docs/branding-methodology.md`). `installer.list` is mixed-universe — udeb names AND deb names; resolver dispatches per-entry. Sub-phases COMP-01a..h below; total ~5-7 weeks elapsed.)* |
| COMP-01h | P1 | todo | **Installer Phase 8 — hardware testing** (1-2w). QEMU UEFI + BIOS smoke tests. Real hardware: at least one BIOS box + one UEFI box. Edge cases: small disk, no network, USB-only install media. Validate target boots cleanly after install completes. Treat as the gate before declaring COMP-01 done. *(VMware BIOS + EFI verified end-to-end through `finish-install.d/20final-message` 2026-05-14.  Real-hardware smoke + edge cases still pending.  Tracked in `docs/plans/comp-02-robust-build.md` § Phase F as the CI gate that would automate this.)* |
| COMP-02 | P1 | todo | **Repository publishing** — given CONF-01, expose a `publish_repo` command that copies `repo/` + generated metadata to a configured destination (local dir, S3-compatible bucket, rsync target). Without this the project cannot be *consumed*. |
| COMP-03 | P1 | todo | **Parallel `source_build`** — `MaxParallelBuilds` is read from config but ignored. Implement a worker-pool that respects build-dep ordering (a topological-sort batching like `BuildSystem.get_install_sequence` already exists; reuse it). |
| COMP-04 | P1 | todo | **Architecture support** beyond `amd64`. The code reads `arch` from config but `Dockerfile`, `pkg.list` (`linux-image-amd64`, `grub-efi-amd64`), and `build_iso` ISO name are amd64-hardcoded. Decide on second-arch target (likely `arm64`), parameterise. |
| COMP-07 | P2 | todo | **Cross-build container per release** — `Dockerfile` is currently hard-pinned to `bookworm`. Auto-rebuild a per-release image (`bookworm`, `trixie`, `noble`, …) when `CONTAINER_RELEASE` changes; keep them in parallel so the user can cross-target. |
| COMP-11 | P2 | todo | **Distro abstraction**: `[Build] Distro = debian \| ubuntu` selects parallel artifact sets — `pkg.list.<distro>` (Ubuntu uses `casper` instead of `live-boot`, plus its own initramfs hooks and grub package names), `Dockerfile.<distro>` (`FROM ubuntu:${RELEASE}`), and the `os-release` ID/ID_LIKE/VENDOR_NAME fields (subsumes COMP-10).  `[Snapshot] Enabled = false` becomes the implicit default for non-Debian distros (no snapshot.d.o equivalent).  Cluster: ARCH-13 + COMP-11 + HK-05 = "distro-portability".  Hardest piece: maintaining two parallel pkg.lists in sync; consider a shared base + per-distro overlay file. |

## 4. Architecture & coding practices — P1 / P2

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|

## 5. Tests & CI — P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|

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

