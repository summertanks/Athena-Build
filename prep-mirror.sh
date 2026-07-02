#!/usr/bin/env bash
#
# prep-mirror.sh — bootstrap (or adopt) a remote host as an Asgard publish
# mirror so `mirror add` can register it cleanly.
#
# A publish mirror is just a host that (a) stores the apt repo + coord
# sidecar + ISOs under a root dir and (b) serves that root over HTTP at
# /<dist-id>/.  This script connects over SSH with the given key and brings
# the host to that state idempotently:
#
#   PREPARED   marker present + ours        → no-op (report + exit 0)
#   ADOPT      no marker, but our layout (or part of it) already lives
#              there → verify, FILL ONLY THE GAPS, write marker
#   FRESH      empty / absent root          → install web server, create
#              the dir tree, configure serving, write marker
#   UNEXPECTED foreign content / bad marker → dump details, change NOTHING,
#              exit loudly
#
# It NEVER clobbers content it didn't create: anything it doesn't
# recognise is an UNEXPECTED hard stop, not a silent overwrite.
#
# The remote root + served path are DERIVED, not typed:
#   * the served path is /<dist-id> where <dist-id> = [Build] DISTRIBUTION
#     (config/distro.conf) lowercased — the SAME value `mirror add` derives
#     via derive_public_url(), so the two can't disagree.
#   * the remote root defaults to <remote-$HOME>/<dist-id> (resolved over
#     SSH), matching `repo publish ssh`.  Pass an explicit path in the
#     ssh-url to override it.
# So `ssh://ubuntu@host` is enough; the script resolves the rest and prints
# the full path-bearing url for `mirror add` (whose rsync push needs it).
#
# Usage:
#   ./prep-mirror.sh <ssh-url> <ssh-key> [--check] [--proto http|https]
#
#   <ssh-url>   ssh://user@host          (root derived: ~/<dist-id>)
#               ssh://user@host/abs/path (root pinned explicitly)
#   <ssh-key>   path to the SSH private key
#   --check     DRY RUN — probe + report what it WOULD do, change nothing
#   --proto     public scheme for the served URL note (default: http)
#
# Example:
#   ./prep-mirror.sh ssh://ubuntu@140.245.198.222 ~/.ssh/asgard_mirror --check
#
set -euo pipefail
_SELF_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CONF="${_SELF_DIR}/config/distro.conf"

# Colour only on a real terminal AND when NO_COLOR is unset.
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
    printf '%s\n' "${_B}Asgard mirror preparation${_R}"
}
step()  { printf '%s\n' "${_C}${_B}▸ ${1}${_R}"; }
info()  { printf '   %s\n' "${1}"; }
ok()    { printf '   %s%s%s\n' "${_G}" "✓ ${1}" "${_R}"; }
warn()  { printf '   %s%s%s\n' "${_Y}" "! ${1}" "${_R}"; }
die()   { printf '%s\n' "${_E}${_B}✗ ${1}${_R}" >&2; exit "${2:-1}"; }
# present|missing → coloured ✓/✗ for the inventory table
mark()  { case "$1" in
            present|yes|ours) printf '%s✓%s' "$_G" "$_R" ;;
            *)                printf '%s✗%s' "$_Y" "$_R" ;;
          esac; }

# ── argument parsing ────────────────────────────────────────────────────
SSH_URL=''; SSH_KEY=''; DRY=0; PROTO='http'
_pos=()
while [ $# -gt 0 ]; do
    case "$1" in
        --check)  DRY=1; shift ;;
        --proto)  PROTO="${2:-}"; shift 2 ;;
        -h|--help)
            # Print the leading comment block (skip the shebang, stop at the
            # first non-comment line) with the '# ' prefix stripped — robust to
            # the header changing length, unlike a hardcoded line range.
            awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
            exit 0 ;;
        --*) die "unknown flag: $1" 2 ;;
        *) _pos+=("$1"); shift ;;
    esac
