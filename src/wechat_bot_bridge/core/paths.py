"""源码运行与冻结发布共用的路径策略。"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

APPLICATION_DIRECTORY = "wechat-bot-bridge"


def is_frozen() -> bool:
    """判断当前进程是否由 PyInstaller 等冻结启动器运行。"""

    return bool(getattr(sys, "frozen", False))


def executable_directory() -> Path:
    """返回用户实际启动的 EXE 所在目录，而不是 one-file 临时解包目录。"""

    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def default_environment_file() -> Path | None:
    """只有源码运行时从当前目录加载 .env。"""

    return None if is_frozen() else Path(".env")


def desktop_config_file() -> Path:
    """Windows 用户配置独立于 EXE 位置、启动目录和可自定义的数据目录。"""

    root = Path(os.environ.get("LOCALAPPDATA", ""))
    if not root.is_absolute():
        root = Path.home() / "AppData" / "Local"
    return root / APPLICATION_DIRECTORY / "config" / "settings.json"


def _frozen_data_directory(
    *,
    platform: str,
    environment: Mapping[str, str],
    executable_dir: Path,
    home: Path,
) -> Path:
    """计算冻结程序的外部可写目录，参数化以便跨平台测试。"""

    if platform == "win32":
        local_app_data = environment.get("LOCALAPPDATA")
        if local_app_data and Path(local_app_data).is_absolute():
            return Path(local_app_data) / APPLICATION_DIRECTORY
        return executable_dir / "data"
    if platform == "darwin":
        return home / "Library" / "Application Support" / APPLICATION_DIRECTORY
    xdg_data_home = environment.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / APPLICATION_DIRECTORY
    return home / ".local" / "share" / APPLICATION_DIRECTORY


def default_data_directory() -> Path:
    """返回默认持久化根目录；冻结程序绝不写入 one-file 临时目录。"""

    if not is_frozen():
        return Path("data")
    return _frozen_data_directory(
        platform=sys.platform,
        environment=os.environ,
        executable_dir=executable_directory(),
        home=Path.home(),
    )


def default_database_path() -> Path:
    """返回默认 SQLite 路径。"""

    return default_data_directory() / "wechat-bridge.db"


def default_credentials_path() -> Path:
    """返回默认微信授权文件路径。"""

    return default_data_directory() / "wechat" / "credentials.json"
