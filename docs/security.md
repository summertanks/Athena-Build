# Athena-Build — security model

This document describes Athena-Build's trust assumptions and the
boundaries that the build pipeline relies on.  It is not a complete
threat model; it covers the points that operators routinely get wrong.

## Trust assumptions

Athena-Build assumes:

1. The **operator's host** (the machine running `build-system.sh`) is
   trusted.  Code with shell access to this host can read your sudo
   password, the keyring you build with, and every source archive on
   disk.
2. The **Debian / configured mirrors** are trusted to serve genuine
   `.deb` data.  Athena enforces this via GPG verification of every
   mirror's `InRelease` file (see `STA-01` / `[Security]` config
   section).  Don't disable that.
3. The **Docker daemon** the build container runs against is on the
   same trust boundary as the host.  See "The build container is a
   sandbox, not a security boundary" below.

Violations of (1) defeat everything else; (2) is mechanically enforced;
(3) is the part operators most often get wrong, so it gets its own
section.

## The build container is a sandbox, not a security boundary

`config/Dockerfile` creates user `athena` with **passwordless sudo to
anything**:

```dockerfile
RUN useradd -G sudo -ms /bin/bash athena
RUN echo 'athena ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers
USER athena
```

This is necessary.  `dpkg-buildpackage` and the Debian build-helper
machinery routinely call `sudo` for build-dependency installs, dpkg
invocations, file-permission fixups, and so on.  An unattended
per-package build loop cannot tolerate TTY-prompted passwords.

The consequence is that **the build container is functionally a
privileged sandbox**:

- Inside the container, the `athena` user is functionally root.
- Source-package code (`debian/rules`, maintainer scripts) executes
  there, with that authority.
- The container has bind-mounts of the host's `source/`, `repo/`, and
  per-package `patch/` directories.  Container-root can write anywhere
  in those bind-mounts on the host.
- The Docker daemon shares the host kernel.  A kernel-bug exploit
  reaching container-root reaches the host.

The defence is **not** to harden the container; it's to ensure that
only code you trust ever ends up running in the container, and that
the daemon controlling the container is itself out of attacker reach.

## DOCKER_SERVER guard

`build.conf` accepts a `DOCKER_SERVER = …` entry that points
`BuildContainer` at a non-default Docker daemon.  If this is set to a
**network-reachable daemon without TLS**, anyone reaching that TCP
socket can `docker run -v /:/host` and own the host running that
daemon.

`BuildContainer._guard_docker_server` checks the URL before any client
call and **raises** unless one of:

- `unix:///path/to/socket` — same host, filesystem-protected
- `tcp://127.0.0.1:…` / `tcp://[::1]:…` / `tcp://localhost:…` — loopback
- `https://…` — TLS-protected (the docker SDK enforces server-cert
  validation)
- A URL that contains `tls=true` or `tls=1` — the operator has
  explicitly set up client-cert auth out of band

If the guard refuses your `DOCKER_SERVER` value, you have two
acceptable options:

1. Run the docker daemon locally and unset `DOCKER_SERVER` (the build
   falls back to `docker.from_env()`).
2. Set up [Docker TLS][docker-tls] with client-cert auth, then add
   `?tls=true` to the URL.

Do not "work around" the guard by binding the daemon to `0.0.0.0:2375`
without TLS — that endpoint is a privilege-escalation primitive and
will be found by any internet-wide scanner within minutes.

[docker-tls]: https://docs.docker.com/engine/security/protect-access/

## What this project does NOT defend against

- A malicious **upstream Debian source package** running arbitrary
  code in `debian/rules`.  The build container limits the blast
  radius (no host-wide root) but cannot stop the package from sending
  its build artifacts to a controlled location, mining crypto, etc.
  Mitigation: snapshot pinning (`[Snapshot] Enabled = true`) so a
  malicious upload landing on the live mirror after your last cache
  refresh does not silently get pulled into the next build.
- A **compromised mirror** if you set `[Security] Disabled = true`.
  Don't do that outside offline test fixtures.
- **Side-channel leakage** between the host and the container (CPU
  timing attacks etc.).  Out of scope.

## See also

- `STA-01` / `SEC-01` (TODO) — InRelease GPG verification (landed)
- `SEC-04` (TODO) — random live-user name on built ISOs (landed for
  live, deferred for installed system → COMP-01)
- `SEC-05` (TODO) — opt-in build-dep review gate before container
  runs `apt-get install` (open)
