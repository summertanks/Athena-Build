#!/bin/bash

# Colour only on a real terminal AND when NO_COLOR is unset — the same
# presentation helpers as prep-mirror.sh; keep the two scripts in step.
if [ -t 1 ] && [ -z "${NO_COLOR+x}" ]; then
    _B=$'\033[1m'; _D=$'\033[2m'; _R=$'\033[0m'
    _G=$'\033[32m'; _Y=$'\033[33m'; _E=$'\033[31m'; _C=$'\033[36m'
else
    _B=''; _D=''; _R=''; _G=''; _Y=''; _E=''; _C=''
fi
banner() {
    printf '%s\n' "${_C}${_B}╭─╮╶┬╴╷ ╷╭─╴╭╮╷╭─╮   ╭╮ ╷ ╷╷╷  ╶┬╮   ╭─╮╷ ╷╭─╮╶┬╴╭─╴╭┬╮${_R}"
    printf '%s\n' "${_C}${_B}├─┤ │ ├─┤├╴ │╰┤├─┤   ├┴╮│ │││   ││   ╰─╮╰┬╯╰─╮ │ ├╴ │││${_R}"
    printf '%s\n' "${_C}${_B}╵ ╵ ╵ ╵ ╵╰─╴╵ ╵╵ ╵   ╰─╯╰─╯╵╰─╴╶┴╯   ╰─╯ ╵ ╰─╯ ╵ ╰─╴╵ ╵${_R}"
    printf '%s\n' "${_D}(c) Harkirat S Virk${_R}"
    printf '%s\n' "${_D}https://github.com/summertanks/Athena-Build${_R}"
    printf '%s\n' "${_B}Host preflight${_R}"
}
step()  { printf '\n%s\n' "${_C}${_B}${1}${_R}"; }
info()  { printf '   %s\n' "${1}"; }
ok()    { printf '   %s%s%s\n' "${_G}" "[  OK] ${1}" "${_R}"; }
warn()  { printf '   %s%s%s\n' "${_Y}" "[WARN] ${1}" "${_R}"; }
die()   { printf '%s\n' "${_E}${_B}[FAIL] ${1}${_R}" >&2; exit "${2:-1}"; }
# info-print "tool: version" without doubling the name when the tool's
# version line already leads with it (mksquashfs, grub-mkrescue, ...).
tool_info() {
    case "$2" in
        "$1"*|*"/$1 "*) info "$2" ;;
        '')             info "$1 (version unknown)" ;;
        *)              info "$1 $2" ;;
    esac
}
# require_min <label> <min> <version-line> — pull the first x.y[.z]
# out of the line and die when it is below <min> (dpkg version compare
# — the build host is always dpkg-based).  Silent when satisfied; an
# unparseable line only warns (present-but-odd output must not block
# startup).  The comment at each call site names the feature that
# demands the floor.
require_min() {
    local _have
    # head -n1: -o prints every match on the line; only the first is the
    # tool's own version (e.g. "OpenSSH_10.0p2 ..., OpenSSL 3.5.6 ...").
    _have=$(printf '%s' "$3" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)*' | head -n1 || true)
    if [ -z "$_have" ]; then
        warn "$1 required >= $2, found unknown"
    elif ! dpkg --compare-versions "$_have" ge "$2"; then
        die "$1 required >= $2, found $_have"
    else
        ok "$1 required >= $2, found $_have"
    fi
}

DIR_TMP="tmp"
DIR_PKG="packages"
DIR_REPO="repo"
DIR_IMAGE="image"
DIR_CACHE="cache"
DIR_DOWNLOAD="download"
DIR_LOG="log"
DIR_SOURCE="source"
DIR_GNUPG="gnupg"

# Fast version exit — handled before any banner / getopt / preflight so the
# output is clean (importing only scripts/_version: pure stdlib + git).
for _arg in "$@"; do
	case "$_arg" in
		-V|--version)
			/usr/bin/python3 -c "import sys; sys.path.insert(0, 'scripts'); import _version; print(_version.version_line())";
			exit 0;;
	esac
