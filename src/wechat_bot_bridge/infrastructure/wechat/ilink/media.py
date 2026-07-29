"""微信 CDN 媒体上传、下载和内容识别。"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from typing import Any, Protocol

from .crypto import (
    decode_wire_aes_key,
    decrypt_aes_ecb,
    encode_wire_aes_key,
    encrypt_aes_ecb,
)
from .errors import ILinkMediaError
from .models import (
    Credentials,
    DownloadedMedia,
    MediaKind,
    MediaReference,
    UploadedMedia,
    UploadMediaType,
)


class MediaTransport(Protocol):
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
    ) -> dict[str, Any]: ...

    def build_cdn_upload_url(self, *, upload_param: str, file_key: str) -> str: ...

    def build_cdn_download_url(self, *, encrypted_query_param: str) -> str: ...

    async def upload_cdn(self, *, url: str, ciphertext: bytes) -> str: ...

    async def download_cdn(self, *, url: str, max_bytes: int) -> bytes: ...


class MediaTransfer:
    """实现腾讯公开协议中的 AES 加密 CDN 流程。"""

    def __init__(self, transport: MediaTransport) -> None:
        self._transport = transport

    async def upload(
        self,
        *,
        credentials: Credentials,
        user_id: str,
        data: bytes,
        media_type: UploadMediaType,
    ) -> UploadedMedia:
        aes_key = os.urandom(16)
        ciphertext = encrypt_aes_ecb(data, aes_key)
        file_key = os.urandom(16).hex()
        upload_response = await self._transport.get_upload_url(
            credentials,
            file_key=file_key,
            media_type=media_type,
            user_id=user_id,
            plaintext_size=len(data),
            plaintext_md5=hashlib.md5(data, usedforsecurity=False).hexdigest(),
            ciphertext_size=len(ciphertext),
            aes_key_hex=aes_key.hex(),
        )
        upload_url = _upload_url(
            self._transport,
            payload=upload_response,
            file_key=file_key,
        )
        encrypted_query_param = await self._transport.upload_cdn(
            url=upload_url,
            ciphertext=ciphertext,
        )
        return UploadedMedia(
            encrypted_query_param=encrypted_query_param,
            aes_key=aes_key,
            plaintext_size=len(data),
            ciphertext_size=len(ciphertext),
        )

    async def download(
        self,
        reference: MediaReference,
        *,
        max_plaintext_bytes: int,
    ) -> DownloadedMedia:
        """下载单个媒体；密文最多允许比明文上限多一个 AES block。"""

        url = reference.cdn.full_url or self._transport.build_cdn_download_url(
            encrypted_query_param=reference.cdn.encrypted_query_param
        )
        encrypted_or_plain = await self._transport.download_cdn(
            url=url,
            max_bytes=max_plaintext_bytes + 16,
        )
        key_source = reference.aes_key_override or reference.cdn.aes_key
        if key_source:
            data = decrypt_aes_ecb(
                encrypted_or_plain,
                decode_wire_aes_key(key_source),
            )
        elif reference.kind is MediaKind.IMAGE:
            # 腾讯官方客户端兼容少量不带密钥的历史图片，它们由 CDN 直接返回明文。
            data = encrypted_or_plain
        else:
            raise ILinkMediaError("The media reference does not contain an AES key")
        if len(data) > max_plaintext_bytes:
            raise ILinkMediaError("CDN media exceeds the configured size limit")
        return DownloadedMedia(
            data=data,
            kind=reference.kind,
            file_name=reference.file_name,
            format=reference.format,
        )


def encode_uploaded_aes_key(uploaded: UploadedMedia) -> str:
    return encode_wire_aes_key(uploaded.aes_key)


def downloaded_media_mime(media: DownloadedMedia) -> str:
    """用内容签名识别图片，其余媒体使用受控的保守 MIME。"""

    if media.kind is MediaKind.IMAGE:
        data = media.data
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        if data.startswith(b"BM"):
            return "image/bmp"
        return "application/octet-stream"
    if media.kind is MediaKind.FILE:
        return mimetypes.guess_type(media.file_name or "")[0] or "application/octet-stream"
    if media.kind is MediaKind.VIDEO:
        return "video/mp4"
    if media.kind is MediaKind.VOICE and media.format == "silk":
        return "audio/silk"
    return "application/octet-stream"


def downloaded_media_name(media: DownloadedMedia) -> str | None:
    if media.kind is MediaKind.FILE:
        return media.file_name or "file.bin"
    if media.kind is MediaKind.VIDEO:
        return "video.mp4"
    if media.kind is MediaKind.VOICE:
        return f"voice.{media.format or 'bin'}"
    return None


def _upload_url(
    transport: MediaTransport,
    *,
    payload: dict[str, Any],
    file_key: str,
) -> str:
    full_url = payload.get("upload_full_url")
    if isinstance(full_url, str) and full_url.strip():
        return full_url.strip()
    upload_param = payload.get("upload_param")
    if isinstance(upload_param, str) and upload_param:
        return transport.build_cdn_upload_url(
            upload_param=upload_param,
            file_key=file_key,
        )
    raise ILinkMediaError("getuploadurl did not return a CDN upload URL")
