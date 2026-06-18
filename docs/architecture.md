# Athena-Build — architecture

How the toolchain is structured, the pipeline stages it runs, the gate that
sequences them, and where the major pieces live in the tree.  This is the
high-altitude tour — pair it with [`docs/pseudocode.md`](pseudocode.md)
(per-function natural-English walkthrough) when you need the per-function
detail, and [`docs/diagrams/build-fsm.png`](diagrams/build-fsm.png) for the
state-machine view.

## Three-layer identity

Athena ships as **Asgard** (the distribution it builds), codename **thor**
(the apt suite).  "Athena-Build" is the toolchain — the build origin, baked
into fork package-name prefixes (`athena-*`) and toolchain logs.  This file
documents the toolchain.

| Layer | What it names | Examples |
|---|---|---|
| Athena | toolchain (this repo) | `fork/source/athena-*/`, `Athena Build <athena@local>` signing UID |
| Asgard | distribution identity | `/etc/os-release` `NAME`, GRUB menu title, hostname |
| thor   | release codename | apt suite, `VERSION_CODENAME`, fork pkgs' `debian/changelog` `Distribution:` |

Memory entry: `project_three_layer_identity.md`.

## Pipeline stages

The toolchain is structured around eight sequential stages.  `autorun live`,
`autorun installer`, and `autorun disk` walk them end-to-end; operators can
also run each command individually.

```
cache build → cache parse → source sync → container init
   → source build → chroot build → chroot verify
   → iso build {live | installer | disk}
   → mirror publish   (repo indexing is automatic)
```

| Stage | Command | Purpose | Major modules |
|---|---|---|---|
| 1. Cache | `cache build` | Fetch + GPG-verify every mirror's `InRelease`, `Packages`, `Sources`.  Resolves the snapshot pin (memoised). | `cache.py`, `utils.verify_inrelease`, `signing.py` |
| 2. Dep parse | `cache parse` | Walk required + important + manual + recommends closures; build the parallel **deb** + **udeb** dep trees over a single source corpus. | `dependencytree.py`, `package.py` |
| 3. Source sync | `source sync` | Download every selected source's tarball/dsc/diff to `source/` via its origin mirror's pool URL.  SHA256 + `.verified` sidecar per file. | `utils.download_source` |
| 4. Container init | `container init` | Build the `athenalinux:build-<release>` Docker image with the toolchain layer (dpkg-dev, devscripts, build-essential, etc.). | `buildcontainer.py:BuildContainer.__init__` |
| 5. Source build | `source build [all\|live\|installer\|recommended\|<pkg>…] [profile,…]` | Per-source: extract → patch → `dpkg-buildpackage` → strip NMU → segregate `.deb`/`.udeb` outputs to `repo/`.  Parallel via `ThreadPoolExecutor` (COMP-03; `[Build] MaxParallelBuilds`). | `buildcontainer.py:build`, `commands/cmd_source.py:_build_one_source` |
| 6. Chroot build | `chroot build {live\|disk\|installer}` | Bootstrap a real chroot from `repo/` using `dpkg --unpack` + `dpkg --configure -a`.  Live (`buildroot/live`) and disk (`buildroot/disk`) each install their own surface closure (SURFACES-01, below); installer = udeb-only initrd content. | `chroot.py`, `installer_chroot.py`, `buildsystem.py`, `surfaces.py` |
| 7. Chroot verify | `chroot verify` (auto after build) | 8-check verifier: filesystem layout, configured packages, signing keyring present, no Debian residue, etc.  Surface-aware: the live-boot check is SKIPped for the disk surface (live-boot is live-ISO machinery).  Gates ISO build. | `commands/cmd_build.py:_verify_chroot` |
| 8. ISO build | `iso build {live\|installer\|disk}` | Master the chroot into a hybrid BIOS+EFI ISO (`grub-mkrescue` inside the build container, COMP-14) or a sparse qcow2 (`iso build disk`, COMP-09). | `iso.py`, `iso_installer.py`, `disk_image.py` |

After the artifact lands, two more stages handle distribution:

