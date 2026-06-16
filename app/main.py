import asyncio
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
    # NetFlow/IPFIX is the per-device source: each flow record credits the
    # LAN IP's bytes (see app/collectors/netflow.py). The SSH poller only
    # handles discovery, WAN totals, and the NAT port-map that feeds
    # attribution. The parser handles both v9 and IPFIX.
    #
    # Accuracy caveat (unverified on this hardware): if Hardware
    # Acceleration is enabled on the router, accelerated flows may bypass
    # the CPU flow exporter and undercount. Verify against a known transfer
    # via /api/netflow/stats `reason_bytes`; if the bytes don't land there,
    # disable Hardware Acceleration on the DrayTek (System Maintenance) and
    # re-check. The /debug/ssh/* endpoints can cross-check per-device rates
    # against the router's own Data Flow Monitor while you calibrate.
    await netflow.start(settings.netflow_port)
    log.info("NetFlow/IPFIX listener on udp/%s (crediting per-device samples)", settings.netflow_port)
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


def _check(check_id: str, label: str, status: str, detail: str, hint: str | None = None) -> dict:
    """One diagnostics row. status is ok | warn | fail | info."""
    row = {"id": check_id, "label": label, "status": status, "detail": detail}
    if hint:
        row["hint"] = hint
    return row


def _human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} PB"


async def _tcp_reachable(host: str, port: int, timeout: float = 4.0) -> tuple[bool, str | None]:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


_VERSION_LABEL = {10: "IPFIX (v10)", 9: "NetFlow v9"}


