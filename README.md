# Pi Network Monitoring

A self-contained network monitoring stack for a Raspberry Pi 4, designed to identify rogue / bandwidth-spiking devices on a LAN behind a **DrayTek 2765ax** — even when the WAN is down.

## What's in the stack

| Service        | Port            | Purpose                                                  |
|----------------|-----------------|----------------------------------------------------------|
| **ntopng**     | `3000`          | Per-host / per-MAC traffic analysis ("Top Talkers")      |
| Redis          | `6379` (local)  | ntopng backend store                                     |
| **Grafana**    | `3001`          | Dashboards over the SNMP data                            |
| Prometheus     | `9090`          | Time-series DB for SNMP counters (90-day retention)      |
| snmp_exporter  | `9116`          | Polls the DrayTek over SNMP                              |

ntopng is the tool that will actually point at the bad device. The Prometheus/Grafana side gives you long-term WAN/LAN saturation graphs from the router itself, which keeps working even if the port mirror isn't configured.

---

## 1. Prerequisites on the Pi

1. **PiOS 64-bit** (Bullseye or Bookworm). The 32-bit version works but the 64-bit images run noticeably better with this many containers.
2. **Static IP** on the Pi's `eth0` — set in `/etc/dhcpcd.conf` or via the DrayTek's DHCP IP reservation. You'll need to know this IP to reach the dashboards.
3. Install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   # log out / back in
   ```
4. Copy this `Network_Monitoring/` folder onto the Pi (e.g. `/home/pi/netmon/`).

---

## 2. Configure the DrayTek 2765ax

Two things to set up on the router. Both are done from the DrayTek web UI.

### 2a. Port mirror (REQUIRED for ntopng to see other devices)

This is the single most important step. Without it, ntopng will only see traffic to/from the Pi itself.

1. Web UI → **LAN → LAN Port Mirror**
2. Configure:
   - **Mirror Port**: the LAN port the Pi is plugged into (e.g. `P1`)
   - **Mirrored Port(s)**: tick every *other* LAN port you want to watch (`P2`, `P3`, `P4`)
   - **Mirrored Direction**: **Both** (TX + RX)
   - **Do NOT tick WAN1.** The DrayTek warns that WAN1 mirroring is done in software and degrades router performance. You don't need it: every byte a LAN device sends to the internet passes through one of the LAN ports first, so ntopng will still see and attribute it. Mirroring WAN1 on a router that's already being saturated by the bad device will only make crashes more likely.
3. Apply.

After this, the Pi's `eth0` will receive a copy of every frame on the mirrored LAN ports.

> **Heads-up:** the Pi loses general LAN connectivity through this port if your DrayTek model dedicates the mirror port to receive-only. On the 2765ax it should remain bidirectional, but if you can't reach the Pi over SSH after enabling, plug a USB-Ethernet adapter into the Pi and use that as a management interface (then change `-i=eth0` in `compose.yaml` to whichever interface receives the mirror traffic — usually still `eth0`).

### 2b. Enable SNMP (for Prometheus / Grafana)

1. Web UI → **System Maintenance → Management → SNMP Setup**
2. Enable **SNMP Agent**
3. Set:
   - **Get Community**: `public` (or change it — see "Changing the SNMP community" below)
   - **Manager Host IP**: the Pi's static IP (so only the Pi can poll)
4. Apply.

---

## 3. Edit the configs for your network

Three places to change before first start:

| File                          | What to change                                                               |
|-------------------------------|------------------------------------------------------------------------------|
| `compose.yaml` (ntopng)       | `-m=192.168.1.0/24` → your actual LAN CIDR                                   |
| `compose.yaml` (grafana)      | `GF_SECURITY_ADMIN_PASSWORD=admin` → a real password                         |
| `prometheus/prometheus.yml`   | `192.168.1.1` → the DrayTek's LAN IP                                         |

If your Pi's monitor interface isn't `eth0` (e.g. you used a USB NIC for the mirror), also update `-i=eth0` in `compose.yaml`.

---

## 4. Start the stack

From inside the `Network_Monitoring/` folder on the Pi:

```bash
docker compose up -d
docker compose ps        # all five services should be "running"
docker compose logs -f ntopng
```

Give it ~30 seconds for ntopng to initialise.

---

## 5. Reach the dashboards

Replace `<pi-ip>` with the Pi's static IP.

- **ntopng**: <http://pi-ip:3000>
  - First-run login: `admin` / `admin` (you'll be forced to change it)
  - **This is where you find the bad device.** Go to **Hosts → Top Local Talkers** for live ranking; **Hosts → Historical** for past windows.
- **Grafana**: <http://pi-ip:3001>
  - Login: `admin` / whatever you set in `compose.yaml`
  - Prometheus is already wired up as the default datasource.
  - Import a dashboard for SNMP interface stats — Grafana dashboard ID **14857** ("SNMP Stats") is a reasonable starting point. Dashboards → Import → paste ID.
- **Prometheus** (raw): <http://pi-ip:9090>

---

## 6. Verifying SNMP works

From the Pi:

```bash
docker exec -it snmp-exporter wget -qO- "http://localhost:9116/snmp?target=192.168.1.1&module=if_mib" | head -40
```

You should see a wall of `ifInOctets`/`ifOutOctets` metrics. If you get errors about timeout, check:
- DrayTek SNMP is on
- Manager Host IP on the DrayTek is the Pi's IP (or empty for "any")
- The router IP in `prometheus.yml` is correct

---

## 7. Finding the bad device

Once port mirroring is on and ntopng has a few minutes of data:

1. ntopng → **Hosts → All Hosts** → sort by **Traffic Sent + Received**
2. The top entry that *isn't* the router or a known server is your suspect
3. Click the host → see protocol breakdown, top contacts, alerts
4. To set up automatic alerting on a host: **Settings → Preferences → Alerts** and enable **Local Host Throughput** alerts at a sensible threshold (e.g. 50 Mbps sustained)

For the DrayTek-side view (good for confirming when the WAN is being saturated):
- Grafana → the SNMP dashboard you imported → look at the WAN interface bytes/sec graph. Time-correlate with ntopng's host data to confirm which device caused which spike.

---

## Operations

```bash
docker compose ps                 # status
docker compose logs -f <service>  # tail logs
docker compose restart <service>
docker compose pull && docker compose up -d   # update images
docker compose down               # stop (data persists in volumes)
docker compose down -v            # stop AND wipe data (careful)
```

### Where data lives

All persistent state is in named Docker volumes (`docker volume ls | grep netmon`). Prometheus is set to 90-day retention; ntopng's free edition keeps a rolling window depending on traffic volume — usually a few weeks on a Pi 4.

### Changing the SNMP community

The bundled `snmp_exporter` image defaults to community = `public` (auth name `public_v2`). If you set a different community on the DrayTek:

1. Mount your own `snmp.yml` (uncomment the `volumes:` block under `snmp-exporter` in `compose.yaml`).
2. Generate it from the [snmp_exporter generator](https://github.com/prometheus/snmp_exporter/tree/main/generator), or copy the default and change the community string under `auths:`.
3. Update the `auth: [<your_auth_name>]` line in `prometheus/prometheus.yml`.

### If the Pi can't see other devices' traffic in ntopng

Almost always one of:
- Port mirror not enabled on the DrayTek (most common)
- ntopng is watching the wrong interface — check `ip -br link` on the Pi and update `-i=` in `compose.yaml`
- The Pi is plugged into the wrong physical port (must be the **Mirror Port** chosen in the DrayTek)
