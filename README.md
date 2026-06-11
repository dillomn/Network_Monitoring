# DrayTek Network Monitor

Receives **IPFIX** (NetFlow v10 — what the Vigor 2765 actually exports; v9 is also accepted) for exact per-device byte accounting, polls the router over SSH for device discovery, live per-device rates, WAN totals and the NAT port-map, and serves a dashboard + historical graphs from a single Docker container on a Raspberry Pi.

Built and tested against the **Vigor 2762n** and **Vigor 2765 series**.

## What it does

- Listens on UDP/2055 for IPFIX / NetFlow v9 records and credits each flow's bytes to the LAN-side device — outbound by LAN source IP or the MAC carried in the record, inbound reverse-NATted via the router's port-map. IPv4 and IPv6 (v6 needs your delegated prefix in `LAN_PREFIXES`).
- Polls the DrayTek over one persistent SSH session, every `POLL_INTERVAL` (1 s): `srv dhcp status` + `ip arp status` (device identity), `show statistic` (per-WAN lifetime counters), `show portmap` (NAT table, every 5th cycle), and **one live Data Flow Monitor reading** (`show traffic <ip>`, round-robin over devices with open NAT sessions) — the live rate source.
- Stores samples in SQLite (default 30-day retention).
- Web UI at `http://<pi-ip>:8090`: summary tiles (network rate now, 24 h volume, devices online, top consumer), a device list led by **transfer volumes** (1 h / 24 h) with live rates (green dot = live router reading) and an **"in progress…"** badge for devices with open sessions but no measurements yet, a network-wide usage chart, and per-device volume + rate charts that line up on a shared time axis with the trailing ~2 minutes shaded ("data still arriving").
- **Settings ⚙** (header gear) runs live diagnostics — router link, IPFIX ingest, byte attribution, live rate poller, DB — backed by `/api/diagnostics`.

> **Data sources at a glance:** two complementary per-device sources. **IPFIX** is the
> accounting source — exact byte counts, but this firmware only exports a flow when it
> *ends*. The **Data Flow Monitor** (`show traffic <ip>` over SSH) is the live source —
> it sees a transfer *while it runs*, including flows the exporter never reports.
> Charts merge them per 10 s bucket by MAX: live estimates fill the gaps, exact flow
> figures win once they arrive.

### Reading the numbers

This firmware ignores the Active Timeout for ongoing flows — a flow's **total bytes + start/end time arrive only when it ends**. How the app deals with that:

- **Volumes (1h/24h columns, the bar charts) are exact where flow records landed** and
  live-estimated where only Data Flow Monitor readings exist (a transfer still in
  progress, or a flow the exporter never sent).
- **Rate lines follow the measured shape where possible**: a flow-end record's bytes are
  distributed across its span *proportionally to the Data Flow Monitor readings* taken
  while it ran — a download that ran 100 Mbps then 5 Mbps charts as exactly that. The
  flow record anchors the exact total (weights are normalised, so a miscalibrated
  `TRAFFIC_UNIT` only affects shape, never magnitude). Without covering readings it
  falls back to a flat average. `flows_spread_shaped` vs `flows_spread_uniform` in
  `/api/netflow/stats` shows which path flows take.
- **Long transfers are visible live** via the Data Flow Monitor readings; when the flow
  record finally exports, its exact figures replace the estimates. A wrong
  `TRAFFIC_UNIT` scales every live reading — calibrate via `/debug/ssh/raw-traffic?ip=<ip>`.
- **If a known download never showed up at all**, open Settings → *Byte attribution*: the
  bytes landed somewhere, and that row says which guard dropped them. A large
  `both_public` share usually means IPv6 — add your delegated prefix to `LAN_PREFIXES`.

## Prerequisites

- Raspberry Pi running 64-bit PiOS (or any Linux host with Docker)
- Docker: `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER`
- LAN reach from the Pi to the DrayTek's SSH port (TCP 22) and from the router to the Pi's collector port (UDP 2055)

## DrayTek setup (one-time)

1. **Enable SSH** — *System Maintenance → Management → tick "SSH" → Apply.*
   Verify from the Pi: `ssh admin@<router-ip>` should land at `DrayTek>`.
