"""内置 iLink 高层客户端的组合测试。"""

from __future__ import annotations

from typing import Any

from wechat_bot_bridge.infrastructure.wechat.ilink.client import ILinkClient
from wechat_bot_bridge.infrastructure.wechat.ilink.models import (
    Credentials,
    TypingStatus,
    UploadMediaType,
)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.typing: list[TypingStatus] = []
        self.upload_types: list[UploadMediaType] = []
        self.notifications: list[str] = []

    async def get_updates(
        self,
        _credentials: Credentials,
        cursor: str,
    ) -> dict[str, Any]:
        return {
            "ret": 0,
            "get_updates_buf": f"{cursor}-next",
            "msgs": [],
        }

    async def send_message(
        self,
        _credentials: Credentials,
        message: dict[str, Any],
    ) -> None:
        self.sent.append(message)

    async def get_config(
        self,
        _credentials: Credentials,
        *,
        user_id: str,
        context_token: str,
    ) -> dict[str, Any]:
        assert user_id == "owner"
        assert context_token == "private-context"
        return {"ret": 0, "typing_ticket": "fake-ticket"}

    async def send_typing(
        self,
        _credentials: Credentials,
        *,
        user_id: str,
        typing_ticket: str,
        status: TypingStatus,
    ) -> None:
        assert user_id == "owner"
        assert typing_ticket == "fake-ticket"
        self.typing.append(status)

    async def get_upload_url(
        self,
        _credentials: Credentials,
        *,
        file_key: str,
        media_type: UploadMediaType,
        user_id: str,
        plaintext_size: int,
        plaintext_md5: str,
        ciphertext_size: int,
        aes_key_hex: str,
    ) -> dict[str, Any]:
        assert file_key
        assert user_id == "owner"
        assert plaintext_size > 0
        assert plaintext_md5
        assert ciphertext_size % 16 == 0
        assert len(aes_key_hex) == 32
        self.upload_types.append(media_type)
        return {"upload_full_url": "https://cdn.example.invalid/upload"}

    def build_cdn_upload_url(self, *, upload_param: str, file_key: str) -> str:
        raise AssertionError((upload_param, file_key))

    def build_cdn_download_url(self, *, encrypted_query_param: str) -> str:
        return f"https://cdn.example.invalid/download/{encrypted_query_param}"

    async def upload_cdn(self, *, url: str, ciphertext: bytes) -> str:
        assert url == "https://cdn.example.invalid/upload"
        assert ciphertext
        return "download-reference"

    async def download_cdn(self, *, url: str, max_bytes: int) -> bytes:
        raise AssertionError((url, max_bytes))

    async def notify_start(self, _credentials: Credentials) -> None:
        self.notifications.append("start")

    async def notify_stop(self, _credentials: Credentials) -> None:
        self.notifications.append("stop")


def _credentials() -> Credentials:
    return Credentials(
        token="fake-token",
        base_url="https://example.invalid",
        account_id="account-1",
        user_id="owner",
    )


async def test_client_sends_caption_and_image_as_protocol_items() -> None:
    transport = FakeTransport()
    client = ILinkClient(
        credentials=_credentials(),
        transport=transport,  # type: ignore[arg-type]
        media_max_bytes=1024,
    )

    await client.send_image(
        user_id="owner",
        context_token="private-context",
        data=b"image-bytes",
        caption="任务截图",
    )

    assert transport.upload_types == [UploadMediaType.IMAGE]
    assert len(transport.sent) == 2
    assert transport.sent[0]["item_list"] == [{"type": 1, "text_item": {"text": "任务截图"}}]
    image_item = transport.sent[1]["item_list"][0]
    assert image_item["type"] == 2
    assert image_item["image_item"]["media"]["encrypt_query_param"] == "download-reference"
    assert image_item["image_item"]["media"]["aes_key"]


async def test_client_sends_file_typing_updates_and_lifecycle_notifications() -> None:
    transport = FakeTransport()
    client = ILinkClient(
        credentials=_credentials(),
        transport=transport,  # type: ignore[arg-type]
        media_max_bytes=1024,
    )

    await client.send_file(
        user_id="owner",
        context_token="private-context",
        data=b"pdf",
        file_name="report.pdf",
        caption=None,
    )
    await client.set_typing(
        user_id="owner",
        context_token="private-context",
        active=True,
    )
    await client.set_typing(
        user_id="owner",
        context_token="private-context",
        active=False,
    )
    await client.notify_start()
    await client.notify_stop()
    updates = await client.get_updates("cursor")

    assert transport.upload_types == [UploadMediaType.FILE]
    file_item = transport.sent[0]["item_list"][0]["file_item"]
    assert file_item["file_name"] == "report.pdf"
    assert file_item["len"] == "3"
    assert transport.typing == [TypingStatus.TYPING, TypingStatus.CANCEL]
    assert transport.notifications == ["start", "stop"]
    assert updates.cursor == "cursor-next"
