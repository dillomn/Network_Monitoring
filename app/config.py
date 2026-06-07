from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


# DEBUG-ONLY. NetFlow drives live per-device traffic; the `show traffic <ip>`
# CLI path survives only in the /debug/ssh/* endpoints (raw-traffic, calibrate)
# for cross-checking NetFlow against the router's Data Flow Monitor. This tells
# those endpoints how to read each integer in the time-series — different
# DrayTek firmwares emit different units:
#   - 2762n (older firmware) — bytes-per-minute aggregate
#   - 2765 series            — appears to be bits-per-second already
# Override per-deployment via the TRAFFIC_UNIT env var.
TrafficUnit = Literal[
    "bytes_per_minute",   # value * 8 / 60   (2762n default)
    "bytes_per_second",   # value * 8
    "bits_per_second",    # value as-is      (likely 2765)
    "kilobits_per_second",  # value * 1000
    "kilobytes_per_second",  # value * 8000
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    router_host: str = "192.168.1.1"
    router_ssh_port: int = 22
    router_ssh_user: str = "admin"
    router_ssh_password: str = ""

    # Connect/read timeout for the SSH session, per-command.
    ssh_timeout: float = 15.0

    # (debug-only) Unit of the values inside `show traffic <ip>`, for the
    # /debug/ssh/raw-traffic + /debug/calibrate cross-check. See TrafficUnit.
    traffic_unit: TrafficUnit = "kilobits_per_second"

    # (debug-only) When picking a "current" reading from the time-series,
    # average the last N non-zero samples in the buffer. Default 1 = no averaging
    # (use the latest sample directly). Higher values flatten spikes
    # because each buffer position already represents a ~10s window on
    # the router side — averaging across positions stacks that window.
    traffic_smoothing_samples: int = 1

    poll_interval: int = 1
    retention_days: int = 30

    db_path: str = "/data/netmon.db"

    # UDP port the NetFlow v9 collector listens on. Must match the
    # router's "Collector Port" under System Maintenance → NetFlow.
    netflow_port: int = 2055

    # Comma-separated CIDR prefixes treated as "LAN" when attributing
    # NetFlow records. Defaults cover RFC1918 IPv4 + IPv6 ULA + IPv6
    # link-local. If your ISP routes a global IPv6 /64 to your LAN, add
    # that prefix here so v6 traffic is attributed correctly.
    lan_prefixes: str = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7,fe80::/10"


settings = Settings()
