"""Windows 桌面配置。首次原子发布，后续启动只读用户保存的文件。"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .config import Settings
from .paths import desktop_config_file


class DesktopConfigError(Exception):
    """可以向本机用户显示的配置错误，不包含配置值。"""


def _create_desktop_config(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    initial = {
        "WBB_WEB_USERNAME": "admin",
        "WBB_WEB_PASSWORD": secrets.token_urlsafe(32),
        "WBB_HOST": "127.0.0.1",
        "WBB_PORT": 8000,
    }
    temp = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(temp.name)
    try:
        with temp:
            json.dump(initial, temp, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        # 两个首次启动者只能发布一个完整的密码文件，不覆盖先到的配置。
        if os.name == "nt":
            os.rename(temporary, path)  # Windows 在目标已存在时抛出 FileExistsError。
        else:
            os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)


def _read_desktop_config(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            _create_desktop_config(path)
        values = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DesktopConfigError(
            f"无法读取或创建配置文件。请检查文件权限和 JSON 格式。\n{path}"
        ) from None
    if not isinstance(values, dict):
        raise DesktopConfigError(f"配置文件应包含一个 JSON 对象。\n{path}")
    return values


class DesktopSettings(Settings):
    """使用同一套业务校验，桌面启动参数只来自用户配置和程序默认值。"""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="forbid",
        alias_generator=lambda name: f"WBB_{name.upper()}",
        hide_input_in_errors=True,
    )
    web_password: SecretStr = Field()
    database_path: Path = Field(default_factory=lambda data: data["data_dir"] / "wechat-bridge.db")
    credentials_path: Path = Field(
        default_factory=lambda data: data["data_dir"] / "wechat" / "credentials.json"
    )

    @field_validator("data_dir", "database_path", "credentials_path")
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Desktop storage paths must be absolute")
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        values = _read_desktop_config(desktop_config_file())
        return (InitSettingsSource(settings_cls, values),)
