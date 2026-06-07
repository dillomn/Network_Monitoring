"""DrayTek SSH/CLI collector.

Connects to the router over SSH, runs a short batch of `show`/`srv`/`ip`
commands in one interactive shell session, and returns parsed device
records, WAN counters, and the NAT port-map. (Per-IP `show traffic`
bandwidth is also parsed here, but only for the /debug/ssh/* cross-check
endpoints — NetFlow drives live per-device traffic.)

Why an interactive shell rather than `conn.run("show arp")` per command?
DrayTeks ship a custom CLI, not a Unix shell — there's no exec-mode
channel. Everything happens inside one session at the `DrayTek>` prompt.

Key quirks this module handles:
    - DrayTek echoes the command back WITHOUT a newline before the output
      starts, so output looks like `show ver1.2.3...` mashed together.
    - Paginated output uses `--- MORE ---   [...]`; we auto-send space.
    - Older firmware negotiates older SSH algorithms; we widen the
      allowed key-exchange / cipher / MAC lists to be permissive.
"""
from __future__ import annotations

import asyncio
import logging
import re

import asyncssh

from ..config import settings
from ..parsers import cli as parsers
from .base import Device, FlowSample

log = logging.getLogger(__name__)

# Match either a configured prompt or the conventional "Something> " tail
# (DrayTek's default is "DrayTek> "; a renamed router uses its name).
PROMPT_RE = re.compile(r"([A-Za-z0-9_\-]+)>\s*$")
MORE_MARKER = "--- MORE ---"

# Permissive algorithm sets for older firmware. asyncssh's defaults already
# cover modern Vigors, but embedded SSH stacks regress occasionally.
LEGACY_SSH_KWARGS = dict(
    known_hosts=None,
    kex_algs=[
        "curve25519-sha256", "curve25519-sha256@libssh.org",
        "ecdh-sha2-nistp256", "ecdh-sha2-nistp384", "ecdh-sha2-nistp521",
        "diffie-hellman-group-exchange-sha256",
        "diffie-hellman-group14-sha256",
        "diffie-hellman-group14-sha1",
        "diffie-hellman-group-exchange-sha1",
        "diffie-hellman-group1-sha1",
    ],
    encryption_algs=[
        "aes128-ctr", "aes192-ctr", "aes256-ctr",
        "aes128-cbc", "aes192-cbc", "aes256-cbc",
        "aes128-gcm@openssh.com", "aes256-gcm@openssh.com",
        "3des-cbc",
    ],
    mac_algs=[
        "hmac-sha2-256", "hmac-sha2-512",
        "hmac-sha1", "hmac-md5",
    ],
    server_host_key_algs=[
        "ssh-ed25519", "ecdsa-sha2-nistp256",
        "rsa-sha2-256", "rsa-sha2-512",
        "ssh-rsa", "ssh-dss",
    ],
)


