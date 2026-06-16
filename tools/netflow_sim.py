#!/usr/bin/env python3
"""NetFlow v9 traffic simulator — exercise the DrayMon collector + UI with
no real DrayTek and nothing on the internet.

It crafts valid NetFlow v9 datagrams (a template flowset + a data flowset)
for a handful of fake LAN devices doing uploads/downloads, and fires them at
the collector's UDP port. Each flow carries the LAN device's MAC, so the
collector attributes it exactly as it would a real record — you watch
/api/netflow/stats climb and (with --db) the device list + graphs fill in.

Each device keeps a STABLE 5-tuple across exports, so the collector treats
successive packets as re-exports of one long-lived flow (overwriting the
same key) — matching how a real router re-exports on its Active Timeout. The
byte counts jitter each tick so the graphs look alive.

Examples
--------
Inside the running container (collector on localhost, seeds the real DB so
the device list populates):

    docker compose exec draymon python tools/netflow_sim.py --db /data/netmon.db

From the host against the published UDP port (stats light up; device list
stays empty because the DB is in the container volume):

    python3 tools/netflow_sim.py --host 127.0.0.1 --port 2055

Remove the test data afterwards (devices + their samples):

    docker compose exec draymon python tools/netflow_sim.py --db /data/netmon.db --cleanup

IPv6 attribution test — emit ADDITIONAL IPv6 flows for the same devices,
sourced from addresses inside the given prefix:

    docker compose exec draymon python tools/netflow_sim.py --db /data/netmon.db \
        --ipv6 2001:db8:abcd::/64

With the prefix ABSENT from LAN_PREFIXES, those bytes must pile up under
`both_public` in /api/netflow/stats (the bug signature: a v6 download that
never shows). Add the prefix to LAN_PREFIXES in .env, `docker compose up -d`,
re-run, and the same flows must now credit to the devices (via the src MAC
carried in the record — v6 addresses have no entry in the devices table).

Stop with Ctrl-C.

NOTE: the running collector writes samples into ITS OWN DB_PATH (the live DB),
regardless of --db. So testing against the production container mixes test
samples into the live DB — clean up with --cleanup, or run a throwaway app
instance with its own DB_PATH for fully isolated testing.
"""
from __future__ import annotations

import argparse
import random
import socket
import sqlite3
import struct
import time
from ipaddress import ip_network

TEMPLATE_ID = 256
TEMPLATE_ID_V6 = 257

# (field_type, length_bytes) — IANA / RFC 3954 IDs, exactly the ones the
# collector extracts. The data-record packing below MUST follow this order.
TEMPLATE_FIELDS = [
    (8, 4),    # IPV4_SRC_ADDR
    (12, 4),   # IPV4_DST_ADDR
    (7, 2),    # L4_SRC_PORT
    (11, 2),   # L4_DST_PORT
    (4, 1),    # PROTOCOL
    (1, 4),    # IN_BYTES   (src->dst)
    (23, 4),   # OUT_BYTES  (dst->src)
    (2, 4),    # IN_PKTS
    (24, 4),   # OUT_PKTS
    (22, 4),   # FIRST_SWITCHED (ms since router boot)
    (21, 4),   # LAST_SWITCHED
    (56, 6),   # IN_SRC_MAC  (the LAN device, on an outbound flow)
    (80, 6),   # IN_DST_MAC  (the non-LAN end)
]

# Same layout with 16-byte v6 addresses — what a DrayTek exports for IPv6
# flows. The src MAC is what attribution must use: a global v6 address never
# appears in the devices table (discovery maps IPv4 only).
TEMPLATE_FIELDS_V6 = [(27, 16), (28, 16)] + TEMPLATE_FIELDS[2:]

# Fake LAN devices: (ip, mac, hostname, rx_peak_mbps, tx_peak_mbps).
DEVICES = [
    ("192.168.1.10", "AA:BB:CC:00:00:10", "Dillons-Laptop", 60, 8),
    ("192.168.1.20", "AA:BB:CC:00:00:20", "Living-Room-TV", 25, 1),
    ("192.168.1.30", "AA:BB:CC:00:00:30", "iPhone-Dillon", 12, 3),
    ("192.168.1.40", "AA:BB:CC:00:00:40", "Desktop-PC", 30, 40),
    ("192.168.1.50", "AA:BB:CC:00:00:50", "Security-NVR", 2, 18),
]
PUBLIC_PEERS = ["8.8.8.8", "1.1.1.1", "140.82.121.4", "151.101.1.140", "13.107.42.14"]
PUBLIC_PEERS_V6 = [
    "2001:4860:4860::8888", "2606:4700:4700::1111", "2620:fe::fe",
    "2a00:1450:4009:81f::200e", "2606:2800:220:1:248:1893:25c8:1946",
]
GATEWAY_MAC = "00:1D:AA:00:00:01"  # stands in for the non-LAN end of each flow


