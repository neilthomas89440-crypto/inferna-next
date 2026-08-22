"""Worker settings, loaded from env vars with the INFERNA_ prefix."""

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
    )

    environment: str = Field(default="development", validation_alias="INFERNA_ENV")
    server_url: str = "localhost:9091"
    registration_token: str = "inferna-registration-token"
    worker_name: str = ""
    cluster_name: str = ""
    mock_engine: bool = False
    models_dir: str = "./models"
    vllm_image: str = "vllm/vllm-openai:v0.8.5"
    sglang_image: str = "lmsysorg/sglang:v0.4.6.post1"
    hf_token: str = ""
    worker_address: str = ""
    log_level: str = "info"

    @model_validator(mode="after")
    def _check_production_secrets(self):
        if self.environment == "production":
            if self.registration_token == "inferna-registration-token":
                raise ValueError("production mode requires a non-default INFERNA_REGISTRATION_TOKEN")
        return self


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.registration_token == "inferna-registration-token":
        logger.warning("INFERNA_REGISTRATION_TOKEN is the dev default; override in production")
    if settings.environment == "production":
        logger.warning("gRPC endpoint is unencrypted — deploy it only on a private network")
    return settings
