#!/bin/bash

# Defining Colors
IWhite='\033[0;97m'       # White
Color_Off='\033[0m'       # Text Reset

DIR_TMP="tmp"
DIR_PKG="packages"
DIR_REPO="repo"
DIR_IMAGE="image"
DIR_CACHE="cache"

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

echo -e "${IWhite}Starting Source Build System for Athena Linux...${Color_Off}"

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
echo Using `/usr/bin/sh  --version | head -n1`

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

# Checking build system
awk -F= '/PRETTY_NAME/ { print "Current Build System " $2 }' /etc/os-release
BUILD_ID=$(awk -F= '/ID_LIKE/ { print $2 }' /etc/os-release)

echo Build Flavour $BUILD_ID
if [[ $BUILD_ID != "debian" ]]; then
	echo "E: Not using Debian to build, not tested, will likely fail"
	exit 1
fi

# Load basic config
if ! [ -f $CONFIG_FILE ]; then
	echo "E: Not found Config file" $CONFIG_FILE > /dev/stderr
	exit 1
else
	echo "Using config file" $CONFIG_FILE
	source $CONFIG_FILE
fi

echo -e "${IWhite}Building for ..."
echo -e "\t Arch\t\t\t" $ARCH
echo -e "\t Parent Distribution\t" $BASECODENAME $BASEVERSION
echo -e "\t Build Distribution\t" $CODENAME $VERSION
echo -e "${Color_Off}"

echo "Building Cache..."
BASE_URL=$MIRROR_URL/$BASEID/dists/$BASECODENAME
BASE_FILENAME="$MIRROR_URL"_"$BASEID"_dist_"$BASECODENAME"

# always download the InRelease file unless in offline mode
# TODO: configure offline more 
wget $BASE_URL/InRelease -q --show-progress -O $DIR_CACHE/"$BASE_FILENAME"_InRelease
RELEASE_FILE=$DIR_CACHE/"$BASE_FILENAME"_InRelease

CACHE_SOURCE=("${BASE_URL}/main/binary-${ARCH}/Packages.gz" \
	"${BASE_URL}/main/i18n/Translation-en.bz2" \
	"${BASE_URL}/main/source/Sources.gz" )

CACHE_FILENAME=("main/binary-${ARCH}/Packages" \
        "main/i18n/Translation-en" \
        "main/source/Sources" )
CACHE_DESTINATION=("_main_binary-${ARCH}_Packages" "_main_i18n_Translation-en" "_main_source_Sources" )

# TODO: option of force rebuild cache on every instance

for i in $(seq 0 $((${#CACHE_FILENAME[@]} - 1)))
do
	HASH_EXPECTED=$(awk -v name="${CACHE_FILENAME[i]}" '$3 == name { if(length($1) == 32) { print $1 } }' "$RELEASE_FILE")	
	if [ -f "$DIR_CACHE/$BASE_FILENAME${CACHE_DESTINATION[i]}" ]; then
        	HASH_PRESENT=$(md5sum "$DIR_CACHE/$BASE_FILENAME${CACHE_DESTINATION[i]}" | awk '{print $1}')
	else
        	HASH_PRESENT=''
	fi
	
	if ! [ "$HASH_PRESENT" == "$HASH_EXPECTED" ]; then
		EXTENSION=${CACHE_SOURCE[i]##*.}
		DESTINATION=$DIR_CACHE/$BASE_FILENAME${CACHE_DESTINATION[i]}
		if [ "$EXTENSION" == "gz" ]; then
			wget -O - ${CACHE_SOURCE[i]} -q --show-progress | gunzip -c > $DESTINATION
		elif [ "$EXTENSION" == "bz2" ]; then
			wget -O - ${CACHE_SOURCE[i]} -q --show-progress | bzip2 -d > $DESTINATION
		else
			wget ${CACHE_SOURCE[i]} -q --show-progress -O $DESTINATION
		fi
	fi
done

PACKAGE_LIST_FILE=$DIR_CACHE/$BASE_FILENAME${CACHE_DESTINATION[0]}
SOURCE_LIST_FILE=$DIR_CACHE/$BASE_FILENAME${CACHE_DESTINATION[2]}

# Load package list
if ! [ -f $PKG_REQ_FILE ]; then
        echo "E: Not found packagelist file" $PACKAGE_FILE > /dev/stderr
        exit 1
fi

python3 scripts/dep_parser.py \
	--depends-file=$PACKAGE_LIST_FILE \
	--pkg-list=$PKG_REQ_FILE \
       	--source-file=$SOURCE_LIST_FILE \
	--download-dir=Download \
       	--output-file=$DIR_CACHE/output.list





