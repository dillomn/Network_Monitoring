# DrayTek Network Monitor

Receives **NetFlow v9** from a DrayTek Vigor router for accurate per-IP bandwidth, polls the router over SSH for device discovery and WAN totals, and serves a live device list + historical graphs from a single Docker container on a Raspberry Pi.

Built and tested against the **Vigor 2762n** and **Vigor 2765 series**.

## What it does

- Listens on UDP/2055 for NetFlow v9 records from the router and credits each flow's bytes to the LAN-side IP — real packet-accurate per-device bandwidth, not a sampled buffer
- Logs into the DrayTek over SSH for `srv dhcp status` + `ip arp status` (device list, hostnames, vendor) and `show statistic` (per-WAN cumulative byte counters → live WAN bps via deltas)
- Stores samples in SQLite (default 30-day retention)
- Web UI at `http://<pi-ip>:8090` with sortable device list, live WAN totals, and historical graphs

## Prerequisites

- Raspberry Pi running 64-bit PiOS (or any Linux host with Docker)
- Docker: `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER`
- LAN reach from the Pi to the DrayTek's SSH port (TCP 22) and from the router to the Pi's NetFlow port (UDP 2055)

## DrayTek setup (one-time)

1. **Enable SSH** — *System Maintenance → Management → tick "SSH" → Apply.*
   Verify from the Pi: `ssh admin@<router-ip>` should land at `DrayTek>`.
2. **Enable NetFlow export** — *System Maintenance → NetFlow*:
   - tick **Enable**
   - **Collector IP** = the Pi's LAN IP
   - **Collector Port** = `2055`
   - **Version** = `v9`
   - **Active Timeout** = `60` seconds (lower = livelier UI updates; 60 is a good balance)
   - **Inactive Timeout** = `15` seconds (default is fine)
   - Click **OK**.
3. *(Recommended)* Create a dedicated read-only SSH user under *System Maintenance → Administrator Password / Management Account*. SSH key auth is not supported on Vigors — password only.

## Configure and run

```bash
cp .env.example .env
nano .env       # fill in ROUTER_HOST, ROUTER_SSH_USER, ROUTER_SSH_PASSWORD
docker compose up -d --build
```

Open <http://`pi-ip`:8090>. After editing `.env`: `docker compose restart`.

## Verifying NetFlow is flowing

After ~30 seconds of activity, hit:

```bash
curl http://<pi-ip>:8090/api/netflow/stats
```

You want to see `packets_received > 0`, `records_processed > 0`, and `last_packet_age_s` under 10 seconds. If `packets_received` stays at 0, the router isn't reaching the collector — usually a firewall on the Pi (`sudo ufw status`) or a wrong Collector IP in the router config.

## .env reference

| Var | Default | Meaning |
|---|---|---|
| `ROUTER_HOST` | `192.168.1.1` | DrayTek LAN IP |
| `ROUTER_SSH_PORT` | `22` | SSH port |
| `ROUTER_SSH_USER` | `admin` | SSH username |
| `ROUTER_SSH_PASSWORD` | *(required)* | SSH password. Wrap in single quotes if it contains `$`, `#`, or `!` |
| `POLL_INTERVAL` | `1` | Seconds between SSH polls (device discovery + WAN totals only) |
| `RETENTION_DAYS` | `30` | Days of history to keep |
| `NETFLOW_PORT` | `2055` | UDP port the NetFlow listener binds. Must match the router's *Collector Port* |

## Troubleshooting

- **`/api/netflow/stats` shows 0 packets** — check the router config (*System Maintenance → NetFlow*), confirm Collector IP is the Pi's LAN IP, confirm UDP 2055 isn't blocked on the Pi. `sudo tcpdump -nni any udp port 2055` on the Pi will show whether packets are arriving at all.
- **Per-IP rates show but the IP is `unknown` in the UI** — the SSH poller hasn't yet learned that MAC. Wait one poll cycle (~1s).
- **`Permission denied` on SSH to router** — test manually: `ssh <user>@<router>`. If the password contains `$#!`, wrap the whole `.env` value in single quotes.
- **`kex_exchange failed` / `no matching cipher`** — add the algorithm DrayTek negotiates to `LEGACY_SSH_KWARGS` in [app/collectors/ssh.py](app/collectors/ssh.py).
- **Can't SSH to the router from PuTTY while the container is running** — DrayTek embedded SSH stacks allow only one session at a time and the container holds it persistently. Run `docker compose stop draymon` before SSH'ing, then `docker compose start draymon` after.

A handful of `/debug/ssh/*` endpoints exist for inspecting raw CLI output — see [app/main.py](app/main.py). They share the poller's single SSH session via an asyncio lock.

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

- [RFC 3954 — Cisco Systems NetFlow Services Export Version 9](https://datatracker.ietf.org/doc/html/rfc3954) — the wire format the parser implements.
- [DrayTek — How do I use the Data Flow Monitor?](https://www.draytek.co.uk/support/guides/kb-vigor-dataflowmonitor) — TX/RX rate display on the router's web UI is in kbps.
- [DrayTek Telnet Commands for DrayOS Routers (PDF, v1.4)](https://www.i-lan.net.au/dfaq/DrayTek/misc/DrayTek_Telnet%20Commands%20V1.4.pdf) — CLI reference for `srv dhcp status`, `ip arp status`, `sys version`, `show statistic`.
