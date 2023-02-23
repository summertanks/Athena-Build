FROM debian:11.6-slim

RUN apt-get update
RUN apt-get install -y \
        bc bison build-essential cpio flex libelf-dev libncurses-dev libssl-dev \
        vim gettext debhelper texinfo groff libattr1-dev libacl1-dev libselinux1-dev gperf libgmp3-dev
RUN useradd athena
RUN mkdir /home/athena
RUN chown athena:athena -R /home/athena