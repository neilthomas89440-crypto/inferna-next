"""Application settings, loaded from env vars with the INFERNA_ prefix."""

from __future__ import annotations

from functools import lru_cache

import structlog
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INFERNA_",
        env_file=(".env", "../.env"),
        extra="ignore",
    )

    # --- server ---
    database_url: str = "sqlite+aiosqlite:///./inferna.db"
    jwt_secret: str = "inferna-dev-secret"
    auth_enabled: bool = True
    admin_password: str = "inferna"
    registration_token: str = "inferna-registration-token"
    grpc_port: int = 9091
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    instance_port_range_start: int = 8010
    instance_port_range_end: int = 8100
    log_level: str = "info"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def instance_port_range(self) -> range:
        return range(self.instance_port_range_start, self.instance_port_range_end + 1)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.jwt_secret == "inferna-dev-secret":
        logger.warning("INFERNA_JWT_SECRET is the dev default; override in production")
    if settings.admin_password == "inferna":
        logger.warning("INFERNA_ADMIN_PASSWORD is the dev default; override in production")
    return settings
