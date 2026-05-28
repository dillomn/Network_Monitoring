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
        self.records_processed: int = 0
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
            self._attribute(rec)

    def _attribute(self, rec: FlowRecord) -> None:
        if rec.src is None or rec.dst is None:
            return
        if rec.in_bytes <= 0 and rec.out_bytes <= 0:
            return
        src_local = _is_lan(rec.src)
        dst_local = _is_lan(rec.dst)
        # Split in_bytes and out_bytes into TX/RX based on which end is
        # the LAN device. The DrayTek 2765 emits "biflow" records — one
        # record per connection with `in_bytes` = src→dst direction and
        # `out_bytes` = dst→src direction. For uniflow routers `out_bytes`
        # is just 0 and only `in_bytes` carries data; same logic works.
        tx_add = 0
        rx_add = 0
        mac: str | None
        if src_local and not dst_local:
            mac = rec.src_mac or self._mac_for_ip(rec.src)
            tx_add = rec.in_bytes   # LAN sent → upload
            rx_add = rec.out_bytes  # LAN received → download
        elif dst_local and not src_local:
            mac = rec.dst_mac or self._mac_for_ip(rec.dst)
            tx_add = rec.out_bytes  # LAN sent (reverse direction)
            rx_add = rec.in_bytes   # LAN received
        else:
            return  # LAN↔LAN or WAN↔WAN: not internet bandwidth
        if mac is not None:
            if tx_add > 0:
                self._tx_bytes[mac] = self._tx_bytes.get(mac, 0) + tx_add
            if rx_add > 0:
                self._rx_bytes[mac] = self._rx_bytes.get(mac, 0) + rx_add
            self.records_processed += 1
        # Stash for diagnostics — bounded ring buffer.
        self._recent.append({
            "ts": self.last_packet_ts,
            "mac": mac,
            "tx_add": tx_add, "rx_add": rx_add,
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
        elapsed = max(0.5, now - self._last_flush)
        self._last_flush = now
        # Drop the IP→MAC cache periodically so DHCP changes get picked up.
        if now >= self._cache_expires_at:
            self._ip_mac_cache.clear()
            self._cache_expires_at = now + 30.0
        macs = set(self._tx_bytes) | set(self._rx_bytes)
        for mac in macs:
            tx = self._tx_bytes.pop(mac, 0)
            rx = self._rx_bytes.pop(mac, 0)
            db.insert_sample(mac, (tx * 8) / elapsed, (rx * 8) / elapsed)

    def stats(self) -> dict:
        return {
            "listening_port": self._port,
            "packets_received": self.packets_received,
            "records_processed": self.records_processed,
            "last_packet_ts": self.last_packet_ts,
            "last_packet_age_s": (int(time.time()) - self.last_packet_ts) if self.last_packet_ts else None,
            "last_router_addr": self.last_router_addr,
            "templates_known": sorted(self._templates.keys()),
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
