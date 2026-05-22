# Branding methodology — Athena/Asgard

**Audience:** future-me when adding the next branding surface (Plymouth,
GDM theme, ANSI-art motd, cdebconf strings, GRUB theme, login banner,
etc.) — and any contributor evaluating "should I patch this?"

**Thesis in one line:** never patch upstream source for branding;
always ship branding via packages we own.

This doc captures the principles we apply, the cross-distro patterns
that exist, the patterns we've already adopted, and the patterns we
explicitly reject.  It is the reference companion to
[fork/source/README.md](../fork/source/README.md), which describes the
mechanism layer (how a fork package overrides upstream).  This doc is
the policy layer (when to use which mechanism, and why).

---

## 1. Five principles

### P1. Never patch upstream for branding.

Branding is permanent.  Patches are per-version maintenance.  Every
upstream point release (`base-files 12.4+deb12u14` →
`12.4+deb12u15`, cdebconf `0.265` → `0.270`, ...) would need the
patch refreshed, conflicts resolved, and re-tested against the new
content.  For an automated build that wants to track upstream
security updates without intervention, that's a permanent tax we
won't pay.

Patches are appropriate for **bug fixes** (small, targeted, expected
to be obviated by an upstream fix eventually) and for **build
adaptations** (nodoc tolerance, examples-sed skipping under
nodoc — `patch/source/wpa/`, `patch/source/libwww-perl/`).  They
are NOT appropriate for "change the visible string from Debian to
Athena."

### P2. Own the data, not the upstream.

When a string, image, or behaviour needs to be ours, ship a package
we own that contains it.  Use the upstream's documented extension
mechanism (debconf overrides, theme files, drop-in directories,
Provides/Conflicts/Replaces) to make our content take precedence.
Upstream doesn't know or care about our content; we don't depend on
upstream's source layout staying constant.

The companion package is the durable unit.  When upstream bumps,
our package's `Depends: cdebconf-newt (>= 0.270)` (or wherever)
keeps working; no source merge required.

### P3. Accept irreducible residue rather than fight it forever.

A handful of strings ARE baked into compiled binaries with no
override path: cdebconf-newt's priority sigils (`[!]`, `[!!]`),
cdrom-detect's syslog line `"Searching for Debian installation
media..."`, certain dialog headers compiled into d-i framework
binaries.

The cost-benefit math: maintaining patches against ~5 strings
across ~3 packages forever, vs. accepting that operators
occasionally see a stale upstream string in a syslog tail or a
priority sigil.  Kali made this call.  We do the same.

**Catalogue the residue** (§ 7 below) so it's auditable rather than
invisible.  If a residual string becomes intolerable on a future
surface, the conversation shifts to "is this enough to justify the
treadmill cost" — not "do we need to patch."

### P4. Same-name fork is the strongest claim, but the heaviest cost.

When we want to control ALL of an upstream package's file paths —
including files we don't actively diff (so future leaks in those
files we already own) — fork the package by name.  Our binary
supersedes upstream's entirely via `cache.py`'s fork-supersede
mechanism; debootstrap and apt only ever see our version.

Cost: re-merge against upstream on every point release.  Industry
consensus for `base-files` specifically: 6 of 8 surveyed Debian
derivatives use same-name fork for this one package.

Use same-name fork **only** when narrower mechanisms (P/C/R,
dpkg-divert, companion package) can't reach the surfaces you need
to own.  See `fork/source/README.md` § 4.

### P5. Companion package + Provides/Conflicts/Replaces is the lighter touch.

When we want to overlay a narrow set of files (a wallpaper, a
debconf templates database, a GRUB background) without forking the
whole upstream package, ship a companion package that declares the
upstream's identity in Provides/Conflicts/Replaces.  Our binary
takes the slot; the upstream binary cannot coexist.

This is what `athena-branding` does for `desktop-base` (the GNOME
wallpaper picker package): same surface, different package name,
zero source patches against the upstream.