| Stage | Command | Purpose |
|---|---|---|
| 9. Repo index | _(automatic)_ | `dpkg-scanpackages` over `repo/dists/<codename>{,-debug}/`, `apt-ftparchive release`, GPG-sign `Release` + clearsign `InRelease`.  Run automatically by `chroot build` / `mirror publish` when the `InRelease` is missing — not a separate operator command. |
| 10. Mirror publish | `mirror publish [<name>]` | Per-file `.deb` push + Ed25519-signed claims + tier-1 GPG-signed `coord-head` to every configured mirror (or one).  Federation-gated; first-publish bootstraps the peer.  See [`docs/mirror-setup.md`](mirror-setup.md). |

For incremental updates (advance the snapshot, rebuild only the changed
source delta, +asg-stamp, publish additively to every configured
mirror): walk `source sync → source build all → mirror publish` and
see memory entry `project_upd01_update_architecture.md`.

## Surface composition (SURFACES-01)

A **surface** is one shipped artifact: the live image, the disk image,
or the installer ISO's `/cdrom/pool`.  In plain terms: each surface
gets exactly the packages something on that surface can actually
reach, computed fresh from the resolved dependency graph — never
assembled from per-group package-name deltas.  The deltas
(`pkg_group_pkg_names`) are *credit*-based: a dependency shared by two
groups is credited to whichever group resolved first, so unioning them
silently drops shared deps from any surface that includes the later
group.

Mechanically, `scripts/surfaces.py:surface_closure` is a BFS from a
seed set over the edges the resolver actually chose (Depends ∪
Pre-Depends; OR groups follow the first selected alternative; virtual
names canonicalized), optionally extended by a Recommends-extras
fixpoint (only extras some closure member actually recommends).

| Surface | Seeds | Closure | Size |
|---|---|---|---|
| Live chroot | `[Live] Groups` from build.conf (default `base, gnome-desktop`) ∪ `config/live.list` ∪ required/important | with Recommends extras | ~1105 pkgs |
| Disk chroot | `[Disk] Groups` (default `base`) ∪ required/important | hard deps only | ~245 pkgs (console + ssh) |
| Installer ISO pool | `[base]` ∪ every pkg.list task group ∪ `config/installer-defaults.list` (grub metas, console-setup, microcode, firmware, VM tools) ∪ `installer.list` roots ∪ required/important | with Recommends extras | ~1252 pkgs (legacy formula shipped 1569) |

The disk surface gets its **own chroot** at `buildroot/disk`
(`chroot build disk`, gated by `chroot_disk_ready`); `iso build disk`
masters that chroot, fully decoupled from the live/GNOME chroot.
`pool.list` entries unreachable by anything on the ISO stay
selected/built/published but are **mirror-only** — installed post-boot
over the network.

The installer's tasksel menu is generated **at ISO mastering** from the
signed selection lockfile (`scripts/tasksel_desc.py`,
cdebconf-sanitized), staged as `/.disk/athena-tasks.desc`, and copied
onto /target by athena-pkgsel's `pre-pkgsel.d/05athena-tasks` hook.  A
pkg.list group edit reaches the menu via `cache parse` + `iso build
installer` — no fork rebuild; the desc packaged in athena-tasksel-data
is only a fallback (fork versions: athena-pkgsel `0.79+athena2`,
athena-tasksel `3.73+athena3`).

## BuildFlags — the stage gate

Each stage produces a flag on `BuildSession.flags` (a `BuildFlags` instance,
`build.py:94`).  A later stage checks the prior flag; missing flags abort
with an actionable message instead of running on stale state.  Clean
operations (`clean cache`, `clean source`, etc.) reset the corresponding
flag so the next run re-does the work.

| Flag | Set by | Required by |
|---|---|---|
| `cache_ready` | `cache build` | `cache parse` |
| `dep_check_ready` | `cache parse` | `source sync`, `source build`, `chroot build` |
| `download_ready` | `source sync` | `source build` |
| `build_container_ready` | `container init` | `source build` |
| `source_build_ready` | `source build` (whole-run completion) | `chroot build` |
| `signing_key_verified` | `_ensure_signing_key_verified` (top of `chroot build`) | `chroot build`, ISO sign step |
| `chroot_ready` / `chroot_verified` | `chroot build live` (verify gates `_verified`) | `iso build live` |
| `chroot_disk_ready` | `chroot build disk` | `iso build disk` |
| `chroot_installer_ready` | `chroot build installer` | `iso build installer` |
| `iso_live_ready` / `iso_installer_ready` / `iso_disk_ready` | `iso build *` | Final autorun gate; surfaced in `print state` |

