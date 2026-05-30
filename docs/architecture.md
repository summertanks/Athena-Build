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
   → repo index → repo publish
```

| Stage | Command | Purpose | Major modules |
|---|---|---|---|
| 1. Cache | `cache build` | Fetch + GPG-verify every mirror's `InRelease`, `Packages`, `Sources`.  Resolves the snapshot pin (memoised). | `cache.py`, `utils.verify_inrelease`, `signing.py` |
| 2. Dep parse | `cache parse` | Walk required + important + manual + recommends closures; build the parallel **deb** + **udeb** dep trees over a single source corpus. | `dependencytree.py`, `package.py` |
| 3. Source sync | `source sync` | Download every selected source's tarball/dsc/diff to `source/` via its origin mirror's pool URL.  SHA256 + `.verified` sidecar per file. | `utils.download_source` |
| 4. Container init | `container init` | Build the `athenalinux:build-<release>` Docker image with the toolchain layer (dpkg-dev, devscripts, build-essential, etc.). | `buildcontainer.py:BuildContainer.__init__` |
| 5. Source build | `source build [all\|live\|installer\|recommended\|<pkg>…] [profile,…]` | Per-source: extract → patch → `dpkg-buildpackage` → strip NMU → segregate `.deb`/`.udeb` outputs to `repo/`.  Parallel via `ThreadPoolExecutor` (COMP-03; `[Build] MaxParallelBuilds`). | `buildcontainer.py:build`, `build.py:_build_one_source` |
| 6. Chroot build | `chroot build {live\|installer}` | Bootstrap a real chroot from `repo/` using `dpkg --unpack` + `dpkg --configure -a`.  Live = full system payload; installer = udeb-only initrd content. | `chroot.py`, `installer_chroot.py`, `buildsystem.py` |
| 7. Chroot verify | `chroot verify` (auto after build) | 8-check verifier: filesystem layout, configured packages, signing keyring present, no Debian residue, etc.  Gates ISO build. | `build.py:_verify_chroot` |
| 8. ISO build | `iso build {live\|installer\|disk}` | Master the chroot into a hybrid BIOS+EFI ISO (`grub-mkrescue` inside the build container, COMP-14) or a sparse qcow2 (`iso build disk`, COMP-09). | `iso.py`, `iso_installer.py`, `disk_image.py` |

After the artifact lands, two more stages handle distribution:

| Stage | Command | Purpose |
|---|---|---|
| 9. Repo index | `repo index full \| minimal` | `dpkg-scanpackages` over `repo/dists/<codename>{,-debug}/`, `apt-ftparchive release`, GPG-sign `Release` + clearsign `InRelease`. |
| 10. Repo publish | `repo publish ssh\|local …` | Transport the indexed repo to a destination.  See [`docs/repo-publish-vm-setup.md`](repo-publish-vm-setup.md). |

For incremental updates (advance the snapshot, rebuild only the changed
source delta, +asg-stamp, publish additively): see `repo refresh` and
memory entry `project_upd01_update_architecture.md`.

## BuildFlags — the stage gate

Each stage produces a flag on `BuildSession.flags` (a `BuildFlags` instance,
`build.py:75`).  A later stage checks the prior flag; missing flags abort
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
| `chroot_ready` / `chroot_verified` | `chroot build live` (verify gates `_verified`) | `iso build live`, `iso build disk` |
| `chroot_installer_ready` | `chroot build installer` | `iso build installer` |
| `iso_live_ready` / `iso_installer_ready` / `iso_disk_ready` | `iso build *` | Final autorun gate; surfaced in `print state` |

`__str__` returns a compact one-line status `[✓] cache  [·] dep_check  …`
used by the TUI top bar; the same shape underlies `print state`.

The state machine is rendered in [`docs/diagrams/build-fsm.png`](diagrams/build-fsm.png)
(source: `build-fsm.dot`).

## Module overview

The toolchain is 21 Python modules under `scripts/` plus an 11-file `tui/`
package.  Grouped by role:

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

### Orchestrator
- **`build.py`** — `BuildSession` owns the full pipeline state and the
  `cmd_*` handlers the TUI dispatches.  Hosts `BuildFlags`, the autorun
  chains (`cmd_auto_run_live`/`_installer`/`_disk`), `_build_one_source`
  (COMP-03 worker), and the publish dispatcher (`cmd_repo_publish` →
  `_publish_via_ssh` / `_publish_via_local`).  ~8k LOC; biggest module
  by far.

### Build execution
- **`buildcontainer.py`** — `BuildContainer.build(src)` runs
  `dpkg-source -x` → patches → `dpkg-buildpackage -us -uc -nc` in a per-
  worker scratch dir, then `strip_nmu_from_deb` + `_segregate_built_artifacts`
  (under `_REPO_DEST_LOCK`).  Live container registry + label-based reap
  for orphan cleanup (COMP-03 Phase 2/3).
- **`buildsystem.py`** — chrootless dpkg helpers for the libc bootstrap
  round; `compute_install_batches` (topological packing).
- **`chroot.py`** — `_ChrootMixin` (mount/umount procfs, sysfs, devpts;
  generate `/etc/{os-release,hostname,hosts,fstab,…}`; install signing
  keyring; pre-install + post-install patch overlay).
- **`installer_chroot.py`** — parallel chroot path that unpacks the udeb
  closure into `buildroot/installer/` (no `dpkg --configure`).
- **`dep_drift.py`** — sync per-package `.shlibs`/`Depends:` constraints
  between cache view and on-disk `.deb`s after a snapshot advance.

### ISO + repo
- **`iso.py`** — `build_iso` masters the live chroot to a squashfs +
  hybrid BIOS+EFI ISO via `grub-mkrescue` inside the build container
  (COMP-14).
- **`iso_installer.py`** — `build_installer_iso` masters the installer
  chroot to a cpio.gz initrd, stages the `.disk/` tree, signs the
  installer's apt-repo, copies pubkey to `.disk/archive-key.gpg`.
- **`disk_image.py`** — `build_disk_image` produces a sparse qcow2 of
  a pre-installed Asgard system (COMP-09).
- **`apt_repo.py`** — `dpkg-scanpackages` orchestration; `apt-ftparchive
  release`; sign helpers; `remote_reindex_and_sign` / `local_reindex_and_sign`
  (COMP-02 publish twins, closing over `_reindex_and_sign_via`).
- **`repo_audit.py`** — `published_ledger` (the `+asg uN` bump authority);
  `_write_signed_manifest` / `_read_signed_manifest` (fail-closed, STA-21);
  external-vs-manifest reconciliation (`repo audit external ssh`).
- **`fork_mirror.py`** — local fork tree mirror generator (the `file://`
  flat-shaped mirror cache reads alongside snapshot mirrors).