Use companion + P/C/R for: cosmetic overlays, identity files where
the upstream pkg isn't Essential, theme udebs.  See
`fork/source/README.md` § 2.

---

## 2. Decision tree — I have a new branding surface, what do I do?

```
A new "Debian" string / image / behaviour shows up on a fresh install.
Where does it live?

  ├── In a debconf template (.templates file shipped by an upstream udeb)?
  │     → Use Pattern A (debconf overrides via companion udeb).
  │       This is the cheapest win and handles 60-70% of installer text.
  │
  ├── In an XML / config / theme file the upstream pkg reads from a
  │   known drop-in directory (/etc/gdm3/, /etc/grub.d/, /usr/share/
  │   backgrounds/, /usr/share/plymouth/themes/, ...)?
  │     → Use Pattern B (drop-in file in a companion package).
  │       Either P/C/R override or just ship alongside with a higher
  │       sort-order filename so we win at runtime composition.
  │
  ├── In a config file that's a conffile (/etc/*) shipped by an
  │   Essential package?
  │     → Use Pattern C (dpkg-divert).
  │       The conffile machinery requires us to OWN the path; divert
  │       is the only safe overlay mechanism without forking the whole
  │       Essential pkg.  Caveat: divert + the dpkg conffile-prompt
  │       trap interact badly — ship the file under
  │       /usr/share/<our-pkg>/ + postinst cp, not directly to /etc/.
  │       (We learned this with athena-base-files 1.0.0 → 1.0.1; see
  │       commit history for the .dpkg-dist trap incident.)
  │
  ├── In a file path no upstream package owns, but where upstream's
  │   maintainer scripts EXPECT a specific content shape (e.g.
  │   /etc/lsb-release, /etc/default-release)?
  │     → Use Pattern D (net-new package shipping the file).
  │       Athena-installer-data does this for installer-side identity
  │       files.
  │
  ├── In essentially every file of an upstream package (we want to
  │   own the whole identity surface that pkg defines)?
  │     → Use Pattern E (same-name fork).
  │       base-files is the canonical case.  This is the last resort —
  │       fork only when narrower mechanisms can't reach.
  │
  ├── Baked into a compiled binary with no template / config override
  │   path?
  │     → Document under § 7 (irreducible residue).  Don't patch.
  │
  └── In a behaviour (not a string) — an upstream pkg installs a hook
      that calls apt-install on a Debian-only target?
        → Use Pattern F (strip via durable allowlist mechanism).
          Reactive hardcoded strip lists are anti-pattern (CONF-10).
```

---

## 3. Patterns from the field — d-i ecosystem focus

The Athena project uses Debian Installer (d-i).  Distros that use d-i
all converged on a small set of branding patterns.  Distros that
replaced d-i entirely (Ubuntu Ubiquity/Subiquity, Mint
live-installer-launcher, Pop!_OS distinst) sidestep this whole
discussion — out of scope.

The four d-i-using exemplars we lean on, in rough order of relevance:

### The two override mechanisms within Pattern A

**Critical sub-distinction discovered 2026-05-22 during COMP-01f
testing.**  Within "ship a companion udeb that overrides debconf",
there are TWO independent mechanisms keyed by **template TYPE**.
Pick the wrong mechanism and the hook fires successfully but the
override is a silent no-op.

**A-VALUE — `debconf-set-selections`**

For templates of type `string`, `boolean`, `select`, `multiselect`.
These have a Description (the prompt) AND a separate user-input
value.  `debconf-set-selections` sets the value portion; the prompt
text stays whatever the template said.  This is what most "preseed"
documentation describes.

Example: `netcfg/get_hostname` is type=string — set-selections pre-fills
the hostname field with our distribution name.

**A-TEXT — `debconf-loadtemplate`**

