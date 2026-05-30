# Publishing the Asgard apt repo (COMP-02)

How to publish the built apt repository so an installed Asgard system can
`apt update` / `apt install` from it.  Two transports are supported:

- **`repo publish ssh`** — to a VM (cloud or local) over rsync + SSH; an
  HTTP server on the VM (nginx) serves the result.  Use this when the repo
  needs to be reachable from multiple machines.
- **`repo publish local <path>`** — to a local filesystem path: USB stick,
  mounted NFS / SMB share, a web server's docroot on the build host, etc.
  Use this for sneakernet, single-host installs, or when staging a tree
  before later transport.

Both transports respect `[Repo] ExternalEnabled`; disabled = manifest-only
(no transport at all).  Either is a host-without-limits alternative to
GitHub Pages, which can't hold the repo (GitHub rejects >100 MB files and
Pages caps a site at ~1 GB; the pool is multiple GB).

## How it fits together

```
build box                         VM (140.245.198.222)            apt clients
─────────                         ────────────────────            ───────────
repo/ or publish/   ──rsync+ssh──▶  ~/asgard/{dists,pool}  ──http──▶  apt update
  (repo index …)     repo publish     (served by nginx)      AptSourceURL

                       OR, for a local destination:

repo/ or publish/   ────rsync────▶  /mnt/usb/asgard/dists/  (or any local path)
                    repo publish local <path>
```

