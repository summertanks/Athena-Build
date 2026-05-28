# Plan — SECBOOT-01: self-signed Secure Boot via MOK enrollment

## Status: DEFERRED (2026-05-28) — not blocking current builds; tracked here for later

## Context

Athena currently has **no Secure Boot support**: `shim-signed` is not in
`pool.list`, so `grub-installer`'s `apt-install shim-signed` step soft-fails
(non-fatal), `06athena-disable-cdrom` runs after `08hw-detect`, and the
install completes on machines where Secure Boot is disabled (or doesn't
exist).  On machines that ship with Secure Boot mandatorily enabled (most
modern OEM laptops), users have to enter firmware setup and disable it
before the install will boot.

This plan is the path to **self-signed Secure Boot without depending on
Debian's signing chain or paying for Microsoft's UEFI signing program** —
the approach used by Pop!_OS in their earlier releases and by some
hardware-vendor distros.  Trade-off vs. the alternatives:

| Path | What it requires | Pros | Cons |
|---|---|---|---|
| **A. Microsoft signs our shim** | Microsoft Partner Center account + EV code-signing cert (~$500/yr) + 4–12 week first-time review + re-submit each shim release | Distro Secure Boot works out of the box on any consumer PC | Months of work, ongoing maintenance, tracks Microsoft UEFI CA 2023 rotation |
| **B. Reuse Debian's chain via tunneling** | Tunnel shim-signed + shim-helpers-amd64-signed + grub2 + grub-efi-amd64-signed + linux-signed-amd64 | Cascade depth 0 confirmed; no new infra | Athena is downstream of Debian's trust; tied to Debian's signed kernel ABI |
| **C. Self-signed + MOK enrollment** (THIS PLAN) | Generate Athena signing key + build shim with our vendor cert + sign grub & kernel ourselves + MOK enrollment hook at first boot | Sovereignty over the entire chain; no Microsoft / no Debian-trust dependency | User-interactive step at first boot (blue UEFI screen); MOK enrollment UX is OEM-specific; **need a Microsoft-signed shim binary somewhere to anchor firmware trust** (the wrinkle, see below) |
| **D. No Secure Boot** (CURRENT) | Drop shim-signed entirely | Zero new work | Users with mandatory Secure Boot must disable in firmware |

**The wrinkle in Path C**: a self-built shim isn't itself trusted by firmware
`db` (Microsoft's UEFI CA isn't ours).  The shim binary has to be signed by
*something* in `db` for the firmware to launch it.  The pragmatic options:

1. **Borrow Debian's Microsoft-signed shim binary** (just the
   `shimx64.efi.signed` file from Debian's `shim-signed` package) — but
   that shim has *Debian's* vendor cert embedded, so it trusts Debian's
   grub key, not ours.  Our self-signed grub won't load.  This is the
   exact problem MOK solves: shim's `MokManager` lets the user enroll
   our key at first boot, and shim then trusts it for verifying the next
   stage.  So we ship Debian's shim binary, our own grub+kernel signed
   with our key, and an installer hook that stages MOK enrollment for the
   blue-screen UI on first boot.

2. **Get our own shim Microsoft-signed** — that's Path A.  Out of scope
   for this plan.

So Path C as described here is more precisely: **reuse Debian's
Microsoft-signed shim binary, but sign everything *above* shim with our
own key, and gate the trust transition through MOK enrollment**.

## Scope

In:
- Athena Secure Boot signing infrastructure (keypair, signing pipeline).
- A new installer step that primes MOK enrollment with our public key.
- Signed grub + signed kernel produced by our build (using our key).
- Shim binary sourced from Debian (the binary file only — we don't need the
  full `shim-signed.deb` chain, just the `shimx64.efi.signed` blob).
- Documentation of the user-facing first-boot MOK enrollment flow.

Out:
- Path A (Microsoft signing).  Re-evaluate when Athena has the org footprint
  and budget.
- Auto-add-to-firmware-`db` (impossible without KEK-trusted signature; would
  require user to put firmware in Setup Mode manually — worse UX than MOK).
- Signed kernel modules (separate concern; can be layered on later via the
  same key).

## Components

### 1. Signing key

