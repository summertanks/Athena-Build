#!/bin/bash

# Defining Colors
IWhite='\033[0;97m'       # White
Color_Off='\033[0m'       # Text Reset

DIR_TMP="tmp"
DIR_PKG="packages"
DIR_REPO="repo"
DIR_IMAGE="image"
DIR_CACHE="cache"
DIR_DOWNLOAD="download"
DIR_LOG="log"
DIR_SOURCE="source"

VERBOSE="0"
CONFIG_FILE="config/build.conf"
PKG_REQ_FILE="config/pkg.list"

usage() { \
        echo -e "Usage:"; \
        echo -e "\t -c|--config-file <filename> : Config file giving basic system config"; \
        echo -e "\t -p|--pkg-list <filename> : File listing all packages included in distro"; \
        echo -e "\t -v|--verbose : Set verbosity high"; \
}

PWD=$(pwd)

set -e
# enable common error handling options
set -o errexit
set -o nounset
set -o pipefail

echo -e "${IWhite}Athena Linux Build Check${Color_Off}"

# Parsing args
ARGS=$(getopt -n Athena -o 'hc:p:v' --long 'help,config-file:,pkg-list,verbose' -- "$@") || exit
eval "set -- $ARGS"

while true; do
	case $1 in
		(-v|--verbose)
			((VERBOSE=1));
			shift;;
		(-c|--config-file)
			CONFIG_FILE=$2;
			shift 2;;
		(-p|--pkg-list)
			PACKAGE_FILE=$2;
			shift 2;;
		(-h|--help)
			usage;
			exit;;
		(--)
			shift;
			if [ -n "$*" ]; then
				usage; exit 1;
			fi
			break;;
		(*)
			usage;
			exit 1;;
	esac
done

# check user state
if [[ "$(id -u)" ==  0 ]]; then
	echo "W: running as sudo"
fi

# Bash version
echo Using `/usr/bin/bash  --version | head -n1`

# gunzip version
echo Using `/usr/bin/gunzip  --version | head -n1`

# python version
echo Using `/usr/bin/python3  --version | head -n1`

# checking wget
if [ -x /usr/bin/wget ]; then
        echo Using `/usr/bin/wget --version | head -n1`
else
        echo "E: wget not found, do we want to be in a world without wget" > /dev/stderr
        exit 1
fi

# Checking awk
if [ -x /usr/bin/awk ]; then
	echo Using `/usr/bin/awk --version | head -n1`
else
	echo "E: awk not found, build script will not work" > /dev/stderr
	exit 1
fi

# Checking build directories
echo "Checking Build Directories (everything is relative to the script path)"
mkdir -p $PWD/$DIR_TMP
mkdir -p $PWD/$DIR_PKG
mkdir -p $PWD/$DIR_REPO
mkdir -p $PWD/$DIR_IMAGE
mkdir -p $PWD/$DIR_CACHE
mkdir -p $PWD/$DIR_DOWNLOAD
mkdir -p $PWD/$DIR_SOURCE
mkdir -p $PWD/$DIR_LOG/build

# Checking build system
awk -F= '/PRETTY_NAME/ { print "Current Build System " $2 }' /etc/os-release
BUILD_ID=$(awk -F= '/^ID/ { print $2 }' /etc/os-release)

echo Build Flavour $BUILD_ID
if [[ $BUILD_ID != "debian" ]]; then
	echo "E: Not using Debian to build, not tested, will likely fail"
fi

# Load basic config
if ! [ -f $CONFIG_FILE ]; then
	echo "E: Not found Config file" $CONFIG_FILE > /dev/stderr
	exit 1
else
	echo "Using config file" $CONFIG_FILE
fi


python3 scripts/build.py --pkg-list=$PKG_REQ_FILE --working-dir=$PWD --config-file=$CONFIG_FILE