def _mac_bytes(mac: str) -> bytes:
    return bytes(int(b, 16) for b in mac.split(":"))


def _build_template_flowset(tmpl_id: int, fields: list) -> bytes:
    packed = b"".join(struct.pack("!HH", t, length) for t, length in fields)
    body = struct.pack("!HH", tmpl_id, len(fields)) + packed
    return struct.pack("!HH", 0, 4 + len(body)) + body  # flowset_id 0 = template


def _build_record(idx: int, dev, uptime_ms: int, interval_ms: int,
                  src_packed: bytes, dst_packed: bytes) -> bytes:
    """One data record. Address fields are pre-packed (4-byte v4 or 16-byte
    v6) so the same body serves both templates."""
    _ip, mac, _hostname, rx_peak, tx_peak = dev
    src_port = 40000 + idx                          # stable per device
    secs = interval_ms / 1000.0
    # Bytes moved this interval = rate * time, jittered; occasionally idle.
    rx_mbps = random.uniform(0, rx_peak) if random.random() > 0.15 else 0
    tx_mbps = random.uniform(0, tx_peak) if random.random() > 0.30 else 0
    in_bytes = int(tx_mbps * 1e6 / 8 * secs)    # LAN sent (upload)   -> IN_BYTES
    out_bytes = int(rx_mbps * 1e6 / 8 * secs)   # LAN received (down) -> OUT_BYTES
    return b"".join([
        src_packed,
        dst_packed,
        struct.pack("!H", src_port),
        struct.pack("!H", 443),
        struct.pack("!B", 6),                                  # TCP
        struct.pack("!I", in_bytes),
        struct.pack("!I", out_bytes),
        struct.pack("!I", max(1, in_bytes // 1400)),
        struct.pack("!I", max(1, out_bytes // 1400)),
        struct.pack("!I", max(0, uptime_ms - interval_ms)),    # first_switched
        struct.pack("!I", uptime_ms),                          # last_switched
        _mac_bytes(mac),                                       # IN_SRC_MAC
        _mac_bytes(GATEWAY_MAC),                               # IN_DST_MAC
    ])


def _build_packet(seq: int, uptime_ms: int, interval_ms: int,
                  v6_addrs: list[str] | None = None) -> bytes:
    records = b"".join(
        _build_record(
            i, d, uptime_ms, interval_ms,
            socket.inet_aton(d[0]),
            socket.inet_aton(PUBLIC_PEERS[i % len(PUBLIC_PEERS)]),
        )
        for i, d in enumerate(DEVICES)
    )
    flowsets = _build_template_flowset(TEMPLATE_ID, TEMPLATE_FIELDS)
    flowsets += struct.pack("!HH", TEMPLATE_ID, 4 + len(records)) + records
    if v6_addrs:
        records6 = b"".join(
            _build_record(
                i, d, uptime_ms, interval_ms,
                socket.inet_pton(socket.AF_INET6, v6_addrs[i]),
                socket.inet_pton(socket.AF_INET6, PUBLIC_PEERS_V6[i % len(PUBLIC_PEERS_V6)]),
            )
            for i, d in enumerate(DEVICES)
        )
        flowsets += _build_template_flowset(TEMPLATE_ID_V6, TEMPLATE_FIELDS_V6)
        flowsets += struct.pack("!HH", TEMPLATE_ID_V6, 4 + len(records6)) + records6
    header = struct.pack(
        "!HHIIII",
        9,                        # version
        len(DEVICES),             # count (the collector ignores it)
        uptime_ms & 0xFFFFFFFF,   # sys_uptime
        int(time.time()),         # unix_secs
        seq,                      # sequence
        0,                        # source_id
    )
    # Templates before data so the collector has them when it reaches the data.
    return header + flowsets


def _seed_devices(db_path: str) -> None:
    """Insert the fake devices into the app's SQLite so they show in the UI
    device list. The collector keys samples by MAC; the list query reads
    FROM devices, so without a row a device never appears."""
    now = int(time.time())
    try:
        c = sqlite3.connect(db_path)
        for ip, mac, hostname, *_ in DEVICES:
            c.execute(
                """INSERT INTO devices (mac, ip, hostname, vendor, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET
                       ip=excluded.ip, hostname=excluded.hostname, last_seen=excluded.last_seen""",
                (mac, ip, hostname, "Simulated", now, now),
            )
        c.commit()
        c.close()
        print(f"seeded {len(DEVICES)} devices into {db_path}")
        print(f"NOTE: samples for these devices are written by the collector into its "
              f"own DB_PATH (the live DB), not {db_path}. Run with --cleanup to remove "
              "the simulated devices + their samples afterwards.")
    except sqlite3.OperationalError as e:
        print(f"WARN: could not seed devices ({e}). NetFlow stats will still work; "
              "the UI device list needs the table the running app creates at startup.")


def _cleanup_devices(db_path: str) -> None:
    """Remove the simulated devices and every sample row keyed to their MACs,
    so a test run leaves no residue in a real DB. Stop the sim first so no
    new samples arrive mid-delete."""
    macs = [mac for _ip, mac, *_ in DEVICES]
    placeholders = ",".join("?" * len(macs))
    try:
        c = sqlite3.connect(db_path)
        n_s = c.execute(f"DELETE FROM samples WHERE mac IN ({placeholders})", macs).rowcount
        n_d = c.execute(f"DELETE FROM devices WHERE mac IN ({placeholders})", macs).rowcount
        c.commit()
        c.close()
        print(f"removed {n_d} simulated devices and {n_s} samples from {db_path}")
    except sqlite3.OperationalError as e:
        print(f"WARN: cleanup failed ({e})")


def main() -> None:
    ap = argparse.ArgumentParser(description="NetFlow v9 traffic simulator for DrayMon")
    ap.add_argument("--host", default="127.0.0.1", help="collector host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=2055, help="collector UDP port (default 2055)")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between exports (default 5)")
    ap.add_argument("--seconds", type=float, default=0, help="run this long then stop (0 = forever)")
    ap.add_argument("--db", default=None, help="path to netmon.db to seed device rows (optional)")
    ap.add_argument("--cleanup", action="store_true",
                    help="remove the simulated devices + their samples from --db, then exit")
    ap.add_argument("--ipv6", default=None, metavar="PREFIX",
                    help="also emit IPv6 flows, device addresses inside PREFIX "
                         "(e.g. 2001:db8:abcd::/64). With the prefix NOT in "
                         "LAN_PREFIXES these bytes land in both_public — the "
                         "missing-download signature; with it added they must "
                         "credit to the devices via src MAC.")
    args = ap.parse_args()

    if args.cleanup:
        if not args.db:
            ap.error("--cleanup needs --db PATH (the DB to clean)")
        _cleanup_devices(args.db)
        return

    v6_addrs = None
    if args.ipv6:
        try:
            net = ip_network(args.ipv6, strict=False)
        except ValueError as e:
            ap.error(f"--ipv6: {e}")
        if net.version != 6:
            ap.error("--ipv6 needs an IPv6 prefix")
        v6_addrs = [str(net.network_address + i + 1) for i in range(len(DEVICES))]
        print("IPv6 flows enabled; device addresses:")
        for d, a in zip(DEVICES, v6_addrs):
            print(f"  {d[2]:<16} {a}")
        print("watch reason_bytes in /api/netflow/stats: both_public if the prefix "
              "is missing from LAN_PREFIXES, outbound once it's added")

    if args.db:
        _seed_devices(args.db)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval_ms = int(args.interval * 1000)
    start = time.time()
    boot = time.monotonic()
    seq = 0
    print(f"sending NetFlow v9 to {args.host}:{args.port} every {args.interval}s "
          f"for {len(DEVICES)} devices — Ctrl-C to stop")
    try:
        while True:
            uptime_ms = int((time.monotonic() - boot) * 1000) + interval_ms
            sock.sendto(_build_packet(seq, uptime_ms, interval_ms, v6_addrs), (args.host, args.port))
            seq += 1
            if args.seconds and (time.time() - start) >= args.seconds:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
