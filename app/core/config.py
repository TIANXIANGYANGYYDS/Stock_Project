from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Qianwen / DashScope
    qian_api_key: str = Field(default="", alias="QIAN_API_KEY")
    qian_model: str = Field(default="qwen-plus", alias="QIAN_MODEL")
    qian_api_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="QIAN_API_BASE_URL",
    )

    # DeepSeek
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field(default="deepseek-v4-pro", alias="DEEPSEEK_MODEL")
    deepseek_api_base_url: str = Field(
        default="https://api.deepseek.com",
        alias="DEEPSEEK_API_BASE_URL",
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