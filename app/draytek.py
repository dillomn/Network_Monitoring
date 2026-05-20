"""DrayTek 2765ax web-UI client.

Logs into the router, scrapes the DHCP table for device identity, and
scrapes the Data Flow Monitor for per-IP bandwidth. Endpoints and HTML
shapes vary between firmware versions, so the parsers fall back across a
few candidate URL paths and tolerate missing columns.

If scraping breaks on a new firmware: hit /debug/raw?page=dhcp (or =flow)
from the running app to see the actual HTML, then adjust _parse_* below.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from .config import settings

log = logging.getLogger(__name__)

MAC_RE = re.compile(r"([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
NUM_RE = re.compile(r"[\d,.]+")

DHCP_PATHS = [
    "/doc/lan_dhcp.sht",
    "/doc/dhcptable.sht",
    "/cgi-bin/v2/dhcptable",
]

FLOW_PATHS = [
    "/doc/dataFlowM.sht",
    "/cgi-bin/v2/dataFlowMonitor",
    "/doc/dataflow.sht",
]

LOGIN_PATHS = [
    "/cgi-bin/wlogin.cgi",
    "/cgi-bin/wlogin1.cgi",
    "/weblogin.htm",
]

LOGIN_FORM_MARKERS = (
    'name="aa"', 'name="ab"', 'name="username"', "wlogin",
    "login.htm", "vigor login", "operation timeout", "session timeout",
)


@dataclass
class Device:
    mac: str
    ip: str
    hostname: str | None = None


@dataclass
class FlowSample:
    ip: str
    mac: str | None
    tx_bps: float
    rx_bps: float
    sessions: int | None = None


def _to_bps(value: str, unit_hint: str = "") -> float:
    """DrayTek shows bandwidth as e.g. '12.3 Kbps' or '1.2 Mbps' or raw bytes/s.

    We coerce everything to bits-per-second.
    """
    if not value:
        return 0.0
    m = NUM_RE.search(value)
    if not m:
        return 0.0
    n = float(m.group(0).replace(",", ""))
    text = (value + " " + unit_hint).lower()
    if "gbps" in text or "gb/s" in text:
        return n * 1_000_000_000
    if "mbps" in text or "mb/s" in text:
        return n * 1_000_000
    if "kbps" in text or "kb/s" in text:
        return n * 1_000
    if "bps" in text or "bit" in text:
        return n
    if "gb" in text:
        return n * 8_000_000_000
    if "mb" in text:
        return n * 8_000_000
    if "kb" in text:
        return n * 8_000
    return n  # assume already bps


class DraytekClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _new_client(self, *, basic_auth: bool = False) -> httpx.AsyncClient:
        auth = (settings.router_user, settings.router_password) if basic_auth else None
        return httpx.AsyncClient(
            base_url=settings.router_base_url,
            verify=settings.router_verify_ssl,
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (DraytekMonitor)"},
            auth=auth,
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._new_client()
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _probe_authenticated(self, client: httpx.AsyncClient) -> bool:
        """Hit a protected page and decide whether we got real content or
        got bounced back to the login form."""
        for path in DHCP_PATHS + FLOW_PATHS:
            try:
                r = await client.get(path)
            except Exception:
                continue
            if r.status_code == 200 and _looks_authenticated(r.text):
                return True
        return False

    async def login(self) -> bool:
        """Try multiple auth strategies. Returns True if any sticks.
        If logged in, self._client is left in an authenticated state.
        """
        await self.close()  # discard any half-state from prior attempts

        # Strategy 1: HTTP Basic Auth (DrayTek's CGI pages accept it on many builds)
        c = self._new_client(basic_auth=True)
        if await self._probe_authenticated(c):
            log.info("Authenticated via HTTP Basic")
            self._client = c
            return True
        await c.aclose()

        # Strategy 2: classic form POST with base64'd creds (older firmware)
        c = self._new_client(basic_auth=False)
        aa = base64.b64encode(settings.router_user.encode()).decode()
        ab = base64.b64encode(settings.router_password.encode()).decode()
        for path in LOGIN_PATHS:
            try:
                await c.get("/")  # prime any session cookie
                r = await c.post(path, data={
                    "aa": aa, "ab": ab,
                    "ja_name": settings.router_user, "ja_passwd": "",
                    "username": settings.router_user, "password": settings.router_password,
                })
                log.debug("Form POST %s -> %s", path, r.status_code)
            except Exception as e:
                log.debug("Form POST %s raised: %s", path, e)
                continue
            if await self._probe_authenticated(c):
                log.info("Authenticated via form POST at %s", path)
                self._client = c
                return True

        await c.aclose()
        log.error("All login strategies failed. Hit /debug/login for diagnostics.")
        return False

    async def diagnose_login(self) -> dict:
        """Run every strategy independently and report what the router said.
        Used by the /debug/login endpoint to help adjust the auth flow to
        a specific firmware build."""
        diag: dict = {
            "base_url": settings.router_base_url,
            "user": settings.router_user,
            "strategies": [],
        }

        # Basic Auth probe
        c = self._new_client(basic_auth=True)
        try:
            for path in DHCP_PATHS:
                try:
                    r = await c.get(path)
                    diag["strategies"].append({
                        "name": "basic_auth",
                        "url": path,
                        "status": r.status_code,
                        "body_len": len(r.text),
                        "body_head": r.text[:400],
                        "looks_authenticated": _looks_authenticated(r.text),
                    })
                    if r.status_code == 200 and _looks_authenticated(r.text):
                        break
                except Exception as e:
                    diag["strategies"].append({"name": "basic_auth", "url": path, "error": str(e)})
        finally:
            await c.aclose()

        # Form POST probes
        for path in LOGIN_PATHS:
            c = self._new_client(basic_auth=False)
            try:
                try:
                    pre = await c.get("/")
                    pre_status = pre.status_code
                except Exception as e:
                    pre_status = f"error: {e}"
                aa = base64.b64encode(settings.router_user.encode()).decode()
                ab = base64.b64encode(settings.router_password.encode()).decode()
                try:
                    r = await c.post(path, data={
                        "aa": aa, "ab": ab,
                        "ja_name": settings.router_user, "ja_passwd": "",
                        "username": settings.router_user, "password": settings.router_password,
                    })
                    check = await c.get(DHCP_PATHS[0])
                    diag["strategies"].append({
                        "name": "form_post",
                        "post_url": path,
                        "pre_get_status": pre_status,
                        "post_status": r.status_code,
                        "post_body_head": r.text[:400],
                        "cookies_after_post": list(c.cookies.keys()),
                        "probe_url": DHCP_PATHS[0],
                        "probe_status": check.status_code,
                        "probe_body_head": check.text[:400],
                        "looks_authenticated": _looks_authenticated(check.text),
                    })
                except Exception as e:
                    diag["strategies"].append({"name": "form_post", "post_url": path, "error": str(e)})
            finally:
                await c.aclose()

        # Login page HTML, so we can see what fields the form actually wants
        c = self._new_client()
        try:
            for path in ("/", "/weblogin.htm", "/login.htm"):
                try:
                    r = await c.get(path)
                    if r.status_code == 200 and len(r.text) > 200:
                        diag["login_page_sample"] = {
                            "url": path,
                            "status": r.status_code,
                            "body_head": r.text[:1500],
                        }
                        break
                except Exception:
                    continue
        finally:
            await c.aclose()

        return diag

    async def _fetch_first(self, paths: list[str]) -> tuple[str, str] | None:
        """Try a list of candidate paths; return (path, html) for the first
        one that returns 200 with a non-trivial body."""
        client = await self._ensure_client()
        for p in paths:
            try:
                r = await client.get(p)
            except Exception as e:
                log.debug("GET %s failed: %s", p, e)
                continue
            if r.status_code == 200 and len(r.text) > 200:
                return p, r.text
        return None

    async def fetch_dhcp_html(self) -> str | None:
        got = await self._fetch_first(DHCP_PATHS)
        return got[1] if got else None

    async def fetch_flow_html(self) -> str | None:
        got = await self._fetch_first(FLOW_PATHS)
        return got[1] if got else None

    async def get_devices(self) -> list[Device]:
        html = await self.fetch_dhcp_html()
        if not html:
            return []
        return _parse_dhcp(html)

    async def get_flow(self) -> list[FlowSample]:
        html = await self.fetch_flow_html()
        if not html:
            return []
        return _parse_flow(html)


def _looks_authenticated(html: str) -> bool:
    """True if `html` looks like a real router page, not a login form.

    DrayTek bounces unauthenticated requests back to the login page, which
    we recognise via the markers in LOGIN_FORM_MARKERS.
    """
    if not html or len(html) < 200:
        return False
    lc = html.lower()
    return not any(m in lc for m in LOGIN_FORM_MARKERS)


def _normalise_mac(s: str) -> str:
    s = s.strip().upper().replace("-", ":")
    return s


def _parse_dhcp(html: str) -> list[Device]:
    """Pull (IP, MAC, hostname) triples out of a DrayTek DHCP page.

    Defensive: scans every table row, picks the cells that look like an IP
    and a MAC, takes the remaining longest text cell as the hostname.
    """
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, Device] = {}
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        ip = next((c for c in cells if IP_RE.fullmatch(c)), None)
        mac_cell = next((c for c in cells if MAC_RE.search(c)), None)
        if not ip or not mac_cell:
            continue
        mac = _normalise_mac(MAC_RE.search(mac_cell).group(0))
        # hostname = longest remaining textual cell that isn't ip/mac/numeric/expiry
        candidates = [c for c in cells if c not in (ip, mac_cell) and not IP_RE.fullmatch(c) and not MAC_RE.search(c)]
        candidates = [c for c in candidates if c and not c.replace(":", "").replace(" ", "").isdigit()]
        hostname = max(candidates, key=len) if candidates else None
        if hostname and hostname.lower() in {"---", "n/a", "unknown"}:
            hostname = None
        out[mac] = Device(mac=mac, ip=ip, hostname=hostname)
    return list(out.values())


def _parse_flow(html: str) -> list[FlowSample]:
    """Pull per-IP bandwidth rows out of a DrayTek Data Flow Monitor page.

    Tolerates differing column orders by inferring which cell is TX vs RX
    from the column header text, falling back to "first rate cell = TX,
    second = RX" when headers are unhelpful.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[FlowSample] = []

    tables = soup.find_all("table")
    for table in tables:
        header_cells = []
        first_tr = table.find("tr")
        if first_tr:
            header_cells = [c.get_text(" ", strip=True).lower() for c in first_tr.find_all(["td", "th"])]

        tx_idx = next((i for i, h in enumerate(header_cells) if "tx" in h or "upload" in h or "up " in h or h == "up"), None)
        rx_idx = next((i for i, h in enumerate(header_cells) if "rx" in h or "download" in h or "dn " in h or h == "down"), None)
        sess_idx = next((i for i, h in enumerate(header_cells) if "session" in h), None)

        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            ip = next((c for c in cells if IP_RE.fullmatch(c)), None)
            if not ip:
                continue
            mac_cell = next((c for c in cells if MAC_RE.search(c)), None)
            mac = _normalise_mac(MAC_RE.search(mac_cell).group(0)) if mac_cell else None

            rate_cells = [c for c in cells if c != ip and (not mac_cell or c != mac_cell) and NUM_RE.search(c)]

            tx_val = cells[tx_idx] if tx_idx is not None and tx_idx < len(cells) else (rate_cells[0] if len(rate_cells) > 0 else "0")
            rx_val = cells[rx_idx] if rx_idx is not None and rx_idx < len(cells) else (rate_cells[1] if len(rate_cells) > 1 else "0")

            sessions = None
            if sess_idx is not None and sess_idx < len(cells):
                m = re.search(r"\d+", cells[sess_idx])
                if m:
                    sessions = int(m.group(0))

            out.append(
                FlowSample(
                    ip=ip,
                    mac=mac,
                    tx_bps=_to_bps(tx_val),
                    rx_bps=_to_bps(rx_val),
                    sessions=sessions,
                )
            )
        if out:
            break  # first table that yielded data wins
    return out
