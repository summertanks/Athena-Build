# Athena-Build — patching conventions

The toolchain carries three patch trees, each with its own scope, format,
and application timing.  Knowing which one to use for a given fix is half
the work; the other half is following the DEP-3 header convention so the
patch's *why* survives.

```
patch/
├── source/<pkg>/<exact-debian-version>/9001-*.patch       — source-level
├── source/<pkg>/prebuild.sh                               — per-pkg build env
├── pre-install/<chroot-relative-path>/{file | *.patch}    — chroot bring-up
├── post-install/<chroot-relative-path>/{file | *.patch}   — chroot finalize
└── empty/                                                 — bind-mount stub
```

Decision shortcut:

| Symptom | Use |
|---|---|
| Upstream source has a bug / OOM / wrong defaults / build-system issue | **`patch/source/<pkg>/<ver>/`** |
| ONE package's build needs environment setup (exports, ulimits, generated files) that must not land in the container image where it affects every build | **`patch/source/<pkg>/prebuild.sh`** |
| A file must exist in the chroot BEFORE a package is unpacked (e.g. `/etc/passwd`, pre-seed input) | **`patch/pre-install/`** |
| A file installed by a package needs to be amended / overwritten AFTER dpkg lays it down (e.g. `/etc/os-release`) | **`patch/post-install/`** |
| You can fix it via debconf preseed / debian-installer answer | **NOT a patch** — use `installer/preseed.cfg` (memory: `feedback_prefer_preseed_over_code.md`) |

For identity / branding overrides specifically, see
[`docs/branding-methodology.md`](branding-methodology.md) — that's a
distinct mechanism (companion udeb + debconf overrides + GRUB splash),
not a patch tree.

## 1. Source patches — `patch/source/<pkg>/<version>/9001-*.patch`

### Layout

```
patch/source/firefox-esr/140.11.0esr-1~deb12u1/
  9001-disable-rust-lto-amd64-avoid-gkrust-oom.patch
  9002-cap-make-jobs-amd64-avoid-cc1plus-oom.patch
```

- `<pkg>` = source package name (`dpkg-source -x`'s output dir name minus
  the version).
- `<version>` = **exact upstream Debian version with `:` epoch stripped**.
  Not the pristine base.  When a snapshot advance bumps the upstream
  version, the directory needs to be migrated (or the patches
  re-evaluated).  Memory entry on the failure mode this prevents:
  `feedback_verify_patch_layer_via_logs.md`.
