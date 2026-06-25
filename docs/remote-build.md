# Remote build hosts + the local build mirror

Two related build-execution optimisations:

- **Remote build hosts** — fan source builds out across one or more remote
  Docker hosts over SSH (`source remotebuild`), recovering the `.debs` locally
  exactly as a local build would.
- **Local build mirror** — a snapshot-pinned apt mirror of the *build closure*
  (Build-Depends), so a container resolves toolchain packages from local disk
  instead of reaching out to `snapshot.debian.org` on every build.

Both are off by default and opt-in. Neither changes the local `source build`
path, the repo layout, or the published artifacts — a remote-built `.deb` and
its `build.json` are byte-for-byte what a local build produces.

---

## Remote build hosts

### The model — ship-to-host, not drive-a-daemon

A remote build is **ship-to-host**: per package we compute the recipe LOCALLY
(`BuildContainer.compose_recipe` — image tag, build-args, `cmd_str`, the patch
list), then `scp` a self-contained bundle (source + patches + Dockerfile +
`remote_build.py`) to the remote and run it over `ssh`, so `docker build` /
`docker run` happen ON the remote (Docker is local there, so bind mounts work).
Only execution is remote; the `.debs` are scp'd back and run through the SAME
local post-build pipeline (segregate → normalize → record). This is distinct
from `[Build] DOCKER_SERVER`, which would drive a *remote daemon's* API.

### Registering a remote — `container remote add`

`container remote add` is guided: it takes an SSH key (copied into
`config/<name>.key`, 0600 — like `mirror add`, not your ambient `~/.ssh`),
probes the host (ssh reachability, `docker` access + group membership,
`python3`, CPU cores, RAM), prompts for per-host build caps (blank = no cap),
asks whether to enable a local build mirror ON that remote, and prints a
summary before persisting.

```
container remote add                 # fully guided
container remote add <name> ssh://user@host   # name+host from argv, rest prompted
```

The non-interactive one-liner is still accepted:

```
container remote add <name> ssh://user@host key=<path> jobs=<N> cpus=<C> mem=<M>
```

Registration is stored in the UNTRACKED `config/remote.conf` as a
`[Remote.<name>]` section:

| Key                 | Meaning                                                    |
|---------------------|------------------------------------------------------------|
| `Host`              | `ssh://user@host[/...]`                                     |
| `SshKey`            | path to the copied key (`config/<name>.key`)               |
| `MaxParallelBuilds` | per-host concurrency cap (blank/0 → 1)                      |
| `BuildCpus`         | Docker `--cpus` for this host (blank/0 → no cap)            |
| `BuildMemory`       | Docker `--memory` for this host (blank → no cap)           |
| `LocalMirror`       | `true` → stage an on-remote build mirror at init           |

### The other `container remote` actions

| Command                          | What it does                                                        |
|----------------------------------|---------------------------------------------------------------------|
| `container remote init`          | eager-stage the toolchain image on EVERY configured remote (copy-from-local if cached here, else build it on the remote); also stages the on-remote build mirror for any remote with `LocalMirror=true` |
| `container remote list`          | table of configured remotes + caps                                 |
| `container remote test <name>`   | full sanity sweep: ssh / docker / python3 / cpu / ram / image presence |
| `container remote delete <name>` | remove the remote + its copied `config/<name>.key`                 |
| `container remote purge <name>`  | stop+remove `athenalinux` containers and images ON the remote (free space / force a fresh image next build) |

### Building — `source remotebuild`

`source remotebuild` has the SAME command surface as `source build` (subset
selectors `pkg`/`live`/`installer`/`recommended`/`all`, named packages,
`force`, and the `[profile,…]` override):

```
source remotebuild all
source remotebuild <pkg> ...
source remotebuild force live [nodoc,nocheck]
```

Packages are distributed across all configured remotes concurrently, each
honouring its own `MaxParallelBuilds` / `BuildCpus` / `BuildMemory`. Heavy
packages drain the fleet before resuming; a transport failure on one remote
marks it down and re-queues its work to another (bundles are host-agnostic);
SIGINT terminates the live ssh sessions. If the remote registry is empty it
falls back to the legacy single `[Build] RemoteBuildHost`.

**Tunneled packages are acquired LOCALLY** (they are network-bound + repo-
locked) and never ship to a remote. The local `source build` path is untouched.

> Update-mode (`+asg` delta) builds work on the remote path too: a bare/subset/
> `all` invocation with a pending delta routes through the same update workflow,
> the remotes build the changed sources, and the `+asg<R>u<N>` stamp is applied
> locally on recovery. No bump logic runs on the remote — the decision is
> computed on the build system and passed through (CONS-10).

---

## Local build mirror

When enabled, the build closure (the Build-Depends of the selected sources,
expanded transitively via Depends/Pre-Depends) is staged as a flat apt repo
pinned `Origin: AthenaLocalMirror`
(priority 1002 > snapshot 1001), and the build container mounts it read-only at
`/localmirror` with a `file:///localmirror ./` source. Apt then serves
build-deps from local disk; only packages outside the closure fall through to
the snapshot mirror. Fork packages (those with `file://` Filenames) are excluded
— the closure mirror carries upstream snapshot bytes only.

### On the build host (BS1's own mirror)

A machine-local decision, separate from any remote's:

```
set create-local-mirror true        # persists local.conf [Local] CreateLocalMirror
```

When on, `cache parse` builds/refreshes the mirror for the current snapshot
(incrementally on a snapshot advance). Manage it directly with:

| Command                            | What it does                                  |
|------------------------------------|-----------------------------------------------|
| `container local mirror build`     | build/refresh for the current snapshot        |
| `container local mirror rebuild`   | force a full rebuild (ignore the up-to-date check) |
| `container local mirror status`    | package count / size / pinned snapshot        |
| `container local mirror purge`     | delete the mirror contents                    |

### On a remote (per-remote, `LocalMirror=true`)

The on-remote mirror is **per-remote** (asked at `container remote add`, stored
on the `[Remote.<name>]` entry) — NOT the machine-wide `create-local-mirror`.
This matters because BS1 staging its own mirror over a thin link to feed a
remote would be backwards: the remote populates its mirror over ITS OWN
connection. `container remote init` stages it (resumable, incremental on a
snapshot advance), gated until complete. `source remotebuild` then mounts it on
that remote per its flag.

---

## See also

- [`docs/architecture.md`](architecture.md) § Build execution — where the
  `remote_*` / `local_mirror` modules sit in the pipeline.
- [`docs/mirror-setup.md`](mirror-setup.md) — the federation / publish surface
  (a separate concern: remotes are build *executors*, not publish targets).