For templates of type `text` or `note`.  These have NO user-input
value; the rendered string IS the `Description:` field of the
template.  `debconf-set-selections` against type=text is a no-op
(no value to set).  To rebrand the rendered string, ship our own
templates file that re-declares the same template path with our
`Description:`, and `debconf-loadtemplate <our-pkg> <our-templates>`
**after** the upstream templates have already loaded.  debconf is
keyed by template path (not owning package); LAST load wins on
path conflict.

Example: `debian-installer/main-menu-title` is type=text — the
main-menu title shown at the top of every screen.  The COMP-01f
v1 (1.1.0) shipped a debconf-set-selections line for this and the
override was a silent no-op; 1.2.0 fixed it by adding the
templates-file mechanism.

**How to identify which mechanism a template needs**

Inside a built `buildroot-installer/`, after S20templates ran:
```
grep -A1 "^Template: <path-you-want-to-override>" \
  buildroot-installer/var/lib/dpkg/info/*.templates
```
Or before any build, inspect the source udeb directly:
```
dpkg-deb -e repo/main/main-menu_<ver>_<arch>.udeb /tmp/mm
grep -A1 "^Template:" /tmp/mm/templates | less
```

Look at the `Type:` line.  `string|boolean|select|multiselect` →
use A-VALUE.  `text|note` → use A-TEXT.

**Where the hook must run**

Either mechanism's apply hook must run AFTER S20templates (which
loads `*.templates` from `/var/lib/dpkg/info/` into the debconf db)
and BEFORE S60main-menu (the first udeb that queries the affected
templates).  S40 is the safe slot we use.

---

### Kali Linux — `kali-defaults` family

Kali ships a multi-package source called `kali-defaults` that
produces several binaries, including a `kali-defaults-udeb` that
lands in the installer ramdisk via `installer.list` equivalent.

The udeb ships:
- A debconf-overrides file (debconf-set-selections format) listing
  the visible-string template keys Kali wants to rebrand
  (`debian-installer/main-menu-title`, step titles, exit titles, ...)
- A startup hook under `/lib/debian-installer-startup.d/SNN-kali-*`
  that runs `debconf-set-selections /path/to/overrides` BEFORE
  main-menu loads (S40-ish, so it beats S60-main-menu)
- Visual assets: cdebconf-newt color palette, GRUB background image
- Postinst hooks to wire the visual assets into the right paths

Source patches against cdebconf / cdrom-detect / main-menu: **zero**.
Strings baked into binaries: accepted as residue.

**This is the dominant pattern; we adopt it directly.**

### Devuan — `devuan-baseconf` (and successors)

Devuan, being a sysvinit-preserving Debian derivative, has minimal
visual ambitions but does override the visible identity strings.
The pattern is the same as Kali's: a companion udeb owns a
debconf-overrides file and a startup hook applies it.

Devuan demonstrates that the pattern is robust **without** custom
visual assets — you can rebrand just the strings if that's all you
care about.  The visual theme is optional.

### Parrot Security — `parrot-core` / `parrot-defaults`

Parrot is Debian-based with security-tooling focus.  Their branding
package follows the same pattern (debconf overrides + theme assets +
P/C/R override on `desktop-base`-equivalent for GNOME-side
wallpapers).  Same shape as Kali.

### Pop!_OS — `pop-default-settings` (the contrarian)

Pop!_OS is the outlier worth knowing about: they use **dpkg-divert**
heavily for runtime-system branding (wallpapers, GDM theme,
GRUB defaults) rather than P/C/R.  The justification is that divert
keeps the upstream package's other files intact and undisturbed —
they only want to override the 3-4 identity files, not claim the
whole package's namespace.

Pop!_OS doesn't ship a d-i-based installer (they use distinst), so
the divert pattern doesn't apply to their installer specifically.
But the **principle** (narrow surface overlay via divert) is worth
keeping in our toolkit for surfaces where P/C/R would be too broad
a claim.

