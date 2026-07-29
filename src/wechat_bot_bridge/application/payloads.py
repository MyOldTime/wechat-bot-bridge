"""出站载荷的统一业务校验。"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import (
    FilePayload,
    ImagePayload,
    OutboundPayload,
    TextPayload,
    TypingCancelPayload,
    TypingStartPayload,
)


@dataclass(frozen=True, slots=True)
class PayloadLimits:
    """部署配置决定的文字与媒体上限。"""

    message_max_chars: int
    media_max_bytes: int


def validate_outbound_payload(payload: OutboundPayload, limits: PayloadLimits) -> None:
    """校验所有入口共用的出站约束。

    HTTP schema 负责请求形状和绝对硬上限；这里负责部署级上限以及领域层仍需
    保证的空白内容规则，避免管理页面或未来非 HTTP 调用绕过校验。
    """

    text_fields: list[tuple[str, str]] = []
    media: bytes | None = None

    if isinstance(payload, TextPayload):
        text_fields.append(("text", payload.text))
    elif isinstance(payload, ImagePayload):
        media = payload.data
        if payload.caption is not None:
            text_fields.append(("caption", payload.caption))
    elif isinstance(payload, FilePayload):
        media = payload.data
        if not payload.file_name.strip():
            raise ValueError("file_name cannot be blank")
        if payload.caption is not None:
            text_fields.append(("caption", payload.caption))
    elif isinstance(payload, (TypingStartPayload, TypingCancelPayload)):
        return

    _validate_text_fields(text_fields, max_chars=limits.message_max_chars)
    _validate_media(media, max_bytes=limits.media_max_bytes)


def _validate_text_fields(
    fields: list[tuple[str, str]],
    *,
    max_chars: int,
) -> None:
    for field_name, value in fields:
        if not value.strip():
            raise ValueError(f"{field_name} cannot be blank")
        if len(value) > max_chars:
            raise ValueError(f"{field_name} exceeds the configured {max_chars} character limit")


def _validate_media(media: bytes | None, *, max_bytes: int) -> None:
    if media is None:
        return
    if not media:
        raise ValueError("Media content cannot be empty")
    if len(media) > max_bytes:
        raise ValueError(f"Media content exceeds the configured {max_bytes} byte limit")