done

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
        echo -e "\t -V|--version : Print the Athena-Build toolchain version and exit"; \
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

banner

# Parsing args
ARGS=$(getopt -n Athena -o 'hc:p:vV' --long 'help,version,config-file:,pkg-list:,verbose,headless,yes,cmd:,api,api-port:' -- "$@") || exit
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
		(-V|--version)
			# Fast, config-free version: import ONLY scripts/_version (pure
			# stdlib + git) — skips the whole preflight + heavy build.py imports.
			/usr/bin/python3 -c "import sys; sys.path.insert(0, 'scripts'); import _version; print(_version.version_line())";
			exit;;
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
step "Host tools"
if [[ "$(id -u)" ==  0 ]]; then
	warn "running as sudo"
fi

# Check sudo access — chroot build & iso build live run dpkg / mksquashfs as root via sudo
if sudo -l -n 2>/dev/null | grep -q '(ALL'; then
    ok "sudo access: $(whoami) has sudo privileges"
elif id -nG "$(whoami)" | grep -qw sudo; then
    ok "sudo access: $(whoami) is in sudo group — password will be required"
else
    die "$(whoami) does not have sudo access — chroot build and iso build live require sudo"
fi

# Version probes use `|| true` after the head pipe.  `head -n1` closes
# its read end after one line; the writer (bash, gunzip, etc.) gets
# SIGPIPE 141 on its next write, and under `set -o pipefail` that
# propagates and `errexit` kills the script silently.  Race condition —
# whether the writer has finished flushing before head closes depends
# on output size and scheduler timing.

# Bash version (assoc arrays + ${var//} rewrites — ancient floor)
require_min bash 4.4 "$(/usr/bin/bash --version 2>/dev/null | head -n1 || true)"

# gzip version (apt_repo compresses indexes with `gzip -k`, added 1.6)
require_min gzip 1.6 "$(/usr/bin/gunzip --version 2>/dev/null | head -n1 || true)"

# python version — hard floor 3.11: utils uses hashlib.file_digest
# (added 3.11); mypy/CI already type-check against 3.11.
_PY_V=$(/usr/bin/python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || true)
if /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    ok "python3 required >= 3.11, found ${_PY_V}"
else
    die "python3 required >= 3.11, found ${_PY_V:-none}"
fi

# checking docker
if [ -x "$(command -v docker || true)" ]; then
    # docker-py auto-negotiates the API; 20.10 is the practical floor
    require_min docker 20.10 "$(docker --version 2>/dev/null | head -n1 || true)"
else
    die "docker not found, build system requires docker"
fi

# mmdebstrap — bootstraps the build-container base rootfs from the pinned
# snapshot (scripts/base_rootfs.py); 1.2 is the --aptopt/--variant floor.
if [ -x "$(command -v mmdebstrap || true)" ]; then
    require_min mmdebstrap 1.2 "$(mmdebstrap --version 2>/dev/null | head -n1 || true)"
else
    die "mmdebstrap not found (apt install mmdebstrap) — required to bootstrap the build-container base"
fi

# checking gpg (used by python-gnupg in verify_inrelease).
# python-gnupg invokes `gpg`, not `gpgv`, so we check for the full binary.
if [ -x "$(command -v gpg || true)" ]; then
    require_min gpg 2.1 "$(gpg --version 2>/dev/null | head -n1 || true)"
else
    die "gpg not found (install gnupg) — required for InRelease verification"
fi

# rsync — every mirror publish/pull transfer and the disk-image chroot
# copy go through it; the coord transport passes --mkpath.
if [ -x "$(command -v rsync || true)" ]; then
    require_min rsync 3.2.3 "$(rsync --version 2>/dev/null | head -n1 || true)"
else
    die "rsync not found — mirror publish/pull and disk imaging require it"