We use divert at the runtime-system layer (cf. the original
athena-base-files 1.0.0 design, before we migrated to same-name
fork for that pkg specifically — divert hit the conffile trap; for
non-conffiles it works fine).

---

## 4. Patterns from the field — runtime system

Beyond the installer, the same principles apply to the booted system.
The patterns we've seen surveying derivatives' runtime branding:

| Derivative | base-files | Visual branding | Conf/identity files |
|---|---|---|---|
| Ubuntu | same-name fork | `ubuntu-mate-*`/`ubuntu-themes` companion debs | own packages |
| Linux Mint | same-name fork + `mint-info` | `mint-themes`/`mint-backgrounds` companion debs | own packages |
| elementary OS | same-name fork | `elementary-default-settings` companion deb | own packages |
| **Pop!_OS** | upstream + `pop-default-settings` divert | `pop-theme` companion deb | dpkg-divert |
| Kali | same-name fork | `kali-themes-common` companion deb | own packages |
| Parrot | same-name fork | `parrot-themes` companion deb | own packages |
| Devuan | same-name fork | minimal | own packages |
| MX Linux | own `mx-common` (not same-name fork) | `mx-tools` + theme debs | P/C/R override |

**Pattern dominance:**

- For `base-files`: **6 of 8 use same-name fork.**  This is why
  Athena's `fork/source/base-files/` adopted Path X.
- For visual assets (wallpapers, themes, GRUB backgrounds): **all 8
  use companion packages.**  Never patches.
- For conffile-shape identity files (`/etc/issue`, `/etc/os-release`,
  `/etc/lsb-release`): **same-name fork OR companion-with-P/C/R**.
  Pop!_OS is the divert outlier.

---

## 5. Athena's current pattern catalogue

What we've already branded and which pattern each surface uses.
Update this table when a new surface lands.

| Surface | Pattern | Package | Notes |
|---|---|---|---|
| `/etc/issue` | E (same-name fork) | `fork/source/base-files/` | Was D (companion divert) at 1.0.0 — migrated to E after conffile trap |
| `/etc/issue.net` | E | `fork/source/base-files/` | Same |
| `/etc/os-release` | E | `fork/source/base-files/` | NAME="Asgard Linux", ID=asgard, LOGO=asgard |
| `/etc/motd` | E | `fork/source/base-files/` | via `share/motd` template |
| `/etc/dpkg/origins/debian` | E | `fork/source/base-files/` | Vendor: Athena Linux |
| GNOME wallpaper picker | B + companion | `fork/source/athena-branding/` | XML registration + SVG assets; declares `Provides: desktop-base` |
| Login wallpaper | B + companion | `fork/source/athena-branding/` | gsettings override (90_ priority for late merge) |
| User-settings logo (gnome-control-center About) | B | `/usr/share/pixmaps/asgard.svg` (athena-branding) | os-release `LOGO=asgard` triggers lookup |
| GRUB distributor string | B + companion | `fork/source/athena-branding/` | `/etc/default/grub.d/50-athena.cfg` token-substituted |
| Target-system GRUB boot menu background | **Pattern B** (drop-in static asset, owned by us) | `fork/source/athena-branding/` 1.2.0 — `data/50-athena.cfg` sets `GRUB_BACKGROUND=`; debian/rules renders `_build/png/grub-background.png` (1920×1080) from `data/aegis-dark.svg` via rsvg-convert; debian/install ships to `/usr/share/athena-branding/`; postinst re-runs update-grub on configure so the override takes effect even when athena-branding installs AFTER grub (typical pkgsel/tasksel order).  Closes the gap left by displacing desktop-base without replacing its GRUB-theme contribution. |
| Installer hostname default | Preseed | `installer/preseed/preseed.cfg` | `netcfg/get_hostname seen false` re-prompts with our default |
| Installer-side `/etc/lsb-release` | D (net-new udeb) | `fork/source/athena-installer-data/` | Token-substituted at build |
| Installer-side `/etc/default-release` | D | `fork/source/athena-installer-data/` | Same |
| Installer debootstrap codename script | D | `fork/source/athena-installer-data/` | Symlink `thor` → `sid` |
| Installer boot menu title ("Install Asgard") | Drop-in | `installer/boot/grub.cfg` | Direct file we own |
| Installer apt-source label ("Athena 0.1 _thor_ amd64 INSTALLER") | Engine-set | `iso_installer.py` | Set at build time, never touches upstream |
| Installer main-menu title ("Asgard installer main menu") | **A-TEXT** (debconf-loadtemplate) | `athena-installer-data` 1.2.0 — `athena-overrides.templates` re-declares `debian-installer/main-menu-title` with our Description; S40-athena-branding loads it after S20templates so we win the path conflict |
| Installer generic title | **A-TEXT** | Same — `debian-installer/title` re-declared |
| Installer hostname pre-fill | **A-VALUE** (debconf-set-selections) | `athena-installer-data` — `netcfg/get_hostname` set via `debconf-overrides.dat` (type=string) |
| Installer visual theme (newt colour palette) | **Residue per § 7** | cdebconf-newt has compiled-in palette; rejected source patch per P1 |
| Installer GRUB boot menu splash | **Pattern B** (drop-in static asset) | `installer/boot/grub-background.png` (committed binary, 800×600); `installer/boot/grub.cfg` `background_image` line gated by `if loadfont`; `iso_installer.py:_stage_grub_cfg` copies the asset alongside grub.cfg.  Regenerable via `installer/boot/regenerate-bg.py` (mirrors Aegis SVG identity in PIL).  Landed COMP-01f Phase 2 (2026-05-22). |
| Pre-pkgsel.d hooks that apt-install Debian-only pkgs | F (reactive — to be replaced) | `installer_chroot._strip_debian_residue_hooks` | Hardcoded 2-entry list; CONF-10 for durable allowlist |