done
[ "${#_pos[@]}" -ge 2 ] || die "usage: ./prep-mirror.sh <ssh-url> <ssh-key> [--check] [--proto http|https]" 2
SSH_URL="${_pos[0]}"; SSH_KEY="${_pos[1]}"
[ -f "$SSH_KEY" ] || die "SSH key not found: ${SSH_KEY}" 2
case "$PROTO" in http|https) ;; *) die "--proto must be http or https (got '${PROTO}')" 2 ;; esac

# ── distribution id (from config/distro.conf, [Build] DISTRIBUTION) ──────
# Mirrors scripts/mirror.py: dist_id = re.sub(r'[^a-z0-9_-]', '', DIST.lower()).
[ -f "$CONF" ] || die "config/distro.conf not found next to this script (${CONF})" 2
DIST_RAW="$(awk '
    /^[[:space:]]*\[/ { insec = ($0 ~ /^[[:space:]]*\[Build\]/) }
    insec && /^[[:space:]]*DISTRIBUTION[[:space:]]*=/ {
        sub(/^[^=]*=[[:space:]]*/, ""); sub(/[[:space:]]+$/, ""); print; exit
    }' "$CONF")"
DIST_ID="$(printf '%s' "$DIST_RAW" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
[ -n "$DIST_ID" ] || die "could not read [Build] DISTRIBUTION from ${CONF}" 2

# ── parse the ssh url → user / host / optional explicit root ─────────────
# ssh://user@host           → root derived remotely as ~/<dist-id>
# ssh://user@host/abs/path  → root pinned to /abs/path
case "$SSH_URL" in
    ssh://*) ;;
    *) die "ssh-url must start with ssh:// (got '${SSH_URL}')" 2 ;;
esac
_authpath="${SSH_URL#ssh://}"             # user@host[/path]
case "$_authpath" in
    */*) _auth="${_authpath%%/*}"; EXPLICIT_ROOT="/${_authpath#*/}"; EXPLICIT_ROOT="${EXPLICIT_ROOT%/}" ;;
    *)   _auth="${_authpath}";     EXPLICIT_ROOT='' ;;
esac
case "$_auth" in
    *@*) SSH_USER="${_auth%@*}"; SSH_HOST="${_auth#*@}" ;;
    *)   SSH_USER=''; SSH_HOST="$_auth" ;;
esac
[ -n "$SSH_HOST" ] || die "could not parse host from ssh-url '${SSH_URL}'" 2

# host-type keyword for `mirror add` (matches mirror.validate_host_for_type):
# IPv4/IPv6 literal → 'ip', otherwise → 'fqdn'.
case "$SSH_HOST" in
    *:*)                       HOST_TYPE=ip ;;   # IPv6
    *[!0-9.]*)                 HOST_TYPE=fqdn ;; # has a non-IPv4 char
    [0-9]*.[0-9]*.[0-9]*.[0-9]*) HOST_TYPE=ip ;; # dotted quad
    *)                         HOST_TYPE=fqdn ;;
esac

# served path + public url are dist-id derived (NOT the root basename) so
# they match derive_public_url() exactly.  IPv6 hosts get re-bracketed.
URL_PATH="/${DIST_ID}"
_urlhost="$SSH_HOST"
case "$_urlhost" in *:*) [ "${_urlhost#\[}" = "$_urlhost" ] && _urlhost="[${_urlhost}]" ;; esac
PUBLIC_URL="${PROTO}://${_urlhost}/${DIST_ID}"
SSH_TARGET="${SSH_USER:+${SSH_USER}@}${SSH_HOST}"
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes)

