# DrayTek Network Monitor

Receives **NetFlow v9** from a DrayTek Vigor router for per-IP bandwidth, polls the router over SSH for device discovery and WAN totals, and serves a live device list + historical graphs from a single Docker container on a Raspberry Pi.

Built and tested against the **Vigor 2762n** and **Vigor 2765 series**.

## What it does

- Listens on UDP/2055 for NetFlow v9 records from the router and credits each flow's bytes to the LAN-side IP for per-device bandwidth (see the accuracy caveat under *Verifying NetFlow is flowing*)
- Logs into the DrayTek over SSH for `srv dhcp status` + `ip arp status` (device list, hostnames, vendor) and `show statistic` (per-WAN cumulative byte counters → live WAN bps via deltas)
- Stores samples in SQLite (default 30-day retention)
- Web UI at `http://<pi-ip>:8090` with sortable device list, live WAN totals, and historical graphs.
  The device list leads with **exact transfer volumes** (last 1h / 24h) — rates are estimates
  (see *Reading the numbers* below), volumes are conserved byte counts. Devices with open NAT
  sessions but no flow data yet show **“in progress…”** instead of a false 0 bps, and charts
  shade the trailing ~2 minutes where flow records may not have arrived yet.

### Reading the numbers

The router exports a flow's **total bytes + start/end time only when the flow ends** (its
Active Timeout doesn't chop ongoing flows — see DrayTek setup below). Consequences:

- **Volumes (1h/24h columns, the bar charts) are exact where NetFlow reported** and
  live-estimated where only Data Flow Monitor readings exist (a transfer still in
  progress, or a flow the exporter never sent).
- **Rate lines follow the measured shape where possible**: when a flow-end record arrives,
  its bytes are distributed across the flow's span *proportionally to the Data Flow Monitor
  readings* taken while it ran — a download that ran 100 Mbps then 5 Mbps charts as exactly
  that. The flow record anchors the exact total (the weights are normalised, so even a
  miscalibrated `TRAFFIC_UNIT` only affects shape, never magnitude). Only when no readings
  cover the window (device wasn't in the DFM rotation) does it fall back to a flat average.
  `flows_spread_shaped` vs `flows_spread_uniform` in `/api/netflow/stats` shows which path
  flows are taking.
- **Long transfers are visible live** via the Data Flow Monitor readings (green dot next to
  the rate = live reading); when the flow record finally exports, its exact figures replace
  the estimates. A wrong `TRAFFIC_UNIT` scales every live reading — calibrate via
  `/debug/ssh/raw-traffic?ip=<ip>`.
- **If a known download never showed up at all**, open Settings → *Byte attribution*: the
  bytes landed somewhere, and that row says which guard dropped them (a large `both_public`
  share usually means IPv6 — add your delegated prefix to `LAN_PREFIXES`).
- **Settings ⚙** (header gear) opens a troubleshooting panel that runs live checks — router reachable, SSH auth, NetFlow ingest/attribution, DB — backed by `/api/diagnostics`

> **Data sources at a glance:** two complementary per-device sources. **NetFlow** is the
> accounting source — exact byte counts, but a flow is only reported when it *ends*.
> The **Data Flow Monitor** (`show traffic <ip>` over SSH, polled round-robin one device
> per second across devices with open NAT sessions) is the live source — it sees a
> transfer *while it runs*, including flows NetFlow never reports. Charts merge them
> per 10 s bucket by MAX, so live estimates fill the gaps and NetFlow's exact figures
> win once they arrive. SSH also supplies device identity (DHCP/ARP), WAN totals, and
> the NAT port-map.

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
   - **Active Timeout** = `60` seconds (the Vigor minimum). Note: this firmware does not actually chop an ongoing flow at the active timeout — a long download is exported as a single record only when it ends. The collector handles that by spreading each flow's bytes across the time span it reports (`flow_start`→`flow_end`), so the chart shows the transfer at the right rate over the right interval. The trade-off is that an in-progress long download doesn't appear until it finishes.
   - **Inactive Timeout** = `15` seconds (the Vigor minimum; lower = bursty flows show up sooner)
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

**Accuracy caveat — Hardware Acceleration.** DrayTek's Hardware Acceleration offloads established sessions past the CPU, and the NetFlow exporter runs on the CPU — so accelerated flows can be undercounted (DrayTek's own Traffic Graph and WAN Budget are documented to miss accelerated traffic for the same reason). To check whether this is biting you: download a known-size file to one device, then read `reason_bytes` in `/api/netflow/stats` — the `inbound`/`outbound` totals should roughly equal the bytes transferred. If they're a tiny fraction, disable **Hardware Acceleration** on the router (*System Maintenance*) and re-test; a cross-check of the same device's rate is available at `/debug/ssh/raw-traffic?ip=<ip>` against the router's Data Flow Monitor page. Note: disabling acceleration lowers max WAN throughput, which only matters if your line is faster than the router's non-accelerated ceiling.

## Testing without a router

`tools/netflow_sim.py` crafts **real NetFlow v9 packets** for a handful of fake LAN devices and fires them at the collector — no DrayTek, nothing on the internet. The rates you see are produced by the genuine receive→parse→attribute→flush pipeline (watch `/api/netflow/stats` climb); only the device *identity* rows are written directly, standing in for SSH discovery.

```bash
docker compose up -d --build
docker compose exec draymon python tools/netflow_sim.py --db /data/netmon.db
```

The device list and graphs fill in within a few seconds. Drop `--db` to send NetFlow only (stats light up, device list stays empty). This exercises the software path but **cannot** test the hardware-acceleration accuracy question — that needs real traffic across the router's WAN.

**IPv6 attribution test (no router needed):** add `--ipv6 2001:db8:abcd::/64` to also emit IPv6 flows. With that prefix *absent* from `LAN_PREFIXES`, the bytes must pile up under `both_public` in *Settings → Byte attribution* — the exact signature of a v6 download that never shows in the UI. Add the prefix to `LAN_PREFIXES`, restart, re-run: the same flows must now credit to the devices (attribution uses the src MAC in the record, since v6 addresses aren't in the devices table). Repeat with your *real* delegated prefix to validate production config.

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
| `LAN_PREFIXES` | RFC1918 + IPv6 ULA + link-local | Comma-separated CIDRs treated as LAN. If your ISP routes a global IPv6 /64, add it here |

## Troubleshooting

- **`/api/netflow/stats` shows 0 packets** — check the router config (*System Maintenance → NetFlow*), confirm Collector IP is the Pi's LAN IP, confirm UDP 2055 isn't blocked on the Pi. `sudo tcpdump -nni any udp port 2055` on the Pi will show whether packets are arriving at all.
- **A device shows traffic but as `(unknown)` / a bare IP** — NetFlow has a flow for an IP the SSH discovery poll hasn't mapped to a MAC/hostname yet. Wait a poll cycle; if it persists, the device has no current DHCP lease or ARP entry on the router.
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