---

## 6. Anti-patterns we reject

### A1. Per-version source patches against upstream templates

**Don't write** `patch/source/cdebconf/<ver>/9001-athena-strings.patch`.
Every upstream point release becomes a patch-refresh chore; conflicts
must be resolved by hand; the patch silently rots if the upstream
template structure changes; the cdebconf maintainer doesn't know we
exist.

If a string MUST be ours and there's no debconf override path,
escalate to either Pattern A (a smaller upstream that DOES expose
the string via debconf is upstream of the one with the baked
string — find it) or § 7 (accept the residue).

### A2. Hardcoded path lists in installer hooks that must be maintained per upstream rev

**Don't write** new entries to `installer_chroot._strip_debian_residue_hooks`'s
target tuple.  Each entry is a static path that breaks silently when
upstream renames a hook or splits it across files.  This is the
existing reactive pattern that CONF-10 was filed to replace.

Replace with a build-time allowlist mechanism (Pattern F): parse
every shipped `pre-pkgsel.d/*` for `apt-install X` calls;
cross-reference X against `repo/`; FAIL the build with an actionable
diagnostic when a target isn't installable, unless the hook is on
`installer/strip-hooks-allowlist`.  Forces explicit per-hit operator
review; catches new hooks automatically on next upstream bump.

### A3. Re-implementing what upstream already supports via debconf

If upstream exposes a string / behaviour via a debconf template, USE
that template.  Don't write a postinst hook that `sed`-s the rendered
string in /etc/, don't patch the calling code to substitute a
constant, don't `dpkg-divert` the template file.  `debconf-set-selections`
is the supported path.

### A4. Bundling unrelated overrides into one mega-patch

Even if patching is unavoidable for a non-branding reason (a real
bug fix), don't sneak branding edits into the same patch.  Keeps
the bug-fix patch reviewable and droppable when upstream fixes the
bug; keeps the branding decision separate so it doesn't masquerade
as a fix.

