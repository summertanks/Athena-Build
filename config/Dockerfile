ARG RELEASE=bookworm
ARG SNAPSHOT_BASEURL=https://snapshot.debian.org/archive
ARG ARCHIVE_KEY=debian
ARG SNAPSHOT_TS=

FROM debian:${RELEASE}-slim
ARG DEBIAN_FRONTEND=noninteractive
# Re-declare the snapshot args inside the build stage — ARGs before FROM
# scope only to the FROM line; consumers (RUN blocks below) need them
# re-declared here.
ARG RELEASE
ARG SNAPSHOT_BASEURL
ARG ARCHIVE_KEY
ARG SNAPSHOT_TS

# CONF-15: pin the toolchain layer to OUR snapshot, not the live Debian
# mirror.  Pre-CONF-15 this RUN used the base image's stock sources.list
# (= live deb.debian.org); when the live mirror advanced past our
# `[Snapshot] Timestamp` pin, pre-installed library packages ended up
# NEWER than what our snapshot/repo ship.  dpkg-shlibdeps inside the
# build container then read the newer .shlibs and embedded
# minimum-version constraints (e.g. wpasupplicant-udeb →
# libcrypto3-udeb (>= 3.0.20)) pointing at versions we don't carry —
# the 2026-05-23 false-bind.
#
# The fix is in three steps below:
#   (a) wipe the base image's apt sources (both legacy sources.list and
#       deb822 sources.list.d/*.{sources,list}) — bookworm-slim's default
#       deb822 file otherwise keeps feeding apt live URLs even with our
#       sources.list rewrite.
#   (b) write a one-line snapshot.debian.org source pinning EVERY apt
#       transaction in the image to OUR snapshot TS, plus an apt
#       preferences file at Pin-Priority 1001 so version downgrades
#       (when our snapshot is older than the base image's pre-installed
#       library set) are forced rather than refused.
#   (c) `dist-upgrade --allow-downgrades` to align the base image's
#       pre-installed library set with the snapshot view BEFORE
#       installing the toolchain.  Without this, pre-installed
#       libraries from the live base image stay newer than snapshot.
#
# Once (a)-(c) run, `apt-get install -y <toolchain>` resolves entirely
# against the snapshot.  Pre-CONF-15, this `apt-get install` was the
# contamination point.  Post-CONF-15, the per-build dist-upgrade in
# BuildContainer.build (line 689) becomes a no-op and has been removed.
#
# Library `-dev` packages still MUST NOT be pre-installed here — they
# come from each source's Build-Depends at per-build time, against the
# snapshot mirrors written by _write_snapshot_sources_cmd (which include
# the full mirror set: main + security + updates).
RUN set -eux; \
    rm -f /etc/apt/sources.list \
          /etc/apt/sources.list.d/*.sources \
          /etc/apt/sources.list.d/*.list; \
    if [ -z "${SNAPSHOT_TS}" ]; then \
        echo "ERROR: SNAPSHOT_TS build-arg is empty — pass it from BuildContainer.__init__" >&2; \
        exit 1; \
    fi; \
    echo "deb [check-valid-until=no] ${SNAPSHOT_BASEURL}/${ARCHIVE_KEY}/${SNAPSHOT_TS} ${RELEASE} main" \
        > /etc/apt/sources.list; \
    printf 'Package: *\nPin: origin snapshot.debian.org\nPin-Priority: 1001\n' \
        > /etc/apt/preferences.d/athena-snapshot; \
    apt-get update; \
    apt-get -y -o Acquire::Retries=5 --allow-downgrades dist-upgrade; \
    apt-get install -y \
        net-tools iputils-ping util-linux \
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
