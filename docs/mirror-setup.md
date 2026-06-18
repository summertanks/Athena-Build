# Configuring publish mirrors (MIRROR-01)

How to register, redo, and verify a publish-target mirror under the
MIRROR-01 federation surface (`mirror add` / `publish` / `pull` /
`reclaim` / `reconcile-neighbours` / `audit`).

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

## First-run onboarding (the wizard)

On the **first interactive launch** of a not-yet-set-up checkout, a one-time
wizard runs before the prompt opens (skipped for `--headless` / `--cmd` /
`--api` / `--yes`).  It establishes the box's identity so you don't have to
wire it up by hand:

1. **Role** — *first/origin* (bootstraps the federation; always distribution
   mode) or *federation peer* (joins an existing mirror).
2. **First system**: mode is forced to `distribution`; optionally enables a
   publish mirror (generates the tier-1 key, `mirror init`, `mirror add`).
3. **Federation peer**: registration is **mandatory** and its prerequisites are
   validated up front — the tier-1 signing key must already be imported
   (copied from the first system; it's the *private* key, since publish
   re-signs the coord-head), then `mirror init` (if needed), `mirror add`, and
   `mirror builders register`.  Only after that can the peer pick build vs
   distribution mode.
4. **Snapshot pin** for the build.

The result is recorded in **`config/local.conf`** — an **untracked,
machine-local** sidecar holding `[Local] Mode/Role/SetupComplete` and a
`[Registration]` marker per mirror.  This is why a fresh `git pull` never
inherits another machine's mode or mirror identity, and why a registered peer
never has to re-register.  Mode is changed later with `set mode` (persisted
here); more mirrors are added later with `mirror add`.

> `set mode build` is refused on a first/origin system, and on any peer that
> hasn't registered to a mirror yet (no `[Registration]` marker) — build mode
> publishes a subset only an already-bootstrapped mirror accepts.

## Prerequisites

> The onboarding wizard above performs all of these for you on a fresh box;
> this table is the manual / re-check reference.

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

### Build modes — `[Build] Mode = distribution | build`

Two operator personas:

- **Distribution mode** (default; unchanged from MIRROR-01) — owns the
  full corpus.  Cache parse walks `pkg.list` + `pool.list` etc.  Builds
  chroot + ISO.  `repo audit` covers the whole repo.
- **Build mode** — owns a subset.  Operator lists their packages
  in `config/build_pkg.list` (flat list, `#` comments).  Cache parse skips
  the runtime closure walk entirely — `selected_pkgs` is exactly the
  names in `build_pkg.list`, no transitive deps pulled.  Chroot / ISO are
  refused (`[Build] Mode = build` skips chroot/ISO assembly).
  `source audit` scopes to the indl subset.

```ini
# config/build.conf
[Build]
Mode = build
```

```text
# config/build_pkg.list
firefox-esr           # OOMs on host A
libreoffice
thunderbird
```

Mode is shown persistently:

- `print state` first line: `MODE: distribution` or
  `MODE: build  [N pkg(s) in build_pkg.list]`
- `autorun <variant>: starting (MODE = …)` header
- `mirror publish <name> [MODE=build]: → <url>` per-mirror header

`autorun build` is the build-mode autorun variant.  Bare `autorun`
in build mode routes there automatically.

### Federated build-mode peer — end-to-end

A build-mode peer (e.g. a WSL builder; see `docs/install-docker.md`) joins
the federation, pulls the mirror, builds + publishes the packages it owns,
and the distribution publisher consolidates on sync-back.

**1. Identity + federation membership** (one-time):

```text
mirror init <peer-id>                 # Ed25519 builder keypair
key verify                            # tier-1 signing key must verify
mirror add <host> <ssh-url> --ssh-key <key> --proto https
mirror builders register <name>       # upload our pubkey + adopt the
                                       # owner's canonical pkg.list/pool.list
```

