# COMP-01 sub-phase — fork `choose-mirror` to an Athena-only mirror list

## Goal

The installer's network-mirror step should appear as part of the standard
d-i flow, **listing only the Athena mirror** (never Debian). When the
operator selects it, `apt-setup`'s `50mirror` writes the Athena repo as the
installed system's network apt source.

## Why a fork (not a workaround)

- `apt-setup`'s `50mirror` generator hard-requires `choose-mirror`
  (`search-path choose-mirror || exit`). We stubbed `choose-mirror` out in
  the rebranding because it ships Debian's `Mirrors.masterlist`. So with no
  `choose-mirror`, the mirror step can't appear at all — which is why
  `apt-setup/use_mirror=false` (the earlier workaround) made it vanish.
- Shipping *stock* `choose-mirror` would re-introduce Debian's mirror list.
- So: fork `choose-mirror`, replace its masterlist with an Athena-only
  entry. The standard step appears showing our mirror; no Debian data ships.

Rejected alternative (operator chose against it): ship stock `choose-mirror`
+ force `mirror/country=manual` preseeded to our host. Simpler, but the
Debian masterlist data rides along on the ramdisk.

## Source baseline

- Bookworm `choose-mirror **2.123+deb12u1**` (matches our snapshot line),
  pulled from snapshot.debian.org and seeded into
  `fork/source/choose-mirror/` (full source tree, like the `base-files`
  same-name fork).
- Builds two udebs: `choose-mirror` (udeb, all — templates/data;
  `XB-Installer-Menu-Item: 2300`) and `choose-mirror-bin` (udeb, any — the C
  selector). `apt-setup`'s `search-path choose-mirror` finds the binary.
- Build flow (Makefile): `./mirrorlist httplist Mirrors.masterlist` →
  generates the http/https/ftp template Choices; `debian/rules` runs
  `make clean check-masterlist`.
- **No collision** with upstream: we don't pull stock `choose-mirror` (it's
  not in `installer.list` and nothing `Depends:` on it — `50mirror`'s
  `search-path` is a runtime check). Only our fork ships.

## The masterlist (the load-bearing change)

Replace `fork/source/choose-mirror/Mirrors.masterlist` with a single Athena
entry. First cut:

```
Site: 140.245.198.222
Type: Push-Primary
Archive-architecture: amd64
Archive-http: /asgard/
```

`choose-mirror` validates a chosen mirror by fetching
`http://<Site><Archive-http>/dists/<suite>/Release` — i.e.
`http://140.245.198.222/asgard/dists/thor/Release`, which exists. It then
sets `mirror/http/hostname=140.245.198.222`, `mirror/http/directory=/asgard`,
`mirror/suite=thor`.

**The masterlist must be pure RFC822 stanzas — no `#` comments.**
`./mirrorlist`'s parser matches every `key: value` line (`([^:]*):\s+(.*)`),
so a comment containing `": "` before the first `Site:` writes to an empty
record and crashes the build (`Modification of non-creatable array value …
subscript -1`). Rationale lives in `README.Athena`, not the masterlist.
(Found on the first build, 2026-05-25.) A regression test pins this.

**Open detail (needs build-time iteration):** the country grouping. d-i's
mirror flow is country → mirror-in-country. A `Country:` line groups the
entry under a country; an entry *without* `Country:` (like upstream's
`deb.debian.org`) surfaces as a top-level choice. The exact UX with a
one-entry list (and whether `check-masterlist` / `mirrorlist` accept a
country-less or fake-country entry) must be confirmed on a real build.

**Decoupling the IP (follow-up):** the masterlist currently hardcodes
`140.245.198.222` + `/asgard`, duplicating `[Repo] AptSourceURL`. Preferred:
template it (`@APT_SOURCE_HOST@` / `@APT_SOURCE_PATH@`) and substitute from
`AptSourceURL` at fork-build time (the same build-time substitution
`athena-installer-data` uses for codename), so there's one source of truth.

## Wiring (alongside the fork)

1. `config/installer.list`: add `choose-mirror`.
2. `fork/source/athena-installer-data`: drop the `mirror/protocol` stub
   template (real `choose-mirror` now provides `mirror/*`).
