from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_ENV_FILE = PROJECT_ROOT / ".local" / "env" / ".env"


class Settings(BaseSettings):
    """应用的部署配置。

    环境文件只承载凭据、服务地址、超时、数据库、代理和日志等部署差异。模型、
    调度时间、分析范围、目标账号及本机处理策略均由对应业务模块中的代码常量管理，
    避免固定规则散落在不同部署环境。
    """

    # Pydantic Settings 从项目私有环境文件读取，忽略未被当前代码使用的旧变量。
    model_config = SettingsConfigDict(
        env_file=ACTIVE_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM API 认证令牌；生产环境必须通过环境变量注入。
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    # 兼容 OpenAI Chat Completions 的服务根地址。
    llm_api_base_url: str = Field(default="", alias="LLM_API_BASE_URL")
    # 单次 LLM HTTP 请求的最大等待秒数。
    llm_timeout: int = Field(default=30, alias="LLM_TIMEOUT")

    # MongoDB 连接 URI，包含部署环境的主机、端口和认证信息。
    mongo_uri: str = Field(default="mongodb://localhost:27017", alias="MONGO_URI")
    # MongoDB 数据库名称。
    mongo_db_name: str = Field(default="stock_project", alias="MONGO_DB_NAME")

    # 代理池服务地址；留空时不使用外部代理池。
    proxy_51_api_url: str = Field(default="", alias="PROXY_51_API_URL")

    # 应用日志级别，例如 INFO、WARNING 或 DEBUG。
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的配置实例，避免重复解析同一个环境文件。"""
    return Settings()
