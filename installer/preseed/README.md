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

## Current entries

- `netcfg/get_hostname` → `asgard`.  Stock d-i ships `Default: debian`
  in netcfg's templates; this override sets the hostname default
  shown on the "Hostname:" prompt to our distribution identity.
  DHCP-supplied hostname still wins when present.

Everything else is still answered interactively — locale, keyboard,
user, disk, etc.  Add lines here only when we have evidence the
preseed key actually takes effect in our build (some d-i keys like
`apt-setup/disable-cdrom-entries` were no-ops for us).

## Authoritative reference

- d-i Installation Guide, appendix B: <https://www.debian.org/releases/stable/amd64/apb.en.html>
- Templates available: walk `/var/lib/dpkg/info/*.templates` in any built
  installer chroot — every question is defined there.