A long-lived RSA-2048 (or RSA-3072) keypair, generated once per
distribution.  Public part embedded into shim (and into grub's keyring) at
build time and shipped as a `.cer` to the target for MOK enrollment.
Private part lives in `signing/` outside the repo, protected by an existing
mechanism (project keyring infrastructure already exists for the repo
signing key — extend it for the EFI signing key).

```
signing/athena-secureboot.key   — private, 0600, NEVER committed
signing/athena-secureboot.crt   — public X.509 cert, committed as a binary blob
signing/athena-secureboot.cer   — DER-encoded form for MOK enrollment
```

Generation: standard `openssl req -newkey rsa:2048 -keyout sb.key -new
-x509 -sha256 -days 3650 -subj "/CN=Athena Secure Boot Signing
Key/O=Athena Linux Project" -out sb.crt`.

### 2. Sign our grub + kernel

Currently `linux-signed-amd64` is built from source without a key — it
produces filenames that imply signing but the binaries are effectively
unsigned (silent bug; harmless today because Secure Boot is off).  Two
options:

- **Replace `linux-signed-amd64` with our own signing pass.**  Drop it from
  the build, post-process the `linux`-source kernel binaries with
  `sbsigntool sign --key athena-secureboot.key --cert athena-secureboot.crt
  --output signed.efi vmlinuz`, repack `.deb`.  Cleaner.
- **Patch `linux-signed-amd64`'s source build to use our key.**  Less code
  diff but more fragile (Debian source assumes their key infrastructure).

Pick the first.  Add a build-pipeline hook (alongside `strip_nmu_from_deb`'s
post-build position) that recognises kernel binaries (`linux-image-*-amd64`)
and signs them with `athena-secureboot.key`.

Apply the same to grub-EFI binaries (`grub-efi-amd64`, `grub-efi-amd64-bin`).
Grub's case is more subtle — the signed binary needs the right keyring built
in.  Likely easier to use `grub-mkstandalone` to produce our signed grub EFI
binary at build time and ship it as a separate package (`athena-grub-signed`
fork) than to retrofit `grub2`'s upstream signing dance.

### 3. Shim binary

Don't fork `shim` or build it ourselves — just **copy
`/usr/lib/shim/shimx64.efi.signed` out of Debian's `shim-signed.deb`** and
ship it as an Athena resource.  At install time, the EFI partition gets:
- `/EFI/athena/shimx64.efi`  (Debian's MS-signed shim)
- `/EFI/athena/grubx64.efi`  (our Athena-signed grub)
- `/EFI/athena/mmx64.efi`    (Debian's MokManager from the same .deb)
- `/EFI/athena/athena-secureboot.cer`  (our public cert, for MOK enrollment)

This requires extracting + redistributing only the EFI blob from Debian's
shim-signed.  The licensing question is real (it's a Microsoft-signed binary
of GPL code) — Debian and Ubuntu redistribute it freely, but verify the
license terms specifically allow embedding the binary in derivatives.
Linux Mint, Pop!_OS etc. do this in practice.

### 4. MOK enrollment hook

A `finish-install.d` script that:
1. Stages our public cert: `cp /cdrom/.disk/athena-secureboot.cer
   /target/var/lib/shim-signed/mok/athena.cer`.
2. Runs `mokutil --import /target/var/lib/shim-signed/mok/athena.cer` in
   the target (sets a one-time password the user must enter on the blue
   screen for confirmation).
3. Surfaces a clear post-install message: "On first boot, you'll see a blue
   UEFI screen titled 'Perform MOK Management' — choose 'Enroll MOK',
   confirm with the password you set, and reboot.  This trusts Athena's
   signing key at the firmware level so Secure Boot can verify our kernel."

`mokutil --import` triggers shim's `MokManager` on next boot.

### 5. grub-installer config

`grub-installer` needs to know to install Athena's grub + shim to the ESP,
not Debian's.  Update the install-time grub config to point at `/EFI/athena/`
and use our shim binary.

### 6. Tests / verification

- Source-inspection test that the signing pipeline runs.
- Unit test for the MOK hook (idempotent, writes the .cer, runs mokutil).
- Manual: real install on a Secure-Boot-enabled VM (qemu/OVMF supports
  Secure Boot), follow MOK enrollment, confirm system boots Athena-signed
  kernel.

## Critical files (anticipated)

- `signing/athena-secureboot.{key,crt,cer}` — keypair + cert (key gitignored,
  crt committed).
- `scripts/signing.py` — extend to load the EFI signing key.
- `scripts/buildcontainer.py` — add post-build EFI-signing pass (similar
  position to `strip_nmu_from_deb`).
- `fork/source/athena-grub-signed/` — new fork that produces our signed
  grub EFI binary (rather than trying to retrofit Debian's signing dance
  into `grub2`).
- `installer/secboot/athena-secureboot.cer` — copy the public cert into the
  installer data layer (overlaid to `.disk/athena-secureboot.cer`).
- `installer/finish-install/07athena-mok-import` — staged between 06
  (athena-default-source) and 08hw-detect.  Actually, since the existing
  06 was renumbered to 11 (it disables cdrom AFTER hw-detect), the MOK
  hook can be at 07 — runs after default-source, before hw-detect, before
  disable-cdrom.
- `scripts/iso_installer.py` — extra ESP-staging step to lay out the four
  `/EFI/athena/*` files.
- `tests/test_module.py` — test entries.

## Verification

- `repo audit` clean (signing additions don't introduce new dep mismatches).
- Build: `linux-image-*-amd64` binaries pass `sbsigntool verify` against
  the Athena public cert.
- Install (qemu OVMF + Secure Boot enabled):
  1. Boot installer (uses Debian's shim → unsigned-by-our-key state means
     installer itself may need Secure Boot temporarily off, or installer
     uses Debian's signed kernel).
  2. Install completes.
  3. Reboot — `MokManager` blue screen appears.
  4. Enroll the Athena key, confirm with the staged password.
  5. Reboot again — system boots Athena-signed grub + Athena-signed kernel
     under Secure Boot.

## Notes / open questions

- The installer ITSELF needs to boot under Secure Boot on the target.  If
  the installer ISO contains an Athena-signed kernel, Secure Boot will
  refuse the installer (our key isn't enrolled yet — MOK enrollment hasn't
  happened).  Either: (a) the installer kernel is Debian's signed kernel
  (we ship Debian's `linux-image-*` for the installer ramdisk only — sidesteps
  the install-time Secure Boot question), or (b) require user to disable
  Secure Boot for install, re-enable after MOK enrollment, OR (c) install
  also runs MOK enrollment for our key — but the installer kernel ALREADY
  ran by then.  Most likely (a).
- The "signed kernel modules" question is separate but related; revisit
  after MOK works for the kernel itself.
- Re-evaluate Path A periodically.  Microsoft's UEFI CA 2023 rotation is
  the existential question for Path B (reuse Debian's chain) too — both A
  and B depend on whichever CA OEM firmware actually trusts.
