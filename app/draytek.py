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
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .config import settings

log = logging.getLogger(__name__)

MAC_RE = re.compile(r"([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
NUM_RE = re.compile(r"[\d,.]+")

DHCP_PATHS = [
    "/cgi-bin/dhcp.cgi",            # modern Vigor firmware (token required)
    "/cgi-bin/ipdhcptbAdv.htm",
    "/doc/lan_dhcp.sht",            # older firmware
    "/doc/dhcptable.sht",
    "/cgi-bin/v2/dhcptable",
]

FLOW_PATHS = [
    # Vigor 2765 modern firmware: dispatcher CGI with function ID 2096.
    # Redirects to /doc/digdatam.htm which holds the actual table.
    # NOTE: requires "Enable Data Flow Monitor" to be ticked in the router UI.
    "/cgi-bin/v2x00.cgi?fid=2096",
    "/doc/digdatam.htm",
    # Older / alternate firmware paths
    "/cgi-bin/dataflow.cgi",
    "/cgi-bin/dataFlowMonitor.cgi",
    "/cgi-bin/datafm.cgi",
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
    "authorization error",
)

# Real tokens we've observed on this firmware are 14-15 chars (e.g. "9SJmXlA6TPCAHIO").
# Require >=10 to skip false matches like JS concatenation `'sFormAuthStr=' + uToken`.
TOKEN_PATTERNS = [
    re.compile(r"sFormAuthStr=([A-Za-z0-9]{10,})"),
    re.compile(r"""sFormAuthStr\s*[:=]\s*['"]([A-Za-z0-9]{10,})['"]"""),
    re.compile(r"""sToken\s*[:=]\s*['"]([A-Za-z0-9]{10,})['"]"""),
    re.compile(r"""['"]sFormAuthStr['"]\s*[:=]\s*['"]([A-Za-z0-9]{10,})['"]"""),
]

# Pages most likely to contain the per-session token. Tried in order.
TOKEN_PAGES = [
    "/cgi-bin/menu.htm", "/cgi-bin/menu.cgi",
    "/cgi-bin/index.cgi", "/cgi-bin/dashboard.htm",
    "/cgi-bin/v2x00.cgi", "/cgi-bin/v2x00.cgi?fid=1",
    "/main.html", "/index.html", "/dashboard.html",
    "/",
]


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
        self._discovered_base: str | None = None
        self._form_auth_token: str | None = None

    def _base(self) -> str:
        return self._discovered_base or settings.router_base_url

    def _new_client(self, *, basic_auth: bool = False) -> httpx.AsyncClient:
        auth = (settings.router_user, settings.router_password) if basic_auth else None
        return httpx.AsyncClient(
            base_url=self._base(),
            verify=settings.router_verify_ssl,
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (DraytekMonitor)"},
            auth=auth,
        )

    async def _discover_base(self) -> None:
        """Many DrayTek units force HTTPS on a non-standard port (e.g.
        :4441). A naive POST to http://router/cgi-bin/wlogin.cgi gets
        redirected and loses its form body. So we follow GET / once to
        learn the real management URL and use that for everything."""
        if self._discovered_base:
            return
        async with httpx.AsyncClient(
            base_url=settings.router_base_url,
            verify=settings.router_verify_ssl,
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (DraytekMonitor)"},
        ) as probe:
            try:
                r = await probe.get("/")
            except Exception as e:
                log.warning("Management URL discovery failed: %s", e)
                return
            u = urlparse(str(r.url))
            if not u.netloc:
                return
            discovered = f"{u.scheme}://{u.netloc}"
            if discovered.rstrip("/") != settings.router_base_url.rstrip("/"):
                log.info("Discovered management URL: %s -> %s", settings.router_base_url, discovered)
                self._discovered_base = discovered

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._new_client()
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _probe_authenticated(self, client: httpx.AsyncClient) -> bool:
        """Authenticated if we hold a router session cookie, OR if a
        protected page returns real content rather than the login form."""
        for name in client.cookies.keys():
            n = name.lower()
            if "session" in n or "vigor" in n:
                return True
        for path in DHCP_PATHS + FLOW_PATHS:
            try:
                r = await client.get(path)
            except Exception:
                continue
            if r.status_code == 200 and _looks_authenticated(r.text):
                return True
        return False

    async def discover_api(self) -> dict:
        """Authenticate, fetch every plausible dashboard URL + JS bundle,
        grep everything for `/cgi-bin/v2/...` references, and probe each
        candidate. Also probes a hardcoded list of known DrayTek endpoints
        in case discovery turns up nothing."""
        if not await self.login():
            return {"error": "login failed"}
        c = await self._ensure_client()

        pages_to_try = [
            "/", "/main.html", "/index.html", "/home.html",
            "/weblogin.htm", "/dashboard.html", "/wizard.htm",
        ]
        pages_seen: list[dict] = []
        all_text: list[str] = []
        js_urls: set[str] = set()

        for page in pages_to_try:
            try:
                r = await c.get(page)
                pages_seen.append({
                    "path": page,
                    "status": r.status_code,
                    "final_url": str(r.url),
                    "body_len": len(r.text),
                })
                if r.status_code == 200 and r.text:
                    all_text.append(r.text)
                    for m in re.findall(r"""['"](/[^'"\s<>]+\.js(?:\?[^'"\s<>]*)?)['"]""", r.text):
                        js_urls.add(m)
            except Exception as e:
                pages_seen.append({"path": page, "error": str(e)})

        scanned_js: list[dict] = []
        for url in list(js_urls)[:40]:
            try:
                r = await c.get(url)
                if r.status_code == 200 and r.text:
                    all_text.append(r.text)
                    scanned_js.append({"path": url, "body_len": len(r.text)})
            except Exception:
                continue

        found: set[str] = set()
        for text in all_text:
            for m in re.findall(r"/cgi-bin/v2/[A-Za-z0-9_./\-]+", text):
                found.add(m.rstrip("./"))

        # Brute-force candidates so we get useful output even if discovery
        # finds nothing referenced in the JS bundles.
        BRUTE_CANDIDATES = [
            "/cgi-bin/v2/dhcptable", "/cgi-bin/v2/dhcpTable",
            "/cgi-bin/v2/get_dhcp_table.cgi", "/cgi-bin/v2/get_dhcp_status.cgi",
            "/cgi-bin/v2/get_lan_status.cgi", "/cgi-bin/v2/get_lan_clients.cgi",
            "/cgi-bin/v2/get_arp_table.cgi", "/cgi-bin/v2/arpTable",
            "/cgi-bin/v2/dataflow", "/cgi-bin/v2/dataFlowMonitor",
            "/cgi-bin/v2/get_data_flow.cgi", "/cgi-bin/v2/get_traffic.cgi",
            "/cgi-bin/v2/get_bandwidth.cgi", "/cgi-bin/v2/get_online_users.cgi",
            "/cgi-bin/v2/online_users.cgi", "/cgi-bin/v2/get_session_info.cgi",
            "/cgi-bin/v2/getStatus.cgi", "/cgi-bin/v2/sessions",
            "/cgi-bin/v2/dhcpTable.cgi", "/cgi-bin/v2/dataFlowMonitor.cgi",
            "/cgi-bin/v2/lan_dhcp.cgi", "/cgi-bin/v2/get_clients.cgi",
        ]
        candidates = found | set(BRUTE_CANDIDATES)

        probes: list[dict] = []
        for path in sorted(candidates):
            for method in ("GET", "POST"):
                try:
                    r = await (c.get(path) if method == "GET" else c.post(path, json={}))
                    snippet = r.text[:400]
                    ct = r.headers.get("content-type", "")
                    is_json = "json" in ct.lower() or snippet.lstrip().startswith(("{", "["))
                    probes.append({
                        "path": path,
                        "method": method,
                        "status": r.status_code,
                        "content_type": ct,
                        "body_len": len(r.text),
                        "is_json": is_json,
                        "looks_interesting": is_json and r.status_code == 200,
                        "body_head": snippet,
                    })
                    if method == "GET" and r.status_code == 200 and is_json:
                        break  # GET worked, no need to try POST
                except Exception as e:
                    probes.append({"path": path, "method": method, "error": str(e)})

        return {
            "pages_seen": pages_seen,
            "js_urls_found": sorted(js_urls),
            "scanned_js": scanned_js,
            "api_paths_from_js": sorted(found),
            "probed_paths": sorted(candidates),
            "probes": probes,
            "interesting": [p for p in probes if p.get("looks_interesting")],
        }

    async def login(self) -> bool:
        """Try multiple auth strategies. Returns True if any sticks.
        If logged in, self._client is left in an authenticated state.
        """
        await self.close()  # discard any half-state from prior attempts
        self._form_auth_token = None  # force re-extraction on every fresh login
        await self._discover_base()

        # Strategy 1: HTTP Basic Auth (DrayTek's CGI pages accept it on many builds)
        c = self._new_client(basic_auth=True)
        if await self._probe_authenticated(c):
            log.info("Authenticated via HTTP Basic")
            self._client = c
            await self._refresh_form_auth_token()
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
                # The login POST response body is the post-login dashboard SPA,
                # which often contains the sFormAuthStr token directly.
                self._try_extract_token(r.text, f"POST {path} response")
            except Exception as e:
                log.debug("Form POST %s raised: %s", path, e)
                continue
            if await self._probe_authenticated(c):
                log.info("Authenticated via form POST at %s", path)
                self._client = c
                if not self._form_auth_token:
                    await self._refresh_form_auth_token()
                return True

        await c.aclose()
        log.error("All login strategies failed. Hit /debug/login for diagnostics.")
        return False

    async def _ensure_form_token(self) -> None:
        """Called from get_devices / get_flow before scraping."""
        if not self._form_auth_token:
            await self._refresh_form_auth_token()

    async def diagnose_login(self) -> dict:
        """Run every strategy independently and report what the router said.
        Used by the /debug/login endpoint to help adjust the auth flow to
        a specific firmware build."""
        await self._discover_base()
        diag: dict = {
            "configured_base_url": settings.router_base_url,
            "discovered_base_url": self._discovered_base,
            "effective_base_url": self._base(),
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

    def _try_extract_token(self, text: str, source: str) -> bool:
        """Run every TOKEN_PATTERN against `text`; cache the first match
        whose length looks plausible. Returns True if a token was found."""
        if not text:
            return False
        for pat in TOKEN_PATTERNS:
            m = pat.search(text)
            if m and len(m.group(1)) >= 10:
                self._form_auth_token = m.group(1)
                log.info("Cached sFormAuthStr token (%d chars) from %s",
                         len(self._form_auth_token), source)
                return True
        return False

    async def _refresh_form_auth_token(self) -> str | None:
        """Fetch a sequence of likely dashboard pages and extract the
        per-session `sFormAuthStr` token. Also walks any JS files
        referenced by each page in case the token is set there."""
        client = await self._ensure_client()
        for page in TOKEN_PAGES:
            try:
                r = await client.get(page)
            except Exception:
                continue
            if r.status_code != 200 or not r.text:
                continue
            if self._try_extract_token(r.text, page):
                return self._form_auth_token
            # Also scan any JS files this page links to
            for js_url in re.findall(r"""['"](/[^'"\s<>]+\.js(?:\?[^'"\s<>]*)?)['"]""", r.text)[:10]:
                try:
                    jr = await client.get(js_url)
                except Exception:
                    continue
                if jr.status_code == 200 and self._try_extract_token(jr.text, js_url):
                    return self._form_auth_token
        log.warning("Could not find sFormAuthStr in any dashboard page or JS")
        return None

    async def _fetch_first(self, paths: list[str]) -> tuple[str, str] | None:
        """Try each path; return (url, body) for the first 200 response
        with non-trivial body. Automatically appends sFormAuthStr if a
        token has been cached."""
        client = await self._ensure_client()
        token = self._form_auth_token
        for p in paths:
            url = p
            if token and "sFormAuthStr" not in p:
                sep = "&" if "?" in p else "?"
                url = f"{p}{sep}sFormAuthStr={token}"
            try:
                r = await client.get(url)
            except Exception as e:
                log.debug("GET %s failed: %s", url, e)
                continue
            if r.status_code == 200 and len(r.text) > 200 and _looks_authenticated(r.text):
                return url, r.text
        return None

    async def fetch_dhcp_html(self) -> str | None:
        await self._ensure_form_token()
        got = await self._fetch_first(DHCP_PATHS)
        return got[1] if got else None

    async def fetch_flow_html(self) -> str | None:
        await self._ensure_form_token()
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
    and a MAC, takes the remaining longest text cell as the hostname. If
    no table rows are found (modern Vigor firmware uses a <pre>/text grid
    rather than an HTML table), falls back to line-by-line text parsing.
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
        candidates = [c for c in cells if c not in (ip, mac_cell) and not IP_RE.fullmatch(c) and not MAC_RE.search(c)]
        candidates = [c for c in candidates if c and not c.replace(":", "").replace(" ", "").isdigit()]
        hostname = max(candidates, key=len) if candidates else None
        if hostname and hostname.lower() in {"---", "n/a", "unknown"}:
            hostname = None
        out[mac] = Device(mac=mac, ip=ip, hostname=hostname)

    if not out:
        out = _parse_dhcp_text(html)
    return list(out.values())


def _parse_dhcp_text(html: str) -> dict[str, Device]:
    """Fallback parser for text-grid DHCP pages like the Vigor 2765's
    `dhcp.cgi`. Each line that contains both an IP and a MAC is treated
    as a device; the remaining longest token on the line becomes the
    hostname (stripping the index, leased-time, and other numerics)."""
    text = re.sub(r"<[^>]+>", " ", html)
    out: dict[str, Device] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        ip_m = IP_RE.search(line)
        mac_m = MAC_RE.search(line)
        if not ip_m or not mac_m:
            continue
        ip = ip_m.group(0)
        mac = _normalise_mac(mac_m.group(0))
        rest = line.replace(ip_m.group(0), " ").replace(mac_m.group(0), " ")
        rest = re.sub(r"\b\d{1,3}:\d{2}:\d{2}\b", " ", rest)        # leased time HH:MM:SS
        rest = re.sub(r"\b\d+\b", " ", rest)                          # standalone numbers (index)
        tokens = [t for t in rest.split() if len(t) > 1]
        hostname: str | None = None
        if tokens:
            hostname = max(tokens, key=len)
            if hostname.lower() in {"---", "n/a", "unknown", "host", "id"}:
                hostname = None
        out[mac] = Device(mac=mac, ip=ip, hostname=hostname)
    return out


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

    if not out:
        out = _parse_flow_text(html)
    return out


def _parse_flow_text(html: str) -> list[FlowSample]:
    """Fallback for text-grid Data Flow Monitor pages (Vigor 2765).
    Each row looks like:
        Index  IP            TX Rate(bps)         RX Rate(bps)         Sessions
        1      192.168.1.15  1.2 K / 5.4 M /Auto  12.3 K / 87 M /Auto  5
    The rate cells are 'Current / Peak / Speed'; we take the first number
    (Current) as the live rate.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    out: list[FlowSample] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        ip_m = IP_RE.search(line)
        if not ip_m:
            continue
        ip = ip_m.group(0)
        if ip.startswith(("0.", "255.")):
            continue
        rest = line[ip_m.end():]
        # rate cells appear as e.g. "1.2 K / 5.4 M /Auto"  -- grab the first numeric token
        numbers = re.findall(r"\d+(?:\.\d+)?\s*[KMG]?", rest)
        if len(numbers) < 2:
            continue
        tx_bps = _to_bps(numbers[0] + "bps")
        # rate cells are 3-wide (Current/Peak/Speed). RX current = 4th number
        rx_idx = 3 if len(numbers) >= 4 else 1
        rx_bps = _to_bps(numbers[rx_idx] + "bps")
        sess_m = re.search(r"\b(\d+)\b\s*$", rest)
        sessions = int(sess_m.group(1)) if sess_m else None
        out.append(FlowSample(ip=ip, mac=None, tx_bps=tx_bps, rx_bps=rx_bps, sessions=sessions))
    return out
