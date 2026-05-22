# installer/boot/

Boot-loader configuration + assets for the installer ISO.  Engine reads
these at iso-build time (Phase 7), not at chroot-build time.

## Files

| File                    | ISO path                              | Purpose |
|-------------------------|---------------------------------------|---------|
| `grub.cfg`              | `/boot/grub/grub.cfg`                 | BIOS + UEFI boot menu — entries, defaults, kernel cmdline, gfxterm setup |
| `grub-background.png`   | `/boot/grub/grub-background.png`      | 800×600 PNG background for the gfxterm boot menu (COMP-01f Phase 2 splash) |
| `regenerate-bg.py`      | (not staged)                          | Regenerator for grub-background.png — run when palette/identity changes; not shipped on the ISO |

`iso_installer.py:_stage_grub_cfg` copies `grub.cfg` (required) and any
listed optional assets (currently just `grub-background.png`) into the
ISO's `/boot/grub/` at iso-build time.  A missing optional asset is
non-fatal — GRUB's `if loadfont … ; then … fi` guard in `grub.cfg`
falls back to text mode.

## Updating the boot background

The PNG is a committed binary blob so the build doesn't depend on PIL /
ImageMagick / librsvg at runtime.  When the visual identity changes
(palette refresh, new resolution, etc.), regenerate from source:

```
python3 installer/boot/regenerate-bg.py
git add installer/boot/grub-background.png
```

The generator is the single source of truth — it mirrors the Aegis
visual identity from `fork/source/athena-branding/data/aegis-dark.svg`
(midnight indigo radial gradient + sparse stars + gold-stroked Greek
alpha).  Per `docs/branding-methodology.md` Pattern B (drop-in static
asset we own) — no upstream GRUB patches, no per-version churn.

## Why GRUB only (no isolinux)

The installer ISO uses `grub-mkrescue` for both BIOS El-Torito and
UEFI ESP boot — one toolchain, one config file (`grub.cfg`), one
menu definition.  isolinux (the SYSLINUX BIOS bootloader) would be
a parallel config we'd have to keep in sync; the maintenance cost
isn't worth the legacy compatibility for our target hardware.

## Authoritative reference

- grub: <https://www.gnu.org/software/grub/manual/grub/grub.html#Configuration>
- grub gfxterm: <https://www.gnu.org/software/grub/manual/grub/grub.html#gfxterm>
