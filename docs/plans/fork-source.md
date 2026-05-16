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

### Step 2 — Self-discovery in cache

**Purpose:** make fork packages visible to the cache as installable
candidates.  After this step, `print cache athena-installer-data`
should show our package with the Version and Filename from its
debian/control + changelog.

**Files created:**
- `scripts/fork_discovery.py` — walks `fork/source/*/debian/`, parses
  control + changelog, emits synthetic Packages-format records
  (one per binary stanza in control)
- `tests/test_module.py` — tests for fork_discovery output shape

**Files modified:**
- `scripts/cache.py` — at cache-build time, merge fork records into
  the in-memory deb and udeb views (post-Parsing-Package-Files stage)

**Synthetic record shape:**
```
Package: athena-installer-data
Source: athena-installer-data
Version: 1.0.0
Architecture: all
Filename: pool/main/a/athena-installer-data/athena-installer-data_1.0.0_all.udeb
Size: 0         ← not yet known; cache validates size when chroot install runs
SHA256:         ← same
Origin: athena-fork
Package-Type: udeb
```

**Acceptance:**
- `print cache athena-installer-data` returns our record.
- Dep tree can resolve a Depends on athena-installer-data without error.
- Existing 319 tests pass; new ~5 tests for fork_discovery pass.
- `ruff check scripts/ tests/` clean.

**Risks:**
- Cache must not crash when fork pkg has a name colliding with an
  upstream pkg (Category 2 P/C/R case) — version comparison picks the
  higher version per existing cache semantics.  Verify with a test.
- Origin: athena-fork lets us audit "what came from where" via the
  existing cache fields.

**Approval needed:** yes — confirm before writing module + cache integration.

---

### Step 3 — Source-build integration for fork packages

**Purpose:** when the source builder is asked to build a fork pkg,
it runs `dpkg-buildpackage` in the fork tree directly, skipping the
`apt-get source` import.  Output lands in `repo/` exactly like
upstream-built packages.

**Files modified:**
- `scripts/build.py` (source build dispatcher): branch on
  "is this a fork pkg?"; if yes, build in-place under
  `fork/source/<pkg>/` or in a copy under `build/fork/<pkg>/`.
- `scripts/buildcontainer.py`: extend to accept a local source dir
  instead of the apt-source-fetched dir for fork pkgs.

**Open question to resolve here:**
- Build in-place (touches the repo's source tree at build time —
  cleaner for iteration, dirties git status) or copy to
  `build/fork/<pkg>/` first (cleaner repo, slower).  My lean: copy.

**Acceptance:**
- `source build athena-installer-data` produces
  `repo/pool/main/a/athena-installer-data/athena-installer-data_1.0.0_all.udeb`
- cache verify finds the file with correct size + sha256
- existing source-build tests pass; new tests for fork build path pass

**Risks:** dpkg-buildpackage emits artifacts to the parent dir by
convention — need to capture and move to `repo/pool/main/`.  Existing
upstream-source build does the same; reuse that move logic.

**Approval needed:** yes — confirm in-place vs copy-to-build before writing.

---

### Step 4 — Auto-include all fork pkgs in build set

**Purpose:** ensure fork packages are built unconditionally on every
source-build run, regardless of whether anything depends on them yet.

**Files modified:**
- `scripts/build.py` (build set computation): union the existing
  set with fork_discovery.list_all_packages()

**Acceptance:**
- `source build` (no args) builds athena-installer-data even when
  no upstream pkg in installer.list / pkg.list depends on it
- `source build clean` removes fork artifacts the same way it
  removes upstream artifacts
- existing tests pass; new test for "fork pkg in build set" passes

**Risks:** minimal — fork pkg count is expected to stay small (~3-5).

**Approval needed:** yes.

---

### Step 5 — First content: stub templates → athena-installer-data

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
- `config/installer.list` — add `athena-installer-data` (still
  manually listed for now; Step 4 makes this auto but we add it
  here for explicit ramdisk inclusion)
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

### Step 6 — Runtime dirs, release files, dpkg status, debootstrap codename

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

### Step 7 — Tasksel wrapper (deletes patch/source/pkgsel)

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

### Step 8 — `athena-base-files` for the target system

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

### Step 9 — `athena-branding` (installer + target shared)

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

1. **Where do fork pkgs build — in-place or copy?** (Step 3)
   Lean: copy to `build/fork/<pkg>/` to keep `fork/source/` clean
   in git status.

2. **Codename substitution mechanism** (Step 6) — read from
   `config/release.list` at fork-build time, sed via `debian/rules`?
   Or environment variable consumed by a templated `debian/install`?

3. **dpkg-divert ordering** (Step 7) — is athena-installer-data
   reliably unpacked AFTER pkgsel?  If not, divert+symlink runs
   against an absent file.

4. **base-files diversion vs P/C/R** (Step 8) — verify on a VM
   that the divert approach survives base-files security upgrades
   on the target.  P/C/R might be cleaner but base-files is
   Essential, and apt's behaviour with Conflicts on Essential is
   subtle.

5. **Multi-binary control file shape** (Step 9) — confirm dh's
   default behaviour with one source + one udeb binary + one deb
   binary works without explicit overrides in `debian/rules`.

## Status log

- 2026-05-16: PROPOSED.  Awaiting approval to begin Step 1.
