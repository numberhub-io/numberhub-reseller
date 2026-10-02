"""Settings from the environment / .env (see .env.example)."""
from __future__ import annotations

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    builder_bot_token: str = ""
    admin_ids: str = ""
    secret_key: str = ""
    db_url: str = "sqlite+aiosqlite:///./reseller.db"
    numberhub_api: str = "https://api.numberhub.io/v1"
    numberhub_site: str = "https://numberhub.io"
    numberhub_bot: str = "TheNumberHubBot"
    default_markup_pct: Decimal = Decimal("30")
    max_markup_pct: Decimal = Decimal("300")
    sync_interval_sec: float = 5.0
    max_open_per_member: int = 10
    max_open_per_route: int = 3

    @property
    def admin_id_list(self) -> list[int]:
        return [int(x) for x in self.admin_ids.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()]


settings = Settings()
