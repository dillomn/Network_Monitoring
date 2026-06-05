import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, oui
from .collectors.netflow import netflow
from .collectors.ssh import DraytekSession
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
    # NetFlow/IPFIX listener runs in DIAGNOSTIC-ONLY mode (write_samples
    # False): it receives + tallies flow records into /api/netflow/stats
    # reason_bytes but does NOT write per-device samples — the SSH Data
    # Flow Monitor poll drives the UI. This lets us A/B test whether an
    # IPFIX export actually carries the bytes (point the router's flow
    # export at this host:NETFLOW_PORT, download a known size, and read
    # reason_bytes). The parser handles both v9 and IPFIX. If IPFIX proves
    # accurate, flip write_samples back on and retire the SSH poll.
    netflow.write_samples = False
    await netflow.start(settings.netflow_port)
    log.info("NetFlow/IPFIX diagnostic listener on udp/%s (not writing samples)", settings.netflow_port)
    try:
        yield
    finally:
        await netflow.stop()
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
    nf = netflow.stats()
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
        "netflow_port": nf["listening_port"],
        "netflow_packets": nf["packets_received"],
        "netflow_records": nf["records_processed"],
        "netflow_last_packet_age_s": nf["last_packet_age_s"],
        "netflow_router_addr": nf["last_router_addr"],
    }


@app.get("/api/netflow/stats")
async def netflow_stats_api() -> dict:
    """Diagnostics for the NetFlow listener — packet/record counts, the
    list of template IDs seen, last packet timestamp. Use it to verify
    the router is actually exporting to us."""
    return netflow.stats()


@app.get("/api/netflow/recent")
async def netflow_recent_api(limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    """Most recent N parsed NetFlow records. Useful when device totals
    look wrong — shows exactly what the router is sending (direction,
    src/dst, byte counts) so you can verify the attribution logic."""
    return netflow.recent(limit)


@app.get("/api/portmap")
async def portmap_api(limit: int = Query(50, ge=1, le=500)) -> dict:
    """Snapshot of the NAT port-mapping table the poller maintains via
    `show portmap` on the router. Each entry tells us which real LAN
    device a (pseudo_ip, pseudo_port) WAN-side slot belongs to —
    the table NetFlow inbound attribution uses to reverse-NAT."""
    items = [
        {"pseudo_ip": k[0], "pseudo_port": k[1], "private_ip": v}
        for k, v in poller._portmap.items()
    ]
    return {"size": len(items), "entries": items[:limit]}


@app.get("/api/wan/current")
async def wan_current() -> list[dict]:
    """Latest tx_bps/rx_bps per WAN, derived from `show statistic` byte
    deltas between polls. Independent of per-IP `show traffic` buffer."""
    return db.list_wan_current()


@app.get("/api/wan/history")
async def wan_history_api(wan: str = Query(...), hours: int = Query(24, ge=1, le=24 * 30)) -> dict:
    since = int(time.time()) - hours * 3600
    points = db.wan_history(wan.upper(), since)
    return {"wan": wan.upper(), "since": since, "points": points}


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
    """Run `sys version` over the poller's shared SSH session and return
    model/firmware. Sanity check that SSH credentials and the legacy-
    algorithm list work for your unit."""
    try:
        async with poller.borrow_session() as s:
            info = await poller.collector.router_info(s)
        return {"ok": True, "model": info.model, "firmware": info.firmware, "router_name": info.router_name}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/debug/ssh/exec", response_class=PlainTextResponse)
async def debug_exec(cmd: str = Query(..., min_length=1, max_length=200)) -> str:
    """Run an arbitrary (allowlisted-prefix) CLI command on the poller's
    shared session. The allowlist is anchored at the *start* of cmd to
    prevent shell-style command chaining (DrayTek CLI doesn't support
    `;`/`&&` but we filter anyway as defence in depth)."""
    if not any(cmd.startswith(prefix) for prefix in _DEBUG_CMD_ALLOWLIST):
        raise HTTPException(400, f"cmd must start with one of: {_DEBUG_CMD_ALLOWLIST}")
    try:
        async with poller.borrow_session() as s:
            return await s.query(cmd)
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/debug/ssh/devices")
async def debug_devices() -> JSONResponse:
    """Parsed device list straight from SSH (no DB)."""
    try:
        async with poller.borrow_session() as s:
            devs = await poller.collector.devices(s)
        return JSONResponse([vars(d) for d in devs])
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/debug/ssh/flow")
async def debug_flow(ip: str | None = Query(None)) -> JSONResponse:
    """Per-IP bandwidth as the collector sees it. If `ip` is given, polls
    just that IP; otherwise discovers devices first then polls all."""
    try:
        async with poller.borrow_session() as s:
            if ip:
                ips = [ip]
            else:
                devs = await poller.collector.devices(s)
                ips = [d.ip for d in devs if d.ip]
            samples = await poller.collector.flow(ips, s)
        return JSONResponse([vars(sample) for sample in samples])
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/debug/ssh/wan")
async def debug_wan() -> JSONResponse:
    """Per-WAN lifetime byte counters from `show statistic`."""
    try:
        async with poller.borrow_session() as s:
            stats = await poller.collector.wan_totals(s)
        return JSONResponse([vars(stat) for stat in stats])
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/debug/ssh/raw-traffic")
async def debug_raw_traffic(ip: str = Query(...)) -> JSONResponse:
    """Return the raw `show traffic <ip> tx/rx` series plus what each
    supported unit interpretation would compute for the latest sample.
    Compare the values to the rate the DrayTek's Data Flow Monitor web UI
    shows for the same IP — the matching column is your `TRAFFIC_UNIT`."""
    from .collectors.ssh import _UNIT_TO_BPS, series_to_bps
    from .parsers.cli import parse_traffic_series, smoothed_sample
    try:
        async with poller.borrow_session() as s:
            tx_cmd = f"show traffic {ip} tx"
            rx_cmd = f"show traffic {ip} rx"
            tx_raw = await s.query(tx_cmd)
            rx_raw = await s.query(rx_cmd)
        tx = parse_traffic_series(tx_raw, tx_cmd)
        rx = parse_traffic_series(rx_raw, rx_cmd)
        tx_smoothed = smoothed_sample(tx, settings.traffic_smoothing_samples)
        rx_smoothed = smoothed_sample(rx, settings.traffic_smoothing_samples)
        interpretations = {
            unit: {
                "tx_bps": tx_smoothed * factor,
                "rx_bps": rx_smoothed * factor,
            }
            for unit, factor in _UNIT_TO_BPS.items()
        }
        return JSONResponse({
            "ip": ip,
            "current_unit": settings.traffic_unit,
            "current_tx_bps": series_to_bps(tx),
            "current_rx_bps": series_to_bps(rx),
            "smoothed_raw_tx": tx_smoothed,
            "smoothed_raw_rx": rx_smoothed,
            "tx_series_tail": tx[-20:],
            "rx_series_tail": rx[-20:],
            "tx_series_length": len(tx),
            "rx_series_length": len(rx),
            "interpretations": interpretations,
        })
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/debug/calibrate")
async def debug_calibrate(ip: str = Query(...), wait_s: int = Query(60, ge=10, le=300)) -> JSONResponse:
    """Snapshot `show traffic <ip> rx` twice, `wait_s` apart, return both
    plus a diff so we can see which positions in the time-series moved and
    in which direction. Note: this command opens its OWN session (not the
    poller's) since it holds for `wait_s` seconds — we don't want to block
    the poll loop that long."""
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
