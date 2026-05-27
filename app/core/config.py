from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_ENV_FILE = PROJECT_ROOT / ".local" / "env" / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ACTIVE_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_model: str = Field(default="", alias="LLM_MODEL")
    llm_api_base_url: str = Field(default="", alias="LLM_API_BASE_URL")
    llm_timeout: int = Field(default=30, alias="LLM_TIMEOUT")
    llm_default_temperature: float = Field(
        default=0.2,
        alias="LLM_DEFAULT_TEMPERATURE",
    )
    llm_extra_body: str = Field(default="", alias="LLM_EXTRA_BODY")

    # LLM worker
    llm_worker_batch_size: int = Field(
        default=5,
        alias="LLM_WORKER_BATCH_SIZE",
    )
    llm_worker_idle_sleep_seconds: float = Field(
        default=5,
        alias="LLM_WORKER_IDLE_SLEEP_SECONDS",
    )
    llm_worker_error_sleep_seconds: float = Field(
        default=10,
        alias="LLM_WORKER_ERROR_SLEEP_SECONDS",
    )

    # MongoDB
    mongo_uri: str = Field(default="mongodb://localhost:27017", alias="MONGO_URI")
    mongo_db_name: str = Field(default="stock_project", alias="MONGO_DB_NAME")

    # Proxy
    proxy_api_key: str = Field(default="", alias="PROXY_API_KEY")

    # Log
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