# ── logging — record every prep run under log/ (same as the build) ───────
# The log dir comes from [Directories] Log in config/distro.conf (default:
# log/).  From here on, everything printed — banner, steps, the remote
# REMOTE: lines, warnings, and the final die — is tee'd to a timestamped
# transcript with ANSI colour stripped, so mirror prep leaves the same
# auditable record as a build run.  sed -u keeps the log line-flushed so a
# fast die/exit can't truncate it.
LOG_DIR_NAME="$(awk '
    /^[[:space:]]*\[/ { insec = ($0 ~ /^[[:space:]]*\[Directories\]/) }
    insec && /^[[:space:]]*Log[[:space:]]*=/ {
        sub(/^[^=]*=[[:space:]]*/, ""); sub(/[[:space:]]+$/, ""); print; exit
    }' "$CONF")"
LOG_DIR="${_SELF_DIR}/${LOG_DIR_NAME:-log}"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="${_SELF_DIR}"
LOGFILE="${LOG_DIR}/prep-mirror-$(date -u +%Y-%m-%dT%H-%M-%S).log"
exec > >(tee >(sed -u 's/\x1b\[[0-9;]*m//g' >> "$LOGFILE")) 2>&1

banner
step "Target"
info "ssh-url      ${SSH_URL}"
info "host         ${SSH_HOST}   user ${SSH_USER:-<default>}"
info "dist-id      ${DIST_ID}   (from ${CONF#"${_SELF_DIR}/"})"
info "root         ${EXPLICIT_ROOT:-~/${DIST_ID}  (derived; resolved over SSH)}"
info "served at    ${PUBLIC_URL}/   (location ${URL_PATH}/)"
info "log          ${LOGFILE#"${_SELF_DIR}/"}"
[ "$DRY" -eq 1 ] && warn "--check: DRY RUN — nothing on the remote will change"

# ── connectivity ────────────────────────────────────────────────────────
step "Connectivity"
if ! ssh "${SSH_OPTS[@]}" "$SSH_TARGET" 'echo ok' >/dev/null 2>&1; then
    die "cannot SSH to ${SSH_TARGET} with key ${SSH_KEY} (check key, user, host, firewall)"
fi
ok "SSH to ${SSH_TARGET} works"

# ── identity + inventory probe (read-only, single round-trip) ────────────
# Remote computes ROOT (it owns $HOME) and reports identity, permissions
# and a per-component inventory as KEY=VALUE lines.
step "Inspecting remote"
# NB: ssh joins remote args with spaces and re-parses them, so an EMPTY
# positional would silently collapse (shifting later args).  Pass a sentinel
# for "no explicit root" instead.
PROBE="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" 'bash -s' -- \
    "$DIST_ID" "${EXPLICIT_ROOT:-_NONE_}" "$URL_PATH" <<'REMOTE'
set -eu
DIST="$1"; EXPLICIT="$2"; URL_PATH="$3"
[ "$EXPLICIT" = "_NONE_" ] && EXPLICIT=""
if [ -n "$EXPLICIT" ]; then ROOT="$EXPLICIT"; else ROOT="${HOME}/${DIST}"; fi
COORD="${ROOT}-coord"; MARKER="${ROOT}/mirror-info.json"
echo "WHOAMI=$(id -un)"
echo "HOME=${HOME}"
echo "ROOT=${ROOT}"
echo "COORD=${COORD}"
if sudo -n true 2>/dev/null; then echo "SUDO=yes"; else echo "SUDO=no"; fi
# write permission for the dir we'd create under
if [ -d "$ROOT" ]; then [ -w "$ROOT" ] && echo "WRITABLE=yes" || echo "WRITABLE=no"
else _p="$(dirname "$ROOT")"; { [ -d "$_p" ] && [ -w "$_p" ]; } && echo "WRITABLE=yes" || echo "WRITABLE=no"; fi
# root shape
if [ -e "$ROOT" ] && [ ! -d "$ROOT" ]; then echo "ROOT_NOTDIR=yes"; else echo "ROOT_NOTDIR=no"; fi
if [ -d "$ROOT" ] && [ -n "$(ls -A "$ROOT" 2>/dev/null || true)" ]; then echo "ROOT_NONEMPTY=yes"; else echo "ROOT_NONEMPTY=no"; fi
# per-component inventory
inv() { [ -d "$1" ] && echo "$2=present" || echo "$2=missing"; }
inv "${ROOT}/dists"            INV_dists
inv "${ROOT}/iso"              INV_iso
inv "${COORD}/claims"          INV_claims
inv "${COORD}/keyring/builders" INV_keyring
if [ -f "$MARKER" ]; then
    if grep -q '"marker"[[:space:]]*:[[:space:]]*"asgard-mirror"' "$MARKER" 2>/dev/null; then echo "INV_marker=present"
    else echo "INV_marker=foreign"; fi
else echo "INV_marker=absent"; fi
# serving + whether the SERVED root is the release index page (vs a listing)
if curl -fsS "http://localhost${URL_PATH}/" >/dev/null 2>&1; then
    echo "INV_serving=yes"
    if curl -fsS "http://localhost${URL_PATH}/" 2>/dev/null | grep -q 'asgard-release-index'; then echo "INV_index=yes"; else echo "INV_index=no"; fi
else echo "INV_serving=no"; echo "INV_index=no"; fi
REMOTE
)"
# parse KEY=VALUE lines into shell vars (whitelisted keys only)
WHOAMI=''; REMOTE_HOME=''; ROOT=''; COORD=''; SUDO=''; WRITABLE=''
ROOT_NOTDIR=''; ROOT_NONEMPTY=''
INV_dists=''; INV_iso=''; INV_claims=''; INV_keyring=''
INV_marker=''; INV_serving=''; INV_index=''
while IFS='=' read -r _k _v; do
    case "$_k" in
        WHOAMI) WHOAMI="$_v" ;; HOME) REMOTE_HOME="$_v" ;;
        ROOT) ROOT="$_v" ;; COORD) COORD="$_v" ;;
        SUDO) SUDO="$_v" ;; WRITABLE) WRITABLE="$_v" ;;
        ROOT_NOTDIR) ROOT_NOTDIR="$_v" ;; ROOT_NONEMPTY) ROOT_NONEMPTY="$_v" ;;
        INV_dists) INV_dists="$_v" ;; INV_iso) INV_iso="$_v" ;;
        INV_claims) INV_claims="$_v" ;; INV_keyring) INV_keyring="$_v" ;;
        INV_marker) INV_marker="$_v" ;; INV_serving) INV_serving="$_v" ;;
        INV_index) INV_index="$_v" ;;
    esac
