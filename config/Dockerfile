FROM debian:11.6-slim
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update
RUN apt-get install -y  \
        net-tools iputils-ping util-linux \
        bc bison build-essential cpio flex  libncurses-dev libssl-dev ca-certificates less sudo keyboard-configuration \
        vim gettext debhelper texinfo groff media-types libselinux1-dev gperf libgmp3-dev \
        python3-minimal python3 dh-python python3-distutils python3-debian python3-gpg \
        apt-file autoconf autotools-dev automake gettext debhelper diffstat zip libtool po-debconf
# Enable IPv6
RUN echo 'net.ipv6.conf.all.disable_ipv6=0' >> /etc/sysctl.conf
RUN echo 'net.ipv6.conf.default.disable_ipv6=0' >> /etc/sysctl.conf
RUN echo 'net.ipv6.conf.lo.disable_ipv6=0' >> /etc/sysctl.conf


RUN useradd -G sudo -ms /bin/bash athena
RUN echo 'athena ALL=(ALL) NOPASSWD:ALL' >>  /etc/sudoers