`__str__` returns a compact one-line status `[✓] cache  [·] dep_check  …`
used by the TUI top bar; the same shape underlies `print state`.

The state machine is rendered in [`docs/diagrams/build-fsm.png`](diagrams/build-fsm.png)
(source: `build-fsm.dot`).

## Module overview

The toolchain is 31 top-level Python modules under `scripts/`, plus a
`scripts/commands/` package (the `cmd_*` handler mixins BuildSession
composes), a 9-file `scripts/coord/` MIRROR-01 federation-sidecar
package and an 11-file `scripts/tui/` curses package.  Grouped by role:

### Foundation
- **`utils.py`** — `BuildConfig` (the canonical config the rest of the
  system runs against), `Mirror`, NMU/binNMU strippers, `+asg<R>u<N>`
  versioning primitives, snapshot timestamp resolution against
  `snapshot.debian.org`, the InRelease GPG verifier, helpers
  (download, SHA256, pkg.list parser, tree-hash, DEP-3 header check).
- **`signing.py`** — generate / verify the project signing key under
  `gnupg/signing/`; sign `Release`/`InRelease`; export the pubkey
  for shipping into the chroot at `/usr/share/keyrings/athena-archive-keyring.gpg`.

### Data layer
- **`cache.py`** — per-mirror `Packages`/`Sources`/`Release` fetch with
  GPG-verified InRelease; `package_hashtable` + `udeb_hashtable` keyed by
  (name, version) pairs; `skip_src` accumulator.
