from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    router_host: str = "192.168.1.1"
    router_user: str = "admin"
    router_password: str = ""
    router_scheme: str = "http"
    router_verify_ssl: bool = False

    poll_interval: int = 10
    retention_days: int = 30

    db_path: str = "/data/netmon.db"

    @property
    def router_base_url(self) -> str:
        return f"{self.router_scheme}://{self.router_host}"


settings = Settings()
