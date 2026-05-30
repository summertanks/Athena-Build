ARG RELEASE=bookworm
ARG SNAPSHOT_BASEURL=https://snapshot.debian.org/archive
ARG ARCHIVE_NAME=debian
ARG SECURITY_ARCHIVE_NAME=debian-security
ARG SNAPSHOT_TS=

FROM debian:${RELEASE}-slim
ARG DEBIAN_FRONTEND=noninteractive
# Re-declare the snapshot args inside the build stage — ARGs before FROM
# scope only to the FROM line; consumers (RUN blocks below) need them
# re-declared here.
ARG RELEASE
ARG SNAPSHOT_BASEURL
ARG ARCHIVE_NAME
ARG SECURITY_ARCHIVE_NAME
ARG SNAPSHOT_TS

# CONF-15 (revised — install-then-realign): pin the toolchain layer to
# OUR snapshot, but in two steps so apt's `install` step uses the live
# mirror it already knows packages exist on, then the snapshot view
# realigns the installed set.
#
# Why two steps?  An earlier single-step attempt (rewrite sources to
# snapshot FIRST, then `apt-get install -y <toolchain>`) failed at image
# build with ~15 packages reported "Unable to locate" — debhelper,
# python3, gettext, autoconf, automake, libtool, po-debconf, etc.  The
# same TS works for per-build `apt-get install` of build-deps inside the
# resulting image; the difference is that per-build container starts with
# the toolchain already installed (so apt-get install <build-dep> only
# needs to find the build-deps, not the toolchain).  At image-build time
# the toolchain is the FIRST resolve against snapshot, and something
# about the index at snapshot.debian.org makes those names not surface
# despite `apt-get update` succeeding.  Diagnosed but not root-caused;
# the install-then-realign shape sidesteps it entirely.
#
# Step (a) — apt-get install <toolchain> against the base image's
# default sources.list.  This is the LIVE mirror (deb.debian.org), which
# we know carries every toolchain package by name.  Same shape the
# pre-CONF-15 Dockerfile used; the contamination problem CONF-15 was
# fixing happens at PER-BUILD time, not here.
#
# Step (b) — wipe sources, write snapshot sources (main + -updates +
# -security at the SAME TS), and apt-get update.  After this point the
# container's apt view is at snapshot.
#
# Step (c) — `apt-get -y --allow-downgrades dist-upgrade` realigns the
# installed package set with the snapshot view.  Pre-installed library
# packages that are newer than snapshot get downgraded; missing ones
# get pulled.  Pin-Priority 1001 on snapshot.debian.org origin forces
# the downgrade direction (default priority 500 keeps installed-newer).
#
# After (a)+(b)+(c), the image's installed set is at snapshot.  Per-build
# inside this image starts already-aligned; no per-build dist-upgrade
# needed.
#
# Library `-dev` packages still MUST NOT be installed in step (a) —
# their .shlibs would be embedded in fresh builds at the LIVE mirror
# version even after the realign.  Step (a) keeps to language-agnostic
# toolchain (build-essential, python, autoconf, etc.); -dev packages
# come from each source's Build-Depends at per-build time.
#
# SNAPSHOT_TS must be passed by the build-args; an empty SNAPSHOT_TS
# fails the build LOUDLY rather than silently leaving the image at the
# live mirror (which would re-introduce the contamination problem).

# (a) toolchain install against the live mirror.
RUN set -eux; \
    apt-get update; \
    apt-get install -y \
        net-tools iputils-ping util-linux \
        bc bison build-essential cpio flex ca-certificates less sudo keyboard-configuration \
        vim gettext debhelper texinfo groff media-types gperf procps \
        python3-minimal python3 dh-python python3-distutils python3-debian python3-gpg \
        apt-file autoconf autotools-dev automake gettext debhelper diffstat zip libtool po-debconf; \
    apt-get clean

# (b)+(c) switch sources to OUR snapshot and realign the installed set.
RUN set -eux; \
    if [ -z "${SNAPSHOT_TS}" ]; then \
        echo "ERROR: SNAPSHOT_TS build-arg is empty — pass it from BuildContainer.__init__" >&2; \
        exit 1; \
    fi; \
    rm -f /etc/apt/sources.list \
          /etc/apt/sources.list.d/*.sources \
          /etc/apt/sources.list.d/*.list; \
    { \
        echo "deb [check-valid-until=no] ${SNAPSHOT_BASEURL}/${ARCHIVE_NAME}/${SNAPSHOT_TS} ${RELEASE} main"; \
        echo "deb [check-valid-until=no] ${SNAPSHOT_BASEURL}/${ARCHIVE_NAME}/${SNAPSHOT_TS} ${RELEASE}-updates main"; \
        echo "deb [check-valid-until=no] ${SNAPSHOT_BASEURL}/${SECURITY_ARCHIVE_NAME}/${SNAPSHOT_TS} ${RELEASE}-security main"; \
    } > /etc/apt/sources.list; \
    printf 'Package: *\nPin: origin snapshot.debian.org\nPin-Priority: 1001\n' \
        > /etc/apt/preferences.d/athena-snapshot; \
    apt-get update; \
    apt-get -y -o Acquire::Retries=5 --allow-downgrades dist-upgrade; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

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
