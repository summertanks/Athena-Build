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
DIR_GNUPG="gnupg"

VERBOSE="0"
CONFIG_FILE="config/build.conf"
PKG_REQ_FILE="config/pkg.list"
HEADLESS="0"
AUTO_YES="0"
ONESHOT_CMDS=()
API_MODE="0"
API_PORT=""

usage() { \
        echo -e "Usage:"; \
        echo -e "\t -c|--config-file <filename> : Config file giving basic system config"; \
        echo -e "\t -p|--pkg-list <filename> : File listing all packages included in distro"; \
        echo -e "\t -v|--verbose : Set verbosity high"; \
        echo -e "\t --headless : Skip the curses TUI; run a plain stdin/stdout REPL (UX-05 Path B)"; \
        echo -e "\t --yes : Auto-answer informational YESNO prompts (UX-05a)"; \
        echo -e "\t --cmd \"<cmd>\" : Run one command then exit; repeat for multiple (UX-05e).  Implies --headless"; \
        echo -e "\t --api : Serve the HTTP API as the session frontend (API-01); localhost-only"; \
        echo -e "\t --api-port <port> : API listen port (default 8765)"; \
}

BUILD_DIR=$(pwd)

# enable common error handling options
set -o errexit
set -o nounset
set -o pipefail

echo -e "Athena Build System Check..."

# Parsing args
ARGS=$(getopt -n Athena -o 'hc:p:v' --long 'help,config-file:,pkg-list:,verbose,headless,yes,cmd:,api,api-port:' -- "$@") || exit
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
		(--headless)
			HEADLESS=1;
			shift;;
		(--yes)
			AUTO_YES=1;
			shift;;
		(--cmd)
			ONESHOT_CMDS+=("$2");
			shift 2;;
		(--api)
			API_MODE=1;
			shift;;
		(--api-port)
			API_PORT=$2;
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

# Check sudo access — chroot build and iso build live run dpkg and mksquashfs
# as root via sudo.  -l lists the user's privileges; -n skips the password
# prompt so this is non-interactive.  A warning (not a fatal error) is issued
# here because the core build pipeline (cache/dependency/source) does not
# require sudo — only the chroot and ISO stages do.
if sudo -l -n 2>/dev/null | grep -q '(ALL'; then
    echo "Sudo access: OK ($(whoami) has sudo privileges)"
elif id -nG "$(whoami)" | grep -qw sudo; then
    echo "Sudo access: OK ($(whoami) is in sudo group — password will be required)"
else
    echo "E: $(whoami) does not have sudo access — chroot build and iso build live require sudo" >&2
    exit 1
fi

# Version probes use `|| true` after the head pipe.  `head -n1` closes
# its read end after one line; the writer (bash, gunzip, etc.) gets
# SIGPIPE 141 on its next write, and under `set -o pipefail` that
# propagates and `errexit` kills the script silently.  Race condition —
# whether the writer has finished flushing before head closes depends
# on output size and scheduler timing.

# Bash version
echo "Using $(/usr/bin/bash --version 2>/dev/null | head -n1 || true)"

# gunzip version
echo "Using $(/usr/bin/gunzip --version 2>/dev/null | head -n1 || true)"

# python version
echo "Using $(/usr/bin/python3 --version 2>/dev/null | head -n1 || true)"

# checking docker
if [ -x "$(command -v docker || true)" ]; then
    echo "Using $(docker --version 2>/dev/null | head -n1 || true)"
else
    echo "E: docker not found, build system requires docker" >&2
    exit 1
fi

# checking gpg (used by python-gnupg in verify_inrelease).
# python-gnupg invokes `gpg`, not `gpgv`, so we check for the full binary.
if [ -x "$(command -v gpg || true)" ]; then
    echo "Using $(gpg --version 2>/dev/null | head -n1 || true)"
else
    echo "E: gpg not found (install gnupg) — required for InRelease verification" >&2
    exit 1
fi

# checking default Debian archive keyring.  build.conf [Security] Keyring
# may override this path; the Python BuildConfig validates the configured
# value.  Here we only check the default location with a warning so a
# config-overridden setup is not blocked.
DEFAULT_KEYRING="/usr/share/keyrings/debian-archive-keyring.gpg"
if [ -r "$DEFAULT_KEYRING" ]; then
    echo "Using keyring $DEFAULT_KEYRING (from debian-archive-keyring)"
else
    echo "W: $DEFAULT_KEYRING not found — install 'debian-archive-keyring'" \
         "or set [Security] Keyring in build.conf to a readable file" >&2
fi

# checking docker group membership
if id -nG "$(whoami)" | grep -qw docker; then
    echo "User $(whoami) is in the docker group"
else
    echo "E: user $(whoami) is not in the 'docker' group — cannot talk to docker daemon" >&2
    exit 1
fi

# checking wget
if [ -x /usr/bin/wget ]; then
        echo "Using $(/usr/bin/wget --version 2>/dev/null | head -n1 || true)"