- `9001-`, `9002-`, … prefix orders the patches.  Numbering starts at
  9001 to stay out of the way of upstream Debian's `0001-`–`0999-`
  numbering (so a future Debian patch series tail won't collide).

### How they're applied

At `dpkg-source -x` extraction time, the build container bind-mounts
`patch/source/<pkg>/<ver>/` at `/patch` and runs:

```sh
for PATCH in 9001-*.patch …; do
    patch -p1 < /patch/"$PATCH"
done
```

The list is read **at build time** from disk (not cached at `cache parse`)
so a patch added after the last parse still gets picked up — symptom of
the old caching bug was a re-run failing with the same error as before,
looking like the patch silently didn't apply.  See
`scripts/buildcontainer.py:545` for the full rationale comment.

Packages without a patch tree at the matching version use a bind-mount
to `patch/empty/` so the path is always valid; the for-loop is a no-op.

### DEP-3 header — required

Every patch in `patch/source/` MUST carry a [DEP-3](https://dep-team.pages.debian.net/deps/dep3/)
header.  The validator (`utils.check_dep3_header`,
`scripts/utils.py:1053`) scans the **first 40 lines** of the patch for
two required fields:

```
Description: one-line summary of what + why
Author: Your Name <you@example.com>           # or Origin:
Forwarded: no                                  # or yes / upstream URL
Last-Update: 2026-05-15
Bug-Athena: short context — what build it surfaced in, link to a TODO/done row
 .
 Long description here.  Use a leading single space + dot to separate
 paragraphs.  Explain WHY the upstream behaviour is wrong AND why this
 patch is the right fix.  Future-you will be grateful.
 .
 Cross-reference any other patch in the same package's series, plus
 the surrounding pkg.list / fork situation that drove the change.
```

`Description` (or `Subject:`) and `Origin` (or `Author:`) are the two
checked fields; the others are conventional.  The validator is a **soft
warning** — it does not block the build, so an operator's one-off
ad-hoc patch can ship.  The project's own patches are held to the rule.

### DEP-3 layout rules

The 40-line scan window has two consequences:

1. **Author/Origin must appear in the first 40 lines.**  Long
   `Description` bodies are fine as long as Author lands first — put
   Author at the very top so a multi-screen Description can't push it
   past the cutoff.  Memory entry: `feedback_dep3_header_40_line_limit.md`.

2. **No `--- a/...` / `+++ b/...` / `@@` lines in the header region.**
   The validator stops the moment it sees `---`.

### Migrating patches across snapshot advances

When a snapshot bump renames `<pkg>/<old-version>/` →
`<pkg>/<new-version>/`, every patch in the old directory must be
re-evaluated against the new upstream:

1. Inspect the upstream changelog between old and new — if upstream fixed
   the issue, **delete** the patch (and `git rm` the directory).
2. If the issue persists, copy the patch verbatim to the new directory,
   then `dpkg-source -x` the new source and `patch --dry-run -p1` to
   verify the patch still applies.  If it doesn't, rebase the patch
   against the new source.
3. Update the patch's `Last-Update:` field to today's date.

The commit message convention for these is `patches: migrate <list of
pkgs> to post-snapshot-advance versions` (see `0fa2680`,
`patches: migrate firefox-esr / gnutls28 / krb5 / libgcrypt20 …`).

### What the `patch/source/` tree is NOT for

- **Adding Athena identity to a package** — use a fork under
  `fork/source/<pkg>/` instead.  Source patches are upstream-Debian
  modifications; identity changes warrant a same-name or athena-prefixed
  fork.  Memory entry: `project_file_path_collision_requires_pkg_override.md`.
- **Fixing build-time `nodoc` / `nocheck` profile holes** — these go in
  `patch/source/` today (see CONF-08 in TODO.md — four such patches will
  be retired when the underlying tooling gap is closed) but they're
  marked as temporary workarounds, not the steady state.
- **Anything you'd preseed** — d-i answer keys, debconf priorities,
  apt-setup mirror choices.  See `installer/preseed.cfg`.

### Prebuild script — `patch/source/<pkg>/prebuild.sh`

Optional, **version-independent** (sits next to the version dirs, survives
version bumps).  For package-specific build *environment* — exports,
ulimits, generated files — that must not go into the container image where
it would affect every build.

- **Sourced, not executed** (`. /prebuild.sh`) in the build shell, inside
  the unpacked source tree, after patches, before
  `dpkg-checkbuilddeps`/`dpkg-buildpackage` — so its exports reach the
  build.
- Runs under the recipe's `set -e -o nounset`: a script error **fails that
  package's build** loudly.
- Content folds into `patch_set_hash` — editing it invalidates the build
  record and triggers a rebuild, exactly like a patch edit.  Disclosed in
  the SBOM as `athena:prebuild`.
- Ships in the remote bundle; local and remote builds behave identically.
- NOT for source modifications (that's a `.patch`) and NOT for env every
  build needs (that's the Dockerfile / `_CONTAINER_ENV`).

## 2. Pre-install overlay — `patch/pre-install/<chroot-relative-path>/`

### Layout

```
patch/pre-install/
├── etc/
│   ├── group
│   ├── hosts
│   ├── passwd
│   └── resolv.conf
└── var/
    ├── cache/
    └── log/
```

### What it does

`_ChrootMixin.pre_install` (`scripts/chroot.py:1052`) walks
`patch/pre-install/` and, for every directory found, mirrors that path
into the chroot.  Files copy verbatim (`sudo cp`, no perm preservation
beyond the default); `.patch` files are applied with `patch -p1 -i`
relative to the mirrored chroot directory.

Runs **before** any package is unpacked.  Used for files that the chroot
needs to exist before `dpkg --unpack` can run cleanly — passwd/group
stubs so dpkg's user creation doesn't fail, `/etc/hosts` so name
resolution works during the bootstrap, log/cache directories that
packages assume exist.

Also runs a hardcoded `cmd_list` of `ln -sfv` + `install -dv` + `chmod`
operations for FHS-mandated symlinks (`/var/run` → `/run`, `/var/lock` →
`/run/lock`) and mode fixups (`/tmp` 1777, `/root` 0750, etc.) that
dpkg can't lay down itself.

### When to use a pre-install file vs a fork

Pre-install is the **simplest** overlay mechanism — drop a file at the
matching relative path and it appears in the chroot.  Use it when the
file is:

- A stub (`/etc/passwd`, `/etc/group`) that bootstrap needs but the real
  contents come from later package installs.
- A bring-up config that no package owns (e.g. `/etc/resolv.conf` for
  the chroot's network access during build).

When the file IS owned by a package, prefer **post-install** or a
**fork**.  Putting a package-owned file in pre-install means dpkg
overwrites it during `--unpack`.

## 3. Post-install overlay — `patch/post-install/<chroot-relative-path>/`

### Layout

Same shape as pre-install — directories mirror chroot paths, regular
files copy verbatim, `.patch` files apply at the matching chroot
directory.

### What it does

`_ChrootMixin.post_install` (`scripts/chroot.py:1114`) runs **after**
every package is unpacked and configured.  Use it for:

- Distro-specific config that dpkg would otherwise reset (`/etc/os-release`,
  `/etc/issue`, `/etc/motd`).  Note: `os-release` is now generated by
  `generate_system_configs()`, not from a post-install file — that's
  the cleaner shape for files whose content depends on `BuildConfig`.
- Patches against files that ARE installed by packages (e.g. a tweak
  to `/etc/default/grub` after the `grub-pc` package laid it down).
- Files that must land at the matching path AFTER dpkg created the
  parent directory.

Unlike pre-install there's no hardcoded `cmd_list` — post-install
permission fixups belong in the overlay files themselves or in
`generate_system_configs()`.

### Identity strip discipline

Anything you add under `patch/post-install/` is a candidate for the
identity-leakage audit (AUDIT-01 in TODO.md, when it ships).  Avoid
embedding `Debian` / `debian.org` references — the file as shipped lives
in an Asgard installation.  Memory entry:
`project_audit_generated_files_for_toolchain_identity.md`.

## 4. The `patch/empty/` stub

A deliberately-empty directory bind-mounted as `/patch` inside the build
container whenever `patch/source/<pkg>/<ver>/` does NOT exist.  Keeps the
container's patch-apply loop unconditional (no `if patch_path then …`
shell branch) at the cost of one always-valid bind-mount target.

Don't put files here.  The validator and the build pipeline both treat
its emptiness as load-bearing.

## 5. Commit conventions for patch changes

- **Adding a new patch**: subject line names the package + the bug class
  in present tense.  Example: `patch firefox-esr: cap parallel make jobs
  on amd64 to avoid cc1plus OOM`.
- **Migrating across snapshot advance**: `patches: migrate <pkg1> /
  <pkg2> / … to post-snapshot-advance versions`.  Body lists what was
  copied vs. rebased vs. dropped.
- **Retiring a patch**: subject `patch <pkg>: retire <patch-name> —
  fixed upstream in <version>`.  Body links the upstream commit.

DEP-3 header lint runs in CI via `cmd_parse_dependency` → warnings
surface in `print warnings`; the test pinning the format is
`test_check_dep3_header_*` in `tests/test_module.py`.

## Cross-references

- [`docs/architecture.md`](architecture.md) — pipeline stages + where
  patch application falls in the sequence.
- [`docs/branding-methodology.md`](branding-methodology.md) — identity
  overrides (companion udeb + debconf), the right mechanism for branding.
- [`docs/pseudocode.md`](pseudocode.md) — natural-English walkthrough
  of `utils.check_dep3_header` + `_ChrootMixin.pre_install` /
  `post_install`.
- Memory: `feedback_dep3_header_40_line_limit.md`,
  `feedback_verify_patch_layer_via_logs.md`,
  `feedback_prefer_preseed_over_code.md`.