2. **Enable flow export** — *System Maintenance → NetFlow*:
   - tick **Enable**
   - **Collector IP** = the Pi's LAN IP
   - **Collector Port** = `2055`
   - **Version** = `IPFIX` (the 2765 exports IPFIX with absolute flow timestamps, which the collector prefers; `v9` also works — older models like the 2762n only offer v9)
   - **Active Timeout** = `60` s (the minimum — though this firmware doesn't actually chop ongoing flows; live visibility comes from the Data Flow Monitor poller instead)
   - **Inactive Timeout** = `15` s (the minimum; lower = finished flows are reported sooner)
   - Click **OK**.
3. *(Recommended)* Create a dedicated read-only SSH user under *System Maintenance → Administrator Password / Management Account*. SSH key auth is not supported on Vigors — password only.

## Configure and run

```bash
cp .env.example .env
nano .env       # fill in ROUTER_HOST, ROUTER_SSH_USER, ROUTER_SSH_PASSWORD
docker compose up -d --build
```

Open <http://`pi-ip`:8090>. After editing `.env`, run `docker compose up -d` again — a plain `restart` does **not** reload env-file changes.

## Verifying it's working

Open **Settings ⚙** in the UI — every check should be green within a minute of startup. The same data is available raw:

```bash
curl http://<pi-ip>:8090/api/netflow/stats
```

Healthy looks like: `packets_received` climbing, `records_processed > 0`, `last_packet_age_s` under ~30 s, and most bytes under `reason_bytes.outbound`/`inbound`. After a real download, `flows_spread_shaped` should increment (the rate chart used measured shape, not a flat average). If `packets_received` stays at 0, the router isn't reaching the collector — usually a firewall on the Pi (`sudo ufw status`) or a wrong Collector IP; `sudo tcpdump -nni any udp port 2055` shows whether packets arrive at all.

## Testing without a router

`tools/netflow_sim.py` crafts real flow packets (v9 wire format — same parse/attribute/flush pipeline) for a handful of fake LAN devices and fires them at the collector — no DrayTek, nothing on the internet. Only the device *identity* rows are written directly, standing in for SSH discovery.

```bash
docker compose up -d --build
docker compose exec draymon python tools/netflow_sim.py --db /data/netmon.db
```

The device list and graphs fill in within a few seconds. Drop `--db` to send flows only (stats light up, device list stays empty). Clean up afterwards with `--cleanup --db /data/netmon.db`.

**IPv6 attribution test:** add `--ipv6 2001:db8:abcd::/64` to also emit IPv6 flows. With that prefix *absent* from `LAN_PREFIXES`, the bytes must pile up under `both_public` in *Settings → Byte attribution* — the exact signature of a v6 download that never shows in the UI. Add the prefix, `docker compose up -d`, re-run: the same flows must now credit to the devices. Repeat with your *real* delegated prefix to validate production config.

## .env reference

| Var | Default | Meaning |
|---|---|---|
| `ROUTER_HOST` | `192.168.1.1` | DrayTek LAN IP |
| `ROUTER_SSH_PORT` | `22` | SSH port |
| `ROUTER_SSH_USER` | `admin` | SSH username |
| `ROUTER_SSH_PASSWORD` | *(required)* | SSH password. Wrap in single quotes if it contains `$`, `#`, or `!` |
| `POLL_INTERVAL` | `1` | Seconds between SSH poll cycles (discovery, WAN totals, port-map, one live rate reading) |
| `RETENTION_DAYS` | `30` | Days of history to keep |
| `NETFLOW_PORT` | `2055` | UDP port the flow collector binds. Must match the router's *Collector Port* |
| `LAN_PREFIXES` | RFC1918 + IPv6 ULA + link-local | Comma-separated CIDRs treated as LAN. **Add your delegated IPv6 prefix** or v6 traffic lands in `both_public` |
| `TRAFFIC_UNIT` | `kilobits_per_second` | Unit of the router's `show traffic` series — scales every live rate reading. Correct for the 2765; calibrate other models via `/debug/ssh/raw-traffic?ip=<ip>` |

## Troubleshooting

- **0 packets received** — check *System Maintenance → NetFlow* (Collector IP/Port), and that UDP 2055 isn't blocked on the Pi.
- **A device shows traffic but as `(unknown)` / a bare IP** — discovery hasn't mapped the IP to a MAC/hostname yet. Wait a poll cycle; if it persists, the device has no current DHCP lease or ARP entry on the router.
- **A download barely registers anywhere in `reason_bytes`** — if even `both_public`/`no_mac` didn't grow, the exporter never saw the flow. DrayTek's Hardware Acceleration offloads established sessions past the CPU exporter; disable it (*System Maintenance*) and re-test (costs peak WAN throughput on fast lines).
- **`Permission denied` on SSH to router** — test manually: `ssh <user>@<router>`. If the password contains `$#!`, wrap the whole `.env` value in single quotes.
- **`kex_exchange failed` / `no matching cipher`** — add the algorithm DrayTek negotiates to `LEGACY_SSH_KWARGS` in [app/collectors/ssh.py](app/collectors/ssh.py).
- **Can't SSH to the router from PuTTY while the container is running** — DrayTek embedded SSH stacks allow only one session at a time and the container holds it persistently. `docker compose stop draymon`, do your work, `docker compose start draymon`.

A handful of `/debug/ssh/*` endpoints exist for inspecting raw CLI output — see [app/main.py](app/main.py). They share the poller's single SSH session via an asyncio lock.

## Docker cheat sheet

```bash
docker compose up -d --build              # build + start (or update after pulling)
docker compose ps                         # status
docker compose logs -f draymon            # tail logs
docker compose up -d                      # apply .env changes (recreates container)
docker compose down                       # stop, keep data
docker compose down -v                    # stop AND wipe SQLite (careful)
```

Data lives in the `draymon_draymon_data` Docker volume (`docker volume inspect draymon_draymon_data`).

## References

DrayTek-side behaviour the code depends on:

- [RFC 7011 — IPFIX Protocol Specification](https://datatracker.ietf.org/doc/html/rfc7011) — the wire format the Vigor 2765 exports (NetFlow v10), including the absolute flow timestamps (IEs 150–153) the collector prefers.
- [RFC 3954 — Cisco NetFlow Services Export Version 9](https://datatracker.ietf.org/doc/html/rfc3954) — the v9 format, also parsed (older Vigors, and the bundled simulator).
- [DrayTek — How do I use the Data Flow Monitor?](https://www.draytek.co.uk/support/guides/kb-vigor-dataflowmonitor) — the per-IP rate readout behind the live overlay; the web UI displays kbps, matching `TRAFFIC_UNIT`.
- [DrayTek Telnet Commands for DrayOS Routers (PDF, v1.4)](https://www.i-lan.net.au/dfaq/DrayTek/misc/DrayTek_Telnet%20Commands%20V1.4.pdf) — CLI reference for `srv dhcp status`, `ip arp status`, `sys version`, `show statistic`.