done <<< "$PROBE"
[ -n "$ROOT" ] || die "could not inspect remote (probe returned: ${PROBE:-<empty>})"

# full path-bearing ssh-url for `mirror add` (its rsync push needs the path)
CANON_URL="ssh://${_auth}${ROOT}"
# the exact `mirror add` invocation: <ip|fqdn> <ssh-url> --ssh-key --proto
ADD_CMD="mirror add ${HOST_TYPE} ${CANON_URL} --ssh-key ${SSH_KEY} --proto ${PROTO}"

# ── report identity + inventory ──────────────────────────────────────────
ok "user ${WHOAMI}   home ${REMOTE_HOME}"
info "resolved root  ${ROOT}"
info "coord root     ${COORD}"
if [ "$WRITABLE" = yes ]; then
    ok "${WHOAMI} can write the root"
else
    warn "${WHOAMI} may NOT be able to write ${ROOT} (check ownership/permissions)"
fi
if [ "$SUDO" = yes ]; then
    info "passwordless sudo available"
else
    info "no passwordless sudo (only needed to install/configure the web server)"
fi

step "Inventory"
printf '   %s  repo dists/            %s\n' "$(mark "$INV_dists")"   "$INV_dists"
printf '   %s  iso/                   %s\n' "$(mark "$INV_iso")"     "$INV_iso"
printf '   %s  coord claims/          %s\n' "$(mark "$INV_claims")"  "$INV_claims"
printf '   %s  coord keyring/         %s\n' "$(mark "$INV_keyring")" "$INV_keyring"
printf '   %s  mirror-info.json       %s\n' "$(mark "$INV_marker")"  "$INV_marker"
printf '   %s  HTTP serving %s/   %s\n'     "$(mark "$INV_serving")" "$URL_PATH" "$INV_serving"
printf '   %s  index page at root     %s\n' "$(mark "$INV_index")"   "$INV_index"

# ── classify state ───────────────────────────────────────────────────────
_our_any=no
for _c in "$INV_dists" "$INV_iso" "$INV_claims" "$INV_keyring"; do
    [ "$_c" = present ] && _our_any=yes
