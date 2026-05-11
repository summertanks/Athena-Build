# installer/branding/

debconf overrides that rewrite visible installer strings (the "Debian"
→ "Athena" rebrand) without patching every step udeb's templates file.

## What is debconf-overrides.dat?

A `debconf-set-selections`-format file that, when applied in the
installer chroot, overrides specific template defaults.  Same syntax as
preseed but conceptually different: preseed pre-answers questions to
skip prompts; overrides change what the prompt *says* when shown.

Common overrides:

```
# Main-menu title (shown at the top of every cdebconf screen)
d-i debian-installer/main-menu-title string Athena installer main menu

# Step "title" templates (the menu item lines)
d-i debian-installer/base-installer/title string Install the base system
d-i debian-installer/user-setup-udeb/title string Set up users and passwords
```

## v1 status

**Shipped empty.**  The installer renders with stock "Debian installer"
strings.  We rebrand progressively — populate this file once the basic
flow boots, runs, and successfully installs a target system.

The hard limit: a handful of strings are baked into compiled binaries
(notably the `[!]` / `[!!]` priority sigils in cdebconf-newt) and
cannot be overridden via this file.  Those require source patches
under `patch/source/cdebconf/<ver>/`.
