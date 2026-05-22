# installer/branding/ — RETIRED

The Phase-6-original mechanism (overlay `debconf-overrides.dat` from
this directory into the chroot at iso-build time) was replaced
2026-05-22 by COMP-01f Phase 1: ship the overrides + startup hook
inside `fork/source/athena-installer-data/`.

## Where the overrides live now

- **Data file:** `fork/source/athena-installer-data/data/debconf-overrides.dat`
  → installed to `/usr/share/athena-installer-data/debconf-overrides.dat`
  in the installer ramdisk
- **Apply hook:** `fork/source/athena-installer-data/data/S40-athena-branding`
  → installed to `/lib/debian-installer-startup.d/S40-athena-branding`,
  runs at install boot BEFORE main-menu

## Why the move

Per `docs/branding-methodology.md` Principles P1 + P2: branding is
packaged content we own, not an engine-time overlay.  The udeb is
versioned, auditable via `dpkg -l athena-installer-data`, ships in
the same closure as the rest of the installer-side identity files,
and follows the Kali / Devuan / Parrot pattern documented in § 3 of
the methodology doc.

## What this directory holds now

Nothing functional — the stub file `debconf-overrides.dat` here is
retained only as a historical placeholder.  Editing it has no
effect; the live file is in the fork pkg.

This directory will be removed once a follow-up cleans up the empty
scaffolding.
