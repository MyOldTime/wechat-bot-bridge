"""iLink 授权文件和二维码登录测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wechat_bot_bridge.infrastructure.wechat.ilink import auth
from wechat_bot_bridge.infrastructure.wechat.ilink.auth import (
    CredentialStore,
    QRAuthenticator,
)
from wechat_bot_bridge.infrastructure.wechat.ilink.errors import ILinkAuthError
from wechat_bot_bridge.infrastructure.wechat.ilink.models import Credentials


class FakeLoginTransport:
    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        self._statuses = list(statuses)
        self.local_tokens: list[str] | None = None
        self.poll_calls: list[tuple[str, str | None]] = []

    async def get_qr_code(self, *, local_tokens: list[str]) -> dict[str, Any]:
        self.local_tokens = local_tokens
        return {
            "qrcode": "opaque-qr-id",
            "qrcode_img_content": "https://weixin.qq.com/x/fake",
        }

    async def poll_qr_status(
        self,
        *,
        base_url: str,
        qrcode: str,
        verify_code: str | None = None,
    ) -> dict[str, Any]:
        assert qrcode == "opaque-qr-id"
        self.poll_calls.append((base_url, verify_code))
        return self._statuses.pop(0)


async def test_credential_store_reads_legacy_sdk_format_and_writes_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wechat" / "credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        """
{
  "token": "fake-token",
  "baseUrl": "https://example.invalid",
  "accountId": "account-1",
  "userId": "user-1",
  "savedAt": "2026-07-29T00:00:00+00:00"
}
""".strip(),
        encoding="utf-8",
    )
    store = CredentialStore(path)

    loaded = await store.load()
    assert loaded is not None
    assert loaded.account_id == "account-1"
    assert loaded.user_id == "user-1"

    await store.save(
        Credentials(
            token="new-fake-token",
            base_url="https://example.invalid",
            account_id="account-2",
            user_id="user-2",
        )
    )
    saved = path.read_text(encoding="utf-8")
    assert '"baseUrl": "https://example.invalid"' in saved
    assert '"accountId": "account-2"' in saved
    assert list(path.parent.glob(".*.tmp")) == []


async def test_qr_authenticator_supports_redirect_and_pairing_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(auth.asyncio, "sleep", no_sleep)
    transport = FakeLoginTransport(
        [
            {
                "status": "scaned_but_redirect",
                "redirect_host": "sz.ilinkai.weixin.qq.com",
            },
            {"status": "need_verifycode"},
            {"status": "scaned"},
            {
                "status": "confirmed",
                "bot_token": "new-fake-token",
                "ilink_bot_id": "account-1",
                "ilink_user_id": "user-1",
                "baseurl": "https://sz.ilinkai.weixin.qq.com",
            },
        ]
    )
    scanned: list[bool] = []
    authenticator = QRAuthenticator(
        transport=transport,
        credentials=CredentialStore(tmp_path / "credentials.json"),
        on_scanned=lambda: scanned.append(True),
        on_verify_code=lambda _retry: "123456",
    )

    credentials = await authenticator.authorize(force=True)

    assert credentials.token == "new-fake-token"
    assert credentials.base_url == "https://sz.ilinkai.weixin.qq.com"
    assert transport.poll_calls == [
        ("https://ilinkai.weixin.qq.com", None),
        ("https://sz.ilinkai.weixin.qq.com", None),
        ("https://sz.ilinkai.weixin.qq.com", "123456"),
        ("https://sz.ilinkai.weixin.qq.com", None),
    ]
    assert scanned == [True]


async def test_existing_credentials_skip_network_unless_force_is_requested(
    tmp_path: Path,
) -> None:
    store = CredentialStore(tmp_path / "credentials.json")
    existing = Credentials(
        token="existing-fake-token",
        base_url="https://example.invalid",
        account_id="account-1",
        user_id="user-1",
    )
    await store.save(existing)
    transport = FakeLoginTransport([])
    authenticator = QRAuthenticator(transport=transport, credentials=store)

    assert await authenticator.authorize() == existing
    assert transport.local_tokens is None


async def test_qr_authenticator_accepts_async_web_verification_provider(
    tmp_path: Path,
) -> None:
    transport = FakeLoginTransport(
        [
            {"status": "need_verifycode"},
            {
                "status": "confirmed",
                "bot_token": "new-fake-token",
                "ilink_bot_id": "account-1",
                "ilink_user_id": "user-1",
                "baseurl": "https://sz.ilinkai.weixin.qq.com",
            },
        ]
    )
    requests: list[bool] = []

    async def provide_code(retry: bool) -> str:
        requests.append(retry)
        return "654321"

    authenticator = QRAuthenticator(
        transport=transport,
        credentials=CredentialStore(tmp_path / "credentials.json"),
        on_verify_code=provide_code,
    )

    credentials = await authenticator.authorize(force=True)

    assert credentials.token == "new-fake-token"
    assert requests == [False]
    assert transport.poll_calls[-1] == ("https://ilinkai.weixin.qq.com", "654321")


async def test_qr_authenticator_never_falls_back_to_blocking_terminal_input(
    tmp_path: Path,
) -> None:
    transport = FakeLoginTransport([{"status": "need_verifycode"}])
    authenticator = QRAuthenticator(
        transport=transport,
        credentials=CredentialStore(tmp_path / "credentials.json"),
    )

    with pytest.raises(ILinkAuthError, match="management verification code provider"):
        await authenticator.authorize(force=True)


async def test_invalid_credentials_fail_without_echoing_secret(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text('{"token":"must-not-leak"}', encoding="utf-8")

    with pytest.raises(ILinkAuthError) as caught:
        await CredentialStore(path).load()

    assert "must-not-leak" not in str(caught.value)
