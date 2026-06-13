#!/usr/bin/env bash
#
# prep-mirror.sh — bootstrap (or adopt) a remote host as an Asgard publish
# mirror so `mirror add` can register it cleanly.
#
# A publish mirror is just a host that (a) stores the apt repo + coord
# sidecar + ISOs under a root dir and (b) serves that root over HTTP.  This
# script connects over SSH with the given key and brings the host to that
# state idempotently:
#
#   PREPARED   marker present + ours        → no-op (report + exit 0)
#   ADOPT      no marker, but a repo already lives there (e.g. a host set
#              up by hand) → verify the layout, ensure dirs, write marker
#   FRESH      empty / absent root          → install web server, create
#              the dir tree, configure serving, write marker
#   UNEXPECTED foreign content / bad marker → dump details, change NOTHING,
#              exit loudly
#
# It NEVER clobbers content it didn't create: anything it doesn't
# recognise is an UNEXPECTED hard stop, not a silent overwrite.
#
# Usage:
#   ./prep-mirror.sh <ssh-url> <ssh-key> [--check] [--proto http|https]
#
#   <ssh-url>   ssh://user@host/path/to/root
#               (the SAME url you'll pass to `mirror add`)
#   <ssh-key>   path to the SSH private key
#   --check     DRY RUN — probe + report what it WOULD do, change nothing
#   --proto     public scheme for the served URL note (default: http)
#
# Example:
#   ./prep-mirror.sh ssh://ubuntu@140.245.198.222/home/ubuntu/asgard \
#       ~/.ssh/asgard_mirror --check
#
set -euo pipefail

# ── presentation ────────────────────────────────────────────────────────
if [ -t 1 ]; then
    _B=$'\033[1m'; _D=$'\033[2m'; _R=$'\033[0m'
    _G=$'\033[32m'; _Y=$'\033[33m'; _E=$'\033[31m'; _C=$'\033[36m'
else
    _B=''; _D=''; _R=''; _G=''; _Y=''; _E=''; _C=''
fi
banner() {
    printf '%s\n' "${_C}${_B}┌──────────────────────────────────────────────┐${_R}"
    printf '%s\n' "${_C}${_B}│  Asgard mirror preparation                   │${_R}"
    printf '%s\n' "${_C}${_B}└──────────────────────────────────────────────┘${_R}"
}
step()  { printf '%s\n' "${_C}${_B}▸ ${1}${_R}"; }
info()  { printf '   %s\n' "${1}"; }
ok()    { printf '   %s%s%s\n' "${_G}" "✓ ${1}" "${_R}"; }
warn()  { printf '   %s%s%s\n' "${_Y}" "! ${1}" "${_R}"; }
die()   { printf '%s\n' "${_E}${_B}✗ ${1}${_R}" >&2; exit "${2:-1}"; }

# ── argument parsing ────────────────────────────────────────────────────
SSH_URL=''; SSH_KEY=''; DRY=0; PROTO='http'
_pos=()
while [ $# -gt 0 ]; do
    case "$1" in
        --check)  DRY=1; shift ;;
        --proto)  PROTO="${2:-}"; shift 2 ;;
        -h|--help)
            sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --*) die "unknown flag: $1" 2 ;;
        *) _pos+=("$1"); shift ;;
    esac
done
[ "${#_pos[@]}" -ge 2 ] || die "usage: ./prep-mirror.sh <ssh-url> <ssh-key> [--check] [--proto http|https]" 2
SSH_URL="${_pos[0]}"; SSH_KEY="${_pos[1]}"
[ -f "$SSH_KEY" ] || die "SSH key not found: ${SSH_KEY}" 2
case "$PROTO" in http|https) ;; *) die "--proto must be http or https (got '${PROTO}')" 2 ;; esac

# ── parse the ssh url → user / host / root ──────────────────────────────
# ssh://user@host/path  (path is absolute on the remote)
case "$SSH_URL" in
    ssh://*) ;;
    *) die "ssh-url must start with ssh:// (got '${SSH_URL}')" 2 ;;