done
if   [ "$ROOT_NOTDIR" = yes ]; then STATE_KIND=UNEXPECTED; STATE_DETAIL="root exists but is not a directory: ${ROOT}"
elif [ "$INV_marker" = foreign ]; then STATE_KIND=UNEXPECTED; STATE_DETAIL="${ROOT}/mirror-info.json exists but is not an asgard-mirror marker"
elif [ "$INV_marker" = present ]; then STATE_KIND=PREPARED; STATE_DETAIL="marker present"
elif [ "$_our_any" = yes ]; then STATE_KIND=ADOPT; STATE_DETAIL="partial/existing asgard layout — will fill the gaps"
elif [ "$ROOT_NONEMPTY" = yes ]; then STATE_KIND=UNEXPECTED; STATE_DETAIL="${ROOT} is non-empty but has none of our components — refusing to touch it"
else STATE_KIND=FRESH; STATE_DETAIL="${ROOT} absent or empty"
fi

step "State: ${STATE_KIND}"
case "$STATE_KIND" in
    PREPARED)
        ok "already prepared — ${STATE_DETAIL}"
        if [ "$INV_serving" = yes ] && [ "$INV_index" = yes ]; then
            ok "serving the release index — ready to register:"
            printf '\n     %s%s%s\n\n' "${_B}" "$ADD_CMD" "${_R}"
        elif [ "$INV_serving" = yes ]; then
            warn "serving works but ${URL_PATH}/ shows a directory listing, not the release index page"
            warn "publish a release (mirror publish) so index.html exists, and ensure nginx has 'index index.html;' for ${URL_PATH}/"
        else
            warn "marker present but ${PUBLIC_URL}/ is not HTTP-reachable — the web server may be down; investigate before mirror add"
        fi
        exit 0 ;;
    UNEXPECTED)
        die "UNEXPECTED remote state — ${STATE_DETAIL}.  Refusing to modify the host.  Inspect it by hand, then re-run." ;;
    ADOPT) ok "${STATE_DETAIL}" ;;
    FRESH) ok "${STATE_DETAIL} — will install the web server + create the layout" ;;
esac

# permission gate: FRESH (or a non-serving adopt) needs sudo to install /
# configure the web server, and we run non-interactively (BatchMode).
if [ "$INV_serving" != yes ] && [ "$SUDO" != yes ]; then
    warn "the web server isn't serving ${URL_PATH}/ yet and ${WHOAMI} has no passwordless sudo"
    warn "installing/configuring nginx needs root; this non-interactive SSH session can't prompt for a password"
    die "grant ${WHOAMI} passwordless sudo (or pre-install + configure the web server), then re-run"
fi
[ "$WRITABLE" = yes ] || die "${WHOAMI} cannot write ${ROOT} — fix ownership/permissions, then re-run"

if [ "$DRY" -eq 1 ]; then
    step "DRY RUN — planned actions (${STATE_KIND})"
    [ "$INV_dists"   = missing ] && info "create ${ROOT}/dists"
    [ "$INV_iso"     = missing ] && info "create ${ROOT}/iso"
    [ "$INV_claims"  = missing ] && info "create ${COORD}/claims"
    [ "$INV_keyring" = missing ] && info "create ${COORD}/keyring/builders"
    if [ "$INV_serving" != yes ]; then
        [ "$STATE_KIND" = FRESH ] && info "apt-get install -y nginx rsync (if absent)"
        info "configure ${URL_PATH}/ → ${ROOT}/ (index index.html; autoindex on)"
    elif [ "$INV_index" != yes ]; then
        info "advise: add 'index index.html;' to the existing ${URL_PATH}/ server block (won't auto-edit)"
    fi
    info "write marker ${ROOT}/mirror-info.json"
    warn "no changes made (--check)"
    info "when applied, register with:"
    info "  ${ADD_CMD}"
    exit 0
fi

# ── apply ───────────────────────────────────────────────────────────────
step "Preparing ${SSH_HOST} (${STATE_KIND})"
APPLY_OUT="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" 'bash -s' -- \
    "$ROOT" "$COORD" "$URL_PATH" "$STATE_KIND" <<'REMOTE'
