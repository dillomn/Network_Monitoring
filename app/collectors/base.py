from __future__ import annotations

from dataclasses import dataclass


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
