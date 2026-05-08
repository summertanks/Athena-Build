ARG RELEASE=bookworm
FROM debian:${RELEASE}-slim
ARG DEBIAN_FRONTEND=noninteractive

# Toolchain layer.  Uses the base image's stock sources.list (live mirror) —
# this only runs at image-build time to install our static build toolchain.
# Per-package builds inside the container rewrite /etc/apt/sources.list to
# the configured mirrors before any apt-get update (see BuildContainer.build).
RUN apt-get update && apt-get install -y \
        net-tools iputils-ping util-linux \
        bc bison build-essential cpio flex  libncurses-dev libssl-dev ca-certificates less sudo keyboard-configuration \
        vim gettext debhelper texinfo groff media-types libselinux1-dev gperf libgmp3-dev procps\
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
