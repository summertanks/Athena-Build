# installer/debug/

Optional installer-runtime hooks for debugging.  Each file here lands
under `/lib/debian-installer-startup.d/SXX-<name>` in the installer
chroot per the engine mapping in `scripts/installer_chroot.py`.

rootskel's `/sbin/debian-installer` init script runs everything in
`/lib/debian-installer-startup.d/` as run-parts at PID 1's startup —
that's where our hooks fire.

## v1 files

| File                    | Target in chroot                                              | Purpose |
|-------------------------|---------------------------------------------------------------|---------|
| `syslog-to-serial.sh`   | `/lib/debian-installer-startup.d/S99-syslog-to-serial`        | Tails `/var/log/syslog` to `/dev/ttyS0` so QEMU's `-serial file:...` or `-serial mon:stdio` captures the same per-step d-i log content that's normally only visible on tty5 inside the VM. |

## Removing for a non-debug ISO

Delete the file(s) from `installer/debug/` and re-run `iso build
installer`.  The engine treats missing overlay files as no-ops — see
`installer/README.md`.

`installer/boot/grub.cfg` also needs its `console=ttyS0,...` term
removed for a fully production-clean kernel cmdline.
