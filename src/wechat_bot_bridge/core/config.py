"""从 WBB_* 环境变量加载应用配置。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """经过校验的运行配置。

    API Key 刻意不提供可用默认值，避免服务因误配置而在无认证状态下
    暴露消息发送能力。
    """

    model_config = SettingsConfigDict(
        env_prefix="WBB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    api_key: SecretStr
    web_username: str = Field(
        default="admin",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    web_password: SecretStr | None = None
    web_token_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    web_typing_timeout_seconds: int = Field(default=5, ge=1, le=30)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "info"

    data_dir: Path = Path("data")
    database_path: Path = Path("data/wechat-bridge.db")
    credentials_path: Path = Path("data/wechat/credentials.json")

    inbound_webhook_url: HttpUrl | None = None
    inbound_webhook_secret: SecretStr | None = None
    inbound_queue_size: int = Field(default=1000, ge=1, le=100_000)
    inbound_media_download_timeout_seconds: float = Field(default=30, gt=0, le=60)
    webhook_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    webhook_max_attempts: int = Field(default=3, ge=1, le=10)

    message_max_chars: int = Field(default=16_000, ge=1, le=100_000)
    media_max_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1,
        le=25 * 1024 * 1024,
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if len(secret) < 16:
            raise ValueError("WBB_API_KEY must contain at least 16 characters")
        if secret == "replace-with-a-long-random-secret":
            raise ValueError("WBB_API_KEY must not use the example value")
        return value

    @field_validator("web_password")
    @classmethod
    def validate_web_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        if len(secret) < 12:
            raise ValueError("WBB_WEB_PASSWORD must contain at least 12 characters")
        if secret == "replace-with-a-different-long-password":
            raise ValueError("WBB_WEB_PASSWORD must not use the example value")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"critical", "error", "warning", "info", "debug", "trace"}:
            raise ValueError(
                "WBB_LOG_LEVEL must be one of critical, error, warning, info, debug or trace"
            )
        return normalized

    @model_validator(mode="after")
    def validate_webhook(self) -> Settings:
        if self.inbound_webhook_url and not self.inbound_webhook_secret:
            raise ValueError(
                "WBB_INBOUND_WEBHOOK_SECRET is required when WBB_INBOUND_WEBHOOK_URL is configured"
            )
        return self

    def prepare_directories(self) -> None:
        """只创建当前进程应当持有的目录。"""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
