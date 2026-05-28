"""Parsers for DrayTek CLI output.

Each function takes the raw text body of one command and returns structured
data. Functions are pure — no side effects, no I/O — so they can be tested
against captured fixtures from any firmware build.

Commands handled:
    sys version                 -> parse_version
    srv dhcp status             -> parse_dhcp
    ip arp status               -> parse_arp
    show traffic <ip> tx|rx     -> parse_traffic_series
    show statistic              -> parse_statistic
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAC_RE = re.compile(r"([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


@dataclass
class DhcpLease:
    ip: str
    mac: str
    hostname: str | None
    lease_time: str | None


@dataclass
class ArpEntry:
    ip: str
    mac: str
    hostname: str | None
    interface: str | None


@dataclass
class RouterInfo:
    model: str | None
    firmware: str | None
    router_name: str | None


@dataclass
class WanStat:
    wan: str        # e.g. "WAN1"
    tx_bytes: int
    rx_bytes: int


def _normalise_mac(s: str) -> str:
    return s.strip().upper().replace("-", ":")


def _looks_like_header(line: str) -> bool:
    low = line.lower()
    return any(t in low for t in ("ip address", "mac address", "host id", "index", "interface"))


def parse_version(text: str) -> RouterInfo:
    """`sys version` -> RouterInfo. Tolerates missing fields."""
    model = firmware = name = None
    for line in text.splitlines():
        m = re.search(r"Router Model:\s*(\S+).*?Version:\s*(\S+)", line)
        if m:
            model, firmware = m.group(1), m.group(2)
            continue
        m = re.search(r"Router Name:\s*(\S+)", line)
        if m:
            name = m.group(1)
    return RouterInfo(model=model, firmware=firmware, router_name=name)


def parse_dhcp(text: str) -> list[DhcpLease]:
    """`srv dhcp status` -> list of leases.

    Output is tab-padded with section headers per LAN:
        Index   IP Address     MAC Address      Leased Time   HOST ID
        1       192.168.1.10   FC-19-28-61-83-58 23:59:37     Dillons-Air-246

    Strategy: for every line that contains both an IP and a MAC, parse it.
    Hostname is whatever's left at the end after stripping numerics and
    time-style fields.
    """
    out: dict[str, DhcpLease] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or _looks_like_header(line):
            continue
        ip_m = IP_RE.search(line)
        mac_m = MAC_RE.search(line)
        if not (ip_m and mac_m):
            continue
        ip = ip_m.group(0)
        # Skip the gateway-info lines ("IP Pool: 192.168.1.10 ~ ...")
        if "pool" in line.lower() or "gateway" in line.lower():
            continue
        mac = _normalise_mac(mac_m.group(0))

        # Split on tabs first (DrayTek's preferred separator), fall back to
        # 2+ spaces. The columns we care about are positionally consistent
        # within one firmware; the last text-y field is the hostname.
        cells = [c.strip() for c in re.split(r"\t+|\s{2,}", line) if c.strip()]
        # Drop cells that match the IP, MAC, or look like an index/time.
        leftover = []
        for c in cells:
            if c == ip or _normalise_mac(c) == mac:
                continue
            if re.fullmatch(r"\d+", c):
                continue  # index
            if re.fullmatch(r"\d{1,3}:\d{2}:\d{2}", c):
                continue  # lease time HH:MM:SS — captured separately
            leftover.append(c)
        hostname = leftover[-1] if leftover else None
        if hostname and hostname.lower() in {"---", "n/a", "unknown"}:
            hostname = None

        lease_time = None
        lt_m = re.search(r"\b\d{1,3}:\d{2}:\d{2}\b", line)
        if lt_m:
            lease_time = lt_m.group(0)

        out[mac] = DhcpLease(ip=ip, mac=mac, hostname=hostname, lease_time=lease_time)
    return list(out.values())


def parse_arp(text: str) -> list[ArpEntry]:
    """`ip arp status` -> list of entries.

    Output is space-padded, header on line 2 starting with `Index IP...`:
        1   192.168.1.10   FC-19-28-61-83-58   Dillons-Air-246  LAN1  ---  P1
    """
    out: dict[str, ArpEntry] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or "[ARP Table]" in line or _looks_like_header(line):
            continue
        ip_m = IP_RE.search(line)
        mac_m = MAC_RE.search(line)
        if not (ip_m and mac_m):
            continue
        ip = ip_m.group(0)
        mac = _normalise_mac(mac_m.group(0))

        cells = [c.strip() for c in re.split(r"\s{2,}", line) if c.strip()]
        cells = [c for c in cells if c != ip and _normalise_mac(c) != mac and not re.fullmatch(r"\d+", c)]
        # Interface is usually "LAN1"/"WAN1"/etc; hostname is whatever's left.
        interface = next((c for c in cells if re.fullmatch(r"(LAN|WAN)\d+", c, re.IGNORECASE)), None)
        if interface:
            cells = [c for c in cells if c != interface]
        # Drop trailing port/vlan markers like "P1", "---"
        cells = [c for c in cells if not re.fullmatch(r"P\d+|---|\d+", c, re.IGNORECASE)]
        hostname = cells[-1] if cells else None
        if hostname and hostname in {"---"}:
            hostname = None

        out[mac] = ArpEntry(ip=ip, mac=mac, hostname=hostname, interface=interface)
    return list(out.values())


def parse_traffic_series(text: str, command_echo: str = "") -> list[int]:
    """`show traffic <ip> tx|rx` -> list of numeric samples.

    Output is one long comma-separated stream of values with NO newline
    between the echoed command and the data:
        show traffic 192.168.1.10 rx0 ,0 ,0 ,...
    If `command_echo` is given, strip it from the front before parsing.
    """
    body = text
    if command_echo and body.startswith(command_echo):
        body = body[len(command_echo):]
    # Some firmware also echoes the prompt back on the same line.
    body = re.sub(r"^[^,0-9-]*", "", body, count=1)
    values: list[int] = []
    for tok in body.split(","):
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r"-?\d+", tok)
        if not m:
            continue
        try:
            values.append(int(m.group(0)))
        except ValueError:
            continue
    return values


def latest_sample(series: list[int]) -> int:
    """Pick the 'current' sample from a time-series.

    Assumption (until calibration confirms): the LAST value is the most
    recent sample. If the last sample is zero but a recent one isn't, the
    last is probably 'still being filled' — fall back to the most recent
    non-zero in the trailing window.
    """
    if not series:
        return 0
    if series[-1] > 0:
        return series[-1]
    # Look back through the most-recent ~5 samples for a non-zero reading.
    for v in reversed(series[-5:]):
        if v > 0:
            return v
    return 0


def smoothed_sample(series: list[int], window: int = 3) -> float:
    """Average the last `window` non-zero samples to get a less jumpy reading.

    If every sample in the trailing window is zero, returns 0. Falls back to
    latest_sample() behaviour when window <= 1.
    """
    if not series:
        return 0.0
    if window <= 1:
        return float(latest_sample(series))
    tail = series[-max(window * 2, window):]
    nonzero = [v for v in tail if v > 0]
    if not nonzero:
        return 0.0
    picked = nonzero[-window:]
    return sum(picked) / len(picked)


def parse_statistic(text: str) -> list[WanStat]:
    """`show statistic` -> per-WAN lifetime byte totals.

    DrayTek prints with a human-readable unit, NOT raw bytes:
        WAN1 total TX: 0 Bytes ,RX: 0 Bytes
        WAN2 total TX: 7.4 GB ,RX: 53.8 GB
        WAN3 total TX: 5.2 MB ,RX: 1.5 KB

    Decimal precision (e.g. "0.1 GB") puts a floor on the smallest delta
    we can observe between polls — ~100 MB at the GB scale. That's fine
    for "what's hogging the WAN"; less so for monitoring trickle traffic.
    """
    pat = re.compile(
        r"(WAN\d+)\s+total\s+TX:\s*([\d.]+)\s*([A-Za-z]+)"
        r"\s*,\s*RX:\s*([\d.]+)\s*([A-Za-z]+)",
        re.IGNORECASE,
    )
    out: list[WanStat] = []
    for line in text.splitlines():
        m = pat.search(line)
        if not m:
            continue
        wan = m.group(1).upper()
        tx = _bytes_from_unit(m.group(2), m.group(3))
        rx = _bytes_from_unit(m.group(4), m.group(5))
        if tx is None or rx is None:
            continue  # unrecognised unit — log silently, don't crash
        out.append(WanStat(wan=wan, tx_bytes=tx, rx_bytes=rx))
    return out


# SI multipliers — DrayTek uses 1000-based units in `show statistic`.
_BYTE_UNIT_MULTIPLIERS: dict[str, int] = {
    "B": 1, "BYTES": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
}


def _bytes_from_unit(num: str, unit: str) -> int | None:
    factor = _BYTE_UNIT_MULTIPLIERS.get(unit.upper())
    if factor is None:
        return None
    try:
        return int(float(num) * factor)
    except ValueError:
        return None
