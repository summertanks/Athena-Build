# fork/source/ — Athena-owned Debian source packages

This tree holds full Debian source packages (`.dsc` + `debian/`
layout) that Athena maintains directly — **as opposed to**
`patch/source/<pkg>/<ver>/`, which holds quilt patches we apply on
top of upstream source pulled via `apt-get source`.

The engine discovers packages here at cache-build time, includes
them in source build automatically, and resolves them through the
same dependency tree that resolves upstream packages.  No central
registry — drop a valid package directory under
`fork/source/<pkg>/` and it's picked up on next cache rebuild.

See `docs/plans/fork-source.md` for the full plan, the stepwise
rollout, and the rationale for adding this tree alongside the
existing `patch/source/` mechanism.

## When to add a package here vs. patch upstream

| Goal | Use |
|---|---|
| Change 1-10 lines of an existing upstream package | `patch/source/<pkg>/<ver>/` (quilt patches) |
| Ship Athena identity / branding / stub files | `fork/source/athena-<name>/` (new package) |
| Replace an upstream package with our own implementation | `fork/source/<pkg>/` or `fork/source/athena-<name>/` (per supersede strategy below) |
| Add a step udeb that doesn't exist upstream | `fork/source/<pkg>/` (new package, declare `Installer-Menu-Item:`) |

**Default to patch when the change is small AND we don't expect
ongoing churn.** Forking means owning the whole package tree
forever — that cost is justified only when the patch surface is
large, when we want Athena identity baked in, or when we'd refresh
the patch on every upstream version bump anyway.

## Three supersede categories

When a fork package needs to OVERRIDE an upstream binary of the
same name, pick the right mechanism per package:

### 1. Net-new (no upstream collision)
The package name doesn't exist upstream (e.g. `athena-installer-data`,
`athena-base-files`).  No collision to resolve — just declare and
ship.  **Default for everything Athena-specific.**

### 2. Provides / Conflicts / Replaces (P/C/R override)
Our package has a different name (`athena-lsb-release`) but declares
the upstream name in `Provides:`, `Conflicts:`, and `Replaces:`.
Anything depending on the upstream name resolves to ours; the
upstream binary cannot coexist.

Example `debian/control` fragment:
```
Package: athena-lsb-release
Provides: lsb-release
Conflicts: lsb-release
Replaces: lsb-release
```

Best for: identity-file overrides where the upstream package isn't
in the Essential set.  Self-documenting (`dpkg -l | grep athena-`
audits everything we override).

### 3. File diversion via dpkg-divert
For packages in the Essential set (notably `base-files`, which
`debootstrap` installs first and which other Essential packages
silently depend on), Conflicts: would break the bootstrap.

Instead, our package `Depends:` on the upstream one and uses
`dpkg-divert --add --rename --divert /path.real /path` in its
postinst to move the upstream file aside and install ours in its
place.  prerm undoes the divert on remove.

Example: `athena-base-files` depends on `base-files`; in postinst:
```sh
dpkg-divert --add --rename --divert /etc/os-release.real /etc/os-release
cp /usr/share/athena/os-release /etc/os-release
```

Best for: identity-file overrides where the upstream is Essential.

## Per-package layout

Each subdirectory is a complete Debian source package.  The
minimum file set:

```
fork/source/<pkg>/
├── debian/
│   ├── changelog    ← Debian-format version + codename + maintainer
│   ├── control      ← Source: + one or more Package: stanzas
│   ├── compat       ← debhelper compat level (currently 13)
│   ├── rules        ← `#!/usr/bin/make -f` + `dh $@` (extended as needed)
│   ├── copyright    ← Debian copyright format
│   └── install      ← maps data/*.* → on-disk paths
└── data/            ← (optional) static file content shipped via debian/install
    └── ...
```

Optional files per package as needed:
- `debian/dirs` — list of directories to create on the target
- `debian/postinst`, `debian/prerm`, `debian/preinst`, `debian/postrm`
  — maintainer scripts (e.g. for dpkg-divert)
- `debian/<binpkg>.install`, `debian/<binpkg>.dirs` — per-binary
  variants when the source ships multiple binaries

## Binary package types: udeb vs deb

The `Package-Type:` field in each `Package:` stanza of
`debian/control` determines the output:

- `Package-Type: udeb` — installer ramdisk package; lands in the
  udeb dep tree; consumed by `installer_chroot.py`
- (absent — defaults to `deb`) — normal package; lands in the deb
  dep tree; consumed by `chroot.py` / pool.list

A single source package can produce both — declare two
`Package:` stanzas in `debian/control`, one with `Package-Type: udeb`
and one without.  Useful for branding (one udeb for installer
chrome, one deb for target-system themes).

## Versioning

Fork packages set their own version via `debian/changelog`.
Format: `<pkg> (<version>) <codename>; urgency=low`.

- For **net-new** packages (Category 1): start at `1.0.0`, bump on
  content changes.  Independent of upstream.
- For **P/C/R override** packages (Category 2): version is ours —
  no need to track upstream's version because the override is by
  package name, not by version comparison.
- For **diversion** packages (Category 3): same — version is ours.

## Build behaviour (when wired up, Step 3)

When the source builder encounters a package name found under
`fork/source/<pkg>/`, it skips `apt-get source <pkg>` and runs
`dpkg-buildpackage` against the local tree directly.  Output
lands in `repo/pool/main/<a>/<pkg>/` exactly like
upstream-built packages.

The artifacts produced by `dpkg-buildpackage` land in the *parent*
directory of the source tree (`fork/source/`) — these are
build-time scratch files and are gitignored.

## Self-discovery

Cache build walks `fork/source/*/debian/control` and synthesises
Packages-format records (one per binary stanza) so the dep tree
sees our packages as installable candidates BEFORE they're built.
See `scripts/fork_discovery.py` (Step 2 of FORK-01).

## Current packages

| Package | Type | Purpose | Status |
|---|---|---|---|
| `athena-installer-data` | udeb | installer-side stub templates, release files, debootstrap codename, tasksel wrapper | scaffold only (Step 1) |
