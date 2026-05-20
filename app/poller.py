import asyncio
import logging
import time

from . import db
from .config import settings
from .draytek import DraytekClient

log = logging.getLogger(__name__)


class Poller:
    def __init__(self) -> None:
        self.client = DraytekClient()
        self.last_poll_ts: int = 0
        self.last_poll_ok: bool = False
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._authed = False

    async def start(self) -> None:
        db.init_db()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        await self.client.close()

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
                self._authed = False

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
        if not self._authed:
            ok = await self.client.login()
            if not ok:
                raise RuntimeError("Router login failed")
            self._authed = True

        devices = await self.client.get_devices()
        for d in devices:
            db.upsert_device(d.mac, d.ip, d.hostname)

        ip_to_mac = {d.ip: d.mac for d in devices}

        flows = await self.client.get_flow()
        for f in flows:
            mac = f.mac or ip_to_mac.get(f.ip)
            if not mac:
                continue
            # If flow gave us a MAC we hadn't seen in DHCP, register it.
            if f.mac and f.mac not in ip_to_mac.values():
                db.upsert_device(f.mac, f.ip, None)
            db.insert_sample(mac, f.tx_bps, f.rx_bps, f.sessions)


poller = Poller()