esac
_authpath="${SSH_URL#ssh://}"            # user@host/path
case "$_authpath" in
    */*) ;;
    *) die "ssh-url must include a root path (e.g. ssh://user@host/home/user/asgard)" 2 ;;
esac
_auth="${_authpath%%/*}"                  # user@host
_path="/${_authpath#*/}"                  # /path  (leading slash restored)
case "$_auth" in
    *@*) SSH_USER="${_auth%@*}"; SSH_HOST="${_auth#*@}" ;;
    *)   SSH_USER=''; SSH_HOST="$_auth" ;;
esac
[ -n "$SSH_HOST" ] || die "could not parse host from ssh-url '${SSH_URL}'" 2
[ "$_path" != "/" ] || die "ssh-url must include a root path (e.g. /home/ubuntu/asgard)" 2
ROOT="${_path%/}"
COORD_ROOT="${ROOT}-coord"
BASENAME="$(basename "$ROOT")"
URL_PATH="/${BASENAME}"
PUBLIC_URL="${PROTO}://${SSH_HOST}${URL_PATH}"
SSH_TARGET="${SSH_USER:+${SSH_USER}@}${SSH_HOST}"
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes)

banner
step "Target"
info "ssh-url    ${SSH_URL}"
info "host       ${SSH_HOST}   user ${SSH_USER:-<default>}"
info "root       ${ROOT}"
info "coord      ${COORD_ROOT}"
info "served at  ${PUBLIC_URL}/   (location ${URL_PATH}/)"
[ "$DRY" -eq 1 ] && warn "--check: DRY RUN — nothing on the remote will change"

# ── connectivity ────────────────────────────────────────────────────────
step "Connectivity"
if ! ssh "${SSH_OPTS[@]}" "$SSH_TARGET" 'echo ok' >/dev/null 2>&1; then
    die "cannot SSH to ${SSH_TARGET} with key ${SSH_KEY} (check key, user, host, firewall)"
fi
ok "SSH to ${SSH_TARGET} works"

# ── probe remote state (read-only) ──────────────────────────────────────
# Emits exactly one line:  STATE=<PREPARED|ADOPT|FRESH|UNEXPECTED> [detail]
step "Probing remote state"
PROBE="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" 'bash -s' -- "$ROOT" "$COORD_ROOT" <<'REMOTE'
set -eu
ROOT="$1"; COORD="$2"; MARKER="${ROOT}/mirror-info.json"
if [ -e "$ROOT" ] && [ ! -d "$ROOT" ]; then
    echo "STATE=UNEXPECTED root exists but is not a directory: ${ROOT}"; exit 0
fi
if [ -f "$MARKER" ]; then
    if grep -q '"marker"[[:space:]]*:[[:space:]]*"asgard-mirror"' "$MARKER" 2>/dev/null; then
        echo "STATE=PREPARED $(tr -d '\n' < "$MARKER")"
    else
        echo "STATE=UNEXPECTED ${MARKER} exists but is not an asgard-mirror marker"
    fi
    exit 0
fi
if [ -d "${ROOT}/dists" ]; then
    echo "STATE=ADOPT existing repo at ${ROOT}/dists (no marker yet)"; exit 0
fi
if [ -d "$ROOT" ] && [ -n "$(ls -A "$ROOT" 2>/dev/null || true)" ]; then
    echo "STATE=UNEXPECTED ${ROOT} is non-empty but has no dists/ and no marker — refusing to touch it"
    exit 0
