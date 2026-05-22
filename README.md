# DrayTek Network Monitor

A self-contained web app for the Raspberry Pi that polls a **DrayTek Vigor router** for per-device bandwidth and presents a live list + historical graphs. No port mirroring, no SNMP, no Grafana — one Docker container, one web UI.

Built and tested against the **Vigor 2765 series** (firmware-specific quirks noted below). Should work on other Vigor models with minor tweaks to the scraper paths.

![arch](https://img.shields.io/badge/python-3.12-blue) ![arch](https://img.shields.io/badge/sqlite-bundled-green) ![arch](https://img.shields.io/badge/pi-arm64-red)

## What it does

- Logs into the DrayTek's web UI on a schedule (default every 10s)
- Scrapes the **DHCP Table** for device identity (IP, MAC, hostname)
- Scrapes the **Data Flow Monitor** for per-IP bandwidth (TX + RX)
- Stores samples in a local SQLite database (default 30-day retention)
- Serves a web UI at `http://<pi-ip>:8090`:
  - Sortable device list with live TX/RX rates
  - Click any device → modal with a big graph, vendor lookup, notes
  - Filter by name / IP / MAC

## Prerequisites on the Pi

1. **PiOS 64-bit** (Bullseye or Bookworm). 32-bit will also work — the image is multi-arch.
2. **Docker**:
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   # log out / back in
   ```
3. Get this folder onto the Pi (e.g. `git clone <repo> ~/Desktop/network_monitoring`).
4. The Pi needs network reach to the DrayTek's LAN IP. Plug it into any LAN port.

## DrayTek setup (one-time)

Two things in the DrayTek web UI:

### 1. Create a dedicated user (optional but recommended)

**System Maintenance → Administrator Password / Management Account** — add a user like `monitor` with a strong password rather than running as `admin`. Restrict to read-only if your firmware supports it.

### 2. Enable Data Flow Monitor

**Diagnostics → Data Flow Monitor → tick "Enable Data Flow Monitor" → click OK/Apply.**

This is **required**. Without it, the router records no per-IP bandwidth and the app's "Top Talker" chart stays empty.

That's it. No SNMP, no port mirror, no other router-side config needed.

## Configure and run

```bash
cp .env.example .env
nano .env       # set ROUTER_HOST, ROUTER_USER, ROUTER_PASSWORD
docker compose up -d --build
docker compose logs -f draymon
```

First image build takes ~3 minutes on a Pi 4. After that, container starts in seconds.

Open <http://`pi-ip`:8090> in any browser.

A healthy log on startup looks like:
```
INFO app.draytek: Discovered management URL: http://192.168.1.1 -> https://192.168.1.1:4441
INFO app.draytek: Authenticated via form POST at /cgi-bin/wlogin.cgi
INFO app.draytek: Cached sFormAuthStr token (15 chars) from /cgi-bin/menu.htm
INFO draymon: Poller started (router=192.168.1.1 every 10s)
```

## .env reference

| Var | Default | Meaning |
|---|---|---|
| `ROUTER_HOST` | `192.168.1.1` | DrayTek LAN IP. Auto-discovery handles the HTTPS port (e.g. `:4441`); you can also hardcode it here, e.g. `192.168.1.1:4441`. |
| `ROUTER_USER` | `admin` | Username for router login |
| `ROUTER_PASSWORD` | *(required)* | Router password. Wrap in single quotes if it contains `$`, `#`, or `!`. |
| `ROUTER_SCHEME` | `http` | `http` or `https`. The app auto-discovers if the router redirects, so leaving this as `http` is fine. |
| `ROUTER_VERIFY_SSL` | `0` | `1` to require valid cert (most DrayTeks are self-signed) |
| `POLL_INTERVAL` | `10` | Seconds between scrapes. 5–30 is sensible. |
| `RETENTION_DAYS` | `30` | Days of bandwidth history to keep |

After editing `.env`: `docker compose restart`.

## How the router auth works (so you can debug it)

Modern Vigor firmware (2765 and similar) has three quirks the app handles automatically:

1. **HTTPS on a non-standard port.** The router redirects HTTP traffic to e.g. `https://192.168.1.1:4441`. The app probes `GET /` once at startup and uses whatever URL it lands on as the base for everything.
2. **Form-POST login with cookie session.** `POST /cgi-bin/wlogin.cgi` with base64-encoded credentials → router sets a `SESSION_ID_VIGOR` cookie. The app also tries HTTP Basic Auth as a fallback for older firmware.
3. **Per-session CSRF token `sFormAuthStr`.** Every data CGI requires this token in the query string. The app extracts it from the post-login dashboard HTML and caches it.

If any of these breaks on your firmware build, the debug endpoints (below) will show you what the router returned.

## Diagnostics

### One-shot diagnostic bundle

```bash
bash diag.sh
```

Collects: system info, git state, config (password masked), container logs, router reachability, every debug endpoint, and bundles it all into `/tmp/draymon-diag-<timestamp>.tar.gz` you can share. Run this **first** whenever something isn't working — it's the fastest path to a fix.

### Manual debug endpoints

| URL | Purpose |
|---|---|
| `/api/health` | Last poll time, status, last error |
| `/debug/token` | Confirms the `sFormAuthStr` token we cached (should be ~15 chars; `0` means extraction failed) |
| `/debug/login` | Reports what every login strategy sent and what the router responded with |
| `/debug/discover` | Walks the router's SPA + JS bundles for `cgi-bin/v2/...` API references and probes each |
| `/debug/raw?page=dhcp` | Raw HTML of the DHCP Table page the scraper sees |
| `/debug/raw?page=flow` | Raw HTML of the Data Flow Monitor page |
| `/debug/parsed?page=dhcp` | What the DHCP parser made of the raw HTML |
| `/debug/parsed?page=flow` | What the flow parser made of the raw HTML |

All endpoints are read-only and require the app to be running. Hit them with `curl -sk http://localhost:8090/debug/<endpoint>`.

## Troubleshooting

### `Router login failed`

1. Run `bash diag.sh` and check `api-login.json` in the bundle.
2. Verify credentials by logging into the DrayTek web UI in a browser with the same user/password.
3. If your firmware uses 2FA on the admin account, disable it for the monitor user — scripted login can't handle 2FA.
4. If `.env`'s `ROUTER_PASSWORD` contains `$`, `#`, `!`, etc., wrap the whole value in single quotes: `ROUTER_PASSWORD='m#y$pass!'`.

### `/debug/token` returns `token_len: 0`

The app can't find `sFormAuthStr` in any of the dashboard pages it tries. Open `/cgi-bin/menu.htm` (or whatever URL has your DrayTek's left-side menu) in your browser, view source, search for `sFormAuthStr` — note which file/page it appears in. Add that path to `TOKEN_PAGES` in [app/draytek.py](app/draytek.py) and rebuild.

