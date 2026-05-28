# DrayTek Network Monitor

Polls a DrayTek Vigor router over SSH for per-device bandwidth and serves a live device list + historical graphs from a single Docker container on a Raspberry Pi.

Built and tested against the **Vigor 2762n** and **Vigor 2765 series**.

## What it does

- Logs into the DrayTek over SSH (default every 1s)
- Reads `srv dhcp status` + `ip arp status` for the device list
- Reads `show traffic <ip> tx|rx` per device for live bandwidth
- Reads `show statistic` and computes live per-WAN bps from byte-counter deltas between polls
- Stores samples in SQLite (default 30-day retention)
- Web UI at `http://<pi-ip>:8090` with a sortable device list, live WAN totals, and historical graphs

## Prerequisites

- Raspberry Pi running 64-bit PiOS (or any Linux host with Docker)
- Docker: `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER`
- LAN reach from the Pi to the DrayTek's SSH port

## DrayTek setup (one-time)

1. **Enable SSH** — *System Maintenance → Management → tick "SSH" → Apply.*
   Verify from the Pi: `ssh admin@<router-ip>` should land at `DrayTek>`.
2. **Enable Data Flow Monitor** — *Diagnostics → Data Flow Monitor → tick "Enable Data Flow Monitor" → OK.*
   Without this, `show traffic <ip>` returns all zeros.
3. *(Recommended)* Create a dedicated read-only user under *System Maintenance → Administrator Password / Management Account* rather than running as `admin`. SSH key auth is not supported on Vigors — password only.

## Configure and run

```bash
cp .env.example .env
nano .env       # fill in ROUTER_HOST, ROUTER_SSH_USER, ROUTER_SSH_PASSWORD
docker compose up -d --build
```

Open <http://`pi-ip`:8090>. After editing `.env`: `docker compose restart`.

## .env reference

| Var | Default | Meaning |
|---|---|---|
| `ROUTER_HOST` | `192.168.1.1` | DrayTek LAN IP |
| `ROUTER_SSH_PORT` | `22` | SSH port |
| `ROUTER_SSH_USER` | `admin` | SSH username |
| `ROUTER_SSH_PASSWORD` | *(required)* | SSH password. Wrap in single quotes if it contains `$`, `#`, or `!` |
| `POLL_INTERVAL` | `1` | Seconds between poll cycles |
| `RETENTION_DAYS` | `30` | Days of history to keep |
| `TRAFFIC_UNIT` | `kilobits_per_second` | Unit of the integers inside `show traffic <ip>`. See Calibration |

## Calibration

`show traffic <ip>` returns an integer time-series whose unit varies by firmware:

| Vigor model | `TRAFFIC_UNIT` |
|---|---|
| 2762n | `bytes_per_minute` |
| 2765 series (default) | `kilobits_per_second` |

To verify on your firmware:

1. Hit `http://<pi-ip>:8090/debug/ssh/raw-traffic?ip=192.168.1.10`.
2. Compare each row in the `interpretations` block to the value shown on the router's *Diagnostics → Data Flow Monitor* page for the same IP.
3. Set `TRAFFIC_UNIT` to the matching key in `.env` and `docker compose restart`.

## Troubleshooting

- **Devices list empty / `show traffic` all zeros** — tick *Diagnostics → Data Flow Monitor → Enable* on the router.
- **`Permission denied` on SSH** — test manually: `ssh <user>@<router>`. If the password contains `$#!`, wrap the whole `.env` value in single quotes.
- **`kex_exchange failed` / `no matching cipher`** — add the algorithm DrayTek negotiates to `LEGACY_SSH_KWARGS` in [app/collectors/ssh.py](app/collectors/ssh.py).
- **Bandwidth numbers look wrong** — re-run Calibration; the unit varies by firmware.

A handful of `/debug/ssh/*` endpoints exist for inspecting raw CLI output and unit interpretations — see [app/main.py](app/main.py). All share the poller's single SSH session via an asyncio lock; the DrayTek embedded SSH stack misbehaves under concurrent connections.

## Docker cheat sheet

```bash
docker compose up -d --build              # build + start (or update after pulling)
docker compose ps                         # status
docker compose logs -f draymon            # tail logs
docker compose restart                    # apply .env changes
docker compose down                       # stop, keep data
docker compose down -v                    # stop AND wipe SQLite (careful)
```

Data lives in the `draymon_draymon_data` Docker volume (`docker volume inspect draymon_draymon_data`).

## References

DrayTek-side behaviour the code depends on:

- [DrayTek — How do I use the Data Flow Monitor?](https://www.draytek.co.uk/support/guides/kb-vigor-dataflowmonitor) — TX rate and RX rate are reported in **kbps** (drives the default `TRAFFIC_UNIT`).
- [DrayTek 2710 Telnet Guide (whirlpool.net.au)](https://whirlpool.net.au/wiki/telnet_guide_01) — `traffic [wan1/wan2] [tx/rx]` CLI form that the per-IP `show traffic <ip>` is built on.
- [DrayTek Telnet Commands for DrayOS Routers (PDF, v1.4)](https://www.i-lan.net.au/dfaq/DrayTek/misc/DrayTek_Telnet%20Commands%20V1.4.pdf) — CLI reference for `srv dhcp status`, `ip arp status`, `sys version`, `show statistic`.
