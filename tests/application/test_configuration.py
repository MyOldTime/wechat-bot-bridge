"""服务级凭据和单向转发配置测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from wechat_bot_bridge.application.configuration import (
    OutboundForwardingConfigurationService,
    PublicApiCredentialService,
)
from wechat_bot_bridge.domain.models import ForwardingScheme, OutboundForwardingConfig
from wechat_bot_bridge.infrastructure.persistence.sqlite import SQLiteStore, utc_now


async def _open_store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    await store.open()
    return store


async def test_generated_credentials_persist_and_survive_account_clear(tmp_path: Path) -> None:
    database_path = tmp_path / "bridge.db"
    store = await _open_store(database_path)
    token_service = PublicApiCredentialService(store)
    forwarding_service = OutboundForwardingConfigurationService(store)
    token = await token_service.initialize()
    forwarding = await forwarding_service.initialize()
    updated = await forwarding_service.update(
        enabled=True,
        scheme=ForwardingScheme.HTTPS,
        host="::1",
        port=9443,
        path="/gateway/events",
        secret=None,
        timeout_seconds=5,
        max_attempts=2,
    )
    await store.clear_all()
    await store.close()

    reopened = await _open_store(database_path)
    try:
        restored_token = await PublicApiCredentialService(reopened).initialize()
        restored_forwarding = await OutboundForwardingConfigurationService(reopened).initialize()

        assert restored_token == token
        assert restored_forwarding == updated
        assert restored_forwarding.secret == forwarding.secret
        assert restored_forwarding.url == "https://[::1]:9443/gateway/events"
        assert token.token not in repr(token)
        assert forwarding.secret not in repr(restored_forwarding)
    finally:
        await reopened.close()


async def test_token_and_forwarding_secret_rotation_invalidates_old_values(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    try:
        token_service = PublicApiCredentialService(store)
        forwarding_service = OutboundForwardingConfigurationService(store)
        old_token = await token_service.initialize()
        old_forwarding = await forwarding_service.initialize()

        new_token = await token_service.rotate()
        new_forwarding = await forwarding_service.rotate_secret()

        assert new_token.token != old_token.token
        assert await token_service.matches(old_token.token) is False
        assert await token_service.matches(new_token.token) is True
        assert new_forwarding.secret != old_forwarding.secret
        assert new_forwarding.updated_at >= old_forwarding.updated_at
    finally:
        await store.close()


@pytest.mark.parametrize(
    ("host", "path"),
    [
        ("http://example.com", "/events"),
        ("user@example.com", "/events"),
        ("example.com", "events"),
        ("example.com", "/events?token=secret"),
    ],
)
def test_forwarding_target_rejects_ambiguous_url_parts(host: str, path: str) -> None:
    with pytest.raises(ValidationError):
        OutboundForwardingConfig(
            host=host,
            path=path,
            secret="x" * 32,
            updated_at=utc_now(),
        )