- **`repo publish ssh …`** rsyncs the `.deb`s to the VM **additively**
  (no `--delete`; `--ignore-existing` — pool `.deb`s are immutable per
  filename, so a re-publish never re-uploads an existing one), then **rebuilds
  the index ON THE VM** with `dpkg-scanpackages` (so `Packages` always reflects
  what's actually there — `full` and `minimal` never clobber each other) and
  **signs locally** (the signing key never leaves the build box).  No separate
  `repo index` step is needed.
- **`repo publish local <path>`** runs the same flow without the ssh wrapper:
  rsync local-to-local (same additive + `--ignore-existing` invariants),
  `dpkg-scanpackages` in-process against `<path>/dists/`, sign locally, refresh
  the manifest.  If `<path>` doesn't exist, you'll be prompted to `mkdir -p`.
- **nginx** on the VM (or any HTTP server pointed at the local destination)
  serves the synced directory over HTTP.
- The installed system reads it via **`AptSourceURL`** (written into
  `/etc/apt/sources.list.d/athena.list`).

The remote folder name is **derived from the distribution id**
(`[Build] DISTRIBUTION` lowercased, e.g. `asgard`), not hardcoded — so a
rebrand follows automatically. With `PublishSshTarget = user@host` the repo
lands at `~/<dist-id>/` (e.g. `~/asgard/`); with `user@host:/base` it lands
at `/base/<dist-id>/`.

## 1. Build-side config (`config/build.conf`, `[Repo]`)

```ini
# Where to publish (user@host, or user@host:/base). The <dist-id> folder
# is appended automatically.
PublishSshTarget = ubuntu@140.245.198.222
# SSH private key for the publish (kept out of git via .gitignore).
PublishSshKey = config/repo_asgard.key
# The HTTP(S) URL the VM serves the repo at — written into the installed
# system's apt sources. Set after nginx is up (step 5).
AptSourceURL =
```

The key must be `chmod 600`. `config/*.key` is git-ignored, so the private
key is never committed.

## 2. One-time VM setup

SSH in first (this also records the host fingerprint — see Troubleshooting):

```bash
ssh -i config/repo_asgard.key ubuntu@140.245.198.222
```

### Create the repo directory

Publish does **not** create it (no `--mkpath`); the `<dist-id>` dir must
exist. It checks over SSH and bails with a hint if missing:

```bash
mkdir -p ~/asgard
```

### Install + configure nginx (and `dpkg-dev`)

`dpkg-dev` provides `dpkg-scanpackages`, which `repo publish` runs ON THE VM to
rebuild the index from the actual pool contents (UPD-02). Without it, publish
fails at the re-index step with a clear hint.

```bash
sudo apt-get update && sudo apt-get install -y nginx dpkg-dev

# nginx (www-data) must be able to traverse the home dir; the repo's own
# files are already world-readable from `rsync -a`.
chmod o+x /home/ubuntu

sudo tee /etc/nginx/sites-available/asgard >/dev/null <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location /asgard/ {
        alias /home/ubuntu/asgard/;
        autoindex on;          # browse the repo in a browser (handy for debugging)
    }
}
EOF

sudo rm -f /etc/nginx/sites-enabled/default          # avoid duplicate default_server
sudo ln -sf /etc/nginx/sites-available/asgard /etc/nginx/sites-enabled/asgard
sudo nginx -t && sudo systemctl reload nginx
```

This serves `~/asgard/` at `http://<host>/asgard/`.

### Open port 80

Check locally on the VM first:

```bash
curl -sI http://localhost/asgard/dists/thor/InRelease
```

If that's `200 OK` but it's unreachable from outside, it's the firewall.
**On Oracle Cloud (OCI) port 80 is blocked in two places:**

- **OCI Security List / NSG** (cloud console): add an ingress rule — TCP,
  port 80, source `0.0.0.0/0`. (Done in the OCI web console.)
- **Instance iptables** (OCI Ubuntu images block all but SSH):
  ```bash
  sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
  sudo netfilter-persistent save        # persist across reboot (iptables-persistent)
  ```

## 3. Publish (indexing happens at the destination)

No separate `repo index` step — `repo publish` pushes the `.deb`s and then
rebuilds + signs the index.

### SSH transport

```
repo publish ssh full       # push the whole pool's .debs → VM
# or
repo publish ssh minimal    # push only the runtime subset (no -dbg/-dbgsym/
                            #   -source/udeb), same nested layout as full
```

Each publish: rsyncs only the `.deb`s up (additive, `--ignore-existing` — so a
re-publish transfers just genuinely-new `.deb`s, never re-uploading immutable
ones), runs `dpkg-scanpackages` on the VM to rebuild `Packages` from what's
actually present, signs `Release`/`InRelease` **locally**, uploads the small
metadata, and refreshes the local signed manifest.  Because the index is
rebuilt from the remote, `full` then `minimal` (or either order) leaves the
published `Packages` as the **union** on the remote — no clobber.

### Local transport

```
repo publish local /mnt/usb/asgard full        # whole pool → local path
# or
repo publish local /var/www/asgard minimal     # runtime subset only
```

Same flow as the SSH transport but in-process:
- rsync runs local-to-local (no `-e ssh`),
- `dpkg-scanpackages` runs against `<path>/dists/` directly,
- signing happens locally as before,
- the local signed manifest reflects what was just laid down.

If `<path>` doesn't exist you'll be prompted (`Create <path>?  y/n`) to
`mkdir -p` it.  Useful for sneakernet (USB stick → offline target), staging
under a web docroot served by a local nginx, or single-host testing.

### Local-only testing (no destination at all)

`repo external disable` → `repo publish ssh full` indexes into the local
signed manifest and skips rsync.  `repo external enable` returns to
publishing (onto an empty remote it rebaselines `current = base`).

## 3a. Summary (inspect a published destination)

To verify the state of an already-published destination without re-publishing:

```
repo summary ssh                       # against [Repo] PublishSshTarget
repo summary local /mnt/usb/asgard     # against any local path
```

Reports for both transports:
- destination + suites discovered
- file count + total bytes under `dists/`
- per-suite `InRelease` signature (verifies against our pubkey) + its
  `Date:` field
- snapshot pin state (base / published / current) from `config/snapshot.state`
- local signed manifest tally (binary versions / packages tracked — the
  `+asg uN` bump authority)

Read-only — never mutates the destination.

## 4. Point the installed system at it

Once `http://140.245.198.222/asgard/` serves the repo, set in `build.conf`:

```ini
AptSourceURL = http://140.245.198.222/asgard/
```

On the next `chroot build`, the installed system gets
`/etc/apt/sources.list.d/athena.list`:

```
deb [signed-by=/usr/share/keyrings/athena-archive-keyring.gpg] \
    http://140.245.198.222/asgard/ thor main
```

(The keyring is installed into the chroot by CONF-02; `Release`/`InRelease`
are signed by `[Repo] SigningKeyUid`.)

## 5. Verify end to end

The built-in check (reads `AptSourceURL`) — confirms the published repo is
reachable, its `InRelease` is signed by our key, and every index's
size/SHA256 matches:

```
repo audit external
```

Or a raw HTTP probe from the build box:

```bash
curl -sI http://140.245.198.222/asgard/dists/thor/InRelease     # expect 200
```

On a booted Asgard system:

```bash
sudo apt update
apt-cache policy <some-package-in-the-pool>
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Host key verification failed` during publish | Publish is non-interactive (no TTY), so ssh can't prompt to trust a new host. Accept it once: `ssh -i config/repo_asgard.key ubuntu@140.245.198.222 echo ok`. |
| `remote dir 'asgard' not found …` | The `<dist-id>` dir doesn't exist on the VM. `ssh … mkdir -p asgard`. |
| `curl` 200 on the VM (`localhost`) but not externally | Firewall — open port 80 in both the OCI Security List **and** the instance iptables (step 2). |
| nginx `403 Forbidden` | `www-data` can't traverse `/home/ubuntu`. `chmod o+x /home/ubuntu`. |
| apt: `does not have a Release file` / signature errors | The publish's re-index/sign step failed, the keyring isn't in the chroot, or `AptSourceURL` points at the wrong path. |
| publish fails at "remote re-index" / `dpkg-scanpackages: command not found` | `dpkg-dev` isn't installed on the VM — `sudo apt-get install -y dpkg-dev` (step 2). |
| `Permission denied (publickey)` | Wrong/again-too-open key, or key not on the VM's `authorized_keys`. Key must be `chmod 600`. |

### Alternative: serve from `/var/www` instead of the home dir

Avoids touching home-dir permissions:

```bash
sudo mkdir -p /var/www/asgard && sudo chown ubuntu:ubuntu /var/www/asgard
```

Then set `PublishSshTarget = ubuntu@140.245.198.222:/var/www` (publish lands
at `/var/www/asgard`) and point the nginx `alias` at `/var/www/asgard/`.
