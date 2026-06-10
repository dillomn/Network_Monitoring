import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager

import asyncssh

from . import db
from .collectors.ssh import DraytekCollector, DraytekSession
from .config import settings

log = logging.getLogger(__name__)

# Exceptions on which we drop the persistent session and reconnect next poll.
# Anything else (parser bugs, DB errors) is logged but the session stays open.
_TRANSPORT_ERRORS = (
    asyncssh.Error,
    asyncio.TimeoutError,
    ConnectionError,
    EOFError,
    OSError,
)

# Max devices in one Data Flow Monitor rotation pass. One device is polled per
# poll cycle (2 CLI commands), so with the default 1 s interval a device's
# live rate refreshes roughly every N seconds where N = active device count
# (capped here). Devices are prioritised by open NAT session count.
DFM_MAX_DEVICES = 12


class Poller:
    def __init__(self) -> None:
        self.collector = DraytekCollector()
        self.last_poll_ts: int = 0
        self.last_poll_ok: bool = False
        self.last_error: str | None = None
        self.last_device_count: int = 0
        self.router_model: str | None = None
        self.router_firmware: str | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._session: DraytekSession | None = None
        # Serialises access to the single SSH session. DrayTek embedded
        # SSH stacks misbehave under concurrent connections — keep it to one
        # session, taken in turn by the poller and any debug endpoints.
        self._session_lock = asyncio.Lock()
        # Cumulative WAN byte counters from the previous poll, keyed by
        # wan name. Used to compute live bps from `show statistic` deltas.
        self._last_wan_tx: dict[str, int] = {}
        self._last_wan_rx: dict[str, int] = {}
        self._last_wan_ts: int = 0
        # NAT port-mapping table from `show portmap`:
        # {(pseudo_ip, pseudo_port): private_ip}. Lets the NetFlow
        # collector reverse-NAT inbound records whose dst is the router's
        # WAN-side address back to the real LAN device. Refreshed on a
        # slower cadence than the device discovery polls because `show
        # portmap` can return thousands of rows on a busy router.
        self._portmap: dict[tuple[str, int], str] = {}
        self._portmap_poll_counter: int = 0
        # Refresh portmap every N device-discovery polls. With the default
        # 1s poll_interval that's 5s, which is fast enough for NAT entries
        # to be there when a flow record using them arrives.
        self._portmap_poll_every: int = 5
        # Live per-device rates from the router's Data Flow Monitor:
        # {mac: (tx_bps, rx_bps, reading_ts)}. The "now" display prefers
        # these over NetFlow-derived rates because the flow exporter says
        # nothing about a transfer until it ends.
        self.live_rates: dict[str, tuple[float, float, int]] = {}
        # Recent reading history per device: {mac: deque[(ts, tx_bps, rx_bps)]}.
        # The NetFlow collector uses this as the measured rate PROFILE when a
        # flow-end record arrives: instead of spreading the flow's bytes
        # uniformly over its duration, it distributes them proportionally to
        # these readings — measured shape, exact total. Zero readings are kept
        # on purpose (a measured zero shapes the profile too). ~6 h at one
        # reading per 10 s.
        self.live_history: dict[str, deque] = {}
        self.dfm_last_ts: int = 0
        # IPs still to poll in the current DFM rotation pass.
        self._dfm_rotation: list[str] = []

    async def start(self) -> None:
        db.init_db()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        async with self._session_lock:
            await self._drop_session()

    async def _ensure_session(self) -> DraytekSession:
        if self._session is None:
            s = DraytekSession()
            await s.__aenter__()
            self._session = s
            log.info("SSH session opened to %s:%s", settings.router_host, settings.router_ssh_port)
        return self._session

    @asynccontextmanager
    async def borrow_session(self):
        """Lend the poller's SSH session to ad-hoc callers (e.g. debug
        endpoints). Blocks the poll loop while held; release quickly.

        If the session is broken when we release, it's dropped so the next
        poll reconnects.
        """
        async with self._session_lock:
            try:
                session = await self._ensure_session()
                yield session
            except _TRANSPORT_ERRORS:
                await self._drop_session()
                raise

    async def _drop_session(self) -> None:
        """Close and discard the cached session. Safe to call even if the
        session is already half-dead — we suppress errors during teardown
        because the next poll will just reconnect."""
        if self._session is None:
            return
        s, self._session = self._session, None
        try:
            await s.__aexit__(None, None, None)
        except Exception as e:
            log.debug("Ignoring error while closing dead session: %s", e)

    async def _run(self) -> None:
        prune_counter = 0
        while not self._stop.is_set():
            try:
                async with self._session_lock:
                    await self._poll_once()
                self.last_poll_ok = True
                self.last_error = None
            except _TRANSPORT_ERRORS as e:
                # Transport-level failure — invalidate the cached session
                # so the next poll opens a fresh one.
                self.last_poll_ok = False
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("Poll failed (transport): %s — will reconnect next cycle", self.last_error)
                async with self._session_lock:
                    await self._drop_session()
            except Exception as e:
                # Non-transport failure (parser, DB, etc.) — the SSH session
                # is probably fine; keep it and just log.
                self.last_poll_ok = False
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Poll failed (non-transport)")

            self.last_poll_ts = int(time.time())
            prune_counter += 1
            prune_every = max(60, 3600 // max(1, settings.poll_interval))
            if prune_counter >= prune_every:  # roughly hourly
                try:
                    deleted = db.prune_old_samples(settings.retention_days)
                    if deleted:
                        log.info("Pruned %d old samples", deleted)
                except Exception:
                    log.exception("Prune failed")
                prune_counter = 0

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self) -> None:
        """SSH side of the pipeline, all over the one SSH session: device
        discovery (DHCP + ARP), WAN totals, the NAT port-map, and one live
        Data Flow Monitor reading per cycle (round-robin over active devices).
        Discovery and the port-map feed the NetFlow collector's attribution;
        the DFM readings are the live per-device rate source — NetFlow only
        reports a flow when it ends, so mid-transfer the DFM is the only
        signal (see _poll_dfm_one)."""
        session = await self._ensure_session()

        if self.router_model is None:
            info = await self.collector.router_info(session)
            self.router_model = info.model
            self.router_firmware = info.firmware
            log.info("Connected to %s (firmware %s)", info.model, info.firmware)

        devices = await self.collector.devices(session)
        for d in devices:
            db.upsert_device(d.mac, d.ip, d.hostname)
        self.last_device_count = len(devices)

        await self._update_wan_rate(session)
        await self._maybe_update_portmap(session)
        await self._poll_dfm_one(session)

    async def _poll_dfm_one(self, session: DraytekSession) -> None:
        """Read one device's live rate from the router's Data Flow Monitor
        (`show traffic <ip> tx/rx`) — one device per poll cycle so the SSH
        session isn't held for seconds at a time. Rotation covers devices
        with open NAT sessions, busiest first, capped at DFM_MAX_DEVICES.

        Readings land in `live_rates` (the "now" display) and, when nonzero,
        in db.live_samples — the fallback layer charts merge with NetFlow
        data (per-bucket MAX, so NetFlow's exact figures win once the
        flow-end record backfills)."""
        if not self._dfm_rotation:
            per_ip = self.sessions_by_ip()
            self._dfm_rotation = sorted(per_ip, key=lambda ip: -per_ip[ip])[:DFM_MAX_DEVICES]
            if not self._dfm_rotation:
                return
        ip = self._dfm_rotation.pop(0)
        mac = db.mac_for_ip(ip)
        if mac is None:
            return  # not discovered yet — skip this slot, retry next pass
        flows = await self.collector.flow([ip], session)
        if not flows:
            return
        f = flows[0]
        now = int(time.time())
        self.live_rates[mac] = (f.tx_bps, f.rx_bps, now)
        hist = self.live_history.setdefault(mac, deque(maxlen=2200))
        hist.append((now, f.tx_bps, f.rx_bps))
        self.dfm_last_ts = now
        if f.tx_bps > 0 or f.rx_bps > 0:
            bucket = now - (now % db.SAMPLE_BUCKET_S)
            db.upsert_live_sample(mac, bucket, f.tx_bps, f.rx_bps)

    def dfm_stats(self) -> dict:
        """Diagnostics for the live-rate poller."""
        return {
            "tracked": len(self.live_rates),
            "last_reading_ts": self.dfm_last_ts,
            "rotation_pending": len(self._dfm_rotation),
        }

    def lookup_portmap(self, pseudo_ip: str, pseudo_port: int) -> str | None:
        """Reverse-NAT lookup. Given the WAN-side IP+port from an inbound
        NetFlow record, return the real LAN IP (or None if not in table)."""
        return self._portmap.get((pseudo_ip, pseudo_port))

    def sessions_by_ip(self) -> dict[str, int]:
        """Active NAT sessions per LAN IP, from the portmap snapshot. Used by
        the UI to flag devices that have open connections but no flow data yet
        — the router exports a long transfer only when it ends, so "0 bps with
        open sessions" means *unmeasured*, not idle. Heuristic: NAT entries
        linger a while after a connection closes, so a recently-idle device can
        still show sessions."""
        out: dict[str, int] = {}
        for priv_ip in self._portmap.values():
            out[priv_ip] = out.get(priv_ip, 0) + 1
        return out

    async def _maybe_update_portmap(self, session: DraytekSession) -> None:
        """Refresh the NAT port-map on a slower cadence than discovery —
        `show portmap` can return thousands of rows on a busy router, so we
        don't pull it every poll. The NetFlow collector consults this table
        to attribute inbound (downloaded) bytes to the LAN device behind
        NAT; without it, inbound records addressed to the router's WAN IP
        land as `no_mac`. Refreshes immediately on first poll, then every
        `_portmap_poll_every` polls."""
        self._portmap_poll_counter += 1
        if self._portmap and self._portmap_poll_counter < self._portmap_poll_every:
            return
        self._portmap_poll_counter = 0
        try:
            self._portmap = await self.collector.portmap(session)
        except Exception:
            log.exception("portmap refresh failed")

    async def _update_wan_rate(self, session: DraytekSession) -> None:
        """Read cumulative WAN byte counters and persist the bps delta
        against the previous reading. Independent of the NetFlow per-device
        path, so this is a true near-instantaneous rate at our poll cadence.

        Skips a sample if the counter went backwards (router reboot, or
        32-bit wrap on a fast link). We could reconstruct around wrap,
        but a one-poll gap is harmless for a chart."""
        try:
            wan_now = await self.collector.wan_totals(session)
        except Exception:
            log.exception("WAN totals query failed")
            return
        ts_now = int(time.time())
        # Only track WANs that have ever shown traffic — the router reports
        # all 6 WAN slots even when only one is wired up, and we don't want
        # five rows of "WAN3 ↑0bps ↓0bps" in the UI.
        active = [w for w in wan_now if w.tx_bytes > 0 or w.rx_bytes > 0]
        if self._last_wan_ts:
            elapsed = max(1, ts_now - self._last_wan_ts)
            for w in active:
                prev_tx = self._last_wan_tx.get(w.wan)
                prev_rx = self._last_wan_rx.get(w.wan)
                if prev_tx is None or prev_rx is None:
                    continue
                dtx = w.tx_bytes - prev_tx
                drx = w.rx_bytes - prev_rx
                if dtx < 0 or drx < 0:
                    log.debug("WAN %s counter went backwards (tx %d->%d, rx %d->%d); skipping",
                              w.wan, prev_tx, w.tx_bytes, prev_rx, w.rx_bytes)
                    continue
                db.insert_wan_sample(w.wan, (dtx * 8) / elapsed, (drx * 8) / elapsed)
        for w in active:
            self._last_wan_tx[w.wan] = w.tx_bytes
            self._last_wan_rx[w.wan] = w.rx_bytes
        self._last_wan_ts = ts_now


poller = Poller()
