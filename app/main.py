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

@app.get("/debug/token")
async def debug_token() -> dict:
    """Logs in fresh and reports the sFormAuthStr token we resolved, plus
    every session cookie the router set. Compare the cookie value to the
    `sFormAuthStr=...` value the browser uses — if they match in length
    and format, the cookie-as-token strategy should work."""
    client = DraytekClient()
    try:
        ok = await client.login()
        cookies = {}
        if client._client is not None:
            for name, value in client._client.cookies.items():
                cookies[name] = {
                    "value": value,
                    "length": len(value) if value else 0,
                }
        return {
            "login_ok": ok,
            "token": client._form_auth_token,
            "token_len": len(client._form_auth_token) if client._form_auth_token else 0,
            "discovered_base_url": client._discovered_base,
            "session_cookies": cookies,
        }
    finally:
        await client.close()


@app.get("/debug/discover")
async def debug_discover() -> JSONResponse:
    """Authenticate against the router, then walk the SPA's JS bundles to
    find candidate JSON API endpoints (DHCP table, Data Flow, etc.)."""
    client = DraytekClient()
    try:
        return JSONResponse(await client.discover_api())
    finally:
        await client.close()


@app.get("/debug/login-trace")
async def debug_login_trace() -> JSONResponse:
    """Walks the login flow WITHOUT following redirects so we can see
    every Set-Cookie / Location header and full body chunk-by-chunk.
    Used to find where on this firmware the sFormAuthStr token actually
    arrives (login response body? redirect Location header? Set-Cookie?)."""
    import base64
    import httpx
    from app.config import settings
    from app.draytek import DraytekClient, LOGIN_PATHS

    client = DraytekClient()
    await client._discover_base()
    base = client._base()

    async with httpx.AsyncClient(
        base_url=base,
        verify=settings.router_verify_ssl,
        timeout=15.0,
        follow_redirects=False,
        headers={"User-Agent": "Mozilla/5.0 (DraytekMonitor)"},
    ) as c:
        steps = []

        # Prime
        try:
            r = await c.get("/")
            steps.append({"step": "GET /", "status": r.status_code,
                          "location": r.headers.get("location"),
                          "set_cookie": r.headers.get_list("set-cookie"),
                          "body_head": r.text[:600]})
        except Exception as e:
            steps.append({"step": "GET /", "error": str(e)})

        # Login POST (no follow)
        aa = base64.b64encode(settings.router_user.encode()).decode()
        ab = base64.b64encode(settings.router_password.encode()).decode()
        for path in LOGIN_PATHS:
            try:
                r = await c.post(path, data={
                    "aa": aa, "ab": ab,
                    "ja_name": settings.router_user, "ja_passwd": "",
                    "username": settings.router_user, "password": settings.router_password,
                })
                steps.append({
                    "step": f"POST {path}",
                    "status": r.status_code,
                    "location": r.headers.get("location"),
                    "set_cookie": r.headers.get_list("set-cookie"),
                    "all_headers": dict(r.headers),
                    "body_len": len(r.text),
                    "body_head": r.text[:1500],
                    "body_token_hits": __import__("re").findall(
                        r"sFormAuthStr=[A-Za-z0-9]{8,}", r.text)[:5],
                })
                # If we got a redirect, manually follow up to 4 hops
                hops = 0
                while hops < 4:
                    loc = r.headers.get("location")
                    if not loc:
                        break
                    next_url = loc if loc.startswith(("http://", "https://")) else loc
                    try:
                        r = await c.get(next_url)
                    except Exception as e:
                        steps.append({"step": f"redirect GET {next_url}", "error": str(e)})
                        break
                    steps.append({
                        "step": f"redirect GET {next_url}",
                        "status": r.status_code,
                        "location": r.headers.get("location"),
                        "set_cookie": r.headers.get_list("set-cookie"),
                        "body_len": len(r.text),
                        "body_head": r.text[:1500],
                        "body_token_hits": __import__("re").findall(
                            r"sFormAuthStr=[A-Za-z0-9]{8,}", r.text)[:5],
                    })
                    hops += 1
                    if r.status_code < 300:
                        break
                if r and r.status_code == 200:
                    break  # we got somewhere
            except Exception as e:
                steps.append({"step": f"POST {path}", "error": str(e)})

        return JSONResponse({"base_url": base, "steps": steps,
                             "final_cookies": {k: v for k, v in c.cookies.items()}})


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