else
        echo "E: wget not found, do we want to be in a world without wget" > /dev/stderr
        exit 1
fi

# Checking awk — every pipe below is `|| true` because under
# `set -o pipefail` + `set -o errexit`:
#   1. `which awk` returning non-zero (rare) would silently kill
#      the script before the [ -x ] check runs.
#   2. `dpkg -S /path` returns 1 when the file is not in any
#      package and pipefail propagates that.
#   3. `cmd --version | head -n1` is the SIGPIPE-141 race that
#      manifested as the script silently exiting at this point.
AWK_PATH=$(command -v awk || true)
if [ -z "$AWK_PATH" ] || [ ! -x "$AWK_PATH" ]; then
    echo "E: awk not found, build script will not work" >&2
    exit 1
fi

REAL_AWK=$(readlink -f "$AWK_PATH" || echo "$AWK_PATH")
PACKAGE=$(dpkg -S "$REAL_AWK" 2>/dev/null | cut -d: -f1 || true)

case "$PACKAGE" in
    gawk)
        AWK_VERSION=$("$AWK_PATH" --version 2>/dev/null | head -n1 || true)
        ;;
    mawk)
        AWK_VERSION=$("$AWK_PATH" -W version 2>&1 | head -n1 || true)
        ;;
    original-awk)
        AWK_VERSION=$("$AWK_PATH" 2>&1 | grep -i version | head -n1 || true)
        ;;
    *)
        AWK_VERSION=$("$AWK_PATH" --version 2>/dev/null | head -n1 || true)
        ;;
esac
echo "Using awk: ${PACKAGE:-unknown} — ${AWK_VERSION:-version unknown}"

# Build mode.  A build-mode peer only builds + publishes packages (chroot/ISO/
# disk steps are refused in build mode), so it needs Docker + the cache/source
# toolchain but NOT the ISO/disk host tools — a missing one is a note, not a
# fatal startup error.
#
# Mode now lives in the untracked machine-local config/local.conf ([Local]
# Mode), falling back to build.conf for back-compat — mirror BuildConfig's
# precedence (local.conf > build.conf > distribution) so this gate agrees with
# the Python side on a peer whose mode is only in local.conf.
BUILD_MODE="distribution"
LOCAL_CONF="$(dirname "$CONFIG_FILE")/local.conf"
MODE_LINE=""
# `|| true`: with `errexit`+`pipefail`, a no-match grep (exit 1) inside a
# command substitution kills the script silently — fatal on a fresh clone
# (no local.conf, no Mode line in build.conf).  Mirror the `command -v … ||
# true` guard used elsewhere here.
if [ -f "$LOCAL_CONF" ]; then
    MODE_LINE=$(grep -iE '^[[:space:]]*Mode[[:space:]]*=' "$LOCAL_CONF" | head -1 || true)
fi
if [ -z "$MODE_LINE" ] && [ -f "$CONFIG_FILE" ]; then
    MODE_LINE=$(grep -iE '^[[:space:]]*Mode[[:space:]]*=' "$CONFIG_FILE" | head -1 || true)
fi
if printf '%s' "$MODE_LINE" | grep -qiE '=[[:space:]]*build([[:space:]]|$)'; then
    BUILD_MODE="build"
    echo "Build mode = build — ISO/disk host tools are optional."
fi

# Checking ISO build tools (required for `iso build live` command only)
echo "Checking ISO build tools..."
ISO_TOOLS_OK=1

if [ -x "$(command -v mksquashfs || true)" ]; then
    echo "Using mksquashfs $(mksquashfs -version 2>&1 | head -n1 || true)"
else
    echo "W: mksquashfs not found (install squashfs-tools) — iso build live will not work"
    ISO_TOOLS_OK=0
fi

if [ -x "$(command -v grub-mkrescue || true)" ]; then
    echo "Using grub-mkrescue $(grub-mkrescue --version 2>/dev/null | head -n1 || true)"
else
    echo "W: grub-mkrescue not found (install grub-pc-bin grub-efi-amd64-bin) — iso build live will not work"
    ISO_TOOLS_OK=0
fi

if [ -x "$(command -v xorriso || true)" ]; then
    echo "Using xorriso $(xorriso --version 2>&1 | head -n1 || true)"
else
    echo "W: xorriso not found (install xorriso) — iso build live will not work"
    ISO_TOOLS_OK=0
fi

if [ -x "$(command -v mformat || true)" ]; then
    echo "Using mformat $(mformat --version 2>&1 | head -n1 || true)"
else
    echo "W: mformat not found (install mtools) — grub-mkrescue will fail"
    ISO_TOOLS_OK=0
fi

if [[ $ISO_TOOLS_OK -eq 0 ]]; then
    if [[ "$BUILD_MODE" == "build" ]]; then
        echo "Note: ISO build tools missing — skipped (Mode = build; iso steps are refused in build mode)."
    else
        echo "E: one or more ISO build tools missing — run: sudo apt install squashfs-tools grub-pc-bin grub-efi-amd64-bin xorriso mtools" >&2
        exit 1
    fi
