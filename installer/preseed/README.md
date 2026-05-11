# installer/preseed/

Preseed file(s) baked into the installer chroot at `/preseed.cfg`.

## What is preseed?

A file of pre-answered debconf questions that lets d-i run unattended (or
mostly-unattended) by skipping prompts the operator would otherwise see.
Format is `debconf-set-selections`:

```
# owner question type value
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i mirror/protocol string file
d-i mirror/file/directory string /cdrom
d-i passwd/root-password password           # left blank → operator answers
d-i partman-auto/method string regular
```

Lines starting with `#` are comments.  Lines may continue with `\`.

## v1 status

`preseed.cfg` ships **empty** (comment-only).  The operator answers every
prompt interactively — no Athena-locked defaults yet.  Once we know which
questions to lock and which to keep interactive (likely: locale/keyboard
locked, hostname/user/disk interactive), populate this file.

## Authoritative reference

- d-i Installation Guide, appendix B: <https://www.debian.org/releases/stable/amd64/apb.en.html>
- Templates available: walk `/var/lib/dpkg/info/*.templates` in any built
  installer chroot — every question is defined there.
