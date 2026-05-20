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

LOGIN_PATH = "/cgi-bin/wlogin.cgi"


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

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=settings.router_base_url,
                verify=settings.router_verify_ssl,
                timeout=15.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (DraytekMonitor)"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def login(self) -> bool:
        """Best-effort login. DrayTek 2765 firmware accepts base64-encoded
        creds in the `aa`/`ab` fields of /cgi-bin/wlogin.cgi.
        """
        client = await self._ensure_client()
        try:
            await client.get("/")  # prime cookies / CSRF tokens
        except Exception as e:
            log.warning("Could not reach router root: %s", e)
            return False

        aa = base64.b64encode(settings.router_user.encode()).decode()
        ab = base64.b64encode(settings.router_password.encode()).decode()
        try:
            r = await client.post(
                LOGIN_PATH,
                data={"aa": aa, "ab": ab, "ja_name": settings.router_user, "ja_passwd": ""},
            )
        except Exception as e:
            log.error("Login POST failed: %s", e)
            return False

        # Success indicators vary; treat 2xx without an obvious "login fail"
        # marker as success. If you see persistent re-login storms, check
        # the response body and tighten this check.
        if r.status_code >= 400:
            log.error("Login HTTP %s", r.status_code)
            return False
        body = r.text.lower()
        if "password" in body and ("incorrect" in body or "invalid" in body):
            log.error("Login rejected by router (bad password?)")
            return False
        return True

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
