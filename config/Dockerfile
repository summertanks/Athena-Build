ARG RELEASE=bookworm
FROM debian:${RELEASE}-slim
ARG DEBIAN_FRONTEND=noninteractive

# Toolchain layer.  Uses the base image's stock sources.list (live mirror) —
# this only runs at image-build time to install our static build toolchain.
# Per-package builds inside the container rewrite /etc/apt/sources.list to
# the configured mirrors before any apt-get update (see BuildContainer.build).
#
# CAREFUL — library `-dev` packages MUST NOT be pre-installed here.  Their
# version is baked at image-build time from the LIVE mirror, but per-build
# `apt-get install` against our snapshot won't downgrade an already-installed
# pkg.  Result: dpkg-shlibdeps reads the live-mirror version's symbols file
# and embeds a minimum-version constraint pointing at a version newer than
# anything in our snapshot/repo — every downstream consumer becomes
# unsatisfied.  Drove the 2026-05-23 wpa→libcrypto3-udeb (>= 3.0.20)
# false-bind when the live mirror advanced past our pinned snapshot.
#
# Keep ONLY language-agnostic toolchain bits here (build-essential, autoconf,
# python tooling).  Library -dev packages come from each source's
# Build-Depends at per-build time, against the snapshot's apt sources.
RUN apt-get update && apt-get install -y \
        net-tools iputils-ping util-linux \
        bc bison build-essential cpio flex ca-certificates less sudo keyboard-configuration \
        vim gettext debhelper texinfo groff media-types gperf procps \
        python3-minimal python3 dh-python python3-distutils python3-debian python3-gpg \
        apt-file autoconf autotools-dev automake gettext debhelper diffstat zip libtool po-debconf
# Enable IPv6
RUN echo 'net.ipv6.conf.all.disable_ipv6=0' >> /etc/sysctl.conf
RUN echo 'net.ipv6.conf.default.disable_ipv6=0' >> /etc/sysctl.conf
RUN echo 'net.ipv6.conf.lo.disable_ipv6=0' >> /etc/sysctl.conf

RUN useradd -G sudo -ms /bin/bash athena
# Passwordless sudo for the build user.  dpkg-buildpackage and the
# Debian build-helper machinery routinely call sudo for build-dep
# installs, dpkg invocations, etc.; a TTY-prompted password would
# break the unattended per-package build loop.
#
# This makes the container a privileged sandbox, NOT a security
# boundary.  See docs/security.md for the threat model — the short
# version: never expose the docker daemon driving this image to a
# network you don't fully control (BuildContainer._guard_docker_server
# enforces the loopback / TLS check at runtime).
RUN echo 'athena ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers
USER athena
