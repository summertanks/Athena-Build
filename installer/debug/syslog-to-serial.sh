#!/bin/sh
# Tail d-i's per-step syslog to the serial console for QEMU `-serial`
# capture.  Mirrors what rootskel does for tty5 inside the VM — same
# source (/var/log/syslog), different sink (/dev/ttyS0), no contention.
#
# Lands at /lib/debian-installer-startup.d/S99-syslog-to-serial via the
# engine overlay mapping in scripts/installer_chroot.py.  Sources are
# the data-layer file installer/debug/syslog-to-serial.sh.
#
# S99 prefix so we run AFTER rootskel's standard S20-S70 scripts
# (template load, frontend select, theme, menu launch).  By that point
# the syslog daemon is up and /var/log/syslog exists.  `tail -F`
# (capital F) re-tries by name if the file isn't there yet, so the
# script is robust to ordering.
#
# Skips silently when /dev/ttyS0 isn't a character device — lets a
# physical install (no serial port) ship the same ISO without the
# script blowing up at boot.

set -e
[ -c /dev/ttyS0 ] || exit 0

# Run in background; we don't want this to block the rest of d-i init.
( tail -F /var/log/syslog 2>/dev/null > /dev/ttyS0 ) &

# Banner so the operator knows the serial pipe is wired when they're
# watching `-serial mon:stdio`.
echo "[debug] d-i syslog → /dev/ttyS0 (Athena installer debug hook)" \
     > /dev/ttyS0 2>/dev/null || true
