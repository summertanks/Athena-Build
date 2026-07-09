ARG RELEASE=bookworm
ARG SNAPSHOT_BASEURL=https://snapshot.debian.org/archive
ARG ARCHIVE_NAME=debian
ARG SECURITY_ARCHIVE_NAME=debian-security
ARG SNAPSHOT_TS=

# The base is OUR OWN bootstrap, not Docker Hub's: BuildContainer runs
# `mmdebstrap --variant=buildd` against the pinned snapshot (see
# scripts/base_rootfs.py) and stages the resulting tar next to this
# Dockerfile in the build context.  Every byte of the image therefore
# comes from the snapshot at SNAPSHOT_TS.
#
# Why not debian:<release>-slim (the pre-2026-07 base)?  Three classes of
# trouble, all hit in practice:
#   1. slim images are assembled with doc/locale stripping baked in —
#      base-layer packages exist in dpkg's database but their doc files
#      are absent on disk (libpsl's publicsuffix test fixture,
#      init-system-helpers' dpkg -L walk).
#   2. docker-specific dpkg config shipped in the base broke exact-stderr
#      package tests (dpkg src/at/chdir.at) until removed.
#   3. the FROM line pulled whatever Docker Hub served on pull day —
#      the one unpinned, third-party layer in an otherwise
#      snapshot-pinned build environment.
# The buildd variant is what sbuild chroots are made of: buildd parity
# by construction, and the install-then-realign dance the slim base
# required (install from live mirror, rewrite sources, dist-upgrade
# back to snapshot) is gone — the base IS at snapshot from birth.
FROM scratch
ADD base-rootfs.tar /
ARG DEBIAN_FRONTEND=noninteractive
# Re-declare the snapshot args inside the build stage — ARGs before FROM
# scope only to the FROM line; consumers (RUN blocks below) need them
# re-declared here.
ARG RELEASE
ARG SNAPSHOT_BASEURL
ARG ARCHIVE_NAME
ARG SECURITY_ARCHIVE_NAME
ARG SNAPSHOT_TS

# Snapshot sources + pin, written explicitly even though mmdebstrap
# bootstrapped from the same three pockets: the image's apt view must
# not depend on what the bootstrap tool happened to leave behind, and
# the Pin-Priority 1001 forces the downgrade direction for any
# later realign (mirrors _write_snapshot_sources_cmd's per-build shape).
# An empty SNAPSHOT_TS fails the build LOUDLY rather than silently
# leaving the image against an unpinned view.
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
        > /etc/apt/preferences.d/athena-snapshot

# Toolchain on top of the buildd base — resolved AGAINST THE SNAPSHOT
# (the buildd variant already carries build-essential; the list below is
# the language-agnostic extras).  Library `-dev` packages still MUST NOT
# be installed here — their .shlibs would be embedded in fresh builds;
# -dev packages come from each source's Build-Depends at per-build time.
#
# Error-Mode=any: a failed index fetch must FAIL the update, not warn —
# apt's default exit-0-with-warnings left empty lists and a wall of
# "Unable to locate package" at install (the certificate-less base,
# 2026-07-08; also the historic CONF-15 "unable to locate" anomaly).
RUN set -eux; \
    apt-get update -o APT::Update::Error-Mode=any; \
    apt-get install -y -o Acquire::Retries=5 \
        net-tools iputils-ping util-linux mount \
        bc bison build-essential cpio flex ca-certificates less sudo keyboard-configuration \
        vim gettext debhelper texinfo groff media-types gperf procps \
        python3-minimal python3 dh-python python3-distutils python3-debian python3-gpg \
        apt-file autoconf autotools-dev automake gettext debhelper diffstat zip libtool po-debconf; \
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