set -eu
ROOT="$1"; COORD="$2"; URL_PATH="$3"; KIND="$4"; MARKER="${ROOT}/mirror-info.json"
SUDO=''; [ "$(id -u)" -ne 0 ] && SUDO='sudo'
say() { echo "REMOTE: $*"; }

# 1. dir layout — create only what's missing, report each
ensure() { if [ -d "$1" ]; then say "present  $1"; else mkdir -p "$1"; say "created  $1"; fi; }
ensure "${ROOT}/dists"
ensure "${ROOT}/iso"
ensure "${COORD}/claims"
ensure "${COORD}/keyring/builders"

# 2. web server
serves_ok()  { curl -fsS "http://localhost${URL_PATH}/" >/dev/null 2>&1; }
serves_idx() { curl -fsS "http://localhost${URL_PATH}/" 2>/dev/null | grep -q 'asgard-release-index'; }
if serves_ok; then
    if serves_idx; then
        say "web server serves the release index at ${URL_PATH}/ — config untouched"
    else
        say "NOTE: ${URL_PATH}/ serves a directory listing, not index.html"
        say "NOTE: add 'index index.html;' to that server block so the release page is the root (not auto-editing an existing config)"
    fi
else
    if ! command -v nginx >/dev/null 2>&1; then
        say "installing nginx + rsync"
        $SUDO apt-get update -qq
        $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx rsync >/dev/null
    fi
    CONF="/etc/nginx/conf.d/asgard-$(basename "$ROOT").conf"
    if [ -f "$CONF" ]; then
        say "ABORT: ${CONF} already exists but ${URL_PATH}/ isn't serving — refusing to overwrite an existing config"
        exit 3
    fi
    if [ -L /etc/nginx/sites-enabled/default ]; then
        $SUDO rm -f /etc/nginx/sites-enabled/default
        say "disabled the stock nginx default site"
    fi
    $SUDO tee "$CONF" >/dev/null <<NGINX
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    location = ${URL_PATH} { return 301 ${URL_PATH}/; }
    location ${URL_PATH}/ {
        alias ${ROOT}/;
        index index.html;
        autoindex on;
        types { application/json json; text/html html; application/octet-stream iso; }
        default_type application/octet-stream;
    }
}
NGINX
    if ! $SUDO nginx -t >/dev/null 2>&1; then
        $SUDO rm -f "$CONF"
        say "ABORT: nginx config test failed — reverted ${CONF}, no reload"
        exit 3
    fi
    $SUDO systemctl reload nginx 2>/dev/null || $SUDO nginx -s reload
    say "configured nginx ${URL_PATH}/ → ${ROOT}/ (index index.html; autoindex on) and reloaded"
fi

# 3. marker (written LAST, after layout + serving are in place)
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$MARKER" <<JSON
{
  "marker": "asgard-mirror",
  "schema": 1,
  "prepared_at": "${NOW}",
  "dist_id": "$(basename "$ROOT")",
  "root": "${ROOT}",
  "coord_root": "${COORD}",
  "url_path": "${URL_PATH}",
  "web_server": "nginx",
  "adopted": $([ "$KIND" = ADOPT ] && echo true || echo false)
}
JSON
say "wrote marker ${MARKER}"
REMOTE
)"
printf '%s\n' "$APPLY_OUT" | sed 's/^REMOTE: /   /'

# ── verify ──────────────────────────────────────────────────────────────
step "Verifying"
if ! curl -fsS "${PUBLIC_URL}/mirror-info.json" >/dev/null 2>&1; then
    warn "marker written but ${PUBLIC_URL}/mirror-info.json is not HTTP-reachable yet"
    warn "check the web server / firewall (port 80) on ${SSH_HOST}, then re-run with --check"
    exit 1
fi
ok "${PUBLIC_URL}/mirror-info.json is HTTP-reachable"
if curl -fsS "${PUBLIC_URL}/" 2>/dev/null | grep -q 'asgard-release-index'; then
    ok "${PUBLIC_URL}/ serves the release index page"
else
    warn "${PUBLIC_URL}/ does not yet serve the release index (publish a release so index.html exists)"
fi
ok "mirror prepared — register it with:"
printf '\n     %s%s%s\n\n' "${_B}" "$ADD_CMD" "${_R}"
