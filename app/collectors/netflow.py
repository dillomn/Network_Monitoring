"""NetFlow v9 listener + per-IP byte aggregator.

The DrayTek is configured (System Maintenance → NetFlow) to export flow
records to this host on UDP. For each WAN-facing flow we credit the LAN
IP — outbound flows (src=LAN, dst=public) count as TX for src; inbound
flows (src=public, dst=LAN) count as RX for dst.

Bytes are accumulated in memory per source IP and flushed every
FLUSH_INTERVAL_S as a bps rate into the existing `samples` table. The
poller still handles device discovery (DHCP + ARP) so we have an IP→MAC
mapping ready by the time NetFlow records arrive.
"""
from __future__ import annotations

import asyncio
import logging
import time
from ipaddress import ip_address, ip_network

from .. import db
from ..config import settings
from ..parsers.netflow import FlowRecord, parse_packet

log = logging.getLogger(__name__)

# Configured LAN prefixes — parsed once on module import. Bad entries are
# logged and skipped rather than crashing startup.
def _parse_lan_prefixes() -> list:
    out = []
    for raw in settings.lan_prefixes.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(ip_network(raw))
        except ValueError as e:
            log.warning("LAN_PREFIXES: ignoring bad entry %r (%s)", raw, e)
    return out


_LAN_NETS = _parse_lan_prefixes()

# How often the in-memory byte accumulators are converted to bps and
# written to SQLite. Lower = livelier UI but more DB churn.
FLUSH_INTERVAL_S = 5.0


def _is_lan(ip: str) -> bool:
    try:
        addr = ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _LAN_NETS)


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, parent: "NetflowCollector") -> None:
        self._parent = parent

    def datagram_received(self, data: bytes, addr) -> None:
        self._parent._on_packet(data, addr)