3. `installer/preseed/preseed.cfg`: `mirror/protocol=http`, suite/codename
   `thor`, and **`apt-setup/use_mirror=true`** (see Build-1 findings — without
   it `50mirror` exits and writes no mirror).
4. `fork/source/choose-mirror/debian/control`: **drop `XB-Installer-Menu-Item`**
   so choose-mirror is not an early standalone step; `apt-setup`'s `50mirror`
   invokes it at the package-manager step (after base) — see Build-1 findings.

## Build-1 findings (2026-05-25)

First build + install (verified on the VM's `/var/log/installer/`):

- **Ordering bug:** choose-mirror ran as an early main-menu step (item
  2300) and asked country/mirror *before* base install — not the stock CD
  flow. Fix: drop `XB-Installer-Menu-Item` (above); `50mirror` then drives it
  after base.
- **No mirror written:** choose-mirror set every `mirror/*` value correctly
  (`hostname=140.245.198.222`, `directory=/asgard/`, `protocol=http`,
  `suite/codename=thor`), but **`apt-setup/use_mirror=false`**, so
  `50mirror` hit `if [ "$RET" = false ]; then exit 1` and discarded its
  output → cdrom-only. Fix: preseed `apt-setup/use_mirror=true`.
- choose-mirror validated our mirror fine (`wget …/dists/thor/Release` OK),
  and the cdrom `InRelease` verified against our key — signing chain works.

## Issue B — installed system blocks on the CD (separate, still needed)

`apt-setup` leaves a `deb cdrom:` / `deb-src cdrom:` entry in
`/target/etc/apt/sources.list`; post-eject `apt update` blocks on "insert the
disc" (`apt-setup/disable-cdrom-entries` is a no-op for us). Even with the
mirror configured, the cdrom entry lingers. Fix: a `finish-install.d` hook
(runs after pkgsel) comments out the `deb(-src)? cdrom:` lines.
(Verified on the installed VM at the time of writing: `sources.list` held the
two cdrom entries; `athena.list` + keyring were already correct; the VM could
reach the repo.)

## Build / test (cannot be validated from the dev box)

1. `cache build` → `cache parse` → `source build installer` (builds the
   forked `choose-mirror*` udebs).
2. `chroot build installer` → `iso build installer`.
3. Boot the ISO in QEMU; confirm the mirror step appears listing only the
   Athena mirror; complete an install; on the target verify
   `/etc/apt/sources.list` has the Athena `deb` line (and no live `cdrom:`
   entry) and `apt update` works without a disc.
4. Iterate the masterlist country/UX details from step 3 feedback.

## Risks

- **Masterlist country grouping.** `./mirrorlist httplist` cross-references
  each entry's `Country:` against `debian/iso_3166.tab` (built from
  `iso-codes` via `isoquery`) to generate the country-grouped picker. The
  Athena entry now carries a real code (`Country: US United States`) so the
  lookup matches and the picker isn't empty; the displayed country is
  cosmetic (one mirror) and can be revisited. Still confirm the one-entry UX
  renders sanely on a real build.
- **Untestable in dev** — d-i mirror internals only show on a real install;
  expect 1-2 iterations on the masterlist shape + the `mirror/suite` keys.
- **IP hardcoding** until the AptSourceURL-substitution follow-up lands.

### NOT risks (verified)

- **Build-deps** `iso-codes` / `isoquery` / `libdebian-installer4-dev` are
  installed into the build container **from the snapshot** (standard
  bookworm-main packages), not satisfied from our `repo/` pool — so they
  don't gate the build the way an earlier draft implied.
- **Runtime deps already in the closure:** `libdebian-installer4-udeb`
  (installer.list) for `choose-mirror-bin`'s `-ldebian-installer`, and
  `configured-network` (provided by `netcfg`, installer.list) for
  `choose-mirror`.
- **`check-masterlist`** is a no-op (only warns inside a git checkout; the
  fork tree has none) and the build is `ONLINE=n` (no network sync).

## Status

Scaffolded: `fork/source/choose-mirror/` seeded from bookworm
`2.123+deb12u1` with the Athena-only masterlist + a fork changelog entry.
Wiring (installer.list / stub removal / preseed), the cdrom-disable hook,
and the build-time iteration are the remaining steps.
