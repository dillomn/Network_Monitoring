#!/usr/bin/env bash
# diag.sh — collect everything Claude could possibly need to diagnose
# DrayTek SSH/CLI scraping issues. Outputs to a timestamped directory and
# tars it up. The router password is masked. Safe to run while the stack
# is up or down.
#
# Usage:
#   bash diag.sh                  # uses http://localhost:8090
#   APP_URL=http://1.2.3.4:8090 bash diag.sh

set -u
cd "$(dirname "$0")"

OUTDIR="/tmp/draymon-diag-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUTDIR"
APP_URL="${APP_URL:-http://localhost:8090}"

c_green() { printf "\033[32m%s\033[0m\n" "$*"; }
c_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
c_dim()   { printf "\033[2m%s\033[0m\n" "$*"; }
section() { echo; printf "\033[1;36m===== %s =====\033[0m\n" "$1"; }

write() {
  local file="$OUTDIR/$1"; shift
  "$@" 2>&1 | tee "$file"
}

fetch_json() {
  local name=$1 path=$2
  local file="$OUTDIR/api-$name.json"
  if curl -sk --max-time 30 -o "$file" -w "HTTP %{http_code}  %{size_download}B  %{time_total}s\n" \
       "$APP_URL$path" >"$OUTDIR/api-$name.meta" 2>&1; then
    cat "$OUTDIR/api-$name.meta"
    head -c 2000 "$file"; echo
    [ "$(wc -c <"$file")" -gt 2000 ] && c_dim "  … truncated (full body in $file)"
  else
    c_red "FAILED to reach $APP_URL$path"
  fi
}

fetch_raw() {
  local name=$1 path=$2
  local file="$OUTDIR/raw-$name.txt"
  curl -sk --max-time 30 -o "$file" "$APP_URL$path"
  if [ -s "$file" ]; then
    echo "($(wc -l <"$file") lines, $(wc -c <"$file") bytes saved to $file)"
    head -c 1500 "$file"; echo
  else
    c_red "no output from $APP_URL$path"
  fi
}

# --------------------------------------------------------------------------
section "SYSTEM"
write system.txt bash -c '
  echo "host:    $(hostname)"
  echo "uname:   $(uname -a)"
  if [ -f /etc/os-release ]; then
    grep -E "^(PRETTY_NAME|VERSION)=" /etc/os-release | sed "s/^/os:      /"
  fi
  echo "arch:    $(uname -m)"
  echo "docker:  $(docker --version 2>&1 || echo MISSING)"
  echo "compose: $(docker compose version 2>&1 | head -1 || echo MISSING)"
'

# --------------------------------------------------------------------------
section "REPO STATE"
write git.txt bash -c '
  if [ -d .git ]; then
    git log -1 --oneline
    echo
    git status -sb
    echo
    git remote -v
  else
    echo "not a git repo"
  fi
'

# --------------------------------------------------------------------------
section "ENV (PASSWORD MASKED)"
if [ -f .env ]; then
  sed -E "s/(ROUTER_SSH_PASSWORD\s*=\s*).*/\1***MASKED***/i" .env > "$OUTDIR/env.masked.txt"
  cat "$OUTDIR/env.masked.txt"
else
  echo "no .env file present" | tee "$OUTDIR/env.masked.txt"
fi

# --------------------------------------------------------------------------
section "DOCKER COMPOSE STATE"
write compose-ps.txt docker compose ps

# --------------------------------------------------------------------------
section "DRAYMON LOGS (last 300 lines)"
docker compose logs --no-color --tail=300 draymon >"$OUTDIR/draymon.log" 2>&1 || true
if [ -s "$OUTDIR/draymon.log" ]; then
  tail -n 60 "$OUTDIR/draymon.log"
  c_dim "  (full 300 lines in $OUTDIR/draymon.log)"
else
  c_red "no log output — is the container running?"
fi

# --------------------------------------------------------------------------
section "ROUTER REACHABILITY"
write connectivity.txt bash -c '
  ROUTER_HOST=$(grep -E "^ROUTER_HOST=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "[:space:]\"")
  ROUTER_SSH_PORT=$(grep -E "^ROUTER_SSH_PORT=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "[:space:]\"")
  ROUTER_HOST=${ROUTER_HOST:-192.168.1.1}
  ROUTER_SSH_PORT=${ROUTER_SSH_PORT:-22}
  echo "router host: $ROUTER_HOST  ssh port: $ROUTER_SSH_PORT"
  echo
  echo "--- ping ---"
  ping -c 2 -W 2 "$ROUTER_HOST" 2>&1 | tail -5
  echo
  echo "--- tcp connect to ssh port ---"
  if command -v nc >/dev/null; then
    nc -z -w 3 "$ROUTER_HOST" "$ROUTER_SSH_PORT" && echo "open" || echo "closed/filtered"
  else
    timeout 3 bash -c "</dev/tcp/$ROUTER_HOST/$ROUTER_SSH_PORT" 2>&1 \
      && echo "open" || echo "closed/filtered"
  fi
'

# --------------------------------------------------------------------------
section "APP API: HEALTH"
fetch_json health /api/health

section "APP API: DEVICES (head)"
fetch_json devices /api/devices
if [ -f "$OUTDIR/api-devices.json" ] && command -v python3 >/dev/null; then
  python3 -c "import json; print('devices count:', len(json.load(open('$OUTDIR/api-devices.json'))))" 2>/dev/null || true
fi

# --------------------------------------------------------------------------
section "DEBUG: SSH INFO"
fetch_json ssh-info /debug/ssh/info

section "DEBUG: SSH DEVICES"
fetch_json ssh-devices /debug/ssh/devices

section "DEBUG: SSH FLOW (all known IPs)"
fetch_json ssh-flow /debug/ssh/flow

section "DEBUG: WAN COUNTERS"
fetch_json ssh-wan /debug/ssh/wan

section "DEBUG: RAW sys version"
fetch_raw sys-version "/debug/ssh/exec?cmd=sys+version"

section "DEBUG: RAW srv dhcp status"
fetch_raw srv-dhcp "/debug/ssh/exec?cmd=srv+dhcp+status"

section "DEBUG: RAW ip arp status"
fetch_raw ip-arp "/debug/ssh/exec?cmd=ip+arp+status"

section "DEBUG: RAW show statistic"
fetch_raw show-statistic "/debug/ssh/exec?cmd=show+statistic"

# --------------------------------------------------------------------------
section "BUNDLE"
TAR="$OUTDIR.tar.gz"
tar -czf "$TAR" -C /tmp "$(basename "$OUTDIR")"
c_green "Bundle written: $TAR"
echo "  size: $(du -h "$TAR" | cut -f1)"
echo "  contents:"
tar -tzf "$TAR" | sed 's/^/    /'
echo
c_green "Share this file (or paste anything from $OUTDIR that looks relevant)."
c_dim "The router password is masked, but skim env.masked.txt before sending."