else
    echo "All ISO build tools found."
fi

# Checking disk image build tools (required for `iso build disk` — COMP-09).
# Gates startup — every tool below must be present.  Same pattern as
# the ISO tools section above.
echo "Checking disk image build tools..."
DISK_TOOLS_OK=1
DISK_MISSING_PKGS=()

# tool → providing package map.  When the tool is missing, the
# matching package name gets appended to DISK_MISSING_PKGS for the
# summary listing.
declare -A _DISK_TOOL_PKG=(
    [rsync]=rsync
    [qemu-img]=qemu-utils
    [mkfs.fat]=dosfstools
    [losetup]=util-linux
    [sfdisk]=util-linux
    [mkfs.ext4]=e2fsprogs
    [grub-install]=grub-common
    [blkid]=util-linux
)

for _t in rsync qemu-img mkfs.fat losetup sfdisk mkfs.ext4 grub-install blkid; do
    _p=$(command -v "$_t" || true)
    if [ -z "$_p" ]; then
        for _d in /sbin /usr/sbin; do
            [ -x "$_d/$_t" ] && _p="$_d/$_t" && break
        done
    fi
    if [ -n "$_p" ]; then
        echo "Using $_t $($_p --version 2>/dev/null | head -n1 || true)"
    else
        echo "W: $_t not found"
        DISK_TOOLS_OK=0
        DISK_MISSING_PKGS+=("${_DISK_TOOL_PKG[$_t]}")
    fi
done

if [[ $DISK_TOOLS_OK -eq 0 ]]; then
    # Dedupe (util-linux covers losetup+sfdisk+blkid; would otherwise
    # appear 3× in the message).
    _UNIQ_PKGS=$(printf '%s\n' "${DISK_MISSING_PKGS[@]}" | sort -u | tr '\n' ' ')
    if [[ "$BUILD_MODE" == "build" ]]; then
        echo "Note: disk image build tools missing — skipped (Mode = build): $_UNIQ_PKGS"
    else
        echo "E: one or more disk image build tools missing: $_UNIQ_PKGS" >&2
        exit 1
    fi
else
    echo "All disk image build tools found."
fi

# CVE-01: grype is an OPTIONAL prerequisite — used by `cve` to scan the
# generated SBOM (`scripts/sbom.py` output) against the NVD + GHSA +
# Debian Security Tracker DBs.  Build pipeline runs fine without it;
# the `cve` command is only useful when grype is on PATH.  Non-blocking
# warning here so the operator knows how to enable it.
if [ -x "$(command -v grype || true)" ]; then
    echo "Using grype $(grype version 2>/dev/null | head -n1 || true) — cve command enabled"
else
    echo "W: grype not found — cve command will be a no-op.  Install: https://github.com/anchore/grype/releases (or apt repo: https://anchore.github.io/grype/install.sh)"
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
# gpg requires 0700 on its homedir; chmod here so the Python verifier
# does not have to (and so re-runs are idempotent).
mkdir -p $BUILD_DIR/$DIR_GNUPG
chmod 0700 $BUILD_DIR/$DIR_GNUPG

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

# Halt on the FIRST missing module rather than collecting them into an
# end-of-run summary: with a half-provisioned box the actionable signal is
# "the next thing to install", and per-line progress shows exactly which
# import failed (the import name often differs from the apt package name).
while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip empty lines and comments
    [[ -z "$line" || "$line" =~ ^# ]] && continue

    import_name=$(echo "$line" | awk '{print $1}')
    install_name=$(echo "$line" | awk '{print $2}')

    printf '   %-14s ... ' "$import_name"
    if python3 -c "import ${import_name}" 2>/dev/null; then
        echo "ok"
    else
        echo "MISSING"
        echo "E: required Python module '${import_name}' is not importable." >&2
        echo "   install it with:  sudo apt install ${install_name}" >&2
        exit 1
    fi
done < "$PY_REQ_FILE"

echo "All required Python packages found."

# Forward UX-05 flags to build.py.  Each is stripped from sys.argv by
# build.py:main before BuildConfig (which uses argparse) sees argv.
PY_EXTRA=()
if [[ "$HEADLESS" == "1" ]]; then
    PY_EXTRA+=(--headless)
fi
if [[ "$AUTO_YES" == "1" ]]; then
    PY_EXTRA+=(--yes)
fi
for _cmd in "${ONESHOT_CMDS[@]}"; do
    PY_EXTRA+=(--cmd "$_cmd")
done
if [[ "$API_MODE" == "1" ]]; then
    PY_EXTRA+=(--api)
    if [[ -n "$API_PORT" ]]; then
        PY_EXTRA+=(--api-port "$API_PORT")
    fi
fi

python3 scripts/build.py --pkg-list=$PKG_REQ_FILE --working-dir=$BUILD_DIR --config-file=$CONFIG_FILE "${PY_EXTRA[@]}"

