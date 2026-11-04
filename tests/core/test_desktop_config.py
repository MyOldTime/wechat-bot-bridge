"""在隔离的用户目录中验证桌面首次启动、并发创建和重启。"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import SecretStr, ValidationError

from wechat_bot_bridge.core.desktop_config import DesktopConfigError, DesktopSettings
from wechat_bot_bridge.core.paths import desktop_config_file


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profile = tmp_path / "user"
    monkeypatch.setenv("LOCALAPPDATA", str(profile))
    monkeypatch.setenv("XDG_DATA_HOME", str(profile))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    return desktop_config_file()


def test_first_run_ignores_dotenv_and_reuses_generated_login(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = tmp_path / "program"
    program.mkdir()
    monkeypatch.setattr(sys, "executable", str(program / "bridge.exe"))
    monkeypatch.chdir(tmp_path)
    # 即使只读取而不应用这两份 dotenv，也会因无效 UTF-8 而失败。
    (tmp_path / ".env").write_bytes(b"\xff")
    (program / ".env").write_bytes(b"\xff")
    monkeypatch.setenv("WBB_PORT", "invalid")
    monkeypatch.setenv("WBB_WEB_PASSWORD", "ignored-environment-example")
    assert not config_path.parent.exists()
    first = DesktopSettings()
    assert first.port == 8000
    assert first.web_username == "admin"
    assert re.fullmatch(r"[a-zA-Z0-9_-]{32,}", first.web_password.get_secret_value())
    assert first.web_password != SecretStr("ignored-environment-example")
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert SecretStr(saved["WBB_WEB_PASSWORD"]) == first.web_password
    assert DesktopSettings().web_password == first.web_password
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "another-user"))
    assert DesktopSettings().web_password != first.web_password


def test_concurrent_first_launch_publishes_one_complete_password(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    barrier = Barrier(6)

    def start() -> SecretStr:
        barrier.wait(timeout=5)
        return DesktopSettings().web_password

    with ThreadPoolExecutor(max_workers=6) as pool:
        tasks = [pool.submit(start) for _ in range(6)]
        passwords = {task.result(timeout=10) for task in tasks}
    assert passwords == {DesktopSettings().web_password}
    assert list(config_path.parent.iterdir()) == [config_path]


def test_existing_config_controls_port_password_and_data_paths(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path.parent.mkdir(parents=True)
    data = tmp_path / "existing data"
    values = {
        "WBB_WEB_USERNAME": "operator",
        "WBB_WEB_PASSWORD": "file-only-example",
        "WBB_PORT": 8128,
        "WBB_DATA_DIR": str(data),
        "WBB_CREDENTIALS_PATH": str(data / "custom-auth.json"),
    }
    config_path.write_text(json.dumps(values), encoding="utf-8-sig")
    monkeypatch.setenv("WBB_PORT", "9999")
    settings = DesktopSettings()
    assert settings.port == 8128
    assert settings.web_username == "operator"
    assert settings.web_password == SecretStr("file-only-example")
    assert settings.database_path == data / "wechat-bridge.db"
    assert settings.credentials_path == data / "custom-auth.json"


@pytest.mark.parametrize(
    "contents",
    ["{broken", "[]", "{}", '{"WBB_WEB_PASSWORD":"short"}', '{"WBB_WEB_PASSWORD":null}'],
)
def test_invalid_config_is_preserved(config_path: Path, contents: str) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text(contents, encoding="utf-8")
    with pytest.raises((DesktopConfigError, ValidationError)):
        DesktopSettings()
    assert config_path.read_text(encoding="utf-8") == contents
