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
from ..parsers.netflow import FlowRecord, parse_packet

log = logging.getLogger(__name__)

# RFC1918 — what we treat as LAN for attribution.
_PRIVATE_NETS = [
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
]

# How often the in-memory byte accumulators are converted to bps and
# written to SQLite. Lower = livelier UI but more DB churn.
FLUSH_INTERVAL_S = 5.0


def _is_private(ip: str) -> bool:
    try:
        return any(ip_address(ip) in net for net in _PRIVATE_NETS)
    except ValueError:
        return False


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
        total = rec.in_bytes + rec.out_bytes
        if total <= 0:
            return
        src_local = _is_private(rec.src)
        dst_local = _is_private(rec.dst)
        direction: str
        if src_local and not dst_local:
            self._tx_bytes[rec.src] = self._tx_bytes.get(rec.src, 0) + total
            self.records_processed += 1
            direction = "tx"
        elif dst_local and not src_local:
            self._rx_bytes[rec.dst] = self._rx_bytes.get(rec.dst, 0) + total
            self.records_processed += 1
            direction = "rx"
        else:
            return  # LAN↔LAN or WAN↔WAN: not internet bandwidth
        # Stash for diagnostics — bounded ring buffer.
        self._recent.append({
            "ts": self.last_packet_ts,
            "direction": direction,
            "src": rec.src, "dst": rec.dst,
            "src_port": rec.src_port, "dst_port": rec.dst_port,
            "proto": rec.protocol,
            "in_bytes": rec.in_bytes, "out_bytes": rec.out_bytes,
            "in_pkts": rec.in_packets, "out_pkts": rec.out_packets,
        })
        if len(self._recent) > self._recent_max:
            del self._recent[:len(self._recent) - self._recent_max]

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
        ips = set(self._tx_bytes) | set(self._rx_bytes)
        for ip in ips:
            tx = self._tx_bytes.pop(ip, 0)
            rx = self._rx_bytes.pop(ip, 0)
            mac = db.mac_for_ip(ip)
            if mac is None:
                # Unknown LAN IP — drop. The SSH poller will discover it
                # via DHCP/ARP within a couple of seconds.
                continue
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
