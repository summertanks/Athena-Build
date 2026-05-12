#!/bin/sh
# Load /preseed.cfg into cdebconf's runtime DB.
#
# Stock d-i does NOT auto-load /preseed.cfg from the initrd root — the
# only existing references to it (reopen-console, S60auto-install) just
# check that the file exists and adjust console / auto-install state.
# Loading the values themselves requires an explicit
# `debconf-set-selections /preseed.cfg`.  In a normal Debian build that
# script is shipped by `simple-cdd` or pulled in via the `auto=true
# preseed/file=...` kernel cmdline; we have neither, so we ship this
# minimal loader as part of the Athena overlay.
#
# Lands at /lib/debian-installer-startup.d/S25-load-preseed via the
# engine overlay mapping in scripts/installer_chroot.py.  S25 so it
# runs AFTER S20templates (templates must be loaded before
# debconf-set-selections can register their values) and BEFORE the
# udebs that read those values (S60auto-install, plus everything
# anna unpacks later).
#
# Caught 2026-05-12 — preseed.cfg's apt-setup/disable-cdrom-entries
# was a no-op because nothing was loading the file.  Bootstrap-base
# trace was clean, but the post-install apt-cdrom-add cascade still
# fired exactly as before.

set -e

[ -f /preseed.cfg ] || exit 0

# Don't fail the boot if a single preseed line is malformed — log and
# continue.  cdebconf's debconf-set-selections returns nonzero on bad
# input; we explicitly tolerate that so a typo in preseed.cfg doesn't
# brick the installer.
if debconf-set-selections /preseed.cfg 2>>/var/log/syslog; then
    logger -t load-preseed "Loaded preseed values from /preseed.cfg"
else
    logger -t load-preseed "WARNING: debconf-set-selections /preseed.cfg returned non-zero"
fi
