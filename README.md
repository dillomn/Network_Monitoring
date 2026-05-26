# DrayTek Network Monitor

A self-contained web app for the Raspberry Pi that polls a **DrayTek Vigor router** over SSH for per-device bandwidth and presents a live list + historical graphs. No port mirroring, no SNMP, no Grafana — one Docker container, one web UI.

Built and tested against the **Vigor 2762n** and **Vigor 2765 series**. Should work on other Vigor models with little or no tweaking — the CLI surface is stable across DrayTek's product line.

![arch](https://img.shields.io/badge/python-3.12-blue) ![arch](https://img.shields.io/badge/sqlite-bundled-green) ![arch](https://img.shields.io/badge/pi-arm64-red)

## What it does

- Logs into the DrayTek over **SSH** on a schedule (default every 5s)
- Runs `srv dhcp status` + `ip arp status` to learn which devices are on-LAN
- Runs `show traffic <ip> tx|rx` per device to read per-IP bandwidth (the same data the router's Data Flow Monitor displays, but pulled from a stable CLI surface instead of HTML)
- Stores samples in a local SQLite database (default 30-day retention)
- Serves a web UI at `http://<pi-ip>:8090`:
  - Sortable device list with live TX/RX rates
  - Click any device → modal with a big graph, vendor lookup, notes
  - Filter by name / IP / MAC

## Why SSH and not the web UI?

The previous version scraped the router's HTML admin pages. That broke whenever DrayTek shipped a new firmware UI. The CLI is the most stable surface DrayTek ships — `show arp`, `srv dhcp status`, `show traffic` have worked the same way for years across the 2762, 2765, and friends. No HTML parsing, no CSRF tokens, no session juggling.

## Prerequisites on the Pi

1. **PiOS 64-bit** (Bullseye or Bookworm). 32-bit also works — the image is multi-arch.
2. **Docker**:
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   # log out / back in
   ```
3. Get this folder onto the Pi (e.g. `git clone <repo> ~/Desktop/network_monitoring`).
4. The Pi needs LAN reach to the DrayTek's IP on the SSH port (default 22).

## DrayTek setup (one-time)

Three things in the DrayTek web UI:

### 1. Enable SSH

**System Maintenance → Management → Allow Management from the Internet** *(or LAN, depending on firmware) → tick "SSH" → set port (default 22) → Apply.*

Confirm from the Pi:
```bash
ssh admin@192.168.1.1
# password prompt → enter admin password → you should see DrayTek>
exit
```

### 2. (Optional but recommended) Dedicated user

**System Maintenance → Administrator Password / Management Account** — add a user like `monitor` with a strong password rather than running as `admin`. Restrict to read-only if your firmware supports it. SSH key auth is **not** supported on these Vigors — password only.

### 3. Make sure Data Flow Monitor is enabled

**Diagnostics → Data Flow Monitor → tick "Enable Data Flow Monitor" → OK.**

Without this, the router records no per-IP bandwidth and the `show traffic <ip>` command returns all zeros.

## Configure and run

```bash
cp .env.example .env
nano .env       # set ROUTER_HOST, ROUTER_SSH_USER, ROUTER_SSH_PASSWORD
docker compose up -d --build
docker compose logs -f draymon
```

First image build takes ~3 minutes on a Pi 4. After that, container starts in seconds.

Open <http://`pi-ip`:8090> in any browser.

A healthy log on startup looks like:
```
INFO draymon: Poller started (router=192.168.1.1 ssh:22 every 5s)
INFO app.poller: Connected to Vigor2762n (firmware 3.9.9.5_MDM1)
```

## .env reference

| Var | Default | Meaning |
|---|---|---|
| `ROUTER_HOST` | `192.168.1.1` | DrayTek LAN IP |
| `ROUTER_SSH_PORT` | `22` | SSH port — must match what you set in *System Maintenance → Management* |
| `ROUTER_SSH_USER` | `admin` | SSH username |
| `ROUTER_SSH_PASSWORD` | *(required)* | SSH password. Wrap in single quotes if it contains `$`, `#`, or `!`. |
| `POLL_INTERVAL` | `5` | Seconds between poll cycles. SSH session is persistent across polls, so 5s is cheap. |
| `RETENTION_DAYS` | `30` | Days of bandwidth history to keep |
| `TRAFFIC_UNIT` | `bits_per_second` | How to interpret values from `show traffic <ip>`. See Calibration below for the available units and how to pick one. |

After editing `.env`: `docker compose restart`.

## Diagnostics

### One-shot diagnostic bundle

```bash
bash diag.sh
```

Collects: system info, git state, config (password masked), container logs, router reachability, every debug endpoint, and bundles it all into `/tmp/draymon-diag-<timestamp>.tar.gz` you can share. Run this **first** whenever something isn't working — it's the fastest path to a fix.

### Manual debug endpoints

| URL | Purpose |
|---|---|
| `/api/health` | Last poll time, status, last error, router model |
| `/debug/ssh/info` | Connects, returns model + firmware. Sanity check for credentials/algorithms. |
| `/debug/ssh/devices` | Live device list parsed from `srv dhcp status` + `ip arp status` (bypasses the DB). |
| `/debug/ssh/flow` | Live per-IP bandwidth as the collector sees it. Add `?ip=192.168.1.10` to poll one host. |
| `/debug/ssh/wan` | Per-WAN lifetime byte counters (`show statistic`). |
| `/debug/ssh/exec?cmd=...` | Run an arbitrary (allowlisted) CLI command. Useful when probing unknown firmware output. |
| `/debug/calibrate?ip=192.168.1.10&wait_s=60` | Confirms the `show traffic` time-series interpretation — see "Calibration" below. |

Hit them with `curl -s http://localhost:8090/debug/...`.

## Calibration (one-time, optional)

The `show traffic <ip> rx` command returns a time-series of integers. **The
unit varies by firmware:**

| Vigor model / firmware  | `TRAFFIC_UNIT` value     |
|-------------------------|--------------------------|
| 2762n (original)        | `bytes_per_minute`       |
| 2765 series             | `bits_per_second` (default) |
| Other (varies)          | `bytes_per_second`, `kilobits_per_second`, `kilobytes_per_second` |

### Picking the right unit

1. Find an idle-ish device and one with known traffic (e.g. running a speed test).
2. Hit `http://<pi-ip>:8090/debug/ssh/raw-traffic?ip=192.168.1.10` — it
   returns the smoothed raw sample for both TX and RX, plus what every
   supported unit would compute. Pick the row whose `tx_bps` / `rx_bps`
   matches the DrayTek's *Diagnostics → Data Flow Monitor* page for the
   same IP, and set `TRAFFIC_UNIT` to that key in `.env`.
3. `docker compose restart`.

### Time-series position calibration

The code also assumes the **last** value in the array is the most recent
sample. To confirm, generate steady traffic on one device and run:

```bash
curl 'http://localhost:8090/debug/calibrate?ip=192.168.1.10&wait_s=90'
```

The response reports which array positions changed during the wait. If
they cluster near the END, the assumption holds.

## Troubleshooting

### `Router login failed` / `Permission denied (publickey,password)`

1. Confirm SSH is enabled on the router (Step 1 in *DrayTek setup*).
2. Try logging in manually from the Pi: `ssh admin@<router-ip>`. If that fails, the app will too.
3. Confirm `ROUTER_SSH_USER` and `ROUTER_SSH_PASSWORD` in `.env`.
4. If your password contains `$`, `#`, `!`, etc., wrap the whole value in single quotes: `ROUTER_SSH_PASSWORD='m#y$pass!'`.

### `kex_exchange failed` / `no matching cipher` on connect

Older firmware negotiates older SSH algorithms. The code already widens the cipher/kex/MAC lists, but if a very old build still won't negotiate, add the algorithm name to `LEGACY_SSH_KWARGS` in [app/collectors/ssh.py](app/collectors/ssh.py).

### Devices list is empty

1. `/debug/ssh/info` — does it report a model? If not, SSH isn't connecting; see above.
2. `/debug/ssh/devices` — returns `[]`? Check the raw output:
   ```bash
   curl 'http://localhost:8090/debug/ssh/exec?cmd=srv+dhcp+status'
   curl 'http://localhost:8090/debug/ssh/exec?cmd=ip+arp+status'
   ```
   If the raw output has device rows but they're not being parsed, paste the output and we'll adjust `parse_dhcp` / `parse_arp` in [app/parsers/cli.py](app/parsers/cli.py).

### Bandwidth graphs stay flat at zero even though devices appear

Did you tick **"Enable Data Flow Monitor"** in *Diagnostics → Data Flow Monitor*? It's off by default. Without it the router records nothing.

If it's enabled and `show traffic` still returns all zeros, run the calibration test above — the time-series interpretation might need adjusting for your firmware.

### `show traffic <ip>` numbers don't match the web UI's chart

Hit `/debug/ssh/raw-traffic?ip=<ip>` and compare its `interpretations`
block to what the DrayTek's *Data Flow Monitor* shows for the same IP.
Set `TRAFFIC_UNIT` in `.env` to whichever row matches. Symptoms:

- Numbers ~60× too low → you're on `bytes_per_minute` but should be `bytes_per_second`.
- Numbers ~8× too low → you're on `bytes_per_second` but should be `bits_per_second`.
- Numbers ~1000× too low → switch to `kilobits_per_second` / `kilobytes_per_second`.

### WiFi clients don't appear

They should. The DrayTek's DHCP table + ARP table include WiFi clients and the per-IP traffic command works on them too. If wired devices appear but WiFi doesn't, paste me the output of `/debug/ssh/exec?cmd=srv+dhcp+status` — usually the WiFi rows are formatted slightly differently than wired.

### Finding the bad device

Once the device list is populated:

1. UI → sort by **TX** or **RX** — the device at the top is the heaviest talker right now
2. Click a device → modal opens with the last 24h chart; switch window to 7 days for trends
3. Cross-reference with the DrayTek's *Diagnostics → Data Flow Monitor* page (live, includes WiFi) when you spot a spike

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
| (SSH on :22)           |
+----------+-------------+
           ^  SSH session every 5s:
           |  - sys version
           |  - srv dhcp status
           |  - ip arp status
           |  - show traffic <ip> tx/rx  (per known IP)
           |  - show statistic
           |
+----------+-------------+
| Pi 4 — Docker container|
|  +------------------+  |
|  | FastAPI + uvicorn|  |  port 8090
|  | +--------------+ |  |
|  | | poller task  | |  |
|  | | DraytekCol-  | |  |
|  | |   lector     | |  |
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
├── diag.sh                       # one-shot diagnostic bundle
├── README.md
└── app/
    ├── main.py                   # FastAPI app + /api + /debug routes
    ├── config.py                 # env-driven settings
    ├── db.py                     # SQLite schema + helpers
    ├── poller.py                 # async background poll loop
    ├── oui.py                    # built-in MAC vendor lookup
    ├── collectors/
    │   ├── base.py               # Device / FlowSample dataclasses
    │   └── ssh.py                # DrayTek SSH session + collector
    ├── parsers/
    │   └── cli.py                # pure functions for CLI output parsing
    └── static/                   # device list UI (HTML + JS + CSS + Chart.js)
```