fi

# openssl — builder Ed25519 identity (genpkey + pkeyutl -rawin).
# NOTE: -rawin needs OpenSSL 3.0, not the oft-quoted 1.1.1.
if [ -x "$(command -v openssl || true)" ]; then
    require_min openssl 3.0.0 "$(openssl version 2>/dev/null | head -n1 || true)"
else
    die "openssl not found — builder identity (Ed25519 claims) requires it"
fi

# ssh — remote transport uses StrictHostKeyChecking=accept-new.
if [ -x "$(command -v ssh || true)" ]; then
    require_min OpenSSH 7.6 "$(ssh -V 2>&1 | head -n1 || true)"
else
    die "ssh not found (install openssh-client) — mirror + remote-build transport requires it"
fi

# git — toolchain version derivation (git describe).
if [ -x "$(command -v git || true)" ]; then
    require_min git 1.8.5 "$(git --version 2>/dev/null | head -n1 || true)"
else
    die "git not found — toolchain version derivation requires it"
fi

# checking default Debian archive keyring.  build.conf [Security] Keyring
# may override this path; the Python BuildConfig validates the configured
# value.  Here we only check the default location with a warning so a
# config-overridden setup is not blocked.
DEFAULT_KEYRING="/usr/share/keyrings/debian-archive-keyring.gpg"
if [ -r "$DEFAULT_KEYRING" ]; then
    info "keyring $DEFAULT_KEYRING (from debian-archive-keyring)"
else
    warn "$DEFAULT_KEYRING not found — install 'debian-archive-keyring' or set [Security] Keyring in build.conf to a readable file"
fi

# checking docker group membership
if id -nG "$(whoami)" | grep -qw docker; then
    ok "user $(whoami) is in the docker group"
else
    die "user $(whoami) is not in the 'docker' group — cannot talk to docker daemon"
fi

# checking wget
if [ -x /usr/bin/wget ]; then
        require_min wget 1.14 "$(/usr/bin/wget --version 2>/dev/null | head -n1 || true)"
else
        die "wget not found, do we want to be in a world without wget"
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
    die "awk not found, build script will not work"
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
require_min "awk (${PACKAGE:-unknown})" 1.3 "${AWK_VERSION:-}"

# Build mode.  A build-mode peer only builds + publishes individual packages
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
    info "build mode = build — ISO/disk host tools are optional"
fi

# Checking ISO build tools (required for `iso build live` command only)
step "ISO build tools"
ISO_TOOLS_OK=1

if [ -x "$(command -v mksquashfs || true)" ]; then
    require_min mksquashfs 4.2 "$(mksquashfs -version 2>&1 | head -n1 || true)"
else
    warn "mksquashfs not found (install squashfs-tools) — iso build live will not work"
    ISO_TOOLS_OK=0
fi

if [ -x "$(command -v grub-mkrescue || true)" ]; then
    require_min grub-mkrescue 2.02 "$(grub-mkrescue --version 2>/dev/null | head -n1 || true)"
else
    warn "grub-mkrescue not found (install grub-pc-bin grub-efi-amd64-bin) — iso build live will not work"
    ISO_TOOLS_OK=0
fi

if [ -x "$(command -v xorriso || true)" ]; then
    require_min xorriso 1.4 "$(xorriso --version 2>&1 | head -n1 || true)"
else
    warn "xorriso not found (install xorriso) — iso build live will not work"
    ISO_TOOLS_OK=0
fi

if [ -x "$(command -v mformat || true)" ]; then
    require_min mformat 4.0 "$(mformat --version 2>&1 | head -n1 || true)"
else
    warn "mformat not found (install mtools) — grub-mkrescue will fail"
    ISO_TOOLS_OK=0
fi

if [[ $ISO_TOOLS_OK -eq 0 ]]; then
    if [[ "$BUILD_MODE" == "build" ]]; then
        warn "ISO build tools missing — skipped (Mode = build; iso steps are refused in build mode)"
    else
        die "one or more ISO build tools missing — run: sudo apt install squashfs-tools grub-pc-bin grub-efi-amd64-bin xorriso mtools"
    fi
