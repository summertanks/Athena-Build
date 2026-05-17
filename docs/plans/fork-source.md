# Plan — FORK-01: fork/source/ tree + Athena-owned udebs/debs

## Status: PROPOSED (awaiting approval to begin Step 1)

Tracking doc for the work of consolidating Athena's installer-side and
target-side overrides into a tree of self-contained Debian source
packages under `fork/source/`, displacing the current mix of quilt
patches (`patch/source/pkgsel/`, `patch/source/base-installer/`) and
in-script file writes (`scripts/installer_chroot.py`'s
`_write_athena_stub_template`, `_install_debootstrap_codename_script`,
`_create_runtime_dirs`, `_write_release_files`,
`_register_self_in_dpkg_status`; `scripts/chroot.py`'s os-release /
hostname / keyring writes).

## Goal

Move Athena-owned installer and target-system content from
script-time file writes and upstream-source patches into proper
Debian source packages under `fork/source/`.  Each package is
self-contained, version-controlled by a real `debian/changelog`, and
discovered automatically by the build engine — no manual registration
in `pkg.list`, `installer.list`, or any other config.

The result: deleting `patch/source/pkgsel/` entirely, deleting
~250 LOC of file-writing helpers from `installer_chroot.py` and
`chroot.py`, and a clean seam for future branding work (logos,
themes, `/etc/os-release`, debconf overrides).

## Non-goals

- Forking large upstream udebs (cdebconf-newt, main-menu, partman-*)
  for branding-string changes that require source edits.  Those wait
  for COMP-01f and are out of scope here.
- Replacing the quilt patch mechanism (`patch/source/`) — patches
  stay as the right tool for tiny upstream edits.  This plan adds a
  parallel tool (`fork/source/`) for the cases where the right move
  is "own the package", not "patch upstream".