`mirror builders register` is gated on a verified tier-1 signing key **and**
SSH write access to the mirror (the pubkey upload proves it). It also
fetches the owner's **canonical config** (`pkg.list` + `pool.list`, pinned
by sha256 in the signed coord-head) and **overwrites** the peer's local
copies so the federation shares one selection. (See "Canonical config
propagation" below.) The tier-1 GPG key itself is transferred out-of-band —
see "Signing-key mismatch".

**2. Sync the mirror**:

```text
mirror pull
```

`mirror pull` **auto-adopts the mirror's snapshot pin forward** (never
backward): it reads the signed coord-head's `snapshot.current`, and if it's
newer than the local pin it advances the local pin to it before downloading
claims — so a peer always pulls the latest published packages instead of
silently filtering them out. It then **re-applies the canonical config** so
the owner's later source-select changes propagate to every builder.
Advancing the pin invalidates `cache_ready` / `dep_check_ready`; re-run
`cache build` + `cache parse`.

**3. Advance + build the subset you own**:

```text
snapshot select latest                # expose updatable packages
cache build && cache parse
source build                          # builds build_pkg.list; strips +
                                      # asg-stamps automatically
repo audit                            # local pool sanity
```

**4. Publish (packages only)**:

```text
mirror publish <name>                 # Mode = build implies --no-iso
mirror audit <name>                   # confirm claims + no conflicts
```

In build mode `mirror publish` **implies `--no-iso`** (a build peer never
builds ISOs, and its mirror snapshot may lead any ISO snapshot) and
publishes **only the packages it built and owns** — pulled-from-a-peer
records (`pulled_from`) are skipped so a peer never tries to claim another
builder's package. The closure gate is a local sanity check that *your*
packages resolve against the merged (pulled) pool; complete federation
closure is the distribution publisher's `mirror audit` on sync-back (see
"Installability gate"). If you publish while the mirror is ahead of your
pin, the publish warns loudly to `mirror pull` first.

**5. Publisher sync-back** (on the distribution machine):

```text
mirror pull                           # snapshot auto-adopts; peer .debs arrive
cache build && cache parse
repo audit && mirror audit
chroot build live && chroot build installer
iso build live && iso build installer
mirror publish                        # full publish incl. ISO leg
```

**Decommissioning a peer** (from any builder with the tier-1 key):

```text
mirror builders decommission <peer-id> [<name>]
```

Adds the peer to the coord-head's `revoked_builders` under the publish lock.
The peer's claims are then dropped federation-wide, so every filename it
owned becomes **no-owner** and other builders take them on their next
publish. (No need for the decommissioned peer's private key — claims are
per-builder signed, so `revoked_builders` is the key-less mechanism.) The
command refuses to revoke the *local* builder (self-lockout).

### Canonical config propagation

