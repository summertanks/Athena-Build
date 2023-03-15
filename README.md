## Features
- Building Debian Distribution from source
- Support for Patching at Source, Pre-Install and Post-Install
- Modular Installation System
- Give process transparency and readability

# Athena-Build

## Introduction
Athena Build system is(trying to be) a (mostly) hands off 'build system' to build and install custom Debian Linux distribution. The distinction is that  sources are built rather than using the prepared packages. It is aimed to be the more transparent and flexible version of debbootstrap and live-build.

The genesis of this project came from the conversation - while the Linux ecosystem as part of the FOSS world, but as the platform matured, can we really build the solution from source? As the build systems are becoming more complex, sparsely documented and obfuscated (personal opinion).

## FYI
 - This will be a maturing solution and not immediately suitable to building production system. Currently, best used for tinkering.
 - Can this be faster, YES. Is it worth making it faster (e.g. shifting to C, trading space with time, etc.) NO
 - It is NOT currently (or ever may be) supported by any of Debian Linux Houses (e.g. debian, ubuntu, etc)
 - Does it have Bugs - YES / MANY, please reach out to me and lets fix what you find.

### Linux
The first question always is - What is Linux?  Linus Torvalds while studying at the University of Helsinki, wrote (for multiple reasons that I am not getting into here) a clone of UNIX operating system called 'Minix' and was supposed to be compatible to ***System V***. 

Accordingly, We ended up with the Ver 0.1 of the **Linux Kernel**. Unfortunately, the Kernel had no application ecosystem to run as remained as such an essential cog in a non-existing ecosystem. Then came along Richard Stallman and GNU and gave it purpose. They brought the application stack that gave Linux Kernel purpose, and hence was born the Linux Distribution, or more colloquially just called **Linux Distribution**. The conversation of distinction between 'Linux Distribution' and 'Linux OS' is a petridish for violence amongst geeks, but for the purpose of this project lets assert debian is a 'Linux Distribution' and stay away from the phrase OS as puch as possible.

The first Linux distribution, called "Softlanding Linux System" (SLS), was released by 1992. and within the next three years we saw the advent of Slackware, Red Hat and Debian. The rest as they say is history.

PS: Red Hat vs Debian - Red Hat was founded with the goal of creating a commercial distribution of Linux that could be sold and supported. Red Hat's approach was to take the existing Linux codebase, add value in the form of support, services, and tools, and sell it to enterprise customers. On the other hand, Debian was founded  with the goal of creating a community-driven Linux distribution that was completely free, open-source and built from scratch, with a focus on stability, security, and ease of use. 

We are currently only looking at Debian & Debian based distributions

### Linux Distribution
A Linux distribution is a complete set of packages included (but not limited to) the Linux kernel, system utilities, applications, and software libraries, along with a package management system and other tools for managing and configuring the system. A Linux distribution is typically designed and packaged by a community or organization, and is intended to provide a complete, ready-to-use OS that can be installed and configured on a variety of hardware platforms.

### Packages and Package Manager
In a Linux distribution, a package is akin to SKU (Stock Kepping Unit) of software that can be installed and managed by the operating system's package manager. A package may include one or more applications, libraries, along with configuration files required to run the software on the system. 

The packages may intrensicly also define other packages as dependencies and it is usually the package manager which checks to ensure that all required dependencies are present and installs any missing dependencies as needed. THe package manager abstracts away the complexity of installing, managing, updating, and removing software packages.

Packages in a Linux distribution may be maintained by the distribution's own developers or by third-party contributors. In this context Debian. They (Debian) identify application, wrap the application's build system to produce the installables as a package construct, i.e. deb - debian package file, test it, patch it, abd publish it in a repository.

Dpkg is a low-level package manager that is used by the Debian and Ubuntu distributions. It is responsible for managing the installation and removal of individual software packages on a system. Dpkg works by maintaining a database of installed packages and their dependencies, and it uses this information to ensure that all required packages are present and properly configured when a package is installed.

Apt, or Advanced Package Tool, is a higher-level package manager that builds on top of dpkg. It is used to manage the entire software repository for a distribution, including all official packages and any third-party repositories that have been added. Apt provides a more user-friendly interface than dpkg, with features such as automatic dependency resolution, automatic package updating, and easy installation of packages from remote repositories.

### Repositories

Debian's package repositories are organized into several official repositories, including "main", "contrib", and "non-free", as well as a "backports" repository for newer software versions. The "main" repository contains packages that are completely free and open-source, while the "contrib" and "non-free" repositories contain packages that may have non-free or proprietary components. 



### RHEL/Debian/Ubuntu

### Stiched together


## Building Image

### Intro

...

### Source Code Patching
Using quilt to create patches. can use standard diff also. Mostly templates are nice in quilt. While we would have prefered to use quilt natively for applying patch too but that requires the patch file being in the tarball, else a lot of 'fuzz' errors. So to apply patching still using standard 'patch'.

Creating patch file involves > expand source > define patch file in quilt > make changes > refresh quilt > edit header (template, optional) > save patch
```
dpkg-source -x package_version.dsc
cd package-version
quilt new xxxx-description.patch
quilt header -e --dep3 
...
# make the changes, use quilt edit file
# the -e edits the header in $EDITOR
...
quilt refresh xxxx-description.patch
cp debian/patch/xxxx-description.patch <patch dir>/package/version/xxxx-description.patch
```

if you want to apply the same on expanded source package
```
patch -p1 < <patch dir>/package/version/xxxx-description.patch
```

The patch file numbering is four digit, preferably start from 9001 for simplicity sake
The patch folder will have folder for each source package 'name' and sub folders for respective versions. each patch is version specific and saved in that version folder.

## Misc

### Installing Docker

Installing docker manually, the distribution repo packages are old.
Everything under superuser
```commandline
apt-get remove docker docker-engine docker.io containerd runc
apt-get install ca-certificates curl gnupg lsb-release
mkdir -m 0755 -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" |  tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker $USER
```
