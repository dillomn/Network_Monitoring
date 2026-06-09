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
from collections import deque
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

# How often per-device rates are computed and written to SQLite. Lower =
# livelier UI but more DB churn.
FLUSH_INTERVAL_S = 5.0

# Sliding wall-clock window used to turn NetFlow's lumpy byte exports into a
# smooth bits-per-second rate. Every attributed record's bytes are credited to
# the device; each flush we sum the bytes credited within the last
# RATE_WINDOW_S and divide by the window:  rate = Σbytes × 8 ÷ window.
#
# This MUST be ≥ the router's NetFlow "Active Timeout" (configurable via
# NETFLOW_RATE_WINDOW_S; see config.py). The router exports a long-lived flow's
# bytes only once per active timeout, so a steady transfer arrives as one lump
# every ~60s. Dividing each lump by the fixed window reconstructs the true
# average rate (75 MB exported once a minute → a flat ~10 Mbps) and holds it
# smoothly between exports.
#
# Crucially the rate is now grounded entirely in real elapsed time and real
# byte counts, so it is physically bounded by the link. The old approach
# divided a lump by the flow's OWN reported duration (FIRST/LAST_SWITCHED);
# when the router reported a collapsed or zero window that denominator turned
# a 10 Mbps download into a multi-Gbps phantom spike. We no longer use those
# timestamps for rating at all.
RATE_WINDOW_S = settings.netflow_rate_window_s


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
        # Diagnostic switch. When False the listener still receives + tallies
        # records (so /api/netflow/stats `reason_bytes` works) but writes no
        # per-device samples — handy for inspecting what the router exports
        # without persisting it. True (default) credits per-device bytes,
        # which is the live UI source.
        self.write_samples: bool = True
        # Per-device byte accumulators driving the live rate. Bytes from every
        # attributed record land in the CURRENT-window tallies (_cur_*); each
        # flush moves them into a per-MAC ring of recent (monotonic_ts, tx, rx)
        # deposits (_history) and evicts deposits older than RATE_WINDOW_S. The
        # live rate is Σbytes-in-window × 8 ÷ RATE_WINDOW_S — a sliding
        # wall-clock byte rate.
        #
        # Why bytes-over-wall-clock instead of summing per-flow rates: a flow's
        # own FIRST/LAST_SWITCHED duration is an unreliable rate denominator on
        # this hardware (it can come back collapsed or zero), which is what
        # produced multi-Gbps phantom spikes. Counting real bytes over real
        # elapsed time can't exceed the link no matter what the router reports.
        self._cur_tx: dict[str, int] = {}
        self._cur_rx: dict[str, int] = {}
        self._history: dict[str, deque[tuple[float, int, int]]] = {}
        # MACs we wrote a non-zero sample for last flush, so when a device goes
        # idle (its window empties) we can emit one explicit 0 rather than
        # leaving the UI showing a stale rate forever.
        self._active_macs: set[str] = set()
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
        # Total bytes (in+out) per disposition — volume-weighted, so it
        # reveals where the BULK of traffic lands regardless of how rarely
        # the big records arrive. A download whose megabytes show up under
        # `no_mac` or `outbound` here is being mis-handled, not just
        # mis-rated.
        self._reason_bytes: dict[str, int] = {}
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
            # duration_s is computed for diagnostics only (it surfaces in
            # /api/netflow/recent and is exactly the value that, used as a rate
            # denominator, produced phantom spikes). Rates no longer use it.
            if rec.last_switched > rec.first_switched > 0:
                duration_s = (rec.last_switched - rec.first_switched) / 1000.0
            if mac is not None:
                self.records_processed += 1
                # Credit the bytes to the device's current window; _flush turns
                # the rolling byte total into a bits-per-second rate.
                self._credit(mac, tx_add, rx_add)
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
        self._reason_bytes[reason] = self._reason_bytes.get(reason, 0) + rec.in_bytes + rec.out_bytes

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

    def _credit(self, mac: str, tx_add: int, rx_add: int) -> None:
        """Add an attributed record's bytes to the device's current-window
        tally. _flush rolls these into the per-MAC history ring and converts
        the windowed byte total into a bits-per-second rate.

        The upload and download halves of one connection arrive as separate
        records and both land on the same MAC; re-exports of a long-lived flow
        simply add the next chunk of bytes — there's no per-flow rate to
        double-count, so no 5-tuple bookkeeping is needed."""
        if tx_add > 0:
            self._cur_tx[mac] = self._cur_tx.get(mac, 0) + tx_add
        if rx_add > 0:
            self._cur_rx[mac] = self._cur_rx.get(mac, 0) + rx_add

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
        self._last_flush = now
        # Move this window's freshly-credited bytes into each device's history
        # ring, then reset the current-window tallies. Done even in diagnostic
        # mode so the accumulators can't grow without bound.
        for mac in set(self._cur_tx) | set(self._cur_rx):
            self._history.setdefault(mac, deque()).append(
                (now, self._cur_tx.get(mac, 0), self._cur_rx.get(mac, 0))
            )
        self._cur_tx.clear()
        self._cur_rx.clear()
        # Evict deposits that have aged out of the sliding window; drop devices
        # whose window is now empty.
        cutoff = now - RATE_WINDOW_S
        for mac, hist in list(self._history.items()):
            while hist and hist[0][0] < cutoff:
                hist.popleft()
            if not hist:
                del self._history[mac]

        # Diagnostic mode: tally only (reason_bytes is kept in _attribute);
        # don't persist samples.
        if not self.write_samples:
            return
        # Drop the IP→MAC cache periodically so DHCP changes get picked up.
        if now >= self._cache_expires_at:
            self._ip_mac_cache.clear()
            self._cache_expires_at = now + 30.0
        # Sliding-window rate per device: Σbytes in the last RATE_WINDOW_S × 8
        # ÷ the window. Bounded by the link; smooth across the router's sparse
        # exports. A device's rate falls to zero on its own once its window
        # empties.
        active_now: set[str] = set()
        for mac, hist in self._history.items():
            tx_bps = sum(d[1] for d in hist) * 8 / RATE_WINDOW_S
            rx_bps = sum(d[2] for d in hist) * 8 / RATE_WINDOW_S
            if tx_bps > 0 or rx_bps > 0:
                db.insert_sample(mac, tx_bps, rx_bps)
                active_now.add(mac)
        # Devices active last flush but quiet now → write one explicit 0 so the
        # UI drops them back to idle instead of holding a stale rate.
        for mac in self._active_macs - active_now:
            db.insert_sample(mac, 0.0, 0.0)
        self._active_macs = active_now

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
            "reason_bytes": dict(self._reason_bytes),
            "templates_known": sorted(self._templates.keys()),
            # Full field layout (field_type, length) per template so we can
            # see exactly what the router is exporting — the field IDs tell
            # us whether IPV4_SRC/DST (8/12), IPV6 (27/28), IN/OUT_BYTES
            # (1/23) etc. are present and at what widths.
            "templates": {
                tid: [[ftype, flen] for ftype, flen in fields]
                for tid, fields in self._templates.items()
            },
            "tracked_devices": len(self._history),
            "windowed_bytes_tx": sum(d[1] for h in self._history.values() for d in h),
            "windowed_bytes_rx": sum(d[2] for h in self._history.values() for d in h),
        }

    def recent(self, limit: int = 50) -> list[dict]:
        """Last N parsed flow records — for diagnosing 'is the router
        sending what we expect' questions without tcpdump."""
        if limit <= 0:
            return []
        return list(self._recent[-limit:])


netflow = NetflowCollector()
