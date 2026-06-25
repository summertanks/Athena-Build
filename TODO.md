# Athena-Build — Project TODO

> Serialized, trackable backlog produced from a maturity / stability /
> completeness review of the source tree at the date below. Each task has a
> stable ID (`AREA-NN`); cite the ID when committing or referencing work.
> Update the **Status** column inline rather than renumbering.

- **Review date:** 2026-05-07 (initial), maintenance pass 2026-05-14 (`master` @ `37cabc9`), consolidation audit 2026-05-21 (`master` @ `a1e193c`), maintenance pass 2026-05-25 (`master` @ `564cf19`), maintenance pass 2026-06-12 (`master` @ `6598c93`), **full-tree code review 2026-06-12** (8 parallel deep-review agents over every module + cross-cutting seams, all P0/P1 findings hand-verified against source; `master` @ `479d950`) — filed SEC-07, STA-27..53, ARCH-19, UX-08/09, HK-07.  Maintenance pass 2026-06-22 — verified the session-memory backlog against current source and filed the survivors: SELECT-01/02, PERF-01, FED-01/02, SUP-01, DIST-01; dropped four as already-done/fixed (virtual arch-gate → arch_filter.py 14edc2e, build-dep provider-expansion → package.py a37727f, dep-drift removal audit → dep_drift.py WARN, Case-C +asg re-normalise of the 19 sources → complete + tool removed in f5d3534)
- **Reviewer:** Claude (read-only audit, with subsequent maintenance reconciling actual state vs ticket status)
- **Tree size:** ~79,900 LOC (scripts/ + tests/) — 31 top-level scripts modules + `commands`/`coord`/`tui`/`webapi` packages (69 `.py` files), single-file test suite (~34.2k LOC, 1086 tests)
- **Pipeline state:** end-to-end working — `cache build → dep parse →
  source sync → container init → source build → chroot build
  (+verify) → iso build live → iso build installer`.  Installer ISO
  reaches `finish-install` cleanly on VMware BIOS + EFI as of
  2026-05-14 (commit `68266a2`); real-hardware validation still
  pending under COMP-01h.  Identity/branding pass (base-files
  same-name fork, GRUB + wallpaper + os-release) verified on a
  fresh install 2026-05-21 (working tag `working-branding-pathx-2026-05-21`).
  Since 2026-06-11 the three shipped surfaces are composed as
  independent reachability closures (SURFACES-01 in `docs/done.md`):
  live = GNOME closure (~1105 pkgs), disk = own minimal chroot
  (~245 pkgs via `chroot build disk` + `iso build disk`), installer
  ISO pool = manifest-driven (~1252 vs 1569 legacy; unreachable
  pool.list entries are mirror-only).  All three artifacts
  boot-verified 2026-06-11.  Publishing carries per-source lifecycle
  (LEDGER-01) and a byte-reclaim escape hatch (RECLAIM-01), both in
  `docs/done.md`.
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

1. **~~No GPG verification of `InRelease`~~** — resolved by STA-01:
   `utils.verify_inrelease()` + `[Security]` config gate every fetched
   `InRelease` against a pinned keyring before `Packages` / `Sources` are
   trusted (in `docs/done.md`).
2. **~~`MaxParallelBuilds` is a lie~~** — resolved by COMP-03: source
   builds now run in `ThreadPoolExecutor` with `[Build] MaxParallelBuilds`
   honoured, per-worker scratch dirs, and `HeavyPackages` serialisation
   for memory hogs (in `docs/done.md`).
3. **~~`--force-depends` in `dpkg --configure -a`~~** — resolved by
   STA-02: removed from `_configure_chroot`; STA-03 snapshot pinning +
   `_check_dep_drift` / `_verify_dep_resolution` catch unresolved deps
   before configure (in `docs/done.md`).
4. **~~No APT-repo metadata~~** — resolved by COMP-02 + MIRROR-01:
   `repo index full|minimal` produces signed `InRelease` over the local
   pool; `mirror publish` does per-file additive push + Ed25519-signed
   claims + tier-1 GPG `coord-head` to every configured peer.  See
   [`docs/mirror-setup.md`](docs/mirror-setup.md).
