# Configuring publish mirrors (MIRROR-01)

How to register, redo, and verify a publish-target mirror under the
MIRROR-01 federation surface (`mirror add` / `publish` / `pull` /
`reconcile-neighbours` / `audit`).

Supersedes the pre-MIRROR-01 `repo publish ssh` / `repo publish local`
command group, which was removed in commit `8cc803b` along with its
operator doc.

## What a "mirror" is here

A **publish-target mirror** is a remote endpoint we push our built `.deb`s
to.  Each one carries:

- the apt **pool** (`<root>/dists/…`, `<root>/pool/…`) — apt clients fetch
  from here over HTTP(S) served by a webserver on the remote
- a sibling **sidecar** tree (`<root>-coord/…`) — signed coord-head + per-
  builder keyring + Ed25519-signed claim ledger; operator-only, never
  touched by apt clients

Both live on the same host; MIRROR-01 derives the sidecar URL from the
pool URL automatically by appending `-coord` to the last path component.

A mirror is **not** the same as a `[Mirror.<name>]` section in
`config/build.conf` — those refer to **upstream Debian apt mirrors**
that source-sync and the apt cache pull from.  Two different concepts;
the section name is historical.

## Prerequisites

| Required | Check |
|---|---|
| Builder identity (Ed25519 keypair + `coord/BUILDER_ID`) | `mirror init <id>` once if absent |
| Tier-1 signing key (GPG) | `print signing` shows the key |
| Local snapshot is set | `config/snapshot.state` has a `current` value |
| Remote host reachable over SSH | host fingerprint already in `~/.ssh/known_hosts` (publish runs non-interactive, can't prompt) |

If the remote is fresh, accept the host key once interactively:

```
ssh -i config/repo_asgard.key ubuntu@140.245.198.222 echo ok
```

## URL forms accepted

Publish-target URLs are restricted to two schemes — the apt-readable
URL the chroot writes into `sources.list.d` is **derived** from the
publish URL, not handed in.

| Form | Example | Use |
|---|---|---|
| `ssh://user@host/abs/path` | `ssh://deploy@mirror.example/srv/asgard` | remote publish (rsync over SSH) |
| `file:///abs/path` | `file:///srv/asgard` | offline / local-fs publish |

`http://` and `https://` URLs are **rejected** as publish targets —
they're the *read* surface, not the *push* surface.  Pass the read
protocol via `--proto`; the chroot writer will compute
`<proto>://<host>/<dist-id-lowercased>` and use that for the apt source
line.

## Adding a mirror (the new shape)

```
mirror add <ip|fqdn|local> <url> [--ssh-key PATH] [--proto http|https]
                                 [--name NAME] [--no-probe] [--yes]
```

- **`<ip|fqdn|local>`** — REQUIRED keyword classifying the host portion
  of the URL.  `ip` requires an IPv4 or IPv6 literal; `fqdn` requires a
  DNS hostname; `local` requires a `file://` URL.  A mismatch is rejected
  loudly.
- **`<url>`** — the publish URL (ssh:// or file:///).
- **`--ssh-key PATH`** — private key for rsync/ssh (required for ssh:// URLs).
- **`--proto http|https`** — REQUIRED for ssh:// URLs.  The chroot writes
  `<proto>://<host>/<dist-id>` into `sources.list.d/athena-<name>.list`.
  Single-dash `-proto` is also accepted.
- **`--name NAME`** — override the auto-derived name.  Default name is
  the host with `.` and `:` mapped to `-` (e.g. `140-245-198-222`,
  `mirror-example-com`).  For `local` mirrors the name comes from the
  path tail.
- **`--no-probe`** — skip the DNS / TCP / SSH / HTTP probes (dev /
  offline scenarios).  Sidecar discovery still runs if reachable.
- **`--yes`** — accept the federation-join summary without prompting.
  Refusal gates (signing-key verify, key-mismatch on peer's coord-head)
  are **never** bypassed by `--yes`.

What the new `mirror add` actually does (10-step pipeline, each step
prints a progress line):

1. **Signing-key gate.**  `signing.verify_key()` must succeed.
   Refused otherwise — there's no published `coord-head` we could sign
   without it.
2. **Sanity.**  Host-type keyword × URL shape.
3. **Proto requirement.**  `--proto` mandatory for ssh, forbidden for
   file.
4. **Derive.**  Name (from host), `public_url` (`<proto>://<host>/<dist-id>`),
   host fields.
5. **Dedup early.**  Refuse if name or URL is already registered locally.
6. **Probes** (unless `--no-probe`):
   * DNS resolve + TCP 22 reachability
   * SSH `BatchMode=yes echo ok` (key + host + auth verified)
   * HTTP HEAD on `<public_url>/dists/<codename>/InRelease`:
     200 = mirror has a published Release;
     404 = empty mirror (first-publish bootstrap is fine);
     anything else = warn.
7. **Sidecar probe.**  Pull peer's `coord-head.json[.sig]` from
   `<pool>-coord/`.  If it's present, **GPG-verify with our local
   tier-1 keyring** — if verify fails, **ABORT**: the peer's federation
   isn't in our trust domain (operator must import the federation's
   public key out-of-band first).  If verify succeeds, render summary
   (head-time / inrelease-sha / per-builder seqs / neighbours list).
8. **Federation discovery.**  For each peer in the verified head's
   `neighbours` list, recursively probe their sidecar.  Classify each
   as `reachable+verified` / `reachable+bootstrap-pending` / `unreachable`.
9. **Dedup + drop policy.**  Intersect discovered peers with the local
   mirror set; the overlap is dedup'd.  Unreachable peers are
   **dropped** (take-control-and-drop default) — the new federation
   joins without them, and `mirror reconcile-neighbours` propagates the
   shrunk membership to the remaining reachable peers.
10. **Sources.list preview + confirm.**  Render every
    `/etc/apt/sources.list.d/athena-<name>.list` line the next
    `chroot build` would emit (existing + primary + every net-new peer).
    Prompt YESNO to accept (skip with `--yes`).  On accept: write state
    files for the primary + every net-new reachable peer; trigger
    `mirror reconcile-neighbours` to propagate.

## First-time setup (fresh remote)

Create the apt-pool dir + its `-coord` sidekick on the remote.  The
probe step in `mirror add` `mkdir -p`s the latter for you when the
operator's ssh user has permission, but for clarity:

```
ssh -i config/repo_asgard.key ubuntu@140.245.198.222 \
    mkdir -p /home/ubuntu/asgard /home/ubuntu/asgard-coord
```

Register and publish:

```
mirror add ip ssh://ubuntu@140.245.198.222/home/ubuntu/asgard \
              --ssh-key config/repo_asgard.key --proto http
mirror publish 140-245-198-222
```

(Or `mirror publish` with no name to push to every configured mirror.)

Because the remote has no `coord-head.json` yet, this triggers the
**first-publish bootstrap path**:

1. Uploads `<builder-id>.pub` to
   `<remote>/asgard-coord/keyring/builders/<builder-id>.pub`.
2. Pushes per-file `.deb`s to `<remote>/asgard/pool/…` under remote
   flock; each one gets an Ed25519-signed claim line appended to
   `<remote>/asgard-coord/claims/<builder-id>.jsonl`.
3. Re-indexes `dists/<suite>/` ON THE REMOTE (`dpkg-scanpackages` etc.),
   signs `InRelease` locally with the tier-1 key, pushes the signed
   `InRelease` back.
4. Writes `<remote>/asgard-coord/coord-head.json` with
   `neighbours = [<your-pool-url>]` plus the new `inrelease_sha256`,
   then signs it with the tier-1 key.

Verify:

```
mirror list             # expect: 140-245-198-222  [ssh   ]  in-sync   ssh://...
mirror status 140-245-198-222
mirror audit 140-245-198-222
```

## Joining an existing federation

If the peer already has a published `coord-head` listing other
neighbours, `mirror add` will fetch + verify it, recursively probe each
neighbour, and propose **adding all reachable peers** in one bundle.

```
mirror add fqdn ssh://ubuntu@mirror-a.example/srv/asgard \
                --ssh-key config/asgard.key --proto https
```

Output sketch:

```
signing key ok: mock-key-verified
derived: name=mirror-a-example  url=ssh://...  host=mirror-a.example  public_url=https://mirror-a.example/asgard
  ssh reachability (mirror-a.example:22): reachable
  ssh auth: ssh auth + echo round-trip ok
  apt URL (https://mirror-a.example): InRelease present at https://...
sidecar: coord-head verified

existing coord-head on this peer:
  head_time:       2026-06-01T00:00:00Z
  inrelease_sha:   a1b2c3d4e5f6a7b8…
  builders/seqs:   athena-a=14, athena-b=3
  neighbours (3):
    - ssh://ubuntu@mirror-a.example/srv/asgard
    - ssh://ubuntu@mirror-b.example/srv/asgard
    - ssh://ubuntu@mirror-c.example/srv/asgard

proposed sources.list (after add):
+ athena-mirror-a-example.list: deb [signed-by=…] https://mirror-a.example/asgard thor main
+ athena-mirror-b-example.list: deb [signed-by=…] https://mirror-b.example/asgard thor main
+ athena-mirror-c-example.list: deb [signed-by=…] https://mirror-c.example/asgard thor main

Register 'mirror-a-example' + 2 discovered peer(s)? [y/n]:
```

If any peer in the list is unreachable when probed, it's shown in a
**DROPPING** block and excluded from the federation join (the default
"take-control-and-drop" policy):

```
DROPPING 1 unreachable peer(s) from the federation (default policy):
  - ssh://ubuntu@mirror-c.example/srv/asgard: TCP connect failed
  `mirror reconcile-neighbours` will propagate the shrunk membership to all remaining reachable peers.
```

## Signing-key mismatch (the federation trust gate)

The peer's `coord-head.json` is GPG-signed with the federation's tier-1
key.  If our local keyring can't verify it, `mirror add` aborts with:

```
sidecar: coord-head present but GPG verify FAILED against local tier-1
keyring — either the federation's signing key is missing from your
keyring (import it out-of-band) or this peer was signed by a key you
don't trust
```

Fix: get the federation's tier-1 public key from an operator who
already has it, `gpg --import` it into your signing keyring, re-run
`mirror add`.  There is no flag to override this — joining a
federation whose key you don't have means signing `coord-head` updates
with a key the rest of the federation can't verify, which would
poison the next reconcile.

## Heterogeneous federations (mixed http / https peers)

`coord-head.json`'s schema is v3 as of MIRROR-01 Phase 7 (commit
`0776fa7`).  `neighbours` is a list of per-peer records — each carries
the apt-readable URL the chroot writes into `sources.list.d`:

```json
"neighbours": [
  {"url": "ssh://ubuntu@a.example/srv/asgard",
   "public_url":   "http://a.example/asgard",
   "public_proto": "http"},
  {"url": "ssh://ubuntu@b.example/srv/asgard",
   "public_url":   "https://b.example/asgard",
   "public_proto": "https"}
]
```

Per-peer records mean a federation can mix http and https peers
freely — each builder records its own apt-readable URL into the
signed federation head at `mirror publish` time, and a joining
builder reads those records verbatim instead of having to inherit
the operator's `--proto` flag.

What happens when you `mirror add` joins an existing v3 federation:

1. The orchestrator's federation-discovery step reads the peer's
   `coord-head.neighbours` records.
2. For each discovered peer, the locally-written
   `config/mirror.<name>.state` carries the **upstream's** `public_url`
   and `public_proto` — not whatever `--proto` you passed.
3. The operator's `--proto` flag is still honoured for: (a) the
   primary mirror you're explicitly adding, and (b) any v2-shaped
   peers whose records have empty `public_url` (the back-compat path
   for federations still running pre-Phase-7 publishers).

When you publish back, your builder's per-peer view of each peer's
apt URL is what lands in the signed coord-head for that
publish-target.  Two builders that disagree on `peer-X.public_proto`
will see each other's view round-trip until both are aligned;
operator can resolve by running a manual `mirror remove peer-X` +
`mirror add` with the desired protocol on whichever builder is wrong.

SSH keys stay homogeneous across the federation — Phase 7 keeps the
operator's `--ssh-key` flag as the source of truth for every peer.
Heterogeneous ssh keys would need a separate schema bump and an
out-of-band key-distribution story.

Read-compat: v2 coord-heads (`neighbours: list[str]`) are still
readable; their per-peer records auto-promote to empty
`public_url` / `public_proto` and `mirror add` falls back to the
operator's `--proto`.  v1 coord-heads (no `neighbours` field at all)
read as empty.

## MIRROR-02: build modes, ownership, installability

MIRROR-02 (shipped 2026-06-04 across 14 chunks; commits `4fd3aa4`
through `aff3482`) layers three new concepts on top of MIRROR-01's
federation:

### Build modes — `[Build] Mode = distribution | individual`

Two operator personas:

- **Distribution mode** (default; unchanged from MIRROR-01) — owns the
  full corpus.  Cache parse walks `pkg.list` + `pool.list` etc.  Builds
  chroot + ISO.  `repo audit` covers the whole repo.
- **Individual mode** — owns a subset.  Operator lists their packages
  in `config/indl.list` (flat list, `#` comments).  Cache parse skips
  the runtime closure walk entirely — `selected_pkgs` is exactly the
  names in `indl.list`, no transitive deps pulled.  Chroot / ISO are
  refused (`[Build] Mode = individual` skips chroot/ISO assembly).
  `source audit` scopes to the indl subset.

```ini
# config/build.conf
[Build]
Mode = individual
```

```text
# config/indl.list
firefox-esr           # OOMs on host A
libreoffice
thunderbird
```

Mode is shown persistently:

- `print state` first line: `MODE: distribution` or
  `MODE: individual  [N pkg(s) in indl.list]`
- `autorun <variant>: starting (MODE = …)` header
- `mirror publish <name> [MODE=individual]: → <url>` per-mirror header

`autorun individual` is the indl-mode autorun variant.  Bare `autorun`
in indl mode routes there automatically.

### Per-package ownership

Each non-retracted `published` claim on the mirror has an "owner":

- `republished_from is None` → claim's `builder` field is the owner.
- `republished_from is not None` → claim is **tunneled**; no owner.

`mirror publish` applies the ownership decision matrix per filename:

| Existing owner | Our version > theirs? | Decision |
|---|---|---|
| no claim          | — | publish; we become owner |
| owned by us       | — | publish (re-claim no-op) |
| tunneled          | — | publish; we take ownership |
| owned by other    | yes (strictly higher) | publish; ownership transfers |
| owned by other    | no (same/lower)       | BLOCK (`ownership_blocked`) |

Blocked filenames surface in the publish detail; the rest of the
publish proceeds (partial-success is the design).  Version compare
via `apt_pkg.version_compare`.

### Installability gate (`mirror_closure_break`)

Every `mirror publish` runs `repo_audit.audit_dep_closure` over the
projected post-publish union state (mirror's existing claims + our
pending), bounded to our pending packages as the consumer set.  Any
unresolved hard `Depends` → REFUSE with detailed findings.

This is the chunk-11 invariant: **at every moment, the mirror's pool
must form a closed dep graph**.  Publishing a package whose deps
aren't satisfied by the projected post-publish union is REFUSED
loudly with the specific consumer + unsatisfied relation in the
error message.

Multi-version-aware: a mirror carries the UNION of every non-retracted
claim across `[mirror.base, mirror.current]`.  An older claim whose
`Depends:` is satisfied only by a newer-snapshot claim is fine — the
projection includes both versions, and the closure walk resolves
against the full set.

### First-publish dist-mode gate

An indl-mode builder publishing to a mirror with no `coord-head` yet
(fresh bootstrap) is REFUSED.  Bootstrapping an indl-mode subset into
a virgin mirror would seed a partial, non-installable starting state.
The error message points the operator at the dist-mode bootstrap path.

### `mirror.base` advances on publish

After every successful `mirror publish`, `mirror.base` is recomputed
to the **oldest snapshot timestamp across all non-retracted claims**
on the mirror.  Combined with Phase 8's `snapshot.current >= mirror.base`
publish gate, this gives operators a meaningful "oldest thing on this
mirror" floor and prevents back-publishing pre-floor builds.

### `mirror pull` writes local `build.json` records

Pulled `.deb`s get a local build record so subsequent `source audit`
and `repo audit` runs see them as already-built:

- Tunneled-on-mirror → local `phase=tunneled`, `republished_from`
  copied verbatim per filename
- Owned by another builder → local `phase=done`, new `pulled_from =
  {mirror_name, owner_builder}` annotation distinguishes "we built it"
  from "we pulled it"

Means a builder can pull from a mirror and immediately drive
`chroot build` against the pulled tree, no `source build` step.

## Migrating from the legacy `[Repo]` keys

If `config/build.conf` still carries the pre-MIRROR-01 keys, they're
dead text — readers were removed in commit `8cc803b`, but the file is
hand-maintained so nothing has stripped them.  Safe to delete:

```
AptSourceURL     = http://…/asgard/
PublishSshTarget = user@host
PublishSshKey    = config/repo_asgard.key
ExternalEnabled  = true
```

Keep `SigningKeyUid` (used by tier-1 GPG signing).  Keep every
`[Mirror.<name>]` section in the upstream area — those are **upstream
Debian mirrors**, unrelated to MIRROR-01 publish targets.

A typical migration:

| Legacy key | New equivalent |
|---|---|
| `PublishSshTarget = ubuntu@host` (path implicit at `~/asgard`) | `mirror add ip ssh://ubuntu@<host>/home/ubuntu/asgard --ssh-key … --proto http` |
| `PublishSshKey = config/repo_asgard.key` | `--ssh-key config/repo_asgard.key` on `mirror add` |
| `AptSourceURL = http://host/asgard/` | Derived as `<proto>://<host>/<dist-id>`; written automatically |
| `ExternalEnabled = true/false` | Removed — every configured mirror is enabled; un-register with `mirror remove` to disable |

## Redoing a mirror (wipe + start over)

If a previous attempt left a half-written state file, or you want to
re-bootstrap from scratch:

```
mirror remove <name>                       # drops local config/mirror.<name>.state
rm -rf cache/mirror/<name>                 # clears staging cache
# on the remote:
ssh ... 'rm -rf <pool>-coord/coord-head.json* \
                <pool>-coord/keyring \
                <pool>-coord/claims'
mirror add <ip|fqdn> <url> --ssh-key … --proto …   # re-register clean
mirror publish <name>                       # re-bootstraps
```

`mirror remove` is intentionally **local-only**: it removes the local
state file but does NOT touch the remote and does NOT update any other
peer's signed `neighbours` list.  If you're running a multi-peer
federation and want every peer's `coord-head.neighbours` to drop the
removed URL, run:

```
mirror reconcile-neighbours
```

after the remove.  Until you do, `mirror list` will show `drift` next
to peers whose last-known `neighbours_known` still lists the removed URL.

## Federation consistency

For a single mirror, this section is trivial — there's nothing to
reconcile.  For multiple peers:

- Every peer's `coord-head.neighbours` must equal your local mirror URL
  set.  A diff makes `mirror publish` BLOCK with a federation-gate
  CRITICAL finding.
- `mirror reconcile-neighbours` fan-outs: for each peer, pulls the
  peer's coord-head, rewrites `neighbours` to match local config,
  re-signs locally with the tier-1 GPG key, and pushes back under
  flock.  An unreachable peer is a hard failure (operator must retry
  once connectivity is restored before any publish can succeed).
- `mirror list` surfaces the federation-consistency tag inline:
  `unpublished` / `in-sync` / `drift`.  Drift rows print the missing
  and extra URLs and remind the operator to reconcile.

## Phase 8 publish gate: `snapshot.current >= mirror.base`

Every `mirror publish` invocation checks that the local build's
snapshot pin is at least as fresh as each target mirror's archive
floor.  If `snapshot.current < mirror.base`, the publish is
**REFUSED** with:

```
mirror publish <name>: REFUSED — build snapshot (<current>) is older
than this mirror's archive floor (mirror.base = <base>).  Advance the
build snapshot (`snapshot select <ts>`) or wipe + re-add the mirror
with a fresh base.
```

Publishing pre-floor packages would either be silently dropped on
the next prune at the mirror, or corrupt the `+asg uN` derivation
(which assumes the mirror's published Packages index is the
authoritative bump-N counter).  Empty `mirror.base` (= mirror has
never been published to) is allowed through — first publish
bootstraps it.

## Where the parts live now (`source` / `repo` / `mirror`)

| Family | Purpose | Notable subcommands |
|---|---|---|
| `source` | produce a built `.deb` (any path) | `sync`, `build`, `tunnel`, `audit`, `repair`, `fork` |
| `repo` | LOCAL `.deb` pool maintenance | `audit`, `repair` |
| `mirror` | REMOTE federation endpoint state | `add`, `publish`, `pull`, `audit`, `summary`, `query`, `reconcile-neighbours`, `init`, etc. |

`source tunnel` (was `repo tunnel` pre-Phase 8): pulls a prebuilt
`.deb` for packages we choose NOT to build from source, records a
tunneled `build.json` claim, and the .deb round-trips through
`mirror publish` like any built artefact.  See operator notes in
the README.

`repo index` was retired from the operator surface in Phase 8 —
`chroot build` and `mirror publish` auto-index when the local
`InRelease` is missing.  Use `repo repair` if you suspect a stale
index.

## `mirror summary` per-mirror `we_own` count

The summary now lists `we_own: N pkg(s)` per mirror — counts
non-retracted claims this builder owns on each mirror, sourced from
the most-recently-fetched `cache/mirror/<name>/fetched/claims/<our-id>.jsonl`.

If you've never run `mirror pull` or `mirror publish` against the
mirror, the line says `(no claims jsonl fetched yet — run mirror pull)`
instead of a count.

## `mirror audit` integrity sweep

Phase 8 extends `mirror audit` with a per-mirror cross-check between
the signed claim ledger and the actual files in the mirror's pool dir:

- **CRITICAL `missing_on_disk`** — a sidecar claim references a file
  that's not actually in the pool.  apt clients fetching it would 404;
  `mirror pull` would skip the download.
- **WARNING `orphan_on_disk`** — a `.deb`/`.udeb` is sitting in the
  pool with no claim backing it (operator out-of-band rsync? leftover
  from a wiped builder?).  Not load-bearing today but operator should
  know.

Cost: one remote `find` per ssh mirror; local `os.walk` for file://
mirrors.  Network/permission failure on the listing surfaces a
single `INFO pool_listing_unavailable` line and the cross-check is
skipped (other audit checks still run).

## Subcommand quick reference

```
mirror init <id>                generate Ed25519 builder identity + persist BUILDER_ID
mirror add <ip|fqdn|local> <url> [--ssh-key PATH] [--proto http|https] [--name N] [--no-probe] [--yes]
                                register a mirror — probes, sidecar, federation discovery, preview, confirm
mirror remove <name|url>        unregister LOCALLY (no remote/peer changes)
mirror list                     name, type, federation-consistency tag, url
mirror summary [<name>]         per-mirror state + we_own count + neighbours_known list
mirror status [<name>]          builder identity + halt sentinel + per-mirror PUBLISHED/NEVER PUBLISHED
mirror reconcile-neighbours     fan-out: align every peer's coord-head.neighbours with local config
mirror publish [<name>]         per-file .deb push + sign claims + re-sign coord-head (federation-gated + snapshot-base-gated)
mirror pull [<name>]            fetch peer sidecar, download missing claim .debs (skip-own; SHA verified)
mirror audit [<name>]           federation consistency, claim sigs, hash conflicts, cross-mirror pool drift, on-disk pool ↔ claims integrity
mirror query <pkg> [<name>]     show claims matching <pkg> from last fetched view of each mirror
mirror builders                 list registered builders (local + fetched keyring)
mirror conflict resolve <pkg>   retract our claim for <pkg>; clear PUBLISH_HALT
```