### Operator surface
- **`cli.py`** — headless launcher (`--headless`); registers Cli as
  `tui.tui_instance` so the curses-only paths fall through cleanly.
- **`tui/`** — 11-file curses package: `tui.py` (event loop),
  `widgets.py` (ProgressBar, Spinner), `render.py`, `dispatcher.py`,
  `facade.py` (console_mark / console_trim_to surface), state, theme,
  geometry, footer, prompt, history.
- **`print_commands.py`** — `print state` / `print mirrors` / `print
  recommended` / etc.  Pulls from `BuildSession.flags`,
  `BuildConfig`, `DependencyTree`.

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
- **Publish-before-prune** — `repo refresh` order is build → merge-index
  → additive publish → THEN local prune.  Never prune a version before
  it's on the remote.
- **Identity strip** — every shipped artifact is audited for `Debian` /
  `debian.org` / `report-bug` residue.  Memory entry:
  `project_filter_debian_specific_installer_hooks.md`.

## Where to read next

- [`docs/pseudocode.md`](pseudocode.md) — per-function natural-English
  walkthrough of every module.
- [`docs/patching.md`](patching.md) — patch directory conventions
  (DEP-3, pre-install, post-install).
- [`docs/repo-publish-vm-setup.md`](repo-publish-vm-setup.md) — operator
  guide for ssh + local publish + `repo summary`.
- [`docs/branding-methodology.md`](branding-methodology.md) — identity-
  strip + Asgard branding methodology (Patterns A / B / C).
- [`docs/plans/`](plans/) — per-initiative implementation plans
  (COMP-01 installer, COMP-02 robust build, etc.).
- [`TODO.md`](../TODO.md) + [`docs/done.md`](done.md) — open and
  closed work, with rationale preserved.