- The CI / signing story for fork-built artifacts — those reuse the
  existing pipeline (gpg sign repo's Packages and Release files); no
  additional infra needed.

## Directory layout

### `fork/source/` — top-level convention

```
fork/source/
├── README.md                          ← how to add a fork package
│
├── athena-installer-data/             ← installer-side udeb
├── athena-base-files/                 ← target-side deb (identity files)
└── athena-branding/                   ← optional, multi-binary (udeb + deb)
```

Each subdir is a complete Debian source package.  The engine reads
`<pkg>/debian/control` to learn the binary names, types (`udeb` vs
`deb`), architectures, and dependencies.  No central registry.

### Per-package layout (worked example: `athena-installer-data`)

```
fork/source/athena-installer-data/
├── debian/
│   ├── changelog        ← athena-installer-data (1.0.0) thor; urgency=low
│   ├── control          ← Source: + Package: + Package-Type: udeb +
│   │                       Section: debian-installer + Architecture: all
│   ├── compat           ← 13
│   ├── rules            ← #!/usr/bin/make -f / dh $@
│   │                     (+ optional sed pass for ${codename} substitution)
│   ├── copyright
│   ├── install          ← maps data/* → on-disk paths
│   ├── dirs             ← list of dirs to create (tmp/, var/tmp/, root/)
│   └── postinst         ← runtime hooks (dpkg-divert, etc.) — optional
│
└── data/
    └── (per-file content shipped via debian/install)
```

### Engine integration points

```
scripts/
├── fork_discovery.py   ← NEW: walks fork/source/*/debian/control,
│                         emits synthetic Packages-format records
├── cache.py            ← MODIFY: merge fork records into deb_view + udeb_view
└── build.py            ← MODIFY: source build dispatches to dpkg-buildpackage
                          on fork pkgs (skip apt-get source); auto-include all
                          fork pkgs in build set
```

## Supersede strategy — three categories

Three different override shapes; pick the right tool per package:

| Category | Example | Mechanism | When |
|---|---|---|---|
| **Net-new** | athena-installer-data | none — just declare and ship | upstream has no such pkg name |
| **P/C/R override** | (future) athena-lsb-release | declare `Provides: lsb-release`, `Conflicts: lsb-release`, `Replaces: lsb-release` | want to replace an upstream binary cleanly, our pkg has different name |
| **File diversion** | athena-base-files | depend on upstream, `dpkg-divert` files in postinst | upstream is in Essential: set (base-files) — can't conflict without breaking debootstrap |

Default to Category 2 for new overrides.  Fall back to Category 3
only when the target pkg is in the Essential set or otherwise can't
tolerate Provides/Conflicts.  Avoid Category 1 same-name same-binary
forks — they create perpetual version-tracking burden against
upstream and don't add value over the cleaner P/C/R or diversion.

## Working principles

1. **One package per concern** — don't bundle unrelated overrides.
   `athena-installer-data` ships installer-specific stubs; identity
   files (os-release, lsb-release for the TARGET) belong in a
   separate `athena-base-files`.
2. **Self-discovery** — no manual registration of fork packages.
   They appear in cache and build set by walking the tree.
3. **Build offline** — fork packages build from local source with no
   network round-trip (no `apt-get source`).
4. **Versioning** — fork pkg version starts at `1.0.0` and bumps
   under our control via `debian/changelog`.  Independent of upstream
   versioning unless using Category 2 (P/C/R) where the upstream
   name is being replaced (then version still ours, since the
   Provides: relationship doesn't require a version match).
5. **No CI gate is special** — fork packages flow through the same
   source build → repo/ → cache verify → chroot install path as
   upstream packages.

## Working agreement: approval gates

Each step below is to be discussed in chat (purpose, what changes,
acceptance criteria, risks) AND approved before code is written.
This doc captures the structure; the discussion happens turn-by-turn.

## Stepwise plan

### Step 0 — Approach approval (this document)

**Purpose:** lock in the directory layout, engine integration shape,
and supersede category strategy before writing any code.

**Acceptance:** operator reads this doc, approves or redirects, and
gives the go-ahead to start Step 1.

**Status:** awaiting approval.

---

### Step 1 — Create `fork/source/` scaffolding + README

**Purpose:** establish the convention with a real on-disk example
that builds standalone (via `dpkg-buildpackage`) but isn't yet
wired into our cache or source-build pipeline.  Smallest possible
proof that the structure works.

**Files created:**
- `fork/source/README.md` — convention doc
- `fork/source/athena-installer-data/debian/changelog`
- `fork/source/athena-installer-data/debian/control`
- `fork/source/athena-installer-data/debian/compat`
- `fork/source/athena-installer-data/debian/rules`
- `fork/source/athena-installer-data/debian/copyright`
- `fork/source/athena-installer-data/debian/install` (empty for now)

**Acceptance:**
- `cd fork/source/athena-installer-data && dpkg-buildpackage -us -uc -b -d`
  produces `athena-installer-data_1.0.0_all.udeb` in the parent dir.
- No engine changes yet — nothing in `scripts/` or `config/` modified.
- README explains the three supersede categories + how to add a new
  fork pkg.

**Risks:** none — purely additive, no behaviour change.

**Approval needed:** yes — confirm before writing files.

---

### Step 2 — Cache-time helper generates fork as a local Mirror (REVISED 2026-05-16)

**Status:** APPROVED (architecture lock-in 2026-05-16).  Replaces the
original "synthetic-records-in-cache.py" Step 2 with a cleaner shape:
fork is just another Mirror, indistinguishable from upstream after
metadata generation.

**Purpose:** at cache-build time, transform `fork/source/*/` into a
fully-formed local Debian repository (Release + Sources + Packages
+ generated `.dsc`/`.tar.*`), register it as a `file://` Mirror
parsed FIRST, so the cache's existing per-mirror walk does all the
heavy lifting unchanged.

**Why this shape (not synthetic-records-in-cache):**
- No cache-side synthesis — fork records flow through the same
  `Packages.parse` → `package_hashtable` path as upstream.
- Dep tree treats fork identically to upstream — no special branches.
- Real hashes for Sources (the .dsc + .tar.* files exist at parse
  time); placeholder hashes for Packages (binary not yet built — but
  fork pkgs are never *tunneled*, so the hash/size fields are never
  consulted to gate a download).
- Supersede semantics handled at cache layer by parsing fork mirror
  FIRST then dropping upstream entries that re-declare our names —
  no version-greater-than maintenance required against upstream.

**Files created:**
- `scripts/fork_mirror.py` — helper with two public functions:
  * `generate_fork_mirror(buildconfig) -> bool`: runs `dpkg-source -b`
    against every `fork/source/<pkg>/`, emits `fork/source/repo/{*.dsc,
    *.tar.xz}` (real source pkgs), then writes `fork/Packages`
    (placeholder hashes), `fork/Sources` (real hashes), `fork/Release`
    (real hashes of the two indices).  Returns True if any fork
    packages exist; False to skip mirror registration.
  * `register_fork_mirror(mirrors, buildconfig) -> List[Mirror]`:
    returns mirrors with a fork Mirror PREPENDED (parsed first).

**Files modified:**
- `scripts/utils.py` — `download_file()`: add file:// scheme support
  at top (just shutil.copy + getsize, no HTTP).  `BuildConfig`:
  expose `dir_fork_source_repo` (auto-mkdir, writability-checked).
- `scripts/cache.py` — at start of `__build_cache`: call
  `generate_fork_mirror`; if True, prepend fork Mirror.  Track
  `_fork_pkg_names` and `_fork_src_names` (Package: field ONLY — NOT
  Provides:).  During upstream walk: skip records whose name appears
  in these sets, print one-line operator warning per skipped record.
  At fork-mirror fetch time: detect `file://` scheme, skip GPG
  verification (file:// mirrors are trusted by definition), read
  plain `Release` (not `InRelease`), accept uncompressed
  `Packages` + `Sources` (no `.xz`/`.gz` requirement).

**Fork directory layout (after helper runs):**
```
fork/
├── Release                    ← generated; lists hashes of Packages + Sources
├── Packages                   ← generated; one stanza per binary in debian/control
├── Sources                    ← generated; one stanza per source pkg in source/repo
└── source/
    ├── athena-installer-data/ ← tracked (Step 1)
    │   └── debian/...
    └── repo/                  ← generated; .dsc + .tar.xz output of dpkg-source
        ├── athena-installer-data_1.0.0.dsc
        └── athena-installer-data_1.0.0.tar.xz
```

**Synthetic Packages stanza shape (athena-installer-data example):**
```
Package: athena-installer-data
Version: 1.0.0
Architecture: all
Maintainer: Athena Linux <athena@local>
Section: debian-installer
Priority: optional
Package-Type: udeb
Filename: athena-installer-data_1.0.0_all.udeb    ← bare basename;
                                                     matches what
                                                     dpkg-buildpackage
                                                     deposits in repo/
Size: 0                                            ← placeholder
MD5sum: 00000000000000000000000000000000           ← placeholder
SHA256: 0000…(64 zeros)                            ← placeholder
Description: Athena installer-side data files
 (full description from debian/control)
```

**Generated Sources stanza shape:**
```
Package: athena-installer-data
Binary: athena-installer-data
Version: 1.0.0
Architecture: all
Format: 3.0 (native)
Maintainer: Athena Linux <athena@local>
Directory: source/repo
Files:
 <real md5> <real size> athena-installer-data_1.0.0.dsc
 <real md5> <real size> athena-installer-data_1.0.0.tar.xz
Checksums-Sha256:
 <real sha256> <real size> athena-installer-data_1.0.0.dsc
 <real sha256> <real size> athena-installer-data_1.0.0.tar.xz
```

**Generated Release file shape:**
```
Origin: Athena
Label: Athena Fork
Suite: thor
Codename: thor
Date: <UTC timestamp>
Architectures: amd64 all
Components: main
SHA256:
 <real sha> <size> Packages
 <real sha> <size> Sources
MD5Sum:
 <real md5> <size> Packages
 <real md5> <size> Sources
```

**Mirror declaration (in-memory, created by `register_fork_mirror`):**
- `id`: `fork`
- `url`: `file:///<working_dir>/fork`
- `dist_url`: `file:///<working_dir>/fork/`   ← suite is `./` (flat)
- `packages_path`: `Packages`
- `sources_path`: `Sources`
- Inserted at index 0 of `self.mirrors` so it's parsed FIRST

**Supersede rule (per user 2026-05-16):**
- Parse fork FIRST → populates `_fork_pkg_names` (binary `Package:`
  field only) and `_fork_src_names` (source `Package:` field only).
- During upstream walk: if `_pkg.package in _fork_pkg_names`, skip
  insertion AND print one-line operator warning (`"Local supersedes
  upstream pkg <name> v<ver>"`).
- NO virtual-package auto-bridging — if fork's `lsb-release` doesn't
  declare a Provides: that the upstream did, dep tree will fail
  loudly at the missing virtual.  Operator fixes by adding the
  Provides: to fork's debian/control.  This is intentional — see
  user feedback 2026-05-16.

**Acceptance:**
- Empty `fork/source/` → helper skips silently, no Mirror registered.
- With `athena-installer-data`: fork/Release, fork/Packages,
  fork/Sources, fork/source/repo/*.dsc + *.tar.xz all generated.
- `print cache athena-installer-data` returns the record (with the
  real Description, with placeholder Size/SHA256).
- Dep tree can resolve `Depends: athena-installer-data` without error.
- Same-name supersede: if a test fork pkg names `lsb-release`,
  upstream `lsb-release` is dropped + warning printed.
- Virtual NOT auto-bridged: if test fork's `lsb-release` lacks a
  `Provides: lsb-release-dev`, dep tree resolution of a pkg that
  needs `lsb-release-dev` fails as expected.
- Existing tests pass; ~6 new tests for fork_mirror helper +
  supersede behaviour pass.
- `ruff check scripts/ tests/` clean.

**Implementation risks:**
- dpkg-source -b runs on the build host (not container) at cache time.
  Tooling confirmed present (`dpkg-source --version` → 1.22.22).  If a
  cleaner environment becomes a constraint later, move dpkg-source
  invocation into the build container.
- file:// URL support in download_file: ~10 lines added at top.  No
  existing call site passes file:// URLs (audited), so this is purely
  additive.
- InRelease bypass: file:// mirrors skip GPG verification by scheme;
  not a per-mirror config knob (avoids changing build.conf shape).
  Operator-visible: cache build logs `[fork] file:// mirror — GPG
  verification skipped (local)`.
- Stale `fork/source/repo/` after operator edits debian/control:
  helper detects mtime drift (debian/changelog mtime > <pkg>.dsc
  mtime) and re-runs dpkg-source.  Operator can force-rebuild by
  `rm -rf fork/source/repo/`.

**Approval status:** APPROVED 2026-05-16.  Implementation in progress.

---

### Step 3 — First content: stub templates → athena-installer-data

**Purpose:** the simplest possible content migration.  Move the
mirror/protocol stub template from `_write_athena_stub_template` in
`installer_chroot.py` into `athena-installer-data/data/`.  After this
step, our udeb actually ships something useful.

**Files modified:**
- `fork/source/athena-installer-data/data/athena-stubs.templates`
  (new — content lifted from `_ATHENA_STUB_TEMPLATES` literal)
- `fork/source/athena-installer-data/debian/install` — add line
  `data/athena-stubs.templates var/lib/dpkg/info/`
- `scripts/installer_chroot.py` — delete `_write_athena_stub_template`
  + its call site + `_ATHENA_STUB_TEMPLATES` constant
- `config/installer.list` — add `athena-installer-data` (operator
  explicitly references the pkg by name, same as any upstream pkg)
- `tests/test_module.py` — drop the stub-template tests, add a
  test that the udeb's debian/install ships the template

**Acceptance:**
- ISO build runs end-to-end
- Installer boots, base-installer's bootstrap-base proceeds without
  the `db_get mirror/protocol` failure
- ~30 LOC deleted from `installer_chroot.py`

**Risks:** high — first time fork pkg actually runs in d-i.  Iterate
on shape before committing to subsequent items.  Keep the upstream
fallback path (the Python helper) commented in `installer_chroot.py`
until end-to-end install confirms; remove after one successful run.

**Approval needed:** yes.

---

### Step 4 — Runtime dirs, release files, dpkg status, debootstrap codename

**Purpose:** migrate the four remaining in-script helpers
(`_create_runtime_dirs`, `_write_release_files`,
`_register_self_in_dpkg_status`, `_install_debootstrap_codename_script`)
into the udeb.

**Per item:**
- `_create_runtime_dirs` → `debian/dirs` (one line per dir)
- `_write_release_files` → `data/lsb-release` + `data/default-release.in`
  with `${codename}` substituted via `debian/rules` sed pass at
  build time
- `_register_self_in_dpkg_status` → STAYS in `installer_chroot.py` —
  fundamentally a post-unpack action on the chroot's dpkg DB, not
  something a udeb can do for itself.  Move comment explaining why
  it's the only remaining helper.
- `_install_debootstrap_codename_script` → ship
  `data/debootstrap-codename` → `usr/share/debootstrap/scripts/${codename}`
  (codename sub via rules at build time)

**Acceptance:**
- `installer_chroot.py` loses ~150 LOC (4 helpers + their constants)
- Only `_register_self_in_dpkg_status` + `_apply_installer_overlay`
  remain as Python-level chroot mutations
- Installer boots, partman runs, base-installer runs, hostname /
  user setup runs (anything that reads /etc/lsb-release or
  /etc/default-release works correctly)

**Risks:** medium — codename substitution at build time must match
what the runtime codename will be (e.g. `thor`).  Resolve by reading
codename from `config/release.list` at fork-build time.

**Approval needed:** yes — discuss codename-substitution mechanism
before implementing.

---

### Step 5 — Tasksel wrapper (deletes patch/source/pkgsel)

**Purpose:** replace the pkgsel patch with a dpkg-divert + wrapper
shipped by athena-installer-data.  After this step, we own zero
patches on pkgsel.

**Files modified:**
- `fork/source/athena-installer-data/data/tasksel-wrapper.sh`:
  ```sh
  #!/bin/sh
  exec env -u DEBIAN_TASKS_ONLY /usr/bin/tasksel.real "$@"
  ```
- `fork/source/athena-installer-data/debian/install` — add
  `data/tasksel-wrapper.sh usr/local/bin/tasksel`
- `fork/source/athena-installer-data/debian/postinst`:
  ```sh
  #!/bin/sh
  set -e
  dpkg-divert --add --rename --divert /usr/bin/tasksel.real \
              /usr/bin/tasksel
  ln -sf /usr/local/bin/tasksel /usr/bin/tasksel
  ```
- DELETE `patch/source/pkgsel/0.79/9001-allow-derivative-task-descs.patch`
- DELETE `patch/source/pkgsel/` (directory)
- `tests/test_module.py` — remove the
  `pkgsel_patch_drops_debian_tasks_only_env_var` test

**Acceptance:**
- ISO build no longer references the pkgsel patch
- Installer runs tasksel; multi-select shows both upstream tasks
  AND Athena tasks; ticking a box installs the right pkgs
- Same end-state behavior as the patch — verified by an end-to-end
  install run

**Risks:** medium — udeb unpack order matters.  If
athena-installer-data unpacks BEFORE pkgsel/tasksel, the divert runs
before the file exists, and `dpkg-divert --add --rename` will
silently no-op the rename (it diverts an absent file).  Mitigation:
test by listing `athena-installer-data` LAST in `installer.list`
and verifying via the dpkg unpack log.  Alternative: postinst
defensively does the divert even if the file is absent, then the
unpack of pkgsel.udeb later writes through the divert.

**Approval needed:** yes — discuss the divert ordering question
before committing to this approach (alternative is keep the patch).

---

### Step 6 — `athena-base-files` for the target system

**Purpose:** extend the same `fork/source/` mechanism to the target
(installed) system.  Ship `/etc/os-release`, `/etc/hostname` default,
`/etc/hosts` baseline as a proper deb instead of writing them from
`chroot.py`.

**New package:**
```
fork/source/athena-base-files/
├── debian/
│   ├── control          ← Package: athena-base-files
│   │                      Depends: base-files
│   │                      Section: misc
│   │                      Architecture: all
│   ├── postinst         ← dpkg-divert /etc/os-release, /etc/hostname;
│   │                      install ours (Category 3: diversion, because
│   │                      base-files is Essential)
│   ├── prerm            ← reverse diverts on remove
│   └── install
└── data/
    ├── os-release       ← NAME=Athena, VERSION_ID=, etc.
    ├── hostname.default
    └── hosts.template
```

**Files modified:**
- `scripts/chroot.py` — delete `/etc/os-release` write, hostname
  write, /etc/hosts write (sections around lines 1171-1195 in the
  current chroot.py)
- `config/pkg.list [base]` — add `athena-base-files`

**Acceptance:**
- Target system after install has `/etc/os-release` with
  `NAME=Athena`
- `chroot.py` loses ~50 LOC
- `lsb_release -a` (if lsb-release deb is on target) shows Athena
  in DISTRIB_ID

**Risks:**
- base-files is Essential; getting the divert wrong could brick
  install.  Test on a throwaway VM first.
- If `lsb-release` deb (not udeb) is installed on target, its
  `/etc/lsb-release` overrides ours unless we extend
  athena-base-files to divert that too.

**Approval needed:** yes — discuss diversion vs Provides/Conflicts/Replaces
before implementing.

---

### Step 7 — `athena-branding` (installer + target shared)

**Purpose:** demonstrate the multi-binary case: one source package
producing both a udeb (for installer chrome) AND a deb (for target
themes/logos).  Establishes the pattern for COMP-01f branding work.

**New package:**
```
fork/source/athena-branding/
├── debian/
│   ├── control                          ← TWO binary stanzas:
│   │                                      Package: athena-branding-installer
│   │                                      Package-Type: udeb
│   │                                      ─────
│   │                                      Package: athena-branding
│   │                                      Architecture: all
│   ├── athena-branding-installer.install ← debconf-overrides.dat → /var/cache/
│   ├── athena-branding.install          ← logo, splash → /usr/share/
│   └── ...
└── data/
    ├── debconf-overrides.dat             ← rebrand strings (Athena, not Debian)
    ├── splash.png
    └── logo.svg
```

**Acceptance:**
- One source pkg, two binary outputs (one udeb, one deb)
- Installer dialogs show "Athena installer" in titles (via
  debconf-overrides.dat applied at first boot)
- Target system /usr/share/athena/ contains logo + splash

**Risks:** scope — this is where branding work starts; resist the
urge to also patch cdebconf-newt here.  Keep this step minimal:
just prove multi-binary works.

**Approval needed:** yes — possibly defer entirely if COMP-01f is
the better home.

---

## Open questions to resolve in-flight

These come up across multiple steps; capturing here so we don't
re-derive each time:

1. **Where does dpkg-source -b run for Step 2** — on host (chosen,
   tooling confirmed present) or in build container (heavier,
   more isolated).  Revisit if host dep proves brittle.

2. **Codename substitution mechanism** (Step 4) — read from
   `config/release.list` at fork-build time, sed via `debian/rules`?
   Or environment variable consumed by a templated `debian/install`?

3. **dpkg-divert ordering** (Step 5) — is athena-installer-data
   reliably unpacked AFTER pkgsel?  If not, divert+symlink runs
   against an absent file.

4. **base-files diversion vs P/C/R** (Step 6) — verify on a VM
   that the divert approach survives base-files security upgrades
   on the target.  P/C/R might be cleaner but base-files is
   Essential, and apt's behaviour with Conflicts on Essential is
   subtle.

5. **Multi-binary control file shape** (Step 9) — confirm dh's
   default behaviour with one source + one udeb binary + one deb
   binary works without explicit overrides in `debian/rules`.

## Status log

- 2026-05-16: PROPOSED.  Awaiting approval to begin Step 1.
