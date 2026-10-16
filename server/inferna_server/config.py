"""Application settings, loaded from env vars with the INFERNA_ prefix."""

from __future__ import annotations

from functools import lru_cache

import structlog
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INFERNA_",
        env_file=(".env", "../.env"),
        extra="ignore",
        populate_by_name=True,
    )

    # --- server ---
    environment: str = Field(default="development", validation_alias="INFERNA_ENV")
    database_url: str = "sqlite+aiosqlite:///./inferna.db"
    jwt_secret: str = "inferna-dev-secret"
    auth_enabled: bool = True
    admin_password: str = "inferna"
    registration_token: str = "inferna-registration-token"
    grpc_port: int = 9091
    # gRPC transport security. Without these the worker channel is plaintext, which the README
    # documents as a private-network assumption; set cert+key to terminate TLS, and a CA (plus
    # require_client_cert) for mTLS.
    grpc_tls_cert: str = ""
    grpc_tls_key: str = ""
    grpc_tls_ca: str = ""
    grpc_tls_require_client_cert: bool = False
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    instance_port_range_start: int = 8010
    instance_port_range_end: int = 8100
    # Rendezvous ports for parallel groups (rank-to-rank NCCL/gloo traffic and the head's
    # control socket). Kept apart from the instance range; see docs/network-matrix.md.
    rendezvous_port_range_start: int = 8110
    rendezvous_port_range_end: int = 8199
    gateway_connect_timeout: float = 5.0
    gateway_read_timeout: float = 300.0
    gateway_enabled: bool = True
    # Ask OpenAI-compatible upstreams for a final usage chunk on streamed responses
    # (token accounting degrades to request-only counting when disabled or unsupported).
    gateway_inject_stream_usage: bool = True
    gateway_upstream_allowlist: str = ""
    log_level: str = "info"
    @model_validator(mode="after")
    def _check_tls_material(self):
        """A certificate without its key is a startup error, not a later surprise."""
        if bool(self.grpc_tls_cert) != bool(self.grpc_tls_key):
            raise ValueError("INFERNA_GRPC_TLS_CERT and INFERNA_GRPC_TLS_KEY must be set together")
        if self.grpc_tls_require_client_cert and not self.grpc_tls_ca:
            raise ValueError("INFERNA_GRPC_TLS_REQUIRE_CLIENT_CERT needs INFERNA_GRPC_TLS_CA")
        return self

    @model_validator(mode="after")
    def _check_production_secrets(self):
        if self.environment == "production":
            problems: list[str] = []
            if self.jwt_secret == "inferna-dev-secret":
                problems.append("INFERNA_JWT_SECRET")
            if self.admin_password == "inferna":
                problems.append("INFERNA_ADMIN_PASSWORD")
            if self.registration_token == "inferna-registration-token":
                problems.append("INFERNA_REGISTRATION_TOKEN")
            if problems:
                raise ValueError(
                    f"production mode requires non-default values for: {', '.join(problems)}"
                )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def gateway_upstream_allowlist_entries(self) -> list[str]:
        return [e.strip() for e in self.gateway_upstream_allowlist.split(",") if e.strip()]

    @property
    def instance_port_range(self) -> range:
        return range(self.instance_port_range_start, self.instance_port_range_end + 1)

    @property
    def grpc_tls_enabled(self) -> bool:
        """True when a certificate and key are configured (CA optional, required for mTLS)."""
        return bool(self.grpc_tls_cert and self.grpc_tls_key)

    @property
    def rendezvous_port_range(self) -> range:
        return range(self.rendezvous_port_range_start, self.rendezvous_port_range_end + 1)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.jwt_secret == "inferna-dev-secret":
        logger.warning("INFERNA_JWT_SECRET is the dev default; override in production")
    if settings.admin_password == "inferna":
        logger.warning("INFERNA_ADMIN_PASSWORD is the dev default; override in production")
    if settings.registration_token == "inferna-registration-token":
        logger.warning("INFERNA_REGISTRATION_TOKEN is the dev default; override in production")
    if settings.environment == "production":
        logger.warning("gRPC endpoint is unencrypted — deploy it only on a private network")
    return settings
