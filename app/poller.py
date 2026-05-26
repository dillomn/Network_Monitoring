import asyncio
import logging
import time

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

    async def start(self) -> None:
        db.init_db()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        await self._drop_session()

    async def _ensure_session(self) -> DraytekSession:
        if self._session is None:
            s = DraytekSession()
            await s.__aenter__()
            self._session = s
            log.info("SSH session opened to %s:%s", settings.router_host, settings.router_ssh_port)
        return self._session

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
                await self._poll_once()
                self.last_poll_ok = True
                self.last_error = None
            except _TRANSPORT_ERRORS as e:
                # Transport-level failure — invalidate the cached session
                # so the next poll opens a fresh one.
                self.last_poll_ok = False
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("Poll failed (transport): %s — will reconnect next cycle", self.last_error)
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


poller = Poller()