5. **~~No installer image~~** — installer ISO ships since 2026-05-14
   (COMP-01a..g done; reaches `finish-install.d/20final-message` cleanly
   on VMware BIOS + EFI).  Architecture **pivoted 2026-05-10** to
   "rebuild d-i from source via a parallel udeb dep tree through our
   existing pipeline" (replaces the original "import vanilla d-i" plan;
   full plan at `docs/plans/comp-01-installer.md`).  COMP-01h
   (real-hardware testing) is the only sub-phase still open.
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
| SELECT-01 | P2 | todo | **SELECT-LOCK restore fidelity — store raw list bytes.**  `cache restore` regenerates `config/{pkg,live,installer,pool}.list` from the lockfile's parsed seed NAMES only (`selection_lock.restore_list_files` / `render_pkg_list` / `render_flat_list`), so (a) comments are destroyed (pool.list lost ~156 operator-doc lines) and (b) pkg-group order is ALPHABETIZED because `write_selection_state` serialises via `json.dumps(sort_keys=True)`.  The reorder is NOT cosmetic — group order changes the closure (+xterm +libutempter0; see SELECT-02).  Fix: capture `seeds_raw` (verbatim bytes of each list file) in `assemble_state`; `restore_list_files` writes it byte-for-byte (fall back to the name-render for older lockfiles); the closure guard keeps using the parsed seeds.  ~5-10 KB on a ~155 KB lockfile.  Removes the TRIGGER for the closure drift.  (Filed 2026-06-22 from session backlog.) |
| SELECT-02 | P2 | todo | **Order-independent OR-dependency resolution.**  `dependencytree.py` (~582-614) resolves `a \| b` OR-groups in a SINGLE greedy pass against the IN-PROGRESS closure ("use an already-selected alt, else pull the first declared"), so the resolved closure depends on seed ORDER, not just the set — a determinism worry.  Reproduced: `xorg` Depends `xterm \| x-terminal-emulator`; `gnome-terminal` Provides `x-terminal-emulator` — pkg.list group order flips whether xterm (+ its dep libutempter0) is pulled (1580 vs 1582 bins).  Fix: defer ORs, resolve each against the FINAL closure to fixpoint (Pass A = seeds + hard Depends/Pre-Depends; Pass B = ORs satisfied if ANY alt in closure else first; iterate to fixpoint).  Makes the closure a pure function of the SET.  Risk: deferring can legitimately drop OTHER currently-pulled first-alternatives → needs old-vs-new closure diff + many-ordering verification.  Removes the CAUSE (SELECT-01 removes the trigger).  (Filed 2026-06-22 from session backlog.) |
| PERF-01 | P3 | todo | **Skip re-downloading the immutable pinned-snapshot InRelease.**  `cache.py:268` re-fetches `InRelease` per mirror on EVERY cache build; for a PINNED snapshot the timestamp is fixed → the file is immutable, so the re-download is pointless.  Fix: when snapshot-pinned AND a cached InRelease exists, skip the download — but keep `verify_inrelease` (GPG) UNCONDITIONAL on whichever file is on disk; on GPG fail delete + re-download once + re-verify (pinning controls re-fetch, GPG controls trust).  LOW value since the IPv4-pin fix (fce12f3) cut re-fetch to ~0.2s (was a multi-min IPv6 connect stall, not server latency) — keep as a minor optimization, not a fix.  Optional belt-and-suspenders: stash the InRelease sha256 in `snapshot.state` at pin time and require a match alongside GPG.  (Filed 2026-06-22 from session backlog.) |

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
| COMP-01 | P1 | todo | **Installer ISO** path.  `iso build live` and `iso build installer` both ship today — installer ramdisk reaches `finish-install.d/20final-message` cleanly on VMware BIOS + EFI as of 2026-05-14 (sub-phases COMP-01a..g closed in `docs/done.md`).  Umbrella remains open pending **COMP-01h — real-hardware testing** (sub-row below).  *(architecture **pivoted 2026-05-10** from "import vanilla d-i" to "rebuild d-i runtime from source via parallel udeb dep tree through our existing pipeline" — full plan: `docs/plans/comp-01-installer.md`, project memory `project_installer_from_source.md`.  Two distinct dep graphs (deb world + udeb world) over a single source corpus; same `source build` produces both `.deb` and `.udeb` outputs.  Installer ramdisk is pure udeb closure (no debs, no systemd) — `rootskel`+busybox init runs cdebconf+main-menu+d-i step udebs.  UI is `cdebconf-text-udeb`.  Branding via debconf-overrides + companion udeb (no upstream source patches — see `docs/branding-methodology.md`).  `installer.list` is mixed-universe — udeb names AND deb names; resolver dispatches per-entry.  Total ~5-7 weeks elapsed across COMP-01a..g.)* |
| COMP-01h | P1 | todo | **Installer Phase 8 — hardware testing** (1-2w). QEMU UEFI + BIOS smoke tests. Real hardware: at least one BIOS box + one UEFI box. Edge cases: small disk, no network, USB-only install media. Validate target boots cleanly after install completes. Treat as the gate before declaring COMP-01 done. *(VMware BIOS + EFI verified end-to-end through `finish-install.d/20final-message` 2026-05-14.  Real-hardware smoke + edge cases still pending.  Tracked in `docs/plans/comp-02-robust-build.md` § Phase F as the CI gate that would automate this.)* |
| COMP-04 | P1 | todo | **Architecture support** beyond `amd64`. The code reads `arch` from config but `Dockerfile`, `pkg.list` (`linux-image-amd64`, `grub-efi-amd64`), and `build_iso` ISO name are amd64-hardcoded. Decide on second-arch target (likely `arm64`), parameterise. |
| COMP-07 | P2 | todo | **Cross-build container per release** — `Dockerfile` is currently hard-pinned to `bookworm`. Auto-rebuild a per-release image (`bookworm`, `trixie`, `noble`, …) when `CONTAINER_RELEASE` changes; keep them in parallel so the user can cross-target. |
| COMP-11 | P2 | todo | **Distro abstraction**: `[Build] Distro = debian \| ubuntu` selects parallel artifact sets — `pkg.list.<distro>` (Ubuntu uses `casper` instead of `live-boot`, plus its own initramfs hooks and grub package names), `Dockerfile.<distro>` (`FROM ubuntu:${RELEASE}`), and the `os-release` ID/ID_LIKE/VENDOR_NAME fields (subsumes COMP-10).  `[Snapshot] Enabled = false` becomes the implicit default for non-Debian distros (no snapshot.d.o equivalent).  Carries the rest of the "distro-portability" cluster: ARCH-13 (URL externalisation) and HK-05 (dead-config-line cleanup) closed in `docs/done.md`; COMP-11 is the surviving member.  Hardest piece: maintaining two parallel pkg.lists in sync; consider a shared base + per-distro overlay file. |
| INST-01 | P2 | todo | **Installer i18n preseeding + non-en_US locale set.**  Today the language step is interactive at boot (`installer/preseed.cfg` ships no `debian-installer/locale`); installer strings are English-only (`S40-athena-branding` applies `@DISTRIBUTION@` substitutions only, no locale-driven template variants).  Preseed default locale, ship a chosen locale set (en_US + 2-3 candidates — pick from operator preference), pre-bake keymap defaults.  Reuses the existing `fork/source/athena-installer-data/data/S40-athena-branding` mechanism — adds locale overrides alongside the existing string overrides, no new infrastructure.  Touches `config/pool.list` (locale udebs).  Stays inside newt — no front-end change.  Filed 2026-05-29 from the comparative-analysis capability-gap audit (vs elementary / Pop / Mint / similar). |
| INST-02 | P2 | todo | **Installer accessibility — speakup + brltty.**  No `speakup` (screen reader) or `brltty` (braille) udebs in the installer closure today (`config/pool.list` / `config/pkg.list` mark a11y as deferred).  Adds the udebs, documents boot-menu shortcut (`s` for speakup at GRUB), validates with a screen-reader smoke pass under QEMU.  Real accessibility gap vs stock Debian d-i.  Touches `config/pool.list` + `installer/grub.cfg` boot menu.  Depends structurally on INST-01 (accessible prompts work better in user's preseeded locale).  Note ISO size cost — verify against current ISO budget before committing.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |
| INST-03 | P2 | todo | **Guided partition recipe — "Erase disk and install Asgard".**  Vanilla partman today: `installer/preseed.cfg` has zero partman entries; operator gets the full manual / guided picker.  Adds a custom partman recipe ("erase whole disk, single root, swap file, EFI on UEFI / BIOS-boot on legacy") + preseed defaults for the common path; keeps the manual mode reachable.  Removes the steepest cliff in the install flow today.  Stays inside d-i's partman — no front-end change.  New `fork/source/athena-installer-data/data/partman-recipe-asgard`.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |
| INST-04 | P3 | todo | **Front-end uplift investigation — graphical d-i vs Calamares.**  Newt is text-only; vs elementary / Pop / Mint / similar's polished GUI installers this is the most visible UX delta.  Investigation only at this stage — scope the gtk-frontend rebuild cost (additional udeb closure, Asgard branding through gtk theming, accessibility implications) vs replacing d-i with a Calamares fork.  No code change; writes a separate plan file under `docs/plans/`.  Picked up only after INST-01..03 land — those define the "do we still need this?" bar.  Most likely the way to spend XL effort for diminishing UX returns if started prematurely.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |
| INST-05 | P2 | todo | **Tasksel sanity check — verify menu tasks actually select + install their package sets on the target.**  `iso build installer` generates the tasksel menu from the lockfile (this build: 6 task(s) → `/.disk/athena-tasks.desc`) but flagged `INFO: pkg.list group [ssh-server] adds 0 unique packages — all seed(s) already pulled in by an earlier group or required/important.  Tasksel task remains valid (Key entries resolve from elsewhere)`.  That "valid but adds nothing unique" case is exactly where tasksel silently misbehaves — a task that renders in the menu but whose selection installs nothing, or whose `Key` entries don't resolve via `apt-cache dumpavail` (→ cdebconf hides it).  **Live-verify in the d-i installer** (under the COMP-01h QEMU smoke): (1) all 6 tasks render in the menu; (2) selecting each task ACTUALLY installs its intended package set on the target — especially `ssh-server` (does selecting it do anything, and should a 0-unique-pkg task even be offered?); (3) no task silently dropped by cdebconf for a non-`user` `Section` or a `Description` with commas/parens/em-dash (must mirror `debian-tasks.desc` shape — see `athena-tasks.desc` `Section: user` + ASCII tests); (4) every group's `Key` list resolves (the `pkg.list group ↔ fork/source/athena-tasksel/tasks/<group>` Key-match test + the `DEBIAN_TASKS_ONLY=1` env-filter interaction).  Cross-check the 6 generated tasks against `.disk/groups/` (this build: base, desktop, development-tools, gnome-desktop, laptop, ssh-server, standard).  (Filed 2026-06-24 from session — installer ISO build flagged ssh-server 0-unique-pkg.) |

## 4. Architecture & coding practices — P1 / P2

_All architecture & coding-practice tickets are closed — see [`docs/done.md`](docs/done.md) § 4._

## 5. Tests & CI — P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| AUDIT-02 | P2 | todo | **Reproducibility gate — `cmd source reproduce <pkg>` + nightly subset.**  Picks 3-5 representative forked sources (one heavy with `dh_strip` invariants e.g. `linux`; one fork with patch overlay e.g. `base-files`; one pure-data fork e.g. `athena-tasksel`), builds each twice in clean COMP-03 scratch dirs, runs `diffoscope` on the resulting `.deb` pair, fails on non-identical output.  Reuses `BuildContainer.build()` end-to-end + the per-worker scratch dir from COMP-03 (no race between concurrent reproduce runs).  Schedule via nightly CI job (separate from the per-commit gate — diffoscope is slow).  More concrete than CONF-06's general "adopt reprotest" — narrower scope (3-5 sources, not the full corpus), specific reproducer command operator can run locally.  CONF-06 stays open as the eventual full-corpus version.  Filed 2026-05-29 from the comparative-analysis capability-gap audit. |

## 6. Documentation — P1 / P2

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| DOC-06 | P2 | todo | Keep `README.md` in sync with the code as the project evolves.  When a pipeline stage is added/renamed, a default in `config/build.conf` changes, a new common failure mode appears (or an old one is fixed), or the operator workflow shifts — update the README in the same PR.  Periodic audit: when closing each ticket touching `scripts/` or `config/`, scan README §Building Image and amend if the change is operator-visible.  Footer note in the README points future-me here. |

## 7. Security & supply-chain — P0 / P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| FED-01 | P2 | todo | **Pull-side stale / closure-limited-ledger detector (design first).**  A peer's `mirror pull` TRUSTS the fetched signed closure ledger as its adoption set; when the ledger is stale or closure-limited (`ledger ⊊ folded live claims`) the peer adopts only the subset and STRANDS the rest → phantom `virtual_hash_conflict` at virtual-build (3522 this session: ledger 1731 = install closure vs 4699 actually claimed; heal required the OWNER to republish, and nothing told the peer its mirror was stale).  Today `cmd_mirror.py:1639` only COUNTS ledger-names-no-claim (`_no_claim`) — not a WARNING — and the inverse SUBSET case (live claims the ledger omits) is unguarded.  Fix idea: at pull, fold live claims, compare filename sets vs the fetched ledger; if ledger is a strict subset WARN loudly and/or fall back to adopting the live-claim union.  Open Qs: is folded-live-claims the authoritative published set (any subset = bug) or can they legitimately diverge (snapshot timing / pruning)?  warn-only vs auto-adopt-union?  redundancy vs the owner-side `closure_ledger_entry_missing` CRITICAL.  (Filed 2026-06-22 from session backlog.) |
| FED-02 | P2 | todo | **Co-publisher snapshot-advance coherence (NOT a block).**  A `role=federation` peer running `snapshot select latest` AHEAD of the mirror is the INTENDED multi-publisher test — it BUILDS + OWNS the forward delta while ADOPTING the unchanged base.  Do NOT block/warn the advance.  The guard work is CONSISTENCY of the resulting MIXED-snapshot mirror (base / current / two owners on adjacent versions): drift packages attributed as the peer's-delta-to-own, virtual build / audit must not false-conflict on them, the ledger's latest-per-pkg must prefer the peer's newer-version delta once published, supersession across two builders at adjacent snapshots, and `reconcile_snapshot_pin` forward-adopt when the origin later catches up.  Design + invariants first.  (Filed 2026-06-22 from session backlog.) |
| FED-03 | P2 | todo | **Builder-id registration is a blind overwrite — add TOFU + tier-1-signed keyring entries (B+D).**  `mirror builders register` (`cmd_mirror.py:3014`) blindly pushes `<id>.pub` to the mirror's `keyring/builders/<id>.pub`, OVERWRITING any existing key with no same-key/different-key check — so re-registering the same builder-id silently succeeds (observed re-registering `BS3` repeatedly).  Two cases hide behind this: same machine re-running configure (same key → harmless) vs another box claiming the id / a regenerated key (different key → SILENT IDENTITY HIJACK: the id's already-published claims now fail verification, and whoever holds the new key can publish AS that id).  Trap is sharp because the builder-id default is the HOSTNAME (`socket.gethostname()`), so two `debian` boxes collide.  Trust anchor is the id→pubkey binding in `keyring/builders/<id>.pub` (`coord/identity.py:verify_claim_against_keyring`); the mirror is a dumb SSH/HTTP target so it can't enforce — enforcement must be client-side + at verify time.  **Agreed approach = B + D.**  **B (client-side TOFU, small):** before upload, fetch existing `<id>.pub`; absent → register; identical → idempotent no-op (friendly "already registered, key unchanged"); different → REFUSE (with `--force` override).  **D (authenticated bindings, bigger):** tier-1-sign keyring entries on register, verify that signature in `verify_claim_against_keyring`, migrate/sign existing entries — so a pubkey dropped by anyone with SSH write but NO tier-1 key is rejected.  **CAVEAT (decided to accept for now):** the tier-1 GPG PRIVATE key is SHARED across all peers (imported at onboarding, `onboarding.py:322`), so D authenticates against OUTSIDERS (SSH-write-but-no-tier-1) but NOT a malicious INSIDER peer (any peer holds tier-1 and could sign a hijacking binding).  Closing the insider case needs owner-only tier-1 (peers verify with pubkey, don't self-sign coord-head/Release — a publish-model change) OR rotation-authorized-by-old-key — a SEPARATE, higher-bar decision, out of scope for B+D.  Sequence: B first, then D.  (Filed 2026-06-23 from session discussion.) |
| FED-04 | P2 | todo | **Stale-lock breaker for `mirror publish` — a crashed/power-failed publish strands the remote flock.**  The publish holds `/var/lock/repo-coord.lock` via `flock -w` over a long-lived SSH session (`coord/transport.py:remote_flock_acquire`), writing a `<lock>.holder` sidecar with the builder-id and `rm`-ing it on clean release.  If the publisher DIES mid-publish (power failure on the builder; mirror host itself stays up), the orphaned `flock`+`cat` chain keeps the lock open on the mirror until sshd finally reaps the dead TCP connection — which can be HOURS (no keepalive tuning).  Meanwhile every retry fails `could not acquire remote flock — builder 'X' is publishing` (`publish.py:~819`) with the ONLY recourse a manual `fuser -k /var/lock/repo-coord.lock; rm -f …holder` on the mirror.  Observed 2026-06-24: BS2 power-failed mid-publish → PIDs held the lock 41 min, `.holder=BS2`, `flock -n` HELD; remediation was manual `fuser -k` (which needs `psmisc`).  **Fix:** detect a GENUINELY-stale lock and offer to break it (kill holder + rm sidecar) with operator confirmation — but the staleness signal must NOT false-positive on a slow-but-LIVE publish over a poor link (the exact conditions here), or one peer breaks another's in-flight publish.  Sidecar mtime alone is insufficient (a legit multi-GB push over 3 MB/s legitimately holds for a long time).  Better: a HEARTBEAT — the lock holder touches `<lock>.holder` every N seconds while alive (background `while sleep N; do touch …holder; done` inside the flock'd shell), so stale ⇔ heartbeat older than a few intervals; the breaker checks `flock -n` (still held) AND heartbeat age > threshold, then SIGKILLs the holder + clears the sidecar.  Optionally add `mirror unlock [--force]` as an explicit operator command (there is none today) and tune sshd `ClientAliveInterval` on the mirror as a belt-and-suspenders so dead sessions reap faster.  Touches `coord/transport.py` (remote_flock_acquire heartbeat + a break helper) and `publish.py` (the acquire-failure path → offer break when stale).  (Filed 2026-06-24 from session — BS2 power-failure stranded the publish lock.) |
| SUP-01 | P3 | todo | **Live-dpkg grype matcher (deferred until a consumer asks).**  SBOM-driven CVE scanning already ships (`cmd_supply_chain.py` `cmd_cve` shells grype over the SBOM — the source of truth, since NMU-stripped binaries false-positive against live grype), and `strip_nmu` stamps `X-Athena-Upstream-Version` for provenance.  A grype matcher that scans the LIVE dpkg status of an installed system (mapping our stripped versions back to upstream via the X-field) is intentionally deferred — row kept open so the capability isn't forgotten if a consumer needs installed-system scanning.  (Filed 2026-06-22 from session backlog.) |

