### Features
- Building Debian Distribution from source
- Support for Patching at Source, Pre-Install and Post-Install
- Modular Installation System
- Give process transperancy and readability

# Athena-Build
![example workflow]([https://github.com/summertanks/stars/badge.svg](https://github-profile-summary-cards.vercel.app/api/cards/profile-details?username={summertanks}))


## Introduction
Athena Build system is(trying to be) a (mostly) hands off 'build system' to build and install custom Debian Linux distribution. The distinction is that  sources are build rather than using the prepared packages. It is aimed to be the more transperent and flexiable version of debbootstrap and live-build.

## Background


### Linux

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
