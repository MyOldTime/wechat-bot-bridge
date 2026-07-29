"""配置测试，绝不读取开发者的真实环境。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from wechat_bot_bridge.core.config import Settings

VALID_API_KEY = "unit-test-api-key-123456"


def test_settings_defaults_are_safe() -> None:
    settings = Settings(api_key=VALID_API_KEY, _env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.media_max_bytes == 20 * 1024 * 1024
    assert settings.inbound_webhook_url is None
    assert settings.inbound_webhook_secret is None
    assert settings.web_username == "admin"
    assert settings.web_password is None
    assert settings.web_token_ttl_seconds == 3600
    assert settings.web_typing_timeout_seconds == 5
    assert settings.inbound_media_download_timeout_seconds == 30


def test_settings_load_wbb_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state" / "bridge.db"
    credentials_path = tmp_path / "auth" / "credentials.json"
    monkeypatch.setenv("WBB_API_KEY", VALID_API_KEY)
    monkeypatch.setenv("WBB_PORT", "8123")
    monkeypatch.setenv("WBB_MEDIA_MAX_BYTES", "1048576")
    monkeypatch.setenv("WBB_WEB_USERNAME", "bridge-admin")
    monkeypatch.setenv("WBB_WEB_PASSWORD", "separate-web-password")
    monkeypatch.setenv("WBB_WEB_TYPING_TIMEOUT_SECONDS", "9")
    monkeypatch.setenv("WBB_INBOUND_MEDIA_DOWNLOAD_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("WBB_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("WBB_CREDENTIALS_PATH", str(credentials_path))
    settings = Settings(_env_file=None)

    assert settings.api_key.get_secret_value() == VALID_API_KEY
    assert settings.port == 8123
    assert settings.media_max_bytes == 1_048_576
    assert settings.web_username == "bridge-admin"
    assert settings.web_password is not None
    assert settings.web_password.get_secret_value() == "separate-web-password"
    assert settings.web_typing_timeout_seconds == 9
    assert settings.inbound_media_download_timeout_seconds == 12.5
    assert settings.database_path == database_path
    assert settings.credentials_path == credentials_path


@pytest.mark.parametrize(
    "api_key",
    ["", "too-short", "replace-with-a-long-random-secret"],
)
def test_settings_reject_invalid_api_key(api_key: str) -> None:
    with pytest.raises(ValidationError):
        Settings(api_key=api_key, _env_file=None)


@pytest.mark.parametrize(
    "web_password",
    ["", "too-short", "replace-with-a-different-long-password"],
)
def test_settings_reject_invalid_management_password(web_password: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            api_key=VALID_API_KEY,
            web_password=web_password,
            _env_file=None,
        )


def test_settings_requires_webhook_secret_when_url_is_set() -> None:
    with pytest.raises(ValidationError, match="WBB_INBOUND_WEBHOOK_SECRET"):
        Settings(
            api_key=VALID_API_KEY,
            inbound_webhook_url="https://webhook.invalid/inbound",
            _env_file=None,
        )


def test_settings_accepts_webhook_with_nonempty_secret() -> None:
    settings = Settings(
        api_key=VALID_API_KEY,
        inbound_webhook_url="https://webhook.invalid/inbound",
        inbound_webhook_secret="webhook-unit-test-secret",
        _env_file=None,
    )

    assert str(settings.inbound_webhook_url) == "https://webhook.invalid/inbound"
    assert settings.inbound_webhook_secret is not None
    assert settings.inbound_webhook_secret.get_secret_value() == "webhook-unit-test-secret"


def test_settings_rejects_media_limit_above_api_hard_limit() -> None:
    with pytest.raises(ValidationError):
        Settings(
            api_key=VALID_API_KEY,
            media_max_bytes=25 * 1024 * 1024 + 1,
            _env_file=None,
        )


def test_settings_normalizes_and_validates_log_level() -> None:
    settings = Settings(
        api_key=VALID_API_KEY,
        log_level=" WARNING ",
        _env_file=None,
    )

    assert settings.log_level == "warning"
    with pytest.raises(ValidationError, match="WBB_LOG_LEVEL"):
        Settings(
            api_key=VALID_API_KEY,
            log_level="verbose",
            _env_file=None,
        )


def test_prepare_directories_creates_only_configured_parents(tmp_path: Path) -> None:
    settings = Settings(
        api_key=VALID_API_KEY,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "database" / "bridge.db",
        credentials_path=tmp_path / "credentials" / "credentials.json",
        _env_file=None,
    )

    settings.prepare_directories()

    assert settings.data_dir.is_dir()
    assert settings.database_path.parent.is_dir()
    assert settings.credentials_path.parent.is_dir()
    assert not settings.database_path.exists()
    assert not settings.credentials_path.exists()
