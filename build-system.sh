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

BUILD_DIR=$(pwd)

# enable common error handling options
set -o errexit
set -o nounset
set -o pipefail

echo -e "Athena Linux Build System Check..."

# Parsing args
ARGS=$(getopt -n Athena -o 'hc:p:v' --long 'help,config-file:,pkg-list:,verbose' -- "$@") || exit
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
			PKG_REQ_FILE=$2;
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

# Check sudo access — build_bootable and build_iso run dpkg and mksquashfs
# as root via sudo.  -l lists the user's privileges; -n skips the password
# prompt so this is non-interactive.  A warning (not a fatal error) is issued
# here because the core build pipeline (cache/dependency/source) does not
# require sudo — only the chroot and ISO stages do.
if sudo -l -n 2>/dev/null | grep -q '(ALL'; then
    echo "Sudo access: OK ($(whoami) has sudo privileges)"
elif id -nG "$(whoami)" | grep -qw sudo; then
    echo "Sudo access: OK ($(whoami) is in sudo group — password will be required)"
else
    echo "E: $(whoami) does not have sudo access — build_bootable and build_iso require sudo" >&2
    exit 1
fi

# Bash version
echo Using `/usr/bin/bash  --version | head -n1`

# gunzip version
echo Using `/usr/bin/gunzip  --version | head -n1`

# python version
echo Using `/usr/bin/python3  --version | head -n1`

# checking docker
if [ -x "$(which docker 2>/dev/null)" ]; then
    echo Using `docker --version`
else
    echo "E: docker not found, build system requires docker" >&2
    exit 1
fi

# checking wget
if [ -x /usr/bin/wget ]; then
        echo Using `/usr/bin/wget --version | head -n1`
else
        echo "E: wget not found, do we want to be in a world without wget" > /dev/stderr
        exit 1
fi

# Checking awk
AWK_PATH=$(which awk 2>/dev/null)

if [ -x "$AWK_PATH" ]; then
    REAL_AWK=$(readlink -f "$AWK_PATH")
    PACKAGE=$(dpkg -S "$REAL_AWK" 2>/dev/null | cut -d: -f1)

    case "$PACKAGE" in
        gawk)
            AWK_VERSION=$("$AWK_PATH" --version | head -n1)
            ;;
        mawk)
            AWK_VERSION=$("$AWK_PATH" -W version 2>&1 | head -n1)
            ;;
        original-awk)
            AWK_VERSION=$("$AWK_PATH" 2>&1 | grep -i version | head -n1)
            ;;
        *)
            AWK_VERSION=$("$AWK_PATH" --version 2>/dev/null | head -n1 || echo "version unknown")
            ;;
    esac
    echo "Using awk: $PACKAGE — $AWK_VERSION"
else
    echo "E: awk not found, build script will not work" >&2
    exit 1
fi

# Checking ISO build tools (required for build_iso command only)
echo "Checking ISO build tools..."
ISO_TOOLS_OK=1

if [ -x "$(which mksquashfs 2>/dev/null)" ]; then
    echo Using "mksquashfs $(mksquashfs -version 2>&1 | head -n1)"
else
    echo "W: mksquashfs not found (install squashfs-tools) — build_iso will not work"
    ISO_TOOLS_OK=0
fi

if [ -x "$(which grub-mkrescue 2>/dev/null)" ]; then
    echo Using "grub-mkrescue $(grub-mkrescue --version 2>/dev/null | head -n1)"
else
    echo "W: grub-mkrescue not found (install grub-pc-bin grub-efi-amd64-bin) — build_iso will not work"
    ISO_TOOLS_OK=0
fi

if [ -x "$(which xorriso 2>/dev/null)" ]; then
    echo Using "xorriso $(xorriso --version 2>&1 | head -n1)"
else
    echo "W: xorriso not found (install xorriso) — build_iso will not work"
    ISO_TOOLS_OK=0
fi

if [ -x "$(which mformat 2>/dev/null)" ]; then
    echo Using "mformat $(mformat --version 2>&1 | head -n1)"
else
    echo "W: mformat not found (install mtools) — grub-mkrescue will fail"
    ISO_TOOLS_OK=0
fi

if [[ $ISO_TOOLS_OK -eq 0 ]]; then
    echo "E: one or more ISO build tools missing — run: sudo apt install squashfs-tools grub-pc-bin grub-efi-amd64-bin xorriso mtools" >&2
    exit 1
else
    echo "All ISO build tools found."
fi

# Checking build directories
echo "Checking Build Directories (everything is relative to the script path)"
mkdir -p $BUILD_DIR/$DIR_TMP
mkdir -p $BUILD_DIR/$DIR_PKG
mkdir -p $BUILD_DIR/$DIR_REPO
mkdir -p $BUILD_DIR/$DIR_IMAGE
mkdir -p $BUILD_DIR/$DIR_CACHE
mkdir -p $BUILD_DIR/$DIR_DOWNLOAD
mkdir -p $BUILD_DIR/$DIR_SOURCE
mkdir -p $BUILD_DIR/$DIR_LOG/build

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

wanted_sections=("Build" "Base" "Source")
current_section=""

while IFS= read -r line || [[ -n "$line" ]]; do
    # Strip leading/trailing whitespace
    line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

    # Skip empty lines and comments
    [[ -z "$line" || "$line" =~ ^# ]] && continue

    # Section headers
    if [[ "$line" =~ ^\[(.*)\]$ ]]; then
        section="${BASH_REMATCH[1]}"
        if [[ " ${wanted_sections[*]} " =~ " $section " ]]; then
            current_section="$section"
            echo -e "\n [$current_section]"
        else
            current_section=""
        fi
        continue
    fi

    # Key = Value lines, only if in a wanted section
    if [[ -n "$current_section" && "$line" =~ ^([^=]+)=[[:space:]]*(.*)$ ]]; then
        key=$(echo "${BASH_REMATCH[1]}" | xargs)
        value=$(echo "${BASH_REMATCH[2]}" | xargs)

        # Remove surrounding quotes
        value="${value%\"}"
        value="${value#\"}"

        printf "   %-20s : %s\n" "$key" "$value"
    fi
done < "$CONFIG_FILE"


# Check required Python packages
PY_REQ_FILE="py_requirements.txt"
if [ ! -f "$PY_REQ_FILE" ]; then
    echo "E: Python requirements file not found: $PY_REQ_FILE" >&2
    exit 1
fi

echo "Checking required Python packages..."
MISSING_PKGS=()

while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip empty lines and comments
    [[ -z "$line" || "$line" =~ ^# ]] && continue

    import_name=$(echo "$line" | awk '{print $1}')
    install_name=$(echo "$line" | awk '{print $2}')

    if ! python3 -c "import ${import_name}" 2>/dev/null; then
        MISSING_PKGS+=("${install_name}  (import: ${import_name})")
    fi
done < "$PY_REQ_FILE"

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "E: Missing Python packages:" >&2
    for pkg in "${MISSING_PKGS[@]}"; do
        echo "   - ${pkg}" >&2
    done
    exit 1
fi

echo "All required Python packages found."

python3 scripts/build.py --pkg-list=$PKG_REQ_FILE --working-dir=$BUILD_DIR --config-file=$CONFIG_FILE

