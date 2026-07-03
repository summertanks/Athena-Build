# Mirror host layout

What actually lives on a publish-mirror host — the directories, files and
system configuration that mirror preparation creates and that publishing
fills in.  Read this before changing anything on a mirror by hand, so you
know what each piece is for, who writes it, and what is safe to touch.

Throughout, the examples use a distribution named **Asgard** with release
codename **thor**, prepared for user `mirror` — so the *dist-id* is
`asgard` and the root is `/home/mirror/asgard`.  Your own dist-id is your
distribution's name lowercased.

## The big picture

A mirror is deliberately dumb: a plain directory tree served read-only
over HTTP, written only over SSH.  There is no daemon, no database and
no code from the toolchain running on it.  Three things live there:

1. **The mirror root** — the apt repository, the images, and a small
   marker file.  Served over HTTP at `/<dist-id>/`.
2. **The coordination sidecar** — a directory *next to* the root that
   holds the federation's bookkeeping (who published what, signed by
   whom).  It is reached over SSH only and is **not** web-served.
3. **Host configuration** — one web-server config file and a handful of
   installed packages.

## The directory tree

```
/home/mirror/asgard/                    the mirror root  (HTTP: /asgard/)
├── mirror-info.json                    "this is a prepared mirror" marker
├── index.html                          human landing page (per release)
├── releases.json                       machine-readable release manifest
├── dists/
│   └── thor/                           the apt repository (signed)
│       ├── InRelease                   clearsigned index-of-indexes
│       ├── Release, Release.gpg        the same, as file + detached sig
│       └── main/                       (one dir per component)
│           ├── binary-amd64/           Packages indexes AND the .deb
│           │                           files themselves, side by side
│           ├── debian-installer/
│           │   └── binary-amd64/       installer .udeb files + indexes
│           └── source/                 Sources indexes
└── iso/                                published images (live ISO,
                                        installer ISO, disk image)

/home/mirror/asgard-coord/              coordination sidecar  (SSH only)
├── coord-head.json                     the federation head: snapshot pin,
├── coord-head.json.sig                 ledger + config fingerprints and
│                                       the builder registry — GPG-signed
├── claims/
│   └── <builder-id>.jsonl              one append-only ledger per
│                                       builder: every file it published,
│                                       each line individually signed
├── keyring/
│   └── builders/
│       └── <builder-id>.pub            each builder's public key
└── config/
    ├── canonical.json                  the package selection the owner
    │                                   published (peers adopt it)
    └── closure_ledger.json             newest published file for every
                                        package — what a pulling peer
                                        actually downloads
```

Two things about the apt tree that differ from a stock Debian archive:

- **There is no separate `pool/` directory.**  Package files sit in the
  same directory as the index that lists them, under
  `dists/<codename>/<component>/`.  If you are looking for a `.deb`,
  it is next to its `Packages` file.
- **The repository is self-contained.**  Installed systems point apt at
  this mirror and nothing else — there is no upstream fallback.  The
  sources line a client uses is:

  ```
  deb http://<host>/asgard thor main non-free-firmware
  ```

## Host configuration

Preparation installs, if missing: **nginx** (serves the root), **rsync**
(all publish/pull transfers), **curl** (health probes) and **psmisc**
(recovery of a stuck publish lock).  Everything else it relies on is part
of any Debian/Ubuntu base system.

One web-server config file is written, only when the path is not already
being served:

```
/etc/nginx/conf.d/asgard-<dist-id>.conf
```

It maps `/<dist-id>/` to the mirror root, makes `index.html` the default
page, and leaves directory listings on so the repository is browsable.
The stock nginx default site is disabled so this server block answers on
port 80.  If the host already serves the path (your own config, another
web server), preparation leaves the web configuration entirely alone.

## Files that come and go during a publish

While a builder is publishing, a lock exists on the mirror:

```
/var/lock/repo-coord.lock            the lock itself
/var/lock/repo-coord.lock.holder     who holds it — touched every few
                                     seconds as a heartbeat while alive
/var/lock/repo-coord.lock.pid        the holder's process id
```

A healthy publish — even a very slow multi-gigabyte one — keeps the
heartbeat fresh.  Only when the holder file has gone stale for over a
minute is the lock considered dead (a crashed or power-failed publisher),
and it is then broken from the build side with the mirror-unlock command,
never by deleting these files by hand.

## What is safe to change by hand

| Area | Safe? | Why |
|---|---|---|
| `iso/` | Yes | Plain files; re-pushed and re-verified on the next release publish. |
| `index.html`, `releases.json` | Yes | Regenerated wholesale on the next release publish. |
| `mirror-info.json` | Mostly | Deleting it just makes the host look unprepared; re-running preparation rewrites it.  Don't edit its contents — adopt/re-prepare instead. |
| nginx config | Yes | It is ordinary web-server config; preparation never overwrites an existing serving setup.  Keep `/<dist-id>/` mapped to the root with `index.html` as the index. |
| `dists/` | **No** | Every file is pinned by hash from the signed `InRelease`.  Add, remove or edit anything and apt clients reject the repository.  Fix content by republishing from a builder. |
| `<root>-coord/` | **Never** | Claims are append-only and individually signed; the head is GPG-signed over the ledger and config fingerprints.  Hand edits make peers reject the mirror or, worse, quietly strand files.  All changes flow through publish/pull. |
| The lock files | **Never** | Breaking a publish lock by hand can corrupt an in-flight publish.  Use the mirror-unlock command, which checks the heartbeat first. |

## Moving or renaming things

The root's location is recorded in the marker file and in every builder's
mirror registration, and the served URL path is derived from the
distribution's name — none of these are independent knobs.  To move a
mirror root, re-run preparation against the new path (it adopts an
existing layout and fills gaps), then re-register the mirror from the
builders.  To rename the distribution, treat it as a new mirror.