### Devices list is empty

1. `/debug/parsed?page=dhcp` → returns `[]`? The scraper isn't finding rows. Fetch `/debug/raw?page=dhcp` and compare to what your browser shows when you load the same page. If the HTML structure is unusual, the parser needs adjusting in `_parse_dhcp` / `_parse_dhcp_text`.
2. `/debug/raw?page=dhcp` → returns the "Authorization Error" page? Token issue — re-check `/debug/token`.
3. `/debug/raw?page=dhcp` → returns a 404 page? The DHCP table URL is at a different path on your firmware. Find the real URL in browser dev tools (Network tab) and add it to `DHCP_PATHS` in [app/draytek.py](app/draytek.py).

### Bandwidth / "Top Talker" graph stays empty even though devices appear

Did you tick **"Enable Data Flow Monitor"** in the DrayTek's Diagnostics page? It's off by default. Without it the router records nothing.

If it's enabled and `/debug/parsed?page=flow` is still `[]`, the flow page HTML may differ on your firmware. Fetch `/debug/raw?page=flow` and we'll adjust `_parse_flow` / `_parse_flow_text`.

### WiFi clients don't appear

They should. The DrayTek's DHCP Table includes WiFi clients and the Data Flow Monitor reports their traffic too — both are scraped the same way. If wired devices appear but WiFi doesn't, paste me the output of `/debug/raw?page=dhcp` — usually means the WiFi rows are formatted slightly differently than expected.

### Finding the bad device

Once the device list is populated:

1. UI → sort by **TX** or **RX** — the device at the top is the heaviest talker right now
2. Click a device → modal opens with the last 24h chart; switch window to 7 days for trends
3. Cross-reference with the DrayTek's *Diagnostics → Data Flow Monitor* page (live, includes WiFi clients) when you spot a spike

The app's strength is **historical data**: when the WAN crashes overnight, the chart will show which device was hammering it 5 minutes before the crash.

## Operations

```bash
docker compose ps
docker compose logs -f draymon
docker compose restart
docker compose down              # stop, keep data
docker compose down -v           # stop AND wipe SQLite (careful)
git pull && docker compose up -d --build   # update
```

Data lives in the `draymon_draymon_data` Docker volume (`docker volume inspect draymon_draymon_data`).

## Architecture

```
+------------------------+
| DrayTek Vigor router   |
| (web UI on :443/:4441) |
+----------+-------------+
           ^  HTTPS (login + scrape every 10s, sFormAuthStr token in URL)
           |
+----------+-------------+
| Pi 4 — Docker container|
|  +------------------+  |
|  | FastAPI + uvicorn|  |  port 8090
|  | +--------------+ |  |
|  | | poller task  | |  |
|  | | DraytekClient| |  |
|  | +------+-------+ |  |
|  |        v         |  |
|  |   SQLite (file)  |  |
|  +------------------+  |
+----------+-------------+
           ^  HTTP
           |
     [ Your browser ]
```

Everything in one Python process. No external dependencies at runtime beyond the DrayTek itself.

## Project layout

```
.
├── Dockerfile
├── compose.yaml
├── requirements.txt
├── .env.example
├── diag.sh                    # one-shot diagnostic bundle
├── README.md
└── app/
    ├── main.py                # FastAPI app + /api + /debug routes
    ├── config.py              # env-driven settings
    ├── db.py                  # SQLite schema + helpers
    ├── draytek.py             # router client: login, token, scrape, parse
    ├── poller.py              # async background poll loop
    ├── oui.py                 # built-in MAC vendor lookup
    └── static/                # device list UI (HTML + JS + CSS + Chart.js)
```
