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

| Form | Example | Inferred type |
|---|---|---|
| `ssh://user@host/abs/path` | `ssh://deploy@mirror.example/srv/asgard` | `ssh` |
| `user@host:/abs/path` (rsync shorthand) | `deploy@mirror.example:/srv/asgard` | `ssh` |
| `file:///abs/path` | `file:///srv/asgard` | `local` |
| `/abs/path` (absolute, no scheme) | `/srv/asgard` | `local` |

Pass `--type ssh|local` to override inference.  Bare `user@host`
(no path) is rejected — the path was implicit at `~/<dist-id>/` under
the legacy `[Repo] PublishSshTarget` key; MIRROR-01 requires an explicit
path so URLs are unambiguous for federation propagation.

## Adding a mirror

```
mirror add <name> <url> [--ssh-key PATH] [--type ssh|local]
```

- **`<name>`** — ASCII alphanumerics + `-` / `_`, 1–64 chars.  Becomes
  the filename component of `config/mirror.<name>.state`.  Must be
  unique locally; URLs must also be unique across all configured mirrors.
- **`<url>`** — the **pool** URL (see forms above).
- **`--ssh-key`** — private key for rsync/ssh transport (ssh mirrors).

What `add` does:

- Writes `config/mirror.<name>.state` with
  `base = current = <local snapshot.current>` so the mirror starts at
  parity with this builder.
- **Does not** touch the remote: no SSH probe, no rsync, no coord-head
  write.
- **Does not** auto-propagate the new URL to existing peers' signed
  `neighbours` list — that's `mirror reconcile-neighbours`.

## First-time setup (fresh remote)

Create the sidecar tree on the remote.  The apt pool dir (e.g.
`~ubuntu/asgard`) may already exist from prior use; the new piece is
its `-coord` sibling:

```
ssh -i config/repo_asgard.key ubuntu@140.245.198.222 mkdir -p asgard asgard-coord
```

Register and publish:

```
mirror add primary ssh://ubuntu@140.245.198.222/home/ubuntu/asgard \
                   --ssh-key config/repo_asgard.key
mirror publish primary
```

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
mirror list             # expect: primary  [ssh   ]  in-sync   ssh://...
mirror status primary   # expect: PUBLISHED  current=<your snapshot.current>
mirror audit primary    # expect: no CRITICAL findings
```

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
| `PublishSshTarget = ubuntu@host` (path implicit at `~/asgard`) | `mirror add primary ssh://ubuntu@host/home/ubuntu/asgard` |
| `PublishSshKey = config/repo_asgard.key` | `--ssh-key config/repo_asgard.key` on `mirror add` |
| `AptSourceURL = http://host/asgard/` | Set in each installed-system `sources.list.d/athena-<name>.list` automatically; nothing to do |
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
mirror add <name> <url> --ssh-key ...      # re-register clean
mirror publish <name>                      # re-bootstraps
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

## Subcommand quick reference

```
mirror init <id>                generate Ed25519 builder identity + persist BUILDER_ID
mirror add <name> <url>         register a mirror; seeds base+current from snapshot.current
mirror remove <name|url>        unregister LOCALLY (no remote/peer changes)
mirror list                     name, type, federation-consistency tag, url
mirror summary [<name>]         full per-mirror state dump
mirror status [<name>]          builder identity + halt sentinel + per-mirror PUBLISHED/NEVER PUBLISHED
mirror reconcile-neighbours     fan-out: align every peer's coord-head.neighbours with local config
mirror publish [<name>]         per-file .deb push + sign claims + re-sign coord-head (federation-gated)
mirror pull [<name>]            fetch peer sidecar, download missing claim .debs (skip-own; SHA verified)
mirror audit [<name>]           federation consistency, claim sigs, hash conflicts, cross-mirror pool drift
mirror query <pkg> [<name>]     show claims matching <pkg> from last fetched view of each mirror
mirror builders                 list registered builders (local + fetched keyring)
mirror conflict resolve <pkg>   retract our claim for <pkg>; clear PUBLISH_HALT
```
