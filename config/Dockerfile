FROM debian:11.6-slim

RUN apt-get update
RUN apt-get install -y \
        bc bison build-essential cpio flex  libncurses-dev libssl-dev ca-certificates less \
        vim gettext debhelper texinfo groff media-types libselinux1-dev gperf libgmp3-dev \
        python3-minimal python3 dh-python python3-distutils python3-debian python3-gpg \
        apt-file autoconf autotools-dev automake gettext debhelper diffstat zip libtool po-debconf
RUN useradd athena
RUN mkdir /home/athena
RUN chown athena:athena -R /home/athena