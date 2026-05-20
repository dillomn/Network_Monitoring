# DrayTek Network Monitor

A self-contained web app for the Raspberry Pi that polls a **DrayTek 2765ax** router for per-device bandwidth and presents a live list + historical graphs. No port mirroring, no SNMP, no Grafana — one Docker container, one web UI.

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
3. Copy this folder onto the Pi (e.g. `/home/pi/draymon/`).
4. The Pi needs network reach to the DrayTek's LAN IP. Plug it into any LAN port.

## DrayTek setup (one-time)

You need a router login that the app can use. **Make a dedicated read-only user** rather than using `admin`:

1. Web UI → **System Maintenance → Administrator Password / Management Account**
2. Add a new user, e.g. `monitor`, with a strong password
3. Restrict its privileges to read-only if your firmware supports it

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

## .env reference

| Var | Default | Meaning |
|---|---|---|
| `ROUTER_HOST` | `192.168.1.1` | DrayTek LAN IP |
| `ROUTER_USER` | `admin` | Username for router login |
| `ROUTER_PASSWORD` | *(required)* | Router password |
| `ROUTER_SCHEME` | `http` | `http` or `https` |
| `ROUTER_VERIFY_SSL` | `0` | `1` to require valid cert (most DrayTeks are self-signed) |
| `POLL_INTERVAL` | `10` | Seconds between scrapes. 5–30 is sensible. |
| `RETENTION_DAYS` | `30` | Days of bandwidth history to keep |

After editing `.env`: `docker compose restart`.

## Troubleshooting

### "Router login failed" in the logs

Verify credentials by visiting the DrayTek web UI from another browser using the same username/password. If they work there but the app fails, your firmware may post-redirect the login form to a different path — see the next section.

### Device list is empty / bandwidth shows zero for everyone

DrayTek's HTML layout varies by firmware version. The scraper tries a few known paths but yours may be different. Use the debug endpoints to see what the router actually returned:

```bash
# Raw HTML of what we tried to parse:
curl http://<pi-ip>:8090/debug/raw?page=dhcp
curl http://<pi-ip>:8090/debug/raw?page=flow

# What the parser made of it:
curl http://<pi-ip>:8090/debug/parsed?page=dhcp
curl http://<pi-ip>:8090/debug/parsed?page=flow
```

If `/debug/raw` returns 404 ("no candidate URL responded"), the router's page is at a different path on your firmware. Find the real URL by browsing the DrayTek web UI with your browser's dev tools open (Network tab), then add it to `DHCP_PATHS` or `FLOW_PATHS` in [app/draytek.py](app/draytek.py).

If `/debug/raw` returns HTML but `/debug/parsed` is empty/wrong, share the HTML with me and I'll adjust the parser.

### "Login rejected by router (bad password?)"

Some firmware versions encode the login payload differently (with CSRF tokens, JS challenges, etc). If a plain user/pass POST doesn't authenticate, we'll need to capture a real browser login from dev tools and replay its exact form. Open an issue / message with the request payload.

### WiFi clients don't appear

They should. The DrayTek's DHCP table includes WiFi clients, and its Data Flow Monitor shows their bandwidth too. If wired devices appear but WiFi doesn't, it likely means the scraper's matching `IP` cells but missing the `MAC` cell on wireless rows — check `/debug/raw?page=flow` and `/debug/raw?page=dhcp`.

## Operations

```bash
docker compose ps
docker compose logs -f
docker compose restart
docker compose down              # stop, keep data
docker compose down -v           # stop AND wipe SQLite (careful)
docker compose pull && docker compose up -d --build   # update
```

Data lives in the `draymon_data` Docker volume (`docker volume inspect draymon_draymon_data`).

## Architecture

```
+------------------------+
| DrayTek 2765ax (router)|
+----------+-------------+
           ^  HTTP (login + scrape every 10s)
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