### A5. Same-name fork for narrow overlays

If you only need to override 1-3 files, **don't** same-name fork the
whole upstream package — pay the re-merge tax forever for very
little gain.  Use companion + P/C/R (Pattern B / § 5 in
fork/source/README.md) or divert (Pattern C).

Same-name fork is justified when you'd be re-merging on every
upstream bump anyway (we want to own the whole file surface) or
when the upstream is Essential AND we want to override most of its
content.  `base-files` is the canonical justified case.

---

## 7. Irreducible residue — strings/surfaces we accept

These are documented so they're auditable and so future operators
don't waste time trying to "fix" them.

### Installer ramdisk (cdebconf-newt + cdrom-detect + a few framework binaries)

| String / surface | Where | Visibility | Why we accept |
|---|---|---|---|
| `[!]` / `[!!]` priority sigils on dialog screens | baked into `cdebconf-newt` binary | every install screen | Patching cdebconf source for every point release is the exact treadmill P1 rejects.  Operators don't read `[!!]` as "Debian." |
| `Searching for Debian installation media...` | baked into `cdrom-detect` postinst messages | only visible in syslog tail / serial console | Not on the operator's screen during install; only visible via `journalctl` post-install if at all. |
| Various error dialog headers ("Debian Installer error") | baked into framework binaries | rare error paths | If we see this often, revisit; for now accept. |
| cdebconf-newt palette (red/teal default colour scheme) | compiled-in palette in cdebconf-newt frontend | every install screen | Confirmed 2026-05-22: cdebconf-newt doesn't honour newt's `NEWT_COLORS` / `NEWT_COLORS_FILE` env vars (those affect plain newt apps; cdebconf-newt has its own palette logic).  Even if it did, rootskel execs startup scripts as children rather than sourcing them, so an `export` in a hook dies with the hook's shell.  Both reasons stack — any env-var approach is dead.  Only path is patching cdebconf source per-release (rejected by P1). |

### Runtime system

| String / surface | Where | Visibility | Why we accept |
|---|---|---|---|
| `Origin: Debian` in apt source labels for upstream-mirror entries | apt's own Release file metadata for non-Athena pkgs | `apt-cache policy <pkg>` | Honest provenance: we're shipping upstream Debian binaries; pretending otherwise breaks audit. |
| Compiled-in package version (e.g. GRUB self-reporting `2.06-13+deb12u1`) | Baked into binaries at `dpkg-buildpackage` time via `PACKAGE_VERSION` / autotools / similar | `grub-pc --version`, occasional boot-time text lines, package-internal "About" strings | Our NMU-strip rewrites the .deb's filename and the control's Version: field (the dpkg metadata IS clean — `dpkg -l grub-pc` shows the stripped value), but the BINARY was compiled by Debian with the original PACKAGE_VERSION baked in.  Scrubbing this would require rebuilding the package from source after rewriting its autotools/cmake/configure version macro — not viable for the ~50 large upstream packages where this surface exists (grub, systemd, gcc, glibc, kernel, etc.).  The dpkg metadata is the authoritative version for dependency resolution and update tracking; the binary's self-reported version is provenance information about the build host that produced it. |
| Installer-ISO bootloader GRUB version (e.g. self-reporting `2.12-9+deb13u1` when build host runs Debian 13/trixie) | `iso_installer.py:_run_grub_mkrescue` + `iso.py:build_iso` invoke the **host's** `/usr/bin/grub-mkrescue`, which assembles the bootable image using GRUB modules from the host's `/usr/lib/grub/<platform>/`.  The compiled-in `PACKAGE_VERSION` of the host's grub binaries embeds into the ISO's bootloader core.img. | GRUB boot menu's compile-time version banner (rare), `strings <iso>/boot/grub/i386-pc/eltorito.img` | Tried extracting our repo's grub debs and passing `grub-mkrescue --directory=<our-temp>` 2026-05-22 (commits `fdcd670` + `e82a867`); empirically verified by md5sum'ing modules in the produced ISO that grub-mkrescue's `--directory=` only retargets modinfo.sh probe + core.img staging, NOT the bulk module-copy step.  And it's single-platform only — hybrid BIOS+EFI ISOs need per-platform dirs and grub-mkrescue 2.06 doesn't accept multiple --directory.  Proper fix needs `sudo mount --bind` to remap `/usr/lib/grub/` system-wide for the duration, OR running grub-mkrescue inside the BuildContainer (bookworm + our debs).  Tracked in COMP-14; accepted as residue until properly fixed. |
| `Description-Md5` references to Debian translation servers | apt translation files | invisible unless operator looks at apt cache | Same as above; not user-visible. |
| `Debian` in 3rd-party-package copyright headers under `/usr/share/doc/<pkg>/copyright` | upstream pkg-shipped legal text | only visible if operator opens the file | Legally accurate; rewriting would violate the licence-attribution requirement. |