else
    ok "all ISO build tools found"
fi

# Checking disk image build tools (required for `iso build disk` — COMP-09).
# Gates startup — every tool below must be present.  Same pattern as
# the ISO tools section above.
step "Disk image build tools"
DISK_TOOLS_OK=1
DISK_MISSING=()          # "tool (pkg)" pairs — names the actual missing tool
DISK_MISSING_PKGS=()     # bare package names — for the apt-install hint

# tool → providing package map.  When the tool is missing, the
# matching package name gets appended to DISK_MISSING_PKGS for the
# summary listing.
# (rsync is checked under Host tools — required unconditionally there.)
declare -A _DISK_TOOL_PKG=(
    [qemu-img]=qemu-utils
    [mkfs.fat]=dosfstools
    [losetup]=util-linux
    [sfdisk]=fdisk
    [mkfs.ext4]=e2fsprogs
    [grub-install]=grub-common
    [blkid]=util-linux
)
# tool → minimum version.  losetup -P needs util-linux 2.25 and
# sfdisk --wipe needs 2.29 (disk_image.py); the rest are ancient
# conservative floors — no modern flag in use.
declare -A _DISK_TOOL_MIN=(
    [qemu-img]=2.0
    [mkfs.fat]=3.0
    [losetup]=2.25
    [sfdisk]=2.29
    [mkfs.ext4]=1.43
    [grub-install]=2.02
    [blkid]=2.25
)

for _t in qemu-img mkfs.fat losetup sfdisk mkfs.ext4 grub-install blkid; do
    _p=$(command -v "$_t" || true)
    if [ -z "$_p" ]; then
        for _d in /sbin /usr/sbin; do
            [ -x "$_d/$_t" ] && _p="$_d/$_t" && break
        done
    fi
    if [ -n "$_p" ]; then
        _v=$($_p --version 2>/dev/null | head -n1 || true)
        # mke2fs and friends only answer -V, and on stderr
        [ -z "$_v" ] && _v=$($_p -V 2>&1 | head -n1 || true)
        require_min "$_t" "${_DISK_TOOL_MIN[$_t]}" "$_v"
    else
        warn "$_t not found"
        DISK_TOOLS_OK=0
        _pkg="${_DISK_TOOL_PKG[$_t]}"
        DISK_MISSING+=("$_t ($_pkg)")
        DISK_MISSING_PKGS+=("$_pkg")
    fi
done

if [[ $DISK_TOOLS_OK -eq 0 ]]; then
    # List the actual missing tools with their providing package, e.g.
    # "sfdisk (fdisk)" — so the operator knows what's absent, not just an
    # opaque package name.  The install hint dedupes packages (util-linux
    # covers losetup+blkid; would otherwise repeat).
    _MISSING_TOOLS=$(printf '%s, ' "${DISK_MISSING[@]}"); _MISSING_TOOLS=${_MISSING_TOOLS%, }
    _UNIQ_PKGS=$(printf '%s\n' "${DISK_MISSING_PKGS[@]}" | sort -u | tr '\n' ' ')
    if [[ "$BUILD_MODE" == "build" ]]; then
        warn "disk image build tools missing — skipped (Mode = build): $_MISSING_TOOLS"
    else
        warn "one or more disk image build tools missing: $_MISSING_TOOLS"
        die "install with: sudo apt install${_UNIQ_PKGS:+ }${_UNIQ_PKGS% }"
    fi
else
    ok "all disk image build tools found"
fi

# systemd-firstboot — chroot build (live + disk) stamps the target's
# root password / hostname / locale with it; --force needs systemd 247.
if [ -x "$(command -v systemd-firstboot || true)" ]; then
    require_min systemd-firstboot 247 "$(systemd-firstboot --version 2>/dev/null | head -n1 || true)"
