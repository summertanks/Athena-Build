# installer/boot/

Boot-loader configuration + assets for the installer ISO.  Engine reads
these at iso-build time (Phase 7), not at chroot-build time.

## Files (when added in Phase 7)

| File                    | ISO path                       | Purpose |
|-------------------------|--------------------------------|---------|
| `isolinux.cfg`          | `/isolinux/isolinux.cfg`       | BIOS boot menu — entries, defaults, kernel cmdline |
| `grub.cfg`              | `/boot/grub/grub.cfg`          | UEFI boot menu — same purpose for EFI hosts |
| `splash.png`            | `/isolinux/splash.png`         | 640×480 indexed PNG, BIOS boot splash |
| `theme.txt`             | `/boot/grub/themes/athena/theme.txt` | GRUB graphical theme (optional) |

## v1 status

**Empty.**  Phase 5 only builds the installer chroot.  Phase 7 wires
`iso build installer`, at which point we'll either:
- write minimal isolinux.cfg + grub.cfg pointing at the installer kernel/initrd, OR
- generate them programmatically from a template in the engine (TBD).

Either way, the assets live here, not in code.

## Authoritative reference

- isolinux: <https://wiki.syslinux.org/wiki/index.php?title=ISOLINUX>
- grub: <https://www.gnu.org/software/grub/manual/grub/grub.html#Configuration>
