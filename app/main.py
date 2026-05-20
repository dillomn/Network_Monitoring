import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, oui
from .config import settings
from .draytek import DraytekClient
from .poller import poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("draymon")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await poller.start()
    log.info("Poller started (router=%s every %ss)", settings.router_host, settings.poll_interval)
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


@app.get("/api/health")
async def health() -> dict:
    return {
        "router": settings.router_host,
        "poll_interval_s": settings.poll_interval,
        "last_poll_ts": poller.last_poll_ts,
        "last_poll_age_s": (int(time.time()) - poller.last_poll_ts) if poller.last_poll_ts else None,
        "last_poll_ok": poller.last_poll_ok,
        "last_error": poller.last_error,
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


# ------- Debug endpoints (use to tune scraping against your firmware) -------

@app.get("/debug/discover")
async def debug_discover() -> JSONResponse:
    """Authenticate against the router, then walk the SPA's JS bundles to
    find candidate JSON API endpoints (DHCP table, Data Flow, etc.)."""
    client = DraytekClient()
    try:
        return JSONResponse(await client.discover_api())
    finally:
        await client.close()


@app.get("/debug/login")
async def debug_login() -> JSONResponse:
    """Returns what every login strategy sent and what the router responded
    with. Use this when 'Router login failed' shows up — paste the output
    so we can match the auth flow to your specific firmware."""
    client = DraytekClient()
    try:
        return JSONResponse(await client.diagnose_login())
    finally:
        await client.close()


@app.get("/debug/raw", response_class=PlainTextResponse)
async def debug_raw(page: str = Query("flow", pattern="^(flow|dhcp)$")) -> str:
    """Returns the raw HTML the scraper fetched. Use this when devices or
    flow rows aren't parsing as expected."""
    client = DraytekClient()
    try:
        if not await client.login():
            raise HTTPException(502, "router login failed")
        html = await (client.fetch_flow_html() if page == "flow" else client.fetch_dhcp_html())
        if html is None:
            raise HTTPException(404, f"no candidate {page} URL responded")
        return html
    finally:
        await client.close()


@app.get("/debug/parsed")
async def debug_parsed(page: str = Query("flow", pattern="^(flow|dhcp)$")) -> JSONResponse:
    client = DraytekClient()
    try:
        if not await client.login():
            raise HTTPException(502, "router login failed")
        if page == "flow":
            data = [vars(s) for s in await client.get_flow()]
        else:
            data = [vars(d) for d in await client.get_devices()]
        return JSONResponse(data)
    finally:
        await client.close()
