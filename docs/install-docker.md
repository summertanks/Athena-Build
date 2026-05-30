# Installing Docker on the build host

Athena-Build's source-build stage runs each `dpkg-buildpackage` inside a
Docker container so the build host stays clean and the build is hermetic
against host-installed dev packages.  You need **Docker Engine** (not
Docker Desktop — Desktop's VM doesn't expose the host filesystem the way
the build pipeline expects).

The distribution-repo Docker is usually too old.  Use Docker's own apt
repository for an up-to-date Engine.

## On Debian / Debian-derived (bookworm, trixie)

All commands as root (or prefixed with `sudo`):

```bash
apt-get remove docker docker-engine docker.io containerd runc
apt-get install ca-certificates curl gnupg lsb-release
mkdir -m 0755 -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) \
signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install docker-ce docker-ce-cli containerd.io \
                docker-buildx-plugin docker-compose-plugin
usermod -aG docker $USER
```

Log out + back in (or `newgrp docker`) so the group change takes effect,
then verify:

```bash
docker run --rm hello-world
```

## On Ubuntu

Swap the `debian` paths for `ubuntu`:

```bash
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) \
signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null
```

Everything else is identical.

## Notes

- The `usermod -aG docker $USER` step is the bit that lets the
  unprivileged build user run `docker` without `sudo`.  Without it,
  Athena-Build's `container init` step prompts for sudo to run the
  daemon command — workable but slower and noisier.
- Athena-Build does not depend on Docker Compose or Buildx; the
  `docker-buildx-plugin` / `docker-compose-plugin` packages in the
  install line are habit, not requirement.  Drop them if you want a
  thinner install.
- Once Docker is running, `container init` builds
  `athenalinux:build-<release>` from `config/Dockerfile`.  The image's
  apt sources point at the snapshot (CONF-15) so it stays reproducible
  alongside the rest of the build.
