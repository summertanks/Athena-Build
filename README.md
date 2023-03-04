### Features
- Building Debian Distribution from source
- Support for Patching at Source, Pre-Install and Post-Install
- Modular Installation System
- Give process transperancy and readability

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

Accordingly, We ended up with the Ver 0.1 of the **Linux Kernel**. Unfortunately, the Kernel had no application ecosystem to run as remained as such an essential cog in a non-existing ecosystem. Then came along Richard Stallman and GNU and gave it purpose. They brough the application stack that gave Linux Kernel purpose, and hence was born the Linux based OS, or more colloquially just called **Linux OS**. The conversation of distinction between 'Linux based OS' and 'Linux OS' is a petridish for violence amougst geeks, but for the purpose of this project lets assert debian == 'Linux based OS'

PS: System V is a version of the Unix operating system that was developed by AT&T in the late 1970s and early 1980s. It was one of the most widely used versions of Unix and included many important features such as the System V init system. 



### 'Linux OS'

### Packages

### Repositories
https://help.ubuntu.com/community/Repositories

Main
The main component contains applications that are free software, can be freely redistributed and are fully supported by the Ubuntu team. This includes the most popular and most reliable open-source applications available, many of which are included by default when you install Ubuntu. Software in main includes a hand-selected list of applications that the Ubuntu developers, community and users feel are most important, and that the Ubuntu security and distribution team are willing to support. When you install software from the main component, you are assured that the software will come with security updates and that commercial technical support is available from Canonical.

Restricted
Our commitment is to only promote free software – or software available under a free licence. However, we make exceptions for a small set of tools and drivers that make it possible to install Ubuntu and its free applications on everyday hardware. These proprietary drivers are kept in the restricted component. Please note that it may not be possible to provide complete support for this software because we are unable to fix the software ourselves - we can only forward problem reports to the actual authors. Some software from restricted will be installed on Ubuntu CDs but is clearly separated to ensure that it is easy to remove. We will only use non-open-source software when there is no other way to install Ubuntu. The Ubuntu team works with vendors to accelerate the open-sourcing of their software to ensure that as much software as possible is available under a free licence.

Universe
The universe component is a snapshot of the free, open-source, and Linux world. It houses almost every piece of open-source software, all built from a range of public sources. Canonical does not provide a guarantee of regular security updates for software in the universe component, but will provide these where they are made available by the community. Users should understand the risk inherent in using these packages. Popular or well supported pieces of software will move from universe into main if they are backed by maintainers willing to meet the standards set by the Ubuntu team.

Multiverse
The multiverse component contains software that is not free, which means the licensing requirements of this software do not meet the Ubuntu main component licence policy. The onus is on you to verify your rights to use this software and comply with the licensing terms of the copyright holder. This software is not supported and usually cannot be fixed or updated. Use it at your own risk.

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
...
# make the changes
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