@app.get("/api/diagnostics")
async def diagnostics() -> dict:
    """Health + data-quality checks for the settings panel, focused on the
    questions that actually come up in operation: is the router link up, is
    flow data arriving (and in the expected format), WHERE are the bytes being
    credited (the accuracy lens — the row that explains a download that never
    showed up), and is the DB healthy. Each row is {id,label,status,detail
    [,hint]}; status is ok | warn | fail | info."""
    now = int(time.time())
    nf = netflow.stats()
    checks: list[dict] = []

    checks.append(_check(
        "config", "Configuration", "info",
        f"router {settings.router_host}:{settings.router_ssh_port} as "
        f"{settings.router_ssh_user} • NetFlow udp/{settings.netflow_port} • "
        f"poll {settings.poll_interval}s • retention {settings.retention_days}d • "
        f"LAN prefixes: {settings.lan_prefixes}",
    ))

    # --- Router link: SSH session + poll loop in one row. Only runs the TCP
    # probe when polling is unhealthy, to tell "router down" from "bad creds".
    poll_age = (now - poller.last_poll_ts) if poller.last_poll_ts else None
    poll_fresh = poll_age is not None and poll_age <= max(10, settings.poll_interval * 5)
    if poller.last_poll_ok and poll_fresh and poller.router_model:
        checks.append(_check(
            "router", "Router link", "ok",
            f"{poller.router_model} (fw {poller.router_firmware}) — last poll "
            f"{poll_age}s ago, {poller.last_device_count} devices discovered",
        ))
    else:
        reachable, err = await _tcp_reachable(settings.router_host, settings.router_ssh_port)
        if not reachable:
            checks.append(_check(
                "router", "Router link", "fail",
                f"TCP {settings.router_host}:{settings.router_ssh_port} unreachable — {err}",
                "Confirm the router is on the LAN and SSH is enabled "
                "(System Maintenance → Management → SSH).",
            ))
        else:
            checks.append(_check(
                "router", "Router link", "warn" if poller.router_model else "fail",
                poller.last_error or "port open but no successful SSH poll yet",
                "Check ROUTER_SSH_USER / ROUTER_SSH_PASSWORD in .env. For "
                "'no matching cipher', widen LEGACY_SSH_KWARGS in app/collectors/ssh.py.",
            ))

    # --- Flow ingest: listener + packet flow + export format in one row.
    age = nf["last_packet_age_s"]
    if not nf["listening_port"]:
        checks.append(_check(
            "nf", "Flow ingest", "fail",
            "listener not bound — check NETFLOW_PORT and that nothing else owns the port",
        ))
    elif nf["packets_received"] == 0:
        checks.append(_check(
            "nf", "Flow ingest", "warn",
            f"listening on udp/{nf['listening_port']} but 0 packets received",
            "Router: System Maintenance → NetFlow → Collector IP = this host, "
            f"Port {settings.netflow_port}, version IPFIX (or v9).",
        ))
    else:
        fresh = age is not None and age < 30
        fmt = _VERSION_LABEL.get(nf.get("last_version"), f"v{nf.get('last_version')}")
        errored = nf.get("records_errored", 0)
        detail = (f"{fmt} • {nf['packets_received']} packets / {nf['records_parsed']} records, "
                  f"{len(nf['templates_known'])} templates; last packet {age}s ago"
                  + ("" if fresh else " (stale)")
                  + (f"; from {nf['last_router_addr']}" if nf["last_router_addr"] else ""))
        if errored:
            detail += f" • {errored} parse/attribute errors"
        checks.append(_check(
            "nf", "Flow ingest", "ok" if (fresh and not errored) else "warn", detail,
            None if not errored else
            "Records are failing mid-attribution — a parser/template mismatch. "
            "Check container logs and /api/netflow/recent.",
        ))

    # --- Byte attribution: where the traffic volume is actually landing.
    # This is the row to read when a known download didn't show up: the bytes
    # went SOMEWHERE — outbound/inbound means credited, the other reasons say
    # exactly which guard dropped them.
    reason_bytes = nf.get("reason_bytes", {})
    if reason_bytes:
        credited = reason_bytes.get("outbound", 0) + reason_bytes.get("inbound", 0)
        total = sum(reason_bytes.values()) or 1
        pct = 100 * credited / total
        top = " • ".join(f"{k} {_human_bytes(v)}"
                         for k, v in sorted(reason_bytes.items(), key=lambda kv: -kv[1])[:4])
        hints = []
        if reason_bytes.get("both_public", 0) / total > 0.10:
            hints.append(
                "large 'both_public' share usually means IPv6: a device downloading over a "
                "global v6 address isn't matched by the default LAN_PREFIXES — check "
                "/api/netflow/recent for v6 src/dst during a test download and add your "
                "delegated prefix (e.g. 2a02:xxxx::/64) to LAN_PREFIXES in .env"
            )
        if reason_bytes.get("no_mac", 0) / total > 0.10:
            hints.append(
                "'no_mac' bytes are flows for IPs discovery hasn't mapped — often inbound "
                "NAT records whose port-map entry was already gone, or v6 addresses with "
                "no MAC in the record"
            )
        checks.append(_check(
            "nf_bytes", "Byte attribution", "ok" if pct >= 80 else "warn",
            f"{pct:.0f}% of bytes credited to devices • {top}",
            "; ".join(hints) if hints else None,
        ))
    else:
        checks.append(_check("nf_bytes", "Byte attribution", "info",
                             "no byte-carrying flow records yet"))

    # --- Database: writable, and how much history it actually holds.
    try:
        counts = db.table_counts()
        oldest = counts.get("oldest_sample_ts")
        span = (f"{(now - oldest) / 86400:.1f}d of history (retention {settings.retention_days}d)"
                if oldest else "no samples yet")
        checks.append(_check(
            "db", "Database", "ok",
            f"{counts['devices']} devices, {counts['samples']} flow samples, "
            f"{counts['wan_samples']} WAN samples • {span} • {settings.db_path}",
        ))
    except Exception as e:
        checks.append(_check("db", "Database", "fail", f"{type(e).__name__}: {e}"))

    if any(c["status"] == "fail" for c in checks):
        summary = "fail"
    elif any(c["status"] == "warn" for c in checks):
        summary = "warn"
    else:
        summary = "ok"
    return {"generated_ts": now, "summary": summary, "checks": checks}


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
    deltas between polls. Independent of the NetFlow per-device path."""
    return db.list_wan_current()


@app.get("/api/wan/history")
async def wan_history_api(wan: str = Query(...), hours: int = Query(24, ge=1, le=24 * 30)) -> dict:
    since = int(time.time()) - hours * 3600
    points = db.wan_history(wan.upper(), since)
    return {"wan": wan.upper(), "since": since, "points": points}


@app.get("/api/devices")
async def devices() -> list[dict]:
    rows = db.list_devices_with_current()
    sessions = poller.sessions_by_ip()
    now = int(time.time())
    for r in rows:
        if not r.get("vendor"):
            r["vendor"] = oui.lookup(r["mac"])
        # rate_pending: open NAT sessions but no fresh sample bucket. NetFlow
        # exports a flow only when it ends, so this device may be mid-transfer
        # and unmeasured — the UI shows "in progress…" instead of a false
        # 0 bps. Limits: NAT entries linger after close (false positive), and
        # a long flow plus background chatter keeps buckets fresh, hiding the
        # badge (false negative). An honest hint, not a meter.
        n = sessions.get(r.get("ip") or "", 0)
        ls = r.get("last_sample")
        fresh = ls is not None and now - ls <= db.CURRENT_STALE_S
        r["active_sessions"] = n
        r["rate_pending"] = n > 0 and not fresh
    return rows


@app.get("/api/devices/{mac}/history")
async def history(mac: str, hours: int = Query(24, ge=1, le=24 * 30)) -> dict:
    since = int(time.time()) - hours * 3600
    points = db.history_for(mac.upper(), since)
    return {"mac": mac.upper(), "since": since, "points": points}


@app.get("/api/devices/{mac}/usage")
async def usage(
    mac: str,
    hours: int = Query(24, ge=1, le=24 * 30),
    bucket_s: int = Query(3600, ge=60, le=86400),
) -> dict:
    """Transfer volume per time bin — the exact "who used how much, when"
    view. Unlike /history (a rate whose shape inside a long flow is a
    uniform-spread estimate), these sums equal what the flow records
    reported."""
    since = int(time.time()) - hours * 3600
    points = db.usage_for(mac.upper(), since, bucket_s)
    return {"mac": mac.upper(), "since": since, "bucket_s": bucket_s, "points": points}


@app.get("/api/usage/total")
async def usage_total_api(
    hours: int = Query(24, ge=1, le=24 * 30),
    bucket_s: int = Query(3600, ge=60, le=86400),
) -> dict:
    """Network-wide volume per time bin, all devices summed — the home page
    chart. Same merged series as the per-device view."""
    since = int(time.time()) - hours * 3600
    return {"since": since, "bucket_s": bucket_s, "points": db.usage_total(since, bucket_s)}


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
