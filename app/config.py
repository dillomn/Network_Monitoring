from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


# How to interpret each integer in the `show traffic <ip> tx|rx` time-series.
# Different DrayTek firmwares emit different units in the same array:
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

    # Unit of the values inside `show traffic <ip>`. Use /debug/calibrate to
    # confirm. See TrafficUnit above for the choices.
    traffic_unit: TrafficUnit = "kilobits_per_second"

    # When picking a "current" reading from the time-series, average the
    # last N non-zero samples in the buffer. Default 1 = no averaging
    # (use the latest sample directly). Higher values flatten spikes
    # because each buffer position already represents a ~10s window on
    # the router side — averaging across positions stacks that window.
    traffic_smoothing_samples: int = 1

    poll_interval: int = 1
    retention_days: int = 30

    db_path: str = "/data/netmon.db"


settings = Settings()