### Things that look like residue but aren't

| Surface | Looks like | Actually is |
|---|---|---|
| `cdrom://Athena 0.1 _thor_ - amd64 INSTALLER` in install log | "where is Athena coming from" | This IS ours — apt's source label is set by `iso_installer.py` at build time |
| `debootstrap: Unpacking debianutils` lines during install | "Debian leak" | Package NAME is literally "debianutils" (Debian's standard utils pkg); we keep the name because dpkg dependency closure refers to it.  Not a rebrand surface. |

---

## 8. Adding a new branding surface — quick reference

When you encounter a new "Debian" string / image / behaviour on a
fresh install:

1. **Locate it.**  Grep the installed system for the literal string;
   use `dpkg -S <filepath>` to find which package ships it.
2. **Identify the layer** using the decision tree in § 2.
3. **Pick the lightest pattern** that reaches the surface (§ 1):
   - Debconf-overridable string? → Pattern A (extend
     `athena-installer-data`'s overrides file).
   - Drop-in theme/config file? → Pattern B (extend `athena-branding`
     or a new companion).
   - Conffile we need to overlay? → Pattern C (dpkg-divert) IF
     non-Essential; Pattern E (same-name fork) if Essential.
   - File we want to ship that no one owns? → Pattern D (extend
     `athena-installer-data` or a sibling).
   - Need to own the whole package surface? → Pattern E (same-name
     fork); document why narrower didn't work.
   - Baked into a binary with no override path? → § 7 (accept).
4. **Update § 5** in this doc with the new surface + pattern.
5. **Audit related surfaces.** A `Debian` string in package X often
   has siblings in packages Y/Z (e.g. the kernel image package also
   ships an `/etc/issue.net` of its own — addressed when we did
   base-files, but the pattern recurs).  Sweep before declaring
   done.

---

## 9. Reference

- [`fork/source/README.md`](../fork/source/README.md) — mechanism
  layer (how a fork package supersedes upstream)
- [`docs/plans/fork-source.md`](plans/fork-source.md) — FORK-01
  plan, fork-supersede design
- `memory/project_filter_debian_specific_installer_hooks.md` — the
  principle reference; this doc is its formal expansion
- `memory/project_three_layer_identity.md` — Athena vs Asgard vs
  thor; remember the toolchain is named Athena, distribution is
  named Asgard, codename is thor; don't conflate when branding
- `memory/project_file_path_collision_requires_pkg_override.md` —
  why "ship the file and hope" doesn't work
- `memory/feedback_dh_helper_files_use_binary_name.md` — when
  forking + renaming binaries, all `debian/<name>.*` files must
  match the new binary name (debhelper silently no-ops mismatches)
- `memory/feedback_dpkg_divert_keep_rename.md` — divert must stay
  `--rename`; `--no-rename` bricks downstream consumers (lesson
  from chroot-setup.sh)
