"""Worker settings, loaded from env vars with the INFERNA_ prefix."""

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

    server_url: str = "localhost:9091"
    registration_token: str = "inferna-registration-token"
    worker_name: str = ""
    cluster_name: str = ""
    mock_engine: bool = False
    models_dir: str = "./models"
    vllm_image: str = "vllm/vllm-openai:v0.8.5"
    sglang_image: str = "lmsysorg/sglang:v0.4.6.post1"
    hf_token: str = ""
    log_level: str = "info"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.registration_token == "inferna-registration-token":
        logger.warning("INFERNA_REGISTRATION_TOKEN is the dev default; override in production")
    return settings
