# installer/ — d-i runtime data layer

Everything in this tree is **data** consumed by the installer build engine.  
The engine code under `scripts/` never inspects file *contents* here — it only
copies files from this tree into the installer chroot (or onto the installer
ISO at mastering time) per the mapping below.

That means: to rebrand, change preseed defaults, swap a splash image, or
ship a new config file into the installer chroot, **edit a file under
`installer/` — do not touch `scripts/`**.

## Engine mapping

This is the only place the engine "knows about" `installer/` paths.

| Source in this tree                          | Target on chroot / ISO                                       | Stage      | v1 status |
|----------------------------------------------|--------------------------------------------------------------|------------|-----------|
| `installer/preseed/preseed.cfg`              | chroot:`/preseed.cfg`                                        | chroot-build | shipped empty (operator answers all) |
| `installer/cdebconf/cdebconf.conf`           | chroot:`/etc/cdebconf.conf`                                  | chroot-build | absent (cdebconf-udeb's default `driver newt` wins) |
| `installer/branding/debconf-overrides.dat`   | chroot:`/var/cache/debconf-overrides.dat` (applied by hook)  | chroot-build | shipped empty (no Athena rebrand yet) |
| `installer/boot/isolinux.cfg`                | iso-stage:`/isolinux/isolinux.cfg`                           | iso-build  | deferred to Phase 7 |
| `installer/boot/grub.cfg`                    | iso-stage:`/boot/grub/grub.cfg`                              | iso-build  | deferred to Phase 7 |
| `installer/boot/splash.png`                  | iso-stage:`/isolinux/splash.png`                             | iso-build  | deferred to Phase 7 |

Files that don't exist are silently skipped.  Files that exist but are empty
(or only contain comments) are skipped too — same as absent.

## Per-subdir READMEs

Each subdir has its own README with concern-specific details (preseed
question reference, debconf-set-selections format, boot config gotchas).

## Adding a new file

1. Drop the file under the appropriate subdir.
2. If the file's target path isn't already in the mapping table above:
   - Add a row to the table here.
   - Wire one line in the engine: `_copy_if_present(src, dst)`.
3. Re-run `chroot build installer` (or `iso build installer` for boot-stage files).

No changes to dep-tree, source-build, or any non-installer engine code.
