"""CDN 媒体下载兼容性测试。"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from wechat_bot_bridge.infrastructure.wechat.ilink.crypto import encrypt_aes_ecb
from wechat_bot_bridge.infrastructure.wechat.ilink.errors import ILinkMediaError
from wechat_bot_bridge.infrastructure.wechat.ilink.media import MediaTransfer
from wechat_bot_bridge.infrastructure.wechat.ilink.models import (
    CdnMedia,
    Credentials,
    MediaKind,
    MediaReference,
    UploadMediaType,
)


class DownloadTransport:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.requested_limit: int | None = None

    async def get_upload_url(
        self,
        credentials: Credentials,
        *,
        file_key: str,
        media_type: UploadMediaType,
        user_id: str,
        plaintext_size: int,
        plaintext_md5: str,
        ciphertext_size: int,
        aes_key_hex: str,
    ) -> dict[str, Any]:
        raise AssertionError(credentials)

    def build_cdn_upload_url(self, *, upload_param: str, file_key: str) -> str:
        raise AssertionError((upload_param, file_key))

    def build_cdn_download_url(self, *, encrypted_query_param: str) -> str:
        return f"https://cdn.example.invalid/download/{encrypted_query_param}"

    async def upload_cdn(self, *, url: str, ciphertext: bytes) -> str:
        raise AssertionError((url, ciphertext))

    async def download_cdn(self, *, url: str, max_bytes: int) -> bytes:
        assert url.startswith("https://cdn.example.invalid/")
        self.requested_limit = max_bytes
        return self.content


async def test_download_decrypts_base64_raw_key_and_applies_padding_allowance() -> None:
    key = bytes(range(16))
    plaintext = b"\x89PNG\r\n\x1a\npayload"
    transport = DownloadTransport(encrypt_aes_ecb(plaintext, key))
    transfer = MediaTransfer(transport)

    result = await transfer.download(
        MediaReference(
            kind=MediaKind.IMAGE,
            cdn=CdnMedia(
                encrypted_query_param="reference",
                aes_key=base64.b64encode(key).decode(),
            ),
        ),
        max_plaintext_bytes=64,
    )

    assert result.data == plaintext
    assert result.kind is MediaKind.IMAGE
    assert transport.requested_limit == 80


async def test_historical_plain_image_without_key_is_supported() -> None:
    transport = DownloadTransport(b"plain-image")
    transfer = MediaTransfer(transport)

    result = await transfer.download(
        MediaReference(
            kind=MediaKind.IMAGE,
            cdn=CdnMedia(encrypted_query_param="reference", aes_key=""),
        ),
        max_plaintext_bytes=64,
    )

    assert result.data == b"plain-image"


async def test_non_image_without_key_is_rejected() -> None:
    transfer = MediaTransfer(DownloadTransport(b"plain-file"))

    with pytest.raises(ILinkMediaError, match="AES key"):
        await transfer.download(
            MediaReference(
                kind=MediaKind.FILE,
                cdn=CdnMedia(encrypted_query_param="reference", aes_key=""),
                file_name="report.pdf",
            ),
            max_plaintext_bytes=64,
        )
