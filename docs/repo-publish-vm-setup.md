# Publishing the Asgard apt repo to a VM (COMP-02)

How to host the built apt repository on a VM (cloud or local) and publish to
it over rsync + SSH, so an installed Asgard system can `apt update` /
`apt install` from it.

This is the host-without-limits alternative to GitHub Pages, which can't
hold the repo (GitHub rejects >100 MB files and Pages caps a site at ~1 GB;
the pool is multiple GB).

## How it fits together

```
build box                         VM (140.245.198.222)            apt clients
─────────                         ────────────────────            ───────────
repo/ or publish/   ──rsync+ssh──▶  ~/asgard/{dists,pool}  ──http──▶  apt update
  (repo index …)     repo publish     (served by nginx)      AptSourceURL
```

- **`repo index …`** builds the signed metadata.
- **`repo publish ssh …`** rsyncs the tree to the VM (incremental; `--delete`
  keeps it a true mirror).
- **nginx** on the VM serves the synced directory over HTTP.
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

### Install + configure nginx

```bash
sudo apt-get update && sudo apt-get install -y nginx

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

## 3. Build the repo metadata

```
repo index full       # all suites in place under repo/dists/ (incl. debug)
# or
repo index minimal    # runtime subset into publish/ (main debs, no
                       # -dbg/-dbgsym/-source/udeb; signed)
```

## 4. Publish

```
repo publish ssh full       # rsync repo/dists  → VM   (complete pool)
# or
repo publish ssh minimal    # rsync publish/{dists,pool} → VM (runtime subset)
```

rsync shows a progress bar (parsed from `rsync --info=progress2`).
Re-publishing only transfers changed/new files.

## 5. Point the installed system at it

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

## 6. Verify end to end

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
| apt: `does not have a Release file` / signature errors | The repo wasn't indexed/signed (`repo index …`), or the keyring isn't in the chroot, or `AptSourceURL` points at the wrong path. |
| `Permission denied (publickey)` | Wrong/again-too-open key, or key not on the VM's `authorized_keys`. Key must be `chmod 600`. |

### Alternative: serve from `/var/www` instead of the home dir

Avoids touching home-dir permissions:

```bash
sudo mkdir -p /var/www/asgard && sudo chown ubuntu:ubuntu /var/www/asgard
```

Then set `PublishSshTarget = ubuntu@140.245.198.222:/var/www` (publish lands
at `/var/www/asgard`) and point the nginx `alias` at `/var/www/asgard/`.
