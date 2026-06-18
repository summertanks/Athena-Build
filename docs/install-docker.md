# Installing Docker on the build host

Athena-Build's source-build stage runs each `dpkg-buildpackage` inside a
Docker container so the build host stays clean and the build is hermetic
against host-installed dev packages.

For a **native-Linux distribution-build host** (the full pipeline — chroot,
ISO, disk image) use **Docker Engine**, not Docker Desktop: a full build
also uses host-side privileged operations (loop devices, mounts, sudo) and
Docker Desktop's VM doesn't expose the host filesystem the way those steps
expect.

For a **WSL build-mode peer** (`[Build] Mode = build` — see "WSL build-mode
peer" below), **Docker Desktop's WSL integration is fine**: build mode only
runs the Docker-based source build (chroot/ISO/disk steps are refused), and
its bind-mounts are Linux paths *inside the WSL distro*, which WSL
integration handles correctly. No native Engine required there.

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

## WSL build-mode peer (Docker Desktop integration)

A federation **build-mode peer** can run under WSL2 on Windows using
**Docker Desktop with WSL integration enabled** — the host's Docker engine
is shared into the distro, so `docker.from_env()` connects to it over the
integrated socket. This is *not* docker-in-docker and *not* a VM-as-build-
environment: WSL2 is the Linux userland, and Docker Desktop is the engine
exposed into it.

Setup:

1. Install Docker Desktop on Windows and enable **Settings → Resources →
   WSL integration** for your distro.
2. In the WSL distro, confirm the engine is reachable:
   ```bash
   docker info        # should print the Docker Desktop engine, no sudo
   ```
3. Set `[Build] Mode = build` in `config/build.conf` and populate
   `config/build_pkg.list` with the packages this peer owns.

On startup Athena-Build logs the connected Docker endpoint + engine identity
so you can confirm it's the host Docker Desktop daemon (shared via WSL
integration) and that daemons aren't being nested. With `Mode = build`,
`build-system.sh` also downgrades the ISO/disk host-tool checks from fatal
to a note — a build-mode peer needs only Docker + the cache/source
toolchain, not `mksquashfs` / `losetup` / `xorriso` / `mkfs.*`.

What stays on the build-mode (Docker-only) path — and therefore works under
WSL integration without host privileges: cache build, dep parse, source
sync, container init, source build (strip + asg-stamp), and `mirror
publish`. The privileged host operations (loop devices, `mkfs`, host sudo,
mounts) only appear in the chroot/ISO/disk steps, which build mode refuses.

> Note: `ATHENA_ALLOW_IPV6=1` is *not* needed under WSL — Athena pins HTTP
> to IPv4 at startup, which also sidesteps WSL2's NAT-IPv6 stalls.

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