class NetflowCollector:
    def __init__(self) -> None:
        self._templates: dict[int, list] = {}
        self._tx_bytes: dict[str, int] = {}
        self._rx_bytes: dict[str, int] = {}
        # Sum of flow durations (FIRST_SWITCHED↔LAST_SWITCHED) per MAC
        # since last flush. Used as the denominator so rate = real_bytes /
        # real_seconds, not bytes / our-arbitrary-flush-window. Without
        # this, a 60s flow record arriving in a 5s flush window reports
        # as 12× the real rate (or a cancel-burst dumps ~all bytes at
        # flush-window resolution → 100+ Mbps fake spikes).
        self._duration_s: dict[str, float] = {}
        self._last_flush: float = 0.0
        self._transport: asyncio.DatagramTransport | None = None
        self._flush_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._port: int | None = None
        # Ring buffer of the most recently parsed flow records — exposed
        # via /api/netflow/recent so we can see what the router is sending.
        self._recent: list[dict] = []
        self._recent_max: int = 200
        # IP→MAC cache. Refreshed every 30s in the flush loop so we don't
        # do a SQLite query per flow record. Negative results are cached.
        self._ip_mac_cache: dict[str, str | None] = {}
        self._cache_expires_at: float = 0.0
        # diagnostics
        self.packets_received: int = 0
        # Every record the parser handed back, regardless of whether we
        # could credit it. records_processed (below) only counts the
        # subset that landed on a known device. parsed >> processed means
        # parsing works but attribution is dropping everything.
        self.records_parsed: int = 0
        self.records_processed: int = 0
        # Records the parser produced that blew up during attribution.
        # Should be 0; anything else means a per-record bug we're now
        # surviving instead of silently dropping the rest of the packet.
        self.records_errored: int = 0
        # Disposition tally over ALL records, and separately over only the
        # records that actually carried bytes. The byte-bearing breakdown
        # is the real diagnostic: it ignores the flood of 0-byte
        # flow-creation records DrayTek emits and shows where the records
        # that matter (flow-expiry, with counts) are being credited or
        # dropped.
        self._reason_counts: dict[str, int] = {}
        self._reason_counts_bytes: dict[str, int] = {}
        self.last_packet_ts: int = 0
        self.last_router_addr: str | None = None

    async def start(self, port: int) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self),
            local_addr=("0.0.0.0", port),
            allow_broadcast=False,
        )
        self._port = port
        self._last_flush = time.monotonic()
        self._flush_task = asyncio.create_task(self._flush_loop())
        log.info("NetFlow collector listening on udp/%d", port)

    async def stop(self) -> None:
        self._stop.set()
        if self._flush_task is not None:
            try:
                await self._flush_task
            except Exception:
                log.exception("NetFlow flush task error during stop")
        if self._transport is not None:
            self._transport.close()
        log.info("NetFlow collector stopped")

    def _on_packet(self, data: bytes, addr) -> None:
        self.packets_received += 1
        self.last_packet_ts = int(time.time())
        self.last_router_addr = addr[0] if addr else None
        try:
            records = parse_packet(data, self._templates)
        except Exception:
            log.exception("NetFlow parse failed")
            return
        for rec in records:
            # Guard each record: a bug attributing one record must not
            # abort the rest of the packet (and must not be swallowed
            # silently by asyncio's datagram callback). This is exactly
            # how a missing-attribute crash hid zero throughput before.
            try:
                self._attribute(rec)
            except Exception:
                self.records_errored += 1
                if self.records_errored <= 10 or self.records_errored % 500 == 0:
                    log.exception(
                        "NetFlow attribute failed (src=%s dst=%s) — total errors=%d",
                        rec.src, rec.dst, self.records_errored,
                    )

    def _attribute(self, rec: FlowRecord) -> None:
        # Every parsed record is stashed in the diagnostics ring buffer
        # below WITH a disposition `reason`, even when we drop it. That's
        # what makes /api/netflow/recent useful on a router whose template
        # doesn't match expectations — you can see exactly which guard the
        # records die on (no_bytes, both_local, no_mac, …) instead of an
        # empty list.
        self.records_parsed += 1
        # Split in_bytes and out_bytes into TX/RX based on which end is
        # the LAN device. The DrayTek 2765 emits "biflow" records — one
        # record per connection with `in_bytes` = src→dst direction and
        # `out_bytes` = dst→src direction. For uniflow routers `out_bytes`
        # is just 0 and only `in_bytes` carries data; same logic works.
        tx_add = 0
        rx_add = 0
        duration_s = 0.0
        mac: str | None = None

        if rec.src is None or rec.dst is None:
            reason = "no_src_or_dst"
        elif rec.in_bytes <= 0 and rec.out_bytes <= 0:
            reason = "no_bytes"
        else:
            src_local = _is_lan(rec.src)
            dst_local = _is_lan(rec.dst)
            if src_local and not dst_local:
                mac = rec.src_mac or self._mac_for_ip(rec.src)
                tx_add = rec.in_bytes   # LAN sent → upload
                rx_add = rec.out_bytes  # LAN received → download
                reason = "outbound"
            elif dst_local and not src_local:
                mac = rec.dst_mac or self._mac_for_ip(rec.dst)
                # NAT reverse-lookup: inbound records often arrive with
                # `dst` set to the router's WAN-side IP (post-NAT, before
                # deNAT to the real LAN device). Consult the portmap table
                # the SSH poller maintains to find the real LAN IP.
                if mac is None and rec.dst_port:
                    priv_ip = self._lookup_portmap(rec.dst, rec.dst_port)
                    if priv_ip is not None:
                        mac = self._mac_for_ip(priv_ip)
                tx_add = rec.out_bytes  # LAN sent (reverse direction)
                rx_add = rec.in_bytes   # LAN received
                reason = "inbound"
            elif src_local and dst_local:
                reason = "both_local"   # LAN↔LAN: not internet bandwidth
            else:
                reason = "both_public"  # WAN↔WAN: not internet bandwidth

        if reason in ("outbound", "inbound"):
            # How long the bytes in this flow actually took to happen, per
            # the router's own clock. Zero/missing means we fall back to
            # the flush-window estimate at flush time.
            if rec.last_switched > rec.first_switched > 0:
                duration_s = (rec.last_switched - rec.first_switched) / 1000.0
            if mac is not None:
                if tx_add > 0:
                    self._tx_bytes[mac] = self._tx_bytes.get(mac, 0) + tx_add
                if rx_add > 0:
                    self._rx_bytes[mac] = self._rx_bytes.get(mac, 0) + rx_add
                if duration_s > 0 and (tx_add > 0 or rx_add > 0):
                    self._duration_s[mac] = self._duration_s.get(mac, 0.0) + duration_s
                self.records_processed += 1
            else:
                # Classified as internet traffic but no known device owns
                # the LAN-side IP (discovery hasn't seen it, or the record
                # carries a post-NAT address with no portmap hit).
                reason = "no_mac"

        # Tally dispositions. The byte-bearing breakdown is what tells us
        # whether the records that carry counts are being credited.
        self._reason_counts[reason] = self._reason_counts.get(reason, 0) + 1
        if rec.in_bytes > 0 or rec.out_bytes > 0:
            self._reason_counts_bytes[reason] = self._reason_counts_bytes.get(reason, 0) + 1

        # Stash for diagnostics — bounded ring buffer.
        self._recent.append({
            "ts": self.last_packet_ts,
            "reason": reason,
            "mac": mac,
            "tx_add": tx_add, "rx_add": rx_add,
            "duration_s": duration_s,
            "src": rec.src, "dst": rec.dst,
            "src_mac": rec.src_mac, "dst_mac": rec.dst_mac,
            "src_port": rec.src_port, "dst_port": rec.dst_port,
            "proto": rec.protocol,
            "in_bytes": rec.in_bytes, "out_bytes": rec.out_bytes,
            "in_pkts": rec.in_packets, "out_pkts": rec.out_packets,
        })
        if len(self._recent) > self._recent_max:
            del self._recent[:len(self._recent) - self._recent_max]

    def _mac_for_ip(self, ip: str) -> str | None:
        """Cached IP→MAC lookup. Cache is cleared every 30s in _flush() so
        new DHCP leases get picked up. Negative results are cached too —
        a flood of records for an unknown IP shouldn't hammer SQLite."""
        if ip in self._ip_mac_cache:
            return self._ip_mac_cache[ip]
        mac = db.mac_for_ip(ip)
        self._ip_mac_cache[ip] = mac
        return mac

    def _lookup_portmap(self, pseudo_ip: str, pseudo_port: int) -> str | None:
        """Ask the poller's NAT table for the real LAN IP behind a
        (router_WAN_IP, nat_port) tuple. Lazy-imported to avoid a
        netflow↔poller circular import at module load."""
        from ..poller import poller as _poller
        return _poller.lookup_portmap(pseudo_ip, pseudo_port)

    async def _flush_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=FLUSH_INTERVAL_S)
                break
            except asyncio.TimeoutError:
                pass
            try:
                self._flush()
            except Exception:
                log.exception("NetFlow flush failed")

    def _flush(self) -> None:
        now = time.monotonic()
        flush_window = max(0.5, now - self._last_flush)
        self._last_flush = now
        # Drop the IP→MAC cache periodically so DHCP changes get picked up.
        if now >= self._cache_expires_at:
            self._ip_mac_cache.clear()
            self._cache_expires_at = now + 30.0
        macs = set(self._tx_bytes) | set(self._rx_bytes)
        for mac in macs:
            tx = self._tx_bytes.pop(mac, 0)
            rx = self._rx_bytes.pop(mac, 0)
            d = self._duration_s.pop(mac, 0.0)
            # Use the flow's own duration when the router gave us
            # FIRST/LAST_SWITCHED; fall back to our flush window if it
            # didn't (older NetFlow exporters, or records arrived with
            # equal first==last meaning a "zero-duration" flow).
            divisor = d if d > 0 else flush_window
            db.insert_sample(mac, (tx * 8) / divisor, (rx * 8) / divisor)

    def stats(self) -> dict:
        return {
            "listening_port": self._port,
            "packets_received": self.packets_received,
            "records_parsed": self.records_parsed,
            "records_processed": self.records_processed,
            "records_errored": self.records_errored,
            "last_packet_ts": self.last_packet_ts,
            "last_packet_age_s": (int(time.time()) - self.last_packet_ts) if self.last_packet_ts else None,
            "last_router_addr": self.last_router_addr,
            # Disposition histograms. `reasons_with_bytes` is the one to
            # read: it shows what happens to records that actually carry
            # byte counts (the 0-byte flow-creation records are noise).
            "reasons": dict(self._reason_counts),
            "reasons_with_bytes": dict(self._reason_counts_bytes),
            "templates_known": sorted(self._templates.keys()),
            # Full field layout (field_type, length) per template so we can
            # see exactly what the router is exporting — the field IDs tell
            # us whether IPV4_SRC/DST (8/12), IPV6 (27/28), IN/OUT_BYTES
            # (1/23) etc. are present and at what widths.
            "templates": {
                tid: [[ftype, flen] for ftype, flen in fields]
                for tid, fields in self._templates.items()
            },
            "buffered_ips_tx": len(self._tx_bytes),
            "buffered_ips_rx": len(self._rx_bytes),
        }

    def recent(self, limit: int = 50) -> list[dict]:
        """Last N parsed flow records — for diagnosing 'is the router
        sending what we expect' questions without tcpdump."""
        if limit <= 0:
            return []
        return list(self._recent[-limit:])


netflow = NetflowCollector()