The distribution-mode owner is authoritative for `pkg.list` + `pool.list`.
On every `mirror publish` it writes `config/canonical.json` (the two list
files) into the coord tree and pins its **sha256 in the signed coord-head**
(`config_sha256`). Peers fetch it on `mirror builders register` (overwrite
on first register) and refresh it on every `mirror pull`. Nothing unverified
is ever applied — the head's GPG signature vouches for the hash, the hash
vouches for the file; a sha mismatch or a missing pin leaves the peer's
local lists untouched. A build-mode peer never owns the canonical config (it
preserves the owner's pin on publish).

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

**Mode responsibility split.** The gate runs in *both* modes, but its
consumer set differs:

- **Distribution publisher** — the consumer set is the FULL resolved
  closure (`dep_tree.selected_pkgs ∪ udeb`), so this is a *complete*
  repo-closure check. This is the authoritative gate.
- **Build-mode peer** — the consumer set is the SUBSET it built
  (`build_pkg.list`), so this is a *local sanity* check that the peer's
  own packages resolve against the merged (pulled) pool. A peer can't
  compute the full closure — it never parses the full `pkg.list` (that's
  the point of build mode), and the owner's corpus would be stale against
  the peer's advanced snapshot. Complete federation closure is therefore
  verified by the distribution publisher's `mirror audit` on sync-back,
  not by the peer's publish. Run `mirror pull` before a build-mode publish
  so `scan_repo_state` reflects the merged mirror pool.

### First-publish dist-mode gate

An build-mode builder publishing to a mirror with no `coord-head` yet
(fresh bootstrap) is REFUSED.  Bootstrapping an build-mode subset into
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

## LEDGER-01: claim lifecycle — how published files age out

The pool on a mirror is append-only, but selections change and
versions move forward.  LEDGER-01 makes that ageing explicit: instead
of inferring "this file is old" from absence, the signed claim ledger
records an end-of-life state per file, and the local `build.json`
records a per-source lifecycle.

### Three end-of-life claim states (claim schema v4)

| State | Written when | Ownership | Pool file |
|---|---|---|---|
| `retracted` | owner withdraws the claim (`mirror conflict resolve`) | — (tombstone; file metadata stripped) | withdrawn |
| `deprecated` | publish **Step 6b** — the file dropped out of our selection | RELEASED — `project_owners` reports no owner; any builder may take it over by republishing | stays in the append-only pool, adoptable |
| `obsolete` | publish **Step 6c** — this old version was superseded by a newer version from the SAME owner | RETAINED | stays in the pool as a labelled prune candidate (UPD-01 publish-before-prune) |

Steps 6b and 6c run automatically inside every `mirror publish` — no
operator action needed.  6b's authority is the signed
`selection.state` closure (not `build.json`, which lingers after a
drop).

Presence/index audits skip all three states
(`PRESENCE_SKIP_CLAIM_STATES` in `coord/schema.py`): a legitimately
pruned old file never fires a false `missing_on_disk` /
`claim_not_in_apt_index` CRITICAL.  Likewise, every audit folds out
claims that a later retraction / deprecation / obsolescence / reclaim
supersedes via its `*_seq` back-reference — only each filename's
current live assertion is audited.

### The local counterpart: `build.json` schema v4

Each `log/build/<pkg>.build.json` is now a per-source lifecycle
document: `selection ∈ selected | deprecated | retracted`, with
`selected_at` / `deprecated_at` / `retracted_at` / `published_at`
timestamps and a `history[]` of rolled episodes.  Lifecycle tracking
starts at `cache parse` — the moment a source enters the selection —
not at first build.

## RECLAIM-01: `mirror reclaim` — same-version rebuilds

**INVARIANT: a published filename's bytes are frozen forever.**  A
normal content change bumps the version and publishes forward.
`mirror reclaim` is the explicit operator-only exception for content
changes that are deliberately version-less — dep-strip normalisation
(Position-X), disaster-recovery rebuilds (builds are NOT
bit-reproducible, so a rebuilt `.deb` never byte-matches the
published copy).

```
mirror reclaim [<source>|<file.deb|.udeb>] [<mirror-name>] [force]
```

- **Bare `mirror reclaim`** — dry-run.  Lists the "local-ahead"
  candidates per mirror: files whose on-disk sha matches the local
  `build.json` `output_hashes` but whose remote claim pins an older
  sha.  A single argument naming a configured mirror scopes the
  listing to that mirror.
- **With a target** (a source name, or an exact `.deb`/`.udeb`
  filename) — shows each candidate's old→new sha pair, prompts YESNO
  (`force` skips the prompt), then runs the normal publish
  transaction with the reclaim claims injected: flock, federation
  gate, hash-conflict scan and the stale-index guard all apply
  unchanged.

A reclaim claim is not a marker like the lifecycle states above — it
is itself a LIVE `published` claim carrying `reclaims_seq`, a
back-reference to the superseded claim.  That back-reference is
folded at every supersession site, including the hash-conflict scan
(so old and new shas for one filename never read as a conflict).

Safety properties:

- Each intent is **re-validated inside the transaction** against the
  just-fetched remote view (`validate_reclaim_intents`): the
  back-referenced claim must still exist, still be ours, still be the
  filename's live post-fold assertion, and still carry the sha the
  operator confirmed.  Any drift between listing and execution skips
  that intent loudly instead of superseding the wrong claim.
- The pool push **overwrites** the remote file for reclaim claims
  only — normal pushes keep rsync `--ignore-existing`, so the
  append-only discipline stays intact for everything else.
- The publish status line appends "N reclaim(s)" when reclaims ride
  along.
- On the consuming side, `mirror pull` sha-rechecks a present local
  file ONLY when its claim carries `reclaims_seq`, and re-downloads
  on mismatch — reported as `refreshed=N` in the pull summary.

The audit finding that points here is
`own_claim_local_ahead_of_remote` (WARNING): "bump + publish, or
`mirror reclaim <source|file>` if this content change is deliberately
version-less".

## `mirror pull` boundary semantics

`mirror pull` fills in what OTHER builders published; it is not a
backup of your own work.  Two rules worth internalising:

- **Skip-own (security rule).**  Pull NEVER downloads a claim signed
  by our own builder id — the local build is the authority for our
  packages.  Consequence: pull is NOT a restore path for our own lost
  files.  Recovery from a wiped local pool is rebuild + `mirror
  reclaim` (the rebuilt bytes won't match the published claim), or a
  deliberate manual copy off the mirror.
- **End-of-life states are skipped.**  Claims in `retracted` /
  `deprecated` / `obsolete` are never downloaded.
- **Snapshot auto-adopt (forward-only).**  Pull reads the signed
  coord-head's `snapshot.current` and, if it is newer than the local pin,
  advances the local pin to it *before* the claim walk — so the snapshot
  filter doesn't silently skip a peer's newer packages. Never rolls a pin
  backward. Advancing invalidates `cache_ready` / `dep_check_ready`
  (re-run `cache build` + `cache parse`).
- **Canonical config refresh.**  Pull re-applies the owner's verified
  `pkg.list` / `pool.list` (see "Canonical config propagation"), so
  source-select changes propagate to every builder.

## Publish hardening (2026-06-11)

Two failure modes observed live, both now closed inside
`mirror publish`:

- **Stale-index auto-reindex.**  Publish pushes `dists/` verbatim and
  the coord-head pins its sha, so a stale local index used to publish
  "cleanly" while apt clients kept resolving superseded metadata.
  Publish now re-indexes not only when the local `InRelease` is
  missing but whenever it is STALE — any pool artifact (or pool
  directory, which catches deletion-only changes) newer than the
  InRelease triggers a fresh index before the push.  Operators no
  longer need to delete `InRelease` manually after touching the pool.
- **Append-only pool protected from `--delete`.**  The dist-tree push
  (rsync `--delete` over `dists/<codename>/`) now PROTECTS pool
  artifacts (`*.deb` / `*.udeb`) from receiver-side deletion.  The
  pool lives INSIDE the dists tree, so a local prune used to
  propagate to the remote on the next publish — violating the
  append-only invariant.  `--delete` still reaps stale index files
  and removed-component dirs.  Remote pruning is an explicit operator
  action, never a push side effect.

Related local-cleanup ordering: publish FIRST, then
`repo repair cleanup`.  Pruning before publishing leaves
`own_claim_disk_missing` CRITICALs in `mirror audit` until the next
publish reconciles them.

## Migrating from the legacy `[Repo]` keys

The pre-MIRROR-01 publish keys have been **removed** from the shipped
`config/build.conf` (they were dead — readers gone since `8cc803b`, and they
leaked one machine's mirror identity into a tracked file):

```
AptSourceURL     = http://…/asgard/
PublishSshTarget = user@host
PublishSshKey    = config/repo_asgard.key
ExternalEnabled  = true
```

If an old checkout still has them, delete them.  Mirror endpoints now live
per-machine in the **untracked** `config/mirror.<name>.state` files (created by
`mirror add` / onboarding) and machine-local settings in `config/local.conf` —
both gitignored.  Keep `SigningKeyUid` (used by tier-1 GPG signing).  Keep every
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
`chroot build` auto-indexes when the local `InRelease` is missing,
and `mirror publish` additionally re-indexes when the InRelease is
STALE (any pool artifact newer than it; see "Publish hardening"
above).  Use `repo repair` for the remaining repo-state fixups
(`strip`, `cleanup`, `backfill-hashes`).

## `mirror summary` per-mirror `we_own` count

The summary now lists `we_own: N pkg(s)` per mirror — counts the live
claims this builder owns on each mirror, with an end-of-life
breakdown appended when present (e.g. `42 pkg(s) (3 obsolete,
2 deprecated, 1 retracted)`), sourced from the most-recently-fetched
`cache/mirror/<name>/fetched/claims/<our-id>.jsonl`.

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

### Findings vocabulary

The full set of per-claim findings `mirror audit` can emit, beyond
the federation-gate and signature checks:

| Finding | Sev | Meaning |
|---|---|---|
| `own_claim_disk_missing` | CRITICAL | our claim references a file absent from the local repo — our pool diverges from our sidecar (typical cause: pruned before publishing) |
| `own_claim_local_ahead_of_remote` | WARNING | on-disk sha matches our local `build.json` but the remote claim pins an older sha — bump + publish, or `mirror reclaim <source\|file>` if the content change is deliberately version-less |
| `own_claim_disk_sha_mismatch` | CRITICAL | file disagrees with both the claim AND any local build record — real bitrot / corruption |
| `claim_not_in_apt_index` | CRITICAL | claim's filename is in no Packages file under the verified InRelease |
| `claim_apt_sha_mismatch` | CRITICAL | claim sha and Packages sha disagree for the same filename |
| `missing_on_disk` | CRITICAL | sidecar claim references a file not in the mirror's pool |
| `orphan_on_disk` | WARNING | pool file with no claim backing it |
| `hash_conflict` | CRITICAL | two builders claim the same filename with different shas → PUBLISH_HALT |
| `reproducible_duplicate` | INFO | two builders claim the same filename with the SAME sha |

Superseded claims are folded out of ALL of these: a claim targeted by
a later retraction / deprecation / obsolescence / reclaim
back-reference (`retracts_seq` / `deprecates_seq` / `obsoletes_seq` /
`reclaims_seq`) is never audited as a live assertion, so pruned old
versions and reclaimed files don't fire false findings.

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
mirror publish [<name>]         per-file .deb push + sign claims + re-sign coord-head (federation-gated + snapshot-base-gated; auto-reindexes a missing/stale local InRelease; warns on snapshot divergence; Mode=build implies --no-iso + owned-only; a blocked publish names the builder holding the lock)
mirror pull [<name>]            fetch peer sidecar, download missing claim .debs (skip-own; SHA verified; retracted/deprecated/obsolete skipped; reclaimed files refreshed; auto-adopts the mirror's snapshot pin FORWARD; refreshes the canonical pkg.list/pool.list)
mirror reclaim [<src>|<file>] [<name>] [force]
                                same-version rebuild: bare = list local-ahead candidates; with target = overwrite published bytes under unchanged filename (sanctioned invariant exception)
mirror audit [<name>]           federation consistency, claim sigs, hash conflicts, cross-mirror pool drift, on-disk pool ↔ claims integrity
mirror query <pkg> [<name>]     show claims matching <pkg> from last fetched view of each mirror
mirror builders [list]          list registered builders (local + fetched keyring)
mirror builders register <name> register THIS builder on a mirror: upload our pubkey (needs signing key + SSH write) + adopt the owner's canonical pkg.list/pool.list
mirror builders decommission <id> [<name>]
                                retire builder <id>: add to coord-head revoked_builders so its claims drop and its packages become no-owner (peers take them)
mirror conflict resolve <pkg>   retract our claim for <pkg>; clear PUBLISH_HALT
```