class DraytekSession:
    """Async context manager wrapping one SSH+shell session.

    Usage:
        async with DraytekSession() as s:
            ver  = await s.query("sys version")
            dhcp = await s.query("srv dhcp status")
    """

    def __init__(self) -> None:
        self._conn: asyncssh.SSHClientConnection | None = None
        self._proc: asyncssh.SSHClientProcess | None = None
        self._prompt: str | None = None
        self._buf: str = ""

    async def __aenter__(self) -> "DraytekSession":
        self._conn = await asyncssh.connect(
            host=settings.router_host,
            port=settings.router_ssh_port,
            username=settings.router_ssh_user,
            password=settings.router_ssh_password,
            # Send SSH-level keepalives so a long-lived session survives the
            # DrayTek's idle timeout (mngt sshtimeout). Drop after 3 missed.
            keepalive_interval=30,
            keepalive_count_max=3,
            **LEGACY_SSH_KWARGS,
        )
        # Wide terminal reduces forced pagination; vt100 is safest.
        self._proc = await self._conn.create_process(
            term_type="vt100",
            term_size=(200, 50),
            encoding="utf-8",
            errors="replace",
        )
        # Consume banner + initial prompt.
        await self._read_until_prompt()
        log.debug("SSH session opened, prompt=%r", self._prompt)
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._proc is not None:
                try:
                    self._proc.stdin.write("exit\r\n")
                except Exception:
                    pass
                self._proc.close()
        finally:
            if self._conn is not None:
                self._conn.close()
                await self._conn.wait_closed()

    async def _read_chunk(self, timeout: float) -> str | None:
        try:
            return await asyncio.wait_for(self._proc.stdout.read(4096), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def _read_until_prompt(self) -> str:
        """Read output until the buffer ends with the router prompt.

        On the way: if a `--- MORE ---` marker appears, send a space to
        advance, then keep reading. The MORE markers are stripped from the
        returned output. Returns everything captured *before* the trailing
        prompt line.
        """
        deadline = settings.ssh_timeout
        loops_without_data = 0
        while True:
            chunk = await self._read_chunk(timeout=1.0)
            if chunk is None:
                loops_without_data += 1
                if loops_without_data * 1.0 > deadline:
                    raise asyncio.TimeoutError(
                        f"DrayTek prompt not seen in {deadline}s; tail={self._buf[-200:]!r}"
                    )
                continue
            loops_without_data = 0
            self._buf += chunk

            # Auto-advance through pagination.
            if MORE_MARKER in self._buf:
                self._proc.stdin.write(" ")
                # Strip the MORE line from the buffer so it doesn't end up
                # tangled with the prompt detection below.
                self._buf = re.sub(
                    r"---\s*MORE\s*---[^\n]*\n?", "", self._buf
                )

            # Prompt is the final non-empty line and ends with "> ".
            lines = self._buf.split("\n")
            tail = lines[-1].rstrip("\r")
            m = PROMPT_RE.search(tail)
            if not m:
                continue

            # Prompt found — split off the body from the trailing prompt.
            if self._prompt is None:
                self._prompt = m.group(0).rstrip()
            body = "\n".join(lines[:-1])
            self._buf = ""
            # Carriage returns are noise from the pseudo-terminal.
            return body.replace("\r", "")

    async def query(self, command: str) -> str:
        """Send one CLI command, return the body (echo + prompt stripped)."""
        assert self._proc is not None, "session not open"
        self._proc.stdin.write(command + "\r\n")
        raw = await self._read_until_prompt()

        # Strip the echoed command. The router echoes it WITHOUT a trailing
        # newline, so the command is mashed against the start of the output.
        idx = raw.find(command)
        if idx != -1:
            raw = raw[idx + len(command):]
        return raw.lstrip("\n")


class DraytekCollector:
    """High-level collector — one method per data source.

    Each method opens its own session by default (cheap on Pi-LAN); to
    amortise the SSH handshake across calls, use `with_session()` and
    pass the session in explicitly.
    """

    async def router_info(self, session: DraytekSession | None = None) -> parsers.RouterInfo:
        async with _maybe_session(session) as s:
            return parsers.parse_version(await s.query("sys version"))

    async def devices(self, session: DraytekSession | None = None) -> list[Device]:
        """Merge DHCP leases + ARP table. ARP wins on hostname when both have
        one, since the ARP table reflects what's currently on-link rather
        than historical leases."""
        async with _maybe_session(session) as s:
            dhcp_raw = await s.query("srv dhcp status")
            arp_raw = await s.query("ip arp status")
        leases = parsers.parse_dhcp(dhcp_raw)
        arps = parsers.parse_arp(arp_raw)

        by_mac: dict[str, Device] = {}
        for L in leases:
            by_mac[L.mac] = Device(mac=L.mac, ip=L.ip, hostname=L.hostname)
        for a in arps:
            existing = by_mac.get(a.mac)
            if existing is None:
                by_mac[a.mac] = Device(mac=a.mac, ip=a.ip, hostname=a.hostname)
            else:
                # ARP is authoritative for current IP. Fill hostname if missing.
                existing.ip = a.ip
                if not existing.hostname and a.hostname:
                    existing.hostname = a.hostname
        return list(by_mac.values())

    async def flow(
        self,
        ips: list[str],
        session: DraytekSession | None = None,
    ) -> list[FlowSample]:
        """Pull `show traffic <ip> tx/rx` for each known IP and convert the
        last sample of the time-series into a current rate.

        Debug-only: feeds /debug/ssh/flow and /debug/ssh/raw-traffic for
        cross-checking NetFlow against the router's Data Flow Monitor. Not
        on the live data path."""
        if not ips:
            return []
        samples: list[FlowSample] = []
        async with _maybe_session(session) as s:
            for ip in ips:
                tx_cmd = f"show traffic {ip} tx"
                rx_cmd = f"show traffic {ip} rx"
                tx_series = parsers.parse_traffic_series(await s.query(tx_cmd), tx_cmd)
                rx_series = parsers.parse_traffic_series(await s.query(rx_cmd), rx_cmd)
                samples.append(FlowSample(
                    ip=ip,
                    mac=None,  # poller fills in from the device list
                    tx_bps=series_to_bps(tx_series),
                    rx_bps=series_to_bps(rx_series),
                ))
        return samples

    async def wan_totals(self, session: DraytekSession | None = None) -> list[parsers.WanStat]:
        async with _maybe_session(session) as s:
            return parsers.parse_statistic(await s.query("show statistic"))

    async def portmap(self, session: DraytekSession | None = None) -> dict[tuple[str, int], str]:
        """`show portmap` -> {(pseudo_ip, pseudo_port): private_ip}.

        Used by the NetFlow collector to attribute inbound flow records
        back to the real LAN device behind NAT."""
        async with _maybe_session(session) as s:
            return parsers.parse_portmap(await s.query("show portmap"))


# Bits-per-second conversion factors for each supported `traffic_unit`.
_UNIT_TO_BPS: dict[str, float] = {
    "bytes_per_minute": 8.0 / 60.0,
    "bytes_per_second": 8.0,
    "bits_per_second": 1.0,
    "kilobits_per_second": 1000.0,
    "kilobytes_per_second": 8000.0,
}


def series_to_bps(series: list[int]) -> float:
    """Convert the most-recent samples of a `show traffic` time-series to
    bits-per-second, using the configured `traffic_unit`.

    Averages the last `traffic_smoothing_samples` non-zero samples so the
    displayed rate is less jumpy than reading a single sample.
    """
    factor = _UNIT_TO_BPS.get(settings.traffic_unit, 1.0)
    raw = parsers.smoothed_sample(series, settings.traffic_smoothing_samples)
    return raw * factor


class _MaybeSession:
    """If a session is passed in, reuse it; otherwise open a fresh one
    that closes on exit. Lets each collector method work standalone OR
    inside a shared session for poll cycles that batch multiple queries."""

    def __init__(self, existing: DraytekSession | None) -> None:
        self._existing = existing
        self._opened: DraytekSession | None = None

    async def __aenter__(self) -> DraytekSession:
        if self._existing is not None:
            return self._existing
        self._opened = DraytekSession()
        await self._opened.__aenter__()
        return self._opened

    async def __aexit__(self, *exc) -> None:
        if self._opened is not None:
            await self._opened.__aexit__(*exc)


def _maybe_session(existing: DraytekSession | None) -> _MaybeSession:
    return _MaybeSession(existing)
