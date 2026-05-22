#!/usr/bin/env bash
# diag.sh — collect everything Claude could possibly need to diagnose
# DrayTek scraping issues. Outputs to a timestamped directory and tars
# it up. The router password is masked. Safe to run while the stack is
# up or down.
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

# Save & echo a section to the bundle and stdout.
write() {
  local file="$OUTDIR/$1"; shift
  "$@" 2>&1 | tee "$file"
}

# Save full output to the bundle, only show the head on stdout.
write_head() {
  local file="$OUTDIR/$1"; shift
  local limit="${LIMIT:-60}"
  "$@" >"$file" 2>&1
  if [ -s "$file" ]; then
    head -n "$limit" "$file"
    local lines; lines=$(wc -l <"$file")
    if [ "$lines" -gt "$limit" ]; then
      c_dim "  … truncated ($lines lines, full output in $file)"
    fi
  else
    c_dim "  (empty)"
  fi
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
    echo "--- HEAD ---"
    git log -1 --oneline 2>&1
    echo
    echo "--- status ---"
    git status -sb 2>&1
    echo
    echo "--- remote ---"
    git remote -v 2>&1
  else
    echo "not a git repo"
  fi
  echo
  echo "--- key code markers (each should be >=1) ---"
  for marker in \
      "All login strategies failed" \
      "Cached sFormAuthStr token" \
      "TOKEN_PATTERNS" \
      "Discovered management URL"; do
    n=$(grep -c "$marker" app/draytek.py 2>/dev/null || echo 0)
    printf "  %-35s %s\n" "$marker" "$n"
  done
'

# --------------------------------------------------------------------------
section "ENV (PASSWORD MASKED)"
if [ -f .env ]; then
  sed -E 's/(ROUTER_PASSWORD\s*=\s*).*/\1***MASKED***/i' .env > "$OUTDIR/env.masked.txt"
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
  ROUTER_HOST=${ROUTER_HOST:-192.168.1.1}
  echo "router host: $ROUTER_HOST"
  echo
  echo "--- ping ---"
  ping -c 2 -W 2 "$ROUTER_HOST" 2>&1 | tail -5
  echo
  echo "--- HTTP GET / (follow redirects, ignore cert) ---"
  curl -skL -o /dev/null -w "final_url: %{url_effective}\nstatus:    %{http_code}\nredirects: %{num_redirects}\n" \
       "http://$ROUTER_HOST/" 2>&1
'

# --------------------------------------------------------------------------
section "APP API: HEALTH"
fetch_json health /api/health

section "APP API: DEVICES (count)"
fetch_json devices /api/devices
# count for quick read
if [ -f "$OUTDIR/api-devices.json" ] && command -v python3 >/dev/null; then
  python3 -c "import json; print('devices count:', len(json.load(open('$OUTDIR/api-devices.json'))))" 2>/dev/null || true
fi

# --------------------------------------------------------------------------
section "DEBUG: TOKEN"
fetch_json token /debug/token

section "DEBUG: LOGIN STRATEGIES"
fetch_json login /debug/login

section "DEBUG: SPA + JS DISCOVERY"
fetch_json discover /debug/discover

section "DEBUG: RAW DHCP HTML (head only on stdout)"
fetch_json raw-dhcp "/debug/raw?page=dhcp"

section "DEBUG: RAW FLOW HTML (head only on stdout)"
fetch_json raw-flow "/debug/raw?page=flow"

section "DEBUG: PARSED DHCP"
fetch_json parsed-dhcp "/debug/parsed?page=dhcp"

section "DEBUG: PARSED FLOW"
fetch_json parsed-flow "/debug/parsed?page=flow"

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
