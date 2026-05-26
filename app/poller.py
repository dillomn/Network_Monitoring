import asyncio
import logging
import time

from . import db
from .collectors.ssh import DraytekCollector, DraytekSession
from .config import settings

log = logging.getLogger(__name__)


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

    async def start(self) -> None:
        db.init_db()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        prune_counter = 0
        while not self._stop.is_set():
            try:
                await self._poll_once()
                self.last_poll_ok = True
                self.last_error = None
            except Exception as e:
                self.last_poll_ok = False
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Poll failed")

            self.last_poll_ts = int(time.time())
            prune_counter += 1
            if prune_counter >= 360:  # roughly hourly at 10s interval
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
        """One SSH session per poll: connect, batch all queries, disconnect.

        The batch is cheap (4 + N commands where N is the device count) and
        per-poll connect avoids stale-session headaches with DrayTek's SSH
        idle timeout. If N grows huge we can switch to keep-alive later.
        """
        async with DraytekSession() as session:
            # First poll caches model/firmware for /api/health visibility.
            if self.router_model is None:
                info = await self.collector.router_info(session)
                self.router_model = info.model
                self.router_firmware = info.firmware
                log.info("Connected to %s (firmware %s)", info.model, info.firmware)

            devices = await self.collector.devices(session)
            for d in devices:
                db.upsert_device(d.mac, d.ip, d.hostname)

            ip_to_mac = {d.ip: d.mac for d in devices}
            flows = await self.collector.flow(list(ip_to_mac.keys()), session)

        # DB writes outside the SSH session — sqlite is local, sub-ms.
        for f in flows:
            mac = f.mac or ip_to_mac.get(f.ip)
            if not mac:
                continue
            db.insert_sample(mac, f.tx_bps, f.rx_bps, f.sessions)

        self.last_device_count = len(devices)


poller = Poller()
