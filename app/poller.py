import asyncio
import logging
import time
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
        """Reuse the persistent SSH session across polls.

        Layout:
            - Cache model/firmware on first successful query.
            - Run DHCP + ARP to learn the LAN.
            - Run `show traffic <ip>` for each discovered IP.
            - DB writes happen after queries so a slow disk doesn't hold
              the SSH session open longer than needed.
        """
        session = await self._ensure_session()

        if self.router_model is None:
            info = await self.collector.router_info(session)
            self.router_model = info.model
            self.router_firmware = info.firmware
            log.info("Connected to %s (firmware %s)", info.model, info.firmware)

        devices = await self.collector.devices(session)
        ip_to_mac = {d.ip: d.mac for d in devices}
        flows = await self.collector.flow(list(ip_to_mac.keys()), session)

        for d in devices:
            db.upsert_device(d.mac, d.ip, d.hostname)
        for f in flows:
            mac = f.mac or ip_to_mac.get(f.ip)
            if not mac:
                continue
            db.insert_sample(mac, f.tx_bps, f.rx_bps, f.sessions)

        self.last_device_count = len(devices)

        await self._update_wan_rate(session)

    async def _update_wan_rate(self, session: DraytekSession) -> None:
        """Read cumulative WAN byte counters and persist the bps delta
        against the previous reading. Independent of the per-IP buffer,
        so this is a true near-instantaneous rate at our poll cadence.

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
