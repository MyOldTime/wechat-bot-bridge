"""从 WBB_* 环境变量加载应用配置。"""

from __future__ import annotations

from pathlib import Path

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import (
    default_credentials_path,
    default_data_directory,
    default_database_path,
    default_environment_file,
)


class Settings(BaseSettings):
    """经过校验的进程级运行配置。

    MCP Token 和消息转发参数由管理页面维护并持久化到服务配置表，
    不属于环境变量配置。
    """

    model_config = SettingsConfigDict(
        env_prefix="WBB_",
        env_file=default_environment_file(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

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
    mcp_public_origin: AnyHttpUrl | None = None
    log_level: str = "info"

    data_dir: Path = Field(default_factory=default_data_directory)
    database_path: Path = Field(default_factory=default_database_path)
    credentials_path: Path = Field(default_factory=default_credentials_path)

    inbound_queue_size: int = Field(default=1000, ge=1, le=100_000)
    inbound_media_download_timeout_seconds: float = Field(default=30, gt=0, le=60)

    message_max_chars: int = Field(default=16_000, ge=1, le=100_000)
    media_max_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1,
        le=25 * 1024 * 1024,
    )

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

    @field_validator("mcp_public_origin")
    @classmethod
    def validate_mcp_origin(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value and (
            value.path not in {None, "/"}
            or any(
                part is not None
                for part in (value.username, value.password, value.query, value.fragment)
            )
            or "*" in (value.host or "")
        ):
            raise ValueError("WBB_MCP_PUBLIC_ORIGIN must be an exact HTTP(S) origin")
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

    def prepare_directories(self) -> None:
        """只创建当前进程应当持有的目录。"""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