- **`package.py`** — `Package` / `Source` (pickle-able subclasses of
  python-debian's `Deb822`); dep-field parsers; `get_provides()`,
  `explicit_provides_version()`.
- **`dependencytree.py`** — `DependencyTree` resolves required + important
  + manual + recommends closures over the deb world; `udeb_dep_tree` does
  the parallel udeb resolution; `parse_sources` walks both for the union
  source corpus; `selected_pkgs` / `selected_srcs` / `extras_*` / `pool_extras_*`.
- **`surfaces.py`** — SURFACES-01 per-surface composition:
  `surface_closure` (reachability BFS over the resolved dep graph,
  optional Recommends-extras fixpoint), `group_seed_names`,
  `read_flat_roots`.  See "Surface composition" above for why
  closures and never credit-based group deltas.

### Orchestrator
- **`build.py`** — `BuildSession` owns the full pipeline state; the
  `cmd_*` handlers the TUI dispatches live in the `scripts/commands/`
  mixin package it composes (`cmd_build`, `cmd_cache`, `cmd_source`,
  `cmd_audit`, `cmd_repo`, `cmd_mirror` — MIRROR-01 federation:
  `add`/`remove`/`list`/`summary`/`status`/`publish`/`pull`/`audit`/
  `query`/`reclaim`/`reconcile-neighbours`/`builders`/`conflict` —
  `cmd_run`, `cmd_snapshot`, `cmd_tunnel`, `cmd_virtual`,
  `cmd_supply_chain`).  `build.py` itself hosts `BuildFlags`, session
  wiring, and `main()`.
- **`mirror.py`** — per-mirror durable state CRUD and the non-network
  helpers behind the `mirror` umbrella: `add_mirror` / `remove_mirror` /
  `read_mirror_state` / `update_mirror_state` (one
  `config/mirror.<name>.state` per peer), `all_mirror_urls` (canonical
  federation source-of-truth), `neighbours_drift` (consistency tag for
  `mirror list`), `reconcile_neighbours` (fan-out federation propagation).
- **`scripts/coord/`** — MIRROR-01 federation sidecar (9 modules):
  `schema` (claim + coord-head schemas, `canonicalize_neighbours`;
  claim schema v4 adds `reclaims_seq` — RECLAIM-01 `mirror reclaim`,
  the sanctioned same-filename/new-sha re-publish; see
  [`docs/mirror-setup.md`](mirror-setup.md)),
  `identity` (Ed25519 keypair + keyring), `store` (per-builder JSONL
  claim ledger), `policy` (BLOCK on hash conflict, PUBLISH_HALT
  sentinel), `head` (read/write tier-1-signed `coord-head.json`),
  `reconcile` (audit_local + audit_cross + `check_federation_consistency`
  + `detect_hash_conflicts`), `transport` (rsync + ssh flock primitives),
  `publish` (11-step `remote_publish` state machine with federation gate
  + first-publish bootstrap + per-file `.deb` push).

### Build execution
- **`buildcontainer.py`** — `BuildContainer.build(src)` runs
  `dpkg-source -x` → patches → `dpkg-buildpackage -us -uc -nc` in a per-
  worker scratch dir, then `strip_nmu_from_deb` + `_segregate_built_artifacts`
  (under `_REPO_DEST_LOCK`).  Live container registry + label-based reap
  for orphan cleanup (COMP-03 Phase 2/3).
- **`buildsystem.py`** — `BuildSystem` composer (chroot + iso +
  dep-drift mixins); sudo-password lifecycle; `for_iso` factory.
- **`chroot.py`** — `_ChrootMixin`: `build_chroot` (Kahn-batched
  `dpkg --unpack`/`--configure` with a debootstrap-style essential
  bootstrap — the dpkg+dash+coreutils closure lands in the earliest
  waves — plus unpack-retry rounds and a dpkg-query completion gate);
  mount/umount procfs, sysfs, devpts; generate
  `/etc/{os-release,hostname,hosts,fstab,…}` (Asgard identity —
  hostname = `asgard`, not the toolchain name); install signing
  keyring; pre-install + post-install patch overlay.
- **`installer_chroot.py`** — parallel chroot path that unpacks the udeb
  closure into `buildroot/installer/` (no `dpkg --configure`).
- **`dep_drift.py`** — sync per-package `.shlibs`/`Depends:` constraints
  between cache view and on-disk `.deb`s after a snapshot advance.

### ISO + repo
- **`iso.py`** — `build_iso` masters the live chroot to a squashfs +
  hybrid BIOS+EFI ISO via `grub-mkrescue` inside the build container
  (COMP-14).
- **`iso_installer.py`** — `build_installer_iso` masters the installer
  chroot to a cpio.gz initrd, stages the `.disk/` tree (including the
  generated `/.disk/athena-tasks.desc`), signs the installer's
  apt-repo, copies pubkey to `.disk/archive-key.gpg`.
- **`tasksel_desc.py`** — SURFACES-01: derive the tasksel `.desc`
  stanzas from the signed lockfile's pkg.list groups at ISO mastering;
  every field is cdebconf-sanitized (ASCII, no commas/parens —
  cdebconf silently drops non-conforming tasks).
- **`disk_image.py`** — `build_disk_image` produces a sparse qcow2 of
  a pre-installed Asgard system from the disk-surface chroot
  (`buildroot/disk`, COMP-09); generated fstab keeps fs_passno 1 for
  `/` only (ESP gets 2).
- **`apt_repo.py`** — `dpkg-scanpackages` orchestration; `apt-ftparchive
  release`; sign helpers; `remote_reindex_and_sign` /
  `local_reindex_and_sign` close over `_reindex_and_sign_via` and feed
  `mirror publish` (ssh and local-fs transports respectively).
- **`repo_audit.py`** — `published_ledger` (the `+asg uN` bump authority);
  `_write_signed_manifest` / `_read_signed_manifest` (fail-closed, STA-21);
  cross-mirror manifest reconciliation (consumed by `mirror audit`).
- **`fork_mirror.py`** — local fork tree mirror generator (the `file://`
  flat-shaped mirror cache reads alongside snapshot mirrors).
- **`sbom.py`** — CycloneDX 1.5 JSON SBOM per build, one component per
  source with PURL + patch-set-hash + Athena-namespaced provenance
  properties (CONF-07).
- **`identity_scan.py`** — three-stage residue audit
  (`audit_identity` over fork content / chroot hooks / staged ISO) with
  word-boundary token regex and operator-reviewable allow-list at
  `audit/identity-allowlist` (CONF-10).

### Operator surface
- **`cli.py`** — headless launcher (`--headless` / `--cmd`); registers
  Cli as `tui.tui_instance` so the curses-only paths fall through cleanly.
  ANSI colour on TTY, `--yes`-respecting prompts, ATHENA_SUDO_PASSWORD
  env-var pickup (UX-05 a–g).
- **`tui/`** — 11-file curses package: `tui.py` (event loop),
  `widgets.py` (ProgressBar, Spinner), `render.py`, `dispatcher.py`,
  `facade.py` (console_mark / console_trim_to surface), state, theme,
  geometry, footer, prompt, history.
- **`print_commands.py`** — `print state` / `print mirrors` / `print
  extras` / `print build-times` / `print signing` / etc.  Pulls from
  `BuildSession.flags`, `BuildConfig`, `DependencyTree`.
- **`persistence.py`** — `save_session` / `restore_session` round-trip
  the parsed `Cache` + `DependencyTree` via pickle protocol 5 + gzip
  under `<dir_cache>/`; fingerprint-gated (config + mirror InReleases +
  fork tree-hashes + patch-set hashes + snapshot + arch + profiles)
  so a `cache parse`-invalidating change refuses restore.  Fully dormant:
  the `resume` command / `--resume` flag that consumed it were removed
  pending a relook, and the `save_session` call sites were dropped too, so
  the blob is no longer written or read — the module is retained intact for
  a future re-wire.
- **`select_packages.py`** — curses-only interactive package picker
  behind `cache select`.

## Process invariants

The build is hermetic and reproducible by design.  Some load-bearing
invariants you'll see referenced across the code:

- **Self-contained pool** — apt never falls back to `deb.debian.org`
  anywhere in the build, installer, or installed system.  Memory entry:
  `project_self_contained_repo.md`.
- **Snapshot pinning** — every mirror request flows through
  `snapshot.debian.org/archive/<key>/<ts>/dists/…` so the build is
  reproducible bit-for-bit at the pinned timestamp.  Default
  `[Snapshot] Enabled = true`.
- **Pristine binary versions** — `strip_nmu_from_deb` runs post-
  `dpkg-buildpackage` to scrub NMU layers (`+bN`, `+debNuN`, `~bpoN+N`,
  etc.).  Memory entry: `feedback_strip_nmu_at_build.md`.
- **Update-version layer (UPD-01)** — incremental updates ship as
  `+asg<R>u<N>` stamps, derived from the local signed manifest (the
  `+asg uN` bump authority), per-binary-file N.  Memory entry:
  `project_upd01_update_architecture.md`.
- **Publish-before-prune** — update order is build → `mirror publish`
  (which per-file pushes new `.deb`s additively, signs claims, re-signs
  `coord-head`) → THEN local prune.  Never prune a version before every
  configured mirror's claim ledger records it.
- **Identity strip** — every shipped artifact is audited for `Debian` /
  `debian.org` / `report-bug` residue.  Memory entry:
  `project_filter_debian_specific_installer_hooks.md`.
- **Pre-flight gates fire early** — `chroot build {live|disk|installer}`
  runs the source + repo audits up front; the repo audit BLOCKS on
  unresolved deps, install-cohort conflicts, record↔disk hash drift,
  and STALE artifacts in `repo/` (version-drift / orphan-source /
  malformed — a superseded same-name `.deb` is silently consumable by
  the chroot installer; remedy: `repo repair cleanup`).  `iso build
  live|installer` gates on the build container up front, because
  grub-mkrescue (the final mastering step) runs in-container and a
  late failure costs ~10 minutes of pool staging first.

## Where to read next

- [`docs/pseudocode.md`](pseudocode.md) — per-function natural-English
  walkthrough of every module.
- [`docs/patching.md`](patching.md) — patch directory conventions
  (DEP-3, pre-install, post-install).
- [`docs/mirror-setup.md`](mirror-setup.md) — operator guide for
  registering / first-publish / wipe-and-redo on a publish-target
  mirror (MIRROR-01 federation surface).
- [`docs/branding-methodology.md`](branding-methodology.md) — identity-
  strip + Asgard branding methodology (Patterns A / B / C).
- [`docs/plans/`](plans/) — per-initiative implementation plans
  (COMP-01 installer, COMP-02 robust build, etc.).
- [`TODO.md`](../TODO.md) + [`docs/done.md`](done.md) — open and
  closed work, with rationale preserved.