## 8. Operator UX — P2 / P3

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| UX-06  | P3 | todo | Localised messages — today everything is English-only. |
| RMIRROR-01 | P2 | todo | **Per-remote local-build-mirror toggle.** Today the localmirror is the machine-wide `create_local_mirror` (local.conf), which governs BOTH BS1's local build mirror AND the on-remote mirror staged at `container remote init` — so enabling it for a remote builder also makes BS1's `cache parse` offer to build its own (thin-link download). Make it a per-remote-container setting instead: prompt during `container remote add` ("Enable a local build mirror?") and persist it on the `[Remote.<name>]` entry in remote.conf (a `LocalMirror=true` key); `container remote init` / `source remotebuild` read it per-remote; BS1's own localmirror stays a separate decision. (Filed 2026-06-24 during remote-localmirror testing.) |
| API-01 | P2 | wip | **HTTP API for the platform** — key-protected (X-Api-Key, `config/api.key` 0600 autogen), localhost-default FastAPI server exposing pipeline state, build records + all sidecars (`.build.json`/`.buildlog`/`.vbuildlog`/container logs with tail-windowing), redacted config, repo/mirror state, and a thin **command dispatcher** (`POST /api/v1/command` feeds the existing noun-verb dispatcher; jobs queue on the single session = single-writer preserved; prompts fail fast, sudo via ATHENA_SUDO_PASSWORD).  OpenAPI `/docs` auto-generated.  Web UI lives in a SEPARATE repo consuming `/openapi.json`.  Build-host-only deps: `python3-fastapi python3-uvicorn python3-httpx` (apt).  Full design: `docs/plans/api-01-web-api.md`.  Filed 2026-06-07 (decisions taken with operator during the thor1 rebuild). |

## 9. House-cleaning — P3

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| DIST-01 | P3 | todo | **Closed-source distribution strategy (discuss later, low priority).**  Explore compiling/packaging the toolchain without shipping the Python source (Nuitka / Cython / PyArmor), with the GPL caveat that derivative-distro tooling interacting with GPL components may compel source availability regardless of the obfuscation method.  Strategic/legal discussion item — no code yet.  (Filed 2026-06-22 from session backlog.) |

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

