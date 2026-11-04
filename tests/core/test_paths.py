"""源码和冻结发布必须使用可预测且彼此隔离的路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

from wechat_bot_bridge.core import paths


def test_frozen_windows_data_uses_local_app_data(tmp_path: Path) -> None:
    local_app_data = tmp_path / "local"

    result = paths._frozen_data_directory(
        platform="win32",
        environment={"LOCALAPPDATA": str(local_app_data)},
        executable_dir=tmp_path / "program",
        home=tmp_path / "home",
    )

    assert result == local_app_data / "wechat-bot-bridge"


def test_frozen_windows_data_falls_back_beside_executable(tmp_path: Path) -> None:
    executable_dir = tmp_path / "program"

    result = paths._frozen_data_directory(
        platform="win32",
        environment={},
        executable_dir=executable_dir,
        home=tmp_path / "home",
    )

    assert result == executable_dir / "data"


def test_frozen_environment_file_is_beside_user_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "release" / "wechat-bot-bridge.exe"
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(paths.sys, "executable", str(executable))

    assert paths.default_environment_file() == executable.parent / ".env"


def test_source_defaults_remain_relative_to_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paths, "is_frozen", lambda: False)

    assert paths.default_environment_file() == Path(".env")
    assert paths.default_data_directory() == Path("data")
    assert paths.default_database_path() == Path("data/wechat-bridge.db")
    assert paths.default_credentials_path() == Path("data/wechat/credentials.json")