fi
echo "STATE=FRESH ${ROOT} absent or empty"
REMOTE
)"
STATE="${PROBE#STATE=}"; STATE_KIND="${STATE%% *}"; STATE_DETAIL="${STATE#* }"
case "$STATE_KIND" in
    PREPARED)
        ok "already prepared"
        info "${STATE_DETAIL}"
        step "Verifying it serves"
        if curl -fsS "${PUBLIC_URL}/mirror-info.json" >/dev/null 2>&1; then
            ok "${PUBLIC_URL}/mirror-info.json reachable — ready for \`mirror add ${SSH_URL} --proto ${PROTO}\`"
        else
            warn "marker present but ${PUBLIC_URL}/mirror-info.json is not HTTP-reachable"
            warn "the web server may be down or not serving ${URL_PATH}/ — investigate before \`mirror add\`"
        fi
        exit 0 ;;
    UNEXPECTED)
        die "UNEXPECTED remote state — ${STATE_DETAIL}.  Refusing to modify the host.  Inspect it by hand, then re-run."
        ;;
    ADOPT) ok "adopting an existing repo (will ensure dirs + serving, write marker)" ;;
    FRESH) ok "fresh host — will install the web server + create the layout" ;;
    *) die "could not classify remote state (got: ${PROBE:-<empty>})" ;;
esac

if [ "$DRY" -eq 1 ]; then
    step "DRY RUN — planned actions (${STATE_KIND})"
    info "mkdir -p ${ROOT}/{dists,iso} ${COORD_ROOT}/{claims,keyring/builders}"
    [ "$STATE_KIND" = FRESH ] && info "apt-get install -y nginx rsync; configure ${URL_PATH}/ → ${ROOT}/ (autoindex)"
    [ "$STATE_KIND" = ADOPT ] && info "ensure nginx serves ${URL_PATH}/ (configure only if it doesn't already)"
    info "write marker ${ROOT}/mirror-info.json"
    warn "no changes made (--check)"
    exit 0
fi

# ── apply ───────────────────────────────────────────────────────────────
step "Preparing ${SSH_HOST} (${STATE_KIND})"
APPLY_OUT="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" 'bash -s' -- \
    "$ROOT" "$COORD_ROOT" "$URL_PATH" "$STATE_KIND" "$PROTO" <<'REMOTE'
set -eu
ROOT="$1"; COORD="$2"; URL_PATH="$3"; KIND="$4"; PROTO="$5"
SUDO=''; [ "$(id -u)" -ne 0 ] && SUDO='sudo'
say() { echo "REMOTE: $*"; }

# 1. dir layout (idempotent)
mkdir -p "${ROOT}/dists" "${ROOT}/iso" \
         "${COORD}/claims" "${COORD}/keyring/builders"
say "dir layout ensured under ${ROOT} and ${COORD}"

# 2. web server — only (re)configure if it isn't already serving the path
serves_ok() { curl -fsS "http://localhost${URL_PATH}/" >/dev/null 2>&1; }
if serves_ok; then
    say "web server already serves ${URL_PATH}/ — leaving config untouched"
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
    # Dedicated default server; remove the stock default site so this one
    # answers the host's IP.  Only the symlink is removed (the file stays
    # in sites-available), and only when it's the stock default.
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
    say "configured nginx ${URL_PATH}/ → ${ROOT}/ and reloaded"
fi

# 3. marker (written LAST, after the layout + serving are in place)
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "${ROOT}/mirror-info.json" <<JSON
{
  "marker": "asgard-mirror",
  "schema": 1,
  "prepared_at": "${NOW}",
  "root": "${ROOT}",
  "coord_root": "${COORD}",
  "url_path": "${URL_PATH}",
  "web_server": "nginx",
  "adopted": $([ "$KIND" = ADOPT ] && echo true || echo false)
}
JSON
say "wrote marker ${ROOT}/mirror-info.json"
REMOTE
)"
printf '%s\n' "$APPLY_OUT" | sed 's/^REMOTE: /   /'

# ── verify ──────────────────────────────────────────────────────────────
step "Verifying"
if curl -fsS "${PUBLIC_URL}/mirror-info.json" >/dev/null 2>&1; then
    ok "${PUBLIC_URL}/mirror-info.json is HTTP-reachable"
    ok "mirror prepared — register it with:"
    printf '\n     %smirror add %s --proto %s%s\n\n' "${_B}" "$SSH_URL" "$PROTO" "${_R}"
else
    warn "marker written but ${PUBLIC_URL}/mirror-info.json is not HTTP-reachable yet"
    warn "check the web server / firewall (port 80) on ${SSH_HOST}, then re-run with --check"
    exit 1
fi
