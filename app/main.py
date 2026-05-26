import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, oui
from .collectors.ssh import DraytekCollector, DraytekSession
from .config import settings
from .poller import poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("draymon")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await poller.start()
    log.info(
        "Poller started (router=%s ssh:%s every %ss)",
        settings.router_host, settings.router_ssh_port, settings.poll_interval,
    )
    try:
        yield
    finally:
        await poller.stop()


app = FastAPI(title="DrayTek Network Monitor", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# --------------- App API (consumed by the bundled frontend) ---------------

@app.get("/api/health")
async def health() -> dict:
    return {
        "router": settings.router_host,
        "router_model": poller.router_model,
        "router_firmware": poller.router_firmware,
        "poll_interval_s": settings.poll_interval,
        "last_poll_ts": poller.last_poll_ts,
        "last_poll_age_s": (int(time.time()) - poller.last_poll_ts) if poller.last_poll_ts else None,
        "last_poll_ok": poller.last_poll_ok,
        "last_error": poller.last_error,
        "device_count": poller.last_device_count,
    }


@app.get("/api/devices")
async def devices() -> list[dict]:
    rows = db.list_devices_with_current()
    for r in rows:
        if not r.get("vendor"):
            r["vendor"] = oui.lookup(r["mac"])
    return rows


@app.get("/api/devices/{mac}/history")
async def history(mac: str, hours: int = Query(24, ge=1, le=24 * 30)) -> dict:
    since = int(time.time()) - hours * 3600
    points = db.history_for(mac.upper(), since)
    return {"mac": mac.upper(), "since": since, "points": points}


class NoteIn(BaseModel):
    note: str


@app.post("/api/devices/{mac}/note")
async def set_note(mac: str, body: NoteIn) -> dict:
    db.set_device_note(mac.upper(), body.note)
    return {"ok": True}


# ---------------------- Debug endpoints (SSH-based) ----------------------
# All endpoints below open a fresh SSH session so they don't interfere
# with the background poller. Hit them with `curl -s localhost:8090/debug/...`.

_DEBUG_CMD_ALLOWLIST = (
    "sys version", "srv dhcp status", "ip arp status", "show statistic",
    "show session", "show traffic",  # show traffic <ip> tx/rx
)


@app.get("/debug/ssh/info")
async def debug_info() -> dict:
    """Connects, runs `sys version`, returns model/firmware. Sanity check
    that SSH credentials and the legacy-algorithm list work for your unit."""
    collector = DraytekCollector()
    try:
        info = await collector.router_info()
        return {"ok": True, "model": info.model, "firmware": info.firmware, "router_name": info.router_name}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/debug/ssh/exec", response_class=PlainTextResponse)
async def debug_exec(cmd: str = Query(..., min_length=1, max_length=200)) -> str:
    """Run an arbitrary (allowlisted-prefix) CLI command. Used for ad-hoc
    inspection of new firmware output. The allowlist is anchored at the
    *start* of cmd to prevent shell-style command chaining (DrayTek CLI
    doesn't support `;`/`&&` but we filter anyway as defence in depth)."""
    if not any(cmd.startswith(prefix) for prefix in _DEBUG_CMD_ALLOWLIST):
        raise HTTPException(400, f"cmd must start with one of: {_DEBUG_CMD_ALLOWLIST}")
    try:
        async with DraytekSession() as s:
            return await s.query(cmd)
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/debug/ssh/devices")
async def debug_devices() -> JSONResponse:
    """Parsed device list straight from SSH (no DB)."""
    collector = DraytekCollector()
    try:
        devs = await collector.devices()
        return JSONResponse([vars(d) for d in devs])
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/debug/ssh/flow")
async def debug_flow(ip: str | None = Query(None)) -> JSONResponse:
    """Per-IP bandwidth as the collector sees it. If `ip` is given, polls
    just that IP; otherwise discovers devices first then polls all."""
    collector = DraytekCollector()
    try:
        async with DraytekSession() as s:
            if ip:
                ips = [ip]
            else:
                devs = await collector.devices(s)
                ips = [d.ip for d in devs if d.ip]
            samples = await collector.flow(ips, s)
        return JSONResponse([vars(s) for s in samples])
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/debug/ssh/wan")
async def debug_wan() -> JSONResponse:
    """Per-WAN lifetime byte counters from `show statistic`."""
    collector = DraytekCollector()
    try:
        stats = await collector.wan_totals()
        return JSONResponse([vars(s) for s in stats])
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/debug/calibrate")
async def debug_calibrate(ip: str = Query(...), wait_s: int = Query(60, ge=10, le=300)) -> JSONResponse:
    """Snapshot `show traffic <ip> rx` twice, `wait_s` apart, return both
    plus a diff so we can see which positions in the time-series moved and
    in which direction. Use to verify the 'last sample = newest' and
    sample-interval assumptions baked into the parser."""
    import asyncio
    from .parsers.cli import parse_traffic_series
    cmd = f"show traffic {ip} rx"
    try:
        async with DraytekSession() as s:
            first = parse_traffic_series(await s.query(cmd), cmd)
            await asyncio.sleep(wait_s)
            second = parse_traffic_series(await s.query(cmd), cmd)
        changed = [i for i, (a, b) in enumerate(zip(first, second)) if a != b]
        return JSONResponse({
            "ip": ip,
            "wait_s": wait_s,
            "len_first": len(first),
            "len_second": len(second),
            "first_tail": first[-10:],
            "second_tail": second[-10:],
            "changed_indices": changed,
            "changed_count": len(changed),
            "hint": (
                "If changed_indices cluster near the END of the array, last=newest. "
                "If near the START, first=newest. The spacing between changed indices "
                "indicates the sample period (e.g. ~1 changed index per 60s of wait = per-minute samples)."
            ),
        })
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")
