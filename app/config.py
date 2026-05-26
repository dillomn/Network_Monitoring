from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    router_host: str = "192.168.1.1"
    router_ssh_port: int = 22
    router_ssh_user: str = "admin"
    router_ssh_password: str = ""

    # Connect/read timeout for the SSH session, per-command.
    ssh_timeout: float = 15.0

    # Bandwidth time-series interpretation. The Vigor's `show traffic <ip> tx`
    # returns ~480 historical samples; without a calibration run we assume:
    #   - the LAST value in the array is the most recent sample
    #   - each sample is per-minute aggregate bytes
    # If calibration shows otherwise, flip these without touching parsers.
    traffic_sample_seconds: int = 60
    traffic_value_is_bytes: bool = True  # if False, treat as already-bps

    poll_interval: int = 10
    retention_days: int = 30

    db_path: str = "/data/netmon.db"


settings = Settings()