elif [[ "$BUILD_MODE" == "build" ]]; then
    warn "systemd-firstboot not found — skipped (Mode = build; chroot steps are refused in build mode)"
else
    die "systemd-firstboot not found (install systemd) — chroot build requires it"
fi

# CVE-01: grype is an OPTIONAL prerequisite — used by `cve` to scan the
# generated SBOM (`scripts/sbom.py` output) against the NVD + GHSA +
# Debian Security Tracker DBs.  Build pipeline runs fine without it;
# the `cve` command is only useful when grype is on PATH.  Non-blocking
# warning here so the operator knows how to enable it.
step "Optional tools"
if [ -x "$(command -v grype || true)" ]; then
    tool_info grype "$(grype version 2>/dev/null | head -n1 || true) — cve command enabled"
else
    warn "grype not found — cve command will be a no-op.  Install: https://github.com/anchore/grype/releases (or apt repo: https://anchore.github.io/grype/install.sh)"
fi

# Checking build directories
step "Build directories"
info "creating any missing (everything is relative to the script path)"
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
step "Configuration"
info "build host: $(awk -F= '/PRETTY_NAME/ { gsub(/"/, "", $2); print $2 }' /etc/os-release)"
BUILD_ID=$(awk -F= '/^ID/ { print $2 }' /etc/os-release)

info "build flavour: $BUILD_ID"
if [[ $BUILD_ID != "debian" ]]; then
	warn "not using Debian to build — untested, will likely fail"
fi

# Load basic config
if ! [ -f $CONFIG_FILE ]; then
	die "config file not found: $CONFIG_FILE"
else
	info "config file: $CONFIG_FILE"
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
            printf '   %s\n' "${_B}[$current_section]${_R}"
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

        printf "     %-20s : %s\n" "$key" "$value"
    fi
# Tracked config is split: [Build]/[Base] live in the sibling distro.conf,
# [Source] in build.conf.  Read both so the summary shows every section.
done < <(cat "$(dirname "$CONFIG_FILE")/distro.conf" "$CONFIG_FILE" 2>/dev/null)


# Check required Python packages
PY_REQ_FILE="py_requirements.txt"
if [ ! -f "$PY_REQ_FILE" ]; then
    die "Python requirements file not found: $PY_REQ_FILE"
fi

step "Python packages"

# Halt on the FIRST missing module rather than collecting them into an
# end-of-run summary: with a half-provisioned box the actionable signal is
# "the next thing to install", and per-line progress shows exactly which
# import failed (the import name often differs from the apt package name).
while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip empty lines and comments
    [[ -z "$line" || "$line" =~ ^# ]] && continue

    import_name=$(echo "$line" | awk '{print $1}')
    install_name=$(echo "$line" | awk '{print $2}')
    dist_name=$(echo "$line" | awk '{print $3}')
    min_ver=$(echo "$line" | awk '{print $4}')

    if ! python3 -c "import ${import_name}" 2>/dev/null; then
        warn "required Python module '${import_name}' is not importable"
        die "install it with:  sudo apt install ${install_name}"
    fi
    # Version floor (columns 3+4; apt installs register dist metadata,
    # so importlib.metadata resolves the installed version).
    have_ver=""
    if [ -n "$dist_name" ] && [ -n "$min_ver" ]; then
        have_ver=$(python3 -c "import importlib.metadata as m; print(m.version('${dist_name}'))" 2>/dev/null || true)
    fi
    if [ -z "$have_ver" ]; then
        warn "${import_name} required >= ${min_ver:-?}, found unknown"
    elif ! dpkg --compare-versions "$have_ver" ge "$min_ver"; then
        die "${import_name} required >= ${min_ver}, found ${have_ver}"
    else
        ok "${import_name} required >= ${min_ver}, found ${have_ver}"
    fi
done < "$PY_REQ_FILE"

ok "all required Python packages found"

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

